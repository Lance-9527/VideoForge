# -*- coding: utf-8 -*-
"""VideoForge · 「改参重跑」的判定 / 记忆（agent 侧）

## 为什么需要这个文件（2026-10-25，T2）

用户在某个流程阶段不满意、要求修改/重做时，agent 会**反复撞同一条错误**。
机制层的根因（已实测，见 `docs`/任务回执）：

    · `run_stage` 的防覆盖守卫只有**一条**放行路：`params["redo"]` 为真；
    · 而 agent 能看到的阶段工具（DSH 插件 `vf_<stage>`）参数面只有
      `project_id` / `idea` / `confirm`（花钱阶段）—— **没有** redo / force；
    · 于是"用户要求改参重跑"永远被挡；错误文案让 agent 去调 `propose_step`，
      但那个工具**不在** DSH agent 的工具表里（它是 sidecar 内部聊天 agent 的），
      指令不可执行 ⇒ agent 改改参数再试 ⇒ 还是被挡 ⇒ 多轮同一句错误。

这里把三件事收成**单一真相源**（不碰 DB，纯函数；落库由调用方负责）：

    ① `decide()`       —— 这次算不算"改参重跑"：显式同意 / 参数与上次不同 ⇒ 放行；
    ② `make_run_entry()` —— 记下每次成功执行时的**参数指纹**，"参数变了"才有基线；
    ③ `bump_error()`   —— 同类错误连续出现时给出 `repeat_count`，
                          让上层把"不要再原样重试"写进 `next_action`。

★ 保护性约束**没有变弱**：
    · 显式 `redo=true`（老路径）照旧放行；
    · 有**待确认提案**在跑（propose→confirm 那条路，也是"8.8 小时老提案覆盖
      用户剧本"那次事故的来源）时，`decide()` 由调用方传
      `allow_params_change=False`，只认显式 redo，不会因为"参数看着不一样"就放行。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Dict, Optional, Tuple

#: 这些参数等价于"用户已经明确要求重做"（任一为真就放行）。
#:
#: · `redo`     —— 历史约定（`propose_step` 在阶段已完成时自动打上；老调用方也在用）
#: · `force`    —— 新增的显式入口（`rerun_stage(force=true)` / DSH 侧也可透传）
#: · `rerun` / `overwrite` —— 语义别名，防止调用方换了个词就又被挡
#: · `confirm`  —— 花钱阶段用户在插件里点了"同意花这笔钱"，那也是一次明确授权
EXPLICIT_REDO_KEYS: Tuple[str, ...] = (
    "redo", "force", "rerun", "overwrite", "confirm",
)

#: 不参与"参数指纹"的键：传输字段 / 重做开关 / 备注。
#: 它们变了**不代表**用户改了具体参数，不能据此自动放行。
_TRANSPORT_KEYS = frozenset({
    "project_id", "stage", "note", "reason",
    "_progress", "_resume", "_from_job", "_job_id",
}) | frozenset(EXPLICIT_REDO_KEYS)

#: settings 里存两份记忆：每项目每阶段的"上次成功参数"与"最近一次错误"。
MEMORY_SETTING_KEY = "stage_retry_memory"
MEMORY_VERSION = 1

#: 错误原文只留这么长做指纹（够区分，不把整段文案写进 settings）。
_ERROR_KEEP = 320


# ──────────────────────────── 参数指纹 ────────────────────────────


def _truthy(v: Any) -> bool:
    """把模型/HTTP 传进来的各种"真"写法都认下来（"true"/"1"/1/True…）。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "y", "是", "对")
    return bool(v)


#: 公开别名：工具层也要判断 "true"/"1"/"是" 这些写法（不改私有命名习惯）。
truthy = _truthy


def meaningful_params(params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """去掉传输字段/重做开关后，**真正表达用户意图**的那些参数。

    排序后用于指纹，所以返回 dict 的遍历顺序不重要（指纹里会 sort_keys）。
    空值（None / "" / [] / {}）不算"改了参数"—— 模型常把没填的参数塞成空串。
    """
    out: Dict[str, Any] = {}
    for k, v in (params or {}).items():
        key = str(k)
        if key in _TRANSPORT_KEYS or key.startswith("_"):
            continue
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        if isinstance(v, (list, dict, tuple, set)) and not v:
            continue
        out[key] = v
    return out


def params_fingerprint(stage: str, params: Optional[Dict[str, Any]]) -> str:
    """一次执行的"参数指纹"（sha256 前 16 位）。只对 meaningful 参数算。"""
    payload = json.dumps(
        {"stage": str(stage or ""), "params": meaningful_params(params)},
        ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def explicit_redo_key(params: Optional[Dict[str, Any]]) -> str:
    """返回第一个"显式要求重做"的键名；没有就返回空串。"""
    for k in EXPLICIT_REDO_KEYS:
        if _truthy((params or {}).get(k)):
            return k
    return ""


# ──────────────────────────── 放行判定 ────────────────────────────


def decide(stage: str, params: Optional[Dict[str, Any]], done: bool,
           run_entry: Optional[Dict[str, Any]],
           allow_params_change: bool = True) -> Dict[str, Any]:
    """这一次执行要不要按"重做"处理？

    参数
    ----
    stage : 阶段 key（只用于文案/指纹）
    params : 调用方传进来的参数（含 redo/force/confirm 等开关）
    done : 这一步是不是**已经做完了**（`stages.already_done`）
    run_entry : 上一次成功执行留下的 `make_run_entry()`（可能没有）
    allow_params_change : 允许"参数变了就自动放行"吗？
        · False = 这次是在执行**待确认提案**（propose→confirm 路径）。
          那条路要保留"过期老提案不许覆盖"的保护，只认显式 redo。
        · True  = agent 直接发起的重跑（插件 vf_<stage> → /api/v2/run 等），
          用户要改的东西通常只能放进参数里 ⇒ 参数变了就当作"改参重跑"。

    返回
    ----
    dict:
        allow            bool   是否放行
        reason           str    first_run / explicit_redo / params_changed / blocked_*
        auto_redo        bool   True = 因为是"参数变了"而自动放行的（非显式标记）
        approved_by      str    显式放行时用的键（redo/force/...）
        changed_keys     list   "参数变了"时，哪些键看起来变了（给人看，不保证精确）
        baseline_missing bool   之前没记过参数基线（只能按"给了具体参数"判断）
        has_params       bool   这次 meaningful 参数是否非空
    """
    res: Dict[str, Any] = {
        "allow": False, "reason": "first_run", "auto_redo": False,
        "approved_by": "", "changed_keys": [], "baseline_missing": False,
        "has_params": bool(meaningful_params(params)),
    }
    if not done:
        res["allow"] = True
        return res

    explicit = explicit_redo_key(params)
    if explicit:
        res.update(allow=True, reason="explicit_redo", approved_by=explicit)
        return res

    if not allow_params_change:
        # 待确认提案路径：只认显式 redo（保留防覆盖事故的原始保护）
        res["reason"] = "blocked_proposal_path"
        return res

    cur_params = meaningful_params(params)
    if not cur_params:
        # 原样重跑（一个具体参数都没给）⇒ 不放行，要求显式同意
        res["reason"] = "blocked_no_params"
        return res

    prev = run_entry or {}
    prev_fp = str(prev.get("fingerprint") or "")
    if not prev_fp:
        # 没有基线：这一步是在 `run_stage` 之外做成的（用户自己导入剧本、
        # 升级前的老项目……）。直连重跑（allow_params_change=True）都是
        # agent 当场替用户发起的，不是"挂了几小时的老提案" —— 给了具体参数
        # 就当作一次改参重跑放行；否则用户会永远卡在"改不了"上。
        # （待确认提案那条路已经在上面 `allow_params_change=False` 挡住了。）
        res.update(allow=True, reason="params_supplied", auto_redo=True,
                   baseline_missing=True,
                   changed_keys=sorted(cur_params.keys())[:8])
        return res

    cur_fp = params_fingerprint(stage, params)
    if cur_fp == prev_fp:
        res["reason"] = "blocked_same_params"
        res["changed_keys"] = []
        return res

    # 参数和上次成功的那次不一样 ⇒ 这就是"改参重跑"，放行。
    prev_keys = set(prev.get("keys") or [])
    cur_keys = set(cur_params.keys())
    res.update(allow=True, reason="params_changed", auto_redo=True,
               changed_keys=sorted(cur_keys | prev_keys)[:8])
    return res


def make_run_entry(stage: str, params: Optional[Dict[str, Any]],
                   ok: bool = True) -> Dict[str, Any]:
    """成功后记下的"这次参数长什么样"（写进 settings，供下次比对）。"""
    return {
        "fingerprint": params_fingerprint(stage, params),
        "keys": sorted(meaningful_params(params).keys()),
        "ok": bool(ok),
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


# ──────────────────────────── 错误记忆（防同一句错误多轮重复）────────────────


def normalize_error(text: Any) -> str:
    """错误文案归一化：空白折叠 + 截断。用于判断"是不是同一句错误"。"""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s[:_ERROR_KEEP]


def error_fingerprint(text: Any) -> str:
    return hashlib.sha256(normalize_error(text).encode("utf-8")).hexdigest()[:16]


def bump_error(prev: Optional[Dict[str, Any]], error: Any,
               next_action: str = "") -> Tuple[Dict[str, Any], int]:
    """更新"这个阶段最近一次错误"的记忆，返回 (新记忆, 连续出现次数)。

    ★ 只按**错误原文**判"同类"：同一句错误第二次出现就 count=2 —— 上层据此
      在 `next_action` 里明写"已经重复 N 次，别再原样重试"，避免多轮空转。
    """
    key = error_fingerprint(error)
    prev = prev or {}
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    same = str(prev.get("key") or "") == key
    count = (int(prev.get("count") or 0) + 1) if same else 1
    entry = {
        "key": key,
        "error": normalize_error(error),
        "count": count,
        "first_at": (prev.get("first_at") or now) if same else now,
        "last_at": now,
        "next_action": str(next_action or "")[:400],
    }
    return entry, count


# ──────────────────────────── 可行动的下一步 ────────────────────────────


_REASON_TEXT = {
    "blocked_no_params": "这次没带任何具体参数（等于原样重跑）",
    "blocked_same_params": "参数和上次成功那次一模一样（等于原样重跑）",
    "blocked_proposal_path": "待确认提案里没有重做标记（防过期提案覆盖）",
    "blocked_no_baseline": "没有它上次执行时的参数记录，无法判断参数变没变",
}


def reason_text(reason: str) -> str:
    """把内部 reason 翻成一句人话（进错误文案用）。"""
    return _REASON_TEXT.get(str(reason or ""), str(reason or "未满足重做条件"))


def redo_next_action(stage_key: str, title: str, project_id: str,
                     reason: str = "blocked_no_params",
                     repeat_count: int = 0) -> Tuple[str, Dict[str, Any]]:
    """被"已经做完"挡住时，给 agent 一条**真能执行**的下一步（不是空话）。

    为什么要写这么细：实测 agent 拿到"请重新 propose_step"后并没有那个工具，
    只能在原地改参数重试。这里必须按**调用方真实拥有的工具面**写：
      · DSH 的 `vf_<stage>`：只有 project_id / idea（所有阶段）/ confirm（花钱阶段）；
      · sidecar 内部聊天 agent：可以用 `rerun_stage(...)`。
    """
    dsh_tool = f"vf_{stage_key}"
    if reason == "blocked_same_params":
        why = ("这次的参数和**上次成功那次一模一样**，所以按「原样重跑」处理。"
               "同一步若只是原样再跑一遍，会覆盖现有内容/重复花钱，需要显式确认。")
    elif reason == "blocked_proposal_path":
        why = ("这次是在执行**待确认提案**，而提案里没有重做标记。"
               "过期的老提案正是覆盖事故的来源，所以不能自动放行。")
    elif reason == "blocked_no_baseline":
        why = ("这一步已经做完了，但**没有**它上次执行时的参数记录"
               "（可能是在别的入口做成的），无法判断「参数有没有变」。")
    else:
        why = "这次没带任何具体参数，按「原样重做」处理，需要显式确认。"

    action = (
        f"{why}\n"
        f"**下一步照这个做（不用猜 redo 标记）**：\n"
        f"  ① 用户要改东西 → 再调一次本阶段工具，把改动放进参数"
        f"（DSH 侧就是 `{dsh_tool}` 的 `idea`：把用户原话的修改意见填进去，"
        f"例：`idea=\"把背景改成民国\"`）。参数与上次不同 ⇒ 自动放行。\n"
        f"  ② 用户只是要**原样再跑一遍** → 把这条明确意见放进 `idea`"
        f"（例：`idea=\"重做：用户要求再来一版\"`），也会当作一次新的重做请求放行。\n"
        f"  ③ 后端工具链里可直接调 "
        f"`rerun_stage(project_id=\"{project_id}\", stage=\"{stage_key}\", force=true)` "
        f"—— 它内部处理 redo / 前置 / 幂等，不要再手改 `redo`。\n"
        f"  ★ 不要再用**完全相同**的参数重试：那只会得到同一句错误。"
    )
    retry = {
        "tool": "rerun_stage",
        "args": {"project_id": project_id, "stage": stage_key, "force": True},
        "dsh_tool": dsh_tool,
        "dsh_arg": {"idea": "重做：用户要求重来一版"},
    }
    if repeat_count >= 2:
        action = (f"⚠ 同一句错误已经重复出现 {repeat_count} 次了 —— "
                  f"**不要原样重试**。\n" + action)
    return action, retry


def failure_next_action(stage_key: str, error: Any) -> str:
    """非守卫类失败的"下一步"建议（按常见错误形状给，尽量具体）。"""
    e = str(error or "")
    low = e.lower()
    # 环境/凭据类：换参数/重试都没用，先去修配置 —— 这是最容易重复撞的一类
    if ("llm" in low or "api key" in low or "apikey" in low or "401" in low
            or "403" in low or "认证" in e or "未配置" in e or "unauthorized" in low):
        return ("这是**环境/凭据**问题（不是重做参数问题）：先去「设置」检查模型 / API Key "
                "配置，修好后再调；**不要**靠改参数或原样重试绕过它。")
    if "idea" in e or stage_key == "script":
        return ("先把「用户想拍什么」问清楚，把他说的**原话**放进 `idea` 再调用一次"
                "（不要用占位文字/问句本身当 idea）。")
    if "缺少必填参数" in e:
        return ("按报错列出的参数补上再调一次；参数名照工具签名写，"
                "**不要**把参数说明文字当成参数值。")
    if "没有分镜" in e or "还没有分镜" in e:
        return "先做「分镜表」这一步（plan_shots），再回来跑这一步。"
    if "还没有角色" in e or "还没有场景" in e or "还没有角色/场景" in e:
        return "先做「角色 / 场景」这一步（extract_assets），再回来跑这一步。"
    if "没有参考图" in e:
        return "先给定妆照/场景图出图（make_looks），再做这一步。"
    if "项目不存在" in e:
        return "先调 list_projects 拿真实 project_id，再重试。"
    return ("按 error 里说的原因修好再调一次；**不要**用完全相同的参数原样重试"
            "（会得到同一句错误）。")


# ──────────────────────── settings 里的记忆结构 ────────────────────────


def memory_doc(raw: Any) -> Dict[str, Any]:
    """把 settings 里读到的值规范成 `{v, runs, errors}`（坏数据不炸）。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except Exception:  # noqa: BLE001
            raw = {}
    doc = raw if isinstance(raw, dict) else {}
    runs = doc.get("runs") if isinstance(doc.get("runs"), dict) else {}
    errors = doc.get("errors") if isinstance(doc.get("errors"), dict) else {}
    return {"v": MEMORY_VERSION, "runs": runs, "errors": errors}


def runs_for(doc: Dict[str, Any], pid: str) -> Dict[str, Any]:
    runs = doc.get("runs") or {}
    per = runs.get(str(pid)) if isinstance(runs, dict) else None
    return per if isinstance(per, dict) else {}


def errors_for(doc: Dict[str, Any], pid: str) -> Dict[str, Any]:
    errs = doc.get("errors") or {}
    per = errs.get(str(pid)) if isinstance(errs, dict) else None
    return per if isinstance(per, dict) else {}


def with_run(doc: Dict[str, Any], pid: str, stage: str,
             entry: Dict[str, Any]) -> Dict[str, Any]:
    """返回一份新 doc：记下这个阶段的成功参数；同时清掉它的错误记忆。"""
    new = {"v": MEMORY_VERSION,
           "runs": dict(doc.get("runs") or {}),
           "errors": dict(doc.get("errors") or {})}
    per_run = dict(new["runs"].get(str(pid)) or {})
    per_run[str(stage)] = entry
    new["runs"][str(pid)] = per_run
    per_err = dict(new["errors"].get(str(pid)) or {})
    per_err.pop(str(stage), None)
    new["errors"][str(pid)] = per_err
    return new


def with_error(doc: Dict[str, Any], pid: str, stage: str,
               entry: Dict[str, Any]) -> Dict[str, Any]:
    """返回一份新 doc：记下这个阶段最近一次错误。"""
    new = {"v": MEMORY_VERSION,
           "runs": dict(doc.get("runs") or {}),
           "errors": dict(doc.get("errors") or {})}
    per_err = dict(new["errors"].get(str(pid)) or {})
    per_err[str(stage)] = entry
    new["errors"][str(pid)] = per_err
    return new
