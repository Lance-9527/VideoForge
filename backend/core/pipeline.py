# -*- coding: utf-8 -*-
"""
VideoForge · 可逐步确认的后期流水线（checkpoint 版）

═══════════════════════════════════════════════════════════════════
设计目标（用户选的方案 3）
═══════════════════════════════════════════════════════════════════
"默认一键跑完，但每步跑完停下来让你确认，不满意就单独重跑某步。"

要做到"单独重跑某一步"，就必须**每步产物都落盘**（checkpoint）：

    outputs/<pid>/pipeline/
      01_base.mp4        ← 第1步：把分镜接成一条底片（带转场）
      02_voice.mp4       ← 第2步：配音
      03_subtitle.mp4    ← 第3步：字幕
      04_bgm.mp4         ← 第4步：背景音乐
      05_aspect_9x16.mp4 ← 第5步：改比例
      06_trim.mp4        ← 第6步：裁时长
      final.mp4          ← 最后一步的产物复制过来

每一步都是**纯函数**：读上一部的产物 → 写自己的产物。
于是"重跑第 3 步"= 从 02_voice.mp4 重新做字幕，不影响也不依赖第 1、2 步，
更不需要重新生成视频（这点对用户最值钱：不用重复花钱调模型）。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("videoforge.pipeline")

# 步骤定义（顺序即执行顺序）
#
# skippable=True 的步骤可以"跳过"，跳过后它的产物 = 上一步的产物（直通），
# 下游照常继续。为什么需要这个：
#   如果视频模型自己就出了对白和字幕（Sora 2 / Seedance 带音频的版本…），
#   再叠加「配音」「字幕」不只是多余，还会把模型原声盖掉。
#   所以这两步必须能一键跳过，而不是逼用户"假装跑一下"。
STEPS: List[Dict[str, Any]] = [
    {
        "key": "base", "name": "① 接底片", "icon": "🧩",
        "what": "把「生成」页做好的分镜视频按顺序接成一条片子（自动加转场）。"
                "没有生成视频的分镜，会用它的场景图/角色图做运镜补上。",
        "why": "这一步决定整片的骨架。重跑它不会重新调用视频模型，不花钱。",
        "params": [
            {"k": "transition", "label": "镜头转场", "type": "select", "def": "fade",
             "options": [{"v": "fade", "t": "交叉溶解（丝滑）"}, {"v": "none", "t": "硬切"}]},
            {"k": "transition_sec", "label": "转场时长(秒)", "type": "number", "def": 0.5, "step": 0.1},
            {"k": "prefer_generated", "label": "优先用已生成的分镜视频", "type": "checkbox", "def": True},
            {"k": "auto_images", "label": "缺画面时用图像模型生成（会消耗图像额度）",
             "type": "checkbox", "def": True},
        ],
    },
    {
        "key": "voice", "name": "② 配音", "icon": "🎙",
        "what": "给每个角色配自己的音色，每句台词落在剧本里它该出现的那一刻。"
                "分镜里没有台词时，会退化成朗读剧情概述并明确告诉你。",
        "why": "如果你的视频模型自带声音，这一步可以直接跳过。",
        "skippable": True,
        "params": [
            {"k": "voice_id", "label": "音色", "type": "voice", "def": "edge:zh-CN-XiaoxiaoNeural"},
            {"k": "rate", "label": "语速", "type": "number", "def": 1.0, "step": 0.1},
            {"k": "keep_original", "label": "保留原声（与配音混合，适合模型自带对白）",
             "type": "checkbox", "def": False},
        ],
    },
    {
        "key": "subtitle", "name": "③ 字幕", "icon": "📝",
        "what": "按每个分镜的台词时间轴生成字幕，并烧进画面。",
        "why": "字幕会与镜头严格对齐，不会串到下一个镜头。模型自带字幕时可直接跳过。",
        "skippable": True,
        "params": [
            {"k": "font_size", "label": "字号", "type": "number", "def": 24},
            {"k": "font_color", "label": "颜色", "type": "select", "def": "white",
             "options": [{"v": "white", "t": "白色"}, {"v": "yellow", "t": "黄色"}]},
        ],
    },
    {
        "key": "bgm", "name": "④ 背景音乐", "icon": "🎵",
        "what": "把你选好的 BGM 混进整片，支持淡入淡出。",
        "why": "需要先在本页「BGM」处选一个音频文件。不想加就直接跳过。",
        "skippable": True,
        "params": [
            {"k": "bgm_volume", "label": "音量", "type": "number", "def": 0.25, "step": 0.05},
            {"k": "fade_in", "label": "淡入(秒)", "type": "number", "def": 1.5, "step": 0.5},
            {"k": "fade_out", "label": "淡出(秒)", "type": "number", "def": 2.0, "step": 0.5},
        ],
    },
    {
        "key": "aspect", "name": "⑤ 改比例", "icon": "📐",
        "what": "把成片导出成竖屏（抖音/视频号）或方形（小红书）。",
        "why": "横屏成片可以一键转竖屏，不用重做。本来就是你想要的画幅就直接跳过。",
        "skippable": True,
        "params": [
            {"k": "aspect", "label": "目标比例", "type": "select", "def": "9:16",
             "options": [{"v": "9:16", "t": "9:16 竖屏"}, {"v": "1:1", "t": "1:1 方形"},
                         {"v": "16:9", "t": "16:9 横屏"}]},
        ],
    },
    {
        "key": "trim", "name": "⑥ 裁时长", "icon": "⏱",
        "what": "把成片裁到指定长度（例如压到 60 秒以内）。",
        "why": "平台对时长有限制时用。时长本来就合适就直接跳过。",
        "skippable": True,
        "params": [
            {"k": "seconds", "label": "保留秒数", "type": "number", "def": 60},
        ],
    },
]

STEP_MAP = {s["key"]: s for s in STEPS}


def _ffmpeg() -> str:
    from core.ffmpeg_manager import get_ffmpeg_for_postprocess
    return get_ffmpeg_for_postprocess()


async def _has_audio(path: str) -> bool:
    """探测文件里有没有音轨（决定能不能做"保留原声"的混音）"""
    try:
        from core.compose import _run
        rc, out = await _run([_ffmpeg(), "-hide_banner", "-i", path], timeout=60)
        return "Audio:" in (out or "")
    except Exception:
        return False


def pipeline_dir(settings: dict, pid: str) -> str:
    root = settings.get("cache_dir") or os.path.join(
        os.environ.get("VIDEOFORGE_DATA_DIR", os.getcwd()), "cache")
    d = os.path.join(root, "pipeline", pid)
    os.makedirs(d, exist_ok=True)
    return d


def _state_file(settings: dict, pid: str) -> str:
    return os.path.join(pipeline_dir(settings, pid), "state.json")


def load_state(settings: dict, pid: str) -> Dict[str, Any]:
    f = _state_file(settings, pid)
    if os.path.exists(f):
        try:
            with open(f, "r", encoding="utf-8") as fp:
                return json.load(fp)
        except Exception:
            pass
    return {"steps": {}}


def save_state(settings: dict, pid: str, state: Dict[str, Any]) -> None:
    f = _state_file(settings, pid)
    with open(f, "w", encoding="utf-8") as fp:
        json.dump(state, fp, ensure_ascii=False, indent=1)


def step_output(settings: dict, pid: str, key: str, state: Optional[dict] = None) -> str:
    """取某一步的产物路径（若已完成）"""
    st = state if state is not None else load_state(settings, pid)
    rec = (st.get("steps") or {}).get(key) or {}
    p = rec.get("output") or ""
    return p if p and os.path.exists(p) else ""


def _prev_output(settings: dict, pid: str, key: str, state: dict) -> str:
    """取该步的输入：上一步的产物；第一步则没有输入"""
    idx = [s["key"] for s in STEPS].index(key)
    for i in range(idx - 1, -1, -1):
        p = step_output(settings, pid, STEPS[i]["key"], state)
        if p:
            return p
    return ""


# ═══════════════════════════════════════════════════════════════
# 各步骤实现（纯函数：输入文件 → 输出文件）
# ═══════════════════════════════════════════════════════════════

async def _step_base(pid: str, params: dict, db, settings: dict, out_dir: str,
                     state: dict, progress=None) -> Dict[str, Any]:
    """第 1 步：把分镜接成底片（复用 compose 的拼接能力，但不配音/不字幕）"""
    from core.compose import compose_project
    res = await compose_project(
        pid, db=db, settings=settings,
        with_voice=False, burn_subtitles=False,
        transition=str(params.get("transition") or "fade"),
        transition_sec=float(params.get("transition_sec") or 0.5),
        prefer_generated=bool(params.get("prefer_generated", True)),
        auto_images=bool(params.get("auto_images", True)),
        output_name="01_base.mp4",
        progress=progress,
    )
    if not res.ok:
        return {"ok": False, "error": "；".join(res.errors) or "拼接失败"}
    # 挪到 pipeline 目录，保持所有中间产物在一处
    dst = os.path.join(out_dir, "01_base.mp4")
    try:
        if os.path.abspath(res.output_path) != os.path.abspath(dst):
            shutil.copy2(res.output_path, dst)
    except Exception as e:
        return {"ok": False, "error": f"产物落盘失败：{e}"}
    return {"ok": True, "output": dst, "duration": res.duration,
            "note": f"{res.clip_count} 个分镜",
            # ★ 上限从 5 提到 12：合成阶段现在会报"逐切点转场 / 纹理统一 /
            #   画幅统一 / 素材短于标称 / 成片连贯度"好几类结论，
            #   截到 5 条会把后面的重要信息（实测发现过一次：转场结论被挤掉）
            #   静默丢弃。宁可多给几条，也不要让用户看不到结论。
            "warnings": (res.warnings or [])[:12]}


async def _step_voice(pid: str, params: dict, db, settings: dict, out_dir: str,
                      state: dict, progress=None) -> Dict[str, Any]:
    """第 2 步：配音。

    ★ 两种模式，按有没有台词自动选：

    **逐句模式（首选）**：分镜的时间线里带 `dialogue` 时启用。
      每个角色用它自己的音色，每句台词落在剧本里它该出现的那一刻，
      字幕与语音同源。这是"对话像对话"的关键。

    **整段旁白模式（兜底）**：分镜里没有台词时，只能把各分镜概述串起来读。
      这时会明确告诉用户"这段配音是朗读剧情概述、不是台词"，
      并指路去「分镜」页点「补全台词」——而不是默默糊一段上去让人不明所以。
    """
    from core.compose import _run, _ffmpeg, probe_duration, narration_for_shot
    src = _prev_output(settings, pid, "voice", state)
    if not src:
        return {"ok": False, "error": "没有上一步的产物，请先跑「① 接底片」"}

    shots = sorted(db.list_shots(pid) or [], key=lambda s: s.get("order_index") or 0)
    voice_id = params.get("voice_id") or "edge:zh-CN-XiaoxiaoNeural"
    dur = await probe_duration(src) or 0
    dst = os.path.join(out_dir, "02_voice.mp4")
    warn: List[str] = []
    note = ""

    # ── 先看全片有没有真台词 ──
    from core.dialogue import timeline_lines
    try:
        chars = db.list_characters(pid) or []
    except Exception:
        chars = []

    # ★ 关键：必须用**每段真实片段的时长**建时间轴，不能用分镜的标称
    #   `duration_seconds`。实测踩过：标称时长之和 60s，而实际成片只有 30s
    #   （模型实际只出了那么长），按标称排的话台词会全部落到片子外面，
    #   表现为"配音没有任何一句合成成功"。
    def _real_clip_duration(shot: Dict[str, Any]) -> float:
        cands = shot.get("candidates") or []
        if isinstance(cands, str):
            try:
                cands = json.loads(cands)
            except Exception:
                cands = []
        cands = [c for c in cands if isinstance(c, dict)]
        sel = next((c for c in cands if c.get("is_selected")), None) \
            or (cands[-1] if cands else None)
        d = float((sel or {}).get("duration_seconds") or 0)
        return d if d > 0.2 else float(shot.get("duration_seconds") or 5)

    shot_durs = [_real_clip_duration(s) for s in shots]
    nominal_total = sum(float(s.get("duration_seconds") or 5) for s in shots)
    real_total = sum(shot_durs)
    # 成片实际长度（01_base 的真实时长）—— 转场会吃掉一点，用它做最终钳制
    film_total = dur if dur > 0 else real_total
    scale = (film_total / real_total) if real_total > 0 else 1.0

    all_lines: List[Dict[str, Any]] = []
    cursor = 0.0
    for s, rd in zip(shots, shot_durs):
        nominal = float(s.get("duration_seconds") or 5)
        # 分镜内的相对时间按"真实时长 / 标称时长"同比缩放
        k = (rd / nominal) if nominal > 0 else 1.0
        for ln in timeline_lines(s.get("layer2_timeline"), nominal):
            x = dict(ln)
            x["start"] = cursor + float(ln["start"]) * k
            x["end"] = cursor + float(ln["end"]) * k
            x["span"] = max(0.2, x["end"] - x["start"])
            x["shot_id"] = s.get("id")
            all_lines.append(x)
        cursor += rd * scale
    # 落下成片之外的（浮点误差/时长不符）直接丢掉并如实说明，不要硬塞
    dropped = [x for x in all_lines if x["start"] >= film_total - 0.05]
    all_lines = [x for x in all_lines if x["start"] < film_total - 0.05]
    if dropped:
        warn.append(
            f"有 {len(dropped)} 句台词落在成片长度（{film_total:.1f}s）之外，已跳过 —— "
            f"说明分镜标称时长（{nominal_total:.0f}s）和实际成片（{film_total:.1f}s）差得多，"
            f"建议到「生成」页把没出片的分镜补上，或缩短分镜时长。")

    audio = os.path.join(out_dir, "voice.mp3")
    if all_lines:
        # ── 逐句模式：按角色选角 + 落在绝对时间点 ──
        if progress:
            progress("voice", 0.25, f"逐句配音（{len(all_lines)} 句，按角色分音色）…")
        from core.voicecast import synthesize_line, assemble_track, build_srt
        placements, subs, cast_used = [], [], {}
        for i, ln in enumerate(all_lines):
            v = ln.get("voice_id") or voice_id
            # 角色专属音色（存在 reference_features.voice.voice_id）
            for c in chars:
                if (c.get("name") or "") == (ln.get("character") or ""):
                    try:
                        from core.voicecast import voice_of_character
                        v = voice_of_character(c) or v
                    except Exception:
                        pass
                    break
            cast_used[ln.get("character") or "（旁白）"] = v
            out = os.path.join(out_dir, f"line_{i:03d}.mp3")
            rr = await synthesize_line(ln["text"], v, settings, out,
                                       target_span=ln["span"],
                                       emotion=ln.get("emotion", ""))
            if not rr.get("ok"):
                warn.append(f"第 {i+1} 句合成失败：{rr.get('error')}")
                continue
            if rr.get("overflow", 0) > 0.35:
                warn.append(f"「{ln['text'][:14]}…」比它的时间片长 {rr['overflow']:.1f} 秒，"
                            f"建议改短台词或加长这一段。")
            placements.append({"path": out, "start": ln["start"], "span": ln["span"],
                               "text": ln["text"], "character": ln.get("character", ""),
                               "voice_id": v, "speed": rr.get("speed", 1.0)})
            # 字幕结束时间要用**混完之后真正占的长度**：当 atempo 留给排轨层去拉时，
            # 文件长度 ≠ 时间轴上占的长度，用错了字幕会比声音早结束。
            _plen = float(rr.get("placed_duration") or rr["duration"])
            subs.append({"start": ln["start"],
                         "end": round(ln["start"] + _plen, 3),
                         "text": ln["text"], "character": ln.get("character", "")})
        if not placements:
            return {"ok": False, "error": "全部台词都合成失败",
                    "warnings": warn}
        if progress:
            progress("voice", 0.75, "按时间轴拼装音轨…")
        tr = assemble_track(placements, dur, audio)
        if not tr.get("ok"):
            return {"ok": False, "error": tr.get("error"), "warnings": warn}
        # 字幕与语音同源，落盘给下一步用
        try:
            build_srt(subs, os.path.join(out_dir, "dub.srt"))
            state["dub_subtitles"] = subs
        except Exception:
            pass
        warn.extend(tr.get("warnings") or [])
        note = (f"逐句配音 {len(placements)} 句 · "
                f"{len(cast_used)} 个角色各用各的音色")
    else:
        # ── 兜底：整段旁白（分镜里确实没有台词） ──
        parts = [narration_for_shot(s, i) for i, s in enumerate(shots)]
        text = "。".join(p for p in parts if p)
        if not text.strip():
            return {"ok": False, "error": "没有可配音的文本（分镜里没有内容）"}
        if progress:
            progress("voice", 0.3, "合成旁白（分镜里没有台词）…")
        from core.voice.base import TTSRequest
        from core.voice import dispatcher as voice_dispatcher
        r = await voice_dispatcher.synthesize(
            TTSRequest(text=text, voice_id=voice_id, output_path=audio,
                       rate=float(params.get("rate") or 1.0)), settings)
        if not r.success or not os.path.exists(audio):
            return {"ok": False, "error": f"配音失败：{r.error}"}
        warn.append(
            "这段配音朗读的是**剧情概述**，不是角色台词（分镜里没有可念的台词）。"
            "想让对话像对话：到「分镜」页对每个分镜点「🎬 补全台词」，再跑这一步。")
        note = f"旁白 {len(text)} 字（无台词，读的是概述）"

    if progress:
        progress("voice", 0.8, "混入音轨…")
    vdur = await probe_duration(audio) or 0
    keep = bool(params.get("keep_original"))
    filters = []
    if dur > 0 and vdur > dur * 1.03:
        filters.append(f"atempo={max(0.5, min(2.0, vdur / dur)):.4f}")
    vchain = f"[1:a]{','.join(filters) + ',' if filters else ''}apad[a]"
    has_orig = await _has_audio(src)
    if keep and has_orig:
        # 视频模型自带的语音/音效 + 我们的配音 混在一起，两边都听得见
        acmd = (f"{vchain};[0:a]volume=1.0[o];[o][a]amix=inputs=2:duration=first:"
                f"dropout_transition=0:normalize=0[aout]")
        amap = "[aout]"
    else:
        acmd, amap = vchain, "[a]"
    # 输出侧用 -t 钳到画面长度：配音略长时不会把成片拉长
    cmd = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", src, "-i", audio,
           "-filter_complex", acmd, "-map", "0:v", "-map", amap,
           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k"]
    if dur > 0:
        cmd += ["-t", f"{dur:.3f}"]
    cmd += ["-movflags", "+faststart", dst]
    rc, err = await _run(cmd, timeout=900)
    if rc != 0 or not os.path.exists(dst):
        return {"ok": False, "error": f"混音失败：{(err or '')[-200:]}"}
    if keep and not has_orig:
        warn.append("这条底片本来就没有原声（分镜视频是静音的），所以只加了配音。")
    return {"ok": True, "output": dst, "duration": await probe_duration(dst),
            "note": note + ("（保留原声混音）" if keep and has_orig else ""),
            "warnings": warn}


async def _step_subtitle(pid: str, params: dict, db, settings: dict, out_dir: str,
                         state: dict, progress=None) -> Dict[str, Any]:
    """第 3 步：字幕（按分镜台词时间轴生成并烧录）

    ★ 三处修正（都是实测踩出来的）：
    1. `dialogue` 现在是 {character,text,emotion} **结构体**，旧代码对它调
       `.strip()` 会 AttributeError 直接崩。
    2. 时间轴必须用**每段真实片段时长**，不能用分镜标称时长 ——
       实测标称合计 60s 而成片只有 28s，字幕会一路排到片子外面去。
    3. 没有台词的分镜不能把整段概述（常常 100+ 字）当一条字幕糊满镜头；
       要切成短句分条显示，并过滤掉"时长为10秒"这类提示词残留。
    """
    from core.compose import _burn, _ffmpeg, probe_duration
    from core.compose import _split_subtitle_text
    from core.dialogue import timeline_lines
    from core.subtitle import build_srt
    src = _prev_output(settings, pid, "subtitle", state)
    if not src:
        return {"ok": False, "error": "没有上一步的产物，请先跑前面的步骤"}

    # ── 用真实片段时长建时间轴（关键）──
    film_total = await probe_duration(src) or 0
    shots = sorted(db.list_shots(pid) or [], key=lambda s: s.get("order_index") or 0)

    def _real_dur(sh):
        cands = sh.get("candidates") or []
        if isinstance(cands, str):
            try:
                cands = json.loads(cands)
            except Exception:
                cands = []
        cands = [c for c in cands if isinstance(c, dict)]
        sel = next((c for c in cands if c.get("is_selected")), None) \
            or (cands[-1] if cands else None)
        d = float((sel or {}).get("duration_seconds") or 0)
        return d if d > 0.2 else float(sh.get("duration_seconds") or 5)

    shot_durs = [_real_dur(s) for s in shots]
    real_total = sum(shot_durs) or 1.0
    scale = (film_total / real_total) if film_total > 0 else 1.0

    entries: List[dict] = []
    cursor = 0.0
    warn: List[str] = []
    for i, (s, rd) in enumerate(zip(shots, shot_durs)):
        nominal = float(s.get("duration_seconds") or 5)
        k = (rd / nominal) if nominal > 0 else 1.0
        lines = timeline_lines(s.get("layer2_timeline"), nominal)
        added = 0
        for ln in lines:
            a, b = float(ln["start"]) * k, float(ln["end"]) * k
            if b <= a or a >= rd:
                continue
            entries.append({"start": cursor + a, "end": cursor + min(b, rd),
                            "text": ln["text"], "character": ln.get("character") or ""})
            added += 1
        if not added:
            from core.compose import narration_for_shot
            n = narration_for_shot(s, i)
            if n:
                chunks = _split_subtitle_text(n) or [n[:18]]
                seg = rd / max(1, len(chunks))
                for j, ck in enumerate(chunks):
                    st_ = j * seg
                    if st_ >= rd:
                        break
                    entries.append({"start": cursor + st_,
                                    "end": cursor + min(st_ + seg, rd),
                                    "text": ck, "character": ""})
                    added += 1
        cursor += rd * scale

    # 超出成片长度的丢掉（标称与实际的差会累积）
    if film_total > 0:
        over = [e for e in entries if e["start"] >= film_total - 0.05]
        entries = [e for e in entries if e["start"] < film_total - 0.05]
        if over:
            warn.append(f"{len(over)} 条字幕落在成片（{film_total:.1f}s）之外，已跳过")
    if not entries:
        return {"ok": False, "error": "没有生成任何字幕内容"}

    # build_srt 会把每条 end 夹到下一句之前并保证最短显示时长
    srt = build_srt(entries, os.path.join(out_dir, "subtitles.srt"))
    if progress:
        progress("subtitle", 0.5, f"烧录 {len(entries)} 条字幕…")
    w = 1280
    try:
        rc, info = await _run([_ffmpeg(), "-hide_banner", "-i", src], timeout=60)
        import re as _re
        m = _re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", info or "", _re.S)
        if m:
            w = int(m.group(1))
    except Exception:
        pass
    dst = os.path.join(out_dir, "03_subtitle.mp4")
    ok, note = await _burn(src, srt, dst, w, int(w * 9 / 16))
    if not ok:
        return {"ok": False, "error": f"字幕烧录失败：{note}", "srt": srt,
                "warnings": warn}
    return {"ok": True, "output": dst, "duration": await probe_duration(dst),
            "note": f"{len(entries)} 条字幕（按真实片段时长排轴）", "srt": srt,
            "warnings": warn}


async def _step_bgm(pid: str, params: dict, db, settings: dict, out_dir: str,
                    state: dict, progress=None) -> Dict[str, Any]:
    """第 4 步：背景音乐"""
    from core.compose import _run, _ffmpeg, probe_duration
    src = _prev_output(settings, pid, "bgm", state)
    if not src:
        return {"ok": False, "error": "没有上一步的产物，请先跑前面的步骤"}
    bgm = params.get("bgm_path") or state.get("bgm_path") or ""
    if not bgm or not os.path.exists(bgm):
        return {"ok": False, "error": "还没有选择 BGM 文件 —— 请在本页「BGM」处先选一个音频"}
    dur = await probe_duration(src) or 0
    vol = float(params.get("bgm_volume") or 0.25)
    fi = float(params.get("fade_in") or 1.5)
    fo = float(params.get("fade_out") or 2.0)
    fo_start = max(0.0, dur - fo) if dur else 0
    chain = (f"[1:a]volume={vol:.3f},afade=t=in:st=0:d={fi:.2f}"
             + (f",afade=t=out:st={fo_start:.2f}:d={fo:.2f}" if dur else "")
             + "[bg];[0:a][bg]amix=inputs=2:duration=first:dropout_transition=0[a]")
    dst = os.path.join(out_dir, "04_bgm.mp4")
    if progress:
        progress("bgm", 0.6, "混入背景音乐…")
    cmd = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", src, "-i", bgm,
           "-filter_complex", chain, "-map", "0:v", "-map", "[a]",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", dst]
    rc, err = await _run(cmd, timeout=900)
    if rc != 0 or not os.path.exists(dst):
        return {"ok": False, "error": f"BGM 混流失败：{(err or '')[-200:]}"}
    return {"ok": True, "output": dst, "duration": await probe_duration(dst),
            "note": f"{os.path.basename(bgm)} · 音量 {vol}"}


async def _step_aspect(pid: str, params: dict, db, settings: dict, out_dir: str,
                       state: dict, progress=None) -> Dict[str, Any]:
    src = _prev_output(settings, pid, "aspect", state)
    if not src:
        return {"ok": False, "error": "没有上一步的产物，请先跑前面的步骤"}
    asp = str(params.get("aspect") or "9:16")
    from core.postprocess import export_aspect_ratio
    from core.compose import probe_duration
    dst = os.path.join(out_dir, f"05_aspect_{asp.replace(':', 'x')}.mp4")
    if progress:
        progress("aspect", 0.5, f"导出 {asp}…")
    try:
        out = await export_aspect_ratio(src, asp, dst, _ffmpeg())
    except Exception as e:
        return {"ok": False, "error": f"比例导出失败：{e}"}
    return {"ok": True, "output": out, "duration": await probe_duration(out),
            "note": f"目标比例 {asp}"}


async def _step_trim(pid: str, params: dict, db, settings: dict, out_dir: str,
                     state: dict, progress=None) -> Dict[str, Any]:
    from core.compose import _run, _ffmpeg, probe_duration
    src = _prev_output(settings, pid, "trim", state)
    if not src:
        return {"ok": False, "error": "没有上一步的产物，请先跑前面的步骤"}
    sec = float(params.get("seconds") or 60)
    dst = os.path.join(out_dir, "06_trim.mp4")
    if progress:
        progress("trim", 0.5, f"裁剪到 {sec:.0f} 秒…")
    rc, err = await _run([_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                          "-i", src, "-t", f"{sec:.2f}", "-c", "copy",
                          "-movflags", "+faststart", dst], timeout=600)
    if rc != 0 or not os.path.exists(dst):
        rc2, err2 = await _run([_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                                "-i", src, "-t", f"{sec:.2f}",
                                "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                                "-c:a", "aac", dst], timeout=1200)
        if rc2 != 0:
            return {"ok": False, "error": f"裁剪失败：{(err2 or err)[-200:]}"}
    return {"ok": True, "output": dst, "duration": await probe_duration(dst),
            "note": f"保留 {sec:.0f} 秒"}


_RUNNERS: Dict[str, Callable] = {
    "base": _step_base, "voice": _step_voice, "subtitle": _step_subtitle,
    "bgm": _step_bgm, "aspect": _step_aspect, "trim": _step_trim,
}


async def run_step(pid: str, key: str, params: dict, *, db, settings: dict,
                   progress=None) -> Dict[str, Any]:
    """执行单个步骤并记录 checkpoint。

    ★ 单独重跑某一步 = 直接再调它一次：它会读**上一步已经落盘的产物**，
      不会重跑前面的步骤，更不会重新调用视频模型（不重复花钱）。
    """
    if key not in STEP_MAP:
        return {"ok": False, "error": f"未知步骤：{key}"}
    out_dir = pipeline_dir(settings, pid)
    state = load_state(settings, pid)
    steps_state = state.setdefault("steps", {})
    params = {k: v for k, v in (params or {}).items() if v is not None}

    if progress:
        progress(key, 0.05, f"{STEP_MAP[key]['name']} 开始…")
    try:
        res = await _RUNNERS[key](pid, params, db, settings, out_dir, state, progress)
    except Exception as e:
        logger.exception("pipeline step %s failed", key)
        res = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    rec = steps_state.setdefault(key, {})
    rec.update({"params": params, "ok": bool(res.get("ok")),
                "error": res.get("error") or "", "note": res.get("note") or "",
                "output": res.get("output") or "", "duration": res.get("duration") or 0,
                # 真跑过之后就不再是"跳过"状态（否则界面上会一直显示"已跳过"）
                "skipped": False,
                "warnings": res.get("warnings") or []})
    if key == "bgm" and params.get("bgm_path"):
        state["bgm_path"] = params["bgm_path"]
    # 后续步骤的产物因输入变了而失效 —— 必须清掉，否则用户会看到旧结果
    if res.get("ok"):
        order = [s["key"] for s in STEPS]
        for later in order[order.index(key) + 1:]:
            old = steps_state.get(later)
            if old:
                old.update({"ok": False, "stale": True, "output": "",
                            "note": "上游步骤已改动，需要重跑"})
    save_state(settings, pid, state)
    if res.get("ok") and progress:
        progress(key, 1.0, f"{STEP_MAP[key]['name']} 完成")
    return res


def skip_step(settings: dict, pid: str, key: str, reason: str = "") -> Dict[str, Any]:
    """把某一步标记为「跳过」：它的产物直接沿用上一步的产物（直通），下游照常继续。

    这样"模型自带对白和字幕"的情况下，用户就不用硬跑一遍配音/字幕
    —— 点一下跳过，后面的步骤和出成片全都不受影响。
    """
    if key not in STEP_MAP:
        return {"ok": False, "error": f"未知步骤：{key}"}
    if not STEP_MAP[key].get("skippable"):
        return {"ok": False, "error": f"「{STEP_MAP[key]['name']}」是必经步骤，不能跳过"}
    state = load_state(settings, pid)
    steps_state = state.setdefault("steps", {})
    src = _prev_output(settings, pid, key, state)
    if not src:
        return {"ok": False, "error": "前面的步骤还没跑，没有可以沿用的产物"}

    # 直通：产物指向上一步的文件（不复制，省磁盘；下游读的是内容不是位置）
    rec = steps_state.setdefault(key, {})
    rec.update({"params": {}, "ok": True, "error": "", "skipped": True,
                "output": src, "duration": 0,
                "note": reason or "已跳过（沿用上一步的画面）",
                "warnings": []})
    # 这一跳，下游的输入就变了 —— 和正常跑完一样要把下游标脏
    order = [s["key"] for s in STEPS]
    for later in order[order.index(key) + 1:]:
        old = steps_state.get(later)
        if old and not old.get("skipped"):
            old.update({"ok": False, "stale": True, "output": "",
                        "note": "上游步骤已改动，需要重跑"})
    save_state(settings, pid, state)
    return {"ok": True, "skipped": True, "output": src,
            "note": rec["note"]}


def unskip_step(settings: dict, pid: str, key: str) -> Dict[str, Any]:
    """取消「跳过」，让这一步回到未执行状态"""
    state = load_state(settings, pid)
    steps_state = state.setdefault("steps", {})
    rec = steps_state.get(key)
    if rec and rec.get("skipped"):
        rec.update({"ok": False, "skipped": False, "output": "", "note": "",
                    "stale": False})
        save_state(settings, pid, state)
        return {"ok": True}
    return {"ok": False, "error": "这一步当前不是跳过状态"}


def finalize(settings: dict, pid: str) -> Dict[str, Any]:
    """把最后一个已完成步骤的产物复制成 final.mp4"""
    state = load_state(settings, pid)
    last = ""
    for s in STEPS:
        p = step_output(settings, pid, s["key"], state)
        if p:
            last = p
    if not last:
        return {"ok": False, "error": "还没有任何已完成步骤的产物"}
    out_root = settings.get("output_dir") or os.path.join(
        os.environ.get("VIDEOFORGE_DATA_DIR", os.getcwd()), "outputs")
    dst_dir = os.path.join(out_root, pid)
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, "final.mp4")
    shutil.copy2(last, dst)
    return {"ok": True, "output": dst, "source": last}


def status(settings: dict, pid: str) -> Dict[str, Any]:
    """返回整条流水线的状态（前端据此渲染每步的完成/待跑/需重跑）"""
    state = load_state(settings, pid)
    steps = []
    for s in STEPS:
        rec = (state.get("steps") or {}).get(s["key"]) or {}
        p = rec.get("output") or ""
        exists = bool(p) and os.path.exists(p)
        steps.append({
            "key": s["key"], "name": s["name"], "icon": s["icon"],
            "what": s["what"], "why": s["why"], "params": s["params"],
            "skippable": bool(s.get("skippable")),
            "skipped": bool(rec.get("skipped")),
            "enabled": rec.get("enabled", True),
            "done": bool(rec.get("ok")) and exists,
            "stale": bool(rec.get("stale")),
            "error": rec.get("error") or "",
            "note": rec.get("note") or "",
            "duration": rec.get("duration") or 0,
            "params_used": rec.get("params") or {},
            "has_output": exists,
            # 让前端「看这一步的结果」有真东西可播（产物在 cache 里，不在输出目录）
            "preview_url": f"/api/projects/{pid}/pipeline/artifact/{s['key']}" if exists else "",
            "download_url": (f"/api/projects/{pid}/pipeline/artifact/{s['key']}?download=1"
                             if exists else ""),
            "warnings": rec.get("warnings") or [],
        })
    done_keys = [x["key"] for x in steps if x["done"]]
    return {"steps": steps, "done": done_keys, "bgm_path": state.get("bgm_path") or "",
            "final_ready": bool(done_keys)}
