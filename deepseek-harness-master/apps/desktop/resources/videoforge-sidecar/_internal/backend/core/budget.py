# -*- coding: utf-8 -*-
"""生成预算刹车（成本累计 + 超预算拒绝）。

═══════════════════════════════════════════════════════════════════
为什么必须有这个
═══════════════════════════════════════════════════════════════════
VideoForge 的设置里一直有 `default_video_budget_yuan` 和
`allow_video_spend`，但 **dev 产品里没有任何代码读它们** ——
预算闸等于不存在，生成一次真视频可能被扣几十上百元。

这个模块把预算真正接进生成链路：
  · `check_budget()`    —— 生成**前**检查，超了直接拒绝（抛 BudgetExceeded）
  · `add_cost()`        —— 生成后累加项目成本
  · `budget_status()`   —— 给前端/对话显示"已用 / 剩余"
  · `spend_enabled()`   —— 用户级开关 + 紧急停止

存储：settings 里 `project_cost_yuan_<project_id>`（沿用打包库既有字段名）。

═══════════════════════════════════════════════════════════════════
与开源子集 `budget.py` 的差异（有意为之）
═══════════════════════════════════════════════════════════════════
开源子集要求「环境变量 VIDEOFORGE_ALLOW_VIDEO_SPEND=1 **且** 设置
allow_video_spend=true」双开才放行。那对**桌面产品**不合适：用户装完
点「生成」毫无反应，只会以为软件坏了。

本版改为：
  1. **紧急停止**：环境变量 `VIDEOFORGE_DISABLE_SPEND=1` → 一律禁止（运维级）
  2. **用户开关**：settings.allow_video_spend 明确为 false/0 → 禁止；
     缺省（未设置）→ **允许**，因为真正的保护是下面的预算闸
  3. **预算闸**（主保护）：budget <= 0 → 一律拒；每次估算 > 剩余 → 拒
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger("videoforge.budget")

# 紧急停止（运维级）：设成 1 就彻底不花钱
DISABLE_SPEND_ENV_VAR = "VIDEOFORGE_DISABLE_SPEND"


class BudgetExceeded(Exception):
    """超出预算 / 不允许花钱。调用方应当直接拒绝生成并把原因告诉用户。"""


def _setting_key(project_id: str) -> str:
    return f"project_cost_yuan_{project_id}"


def _get_db():
    try:
        from main import get_app_state
        return get_app_state().get("db")
    except Exception:
        return None


def get_budget_limit(db=None) -> float:
    """项目预算上限（元）。0 或负数表示**未设置**（会被拒绝，不是不限）。"""
    db = db or _get_db()
    if db is None:
        return 0.0
    try:
        s = db.get_all_settings() or {}
        return float(s.get("default_video_budget_yuan") or 0)
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
    """累加项目成本，返回累计值。金额 <= 0 时只读取不写。"""
    db = db or _get_db()
    if db is None or not project_id:
        return 0.0
    try:
        amt = float(amount_yuan or 0)
        if amt <= 0:
            return get_spent(db, project_id)
        cur = get_spent(db, project_id)
        new = round(cur + amt, 2)
        if hasattr(db, "set_setting"):
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
        BudgetExceeded —— 预算未设置或不够时抛出（调用方必须捕获并拒绝生成）
    """
    db = db or _get_db()
    limit = get_budget_limit(db)
    spent = get_spent(db, project_id)
    est = float(estimated_yuan or 0)

    if limit <= 0:
        raise BudgetExceeded(
            f"项目预算未设置（当前上限 {limit:.2f} 元），已阻止生成。"
            f"请在「设置」里把「单项目视频预算」设成大于 0 的值再试。"
        )

    remaining = round(limit - spent, 2)
    if est > remaining:
        msg = (
            f"预算不足：本次预计 {est:.2f} 元，项目预算 {limit:.2f} 元，"
            f"已用 {spent:.2f} 元，只剩 {remaining:.2f} 元。"
        )
        if provider:
            msg += f"（provider：{provider}）"
        if detail:
            msg += detail
        msg += "　请调高预算，或减少镜头数 / 时长 / 候选数后重试。"
        raise BudgetExceeded(msg)

    return {
        "ok": True, "spent": spent, "limit": limit, "estimate": est,
        "remaining": remaining,
        "message": f"本次预计 {est:.2f} 元，预算剩余 {remaining:.2f} 元",
    }


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
# 用户级开关 + 紧急停止
# ════════════════════════════════════════════════════════════════════

def spend_enabled(db=None) -> tuple:
    """是否允许花钱。

    Returns:
        (enabled: bool, reason: str) —— reason 说明为什么没开（给用户看）
    """
    # 1. 紧急停止优先
    if os.environ.get(DISABLE_SPEND_ENV_VAR, "").strip().lower() in (
            "1", "true", "yes", "on"):
        return False, f"环境变量 {DISABLE_SPEND_ENV_VAR}=1（运维紧急停止）"

    # 2. 用户开关：只有**明确设为假**才拦；未设置视为允许
    db = db or _get_db()
    if db is not None:
        try:
            flag = (db.get_all_settings() or {}).get("allow_video_spend")
            if flag is not None and str(flag).strip().lower() in (
                    "0", "false", "no", "off", ""):
                return False, "设置里的「允许视频生成花费」被关掉了"
        except Exception:
            pass
    return True, ""


def assert_spend_allowed(db=None, provider: str = "", detail: str = "") -> None:
    """开关检查。不放行就抛 BudgetExceeded（调用方拒绝生成）。"""
    ok, reason = spend_enabled(db)
    if ok:
        return
    msg = f"视频生成已被安全闸拦住：{reason}。确认要花钱再打开即可。"
    if provider:
        msg += f"（当前 provider：{provider}）"
    if detail:
        msg += detail
    raise BudgetExceeded(msg)
