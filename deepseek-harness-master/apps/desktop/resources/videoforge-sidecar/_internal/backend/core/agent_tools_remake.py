# -*- coding: utf-8 -*-
"""VideoForge · 「视频复刻」功能的 Agent 工具

═══════════════════════════════════════════════════════════════════
这个功能点是什么
═══════════════════════════════════════════════════════════════════
用户给一段**参考视频**，产出两份东西：

  ① **复刻规格**（remake_spec.json）—— 切好的镜头表 + 每镜的
     景别/运镜/色调/构图/字幕版式 + 落在该镜里的台词 + 节奏指标。
     这是「照单施工」的施工图。
  ② **台词体检** —— 把规格里的台词交给 `core/dialogue_natural.py`，
     报出 AI 味问题（AI 味太重的台词直接不合格）。

用户原话（2026-10-24）：「再研究下如何复刻视频，研究明白后做成一个单独的功能点
放到 VideoForge 里面」。

═══════════════════════════════════════════════════════════════════
工具清单
═══════════════════════════════════════════════════════════════════
  · `remake_analyze_video`  —— 参考视频 → 复刻规格（本地 CPU，ASR/多模态可选花钱）
  · `remake_transcribe`      —— 视频/音频 → 台词稿（硅基流动 ASR + 本地 whisper 时间戳）
  · `remake_audit_dialogue`  —— 台词 AI 味体检（纯本地、免费）

═══════════════════════════════════════════════════════════════════
花钱纪律
═══════════════════════════════════════════════════════════════════
默认 `dry_run=true` 只报「大概多少钱/多久」，不真调；
真跑要 `dry_run=false`，此时走 `core.budget.assert_spend_allowed()`（用户级开关
+ 紧急停止），并用 `core.budget.add_cost()` 记账。

═══════════════════════════════════════════════════════════════════
合规边界（写进返回结果，不只是写进文档）
═══════════════════════════════════════════════════════════════════
复刻的是**手法**（节奏/景别序列/运镜类型/色调/构图/台词密度），
不是内容（原片角色肖像、逐字台词、具体美术设计、音乐素材、水印）。
依据：17 U.S.C. §102；美国版权局 Compendium §310.2「版权局不看风格」、
§310.4「look and feel 不受保护」。非法律意见。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from .agent_tools import REGISTRY, ToolResult, err, merged, ok

logger = logging.getLogger("videoforge.agent.tools.remake")


def _services():
    from .agent_tools import _services as s
    return s()


def _resolve_video(args: Dict[str, Any], db=None) -> str:
    """定位参考视频：本地路径优先，其次项目里的成片。"""
    p = str(args.get("video_path") or args.get("path") or "").strip()
    if p:
        if os.path.isfile(p):
            return p
        # 相对 data_dir 试一次
        try:
            from .data_root import resolve_data_root
            cand = os.path.join(resolve_data_root(), p)
            if os.path.isfile(cand):
                return cand
        except Exception:  # noqa: BLE001
            pass
        return p
    pid = str(args.get("project_id") or "").strip()
    if pid and db is not None:
        try:
            outs = db.list_outputs(pid) or []
            for o in outs:
                for k in ("final_path", "video_path", "path", "file_path"):
                    v = str((o or {}).get(k) or "").strip()
                    if v and os.path.isfile(v):
                        return v
        except Exception:  # noqa: BLE001
            pass
    return ""


@REGISTRY.register(
    "remake_analyze_video",
    "**视频复刻**：把一段参考视频拆成「可照着做的施工图」——"
    "镜头表（每镜的起止/时长）+ 景别 + 运镜 + 色调 + 构图 + 字幕版式 + 落在该镜的台词 + 节奏指标。"
    "产出 remake_spec.json，可直接用来生成新片子（复刻手法，不复制内容）。"
    "★ 默认 dry_run=true：只报大概多少钱/多久/多少镜，不真跑。"
    "真跑（dry_run=false）才做切镜、抽帧、OCR、ASR。",
    {"video_path": {"type": "string",
                    "desc": "参考视频的本地路径（不给则用当前项目的成片）"},
     "project_id": {"type": "string", "desc": "项目 id（用于记账）"},
     "dry_run": {"type": "boolean", "desc": "true=只估算不真跑（默认）"},
     "use_siliconflow": {"type": "boolean",
                         "desc": "是否用硅基流动 ASR 校对台词（花钱，默认 true）"},
     "use_whisper": {"type": "boolean",
                     "desc": "是否用本地 whisper 出带时间戳的台词（免费，默认 true）"},
     "use_ocr": {"type": "boolean", "desc": "是否 OCR 硬字幕（默认 true）"},
     "vision": {"type": "boolean",
                "desc": "是否用多模态模型反推画面描述（花钱，默认 false）"},
     "max_shots": {"type": "number", "desc": "最多分析多少镜（0=全部）"},
     "out_dir": {"type": "string", "desc": "产物目录（默认临时目录）"},
     "style_profile": {"type": "string",
                       "desc": "风格档：auto / life-stream-cinematic / era-texture"}},
    needs_project=False,
)
async def t_remake_analyze_video(args: Dict[str, Any]) -> ToolResult:
    db, settings, _q = _services()
    try:
        from . import video_remake as VR
    except Exception as e:  # noqa: BLE001
        return err("复刻模块没加载上：%s: %s" % (type(e).__name__, e))

    path = _resolve_video(args, db=db)
    if not path:
        return err("找不到参考视频：给 `video_path`，或在有成片的项目里调用。")
    if not os.path.isfile(path):
        return err("参考视频不存在：%s" % path)

    style = str(args.get("style_profile") or "auto").strip() or "auto"
    dry = args.get("dry_run")
    dry_run = True if dry is None else bool(dry)
    use_sf = bool(args.get("use_siliconflow", True))
    use_wh = bool(args.get("use_whisper", True))
    use_ocr = bool(args.get("use_ocr", True))
    vision = bool(args.get("vision", False))
    max_shots = int(args.get("max_shots") or 0)

    pr = VR.probe(path)
    if not pr.get("ok"):
        return err("读不了这个视频：%s" % (pr.get("error") or "未知错误"))
    dur = float(pr.get("duration") or 0)

    if dry_run:
        sd = VR.detect_shots(path)
        n = len(sd.get("shots") or [])
        from . import asr_siliconflow as ASR
        est = ASR.estimate(dur)
        est_v = ASR.estimate(dur)
        return ok(dry_run=True, video=path, source=pr,
                  estimated_shots=n,
                  estimated_cost_yuan=round(est.get("est_cost_yuan", 0) +
                                            (est_v.get("est_cost_yuan", 0) if vision else 0), 4),
                  estimated_minutes=round(max(1.0, dur / 60.0 * 0.9), 1),
                  asr_model=est.get("model"),
                  style_profile=style,
                  note=("这是估算。真跑会：切镜 → 每镜抽 3 张关键帧 → 量化色调/景别/运镜/构图 → "
                        "OCR 硬字幕 → ASR 台词（本地 whisper 出时间戳 + 硅基流动校对文本）→ "
                        "产出 remake_spec.json。确认后传 dry_run=false。"))

    try:
        spec = VR.analyze(path, out_dir=str(args.get("out_dir") or ""),
                           project_id=str(args.get("project_id") or ""), db=db,
                           use_siliconflow=use_sf, use_whisper=use_wh,
                           use_ocr=use_ocr, vision=vision, max_shots=max_shots)
    except Exception as e:  # noqa: BLE001
        logger.exception("remake_analyze_video failed")
        return err("复刻分析失败：%s: %s" % (type(e).__name__, e))
    if not spec.get("ok"):
        return err("复刻分析失败：%s" % (spec.get("error") or "未知错误"))

    # 摘要（正文太长，工具结果只给摘要 + 文件路径）
    shots = spec.get("shots") or []
    preview = [{
        "index": s.get("index"), "start": s.get("start"), "end": s.get("end"),
        "duration": s.get("duration"), "shot_size": s.get("shot_size"),
        "camera_motion": s.get("camera_motion"),
        "camera_allowed": s.get("camera_allowed"),
        "palette": (s.get("color") or {}).get("palette"),
        "composition": s.get("composition"),
        "description": s.get("description"),
        "prompt": s.get("prompt"),
        "dialogue": [d.get("text") for d in (s.get("dialogue") or [])][:4],
        "on_screen_text": (s.get("on_screen_text") or [])[:4],
    } for s in shots[:12]]
    return ok(dry_run=False,
              spec_path=spec.get("spec_path"),
              out_dir=spec.get("out_dir"),
              shots=len(shots),
              style_profile=spec.get("style_profile"),
              style_profile_label=spec.get("style_profile_label"),
              pacing=spec.get("pacing"),
              dialogue_stats=spec.get("dialogue_stats"),
              asr=spec.get("asr"),
              audio_peak_at=(spec.get("audio_envelope") or {}).get("peak_at"),
              global_palette=spec.get("global_palette"),
              shots_preview=preview,
              warnings=spec.get("warnings"),
              compliance=spec.get("compliance"),
              note=("规格已落盘。**复刻手法不复制内容**：人物与剧情要重新设计。"
                    "下一步：use_skill('cinematic-film-style') 看质感门，"
                    "use_skill('short-drama-script') 看剧本结构规范。"))


@REGISTRY.register(
    "remake_transcribe",
    "从视频或音频里提取**台词**：本地 whisper 出带时间戳的分句，"
    "硅基流动 ASR 出整段校对文本（SenseVoiceSmall，纯文本无时间戳）。"
    "返回台词行 [{start, end, text}] + 整段文本 + 这次花了多少钱。"
    "★ 台词只用于理解剧情意图，绝不写进画面提示词。",
    {"video_path": {"type": "string", "desc": "视频或音频路径"},
     "project_id": {"type": "string", "desc": "项目 id（用于记账）"},
     "whisper_size": {"type": "string", "desc": "tiny / small / base / medium（默认 small）"},
     "use_siliconflow": {"type": "boolean", "desc": "是否用硅基流动校对（花钱，默认 true）"},
     "dry_run": {"type": "boolean", "desc": "true=只估算（默认）"}},
    needs_project=False,
)
async def t_remake_transcribe(args: Dict[str, Any]) -> ToolResult:
    db, settings, _q = _services()
    try:
        from . import asr_siliconflow as ASR
    except Exception as e:  # noqa: BLE001
        return err("ASR 模块没加载上：%s: %s" % (type(e).__name__, e))

    path = _resolve_video(args, db=db)
    if not path or not os.path.isfile(path):
        return err("找不到要转写的文件：%s" % (path or "(空)"))
    dry = args.get("dry_run")
    dry_run = True if dry is None else bool(dry)
    use_sf = bool(args.get("use_siliconflow", True))
    size = str(args.get("whisper_size") or "small")

    # 视频先抽音轨
    audio = path
    tmp_wav = ""
    if not path.lower().endswith((".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac")):
        try:
            from . import video_remake as VR
            import tempfile
            tmp_wav = os.path.join(tempfile.gettempdir(),
                                   "vf-asr-%d.wav" % int(os.path.getmtime(path)))
            exe = VR._which("ffmpeg")
            code, _o, e = VR._run([exe, "-y", "-loglevel", "error", "-i", path,
                                   "-vn", "-ac", "1", "-ar", "16000",
                                   "-c:a", "pcm_s16le", tmp_wav], timeout=900)
            if code != 0 or not os.path.isfile(tmp_wav):
                return err("抽音轨失败：%s" % (e or "")[:200])
            audio = tmp_wav
        except Exception as e:  # noqa: BLE001
            return err("抽音轨失败：%s: %s" % (type(e).__name__, e))

    try:
        from . import video_remake as VR
        pr = VR.probe(audio) if audio.lower().endswith(".wav") else {"duration": 0}
        dur = float(pr.get("duration") or 0)
        if not dur:
            dur = _audio_seconds(audio)
        if dry_run:
            est = ASR.estimate(dur)
            return ok(dry_run=True, audio=audio, duration=dur,
                      est=est, whisper_model=size,
                      note="估算模式。真跑会调本地 whisper（免费）+ 硅基流动（花钱）。")
        r = ASR.transcribe_timed(audio, whisper_size=size,
                                 use_siliconflow=use_sf,
                                 project_id=str(args.get("project_id") or ""), db=db)
        return ok(dry_run=False, audio=audio,
                  lines=[{"start": s.get("start"), "end": s.get("end"), "text": s.get("text")}
                         for s in (r.get("segments") or [])],
                  full_text=r.get("full_text"),
                  siliconflow_text=r.get("siliconflow_text"),
                  cost_yuan=r.get("cost_yuan"), elapsed=r.get("elapsed"),
                  error=r.get("error"), note=r.get("note"))
    except Exception as e:  # noqa: BLE001
        logger.exception("remake_transcribe failed")
        return err("转写失败：%s: %s" % (type(e).__name__, e))
    finally:
        if tmp_wav and os.path.isfile(tmp_wav):
            try:
                os.remove(tmp_wav)
            except Exception:  # noqa: BLE001
                pass


def _audio_seconds(path: str) -> float:
    try:
        from . import video_remake as VR
        exe = VR._which("ffprobe")
        if not exe:
            return 0.0
        code, out, _e = VR._run([exe, "-v", "error", "-show_entries", "format=duration",
                                 "-of", "csv=p=0", path], timeout=60)
        if code == 0:
            return float(str(out).strip().splitlines()[0])
    except Exception:  # noqa: BLE001
        pass
    return 0.0


@REGISTRY.register(
    "remake_audit_dialogue",
    "给一批台词做「AI 味」体检：查书面议论句、解释性对白、句长过于均匀、排比成瘾、"
    "情绪直陈、万能过渡词、句长超载、金句密度失控等，并给可替换的改写建议。"
    "纯本地、免费。台词是 AI 味重的时候，先体检再改，别直接重写整篇。",
    {"lines": {"type": "array",
               "desc": "台词行数组 [{text, character?, duration_seconds?}]；"
                       "也可以传 text（整段台词，一行一句）"},
     "genre": {"type": "string", "desc": "drama / promo（默认 drama）"},
     "style": {"type": "string",
               "desc": "auto / chenchen(罗臣臣群像) / bodaoweng(生活流)"},
     "project_id": {"type": "string", "desc": "项目 id（可选）"}},
    needs_project=False,
)
async def t_remake_audit_dialogue(args: Dict[str, Any]) -> ToolResult:
    try:
        from . import dialogue_natural as DN
    except Exception as e:  # noqa: BLE001
        return err("台词体检模块没加载上：%s: %s" % (type(e).__name__, e))
    raw = args.get("lines")
    lines: List[Dict[str, Any]] = []
    if isinstance(raw, list):
        for x in raw:
            if isinstance(x, dict):
                lines.append(x)
            elif isinstance(x, str):
                lines.append({"text": x})
    elif isinstance(raw, str) and raw.strip():
        lines = [{"text": s} for s in raw.splitlines() if s.strip()]
    if not lines:
        return err("没有台词可查：传 `lines`（数组或整段文本）。")
    try:
        rep = DN.audit_lines(lines,
                             genre=str(args.get("genre") or "drama"),
                             style=str(args.get("style") or "auto"))
    except Exception as e:  # noqa: BLE001
        logger.exception("remake_audit_dialogue failed")
        return err("台词体检失败：%s: %s" % (type(e).__name__, e))
    return ok(audit=rep, note="AI 味是**可测的**：先看 checks 里报出的问题行，"
                              "再按 suggestions 逐句改，别整篇重写。")
