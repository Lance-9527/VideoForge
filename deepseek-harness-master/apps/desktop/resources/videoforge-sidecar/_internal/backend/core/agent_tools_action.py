# -*- coding: utf-8 -*-
"""
动作序列设计的 agent 工具
=========================

把 `core.action_sequence`（吸收 script-to-shot-engine v2.3.2）暴露给 agent：

  · `design_action_sequence`   把一场戏拆成高密度短镜头序列（会调 LLM，**不花钱**）
  · `review_action_sequence`   只校验一份已有序列（**不调模型、零成本**）

★ 为什么值得做成工具
  用户的原话是「出的视频没人类爱看 —— 动作顺序不符合人类的观赏习惯」。
  根因是镜头密度不足（一个 15 秒长镜 vs 人类习惯的 10+ 个短镜）。
  这件事必须在**写分镜的时候**就做对，而不是等到出片。
  所以它是「剧本 → 分镜」之间的一个独立步骤，agent 应该主动跑它。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from .agent_tools import REGISTRY, err, ok, _services

logger = logging.getLogger("videoforge.action_tools")


def _make_llm(settings: Dict[str, Any]):
    try:
        from main import _make_llm as _f  # type: ignore
        return _f(settings)
    except Exception:  # noqa: BLE001
        from core.llm import LLMClient  # type: ignore
        from core.chat import resolve_llm_settings  # type: ignore
        r = resolve_llm_settings(settings)
        return LLMClient(provider=r["provider"], api_key=r["api_key"], model=r["model"])


@REGISTRY.register(
    "design_action_sequence",
    "把一场戏（打斗/对峙）拆成**高密度短镜头动作序列**，输出按时间顺序排列的镜头表。\n"
    "★ 用途：解决「出的视频没人类爱看」—— 人类观赏习惯要求 15 秒切 10+ 次镜头，\n"
    "  而不是一个 13 秒的连续长镜。这个工具负责在**写分镜阶段**就把节奏设计对。\n"
    "输出含：每镜的起止时间 / 光圈 / 焦段 / 景别 / 动作与结果，"
    "以及可直接用于白膜渲染的逐镜机位。\n"
    "· `scene_text`：本场戏的原文（动作、台词、人物）。也可以传 `shot_id` 让我从库里读。\n"
    "· `total_seconds`：本段时长（默认 15 秒，与出片模型单段上限一致）。\n"
    "· `mode`：action（动作）/ dialogue（对峙）；不传则按内容自动判断。\n"
    "**内部会机器校验镜头数与时间轴，不合格会自动带上错误重写**，最多 3 轮。",
    {"scene_text": {"type": "string", "desc": "本场戏原文", "required": False},
     "shot_id": {"type": "string", "desc": "也可传分镜 id，从库里读它的内容", "required": False},
     "total_seconds": {"type": "string", "desc": "本段时长，默认 15", "required": False},
     "mode": {"type": "string", "desc": "action / dialogue，不传自动判断", "required": False}},
)
async def t_design_action_sequence(args: Dict[str, Any]) -> Any:
    scene = str(args.get("scene_text") or "").strip()
    shot_id = str(args.get("shot_id") or "").strip()
    if not scene and shot_id:
        try:
            db, _, _ = _services()
            sh = db.get_shot(shot_id)
            if sh:
                import json as _j
                parts = [str(sh.get("title") or "")]
                for k in ("layer1_overview", "description", "summary"):
                    if sh.get(k):
                        parts.append(str(sh[k]))
                l2 = sh.get("layer2_timeline")
                if l2:
                    parts.append(_j.dumps(l2, ensure_ascii=False) if not isinstance(l2, str) else l2)
                scene = "\n".join(p for p in parts if p).strip()
        except Exception as e:  # noqa: BLE001
            logger.warning("读分镜失败：%s", e)
    if not scene:
        return err("需要 scene_text（本场原文）或有效的 shot_id。")

    try:
        total = float(args.get("total_seconds") or 15)
    except Exception:  # noqa: BLE001
        total = 15.0
    mode = str(args.get("mode") or "").strip().lower() or None
    if mode not in (None, "action", "dialogue"):
        mode = None

    from . import action_sequence as AS
    db, settings, _ = _services()
    llm = _make_llm(settings)

    r = await AS.design_sequence(llm, scene, total, mode)
    if not r.get("ok"):
        return err(
            f"动作序列没做过校验（{r.get('rounds')} 轮）。\n"
            + "\n".join(f"· {e}" for e in (r.get("errors") or ["未知原因"]))
            + "\n\n这通常说明模型一直写成少数长镜。可以：\n"
              "  ① 把本场内容写得更具体（谁对谁、空间关系、发生了什么）；\n"
              "  ② 或减小 total_seconds 让每镜容量更紧。",
            mode=r.get("mode"), rounds=r.get("rounds"),
            shots=len((r.get("parsed") or {}).get("shots") or []))

    return ok(mode=r["mode"], rounds=r["rounds"],
              shots=len(r["parsed"]["shots"]),
              stats=r["validation"]["stats"],
              warnings=r["validation"]["warnings"],
              timeline=r["timeline"],
              raw_text=r["text"],
              previz_shots=r["previz_shots"],
              note=f"已通过校验：{len(r['parsed']['shots'])} 个镜头，"
                   f"平均 {r['validation']['stats']['avg_sec']} 秒/镜。"
                   f"把 timeline 里的镜头表作为分镜依据；"
                   f"previz_shots 可直接用于白膜渲染。")


@REGISTRY.register(
    "review_action_sequence",
    "只**校验**一份已有的动作序列（**不调模型、零成本**），看它够不够「人类爱看」。\n"
    "检查：镜头数是否达标、时间轴是否首尾连续、每镜是否都有光圈焦段和可见动作、"
    "单镜是否过长。用于在出片前把关。",
    {"sequence_text": {"type": "string", "desc": "序列正文（镜头一（0-1.2秒）：… 这种格式）",
                       "required": True},
     "total_seconds": {"type": "string", "desc": "总时长，默认 15", "required": False},
     "mode": {"type": "string", "desc": "action / dialogue，不传自动判断", "required": False}},
)
async def t_review_action_sequence(args: Dict[str, Any]) -> Any:
    text = str(args.get("sequence_text") or "").strip()
    if not text:
        return err("需要 sequence_text。")
    try:
        total = float(args.get("total_seconds") or 15)
    except Exception:  # noqa: BLE001
        total = 15.0
    from . import action_sequence as AS
    mode = str(args.get("mode") or "").strip().lower() or AS.infer_mode(text)
    parsed = AS.parse_shots(text)
    if not parsed["shots"]:
        return err("没解析出任何镜头行。格式必须是 `镜头一（0-1.2秒）：光圈 f/2.8（…），"
                   "焦段 80mm（近景），…`。")
    v = AS.validate(parsed, total, mode)
    return ok(ok=v["ok"], mode=mode, stats=v["stats"],
              errors=v["errors"], warnings=v["warnings"],
              timeline=AS.render_timeline(parsed, total, mode),
              note="✓ 合格" if v["ok"] else f"✗ {len(v['errors'])} 项不合格")
