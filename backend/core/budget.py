# -*- coding: utf-8 -*-
"""生成预算刹车（成本累计 + 超预算拒绝）。

═══════════════════════════════════════════════════════════════════
为什么必须有这个
═══════════════════════════════════════════════════════════════════
VideoForge 的设置里一直有 `default_video_budget_yuan`（默认 30 元），
但它**从来没有被接进生成链路**。2026-10-19 真实事故：agent 跑一次完整
流程，用户被扣了近 100 元，视频对用户毫无价值。

这个模块把预算真正落地：
  · `add_cost()`      —— 生成后累加项目成本
  · `check_budget()`  —— 生成**前**检查，超了就直接拒绝
  · `budget_status()` —— 给前端/对话显示"已用 / 剩余"

存储：settings 里 `project_cost_yuan_<project_id>`（沿用已有字段名）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("videoforge.budget")


class BudgetExceeded(Exception):
    """超出预算。调用方应当直接拒绝生成并把原因告诉用户。"""


def _setting_key(project_id: str) -> str:
    return f"project_cost_yuan_{project_id}"


def _get_db():
    try:
        from main import get_app_state
        return get_app_state().get("db")
    except Exception:
        return None


def get_budget_limit(db=None) -> float:
    """项目预算上限（元）。0 或负数表示不限。"""
    db = db or _get_db()
    if db is None:
        return 0.0
    try:
        s = db.get_all_settings() or {}
        v = s.get("default_video_budget_yuan")
        return float(v or 0)
    except Exception:
        return 0.0


def get_spent(db=None, project_id: str = "") -> float:
    """项目已花（元）。"""
    db = db or _get_db()
    if db is None or not project_id:
        return 0.0
    try:
        s = db.get_all_settings() or {}
        return float(s.get(_setting_key(project_id)) or 0)
    except Exception:
        return 0.0


def add_cost(project_id: str, amount_yuan: float, db=None) -> float:
    """累加项目成本，返回累计值。"""
    db = db or _get_db()
    if db is None or not project_id:
        return 0.0
    try:
        amt = float(amount_yuan or 0)
        if amt <= 0:
            return get_spent(db, project_id)
        cur = get_spent(db, project_id)
        new = round(cur + amt, 2)
        db.set_setting(_setting_key(project_id), new)
        logger.info("预算累计: project=%s +%.2f = %.2f 元", project_id, amt, new)
        return new
    except Exception as e:
        logger.warning("成本累计失败: %s", e)
        return get_spent(db, project_id)


def check_budget(
    project_id: str,
    estimated_yuan: float,
    db=None,
    provider: str = "",
    detail: str = "",
) -> Dict[str, Any]:
    """生成前检查预算。

    Returns:
        {"ok": bool, "spent": float, "limit": float, "estimate": float,
         "remaining": float, "message": str}

    Raises:
        BudgetExceeded —— 预算不够时抛出（调用方必须捕获并拒绝生成）
    """
    db = db or _get_db()
    limit = get_budget_limit(db)
    spent = get_spent(db, project_id)
    est = float(estimated_yuan or 0)

    if limit <= 0:
        # 不限预算
        return {"ok": True, "spent": spent, "limit": 0.0, "estimate": est,
                "remaining": -1.0, "message": ""}

    remaining = round(limit - spent, 2)
    if est > remaining:
        msg = (
            f"预算不足：本次预计花费 {est:.2f} 元，项目预算 {limit:.2f} 元，"
            f"已用 {spent:.2f} 元，只剩 {remaining:.2f} 元。"
        )
        if detail:
            msg += detail
        msg += "请在「设置」里调高预算，或减少镜头数/时长/候选数后重试。"
        raise BudgetExceeded(msg)

    return {"ok": True, "spent": spent, "limit": limit, "estimate": est,
            "remaining": remaining,
            "message": f"本次预计 {est:.2f} 元，预算剩余 {remaining:.2f} 元"}


def budget_status(project_id: str, db=None) -> Dict[str, Any]:
    """给 UI / 对话显示的预算状态。"""
    db = db or _get_db()
    limit = get_budget_limit(db)
    spent = get_spent(db, project_id)
    remaining = round(limit - spent, 2) if limit > 0 else -1.0
    return {
        "spent_yuan": spent,
        "limit_yuan": limit,
        "remaining_yuan": remaining,
        "unlimited": limit <= 0,
    }


# ════════════════════════════════════════════════════════════════════
# 总闸（运维级）：默认**关**
# ════════════════════════════════════════════════════════════════════
# 2026-10-19 事故复盘：用户被扣了近 100 元，因为
#   1) orchestrator.generate_shot_handler **绕过了** dev 版本里的三层花钱闸
#      （那三层只在 core/shot_segments.py 里，主流程不走那条路）
#   2) default_video_budget_yuan 从来没接进生成链路
#
# 现在：**所有**真实视频生成都必须同时满足三个条件才放行 ——
#   1. 环境变量 VIDEOFORGE_ALLOW_VIDEO_SPEND=1（总闸，运维级，默认关）
#   2. 设置里 allow_video_spend = true（用户级，默认关）
#   3. 项目预算够用（core/budget.check_budget）
#
# 也就是说：**默认情况下 VideoForge 一分钱都不会花。**

SPEND_ENV_VAR = "VIDEOFORGE_ALLOW_VIDEO_SPEND"


def spend_enabled(db=None) -> bool:
    """总闸：环境变量 + 设置开关，两者都要开。

    Returns:
        (enabled: bool, reason: str) —— reason 说明为什么没开（给用户看）
    """
    import os
    env_ok = (os.environ.get(SPEND_ENV_VAR, "").strip().lower()
              in ("1", "true", "yes", "on"))
    db = db or _get_db()
    setting_ok = False
    if db is not None:
        try:
            flag = (db.get_all_settings() or {}).get("allow_video_spend")
            setting_ok = str(flag).strip().lower() in ("1", "true", "yes", "on")
        except Exception:
            setting_ok = False
    if env_ok and setting_ok:
        return True, ""
    missing = []
    if not env_ok:
        missing.append(f"环境变量 {SPEND_ENV_VAR}=1")
    if not setting_ok:
        missing.append("设置里的 allow_video_spend")
    return False, "、".join(missing)


def assert_spend_allowed(db=None, provider: str = "",
                          detail: str = "") -> None:
    """总闸检查。不放行就抛 BudgetExceeded（调用方拒绝生成）。"""
    ok, reason = spend_enabled(db)
    if ok:
        return
    msg = (f"视频生成已被安全闸拦住（默认禁止花钱）：需要同时打开 {reason}。"
           f"这是为了避免意外扣费 —— 确认要花钱再打开即可。")
    if provider:
        msg += f"（当前 provider：{provider}）"
    if detail:
        msg += detail
    raise BudgetExceeded(msg)
