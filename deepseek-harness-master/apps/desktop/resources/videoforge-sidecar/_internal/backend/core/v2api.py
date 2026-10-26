# -*- coding: utf-8 -*-
"""VideoForge v2 · sidecar API（给 DSH 侧注册真正的后台任务）

═══════════════════════════════════════════════════════════════════
这个模块解决什么
═══════════════════════════════════════════════════════════════════
v2 要把「执行」交给 DSH 的 jobs 子系统，Python 侧退成 sidecar。于是 sidecar
要提供三件 DSH 能直接用的东西：

  S1  job 语义   `POST /api/v2/run` 立刻返回 job_id，真正执行在后台
  S2  花费预估   审批卡要显示金额，不能靠模型口算
  S3  数据源纪律 sidecar 的库是唯一真相（本模块只写 `tasks` 行，不碰业务表）

═══════════════════════════════════════════════════════════════════
四条纪律（违反任何一条都算改坏）
═══════════════════════════════════════════════════════════════════
1. **执行入口只有一个**：`core.agent_tools_stages.run_stage`。
   本模块只负责"提交 + 记账 + 转播进度"，**不重写任何阶段业务**。
2. **进度不另造一套**：阶段进度一律读 `core.stages` 的 EVIDENCE
   （`_ev_looks` 已经算好 have/total，`_ev_draft`/`_ev_final` 也是）。
   本模块不自己数文件、不自己造百分比。
3. **金额不另写一套**：单价来自 `core.shot_segments._estimate_segment_cost_yuan`
   （2026-09-30 用户真实账单反推的那张表）；余额/闸门来自 `core.budget`。
4. **不动业务数据**：只 `db.create_task` / `db.update_task`（tasks 表）。
   `project_id`/`stage`/`params` 全部塞在 payload 里，不新增表、不改 schema。

═══════════════════════════════════════════════════════════════════
job 的 id 空间
═══════════════════════════════════════════════════════════════════
`job_id` 就是 `tasks.id`（同一个 id），不另发一套 id。
理由：`tasks` 表已有 status/progress/progress_message/result/error/cancellable，
     而且进程重启后仍然查得到 —— 这正是 DSH 侧要的"真 job"。
     陈旧的 `interrupted` 语义也复用 `/stage/job` 已有的判法（见 main.py）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

logger = logging.getLogger("videoforge.v2api")

#: tasks.type —— 一眼能看出这条 job 是 v2 提交的
JOB_TYPE = "v2_stage"

#: 内存里的 job 现场（进程内）。**只放"事件环"和运行态**，
#: 真实状态以 tasks 行为准（重启后靠 DB 兜底）。
_JOB_STATE: Dict[str, Dict[str, Any]] = {}
_JOB_TASKS: Dict[str, "asyncio.Task[Any]"] = {}

#: 事件环上限（DSH 侧只回放最近这么多条，够覆盖一次阶段执行）
_EVENT_RING = 400

#: SSE 轮询间隔（秒）—— 0.4s 与既有 `/api/tasks/{tid}/stream` 一致
POLL_INTERVAL = 0.4

#: 心跳注释间隔（秒）：长时间没新事件时也发一行，防中间层掐连接
HEARTBEAT_INTERVAL = 10.0


class V2Error(ValueError):
    """参数/前置不合法 —— 路由层转成 400（不是 500）。"""


# ══════════════════════════════════════════════════════════════════
# 小工具
# ══════════════════════════════════════════════════════════════════

def _json_list(v: Any) -> List[Any]:
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            v = json.loads(v or "[]")
        except Exception:  # noqa: BLE001
            return []
    return v if isinstance(v, list) else []


def _now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _task_payload(task: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    p = (task or {}).get("payload")
    if isinstance(p, dict):
        return p
    try:
        return json.loads(p or "{}")
    except Exception:  # noqa: BLE001
        return {}


def _stage_or_raise(stage: str):
    from core import stages as S
    st = S.STAGE_BY_KEY.get(stage or "")
    if st is None:
        raise V2Error(
            f"没有这个阶段：{stage or '(空)'}（可选：{', '.join(S.STAGE_ORDER)}）")
    return st


def _require_project(db, pid: str) -> Dict[str, Any]:
    if not str(pid or "").strip():
        raise V2Error("缺 project_id")
    proj = db.get_project(pid)
    if not proj:
        raise V2Error(f"项目不存在：{pid}")
    return proj


# ══════════════════════════════════════════════════════════════════
# 进度：**只从 core.stages 的 EVIDENCE 推**
# ══════════════════════════════════════════════════════════════════

def progress_snapshot(db, pid: str, stage: str) -> Dict[str, Any]:
    """当前阶段进度（分数 + 人话）。数据源 = `core.stages.EVIDENCE[stage]`。

    为什么不用"已执行秒数"当进度：那不是业务进度。用户要看的是
    "9 个资产出了 3 张图" / "5 个镜头出过 2 条小样" —— 这正是 EVIDENCE
    已经在算的东西（`have`/`total`）。本函数只是把它的字段翻成 0~1。
    """
    from core import stages as S
    snap: Dict[str, Any] = {
        "stage": stage, "status": "", "done": False,
        "progress": 0.0, "have": None, "total": None,
        "message": "", "detail": "",
    }
    try:
        snap["status"] = S.stage_status(db, pid, stage) or ""
    except Exception as e:  # noqa: BLE001
        logger.debug("取阶段状态失败 %s: %s", stage, e)

    ev: Dict[str, Any] = {}
    try:
        fn = S.EVIDENCE.get(stage)
        if fn is not None:
            ev = fn(db, pid) or {}
    except Exception as e:  # noqa: BLE001
        logger.debug("取阶段证据失败 %s: %s", stage, e)

    snap["done"] = bool(ev.get("done"))
    snap["detail"] = str(ev.get("detail") or "")
    have, total = ev.get("have"), ev.get("total")
    if isinstance(total, int) and total > 0 and isinstance(have, int):
        snap["have"], snap["total"] = have, total
        snap["progress"] = round(max(0.0, min(1.0, have / float(total))), 4)
    else:
        # 没有计数的阶段（brief/script/assets/shots/compose）：只能二值
        snap["progress"] = 1.0 if snap["done"] else 0.0
    snap["message"] = snap["detail"] or (
        "已完成" if snap["done"] else "进行中…")
    return snap


def _emit(job_id: str, event: str = "progress", **fields: Any) -> Dict[str, Any]:
    """往 job 的事件环里追加一条。返回这条事件（方便调用方直接用）。"""
    st = _JOB_STATE.setdefault(job_id, {"events": [], "state": "running"})
    ring: List[Dict[str, Any]] = st.setdefault("events", [])
    item = {"event": event, "seq": len(ring) + 1, "ts": time.time(), **fields}
    ring.append(item)
    if len(ring) > _EVENT_RING:
        del ring[: len(ring) - _EVENT_RING]
    return item


# ══════════════════════════════════════════════════════════════════
# S2 · 花费预估（审批卡用）
# ══════════════════════════════════════════════════════════════════

#: 各阶段每"单位"的秒数（人给的eta是区间，这里给个可算的期望值）
_ETA_PER_UNIT = {
    "looks": 150.0,     # 每张图 1~4 分钟
    "draft": 300.0,     # 每条小样 2~8 分钟
    "final": 600.0,     # 每条成片 5~15 分钟
    "previz": 120.0,    # 每镜 1~3 分钟
    "compose": 180.0,
}

#: 判"这一档已经出过"的来源标记 —— 与 `core.stages._ev_draft/_ev_final` **同一口径**。
#: 刻意照抄那两个函数的判据，不另立标准（否则预估和"做完没"会各说各话）。
_TIER_MARK = {
    "draft": ("preview", "480", "draft"),
    "final": ("final", "1080", "upgrade"),
}


def _tier_done(shot: Dict[str, Any], stage: str) -> bool:
    marks = _TIER_MARK.get(stage) or ()
    if not marks:
        return False
    for c in _json_list(shot.get("candidates")):
        src = str((c or {}).get("source") or "").lower()
        if any(m in src for m in marks):
            return True
    return False


def _shots(db, pid: str) -> List[Dict[str, Any]]:
    shots = db.list_shots(pid) or []
    return sorted(shots, key=lambda s: s.get("order_index") or 0)


# ── 视频计价口径（2026-10-21 加）──────────────────────────────────
#
# ★ 为什么不能再用"除了 seedance 都按 0.9 元/秒"那套：
#   `core/shot_segments._estimate_segment_cost_yuan` 的 else 分支是
#   **可灵体感价**（0.9 元/秒）。出片决定走 token6688 网关（`seedance_t6`）后，
#   拿 0.9 元/秒去报 t6 的价 = 给审批卡报了**错的金额**。
#   审批卡上写错钱比不写更糟（用户按错数批钱），所以：
#     · 有本地价目表的 provider → 给具体金额；
#     · 没有价目表的（尤其 seedance_t6 网关）→ **cost_yuan=None** +
#       needs_approval=True + "金额待确认"，另外给一个**文档下界**（不是报价）。
#
#: 有"真实账单反推"价目表的 provider（走 `_estimate_segment_cost_yuan`）
_ARK_PRICED = ("seedance", "doubao", "jimeng")
#: 只有"用户体感价"0.9 元/秒的 provider（可灵，无人民币官方价，按灵感值）
_ROUGH_0_9 = ("kling", "kling_mcp")
#: 明确**没有**本地价目表、必须报"待确认"的 provider
_UNPRICED = ("seedance_t6",)

#: token6688 网关按秒计费的下界（`core/adapters/seedance_t6.py:36` 原文：
#: "计费：按秒（token6688 后端各厂商通道不同，最便宜的约 0.42 元/秒）"）
#: ⚠️ 这是**下界**，不是报价 —— 只用来给审批卡一个"至少多少钱"的锚。
_T6_FLOOR_YUAN_PER_SEC = 0.42


def _provider_price(provider: str, duration: float, resolution: str
                    ) -> Tuple[Optional[float], float, str]:
    """单镜价格 → (金额 or None, 下界, 依据一句话)。

    返回 None 表示**本地无法定价**（不猜、不用别的 provider 的价顶上）。
    """
    p = str(provider or "").strip().lower()
    d = max(1.0, float(duration or 5))
    if p in _UNPRICED:
        return None, round(_T6_FLOOR_YUAN_PER_SEC * d, 2), (
            "token6688 网关按秒计费，本地没有 480p/1080p 价目表"
            f"（adapter 文档原文：最便宜约 {_T6_FLOOR_YUAN_PER_SEC} 元/秒）")
    if p in _ARK_PRICED:
        from core.shot_segments import _estimate_segment_cost_yuan as _seg
        return float(_seg(p, d, resolution) or 0.0), 0.0, (
            "火山方舟价目（2026-09-30 用户真实账单反推）")
    if p in _ROUGH_0_9:
        from core.shot_segments import _estimate_segment_cost_yuan as _seg
        return float(_seg(p, d, resolution) or 0.0), 0.0, (
            "可灵无人民币官方价（按灵感值），0.9 元/秒是用户体感估值")
    return None, 0.0, f"provider「{p or '未知'}」本地没有价目表"


def _resolve_video_provider(db, shot: Dict[str, Any],
                            params: Dict[str, Any],
                            settings: Dict[str, Any]) -> Tuple[str, str, str]:
    """这一镜**实际会用哪个 provider/model** → (provider, model, 来源)。

    优先级与主流程一致（`main.py:1130` 的口径）：
        params 显式指定 > 设置里的 default_model_provider > 分镜自己的 > "seedance"
    ★ 返回"来源"是为了让调用方能看出"预估按谁算的"——
      分镜写 kling、设置写 seedance_t6 时，这个不一致本身就是要暴露的信息。
    """
    p = str(params.get("provider") or params.get("model_provider") or "").strip()
    m = str(params.get("model_name") or "").strip()
    if p:
        return p, m or str(shot.get("model_name") or ""), "request_params"
    sp = str(settings.get("default_model_provider") or "").strip()
    if sp:
        return sp, str(settings.get("default_model_name") or m
                       or shot.get("model_name") or ""), "settings.default_model_provider"
    bp = str(shot.get("model_provider") or "").strip()
    if bp:
        return bp, m or str(shot.get("model_name") or ""), "shot"
    return "seedance", m, "fallback"


def shot_progress(db, pid: str, stage: str) -> Dict[str, Any]:
    """多镜阶段的**逐镜**进度 —— 判据与 `core.stages` 的 evidence **同源**。

    · draft  → 与 `stages._ev_draft` 同一判据（candidates 的 source 含 preview/480/draft）
    · final  → 与 `stages._ev_final` 同一判据（source 含 final/1080/upgrade）
    · previz → 与 `stages.PREVIZ_OK_STATES` 同一判据（previz_status / previz_path）

    返回 `{stage,total,have,done_ids,shots:[{shot_id,index,order,done}]}`。
    这是给"第 N/M 镜"事件用的，**不改聚合口径**（聚合仍走 EVIDENCE）。
    """
    from core import stages as S
    shots = _shots(db, pid)
    rows: List[Dict[str, Any]] = []
    done_ids: List[str] = []
    for i, s in enumerate(shots):
        ok = False
        if stage in ("draft", "final"):
            ok = _tier_done(s, stage)
        elif stage == "previz":
            pst = str(s.get("previz_status") or "")
            ppath = str(s.get("previz_path") or "")
            ok = bool(pst in getattr(S, "PREVIZ_OK_STATES", ())
                      or (ppath and os.path.exists(ppath)))
        if ok:
            done_ids.append(str(s.get("id") or ""))
        rows.append({
            "shot_id": s.get("id"),
            "index": i + 1,
            "order": (s.get("order_index") or 0) + 1,
            "done": bool(ok),
        })
    total = len(shots)
    return {"stage": stage, "total": total, "have": len(done_ids),
            "done_ids": done_ids, "shots": rows}


#: 需要"逐镜"进度的阶段（多镜；compose 是整片级，不走这里）
_SHOT_STAGES = ("draft", "final", "previz")


def estimate_stage(db, pid: str, stage: str,
                   params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """这一步**预计**要花多少钱、多久；以及要不要审批。

    ★ 只读：不发任何网络请求、不写任何表、不扣费。
      金额来源是 `core.shot_segments._estimate_segment_cost_yuan`（真实账单反推的
      保守估值），余额来源是 `core.budget`。
    """
    from core import stages as S
    st = _stage_or_raise(stage)
    _require_project(db, pid)
    params = params or {}

    lines: List[Dict[str, Any]] = []
    cost: Optional[float] = 0.0
    cost_floor: Optional[float] = None
    units = 0
    basis = ""
    unpriced = False

    if stage in ("draft", "final"):
        resolution = "480p" if stage == "draft" else "1080p"
        n_cand = max(1, int(params.get("num_candidates") or 1))
        settings_v = db.get_all_settings() or {}
        all_shots = _shots(db, pid)
        todo = [s for s in all_shots if not _tier_done(s, stage)]
        done_n = len(all_shots) - len(todo)
        unknown = 0
        floor = 0.0
        for s in todo:
            prov, model, src = _resolve_video_provider(db, s, params, settings_v)
            dur = float(s.get("duration_seconds") or 5)
            unit, fl, why = _provider_price(prov, dur, resolution)
            floor += fl * n_cand
            if unit is None:
                unknown += 1
                yuan = None
            else:
                yuan = round(unit * n_cand, 2)
                cost += yuan
            units += 1
            lines.append({
                "shot_id": s.get("id"),
                "order": (s.get("order_index") or 0) + 1,
                "provider": prov,
                "model_name": model,
                "provider_source": src,
                "duration_seconds": dur,
                "resolution": resolution,
                "candidates": n_cand,
                "yuan": yuan,                 # None = 本地无法定价
                "price_basis": why,
            })
        basis = (f"{len(todo)} 个镜头待出（{resolution}）"
                 + (f"；{done_n} 个已出过这一档，不计费" if done_n else "")
                 + (f"；候选数 {n_cand}" if n_cand > 1 else ""))
        if unknown:
            basis += (f"；其中 {unknown} 个镜头的 provider 本地没有价目表 → "
                      f"总金额**待确认**（只给下界 ¥{floor:.2f}）")
        if not todo:
            basis = "所有镜头都已经出过这一档，无需再花钱"
        if unknown:
            cost = None                    # ★ 混了不可定价的 provider：宁可不给数
            cost_floor = round(floor, 2)
        else:
            cost_floor = None

    elif stage == "looks":
        ev = {}
        try:
            ev = S.EVIDENCE["looks"](db, pid) or {}
        except Exception as e:  # noqa: BLE001
            logger.warning("预估 looks 时取证据失败：%s", e)
        total = int(ev.get("total") or 0)
        have = int(ev.get("have") or 0)
        units = max(0, total - have)
        settings = db.get_all_settings() or {}
        img_provider = str(settings.get("image_provider") or "local").strip().lower()
        if img_provider == "local":
            unit_px = 0.0
            basis = ("本机出图（ComfyUI），**0 元**；"
                     f"待出 {units} 张（{have}/{total} 已有图）")
        else:
            try:
                unit_px = float(settings.get("image_price_yuan") or 0.0)
            except (TypeError, ValueError):
                unit_px = 0.0
            if unit_px <= 0:
                # ★ 非本机出图、又没配单价 → **不猜**：金额待确认
                unpriced = True
                basis = (f"图像模型 {img_provider}：本地没配单价"
                         f"（`image_price_yuan`）→ **金额待确认**；"
                         f"待出 {units} 张（{have}/{total} 已有图）")
            else:
                basis = (f"图像模型 {img_provider}：{unit_px:.2f} 元/张 × {units} 张"
                         f"（{have}/{total} 已有图）")
        if unpriced:
            cost = None
            lines.append({"kind": "images", "count": units,
                          "unit_yuan": None, "yuan": None,
                          "price_basis": f"图像模型 {img_provider} 单价未配置"})
        else:
            cost = round(unit_px * units, 2)
            lines.append({"kind": "images", "count": units,
                          "unit_yuan": round(unit_px, 2), "yuan": cost})

    else:
        basis = (f"这一步是「{st.cost_label}」——"
                 + ("不调用付费模型" if st.cost == "free"
                    else "计费方式无法在本地预判，按 0 元估，实际以账单为准"))

    # ── 预算与花钱闸（复用 core.budget，不另写）──
    budget_info: Dict[str, Any] = {}
    try:
        from core import budget as _b
        # ★ 注意签名是 `budget_status(project_id, db=None)` —— 参数顺序别写反
        #   （第一版写反了：把 db 当 project_id，结果 budget 内部把字符串当 db 用，
        #     异常被吞 → 预算恒显示 0 元/不限。curl 实测抓到，已修。）
        budget_info = dict(_b.budget_status(pid, db=db))
        allowed, why = _b.spend_enabled(db)
        budget_info["spend_allowed"] = bool(allowed)
        budget_info["spend_block_reason"] = why
        rem = budget_info.get("remaining_yuan")
        if isinstance(rem, (int, float)) and rem >= 0 and isinstance(cost, (int, float)):
            budget_info["affordable"] = bool(cost <= rem)
        else:
            # 金额未知（None）时**不装作算得出**：给 None，让界面显示"待确认"
            budget_info["affordable"] = None if cost is None else True
    except Exception as e:  # noqa: BLE001
        logger.warning("预估时取预算失败：%s", e)

    # ── 时长 ──
    per_unit = _ETA_PER_UNIT.get(stage, 0.0)
    eta_seconds = int(max(30.0, per_unit * max(units, 1 if per_unit else 0))) \
        if per_unit else 30

    # ★ 三态：有钱数 / 确定不要钱 / **金额待确认**
    cost_known = cost is not None
    cost_value = round(float(cost), 2) if cost_known else None
    will_spend: Optional[bool] = (True if (cost_value or 0) > 0 else False) \
        if cost_known else None
    # 金额拿不到时必须走审批（宁可多问一句，也不能让用户按错数批钱）
    needs_approval = bool(st.gate) or not cost_known
    if not cost_known:
        message = ("金额待确认：" + basis)
    else:
        message = ""
    return {
        "stage": stage,
        "title": st.title,
        "tool": st.tool,
        # ★ 验收要的四个字段
        "cost_yuan": cost_value,              # None = 本地无法定价（不是 0）
        "eta": st.eta,
        "eta_seconds": eta_seconds,
        "needs_approval": needs_approval,
        # 补充（审批卡需要的上下文）
        "cost_known": cost_known,
        "cost_floor_yuan": cost_floor,         # 不可定价时的**文档下界**，不是报价
        "message": message,
        "will_spend": will_spend,
        "cost_label": st.cost_label,
        "basis": basis,
        "units": units,
        "lines": lines[:40],
        "budget": budget_info,
        "project_id": pid,
        "dry_run": True,
    }


# ══════════════════════════════════════════════════════════════════
# S1 · 提交 job（立刻返回）
# ══════════════════════════════════════════════════════════════════

async def _watch_evidence(db, pid: str, job_id: str, stage: str,
                          stop: "asyncio.Event") -> None:
    """盯着 EVIDENCE 的变化，变了就发一条进度事件。

    ★ 这就是"复用阶段证据"的落点：进度条不是按时间走的，是按
      "又出了一张图 / 又出了一条小样"走的。
    """
    last: Optional[Tuple[Any, ...]] = None
    seen_shots: set = set()
    first_shot_poll = True
    while not stop.is_set():
        try:
            snap = progress_snapshot(db, pid, stage)
        except Exception as e:  # noqa: BLE001
            logger.debug("看进度失败：%s", e)
            snap = None
        if snap is not None:
            fp = (snap["progress"], snap["have"], snap["total"],
                  snap["done"], snap["message"][:80])
            if fp != last:
                last = fp
                _emit(job_id, "progress",
                      stage=stage, status="running",
                      progress=snap["progress"],
                      have=snap["have"], total=snap["total"],
                      done=snap["done"],
                      message=snap["message"],
                      source="evidence")
                try:
                    db.update_task(job_id, progress=float(snap["progress"]),
                                   progress_message=str(snap["message"])[:200])
                except Exception:  # noqa: BLE001
                    pass

        # ── 多镜阶段：**每完成一镜发一条**（第 N/M 镜）──────────────
        #   判据来自 `shot_progress()`（与 core.stages 的 evidence 同源）：
        #   draft/final 看候选的 source，previz 看 previz_status/previz_path。
        #   第一次轮询要把**已经完成**的也补发（否则订阅者不知道从哪开始）。
        if stage in _SHOT_STAGES:
            try:
                sp = shot_progress(db, pid, stage)
            except Exception as e:  # noqa: BLE001
                logger.debug("逐镜进度失败 %s: %s", stage, e)
                sp = None
            if sp and sp.get("total"):
                total = int(sp["total"])
                for row in sp["shots"]:
                    sid = str(row.get("shot_id") or "")
                    if not row.get("done") or sid in seen_shots:
                        continue
                    seen_shots.add(sid)
                    have = int(sp["have"])
                    idx = int(row.get("index") or 0)
                    _emit(job_id, "progress",
                          stage=stage, status="running",
                          progress=round(have / float(total), 4),
                          have=have, total=total,
                          shot_id=sid, shot_index=idx,
                          message=f"第 {have}/{total} 镜（第 {idx} 镜出好了）",
                          source="shot")
                    try:
                        db.update_task(
                            job_id, progress=round(have / float(total), 4),
                            progress_message=f"第 {have}/{total} 镜")
                    except Exception:  # noqa: BLE001
                        pass
                if first_shot_poll and not sp.get("done_ids"):
                    # 一镜都没完成时也发一条"0/M"，让 DSH 侧先见到分母
                    _emit(job_id, "progress",
                          stage=stage, status="running", progress=0.0,
                          have=0, total=total,
                          message=f"第 0/{total} 镜（还没出片）",
                          source="shot")
                first_shot_poll = False

        try:
            await asyncio.wait_for(stop.wait(), timeout=0.6)
        except asyncio.TimeoutError:
            continue
        except Exception:  # noqa: BLE001
            break


async def _run_job(db, pid: str, job_id: str, stage: str,
                   params: Dict[str, Any], title: str) -> None:
    """后台真正跑这一步 —— **只调 run_stage**，不碰业务。"""
    from core import stages as S
    from core.agent_tools_stages import run_stage as _run_stage

    t0 = time.time()
    _JOB_STATE.setdefault(job_id, {})["state"] = "running"
    try:
        db.update_task(job_id, status="running", started_at=_now_iso(),
                       progress=0.0, progress_message=f"开始「{title}」…")
    except Exception:  # noqa: BLE001
        pass
    _emit(job_id, "progress", stage=stage, status="running", progress=0.0,
          message=f"开始「{title}」…", source="job")

    stop = asyncio.Event()
    watcher = asyncio.create_task(_watch_evidence(db, pid, job_id, stage, stop))
    ok = False
    out: Dict[str, Any] = {}
    try:
        res = await _run_stage(db, pid, stage, params)
        ok = bool(isinstance(res, dict) and res.get("ok"))
        if ok:
            try:
                S.consume_pending(pid)      # 与 /stage/confirm 完全一致
            except Exception:  # noqa: BLE001
                pass
        try:
            nxt = str(S.next_stage_key(db, pid) or "")
        except Exception:  # noqa: BLE001
            nxt = ""
        out = {
            "ok": ok,
            "stage": stage,
            "tool": (res or {}).get("tool") if isinstance(res, dict)
            else S.STAGE_BY_KEY[stage].tool,
            "error": "" if ok else str((res or {}).get("error") or "未知错误")
            if isinstance(res, dict) else str(res),
            "summary": str((res or {}).get("note") or "") if isinstance(res, dict) else "",
            "result": res,
            "next": S.STAGE_BY_KEY[nxt].as_dict() if nxt in S.STAGE_BY_KEY else None,
            "elapsed_seconds": round(time.time() - t0, 1),
        }
        if ok and stage in ("compose", "final"):
            try:
                db.update_project(pid, status="completed")
            except Exception:  # noqa: BLE001
                pass
    except asyncio.CancelledError:
        out = {"ok": False, "stage": stage, "error": "cancelled",
               "summary": "已取消", "result": {}, "next": None,
               "elapsed_seconds": round(time.time() - t0, 1)}
        ok = False
        try:
            db.update_task(job_id, status="failed", error="cancelled",
                           finished_at=_now_iso(), progress_message="已取消")
        except Exception:  # noqa: BLE001
            pass
        _emit(job_id, "progress", stage=stage, status="cancelled", progress=0.0,
              message="已取消", source="job")
        _JOB_STATE.setdefault(job_id, {})["state"] = "cancelled"
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("v2 job 执行失败 job=%s stage=%s", job_id, stage)
        out = {"ok": False, "stage": stage, "error": f"{type(e).__name__}: {e}",
               "summary": "", "result": {}, "next": None,
               "elapsed_seconds": round(time.time() - t0, 1)}
        ok = False
    finally:
        stop.set()
        try:
            await asyncio.wait_for(watcher, timeout=3.0)
        except Exception:  # noqa: BLE001
            watcher.cancel()
        _JOB_TASKS.pop(job_id, None)

    # ── 收尾：写库 + 发终态事件 ──
    try:
        db.update_task(
            job_id,
            status="completed" if ok else "failed",
            progress=1.0,
            result=out,
            error=("" if ok else str(out.get("error") or ""))[:500] or None,
            finished_at=_now_iso(),
            progress_message=("已完成" if ok else "失败：" + str(out.get("error") or "")[:120]),
        )
    except Exception:  # noqa: BLE001
        pass
    # ★ 终态也发一条 progress（保证订阅者至少收到 2 条进度，而不是只有一条 done）
    _emit(job_id, "progress", stage=stage,
          status="completed" if ok else "failed",
          progress=1.0 if ok else 0.0,
          message=out.get("summary") or ("已完成" if ok else str(out.get("error") or "失败")),
          source="job")
    _emit(job_id, "done", stage=stage, ok=ok, result=out)
    _JOB_STATE.setdefault(job_id, {})["state"] = "completed" if ok else "failed"
    _JOB_STATE[job_id]["out"] = out


async def start_job(db, pid: str, stage: str,
                    params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """提交一个后台 job，**立刻**返回 job_id。"""
    from core import stages as S

    st = _stage_or_raise(stage)
    _require_project(db, pid)
    params = dict(params or {})
    params.setdefault("project_id", pid)

    # 去重：同一项目同一阶段已在跑 → 返回既有 job（避免重复花钱）
    for jid, state in list(_JOB_STATE.items()):
        if (state or {}).get("state") == "running" \
                and state.get("project_id") == pid \
                and state.get("stage") == stage:
            return {"job_id": jid, "reused": True, "stage": stage,
                    "status": "running",
                    "message": "这一步已经在后台执行中，复用原 job（没有重复提交）"}

    job_id = db.create_task(JOB_TYPE, {
        "project_id": pid, "stage": stage, "params": params, "v2": True,
        "created_by": "v2api",
    })
    _JOB_STATE[job_id] = {"state": "running", "project_id": pid, "stage": stage,
                          "started": time.time(), "events": []}
    _JOB_TASKS[job_id] = asyncio.create_task(
        _run_job(db, pid, job_id, stage, params, st.title))

    return {
        "job_id": job_id,          # 与 tasks.id 同一个 id
        "stage": stage,
        "title": st.title,
        "status": "running",
        "stream": f"/api/v2/jobs/{job_id}/events",
        "poll": f"/api/v2/jobs/{job_id}",
        "reused": False,
    }


# ══════════════════════════════════════════════════════════════════
# 查询 + 取消
# ══════════════════════════════════════════════════════════════════

def _synth_events_from_db(db, job_id: str, task: Dict[str, Any]) -> List[Dict[str, Any]]:
    """进程重启后事件环没了 —— 用 tasks 行合成一条快照，别让订阅者干等。"""
    pl = _task_payload(task)
    stage = str(pl.get("stage") or "")
    status = str(task.get("status") or "")
    prog = float(task.get("progress") or 0.0)
    msg = str(task.get("progress_message") or "")
    evs = [{"event": "progress", "seq": 1, "ts": time.time(),
            "stage": stage, "status": status, "progress": prog,
            "message": msg or f"job {job_id} 状态：{status}", "source": "db"}]
    if status in ("completed", "failed"):
        evs.append({"event": "done", "seq": 2, "ts": time.time(),
                    "stage": stage, "ok": status == "completed",
                    "result": task.get("result")})
    return evs


def get_job(db, job_id: str) -> Dict[str, Any]:
    """job 现状（DSH 侧轮询用；SSE 是主通道）。"""
    task = db.get_task(job_id)
    if not task:
        raise V2Error(f"job 不存在：{job_id}")
    pl = _task_payload(task)
    live = _JOB_STATE.get(job_id) or {}
    return {
        "job_id": job_id,
        "project_id": pl.get("project_id"),
        "stage": pl.get("stage"),
        "status": task.get("status"),
        "progress": task.get("progress"),
        "message": task.get("progress_message"),
        "error": task.get("error"),
        "result": task.get("result"),
        "created_at": task.get("created_at"),
        "started_at": task.get("started_at"),
        "finished_at": task.get("finished_at"),
        "live_in_process": bool(live),
        "events_seen": len((live or {}).get("events") or []),
    }


def cancel_job(job_id: str) -> Dict[str, Any]:
    """取消一个在跑的 job（DSH 的 `ctx.jobs` 取消会落到这里）。"""
    t = _JOB_TASKS.get(job_id)
    if t is None:
        return {"job_id": job_id, "cancelled": False,
                "message": "这个 job 不在本进程里（已完成 / 已重启），无需取消"}
    t.cancel()
    return {"job_id": job_id, "cancelled": True,
            "message": "已发出取消信号；真正停下由当前这一步的下一处 await 生效"}


# ══════════════════════════════════════════════════════════════════
# S3 · SSE
# ══════════════════════════════════════════════════════════════════

def sse_format(ev: Dict[str, Any]) -> str:
    """一条事件 → SSE 文本（带 event 名 + data JSON）。"""
    name = str(ev.get("event") or "message")
    body = {k: v for k, v in ev.items() if k != "event"}
    try:
        payload = json.dumps(body, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        payload = json.dumps({"error": "serialize failed"}, ensure_ascii=False)
    return f"event: {name}\ndata: {payload}\n\n"


async def job_events(db, job_id: str) -> AsyncIterator[Dict[str, Any]]:
    """把 job 的进度当事件流吐出来。

    设计要点（为什么不是"每 0.4s 查一次 DB 再推"）：
      · 事件环（`_JOB_STATE[job]["events"]`）**先回放再实时** —— 晚订阅的人
        也能拿到前几条（否则瞬间完成的阶段，订阅者只看到一条 done）；
      · 进程重启后事件环为空 → 用 tasks 行合成快照（`_synth_events_from_db`）；
      · 终态时把环里剩下的吐完就结束，不吊着连接。
    """
    task = db.get_task(job_id)
    if not task:
        yield {"event": "error", "message": f"job 不存在：{job_id}"}
        return
    pl = _task_payload(task)
    stage = str(pl.get("stage") or "")

    # 先发一条 hello（带 job 元信息），再回放事件环
    yield {"event": "hello", "job_id": job_id,
           "project_id": pl.get("project_id"), "stage": stage,
           "poll": f"/api/v2/jobs/{job_id}"}

    sent = 0
    last_beat = time.time()
    while True:
        ring = (_JOB_STATE.get(job_id) or {}).get("events") or []
        if not ring and sent == 0:
            # 没有环（重启/老 job）→ 用 DB 合成
            for e in _synth_events_from_db(db, job_id, db.get_task(job_id) or task):
                sent += 1
                yield e
        while sent < len(ring):
            ev = ring[sent]
            sent += 1
            yield ev

        # 终态判断：环里有 done，或 tasks 行已终态且环已吐完
        has_done = any(e.get("event") == "done" for e in ring)
        cur = db.get_task(job_id) or {}
        status = str(cur.get("status") or "")
        if has_done or (status in ("completed", "failed") and sent >= len(ring)):
            # 若环里没有 done（重启场景），补一条
            if not has_done:
                yield {"event": "done", "job_id": job_id, "stage": stage,
                       "ok": status == "completed", "result": cur.get("result")}
            return

        now = time.time()
        if now - last_beat >= HEARTBEAT_INTERVAL:
            last_beat = now
            yield {"event": "ping", "ts": now,
                   "status": status, "sent": sent}
        await asyncio.sleep(POLL_INTERVAL)
