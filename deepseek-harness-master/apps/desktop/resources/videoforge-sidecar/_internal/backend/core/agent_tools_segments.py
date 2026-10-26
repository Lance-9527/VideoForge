"""
VideoForge · Agent 工具（按 L2 分段时间轴出片）

════════════════════════════════════════════════════════════════════════
为什么有这一批
════════════════════════════════════════════════════════════════════════
之前出片是「合成首帧 → image_to_video」一条龙。结果：

    剧本 L2 时间轴写「0–4s 空镜（人未入画）」，
    但合成首帧里已经有人 → 可灵强跟随 → 视频从第 0 秒就有角色。

这是流程性 bug，不是模型 bug：图生视频的首帧就是视频内容。
要还原剧本节奏，**必须按 L2 拆段，每段的首帧按本段动作的开始状态选**。

`produce_shot_segments` 把这件事固化下来：
    · 读 L2 拆成 N 段
    · 按段动作分类（empty/appear/active/passive）
    · 每段用对应的首帧策略：
          - 空镜 → 纯场景图
          - 人物登场/侧卧 → lying 合成首帧
          - 主动动作 → active 合成首帧（必要时用 kling-image-v3_0_omni 现做）
    · 段段出 → ffmpeg concat → 抽帧验证
    · **dry_run**：灵感值不够时仍可跑通链路，输出方案 + 估算费用

════════════════════════════════════════════════════════════════════════
工具清单
════════════════════════════════════════════════════════════════════════
    produce_shot_segments     按 L2 分段时间轴出片（可花可不花，dry_run 优先）
    segment_dry_plan          只看方案，不花钱（同 produce_shot_segments(dry_run=True)）
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

from .agent_tools import REGISTRY, ToolResult, err, ok, _services, _need

logger = logging.getLogger("videoforge.agent_tools.segments")


def _SS():
    from core import shot_segments as SS
    return SS


@REGISTRY.register(
    "produce_shot_segments",
    "★ 按 L2 分段时间轴出片（取代「合成首帧 → image_to_video 一条龙」）。\n"
    "读 L2 → 按段分类（empty / appear / active / passive）→ 每段用对应首帧"
    "（空镜用纯场景图、人物登场用 lying 合成首帧、主动动作用 active 合成首帧）"
    "→ 段段出 → ffmpeg concat → 抽帧验证。\n"
    "★ 修复「首帧决定视频」导致的**剧本节奏失真**：剧本 L2 写「0–4s 空镜」时，"
    "这一段真的就是空镜，**人物不会从第 0 秒就在画里**。\n"
    "★ dry_run=true **不花任何灵感值**，只算方案（灵感值够再 dry_run=false 真跑）。",
    {"shot_id": {"type": "string", "desc": "分镜 id", "required": True},
     "model": {"type": "string", "desc": "可灵视频模型，默认 kling-video-v3_0"},
     "target_resolution": {"type": "string", "desc": "最终拼成的分辨率",
                           "enum": ["480p", "720p", "1080p"]},
     "dry_run": {"type": "boolean", "desc": "true = 只出方案，不真调模型（推荐先用）"},
     "upload_only": {"type": "boolean", "desc": "true = 只合成首帧，不调视频模型"},
     "use_existing_first_frame": {"type": "boolean",
                                  "desc": "true = 优先用 ctx 里现成的合成首帧；"
                                          "false = 强制为每段现做新首帧"},
     "force": {"type": "boolean",
               "desc": "★ 防重复烧钱开关：默认 false 走去重闸门（merged_video 60 分钟内刚出过就跳过）。"
                       "明确要重出才传 true"},
     "dedup_window_minutes": {"type": "integer",
                                "desc": "去重窗口（分钟），默认 60"},
     "budget_yuan": {"type": "number",
                     "desc": "★★ 花钱闸：真出片（dry_run=false）时必须传一个 > 0 的预算上限（元），"
                             "且必须 ≥ 本镜估算总额，否则拒跑。例：预算 15 元就传 15。"
                             "默认 0 = 拒绝真跑（只允许 dry-run）。"}},
    writes=True,
)
async def t_produce_shot_segments(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "shot_id")
    if bad:
        return bad
    try:
        SS = _SS()
        db, _, _ = _services()
        r = await SS.produce_shot_segments(
            db, args["shot_id"],
            model=args.get("model") or "kling-video-v3_0",
            target_resolution=args.get("target_resolution") or "480p",
            dry_run=bool(args.get("dry_run")),
            upload_only=bool(args.get("upload_only")),
            use_existing_first_frame=bool(args.get("use_existing_first_frame", True)),
            force=bool(args.get("force")),
            dedup_window_minutes=int(args.get("dedup_window_minutes") or 60),
            budget_yuan=float(args.get("budget_yuan") or 0.0),
        )
        if r.get("ok") is False:
            return err(r.get("error") or "出片失败",
                       detail=r.get("failures") or r.get("note"),
                       merged_video=r.get("merged_video"))
        # 把 plan 简化输出（过长时只给摘要）
        out: Dict[str, Any] = {
            "ok": True,
            "shot_id": args["shot_id"],
            "dry_run": bool(args.get("dry_run")),
            "totals": r.get("totals"),
            "segments": r.get("segments") or [],
            "failures": r.get("failures") or [],
            "merged_video": (r.get("merged_video") or {}).get("path") or "",
            "verify": r.get("verify") or {},
            "log": r.get("log") or [],
            "note": r.get("note") or "",
        }
        # 把 plan 每段的 first/tail 路径也报出来（UI 要直接显示「这一段用了什么首帧」）
        plans = r.get("plan") or []
        if plans:
            out["plan_summary"] = [
                {"i": p.get("index"),
                 "kind": p.get("kind"),
                 "start": p.get("start"), "end": p.get("end"),
                 "duration": p.get("duration"),
                 "first_image": p.get("first_image"),
                 "needs_synthesis": p.get("needs_synthesis"),
                 "note": p.get("note")}
                for p in plans
            ]
        return ok(**out)
    except Exception as e:
        logger.exception("produce_shot_segments failed")
        return err(f"按段出片失败：{type(e).__name__}: {e}")


@REGISTRY.register(
    "segment_dry_plan",
    "只看 L2 拆段方案（不花任何灵感值）。同 produce_shot_segments(dry_run=true)。"
    "★ 灵感值不够 / 想先看清楚怎么出片的时候先用这个。",
    {"shot_id": {"type": "string", "desc": "分镜 id", "required": True}},
)
async def t_segment_dry_plan(args: Dict[str, Any]) -> ToolResult:
    bad = _need(args, "shot_id")
    if bad:
        return bad
    try:
        SS = _SS()
        db, _, _ = _services()
        r = await SS.produce_shot_segments(db, args["shot_id"], dry_run=True)
        return ok(
            shot_id=args["shot_id"],
            n_segments=(r.get("totals") or {}).get("n_segments"),
            seconds=(r.get("totals") or {}).get("seconds"),
            cost_estimate=(r.get("totals") or {}).get("cost_estimate"),
            needs_synthesis=(r.get("totals") or {}).get("needs_synthesis") or [],
            plan_summary=[
                {"i": p.get("index"), "kind": p.get("kind"),
                 "start": p.get("start"), "end": p.get("end"),
                 "duration": p.get("duration"),
                 "first_image": p.get("first_image"),
                 "needs_synthesis": p.get("needs_synthesis"),
                 "note": p.get("note"),
                 "synthesis_prompt": p.get("synthesis_prompt") or ""}
                for p in (r.get("plan") or [])
            ],
            note=(f"已拆 {r.get('totals', {}).get('n_segments', 0)} 段 / "
                  f"{r.get('totals', {}).get('seconds', 0):.1f}s / "
                  f"约 {r.get('totals', {}).get('cost_estimate', 0):.0f} 灵感值。"
                  "★ 这是方案，**没有花任何灵感值**。要真跑 → "
                  "produce_shot_segments(shot_id=..., dry_run=false)"),
        )
    except Exception as e:
        logger.exception("segment_dry_plan failed")
        return err(f"看拆段方案失败：{type(e).__name__}: {e}")