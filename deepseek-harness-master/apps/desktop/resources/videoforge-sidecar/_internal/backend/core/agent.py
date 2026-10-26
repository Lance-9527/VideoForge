"""
VideoForge · Agent 循环

把「聊天」变成「能干活」：
    用户消息
      ↓
    LLM（system prompt 里带着工具目录）
      ↓
    ┌─ 输出是 JSON 动作？ ─┐
    │ 是                    │ 否
    ↓                       ↓
  执行工具               直接作为答复返回
    ↓
  结果回灌成 user 消息
    ↓
  继续下一轮（最多 max_steps 轮）
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
import re
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

from .agent_tools import REGISTRY, build_agent_prompt, _call, _services

logger = logging.getLogger("videoforge.agent")

MAX_STEPS_DEFAULT = 12

# 匹配围栏代码块（模型有时会包 ```json ... ```）
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


# ★★ 2026-10-20（用户截图的真 bug 之一）：当前项目 id 的**兜底**来源。
#
#   症状：AI 说"这一步做完了，但我读不到这个项目的进度 —— 它可能已经被删掉了"，
#   并甩出一个"看看我有哪些项目"按钮（用户截图里那坨裸 HTML 就是这个按钮）。
#
#   根因：工具执行时用的是**模型自己传的** project_id。
#   `needs_project=True` 只表示"这个工具需要 project_id"，
#   但**没有任何代码自动注入**（agent_tools.py 的注释原话：
#   「目前没有任何代码读它」）。模型把 id 记错 / 传了剧本 id /
#   干脆幻觉一个，就直接被判"项目不存在"。
#
#   修法：把"本轮对话的当前项目"用 contextvars 存下来（并发安全），
#   模型没传 / 传错时自动改用当前项目，并**明确告诉模型**改过了，
#   这样它下一轮就知道该用哪个 id，不会反复撞。
CURRENT_PROJECT_ID = ContextVar("vf_current_project_id", default="")
# ★★ 2026-10-20：用户**这一轮明确指着的项目**（请求里带过来的）。
#   为什么要和 CURRENT_PROJECT_ID 分开：
#     CURRENT_PROJECT_ID 是"agent 以为自己在哪个项目上"，**模型可以改写它**
#     （模型给工具传了别的 project_id 就跟着变）；
#     而这一个是"**用户说话时指着哪个项目**"—— 不能被模型改。
#   实测（用户实机）：用户明明在《日常反转》里点了项目、然后说「继续」，
#   agent 却带着另一个项目的 id 去跑 confirm_step ⇒ **图出到了别的项目上**，
#   用户看到的是"我这边什么都没发生"。
REQUEST_PROJECT_ID = ContextVar("vf_request_project_id", default="")


@dataclass
class AgentStep:
    """一步的记录，用于前端展示与审计。"""

    index: int
    kind: str                      # "tool" | "text" | "error"
    tool: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    result: Optional[Dict[str, Any]] = None
    text: str = ""

    def summary(self) -> str:
        if self.kind == "tool":
            flag = "OK" if (self.result or {}).get("ok") else "FAIL"
            return f"[{self.index}] {self.tool} -> {flag}"
        if self.kind == "error":
            return f"[{self.index}] error: {self.text[:80]}"
        return f"[{self.index}] text"


@dataclass
class AgentRun:
    """一次完整对话轮的结果。"""

    reply: str = ""                        # 给用户看的最终答复
    steps: List[AgentStep] = field(default_factory=list)
    # done（模型给了自然语言答复）| max_steps（步数用尽）
    # | error（LLM 调用失败）| awaiting_user（提案已登记，等用户确认 —— 见 await_user_text）
    stopped_because: str = ""
    tool_calls: int = 0

    @property
    def trace(self) -> List[str]:
        return [s.summary() for s in self.steps]


# ──────────────────────── 动作解析 ────────────────────────


def parse_action(text: str) -> Optional[Dict[str, Any]]:
    """从模型输出里抽出工具调用，没有则返回 None。

    容忍这些形式：
        {"tool": "...", "args": {...}}
        ```json\\n{...}\\n```
        {"tool":"...", "args":{...}} 前面还有一句解释文字
        {{"tool": "...", "args": {...}}}   ← 双花括号（见下面的说明）

    ★ 为什么必须容忍**双花括号**（实测事故）：
      提示词里的 JSON 示例曾经因为 `str.format()` 的转义要求写成 `{{...}}`，
      模型（MiniMax 实测）会**逐字照抄**这个转义形式。
      解析不出来时，agent 把这串文本当成"最终答复"直接回给用户 ——
      界面上就是"AI 只回了一串代码，什么都没干"。
      提示词那边已经改成单花括号（用 `scripts/_undouble_prompt_braces.py` 修过），
      但模型自己也可能主动这么写（不少模型见过太多 Python 模板字符串），
      所以这里再加一层兜底：把最外层多余的一对花括号剥掉再试。
    """
    if not text:
        return None
    raw = text.strip()

    # ★★ 2026-09-28：MiniMax（M3.1/M3）**自带一套工具调用标记**，必须认。
    #
    #   实测原始输出长这样（M3.1 走 MiniMax 的流式接口时）：
    #       ]<]minimax[>[<tool_call>tool": "list_projects", "args": {"limit": 20}}
    #       </tool_call>
    #   注意两件事：
    #     ① 前面那串 `]<]minimax[>[` 是它的**通道分隔符**，不是内容；
    #     ② `<tool_call>` 里面是 `tool": ...}}` —— **开头那个 `{"` 被吞了**
    #        （流式分片时被前面的解析吃掉了），所以不能指望它能被 json.loads 直接解。
    #
    #   不认识它的后果很严重：agent 解不出动作 ⇒ 把这串**原始标记当答复回给用户**
    #   ⇒ 界面上就是"AI 只回了一串代码，什么都没干"，工具一次都没执行
    #   （`tests/e2e_agent.py` 实测就是这么红的：有 delta、但**没有 step/result 事件**）。
    if "<tool_call>" in raw or "]<]minimax[>" in raw:
        _tc = re.search(r"<tool_call>\s*(.*?)\s*</tool_call>", raw, re.S)
        seg = (_tc.group(1) if _tc else raw.split("<tool_call>", 1)[-1])
        seg = seg.replace("]<]minimax[>", "").strip()
        # 缺开头的 `{` 时补回来：`tool": "x", "args": {...}}` → `{"tool": ...}`
        if not seg.startswith("{") and re.match(r'^"?tool"?\s*:', seg):
            seg = "{" + seg
        # ★ 实测还有更碎的一种：键名的**开引号也被吃了** ——
        #   `{tool": "list_projects", "args": {"limit": 20}}`
        #   （只有 `tool"` 没有 `"tool"`）。这种 json.loads 一定失败，
        #   所以先把"缺开引号的键名"补回去。
        seg = re.sub(r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)"\s*:', r'\1"\2":', seg)
        if not seg.endswith("}") and seg.count("{") > seg.count("}"):
            seg += "}"
        for cand in (seg, seg.rstrip("}") + "}"):
            try:
                obj = json.loads(cand)
                if isinstance(obj, dict) and isinstance(obj.get("tool"), str):
                    args = obj.get("args")
                    if not isinstance(args, dict):
                        args = {k: v for k, v in obj.items() if k != "tool"}
                    return {"tool": obj["tool"].strip(), "args": args}
            except Exception:  # noqa: BLE001
                continue
        logger.warning("认出了 MiniMax 的 <tool_call> 标记，但里面的 JSON 解不开：%s",
                       seg[:120])

    # 去掉 markdown 围栏
    m = _FENCE_RE.search(raw)
    if m:
        raw = m.group(1).strip()

    # 优先整体解析
    candidates: List[str] = [raw]

    # 退一步：截取第一个平衡的 {...}
    if not raw.startswith("{"):
        start = raw.find("{")
        if start >= 0:
            depth = 0
            for i in range(start, len(raw)):
                if raw[i] == "{":
                    depth += 1
                elif raw[i] == "}":
                    depth -= 1
                    if depth == 0:
                        candidates.append(raw[start:i + 1])
                        break

    # ★ 统一给每个候选再补一个"剥掉最外层多余花括号"的版本。
    #   为什么不只在整条以 {{ 开头时才剥：实测的形式是
    #       `好的，我来查：{{"tool": ...}}`
    #   —— 前面有解释文字，于是整条不以 {{ 开头，走不到"整体剥壳"那条路；
    #   而平衡括号截出来的是 `{{"tool": ...}}`，照样解不开。
    #   所以剥壳要对**每一个**候选都做，不能只看开头。
    for c in list(candidates):
        s = c.strip()
        if s.startswith("{{") and s.endswith("}}"):
            candidates.append(s[1:-1].strip())

    # 兜底：模型把 JSON 当成**字符串**塞进 JSON 里
    for c in list(candidates):
        s = c.strip()
        if s.startswith('"') and s.endswith('"') and len(s) > 2:
            try:
                v = json.loads(s)
                if isinstance(v, str) and v.strip().startswith("{"):
                    candidates.append(v.strip())
            except Exception:
                pass

    for c in candidates:
        try:
            obj = json.loads(c)
        except Exception:
            # 用项目自带的 JSON 修复器再试一次
            # ★ 2026-09-26 修：这一行原来调的是 `LLMClient.extract_json(c)`，
            #   但 `extract_json` 是 `core.llm` 的**模块级函数**，
            #   `LLMClient` 只有 __init__ / resolve_endpoint / _client / chat
            #   —— 没有这个方法。于是每次都抛 AttributeError，被下面那个
            #   `except Exception` 吞成 `obj = None`：**修复器从来没生效过**。
            #   它正是为"模型吐了带尾逗号/单引号/全角标点的 JSON"写的，
            #   而中文模型这么写很常见 ⇒ 解析不出来 ⇒ agent 把那串代码
            #   当成最终答复回给用户，界面上就是"AI 只回了一串代码，什么都没干"
            #   （与本文件 docstring 记的那次事故同一个表现）。
            #   `tests/test_agent.py` 原有 7 个写法全是合法 JSON，走的是
            #   上面那条 `json.loads` 分支，所以这行死代码**测不出来**；
            #   已在那里补 a8/a9/a10 三条用例钉住这条路。
            try:
                from .llm import extract_json as _extract_json  # type: ignore
                obj = _extract_json(c)
            except Exception:
                obj = None
        if isinstance(obj, dict) and isinstance(obj.get("tool"), str):
            args = obj.get("args")
            if not isinstance(args, dict):
                args = {}
            # 允许扁平写法 {"tool":"x","project_id":"p1"}
            extra = {k: v for k, v in obj.items() if k not in ("tool", "args")}
            args = {**extra, **args}
            return {"tool": obj["tool"].strip(), "args": args}
    return None


# ──────────────────────── 执行 ────────────────────────


async def execute_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """执行一个工具调用（未知工具/异常都收敛成 ok=false）。"""
    spec = REGISTRY.get(name)
    if not spec or not spec.handler:
        avail = ", ".join(REGISTRY.names())
        return {"ok": False, "error": f"没有这个工具：{name}。可用工具：{avail}"}
    # ★★ 2026-09-27 第 46 轮：`*_id` 参数必须**长得像 id**。
    #
    #   用户实测（他对话里的原话就是工具的报错）：
    #       render_previz(shot_id="第一个镜头的ID")  → 「分镜不存在：第一个镜头的ID」
    #   模型又把这段技术报错原文倒给了用户（还带 HTML 转义），用户看到的是
    #   "AI 坏了" —— 而真实原因是模型**把说明文字当成了 id**。
    #   这和"把该问用户的那句话当成 idea"是同一类（见
    #   `agent_tools_stages._looks_like_placeholder`），只是发生在**直接调工具**时，
    #   原来的守卫只覆盖了阶段提案那两条路。
    #
    #   判据刻意保守：id 里**不该有中日韩文字、空格、换行**，也不该很长。
    #   真 id 是 uuid / `p1` / `api_1789…` 这种，全部放行。
    _ID_KEYS = ("id", "cid", "sid", "pid", "tid", "gid")
    bad_ids = []
    for k, v in (args or {}).items():
        if not isinstance(v, str):
            continue
        kk = str(k).lower()
        if not (kk.endswith("_id") or kk in _ID_KEYS):
            continue
        s = v.strip()
        if not s:
            continue
        if re.search(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", s) \
                or re.search(r"\s", s) or len(s) > 72:
            bad_ids.append((k, s[:40]))

    # ★★ 2026-09-28 第 74 轮：**可选**的 id 参数收到说明文字时，**丢掉它**继续，
    #   而不是整条调用失败。
    #
    #   用户截图里的那条报错就是这件事：
    #       `draft_task_id 要的是真实 id，但收到的是「生成的草稿任务 ID（如果适用）」`
    #   模型在"还没出过草稿"的情况下，把**参数说明**当成了值传进来。
    #   这个参数本来就是可选的（`generate_final` 的 desc 写着"可选"），
    #   意即"有就带上、没有就别带" —— 那么模型传了一句废话时，
    #   **正确的处理是当作没传**，而不是让整个出片动作失败、
    #   再把一段技术报错打到用户脸上（用户原话：「视频生成有bug」）。
    #
    #   只有**必填**的 id 参数才硬失败（那种情况下继续跑一定拿不到正确结果）。
    if bad_ids:
        params = getattr(spec, "params", None) or {}
        dropped, fatal = [], []
        for k, s in bad_ids:
            meta = params.get(k) or {}
            (fatal if meta.get("required") else dropped).append((k, s))
        if dropped:
            for k, _s in dropped:
                args.pop(k, None)
            logger.info("丢掉模型填错的可选 id 参数（当没传）：%s",
                        ", ".join(k for k, _ in dropped))
        if not fatal:
            # 已经清理干净 → 继续执行（下面照常用清理后的 args）
            pass
        else:
            _k, _v = fatal[0]
            return {
                "ok": False,
                # ★ 两段话分开：`error` 给模型看（技术、可执行），
                #   `say_to_user` 给用户看（人话）。前端与 agent 都不会把
                #   技术报错直接甩给用户 —— 这正是用户反复抱怨的那件事。
                "error": (f"`{_k}` 要的是**真实 id**，但收到的是「{_v}」——"
                          f"这看起来是说明文字，不是一个 id。"
                          f"先查一遍列表拿到真实的 id（例如 list_shots / list_assets / "
                          f"list_projects），再把那串 id 原样传进来。"
                          f"**不要**把「第一个镜头的 ID」这种话当参数值。"),
                "say_to_user": ("这一步需要先拿到真实的 id 才能真正执行 —— "
                                "我先把该查的查出来（列表），再动手。"),
                "internal": True,
                "bad_args": [{"key": k, "value": s} for k, s in fatal],
            }
    # ★★ 2026-09-27：`needs_project=True` 的工具，project_id 必须**真的存在**。
    #
    #   以前所有工具都只是"拿这个 id 去查"，查不到就当空处理 —— 于是
    #   `skip_step` 会给一个**不存在的项目**记一条「已跳过立项」，
    #   `make_looks` 会回「没有缺参考图的资产 —— 这一步已经完成了」。
    #   用户看到的是"做完了"，而那个项目根本不存在（模型记错 id / 项目被删）。
    #   这正是用户反复撞到的那个形状：**报成功，但其实什么都没做**。
    #
    #   放在这里而不是逐个工具里加：这是**唯一的**派发口
    #   （agent 循环与阶段执行都走它），加一次就覆盖全部工具，
    #   包括以后新写的；漏掉一个工具的可能性就没有了。
    if spec.needs_project:
        db, _, _ = _services()
        pid = str(args.get("project_id") or "").strip()
        cur = (CURRENT_PROJECT_ID.get() or "").strip()
        req = (REQUEST_PROJECT_ID.get() or "").strip()
        # ★★ 2026-10-20：用户这一轮**明确指着**的项目优先于模型传的。
        #   原来只在"模型传的 id 不存在"时才纠正；可模型传的是**另一个存在的**
        #   项目时就被放行了 —— 实测：用户在《日常反转》里说「继续」，
        #   agent 带着另一个项目的 id 去跑 confirm_step，图出到了那个项目上。
        if req and pid and pid != req:
            logger.info("模型传了 project_id=%s，但用户这一轮指的是 %s → 改用用户的",
                        pid, req)
            args["project_id"] = req
            args["__vf_project_corrected_from"] = pid
            pid = req
        # ★ 2026-10-20：模型没传 id -> 用当前项目
        elif not pid and cur:
            pid = cur
            args["project_id"] = cur
        elif pid and cur and pid != cur:
            # ★ 模型传了别的 id -> 先看它存不存在；
            #   不存在就**改用当前项目**（而不是直接判"项目不存在"）。
            if not (await _call(db.get_project, pid)):
                logger.info(
                    "模型传的 project_id(%s) 不存在，改用当前项目 %s", pid, cur)
                args["project_id"] = cur
                args["__vf_project_corrected_from"] = pid
                pid = cur
        if pid:
            proj = await _call(db.get_project, pid)
            if not proj:
                return {
                    "ok": False,
                    "error": f"项目不存在：{pid}。可能是项目已被删除，或 id 记错了。"
                             f"先调 list_projects 看现在有哪些项目，拿到正确的 id 再试。",
                    "project_id": pid, "project_missing": True,
                }
    try:
        result = await spec.handler(args or {})
        if not isinstance(result, dict):
            return {"ok": True, "result": result}
        return result
    except Exception as e:
        logger.exception("tool %s failed", name)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ──────────────────────── ★ 确认闸门（代码强制）────────────────────────
#
# 为什么必须在**代码里**拦，而不是在 system prompt 里写"请先确认"：
#   实测过，模型会无视。一轮 12 步它可以连着调 generate_script →
#   plan_shots → generate_preview，用户还没说"开始"钱就花了。
#   提示词是建议，`if` 才是规则。


def _gate_check(name: str, args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """被闸门保护的工具，在没有待确认提案时拒绝执行。

    返回 None = 放行；返回 dict = 拒绝并把这个 dict 当作工具结果。
    """
    try:
        from .stages import GATED_TOOLS, STAGE_BY_KEY, pending_gate
    except Exception:                      # 闸门模块不可用时不阻断业务
        return None

    if name not in GATED_TOOLS:
        return None

    pid = str(args.get("project_id") or "").strip()
    if not pid:
        # 拿不到项目就没法判断闸门状态 —— 放行，让工具自己报"缺 project_id"。
        return None

    pend = pending_gate(pid)
    if pend:
        # 有待确认提案：只允许执行**那一步**，不许"顺手"调别的付费工具。
        want_tool = (STAGE_BY_KEY.get(str(pend.get("stage"))) or {})
        want_tool = getattr(want_tool, "tool", None)
        if want_tool == name or name in _TOOLS_OF_STAGE(str(pend.get("stage"))):
            return None
        # ★★ 2026-09-27 第 58 轮修一个**死锁**（用户实测原话："还是有bug，
        #    卡在我让它出角色后，没在资产上反馈"）：
        #
        #    原来的逻辑是"只要挂着任何提案，别的被闸门管的工具一律拒绝" ✗。
        #    实测现场：项目上挂着一条 `looks` 的待确认提案，用户接着说
        #    「可以了」（意思是继续做角色/资产），模型去调 `extract_assets`
        #    —— 被这条规则挡住 ⇒ **用户说了"可以"也什么都动不了**，
        #    而且模型只能把"请先确认那一步"这种技术话倒给用户看。
        #
        #    闸门要防的是「**同一步**没经用户同意就花钱」，不是把整个项目冻住。
        #    所以：提案的步骤与这个工具**不属于同一步** ⇒ 放行。
        _my_stage = ""
        for _k, _st in STAGE_BY_KEY.items():
            if getattr(_st, "tool", None) == name or name in _TOOLS_OF_STAGE(_k):
                _my_stage = _k
                break
        # 判据用**阶段顺序**（不是简单的不相等）：
        #   · 工具属于**更早/并行**的阶段（例如挂起 looks、用户却说要先补角色
        #     ⇒ assets 这一步）⇒ **放行**：用户说了"可以"就该能干活；
        #   · 工具属于**更晚**的阶段（例如挂起 looks 却去跑 final）⇒ **拦住**：
        #     那正是"跳过确认一路跑到最贵那一步"的路子 ✗✗。
        try:
            from .stages import STAGE_ORDER as _ORDER
        except Exception:                       # noqa: BLE001
            _ORDER = []
        _pend_key = str(pend.get("stage"))
        _ok_order = (_my_stage and _pend_key in _ORDER and _my_stage in _ORDER
                     and _ORDER.index(_my_stage) < _ORDER.index(_pend_key))
        if _ok_order:
            logger.info("闸门放行：挂起的是 %s（第 %d 步），这次要做的是 %s（第 %d 步，"
                        "没有跳过它）：%s", _pend_key, _ORDER.index(_pend_key) + 1,
                        _my_stage, _ORDER.index(_my_stage) + 1, name)
            return None
        return {
            "ok": False,
            "error": (
                f"「{name}」正是**挂着待确认的那一步**（{pend.get('stage')}），"
                f"用户还没答复，不能重复发起。"
                f"**不要**把这个内部细节讲给用户听 —— 用一句人话问他："
                f"「刚才那一步还等你点确认，你要先做它，还是先做别的？」"
            ),
            "blocked_by": "gate",
            "pending_stage": pend.get("stage"),
            # ★ 给模型一句"可以直接照着说"的话，避免它自己去编技术解释 ✗
            "say_to_user": "刚才那一步还在等你确认。你是想先做它，还是先做别的？",
        }

    return {
        "ok": False,
        "error": (
            f"「{name}」是会花钱/耗时的操作，**必须先让用户确认**。"
            f"正确做法：先调 pipeline_status 看下一步，再调 propose_step "
            f"(stage=合适的阶段) 把这一步提给用户，等用户说「确认」之后，"
            f"调 confirm_step 真正执行。"
            f"不要跳过确认直接调 {name}。"
        ),
        "blocked_by": "gate",
        "hint": "先 propose_step，再等用户确认，然后 confirm_step。",
        # ★★ 2026-09-27 第 71 轮：带上"这一步属于哪一阶段"。
        #   为什么：实测（用户原话「在对话上流程还是跑不通」+ 截图）——
        #   被拦下之后，上面那段"请改用 propose_step"对模型来说只是**一句话**，
        #   MiniMax 收到它**并没有去调 propose_step**，而是把这段内部规则
        #   原样倒给用户看，流程就断在那里了（截图里正是这一幕）。
        #   ⇒ 拦下时顺手替模型把提案登记好（登记不执行、不花钱），
        #     再把确认卡片交给用户。安全性不变：真正花钱的仍是 confirm_step。
        "auto_stage": _tool_stage(name),
    }


def _tool_stage(name: str) -> str:
    try:
        from .stages import tool_stage
        return tool_stage(name)
    except Exception:                       # noqa: BLE001
        return ""


def _TOOLS_OF_STAGE(stage_key: str) -> set:
    """某个阶段执行时**顺带**允许调用的工具（多资产阶段会调单资产工具）。"""
    try:
        from .stages import SIDE_TOOLS
        return set(SIDE_TOOLS.get(stage_key, set()))
    except Exception:                       # noqa: BLE001
        return set()


async def _auto_propose_for_blocked(tool: str, args: Dict[str, Any],
                                    blocked: Dict[str, Any],
                                    ) -> Optional[Dict[str, Any]]:
    """被闸门拦下、且**没有待确认提案**时：替模型把这一步登记成待确认提案。

    ════════════════════════════════════════════════════════════════════
    为什么要有它（2026-09-27 第 71 轮，用户实测）
    ════════════════════════════════════════════════════════════════════
    用户原话：「在对话上流程还是跑不通」+ 一张截图：模型去调 `plan_shots`，
    被闸门拦下，然后**把那段内部拒绝语原样讲给用户听**（"系统限制：为了确保
    您完全掌控…"），流程就停在那儿。

    也就是说：闸门本身是对的（钱必须先经用户点头），但**拒绝的姿态是错的** ——
    它把"继续往下走"的责任推给了一个不一定照做的模型（实测 MiniMax 就没照做）。
    `propose_step` 是一条**记账**动作（不执行、不花钱、不起任务），
    完全可以由代码替模型做掉，然后把卡片交给用户。

    ⇒ 拦下时自动登记提案；登记成功就把**卡片**返回给上层（前端会画成
      可点的「确认」按钮），登记失败则退回原来的拒绝语（绝不假装成功）。

    ★ 安全性没有变弱：
      · 真正花钱的是 `confirm_step` / `/stage/confirm`，它仍然需要用户点头；
      · 返回的卡片走 `_AWAIT_USER_TOOLS` 那条路 —— **这一轮到此结束**，
        模型没有机会在同一轮里自己确认（见 `await_user_text` 的说明）。
    """
    stage = str(blocked.get("auto_stage") or "")
    pid = str((args or {}).get("project_id") or "")
    if not (stage and pid):
        return None
    # 挂着**别的**待确认提案时不要自作主张（那种情况是"同一步不许重复发起"），
    # 让模型按人话问用户"先做哪个"。
    if blocked.get("pending_stage"):
        return None
    propose_args: Dict[str, Any] = {"project_id": pid, "stage": stage}
    for k, v in (args or {}).items():
        if k in ("project_id", "stage", "args", "note"):
            continue
        propose_args[k] = v
    try:
        res = await execute_tool("propose_step", propose_args)
    except Exception as e:                  # noqa: BLE001
        logger.warning("自动登记提案异常（退回普通拒绝）：%s", e)
        return None
    if not res.get("ok"):
        # 登记不了（参数不全 / 阶段前置没满足）→ 如实退回原来的拒绝语，
        # 让模型去解释"为什么现在做不了"，而不是编一个能做的假象。
        logger.info("自动登记提案失败（%s）：%s", tool, res.get("error"))
        return None
    res["auto_proposed"] = True
    res["blocked_tool"] = tool
    res["say_to_user"] = res.get("ask_user") or ""
    return res


# ──────────────── ★★ 提案之后必须把这一轮交还给用户（2026-09-26 修）────────────────
#
# 【修的是什么】
#   闸门 `_gate_check` 只保证"**没有待确认提案**时，付费工具不许调"。
#   而 `propose_step` **本身就是模型能调的工具** —— 于是模型可以在**同一轮**里
#   自己提、自己确认：
#
#       第 1 步  propose_step(project_id, stage="draft", args={...})
#                  → `stages.set_pending(...)` 登记待确认提案
#       第 2 步  confirm_step(project_id)
#                  → `stages.pending_gate(pid)` 有值（正是它自己刚写进去的）
#                  → `consume_pending` → `run_stage` → **generate_preview 真的执行**
#
#   用户连那张卡片都没看见。也就是说：闸门的强度原来只等于
#   "模型必须先调一次 propose_step" —— 一个**记账动作**，不是用户确认。
#   这和 `stages.py` 开头写的"需要用户点头的步骤**必须**先 propose 再 confirm"、
#   以及本文件上面那句"提示词是建议，`if` 才是规则"，是不符的。
#
# 【修法】
#   提案类工具成功后**立刻结束这一轮**，把卡片交给用户看。
#   模型没有机会在同一轮里接着确认 —— 用户必须先看到卡片、并且**再发一条消息**。
#
# ★ 诚实边界：这是**必要条件**级的保证，不是充分条件。
#   用户下一条消息如果只说"随便"，模型仍然可以去确认（但那时用户已经
#   有机会看到卡片了）。要更强的保证，只能让"确认"从**用户点击**的通道进来 ——
#   `/api/projects/{pid}/stage/confirm`（前端按钮）本来就是那条通道，
#   它和对话里的确认都走 `agent_tools_stages.run_stage`。
_AWAIT_USER_TOOLS = ("propose_step", "revise_step")


def await_user_text(tool: str, result: Optional[Dict[str, Any]]) -> str:
    """提案类工具执行成功后，返回"该交给用户看的话"；否则返回空串。

    ★ 抽成纯函数是为了**能直接测** —— 否则验这一条要起一个 app、
    再造一个脚本化的 LLM 才能间接看到。
    """
    if tool not in _AWAIT_USER_TOOLS:
        return ""
    r = result or {}
    if not r.get("ok"):
        return ""
    return str(r.get("ask_user") or "").strip()



def _result_to_message(tool: str, result: Dict[str, Any]) -> str:
    """把工具结果回灌成给模型看的消息。

    成功时保留数据但截断，避免爆上下文；失败时把 error 原样给出。
    """
    if result.get("ok"):
        payload = json.dumps(result, ensure_ascii=False, default=str)
        if len(payload) > 4000:
            payload = payload[:4000] + "…(已截断)"
        return f"[工具 {tool} 执行成功]\n{payload}\n\n请继续：需要再调工具就直接输出 JSON 动作，否则用自然语言回复用户。"
    msg = (
        f"[工具 {tool} 执行失败]\n{result.get('error', '未知错误')}\n\n"
        f"请把上面这条错误如实告诉用户，或换个方式重试。不要假装成功。"
    )
    # ★ 有些失败**已经替你想好了该怎么说**（`say_to_user`）。直接给它，
    #   免得模型把内部规则原封不动倒给用户 —— 实测（2026-09-27 用户截图）
    #   模型收到拒绝语后就是这么干的，用户看到的是一段系统设计说明。
    say = str(result.get("say_to_user") or "").strip()
    if say:
        msg += f"\n\n可以直接对用户这样说（用自己的话也行，意思别变）：\n「{say}」"
    return msg


# ──────────────────────── 主循环 ────────────────────────


#: 用户表示"同意/开始"的说法（用于**确定性确认**，见 `maybe_confirm_pending`）
_CONFIRM_WORDS = ("确认", "確定", "确定", "开始做", "开始吧", "开始", "都做", "可以",
                  "好的", "好", "行", "同意", "继续", "go", "ok", "yes", "同意开始")


def _looks_like_confirm(text: str) -> bool:
    """用户这句话是不是在"点头"？（判断刻意保守：短句 + 命中确认词）"""
    t = str(text or "").strip().lower()
    if not t or len(t) > 24:
        return False
    # "不要/先别/改一下/等一下" 这类否决优先
    if any(w in t for w in ("不", "别", "改", "换", "等", "暂停", "算了")):
        return False
    return any(w in t for w in _CONFIRM_WORDS)


async def maybe_confirm_pending(project_id: str, user_text: str) -> Optional[AgentStep]:
    r"""**用户点头 → 直接把待确认的那一步执行掉**（不依赖模型愿不愿意调工具）。

    ★★ 2026-09-28（用户第 1、5 条）：
        「重复问用户定妆，多次问问完不办事！」
        「对话中出现的确认开始做在用户点击后要直接开始做，
          而不是还要输入确认再动手！！！」

    实测病根：点卡片上的按钮走的是 HTTP `/stage/confirm`（会执行）；
    但用户在输入框里**打字**说「确认」时，**执行与否完全取决于模型这一轮
    愿不愿意输出 `{"tool":"confirm_step"}`** —— 而实测它会**只在嘴上说**
    「好的，我已收到您的确认，并开始为以下角色重新生成定妆照：…请稍等」
    然后**一个工具都没调**（截图里连一个 ✓ 步骤都没有）。
    用户看到的就是"问完不办事"。

    这里把这条**确定性**做掉：只要
      · 用户这句话是在点头（`_looks_like_confirm`），且
      · 项目里**确实有一条待确认提案**，
    就直接执行 `confirm_step`（与点按钮走**同一个** `run_stage`，
    闸门、参数校验、防重复全都在），并把这一步作为 AgentStep 返回。
    模型只是**事后解说**，没有"要不要动手"的自由。
    """
    if not project_id or not _looks_like_confirm(user_text):
        return None
    try:
        from .stages import pending_gate
        pend = pending_gate(project_id)
    except Exception:  # noqa: BLE001
        return None
    if not pend:
        return None
    cap = str(pend.get("stage") or "")
    logger.info("用户点头（%r）且有待确认提案「%s」→ 直接执行（不等模型）",
                str(user_text)[:12], cap)
    try:
        result = await execute_tool("confirm_step", {"project_id": project_id})
    except Exception as e:  # noqa: BLE001
        result = {"ok": False, "error": f"执行失败：{type(e).__name__}: {e}"}
    return AgentStep(index=0, kind="tool", tool="confirm_step",
                     args={"project_id": project_id}, result=result)


def _confirmation_context(step: AgentStep) -> str:
    """把"我替你执行了确认"这件事告诉模型，让它只负责解说与继续。

    措辞刻意说清"**已经执行**"而不是"请你去执行" —— 否则模型会再调一次
    `confirm_step`（提案已被消费，只会报"没有待确认的提案"，又给用户添乱）。
    """
    import json as _json
    try:
        res = _json.dumps(step.result or {}, ensure_ascii=False)[:1200]
    except Exception:  # noqa: BLE001
        res = str(step.result)[:800]
    ok = bool((step.result or {}).get("ok"))
    return (
        "（系统提示，不是用户说的）用户刚才点头了，我**已经直接执行**了那一步"
        f"（工具 confirm_step，ok={ok}）。执行结果：\n{res}\n\n"
        "现在请你：\n"
        "1. 用一两句人话说清**刚刚发生了什么**（做成了什么 / 失败在哪一步、为什么）；\n"
        "2. 如果结果是 ok：按里面的 next_stage / next_hint 或流水线现状，"
        "把**下一步**用 propose_step 提给用户（一张卡片），并说明要花多少、多久；\n"
        "3. 如果 ok=false：**照实说**原因 + 该怎么办，不要假装成功；\n"
        "4. **不要**再调一次 confirm_step（提案已经用掉了）。\n"
        "5. 不要输出 JSON 给用户看，也不要说「我已经开始生成」——除非你刚刚真的调了工具。"
    )


#: 回复里"我改了/我做了"的说法（用来抓**谎报**）
_CLAIM_PATTERNS = (
    "已经改", "已改", "已按你的要求改", "已经更新", "已更新", "已经调整", "已调整",
    "已经设置", "已设置", "改好了", "已经完成", "已完成", "已经帮你改",
)
#: "这算真的动手了"的工具（写过库的那些）
_WRITE_TOOLS = ("revise_step", "propose_step", "confirm_step", "make_looks",
                "render_previz", "generate_preview", "generate_final",
                "plan_shots", "extract_assets", "generate_script",
                "merge_characters", "set_shots_model", "fix_missing_scenes",
                "relink_shots", "compose_film", "review_previz")


def _correct_false_claim(reply: str, steps: List[AgentStep], *,
                         applied: str = "") -> str:
    r"""说了"已经改了"但**什么都没做** → 如实更正（不许谎报）。

    ★ 2026-09-29（第 6 条的真往返测试抓到）：
      模型回复「已按你的要求改了「480P 小样」：duration=5」，
      而这一轮 `steps` 里**一个工具都没有** ⇒ 参数根本没改。
      用户看到的就是"它说改了、其实没改"（第 1 条那种"问完不办事"）。

    `applied` 非空表示**我替它真的做了**（确定性代做）：
    这时更正的话要改成"我上一条说已改不准确 —— 下面是**实际**做的"，
    不能再说"没有改动"（否则自相矛盾）。
    """
    text = str(reply or "")
    if not text or not any(p in text for p in _CLAIM_PATTERNS):
        return text
    did_write = any(s.kind == "tool" and str(s.tool) in _WRITE_TOOLS for s in steps)
    if did_write:
        return text
    logger.warning("模型声称'已经改了'但没有任何写操作 —— 追加更正")
    if applied:
        return (text.rstrip()
                + "\n\n⚠ **交代一句**：我上一条说的「已经改」其实**不是我自己做的**"
                  "（我没有调用修改动作）。下面是**实际**发生的事：")
    return (text.rstrip()
            + "\n\n⚠ **更正一下**：我上一条说「已经改」是不准确的 —— "
              "这一步**实际上没有改动**（我没有真的调用修改动作）。\n"
              "请再点一次「✏ 我要改一下」重说一遍，"
              "或者直接告诉我「把 XX 改成 YY」，我会真的改并重新给你确认。")


async def _maybe_apply_simple_revise(reply: str, messages: List[Dict[str, str]],
                                     project_id: str) -> str:
    r"""能**确定性代做**的修改就直接做掉（现在是"时长"这一类）。

    ★ 为什么只做一小类：把任意中文改法映射成参数是"猜"，猜错会改坏用户的提案。
      但**"时长改成 N 秒"** 这种是明确的、可解析的，而且是最常见的改法 ——
      替用户做掉它，比让模型"嘴上改"强得多。

    做完会在回复后面**附一句真实结果**（改成了什么、下次要不要确认）。
    """
    if not project_id:
        return ""
    import re as _re
    text = ""
    for m in reversed(messages or []):
        if m.get("role") == "user":
            text = str(m.get("content") or "")
            break
    if not text:
        return ""
    m = _re.search(r"(?:时长|duration|秒数)[^0-9]{0,6}(\d{1,3})\s*秒?", text)
    if not m:
        return ""
    try:
        from .stages import pending_gate
        pend = pending_gate(project_id)
    except Exception:  # noqa: BLE001
        return ""
    if not pend:
        return ""
    try:
        want = int(m.group(1))
    except Exception:  # noqa: BLE001
        return ""
    if want < 1 or want > 60:
        return ""
    try:
        res = await execute_tool("revise_step", {
            "project_id": project_id, "changes": {"duration": want}})
    except Exception as e:  # noqa: BLE001
        logger.warning("代做 revise_step 失败：%s", e)
        return ""
    if not (res or {}).get("ok"):
        return ""
    logger.info("替模型把「时长」改成了 %s 秒（它自己只说不做）", want)
    return ("\n\n（我直接替你把这个改动**真的写进去了**：时长 = %s 秒。"
            "提案还在，**没有开始做** —— 确认一次才会花这笔钱。）" % want)


#: 逼模型动手时回灌的那句话（要短、要硬、要给出"确实不用工具"的出口）
_NUDGE_TEXT = (
    "（系统提示，不是用户说的）你上一轮**只输出了自然语言，没有调用任何工具**。"
    "而这件事需要看真实数据才能回答 —— 你刚才说的「马上做 / 正在做 / 已经查了」"
    "都不成立。\n"
    "现在请二选一，**只做一件**：\n"
    "  ① 需要动手 → **只输出一个 JSON 工具调用**，不要有任何其他文字："
    '{"tool": "工具名", "args": {...}}\n'
    "  ② 确实不需要任何工具（纯聊天/解释）→ 用一句话说明为什么不需要，再回答。"
)

#: 用户这句话像不像"要你去干活"（要查/要做/要改/要生成）
_ACTION_HINTS = (
    "看", "查", "列", "显示", "告诉", "做", "生成", "出图", "出片", "开始", "执行",
    "改", "换", "加", "补", "删", "合并", "关联", "渲", "配音", "合成", "导出",
    "下一步", "继续", "确认", "为什么", "怎么", "多少", "有哪些", "缺什么",
)


def _needs_action(messages: List[Dict[str, str]], reply: str,
                  steps: List[AgentStep]) -> bool:
    r"""这一轮**该不该动手**？（决定要不要逼模型一次）

    判断刻意保守 —— 只在"明显该动手却只说话了"时返回 True，避免把
    正常聊天（"你好"）也逼一遍（那会让回答变得机械）。

    三个必要条件：
      ① 这一轮**还没有任何工具调用**（有就不用逼了）；
      ② 模型的回复里出现"我这就做/已经查了/马上"这类**承诺动作**的话，
         **或者**用户那句话本身就是"要你干活"的语气；
      ③ 用户那句话**不是**纯闲聊（长度 > 4 且命中动作词）。
    """
    if any(s.kind == "tool" for s in steps):
        return False
    last_user = ""
    for m in reversed(messages or []):
        if m.get("role") == "user":
            last_user = str(m.get("content") or "")
            break
    u = last_user.strip()
    if len(u) <= 4:
        return False
    r = str(reply or "")
    promised = any(w in r for w in ("马上", "这就", "正在", "已经查", "已经调",
                                    "我这就", "立刻", "现在开始", "先查", "我先看",
                                    "我先调", "让我看", "让我查"))
    asked = any(w in u for w in _ACTION_HINTS)
    talker = any(w in r for w in ("走神", "抱歉", "抱歉！", "对不起"))
    return bool((promised or asked) and (asked or talker))


async def run_agent(
    *,
    llm_chat: Callable[..., Any],
    messages: List[Dict[str, str]],
    max_steps: int = MAX_STEPS_DEFAULT,
    system_extra: str = "",
    on_step: Optional[Callable[[AgentStep], None]] = None,
    project_id: str = "",
) -> AgentRun:
    """跑一轮 agent。

    Args:
        llm_chat: 形如 `async def chat(messages, **kw) -> str` 的调用（通常是
                  `core.llm.LLMClient.chat` 的绑定方法）
        messages: 历史对话（不含 system，本函数会自己加）
        max_steps: 最多几轮工具调用
        system_extra: 追加到 system prompt 的内容（如当前项目上下文）
        on_step: 每完成一步回调（用于 SSE 推送）
    """
    # ★ 记住本轮的"当前项目"，给工具执行时的 project_id 兜底。
    #   每个请求跑在独立的 asyncio task 里，contextvar 天然隔离，不用手动 reset。
    CURRENT_PROJECT_ID.set(str(project_id or "").strip())
    # ★ 2026-10-20：同时记住"用户这一轮指着的项目"（模型改不动它）
    REQUEST_PROJECT_ID.set(str(project_id or "").strip())
    run = AgentRun()
    convo: List[Dict[str, str]] = [
        {"role": "system", "content": build_agent_prompt(system_extra, registry=REGISTRY)},
        *[m for m in messages if m.get("role") in ("user", "assistant")],
    ]

    # ★★ 先做**确定性确认**（用户点头 + 有待确认提案 → 直接执行那一步）。
    #   放在模型之前，是为了不让"要不要动手"取决于模型这一轮的脾气
    #   （实测它会在嘴上说"我已开始"却一个工具都不调）。
    _last_user = ""
    for _m in reversed(messages):
        if _m.get("role") == "user":
            _last_user = str(_m.get("content") or "")
            break
    _nudged = False   # ★ 每轮最多逼模型动手一次（见 _needs_action）
    _pre = await maybe_confirm_pending(project_id, _last_user)
    if _pre is not None:
        run.steps.append(_pre)
        run.tool_calls += 1
        if on_step:
            on_step(_pre)
        convo.append({"role": "user", "content": _confirmation_context(_pre)})

    for i in range(1, max_steps + 1):
        try:
            out = llm_chat(convo)
            if hasattr(out, "__await__"):
                out = await out
            text = out if isinstance(out, str) else str(out)
        except Exception as e:
            run.stopped_because = "error"
            # ★★ 2026-10-20：给用户看的错误要**能看懂、能动手**。
            #   原来直接把 httpx 的原文甩出去：
            #     模型调用失败：Server error '529 Unknown Status Code' for url
            #     'https://api.minimaxi.com/v1/text/chatcompletion_v2'
            #     For more information check: https://developer.mozilla.org/…
            #   用户看到的是一串英文报错 + 一个 MDN 链接，完全不知道该干嘛
            #   （实测 MiniMax 过载时就撞上了这一条）。
            #   现在按状态码翻成中文 + 给可执行的下一步。
            _raw = str(e)
            _code = 0
            for _c in (getattr(e, "response", None) and getattr(e.response, "status_code", None),
                       getattr(e, "status_code", None)):
                try:
                    _code = int(_c or 0)
                    if _code:
                        break
                except Exception:
                    _code = 0
            if _code == 529 or _code == 502 or _code == 503 or _code == 504:
                _friendly = ("文字模型服务现在很忙（%d），我已经自动重试过仍然没成功。"
                             "这通常是对方临时过载，**等一两分钟再发一次就好**，"
                             "你的项目和数据都不会丢。" % _code)
            elif _code == 429:
                _friendly = ("文字模型接口被限流了（429）—— 可能是调用太频繁或额度用尽。"
                             "等一会儿再试，或者到「设置 → 对话 API Keys」换一个模型。")
            elif _code == 401 or _code == 403:
                _friendly = ("文字模型的 Key 没通过（%d）。到「设置 → 对话 API Keys」"
                             "检查这个 provider 的 Key。" % _code)
            elif _code:
                _friendly = ("文字模型返回了 %d，调用没成功。原始信息：%s" % (_code, _raw[:200]))
            else:
                _friendly = ("文字模型调用失败：%s" % _raw[:200])
            step = AgentStep(index=i, kind="error", text=_friendly)
            run.steps.append(step)
            if on_step:
                on_step(step)
            run.reply = _friendly
            return run

        action = parse_action(text)

        # 不是动作 → 这就是最终答复
        if not action:
            run.reply = text.strip()
            # ★★★ 2026-09-29（用户第 2、3 条：「AI 还是纯嘴炮，不干活，不执行」）
            #
            #   用户截图里的真实对话（连着好几轮）：
            #     「我先把陈砚的提示词拼出来…同时调出图」
            #     「抱歉，我走神了。现在真的开始 —— 先查项目里有哪些资产。」
            #     「抱歉！马上发申请。」「好，这就发。」「我终于真的发出图申请了。」
            #   —— 全程**一个工具都没调**（界面上一行 ✓ 步骤都没有），
            #      而它每轮都说"马上做/正在做/真的发了"。
            #
            #   根因：这个 agent 用的是"文本里写 JSON"的协议，模型有时
            #   **只输出自然语言**（提示词越长、历史越长越容易）。旧代码遇到
            #   这种情况就直接当成"最终答复"返回 —— 用户就被晾在那儿。
            #
            #   修法：**第一次**只说话时别认账 —— 逼它一次：
            #     · 把它刚才那段话 + "你这一轮**没有调用任何工具**" 回灌；
            #     · 明确要求：要么只输出一个 JSON 工具调用，要么说明为什么不需要。
            #   每轮最多逼一次（`_nudged`），避免死循环；第二次还只说话就如实返回
            #   （并且下面的"不许谎报"会兜住）。
            if not _nudged and _needs_action(messages, run.reply, run.steps):
                _nudged = True
                logger.info("模型这一轮只说话、没调工具 —— 逼它一次（nudge）")
                convo.append({"role": "assistant", "content": run.reply})
                convo.append({"role": "user", "content": _NUDGE_TEXT})
                step = AgentStep(index=i, kind="text",
                                 text="（它刚才只说了话、没动手 —— 我要求它真的调用工具）")
                run.steps.append(step)
                if on_step:
                    on_step(step)
                continue

            # ★★ 2026-09-29（第 6 条的真往返测试抓到的**谎报**）
            #
            #   实测：用户按「✏ 我要改一下」提交了修改意见，模型回复
            #     「已按你的要求改了「480P 小样」：duration=5。还是没开始做。
            #       确认后我就执行。」
            #   —— 而**执行的工具是空的**（它一个工具都没调）。
            #   参数没改、提案也还是老样子，但它说"已经改了"。
            #   这正是用户第 1 条抱怨的「问完不办事」在"改"这条路复现。
            #
            #   修法两层（这里做第一层，也是最要紧的一层）：
            #     ① **不许谎报**：说了"已经改了/已改成/已更新"但这一轮
            #        **没有任何写操作** ⇒ 在回复后面**如实更正**。
            #     ② 能确定性代做的（如"时长改成 N 秒"）直接代做 ——
            #        见 `_maybe_apply_simple_revise`。
            # ⚠ 顺序要紧：**先试着真的代做**，再决定"更正"怎么说 ——
            #   代做成功时若还说"实际上没有改动"，就自相矛盾了（自测看到过）。
            _fix = await _maybe_apply_simple_revise(run.reply, messages, project_id)
            run.reply = _correct_false_claim(run.reply, run.steps, applied=_fix)
            if _fix:
                run.reply = run.reply + _fix
            step = AgentStep(index=i, kind="text", text=run.reply)
            run.steps.append(step)
            if on_step:
                on_step(step)
            run.stopped_because = "done"
            return run

        # 是动作 → 先过闸门，再执行
        tool, args = action["tool"], action["args"]
        blocked = _gate_check(tool, args)
        shown_tool = tool
        if blocked is not None:
            # ★ 没有待确认提案却直接调付费工具 —— 不再把"继续往下走"的责任
            #   推给模型：替它登记成待确认提案，把卡片交给用户。
            auto = await _auto_propose_for_blocked(tool, args, blocked)
            if auto is not None:
                result = auto
                shown_tool = "propose_step"
            else:
                result = blocked
        else:
            result = await execute_tool(tool, args)
        run.tool_calls += 1

        step = AgentStep(index=i, kind="tool", tool=shown_tool, args=args, result=result)
        run.steps.append(step)
        if on_step:
            on_step(step)

        # ★★ 提案之后**把这一轮结束掉**，把卡片交给用户（见 `await_user_text` 的说明）。
        #   不加这一步，模型可以在同一轮里 propose_step → confirm_step，
        #   自己把闸门打开 —— 用户连卡片都没看见钱就花了。
        #   注意 `on_step(step)` 已经在上面发过了，所以前端仍能拿到完整卡片数据。
        #   ★ 自动登记的那一次也走这里（`shown_tool` 已是 propose_step）。
        _ask = await_user_text(shown_tool, result)
        if _ask:
            run.reply = _ask
            run.stopped_because = "awaiting_user"
            return run

        # 回灌
        convo.append({"role": "assistant", "content": text})
        convo.append({"role": "user", "content": _result_to_message(shown_tool, result)})

    run.stopped_because = "max_steps"
    # ★ 步数用尽时必须**说清楚发生了什么、以及已经做成了什么**。
    #   原来只回一句"我没能全部完成"，用户既不知道卡在哪，也不知道
    #   前面几步到底有没有产生效果 —— 实测就是这么一轮：
    #   agent 把 6 步全花在重复调 plan_long_video 上，最后只回一句失败。
    did = [s for s in run.steps if s.kind == "tool"]
    failed = [s for s in did if not (s.result or {}).get("ok")]
    lines = [f"这一轮我用完了 {max_steps} 步，没能给出最终结论。"]
    # ★★★ 2026-09-29（用户第 2、3 条：「没有智能 / 体验极差」）
    #   实测：问「帮我看看 5 个镜头的白膜状态」，模型**逐个镜头**调
    #   `get_shot_progress`，4 步用光，最后回一句
    #   「这一轮我用完了 4 步，没能给出最终结论」+ 四个工具名 ——
    #   用户白问一场，**明明数据都已经查回来了**。
    #
    #   所以这里补一句：把**已经查到的结果**挑要点回给用户。
    #   宁可给半份有用的答案，也不要给一份"我失败了"的报告。
    seen_tools = {s.tool for s in did if (s.result or {}).get("ok")}
    if "list_shots" in seen_tools:
        lines.append("（提示：其实我这一轮已经用 `list_shots` 拿到每个镜头的"
                     "真实状态了，上面的表格/清单里就有答案。）")
    if did:
        lines.append(f"已经执行了 {len(did)} 个操作"
                     + (f"（其中 {len(failed)} 个失败）" if failed else "") + "：")
        for s in did:
            flag = "✓" if (s.result or {}).get("ok") else "✗"
            lines.append(f"  {flag} {s.tool}")
            err = (s.result or {}).get("error")
            if err:
                lines.append(f"      {str(err)[:120]}")
    lines.append("")
    lines.append("可以这样继续：把任务拆小一点再说一次"
                 "（比如\"只看第 1 个镜头的排片\"），或者告诉我先做哪一步。")
    run.reply = "\n".join(lines)
    return run


# ──────────────────────── 流式版本（给 SSE 用）────────────────────────


async def run_agent_stream(
    *,
    llm_chat: Callable[..., Any],
    messages: List[Dict[str, str]],
    max_steps: int = MAX_STEPS_DEFAULT,
    system_extra: str = "",
    project_id: str = "",
) -> AsyncIterator[Dict[str, Any]]:
    """边跑边吐事件，供前端实时展示「正在调哪个工具」。

    产出事件：
        {"type":"step",   "index":1, "tool":"plan_shots", "args":{...}}
        {"type":"result", "index":1, "ok":true,  "data":{...}}
        {"type":"reply",  "text":"最终答复"}
        {"type":"error",  "text":"..."}
    """
    # ★ 同上：记住当前项目，供工具执行时兜底 project_id
    CURRENT_PROJECT_ID.set(str(project_id or "").strip())
    # ★ 2026-10-20：同时记住"用户这一轮指着的项目"（模型改不动它）
    REQUEST_PROJECT_ID.set(str(project_id or "").strip())
    queue: List[Dict[str, Any]] = []

    def on_step(step: AgentStep) -> None:
        if step.kind == "tool":
            queue.append({"type": "step", "index": step.index,
                          "tool": step.tool, "args": step.args})
            queue.append({"type": "result", "index": step.index,
                          "ok": bool((step.result or {}).get("ok")),
                          "data": step.result})

    run = await run_agent(
        llm_chat=llm_chat, messages=messages,
        max_steps=max_steps, system_extra=system_extra, on_step=on_step,
        project_id=project_id,
    )

    for ev in queue:
        yield ev
    if run.stopped_because == "error":
        yield {"type": "error", "text": run.reply}
    else:
        yield {"type": "reply", "text": run.reply}
