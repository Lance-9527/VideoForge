"""
VideoForge · Agent 工具注册表

设计取舍（为什么不直接用 provider 原生 function calling）：
    VideoForge 支持 9 个 LLM provider（DeepSeek / Qwen / GLM / Kimi / MiniMax /
    豆包 / 混元 / Ollama / OpenAI 兼容），各家对 function calling 的支持程度
    参差不齐。若依赖原生 FC，换 provider 就废。

    因此采用 **JSON 动作协议**：模型在回复里输出

        {"tool": "plan_shots", "args": {"project_id": "p1"}}

    由 agent 循环解析 → 执行 → 把结果回灌成下一条 user 消息 → 继续。
    纯文本回复则直接作为最终答复。这对所有 OpenAI 兼容接口都有效。

工具本身**只做参数校验 + 委派**，业务逻辑全部复用 VideoForge 现有服务，
不在这里重写（避免与 main.py 的路由逻辑分叉）。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger("videoforge.agent.tools")

# 工具返回统一结构
ToolResult = Dict[str, Any]


def ok(**kw: Any) -> ToolResult:
    """成功结果：`{"ok": True, **kw}`。

    ★★ 2026-09-26 加了一道防线：`kw` 里**不能**再出现 `"ok"`。
    `{"ok": True, **kw}` 在 Python 里遇到重复键是**后写的覆盖先写的**，
    于是 `ok(**some_dict)`（而 some_dict 自带 `"ok": False`）会让返回值变成
    `"ok": False` —— 调用方看到的是"工具失败"，而实际是"工具成功、内容里
    有发现"。

    这不是假想：`t_audit_character_image` / `t_audit_character_batch`
    原来写的就是 `ok(..., **audit)`，于是**角色体检只要查出任何缺口或
    与剧本的冲突，工具就被报成调用失败** —— 而那正是报告最有价值的时候。

    现在把这种覆盖改成**响亮的告警**（不抛异常，避免把"报告有内容"
    升级成"工具崩了"）。这条防线对所有工具都生效，包括以后新写的。
    """
    if "ok" in kw and not kw.get("ok"):
        # 只在**真的会翻转**时告警：kw["ok"] 为假 → True 会变成 False。
        # 若两边同值（例如 mcp_call 的失败路径把 ok=False 又传进来一次），
        # 那只是写法不干净，不影响语义，不吵。
        logger.warning(
            "工具成功结果里出现了会**翻转**的 'ok' 键：调用方把含 \"ok\" 的字典"
            "直接展开了（`ok(**d)`），内层的值覆盖掉了 True —— 于是\"有发现\"会被"
            "上层读成\"调用失败\"。请把它嵌到子键下（例如 `audit=d`），"
            "或改名（例如 `batch_ok=d['ok']`）。当前值=%r", kw.get("ok"))
    return {"ok": True, **kw}


def merged(base: Optional[Dict[str, Any]] = None, **explicit: Any) -> Dict[str, Any]:
    """把 `explicit` 合并到 `base` 上（**explicit 优先**），供 `ok(**...)` 展开。

    ★★ 2026-09-27 为什么必须有这个函数：`ok(**d, how=how)` 这种写法，
    只要 `d` 里**也有** `"how"`，Python 就会在**调用 `ok()` 之前**抛

        TypeError: ok() got multiple values for keyword argument 'how'

    —— `ok()` 自己根本来不及兜（异常发生在调用方的帧里），
    而外层工具普遍写着 `except Exception as e: return err(f"...：{e}")`，
    于是这个 TypeError 被吞成一句看着挺合理的话。

    实测代价：`check_kling_mcp`（查可灵免 key 通道）**永远失败**，
    用户看到的是「查可灵 MCP 失败：TypeError…」，而不是真实授权状态；
    `kling_mcp_auth` 里还潜伏着同款的第二处（只在"已授权"时才触发）。
    `K.status()` 返回的字典里**天生带一个 `"how"` 键**，
    而这两个工具又都想自己给一句更好懂的 `how` —— 必然撞。

    合并成一个 dict 再展开，这一整类问题就不存在了：
    `ok(**merged(st, how=how, usable=True))`。

    （静态检查 `tests/test_module_shape.py` 会禁止再写出
    `ok(**d, key=value)` 这种裸展开写法。）
    """
    out: Dict[str, Any] = dict(base or {})
    out.update(explicit)
    return out


def err(message: str, **kw: Any) -> ToolResult:
    """失败结果：`{"ok": False, "error": message, **kw}`。

    ★ 与 `ok()` 同理：`kw` 里若带真的 `"ok": True`，会覆盖掉这里的 `False` ——
    那会让"工具明确报错"被读成"成功"。同样是后写的覆盖先写的。
    """
    if kw.get("ok") is True:
        logger.warning(
            "工具错误结果里出现了会**翻转**的 'ok' 键：内层的 True 会覆盖掉 False，"
            "把\"明确报错\"读成\"成功\"。请改名。")
    return {"ok": False, "error": str(message), **kw}


# ──────────────────────── 注册表 ────────────────────────


@dataclass
class ToolSpec:
    name: str
    description: str
    params: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # name -> {type, desc, required}
    needs_project: bool = False      # 是否自动注入 project_id
    # 是否有副作用。
    # ★★ 2026-09-26 更正：这个标记**目前没有任何代码读它** ——
    #   `signature_text` / `doc_text` / `to_json_schema` 三条序列化路径都不含
    #   `writes`，`agent.py` 的 `AgentStep` 也不带它，所以标 `writes=True`
    #   **不会**让前端弹确认。代码强制的闸门是 `stages.GATED_TOOLS`
    #   （见 `core/agent.py::_gate_check`，以及 `tests/test_safety_posture.py`）。
    #   ★ 第 27 轮补记：`_gate_check` 的**第一行**是
    #     `if name not in GATED_TOOLS: return None` —— 名单之外的工具闸门
    #     连看都不看，所以「没确认就不许花钱」这个安全性质**完全等于**
    #     `GATED_TOOLS` 完不完整。而上面那句指向的
    #     `tests/test_safety_posture.py` 在第 27 轮去读时**报 not found**
    #     （文件根本不存在，一句指向不存在文件的注释比没有注释更糟）。
    #     现在它存在了，守三件事：名单里的名字都真实存在／名单里的都真被拦／
    #     名字像会花钱的工具都在名单里；并把未上闸门的全量清单打出来供人工复核。
    #     ⇒ **新加一个付费工具时，必须同时把它加进 `GATED_TOOLS`**，否则那个测试会红。
    #   别把它当安全机制用 —— `agent_tools_mcp.py` 的文件头一度就是这么写错的。
    writes: bool = False
    handler: Optional[Callable[..., Awaitable[ToolResult]]] = None

    def signature_text(self) -> str:
        """给模型看的紧凑签名。"""
        if not self.params:
            return f"{self.name}()"
        bits = []
        for pname, meta in self.params.items():
            t = meta.get("type", "string")
            req = "" if meta.get("required") else "?"
            bits.append(f"{pname}{req}: {t}")
        return f"{self.name}({', '.join(bits)})"

    def doc_text(self) -> str:
        """给模型看的完整说明。"""
        lines = [f"- {self.signature_text()}", f"    {self.description}"]
        for pname, meta in self.params.items():
            flag = "必填" if meta.get("required") else "可选"
            lines.append(f"      · {pname} ({meta.get('type','string')}, {flag}): {meta.get('desc','')}")
        return "\n".join(lines)

    def to_json_schema(self) -> Dict[str, Any]:
        """标准 JSON Schema（给支持原生 FC 的 provider 备用）。"""
        props: Dict[str, Any] = {}
        required: List[str] = []
        for pname, meta in self.params.items():
            props[pname] = {
                "type": meta.get("type", "string"),
                "description": meta.get("desc", ""),
            }
            if meta.get("enum"):
                props[pname]["enum"] = meta["enum"]
            if meta.get("required"):
                required.append(pname)
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": required,
                "additionalProperties": False,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, ToolSpec] = {}

    def register(
        self,
        name: str,
        description: str,
        params: Optional[Dict[str, Dict[str, Any]]] = None,
        *,
        needs_project: bool = False,
        writes: bool = False,
    ):
        def deco(fn: Callable[..., Awaitable[ToolResult]]) -> Callable[..., Awaitable[ToolResult]]:
            p = dict(params or {})
            # ★★ 2026-09-27：`needs_project=True` ⇒ 参数表里**必须**有 project_id。
            #
            #   为什么必须在这里补：`needs_project=True` 是**声明**"这个工具要
            #   项目上下文"，而模型能看到的只有 `params` 生成的说明/JSON Schema。
            #   本仓有 2 个工具（`audit_character_image` / `audit_character_batch`）
            #   声明了 needs_project 却**没写 project_id 参数** —— 于是模型
            #   根本不知道要传 id，工具必然回一句"需要在有项目的上下文里调用"。
            #   框架**不会**自动注入 project_id（agent 循环只透传模型给的 args），
            #   所以那不是"框架会补"，而是**这个工具实际上调不通**。
            #   实测来源：`tests/test_agent_tools_smoke.py` 按参数表造假参数时，
            #   这两个工具拿不到 project_id ⇒ 暴露。
            #
            #   放在注册表里补：一次覆盖全部工具（含以后新写的），
            #   让 `needs_project` 这个声明与参数表**永远自洽**。
            if needs_project and "project_id" not in p:
                p["project_id"] = {"type": "string", "desc": "项目 id", "required": True}
            self._tools[name] = ToolSpec(
                name=name, description=description, params=p,
                needs_project=needs_project, writes=writes, handler=fn,
            )
            return fn
        return deco

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def all(self) -> List[ToolSpec]:
        return list(self._tools.values())

    def names(self) -> List[str]:
        return sorted(self._tools.keys())

    def catalog_text(self, core_only: bool = False) -> str:
        """拼给模型看的工具目录。

        ★★ 2026-09-29（用户第 2、3 条：「AI 还是纯嘴炮、不干活、没有智能」）

        实测：注册工具 **64 个**，每个都把「签名 + 说明 + 每个参数的说明」全塞进
        系统提示词 ⇒ **28,624 字符**。后果实测到了：
          · 模型经常只说话、不调工具（用户看到的就是「抱歉我走神了」）；
          · 偶尔调错工具、或忘了参数。

        这里改成**渐进式披露**（本仓的 skill 早就用这个套路：
        `list_skills` 只给名字+一句话，`use_skill` 才给正文）：
          · **核心工具**（流水线那十几个）给完整参数说明；
          · 其余只给 **名字 + 一句话**；
          · 需要细节时模型可以调 `list_tools`（按关键词拿完整说明）。

        这样提示词从 ~28k 降到 ~10k，而**能力一个都没少**。
        """
        core = [t for t in self.all() if t.name in CORE_TOOLS]
        rest = [t for t in self.all() if t.name not in CORE_TOOLS]
        if not core_only:
            return "\n".join(t.doc_text() for t in self.all())
        # ★ 注册表本来就小（测试用的临时注册表、或用户只启用了几个 MCP 工具）时
        #   **全给完整说明** —— 分层的意义是"别把 60 多个工具全塞进去"，
        #   工具本来就没几个的时候再分层只会让模型查不到参数。
        #   （实测踩到：`test_agent.py` 用一个只有 echo 的注册表，
        #     断言 prompt 里有 `echo(`，分层之后就不见了。）
        if len(self.all()) <= SMALL_CATALOG:
            return "\n".join(t.doc_text() for t in self.all())
        out = ["### 最常用的（参数说明给全）", ""]
        out += [t.doc_text() for t in core]
        if rest:
            out += ["", "### 其余工具（只给名字 —— 要用就先调 `list_tools` 拿参数，"
                        "`list_tools` 带 keyword 可以搜）", ""]
            # ★ 只列名字：43 个工具各带一句摘要会多占 ~4k 字符，
            #   而模型正确的做法是"先 list_tools 查，再调"。
            out.append("、".join("`%s`" % t.name for t in rest))
        return "\n".join(out)

    def doc_for(self, name: str) -> str:
        t = self._tools.get(str(name or "").strip())
        return t.doc_text() if t else ""

    def json_schemas(self) -> List[Dict[str, Any]]:
        return [t.to_json_schema() for t in self.all()]


REGISTRY = ToolRegistry()

#: **核心工具**：这些给完整的参数说明。其余只给"名字 + 一句话"，
#: 模型需要细节时调 `list_tools`（见 `catalog_text` 的说明）。
#: 挑选依据：跑完「立项 → 剧本 → 资产 → 定妆 → 分镜 → 白膜 → 480P → 成片 → 合成」
#: 这一条主线所必需的，加上最常用的几个查询。
#: 注册表**不超过**这么多个工具时，就不做分层（全给完整说明）。
#: 见 `catalog_text`：分层的目的是别把 60+ 个工具全塞进提示词；
#: 工具本来就少的时候分层只会让模型查不到参数。
SMALL_CATALOG = 25

CORE_TOOLS = {
    # 查询 / 上下文
    "list_projects", "pipeline_status", "get_project_status", "list_assets",
    "list_shots", "list_video_models",
    # 流水线推进（提案 → 确认 → 改 → 跳）
    # ★ 2026-10-20：「一口气推进」—— 免费/几乎不花钱的步骤自己连做，
    #   花钱的才停下等人点。这是"它是 agent"的核心能力，必须给全参数说明。
    "advance_pipeline",
    "propose_step", "confirm_step", "revise_step", "skip_step",
    # 各阶段真正干活的
    "create_project", "generate_script", "import_script", "extract_assets",
    "add_asset", "make_looks", "make_character_look", "make_scene_look",
    "plan_shots", "render_previz", "review_previz", "plan_long_video",
    "generate_preview", "generate_final", "compose_film", "dub_project_voices",
    # 修资产/分镜的常用手段
    "relink_shots", "fix_missing_scenes", "merge_characters", "refine_look",
}


# ──────────────────────── 懒加载服务入口 ────────────────────────


@REGISTRY.register(
    "apply_uploaded_image",
    "把**用户在对话里刚上传的那张图片**直接用作某个角色/场景的参考图。"
    "用户说「就用这张当陈砚的参考图 / 把这张图设成巷口的场景」时用它。"
    "会**落盘到资产目录**并写回 `reference_image_path`（旧图备份保留）。"
    "★ 这张图之后就会出现在出片请求里（角色/场景参考），所以要先跟用户确认是哪一张。",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True},
     "path": {"type": "string", "desc": "上传图片的本机路径（对话里附件给的 path）",
              "required": True},
     "kind": {"type": "string", "desc": "character / scene", "required": True},
     "asset_id": {"type": "string", "desc": "角色或场景 id", "required": True}},
    needs_project=True, writes=True,
)
async def t_apply_uploaded_image(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "project_id", "path", "kind", "asset_id")
    if bad:
        return bad
    import os
    import shutil
    src = str(args["path"])
    kind = str(args["kind"]).strip().lower()
    if not os.path.exists(src):
        return err(f"找不到这张图片：{src}（可能没上传成功，或临时目录被清了）")
    db, _, _ = _services()
    try:
        if kind.startswith("char"):
            item = db.get_character(args["asset_id"])
            is_char = True
        elif kind.startswith("scene"):
            item = db.get_scene(args["asset_id"])
            is_char = False
        else:
            return err("kind 只能是 character 或 scene")
    except Exception as e:  # noqa: BLE001
        return err(f"读资产失败：{e}")
    if not item:
        return err("这个资产不存在（可能 id 不对，先调 list_assets 拿 id）")
    if str(item.get("project_id")) != str(args["project_id"]):
        return err("这个资产不属于当前项目")
    try:
        root = os.environ.get("VIDEOFORGE_DATA_DIR") or os.getcwd()
        sub = "characters" if is_char else "scenes"
        pid = str(args["project_id"])
        out_dir = os.path.join(root, "cache", "images", pid, sub)
        os.makedirs(out_dir, exist_ok=True)
        import time as _t
        ext = os.path.splitext(src)[1] or ".png"
        dst = os.path.join(out_dir, "uploaded-%s%s" % (_t.strftime("%Y%m%d-%H%M%S"), ext))
        # 旧图备份（不删用户的图）
        old = str(item.get("reference_image_path") or "")
        backup = ""
        if old and os.path.exists(old):
            backup = old + ".prev"
            try:
                shutil.copy2(old, backup)
            except Exception:  # noqa: BLE001
                backup = ""
        shutil.copy2(src, dst)
        if is_char:
            db.update_character(args["asset_id"], reference_image_path=dst)
        else:
            db.update_scene(args["asset_id"], reference_image_path=dst)
        return ok(asset_id=args["asset_id"], kind=kind, path=dst,
                  backed_up=backup, name=item.get("name"),
                  note=(f"已经把这张图设为「{item.get('name')}」的参考图。"
                        f"之后出片时它会作为参考发出去"
                        + ("（旧图已备份，没有丢）。" if backup else "。")),
                  next_step="让用户看一眼资产页确认换成他想要的那张。")
    except Exception as e:  # noqa: BLE001
        logger.exception("apply_uploaded_image failed")
        return err(f"设置参考图失败：{type(e).__name__}: {e}")


@REGISTRY.register(
    "list_tools",
    "查某个工具的**完整参数说明**（提示词里只给了核心工具的详细说明，"
    "其余工具只有一句话 —— 要它们的参数就先调这个）。免费、纯本地。",
    {"name": {"type": "string", "desc": "工具名（不填=只列全部名字）"},
     "keyword": {"type": "string", "desc": "按关键词搜（例如「白膜」「配音」）"}},
)
async def t_list_tools(args: Dict[str, Any]) -> ToolResult:
    name = str(args.get("name") or "").strip()
    kw = str(args.get("keyword") or "").strip()
    if name:
        doc = REGISTRY.doc_for(name)
        if not doc:
            near = [n for n in REGISTRY.names() if name[:4] in n][:6]
            return err(f"没有这个工具：{name}。"
                       + (f"相近的：{'、'.join(near)}" if near else
                          "调 list_tools（不带参数）看全部名字。"))
        return ok(name=name, doc=doc, parameters=REGISTRY._tools[name].params)
    rows = []
    for n in REGISTRY.names():
        t = REGISTRY._tools[n]
        first = (t.description or "").strip().split("\n")[0]
        if kw and kw not in first and kw not in n:
            continue
        rows.append({"name": n, "summary": first[:90],
                     "params": list((t.params or {}).keys())})
    return ok(count=len(rows), tools=rows,
              note=("要看某个工具的**参数说明**，再用 "
                    "{\"tool\": \"list_tools\", \"args\": {\"name\": \"工具名\"}}。"))


def _services():
    """惰性取服务单例，避免 import 时循环依赖。"""
    from main import get_app_state  # type: ignore
    st = get_app_state()
    return st.get("db"), st.get("settings") or {}, st.get("queue")


def _missing(mod: str, attr: str) -> ToolResult:
    return err(
        f"功能未就绪：{mod}.{attr} 不存在。"
        f"（说明该能力在当前版本尚未接线，不是参数写错）"
    )


async def _call(fn, *args, **kwargs) -> Any:
    """同时兼容同步/异步函数。"""
    r = fn(*args, **kwargs)
    if inspect.isawaitable(r):
        return await r
    return r


def _need(args: Dict[str, Any], *keys: str) -> Optional[ToolResult]:
    """参数必填校验。"""
    miss = [k for k in keys if not str(args.get(k) or "").strip()]
    if miss:
        return err(f"缺少必填参数：{', '.join(miss)}")
    return None


# ──────────────────────── 查询类工具 ────────────────────────


@REGISTRY.register(
    "list_projects",
    "列出所有项目（短剧/短片工程）。开始任何操作前通常先调这个拿 project_id。",
    {"limit": {"type": "integer", "desc": "最多返回几个，默认 20"}},
)
async def t_list_projects(args: Dict[str, Any]) -> ToolResult:
    try:
        db, _, _ = _services()
        n = int(args.get("limit") or 20)
        items = await _call(db.list_projects, n) or []
        return ok(
            count=len(items),
            projects=[
                {"id": p.get("id"), "title": p.get("title"),
                 "status": p.get("status"), "updated_at": p.get("updated_at")}
                for p in items
            ],
        )
    except Exception as e:
        return err(f"列项目失败：{e}")


@REGISTRY.register(
    "create_project",
    "新建一个项目。用户说要做一部新片子时用。",
    {"title": {"type": "string", "desc": "项目名/片名", "required": True},
     "description": {"type": "string", "desc": "一句话简介"}},
    writes=True,
)
async def t_create_project(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "title")
    if bad:
        return bad
    try:
        db, _, _ = _services()
        p = await _call(db.create_project, args["title"], args.get("description") or "")
        return ok(project_id=p.get("id"), title=p.get("title"))
    except Exception as e:
        return err(f"建项目失败：{e}")


@REGISTRY.register(
    "get_project_status",
    "查看某项目当前进度：有几场戏、几个镜头、各状态数量、最终成片有没有。",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True}},
    needs_project=True,
)
async def t_get_project_status(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "project_id")
    if bad:
        return bad
    pid = args["project_id"]
    try:
        db, _, _ = _services()
        proj = await _call(db.get_project, pid)
        if not proj:
            return err(f"项目不存在：{pid}")
        scenes = await _call(db.list_scenes, pid) or []
        shots = await _call(db.list_shots, pid) or []
        chars = await _call(db.list_characters, pid) or []
        props = await _call(db.list_props, pid) or []
        script = await _call(db.get_script, pid)

        by_status: Dict[str, int] = {}
        for s in shots:
            k = str(s.get("status") or "unknown")
            by_status[k] = by_status.get(k, 0) + 1

        return ok(
            project={"id": pid, "title": proj.get("title"), "status": proj.get("status")},
            has_script=bool(script and (script.get("outline") or script.get("scenes"))),
            counts={"scenes": len(scenes), "shots": len(shots),
                    "characters": len(chars), "props": len(props)},
            shot_status=by_status,
        )
    except Exception as e:
        return err(f"查状态失败：{e}")


@REGISTRY.register(
    "list_assets",
    "列出项目的资产：角色 / 场景 / 道具。生成视频前用它确认 @引用 能引用到谁、谁还没有参考图。",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True}},
    needs_project=True,
)
async def t_list_assets(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "project_id")
    if bad:
        return bad
    pid = args["project_id"]
    try:
        db, _, _ = _services()
        chars = await _call(db.list_characters, pid) or []
        scenes = await _call(db.list_scenes, pid) or []
        props = await _call(db.list_props, pid) or []

        def brief(x):
            d = {
                "id": x.get("id"), "name": x.get("name"),
                "has_reference_image": bool(x.get("reference_image_path")),
                "description": (x.get("description") or "")[:60],
            }
            # 可灵靠 Element 锁角色，不是靠发参考图。模型得知道谁注册了、
            # 谁没注册，才会去调 register_kling_elements 补齐。
            if "element_id" in x:
                d["kling_element"] = (x.get("element_id") or "") or "未注册"
            return d

        # 只统计有角色的，避免给"道具"也报 Element 状态
        n_no_elem = sum(1 for c in chars
                        if c.get("reference_image_path") and not (c.get("element_id") or "").strip())
        hint = "没有参考图的资产无法保证一致性，建议先生成定妆照/场景图"
        if n_no_elem:
            hint += (f"；另有 {n_no_elem} 个角色有参考图但**没注册可灵 Element** —— "
                     f"如果这个项目用可灵出片，先调 register_kling_elements，"
                     f"否则可灵锁定不了人物形象（每个镜头一张脸）")

        return ok(
            characters=[brief(c) for c in chars],
            scenes=[brief(s) for s in scenes],
            props=[brief(p) for p in props],
            hint=hint,
        )
    except Exception as e:
        return err(f"列资产失败：{e}")


@REGISTRY.register(
    "list_shots",
    "★ 需要「某一镜的 id」之前**先调这个**。列出项目里所有分镜的**真实 id**"
    "（还有序号、时长、状态、有没有出片、白膜到哪一步）。\n"
    "★★ 想一次看清**每个镜头**的白膜/小样/成片状态，也**用这个**（一次全给你，"
    "含 `previz_status` / `has_draft_480p` / `has_final_1080p` / `clip_tiers`）。"
    "**不要**为每个镜头各调一次 `get_shot_progress` —— 实测那样会把步数用光，"
    "最后只能回一句「我用完了 N 步，没能给出结论」，用户白问一场。\n"
    "为什么要它：`get_shot_progress` / `render_previz` / `generate_preview` /"
    "`review_previz` / `set_shot_transition` 全都要 `shot_id`，"
    "而模型手上没有时只能瞎猜（实测把「具体的镜头 ID」这种说明文字当参数传进去，"
    "然后被守卫拦下、再把技术报错念给用户看）。**不要凭印象编 id。**",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True},
     "limit": {"type": "number", "desc": "最多列几条（默认全部）"}},
    needs_project=True,
)
async def t_list_shots(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "project_id")
    if bad:
        return bad
    pid = args["project_id"]
    try:
        db, _, _ = _services()
        shots = await _call(db.list_shots, pid) or []
        try:
            shots = sorted(shots, key=lambda s: s.get("order_index") or 0)
        except Exception:  # noqa: BLE001
            pass
        try:
            limit = int(args.get("limit") or 0)
        except Exception:  # noqa: BLE001
            limit = 0
        if limit > 0:
            shots = shots[:limit]

        out = []
        for s in shots:
            cands = s.get("candidates") or []
            if isinstance(cands, str):
                import json as _json
                try:
                    cands = _json.loads(cands) if cands.strip() else []
                except Exception:  # noqa: BLE001
                    cands = []
            segs = s.get("segments") or []
            if isinstance(segs, str):
                import json as _json
                try:
                    segs = _json.loads(segs) if segs.strip() else []
                except Exception:  # noqa: BLE001
                    segs = []
            # ★★★ 2026-09-29（用户第 1、9 条：不许说假话）
            #
            #   实测踩到：这个工具原来只给 `has_clip` 和候选数量，于是模型看到
            #   「status=completed、has_clip=true、3 个候选」就断言
            #   「**视频其实已经出好了**」—— 而磁盘上那 5 条其实是我早先
            #   **认回来的旧片段**（`recovered`）和配音版，**一条 480P 小样、
            #   一条 1080P 成片都没有**。用户会被这句话误导去花错钱。
            #
            #   修法：把候选**按档位拆开报**（复用 media_assets.classify，
            #   和「成片素材」页用的是同一套归类），并直接给出两个明确的布尔：
            #   `has_draft` / `has_final`。模型就不会再把"有视频"当成"小样出好了"。
            tiers: Dict[str, int] = {}
            for c in cands:
                if not isinstance(c, dict):
                    continue
                try:
                    from core import media_assets as _MA
                    k = _MA.classify(c.get("source"), c.get("path") or "",
                                     c.get("resolution") or "")
                except Exception:  # noqa: BLE001
                    k = "其它片段"
                tiers[k] = tiers.get(k, 0) + 1
            has_draft = bool(tiers.get("480P 小样"))
            has_final = bool(tiers.get("1080P 成片"))
            out.append({
                "shot_id": s.get("id"),
                "index": (s.get("order_index") or 0) + 1,
                "duration_seconds": s.get("duration_seconds"),
                "status": s.get("status"),
                "provider": s.get("model_provider"),
                "model": s.get("model_name"),
                # ★ 用"本地真的有文件"来判，不信任状态字段（本仓栽过）
                "has_clip": bool([c for c in cands
                                  if isinstance(c, dict) and c.get("path")]),
                "candidates": len([c for c in cands if isinstance(c, dict)]),
                # ★ 关键：把"有视频"拆成"到底哪一档"。只有这两个为 true
                #   才算那一步真的做过了。
                "has_draft_480p": has_draft,
                "has_final_1080p": has_final,
                "clip_tiers": tiers,
                "segments_ok": len([x for x in segs
                                    if isinstance(x, dict) and x.get("status") == "ok"]),
                "previz_status": s.get("previz_status") or "none",
                "overview": (s.get("layer1_overview") or "")[:40],
            })
        if not out:
            return ok(shots=[], count=0,
                      hint=("这个项目还没有分镜 —— 先在对话里说一句"
                            "「从剧本拆分镜」，或用 propose_step(stage=\"shots\") 提给用户确认"))
        return ok(count=len(out), shots=out,
                  hint=("拿上面的 `shot_id` **原样**传给需要它的工具"
                        "（get_shot_progress / render_previz / generate_preview / …）。"))
    except Exception as e:  # noqa: BLE001
        return err(f"列分镜失败：{type(e).__name__}: {e}")


@REGISTRY.register(
    "list_video_models",
    "列出可用的视频生成模型及其能力（时长上限、是否支持首尾帧、是否支持角色参考图）。选模型前先看这个。",
    {},
)
async def t_list_video_models(args: Dict[str, Any]) -> ToolResult:
    try:
        from core.adapters import ADAPTERS  # type: ignore
        out = []
        for name, cls in (ADAPTERS or {}).items():
            try:
                a = cls(api_key="", config={}) if inspect.isclass(cls) else cls
                out.append({
                    "provider": getattr(a, "name", name),
                    "display": getattr(a, "display_name", name),
                    "max_duration": getattr(a, "max_duration", None),
                    "supports_first_last_frame": getattr(a, "supports_first_last_frame", False),
                    "supports_character_ref": getattr(a, "supports_character_ref", False),
                    "resolutions": getattr(a, "supported_resolutions", []),
                })
            except Exception:
                continue
        return ok(count=len(out), models=out)
    except Exception as e:
        return err(f"列模型失败：{e}")


# ──────────────────────── 生成类工具 ────────────────────────


def _strip_attachment_block(text: str) -> str:
    """把「【附件：xxx（原文 N 字）】…正文…【附件结束】」剥成**纯正文**。

    ★★ 2026-09-27 第 57 轮（用户实测）：他的《执棋者》剧本 outline 只有 **24 个字** ——

        【附件：执棋者-拍摄稿v2.docx的剧本内容】

    也就是**只把附件的标题行存成了剧本**，正文根本没进来 ✗。
    后果：从这点内容里只能认出「老者 / 年轻人」两个角色（主角没出来），
    用户说「人物不全，加上」时又发现没有添加工具 —— 一路卡死。
    （标题行正是 `core/attachments.as_prompt_block()` 拼出来的第一行，
      模型把它当成了"剧本内容"传进来。）
    """
    t = str(text or "")
    if "【附件：" not in t:
        return t
    import re as _re
    # 去掉所有【附件：…】标题行与【附件结束】标记，剩下的就是正文
    t = _re.sub(r"^【附件：[^\n]*】\s*$", "", t, flags=_re.M)
    t = t.replace("【附件结束】", "")
    return t.strip()


def _read_latest_upload(data_dir: str) -> str:
    """兜底：从 `<data>/uploads/` 里读**最近上传的那个文件**的正文。

    为什么需要：上一条剥完之后如果还是空的/太短，说明模型压根没把正文带进来 ——
    但文件就在本机上传目录里（`core.attachments.save_upload` 存过），
    这时候应该**自己去读**，而不是拿一句标题去当剧本 ✗。
    """
    import os as _os
    try:
        from core import attachments as _att
    except Exception:  # noqa: BLE001
        return ""
    updir = _os.path.join(str(data_dir or ""), "uploads")
    if not _os.path.isdir(updir):
        return ""
    files = [f for f in (_os.path.join(updir, x) for x in _os.listdir(updir))
             if _os.path.isfile(f)]
    if not files:
        return ""
    files.sort(key=lambda f: _os.path.getmtime(f), reverse=True)
    for f in files[:3]:
        try:
            info = _att.extract_text(f)
            if info.get("ok") and info.get("text"):
                return str(info["text"])
        except Exception:  # noqa: BLE001
            continue
    return ""


@REGISTRY.register(
    "import_script",
    "把用户给的剧本/大纲文本导入项目。用户贴了一段剧本时用这个。",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True},
     "text": {"type": "string", "desc": "剧本全文", "required": True}},
    needs_project=True, writes=True,
)
async def t_import_script(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "project_id", "text")
    if bad:
        return bad
    pid, text = args["project_id"], args["text"]
    try:
        db, _, _ = _services()
        # ★ 第 57 轮：先把「附件标题行」剥掉；还不够就去 uploads 里把正文读出来
        _raw = text
        text = _strip_attachment_block(text)
        if len(text.strip()) < 40:
            import os as _os2
            _dd = _os2.environ.get("VIDEOFORGE_DATA_DIR") or ""
            _full = _read_latest_upload(_dd)
            if len(_full.strip()) > len(text.strip()):
                logger.info("导入剧本：附件正文没带进来（只有 %d 字），"
                            "已从 uploads 读回 %d 字", len(text.strip()), len(_full))
                text = _full
        # ══════════════════════════════════════════════════════════════
        # ★★ 2026-09-27 第 43 轮修一个真 bug（用户实测原话："流程跑不通"）
        #
        # 原来这里按 ("import_script_text", "save_script_from_text",
        # "upsert_script") 逐个试 `db` 上的方法：
        #   · 前两个**根本不存在** —— `getattr` 返回 None，**静默跳过**；
        #   · 第三个的签名是 `upsert_script(project_id, user_prompt,
        #     outline="", scenes=None, …)` ⇒ `upsert_script(pid, text)`
        #     把整段剧本写进了 **`user_prompt`**，而 `outline` / `scenes`
        #     **仍然是空的**。
        #
        # 后果（用户就是这么撞上的）：工具回报"导入成功 ✓"，
        # 但流水线的 `evidence` 查的是 outline/scenes ⇒
        # **「剧本」这一步仍然显示没做** ⇒ agent 立刻又提一次「剧本」⇒
        # 用户看到"我刚导入完，它又要生成剧本"；**真点下去会用 AI
        # 覆盖掉用户自己导入的剧本**。
        # 另外那句退路 `upsert_script(pid, outline=text)` 也必崩 ——
        # `user_prompt` 是必填的位置参数。
        #
        # 修法：走**已经注册的路由** `/script/paste`
        # （`import_script(..., raw_text=text, structurize=True)` —— 它会真的
        #  把文本分场、写 outline 与 scenes），与本文件开头那句"业务逻辑
        #  全部复用现有服务，不在这里重写"的原则一致。
        #  路由不可用时再退回直接写（这次把 user_prompt 一起给对）。
        # 并且**写完要回读校验**：读不到就如实报错，不再"报成功"。
        # ══════════════════════════════════════════════════════════════
        via = "script/paste"
        try:
            await _call_registered_route("/api/projects/{pid}/script/paste",
                                         pid=pid, payload={"text": text})
        except Exception as e:  # noqa: BLE001
            logger.warning("调 /script/paste 失败（回落直接写 outline）：%s", e)
            via = "upsert_script(outline)"
            await _call(db.upsert_script, pid, text, outline=text)

        script = await _call(db.get_script, pid) or {}
        outline = str(script.get("outline") or "").strip()
        n_sc = len(await _call(db.list_scenes, pid) or [])
        if not outline and not n_sc:
            return err(
                "剧本**没写进去**（outline 和场次都是空的）—— 不要报成功。"
                "请把原文再发一次；若仍不行，让用户检查一下剧本格式"
                "（每场要有场次标题或地点，否则分不出场）。")
        return ok(imported=True, via=via, scenes=n_sc, outline_chars=len(outline),
                  note=("剧本已导入（%d 场）。下一步是「角色 / 场景」。" % n_sc
                        if n_sc else
                        "剧本大纲已导入，但**没能自动分场** —— "
                        "后面拆分镜需要场次，必要时请用户确认一下剧本格式。"))
    except Exception as e:
        return err(f"导入剧本失败：{e}")


@REGISTRY.register(
    "generate_script",
    "用 AI 从一句想法生成专业剧本大纲。用户说「帮我想个故事/写个剧本」时用。",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True},
     "idea": {"type": "string", "desc": "用户的想法/题材/要求", "required": True},
     "total_duration": {"type": "number",
                        "desc": "整片总秒数。用户说了「10 秒」「30 秒」就传这个数；"
                                "不传会自动从 idea 里解析，解析不到才用 30"},
     "scene_count": {"type": "number",
                     "desc": "场次/镜头数。用户说了「2 个镜头」「3 场」就传"}},
    needs_project=True, writes=True,
)
async def t_generate_script(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "project_id", "idea")
    if bad:
        return bad
    pid, idea = args["project_id"], args["idea"]

    # ★★ 2026-10-20：**时长与镜头数必须听用户的**。
    #   实测病根：`generate_script_handler` 一直支持 `total_duration`，
    #   但本工具**从来不传** → 永远走默认 30 秒。
    #   用户说「10 秒 2 个镜头」，出来的是 2 镜 × 15 秒 = 30 秒。
    #   这里：显式参数优先 → 从 idea 里正则解析 → 才退回 30 秒。
    import re as _re
    def _pick_duration() -> int:
        v = args.get("total_duration")
        try:
            if v is not None and float(v) > 0:
                return int(float(v))
        except Exception:
            pass
        m = _re.search(r"(\d+(?:\.\d+)?)\s*(?:秒|s|sec)", str(idea), _re.I)
        if m:
            try:
                return max(3, min(600, int(float(m.group(1)))))
            except Exception:
                pass
        return 30

    def _pick_scenes() -> Optional[int]:
        v = args.get("scene_count")
        try:
            if v is not None and int(v) > 0:
                return int(v)
        except Exception:
            pass
        m = (_re.search(r"(\d+)\s*(?:个)?\s*(?:镜|场|个镜头|个分镜)", str(idea))
             or _re.search(r"(\d+)\s*(?:shots?|scenes?)", str(idea), _re.I))
        if m:
            try:
                return max(1, min(30, int(m.group(1))))
            except Exception:
                pass
        return None

    _dur = _pick_duration()
    _scn = _pick_scenes()
    payload = {"project_id": pid, "user_prompt": idea,
               "total_duration": _dur, "_task_id": ""}
    if _scn:
        payload["scene_count"] = _scn
    try:
        from core.llm import LLMClient, ScriptService  # type: ignore
        from core.orchestrator import generate_script_handler  # type: ignore
        r = await generate_script_handler(payload, [])
        return ok(generated=True, total_duration=_dur,
                  scene_count=_scn or "自动",
                  result=str(r)[:500])
    except ImportError:
        return _missing("core.orchestrator", "generate_script_handler")
    except Exception as e:
        return err(f"生成剧本失败：{e}")


def _table_columns(db, table: str) -> set:
    """拿一张表的真实列名。

    ★ 为什么需要：文字模型抽出来的字段**比表结构多**。
      实测报错：`table characters has no column named aliases` ——
      LLM 照着自己的理解多给了 `aliases`（别名）等字段，
      而 `create_character(**kwargs)` 是把所有 key 当列名拼进 INSERT 的，
      多一个就 SQL 报错、整步失败。

      所以写库前必须**按表结构过滤** —— 而不是指望模型只给表里有的字段。
    """
    try:
        rows = db._execute(f"PRAGMA table_info({table})").fetchall()
        return {r["name"] if hasattr(r, "keys") else r[1] for r in rows}
    except Exception as e:  # noqa: BLE001
        logger.warning("读 %s 表结构失败：%s", table, e)
        return set()


def _norm_asset_name(s: Any) -> str:
    """资产名归一化：**判断"是不是同一个东西"时用**，不改变真正存下来的名字。

    ★★ 2026-09-27 第 47 轮实测踩到的：
      用户的《亡户》项目里已经有「灰巷巷口」「灰巷老周棋摊」（他之前那轮建的，
      都有参考图）。这一轮 LLM 提取返回的是「灰巷·巷口」「灰巷·老周棋摊」——
      只差一个「·」，精确比较就判定"不重名"，于是又建了 2 个新场景 ✗
      ⇒ 资产库里同一地点两份、出图多花一遍时间，后面还会挑错参考图。

      所以比较时把空格与标点去掉（中英文都去掉）：`·`、`・`、空格、`，。、；：`、
      `()（）[]【】`、`-—_` 这些都不算区别。
    """
    import re as _re
    return _re.sub(r"[\s·・,，.。、;；:：()（）\[\]【】{}\-—_/\\|'\"“”‘’]+", "",
                   str(s or "")).strip().lower()


def _filter_cols(item: Dict[str, Any], cols: set) -> Dict[str, Any]:
    """只保留表里有的列。丢掉的那些**记一条日志**，便于发现"模型给了新字段"。"""
    if not cols:
        return dict(item)
    keep = {k: v for k, v in item.items() if k in cols}
    dropped = [k for k in item if k not in cols]
    if dropped:
        logger.info("写库时忽略了模型多给的字段：%s", dropped)
    return keep


@REGISTRY.register(
    "add_asset",
    "**手动补一个角色或场景**（剧本里漏掉的、或者你想临时加的都行）。\n"
    "为什么必须有这个工具（2026-09-27 第 56 轮，用户实测）：\n"
    "  剧本里的人物**没被提取全**（主角都没出来），用户说「不全，加上」时，\n"
    "  模型去调了一个**根本不存在的工具 `add_asset`** ✗ —— 因为当时确实\n"
    "  没有任何「手工建资产」的工具（工具表里只有 audit_/make_/list_ 那些）。\n"
    "  于是「发现不全 → 无法添加」，用户只能干看着。\n"
    "  这个工具就是那道口子：给名字就能建，能顺手带上外貌/服装/性别/年龄。\n"
    "★ 建完会**回读**确认真的落库了（这个仓库栽过「报成功却没写进去」）。",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True},
     "kind": {"type": "string", "desc": "character=角色（默认） / scene=场景",
              "enum": ["character", "scene"]},
     "name": {"type": "string", "desc": "名字（必填）", "required": True},
     "description": {"type": "string", "desc": "外貌/环境描述（有就填，出图会用到）"},
     "costume_main": {"type": "string", "desc": "角色主服装（角色才有）"},
     "gender": {"type": "string", "desc": "性别（角色才有）"},
     "age": {"type": "string", "desc": "年龄（角色才有）"},
     "location_type": {"type": "string", "desc": "场景类型（场景才有）"}},
    needs_project=True, writes=True,
)
async def t_add_asset(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "project_id", "name")
    if bad:
        return bad
    pid = args["project_id"]
    kind = str(args.get("kind") or "character").strip().lower()
    if kind not in ("character", "scene"):
        return err("kind 只能是 character 或 scene")
    name = str(args["name"]).strip()
    if not name:
        return err("名字不能是空的")
    try:
        db, _, _ = _services()
        # 同名（归一化后）就不重复建 —— 与 extract_assets 同一套判据
        existing = (await _call(db.list_characters, pid) if kind == "character"
                    else await _call(db.list_scenes, pid)) or []
        for it in existing:
            if _norm_asset_name(it.get("name")) == _norm_asset_name(name):
                return ok(kind=kind, name=it.get("name"), id=it.get("id"),
                          already=True,
                          note=f"「{it.get('name')}」已经在资产库里了，没有重复建。"
                               f"（想改它就用 refine_look 或直接说出要改什么）")
        fields = {k: v for k, v in {
            "name": name,
            "description": str(args.get("description") or "").strip(),
            "costume_main": str(args.get("costume_main") or "").strip(),
            "gender": str(args.get("gender") or "").strip(),
            "age": str(args.get("age") or "").strip(),
            "location_type": str(args.get("location_type") or "").strip(),
        }.items() if v}
        ccols = _table_columns(db, kind == "character" and "characters" or "scenes")
        fields = _filter_cols(fields, ccols)
        if "name" not in fields:
            return err(f"数据库里没有 name 字段（异常）：{sorted(ccols)[:8]}")
        row = await _call(db.create_character if kind == "character" else db.create_scene,
                          project_id=pid, **fields)
        # ★ 回读：真的落库了才算成功
        back = (await _call(db.list_characters, pid) if kind == "character"
                else await _call(db.list_scenes, pid)) or []
        hit = [x for x in back if _norm_asset_name(x.get("name")) == _norm_asset_name(name)]
        if not hit:
            logger.error("★ add_asset 写完回读不到：%s（%s）", name, kind)
            return err(f"「{name}」没能写进资产库（写完读不回来）—— "
                       f"这是数据库层面的问题，请把这条消息发给我。")
        total = len(back)
        return ok(kind=kind, name=name, id=(row or {}).get("id") or hit[0].get("id"),
                  in_db=total, note=(
                      f"已经加进资产库：{'角色' if kind == 'character' else '场景'}"
                      f"「{name}」（现在共 {total} 个）。"
                      f"下一步可以给它出参考图，或继续补别的。"))
    except Exception as e:  # noqa: BLE001
        logger.exception("add_asset failed")
        return err(f"添加失败：{type(e).__name__}: {e}")


@REGISTRY.register(
    "extract_assets",
    "从剧本里自动抽取角色和场景，**写进资产库**。这是流水线「角色/场景」那一步。"
    "抽到的角色/场景会拿到 id，后面分镜引用、出定妆照、生成视频都靠它们。",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True}},
    needs_project=True, writes=True,
)
async def t_extract_assets(args: Dict[str, Any]) -> ToolResult:
    """★ 2026-09-25 修一个真 bug：这个工具**以前根本不写库**。

    原实现把 `extract_characters` / `extract_scenes` 的结果放进**局部变量**，
    直接 `return ok(characters=len(chars), scenes=len(scenes))` ——
    于是调用方看到"成功抽到 3 个角色、2 个场景"，而资产库**一条都没有**。

    这是最坏的一类失败：**静默空转**。用户（或 agent）以为做完了，
    下一步拆分镜时拿不到角色/场景，生成时也就拿不到正确的参考图，
    而界面上一直显示"成功"。实测就是从"全链路实测"里掉出来的：
    抽资产报成功（0.7 秒），资产库里 0 个角色 0 个场景。

    顺带修第二个问题：**抽到 0 个也报成功**。
    现在按"真的写进去了几条"来报，一条都没有就如实说失败 + 为什么。
    """
    bad = _need(args, "project_id")
    if bad:
        return bad
    pid = args["project_id"]
    try:
        db, settings, _ = _services()
        script = await _call(db.get_script, pid)
        if not script:
            return err("项目还没有剧本，先导入或生成剧本")
        from core.assets import extract_characters, extract_scenes  # type: ignore
        from core.orchestrator import _get_llm_from_settings  # type: ignore

        settings = await _call(db.get_all_settings) or settings or {}
        llm = _get_llm_from_settings(settings)

        # 已有的资产：同名的不重复建（这个工具允许重复调用）
        # ★ 用归一化比较：`灰巷巷口` 与 `灰巷·巷口` 是**同一个场景**（见 _norm_asset_name）
        existing_c = {_norm_asset_name(c.get("name"))
                      for c in (await _call(db.list_characters, pid) or [])}
        existing_s = {_norm_asset_name(s.get("name"))
                      for s in (await _call(db.list_scenes, pid) or [])}

        try:
            chars = await extract_characters(llm, script)
        except Exception as e:  # noqa: BLE001
            logger.warning("抽角色失败：%s", e)
            chars = []
            char_err = f"{type(e).__name__}: {e}"
        try:
            scenes = await extract_scenes(llm, script)
        except Exception as e:  # noqa: BLE001
            logger.warning("抽场景失败：%s", e)
            scenes = []
            scene_err = f"{type(e).__name__}: {e}"

        made_c, made_s, skipped = [], [], []
        char_err = ""
        scene_err = ""
        # ★ 必须先按表结构过滤：模型给的字段比表多（实测 `aliases` 就报错过）
        ccols = _table_columns(db, "characters")
        scols = _table_columns(db, "scenes")
        for c in (chars or []):
            if not isinstance(c, dict):
                continue
            name = str(c.get("name") or "").strip()
            if not name:
                continue
            # ★★ 2026-09-27 第 71 轮：挡掉模型把**模板占位符**当角色抄回来的情况
            #   （实测库里真有一个叫「角色名称」、描述「角色描述」、年龄「年龄」
            #     的垃圾角色，还占了一整把音色）。
            try:
                from core.assets import is_placeholder_character as _is_ph
                if _is_ph(c):
                    skipped.append(f"{name}（占位符，已丢弃）")
                    continue
            except Exception:  # noqa: BLE001
                pass
            if _norm_asset_name(name) in existing_c:
                skipped.append(name)
                continue
            # ★★ 把"表里没有的字段"（**gender** / personality / role /
            #    aliases / slots）写进 `reference_features`。
            #    原来走 `_filter_cols(c, ccols)` 会把它们**整批丢掉** ——
            #    丢掉 gender 的直接后果就是配音按"气质"猜性别：
            #    实测 60 岁的「老者」被配成了「成熟女声」，
            #    用户原话「声音跟人物形象对不上」。
            #    两条路（这里与 `/characters/from-script`）共用同一个 payload 函数。
            from core.assets import character_payload as _payload
            row = await _call(db.create_character, project_id=pid,
                              **_filter_cols(_payload(c), ccols))
            existing_c.add(_norm_asset_name(name))
            made_c.append({"id": row.get("id"), "name": name})

        for s in (scenes or []):
            if not isinstance(s, dict):
                continue
            name = str(s.get("name") or "").strip()
            if not name:
                continue
            if _norm_asset_name(name) in existing_s:
                skipped.append(name)
                continue
            row = await _call(db.create_scene, project_id=pid,
                              **_filter_cols(s, scols))
            existing_s.add(_norm_asset_name(name))
            made_s.append({"id": row.get("id"), "name": name})

        total = len(made_c) + len(made_s)
        if not total:
            if skipped:
                return ok(
                    characters=0, scenes=0, created=0, skipped=skipped,
                    note=(f"剧本里的角色/场景**都已经在资产库里了**"
                          f"（跳过 {len(skipped)} 个同名），没有新建。"),
                )
            # ★★ 2026-09-27 第 46 轮：抽不出来要**说清是哪一步坏的**。
            #    原来只 `logger.warning` 然后 `chars=[]`，于是"抽角色失败"
            #    被吞成"没抽出任何角色或场景"，用户和 AI 都不知道真实原因。
            why = "；".join(x for x in (
                (f"抽角色报错：{char_err}" if char_err else ""),
                (f"抽场景报错：{scene_err}" if scene_err else "")) if x)
            return err(
                "没抽出任何角色或场景 —— 资产库还是空的。"
                + (f"（{why}）" if why else "")
                + "常见原因：① 剧本太短/没有人物（先看看 outline 和 scenes）；"
                "② 文字模型的 Key 没配好或额度用尽（这一步要调 LLM）。"
                "不要假装成功，先把原因告诉用户。",
                hint="可以用 get_project_status 看剧本到底有没有内容。",
            )

        # ══════════════════════════════════════════════════════════════
        # ★★ 2026-09-27 第 46 轮：写完**回读校验**（这是本轮最重要的一处）
        #
        # 为什么要这一步：用户真实机器上发生过 ——
        #   日志清清楚楚写着「11:03:52 LLM 提取角色 7 个（含固定槽位 91 项）」，
        #   11:04:04 调了 7 次 `create_character`（每次都有"忽略多余字段"日志），
        #   同一秒里 2 个场景也建成功了；11:11–11:33 还**为这些角色出了 7 张图**
        #   （出图的前提是角色在库里）。
        #   可是事后查库：**角色 0 行**，场景 2 行。
        #   而工具当时报的是"已写进资产库：7 个角色、2 个场景" —— 一次彻底的假成功。
        #
        # 我复现过：在**他库的副本**上重跑这个工具，8 条全部落库、读得回来
        #   （6 个角色 + 2 个场景）⇒ 代码路径本身是对的，问题出在"写完就相信"，
        #   中间任何一环（连接、事务、另一个进程、数据库被换掉）出错都不会被发现。
        #
        # ⇒ 从此**不信任内存里的计数**：写完必须**从库里读回来**，
        #   读不回来的名字要如实报错（并落 ERROR 日志）。宁可当场报错，
        #   也不要让用户第二天发现自己辛苦等出来的角色"没保存"。
        # ══════════════════════════════════════════════════════════════
        back_c, back_s = [], []
        try:
            back_c = await _call(db.list_characters, pid) or []
            back_s = await _call(db.list_scenes, pid) or []
        except Exception as e:  # noqa: BLE001
            logger.error("回读资产失败（写完却读不回来）：%s", e, exc_info=True)
            return err(f"资产写进去了但**读不回来**：{type(e).__name__}: {e}。"
                       f"这是数据库层面的问题，请把这条消息发给我。")
        back_cn = {str(x.get("name") or "").strip() for x in back_c}
        back_sn = {str(x.get("name") or "").strip() for x in back_s}
        missing = ([x["name"] for x in made_c if x["name"] not in back_cn]
                   + [x["name"] for x in made_s if x["name"] not in back_sn])
        if missing:
            logger.error("★ 资产写完回读不到：%s（写成功 %d 角色/%d 场景，"
                         "实际库里 %d 角色/%d 场景）",
                         missing, len(made_c), len(made_s), len(back_cn), len(back_sn))
            return err(
                f"**写进资产库以后读不回来**：{('、'.join(missing))[:120]}"
                f"（刚才报告写了 {len(made_c)} 个角色 / {len(made_s)} 个场景，"
                f"实际库里只有 {len(back_cn)} 个角色 / {len(back_sn)} 个场景）。"
                f"这说明写入没有真正落库 —— 不是你的操作问题，"
                f"请把这条消息发给我，我先修它。",
                wrote={"characters": len(made_c), "scenes": len(made_s)},
                in_db={"characters": len(back_cn), "scenes": len(back_sn)},
            )

        return ok(
            characters=len(made_c), scenes=len(made_s), created=total,
            detail={"characters": [x["name"] for x in made_c],
                    "scenes": [x["name"] for x in made_s]},
            in_db={"characters": len(back_cn), "scenes": len(back_sn)},
            skipped=skipped or None,
            note=(f"已写进资产库：{len(made_c)} 个角色、{len(made_s)} 个场景"
                  + (f"（跳过 {len(skipped)} 个同名）" if skipped else "")
                  + "。下一步建议给它们出定妆照/场景图 —— "
                    "没有参考图的话视频模型锁不住长相。"),
        )
    except ImportError as e:
        return _missing("core.assets", str(e))
    except Exception as e:
        return err(f"抽取资产失败：{e}")


async def _call_registered_route(url_path: str, **path_params) -> Dict[str, Any]:
    """调用 main.py 里**已经注册的**路由函数，拿它的返回值。

    ★★ 为什么这么做（而不是在这里重写一遍逻辑）：

      项目自己的原则写在 `agent_tools.py` 开头：
        "工具本身只做参数校验 + 委派，业务逻辑全部复用 VideoForge 现有服务，
         不在这里重写（避免与 main.py 的路由逻辑分叉）"

      `plan_shots` 原来是**违反了**这条原则的：它去调
      `db.shots_from_script(pid)` —— 而 `db` 上**根本没有这个方法**，
      外层 `if fn:` 又把这种情况静默跳过，于是这个工具**永远建不出分镜**，
      只会回一句"分镜仍为空"。真正的实现在 main.py 的
      `POST /api/projects/{pid}/shots/from-script` 里（一百多行匹配逻辑：
      按场次地点匹配场景、按出场人物匹配角色……）。

      重写一遍那段逻辑 = 制造第二份真相，以后必然分叉。
      所以这里**从 app 的路由表里把那个函数找出来直接调** ——
      接口和界面按钮走的是同一份代码。

    返回值：路由返回的是 JSONResponse，这里把它的 body 解回 dict。
    """
    import json as _json
    from main import app as _app  # type: ignore

    target = None
    for r in getattr(_app, "routes", []):
        if getattr(r, "path", "") == url_path and hasattr(r, "endpoint"):
            target = r.endpoint
            break
    if target is None:
        raise RuntimeError(f"找不到路由 {url_path}（main.py 里改过路径？）")

    resp = await target(**path_params)
    body = getattr(resp, "body", None)
    if body:
        try:
            data = _json.loads(body.decode("utf-8"))
        except Exception:  # noqa: BLE001
            data = {}
        # 统一信封：{"code","message","data"}
        return data.get("data") if isinstance(data, dict) and "data" in data else data
    return {}


@REGISTRY.register(
    "plan_shots",
    "把剧本拆成分镜（镜头表），**并建立分镜 ↔ 角色/场景 的真实引用**。"
    "有了分镜才能排片、渲白膜、出片。会覆盖已有分镜（想保留就先跟用户确认）。",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True},
     "replace": {"type": "boolean",
                 "desc": "是否覆盖已有分镜，默认 true。用户已经手工改过镜头就传 false"}},
    needs_project=True, writes=True,
)
async def t_plan_shots(args: Dict[str, Any]) -> ToolResult:
    """★ 2026-09-25 修一个真 bug：这个工具**以前永远建不出分镜**。

    原实现调 `db.shots_from_script(pid)`，而 `Database` 上**没有这个方法**；
    `if fn:` 把这种情况静默跳过 → 然后报"分镜仍为空 —— 可能剧本还没分场，
    或调用失败"。用户看到的是"拆分镜老是失败"，而真实原因是
    **工具调了一个不存在的方法**。

    现在直接调用已注册的路由（`/api/projects/{pid}/shots/from-script`），
    与界面按钮共用同一份实现。
    """
    bad = _need(args, "project_id")
    if bad:
        return bad
    pid = args["project_id"]
    try:
        db, _, _ = _services()
        before_shots = await _call(db.list_shots, pid) or []
        before = len(before_shots)
        replace = args.get("replace")
        replace = True if replace is None else bool(replace)

        # ★★ 2026-09-28（实测事故）：`replace=true` 会**删掉旧分镜重建**，
        #   而旧分镜的 `candidates`（已经花钱生成好的片段）**跟着一起没了**。
        #   用户就是这么丢掉 5 个镜头的片段的：对话里跑了一次 plan_shots(replace)，
        #   之后配音说"一个镜头都没配上"、合成说"拼出来会是无声片" ——
        #   他感觉到的就是「流程跑不通」，但真实原因是**片段在库里失联了**
        #   （文件还在磁盘上，见 core/recover.py）。
        #
        #   所以：**要重建、而旧分镜里有片段时，必须先拿到明确同意**，
        #   而且先把旧候选**归档到文件**（可恢复），再动手。
        if replace and before_shots:
            with_clips = []
            for s in before_shots:
                c = s.get("candidates")
                if isinstance(c, str):
                    try:
                        import json as _jj
                        c = _jj.loads(c) if c.strip() else []
                    except Exception:  # noqa: BLE001
                        c = []
                if c:
                    with_clips.append(s)
            if with_clips and not args.get("force"):
                total = sum(len(s.get("candidates") or []) for s in with_clips)
                return err(
                    f"**先别拆** —— 现在有 {len(with_clips)} 个镜头已经出过片"
                    f"（共 {total} 个候选片段）。`replace=true` 会把旧分镜删掉重建，"
                    f"那些片段就**在库里失联了**（文件还在磁盘上，但要靠"
                    f"`relink_orphan_clips` 认回来，很麻烦）。\n"
                    f"请先问用户一句：「重新拆分镜会让已生成的 N 个片段失联，"
                    f"确定要重拆吗？」\n"
                    f"· 他说要 → 带 `force=true` 重调一次（我会先把旧候选**归档**到文件再拆）；\n"
                    f"· 他说不要 → 用 `relink_orphan_clips` 或直接不拆，"
                    f"在现有分镜上继续（渲白膜/出片/合成）。",
                    existing_shots=len(with_clips), clips=total,
                    hint="需要用户明确同意才重拆；或改用 relink_orphan_clips",
                )
            # 归档（无论 force 与否，只要真要拆就留一份可恢复的底）
            try:
                from core import recover as _rc
                _arc = _rc.archive_candidates(db, pid)
                if _arc.get("path"):
                    logger.info("重建分镜前已归档旧候选 → %s", _arc["path"])
            except Exception as _e:  # noqa: BLE001
                logger.warning("归档旧候选失败（继续拆，但请注意不可恢复）：%s", _e)

        try:
            await _call_registered_route("/api/projects/{pid}/shots/from-script",
                                         pid=pid, replace=replace)
        except Exception as e:  # noqa: BLE001
            logger.warning("调 from-script 路由失败：%s", e)
            return err(
                f"拆分镜失败：{type(e).__name__}: {e}\n"
                f"（这一步由 main.py 的 /shots/from-script 实现；"
                f"如果它不存在了，说明路由被改过路径）"
            )

        shots = await _call(db.list_shots, pid) or []
        if not shots:
            return err(
                "分镜仍然是空的。常见原因：① 剧本**没有分场**"
                "（只有大纲不行，要有 scenes）；② 剧本内容太少。"
                "先调 get_project_status 看剧本到底有没有场次，"
                "把真实原因告诉用户，不要假装成功。"
            )

        # 关联体检：分镜必须挂上角色/场景，否则生成时拿不到正确的参考图
        def _n(v):
            if isinstance(v, list):
                return len(v)
            if isinstance(v, str) and v.strip():
                try:
                    import json as _j
                    x = _j.loads(v)
                    return len(x) if isinstance(x, list) else 0
                except Exception:  # noqa: BLE001
                    return 0
            return 0

        no_char = [s for s in shots if _n(s.get("character_ids")) == 0]
        no_scene = [s for s in shots if not s.get("scene_id")]
        # ★ 2026-10-21（用户第 5 条）：分镜落库后**自动编译**三段式提示词
        #   （video_prompt / negative_prompt / audio_prompt + 角色/场景锚定），
        #   写回 layer3_constraints.prompts；编译失败只记日志，不阻塞分镜本身。
        try:
            from core.shot_prompts import compile_project_prompts as _cpp
            _settings = {}
            try:
                _settings = db.get_all_settings() or {}
            except Exception:  # noqa: BLE001
                _settings = {}
            _out = str(_settings.get("output_dir") or "")
            _n = await _cpp(db, args.get("project_id") or "", settings=_settings, output_dir=_out)
            if _n:
                logger.info("分镜提示词已编译：%d 镜", _n)
        except Exception as _e:  # noqa: BLE001
            logger.warning("分镜提示词编译失败（不影响分镜本身）：%s", _e)
        return ok(
            shots=len(shots), replaced=max(0, before) if replace else 0,
            without_character=len(no_char), without_scene=len(no_scene),
            detail=[{"id": s.get("id"), "order": s.get("order_index"),
                     "duration": s.get("duration_seconds"),
                     "which": (s.get("layer1_overview") or "")[:50]}
                    for s in shots[:20]],
            note=(f"拆出 {len(shots)} 个镜头"
                  + (f"；其中 {len(no_char)} 个没关联角色、"
                     f"{len(no_scene)} 个没关联场景 —— "
                     f"生成时这些镜头拿不到参考图，容易出现「每个镜头一张脸」。"
                     f"建议先补齐资产再出片。"
                     if (no_char or no_scene) else "；角色/场景都关联上了。")),
        )
    except Exception as e:
        logger.exception("plan_shots failed")
        return err(f"拆分镜失败：{e}")


# ──────────────────────── 预留：需要后续接线 ────────────────────────


@REGISTRY.register(
    "preview_mentions",
    "在真正生成前，预览一句话里的 @角色/@场景 会被解析成什么（会引用哪张参考图、"
    "在目标平台上会写成什么）。生成提示词前先自查，避免引用错了人。",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True},
     "text": {"type": "string", "desc": "含 @引用 的文本", "required": True},
     "provider": {"type": "string", "desc": "目标视频模型（kling/jimeng/wanx/runway…），默认 jimeng"}},
    needs_project=True,
)
async def t_preview_mentions(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "project_id", "text")
    if bad:
        return bad
    try:
        from core.mentions import MentionResolver, unresolved_message  # type: ignore
        from core.platform_refs import to_platform_refs  # type: ignore
        db, _, _ = _services()
        resolver = MentionResolver.from_db(db, args["project_id"])
        res = resolver.resolve(args["text"])
        if not res.ok:
            return err(unresolved_message(res, db, args["project_id"]))
        pf = to_platform_refs(res, args.get("provider") or "jimeng")
        return ok(
            matched=[{"raw": r.raw, "kind": r.kind, "name": r.name,
                      "has_image": bool(r.reference_image)} for r in res.refs],
            platform=pf.provider,
            final_prompt=pf.full_prompt(),
            reference_images=pf.reference_images,
            warnings=pf.warnings,
            dropped=pf.dropped,
        )
    except ImportError as e:
        return _missing("core.mentions / core.platform_refs", str(e))
    except Exception as e:
        return err(f"预览引用失败：{e}")


# ──────────────────────── agent 用的提示词片段 ────────────────────────


AGENT_TOOLS_HEADER = """你是一个能**直接操作** VideoForge 的 Agent，不只是聊天。

## 你可以调用的工具

{tools}

## 怎么调用

需要执行动作时，**整条回复就是一个 JSON 对象**：

{"tool": "工具名", "args": {"参数名": "值"}}

**除此之外什么都不要写** —— 不要解释、不要用 markdown 代码块、不要写「好的我来看看」。
系统会把这条 JSON 拦下来去执行，然后把结果告诉你，你再决定下一步。

★ 最常见的错误（实测真的踩到过）：模型回一句
「我先查看第 1 个镜头的排片方案；只排片，不生成任何视频。」
—— 这只是**一句话、没有动作**，系统什么都不会执行，用户白等一轮。
正确写法就只有一行：

    {"tool": "plan_long_video", "args": {"shot_id": "那个镜头的id"}}

如果你确实只想说话（回答、解释、确认、报结论），那就**完全不要出现 JSON**，
直接写自然语言。两种模式**不要混在同一条回复里**。

### ★ 说话的时候怎么说话（用户实测反馈，2026-09-29）

用户的原话是「**纯嘴炮、不干活、用户体验极差**」，并贴了一屏这样的回复：

    「我先把陈砚的提示词拼出来…同时调出图」
    「抱歉，刚才走神了。现在真的开始 —— 先查项目里有哪些资产。」
    「抱歉！马上发申请。」「好，这就发。」「我终于真的发出图申请了。」

三条硬规矩，**违反了等于没干活**：

1. **不要预告** —— 不要写「我先去查一下 / 我这就去调 / 让我看看」。
   要查就**直接调工具**；工具跑完再一次性把**结果**说出来。
   （预告完不调用 = 用户白等一轮，这是最招骂的一种。）
2. **不要反复道歉** —— 一句都不要说「抱歉我走神了」。用户要的是结果，
   不是道歉。真做错了就**一句话说清**（哪个没做成、为什么），然后给下一步。
3. **报结果就报数据** —— 用工具返回的**真实数字/名字**说话
   （「10 个角色、全部有定妆照」「5 个镜头，480P 一个都没出」），
   不要用「已经处理好了 / 一切正常」这种空话。

★ 还有一条（用户第 5 条）：**别把活推回给用户**。
不要写「你说一句『继续』我就…」—— 界面会给按钮；
你要做的是**说清现状 + 给出可点的下一步**，而不是让用户手打指令。

### 完整示范

    用户     这个项目有几个镜头？先排个片看看。
    你(1)    {"tool": "list_shots", "args": {"project_id": "p1"}}
    系统     [工具 list_shots 执行成功] {"ok": true, "shots": [{"id":"s1","duration_seconds":4}]}
    你(2)    {"tool": "plan_long_video", "args": {"shot_id": "s1"}}
    系统     [工具 plan_long_video 执行成功] {"ok": true, "strategy": "single"}
    你(3)    这个项目有 1 个镜头，4 秒，一次出完不用切段。
             ← 活干完了，**从这条开始才用自然语言**


## 规则

1. **一次只调一个工具**，等结果再决定下一步。
2. 缺少必填参数时，先问用户，**不要瞎猜 id**。不确定项目 id 就先调 list_projects。
3. 生成类操作（生成剧本/图片/视频、拼接）耗时较长，调用前用一句话告诉用户你要做什么。
4. 涉及 @角色/@场景 的提示词，调用 preview_mentions 自查后再生成。
5. 工具返回 ok=false 时，把 error 原文告诉用户，**不要假装成功**。
6. 不要编造工具名。只能用上面列出的工具。
7. ★ **凡是问"用户自己数据"的问题，必须先调工具，再开口。**
   「有哪些项目？」「这个片子几个镜头？」「现在走到哪一步了？」都属于这一类。
   你**手上没有**项目列表、没有分镜表、没有进度 —— 说
   「我无法直接访问你的项目列表」「请提供项目 id」**都是错的**，
   对应的工具就在上面（list_projects / list_shots / get_shot_progress /
   list_assets…），先调它，再拿结果回答。
   实测真的发生过：用户问「有哪些项目？」，模型直接回"我无法访问"，
   用户看到的就是"这个 AI 连自己的数据都读不到"。
9. ★★ **你是 agent，不是聊天窗口 —— 能连着做就一口气做完。**
   用户说「继续 / 往下走 / 自动做完 / 一口气做完 / 别问我了 / 都做掉」时，
   **不要**一步步 `propose_step` 让他点：直接调 **`advance_pipeline`**，
   它会自己把**免费**的步骤（分镜、白膜…）连着做完，
   到**要花钱**的（出图 / 出片）才停下来，把确认卡片交到用户手上。
   · 用户只是随口问一句 / 想改东西 → 照旧用 `propose_step` 一步一步来。
   · 用户明确说"不用问我/都做完" → 传 `allow_cheap: "true"`（那一档通常不到 1 分钱）。
   · 花钱的步骤**永远**要停（`image` / `video` / `expensive`）—— 这是唯一不能自作主张的地方。

8. ★ **别为了"分清新旧"而打断用户**。
   用户说「我要做一部 X / 做一个 X / 建个新项目 / 来一部 X」——
   这**就是**要做新片子：**直接 create_project**，不要问。
   实测（用户实机）：用户说「我要做一部生活类的日常短片，60s」，
   模型回了一句「请选择：**新建**或**继续《日常反转》**」——
   用户完全看不懂，回了「？你在说什么」，**这一轮白等**。
   只有这几种情况才在**当前项目**上动手：
     · 用户明确说「继续 / 接着做 / 就刚才那个 / 重做 X」；
     · 用户说「这个 / 它 / 刚才那段」等**指代当前项目**的话；
     · 用户在问当前项目的状态。
   其余一律按新片子处理。**新建项目不会破坏旧项目**，
   而在旧项目上"重做并覆盖"才是不可逆的破坏 —— 所以宁可多建一个项目。

## ★★ 一步一步走：花钱的事必须先问过用户（最重要的一条）

用户明确要求：**像即梦那样，一步一步走完，每一步等他确认或者改**。

改版前你会连着调工具，一轮就把钱花了 —— 用户看到的是"我还没说开始，
它就自己花钱了"。所以现在有一条**硬规则**：

    凡是会花钱 / 耗时的步骤，你**不能**直接调那个工具。
    你必须先 propose_step 把这一步提给用户，然后**停下来**，等用户回话。

被这条规则管的工具（直接调会被系统拒绝，返回 blocked_by=gate）：
    generate_script / plan_shots / extract_assets / render_previz /
    generate_preview / generate_final / compose_film / make_looks /
    make_character_look / make_scene_look / refine_look /
    kling_generate / kling_white_model

标准三步走：

    你(1)   {"tool": "pipeline_status", "args": {"project_id": "p1"}}
    系统    告诉你 9 个阶段各做到哪了、下一步是哪个
    你(2)   {"tool": "propose_step", "args": {"project_id":"p1", "stage":"script",
                                             "args": {"idea": "抗战题材三分钟短片"}}}
    系统    返回一张待确认卡片（做什么/花多少/多久/能改什么），
            **一步都没执行**。这时你必须用自然语言把卡片内容讲给用户，
            并问他要不要开始、要不要改。
    你(3)   ← 停在这里。用户回话之前不要再调任何工具。
    用户    「确认」/「时长改成 5 秒」/「先只做主角」
    你(4)   {"tool": "confirm_step", "args": {"project_id": "p1",
                                              "overrides": {"duration": 5}}}
            ← 或者用户要改时，先 revise_step 改参数，再等确认
    系统    这一步真正执行完了

★★ 关于上面那个例子里的 "idea"（**真出过事故，务必看清楚**）：
   例子里的 "抗战题材三分钟短片" 只是**形状示例**，不是让你照抄的内容。
   这个字段必须填**用户自己说过的题材**。实测真的发生过：用户还没说要拍什么，
   模型就把「请提供您的想法或题材」这句话**当成了 idea 的值**提了案 ——
   用户看到的卡片上写着那句问话，完全看不懂；点「确认」还会拿这句废话去生成剧本。
   ⇒ 用户还没给题材时，**不要 propose_step**。直接用一句自然语言问他
     「你想拍个什么故事？给我一句题材就行」，等他回答。

★ 上面每个示例都是**合法 JSON，花括号必须配平**。
  它们不是装饰：你输出动作时会照这个形状写，少一个括号就解析不了，
  用户看到的就是「AI 回了一串代码但什么都没干」。

★ 用户说"改一下 X" 时用 revise_step，**不要**重新 propose，
  更**不要**为了改个参数就去调真正的生成工具。

★ 一次只推进**一个**阶段。做完一步就用自然语言汇报结果，
  然后 propose_step 下一步，再停下等确认。**不要一口气推到底。**

★ 有 8 个阶段的顺序是固定的（见 pipeline_status 返回的 needs）：
  立项 → 剧本 → 角色/场景 → 定妆照 → 分镜 → 白膜 → 480P 小样 → 1080P 成片 → 后期合成
  用户想跳步可以跳，但要先把"跳过会有什么后果"说清楚。


## ★ 步数很宝贵（这条最容易踩）

你**只有有限的步数**（默认 12 步）就把这一轮结束了。所以：

- **不要重复调同一个工具**。`plan_long_video` 同一个镜头调两次结果一模一样，
  纯属浪费。要拿多个镜头的排片，就用项目状态里已经给你的镜头列表**挑重点**，
  或者告诉用户"我看了第 1 个镜头的排片是 X，要不要我把所有镜头都排一遍"。
- **用户说"先只做 X、不要做 Y"就严格照做**。他说只排片，就不要顺手渲白膜 ——
  那是擅自扩大范围。
- **多在最后给结论，少在中间绕**。做完 2-3 步就该能说出有用的东西了。
- 如果确实需要很多步（比如逐镜处理 20 个镜头），**先告诉用户这需要多轮**，
  让他决定是"先处理前几个"还是"你自己分批说"。
"""


def build_agent_prompt(extra: str = "", registry: Optional[ToolRegistry] = None) -> str:
    """生成完整的 agent system prompt。

    registry 可显式传入（测试时用独立注册表；生产走全局 REGISTRY）。

    ★ 这里用 `replace` 而**不是** `str.format`。
      原因：这段提示词里全是 JSON 示例（`{"tool": "...", "args": {...}}`），
      走 format 就得把每一个花括号写成 `{{` `}}`。漏一个的后果不是"少显示几个字"，
      而是 `build_agent_prompt` 直接抛 KeyError/ValueError →
      **所有对话请求 500**，界面上是"AI 完全不能用了"。
      实测踩到过：新加了一段带 JSON 示例的规则，整个聊天全挂。
      提示词模板里出现用户可见的 JSON，就不该用 format。
    """
    reg = registry or REGISTRY
    body = AGENT_TOOLS_HEADER.replace("{tools}", reg.catalog_text(core_only=True))
    body += _promptlib_section()
    if extra:
        body += "\n\n" + extra
    return body


def _promptlib_section() -> str:
    """
    电影语言提示词库的用法段 + 129 条静态可用技法的目录。

    ★ 为什么要放进 system prompt 而不是等模型自己 discover 工具：
      实测（2026-09-29，image-01 对照出图）—— 用户说「拍出不安的感觉」时，
      模型自己写的提示词出来是青绿扁平光（无电影感）；换成库里的
      chiaroscuro / Dutch angle / low-key 之后，构图与光线才成立。
      也就是说：**不给目录，模型默认不会想到用摄影术语**。
      光有工具不够，得让它看见有哪些词可用。

    ★ 只列 129 条对静态出图有效的；剪辑/叙事那 21 条是时间结构概念，
      对单张图无效，放出来只会诱导模型选到无效项。
    """
    try:
        from . import prompt_library as _pl
        if not _pl.is_available():
            return ""
        cat = _pl.catalog_text(static_only=True)
    except Exception:  # noqa: BLE001
        return ""
    n = len([x for x in cat.splitlines() if x.strip()])
    if n <= 0:
        return ""
    return (
        "\n\n───────────────────────────────\n"
        "电影语言提示词库（出图时优先用这里的技法）\n"
        "───────────────────────────────\n"
        "用户说「不安 / 压抑 / 孤独 / 怀旧 / 梦幻」这类**氛围或镜头感觉**时，"
        "**不要自己现编提示词**——先调 `find_cinematic_techniques`，"
        "它会直接返回可用的完整提示词模板。\n"
        "自己写「房间很暗、气氛沉重」这种，出来是普通照片；"
        "写 chiaroscuro / Dutch angle / shallow depth of field 这种，"
        "出来才是电影画面（已实测验证）。\n"
        f"格式：slug|技法名|分类|何时使用　（当前只列对静态出图有效的 {n} 条，"
        f"剪辑与叙事类已排除——它们是时间结构概念，对单张图无效）\n"
        + cat
    )


# ──────────────────────── 项目上下文（给 agent 当"当前视野"）────────────────────────


async def build_project_context(project_id: str = "", *, limit_projects: int = 20) -> str:
    """拼一段「用户现在在看什么」的上下文塞进 system prompt。

    目的：让模型不用每次都先调 list_projects / list_assets 就能直接干活。
    拿不到 db（比如单测环境）时返回空串，不抛异常。

    ★★ 2026-09-28：**没传 project_id 时自己推断一个**。
      用户实测的原话是「上下文衔接不到」「流程跑不通」：他在卡片上点了「确认」，
      模型却回「当前对话里缺少上一步的待执行操作」——因为前端那一侧
      `State.currentProject` 没绑定（页面没打开过项目），于是 `project_id=null`，
      而老代码在这种情况下**直接 return，连流水线进度和"有一步正等用户确认"都不写**。
      模型看不到待确认的那一步，自然接不上。

      推断顺序（都是"用户当下最可能在做的那一个"）：
        ① **有待确认提案的项目** —— 用户点了确认就是在说这个项目；
        ② 最近更新的项目；
      并且把"这是推断出来的"写进上下文，别让模型以为是用户明确指定的。
    """
    try:
        db, _, _ = _services()
    except Exception:
        return ""

    lines: List[str] = []

    # 项目列表
    try:
        projects = await _call(db.list_projects, limit_projects) or []
    except Exception:
        projects = []

    if projects:
        lines.append("## 用户的项目")
        for p in projects:
            mark = "  ← 当前项目" if str(p.get("id")) == str(project_id) else ""
            lines.append(f"- {p.get('title')}（id={p.get('id')}）{mark}")
    else:
        lines.append("## 用户的项目\n（还没有任何项目，用户要开新片就先调 create_project）")

    # ── 没给 project_id：按"最可能在做的那一个"推断 ──
    inferred = False
    if not project_id and projects:
        try:
            from core import stages as _S
            for p in projects:
                _pid = str(p.get("id") or "")
                if _pid and _S.pending_gate(_pid):
                    project_id = _pid
                    inferred = True
                    break
        except Exception as _e:  # noqa: BLE001
            logger.debug("按待确认提案推断项目失败：%s", _e)
        if not project_id:
            # 最近更新的那个（list_projects 一般已按时间倒序，再兜一层排序）
            try:
                cand = sorted(projects, key=lambda x: str(x.get("updated_at") or ""),
                              reverse=True)
            except Exception:  # noqa: BLE001
                cand = projects
            project_id = str((cand[0] or {}).get("id") or "")
            inferred = bool(project_id)

    if not project_id:
        return "\n".join(lines)

    if inferred:
        lines.append("")
        lines.append(f"⚠ 调用方**没有指定** `project_id`，我（系统）按"
                     f"「有待确认提案 / 最近更新」推断当前项目是 **{project_id}**。"
                     f"如果用户说的不是这个项目，先跟他确认，别硬着头皮做。")

    try:
        chars = await _call(db.list_characters, project_id) or []
        scenes = await _call(db.list_scenes, project_id) or []
        shots = await _call(db.list_shots, project_id) or []
        script = await _call(db.get_script, project_id)

        lines.append("")
        lines.append(f"## 当前项目 {project_id} 的现状")
        lines.append(f"- 剧本：{'已有' if (script and (script.get('outline') or script.get('scenes'))) else '还没有'}")
        lines.append(f"- 镜头数：{len(shots)}；场景数：{len(scenes)}")

        # ★★ 把流水线进度直接写进上下文。
        #   为什么不省这一步、让模型自己调 pipeline_status：
        #     ① 省一次宝贵的工具步数（默认只有 12 步）；
        #     ② 更重要的 —— 模型**没理由**说"我不知道走到哪了"。
        #        进度已经在它眼前了，它就只能照着往下推。
        try:
            from core import stages as _S
            _sm = _S.summary(db, project_id)
            lines.append(f"- 📋 流水线进度：{_sm['headline']}")
            _bits = []
            for _s in _sm["stages"]:
                _mark = {"done": "✅", "current": "▶", "gate": "⏳",
                         "waiting": "·"}.get(_s["status"], "?")
                _bits.append(f"{_mark}{_s['title']}")
            lines.append("  " + " → ".join(_bits))
            if _sm.get("next"):
                _n = _sm["next"]
                lines.append(f"  ▶ 下一步：**{_n['title']}**（{_n['cost_label']}，"
                             f"预计 {_n['eta']}）→ 必须用 propose_step 提给用户，"
                             f"**不要**直接调 {_n['tool']}")
            if _sm.get("pending_gate"):
                _pg = _sm["pending_gate"]
                _t = _S.STAGE_BY_KEY.get(str(_pg.get("stage")))
                lines.append(f"  ⏳ **有一步正等着用户确认**："
                             f"{_t.title if _t else _pg.get('stage')}"
                             f"（{_pg.get('created_str')} 提的）。"
                             f"用户说「确认」→ 调 confirm_step；"
                             f"说要改 → 调 revise_step。")
        except Exception as _e:
            logger.debug("注入流水线进度失败（不影响对话）：%s", _e)

        if chars:
            pick = "、".join(
                f"@{c.get('name')}"
                + ("" if c.get("reference_image_path") else "(缺参考图)")
                for c in chars[:15]
            )
            lines.append(f"- 可引用的角色：{pick}")
        else:
            lines.append("- 可引用的角色：（还没有角色资产）")

        if scenes:
            pick = "、".join(
                f"@{s.get('name')}"
                + ("" if s.get("reference_image_path") else "(缺参考图)")
                for s in scenes[:15]
            )
            lines.append(f"- 可引用的场景：{pick}")

        # 逐镜进度：让模型不用先调 get_shot_progress 就知道做到哪了
        if shots:
            shots_sorted = sorted(shots, key=lambda s: (s.get("order_index") or 0))[:20]
            rows = []
            pending_previz = []      # 白膜渲好了、**等着用户确认**的镜头
            for idx, s in enumerate(shots_sorted, 1):
                segs = s.get("segments")
                if isinstance(segs, str):
                    try:
                        import json as _j
                        segs = _j.loads(segs)
                    except Exception:
                        segs = []
                n_ok = sum(1 for x in (segs or []) if x.get("status") == "ok")
                bits = [f"#{idx}", f"id={s.get('id')}"]
                if s.get("duration_seconds"):
                    bits.append(f"{s['duration_seconds']}s")
                bits.append(f"状态={s.get('status') or 'pending'}")
                pst = s.get("previz_status") or "none"
                if pst != "none":
                    bits.append(f"白膜={pst}")
                if n_ok:
                    bits.append(f"已出{n_ok}段")
                rows.append("  · " + " ".join(bits))
                if pst == "draft_ready":
                    pending_previz.append((idx, s.get("id"), s.get("previz_camera") or ""))
            lines.append("- 镜头列表（前 20 个，按顺序）：")
            lines.extend(rows)

            # ★★ 白膜等着确认的镜头要**点名并带上 id**。
            #   实测踩到：白膜渲完后用户说"驳回，理由码 LOOK"，模型只能回
            #   "请告诉我是第几个镜头" —— 它不知道哪个镜头在等确认，
            #   白白多一轮对话。这种"信息明明在库里、却没给模型"的摩擦，
            #   用户感受到的就是"这个 AI 怎么这么笨"。
            if pending_previz:
                lines.append("")
                lines.append("- ⏳ **有白膜渲好了、正等着确认的镜头**"
                             "（用户说「白膜不行 / 确认」时直接用这些 id，不要再问是哪个）：")
                for idx, sid, cam in pending_previz:
                    lines.append(f"  · 第 {idx} 个镜头 id={sid}"
                                 + (f"（运镜 {cam}）" if cam else ""))

            lines.append(
                "  ★ 生成流程是**三级闸门**，别跳步："
                "① plan_long_video 免费排片 → ② render_previz 渲白膜（本地免费、可无限重来）"
                "→ ③ review_previz 让用户确认 → ④ generate_preview 出便宜样片"
                "→ ⑤ generate_final 出成片（最贵，要用户明确同意）。"
            )

            # ★ 白膜参考通道状态直接写进上下文，省掉一次 check_previz_channel 调用，
            #   也**避免模型以为"白膜一定在约束生成"** ——
            #   通道不通时白膜其实只约束了提示词文字，不约束运动轨迹。
            #   ★★ 2026-09-25：加上"域名会不会变"这个关键区别。
            #      临时隧道的地址发给厂商后重启就失效，用户得重新确认一遍；
            #      模型在向用户解释"白膜有没有真的约束生成"时必须说准这一点。
            try:
                from core import publish as _pub
                _st = _pub.tunnel_status()
                _s = db.get_all_settings() or {}
                _base = _pub.public_base_url(_s)
                _named = _pub.named_tunnel_hostname(_s)
                if _named:
                    ch = (f"✅ 可用（**命名隧道，域名永久固定**：{_named}）"
                          + ("，隧道已就绪" if _st.get("permanent") else
                             "，隧道用到时自动起"))
                elif _base:
                    ch = f"✅ 可用（public_base_url 固定域名：{_base}）"
                elif _st.get("running"):
                    ch = (f"✅ 可用（临时隧道 {_st.get('base_url')}，"
                          f"**域名每次重启都变**）—— 要长期固定就配命名隧道")
                elif _pub.tunnel_available():
                    ch = "✅ 可用（装了隧道工具，用到时自动起；临时域名，会变）"
                else:
                    ch = ("❌ 不通 —— 白膜喂不进视频模型（本地仍可给用户看）。"
                          "两条修法：① 跑 `scripts\\装永久白膜隧道.ps1` 拿固定域名"
                          "（或配 public_base_url）；"
                          "② ★**走可灵官方 MCP（kling_white_model），那条路用"
                          "file_upload 传白膜，压根不需要公网 URL**。")
                lines.append(f"- 白膜参考通道：{ch}")
            except Exception:
                pass

            # ★ 可灵官方 MCP（免 API key）在不在，直接写进上下文 ——
            #   免得模型每次都先花一步去 check_kling_mcp。
            try:
                from core import kling_mcp as _km
                _kst = _km.status()
                if _kst.get("token_valid") or _kst.get("has_refresh"):
                    lines.append(
                        "- 可灵官方 MCP：✅ 已授权（**不用 API key**，token 自动续期）。"
                        "白膜出正式镜头可以直接用 `kling_white_model`"
                        "（motion_control，白膜自动上传，**不需要公网 URL**）；"
                        "角色一致性用 `kling_element` + `kling_generate` 的 "
                        "image_to_video。生成前先 `kling_status` 看灵感值余额。")
                else:
                    lines.append(
                        "- 可灵官方 MCP：未授权。用户问「可灵能不能不用 key」时，"
                        "答案是**能** —— 跑 `kling_mcp_auth` 拿 OAuth 链接，"
                        "用户在浏览器点一次「同意」即可，之后自动续期。")
            except Exception:
                pass

        lines.append(
            "- 提示：这些 id 已经给你了，**不要再调 list_projects 去猜**；"
            "只在用户明确要切换到别的项目时才查列表。"
        )
    except Exception:
        pass

    # ── Skill 目录：只注入**名字 + 一句话描述**（渐进式加载的第一层）──
    # ★ 为什么不注入正文：正文合计可达数万字符，会把系统提示词预算吃光，
    #   而且大部分和当前任务无关。模型需要时自己调 `use_skill`。
    try:
        # ★ extra_dirs 必须用**同一个**解析函数：两处各解析一遍的话，
        #   `get_registry()` 的缓存 key 会交替变化 → 每轮对话都重建注册表 +
        #   全量扫盘（详见 `skills.extra_dirs_from_settings` 的说明）。
        from .skills import extra_dirs_from_settings, get_registry
        _db2, _st2, _q2 = _services()
        _d = str((_st2 or {}).get("data_dir") or "")
        _reg = get_registry(_d or None,
                            extra_dirs=extra_dirs_from_settings(_st2))
        _cat = _reg.catalog()
        if _cat:
            lines.append("")
            lines.append("## 可用 Skill（用 `use_skill(名字)` 加载完整正文）")
            lines.append(_reg.catalog_text())
            _errs = _reg.errors()
            if _errs:
                lines.append("- ⚠ 有 %d 个 skill 目录没被加载（缺 SKILL.md 或名字不合法）：%s"
                             % (len(_errs), "；".join(e["path"] for e in _errs[:3])))
            lines.append("- 用户教了你一套**将来还能复用**的做法时，用 `learn_skill` 记下来；"
                         "对已有 skill 的后续经验用 `update_skill` 追加，不要重写整个 skill。")
        else:
            lines.append("")
            lines.append("## 可用 Skill\n- （无。用户教你可复用做法时用 `learn_skill` 建立）")
    except Exception as _e:
        logger.warning("注入 skill 目录失败（agent 仍可用 list_skills 查）：%s", _e)

    # ── MCP 服务器与其工具名：同样是"让模型知道有什么"，细节按需查 ──
    try:
        from .mcp_manager import manager_from_settings
        _db3, _st3, _q3 = _services()
        _mgr = manager_from_settings(_st3)
        _sum = _mgr.prompt_summary()
        if _sum:
            lines.append("")
            lines.append("## " + _sum)
        else:
            lines.append("")
            lines.append("## MCP 服务器\n- （无。用户想接外部工具/数据源时，"
                         "用 `mcp_add_server` 添加；stdio 会在本机起子进程，"
                         "**加之前先跟用户确认要运行什么**。）")
    except Exception as _e:
        logger.warning("注入 MCP 摘要失败（agent 仍可用 mcp_servers 查）：%s", _e)

    return "\n".join(lines)


# ──────────────────────── 生产类工具（长视频 / 白膜）────────────────────────
# 放在文件末尾 import：那批工具要复用本模块的 ok/err/_services/_need，
# 放在顶部 import 会形成循环。这里 import 是**为了执行注册副作用**，
# 所以即使看着"没用到"也不能删 —— 删掉之后 agent 就看不到
# plan_long_video / render_previz 这些工具了，而且不会报错，只是"工具莫名消失"。
from . import agent_tools_production as _prod  # noqa: E402,F401

# 流水线闸门（pipeline_status / propose_step / confirm_step …）与出图工具
# （make_character_look / make_scene_look / make_looks / refine_look）。
# ★ 这是「工作区只剩对话」之后的**唯一**推进入口：没有它，
#   用户在工作区里既看不到进度，也没法给角色出定妆照。
#   同样不能删 —— 删了之后那些工具会"莫名消失"且不报错。
from . import agent_tools_stages as _stages  # noqa: E402,F401

# Skill 系统（list_skills / use_skill / learn_skill / update_skill /
# audit_character_image / audit_character_batch）。
# 同样靠 import 副作用注册，不能删。
from . import agent_tools_skills as _skills  # noqa: E402,F401

# 通用 MCP（mcp_servers / mcp_tools / mcp_call / mcp_add_server …）。
# 同样靠 import 副作用注册，不能删。
from . import agent_tools_mcp as _mcp  # noqa: E402,F401

# 电影语言提示词库（find_cinematic_techniques / build_cinematic_prompt /
# list_cinematic_categories）。
# ★ 2026-09-29 新增：150 条电影摄影技法（crealens.ai，已实测净化）。
#   让 agent 自己按场景语义选技法，而不是让用户手写摄影术语 ——
#   实测普通提示词（"房间很暗气氛沉重"）出来是青绿扁平光，
#   换成技法提示词（chiaroscuro / shallow depth of field）才有电影感。
#   同样靠 import 副作用注册，不能删。
from . import agent_tools_promptlib as _promptlib  # noqa: E402,F401

# 白膜 3D 预演（previz_status / previz_render / previz_asset_cards）。
# ★ 2026-09-29 新增：Blender 引擎，**取代**原来的 A 档（ffmpeg 平面色块）
#   和 C 档（previz_multi）。原两档都达不到「渲染视频前的 3D 预演」这个要求：
#     · A 档：体块只有矩形，剧本写「俯卧泥水中」渲出来是站着的方块；
#             没有厚度、没有遮挡；y 是方块顶部，按中心给值就整体入地。
#     · C 档：相机距离靠内容包围盒反推 —— 喂像素把画面撑爆，喂米又把横向
#             跨度过大的人压成角落小方块（连调三轮才对）。
#   Blender 的世界单位就是米，没有二义性；相机/透视/遮挡/影子全由它自己解。
#   实测 12 镜 5.7 秒渲完，零费用。
# 同样靠 import 副作用注册，不能删。
from . import agent_tools_previz_blender as _previz_bd  # noqa: E402,F401
from . import agent_tools_lipsync as _lipsync_tools  # noqa: E402,F401

# 动作序列设计（design_action_sequence / review_action_sequence）。
# ★ 2026-09-29 新增：吸收 script-to-shot-engine v2.3.2。
#   用户原话「出的视频没人类爱看 —— 动作顺序不符合人类的观赏习惯」。
#   根因是**镜头密度**：之前一个 13~15 秒就是**一个连续长镜**；
#   人类习惯是 15 秒切 10+ 次（动作模式 ≥10、对峙模式 ≥5）。
#   而且镜头数 ≠ 招式数 —— 同一条动作因果链要拆成不同观看重点
#   （全景交代 → 中近景看接触 → 脚部特写看重心 → 主观看逼近 → 反应看受力）。
#   模块内自带**机器校验 + 失败回写重写**，靠提示词要求是拦不住的。
# 同样靠 import 副作用注册，不能删。
from . import agent_tools_action as _action  # noqa: E402,F401

# 按 L2 分段时间轴出片（produce_shot_segments / segment_dry_plan）。
# ★ 2026-09-30 新增：解决「合成首帧 → image_to_video 一条龙」导致的
#   「0–4s 应为空镜但人物从第 0 秒就在画」 —— 首帧决定视频，剧本节奏被吞。
#   新路：读 L2 拆段，每段按段动作分类（empty/appear/active/passive），
#   按段选首帧 → 段段出 → ffmpeg concat。详见 `core/shot_segments.py`。
# 同样靠 import 副作用注册，不能删。
from . import agent_tools_segments as _segments  # noqa: E402,F401

# 视频复刻（remake_analyze_video / remake_transcribe / remake_audit_dialogue）。
# ★ 2026-10-24 新增：用户原话「再研究下如何复刻视频，研究明白后做成一个单独的
#   功能点放到 VideoForge 里面」。核心是 `core/video_remake.py`：
#   参考视频 → 镜头表/景别/运镜/色调/构图/字幕版式/台词 → remake_spec.json。
#   台词走硅基流动 ASR（`core/asr_siliconflow.py`，Key 复用 tts_api_keys.siliconflow）。
# 同样靠 import 副作用注册，不能删。
from . import agent_tools_remake as _remake  # noqa: E402,F401

# 电商商品视频（ecommerce_plan_video / ecommerce_audit_copy /
# ecommerce_platform_spec / ecommerce_ai_labeling）。
# ★ 2026-10-24 新增：用户原话「最后再研究下电商行业，电商商品视频的技巧，
#   然后完美搜集做好后，单独做成一个功能，回馈到 VideoForge」。
#   核心是 `core/ecommerce_video.py` + `skills_builtin/ecommerce-product-video/rules.json`：
#   平台规格表（13 个 placement）/ 7 品类节拍 / 178 条合规词 / AI 标识法定义务。
#   纯本地零付费（不调任何生成模型），平台规格查不到就如实 unknown。
# 同样靠 import 副作用注册，不能删。
from . import agent_tools_ecommerce as _ecom  # noqa: E402,F401

# 运行环境体检（env_health）——客户 skill「缺依赖」时的第一站。
# ★ 2026-10-25 新增：安装版是冻结包（无 pip、裁过依赖），自蒸馏 skill 常 import 失败；
#   这个工具只读地报告「缺什么、怎么补」，不碰生成/计费链路。靠 import 副作用注册。
from . import agent_tools_env as _env  # noqa: E402,F401


@REGISTRY.register(
    "audit_visual_consistency",
    "**出片之后**的角色一致性自检：拿角色参考图，去和每个镜头**真实抽帧**里检出的"
    "人脸做客观比对，把「哪一镜最不像」排在前面。"
    "★ 只读、不花钱、不阻断；结论是「建议复核」而不是判决（没有身份识别模型）。"
    "用户问「这几镜是不是同一个人 / 脸有没有漂」时用这个。",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True},
     "max_frames": {"type": "number",
                    "desc": "每个镜头最多抽几帧比对，默认 3（抽多了慢）"}},
    needs_project=True, writes=False,
)
async def t_audit_visual_consistency(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "project_id")
    if bad:
        return bad
    try:
        db, _, _ = _services()
        from core.consistency_audit import audit_visual_consistency  # type: ignore
        try:
            _mf = int(float(args.get("max_frames") or 3))
        except Exception:
            _mf = 3
        r = await _call(audit_visual_consistency, db, args["project_id"], _mf)
        if isinstance(r, dict) and r.get("ok"):
            return ok(result=r)
        return err(str((r or {}).get("error") or "视觉一致性自检失败"))
    except Exception as e:  # noqa: BLE001
        logger.exception("audit_visual_consistency failed")
        return err(f"视觉一致性自检失败：{type(e).__name__}: {e}")
