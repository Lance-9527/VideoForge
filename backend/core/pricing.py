# -*- coding: utf-8 -*-
"""视频生成单价估算（人民币）。

═══════════════════════════════════════════════════════════════════
为什么必须有这个
═══════════════════════════════════════════════════════════════════
2026-10-19 真实事故：agent 跑一次完整流程（2 个镜头），
**没有任何预算刹车**，用户被扣了近 100 元，而且那些视频对用户毫无价值。

VideoForge 的设置里一直有 `default_video_budget_yuan`（默认 30），
但它**从来没有接进生成链路** —— 只是个摆设。

现在有了这张表 + core/budget.py，生成前会：
  1. 用本表估算本次成本
  2. 加上项目已累计成本
  3. 超过预算 → **直接拒绝生成**并说明差多少

═══════════════════════════════════════════════════════════════════
价格是**估算值**
═══════════════════════════════════════════════════════════════════
各平台按"灵感值/秒/次"计费，价格随时会变。这里给的是**保守上限**
（宁可高估导致提前刹车，也不要低估导致超支）。

标注说明：
  · 「元/秒」按生成时长计费
  · 「元/次」按调用次数计费
  · 数字越大越保守（宁可少生成，也不要超预算）
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# provider -> 每秒单价（元/秒）。None 表示该 provider 走"按次计费"。
PER_SECOND_YUAN: Dict[str, float] = {
    # 豆包 Seedance 2.0（火山方舟官方 / token6688 网关）
    # 网关不同通道价格不同：便宜的 ~0.42，贵的 ~1.3。这里取保守值。
    "seedance": 1.30,
    "seedance_t6": 1.30,
    # 可灵 3.0（官方 MCP，按"灵感值"计费；灵感值 ≈ 0.5 元/条）
    "kling_mcp": 1.60,
    # 可灵直连
    "kling": 1.60,
    # 海螺 MiniMax
    "hailuo": 0.90,
    # MiniMax H3 系列
    "minimax": 1.20,
    # Runway（按秒最贵）
    "runway": 4.00,
    "sora": 4.00,
    "luma": 3.00,
    "pika": 1.50,
    "cogvideox": 1.20,
    "jimeng": 0.80,
    "wanx": 0.60,
}

# 每次调用固定费用（元/次）
PER_CALL_YUAN: Dict[str, float] = {}

# 完全没有价格信息的 provider —— 按最贵的算（宁可保守）
UNKNOWN_PROVIDER_YUAN_PER_SECOND = 4.00

# 单次生成的最大估算成本（元）。超过这个数说明参数异常，直接拒绝。
MAX_SINGLE_CALL_YUAN = 60.0


def estimate_cost_yuan(
    provider: str,
    duration_seconds: int = 5,
    resolution: str = "720p",
    num_candidates: int = 1,
) -> float:
    """估算一次视频生成的花费（元）。

    Args:
        provider: provider 名
        duration_seconds: 生成时长
        resolution: 分辨率（越高越贵，这里按档位加成）
        num_candidates: 候选数（每个候选都单独计费）
    """
    try:
        dur = max(1, int(duration_seconds or 5))
    except Exception:
        dur = 5
    try:
        n = max(1, int(num_candidates or 1))
    except Exception:
        n = 1

    p = (provider or "").strip().lower()
    per_sec = PER_SECOND_YUAN.get(p)
    if per_sec is None:
        per_sec = UNKNOWN_PROVIDER_YUAN_PER_SECOND

    # 分辨率加成（1080p 大约贵 40%，720p 是基准）
    r = (resolution or "720p").lower()
    if r in ("1080p", "1080"):
        per_sec *= 1.4
    elif r in ("480p", "480"):
        per_sec *= 0.8

    per_call = PER_CALL_YUAN.get(p, 0.0)
    total = per_sec * dur + per_call
    return round(total * n, 2)


def describe_cost(provider: str, duration_seconds: int = 5,
                  resolution: str = "720p", num_candidates: int = 1) -> str:
    """给人看的一句话成本说明。"""
    c = estimate_cost_yuan(provider, duration_seconds, resolution, num_candidates)
    return f"约 {c:.2f} 元/次（{provider} {duration_seconds}s {resolution} × {num_candidates} 候选，估算）"
