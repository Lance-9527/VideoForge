# -*- coding: utf-8 -*-
"""VideoForge · 接缝处理（Seam Fix）

═══════════════════════════════════════════════════════════════════
要解决的问题
═══════════════════════════════════════════════════════════════════
用户原话：
  "把多个分镜视频整合成为一个完整的衔接良好丝滑的视频你现在还是做不到"
  "别的 agent 一次性生成的视频连贯性很强且看不出 ai 的痕迹"

先用眼睛看线上真实素材（`tests/_visual/seams.png`）+ 用数字量
（`tests/analyze_seams.py`，实测平均 **seam_ratio = 5.74**，目标 < 1.6），
把"一眼AI"拆成**五个独立的物理原因**，逐个解决：

  ① **画幅/编码不统一**（最严重，而且是硬 bug）
     线上真实项目的 4 个片段是 1280x720 / 1920x1080 / 1080x1080 / 1080x1080，
     fps 还有 25 与 24 混用。而拼接口在"全部硬切"时直接走
     concat demuxer + `-c copy` —— 它要求所有输入编码参数**完全一致**。
     实测后果：ffmpeg **返回成功**，产物却是 1280x720 / 25.25fps、
     总长 22.33s（源 23.28s，**静默丢了 0.95 秒**）。
     这种文件播放时接缝处会卡顿/花屏/跳一下 —— 因为 SPS/PPS 对不上。
     → `plan_target_geometry` + `normalize_clip`：**先统一，再拼接**。

  ② **模型首帧"沉降" / 尾帧"定住"**
     单段生成常见开头几帧几乎不动（模型从输入首帧慢慢启动）、结尾定住。
     拼起来观感是"动着动着停住 → 换一段又慢慢启动"。
     实测真实片段 head_settle 低到 **0.19**（头 0.4 秒只有正常运动量的 19%）。
     → `detect_settle_windows` + 裁掉沉降/定住的那几帧。

  ③ **色调漂移**
     每段各自成片，色温/反差略有不同，接缝处出现颜色跳变。
     实测相邻段 RGB 平均差 7.2~9.7。
     → `plan_color_match`：**只在同一场景内**做温和匹配（限幅），
       跨场景不动 —— 白天和夜晚本来就该不一样，强行拉平反而是错的。

  ④ **运动节奏差**
     实测相邻段"内部运动量"能差 4 倍（4.58 vs 19.97），接缝像换了速度。
     → 报告出来（`pace_ratio`），交给转场长度去吸收；不擅自变速。

  ⑤ **两段根本不是同一个世界**（最根本，只能在生成端解决）
     第 1 镜是 3D 卡通（蓝色马甲圆眼镜），第 2 镜是写实真人；
     原因是这个项目 6 个分镜用了 **3 种不同模型**
     （video-01 × 2 / MiniMax-Hailuo-02 × 2 / kling-1.6 × 2）。
     不同模型 = 不同画风/色彩科学/运动风格，**任何后期都补不回来**。
     → 这一条不在本模块，见 `core/consistency.py` 与项目级"统一生成条件"。

本模块只做 ①~④（对已有片段能做的全部事情），并且**每一项都留下数字**：
`harmonize()` 返回 before/after 的 seam_ratio，让"变好了多少"可核对。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("videoforge.seamfix")

# 分析用降采样：够看出构图/色调/运动，又快
ANALYZE_W, ANALYZE_H = 160, 90
ANALYZE_FPS = 12.0

# 纹理分析用的分辨率。★ 为什么比运动分析高得多：
# 颗粒/微细节是**高频**信息，降到 160x90 会被平均掉 —— 那样量到的
# 只是"构图复杂度"，不是纹理。512x288 能留住中高频，才量得出"颗粒感差多少"。
TEXTURE_W, TEXTURE_H = 512, 288
TEXTURE_FRAMES = 6

# 沉降/定住的判定：头尾这段时间的运动量低于整体中位数的这个比例，就算"定住了"
SETTLE_RATIO = 0.55
MAX_HEAD_TRIM = 0.60      # 最多裁掉开头这么多秒（再多就是在删内容了）
MAX_TAIL_TRIM = 0.40
MIN_KEEP_RATIO = 0.60     # 裁完至少保留原长的这个比例，否则不裁

# 调色匹配的限幅：只修"漂移"，不重画风格
COLOR_GAIN_LIMIT = 0.10   # 增益最多 ±10%
COLOR_OFF_LIMIT = 12.0    # 偏移最多 ±12（0..255）

# 画幅不匹配时的告警阈值（裁剪掉的比例）
CROP_WARN_RATIO = 0.18


def _ffmpeg() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


async def _run(cmd: List[str], timeout: int = 900) -> Tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return -1, "timeout"
    return proc.returncode or 0, (out or b"").decode("utf-8", "replace") + \
        (err or b"").decode("utf-8", "replace")


async def probe(path: str) -> Dict[str, Any]:
    """读规格：宽高、时长、fps、有无音轨。"""
    rc, txt = await _run([_ffmpeg(), "-hide_banner", "-i", path], timeout=90)
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", txt)
    dur = (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))) if m else 0.0
    v = re.search(r"Video:.*?,\s*(\d+)x(\d+)", txt)
    f = re.search(r"(\d+(?:\.\d+)?)\s*fps", txt)
    s = re.search(r"SAR\s+(\d+):(\d+)", txt)
    return {
        "duration": round(dur, 3),
        "w": int(v.group(1)) if v else 0,
        "h": int(v.group(2)) if v else 0,
        "fps": float(f.group(1)) if f else 0.0,
        "has_audio": "Audio:" in txt,
        "sar": f"{s.group(1)}:{s.group(2)}" if s else "1:1",
    }


# ═══════════════════════════════════════════════════════════════
# 一、把片段"看"成数字：运动曲线 + 色调统计
# ═══════════════════════════════════════════════════════════════

async def _gray_frames(path: str, w: int = ANALYZE_W, h: int = ANALYZE_H):
    """抽成 (N,H,W) float32 灰度。

    ★ 用**临时文件**而不是管道：Windows 上 ffmpeg 写 pipe 偶发死锁。
    ★ 文件名必须唯一（tempfile），否则同目录两段片段并发分析会互相踩。
    """
    import numpy as np
    import tempfile
    fd, raw = tempfile.mkstemp(suffix=".__f.raw")
    os.close(fd)
    try:
        rc, txt = await _run([_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                              "-i", path,
                              "-vf", f"fps={ANALYZE_FPS},scale={w}:{h},format=gray",
                              "-f", "rawvideo", "-pix_fmt", "gray", raw], timeout=600)
        if rc != 0 or not os.path.exists(raw):
            logger.warning("抽帧失败 %s: %s", os.path.basename(path), (txt or "")[-200:])
            return np.zeros((0, h, w), dtype=np.float32)
        buf = np.fromfile(raw, dtype=np.uint8)
    finally:
        try:
            os.remove(raw)
        except Exception:
            pass
    n = len(buf) // (w * h)
    if n <= 0:
        return np.zeros((0, h, w), dtype=np.float32)
    return buf[:n * w * h].reshape(n, h, w).astype(np.float32)


async def _texture_energy(path: str, n: int = TEXTURE_FRAMES) -> float:
    """量这段画面的**高频纹理能量**（颗粒感/微细节的多少）。

    做法：取几帧、缩到 512x288、转灰度，算离散拉普拉斯
        lap = 4·c − 上 − 下 − 左 − 右
    然后取 lap 的标准差。标准差越大 = 高频越多 = 颗粒/细节越丰富。

    ★ 为什么用这个当"纹理不统一"的代理指标：
      观众说"这两段一看就不是一个机器拍的"，一半来自**微纹理不一致** ——
      一段有胶片颗粒、另一段像塑料一样光滑。这个量能直接把它变成数字。
      拉普拉斯是标准的边缘/高频检测算子，比"看锐度"客观。
    """
    import numpy as np
    import tempfile
    fd, raw = tempfile.mkstemp(suffix=".__tex.raw")
    os.close(fd)
    try:
        rc, txt = await _run([
            _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", path,
            "-vf", (f"fps=1,scale={TEXTURE_W}:{TEXTURE_H}:"
                    f"force_original_aspect_ratio=increase,"
                    f"crop={TEXTURE_W}:{TEXTURE_H},format=gray"),
            "-frames:v", str(n), "-f", "rawvideo", "-pix_fmt", "gray", raw],
            timeout=300)
        if rc != 0 or not os.path.exists(raw):
            logger.info("纹理取样失败 %s: %s", os.path.basename(path), (txt or "")[-160:])
            return 0.0
        buf = np.fromfile(raw, dtype=np.uint8)
    finally:
        try:
            os.remove(raw)
        except Exception:
            pass
    k = TEXTURE_W * TEXTURE_H
    c = len(buf) // k
    if c <= 0:
        return 0.0
    f = buf[:c * k].reshape(c, TEXTURE_H, TEXTURE_W).astype(np.float32)
    lap = (4.0 * f[:, 1:-1, 1:-1] - f[:, :-2, 1:-1] - f[:, 2:, 1:-1]
           - f[:, 1:-1, :-2] - f[:, 1:-1, 2:])
    return round(float(lap.std()), 3)


async def _rgb_stats(path: str) -> Dict[str, List[float]]:
    """逐通道均值/标准差（多点采样）。用来做颜色匹配。"""
    import numpy as np
    import tempfile
    fd, raw = tempfile.mkstemp(suffix=".__rgb.raw")
    os.close(fd)
    try:
        await _run([_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", path,
                    "-vf", f"fps=3,scale={ANALYZE_W}:{ANALYZE_H},format=rgb24",
                    "-f", "rawvideo", "-pix_fmt", "rgb24", raw], timeout=600)
        if not os.path.exists(raw):
            return {"mean": [128.0] * 3, "std": [32.0] * 3}
        buf = np.fromfile(raw, dtype=np.uint8)
    finally:
        try:
            os.remove(raw)
        except Exception:
            pass
    k = ANALYZE_W * ANALYZE_H * 3
    c = len(buf) // k
    if c <= 0:
        return {"mean": [128.0] * 3, "std": [32.0] * 3}
    a = buf[:c * k].reshape(c, ANALYZE_H, ANALYZE_W, 3).astype(np.float32)
    # ★ 除了整段均值/标准差，还要给出**首帧与尾帧**的逐通道均值：
    #   "亮度色调桥接"要修的是接缝两侧（上一镜尾帧 ↔ 本镜首帧），
    #   拿整段均值去修会把新场景本来的色调也拉歪。
    #   这里复用同一次解码的采样序列（fps=3，第一点≈片头、最后一点≈片尾），
    #   **不额外增加一次解码**。
    return {"mean": [round(float(a[:, :, :, i].mean()), 3) for i in range(3)],
            "std": [round(float(a[:, :, :, i].std()), 3) for i in range(3)],
            "first": [round(float(a[0, :, :, i].mean()), 3) for i in range(3)],
            "last": [round(float(a[-1, :, :, i].mean()), 3) for i in range(3)]}


def head_settle_seconds(mc, median: float, *, ratio: float = 0.30,
                        max_head: float = 1.0, keep_ratio: float = 0.7,
                        severe: float = 0.40, head_window: float = 0.40) -> Dict[str, float]:
    """量一个片段的**起幅**有多静，并给出"该裁掉多少秒死帧"（纯函数，可单测）。

    ★★ 为什么需要它（2026-09-13，换场默认改硬切之后暴露出来的真回归）：
      I2V 模型（首帧是我们给的场景图）常常**前几帧几乎不动** —— 模型在从静图里"苏醒"。
      在"换场用溶解"的默认下，这段死的起幅被溶解盖住了（实测成片里 1.41× 全片中位）；
      可默认改成**硬切**之后就露出来了：实测同一部片子第 5 镜起幅只有 **0.26×**，
      观众的观感就是"切过来之后画面卡住半秒" —— 目标点名的"运动停顿重启"。
      治它的办法不是转场（用户已经选了硬切），而是**把死帧裁掉**。

    返回 `{"head_settle", "head_trim", "severe"}`：
      · `head_settle` = 头 0.4 秒的平均运动 ÷ 该片段运动中位（越小越静）
      · `head_trim`   = 扫描出的"连续不动"时长（受 `max_head` 与 `keep_ratio` 约束）
      · 只有 `head_settle < severe` 时才真的给裁剪量 —— 正常起幅不要动它

    `max_head` 定 1.0s 是**实测定的**：项目 caa9904f 第 1 镜的静止段实测 0.83s，
    上限 0.6s 时只裁掉 0.583s，成片里那一段的起幅仍是 0.38×（仍偏静）；
    放宽到 1.0s 才能把整段死帧吃掉。
    """
    # ⚠ 不能写 `mc or []`：传进来的可能是 **numpy 数组**，
    #   而数组的真值判断会抛 "truth value of an array ... is ambiguous"。
    #   实测踩过：这个异常被 compose 的 try/except 吞掉 → "裁死帧"永远不触发，
    #   表现就是"功能写了但一次都没生效"。
    mc = [float(x) for x in (mc if mc is not None else [])]
    n = len(mc)
    if n < 4 or median <= 1e-6:
        return {"head_settle": 1.0, "head_trim": 0.0, "severe": 0.0}
    k = max(1, int(round(head_window * ANALYZE_FPS)))
    hs = float(sum(mc[:min(k, n)]) / max(1, min(k, n))) / median
    if hs >= severe:
        return {"head_settle": round(hs, 3), "head_trim": 0.0, "severe": 0.0}
    thr = median * ratio
    max_n = int(max_head * ANALYZE_FPS)
    hn = 0
    while hn < min(max_n, n) and mc[hn] < thr:
        hn += 1
    head = hn / ANALYZE_FPS
    total = n / ANALYZE_FPS
    if head > total * (1.0 - keep_ratio):
        head = total * (1.0 - keep_ratio)
    return {"head_settle": round(hs, 3), "head_trim": round(max(0.0, head), 3),
            "severe": 1.0}


def tail_settle_seconds(mc, median: float, *, ratio: float = 0.30,
                        max_tail: float = 1.0, keep_ratio: float = 0.7,
                        severe: float = 0.40, head_window: float = 0.40) -> Dict[str, float]:
    """尾巴上的"定住"（与 `head_settle_seconds` 对称）。

    ★ 实测需求（项目 `d5815c1e`，2026-09-13）：5 个镜头**全都在**
      `t=4.67s`（约 5.0s 片长的倒数 0.33s）起定住不动 —— 模型的收尾习惯。
      只裁头不裁尾，这些"结尾卡住"就永远留在成片里。
    """
    mc = [float(x) for x in (mc if mc is not None else [])]
    n = len(mc)
    if n < 4 or median <= 1e-6:
        return {"tail_settle": 1.0, "tail_trim": 0.0, "severe": 0.0}
    k = max(1, int(round(head_window * ANALYZE_FPS)))
    ts = float(sum(mc[-min(k, n):]) / max(1, min(k, n))) / median
    if ts >= severe:
        return {"tail_settle": round(ts, 3), "tail_trim": 0.0, "severe": 0.0}
    thr = median * ratio
    max_n = int(max_tail * ANALYZE_FPS)
    tn = 0
    while tn < min(max_n, n) and mc[-1 - tn] < thr:
        tn += 1
    tail = tn / ANALYZE_FPS
    total = n / ANALYZE_FPS
    if tail > total * (1.0 - keep_ratio):
        tail = total * (1.0 - keep_ratio)
    return {"tail_settle": round(ts, 3), "tail_trim": round(max(0.0, tail), 3),
            "severe": 1.0}


async def probe_settle(path: str) -> Dict[str, float]:
    """抽一次帧：同时给出**头/尾**该裁掉多少秒（以及各自的"静"程度）。"""
    import numpy as np
    try:
        f = await _gray_frames(path)
    except Exception as e:
        logger.warning("沉降测量失败（不裁）：%s", e)
        return {"head_settle": 1.0, "head_trim": 0.0, "tail_settle": 1.0,
                "tail_trim": 0.0, "seconds": 0.0}
    if len(f) < 4:
        return {"head_settle": 1.0, "head_trim": 0.0, "tail_settle": 1.0,
                "tail_trim": 0.0, "seconds": 0.0}
    mc = np.abs(np.diff(f, axis=0)).mean(axis=(1, 2))
    med = float(np.median(mc))
    out = {}
    out.update(head_settle_seconds(mc, med))
    ts = tail_settle_seconds(mc, med)
    out["tail_settle"] = ts["tail_settle"]
    out["tail_trim"] = ts["tail_trim"]
    # ★ 顺手把"中间的冻结段"也报出来 —— **复用同一条运动曲线，不额外抽帧**。
    #   上层用它决定"剪不剪"（`cut_stalls`），不剪时也用来提示用户有这个选项。
    try:
        stat = {"motion_curve": [float(x) for x in mc], "motion_median": med,
                "duration": len(mc) / ANALYZE_FPS}
        mid = []
        for w in stall_windows(stat, ratio=0.25, min_seconds=1.0):
            a = float(w["at"])
            b = a + float(w["seconds"])
            if a < 0.6 or b > stat["duration"] - 0.6:
                continue
            mid.append({"at": round(a, 3), "seconds": round(b - a, 3)})
        out["mid_stalls"] = mid
        out["mid_stall_seconds"] = round(sum(x["seconds"] for x in mid), 2)
    except Exception:
        out["mid_stalls"] = []
        out["mid_stall_seconds"] = 0.0
    out["motion_median"] = round(med, 4)
    out["seconds"] = round(len(f) / ANALYZE_FPS, 2)
    return out


async def probe_head_settle(path: str) -> Dict[str, float]:
    """兼容旧名（只关心头部时用）；等价于 `probe_settle`。"""
    return await probe_settle(path)


async def probe_stall_mid_windows(path: str, *, min_seconds: float = 1.0,
                                  ratio: float = 0.25,
                                  edge: float = 0.6) -> List[Dict[str, Any]]:
    """找出片段**中间**真正"几乎不动"的长窗口（用于"剪掉冻结段"）。

    ★ 与 `stall_windows` 的分工：
      · 头尾的静止由 `head_settle_seconds` / `tail_settle_seconds` 用**裁头裁尾**处理；
      · 这里只报**中间**的（避开两端 `edge` 秒），且只报 ≥`min_seconds` 的 ——
        剪中间是**动刀**（画面会出现一次跳切），门槛要比"报警"高。

    实测（`caa9904f` 第 1 镜重生成的素材）：窗口内运动 0.268、本段中位 3.80（=7%），
    持续 **4.42 秒** —— 是真冻结、不是阈值误报（**先自查过这一点才敢动刀**）。
    另外实测：同一提示词重生成**不会**让这种停顿消失（2.92s → 4.42s，反而更长），
    所以它是**内容/提示词驱动**的，管线只能"剪掉"或"sorry 不管"。
    """
    import numpy as np
    try:
        f = await _gray_frames(path)
    except Exception as e:
        logger.warning("停顿探测失败：%s", e)
        return []
    if len(f) < 6:
        return []
    mc = np.abs(np.diff(f, axis=0)).mean(axis=(1, 2))
    med = float(np.median(mc))
    total = len(mc) / ANALYZE_FPS
    stat = {"motion_curve": [float(x) for x in mc], "motion_median": med,
            "duration": total}
    out: List[Dict[str, Any]] = []
    for w in stall_windows(stat, ratio=ratio, min_seconds=min_seconds):
        a, b = float(w["at"]), float(w["at"]) + float(w["seconds"])
        if a < edge or b > total - edge:
            continue                      # 头尾交给裁头裁尾，不在这里动
        out.append({"at": round(a, 3), "seconds": round(b - a, 3)})
    merged: List[Dict[str, Any]] = []
    for w in out:
        if merged and w["at"] - (merged[-1]["at"] + merged[-1]["seconds"]) < 0.5:
            merged[-1]["seconds"] = round(w["at"] + w["seconds"] - merged[-1]["at"], 3)
        else:
            merged.append(dict(w))
    return merged


def kept_ranges(duration: float,
                removals: List[Any]) -> List[Tuple[float, float]]:
    """`[0, duration]` 去掉若干段之后剩下的区间（纯函数，可单测）。

    `removals` 允许乱序/重叠；返回**有序、互不重叠**的保留区间。
    全被删光时返回空列表（调用方要兜底，别输出 0 秒的片子）。
    """
    rms = sorted((max(0.0, float(a)), min(float(duration), float(b)))
                 for a, b in (removals or []) if float(b) > float(a))
    out: List[Tuple[float, float]] = []
    cursor = 0.0
    for a, b in rms:
        if a > cursor + 1e-6:
            out.append((round(cursor, 3), round(a, 3)))
        cursor = max(cursor, b)
    if cursor < float(duration) - 1e-6:
        out.append((round(cursor, 3), round(float(duration), 3)))
    return [(a, b) for a, b in out if b - a > 0.05]


def map_after_removals(t: float, removals: List[Any]) -> float:
    """把"删掉若干段之前"的时刻映射成"删掉之后"的时刻（纯函数）。"""
    shift = 0.0
    for a, b in sorted((float(a), float(b)) for a, b in (removals or [])):
        if t >= b:
            shift += (b - a)
        elif t > a:
            shift += (t - a)          # 落在被删区间内 → 折到区间起点
    return max(0.0, t - shift)


async def analyze_clip(path: str) -> Dict[str, Any]:
    """一个片段的全部可测指标。

    ★ `_first` / `_last` 是**首尾帧的灰度图**（numpy，160x90），
      专门留给 `seam_report` 算真正的"接缝处帧差"。
      它们以 `_` 开头 —— 对外序列化前请用 `strip_private()` 去掉，
      别把 14400 个浮点数塞进 JSON。
    """
    import numpy as np
    info = await probe(path)
    f = await _gray_frames(path)
    mc = np.abs(np.diff(f, axis=0)).mean(axis=(1, 2)) if len(f) >= 2 else np.zeros(0)
    med = float(np.median(mc)) if len(mc) else 0.0
    out: Dict[str, Any] = {
        "path": path, **info,
        "frames": int(len(f)),
        "motion_median": round(med, 4),
        "rgb": await _rgb_stats(path),
        "texture": await _texture_energy(path),
    }
    if len(f):
        out["_first"] = f[0]
        out["_last"] = f[-1]
        # ★ 头尾各留一小段帧序列，用来做"冻结重演"检测。
        #   `_first`/`_last` 只是单帧，判断"接缝两侧是不是同一帧"够用，
        #   但判断"开头有几帧是重复的"需要一小段序列。
        #   0.6 秒 × 12fps = 7 帧，160x90，代价可以忽略。
        k = max(2, int(round(0.6 * ANALYZE_FPS)))
        out["_head"] = f[:k]
        out["_tail"] = f[-k:]
    if len(mc) >= 8 and med > 1e-6:
        out["motion_curve"] = [round(float(x), 3) for x in mc]
        out["head_settle"] = round(float(mc[:max(2, int(0.4 * ANALYZE_FPS))].mean()) / med, 3)
        out["tail_freeze"] = round(float(mc[-max(2, int(0.4 * ANALYZE_FPS)):].mean()) / med, 3)
    else:
        out["motion_curve"] = [round(float(x), 3) for x in mc]
        out["head_settle"] = out["tail_freeze"] = 1.0
    return out


def strip_private(obj: Any) -> Any:
    """递归去掉 `_` 开头的内部字段（首尾帧灰度图），让结果可以 JSON 序列化。"""
    if isinstance(obj, dict):
        return {k: strip_private(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, list):
        return [strip_private(x) for x in obj]
    return obj


def motion_base(a: Dict[str, Any], b: Dict[str, Any]) -> Optional[float]:
    """接缝比值的**分母**：两侧"自身运动尺度"（相邻帧平均绝对差的中位数）的均值。

    ★ 返回 `None` 表示**这个参照根本不存在**，而不是"很小"。
      实测正常素材的 `motion_median` 在 0.15~10 之间；恰好为 0 只有一种情况：
      这段素材压根没动（图片卡 / 纯静止画面）。

      旧代码把分母钳成 `max(1e-6, ...)`，于是静止素材会算出一个**荒唐的**
      比值：2026-09-13 在真实项目 `e62a182d`（5 个分镜一个都没生成出视频、
      compose 退回图片卡 → 全片零运动）上实测到 **22307014.465 倍**。
      危害有两层：
        ① 这个数字会被当成"实测跳变"原样写进用户能看到的告警里；
        ② 它 > 阈值，于是**每一处接缝**都被强行改成 0.9s 溶解 ——
           等于用一次不存在的测量，去推翻用户选的"换场硬切"。

      所以参照不存在时如实返回 `None`，让调用方说"无从比较"。
    """
    ma = float(a.get("motion_median") or 0)
    mb = float(b.get("motion_median") or 0)
    base = (ma + mb) / 2.0
    return base if base >= NO_MOTION_EPS else None


def seam_ratio_of(a: Dict[str, Any], b: Dict[str, Any]) -> Optional[float]:
    """一对片段的接缝强度（逐像素口径，与 `tests/analyze_seams.py` 一致）。

    返回 `None` = 参照不存在（见 `motion_base`），调用方必须按"无从比较"处理。
    """
    import numpy as np
    base = motion_base(a, b)
    fa, fb = a.get("_last"), b.get("_first")
    if base is None:
        return None
    if fa is None or fb is None or getattr(fa, "shape", None) != getattr(fb, "shape", None):
        return 0.0
    return round(float(np.abs(fa - fb).mean()) / base, 3)


# ═══════════════════════════════════════════════════════════════
# 转场长度：按**实测**接缝强度决定，不按语义猜
# ═══════════════════════════════════════════════════════════════
#
# 这是本模块里**收益最大**的一条，而且是量出来的
# （`tests/experiment_transitions.py`，线上真实片段、统一到 1080x1080@24）：
#
#     接法              接缝处最大逐帧差   vs 硬切   vs 该片段自身内部最大值
#     硬切                    94.13        1.00x        3.86x   ← 一眼就跳
#     溶解 0.4s               23.37        0.25x        0.96x
#     溶解 0.8s               23.46        0.25x        0.96x
#     溶解 1.2s               19.19        0.20x        0.79x
#     溶解 0.8s + 运动补偿插帧  23.46        0.25x        0.96x   ← 没有额外收益
#
# 结论：
#   ① 硬切的跳变是**该片段自身最剧烈一帧的 3.86 倍** —— "一眼AI"就是这个数
#   ② 一个 0.4 秒的溶解就能把它压到 **0.96 倍**（比片子自己的正常运动还平缓）
#   ③ 继续加长（0.8→1.2s）收益很小，还吃掉成片时长 → 0.35~0.6 是甜点区
#   ④ **运动补偿插帧（minterpolate）零收益**，还慢好几倍 → 不装
#
# 所以规则是：**硬切本身没问题，有问题的是"在硬得要命的地方硬切"。**
# 语义层（同场景硬切、跨场景溶解）保留；这一层只做一件事：
# 量一下这个切点硬不硬，硬就把它换成一条够长的溶解。

MAX_HARD_CUT_RATIO = 1.25   # ≤ 这个倍数：硬切跟片子自身运动同量级，看不出来
ADAPT_BASE_SEC = 0.35       # 溶解起步时长
ADAPT_MAX_SEC = 0.90        # 溶解上限（再长会明显拖节奏）

# ═══════════════════════════════════════════════════════════════
# 换场处理：亮度跳变 → 黑场（dip to black）+ 地点字幕卡
# ═══════════════════════════════════════════════════════════════
# 用户原话（2026-09-13）：
#   "有的衔接很突兀（突然特别亮、突然特别暗）……这种情况你可以设定转场或者
#    给个毫秒级别的黑屏+字幕说明下一个情景发生地点都行"
#
# ★ 阈值是**自己标定的**，不是抄来的：方法论仓库
#   （Hell-Grind-AIGC-Skill）里**没有任何亮度差阈值** —— 它对亮度连续性的
#   全部要求只有定性的 "不能让窗光瞬移" 与 `F-EXPOSURE`（关键高光溢出或暗部压死）。
#   所以这里给出来源与样本量，方便以后用新素材复核：
#
#   2026-09-13 在 3 个真实项目、12 处接缝上量 `luma_delta`
#   （上一镜尾帧灰度均值 − 本镜首帧灰度均值，0~255 量纲）：
#     · 同场景内（d5815c1e 单场景 4 处）：6.7 / 9.9 / 8.7 / 7.2  → 中位 7.9
#     · 跨场景（caa9904f 4 处）：1.6 / 125.0 / 15.0 / 22.2
#     · 跨场景（19636f68 3 处）：17.9 / 31.1 / 0.7
#   取 **18.0** 作为"明显明暗突变"的门槛：它把 caa9904f 那个
#   45→170（Δ=125.0，用户点名的"突然特别亮"）与 19636f68 的两处 17.9/31.1
#   判为需要处理，而把同场景内的 6.7~9.9 全部放过 —— 同一场景内的轻微曝光差
#   是**正常的**，加黑场反而是过度处理。
LUMA_JUMP_OBVIOUS = 18.0    # ≥ 此值：明暗突变，值得用黑场隔开
LUMA_JUMP_SEVERE = 60.0     # ≥ 此值：极端突变（样本里那个 125.0）
# 黑场（dip）总时长：0.5s = 0.25s 淡出 + 0.25s 淡入。
# 为什么不是 1s 以上：用户要的是"毫秒级别的黑屏"——它是个**标点**，不是一场戏。
DIP_SECONDS = 0.5


def choose_scene_treatment(prev: Dict[str, Any], cur: Dict[str, Any],
                           semantic: Dict[str, Any], *,
                           mode: str = "auto",
                           cross_scene: bool = True) -> Dict[str, Any]:
    """跨场景接缝的**处理方式**：硬切 / 溶解 / 黑场（带地点字幕卡）。

    这是接缝处理链的**最后一层**，只回答一件事：
    "这两段之间的明暗差，要不要用黑场给观众一个换气的标点？"

    mode：
      · `"cut"`       —— 一律硬切（用户 2026-09-13 之前的定稿；保留为可选项）
      · `"dissolve"`  —— 一律 0.9s 溶解（`continuity.TRANSITIONS["loose"]`）
      · `"dip"`       —— 跨场景一律黑场 + 地点卡
      · `"auto"`      —— **默认**：跨场景先看亮度跳变，只有跳变明显才上黑场，
                        否则尊重"硬切"的电影语法（不乱加黑场）

    测量口径与 `seam_report` 的 `luma_delta` 完全一致（同一对 `_last`/`_first`），
    所以"为什么这里上了黑场"可以直接用体检表复核。
    """
    out = dict(semantic or {})
    md = str(mode or "auto").strip().lower()
    if not cross_scene:
        # 同场景内不存在"换地方"，不该有地点卡
        out["treatment"] = "same_scene"
        return out

    lum = luma_delta_of(prev, cur)
    _pair = luma_pair_of(prev, cur)
    out["luma_delta"] = lum
    out["luma_prev_tail"] = _pair[0]
    out["luma_cur_head"] = _pair[1]
    if md == "cut":
        out["treatment"] = "cut"
        return out
    if md == "dissolve":
        out["treatment"] = "dissolve"
        return out
    if md == "dip":
        out["treatment"] = "dip"
        out["type"] = "fadeblack"
        out["seconds"] = DIP_SECONDS
        out["card"] = True
        out["measured"] = "forced"
        out["why"] = ("按设置：跨场景一律用黑场 + 地点字幕卡隔开"
                      f"（实测亮度差 {lum}）" if lum is not None
                      else "按设置：跨场景一律用黑场 + 地点字幕卡隔开")
        return out

    # auto：只有跳变明显才上黑场
    if lum is None:
        out["treatment"] = "cut"
        out["why"] = "量不到亮度（缺首尾帧）→ 保持硬切，不凭猜测加黑场"
        return out
    if lum >= LUMA_JUMP_OBVIOUS:
        out["treatment"] = "dip"
        out["type"] = "fadeblack"
        out["seconds"] = DIP_SECONDS
        out["card"] = True
        out["measured"] = "luma_jump"
        _lv = ("极端" if lum >= LUMA_JUMP_SEVERE else "明显")
        out["why"] = (f"换场处明暗{_lv}突变：上一镜尾帧灰度 "
                      f"{out.get('luma_prev_tail')} → 本镜首帧 {out.get('luma_cur_head')}"
                      f"（差 {lum}，门槛 {LUMA_JUMP_OBVIOUS}）"
                      f"→ 用 {DIP_SECONDS}s 黑场隔开并打出地点字幕卡，"
                      f"让'换地方了'变成主动交代而不是突然一亮")
        return out
    if lum >= LUMA_JUMP_MILD:
        # ★ 第三档：**亮度色调桥接**。
        #   这一段差得不算小（看得见"一亮/一暗"），但还不到需要黑场"打标点"的程度。
        #   旧代码在这一档直接硬切 —— 于是"轻微突兀"被原样保留。
        #   现在改成：入场那一段的**头部**（约 0.45s）从"上一镜的亮度/色调"
        #   平滑过渡到它自己的亮度，像摄影机光圈自己适应过来。
        #   接缝仍是硬切（时间轴不变、不引入叠影），只是曝光不再"啪"地跳。
        _br = plan_luma_bridge(prev, cur)
        out["treatment"] = "bridge"
        out["measured"] = "luma_mild"
        out["bridge"] = _br or {}
        out["why"] = (f"换场处亮度差 {lum}（≥{LUMA_JUMP_MILD} 但 <{LUMA_JUMP_OBVIOUS}）："
                      f"不够黑场、又不该放任硬切 → 给入场镜头部做 "
                      f"{(_br or {}).get('seconds', BRIDGE_SECONDS)}s 亮度色调桥接"
                      f"（偏移 {(_br or {}).get('offsets')}），"
                      f"把'突然一亮/一暗'变成曝光自己适应过来")
        return out
    out["treatment"] = "cut"
    out["measured"] = "ok"
    out["why"] = (f"换场处亮度差 {lum} < 门槛 {LUMA_JUMP_OBVIOUS} → 保持硬切"
                  f"（同一场景内的轻微曝光差不加黑场）")
    return out


# ═══════════════════════════════════════════════════════════════
# 亮度色调桥接：换场的**第三种**处理
# ═══════════════════════════════════════════════════════════════
#
# 三种处理各自负责一个量级（这是标定出来的三段，不是拍脑袋）：
#   · Δ亮度 < 8    → 硬切      ：看不出来，别动它（动了反而"过度处理"）
#   · 8 ≤ Δ < 18   → **桥接**  ：看得见一亮/一暗，但还不到要"打标点"
#   · Δ ≥ 18       → 黑场+地点卡：明显/极端突变，需要一次明确的换气
#
# 桥接的做法刻意避开两件事：
#   · **不加叠影**（不用溶解）：溶解会让两幅画面短暂叠在一起（ghosting），
#     在"两个完全不同的地方"之间特别假；
#   · **不动时间轴**：只在入场镜的头部做一次"校正→原样"的渐变，
#     帧数不变 → 字幕/配音的时间映射完全不受影响（这点很关键，
#     历史上被转场时长改动坑过一次，见 compose 里 `_cuts_actual` 那段）。
#
# ffmpeg 实现用 `blend` 的时间变量 `T`（实测可用）：
#   `lutrgb` 里用 `t`/`n` 会报 "Undefined constant"（这个构建不支持），
#   所以只能用"截头部 → 做一份校正副本 → 按 T 混合回原样 → 与剩余部分 concat"。
BRIDGE_SECONDS = 0.45          # 桥接时长（≈11 帧 @24fps，够"适应"又不拖）
BRIDGE_OFFSET_LIMIT = 48.0     # 单通道偏移上限（0-255）。再大就不是"适应"而是"改风格"了
LUMA_JUMP_MILD = 8.0           # ≥ 此值且 < LUMA_JUMP_OBVIOUS → 走桥接


def plan_luma_bridge(prev: Dict[str, Any], cur: Dict[str, Any], *,
                     seconds: float = BRIDGE_SECONDS,
                     limit: float = BRIDGE_OFFSET_LIMIT) -> Optional[Dict[str, Any]]:
    """算出**入场片段头部**要叠加的逐通道偏移，把首帧拉到出场尾帧的水平。

    分解成两部分（这个分解很重要，否则会"把新场景的色调也拉平"）：
      · **亮度分量** `base = 出场尾帧灰度 − 入场首帧灰度`，三个通道一起加 ——
        它负责"一亮/一暗"；
      · **色调分量** `残差 = 各通道差 − base`，再单独限幅到更小 ——
        它负责"色调偏冷/偏暖"，比如上一镜是暖黄灯、下一镜是冷白日光。

    返回 `None` 表示量不到（缺首尾帧），调用方应当保持硬切而不是瞎修。
    """
    lum = luma_delta_of(prev, cur)
    lp, lc = luma_pair_of(prev, cur)
    if lum is None or lp is None or lc is None:
        return None
    try:
        base = max(-limit, min(limit, float(lp) - float(lc)))
    except Exception:
        return None
    offs = [base, base, base]
    tint = None
    try:
        pr = (prev.get("rgb") or {}).get("last")
        cr = (cur.get("rgb") or {}).get("first")
        if pr and cr and len(pr) == 3 and len(cr) == 3:
            # ★★ 这里有个**必须分清的口径**（第一版写错，把亮度补偿抵消掉了）：
            #   `base` 是**灰度口径**的亮度差；而 `pr-cr` 是**逐通道 RGB** 差。
            #   对一个中性灰场景，两者数值接近；但直接 `(pr-cr) - base` 会得到
            #   `0 - (-12) = +12`，被误判成"色调差"，又原样加回去 → 偏移互相抵消，
            #   首帧亮度一点没改（实测：Δ=12 的场景算出来 offsets 全是 0）。
            #   正确做法：色调分量只看**通道之间的相对偏差**
            #   （逐通道差 − 各通道差的均值），它与亮度分量正交。
            diffs = [float(pr[k]) - float(cr[k]) for k in range(3)]
            dmean = sum(diffs) / 3.0
            tint = []
            for k in range(3):
                t = max(-limit * 0.6, min(limit * 0.6, diffs[k] - dmean))
                tint.append(round(t, 2))
                offs[k] = round(max(-limit, min(limit, base + t)), 2)
    except Exception:
        tint = None
    if all(abs(o) < 0.75 for o in offs):
        return None                     # 差得太小，不值得为它重编码一次
    return {"offsets": offs, "tint": tint, "seconds": round(float(seconds), 3),
            "luma_delta": lum, "luma_prev_tail": lp, "luma_cur_head": lc}


async def apply_head_bridge(src: str, dst: str, *, offsets, seconds: float,
                            w: int, h: int, fps: float, crf: int = 18
                            ) -> Dict[str, Any]:
    """把"头部渐变校正"真正烧进入场片段。帧数**必须与原来一致**。

    为什么要显式校验帧数：这一层的契约是"只改曝光、不改时间轴"。
    一旦帧数变了，字幕和配音映射就会整体错位（历史事故见
    `compose.py` 里 `_cuts_actual` 的注释）。所以宁可放弃桥接，也不许时长漂移。
    """
    info = await probe(src)
    dur = float(info.get("duration") or 0.0)
    D = max(0.15, float(seconds))
    off = [float(x) for x in (offsets or [])]
    if len(off) != 3 or dur <= 0:
        return {"ok": False, "why": "参数不完整或读不到时长"}
    if dur <= D * 1.25:
        return {"ok": False, "why": f"片段只有 {dur:.2f}s，比桥接时长还短，不做"}
    lut = "lutrgb=" + ":".join(
        f"{n}='clip(val{'+' if v >= 0 else '-'}{abs(v):.3f},0,255)'"
        for n, v in zip(("r", "g", "b"), off))
    # ★★ 三个必须写对的细节（第一版三个全错，实验里一眼看出来）：
    #   ① **切分要用帧号，不能用秒**：`trim=0:0.45` + `trim=start=0.45` 会丢掉
    #      一帧（实测 48 → 47 帧）。帧数变了就是动了时间轴，字幕/配音会整体错位。
    #      改用 `end_frame=N` / `start_frame=N`，N = round(D*fps)，精确。
    #   ② **混合方向要反过来**：`A*(1-k)+B*k` 在 T=0 处取的是 A（原样），
    #      于是"桥接"把原样渐变成了校正 —— 看起来像生效了，实际是在窗口**末尾**
    #      又造了一次跳变（实测 128.9 → 200.7，反而更刺眼）。
    #      正确的是 T=0 处取 B（校正版），再平滑回到 A（原样）。
    #   ③ 渐变分母用帧数换出来的真实窗口长度 `n/fps`，与切分严格对齐。
    n = max(2, int(round(D * float(fps or 24))))
    dd = n / float(fps or 24)
    #  ④ 链里**不要**再放 `fps=` 滤镜：实测它会吃掉最后一帧（48 → 47）。
    #     送进来的片段在 `normalize_clip` 阶段已经统一过帧率了，这里是冗余的。
    fc = (f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
          f"setsar=1,format=yuv420p,split=3[main][h1][h2];"
          f"[h1]trim=end_frame={n},setpts=PTS-STARTPTS[h1t];"
          f"[h2]trim=end_frame={n},setpts=PTS-STARTPTS,{lut}[h2c];"
          f"[h1t][h2c]blend=all_expr='B*(1-T/{dd:.5f})+A*(T/{dd:.5f})'[bh];"
          f"[main]trim=start_frame={n},setpts=PTS-STARTPTS[rest];"
          f"[bh][rest]concat=n=2:v=1:a=0[out]")
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    rc, txt = await _run([
        _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", src,
        "-filter_complex", fc, "-map", "[out]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-r", f"{fps}", "-movflags", "+faststart", dst],
        timeout=900)
    if rc != 0 or not os.path.exists(dst):
        return {"ok": False, "why": (txt or "")[-200:]}
    # 帧数一致性守卫
    n0 = await _count_frames(src)
    n1 = await _count_frames(dst)
    if n0 and n1 and n0 != n1:
        try:
            os.remove(dst)
        except Exception:
            pass
        return {"ok": False, "why": f"帧数不一致（{n0} → {n1}），放弃桥接以免时间轴漂移"}
    return {"ok": True, "dst": dst, "offsets": off, "seconds": round(D, 3),
            "frames": n1 or n0}


async def _count_frames(path: str) -> int:
    """数帧（用于"桥接不许改时间轴"的守卫）。数不到返回 0。

    这里**不引入 ffprobe**：本项目只保证有 `imageio_ffmpeg` 提供的 ffmpeg，
    `ffprobe` 不保证存在（打包版尤其）。用 ffmpeg 解码到 null 并读
    `frame= N` 那一行即可；桥接只用在十几秒的短片段上，代价可接受。
    """
    try:
        rc, txt = await _run([_ffmpeg(), "-hide_banner", "-i", path,
                              "-f", "null", "-"], timeout=600)
        if rc == 0:
            ms = re.findall(r"frame=\s*(\d+)", txt or "")
            if ms:
                return int(ms[-1])
    except Exception:
        pass
    return 0


# ═══════════════════════════════════════════════════════════════
# "多个镜头其实是同一个画面"：整帧相似度
# ═══════════════════════════════════════════════════════════════
#
# 为什么用**整帧**相似度而不是"检测画面里的文字"：
#   试过"静止 + 高频边缘"那套来认压字，**失败了** —— 缩到 96x54 后文字是
#   亚像素，时间标准差又被 mp4 压缩噪声抬高，真实压字的片段得分 0.0（漏报），
#   反而把正常片段排在最前面。整帧相似度不需要看见文字笔画，
#   只要"这一镜和那一镜长得一样"，实测分得很干净（见 harmonize 里的实测值）。
#
# 门槛 0.93：真重复 0.965~0.983，正常最高 0.846。
DUP_FRAME_SIM = 0.93


def frame_similarity(a, b) -> float:
    """两帧的相似度（1 - 归一化平均绝对差）。任一为空返回 0。"""
    try:
        import numpy as np
        if a is None or b is None:
            return 0.0
        x, y = np.asarray(a, dtype="float32"), np.asarray(b, dtype="float32")
        if x.size == 0 or x.shape != y.shape:
            return 0.0
        return float(1.0 - np.abs(x - y).mean() / 255.0)
    except Exception:
        return 0.0


def duplicate_shot_report(stats: List[Dict[str, Any]],
                          *, threshold: float = DUP_FRAME_SIM) -> Dict[str, Any]:
    """找出"画面几乎相同"的镜头对。

    用 `analyze_clip` 已经算好的**首帧**（`_first`）比，不额外解码。
    返回 `{"pairs": [(镜i, 镜j, 相似度), ...], "max_sim": float}`（镜号从 1 起）。
    """
    pairs: List[tuple] = []
    mx = 0.0
    n = len(stats or [])
    for i in range(n):
        for j in range(i + 1, n):
            s = frame_similarity((stats[i] or {}).get("_first"),
                                 (stats[j] or {}).get("_first"))
            if s > mx:
                mx = s
            if s >= threshold:
                pairs.append((i + 1, j + 1, round(s, 4)))
    pairs.sort(key=lambda x: -x[2])
    return {"pairs": pairs, "max_sim": round(mx, 4), "threshold": threshold}


def luma_pair_of(a: Dict[str, Any], b: Dict[str, Any]):
    """(上一镜尾帧灰度均值, 本镜首帧灰度均值)。量不到返回 (None, None)。"""
    try:
        import numpy as np
        fa, fb = a.get("_last"), b.get("_first")
        if fa is None or fb is None:
            return (None, None)
        return (round(float(np.asarray(fa, dtype="float32").mean()), 2),
                round(float(np.asarray(fb, dtype="float32").mean()), 2))
    except Exception:
        return (None, None)


def luma_delta_of(a: Dict[str, Any], b: Dict[str, Any]) -> Optional[float]:
    """上一镜尾帧与本镜首帧的**灰度均值之差**（0~255 量纲）。量不到返回 None。"""
    la, lb = luma_pair_of(a, b)
    if la is None or lb is None:
        return None
    return round(abs(lb - la), 2)

# 「自身运动尺度」这个参照**什么时候不存在**：`motion_median` 恰好为 0
# （素材压根没动 = 图片卡）。见 `motion_base` 的说明。
NO_MOTION_EPS = 1e-3


def choose_transition(prev: Dict[str, Any], cur: Dict[str, Any],
                      semantic: Dict[str, Any], *,
                      max_ratio: float = MAX_HARD_CUT_RATIO) -> Dict[str, Any]:
    """语义规则给的转场 + **实测接缝强度**的修正。

    `semantic` 来自 `core.continuity.pick_transition`（同场景硬切 / 跨场景溶解）。
    这里只在一件事上覆盖它：**语义说硬切、但实测这个切点很硬**的时候。
    """
    out = dict(semantic or {})
    ratio = seam_ratio_of(prev, cur)
    out["seam_ratio"] = ratio
    sem_sec = float(out.get("seconds") or 0.0)

    if ratio is None:
        # 参照不存在（两段本身几乎不动）→ **不做**测量驱动的修正。
        # 硬切还是溶解交回语义层（含用户显式选的硬切）。
        # 关键是别让一个算不出来的数悄悄替用户做决定，所以这里如实标注。
        out["measured"] = "unmeasurable"
        out["why"] = ("这两段素材本身几乎没有运动（很可能是图片卡/纯静止画面），"
                      "『跳变是自身运动尺度的几倍』这个参照不存在，"
                      "因此不做测量驱动的摊薄，按镜头关系决定转场。")
        return out

    if ratio <= max_ratio:
        # 硬切和片子自身运动同量级 → 保持原判（语义层是对的）
        out["measured"] = "ok"
        out.setdefault("why", "")
        return out

    # 接缝比片子自身运动剧烈得多 → 必须摊薄
    if sem_sec <= 0:
        # 语义是硬切，改成溶解。时长按超出的倍数加，但落在甜点区
        extra = min(ADAPT_MAX_SEC, ADAPT_BASE_SEC + (ratio - max_ratio) * 0.10)
        out["type"] = "fade"
        out["seconds"] = round(max(ADAPT_BASE_SEC, extra), 3)
        out["measured"] = "softened"
        out["why"] = (f"实测这个切点跳变是该片段自身运动尺度的 {ratio:.1f} 倍"
                      f"（硬切会明显跳），已改用 {out['seconds']:.2f}s 溶解摊薄"
                      f"—— 实测能把跳变压到自身运动量级以下")
    else:
        # 本来就是溶解，但如果接缝极硬就给足时长
        want = min(ADAPT_MAX_SEC, max(sem_sec, ADAPT_BASE_SEC + (ratio - max_ratio) * 0.10))
        if want > sem_sec + 0.02:
            out["seconds"] = round(want, 3)
            out["measured"] = "extended"
            out["why"] = (str(out.get("why") or "") +
                          f"；实测接缝强度 {ratio:.1f} 倍，溶解延长到 {want:.2f}s")
        else:
            out["measured"] = "ok"
    return out


# ═══════════════════════════════════════════════════════════════
# 二、目标画幅：让所有片段归一到一个规格
# ═══════════════════════════════════════════════════════════════

def _even(n: int) -> int:
    """H.264 要求宽高是偶数，否则 libx264 直接报错。"""
    return int(n) - (int(n) % 2)


def plan_target_geometry(stats: List[Dict[str, Any]]) -> Dict[str, Any]:
    """从各片段规格里挑一个**共同目标**画幅。

    选择逻辑（为什么这样选）：
      · **按画幅（宽高比）分组，取"总时长最长"的那组** —— 主流画幅保留最多内容，
        少数派才被裁。反过来会让多数片段被裁，损失更大。
      · 目标分辨率取该组里**面积最大**的规格，但**不放大**超过 1920 宽 ——
        放大只会变糊，不会变清晰。
      · fps 取该组里出现最多的值（避免 24/25 混用导致的时间基错乱）。
    """
    import numpy as np
    valid = [s for s in stats if s.get("w") and s.get("h")]
    if not valid:
        return {"w": 1280, "h": 720, "fps": 24.0,
                "reason": "读不到任何片段规格，用默认 1280x720@24"}
    groups: Dict[float, Dict[str, Any]] = {}
    for s in valid:
        ar = round(s["w"] / s["h"], 3)
        g = groups.setdefault(ar, {"dur": 0.0, "shots": [], "sizes": []})
        g["dur"] += float(s.get("duration") or 0)
        g["shots"].append(s)
        g["sizes"].append((s["w"], s["h"]))
    best_ar = max(groups, key=lambda k: groups[k]["dur"])
    g = groups[best_ar]
    # 该组里面积最大、且不超过 1920 宽的那个规格作目标
    cands = sorted(set(g["sizes"]), key=lambda wh: wh[0] * wh[1])
    tw, th = cands[-1]
    for w_, h_ in cands:
        if w_ <= 1920:
            tw, th = w_, h_
            break
    w, h = _even(tw), _even(th)
    fps_list = [float(s.get("fps") or 0) for s in g["shots"] if s.get("fps")]
    fps = 24.0
    if fps_list:
        vals, counts = np.unique(np.round(fps_list, 2), return_counts=True)
        fps = float(vals[int(np.argmax(counts))])
    return {"w": w, "h": h, "fps": fps, "aspect": round(w / h, 3),
            "group_shots": len(g["shots"]), "all_shots": len(valid),
            "reason": (f"主流画幅 {w}x{h}（{len(g['shots'])}/{len(valid)} 段，"
                       f"按总时长加权选出）· {fps:g}fps")}


async def crop_loss_ratio(src_w: int, src_h: int, tw: int, th: int) -> float:
    """从 src 画幅裁到 target 画幅会丢掉多少画面（面积比）。"""
    if not (src_w and src_h and tw and th):
        return 0.0
    src_ar, tgt_ar = src_w / src_h, tw / th
    if abs(src_ar - tgt_ar) < 1e-3:
        return 0.0
    if src_ar > tgt_ar:
        keep = tgt_ar / src_ar          # 太宽 → 裁两侧
    else:
        keep = src_ar / tgt_ar          # 太高 → 裁上下
    return round(1.0 - keep, 4)


# ═══════════════════════════════════════════════════════════════
# 三、裁掉"沉降/定住"的头尾
# ═══════════════════════════════════════════════════════════════

def detect_settle_windows(stat: Dict[str, Any]) -> Dict[str, float]:
    """算出这个片段头尾各该裁掉多少秒。

    思路：从第 0 帧往后扫，只要还"明显不动"（< 中位运动量 × SETTLE_RATIO）就继续，
    但最多裁 MAX_HEAD_TRIM 秒。结尾同理反向扫。
    **裁完必须还剩 MIN_KEEP_RATIO 以上**，否则宁可不动 —— 别为了顺滑把内容删光。
    """
    mc = stat.get("motion_curve") or []
    med = float(stat.get("motion_median") or 0.0)
    dur = float(stat.get("duration") or 0.0)
    if len(mc) < 6 or med <= 1e-6 or dur <= 0:
        return {"head": 0.0, "tail": 0.0}
    thr = med * SETTLE_RATIO
    max_head_n = int(MAX_HEAD_TRIM * ANALYZE_FPS)
    max_tail_n = int(MAX_TAIL_TRIM * ANALYZE_FPS)
    hn = 0
    while hn < min(max_head_n, len(mc)) and mc[hn] < thr:
        hn += 1
    tn = 0
    while tn < min(max_tail_n, len(mc)) and mc[-1 - tn] < thr:
        tn += 1
    head, tail = hn / ANALYZE_FPS, tn / ANALYZE_FPS
    if head + tail > dur * (1.0 - MIN_KEEP_RATIO):
        scale = (dur * (1.0 - MIN_KEEP_RATIO)) / max(1e-6, head + tail)
        head, tail = head * scale, tail * scale
    return {"head": round(head, 3), "tail": round(tail, 3)}


# ═══════════════════════════════════════════════════════════════
# 四、颜色匹配（限幅、只在同场景内用）
# ═══════════════════════════════════════════════════════════════

def plan_color_match(src: Dict[str, Any], target: Dict[str, Any]) -> Dict[str, Any]:
    """算出一组 per-channel 线性校正：val' = clip(val*gain + off, 0, 255)。

    用"均值+标准差"匹配（Reinhard 那套的逐通道线性版）：
        gain = σt/σs      （把反差拉齐）
        off  = μt − μs·gain（把亮度拉齐）
    两侧都**限幅**：只修"漂移"，不重画风格。
    一个故意拍成夜景的镜头不该被拉成白天 —— 限幅 + "只在同场景内用"两道闸。
    """
    sm = src.get("mean") or [0, 0, 0]
    ss = src.get("std") or [1, 1, 1]
    tm = target.get("mean") or [0, 0, 0]
    ts = target.get("std") or [1, 1, 1]
    gains, offs = [], []
    for i in range(3):
        g = (ts[i] / ss[i]) if ss[i] > 1e-3 else 1.0
        g = max(1.0 - COLOR_GAIN_LIMIT, min(1.0 + COLOR_GAIN_LIMIT, g))
        o = tm[i] - sm[i] * g
        o = max(-COLOR_OFF_LIMIT, min(COLOR_OFF_LIMIT, o))
        gains.append(round(g, 4))
        offs.append(round(o, 3))
    changed = any(abs(g - 1.0) > 0.005 for g in gains) or any(abs(o) > 0.5 for o in offs)
    return {"gain": gains, "off": offs, "changed": changed}


# ═══════════════════════════════════════════════════════════════
# 冻结重演 / 首帧沉降：另一种"一眼AI"，症状与"跳变"正好相反
# ═══════════════════════════════════════════════════════════════
#
# 症状对比（同一个接缝，两种病）：
#   · **跳变（jump）**：接缝处帧差**远大于**片段内部典型帧差 → 画面"啪"地跳一下
#   · **停顿（stall）**：接缝处帧差**接近 0** → 画面"定住"了，然后才重新动起来
# 后者是**串联生成**特有的：第 i 段的输入首帧 = 第 i-1 段的尾帧，
# 模型往往先原样复现这个输入帧几帧、再开始动 —— 于是拼接处出现一段静止，
# 观感就是"动着动着停住 → 又慢慢启动"。用户说的"运动停顿重启"就是它。
#
# ★ 关键：治它的办法（裁掉重复的头几帧）与治跳变的办法（加长溶解）**不通用**。
#   上一批我一度把"裁沉降帧"当默认打开，结果 seam_ratio 反而变差
#   （7.209 → 7.863）—— 因为裁掉几帧会让接缝两侧**更不像**，
#   而那个指标量的正是"两侧像不像"。**指标选错了，结论就会反。**
#   治停顿要用"停顿指标"：接缝附近的**最小**帧差 / 静止持续了几帧。

# 判定"这一帧是上一段尾帧的重复"的阈值（相对片段内部典型帧差）
DUP_FRAME_RATIO = 0.35
MAX_DEDUP_SEC = 0.80


def detect_dup_head(prev: Dict[str, Any], cur: Dict[str, Any],
                    *, ratio: float = DUP_FRAME_RATIO) -> Dict[str, Any]:
    """检测 cur 的开头是不是"把 prev 的尾帧冻住复现了几帧"（串联生成的冻结重演）。

    两个条件同时成立才算（缺一不可）：

      **(a) 边界像同一帧**：cur 的首帧与 prev 的尾帧的差异，不超过"一个典型帧步"。
          阈值故意放得**宽松**（`base × 1.0`，而不是 `× ratio`）——
          真实场景里 cur 是模型**重新生成**的，不可能与 prev 的尾帧逐像素相同，
          压得太紧就会全部漏检（实测第一次写严了：人为造的 0.25s 冻结，
          检出 0 帧，因为两帧差了 6.5，而阈值只有 2.47）。

      **(b) 开头有一段"几乎不动"**：用 cur **自己**相邻帧的差来量有几帧连续静止。
          为什么不拿每一帧都去跟 prev 的尾帧比：那样要求"复现的帧与原帧几乎一样"，
          而模型复现出来的帧彼此之间一致、与原帧却会有细微出入。
          用"自己跟自己比"量静止时长，稳得多。

    没有 (a) 就会把"本来就在缓慢起幅"的正常镜头误裁；
    没有 (b) 就会把"只是接得上"但一直在动的镜头误裁。
    """
    import numpy as np
    p_last = prev.get("_last")
    head = cur.get("_head")
    if p_last is None or head is None or len(head) < 2:
        return {"dup_frames": 0, "dup_seconds": 0.0, "verdict": "数据不足"}
    ma = float(prev.get("motion_median") or 0)
    mb = float(cur.get("motion_median") or 0)
    base_raw = (ma + mb) / 2.0
    base = max(1e-6, base_raw)      # 只用于**返回里报数**；判据见下
    if base_raw < NO_MOTION_EPS:
        # ★ 两段都没有可测运动（图片卡）→ "有没有冻结重演"**判不出来**。
        #   旧代码在这里会拿 1e-6 当阈值：静止素材的头几帧差恰好也是 0，
        #   于是"全部小于阈值"，一路把片头裁掉最多 MAX_DEDUP_SEC ——
        #   而纯静止片段压根不存在"冻结重演"，这是凭一个不存在的参照删内容。
        return {"dup_frames": 0, "dup_seconds": 0.0,
                "boundary_diff": round(float(np.abs(p_last - head[0]).mean()), 2),
                "base_motion": round(base_raw, 4),
                "verdict": "无从判断（两段素材本身几乎不动，"
                           "没有『自身运动尺度』可作参照）"}

    boundary = float(np.abs(p_last - head[0]).mean())
    if boundary > base * 1.0:
        return {"dup_frames": 0, "dup_seconds": 0.0, "boundary_diff": round(boundary, 2),
                "base_motion": round(base, 3), "threshold": round(base * ratio, 3),
                "verdict": "首帧与上一段尾帧差异偏大，判为正常切点（不是重复）"}

    thr = base * ratio
    n_small = 0
    diffs = []
    for i in range(1, len(head)):
        d = float(np.abs(head[i] - head[i - 1]).mean())
        diffs.append(round(d, 2))
        if d <= thr:
            n_small += 1
        else:
            break
    # ★ 要裁掉的帧数 = 静止的**帧**数，不是"静止间隔"数。
    #   例：4 帧完全相同 → 只有 3 个"间隔"是小的，但重复的**帧**是 4 帧。
    #   写少一帧就会在接缝上残留一帧重复（观感上仍是"卡了一下"）。
    hold = (n_small + 1) if n_small >= 1 else 0
    hold = min(hold, int(MAX_DEDUP_SEC * ANALYZE_FPS))
    sec = round(hold / ANALYZE_FPS, 3)
    return {
        "dup_frames": hold, "dup_seconds": sec,
        "boundary_diff": round(boundary, 2), "base_motion": round(base, 3),
        "threshold": round(thr, 3), "head_diffs": diffs,
        "verdict": ("首帧接得上上一段尾帧，且开头有一段静止 —— 判为冻结重演"
                    if hold >= 2 else "开头没有持续静止，判为正常"),
    }


def plan_dedup(stats: List[Dict[str, Any]]) -> List[float]:
    """逐段算出"该裁掉多少秒的重复头"（只治冻结重演，不动别的）。"""
    out = [0.0] * len(stats)
    for i in range(1, len(stats)):
        d = detect_dup_head(stats[i - 1], stats[i])
        out[i] = float(d.get("dup_seconds") or 0.0)
    return out


def stall_windows(stat: Dict[str, Any], *, ratio: float = 0.25,
                  min_seconds: float = 0.35) -> List[Dict[str, Any]]:
    """找出**片段内部**"几乎不动"的段落（模型自己生成的停顿）。

    ★★ 为什么必须有这个函数（2026-09-13 实测）：
      `stall_report` 只看**接缝处**的帧差 —— 它能抓到"串联段开头冻结重演"
      （接缝帧差接近 0），但抓不到**一个片段中间整整几秒几乎不动**。
      实测项目 caa9904f 第 1 镜（20.2s，两段 10s 串联）：
      段间拼点本身**完全看不出来**（内部最猛帧只有 1.02× p99），
      可是它内部 **t=7.25s 起有 2.88 秒几乎静止** ——
      观众的感受就是"画面卡住了几秒"，这是目标里点名的"运动停顿重启"，
      而在此之前的告警里**一个字都没提**。

    判据：连续 ≥`min_seconds` 秒的帧差都低于"该片段运动中位 × ratio"。
    返回 `[{at, seconds}, ...]`（按时间排序）。
    """
    mc = [float(x) for x in (stat.get("motion_curve") or [])]
    med = float(stat.get("motion_median") or 0.0)
    if len(mc) < 4 or med <= 1e-6:
        return []
    thr = med * ratio
    need = max(2, int(round(min_seconds * ANALYZE_FPS)))
    out: List[Dict[str, Any]] = []
    run = 0
    for i, v in enumerate(mc):
        if v < thr:
            run += 1
        else:
            if run >= need:
                out.append({"at": round((i - run + 1) / ANALYZE_FPS, 2),
                            "seconds": round(run / ANALYZE_FPS, 2)})
            run = 0
    if run >= need:
        out.append({"at": round((len(mc) - run + 1) / ANALYZE_FPS, 2),
                    "seconds": round(run / ANALYZE_FPS, 2)})
    return out


def stall_report(stats: List[Dict[str, Any]]) -> Dict[str, Any]:
    """接缝处的"停顿"程度：接缝帧差 ÷ 内部典型帧差。

    ★ 与 `seam_report` 的 `seam_ratio` 是**互补**的两个指标：
        seam_ratio 大 → 跳变（用溶解治）
        stall_ratio 小（接近 0）→ 停顿（用裁重复帧治）
    只看其中一个都会漏掉一半的病，甚至会把结论搞反。

    ★ 2026-09-13 起还多带一份**片段内部**的停顿清单（`freezes`）——
      原因见 `stall_windows` 的注释：接缝不跳 ≠ 片子里没有卡住的地方。
    """
    import numpy as np
    out: List[Dict[str, Any]] = []
    for i in range(len(stats) - 1):
        a, b = stats[i], stats[i + 1]
        fa, fb = a.get("_last"), b.get("_first")
        if fa is None or fb is None or getattr(fa, "shape", None) != getattr(fb, "shape", None):
            continue
        ma = float(a.get("motion_median") or 0)
        mb = float(b.get("motion_median") or 0)
        base = motion_base(a, b)
        boundary = float(np.abs(fa - fb).mean())
        dup = detect_dup_head(a, b)
        out.append({
            "seam": f"{i+1}→{i+2}",
            "boundary_diff": round(boundary, 3),
            "base_motion": round((ma + mb) / 2.0, 4),
            "meaningful": base is not None,
            # 分母不存在 → 报 None，别报 2e7 那种"看着像结论"的数
            "stall_ratio": (round(boundary / base, 3) if base is not None else None),
            "dup_seconds": dup.get("dup_seconds"),
            "verdict": ("无从比较（两段素材本身几乎不动）" if base is None else
                        "停顿（接缝几乎不动，像卡住）" if boundary / base < 0.45 else "正常"),
        })
    stalls = [s for s in out if s["verdict"].startswith("停顿")]
    freezes: List[Dict[str, Any]] = []
    for i, st in enumerate(stats):
        for w in stall_windows(st):
            freezes.append({"clip": i + 1, "clip_seconds": st.get("duration"),
                            **w})
    _sv = [s["stall_ratio"] for s in out if s["stall_ratio"] is not None]
    return {"seams": out, "stall_count": len(stalls),
            "freezes": freezes, "freeze_count": len(freezes),
            "freeze_total_seconds": round(sum(f["seconds"] for f in freezes), 2),
            "mean_stall_ratio": round(float(np.mean(_sv)), 3) if _sv else None,
            "meaningful_count": len(_sv)}


async def film_report(path: str) -> Dict[str, Any]:
    """**成片级**指标：整片逐帧算变化量，看有没有"尖峰"。

    ★ 它回答的是"这一条成片里，有没有某一帧突然跳一下" ——
      也就是观众感知到的"接缝处啪地跳"。

    指标：
      `max_delta`  单帧最大跳变
      `p99_delta`  第 99 百分位（= 这个片子自身的"常态上限"）
      `spike_ratio = max/p99`
          ≈1    全片最猛的一帧只是普通运动 → **没有尖峰**，接缝看不出来
          >2    有孤立尖峰（多半就是某个没被摊平的接缝）

    ★★ 一个**必须记住的局限**（我在这上面栽过）：
      这个指标**不适合用来衡量"串联有没有生效"**。成片的 max 几乎总是来自
      **片段内部的运动**（实测 p99≈3.0、中位≈1.5 都是内容自身的动态），
      而接缝早被自适应转场摊平了，根本不会成为全片最猛的一帧。
      实测：串联前后 max/p99 是 1.853 → 1.839（几乎不变），
      但**接缝处的帧差**是 14.123 → 4.721 —— 那才是串联的证据（看 `seam_report`）。
      **想量接缝就去量接缝，别拿全片 max 去推。**
    """
    import numpy as np
    import tempfile
    fd, raw = tempfile.mkstemp(suffix=".__film.raw")
    os.close(fd)
    try:
        rc, txt = await _run([_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                              "-i", path, "-vf", f"fps=24,scale={ANALYZE_W}:{ANALYZE_H},"
                              f"format=gray",
                              "-f", "rawvideo", "-pix_fmt", "gray", raw], timeout=1800)
        if rc != 0 or not os.path.exists(raw):
            return {"ok": False, "error": (txt or "")[-200:]}
        a = np.fromfile(raw, dtype=np.uint8).astype(np.float32)
    finally:
        try:
            os.remove(raw)
        except Exception:
            pass
    k = ANALYZE_W * ANALYZE_H
    n = len(a) // k
    if n < 3:
        return {"ok": False, "error": "成片太短或读不到帧"}
    f = a[:n * k].reshape(n, ANALYZE_H, ANALYZE_W)
    d = np.abs(np.diff(f, axis=0)).mean(axis=(1, 2))
    med = float(np.median(d))
    p99 = float(np.percentile(d, 99))
    mx = float(d.max())
    ratio = round(mx / max(1e-6, p99), 3)
    return {
        "ok": True, "frames": int(len(d)),
        "max_delta": round(mx, 2), "p99_delta": round(p99, 2),
        "median_delta": round(med, 2), "spike_ratio": ratio,
        "verdict": ("没有尖峰（接缝不是全片最猛的一帧）" if ratio < 1.6 else
                    "轻微尖峰" if ratio < 2.2 else "有明显尖峰"),
    }


def lut_filter(cm: Dict[str, Any]) -> str:
    """把计划变成 ffmpeg 的 lutrgb 表达式。"""
    g, o = cm["gain"], cm["off"]
    ch = []
    for i, name in enumerate(("r", "g", "b")):
        ch.append(f"{name}='clip(val*{g[i]:.4f}+{o[i]:.3f},0,255)'")
    return "lutrgb=" + ":".join(ch)


# ═══════════════════════════════════════════════════════════════
# 纹理统一：把"颗粒感"拉到同一个量级
# ═══════════════════════════════════════════════════════════════
#
# 目标里点名的"纹理不统一"：不同模型/不同提示词生成的片段，
# 一段有胶片颗粒、一段像塑料一样光滑 —— 剪在一起就像两台机器拍的。
#
# 两个手段，一个都不能少：
#   ① **等化微细节**：用 `unsharp` 逐段把高频能量拉向共同目标。
#      拉普拉斯标准差高的段落少加锐（甚至负量=柔化），低的段落多加。
#   ② **统一纹理地板**：给**所有**段加**同样**的一点颗粒。
#      等化只能把差异缩小；加一层共同的颗粒才让它们"像同一批胶片"。
#      顺带一个副作用也是好事：颗粒能盖住生成模型的高频伪影。
#
# ★ 为什么不用运动补偿那类重型手段：上一批实测过（experiments_transitions）
#   运动补偿插帧对接缝**零收益**，还慢好几倍。这里同理，只用 ffmpeg 自带的
#   unsharp / noise 两个轻量滤镜。

# unsharp 的强度上限。太大就会出"锐化光环"，比原来的不一致还难看。
TEXTURE_SHARP_LIMIT = 1.10
# 统一的颗粒强度（0..100 的 ffmpeg noise 尺度）。10 在 1080p 下是"轻微胶片感"，
# 再高就有明显噪点了 —— 我们的目的是**统一纹理**，不是做风格化。
TEXTURE_GRAIN = 8

# ★★ unsharp 的**实测灵敏度**（不是猜的）
#     `tests/test_texture_unify.py` A 段在真实片段上逐个量出来的曲线：
#         unsharp   -0.80  -0.40   0.00  +0.40  +0.80  +1.10
#         实测倍数   0.754  0.870  0.988  1.111  1.234  1.331
#         线性预测   0.200  0.600  1.000  1.400  1.800  2.100   ← 我最初就是这么写的，错得很离谱
#     真实响应 ≈ 1 + 0.30×amount，而且两端会饱和。
#     所以**可修范围只有大约 ±33%**：这是硬限制，不能吹成"想拉多齐就多齐"。
TEXTURE_SENSITIVITY = 0.30
# 实测能达到的倍数区间（用于如实报告"最多能压到多少"）
TEXTURE_MIN_SCALE = 1.0 - TEXTURE_SENSITIVITY * TEXTURE_SHARP_LIMIT   # ≈0.67
TEXTURE_MAX_SCALE = 1.0 + TEXTURE_SENSITIVITY * TEXTURE_SHARP_LIMIT   # ≈1.33


def plan_texture_match(energies: List[float], *,
                       sharp_limit: float = TEXTURE_SHARP_LIMIT,
                       grain: int = TEXTURE_GRAIN) -> Dict[str, Any]:
    """按各段的纹理能量，算出每段该加多少锐化、以及统一加多少颗粒。

    目标值取**中位数**而不是平均值：平均值会被一段极端值（比如纯色动画几乎
    没有高频）拽偏，导致所有真实素材都被过度锐化。

    ★ 反解用的是**实测灵敏度** `1 + 0.30×amount`，不是想当然的 `1 + amount`。
      并且如实报告"最多能压到多少"—— unsharp 可修范围只有 ±33%，
      差得比这多的（比如一段是纯色动画、另一段是实拍）**修不齐**，
      只能靠统一颗粒缩小观感差距。
    """
    vals = [float(x) for x in energies if x and x > 0.05]
    if len(vals) < 2:
        return {"target": 0.0, "amounts": [0.0] * len(energies),
                "grain": 0, "ratio_before": 1.0, "ratio_after_estimate": 1.0,
                "achievable_floor": 1.0,
                "note": "纹理能量数据不足，不做统一"}
    import statistics
    target = float(statistics.median(vals))
    amounts: List[float] = []
    for e in energies:
        e = float(e or 0.0)
        if e <= 0.05:
            # 几乎没有高频（纯色/动画）→ 加锐也长不出细节，如实给 0
            amounts.append(0.0)
            continue
        want_scale = target / e
        a = (want_scale - 1.0) / TEXTURE_SENSITIVITY
        amounts.append(round(max(-sharp_limit, min(sharp_limit, a)), 3))
    ratio_before = round(max(vals) / min(vals), 3)
    # 反解后各段预计落在哪（用实测灵敏度，不是线性假设）
    est = [e * (1.0 + TEXTURE_SENSITIVITY * a)
           for e, a in zip(energies, amounts) if e and e > 0.05]
    ratio_after = round(max(est) / min(est), 3) if len(est) >= 2 else 1.0
    # 理论上能压到的最齐程度（受 ±33% 硬限制）
    floor = round(ratio_before * (TEXTURE_MIN_SCALE / TEXTURE_MAX_SCALE), 3)
    return {
        "target": round(target, 3),
        "amounts": amounts,
        "grain": int(grain),
        "ratio_before": ratio_before,
        "ratio_after_estimate": ratio_after,
        "achievable_floor": max(1.0, floor),
        "note": (f"纹理能量中位数 {target:.2f}；按实测灵敏度 "
                 f"(1+{TEXTURE_SENSITIVITY}×锐化量) 等化，预计 "
                 f"{ratio_before:.2f}x → {ratio_after:.2f}x"
                 f"（unsharp 可修范围只有 ±{TEXTURE_SENSITIVITY*TEXTURE_SHARP_LIMIT*100:.0f}%，"
                 f"理论上最多压到 {max(1.0, floor):.2f}x），再叠加统一颗粒 {grain}"),
    }


def texture_report(stats: List[Dict[str, Any]]) -> Dict[str, Any]:
    """各段纹理能量的离散程度 —— "纹理不统一"的可量化指标。

    `texture_ratio = max/min`：1.0 表示各段纹理完全一致；
    越大说明越像"两台不同的机器拍的"。
    """
    vals = [float(s.get("texture") or 0.0) for s in stats]
    good = [v for v in vals if v > 0.05]
    if len(good) < 2:
        return {"texture_ratio": 1.0, "values": vals, "target": 0.0,
                "verdict": "数据不足"}
    ratio = round(max(good) / min(good), 3)
    import statistics
    return {
        "texture_ratio": ratio,
        "values": [round(v, 3) for v in vals],
        "target": round(float(statistics.median(good)), 3),
        "verdict": ("一致" if ratio < 1.25 else
                    "略不一致" if ratio < 1.8 else "明显不一致（像两台机器拍的）"),
    }


# ═══════════════════════════════════════════════════════════════
# 五、归一化单个片段
# ═══════════════════════════════════════════════════════════════

async def normalize_clip(
    src: str, dst: str, *, w: int, h: int, fps: float,
    head_trim: float = 0.0, tail_trim: float = 0.0,
    color: Optional[Dict[str, Any]] = None, crf: int = 18,
    unsharp: float = 0.0, grain: int = 0,
) -> Dict[str, Any]:
    """把片段变成"可以安全拼接"的标准件：统一尺寸/SAR/fps/pix_fmt，裁掉头尾，
    可选调色，可选纹理等化 + 统一颗粒。

    ★ 画面用 `scale=increase,crop` 即"填满并居中裁切"（cover），**不加黑边**。
      为什么不用留黑边（pad）：黑边一眼就是"素材规格不对"，破坏一镜到底的错觉；
      居中裁切虽然会丢掉边缘画面，但观感是"这个镜头就这么构图的"。
    """
    info = await probe(src)
    dur = float(info.get("duration") or 0)
    start = max(0.0, float(head_trim))
    end = dur - max(0.0, float(tail_trim))
    if end - start < 0.4:
        start, end = 0.0, dur
    keep = max(0.4, end - start)

    # 音频：原片段可能没有音轨 → 补静音，否则 concat/xfade 的音轨对不齐
    chain = [f"scale={w}:{h}:force_original_aspect_ratio=increase",
             f"crop={w}:{h}", "setsar=1", f"fps={fps}"]
    if color and color.get("changed"):
        chain.append(lut_filter(color))
    # 纹理等化：正=加锐、负=柔化。放在调色之后、颗粒之前。
    if abs(float(unsharp or 0.0)) > 0.01:
        chain.append(f"unsharp=5:5:{float(unsharp):.3f}:5:5:0")
    # 统一颗粒：所有段用**同一个**强度，制造共同的"纹理地板"。
    # allf=t+u = 时间变化 + 均匀分布，接近胶片颗粒的观感。
    if int(grain or 0) > 0:
        chain.append(f"noise=alls={int(grain)}:allf=t+u")
    chain += ["format=yuv420p"]
    if start > 0 or keep < dur:
        chain.append(f"trim=start={start:.4f}:duration={keep:.4f}")
        chain.append("setpts=PTS-STARTPTS")

    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    cmd = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", src,
           "-vf", ",".join(chain), "-an",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
           "-pix_fmt", "yuv420p", "-r", f"{fps}", "-movflags", "+faststart", dst]
    rc, txt = await _run(cmd, timeout=900)
    if rc != 0 or not os.path.exists(dst):
        raise RuntimeError(f"归一化失败 {os.path.basename(src)}：{(txt or '')[-300:]}")
    # 音轨单独一条静音轨，长度与画面一致（拼接阶段统一处理音频）
    return {"src": src, "dst": dst, "w": w, "h": h, "fps": fps,
            "trim_head": round(start, 3), "trim_tail": round(max(0.0, dur - end), 3),
            "duration": round(keep, 3),
            "crop_loss": await crop_loss_ratio(info.get("w", w), info.get("h", h), w, h)}


# ═══════════════════════════════════════════════════════════════
# 六、主入口：把一串片段处理成"可以拼接"的形态
# ═══════════════════════════════════════════════════════════════

async def harmonize(
    clips: List[str], work_dir: str, *,
    scene_ids: Optional[List[str]] = None,
    do_color: bool = True, do_trim: bool = False, do_texture: bool = True,
    do_dedup: bool = True,
    target: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """接缝预处理：统一画幅 →（可选）裁头尾 →（同场景）调色 → 给出前后对比数字。

    ★ `do_trim` 默认 **False**，这是量出来的结论，不是偷懒：
      `tests/test_seam_fix.py` 的 B 段实测显示，裁掉"沉降帧"对**接缝跳变**
      没有帮助（5.739 → 5.576，其中 1→2 甚至从 7.209 变差到 7.863）——
      因为裁掉开头几帧，会让边界两帧**更不像**（中间已经动过了）。
      它真正能治的是另一个病：**串联段开头的"冻结重演"**（首帧 = 上一段尾帧，
      模型先原地不动几帧再动），那时接缝处的帧差是**接近 0** 而不是很大。
      所以 `detect_settle_windows` 保留可用，但要在"确知这一段是串联来的"
      时才开 —— 不能当默认。治跳变的正解是 `choose_transition` 的转场摊薄。

    返回：
      {
        ok, target, clips:[{src,dst,...}], out:[归一化后的路径],
        before:{seams:[...]}, after:{seams:[...]}, warnings:[], notes:[]
      }
    """
    import numpy as np
    os.makedirs(work_dir, exist_ok=True)
    warnings: List[str] = []
    notes: List[str] = []

    stats = [await analyze_clip(c) for c in clips]
    tgt = target or plan_target_geometry(stats)
    notes.append(f"统一画幅 {tgt['w']}x{tgt['h']} @ {tgt['fps']:g}fps —— {tgt['reason']}")

    before = seam_report(stats)
    stall_before = stall_report(stats)
    tex_before = texture_report(stats)

    # 画幅不一致 → 必须告警（这是"一眼AI"的头号原因）
    sizes = {f"{s['w']}x{s['h']}" for s in stats if s.get("w")}
    if len(sizes) > 1:
        warnings.append(
            f"这 {len(clips)} 段的画幅**不一致**（{'、'.join(sorted(sizes))}）—— "
            f"拼接处一定会跳。已统一到 {tgt['w']}x{tgt['h']}，"
            f"多出来的边被裁掉。"
            f"**根治办法是让所有分镜用同一个模型和同一个分辨率生成**。")
    for i, s in enumerate(stats):
        loss = await crop_loss_ratio(s.get("w", 0), s.get("h", 0), tgt["w"], tgt["h"])
        if loss > CROP_WARN_RATIO:
            warnings.append(
                f"第 {i+1} 段是 {s['w']}x{s['h']}，和主流画幅差太多，"
                f"为了填满画面裁掉了约 {loss*100:.0f}% 的画面。"
                f"建议按 {tgt['w']}x{tgt['h']} 重新生成这一段。")

    # 同一场景内做温和调色匹配（跨场景不动：白天/夜晚本来就该不一样）
    scene_ids = scene_ids or ["" for _ in clips]
    color_plans: List[Optional[Dict[str, Any]]] = [None] * len(clips)
    if do_color:
        by_scene: Dict[str, List[int]] = {}
        for i, sid in enumerate(scene_ids):
            by_scene.setdefault(sid or f"__nos__{i}", []).append(i)
        n_matched = 0
        for sid, idxs in by_scene.items():
            if len(idxs) < 2:
                continue
            ms = np.array([stats[j]["rgb"]["mean"] for j in idxs])
            ss = np.array([stats[j]["rgb"]["std"] for j in idxs])
            tgt_c = {"mean": [float(x) for x in ms.mean(axis=0)],
                     "std": [float(x) for x in ss.mean(axis=0)]}
            for j in idxs:
                cm = plan_color_match(stats[j]["rgb"], tgt_c)
                if cm["changed"]:
                    color_plans[j] = cm
                    n_matched += 1
        if n_matched:
            notes.append(f"同一场景内做了温和调色匹配（{n_matched} 段，"
                         f"增益限幅 ±{COLOR_GAIN_LIMIT*100:.0f}%、偏移限幅 ±{COLOR_OFF_LIMIT:.0f}）")
        diff_scenes = len({s for s in scene_ids if s})
        if diff_scenes:
            notes.append(f"跨场景的 {diff_scenes} 个场景**不做**调色统一 —— "
                         f"白天和夜晚本来就该不一样，强行拉平反而假")

    # 纹理统一：等化微细节 + 统一颗粒
    tex_plan: Dict[str, Any] = {"amounts": [0.0] * len(clips), "grain": 0}
    texture_plans: List[Dict[str, Any]] = [{} for _ in clips]
    # 归一化之后、纹理处理之前的纹理离散度 —— 这才是"纹理统一"真正要看的前后对比
    tex_post_before: Dict[str, Any] = {"texture_ratio": 1.0, "values": []}

    # 归一化（第一遍：只统一画幅/调色/裁剪/裁重复头，不动纹理）
    #
    # ★ 裁掉多少头，由 `plan_dedup` **按画面自动判定**（cur 开头几帧是不是在重复
    #   prev 的尾帧），不需要上层传"这一段是串联来的"标记。
    #   这和之前那个"通用沉降裁帧"（do_trim）是两回事：
    #     通用沉降裁帧 → 治不了跳变、还可能误删正常起幅（实测 seam_ratio 反而变差）
    #     重复头裁帧   → 精确瞄准"冻结重演"，只删真正重复的那几帧
    dedup = plan_dedup(stats) if do_dedup else [0.0] * len(clips)
    outs: List[str] = []
    metas: List[Dict[str, Any]] = []
    trims: List[Dict[str, float]] = []
    for i, src in enumerate(clips):
        trim = detect_settle_windows(stats[i]) if do_trim else {"head": 0.0, "tail": 0.0}
        trim = {"head": max(float(trim["head"]), float(dedup[i])),
                "tail": float(trim["tail"])}
        trims.append(trim)
        dst = os.path.join(work_dir, f"norm_{i:02d}.mp4")
        try:
            m = await normalize_clip(
                src, dst, w=tgt["w"], h=tgt["h"], fps=tgt["fps"],
                head_trim=trim["head"], tail_trim=trim["tail"],
                color=color_plans[i] if do_color else None)
        except Exception as e:
            logger.warning("第 %d 段归一化失败，退回原片：%s", i + 1, e)
            warnings.append(f"第 {i+1} 段归一化失败（已退回原片，接缝可能不平滑）：{e}")
            outs.append(src)
            metas.append({"src": src, "dst": src, "failed": True})
            continue
        outs.append(dst)
        metas.append(m)

    # ══════════════════════════════════════════════════════════════
    # ★★ 纹理必须在**归一化之后**再量、再动，不能拿原片去算
    # ══════════════════════════════════════════════════════════════
    # 踩过的坑（`tests/test_texture_unify.py` B 段暴露）：
    #   画幅统一本身就会改变高频能量 —— 把 1280x720 裁成 1080x1080 要**放大**高度，
    #   放大就是柔化，高频直接掉一截。拿归一化**前**的能量去算该加多少锐化，
    #   算出来的全是错的（实测：计划给某段 +0.337 加锐，结果它反而掉了 24%）。
    # 所以流程改成：先归一化 → 在归一化产物上量纹理 → 按实测灵敏度算 → 第二遍应用。
    if do_texture:
        try:
            post_stats = [await analyze_clip(p) for p in outs]
            tex_post_before = texture_report(post_stats)
            tex_plan = plan_texture_match([s.get("texture") or 0.0 for s in post_stats])
            if tex_plan.get("grain") or any(abs(a) > 0.01 for a in tex_plan["amounts"]):
                for i, p in enumerate(outs):
                    if metas[i].get("failed"):
                        continue
                    dst2 = os.path.join(work_dir, f"tex_{i:02d}.mp4")
                    try:
                        texture_plans[i] = await normalize_clip(
                            p, dst2, w=tgt["w"], h=tgt["h"], fps=tgt["fps"],
                            unsharp=tex_plan["amounts"][i],
                            grain=tex_plan["grain"])
                        outs[i] = dst2
                        metas[i]["texture_pass"] = True
                    except Exception as e:
                        logger.warning("第 %d 段纹理统一失败（保留未统一的）：%s", i + 1, e)
                        warnings.append(f"第 {i+1} 段纹理统一失败：{e}")
            _rb = tex_plan.get("ratio_before") or 1.0
            if _rb > 1.25:
                notes.append(
                    f"纹理统一：各段（归一化后）高频纹理能量相差 {_rb:.2f} 倍 —— "
                    f"逐段等化到 {tex_plan['target']:.2f}（{'、'.join(f'{a:+.2f}' for a in tex_plan['amounts'])}），"
                    f"再统一叠加颗粒 {tex_plan['grain']}，让不同模型出来的片段"
                    f"看起来像同一批胶片")
            else:
                notes.append(f"纹理本来就一致（相差 {_rb:.2f} 倍），"
                             f"仍统一叠加颗粒 {tex_plan['grain']} 以盖住模型的高频伪影")
            notes.append("纹理说明：" + str(tex_plan.get("note") or ""))
        except Exception as e:
            logger.warning("纹理统一整体失败（保留原样）：%s", e)
            warnings.append(f"纹理统一失败（保留原样）：{e}")

    trimmed = [i + 1 for i, t in enumerate(trims) if t["head"] + t["tail"] > 0.08]
    if any(d > 0.05 for d in dedup):
        _dd = [f"第{i+1}段 {dedup[i]:.2f}s" for i in range(len(dedup)) if dedup[i] > 0.05]
        notes.append(
            "裁掉了『冻结重演』的重复头帧（" + "、".join(_dd) + "）—— "
            "串联生成时模型会先把输入的上一段尾帧原样复现几帧才动起来，"
            "拼起来就是『动着动着定住、再慢慢启动』。这几帧是纯重复，删掉即可。")
    if do_trim and trimmed:
        notes.append(
            "另按通用沉降检测裁掉了片头/片尾：" +
            "、".join(f"第{i+1}段-{trims[i-1]['head']:.2f}s/+{trims[i-1]['tail']:.2f}s"
                      for i in trimmed))

    after_stats = [await analyze_clip(p) for p in outs]
    after = seam_report(after_stats)
    stall_after = stall_report(after_stats)
    tex_after = texture_report(after_stats)

    improved = []
    for i in range(len(before["seams"])):
        b = before["seams"][i].get("seam_ratio")
        a = after["seams"][i].get("seam_ratio") if i < len(after["seams"]) else None
        improved.append({"seam": f"{i+1}→{i+2}", "before": b, "after": a,
                         # 分母不存在时前后都无从比较，如实标出来，
                         # 免得下游把 None 当成 0（"进步了"）来读
                         "meaningful": b is not None and a is not None})
    if before.get("meaningful_count") == 0 and before.get("count"):
        warnings.append(
            f"接缝强度指标本次**无法计算**：{before['count']} 处接缝的两侧素材"
            f"都没有可测的运动（`motion_median` 为 0，通常是图片卡/纯静止画面），"
            f"『跳变是自身运动尺度的几倍』这个参照不存在。"
            f"不要再把这里读成『接缝很自然』—— 它只是没量出来。")

    return {
        "ok": True, "target": tgt, "clips": metas, "out": outs,
        "before": before, "after": after, "improved": improved,
        "stall": {"before": stall_before, "after": stall_after,
                  "dedup": [round(d, 3) for d in dedup]},
        "trims": trims, "color_plans": color_plans,
        "texture": {"raw": tex_before, "post_norm": tex_post_before,
                    "after": tex_after, "plan": tex_plan, "passes": texture_plans,
                    # ★ 真正可比的"前后"：两个都在归一化之后量的。
                    #   `raw` 那个是归一化**前**的，只能当参考 ——
                    #   画幅统一本身会改高频能量（放大=柔化），拿它跟前比是无效对比。
                    "ratio_before": tex_post_before.get("texture_ratio"),
                    "ratio_after": tex_after.get("texture_ratio"),
                    "ratio_raw_before": tex_before.get("texture_ratio")},
        "warnings": warnings, "notes": notes,
    }


# ═══════════════════════════════════════════════════════════════
# 七、接缝指标（before / after 用同一套算法，才可比）
# ═══════════════════════════════════════════════════════════════

def seam_report(stats: List[Dict[str, Any]]) -> Dict[str, Any]:
    """相邻片段的接缝强度：seam_ratio = 接缝处的帧差 ÷ 片段内部的典型帧差。

    ★ 为什么用这个数：它同时涵盖了构图、色调、亮度、主体位置的突变 ——
      观众在拼接点感知到的"跳"，正是这几样一起变。
        ratio ≈ 1  → 这个切点和片子里普通的一次切一样自然
        ratio >> 1 → 明显跳了一下，就是"一眼AI"

    ★ 口径必须与 `tests/analyze_seams.py` **完全一致**，否则 before/after
      不可比、也就没法证明"改好了多少"。所以这里用的同样是
      **逐像素**：A 的尾帧 vs B 的首帧 在 160x90 灰度域的平均绝对差，
      分母是两段"内部相邻帧差中位数"的平均。

    ★ 分母不存在时（两段都是图片卡/零运动）`seam_ratio` 报 `None` 并把
      `meaningful` 标成 False —— 不许拿一个不存在的参照编出天文数字
      （见 `motion_base` 里那次 22307014.465 倍的实测事故）。
    """
    import numpy as np
    out: List[Dict[str, Any]] = []
    for i in range(len(stats) - 1):
        a, b = stats[i], stats[i + 1]
        ma = float(a.get("motion_median") or 0)
        mb = float(b.get("motion_median") or 0)
        base = motion_base(a, b)
        fa, fb = a.get("_last"), b.get("_first")
        if fa is not None and fb is not None and getattr(fa, "shape", None) == \
                getattr(fb, "shape", None):
            boundary = float(np.abs(fa - fb).mean())
        else:
            boundary = 0.0
        rgb_delta = [round(b["rgb"]["mean"][k] - a["rgb"]["mean"][k], 2)
                     for k in range(3)]
        rgb_jump = round(float(np.abs(rgb_delta).mean()), 2)
        # ★ 亮度跳变（0~255 灰度）：观众说的"突然特别亮 / 突然特别暗"量的是这个。
        #   与 `rgb_jump` 的区别：rgb_jump 是**平均色差**（三个通道绝对差的均值），
        #   而"明暗变化"要的是**灰度均值之差** —— 同样 rgb_jump=40 的情况，
        #   "整体变亮/变暗" 和 "只是偏色" 在观感上完全不同。
        #   2026-09-13 用户原话："有的衔接很突兀（突然特别亮、突然特别暗）"。
        _la = _lb = None
        if fa is not None and fb is not None:
            try:
                _la = float(np.asarray(fa, dtype="float32").mean())
                _lb = float(np.asarray(fb, dtype="float32").mean())
            except Exception:
                _la = _lb = None
        luma_delta = (round(abs(_lb - _la), 2) if _la is not None else None)
        out.append({
            "seam": f"{i+1}→{i+2}",
            "seam_diff": round(boundary, 3),
            # 原始均值照报（诊断要用），但**不**当分母用
            "base_motion": round((ma + mb) / 2.0, 4),
            "meaningful": base is not None,
            "rgb_delta": rgb_delta, "rgb_jump": rgb_jump,
            # 上一镜尾帧亮度 → 本镜首帧亮度，以及差值（0~255 量纲）
            "luma_prev_tail": (round(_la, 2) if _la is not None else None),
            "luma_cur_head": (round(_lb, 2) if _lb is not None else None),
            "luma_delta": luma_delta,
            "seam_ratio": (round(boundary / base, 3) if base is not None else None),
            "pace_ratio": (round(max(ma, mb) / min(ma, mb), 2)
                           if min(ma, mb) >= NO_MOTION_EPS else None),
            "verdict": ("无从比较（两段素材本身几乎不动，"
                        "没有『自身运动尺度』可作参照）" if base is None else
                        "自然" if boundary / base < 1.6 else
                        "略跳" if boundary / base < 3.0 else "明显跳变"),
        })
    _vals = [s["seam_ratio"] for s in out if s["seam_ratio"] is not None]
    mean_ratio = round(float(np.mean(_vals)), 3) if _vals else None
    return {"seams": out, "mean_seam_ratio": mean_ratio, "count": len(out),
            "meaningful_count": len(_vals)}
