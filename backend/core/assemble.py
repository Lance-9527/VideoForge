# -*- coding: utf-8 -*-
"""
VideoForge · 成片装配（Assemble）

═══════════════════════════════════════════════════════════════════
把这一轮所有新能力串成一条正确的流水线
═══════════════════════════════════════════════════════════════════
旧做法（有真实的三个后果）：
  1. 每个分镜各自配音，念的是 `layer1_overview`（剧情概述），不是台词
  2. 拼接时一律 fade 0.5 秒 —— 同一场戏的镜头之间也溶解，一眼假
  3. 音频在**每个片段内**生成，拼接用 xfade 会让成片总长少掉 (n−1)×t，
     于是从第二个镜头起，所有声音都**整体提前**，越往后偏得越多

新做法（本模块）：
  ┌ 画面轨：逐段规范化 → 按"同场景硬切 / 跨场景溶解"选转场 → 拼接（无音轨）
  ├ 时间轴：用 continuity.final_timeline 算出每段在成片里的真实起点
  ├ 声音轨：所有台词按**角色音色**合成，落在**映射后的成片绝对时间**上
  │         一次性编码（避免 MP3 逐段拼接的累积漂移）
  ├ 字幕：与声音同源，同样用映射后的时间
  └ 最后：画面 + 声音 + BGM(带 ducking) 在**同一原点**合轨，输出侧 -t 精确钳制

这样"台词→声音→字幕→成片"是**一条时间轴**，不可能对不上。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger("videoforge.assemble")


def _ffmpeg() -> str:
    from core.ffmpeg_manager import get_ffmpeg_for_postprocess
    return get_ffmpeg_for_postprocess()


async def _run(cmd: List[str], timeout: float = 1800.0):
    from core.compose import _run as _crun
    return await _crun(cmd, timeout=timeout)


async def _probe_duration(p: str) -> float:
    try:
        from core.compose import probe_duration
        return float(await probe_duration(p) or 0.0)
    except Exception:
        return 0.0


# 实测与计划的容差：小于这个差就当成"就是这么多"。
# 为什么不追求 0：容器时长与精确时长本来就有几十毫秒的出入，
# 每一条都报会让提示变成噪音，用户就不看了。
DURATION_TOLERANCE = 0.35


def reconcile_duration(planned: float, actual: float) -> Dict[str, Any]:
    """把「计划要多久」和「模型实际给了多久」对账。

    ★ 这是用户那句"分层定 10s 但最终只能生成 5s"的**正面回答**。
      以前的做法是：只有一段时就 `duration = 请求值`，
      于是模型给了 5 秒、库里却写着 10 秒 —— 画面确实短了，
      但**时间轴、字幕、成片长度全都按 10 秒排**，结果是成片后半段没画面，
      或者字幕压在一片空白上。用户看到的是"东西不对"，却查不出原因。

      MoneyPrinterTurbo 在这一点上分得很清（`video.py:743-748`）：
      `max_clip_duration` 约束的是**成片里的最终播放时长**，
      读源文件用的是换算过的 `source_clip_duration`；
      它每次都拿**实际** `clip.duration` 累加，缺口还会明确打日志
      （`"video duration (X) is shorter than required duration (Y)"`）。

    返回 {actual, shortfall, over, warnings}：
      - `shortfall` > 0 表示**模型没给够**（要提示用户，并说明成片按实际长度排）
      - `over` > 0 表示给多了（裁掉即可，不算问题，但让用户知道）
    """
    p = float(planned or 0.0)
    a = float(actual or 0.0)
    warnings: List[str] = []
    if a <= 0.2:
        return {"actual": a, "shortfall": 0.0, "over": 0.0,
                "warnings": ["量不出成片时长（文件可能损坏），已按计划时长记录。"]}
    if p > 0 and a + DURATION_TOLERANCE < p:
        warnings.append(
            f"⚠ 模型实际只出了 {a:.1f} 秒，而计划是 {p:.0f} 秒"
            f"（差 {p - a:.1f} 秒）。已按**实际长度**记录，"
            f"成片不会出现空白画面；这段的台词可能被压缩 —— "
            f"可以把分镜时长改短，或换一个支持更长时长的模型。")
    elif p > 0 and a > p + DURATION_TOLERANCE:
        warnings.append(
            f"模型出了 {a:.1f} 秒（计划 {p:.0f} 秒），"
            f"多出的部分在成片时按内容裁切，不会浪费。")
    return {"actual": round(a, 3), "shortfall": round(max(0.0, p - a), 3),
            "over": round(max(0.0, a - p), 3), "warnings": warnings}


async def _has_audio(p: str) -> bool:
    try:
        rc, out = await _run([_ffmpeg(), "-hide_banner", "-i", p], timeout=60)
        return "Audio:" in (out or "")
    except Exception:
        return False


async def normalize_picture(src: str, dst: str, w: int, h: int, fps: int = 30) -> None:
    """把一段画面统一规格，并且**丢掉音轨**。

    为什么中间产物一律无音轨（这条是从 MPT 的架构里确认过的）：
      - 各段音轨采样率/声道可能不同，concat 会因为流不一致失败
      - 素材原声会串味（我们要的是自己配的音）
      - 音轨反复重编码会劣化
    音频在最后一步一次性挂上，共同原点 t=0 → 天然同步。
    """
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
          f"crop={w}:{h},setsar=1,fps={fps},format=yuv420p")
    rc, err = await _run([
        _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", src,
        "-vf", vf, "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", dst], timeout=900)
    if rc != 0 or not os.path.exists(dst):
        raise RuntimeError(f"画面规范化失败：{(err or '')[-300:]}")


async def _ensure_audio(src: str, dst: str, w: int, h: int, fps: int) -> str:
    """确保一段画面**有音轨**（没有就补静音）。

    xfade/acrossfade 要求两路都有音频流，缺一路整条命令就失败。
    但中间产物的规范是"不要音轨"（避免素材原声串味），
    所以只在真正要溶解的切点两侧临时补静音。
    """
    if await _has_audio(src):
        return src
    rc, err = await _run([
        _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", src,
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-map", "0:v", "-map", "1:a", "-shortest",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", dst], timeout=600)
    if rc != 0 or not os.path.exists(dst):
        raise RuntimeError(f"补静音轨失败：{(err or '')[-300:]}")
    return dst


async def _concat_run(paths: List[str], dst: str) -> str:
    """把若干段**硬切**拼成一段（视频+音频）。"""
    if len(paths) == 1:
        return paths[0]
    inputs: List[str] = []
    for p in paths:
        inputs += ["-i", p]
    parts = "".join(f"[{i}:v][{i}:a]" for i in range(len(paths)))
    filt = f"{parts}concat=n={len(paths)}:v=1:a=1[ov][oa]"
    rc, err = await _run([
        _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error"] + inputs +
        ["-filter_complex", filt, "-map", "[ov]", "-map", "[oa]",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
         "-movflags", "+faststart", dst], timeout=1800)
    if rc != 0 or not os.path.exists(dst):
        raise RuntimeError(f"片段组拼接失败：{(err or '')[-300:]}")
    return dst


def _card_drawtext(card: str, w: int, h: int, *, hold: float = 1.2) -> str:
    """生成"地点字幕卡"的 drawtext 滤镜串。

    观感设计（这是它为什么在**黑场期间**出现的关键）：
      · 换场用 `xfade=fadeblack`，所以接缝处画面本来就会经过全黑；
      · 卡片画在**进来的那一段**开头 `hold` 秒上：黑场时字先浮出来，
        画面淡入后字仍在，最后淡出 —— 观众读到的是"下一场发生在哪"。
      · 位置在下三分之一（不压住主体脸部），字号随画幅缩放。

    转义：`drawtext` 的 text 里冒号、反斜杠、单引号都要转义，
    而且路径必须用正斜杠（Windows 反斜杠会被当成转义符）。
    """
    def _esc(s: str) -> str:
        return (str(s).replace("\\", "\\\\").replace(":", r"\:")
                .replace("'", r"\'").replace("%", r"\%"))

    fontfile = ""
    try:
        from core.subtitle import pick_font_file
        fontfile = pick_font_file() or ""
    except Exception:
        fontfile = ""
    size = max(20, int(h * 0.052))
    x = "(w-text_w)/2"
    y = f"h*0.78"
    # alpha 淡入淡出：0→1 用 0.25s，末 0.25s 淡出
    alpha = f"if(lt(t,0.25),t/0.25,if(lt(t,{hold - 0.25:.3f}),1,max(0,({hold:.3f}-t)/0.25)))"
    parts = [
        f"text='{_esc(card)}'",
        f"fontsize={size}",
        "fontcolor=white",
        "borderw=3",
        "bordercolor=black@0.85",
        f"x={x}", f"y={y}",
        f"alpha='{alpha}'",
        f"enable='lt(t,{hold:.3f})'",
    ]
    if fontfile:
        parts.insert(0, "fontfile='" + fontfile.replace("\\", "/").replace(":", r"\:") + "'")
    return "drawtext=" + ":".join(parts)


async def concat_with_transitions(
    clips: List[str], out_path: str, transitions: List[Dict[str, Any]],
    *, w: int, h: int, fps: int = 30, work_dir: str = "",
    scene_ids: Optional[List[str]] = None,
    scene_transition_mode: str = "auto",
    scene_card: bool = True,
    card_texts: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """按**逐切点**的转场拼接（不是全局一律 fade）。

    `transitions[i]` 描述第 i 段与前一段之间的转场（transitions[0] 忽略）。
    只有真正需要溶解的切点才进 xfade，其余走 concat demuxer（更快、无损）。

    `scene_transition_mode` / `scene_card` / `card_texts`（2026-09-13 新增）：
    跨场景切点的处理方式 —— `auto`（默认，按实测亮度跳变决定）/ `cut` /
    `dissolve` / `dip`（黑场 + 地点字幕卡）。详见函数体内"换场处理"那一节。

    ★ 实现要点（踩过坑）：
      把 `concat` 滤镜和 `xfade` 混在同一张 filter graph 里会让流标签互相污染，
      实测直接报 "Could not open encoder before EOF"。
      正确做法是分两层：
        1. 先用 concat 把**连续硬切**的镜头合成一个个"片段组"（run）
        2. 再在片段组之间用 xfade/acrossfade 做溶解
      这也正好对应剪辑师的心智模型：一场戏内部硬切，场与场之间才做转场。
    """
    if not clips:
        raise ValueError("没有可拼接的片段")
    work_dir = work_dir or os.path.dirname(out_path) or "."
    # 这里会在 work_dir 下写中间件（_ra*.mp4 / _run*.mp4 / seamfix/）。
    # 不确保它存在的话，报错是 `Error opening output ... No such file or directory`
    # —— 信息里完全看不出"其实是目录没建"，很难查。自己建掉。
    os.makedirs(work_dir, exist_ok=True)

    # ══════════════════════════════════════════════════════════════
    # ★★ 归一化闸门：参数不一致就**先统一，再拼接**
    # ══════════════════════════════════════════════════════════════
    # 这里曾经有一个**静默损坏**的 bug：
    #   全部硬切时走 concat demuxer + `-c copy`（本意是"无损又快"），
    #   但 concat demuxer 要求所有输入的编码参数**完全一致**
    #   （分辨率 / SAR / fps / pix_fmt / timebase）。
    #   线上真实项目 `19636f68` 的 4 个片段是
    #     1280x720 / 1920x1080 / 1080x1080 / 1080x1080，fps 还 25 与 24 混用。
    #   实测后果（`tests/repro_concat_bug.py`）：
    #     ffmpeg **返回成功(rc=0)**，产物却是 1280x720 / 25.25fps、
    #     总长 22.33s（源 23.28s —— **静默丢了 0.95 秒**）。
    #   这种文件播放时接缝处会卡顿/花屏 —— SPS/PPS 与时间基都对不上。
    #   用户说的"拼起来一眼AI、不丝滑"，有一份就是它。
    # 现在：先探测，只要不齐就先过 `core.seamfix.harmonize` 统一画幅，
    #       顺手把"片头沉降/片尾定住"的帧裁掉、同场景内做温和调色。
    harmonize_info: Dict[str, Any] = {}
    seam_stats: List[Dict[str, Any]] = []
    try:
        from core import seamfix as _sf
        seam_stats = [await _sf.analyze_clip(c) for c in clips]
        sigs = {(s["w"], s["h"], round(s["fps"], 3)) for s in seam_stats if s["w"]}
        if len(sigs) > 1:
            hres = await _sf.harmonize(clips, os.path.join(work_dir, "seamfix"),
                                       scene_ids=scene_ids, do_color=True, do_trim=False)
            if hres.get("ok") and hres.get("out"):
                clips = hres["out"]
                seam_stats = [await _sf.analyze_clip(c) for c in clips]
                harmonize_info = {
                    "applied": True,
                    "target": hres["target"],
                    "notes": hres["notes"], "warnings": hres["warnings"],
                    "before": hres["before"], "after": hres["after"],
                    "texture": hres.get("texture"),
                }
                logger.info("接缝归一化：%s", "；".join(hres["notes"])[:400])
    except Exception as e:
        logger.warning("接缝归一化失败（继续用原片拼接，可能不平滑）：%s", e)
        harmonize_info = {"applied": False, "error": f"{type(e).__name__}: {e}"}

    # ══════════════════════════════════════════════════════════════
    # ★★ 冻结重演：即使画幅与纹理都一致，串联段之间也可能有"定住几帧"
    # ══════════════════════════════════════════════════════════════
    # 画幅一致、纹理一致、冻结重演 —— 这是**三件独立的事**。
    # 前两关都没触发时（画幅已统一、纹理本来就齐），如果就不跑 harmonize，
    # 那"串联生成的开头定住几帧"永远没人管 —— 而这恰恰是最常见的情形
    # （同一个模型跑同一个项目，画幅纹理天然一致，但分段串联一定有重复头）。
    # 所以这里再单独量一次停顿，有停顿就把重复头裁掉。
    if not harmonize_info.get("applied") and not harmonize_info.get("texture_only") \
            and seam_stats:
        try:
            from core import seamfix as _sf
            sres = _sf.stall_report(seam_stats)
            if (sres.get("stall_count") or 0) > 0 or any(
                    float(s.get("dup_seconds") or 0) > 0.05 for s in sres["seams"]):
                hres3 = await _sf.harmonize(
                    clips, os.path.join(work_dir, "dedupfix"),
                    scene_ids=scene_ids, do_color=False, do_trim=False,
                    do_texture=False, do_dedup=True)
                if hres3.get("ok") and hres3.get("out"):
                    clips = hres3["out"]
                    seam_stats = [await _sf.analyze_clip(c) for c in clips]
                    harmonize_info = {
                        "applied": False, "dedup_only": True,
                        "notes": hres3["notes"], "warnings": [],
                        "stall": hres3.get("stall"),
                    }
                    logger.info("仅裁重复头：%s", "；".join(hres3["notes"])[:300])
        except Exception as e:
            logger.warning("重复头裁剪失败（继续用原片拼接）：%s", e)

    # ══════════════════════════════════════════════════════════════
    # ★★ 片段**内部**的停顿（与接缝跳变是两件独立的事）
    # ══════════════════════════════════════════════════════════════
    # 实测教训（2026-09-13，项目 caa9904f）：段间拼点**完全看不出来**
    # （内部最猛帧只有 1.02× p99），可它内部 t=7.25s 起有 **2.88 秒几乎静止** ——
    # 观众的感受是"卡住了几秒"。这条信息以前**一次都没进过告警**，
    # 因为上面的分支只在"接缝处停顿"或"画幅/纹理不齐"时才跑。
    # 这里无条件量一次（用的是已经在手上的统计，几乎不花时间）。
    # ══════════════════════════════════════════════════════════════
    # ★★ "多个镜头其实是同一个画面" —— **无条件**量一次
    # ══════════════════════════════════════════════════════════════
    # 为什么放在这一层、且不放在任何 `if` 分支里：
    #   它是**关于素材本身的事实**，与"这次有没有画幅要统一 / 有没有重复头要裁"
    #   毫无关系。放在 `harmonize` 里就会变成"只有需要归一化时才检查"——
    #   实测正是这样漏掉的：项目 46c75ddf 的 5 个镜头是**同一张老者肖像**，
    #   而那次合并没有触发归一化，于是这条告警**一次都没出现过**。
    #   用的是已经在手上的 `seam_stats[i]["_first"]`，零额外解码。
    if seam_stats:
        try:
            from core import seamfix as _sf
            _dup = _sf.duplicate_shot_report(seam_stats)
            harmonize_info["duplicate_shots"] = _dup
            if _dup.get("pairs"):
                _ds = "、".join(f"镜{a}↔镜{b}（{s:.3f}）"
                                for a, b, s in _dup["pairs"][:6])
                harmonize_info["warnings"] = list(harmonize_info.get("warnings") or []) + [
                    f"有 {len(_dup['pairs'])} 对分镜的画面**几乎完全相同**（{_ds}）—— "
                    f"大概率是同一张图/同一段素材被当成了多个镜头，成片会像卡住不动。"
                    f"建议逐条核对候选片段，必要时重新生成；"
                    f"（判定口径：整帧灰度相似度 ≥ {_sf.DUP_FRAME_SIM}）"]
        except Exception as e:
            logger.warning("同画面镜头体检失败：%s", e)

    if seam_stats:
        try:
            from core import seamfix as _sf
            # 已有（比如 dedup 分支给的"裁后"结果）就不覆盖 —— 那份更准
            harmonize_info.setdefault("stall", _sf.stall_report(seam_stats))
        except Exception as e:
            logger.warning("片段内部停顿体检失败：%s", e)

    # ══════════════════════════════════════════════════════════════
    # ★★ 纹理统一：即使画幅本来就一致，纹理也可能"像两台机器拍的"
    # ══════════════════════════════════════════════════════════════
    # 画幅一致 / 画幅不一致 是两件独立的事：
    #   项目 `19636f68` 不同模型的片段，即使都缩到同一画幅，
    #   高频纹理能量也能差 2~3 倍（一段有胶片颗粒、一段像塑料）。
    # 所以几何那一关没过（画幅已一致）时，这里再单独量一次纹理，
    # 差得多就只做纹理这一遍 —— 不做无谓的两遍编码。
    # ★ 注意：这一支**不能**把后面的纹理那一支挡掉。
    #   它们治的是两件独立的事，一个片子可能两样都要修。
    #   早先这里把 `texture_only` 也一起判掉了，结果"有重复头 **且** 纹理不齐"时
    #   只修了重复头、纹理被跳过（实测：几何 1 种、纹理 ratio 1.306、dedup 跑了、
    #   texture 没跑）。现在两关各自独立，纹理会拿到裁过重复头之后的片段继续处理，
    #   并把两边的说明合并起来（不再互相覆盖）。
    if not harmonize_info.get("applied") and not harmonize_info.get("texture_only") \
            and seam_stats:
        try:
            from core import seamfix as _sf
            tres = _sf.texture_report(seam_stats)
            if (tres.get("texture_ratio") or 1.0) > 1.25:
                hres2 = await _sf.harmonize(
                    clips, os.path.join(work_dir, "texturefix"),
                    scene_ids=scene_ids, do_color=False, do_trim=False,
                    do_texture=True)
                if hres2.get("ok") and hres2.get("out"):
                    clips = hres2["out"]
                    seam_stats = [await _sf.analyze_clip(c) for c in clips]
                    harmonize_info = {
                        # 保留前一步（可能跑过的"仅裁重复头"）的结论与说明，
                        # 不要覆盖掉 —— 不然用户就看不到重复头被裁过。
                        **harmonize_info,
                        "applied": False,      # 几何没动
                        "texture_only": True,
                        "notes": list(harmonize_info.get("notes") or []) + hres2["notes"],
                        "warnings": list(harmonize_info.get("warnings") or []),
                        "texture": hres2.get("texture"),
                    }
                    logger.info("仅纹理统一：%s", "；".join(hres2["notes"])[:300])
        except Exception as e:
            logger.warning("纹理统一失败（继续用原片拼接）：%s", e)

    durs = [await _probe_duration(c) for c in clips]
    # 逐切点决定"硬切还是溶解"，并对转场时长做钳制（不允许吃掉半段）
    cuts: List[float] = [0.0] * len(clips)
    for i in range(1, len(clips)):
        t = transitions[i] if i < len(transitions) else {}
        sec = float(t.get("seconds") or 0.0)
        if (t.get("type") or "none") == "none":
            sec = 0.0
        sec = max(0.0, min(sec, (durs[i] or 5.0) * 0.5, (durs[i - 1] or 5.0) * 0.5))
        cuts[i] = round(sec, 3)

    # 「这一处接缝用什么转场」：`fade`（溶解）或 `fadeblack`（黑场）。
    # 默认 fade；换场处理那一层会把需要黑场的接缝改成 fadeblack。
    kinds: List[str] = ["fade"] * len(clips)
    # 地点字幕卡文本（按片段下标），空串表示这一处不打卡
    cards: List[str] = [""] * len(clips)

    # ══════════════════════════════════════════════════════════════
    # ★★ 实测修正：语义说"硬切"，但量出来这个切点很硬 → 换成溶解
    # ══════════════════════════════════════════════════════════════
    # 依据 `tests/experiment_transitions.py` 在线上真实片段上的实测：
    #   硬切的跳变是**该片段自身最剧烈一帧的 3.86 倍**（一眼就跳），
    #   一个 0.4 秒溶解就能压到 **0.96 倍**（比片子自己的正常运动还平缓）。
    # 所以问题不在"硬切"本身，而在"在硬得要命的地方硬切"。
    # 语义层保留（同场景硬切是对的），这一层只做"量一下、太硬就摊薄"。
    softened: List[Dict[str, Any]] = []
    respected: List[int] = []
    # ★ 「量不了」必须和「量出来很自然」分开报。
    #   2026-09-13 事故：图片卡项目（全片零运动）里分母被钳成 1e-6，
    #   每处接缝都算出 22307014.465 倍 → 全部被强行改成 0.9s 溶解。
    #   修好之后它们既不软化、也不该**默不作声** —— 沉默会让人以为"量过、很自然"。
    unmeasurable: List[Dict[str, Any]] = []
    if seam_stats and len(seam_stats) == len(clips):
        try:
            from core import seamfix as _sf
            for i in range(1, len(clips)):
                sem = transitions[i] if i < len(transitions) else {}
                if (sem.get("type") or "none") != "none":
                    continue          # 本来就是溶解，交给上面的时长逻辑
                if sem.get("respect"):
                    # ★ 用户/上层**显式**选择保留硬切 → 不许自适应层把它改成溶解。
                    #   实测教训：`scene_transition="cut"` 曾被这一层静默覆盖成
                    #   0.9s 溶解，成片里"4 处硬切"变成了"4 处溶解"而没人吭声。
                    respected.append(i)
                    continue
                got = _sf.choose_transition(seam_stats[i - 1], seam_stats[i], sem)
                if got.get("measured") == "unmeasurable":
                    unmeasurable.append({"seam": f"{i}→{i+1}",
                                         "why": got.get("why")})
                    continue
                if got.get("measured") == "softened" and float(got.get("seconds") or 0) > 0:
                    sec = max(0.0, min(float(got["seconds"]),
                                       (durs[i] or 5.0) * 0.5, (durs[i - 1] or 5.0) * 0.5))
                    if sec > 0.05:
                        cuts[i] = round(sec, 3)
                        softened.append({"seam": f"{i}→{i+1}",
                                         "seam_ratio": got.get("seam_ratio"),
                                         "seconds": round(sec, 3),
                                         "why": got.get("why")})
        except Exception as e:
            logger.warning("实测转场修正失败（按语义转场继续）：%s", e)
    harmonize_info["softened"] = softened
    harmonize_info["respected_cuts"] = respected
    harmonize_info["unmeasurable"] = unmeasurable

    # ══════════════════════════════════════════════════════════════
    # ★★ 换场处理：亮度突变 → 黑场（dip to black）+ 地点字幕卡
    # ══════════════════════════════════════════════════════════════
    # 用户原话："有的衔接很突兀（突然特别亮、突然特别暗）……
    #            你可以设定转场或者给个毫秒级别的黑屏+字幕说明下一个情景发生地点都行"
    #
    # 这一层放在**最后**（自适应摊薄之后）—— 它是关于"换场"的最终决定，
    # 硬切/溶解/黑场三选一，不该再被别的层改掉。
    #
    # 关键实现选择：**用 xfade 的 `fadeblack`，而不是插入一段独立的黑场片段**。
    #   · `fadeblack` 就是"经过黑"的交叉淡化，视觉上等价于 dip to black；
    #   · 它**不改变时间轴**（仍是一处重叠），所以配音/字幕的绝对时间映射
    #     一个字都不用改 —— 插入独立黑片段会平移后续所有时间，属于自找的坑
    #     （§18.27 那个"声画整体晚 4 秒"就是映射与实际不一致造成的）；
    #   · 地点字幕卡用 `drawtext` 画在**进来的那一段**开头 1.2 秒上：黑场期间
    #     画面本来就是黑的，字先浮出来；画面淡入后字仍在，最后淡出。同样零时间代价。
    #
    # 阈值与依据见 `seamfix.LUMA_JUMP_OBVIOUS`（3 个项目 12 处接缝标定）。
    scene_treat: List[Dict[str, Any]] = []
    for i in range(1, len(clips)):
        # ★★ 这里**不能**要求 `cuts[i] > 0`。
        #   这一层要治的恰恰是"本来硬切的换场"（实测 caa9904f 的换场全是硬切，
        #   `cuts[i] == 0`）—— 写了一版 `if cuts[i] <= 0: continue`，
        #   结果**这一层从来没有生效过**（成片里 0 处黑场）。
        #   正确的先后关系是：本层**决定要不要造一处转场**，
        #   所以它必须能看到 cuts==0 的接缝；决定 dip 之后再把 cuts[i] 设成重叠时长。
        if i >= len(seam_stats):
            continue
        _sid_a = str(scene_ids[i - 1]) if i - 1 < len(scene_ids) else ""
        _sid_b = str(scene_ids[i]) if i < len(scene_ids) else ""
        _cross = bool(_sid_a and _sid_b and _sid_a != _sid_b)
        try:
            from core import seamfix as _sf2
            _tr = _sf2.choose_scene_treatment(
                seam_stats[i - 1], seam_stats[i], transitions[i] if i < len(transitions) else {},
                mode=scene_transition_mode, cross_scene=_cross)
        except Exception as e:
            logger.warning("换场处理判定失败（保持原转场）：%s", e)
            continue
        if not _cross:
            continue
        scene_treat.append({"seam": f"{i}→{i+1}", **_tr})
        if _tr.get("treatment") == "dip":
            # fadeblack 太短等于硬切，给一档下限
            cuts[i] = round(max(0.3, min(float(_tr.get("seconds") or 0.5),
                                         (durs[i] or 5.0) * 0.5,
                                         (durs[i - 1] or 5.0) * 0.5)), 3)
            kinds[i] = "fadeblack"
            if scene_card and _tr.get("card") and i < len(card_texts) and card_texts[i]:
                cards[i] = card_texts[i]
    harmonize_info["scene_treatment"] = scene_treat

    # ── 亮度色调桥接：**第三种**换场处理（只在入场镜的头部做渐变校正）──
    #
    # 为什么放在这里而不是 `harmonize`：处理方式是**按接缝**定的，而接缝信息
    # （哪两段相邻、是不是跨场景、实测亮度差）都在这一层才齐。
    # `cuts[i]` 保持 0 —— 桥接**不改时间轴**，所以字幕/配音的时间映射
    # 完全不受影响（历史上被"转场时长偷偷变长"坑过一次，见 compose 里
    # `_cuts_actual` 那段注释）。
    _bridged = 0
    for _st in scene_treat:
        if _st.get("treatment") != "bridge":
            continue
        _br = _st.get("bridge") or {}
        if not _br.get("offsets"):
            continue
        try:
            _i = int(str(_st.get("seam") or "0→0").split("→")[0])
        except Exception:
            continue
        if not (0 < _i < len(clips)):
            continue
        from core import seamfix as _sf3
        _dst = os.path.join(work_dir, f"bridge_{_i:02d}.mp4")
        try:
            _r = await _sf3.apply_head_bridge(
                clips[_i], _dst, offsets=_br.get("offsets"),
                seconds=float(_br.get("seconds") or _sf3.BRIDGE_SECONDS),
                w=w, h=h, fps=fps)
        except Exception as e:
            _r = {"ok": False, "why": f"{type(e).__name__}: {e}"}
        if _r.get("ok"):
            clips[_i] = _dst
            _bridged += 1
            _st["bridge_applied"] = True
        else:
            _st["bridge_applied"] = False
            _st["bridge_skipped"] = str(_r.get("why"))[:160]
            logger.warning("亮度桥接未生效（保持硬切）：%s", _r.get("why"))
    if _bridged:
        harmonize_info["luma_bridge"] = _bridged

    # ── 无溶解：直接 concat demuxer，无损且快 ──
    if all(c == 0.0 for c in cuts):
        listfile = out_path + ".concat.txt"
        with open(listfile, "w", encoding="utf-8") as f:
            for c in clips:
                f.write("file '%s'\n" % str(c).replace("\\", "/").replace("'", "'\\''"))
        rc, err = await _run([
            _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", listfile,
            "-c", "copy", "-movflags", "+faststart", out_path], timeout=900)
        try:
            os.remove(listfile)
        except Exception:
            pass
        if rc != 0 or not os.path.exists(out_path):
            raise RuntimeError(f"硬切拼接失败：{(err or '')[-300:]}")
        total = sum(d for d in durs if d > 0)
        return {"ok": True, "path": out_path, "mode": "concat",
                "durations": durs, "cuts": cuts, "total": round(total, 3),
                "runs": [list(range(len(clips)))], "harmonize": harmonize_info}

    # ── 先分组：溶解点把片段切成若干"硬切片段组" ──
    runs: List[List[int]] = [[0]]
    for i in range(1, len(clips)):
        if cuts[i] > 0:
            runs.append([i])
        else:
            runs[-1].append(i)

    run_files: List[str] = []
    run_durs: List[float] = []
    for ri, idxs in enumerate(runs):
        if len(idxs) == 1:
            src = clips[idxs[0]]
            # 溶解需要音频流，单段也要保证有
            src = await _ensure_audio(src, os.path.join(work_dir, f"_ra{ri}.mp4"), w, h, fps)
        else:
            with_a = []
            for k, ci in enumerate(idxs):
                with_a.append(await _ensure_audio(
                    clips[ci], os.path.join(work_dir, f"_ra{ri}_{k}.mp4"), w, h, fps))
            src = await _concat_run(
                with_a, os.path.join(work_dir, f"_run{ri}.mp4"))
        run_files.append(src)
        run_durs.append(await _probe_duration(src))

    # ── 再在片段组之间做溶解 ──
    #
    # ★ offset 的语义：xfade 的 `offset` 是"在**已合成轨**上，从第几秒开始溶解"。
    #   所以第一次溶解的位置 = 第一个片段组的时长 − 转场时长，
    #   **不是 0**。写成 0 的后果是溶解发生在第 0 秒、第二个片段直接盖掉第一个
    #   （实测成片只有一段的长度，而且看起来"成功"了，很难发现）。
    inputs: List[str] = []
    for c in run_files:
        inputs += ["-i", c]
    parts: List[str] = []
    v_prev, a_prev = "[0:v]", "[0:a]"
    offset = float(run_durs[0] or 0.0)
    # 每个片段组对应的"溶解时长"（该组第一个镜头的 cuts 值）
    run_cut = [0.0] + [cuts[idxs[0]] for idxs in runs[1:]]
    # ══════════════════════════════════════════════════════════════════
    # ★★ 音频**不能**用累积的 acrossfade 链 —— 会死锁，进而把整片截断
    # ══════════════════════════════════════════════════════════════════
    # 线上实测（`tests/repro_merged_short.py`）：6 段各 10.00s 的片段，
    # offset 累加算出来是 58.25s，但产物只有 **10.03s**（≈ 只剩第一段）。
    # ffmpeg 的告警是：
    #     [out_#0:1 @ ...] 100 buffers queued in out_#0:1, something may be wrong.
    # 也就是说**音频那条 acrossfade 链堵住了**（每个 acrossfade 都要等下一路
    # 音频开始，累积 5 层之后缓冲排空不了），最后整个 filtergraph 提前 EOF，
    # **视频也跟着被截断**。这不是"少一点音频"，是整部片子只剩 1/6。
    #
    # 改用「各自延迟到绝对位置 → amix 求和」：
    #   · 每个片段的音频用 adelay 放到它在成片里的绝对起点
    #   · amix 求和（normalize=0，保持电平，后面还有 loudnorm 统一）
    #   · 溶解区间内两路音频自然重叠混音 —— 听感就是交叉淡化
    # 这条路径没有互相等待，不会死锁；长度由最长的输入决定，不会被截断。
    audio_parts: List[str] = []
    amix_labels: List[str] = ["[0:a]"]
    # 前处理链：需要打"地点字幕卡"的片段组，先在它的视频流上画字
    pre_parts: List[str] = []
    for ri, idxs in enumerate(runs):
        if ri == 0:
            continue
        _first = idxs[0]
        _card = cards[_first] if _first < len(cards) else ""
        if _card:
            pre_parts.append(f"[{ri}:v]{_card_drawtext(_card, w, h)}[vin{ri}]")
    for i in range(1, len(run_files)):
        t = max(0.05, min(run_cut[i], (run_durs[i] or 5.0) * 0.5))
        at = max(0.0, offset - t)
        v_out = f"[vx{i}]"
        # 这一处接缝用哪种转场：`fade`（溶解）或 `fadeblack`（黑场 = dip to black）
        _kind = kinds[runs[i][0]] if runs[i] and runs[i][0] < len(kinds) else "fade"
        if _kind not in ("fade", "fadeblack"):
            _kind = "fade"
        v_in = f"[vin{i}]" if any(f"[vin{i}]" in p for p in pre_parts) else f"[{i}:v]"
        parts.append(f"{v_prev}{v_in}xfade=transition={_kind}:duration={t:.3f}:"
                     f"offset={at:.3f}{v_out}")
        # 本段音频的绝对起点 = at（溶解开始时本段就已经进来了）
        delay_ms = int(round(at * 1000))
        lab = f"[ad{i}]"
        audio_parts.append(
            f"[{i}:a]aresample=48000,"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"adelay={delay_ms}|{delay_ms}{lab}")
        amix_labels.append(lab)
        v_prev = v_out
        offset = at + float(run_durs[i] or 0.0)

    if len(amix_labels) == 1:
        # 只有一个片段组：音频直通
        audio_filter = "[0:a]aresample=48000,aformat=sample_fmts=fltp:" \
                       "channel_layouts=stereo[aout]"
    else:
        audio_filter = (";".join(audio_parts) + ";" +
                        "".join(amix_labels) +
                        f"amix=inputs={len(amix_labels)}:duration=longest:"
                        f"dropout_transition=0:normalize=0,"
                        f"aresample=48000[aout]")

    cmd = ([_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error"] + inputs +
           ["-filter_complex", ";".join(pre_parts + parts) + ";" + audio_filter,
            "-map", v_prev, "-map", "[aout]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart", out_path])
    rc, err = await _run(cmd, timeout=1800)
    if rc != 0 or not os.path.exists(out_path):
        raise RuntimeError(f"带转场拼接失败：{(err or '')[-400:]}")
    total = sum(d for d in durs if d > 0) - sum(cuts)
    return {"ok": True, "path": out_path, "mode": "xfade",
            "durations": durs, "cuts": cuts, "total": round(total, 3),
            "runs": runs, "harmonize": harmonize_info}


async def mux_film(
    picture: str, audio: Optional[str], out_path: str, *,
    bgm: str = "", bgm_volume: float = 0.18,
    duck: bool = True, loudness_norm: bool = True,
    total_duration: float = 0.0, duck_threshold: float = 0.08,
) -> Dict[str, Any]:
    """把画面、配音、BGM 合成最终成片。

    ★ BGM **自动避让人声**（sidechaincompress）。
      MoneyPrinterTurbo 完全没有这个能力 —— 它只是把两条音轨按固定音量
      `amix` 相加。结果就是"人一说话，音乐糊上来压住对白"，
      用户说的"特别不协调"里有它一份。

    ★ 整体响度归一化（EBU R128，单遍 loudnorm）。
      不同 TTS 厂商的输出电平差异很大（实测 MiniMax 与 Edge 能差近 10dB），
      多角色配音必然一段响一段轻 —— 不归一化就是"不协调"。

    滤波器图（一次成型，不做多遍编码）：
        人声 ── aresample → stereo ─┬──────────────────────────────┐
                                    └→ asplit → [key]              │
        BGM  ── aresample → volume → afade in/out ─→ sidechaincompress(key) ─┐
                                                                             ├→ amix → loudnorm → [aout]
                                                          人声（原始电平）───┘
    """
    if not os.path.exists(picture):
        raise ValueError(f"画面文件不存在：{picture}")
    has_voice = bool(audio) and os.path.exists(audio)
    has_bgm = bool(bgm) and os.path.exists(bgm)
    warns: List[str] = []
    # ★★ 画面**自带的音轨**也要当成"人声"看待（2026-09-13 真实事故）。
    #   `compose` 调本函数时 `audio=""`，因为按设计"每镜的配音已经烧进画面了"。
    #   而旧代码 `has_voice=False` 直接走"无声"分支 —— 用 `-an` **把画面自带的
    #   音轨整个丢掉**，成片实测只剩一条视频流（静音）。
    #   响度归一化那一步同样丢：`loudness.mp4` 里连音轨都没有。
    pic_voice = False
    if not has_voice:
        try:
            _rc, _info = await _run([_ffmpeg(), "-hide_banner", "-i", picture], timeout=60)
            pic_voice = "Audio:" in (_info or "")
        except Exception:
            pic_voice = False

    if not has_voice and not has_bgm:
        if not pic_voice:
            warns.append("画面没有音轨、也没有配音与 BGM，成片将没有声音。")
            cmd = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", picture,
                   "-map", "0:v", "-c:v", "copy", "-an", "-movflags", "+faststart"]
            if total_duration and total_duration > 0:
                cmd += ["-t", f"{float(total_duration):.3f}"]
            cmd.append(out_path)
            rc, err = await _run(cmd, timeout=900)
            if rc != 0 or not os.path.exists(out_path):
                raise RuntimeError(f"输出失败：{(err or '')[-300:]}")
            return {"ok": True, "path": out_path, "ducked": False, "normalized": False,
                    "warnings": warns}
        # 保留画面自带的音轨；要求归一化就把它归一化
        _af = "aresample=48000,aformat=channel_layouts=stereo"
        if loudness_norm:
            _af += ",loudnorm=I=-16:TP=-2.0:LRA=11"
        cmd = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", picture,
               "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy",
               "-af", _af, "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
               "-movflags", "+faststart"]
        if total_duration and total_duration > 0:
            cmd += ["-t", f"{float(total_duration):.3f}"]
        cmd.append(out_path)
        rc, err = await _run(cmd, timeout=1800)
        if rc != 0 or not os.path.exists(out_path):
            logger.warning("画面自带音轨的处理失败，直接复制：%s", (err or "")[-200:])
            cmd2 = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", picture,
                    "-map", "0", "-c", "copy", "-movflags", "+faststart", out_path]
            rc2, err2 = await _run(cmd2, timeout=900)
            if rc2 != 0 or not os.path.exists(out_path):
                raise RuntimeError(f"输出失败：{(err2 or '')[-300:]}")
        return {"ok": True, "path": out_path, "ducked": False,
                "normalized": bool(loudness_norm), "warnings": warns}

    cmd = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", picture]
    if has_voice:
        cmd += ["-i", audio]
    if has_bgm:
        cmd += ["-stream_loop", "-1", "-i", bgm]

    dur = float(total_duration or 0) or (await _probe_duration(picture)) or 60.0
    parts: List[str] = []
    # 人声来源：显式配音文件优先；没有就用**画面自带的音轨**（第 0 路输入）
    voice_ref = f"[{1}:a]" if has_voice else "[0:a]"
    b_idx = 2 if has_voice else 1                 # BGM 的输入序号

    if has_voice or pic_voice:
        # ★ 先把人声响度归一化，**再**拿去做侧链。
        #   为什么必须这样：sidechaincompress 是按"侧链信号电平"决定压多少的，
        #   而不同 TTS 厂商的输出电平差很多（实测 MiniMax 与 Edge 差近 10dB）。
        #   不先归一化，同一组参数在 A 角色身上压 16dB、在 B 角色身上可能只压 3dB
        #   —— 表现就是"有时音乐压下去了、有时没有"，很难查。
        if loudness_norm:
            parts.append(f"{voice_ref}aresample=48000,"
                         f"aformat=channel_layouts=stereo,"
                         f"loudnorm=I=-16:TP=-2.0:LRA=11[voc_raw]")
        else:
            parts.append(f"{voice_ref}aresample=48000,"
                         f"aformat=channel_layouts=stereo[voc_raw]")
    if has_bgm:
        fo = max(0.0, dur - 2.5)
        parts.append(f"[{b_idx}:a]aresample=48000,aformat=channel_layouts=stereo,"
                     f"volume={float(bgm_volume):.3f},"
                     f"afade=t=in:st=0:d=1.5,afade=t=out:st={fo:.2f}:d=2.5[bgm_raw]")

    if (has_voice or pic_voice) and has_bgm and duck:
        # 人声复制一路当侧链键，另一路参与最终混音
        parts.append("[voc_raw]asplit=2[voc][key]")
        # ★ sidechaincompress 参数已在本机实测标定（tests/probe_sidechain.py
        #   与 threshold 扫描，全部为真实测量值）：
        #     threshold=0.05, ratio=20, level_sc=1  → 压降  0.0 dB（等于没做）
        #     threshold=0.05, ratio=20, level_sc=8  → 压降  3.1 dB
        #     threshold=0.02 → 26.2 dB（过重，音乐会"消失"，有抽气感）
        #     threshold=0.04 → 20.7 dB
        #     threshold=0.08 → 15.0 dB  ← 采用（落在行业常用的 12-18dB 区间）
        #     threshold=0.15 →  9.8 dB（偏轻，人声容易被音乐盖住）
        #     threshold=0.25 →  5.6 dB
        #   **`level_sc` 才是最关键的旋钮**：只调 threshold/ratio 而 level_sc=1
        #   几乎压不动 —— 这是"写了 ducking 却听不出效果"的根因。
        #   所有取值下静默段实测都稳定在 -33.0 dB、完全不被误压。
        parts.append("[bgm_raw][key]sidechaincompress="
                     f"threshold={float(duck_threshold):.4f}:ratio=20:"
                     "attack=20:release=300:"
                     "makeup=1:level_sc=8:mode=downward[bgm_ducked]")
        # normalize=0 必须显式写：amix 默认 normalize=1 会把每路乘 1/N，
        # 实测让人声无端掉 5.6dB —— 这是"一加 BGM 人声就变小"的根因。
        parts.append("[voc][bgm_ducked]amix=inputs=2:duration=first:"
                     "dropout_transition=0:normalize=0[premix]")
    elif (has_voice or pic_voice) and has_bgm:
        parts.append("[voc_raw][bgm_raw]amix=inputs=2:duration=first:"
                     "dropout_transition=0:normalize=0[premix]")
    elif has_voice or pic_voice:
        parts.append("[voc_raw]anull[premix]")
    else:
        parts.append("[bgm_raw]anull[premix]")

    if loudness_norm:
        # 终混再过一次 loudnorm，把成片整体拉到 -16 LUFS（短视频平台常用目标）
        parts.append("[premix]loudnorm=I=-16:TP=-1.5:LRA=11:print_format=summary[aout]")
    else:
        parts.append("[premix]anull[aout]")

    cmd += ["-filter_complex", ";".join(parts),
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-shortest", "-movflags", "+faststart"]
    if total_duration and total_duration > 0:
        cmd += ["-t", f"{float(total_duration):.3f}"]
    cmd.append(out_path)

    rc, err = await _run(cmd, timeout=1800)
    if rc != 0 or not os.path.exists(out_path):
        logger.warning("混音失败，走简化路径：%s", (err or "")[-300:])
        return await _mux_simple(picture, audio, bgm, out_path,
                                 bgm_volume, total_duration, warns)
    return {"ok": True, "path": out_path,
            "ducked": bool((has_voice or pic_voice) and has_bgm and duck),
            "normalized": bool(loudness_norm), "warnings": warns}


async def _mux_simple(picture: str, audio: Optional[str], bgm: str, out_path: str,
                      bgm_volume: float, total_duration: float,
                      warns: List[str]) -> Dict[str, Any]:
    cmd = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", picture]
    if audio and os.path.exists(audio):
        cmd += ["-i", audio]
    if bgm and os.path.exists(bgm):
        cmd += ["-stream_loop", "-1", "-i", bgm]
    cmd += ["-map", "0:v"]
    if audio and os.path.exists(audio) and bgm and os.path.exists(bgm):
        cmd += ["-filter_complex",
                f"[1:a]aresample=48000[v];[2:a]aresample=48000,volume={bgm_volume:.3f}[b];"
                f"[v][b]amix=inputs=2:duration=first:normalize=0[a]",
                "-map", "[a]", "-shortest"]
    elif audio and os.path.exists(audio):
        cmd += ["-map", "1:a"]
    elif bgm and os.path.exists(bgm):
        cmd += ["-filter_complex", f"[1:a]aresample=48000,volume={bgm_volume:.3f}[a]",
                "-map", "[a]", "-shortest"]
    cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    if total_duration and total_duration > 0:
        cmd += ["-t", f"{float(total_duration):.3f}"]
    cmd += ["-movflags", "+faststart", out_path]
    rc, err = await _run(cmd, timeout=1800)
    if rc != 0 or not os.path.exists(out_path):
        raise RuntimeError(f"合轨失败：{(err or '')[-400:]}")
    warns.append("已走简化混音（BGM 未避让人声、未做响度归一化）")
    return {"ok": True, "path": out_path, "ducked": False, "warnings": warns}
