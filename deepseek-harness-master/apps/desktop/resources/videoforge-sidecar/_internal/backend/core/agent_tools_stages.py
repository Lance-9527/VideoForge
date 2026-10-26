# -*- coding: utf-8 -*-
"""VideoForge · 流水线闸门工具（"一步步走完、每步等你点头"）

这个模块补上 agent 改造里最关键的一环：**把"要不要花钱、要不要往下走"的决定权
还给用户**。

改造前的问题（实测）：
    agent 一轮 12 步可以把 `generate_script` → `plan_shots` → `generate_preview`
    连着跑完。用户看到的是"我还没说开始，它就自己花钱了"。

改造后：
    · `pipeline_status`  —— 用**数据库真状态**回答"走到哪了、下一步是什么"；
    · `propose_step`     —— 只登记"我打算做这一步"，**不执行**，并返回一张
                            待确认卡片（做什么/花多少/大概多久/能改什么）；
    · `confirm_step`     —— 用户点头后才真正执行，可以带上修改后的参数；
    · `revise_step`      —— 用户说"改成 5 秒"时改参数，仍然不执行；
    · `skip_step`        —— 明确跳过并记下原因。

★ 闸门是**代码强制**的，不是提示词建议：`core/agent.py` 里有一道检查，
  `GATED_TOOLS` 里的工具在没有 pending 提案时直接调用会被拒。
  只靠 system prompt 说"要先确认"，实测会被模型无视。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

from .agent_tools import REGISTRY, ToolResult, _call, _need, _services, err, ok
from . import stage_rerun as R
from . import stages as S

logger = logging.getLogger("videoforge.agent.stages")


# ──────────────────────── 1. 进度 ────────────────────────


@REGISTRY.register(
    "pipeline_status",
    "★ 回答「做到哪一步了 / 下一步干什么」**只准用这个工具**。"
    "它返回整条制作流水线的 9 个阶段、每一阶段的真实完成状态（直接查数据库，"
    "不是模型记忆）、当前该做哪一步、以及这一步要花什么代价。"
    "用户问进度、说「继续」、或你不知道该干什么时，先调它。",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True}},
    needs_project=True,
)
async def t_pipeline_status(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "project_id")
    if bad:
        return bad
    pid = args["project_id"]
    try:
        db, _, _ = _services()
        proj = await _call(db.get_project, pid)
        if not proj:
            return err(f"项目不存在：{pid}")
        sm = S.summary(db, pid)
        return ok(
            headline=sm["headline"],
            done_count=sm["done_count"],
            total=sm["total"],
            next=sm["next"],
            pending_gate=sm["pending_gate"],
            stages=[{
                "key": s["key"], "title": s["title"], "tool": s["tool"],
                "status": s["status"], "cost": s["cost_label"],
                "evidence": s["evidence"].get("detail"),
                "missing_prereq": [S.STAGE_BY_KEY[k].title for k in s["missing_prereq"]],
            } for s in sm["stages"]],
            how_to_advance=(
                "下一步要用 propose_step 登记提案并等用户确认；"
                "**不要**直接调 " + (sm["next"] or {}).get("tool", "生成类工具") + "。"
                if sm.get("next") else "全流程已走完。"
            ),
        )
    except Exception as e:
        logger.exception("pipeline_status failed")
        return err(f"查流水线状态失败：{type(e).__name__}: {e}")


# ──────────────────────── 2. 提案 ────────────────────────


@REGISTRY.register(
    "propose_step",
    "★ 推进流水线**唯一**正确的入口。告诉系统「我打算做第 X 步」，"
    "系统**不会执行**，而是返回一张待用户确认的卡片（会做什么、花多少钱、"
    "大约多久、可以改哪些地方），并把这一步挂起。"
    "用户确认后你才能调 confirm_step 真正执行。\n"
    "凡是会花钱 / 耗时的步骤（生成剧本、定妆照、分镜、白膜、样片、成片、合成），"
    "都**必须**先走这一步 —— 直接调那些工具会被系统拒绝。",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True},
     "stage": {"type": "string", "desc": "阶段 key", "required": True,
               "enum": S.STAGE_ORDER},
     "args": {"type": "object", "desc": "这一步的参数（如 idea / names / transition）"},
     "note": {"type": "string", "desc": "你想对用户说的话，会显示在卡片上"}},
    needs_project=True,
)
async def t_propose_step(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "project_id", "stage")
    if bad:
        return bad
    pid, key = args["project_id"], str(args["stage"]).strip().lower()
    if key not in S.STAGE_BY_KEY:
        return err(f"没有这个阶段：{key}。可选：{'、'.join(S.STAGE_ORDER)}")
    try:
        db, _, _ = _services()
        if not await _call(db.get_project, pid):
            return err(f"项目不存在：{pid}")

        inner = args.get("args")
        inner = inner if isinstance(inner, dict) else {}
        # 允许扁平写法：{"stage":"script","idea":"..."}
        for k, v in args.items():
            if k not in ("project_id", "stage", "args", "note") and v not in (None, ""):
                inner.setdefault(k, v)
        inner.setdefault("project_id", pid)

        # ★ 占位符拦截：模型有时把"参数说明"当参数值抄进来，
        #   生成出来的东西会以那句说明为题材，用户完全看不懂。
        bad_arg = check_stage_args(key, inner, db=db, pid=pid)
        if bad_arg:
            return err(bad_arg)

        # ★★ 2026-09-27 第 45 轮：这一步**已经做完了** ⇒ 这是"重做"，要打标记。
        #    为什么：`/stage/confirm` 与 `run_stage` 默认**拒绝**重跑已完成的步骤
        #    （防止过期老提案把用户的内容覆盖掉）。打上 redo 之后，
        #    "用户说重做 → AI 重新提案 → 用户确认"这条路才走得通。
        if S.already_done(db, pid, key):
            inner["redo"] = True

        card = S.proposal_text(db, pid, key, inner)
        if not card.get("ok"):
            return err(card.get("error") or "生成提案失败")

        # ★★ 2026-09-28（用户第 1 条：「重复问用户定妆，多次问问完不办事」）
        #
        #   现象：对话里出现**两张一模一样的「角色/场景」卡片**，
        #   用户不知道该点哪张，只觉得"它又问了一遍"。
        #   真相：`set_pending` 只有一个槽位（后写覆盖前写），
        #   所以库里其实只有一份提案 —— 但**卡片是每条消息各画一份**，
        #   于是模型每提一次，聊天里就多一张卡。
        #
        #   这里给第二次及以后的提案打上 `duplicate` 标记，并明确告诉用户
        #   "上面那张卡直接点就行"，前端也会据此**不再画第二张卡**。
        dup = None
        try:
            prev = S.pending_gate(pid) or {}
            if str(prev.get("stage") or "") == key and prev.get("args") == inner:
                dup = {"stage": key, "since": prev.get("created_str") or "",
                       "already": True}
        except Exception:  # noqa: BLE001
            dup = None

        S.set_pending(pid, key, inner, note=str(args.get("note") or ""))
        card["note"] = str(args.get("note") or "")
        card["pending"] = True
        if dup:
            card["duplicate"] = dup
        if inner.get("redo"):
            # 让卡片**自己说清楚**：这是重做，会覆盖现有内容。
            card["redo"] = True
            card["warning"] = (
                ("⚠️ 这一步**已经做过了**，确认后会**重做并覆盖**现有内容。"
                 + ("　" + str(card["warning"]) if card.get("warning") else ""))
            )
        card["ask_user"] = (
            f"我已经准备好了「{card['title']}」，**还没有开始做**。"
            f"要花：{card['cost_label']}，预计 {card['eta']}。"
            f"\n{card['doing']}"
            + (f"\n⚠️ {card['warning']}" if card.get("warning") else "")
            + (f"\n\n★ 这一步的卡片**上面已经发过一次了**（{dup['since']}）——"
               f"**直接点那张卡的「✅ 确认，开始做」就行**，不用重新提，"
               f"也不用再打字说「确认」。" if dup else
               f"\n\n直接点卡片上的「✅ 确认，开始做」即可（不用打字）；"
               f"要改的话点「✏ 我要改一下」。")
        )
        return ok(**card)
    except Exception as e:
        logger.exception("propose_step failed")
        return err(f"登记提案失败：{type(e).__name__}: {e}")


# ──────────────────────── 3. 修改（仍不执行）────────────────────────


@REGISTRY.register(
    "revise_step",
    "用户对**待确认的提案**提出修改时用这个（例如「时长改成 5 秒」「只做主角」）。"
    "只更新参数，**仍然不执行** —— 改完还要用户再确认一次。"
    "不要为了改一个参数就去调真正的生成工具。",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True},
     "changes": {"type": "object", "desc": "要改的参数，键值对", "required": True},
     "stage": {"type": "string", "desc": "换成另一个阶段（可选）"}},
    needs_project=True,
)
async def t_revise_step(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "project_id", "changes")
    if bad:
        return bad
    pid = args["project_id"]
    try:
        db, _, _ = _services()
        pend = S.pending_gate(pid)
        if not pend:
            return err("现在没有待确认的提案 —— 先用 propose_step 提一个，再来改。")

        key = str(args.get("stage") or pend["stage"]).strip().lower()
        if key not in S.STAGE_BY_KEY:
            return err(f"没有这个阶段：{key}")

        changes = args.get("changes")
        changes = changes if isinstance(changes, dict) else {}
        merged = dict(pend.get("args") or {})
        merged.update(changes)
        merged.setdefault("project_id", pid)

        card = S.proposal_text(db, pid, key, merged)
        if not card.get("ok"):
            return err(card.get("error") or "更新提案失败")
        S.set_pending(pid, key, merged, note=pend.get("note") or "")
        card["note"] = pend.get("note") or ""
        card["pending"] = True
        card["changed"] = list(changes.keys())
        card["ask_user"] = (
            f"已按你的要求改了「{card['title']}」："
            f"{'、'.join(f'{k}={v}' for k, v in changes.items())}。"
            f"**还是没开始做**。确认后我就执行。"
        )
        return ok(**card)
    except Exception as e:
        logger.exception("revise_step failed")
        return err(f"修改提案失败：{type(e).__name__}: {e}")


# ──────────────────────── 4. 跳过 ────────────────────────


@REGISTRY.register(
    "skip_step",
    "用户明确说「这一步不用做 / 跳过」时用这个。会记下原因，"
    "之后 pipeline_status 会把这一步标成跳过而不是待做。",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True},
     "stage": {"type": "string", "desc": "要跳过的阶段 key", "required": True,
               "enum": S.STAGE_ORDER},
     "reason": {"type": "string", "desc": "用户给的理由"}},
    needs_project=True,
)
async def t_skip_step(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "project_id", "stage")
    if bad:
        return bad
    pid = args["project_id"]
    key = str(args["stage"]).strip().lower()
    if key not in S.STAGE_BY_KEY:
        return err(f"没有这个阶段：{key}")
    try:
        db, _, _ = _services()
        skips = await _skips_get(db, pid)
        skips[key] = {"reason": str(args.get("reason") or ""), "at": _now()}
        await _skips_set(db, pid, skips)
        pend = S.pending_gate(pid)
        if pend and pend.get("stage") == key:
            S.clear_pending(pid)
        return ok(skipped=key, title=S.STAGE_BY_KEY[key].title,
                  reason=str(args.get("reason") or ""),
                  note=f"已跳过「{S.STAGE_BY_KEY[key].title}」。")
    except Exception as e:
        return err(f"跳过失败：{type(e).__name__}: {e}")


# ──────────────────────── 5. 确认并执行 ────────────────────────


#: 模型偷懒时会把"参数说明"当成参数值填进来。实测抓到：
#:     propose_step(stage="script", args={"idea": "用户提供的想法或题材"})
#: —— 照着提示词里的占位说明抄了一遍。这样生成出来的剧本会以
#:    "用户提供的想法或题材" 为题材，用户看到的是莫名其妙的成品。
#: 这些词一旦出现在**关键参数**里，就要拦下来并要求补充真实内容。
_PLACEHOLDER_VALUES = {
    "用户提供的想法或题材", "用户的想法", "用户输入", "待补充", "未提供",
    "（待补充）", "(待补充)", "todo", "tbd", "n/a", "na", "placeholder",
    "用户的想法/题材/要求", "你的想法", "主题", "题材", "内容",
    # ★ 2026-09-27 第 45 轮补：用户真实库里 `scripts.user_prompt` 存的就是这个 ——
    #   `（剧本正文内容）`。那是界面上文本框的**提示语**，被当成内容写进了库。
    "（剧本正文内容）", "(剧本正文内容)", "剧本正文内容", "（正文）", "(正文)",
}

#: 括号包起来的**短标签**（"（剧本正文内容）"这种）：里面带这些词就当成占位。
#: 单独列是因为它既不是精确匹配、也不含常见标记词。
_LABEL_WORDS = ("正文", "内容", "剧本", "想法", "题材", "示例", "待定", "填写", "输入")

#: ★★ 2026-09-27 第 45 轮补：光有"精确匹配"列表是不够的 ——
#:    模型还会**把该问用户的那整句话填进参数**。
#:
#:    实物（**用户真实库里残留的待确认提案**，`pending_gates.json`，01:43）：
#:        stage=script
#:        args={"idea": "请提供您的想法或题材，例如：'抗战题材三分钟短片'", ...}
#:        note="请提供您的想法或题材，以便我为您生成剧本大纲。"
#:    —— 也就是说：它**知道**该问用户（note 就是那句问话），
#:       却把问句本身当成了 `idea` 的值，还登记成了"待确认提案"。
#:    后果有两层：① 卡片上"要做什么"写的是那句问话，用户看不懂；
#:               ② 用户真点「确认」，就会拿那句废话去生成剧本（白花钱）。
#:    而原来那份列表只做 `s in _PLACEHOLDER_VALUES` 的**精确匹配**，
#:    这一整句自然不相等 ⇒ **漏过去了**。下面是"句子级"的判据。
_PLACEHOLDER_PREFIXES = (
    "请提供", "请告诉", "请给", "请问", "请说明", "请描述", "请补充", "请先",
    "告诉我", "想问", "问一下", "用户提供", "用户输入", "用户的想法",
    "你的想法", "您的想法", "你想拍", "您想拍", "你想做", "您想做",
    "例如：", "例如:", "待补充", "未提供", "没有提供",
)

#: 出现在**句子中间**也算（配合长度门槛，避免误伤正常内容）
_PLACEHOLDER_MARKERS = (
    "请提供", "请告诉", "请说明", "请描述", "请补充",
    "用户的想法", "用户提供的", "用户输入", "你的想法", "您的想法",
    "你想拍什么", "您想拍什么", "待补充", "未提供",
    "例如：", "例如:", "比如：", "比如:",
)


def _looks_like_placeholder(v: Any) -> bool:
    s = str(v or "").strip()
    if not s:
        return True
    if s.lower() in _PLACEHOLDER_VALUES or s in _PLACEHOLDER_VALUES:
        return True
    # 尖括号/花括号包起来的说明，比如 "<你的想法>"
    if (s.startswith("<") and s.endswith(">")) or \
            (s.startswith("{") and s.endswith("}") and len(s) < 30 and "tool" not in s):
        return True
    # ── 句子级（第 45 轮新增）────────────────────────────────────
    # ① 整句以"在向用户要东西"开头 —— 这是一句**该问用户的话**，不是一个值
    if s.startswith(_PLACEHOLDER_PREFIXES):
        return True
    # ② 以问号结尾 —— 同理（"要什么题材？"）
    if s.endswith(("？", "?")):
        return True
    # ③ 句子里出现"向用户要东西 / 举例占位"的标记词（且够长，排除巧合）
    if len(s) >= 8 and any(m in s for m in _PLACEHOLDER_MARKERS):
        return True
    # ④ 句子里**包含**一个占位值（例如"用户提供的想法或题材"被包在一句话里）
    if len(s) >= 10 and any(p in s for p in _PLACEHOLDER_VALUES if len(p) >= 4):
        return True
    # ⑤ 括号包起来的**短标签**：`（剧本正文内容）` —— 界面的提示语被当成了内容。
    #    限定"整串就是一对括号 + 很短 + 含标签词"，免得误伤
    #    `（一个关于抢收玉米的故事）` 这种真在说事的写法。
    for _l, _r in (("（", "）"), ("(", ")")):
        if s.startswith(_l) and s.endswith(_r) and len(s) <= 14:
            inner = s[1:-1]
            if any(w in inner for w in _LABEL_WORDS):
                return True
    return False


def _stored_idea(db, pid: str) -> str:
    """项目里**已经存着的**那句想法（旧界面"剧本"面板里用户打过的题材）。

    ★ 为什么需要它（2026-09-27 第 45 轮）：`idea` 不是唯一来源 ——
      改造前的界面有一个「你的想法」输入框，那句话存在 `scripts.user_prompt` 里；
      用户从那儿生成剧本时，提案里**本来就没有** `idea` 参数。
      所以"参数为空"不能一律判错，得先看看项目里是不是已经有一句了。

    ★★ 只返回**看起来是真内容**的那句：用户库里 `scripts.user_prompt` 里
      既有真题材（"生成权力的游戏最终版"、"农夫与蛇.docx"），
      也有界面提示语（`（剧本正文内容）`）—— 后者绝不能当成题材去生成剧本。
      调用方拿到的要么是真内容、要么是空串。

    （刻意**不用** `projects.description`：那字段经常是标题的复述或占位
     如 "t"，拿它当题材反而会生成莫名其妙的东西。）
    """
    try:
        s = db.get_script(pid) or {}
        txt = str(s.get("user_prompt") or "").strip()
        if txt and not _looks_like_placeholder(txt):
            return txt
    except Exception:  # noqa: BLE001
        logger.warning("取剧本 user_prompt 失败（不算错，继续看下一处）", exc_info=True)
    return ""


def check_stage_args(stage: str, args: Dict[str, Any],
                     db=None, pid: str = "") -> Optional[str]:
    """关键参数不能是占位符。返回错误文案，None = 通过。

    ★ `db`/`pid` 是可选的：给了就能在"参数为空"时回退去查项目里已存的想法
      （见 `_stored_idea`）。`run_stage` 与两条提案路径都给。
    """
    if stage == "script":
        idea = (args or {}).get("idea")
        if _looks_like_placeholder(idea):
            # 空的 idea：如果项目里已经存着一句想法，那就不算错（旧界面那条路）
            if not str(idea or "").strip() and db is not None and pid \
                    and _stored_idea(db, pid):
                return None
            return (
                "生成剧本要先知道**拍什么**，但 idea 现在是空的或只是占位文字"
                f"（收到的是「{str(idea)[:40]}」）。"
                "请先问用户想拍什么，再带上他说的内容重新 propose_step。"
            )
    if stage == "looks":
        names = (args or {}).get("names")
        if names is not None and _looks_like_placeholder(names):
            return ("names 看起来是占位文字。要么不填（默认给所有缺图的资产出图），"
                    "要么填真实的名字。")
    return None


@REGISTRY.register(
    "confirm_step",
    "用户**明确同意**之后，用这个真正执行待确认的那一步。"
    "只能执行 propose_step 挂起的那一步；没有挂起提案就报错"
    "（这时应该先 propose_step）。overrides 可以带用户临时改的参数。",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True},
     "overrides": {"type": "object", "desc": "执行前临时覆盖的参数"},
     "stage": {"type": "string", "desc": "可选，校验是不是这一步"}},
    needs_project=True, writes=True,
)
async def t_confirm_step(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "project_id")
    if bad:
        return bad
    pid = args["project_id"]
    try:
        db, _, _ = _services()
        pend = S.pending_gate(pid)
        if not pend:
            return err(
                "现在没有待确认的提案，不能执行。"
                "正确做法：先调 pipeline_status 看下一步，再 propose_step 登记提案，"
                "等用户说「确认」之后才调 confirm_step。"
                "★ 如果用户只是要**改参重跑某一步**（那一步已经做过了），"
                "不用提案：直接调 rerun_stage(stage=..., params={...}, force=true)。",
                blocked_by="no_pending",
                next_action=("要推进新步骤 → 先 propose_step；要改参重跑已经做过的步骤 → "
                             "调 rerun_stage(stage=…, force=true)。"),
                retry={"tool": "rerun_stage",
                       "args": {"project_id": pid, "stage": "<要重跑的阶段>",
                                "force": True}},
            )
        key = str(pend.get("stage"))
        if args.get("stage") and str(args["stage"]).strip().lower() != key:
            return err(f"待确认的是「{S.STAGE_BY_KEY[key].title}」，不是 "
                       f"{args['stage']}。要换步骤就重新 propose_step。")

        merged = dict(pend.get("args") or {})
        ov = args.get("overrides")
        if isinstance(ov, dict):
            merged.update(ov)
        merged["project_id"] = pid

        # ★★ 2026-09-28：**先执行，成功了才作废提案**。
        #
        #   用户实测的「流程跑不通」就是这里造成的：
        #     ① 用户在卡片上点「确认」→ confirm_step；
        #     ② 这一步的执行因为**缺参数**失败（实测 `缺少必填参数：shot_id`）；
        #     ③ 但旧代码在**执行之前**就把提案 `consume_pending` 掉了
        #        ⇒ 再点一次变成「现在没有待确认的提案」
        #        ⇒ 用户被卡死，只能重新 let AI 提一遍（而他并不知道要做这个）。
        #   所以顺序反过来：失败就**保留**提案，让用户/模型能直接重试。
        #   （"同一个提案不会被确认两次"这个初衷仍然成立：成功路径上照样只执行一次。）
        res = await run_stage(db, pid, key, merged)
        if isinstance(res, dict) and res.get("ok"):
            S.consume_pending(pid)
        return res
    except Exception as e:
        logger.exception("confirm_step failed")
        return err(f"执行失败：{type(e).__name__}: {e}")


# ──────────────────────── 4.5 改参重跑（agent 侧唯一入口）────────────────────────
#
# ★★ 2026-10-25（T2）新增。为什么必须有它：
#   用户说「这一步重做 / 改一下」时，agent 现在只有两个选择：
#     ① 再调一次阶段工具 —— 撞 `run_stage` 的防覆盖守卫，而且它**没法**传 redo；
#     ② 重新 propose_step —— 这个工具在 DSH agent 的工具表里根本不存在。
#   结果就是同一句错误一轮一轮出现。这个工具把"重做语义"收进代码：
#   内部打 redo 标记、检查前置、花钱阶段要 confirm，agent 不用再猜任何内部标记。


@REGISTRY.register(
    "rerun_stage",
    "★ 用户对**已经做过的**某一步不满意、要求改参数重做/重跑时，**直接用这个**。"
    "不要自己猜 redo 标记，也不要为改一个参数重新 propose_step —— 这个工具内部会处理"
    "「重做标记 / 前置检查 / 花钱确认」。`params` 放改后的参数（如 idea / names / filename），"
    "`force=true` 表示明确要重做（原样重跑时必须传；参数与上次不同会自动放行）。"
    "花钱的阶段（looks / draft / final）**必须**带 confirm=true（= 用户已同意这笔花费）。",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True},
     "stage": {"type": "string", "desc": "要重跑的阶段 key", "required": True,
               "enum": S.STAGE_ORDER},
     "params": {"type": "object",
                "desc": "改后的参数（如 idea / names / filename）。不填=原样重做"},
     "force": {"type": "boolean",
               "desc": "true = 明确要重做（原样重做时必须传；参数变化时会自动放行）"},
     "confirm": {"type": "boolean",
                 "desc": "花钱阶段（looks/draft/final）必须传 true：用户已同意这笔花费"},
     "reason": {"type": "string", "desc": "用户为什么要求重做（记日志/结果用）"}},
    needs_project=True, writes=True,
)
async def t_rerun_stage(args: Dict[str, Any]) -> ToolResult:
    r"""「改参重跑」一条命令做完：前置检查 → 重做标记 → 执行 → 下一步提示。

    与 `run_stage` 的分工：`run_stage` 是**执行入口**（被确认/HTTP/v2 job 调用），
    这个工具是**agent 门面**：它保证 agent 永远不会因为"不知道要传 redo"而被挡。
    向后兼容：老调用方（propose_step + redo=true / confirm_step）行为不变。
    """
    bad = _need(args, "project_id", "stage")
    if bad:
        return bad
    pid = args["project_id"]
    key = str(args["stage"]).strip().lower()
    st = S.STAGE_BY_KEY.get(key)
    if not st:
        return err(
            f"没有这个阶段：{key}。可选：{'、'.join(S.STAGE_ORDER)}",
            blocked_by="bad_stage",
            next_action=f"stage 只能是：{'、'.join(S.STAGE_ORDER)}。")
    try:
        db, _, _ = _services()
        if not await _call(db.get_project, pid):
            return err(f"项目不存在：{pid}",
                       next_action="先调 list_projects 拿真实 project_id 再重跑。")

        inner = args.get("params")
        inner = dict(inner) if isinstance(inner, dict) else {}
        # 允许扁平写法：{"stage":"script","idea":"..."}（与 propose_step 一致）
        for k, v in args.items():
            if k in ("project_id", "stage", "params", "force", "confirm",
                     "reason", "note"):
                continue
            if v not in (None, ""):
                inner.setdefault(k, v)

        # ★ 花钱阶段：没有"用户已同意"就不动钱（与 vf-<stage> 的 confirm 语义一致）
        wants_money = st.cost in ("image", "video", "expensive")
        consent = R.truthy(args.get("confirm")) or R.truthy(inner.get("confirm"))
        if wants_money and not consent:
            return err(
                f"「{st.title}」是花钱的阶段（{st.cost_label}），重做前必须拿到"
                f"用户的明确同意。",
                blocked_by="needs_confirm", stage=key,
                next_action=(f"先用一句话告诉用户「重做「{st.title}」要花："
                             f"{st.cost_label}」，他同意后再带 confirm=true 调这个工具。"),
                retry={"tool": "rerun_stage",
                       "args": {"project_id": pid, "stage": key,
                                "force": True, "confirm": True}})
        if consent:
            inner["confirm"] = True

        # ★ 前置检查：缺前置就说清先做哪一步（不要等工具执行到一半才报）
        missing: List[str] = []
        for need in (st.needs or []):
            try:
                ev = S.EVIDENCE[need](db, pid)
            except Exception:  # noqa: BLE001
                ev = {}
            if not ev.get("done"):
                missing.append(need)
        if missing and not R.truthy(args.get("force")):
            titles = "、".join(S.STAGE_BY_KEY[k].title for k in missing
                              if k in S.STAGE_BY_KEY)
            first = missing[0] if missing[0] in S.STAGE_BY_KEY else ""
            return err(
                f"「{st.title}」的前置还没做完：{titles}。先做完前置再重跑这一步。",
                blocked_by="prerequisite", stage=key, missing_prereq=missing,
                next_action=(
                    f"先做「{titles}」"
                    + (f"（rerun_stage(project_id=\"{pid}\", stage=\"{first}\") "
                       f"或 vf_{first}）" if first else "")
                    + "；确实要强行跳过前置就传 force=true。"),
                retry=({"tool": "rerun_stage",
                        "args": {"project_id": pid, "stage": first}}
                       if first else None))

        # ★ 重做语义由**这里**处理，agent 不用猜：内部打 redo，force 一并透传
        inner["redo"] = True
        if R.truthy(args.get("force")):
            inner["force"] = True
        if args.get("reason"):
            inner["reason"] = str(args.get("reason"))[:200]

        res = await run_stage(db, pid, key, inner)
        if isinstance(res, dict):
            res.setdefault("rerun", True)
            res.setdefault("stage", key)
            if res.get("ok"):
                res.setdefault(
                    "next_action",
                    f"「{st.title}」已重跑完成。把结果给用户看；满意就按 "
                    f"pipeline_status 的下一步推进，不满意就再用 rerun_stage。")
        return res
    except Exception as e:
        logger.exception("rerun_stage failed")
        return err(
            f"重跑失败：{type(e).__name__}: {e}",
            next_action="把完整错误发给我；**不要**用同样的参数原样重试。")


# ──────────────────────── 执行分发（HTTP 路由也用它）────────────────────────


async def _run_clip_stage(db, pid: str, params: Dict[str, Any], tool: str) -> ToolResult:
    r"""「480P 小样 / 1080P 成片」这两个**阶段**的执行。

    ★★ 2026-10-20（用户实机，和 2026-09-28 白膜那次**一模一样的形状**）：
      卡片上的提案是**阶段语义**（"给这 2 个镜头出小样"），
      但旧代码把阶段直接派发给 `generate_preview` / `generate_final`，
      而那两个工具**必须**有 `shot_id`
      ⇒ 用户点「✅ 确认，开始做」永远得到「缺少必填参数：shot_id」，
        一步都走不动（实测截图：「「draft」没做成：缺少必填参数：shot_id」）。
      当时只给 `render_previz` 补了 `_run_previz_stage`，draft/final **一直没补** ——
      于是"一路点到成片"这条路，在**第一个要花钱的步骤**上就断了。

    语义：
      · 有 `shot_id` → 只出那一个（跟工具语义一致）；
      · 没有 → 给**每个**镜头都出。工具内部自带"已有该档片段就不再花钱"的守卫
        （`_existing_local_clip`），所以重复点确认**不会重复扣费**；
      · 一条都没成 ⇒ 返回失败（绝不报成功）。
    """
    label = "480P 小样" if tool == "generate_preview" else "1080P 成片"
    shots = await _call(db.list_shots, pid) or []
    if not shots:
        return err(f"这个项目还没有分镜 —— 先拆镜头，再出{label}。")
    if params.get("shot_id"):
        return await _dispatch(tool, params)

    try:
        limit = int(params.get("limit") or len(shots))
    except (TypeError, ValueError):
        limit = len(shots)
    batch = shots[:max(1, min(limit, 20))]

    made, failed = [], []
    for s in batch:
        try:
            r = await _dispatch(tool, {"project_id": pid, "shot_id": s.get("id")})
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if isinstance(r, dict) and r.get("ok"):
            made.append({"shot_id": s.get("id"),
                         "index": (s.get("order_index") or 0) + 1,
                         "path": r.get("path"),
                         "candidate_id": r.get("candidate_id"),
                         "resolution": r.get("resolution")})
        else:
            failed.append({"shot_id": s.get("id"),
                           "index": (s.get("order_index") or 0) + 1,
                           "error": str((r or {}).get("error"))[:200]})

    if not made and failed:
        return err(
            f"{label}**一条都没成**（{len(failed)} 个失败）："
            + "；".join(str(f.get("error"))[:90] for f in failed[:3])
            + "\n（提案**保留着**，修好原因后可以直接再确认一次。）",
            made=0, failed=len(failed), total=len(shots), errors=failed)

    return ok(stage=tool, made=len(made), failed=len(failed), total=len(shots),
              clips=made, errors=failed,
              note=(f"{label}出好了 {len(made)} 条"
                    + (f"，{len(failed)} 条失败（看 errors）" if failed else "")
                    + "。合成时直接用它们，重跑也不会重复花钱。"))


@REGISTRY.register(
    "advance_pipeline",
    "★★ **一口气把流水线往前推** —— 这才是 agent 该有的用法，别一步步让人点。"
    "从当前这一步起，把**免费**（以及可选地\"很少的钱\"那档）的步骤**连着做完**，"
    "遇到真正花钱的步骤（出图/出片）就停下来，把那一步的确认卡片直接交到用户手上。\n"
    "用户说「继续 / 往下走 / 自动做完 / 一口气做完 / 别问我了 / 都做掉」时**必须用它**。\n"
    "`max_stages`：最多连做几步（默认 3，上限 6）。"
    "`allow_cheap`：true 时允许连做\"很少的钱\"那档（一次文字模型调用，"
    "通常不到 1 分钱 —— 例如抽角色/场景、写分镜）。",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True},
     "max_stages": {"type": "string", "desc": "最多连做几步，默认 3，上限 6"},
     "allow_cheap": {"type": "string", "desc": "true = 允许连做\"很少的钱\"那档"}},
    needs_project=True,
)
async def t_advance_pipeline(args: Dict[str, Any]) -> ToolResult:
    r"""自主推进：免费/几乎不花钱的步骤**连着做完**，花钱的停下等人点头。

    ★★ 2026-10-20（用户："对话的权限能不能做的大一些，它是 agent"）。

    之前的问题：每个阶段都必须 `propose_step` → 用户点「确认」→ 再 proposal →
    再点 … 一个 9 步的片子要用户点 8~9 次，中间还夹着"下一步：X"的追问。
    用户的原话就是"它只是个聊天机器人"。**但钱不能乱花**，所以分档：

      · free（本地跑，零费用）      → 自己连做，不问；
      · cheap（一次文字模型调用）    → 用户说"自动/都做掉"时连做；
      · image / video / expensive   → **必须**停下来提案，等用户点确认。

    这样"从剧本到白膜"这段完全不需要人工，而"要花钱"那几步仍然是**一次点击**。
    """
    bad = _need(args, "project_id")
    if bad:
        return bad
    pid = args["project_id"]
    try:
        mx = int(str(args.get("max_stages") or "3").strip())
    except (TypeError, ValueError):
        mx = 3
    mx = max(1, min(mx, 6))
    allow_cheap = str(args.get("allow_cheap") or "").strip().lower() in (
        "1", "true", "yes", "on", "是", "对")
    auto_costs = {"free"} | ({"cheap"} if allow_cheap else set())

    db, _, _ = _services()
    ran: List[Dict[str, Any]] = []
    stopped: Optional[Dict[str, Any]] = None
    card: Any = None
    for _ in range(mx):
        try:
            nxt = str(S.next_stage_key(db, pid) or "")
        except Exception as e:  # noqa: BLE001
            return err(f"读流水线进度失败：{type(e).__name__}: {e}", steps=ran)
        if not nxt or nxt not in S.STAGE_BY_KEY:
            break
        st = S.STAGE_BY_KEY[nxt]
        if st.cost not in auto_costs:
            # 到花钱的步骤了 → 停下来，把确认卡片给他（用户只需点一下）
            try:
                params = {"project_id": pid}
                S.set_pending(pid, nxt, params, note="")
                card = S.proposal_text(db, pid, nxt, params)
            except Exception:  # noqa: BLE001
                card = None
            stopped = {"stage": nxt, "title": st.title,
                       "cost": st.cost, "cost_label": st.cost_label,
                       "why": st.why, "eta": st.eta}
            break
        res = await run_stage(db, pid, nxt, {"project_id": pid})
        ok_flag = bool(isinstance(res, dict) and res.get("ok"))
        ran.append({"stage": nxt, "title": st.title, "ok": ok_flag,
                    "note": str((res or {}).get("note") or "")[:300],
                    "error": "" if ok_flag else str((res or {}).get("error") or "")[:300]})
        if not ok_flag:
            return ok(auto=True, steps=ran, failed=ran[-1],
                      note=(f"连着做到「{st.title}」时没做成，停在它这里（没有继续往下花时间）。"
                            f"原因：{ran[-1].get('error') or ran[-1].get('note')}"))
    if not ran and not stopped:
        return ok(auto=True, steps=[], done=True,
                  note="流水线已经走完了（没有可自动推进的步骤了）。")
    _n = "；".join(f"{x['title']}{'✓' if x['ok'] else '✗'}" for x in ran) or "（没做新步骤）"
    if stopped:
        _note = (f"我自己往前做了 {len(ran)} 步：{_n}。\n"
                 f"下一步「{stopped['title']}」要花：{stopped['cost_label']}"
                 f"（{stopped['eta']}）—— 卡片已经放在下面，点一下「✅ 确认，开始做」就继续。")
    else:
        _note = f"我自己往前做了 {len(ran)} 步：{_n}。"
    return ok(auto=True, steps=ran, stopped_at=stopped, card=card, note=_note)


# ════════════════════════════════════════════════════════════════════════
# ★★ 2026-10-25（T2）：「改参重跑」的记忆与守卫
#
# 修的是什么（用户实机反复撞的那条循环）：
#   用户对某一阶段不满意、要求修改/重做时，agent 会一直报同一条错：
#       「X 这一步已经做完了 … 重新 propose_step（这次会带上重做标记）」
#   而 agent 手里**没有** propose_step（那是 sidecar 内部聊天 agent 的工具），
#   DSH 插件给它的阶段工具 `vf_<stage>` 只有 project_id / idea / confirm ——
#   **根本传不了 redo**。于是它只能改改参数再调一次，又被同一守卫挡住 ⇒
#   同类错误一轮一轮重复出现。
#
# 现在的语义（向后兼容）：
#   · 显式 redo=true（老路径、propose_step 自动打标）                 → 放行；
#   · **参数与上次成功那次不同** ⇒ 视为"改参重跑"，自动放行，结果带 auto_redo；
#   · 参数完全一样、又没显式标记 ⇒ 仍然挡下（防覆盖/防重复花钱），但错误里带
#     `next_action` + `retry`（一条**真能执行**的下一步），并按"同一句错误连续
#     出现几次"给 `repeat_count` / `do_not_retry`，避免多轮空转；
#   · **待确认提案路径**（propose→confirm）只认显式 redo —— 保留"8.8 小时前的
#     老提案覆盖用户剧本"那次事故的原始保护。
# 记忆存 settings（`stage_retry_memory`），不新增表结构。
# ════════════════════════════════════════════════════════════════════════

_STAGE_MEM_LOCK = threading.RLock()


async def _stage_mem_read(db) -> Dict[str, Any]:
    """读"阶段重做记忆"（上次成功参数 + 最近错误）。读失败当作空，不阻断执行。"""
    try:
        settings = await _call(db.get_all_settings) or {}
        return R.memory_doc(settings.get(R.MEMORY_SETTING_KEY))
    except Exception as e:  # noqa: BLE001
        logger.warning("读阶段重做记忆失败（按空处理，不影响本次执行）：%s", e)
        return R.memory_doc({})


async def _stage_mem_update(db, mutator) -> Dict[str, Any]:
    """读-改-写"阶段重做记忆"。mutator(doc) -> 新 doc。

    进程内用锁把读改写串起来（sidecar 是单进程；多个阶段 job 可能并发）。
    写失败只记日志：**不能**因为记忆写不进去就阻断真正的阶段执行。
    """
    with _STAGE_MEM_LOCK:
        doc = await _stage_mem_read(db)
        try:
            new = mutator(doc) or doc
        except Exception as e:  # noqa: BLE001
            logger.warning("计算阶段重做记忆失败（沿用旧值）：%s", e)
            return doc
        try:
            await _call(db.set_setting, R.MEMORY_SETTING_KEY, new)
        except Exception as e:  # noqa: BLE001
            logger.warning("写阶段重做记忆失败（不影响本次执行）：%s", e)
        return new


async def _decorate_stage_failure(db, pid: str, key: str,
                                  res: ToolResult) -> ToolResult:
    """给失败结果补 `next_action` / `repeat_count` / `do_not_retry`。

    ★ 为什么放在 `run_stage` 的出口统一做：错误形状有十几种（参数不合法、
      缺前置、工具报错……），每种都手写一遍 next_action 必然漂。这里用
      `R.failure_next_action()` 按错误文本给一句**具体**建议，再叠"同一句
      错误重复了几次"的计数 —— 模型就算不读长文案，也能看到 do_not_retry。
    """
    if not isinstance(res, dict) or res.get("ok") is not False:
        return res
    error = str(res.get("error") or "")
    nxt = str(res.get("next_action") or "") or R.failure_next_action(key, error)
    mem = await _stage_mem_read(db)
    entry, count = R.bump_error(R.errors_for(mem, pid).get(key), error, nxt)
    await _stage_mem_update(db, lambda d: R.with_error(d, pid, key, entry))
    if count >= 2:
        nxt = (f"⚠ 同一句错误已经重复出现 {count} 次 —— **不要原样重试**。\n" + nxt)
        res["do_not_retry"] = True
    res["repeat_count"] = count
    res["next_action"] = nxt
    return res


async def run_stage(db, pid: str, key: str, params: Dict[str, Any]) -> ToolResult:
    """真正执行某个阶段 —— 委派给已有的工具，不在这里重写业务逻辑。

    ★ 这里是唯一的执行入口：对话里点「确认」、HTTP 路由 `/stage/confirm`、
      以及 DSH 插件走的 `/api/v2/run` 都走它，保证三条路行为完全一致。

    ★★ 2026-10-25（T2）改造点（详见模块里那段注释）：
      · "已经做完的步骤"不再只认 `params.redo`：显式 redo/force/confirm 或
        **参数与上次成功那次不同** 都放行（`stage_rerun.decide`）；
      · 仍然挡下时，错误里带 `next_action` / `retry` / `repeat_count`，
        让 agent 有一条**真能执行**的下一步，而不是反复撞同一句；
      · 失败出口统一补 `next_action`（`_decorate_stage_failure`）。
    """
    st = S.STAGE_BY_KEY.get(key)
    if not st:
        return err(f"没有这个阶段：{key}")

    params = dict(params or {})
    params["project_id"] = pid

    # ══════════════════════════════════════════════════════════════
    # ★★ 2026-09-27 第 45 轮：执行前**再查一遍参数**（不是只在 propose 时查）
    #
    # 为什么必须在这里查第二遍：`propose_step` 那次检查只管"新登记的提案"，
    # 而提案是**持久**的 —— 用户真实库里就躺着一条 8.8 小时前的坏提案
    # （`idea` 是"请提供您的想法或题材，例如：'抗战题材短三分钟短片'"这句问话）。
    # 那条提案是**旧代码**登记的，新加的检查在 propose 时根本不会经过它；
    # 用户一觉醒来点「确认」，就会拿那句废话去生成剧本（白花钱）。
    # 放在 `run_stage` 里 ⇒ 对话路径与 HTTP 路径**都**覆盖，
    # 而且对"过去登记的坏提案"同样生效。
    # ══════════════════════════════════════════════════════════════
    bad_arg = check_stage_args(key, params, db=db, pid=pid)
    if bad_arg:
        S.clear_pending(pid)          # 作废这条坏提案，别让它一直卡着用户
        return await _decorate_stage_failure(db, pid, key, err(
            bad_arg + "（这条待确认提案已作废，请重新提一个。）",
            blocked_by="bad_args", stage=key))

    # ★★ 已经做完的步骤，默认仍然**不许**随便再跑一遍（覆盖 / 重复花钱）。
    #    但"要重做"不再要求调用方猜一个内部标记：见 `stage_rerun.decide`。
    done = bool(S.already_done(db, pid, key))
    if done:
        mem = await _stage_mem_read(db)
        run_entry = R.runs_for(mem, pid).get(key)
        # 这次是不是在执行"待确认提案"？是的话只认显式 redo（防过期提案覆盖）。
        pend = S.pending_gate(pid)
        proposal_path = bool(pend and str(pend.get("stage") or "") == key)
        dec = R.decide(key, params, True, run_entry,
                       allow_params_change=not proposal_path)
        if not dec["allow"]:
            nxt, retry = R.redo_next_action(key, st.title, pid, dec["reason"])
            entry, count = R.bump_error(
                R.errors_for(mem, pid).get(key),
                f"redo_guard:{dec['reason']}:{st.title}", nxt)
            await _stage_mem_update(db, lambda d: R.with_error(d, pid, key, entry))
            if count >= 2:
                nxt = (f"⚠ 同一句错误已经重复出现 {count} 次 —— "
                       f"**不要原样重试**。\n" + nxt)
            logger.info("run_stage 重做守卫挡下：pid=%s stage=%s reason=%s "
                        "repeat=%d path=%s", pid, key, dec["reason"], count,
                        "proposal" if proposal_path else "direct")
            blocked_kw: Dict[str, Any] = {
                "blocked_by": "redo_guard", "stage": key,
                "stage_title": st.title, "redo_reason": dec["reason"],
                "next_action": nxt, "retry": retry, "repeat_count": count,
                "auto_redo_hint": ("有改动就把改动放进参数再调一次（如 idea=…），"
                                   "参数与上次不同会自动放行"),
            }
            if count >= 2:
                # 只在真的重复出现时才给这个键：第一次就给会被误读成"已经试过很多次"
                blocked_kw["do_not_retry"] = True
            return err(
                f"「{st.title}」这一步**已经做完了**，再跑一遍会覆盖掉现有内容。"
                f"（这次被挡下的原因：{R.reason_text(dec['reason'])}）",
                **blocked_kw)
    else:
        dec = R.decide(key, params, False, None, allow_params_change=True)

    res = await _execute_stage(db, pid, key, params)

    # ── 出口：成功后记下"这次参数"（下次才能判断参数有没有变）─────────────
    ok_flag = bool(isinstance(res, dict) and res.get("ok"))
    if ok_flag:
        entry = R.make_run_entry(key, params, ok=True)
        await _stage_mem_update(db, lambda d: R.with_run(d, pid, key, entry))
        if isinstance(res, dict) and dec.get("auto_redo"):
            # 说清"为什么这次没被挡"：让模型/日志都能看出是改参重跑，不是漏了守卫
            res["auto_redo"] = True
            res["redo_reason"] = dec["reason"]
            res["redo_note"] = (
                "本次是**改参重跑**：参数与上次执行时不同，已自动放行"
                "（不需要 redo 标记）。")
    elif isinstance(res, dict) and dec.get("auto_redo"):
        # 放行了但没做成：把这次尝试也记下来 ⇒ 用**一模一样**的参数再试会被挡，
        # 逼调用方换参数/看 next_action，而不是多轮重复同一个失败。
        entry = R.make_run_entry(key, params, ok=False)
        await _stage_mem_update(db, lambda d: R.with_run(d, pid, key, entry))
        res = await _decorate_stage_failure(db, pid, key, res)
    else:
        res = await _decorate_stage_failure(db, pid, key, res)
    return res


async def _execute_stage(db, pid: str, key: str, params: Dict[str, Any]) -> ToolResult:
    """真正派发到绑定工具（`run_stage` 去掉守卫/记忆之后的执行体）。

    拆出来是为了让"守卫 + 记忆"只写一处：确认、HTTP、v2 job 三条路都走
    `run_stage`，不会再出现"某条路少了 redo 判断"的分叉。
    """
    st = S.STAGE_BY_KEY.get(key)
    if not st:
        return err(f"没有这个阶段：{key}")

    # 阶段 key 与真实工具的参数名不完全一样，这里做一次映射
    tool = st.tool
    if tool == "make_looks":
        return await _run_make_looks(db, pid, params)
    if tool == "render_previz":
        # ★★ 2026-09-28：**"白膜预演"是一个阶段，不是单个镜头**。
        #   用户点「确认」时提案里只有 stage，没有 shot_id
        #   （提案文案写的就是"给 4 个还没白膜的镜头渲白膜预演"）。
        #   旧代码直接把它派发给 `render_previz`，而那个工具**必须**有 shot_id
        #   ⇒ 点确认永远报「缺少必填参数：shot_id」，流程卡死在这里（实测）。
        #   所以：有 shot_id 就渲那一个；没有就**按阶段语义**把该渲的都渲了。
        return await _run_previz_stage(db, pid, params)
    if tool in ("make_character_look", "make_scene_look", "refine_look"):
        return await _dispatch(tool, params)

    # 其余阶段直接转给绑定工具
    if tool == "generate_script":
        # ★ 2026-09-27 第 45 轮：idea 还可以来自**项目里已经存着**的那句
        #   （旧界面「你的想法」输入框写进 `scripts.user_prompt` 的内容）。
        #   这条回退以前**只在参数守卫里**有、执行时没有 ⇒ 两条路不一致：
        #   守卫放行了，执行却报"需要先有想法"。现在两边都用 `_stored_idea`，
        #   而且它已经过滤掉占位文字（`（剧本正文内容）` 这种不会被当成题材）。
        params.setdefault("idea", params.get("idea") or params.get("prompt") or "")
        if not str(params.get("idea") or "").strip():
            params["idea"] = _stored_idea(db, pid)
        if not str(params.get("idea") or "").strip():
            return err("生成剧本需要先有「想法」。问用户想拍什么，再用 "
                       "propose_step 带上 args.idea 重新提一次。")
    if tool in ("generate_preview", "generate_final"):
        # ★ 2026-10-20：这两个是**阶段**（"给 N 个镜头出小样/成片"），
        #   而工具本身要 shot_id —— 直接派发必然报「缺少必填参数」。
        return await _run_clip_stage(db, pid, params, tool)
    if tool == "compose_film":
        # 逐镜转场存在分镜表里，合成时自动生效 —— 这里把用户的话翻译成参数
        for k in ("add_subtitles", "filename", "method", "transition",
                  "transition_seconds", "shot_ids"):
            if k in params:
                continue

    res = await _dispatch(tool, params)

    # ★★ 2026-10-20：「后期成片」的最后一道兜底。
    #   `compose_film` 需要**至少一个镜头的片段**；用户跳过 draft/final
    #   （没花视频钱）时一个片段都没有，它只会回
    #   「没有任何镜头有可用的视频」→ 九步链路卡死在最后一步。
    #   这时退回**本地渲染**（core.compose.compose_project：
    #   文字卡 / 参考图 + TTS 配音 + 字幕 + FFmpeg Ken Burns），
    #   零视频花费也能出一条成片 —— 不能因为"没花过钱"就交付不了。
    if key == "compose" and (not isinstance(res, dict) or not res.get("ok")):
        _e = str((res or {}).get("error") or "") if isinstance(res, dict) else str(res)
        if "没有任何镜头有可用的视频" in _e:
            try:
                from core.compose import compose_project as _cp_fb
                _settings = await _call(db.get_all_settings) or {}
                # ★ 2026-10-20：没有任何台词时，改走**旁白**而不是交静音片。
                #   实测：剧本阶段生成的分镜 `dialogue` 全是 null，
                #   于是 per-shot 配音一步都走不到，成片 volumedetect 是 -91dB（纯静音）。
                #   compose 本身就支持 `narration_fallback`（纪录片式旁白读画面概述），
                #   这里按"有没有台词"自动决定是否开。
                _has_dialogue = False
                try:
                    _raw_shots = await _call(db.list_shots, pid) or []
                    # `_call` 可能返回 {"shots": [...]} 也可能直接是 list —— 两种都接
                    if isinstance(_raw_shots, dict):
                        _raw_shots = (_raw_shots.get("shots")
                                      or _raw_shots.get("data") or [])
                    if not isinstance(_raw_shots, (list, tuple)):
                        _raw_shots = []
                    for _s in _raw_shots:
                        if not isinstance(_s, dict):
                            continue
                        _tl = _s.get("layer2_timeline") or []
                        if isinstance(_tl, str):
                            import json as _j2
                            try:
                                _tl = _j2.loads(_tl or "[]")
                            except Exception:
                                _tl = []
                        for _it in _tl:
                            if isinstance(_it, dict) and _it.get("dialogue"):
                                _has_dialogue = True
                                break
                        if _has_dialogue:
                            break
                except Exception as _de:
                    # 查不出来时**不要**乱开旁白（会念画面描述）；默认按"没台词"处理，
                    # 因为静音成片比"念错内容"更可接受，且警告里会说明。
                    logger.warning("检测台词失败（按无台词处理）：%s", _de)
                    _has_dialogue = False
                _fb = await _cp_fb(
                    pid, db=db, settings=_settings,
                    voice_id=str(params.get("voice_id")
                                 or "edge:zh-CN-XiaoxiaoNeural"),
                    with_voice=bool(params.get("with_voice", True)),
                    burn_subtitles=bool(params.get("add_subtitles", True)),
                    narration_fallback=not _has_dialogue,
                    output_name=str(params.get("filename") or "final.mp4"),
                )
                _p = str(getattr(_fb, "output_path", "") or "")
                import os as _os_fb
                if _p and _os_fb.path.exists(_p):
                    logger.info("compose 回退本地渲染成功：%s", _p)
                    return ok(
                        tool="compose_project",
                        fallback_from="compose_film",
                        output_path=_p,
                        # \u2605 2026-10-20 \u4fee\uff1a\u62ff total_duration\u62ff\u4e0d\u5230\uff08\u90a3\u4e2a\u5bf9\u8c61\u4e0a\u53eb duration\uff09
                        #   \u2192 \u6210\u7247\u5b9e\u9645 11 \u79d2\u5374\u62a5 "duration: 0"\uff0c\u754c\u9762\u4e0a\u770b\u7740\u50cf\u6ca1\u51fa\u7247\u3002
                        duration=(getattr(_fb, "total_duration", 0)
                                  or getattr(_fb, "duration", 0)
                                  or getattr(_fb, "seconds", 0) or 0),
                        note=("还没有任何镜头的片段，已用**本地渲染**出片"
                              "（画面用文字卡/参考图，配音与字幕照常）。"
                              "要真视频就回到「480P 小样」那一步出片后再合成。"),
                    )
                logger.warning("本地渲染回退未产出文件：%r", _p)
            except Exception as _fe:
                logger.warning("本地渲染回退失败：%s", _fe)
    # 补一句"下一步"，让模型知道该说什么
    if res.get("ok") and key not in ("compose",):
        try:
            nxt = S.summary(db, pid).get("next")
            if nxt:
                res["next_stage"] = {"key": nxt["key"], "title": nxt["title"],
                                     "cost": nxt["cost_label"]}
                res["next_hint"] = (f"这一步做完了。下一步是「{nxt['title']}」"
                                    f"（{nxt['cost_label']}）—— 用 propose_step 提给用户。")
        except Exception:
            pass
    return res


async def _dispatch(tool: str, params: Dict[str, Any]) -> ToolResult:
    """按名字调另一个已注册的工具。

    ★ 2026-09-27：改走 `core.agent.execute_tool` —— 那是**唯一的**工具派发口，
    它额外做了一件这里以前不做的事：`needs_project` 的工具会先校验
    project_id **真的存在**（否则返回「项目不存在」，而不是让工具拿假 id
    去查、查空后回一句"这一步已经完成了"）。
    直接 `spec.handler(params)` 会绕过那道校验。
    """
    spec = REGISTRY.get(tool)
    if not spec or not spec.handler:
        return err(f"阶段绑定的工具不存在：{tool}（这是配置问题，不是用户操作问题）")
    try:
        from core.agent import execute_tool  # 惰性导入：避免模块级循环
        res = await execute_tool(tool, params)
        return res if isinstance(res, dict) else ok(result=res)
    except Exception as e:
        logger.exception("stage tool %s failed", tool)
        return err(f"{tool} 执行失败：{type(e).__name__}: {e}")


# ──────────────────────── 6. 出海报（定妆照/场景图）────────────────────────
#
# ★ 为什么必须补这几个工具：工作区已经**去掉了生成按钮**，只剩对话。
#   而改造前"生成角色形象图"只有 HTTP 路由
#   （/api/projects/{pid}/characters/{cid}/generate-image），**没有对应工具**。
#   也就是说：用户在新工作区里根本没法给角色出定妆照 —— 这是个死路。
#   没有参考图 → 视频模型锁不住长相 → 每个镜头一张脸。


async def _gen_all_missing(db, pid: str, kind: str,
                            params: Dict[str, Any]) -> ToolResult:
    """把这一类里**还没有参考图**的资产全出了（一次最多 6 个，和 make_looks 一致）。

    ★ 为什么要有它（2026-09-27 第 59 轮，用户实测）：模型常常"想给角色出图"
    但**不给名字**，以前会回一句「要指定角色名字」—— 对用户来说就是**卡住** ✗。
    "没给名字"的意图很清楚：缺图的都出了。于是这里照做，并如实报告做了几个、
    还剩几个（剩下的说一声继续）。
    """
    items = (await _call(db.list_characters, pid) if kind == "character"
             else await _call(db.list_scenes, pid)) or []
    todo = [x for x in items if not (x.get("reference_image_path") or "").strip()]
    label = "角色" if kind == "character" else "场景"
    if not todo:
        return ok(nothing_to_do=True, made=0, total=len(items),
                  note="%d 个%s**都已经有参考图了**，没有要补的。" % (len(items), label))
    try:
        limit = int(params.get("limit") or 6)
    except (TypeError, ValueError):
        limit = 6
    batch = todo[:max(1, min(limit, 20))]
    made, failed = [], []
    for obj in batch:
        r = await _gen_asset_image(db, pid, kind, obj, params)
        if r.get("ok"):
            made.append({"name": obj.get("name"), "path": r.get("path")})
        else:
            failed.append({"name": obj.get("name"), "error": r.get("error")})
    left = len(todo) - len(batch)
    left_names = [x.get("name") for x in todo[len(batch):]]
    return ok(
        kind=kind, made=len(made), failed=len(failed), items=made,
        errors=failed or None, remaining=left, remaining_names=left_names or None,
        note=("给 %d 个%s出了参考图（本机出图，一张 2~4 分钟）" % (len(made), label)
              + ("，%d 个失败" % len(failed) if failed else "")
              + ("；还有 %d 个没做（%s），说一声「继续出」我就接着做"
                 % (left, "、".join(str(x) for x in left_names[:6])) if left
                 else "；这一类全齐了")),
    )


@REGISTRY.register(
    "make_character_look",
    "给**一个角色**生成定妆照（形象参考图），写回资产库。"
    "没有参考图的角色在视频里会「每个镜头一张脸」，所以这是必做的一步。"
    "会花钱（图像模型按张计费）。★ 走闸门：先 propose_step(stage=looks) 让用户确认。",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True},
     "name": {"type": "string", "desc": "角色名（用名字，不要用 id）"},
     "character_id": {"type": "string", "desc": "也可以直接给 id"},
     "prompt": {"type": "string", "desc": "想额外指定的画面要求（可选）"},
     "style": {"type": "string", "desc": "风格，如 写实/国风/赛博朋克"},
     "view": {"type": "string", "desc": "景别：bust=半身(默认) / full=全身 / face=特写"},
     "confirm_redraw": {"type": "boolean",
                        "desc": "该资产**已有参考图**时必须显式传 true 才允许整张重画"
                                "（重画会换脸；只改细节请用 refine_look）"}},
    needs_project=True, writes=True,
)
async def t_make_character_look(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "project_id")
    if bad:
        return bad
    pid = args["project_id"]
    try:
        db, _, _ = _services()
        # ★ 第 59 轮：没给名字 = 把还没有图的都出了（见 _gen_all_missing）
        if not (args.get("name") or args.get("character_id")):
            return await _gen_all_missing(db, pid, "character", args)
        char, bad = await _find_asset(db, pid, "character",
                                      args.get("character_id"), args.get("name"))
        if bad:
            return bad
        return await _gen_asset_image(db, pid, "character", char, args)
    except Exception as e:
        logger.exception("make_character_look failed")
        return err(f"生成定妆照失败：{type(e).__name__}: {e}")


@REGISTRY.register(
    "make_scene_look",
    "给**一个场景**生成概念图（参考图），写回资产库。"
    "会花钱（图像模型）。★ 走闸门：先 propose_step(stage=looks) 让用户确认。",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True},
     "name": {"type": "string", "desc": "场景名"},
     "scene_id": {"type": "string", "desc": "也可以直接给 id"},
     "prompt": {"type": "string", "desc": "额外画面要求（可选）"},
     "size": {"type": "string", "desc": "尺寸，不填按项目画幅自动定"},
     "confirm_redraw": {"type": "boolean",
                        "desc": "该场景**已有参考图**时必须显式传 true 才允许整张重画"
                                "（只改细节请用 refine_look）"}},
    needs_project=True, writes=True,
)
async def t_make_scene_look(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "project_id")
    if bad:
        return bad
    pid = args["project_id"]
    try:
        db, _, _ = _services()
        if not (args.get("name") or args.get("scene_id")):
            return await _gen_all_missing(db, pid, "scene", args)
        sc, bad = await _find_asset(db, pid, "scene", args.get("scene_id"), args.get("name"))
        if bad:
            return bad
        return await _gen_asset_image(db, pid, "scene", sc, args)
    except Exception as e:
        logger.exception("make_scene_look failed")
        return err(f"生成场景图失败：{type(e).__name__}: {e}")


@REGISTRY.register(
    "make_looks",
    "批量补齐定妆照/场景图：把所有**还没有参考图**的角色和场景一次性出图。"
    "这是流水线里的「looks」阶段。会花钱（按张计费）。\n"
    "★ 走闸门：先 propose_step(stage=looks) 让用户确认。"
    "想只做一部分，用 names 指定（如 names=\"小明,咖啡馆\"）。",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True},
     "names": {"type": "string", "desc": "只做这些名字，逗号分隔；不填=全部缺图的"},
     "style": {"type": "string", "desc": "统一风格"},
     "limit": {"type": "integer", "desc": "最多做几个，默认 6（防止一口气花太多）"}},
    needs_project=True, writes=True,
)
async def t_make_looks(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "project_id")
    if bad:
        return bad
    try:
        db, _, _ = _services()
        return await _run_make_looks(db, args["project_id"], args)
    except Exception as e:
        logger.exception("make_looks failed")
        return err(f"批量出图失败：{type(e).__name__}: {e}")


async def _run_previz_stage(db, pid: str, params: Dict[str, Any]) -> ToolResult:
    r"""「白膜预演」这个**阶段**的执行：把该渲白膜的镜头都渲了（本地免费）。

    ★★ 2026-09-28（用户实测的「流程跑不通」就死在这里）

      提案的文案写的是"给 4 个还没白膜的镜头渲白膜预演"，也就是**阶段语义**；
      但旧代码把阶段直接派发给 `render_previz`，而那个工具**必须**有 `shot_id`
      ⇒ 用户在卡片上点「确认」永远得到「缺少必填参数：shot_id」，一步都走不动。

    现在的语义：
      · `params` 里有 `shot_id` → 只渲那一个（跟工具的语义一致）；
      · 没有 → 渲**所有"还没有白膜"的镜头**（`not_started` / 被驳回的），
        本地渲染、完全免费，所以按阶段语义全渲是对的；
      · 全部渲过 → 如实说"已经渲完了"，并提示下一步（让用户去确认）。
    """
    from core import previz as _pv
    shots = await _call(db.list_shots, pid) or []
    if not shots:
        return err("这个项目还没有分镜 —— 先拆镜头，再渲白膜。")

    # 指定了单个镜头 → 走原工具
    if params.get("shot_id"):
        return await _dispatch("render_previz", params)

    # ★★ 2026-10-26（T3 收尾）：**重做必须真渲**。
    #
    #   用户原话（实机，见会话「重做白膜预演」）：
    #     「重做」时阶段**根本没有提交任何渲染任务**，却用**旧的 previz_path**
    #     判定成「2 个镜头已就绪」，然后回包 ok:true —— 这就是"报成功其实没做"。
    #   根因：这段只按 `previz.get_state()` 跳过"已渲过"的镜头，**不看 redo**；
    #   而 rerun_stage / vf_rerun_stage 传下来的正是 redo=true。
    #   现在：redo/force ⇒ 不跳过任何镜头（该渲的都重渲），渲不出就照常报失败。
    redo = R.truthy(params.get("redo")) or R.truthy(params.get("force"))
    todo = []
    for s in shots:
        try:
            st = _pv.get_state(s)["status"]
        except Exception:  # noqa: BLE001
            st = _pv.S_NOT_STARTED
        if not redo and st in (_pv.S_DRAFT_READY, _pv.S_DRAFT_APPROVED, _pv.S_FINAL_READY):
            continue                      # 已经渲过 / 已确认 → 不重复渲（除非用户要求重做）
        todo.append(s)
    if not todo:
        return ok(nothing_to_do=True, rendered=0,
                  total=len(shots), already=len(shots),
                  note=("所有镜头的白膜都已经渲好了 —— **不需要再渲**，"
                        "现在该让用户逐个看一眼并确认（review_previz approve）。"),
                  next_step="让用户确认白膜：review_previz(shot_id=…, decision=\"approve\")")

    try:
        limit = int(params.get("limit") or len(todo))
    except (TypeError, ValueError):
        limit = len(todo)
    limit = max(1, min(limit, 20))
    batch = todo[:limit]

    # ★ 2026-10-22：**白膜并发**（原来串行，每镜一次 Blender 进程 ≈ 54 秒）。
    #   并发度 2：Blender 是 CPU/内存大户，2 路在普通机器上安全，且把 4 镜从 ~3.6 分钟压到 ~1.8 分钟。
    import asyncio as _aio2

    made, failed = [], []
    _pdone = {"n": 0}
    _plock = _aio2.Lock()
    _psem = _aio2.Semaphore(2)

    async def _render_one(s: Dict[str, Any]) -> None:
        try:
            r = await _dispatch("render_previz", {"project_id": pid,
                                                  "shot_id": s.get("id")})
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        async with _plock:
            _pdone["n"] += 1
            try:
                _prog = params.get("_progress")
                if callable(_prog):
                    _prog(_pdone["n"] / max(1, len(batch)),
                           f"白膜 {_pdone['n']}/{len(batch)}")
            except Exception:  # noqa: BLE001
                pass
        return r

    async def _guarded_previz(s: Dict[str, Any]) -> Dict[str, Any]:
        async with _psem:
            return await _render_one(s)

    _results = await _aio2.gather(*[_guarded_previz(s) for s in batch])
    # ★ 2026-10-26：**按下标配对**（以前用循环外残留的变量 `s`，所有结果都记到
    #   最后一个镜头上 —— 4 镜重渲会显示成"4 次 shot_id 都是最后一镜"）。
    for _s, _r in zip(batch, _results):
        r = _r if isinstance(_r, dict) else {"ok": False, "error": "白膜渲染无返回"}
        if r.get("ok"):
            made.append({"shot_id": _s.get("id"),
                         "index": (_s.get("order_index") or 0) + 1,
                         "path": r.get("path"),
                         "engine": r.get("engine"),
                         "moving_subjects": r.get("moving_subjects") or [],
                         "camera": (r.get("motion") or {}).get("camera"),
                         "names": ((r.get("motion") or {}).get("script") or {}).get("names")})
        else:
            failed.append({"shot_id": _s.get("id"),
                           "index": (_s.get("order_index") or 0) + 1,
                           "error": str(r.get("error"))[:200]})
    # ★★ 2026-09-28：**一个都没渲成 ⇒ 这一步就是没成功**。
    #   原来无论成败都返回 ok=True（只在 errors 里记一笔），后果是：
    #     · `confirm_step` 看到 ok=True 就把待确认提案消费掉 →
    #       用户想重试时变成「没有待确认的提案」（卡死，实测踩过）；
    #     · 界面显示"做完了"，而实际一个白膜都没有 —— 正是用户最烦的
    #       「报成功但其实没做」。
    if not made and failed:
        return err(
            f"白膜预演**一个都没渲成**（{len(failed)} 个失败）："
            + "；".join(str(f.get("error"))[:80] for f in failed[:3])
            + "\n（提案**保留着**，修好原因后可以直接再确认一次。）",
            rendered=0, failed=len(failed), total=len(shots),
            errors=failed, still_need=len(todo))
    return ok(rendered=len(made), failed=len(failed), total=len(shots),
              still_need=len(todo) - len(made),
              clips=made, errors=failed, redo=redo,
              note=(("本次是**强制重做**：已重新真渲，不复用旧白膜。" if redo else "")
                    + f"渲好了 {len(made)} 个镜头的白膜（本地免费，没花钱）。"
                    + (f" 有 {len(failed)} 个失败，看 errors。" if failed else "")
                    + " 现在让用户看一眼：**看角色名/运镜/走位对不对**，"
                      "满意就 review_previz approve。"),
              next_step="把白膜结果给用户看，等他确认（review_previz）。",
              cost="0（本地渲染）")


async def _run_make_looks(db, pid: str, params: Dict[str, Any]) -> ToolResult:
    """批量出图的实现（run_stage 与 make_looks 工具共用）。"""
    chars = await _call(db.list_characters, pid) or []
    scenes = await _call(db.list_scenes, pid) or []

    want: Optional[set] = None
    raw = params.get("names")
    if raw:
        if isinstance(raw, str):
            want = {x.strip() for x in raw.replace("，", ",").split(",") if x.strip()}
        elif isinstance(raw, list):
            want = {str(x).strip() for x in raw if str(x).strip()}

    todo: List[tuple] = []
    for c in chars:
        if (c.get("reference_image_path") or "").strip():
            continue
        if want and (c.get("name") or "") not in want:
            continue
        todo.append(("character", c))
    for s in scenes:
        if (s.get("reference_image_path") or "").strip():
            continue
        if want and (s.get("name") or "") not in want:
            continue
        todo.append(("scene", s))

    if not todo:
        return ok(nothing_to_do=True, made=0,
                  note="没有缺参考图的资产 —— 这一步已经完成了。")

    try:
        limit = int(params.get("limit") or 6)
    except (TypeError, ValueError):
        limit = 6
    limit = max(1, min(limit, 20))
    batch = todo[:limit]

    # ★ 2026-10-22：**并发出图**（原来串行，6 张要 6×单张耗时，用户反馈"卡了 10 分钟"）。
    #   并发度 3：Qwen-Image 侧不易被限流，又能把批次耗时压到 ~1/3。
    #   逐张完成即回报进度（progress 回调），让用户看到"第 2/6 张完成"而不是干等。
    import asyncio as _aio

    made, failed = [], []
    _done = {"n": 0}
    _total = len(batch)
    _lock = _aio.Lock()

    async def _one(kind: str, obj: Dict[str, Any]) -> None:
        r = await _gen_asset_image(db, pid, kind, obj, params)
        async with _lock:
            _done["n"] += 1
            if r.get("ok"):
                made.append({"kind": kind, "name": obj.get("name"),
                             "path": r.get("path")})
            else:
                failed.append({"kind": kind, "name": obj.get("name"),
                               "error": r.get("error")})
            _n = _done["n"]
        # 进度回报：一张完成就报一次
        try:
            _prog = params.get("_progress")
            if callable(_prog):
                _prog(_n / max(1, _total),
                      f"出图 {_n}/{_total}"
                      + (f"（{obj.get('name')}）" if obj.get("name") else ""))
        except Exception:  # noqa: BLE001
            pass

    _sem = _aio.Semaphore(3)

    async def _guarded(kind: str, obj: Dict[str, Any]) -> None:
        async with _sem:
            await _one(kind, obj)

    await _aio.gather(*[_guarded(k, o) for k, o in batch])

    left = len(todo) - len(batch)
    return ok(
        made=len(made), failed=len(failed), items=made, errors=failed,
        remaining=left,
        note=(f"出好了 {len(made)} 张"
              + (f"，{len(failed)} 张失败" if failed else "")
              + (f"；还有 {left} 个没做（一次最多 {limit} 个，"
                 f"说一声我继续）" if left else "；所有缺图的资产都补齐了")),
    )


async def _find_asset(db, pid: str, kind: str,
                      aid: Optional[str], name: Optional[str]
                      ) -> Tuple[Optional[Dict[str, Any]], Optional[ToolResult]]:
    """按 id 或名字找一个角色/场景。

    ★★ 返回 **(资产, 错误)** 二元组 —— 2026-09-27 修一个真 bug：

      原来写的是「找不到就返回一个 err 结果」，调用方用
      `isinstance(x, ToolResult)` 来区分。这**两处都错**：

        ① `ToolResult = Dict[str, Any]` 是个**下标泛型**，Python 3.11+ 的
           isinstance 直接抛
           `TypeError: Subscripted generics cannot be used with class and instance checks`
           （3.9 上只是 DeprecationWarning —— 所以这是**升级 Python 才爆**的雷，
           在本机的 3.14 上是硬错误）。
        ② 就算 isinstance 不炸，它也**区分不出来**：资产记录本来就是 dict，
           工具结果也是 dict。

      后果（实测）：`make_character_look` / `make_scene_look` / `refine_look`
      **三个工具全都死在第一行**，外层 `except Exception` 把它吞成
      「生成定妆照失败：TypeError: Subscripted generics…」——
      也就是「出定妆照 / 场景图 / 改图」整条线是死的，用户完全做不下去。

      二元组没有歧义：第一个是资产（可能是 None），第二个是错误（可能是 None）。
    """
    if aid:
        obj = (await _call(db.get_character, aid) if kind == "character"
               else await _call(db.get_scene, aid))
        if not obj:
            return None, err(f"找不到这个{'角色' if kind == 'character' else '场景'}：{aid}")
        if str(obj.get("project_id")) != str(pid):
            return None, err("这个资产不属于当前项目")
        return obj, None

    if not name:
        return None, err(f"要指定{'角色' if kind == 'character' else '场景'}名字"
                         f"（或 id），否则我不知道给谁出图。")

    items = (await _call(db.list_characters, pid) if kind == "character"
             else await _call(db.list_scenes, pid)) or []
    hit = [x for x in items if (x.get("name") or "").strip() == str(name).strip()]
    if not hit:
        near = [x.get("name") for x in items][:12]
        return None, err(f"项目里没有叫「{name}」的"
                         f"{'角色' if kind == 'character' else '场景'}。"
                         f"现有：{'、'.join(str(x) for x in near) or '（一个都没有）'}")
    return hit[0], None


async def _gen_asset_image(db, pid: str, kind: str, obj: Dict[str, Any],
                           params: Dict[str, Any]) -> ToolResult:
    """调图像模型出一张图并写回资产。复用 main.py 里同一套凭据解析与提示词拼装。

    ★ 2026-10-20 新增「重画保护」：资产**已经有参考图**时，默认**拒绝**重画，
      因为重画是纯文生图（不带参考图），画出来就是**换一张脸** ——
      之后所有镜头的主角长相都变了，而用户往往只是想"改一下衣服/背景"。

      正确做法：要改细节用 `refine_look`（带原图 + 指令，保住脸）；
      确实要整张重画，显式传 `confirm_redraw=true`。
      旧图不会丢：覆盖前先进 `reference_features.previous_reference_images`。
    """
    import os as _os

    _existing = str(obj.get("reference_image_path") or "").strip()
    if _existing and not bool(params.get("confirm_redraw")):
        _nm = obj.get("name") or ""
        return err(
            f"「{_nm}」**已经有参考图**了（{_os.path.basename(_existing)}），"
            f"重画会换一张脸 —— 之后所有镜头的主角长相都会变。\n"
            f"· 只想改衣服/背景/表情 → 用 `refine_look`（带原图改，脸不变）\n"
            f"· 确实要整张重画 → 再调一次并传 `confirm_redraw=true`"
            f"（旧图会保留在历史里）",
            blocked="needs_confirm_redraw", existing_image=_existing,
        )

    from core.imagegen import generate_image  # type: ignore
    from core.agent_tools import _services as _svc  # 自引用只为一致性

    settings = await _call(db.get_all_settings) or {}

    # ── 找 Key：与 main.py 的 _resolve_image_credentials 同一套优先级 ──
    provider = str(params.get("provider") or settings.get("image_provider")
                   or "local").strip()
    api_key = ""
    for bucket in ("llm_api_keys", "api_keys", "tts_api_keys"):
        d = settings.get(bucket) or {}
        if isinstance(d, dict):
            for k, v in d.items():
                if str(k).strip().lower() == provider.lower() and v:
                    api_key = str(v)
                    break
        if api_key:
            break
    if not api_key:
        api_key = str(settings.get(f"image_api_key_{provider.lower()}") or "").strip()

    cache_root = settings.get("cache_dir") or _os.path.join(
        _os.environ.get("VIDEOFORGE_DATA_DIR", _os.getcwd()), "cache")

    if kind == "character":
        out_dir = _os.path.join(cache_root, "images", pid, "characters")
        prompt = (params.get("prompt") or "").strip()
        if not prompt:
            try:
                from core.assets import build_portrait_prompt  # type: ignore
                merged = dict(obj)
                # ★ 带上剧本上下文（casting skill 用它选基调 / 写戏份依据 / 查年代契合）。
                #   取不到就传 None，退化成"只有人物卡"，不让出图失败。
                _ctx = None
                try:
                    from core.casting import context_from_db  # type: ignore
                    _ctx = context_from_db(db, pid)
                except Exception as _e:
                    logger.warning("取剧本上下文失败（出图不带剧本依据）：%s", _e)
                prompt = build_portrait_prompt(
                    merged,
                    style=params.get("style") or settings.get("default_style") or "cinematic",
                    view=params.get("view") or "bust",
                    ctx=_ctx)
            except Exception as e:
                logger.warning("拼角色提示词失败，回落模板：%s", e)
                prompt = (f"角色设定图：{obj.get('name', '')}。{obj.get('description') or ''} "
                          f"服装：{obj.get('costume_main') or '符合角色身份'}。"
                          f"正面半身像，单人，简洁背景，电影级光影，高清写实")
        size = params.get("size") or "1024x1024"
    else:
        out_dir = _os.path.join(cache_root, "images", pid, "scenes")
        prompt = (params.get("prompt") or "").strip() or (
            f"场景概念图：{obj.get('name', '')}。{obj.get('description') or ''} "
            f"类型：{obj.get('location_type') or 'outdoor'}，"
            f"光线：{obj.get('lighting') or 'natural'}，"
            f"时间：{obj.get('time_of_day') or 'day'}，"
            f"天气：{obj.get('weather') or 'clear'}。无人物，广角电影感，高清写实")
        # 场景图跟随项目画幅（方形场景图会被拼进 16:9 成片裁掉一大块）
        ar = "16:9"
        try:
            shots = await _call(db.list_shots, pid) or []
            ar = next((s.get("aspect_ratio") for s in shots if s.get("aspect_ratio")),
                      "") or "16:9"
        except Exception:
            pass
        size = params.get("size") or {"16:9": "1280x720", "9:16": "720x1280",
                                      "1:1": "1024x1024"}.get(str(ar), "1024x1024")

    try:
        result = await generate_image(
            provider=provider, api_key=api_key, prompt=prompt, out_dir=out_dir,
            size=size,
            # ★ 2026-10-20：尊重用户在「设置」里选的图像模型。
            #   原来只看 params.get("model")（agent 传的），设置里的 image_model
            #   形同虚设 —— 用户明明选了模型，出图时还是用 provider 的默认值。
            model=params.get("model") or settings.get("image_model") or "",
            base_url=params.get("base_url") or "",
        )
    except Exception as e:
        return err(f"图像生成失败：{type(e).__name__}: {e}")

    if not result.get("ok"):
        # ★ 2026-10-20：把 hint 也带上（"在 E:\出图 里跑 start-qwen.bat" 这类
        #   **能照着做的提示**原来只挂在结果上，这里没取，用户只看到干巴巴一句报错）
        _h = str(result.get("hint") or "").strip()
        return err((result.get("error") or "图像生成失败")
                   + (("\n" + _h) if _h else ""),
                   provider=provider)

    # ══════════════════════════════════════════════════════════════════
    # ★★ 同一个污染，agent 这条路也要修（2026-09-26 第 13 轮）
    #
    # 原来这里写的是：
    #     reference_features=result.get("prompt") or prompt
    #     color_palette=result.get("prompt") or prompt
    #
    # 注意那个 **`or prompt`** —— 本地的 `prompt` 变量**必然非空**
    # （上面刚拼好，最差也有回落模板），所以这里**每次出图都写**，
    # 比 main.py 那两个接口还稳。
    #
    # 而 `reference_features` 是**结构化角色卡的 JSON**
    # （`{"slots": {...}, "personality":…, "relationships":…}`），
    # `color_palette` 是 **JSON 数组**。写一段提示词文本进去 →
    # casting 的 `json.loads` 失败 → **固定槽位/身份锚点/性格/关系全部归零**：
    # 提示词的第 3、5、8、9 步整段消失，`audit()` 从此永远报「五官字段 0/3」，
    # `batch_report()` 的兄弟姐妹脸检查失效。
    #
    # 也就是说：**不管走界面按钮还是走 agent 工具，点一次出图就把卡毁了。**
    # 我上一轮只看了 main.py，误以为 agent 这条路干净 —— 看的是 `t_refine_look`
    # 的写回（那个确实只写 `reference_image_path`），不是这里。
    # 教训同 docs 第 40 节：**"查过一处"不等于"查过所有消费点"**。
    #
    # 提示词不会因此丢：它在本工具的返回里（见下面 `prompt=` 字段）。
    # 要长期存就得先给表加列，不是借用 JSON 列。
    # ══════════════════════════════════════════════════════════════════
    # ★★ 2026-09-27 第 46 轮：写回之后**回读校验**。
    #   理由同 `t_extract_assets`：用户机器上出现过"图出了、库里的角色却没了"，
    #   而当时报的是成功。出图一次要 2~4 分钟（本机实测 144~265 秒），
    #   这种"等了几分钟结果没保存"的失败必须**当场**暴露，不能装作成功。
    # ★ 2026-10-20：覆盖前先把旧参考图记进历史（永不丢用户确认过的脸）
    try:
        _old_path = str(obj.get("reference_image_path") or "").strip()
        if _old_path and _old_path != str(result["path"]).strip():
            _rf = obj.get("reference_features")
            if isinstance(_rf, str):
                import json as _json
                try:
                    _rf = _json.loads(_rf or "{}")
                except Exception:
                    _rf = {}
            _rf = dict(_rf or {}) if isinstance(_rf, dict) else {}
            _hist = list(_rf.get("previous_reference_images") or [])
            if _old_path not in _hist:
                _hist.insert(0, _old_path)
            _rf["previous_reference_images"] = _hist[:5]
            _rf["reference_image_source"] = "agent_redraw"
            if kind == "character":
                await _call(db.update_character, obj["id"],
                            reference_features=_rf,
                            reference_image_path=result["path"])
            else:
                await _call(db.update_scene, obj["id"],
                            reference_features=_rf,
                            reference_image_path=result["path"])
        else:
            if kind == "character":
                await _call(db.update_character, obj["id"],
                            reference_image_path=result["path"])
            else:
                await _call(db.update_scene, obj["id"],
                            reference_image_path=result["path"])
    except Exception as _e:
        logger.warning("参考图历史备份失败（继续写新图）：%s", _e)
        if kind == "character":
            await _call(db.update_character, obj["id"],
                        reference_image_path=result["path"])
        else:
            await _call(db.update_scene, obj["id"],
                        reference_image_path=result["path"])
    if kind == "character":
        back = await _call(db.get_character, obj["id"]) or {}
    else:
        back = await _call(db.get_scene, obj["id"]) or {}
    saved = str(back.get("reference_image_path") or "").strip()
    if saved != str(result["path"]).strip():
        logger.error("★ 出图写回失败：%s %s 期望 path=%s，库里是 %r",
                     kind, obj.get("id"), result["path"], saved)
        return err(
            f"图**画好了**（文件在 {result['path']}），但**没能记到资产库里**"
            f"（库里读到的是「{saved or '空'}」）。"
            f"这是数据库写入的问题，不是出图失败 —— 请把这条消息发给我。",
            path=result["path"], saved=saved,
        )

    return ok(kind=kind, name=obj.get("name"), path=result.get("path"),
              prompt=(result.get("prompt") or prompt)[:400],
              provider=provider,
              note=f"「{obj.get('name')}」的参考图出好了，已写进资产库。")


# ──────────────────────── 7. 指令式改图（后期再修改）────────────────────────


@REGISTRY.register(
    "refine_look",
    "**指令式改图**：拿资产现有的参考图 + 一句话，只改你要求改的地方，"
    "其余（长相、构图、背景）保持不动。"
    "用户说「把衣服换成军装」「背景改成夜景」时用这个，"
    "**不要**用 make_character_look 重画 —— 重画会换一张脸。",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True},
     "kind": {"type": "string", "desc": "character 或 scene", "required": True,
              "enum": ["character", "scene"]},
     "name": {"type": "string", "desc": "资产名"},
     "asset_id": {"type": "string", "desc": "也可以直接给 id"},
     "instruction": {"type": "string", "desc": "要改什么，如「换成灰蓝军装，加绑腿」",
                     "required": True},
     "keep": {"type": "string", "desc": "要保住什么，如「轮廓和背景」"}},
    needs_project=True, writes=True,
)
async def t_refine_look(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "project_id", "kind", "instruction")
    if bad:
        return bad
    pid = args["project_id"]
    kind = str(args["kind"]).strip().lower()
    if kind not in ("character", "scene"):
        return err("kind 只能是 character 或 scene")
    try:
        db, _, _ = _services()
        obj, bad = await _find_asset(db, pid, kind, args.get("asset_id"), args.get("name"))
        if bad:
            return bad
        base = (obj.get("reference_image_path") or "").strip()
        if not base:
            return err(f"「{obj.get('name')}」还没有参考图，改不了。"
                       f"先用 make_character_look / make_scene_look 出一张。")

        from core.imagegen import generate_image  # type: ignore
        settings = await _call(db.get_all_settings) or {}
        provider = str(args.get("provider") or settings.get("image_provider")
                       or "local").strip()
        api_key = ""
        for bucket in ("llm_api_keys", "api_keys", "tts_api_keys"):
            d = settings.get(bucket) or {}
            if isinstance(d, dict):
                for k, v in d.items():
                    if str(k).strip().lower() == provider.lower() and v:
                        api_key = str(v)
                        break
            if api_key:
                break

        instruction = str(args["instruction"]).strip()
        keep = str(args.get("keep") or "人物长相、构图与背景").strip()
        prompt = (f"只修改这一处：{instruction}。"
                  f"**必须保持不变**：{keep}。不要重画整张图。")

        # ══════════════════════════════════════════════════════════════
        # ★★ 角色精修也必须带 skill 的质感硬规则 —— 2026-09-26 补
        #
        # 这里原来**一条质感约束都没有**（只有"只改这一处 + 保持什么"）。
        # 而 img2img 是**重绘**：精修一遍皮肤会被重新合成，
        # 没有 NO WAX SKIN / 中性白平衡 / 干净电影感这几条，
        # 蜡像感、油腻高光、脸被环境染黄就会**被带回来** ——
        # 那正是 character-casting-studio 这个 skill 要治的病。
        #
        # 更糟的是：精修结果会**覆盖** `reference_image_path`（见下面
        # `update_character`），于是这个退化版本成了角色的**正式定妆照**，
        # 再作为参考图传到每一个镜头 —— 一次精修污染全片。
        #
        # 修法：调 `casting.build_refine_guard()`（正向中性肤色白平衡 +
        # 负向 NO WAX SKIN，和出图时**同一份** rules，单一真相源）。
        # 只对 character 加：这些约束是围绕人脸的，场景图不适用。
        #
        # ★ 2026-09-26 二次修正：第一版只加了**负向**串，漏了
        #   `neutral_skin_constraints`（正向）。只告诉模型"不要黄"、
        #   不说"应该是什么肤色"，它仍可能选一个偏亮的暖调。
        #   现已由 build_refine_guard 一并带上，并有测试断言两条都在。
        # ══════════════════════════════════════════════════════════════
        if kind == "character":
            try:
                from core.casting import build_refine_guard  # type: ignore
                _guard = build_refine_guard(obj)
                if _guard:
                    prompt += " " + _guard
            except Exception as _e:  # noqa: BLE001
                logger.warning(
                    "精修时补质感约束失败（本次精修**不带** skill 的硬规则，"
                    "有可能把蜡像感带回来）：%s: %s", type(_e).__name__, _e)

        cache_root = settings.get("cache_dir") or _os.path.join(
            _os.environ.get("VIDEOFORGE_DATA_DIR", _os.getcwd()), "cache")
        out_dir = _os.path.join(cache_root, "images", pid,
                                "characters" if kind == "character" else "scenes")
        result = await generate_image(
            provider=provider, api_key=api_key, prompt=prompt, out_dir=out_dir,
            size=args.get("size") or "", reference_images=[base],
            timeout=0.0,
        )
        if not result.get("ok"):
            return err(result.get("error") or "改图失败", provider=provider)

        if kind == "character":
            await _call(db.update_character, obj["id"],
                        reference_image_path=result["path"])
        else:
            await _call(db.update_scene, obj["id"],
                        reference_image_path=result["path"])
        return ok(kind=kind, name=obj.get("name"), path=result.get("path"),
                  instruction=instruction, kept=keep,
                  note=f"「{obj.get('name')}」已按你的要求改好，其余保持不变。")
    except Exception as e:
        logger.exception("refine_look failed")
        return err(f"改图失败：{type(e).__name__}: {e}")


# ──────────────────────── 小工具 ────────────────────────


def _now() -> str:
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S")


async def _skips_get(db, pid: str) -> Dict[str, Any]:
    try:
        s = await _call(db.get_all_settings) or {}
        all_skips = s.get("stage_skips") or {}
        if isinstance(all_skips, str):
            import json
            all_skips = json.loads(all_skips or "{}")
        v = all_skips.get(str(pid)) or {}
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


async def _skips_set(db, pid: str, val: Dict[str, Any]) -> None:
    try:
        s = await _call(db.get_all_settings) or {}
        all_skips = s.get("stage_skips") or {}
        if isinstance(all_skips, str):
            import json
            all_skips = json.loads(all_skips or "{}")
        if not isinstance(all_skips, dict):
            all_skips = {}
        all_skips[str(pid)] = val
        await _call(db.set_setting, "stage_skips", all_skips)
    except Exception as e:
        logger.warning("记 skip 失败：%s", e)
