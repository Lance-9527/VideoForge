# -*- coding: utf-8 -*-
"""VideoForge · 接缝门（Gate Seam）—— 声音侧的接缝对齐、焊缝与量化
==================================================================

## 为什么有这个模块

`core/seamfix.py` 把**画面**侧的接缝问题拆成了五块（画幅统一 / 首尾沉降 / 色温漂移 /
运动节奏 / 不是同一个世界），每块都留了数字。但它对**声音**一个字没说。

Lance 的原话（2026-10-24）：
    「两段 15 秒视频衔接特别差」

两条 15s 素材直接 `concat` 出来的洞，一半在画面，另一半在声音：切点落在一句话/一阵鼓
的中间，前一条的声音突然断、后一条突然起，听感上就是"啪"地换了一台电视。波导翁的做法
（本模块的全部依据）：

  ① **不要靠感觉对齐**：找音频波形最猛的那块（onset/重拍），把切点挪过去，让画面切在
     声音的点上 —— 人耳对"声音事件"的时间定位精度约 10~20ms，对画面亮度跳变只有 100ms
     量级，所以**画面可以错，声音不能错**。
  ② **拿不准就在接缝上叠 0.2~0.4s 的 Whoosh / Impact 把接缝焊住** —— 一段扫频噪声盖过去，
     耳朵会把它当成"换场"的自然过渡，而不是"断带"。
  ③ **J-cut / L-cut：让声音先于画面**。下一段的声音提前 0.2~0.4s 进来、或上一段的声音
     多留 0.2~0.4s，观感上就是"场景换了"而不是"卡了一刀"。这条是电影剪辑的通用手法，
     也是唯一能在**不重渲画面**的前提下把硬切变成软切的办法。

本模块只做声音侧和三件可交付的东西：
  · `audio_profile()`            —— 把任意视频/音频解成 48k 单声道，做频谱通量 onset 检测；
  · `plan_sound_anchored_seams()` —— 每个切点 → 最近 onset → anchor / micro-shift / keep；
  · `plan_paragraph_seams()`      —— 逻辑段落边界 → J-cut + whoosh + 短转场；
  · `synth_whoosh()/write_whoosh()` —— 零成本、确定性、峰值 -6dBFS 的焊缝音效；
  · `measure_seams()`            —— 接缝**前后** 0.3s 的 RMS 落差(dB)、静音长度、突变判定；
  · `build_seam_filter()`        —— 输出可直接拼进 ffmpeg `filter_complex` 的字符串片段
                                    + `[N:a]` 输入索引语义，交给 compose/assemble 的 amix 用。

## 常量是怎么定下来的（每条都有实测依据，不是拍脑袋）

全部在 `%LOCALAPPDATA%\\VideoForge\\data\\outputs\\7b6ee913-...\\shots\\` 的 6 条
15.104s / 864x496 / 24fps / 32k AAC 素材上量过：

· `ONSET_WIN=1024 @48k = 21.3ms`：帧长要盖住一个"撞击"的能量，又不至于糊掉相邻两个
  重拍。1024 点给出 47Hz 频率分辨率（能分辨 55Hz 的底鼓和 80Hz 的低频嗡鸣）。
· `ONSET_HOP=256 @48k = 5.33ms`：onset 时间分辨率 = hop。anchor 容差是 150ms，5.33ms
  的量化误差可以忽略；再小（64）只会让 flux 抖动、检出翻倍。
· `ONSET_BAND=(80, 8000)Hz`：实测素材能量分布 `0-80Hz 25.9% / 80-300Hz 53.1% /
  300-1200Hz 15.2% / 1.2-4kHz 3.9% / 4-12kHz 1.9% / 12k+ 0.0%`。12k 以上是编码器噪声，
  80Hz 以下是直流/房间嗡鸣 —— 两边都只贡献假 onset，砍掉。
· `ONSET_DELTA_DB=0.35`：通量定义成 `mean(max(0, Δ(20log10|X|)))`，单位 dB/bin/帧。
  实测 p50≈2.37、p90≈3.0、p99≈4.5（噪声床的谱抖动本身就有 ~2.4dB/bin/帧），所以阈值必须
  **相对局部中位数**抬高，而不是绝对阈值。取 0.35 → 每条 15s 素材检出 7~42 个 onset
  （0.5~2.8/s）：稀疏素材（`44fc63d5` 7 个）刚好是"能听出来的事件"，密集素材（`a9a2f470`
  42 个）是带节奏的段落。取 0.15 会到 100+（噪声），取 0.7 会漏掉 `a9a2f470` 一半重拍。
· `ONSET_MEDIAN_S=0.50`：自适应阈值的滑动中位数窗口。0.5s ≈ 60~120BPM 的一到两拍，
  能跟住渐强，又不会把孤立撞击吃掉（librosa 默认也是 0.5s 量级）。
· `ONSET_RISE_DB=4.0`：**能量门**，帧 RMS 必须比前 0.3s 的中位数高 4dB。实测这 6 条素材的
  底噪在 -46dBFS 上下抖动 ±3dB（`env p10=-47.8 / p50=-46.3 / p90=-37.6`），光靠谱通量会
  在安静床上一路假触发；4dB 刚好把"床在抖"和"真来了个东西"分开。
· `ONSET_MIN_GAP_S=0.09`：最小间隔 90ms ≈ 667BPM，同一个撞击的多次触发合并成一次。
· `ONSET_EDGE_GUARD_S=0.20`：**首尾 200ms 不采信**。这条是被实测打脸后改的：先用 0.10s，
  结果 6 条素材在首/尾 0.10~0.17s 内全都各触发一次假 onset（`36adb12b` 0.133 与 14.955、
  `26472a83` 0.165 与 14.970、`4307fc07` 0.140、`44fc63d5` 0.130、`c1bb8bf4` 0.170）——
  这是**文件边界效应**：尾部音频在最后 60ms 淡出到数字静音（实测 36adb12b 在 15.04s 之后
  RMS = -107dB），头部是起播 fade-in。最狠的是：两段素材的假 onset 都落在离接缝
  0.13~0.15s 的位置，**正好在 0.15s 的 anchor 容差里**，会把"没对上"判成"完美对上"（自欺）。
  0.20s = 4.8 帧 @24fps ≈ 剪辑上"最后一两个转场帧不落刀"的常识，一次把整个边界簇掐掉。
· `BPM_MIN_CONFIDENCE=0.60`：实测 6 条素材的 BPM 置信度最高只有 0.50（`26472a83` → 130.2BPM），
  其余 ≤0.33，所以全判 `bpm=None`。这是诚实结论而不是漏报：这批 AI 音轨是环境声 + 散落事件，
  **根本没有拍网**，本模块找的是"单个音频事件"，不假设节拍。
· `ANCHOR_TOLERANCE_S=0.15` / `MAX_MICRO_SHIFT_S=0.20`：任务卡规定。15 帧 @24fps 的容差，
  即"挪一帧到六帧"，观众看不出来但耳朵能听出对齐；0.2s 是"再挪就吃掉内容了"的上限
  （15s 素材只见 1.3%）。
· `MIN_SEAM_GAP_S=0.50`：相邻切点调整后的最小间距，防止两个 shift 撞到一起。
· `WHOOSH_PEAK_DBFS=-6`：焊缝峰值。留 6dB 余量，混音再乘 -6dB 后有效峰值约 -12dBFS，
  比台词峰值低 9~11dB —— 听得见"换场"，但不会抢台词。
· `SILENCE_DBFS=-50` / `POP_DB=6` / `SILENCE_GAP_WARN_S=0.12`：接缝判定。6dB 是"人耳明显
  听出音量台阶"的保守线；0.12s 是"能听出空一拍"的门限（低于它只是气口）。

## 诚实边界（哪些是真测的、哪些只是接口）

· **真测**：onset 检测/plan（对 6 条真素材）、whoosh 合成（回读 wav 量峰值）、
  `build_seam_filter` 产出的 filter 字符串（真喂给 ffmpeg 跑通，1 条焊缝 8 条焊缝都跑过）、
  `measure_seams`（对 A|B 拼接前后同一个文件、对现成成片、对无音轨文件）。
· **半验证**：J-cut 在本模块只产出**参数与指令**（`jcut.lead_ms` /
  `next_clip_adelay_delta_ms`），因为本模块不知道 compose 的输入顺序。`D:\\tmp\\test_seam_gate.py`
  里用一条 `atrim + adelay` 分支把同一件事真跑通过（见下面实测结论），但**没有接进 compose**。
· 程序化 whoosh 是**气氛焊缝**，不是"真拟音"。要真实撞击/风声请放素材到 `data/sfx/`。
· **whoosh 是掩蔽，不是压平电平**：它把接缝处两边的能量一起抬起来、把空缝填掉，所以
  "静音缝 / 能量塌陷"会被它治好；但"两边本来就不一样响"的系统性电平台阶它治不好
  （实测 8 个任意切点加完 whoosh，|ΔRMS| 仍有 5~7dB）—— 那种台阶要靠 **J-cut / 交叉淡化**
  （让下一段的声音提前进来，用重叠区做过渡）解决。这两个工具是互补的，别指望谁单独包治。

## 实测结论（2026-10-24，`D:\\tmp\\test_seam_gate.py` 真跑的数字）

素材 = `shots\\36adb12b...mp4`（21 个 onset）+ `26472a83...mp4`（13 个 onset），各 15.104s：

| 项目 | 数字 |
|---|---|
| 接缝位置（两段首尾相接） | 15.104s |
| 最近重拍 | 14.043s（前一段），Δ = **-1.061s** |
| 声锚结论 | 超容差 → micro-shift **-0.200s** → 落点 14.904s，**残余 0.861s** → 必须焊 |
| 焊前 A\\|B 硬拼接 | 接缝前后 RMS -43.53 / -51.24 dB，ΔRMS **-7.71dB**，静音 0.03\\|0.15s，verdict **pop** |
| 焊后（0.20s whoosh，adelay 14994ms） | -31.67 / -33.00 dB，ΔRMS **-1.33dB**，静音 0\\|0s，verdict **smooth** |
| 接缝 ±0.25s 能量 | -45.63 → **-31.51 dBFS**（whoosh 确实进去了） |
| J-cut 落地（lead 0.4s，第二段 adelay 14704ms） | 接缝前 0.4s RMS -43.79 → **-32.68 dBFS**；接缝 ΔRMS -7.71 → **-1.40dB** |
| 8 条焊缝 + `first_input_index=2` | filter 真跑 rc=0；`min_input_count=10` 与 `[2:a]..[9:a]` 语义一致 |
| 现成成片 `雨夜便利店-12秒-480P-新配音.mp4` | **8.1s 之后是数字静音（8.1→12.05s，4 秒没声音）**，measure 判定 pop/gap |


零付费、零联网、零新依赖（numpy + ffmpeg，无 scipy）。
"""

from __future__ import annotations

import copy
import glob
import hashlib
import json
import math
import os
import re
import subprocess
import wave
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

__all__ = [
    "audio_profile",
    "plan_sound_anchored_seams",
    "plan_paragraph_seams",
    "synth_whoosh",
    "write_whoosh",
    "measure_seams",
    "build_seam_filter",
    "selftest",
]

# ---------------------------------------------------------------- 常量（依据见模块头） ---
TARGET_SR = 48000

ONSET_WIN = 1024
ONSET_HOP = 256
ONSET_BAND = (80.0, 8000.0)
ONSET_DELTA_DB = 0.35
ONSET_MEDIAN_S = 0.50
ONSET_RISE_DB = 4.0
ONSET_MIN_GAP_S = 0.09
ONSET_EDGE_GUARD_S = 0.20
ONSET_BACKTRACK_FRAMES = 4          # 起音回溯：峰值往前最多 4 帧（21ms）找 flux 局部极小
ONSET_ENERGY_BG_S = 0.30            # 能量门的背景窗口（前 0.3s）

BPM_MIN_IOI = 6                     # 至少 6 个 IOI（≥7 个 onset）才敢报 BPM
BPM_MIN_CONFIDENCE = 0.60           # IOI 落在拍点整数倍 ±12% 内的比例

ANCHOR_TOLERANCE_S = 0.15           # 卡规定：偏差 ≤0.15s 直接 anchor
MAX_MICRO_SHIFT_S = 0.20            # 卡规定：挪不动就最多挪 0.2s
MIN_SEAM_GAP_S = 0.50               # 相邻切点调整后最小间距
CUT_EDGE_KEEP_S = 0.20              # 调整后切点离时间轴两端至少这么远

JCUT_LEAD_RANGE = (0.20, 0.40)      # 卡规定
WHOOSH_SECONDS_RANGE = (0.20, 0.40)
TRANSITION_SECONDS_RANGE = (0.10, 0.30)
PARAGRAPH_EVERY = 3                 # 默认每 3 个切点挑一个当逻辑段落边界

JCUT_LEAD_OPTIONS_JUNCTION = (0.30, 0.35, 0.40)   # 换场（新场景）→ 给足提前量
JCUT_LEAD_OPTIONS_INNER = (0.20, 0.25, 0.30)      # 段落内切点
TRANSITION_SECONDS_OPTIONS = (0.12, 0.16, 0.20, 0.24, 0.28)
WHOOSH_SECONDS_OPTIONS = (0.20, 0.25, 0.30, 0.35, 0.40)

WHOOSH_PEAK_DBFS = -6.0
WHOOSH_MIX_GAIN_DB = -6.0
WHOOSH_IN_FADE_S = 0.015
PEAK_RATIO = {"whoosh": 0.55, "impact": 0.18, "riser": 0.90}

RMS_WINDOW_S = 0.30                 # 接缝前后各量 0.3s
SILENCE_DBFS = -50.0
SILENCE_BLOCK_S = 0.010
SILENCE_SCAN_LIMIT_S = 1.00
POP_DB = 6.0
DIP_DB = 18.0
SILENCE_GAP_WARN_S = 0.12

ENVELOPE_WIN_S = 0.05

SEAM_OUT_DIR = r"D:\tmp\seam_out"
DEFAULT_SHOTS_DIR = (
    r"C:\Users\Administrator\AppData\Local\VideoForge\data\outputs"
    r"\7b6ee913-5ea1-4b47-8895-856852717a48\shots"
)
DEFAULT_FILM = r"D:\VideoForge-dsh\dist\成片\雨夜便利店-12秒-480P-新配音.mp4"

_HANN_CACHE: Dict[int, np.ndarray] = {}
_PROFILE_CACHE: Dict[tuple, Dict[str, Any]] = {}
_SIGNAL_CACHE: Dict[tuple, np.ndarray] = {}
_DECODE_FAIL: Dict[tuple, str] = {}


# ------------------------------------------------------------------------ 基础工具 ---
def _ffmpeg() -> str:
    """ffmpeg 可执行文件。优先用项目自己的 `core.seamfix._ffmpeg()`（和 seamfix 一致）。"""
    try:
        from core.seamfix import _ffmpeg as _sf_ffmpeg  # type: ignore

        return _sf_ffmpeg()
    except Exception:  # noqa: BLE001
        return "ffmpeg"


def _run(cmd: Sequence[str], timeout: float = 300.0) -> Tuple[int, str, bytes]:
    """同步跑一个外部命令。返回 (rc, stderr_text, stdout_bytes)。不抛异常。"""
    try:
        pr = subprocess.run(
            list(cmd),
            capture_output=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        err = (pr.stderr or b"").decode("utf-8", "replace")
        return pr.returncode, err, pr.stdout or b""
    except Exception as exc:  # noqa: BLE001
        return -1, "%s: %s" % (type(exc).__name__, exc), b""


def _abspath(path: Any) -> str:
    return os.path.abspath(os.path.expanduser(str(path)))


def _probe_duration_seconds(path: str) -> Optional[float]:
    """用 ffmpeg 的 stderr 解出容器时长（纯视频/无音轨时也要拿到时长）。"""
    rc, err, _out = _run([_ffmpeg(), "-nostdin", "-hide_banner", "-i", path], timeout=60.0)
    if rc == 0 and not err:
        return None
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", err or "")
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def _decode_mono(path: str, sample_rate: int = TARGET_SR, timeout: float = 300.0):
    """把任意音轨解成 48k 单声道 float64。无音轨/解码失败返回 (None, 原因)。"""
    cmd = [
        _ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", path,
        "-map", "0:a:0", "-vn",
        "-ac", "1", "-ar", str(int(sample_rate)),
        "-f", "f32le", "-",
    ]
    rc, err, out = _run(cmd, timeout=timeout)
    if rc != 0 or len(out) < 4:
        reason = (err or "").strip().splitlines()
        return None, (reason[-1] if reason else "no audio stream / empty decode")[:400]
    sig = np.frombuffer(out, dtype=np.float32).astype(np.float64)
    if sig.size < 64:
        return None, "decoded audio too short"
    return sig, ""


def _load_signal(path: Any, sample_rate: int = TARGET_SR) -> Optional[np.ndarray]:
    """解码 + 小 LRU 缓存（audio_profile / measure_seams 会重复解码同一个文件）。"""
    p = _abspath(path)
    try:
        st = os.stat(p)
    except OSError:
        return None
    key = (p, st.st_size, st.st_mtime_ns, int(sample_rate))
    if key in _SIGNAL_CACHE:
        return _SIGNAL_CACHE[key]
    sig, err = _decode_mono(p, sample_rate)
    if sig is None:
        _DECODE_FAIL[key] = err
        return None
    if len(_SIGNAL_CACHE) >= 4:
        _SIGNAL_CACHE.pop(next(iter(_SIGNAL_CACHE)))
    _SIGNAL_CACHE[key] = sig
    return sig


def _rms(x: np.ndarray) -> float:
    if x is None or x.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(x))))


def _db(x: float) -> float:
    return float(20.0 * math.log10(max(float(x), 1e-9)))


def _round_list(values: Iterable[float], ndigits: int = 6) -> List[float]:
    return [round(float(v), ndigits) for v in values]


def _hash_unit(*parts: Any) -> float:
    """确定性 0..1 随机数：同一组输入永远同一个值（保证同一时间轴永远同一套方案）。"""
    raw = "|".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha1(raw).digest()[:4], "big") / float(2 ** 32)


def _pick_deterministic(cut: float, options: Sequence[float], tag: str) -> float:
    u = _hash_unit("gate_seam", tag, int(round(float(cut) * 1000.0)))
    return float(options[min(len(options) - 1, int(u * len(options)))])


# ---------------------------------------------------------------- onset（频谱通量） ---
def _rms_envelope(sig: np.ndarray, sample_rate: int, win_s: float = ENVELOPE_WIN_S):
    win = max(16, int(round(win_s * float(sample_rate))))
    n = int(sig.size // win)
    if n <= 0:
        return [], []
    r = np.sqrt(np.mean(np.square(sig[: n * win].reshape(n, win)), axis=1))
    return _round_list(r, 6), [round(_db(float(v)), 2) for v in r]


def _detect_onsets(
    sig: np.ndarray,
    sample_rate: int = TARGET_SR,
    *,
    window: int = ONSET_WIN,
    hop: int = ONSET_HOP,
    band: Tuple[float, float] = ONSET_BAND,
    delta_db: float = ONSET_DELTA_DB,
    median_s: float = ONSET_MEDIAN_S,
    rise_db: float = ONSET_RISE_DB,
    min_gap_s: float = ONSET_MIN_GAP_S,
    edge_guard_s: float = ONSET_EDGE_GUARD_S,
):
    """频谱通量 onset 检测（numpy 手写，无 scipy）。

    步骤：分帧 → Hann → |rfft| → 取 80~8kHz 频带 → 20log10 帧间正差求均值(=通量) →
    局部中位数阈值 + 能量门 → 局部极大 → 起音回溯 → 最小间隔合并 → 掐首尾边界。

    返回 (onsets 秒, flux, frame_rms_db)。onsets 是 np.float64 数组。
    """
    sr = float(sample_rate)
    if sig.size < window * 4:
        return np.zeros(0), np.zeros(0), np.zeros(0)

    n_frames = 1 + int((sig.size - window) // hop)
    idx = np.arange(window)[None, :] + hop * np.arange(n_frames)[:, None]
    if window not in _HANN_CACHE:
        _HANN_CACHE[window] = np.hanning(window)
    frames = sig[idx] * _HANN_CACHE[window]
    mag = np.abs(np.fft.rfft(frames, axis=1)) + 1e-7
    freqs = np.fft.rfftfreq(window, 1.0 / sr)
    band_mask = (freqs >= band[0]) & (freqs <= band[1])
    logmag = 20.0 * np.log10(mag[:, band_mask])
    diff = np.diff(logmag, axis=0, prepend=logmag[:1])
    flux = np.maximum(diff, 0.0).mean(axis=1)
    frame_rms = np.sqrt(np.mean(np.square(frames), axis=1))
    frame_db = 20.0 * np.log10(frame_rms + 1e-9)

    med_w = int(round(median_s * sr / hop))
    med_w += 1 - (med_w % 2)                      # 中位数窗口取奇数
    half = med_w // 2
    padded = np.pad(flux, (half, half), mode="edge")
    local_median = np.median(
        np.lib.stride_tricks.sliding_window_view(padded, med_w), axis=1
    )
    bg_w = max(2, int(round(ONSET_ENERGY_BG_S * sr / hop)))
    padded_db = np.pad(frame_db, (bg_w, 0), mode="edge")
    bg_db = np.median(
        np.lib.stride_tricks.sliding_window_view(padded_db, bg_w)[:n_frames], axis=1
    )

    times = np.arange(n_frames) * hop / sr
    duration = sig.size / sr
    peaks: List[Tuple[float, float]] = []
    for i in range(1, n_frames - 1):
        if flux[i] <= local_median[i] + delta_db:
            continue
        if flux[i] < flux[i - 1] or flux[i] < flux[i + 1]:   # 必须是局部极大
            continue
        if frame_db[i] < bg_db[i] + rise_db:                 # 能量门
            continue
        lo = max(0, i - ONSET_BACKTRACK_FRAMES)
        j = lo + int(np.argmin(flux[lo : i + 1]))            # 起音回溯
        t = float(times[j])
        if t < edge_guard_s or t > duration - edge_guard_s:
            continue                                         # 首尾边界假 onset
        if peaks and t - peaks[-1][0] < min_gap_s:
            if flux[i] > peaks[-1][1]:
                peaks[-1] = (t, float(flux[i]))
            continue
        peaks.append((t, float(flux[i])))

    onsets = np.array([p[0] for p in peaks], dtype=np.float64)
    return onsets, flux, frame_db


def _estimate_bpm(onsets: np.ndarray):
    """从 IOI 估计 BPM（折到 60~180）。onset 太少/太乱返回 (None, 置信度)。

    实测这 6 条 AI 素材里 BPM 置信度最高只有 0.50（`26472a83` → 130.2BPM），其余 ≤0.33，
    所以阈值定在 `BPM_MIN_CONFIDENCE=0.60`：**宁可不报，也不假报一个拍速**。这不是漏报 ——
    这些音轨本来就是环境声 + 散落事件，没有稳定节拍，"找最近重拍"实际是在找单个音频事件。
    """
    if onsets is None or onsets.size < BPM_MIN_IOI + 1:
        return None, 0.0
    ioi = np.diff(np.sort(onsets))
    ioi = ioi[(ioi > 0.10) & (ioi < 2.0)]
    if ioi.size < BPM_MIN_IOI:
        return None, 0.0
    period = float(np.median(ioi))
    while period < 0.3333:
        period *= 2.0
    while period >= 1.0:
        period /= 2.0
    # 置信度：各 IOI 落在 period 整数倍 ±12% 内的比例
    k = np.maximum(1.0, np.round(ioi / period))
    err = np.abs(ioi - k * period) / (k * period)
    conf = float(np.mean(err <= 0.12))
    if conf < BPM_MIN_CONFIDENCE:
        return None, conf
    return round(60.0 / period, 1), conf


def audio_profile(path_or_wav: Any, *, sample_rate: int = TARGET_SR, use_cache: bool = True) -> Dict[str, Any]:
    """把一条视频/音频解成 48k 单声道并做音乐性分析。

    返回:
        {"has_audio": True, "path", "duration", "sample_rate", "onsets":[秒], "bpm",
         "bpm_confidence", "rms_envelope":[每 50ms 线性 RMS], "rms_envelope_db":[...],
         "rms_mean", "rms_mean_db", "peak_db", "n_frames", "onset_rate"}
    无音轨返回 {"has_audio": False, ...}，**不抛异常**。
    """
    p = _abspath(path_or_wav)
    base: Dict[str, Any] = {"path": p, "sample_rate": int(sample_rate)}
    if not os.path.exists(p):
        return dict(base, has_audio=False, duration=0.0, reason="file_not_found")

    st = os.stat(p)
    key = (p, st.st_size, st.st_mtime_ns, int(sample_rate))
    if use_cache and key in _PROFILE_CACHE:
        return copy.deepcopy(_PROFILE_CACHE[key])

    sig = _load_signal(p, sample_rate)
    if sig is None:
        prof = dict(
            base,
            has_audio=False,
            duration=float(_probe_duration_seconds(p) or 0.0),
            reason="no_audio_stream",
            detail=_DECODE_FAIL.get(key, "decode failed"),
        )
        if use_cache:
            _PROFILE_CACHE[key] = prof
        return copy.deepcopy(prof)

    sr = int(sample_rate)
    duration = float(sig.size) / sr
    onsets, _flux, _fdb = _detect_onsets(sig, sr)
    bpm, conf = _estimate_bpm(onsets)
    env, env_db = _rms_envelope(sig, sr)
    rms_mean = _rms(sig)
    prof = dict(
        base,
        has_audio=True,
        duration=round(duration, 4),
        onsets=_round_list(onsets, 3),
        bpm=bpm,
        bpm_confidence=round(float(conf), 3),
        rms_envelope=env,
        rms_envelope_db=env_db,
        rms_mean=round(rms_mean, 6),
        rms_mean_db=round(_db(rms_mean), 2),
        peak_db=round(_db(float(np.max(np.abs(sig)))), 2),
        n_frames=int(sig.size),
        onset_rate=round(onsets.size / duration, 2) if duration > 0 else 0.0,
        constants={
            "window": ONSET_WIN, "hop": ONSET_HOP, "band": list(ONSET_BAND),
            "delta_db": ONSET_DELTA_DB, "median_s": ONSET_MEDIAN_S,
            "rise_db": ONSET_RISE_DB, "min_gap_s": ONSET_MIN_GAP_S,
            "edge_guard_s": ONSET_EDGE_GUARD_S,
        },
    )
    if use_cache:
        _PROFILE_CACHE[key] = prof
    return copy.deepcopy(prof)


# ------------------------------------------------------------------ 素材与切点归一化 ---
def _as_list(clips: Any) -> List[Any]:
    if clips is None:
        return []
    if isinstance(clips, (str, os.PathLike, dict)):
        return [clips]
    return list(clips)


def _clip_meta(clips: Any, *, sample_rate: int = TARGET_SR) -> List[Dict[str, Any]]:
    """把 clips 归一成 [{index,path,offset,duration,has_audio,onsets,bpm,rms_mean_db}]。

    clips 元素可以是：
      · 路径字符串；
      · {"path": ..., "offset": 可选绝对起点, "duration": 可选};
      · {"profile": <audio_profile 结果>, "duration": ...}（已解过的直接给，省一次解码）。
    """
    out: List[Dict[str, Any]] = []
    offset = 0.0
    for i, item in enumerate(_as_list(clips)):
        profile: Optional[Dict[str, Any]] = None
        path = ""
        if isinstance(item, dict):
            path = str(item.get("path") or item.get("file") or "")
            if item.get("offset") is not None:
                offset = float(item["offset"])
            if isinstance(item.get("profile"), dict):
                profile = item["profile"]
            dur_override = item.get("duration")
        else:
            path = str(item)
            dur_override = None

        if profile is None:
            if path:
                profile = audio_profile(path, sample_rate=sample_rate)
            else:
                profile = {"has_audio": False, "duration": 0.0}

        duration = float(profile.get("duration") or dur_override or 0.0)
        out.append(
            {
                "index": i,
                "path": _abspath(path) if path else "",
                "offset": round(offset, 6),
                "duration": round(duration, 6),
                "has_audio": bool(profile.get("has_audio")),
                "onsets": list(profile.get("onsets") or []),
                "bpm": profile.get("bpm"),
                "rms_mean_db": profile.get("rms_mean_db"),
            }
        )
        offset += duration
    return out


def _normalize_cut(cut: Any, metas: Sequence[Dict[str, Any]], total: float) -> float:
    """切点归一成"整条时间轴上的绝对秒"。支持 float / (clip_index, local) / dict。"""
    if isinstance(cut, dict):
        if "time" in cut and cut["time"] is not None:
            t = float(cut["time"])
        else:
            idx = int(cut.get("clip", cut.get("clip_index", 0)))
            local = float(cut.get("local", cut.get("local_time", 0.0)))
            off = metas[idx]["offset"] if 0 <= idx < len(metas) else 0.0
            t = off + local
    elif isinstance(cut, (list, tuple)) and len(cut) >= 2:
        idx = int(cut[0])
        local = float(cut[1])
        off = metas[idx]["offset"] if 0 <= idx < len(metas) else 0.0
        t = off + local
    else:
        t = float(cut)
    return max(0.0, min(t, max(0.0, total)))


def _clip_summary(metas: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "index": m["index"],
            "path": m["path"],
            "offset": m["offset"],
            "duration": m["duration"],
            "has_audio": m["has_audio"],
            "onsets": len(m["onsets"]),
            "bpm": m["bpm"],
            "rms_mean_db": m["rms_mean_db"],
        }
        for m in metas
    ]


# ------------------------------------------------------- ① 声音锚点：把切点挪到重拍上 ---
def plan_sound_anchored_seams(
    clips: Any,
    cut_times: Sequence[Any],
    *,
    tolerance: float = ANCHOR_TOLERANCE_S,
    micro_shift: float = MAX_MICRO_SHIFT_S,
    min_gap: float = MIN_SEAM_GAP_S,
    sample_rate: int = TARGET_SR,
) -> Dict[str, Any]:
    """把每个切点挪到"最近的重拍"上。这是波导翁法①。

    对每个切点 cut：
      · 在**整条时间轴**的 onset 里找最近的一个；
      · |delta| ≤ tolerance → action="anchor"，shift=delta（切点直接落到重拍上）；
      · |delta| > tolerance → action="shift"，shift=±min(micro_shift,|delta|)
        （只挪最多 0.2s，挪不到位就交给 whoosh 焊，**不许为了凑拍吃掉内容**）；
      · 没有任何 onset → action="keep"，shift=0。

    delta 定义：`delta = nearest_onset - cut`（正=重拍在切点之后）。
    落地后 `new_cut = cut + shift`，`residual = delta - shift`（残余偏差）。

    cut_times 可以是：绝对秒 float / (素材序号, 素材内秒) / {"clip":i,"local":s}。
    clips 顺序即时间轴顺序（首尾相接）。

    返回 {"seams":[{cut,nearest_onset,delta,action,shift,new_cut,residual,...}],
          "max_abs_delta_before", "max_abs_delta_after", "note", ...}
    """
    metas = _clip_meta(clips, sample_rate=sample_rate)
    total = float(sum(m["duration"] for m in metas))

    timeline_onsets: List[Tuple[float, int]] = []
    for m in metas:
        for o in m["onsets"]:
            t = m["offset"] + float(o)
            if 0.0 <= t <= total:
                timeline_onsets.append((t, m["index"]))
    timeline_onsets.sort(key=lambda x: x[0])
    onset_arr = np.array([t for t, _ in timeline_onsets], dtype=np.float64)

    seams: List[Dict[str, Any]] = []
    for raw_cut in cut_times or []:
        cut = _normalize_cut(raw_cut, metas, total)
        nearest = None
        delta = None
        owner = None
        if onset_arr.size:
            k = int(np.argmin(np.abs(onset_arr - cut)))
            nearest, owner = timeline_onsets[k]
            delta = float(nearest - cut)

        if delta is None:
            action, shift = "keep", 0.0
        elif abs(delta) <= float(tolerance):
            action, shift = "anchor", delta
        else:
            action = "shift"
            shift = math.copysign(min(float(micro_shift), abs(delta)), delta)

        seams.append(
            {
                "cut": round(cut, 4),
                "nearest_onset": round(float(nearest), 4) if nearest is not None else None,
                "delta": round(delta, 4) if delta is not None else None,
                "action": action,
                "shift": round(shift, 4),
                "new_cut": None,          # 等冲突消解后再填
                "residual": None,
                "nearest_clip": owner,
                "conflict": False,
                "explain": _explain_seam(action, delta, shift),
            }
        )

    # ---- 冲突消解：相邻切点调整后间距 < min_gap 时，两边 shift 各退一半 ----
    order = sorted(range(len(seams)), key=lambda i: seams[i]["cut"])
    for _ in range(4):
        changed = False
        for a, b in zip(order, order[1:]):
            gap = (seams[b]["cut"] + seams[b]["shift"]) - (seams[a]["cut"] + seams[a]["shift"])
            if gap < float(min_gap):
                seams[a]["shift"] = round(seams[a]["shift"] * 0.5, 4)
                seams[b]["shift"] = round(seams[b]["shift"] * 0.5, 4)
                seams[a]["conflict"] = seams[b]["conflict"] = True
                changed = True
        if not changed:
            break

    # ---- 落地 + 边界夹取 + 统计 ----
    before_vals: List[float] = []
    after_vals: List[float] = []
    for s in seams:
        cut = float(s["cut"])
        new_cut = cut + float(s["shift"])
        lo, hi = CUT_EDGE_KEEP_S, max(CUT_EDGE_KEEP_S, total - CUT_EDGE_KEEP_S)
        clamped = min(max(new_cut, lo), hi)
        if abs(clamped - new_cut) > 1e-9:
            s["shift"] = round(clamped - cut, 4)
            s["conflict"] = True
            new_cut = clamped
        s["new_cut"] = round(new_cut, 4)
        if s["delta"] is not None:
            s["residual"] = round(float(s["delta"]) - float(s["shift"]), 4)
            before_vals.append(abs(float(s["delta"])))
            after_vals.append(abs(float(s["residual"])))

    max_before = round(max(before_vals), 4) if before_vals else None
    max_after = round(max(after_vals), 4) if after_vals else None
    n_anchor = sum(1 for s in seams if s["action"] == "anchor")
    n_shift = sum(1 for s in seams if s["action"] == "shift")
    n_keep = sum(1 for s in seams if s["action"] == "keep")

    if not seams:
        note = "没有切点。"
    elif not onset_arr.size:
        note = "素材里没有可用重拍（无音轨或 onset 为 0）→ 全部 keep，只能靠 whoosh 焊。"
    else:
        note = "%d 个切点：anchor %d / shift %d / keep %d；最大偏差 %s → %s s。" % (
            len(seams), n_anchor, n_shift, n_keep,
            ("%.3f" % max_before) if max_before is not None else "—",
            ("%.3f" % max_after) if max_after is not None else "—",
        )
        if max_after is not None and max_after > float(tolerance):
            note += (
                " 仍有 %.3fs 没落到拍上（micro-shift 上限 %.2fs）→ 切点交给 whoosh/J-cut 焊。"
                % (max_after, float(micro_shift))
            )
        else:
            note += " 全部切点已落在重拍 ±%.0fms 内。" % (float(tolerance) * 1000)

    return {
        "kind": "sound_anchored",
        "tolerance": float(tolerance),
        "max_micro_shift": float(micro_shift),
        "timeline_duration": round(total, 4),
        "clips": _clip_summary(metas),
        "onsets_total": int(onset_arr.size),
        "seams": seams,
        "max_abs_delta_before": max_before,
        "max_abs_delta_after": max_after,
        "counts": {"anchor": n_anchor, "shift": n_shift, "keep": n_keep},
        "next_action": "anchor_ok" if (max_after is not None and max_after <= float(tolerance)) else "weld_with_whoosh",
        "note": note,
    }


def _explain_seam(action: str, delta: Optional[float], shift: float) -> str:
    if delta is None:
        return "素材里没有 onset，无法锚定。"
    if action == "anchor":
        if abs(delta) <= 1e-6:
            return "切点本来就压在重拍上，不动。"
        return "偏差 %.0fms ≤ 容差，把切点挪到重拍上（shift=%+.0fms）。" % (delta * 1000, shift * 1000)
    if action == "shift":
        return (
            "最近重拍差 %+.0fms（超容差），只挪 %+.0fms 靠过去；剩下的 %.0fms 用 whoosh 焊。"
            % (delta * 1000, shift * 1000, (delta - shift) * 1000)
        )
    return "没有可用的重拍。"


# ----------------------------------------------------------- ② 段落焊缝：J-cut + whoosh ---
def _default_boundaries(metas: Sequence[Dict[str, Any]], all_cuts: Optional[Sequence[Any]] = None,
                        total: float = 0.0) -> List[float]:
    """默认逻辑段落边界 = 每条素材的结尾（= 下一条的开头）+ all_cuts 里每 3 个挑一个。"""
    out: List[float] = []
    acc = 0.0
    for m in metas[:-1]:
        acc += float(m["duration"])
        out.append(acc)
    if all_cuts:
        for k, c in enumerate(all_cuts):
            if (k + 1) % PARAGRAPH_EVERY == 0:
                out.append(_normalize_cut(c, metas, total))
    return out


def plan_paragraph_seams(
    clips: Any,
    boundaries: Optional[Sequence[Any]] = None,
    *,
    whoosh: bool = True,
    all_cuts: Optional[Sequence[Any]] = None,
    junctions: Optional[Sequence[Any]] = None,
    sample_rate: int = TARGET_SR,
) -> Dict[str, Any]:
    """只对**逻辑段落边界**安排 J-cut + whoosh + 短转场（不是每个切点都上，否则全片都在"嗖"）。

    boundaries=None 时按默认规则取：每条素材首尾（= 素材接缝）+ `all_cuts` 里每
    PARAGRAPH_EVERY(=3) 个切点挑一个。

    `junctions`：显式指定哪些边界算"换场"。默认用素材接缝（累加时长）自动判定；
    但当上游把它挪过（`plan_sound_anchored_seams` 给的 `new_cut` 会偏离接缝），
    传 `junctions=[new_cut]` 才能继续按"换场"给长 J-cut + 扫频 whoosh。否则它会被当成
    段落内切点，拿"短 J-cut + impact"，语义就错了。

    每个边界给：
      · jcut_lead 0.20~0.40s：下一段**音频**提前进入的量（声先于画）。换场给 0.30~0.40，
        段落内 0.20~0.30；用切点毫秒数做确定性哈希选值 → 同一时间轴永远同一套方案。
      · whoosh：{style, seconds, path, peak_ratio}，0.20~0.40s，峰值恰好落在切点上
        （start = cut - peak_ratio*seconds）。换场用 "whoosh"（扫频），段落内用 "impact"（撞击）。
      · transition：{type:"xfade", name:"fade"|"dissolve", seconds:0.10~0.30}。
        **禁止 1s 溶解**：溶解太长会让人觉得"卡了"，0.1~0.3s 只是把接缝藏进动作里。

    返回 {"seams":[{cut,jcut_lead,whoosh,transition,...}], "rules_echo":{...}, ...}
    """
    metas = _clip_meta(clips, sample_rate=sample_rate)
    total = float(sum(m["duration"] for m in metas))

    if boundaries is None:
        raw_boundaries = _default_boundaries(metas, all_cuts, total)
    else:
        raw_boundaries = [_normalize_cut(b, metas, total) for b in boundaries]

    # 去重（0.25s 内算同一个边界）+ 丢掉贴着片头/片尾的（那里没有"下一段"可 J-cut）
    bounds: List[float] = []
    for b in sorted(raw_boundaries):
        if b < 0.15 or b > total - 0.15:
            continue
        if bounds and b - bounds[-1] < 0.25:
            continue
        bounds.append(b)

    if junctions is not None:
        junction_set = {round(_normalize_cut(j, metas, total), 3) for j in junctions}
    else:
        junction_set = set()
        acc = 0.0
        for m in metas[:-1]:
            acc += float(m["duration"])
            junction_set.add(round(acc, 3))

    seams: List[Dict[str, Any]] = []
    for cut in bounds:
        is_junction = any(abs(cut - j) <= 0.02 for j in junction_set)
        lead_options = JCUT_LEAD_OPTIONS_JUNCTION if is_junction else JCUT_LEAD_OPTIONS_INNER
        jcut_lead = _pick_deterministic(cut, lead_options, "jcut")
        whoosh_secs = _pick_deterministic(cut, WHOOSH_SECONDS_OPTIONS, "whoosh_seconds")
        tr_secs = _pick_deterministic(cut, TRANSITION_SECONDS_OPTIONS, "transition_seconds")
        style = "whoosh" if is_junction else "impact"
        peak_ratio = PEAK_RATIO.get(style, PEAK_RATIO["whoosh"])
        tr_name = "fade" if is_junction else "dissolve"

        whoosh_spec: Optional[Dict[str, Any]] = None
        if whoosh:
            path = write_whoosh(whoosh_secs, style=style, sample_rate=sample_rate)
            whoosh_spec = {
                "style": style,
                "seconds": round(whoosh_secs, 3),
                "path": path,
                "peak_ratio": peak_ratio,
                "peak_dbfs": WHOOSH_PEAK_DBFS,
                "mix_gain_db": WHOOSH_MIX_GAIN_DB,
                "starts_at": round(max(0.0, cut - peak_ratio * whoosh_secs), 4),
            }

        seams.append(
            {
                "cut": round(cut, 4),
                "jcut_lead": round(jcut_lead, 3),
                "whoosh": whoosh_spec,
                "transition": {
                    "type": "xfade",
                    "name": tr_name,
                    "seconds": round(tr_secs, 3),
                },
                "junction": bool(is_junction),
                "why": (
                    "换场：声音提前 %.2fs 进入 + %.2fs 扫频焊缝 + %.2fs 转场。"
                    % (jcut_lead, whoosh_secs, tr_secs)
                    if is_junction
                    else "段落内切点：轻 J-cut %.2fs + %.2fs 撞击 + %.2fs 转场。"
                    % (jcut_lead, whoosh_secs, tr_secs)
                ),
            }
        )

    rules_echo = {
        "jcut_lead_s": [JCUT_LEAD_RANGE[0], JCUT_LEAD_RANGE[1]],
        "jcut_options_junction": list(JCUT_LEAD_OPTIONS_JUNCTION),
        "jcut_options_inner": list(JCUT_LEAD_OPTIONS_INNER),
        "whoosh_s": [WHOOSH_SECONDS_RANGE[0], WHOOSH_SECONDS_RANGE[1]],
        "whoosh_peak_ratio": dict(PEAK_RATIO),
        "whoosh_peak_dbfs": WHOOSH_PEAK_DBFS,
        "transition_s": [TRANSITION_SECONDS_RANGE[0], TRANSITION_SECONDS_RANGE[1]],
        "transition_forbidden": "禁止 ≥0.5s 溶解；1s dissolve 会让人觉得卡带",
        "paragraph_every": PARAGRAPH_EVERY,
        "anchor_tolerance_s": ANCHOR_TOLERANCE_S,
        "why": "声音先于画面（J-cut）：耳朵先被告知换场，眼睛到的时候已有预期，硬切就变成软切。",
    }
    return {
        "kind": "paragraph",
        "timeline_duration": round(total, 4),
        "clips": _clip_summary(metas),
        "seams": seams,
        "counts": {"junction": sum(1 for s in seams if s["junction"]),
                   "inner": sum(1 for s in seams if not s["junction"])},
        "rules_echo": rules_echo,
        "note": "段落边界 %d 处；每处 J-cut + whoosh + 短转场，其余切点走 plan_sound_anchored_seams。"
                % len(seams),
    }


# ------------------------------------------------------------ ③ 焊缝音效：程序化 whoosh ---
def _sweep_lowpass(x: np.ndarray, sample_rate: int, f0: float, f1: float) -> np.ndarray:
    """时变一阶低通（截止频率 f0→f1 指数扫频）。

    没有 scipy，也不值得为 0.3s 的 14400 个样点引入 FFT 分块；直接一阶 IIR 逐点递推
    （实测 0.4s 波形 <20ms，可接受）。一阶低通在扫频时的相位/幅度特性不完美，但对
    "嗖"这种噪声焊缝足够了 —— 耳朵听的是扫频方向，不是频响曲线。
    """
    n = int(x.size)
    f = np.geomspace(max(20.0, float(f0)), max(20.0, float(f1)), n)
    a = 1.0 - np.exp(-2.0 * np.pi * f / float(sample_rate))
    y = np.empty(n, dtype=np.float64)
    acc = 0.0
    for i in range(n):
        acc += a[i] * (x[i] - acc)
        y[i] = acc
    return y


def _envelope(style: str, duration: float, sample_rate: int) -> np.ndarray:
    """焊缝包络：起音平滑（避免咔哒）、峰值落在 PEAK_RATIO 处、尾部衰减到 0。"""
    n = max(64, int(round(duration * sample_rate)))
    t = np.arange(n, dtype=np.float64) / float(sample_rate)
    u = t / max(duration, 1e-6)
    peak = PEAK_RATIO.get(style, PEAK_RATIO["whoosh"])
    if style == "impact":
        env = np.exp(-np.clip(u, 0.0, 1.0) / 0.18)
        attack_n = max(1, int(0.002 * sample_rate))
        env[:attack_n] *= np.linspace(0.0, 1.0, attack_n)
    else:
        atk = np.clip(u / max(peak, 1e-6), 0.0, 1.0)
        atk = np.sin(atk * np.pi / 2.0) ** 1.5
        tau = 0.22 if style == "whoosh" else 0.07
        decay = np.where(u > peak, np.exp(-(u - peak) / tau), 1.0)
        env = atk * decay
    fade_n = max(2, int(0.010 * sample_rate))          # 尾端 10ms 收干净
    env[-fade_n:] *= np.linspace(1.0, 0.0, fade_n)
    env[0] = 0.0
    return env


def synth_whoosh(duration: float = 0.30, *, style: str = "whoosh",
                 sample_rate: int = TARGET_SR) -> np.ndarray:
    """程序化合成焊缝音效。**确定性**（同一 style+duration 永远同一条波形）、零成本。

    做法（`style="whoosh"`，卡上要的那一种）：
      白噪 → 一阶低通 250→6000Hz 指数扫频（"由远及近"的方向感）→ 叠一层 4k→12kHz
      的高频噪声（"空气"）→ 包络（起音到 55% 达峰、之后指数衰减）→ 峰值归一化到
      -6dBFS（留 6dB 余量、绝不削波）→ 尾端 10ms 淡出。

    另有 `impact`（55Hz 低频撞击 + 短噪声，峰值在 18%）和 `riser`（更长起音，90% 达峰）。

    返回 float64 单声道波形（峰值 = -6dBFS = 0.5012）。
    """
    dur = max(0.05, min(float(duration), 2.0))
    sr = int(sample_rate) or TARGET_SR
    style = style if style in PEAK_RATIO else "whoosh"
    n = max(64, int(round(dur * sr)))

    seed = int.from_bytes(
        hashlib.sha1(("gate_seam|whoosh|%s|%.4f" % (style, dur)).encode("utf-8")).digest()[:8],
        "big",
    )
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(n)
    noise2 = rng.standard_normal(n)
    t = np.arange(n, dtype=np.float64) / float(sr)

    if style == "impact":
        body = np.sin(2.0 * np.pi * 55.0 * t + 0.35 * np.sin(2.0 * np.pi * 3.1 * t))
        air = _sweep_lowpass(noise, sr, 100.0, 2500.0)
        sig = 0.85 * body + 0.45 * air
    elif style == "riser":
        body = _sweep_lowpass(noise, sr, 200.0, 9000.0)
        air = _sweep_lowpass(noise2, sr, 3000.0, 14000.0)
        sig = body + 0.25 * air
    else:  # whoosh
        body = _sweep_lowpass(noise, sr, 250.0, 6000.0)
        air = _sweep_lowpass(noise2, sr, 4000.0, 12000.0)
        sig = body + 0.25 * air

    env = _envelope(style, dur, sr)
    x = sig * env
    peak = float(np.max(np.abs(x)))
    if peak > 0.0:
        x = x * (10.0 ** (WHOOSH_PEAK_DBFS / 20.0) / peak)
    return np.clip(x, -0.999, 0.999)


def write_whoosh(duration: float = 0.30, *, style: str = "whoosh",
                 sample_rate: int = TARGET_SR) -> str:
    """把 synth_whoosh 落成 16bit 单声道 wav（`D:\\tmp\\seam_out\\whoosh_*.wav`），返回路径。

    幂等：同参数已存在且非空就直接复用（不重复写盘、不重复合成）。
    """
    style = style if style in PEAK_RATIO else "whoosh"
    dur = max(0.05, min(float(duration), 2.0))
    sr = int(sample_rate) or TARGET_SR
    os.makedirs(SEAM_OUT_DIR, exist_ok=True)
    name = "whoosh_%s_%05dms_sr%d.wav" % (style, int(round(dur * 1000)), sr)
    path = os.path.join(SEAM_OUT_DIR, name)
    if os.path.exists(path) and os.path.getsize(path) > 44:
        return path

    x = synth_whoosh(dur, style=style, sample_rate=sr)
    pcm = np.clip(np.round(x * 32767.0), -32768, 32767).astype(np.int16)
    tmp = path + ".part"
    with wave.open(tmp, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    os.replace(tmp, path)
    return path


def _read_wav_mono(path: str) -> Tuple[np.ndarray, int]:
    with wave.open(path, "rb") as w:
        n, sw, ch, sr = w.getnframes(), w.getsampwidth(), w.getnchannels(), w.getframerate()
        raw = w.readframes(n)
    if sw == 2:
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    elif sw == 4:
        x = np.frombuffer(raw, dtype=np.int32).astype(np.float64) / 2147483648.0
    elif sw == 1:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128.0) / 128.0
    else:
        raise ValueError("unsupported sample width: %d" % sw)
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, int(sr)


# ------------------------------------------------------------ ④ 接缝量化（真量，不是估） ---
def _trailing_silence(sig: np.ndarray, end_index: int, sample_rate: int,
                      thr_db: float = SILENCE_DBFS) -> float:
    """从 end_index 往回看，紧贴切点的**连续静音**有多长（秒，最多 1s）。"""
    block = max(1, int(round(SILENCE_BLOCK_S * sample_rate)))
    limit = int(round(SILENCE_SCAN_LIMIT_S * sample_rate))
    total = 0
    i = int(end_index)
    lo = max(0, i - limit)
    while i - block >= lo:
        seg = sig[i - block : i]
        if _db(_rms(seg)) < thr_db:
            total += block
            i -= block
        else:
            break
    return round(total / float(sample_rate), 3)


def _leading_silence(sig: np.ndarray, start_index: int, sample_rate: int,
                     thr_db: float = SILENCE_DBFS) -> float:
    block = max(1, int(round(SILENCE_BLOCK_S * sample_rate)))
    limit = int(round(SILENCE_SCAN_LIMIT_S * sample_rate))
    total = 0
    i = int(start_index)
    hi = min(sig.size, i + limit)
    while i + block <= hi:
        seg = sig[i : i + block]
        if _db(_rms(seg)) < thr_db:
            total += block
            i += block
        else:
            break
    return round(total / float(sample_rate), 3)


def measure_seams(
    video_path: Any,
    cut_times: Sequence[Any],
    *,
    sample_rate: int = TARGET_SR,
    window_s: float = RMS_WINDOW_S,
    silence_dbfs: float = SILENCE_DBFS,
    pop_db: float = POP_DB,
    dip_db: float = DIP_DB,
    gap_warn_s: float = SILENCE_GAP_WARN_S,
) -> Dict[str, Any]:
    """**真量**每个接点：切点前/后各 0.3s 的 RMS(dB)、能量落差(dB)、静音长度、是否突变。

    · delta_db = rms_after_db - rms_before_db（正=切完更响，负=切完掉能量）
    · silence_before/after：紧贴切点、低于 `silence_dbfs` 的连续静音秒数（数字静音/断轨才抓得到）
    · verdict：
        "pop"       |delta_db| ≥ pop_db(6) —— 音量台阶，人耳能听出"啪"一下 → 要焊
        "dip"       delta_db ≤ -dip_db(18) —— 切完塌了，像断带
        "gap"       静音 ≥ gap_warn_s(0.12) —— 切点处空了一拍
        "smooth"    以上都不是
    **注意**：这个函数量的是"焊之前有多烂"，用来决定**要不要焊**；焊完再量，`delta_db`
    不一定会变小 —— whoosh 是**掩蔽**（两边一起抬、把空缝填掉），不是**压平电平**。
    系统性电平台阶要靠 J-cut / 交叉淡化。实测：主接缝 -7.71 → -1.33dB（因为是静音缝，焊好了），
    但 8 个任意切点加完 whoosh 仍有 5~7dB 台阶（因为两边本来就不同响）。
    返回 {"has_audio", "seams":[{cut, rms_before_db, rms_after_db, delta_db,
          silence_before, silence_after, verdict, needs_weld}], "overall"}
    """
    prof = audio_profile(video_path, sample_rate=sample_rate)
    path = prof.get("path")
    if not prof.get("has_audio"):
        return {
            "has_audio": False,
            "path": path,
            "duration": prof.get("duration", 0.0),
            "seams": [],
            "overall": {"n": 0, "verdict": "no_audio",
                        "note": "没有音轨，接缝只能从画面侧处理（见 core/seamfix.py）。"},
        }

    sig = _load_signal(path, sample_rate)
    if sig is None:
        return {"has_audio": False, "path": path, "seams": [],
                "overall": {"n": 0, "verdict": "decode_failed"}}

    sr = int(sample_rate)
    n = int(sig.size)
    duration = n / float(sr)
    win = max(1, int(round(float(window_s) * sr)))
    seams: List[Dict[str, Any]] = []

    for raw_cut in cut_times or []:
        if isinstance(raw_cut, dict):
            cut = float(raw_cut.get("time", raw_cut.get("local", 0.0)))
        elif isinstance(raw_cut, (list, tuple)):
            cut = float(raw_cut[-1])          # (clip, local) 形式：这里只取本地/绝对秒
        else:
            cut = float(raw_cut)
        cut = max(0.0, min(cut, duration))
        i_cut = int(round(cut * sr))
        i0 = max(0, i_cut - win)
        i2 = min(n, i_cut + win)

        before = sig[i0:i_cut]
        after = sig[i_cut:i2]
        rms_b = _rms(before)
        rms_a = _rms(after)
        db_b = round(_db(rms_b), 2) if before.size else None
        db_a = round(_db(rms_a), 2) if after.size else None
        delta = round(_db(rms_a) - _db(rms_b), 2) if (before.size and after.size) else None

        sil_b = _trailing_silence(sig, i_cut, sr, silence_dbfs)
        sil_a = _leading_silence(sig, i_cut, sr, silence_dbfs)

        if delta is None:
            verdict, needs = "edge", False
        elif abs(delta) >= float(pop_db):
            verdict, needs = "pop", True
        elif delta <= -float(dip_db):
            verdict, needs = "dip", True
        elif max(sil_b, sil_a) >= float(gap_warn_s):
            verdict, needs = "gap", True
        else:
            verdict, needs = "smooth", False

        seams.append(
            {
                "cut": round(cut, 4),
                "rms_before_db": db_b,
                "rms_after_db": db_a,
                "delta_db": delta,
                "silence_before": sil_b,
                "silence_after": sil_a,
                "verdict": verdict,
                "needs_weld": bool(needs),
            }
        )

    deltas = [abs(s["delta_db"]) for s in seams if s["delta_db"] is not None]
    needs_n = sum(1 for s in seams if s["needs_weld"])
    overall = {
        "n": len(seams),
        "max_abs_delta_db": round(max(deltas), 2) if deltas else None,
        "mean_abs_delta_db": round(float(np.mean(deltas)), 2) if deltas else None,
        "needs_weld": needs_n,
        "verdict": "weld_recommended" if needs_n else "ok",
        "thresholds": {"pop_db": float(pop_db), "dip_db": float(dip_db),
                       "silence_dbfs": float(silence_dbfs), "gap_warn_s": float(gap_warn_s),
                       "window_s": float(window_s)},
        "note": (
            "%d 个接点，%d 个需要焊缝（|ΔRMS|≥%.0fdB 或 静音≥%.2fs）。"
            % (len(seams), needs_n, float(pop_db), float(gap_warn_s))
            if seams else "没有接点。"
        ),
    }
    return {"has_audio": True, "path": path, "duration": round(duration, 4),
            "sample_rate": sr, "seams": seams, "overall": overall}


# ------------------------------------------------------ ⑤ 给 ffmpeg 的可拼接片段 + 索引 ---
def _bracket(label: str) -> str:
    s = str(label).strip()
    return s if s.startswith("[") else "[" + s + "]"


def _strip_bracket(label: str) -> str:
    s = str(label).strip()
    return s[1:-1] if s.startswith("[") and s.endswith("]") else s


def build_seam_filter(
    plan: Dict[str, Any],
    *,
    sample_rate: int = TARGET_SR,
    base_label: str = "[0:a]",
    first_input_index: int = 1,
    whoosh_gain_db: float = WHOOSH_MIX_GAIN_DB,
    output_label: str = "seam_aout",
) -> Dict[str, Any]:
    """把 plan 变成**可直接拼进 filter_complex** 的字符串 + 输入顺序说明。

    做的三件事：
      ① 基础音轨：`[0:a]` → aresample/aformat 统一成 `[gbase]`；
      ② 每条 whoosh：`[N:a]` → aresample/aformat/volume/afade(in)/adelay → `[gswK]`，
         `adelay` 让 whoosh 的**峰值正好落在切点上**（start = cut - peak_ratio*seconds）；
      ③ amix 求和：`[gbase][gsw0][gsw1]...amix=inputs=K:duration=longest:
         dropout_transition=0:normalize=0,aresample=48000[seam_aout]`
         （`normalize=0` 关键：否则 ffmpeg 会把每路按 1/N 衰减，焊缝就听不见了）。

    `[N:a]` 索引语义：N 是**你 ffmpeg 命令里 -i 的顺序，从 0 数**。
    默认 `first_input_index=1`（假设 0 是拼好的视频/基础混音），第 k 个 whoosh 用
    `N = first_input_index + k`。若 Lead 的 compose 里 0..3 已被素材/配音/BGM 占用，
    传 `first_input_index=4` 即可，其余不用改。

    J-cut **不是** filter 片段（它要改动下一段素材音频的 adelay，属于 compose 的输入
    布局），所以单独放在返回的 `jcut` 里：`next_clip_adelay_delta_ms` 是"下一段素材音频
    的 adelay 该减掉多少毫秒"（视频不动 → 声先于画）。

    返回 {"filter", "mix", "full", "output_label", "inputs", "jcut", "input_note", ...}
    """
    seams = list(plan.get("seams") or [])
    sr = int(sample_rate)

    base_in = _bracket(base_label)
    base_out = "[gbase]"
    base_chain = (
        "%saresample=%d,aformat=sample_fmts=fltp:channel_layouts=stereo%s"
        % (base_in, sr, base_out)
    )

    chains: List[str] = []
    labels: List[str] = []
    inputs: List[Dict[str, Any]] = []
    jcuts: List[Dict[str, Any]] = []
    weld_index = 0

    for seam_i, seam in enumerate(seams):
        cut = float(seam.get("cut") or 0.0)
        if seam.get("jcut_lead") is not None:
            lead_ms = int(round(float(seam["jcut_lead"]) * 1000.0))
            jcuts.append(
                {
                    "seam_index": seam_i,
                    "cut": round(cut, 4),
                    "lead_ms": lead_ms,
                    "next_clip_adelay_delta_ms": -lead_ms,
                    "hint": "下一段素材音频的 adelay 减去 lead_ms（视频 offset 不动）→ 声先于画",
                }
            )

        spec = seam.get("whoosh") or {}
        if not spec or not spec.get("style"):
            continue
        style = str(spec.get("style"))
        seconds = float(spec.get("seconds") or 0.30)
        path = spec.get("path") or write_whoosh(seconds, style=style, sample_rate=sr)
        peak_ratio = float(spec.get("peak_ratio") or PEAK_RATIO.get(style, PEAK_RATIO["whoosh"]))
        start = max(0.0, cut - peak_ratio * seconds)
        delay_ms = int(round(start * 1000.0))
        slot = int(first_input_index) + weld_index
        lab = "[gsw%d]" % weld_index
        chains.append(
            "[%d:a]aresample=%d,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            "volume=%.1fdB,afade=t=in:st=0:d=%.3f,adelay=%d|%d%s"
            % (slot, sr, float(whoosh_gain_db), WHOOSH_IN_FADE_S, delay_ms, delay_ms, lab)
        )
        labels.append(lab)
        inputs.append(
            {
                "slot": slot,
                "label": "[%d:a]" % slot,
                "path": path,
                "role": "whoosh",
                "seam_index": seam_i,
                "cut": round(cut, 4),
                "style": style,
                "seconds": round(seconds, 3),
                "peak_ratio": peak_ratio,
                "starts_at": round(start, 4),
                "delay_ms": delay_ms,
                "gain_db": float(whoosh_gain_db),
                "why": "峰值落在 cut 上（start = cut - %.2f*%.2fs）" % (peak_ratio, seconds),
            }
        )
        weld_index += 1

    if labels:
        mix = (
            "[gbase]%samix=inputs=%d:duration=longest:dropout_transition=0:normalize=0,"
            "aresample=%d[%s]" % ("".join(labels), 1 + len(labels), sr, output_label)
        )
        full = ";".join([base_chain] + chains + [mix])
        out_lab = output_label
    else:
        mix = ""
        full = "%saresample=%d,aformat=sample_fmts=fltp:channel_layouts=stereo[%s]" % (
            base_in, sr, output_label,
        )
        out_lab = output_label

    input_note = (
        "[N:a] 的 N = ffmpeg 命令里 -i 的顺序（0 基）。本方案假设 [%s] 是基础音轨，"
        "whoosh 输入从 -i 序号 %d 开始（第 k 条 whoosh 用 [%d:a]）。"
        "whoosh 是**额外输入文件**（-i %s），不是滤镜生成，所以要按 inputs 顺序加 -i。"
        % (
            _strip_bracket(base_label),
            int(first_input_index),
            int(first_input_index),
            (inputs[0]["path"] if inputs else "<无>"),
        )
    )

    return {
        "filter": full,
        "chains": chains,
        "mix": mix,
        "full": full,
        "output_label": out_lab,
        "base_label": base_in,
        "first_input_index": int(first_input_index),
        "n_welds": len(inputs),
        "inputs": inputs,
        "jcut": jcuts,
        "input_note": input_note,
        # 你的 ffmpeg 命令里至少要存在 0..min_input_count-1 这些 -i 序号，否则
        # filter_complex 会报 "Error binding filtergraph inputs/outputs"
        "min_input_count": int(first_input_index) + len(inputs),
        "map": "-map 0:v -map [%s] -c:v copy -c:a aac -b:a 192k" % out_lab,
        "note": (
            "把 full 直接塞进 -filter_complex；whoosh 按 inputs 顺序补 -i。"
            "jcut 需要在 compose 的 adelay 里落地（本模块不渲染）。"
            if inputs else "这个 plan 没有 whoosh，只有基础音轨直通。"
        ),
    }


# ---------------------------------------------------------------- ⑥ selftest（真跑） ---
def _fmt_profile(p: Dict[str, Any]) -> str:
    name = os.path.basename(p.get("path") or "")
    if not p.get("has_audio"):
        return "%-42s 无音轨 dur=%.3fs" % (name, float(p.get("duration") or 0.0))
    return "%-42s dur=%.3fs onsets=%d bpm=%s rms=%sdBFS env_med=%.1fdB" % (
        name,
        float(p.get("duration") or 0.0),
        len(p.get("onsets") or []),
        p.get("bpm"),
        p.get("rms_mean_db"),
        float(np.median(p.get("rms_envelope_db") or [0.0])),
    )


def _fmt_plan(plan: Dict[str, Any], limit: int = 12) -> str:
    lines = [plan.get("note", "")]
    for s in (plan.get("seams") or [])[:limit]:
        if plan.get("kind") == "paragraph":
            w = s.get("whoosh") or {}
            t = s.get("transition") or {}
            lines.append(
                "  cut=%7.3fs  jcut=+%.2fs  whoosh=%s/%.2fs(%s)  xfade=%s/%.2fs"
                % (
                    float(s.get("cut") or 0.0), float(s.get("jcut_lead") or 0.0),
                    w.get("style"), float(w.get("seconds") or 0.0),
                    os.path.basename(w.get("path") or ""),
                    t.get("name"), float(t.get("seconds") or 0.0),
                )
            )
        else:
            lines.append(
                "  cut=%7.3fs  onset=%-9s delta=%-9s action=%-6s shift=%+.3fs residual=%s"
                % (
                    float(s.get("cut") or 0.0),
                    ("%.3f" % s["nearest_onset"]) if s.get("nearest_onset") is not None else "—",
                    ("%+.3f" % s["delta"]) if s.get("delta") is not None else "—",
                    s.get("action"),
                    float(s.get("shift") or 0.0),
                    ("%+.3f" % s["residual"]) if s.get("residual") is not None else "—",
                )
            )
    return "\n".join(lines)


def _fmt_measure(m: Dict[str, Any]) -> str:
    if not m.get("has_audio"):
        return "无音轨：%s" % (m.get("overall", {}).get("note", ""))
    lines = ["  %s" % (m.get("overall", {}).get("note", ""))]
    for s in m.get("seams") or []:
        lines.append(
            "  cut=%7.3fs  RMS %7.2f→%7.2f dB  Δ=%+7.2f dB  静音 %.3f|%.3f s  verdict=%s"
            % (
                float(s["cut"]),
                float(s["rms_before_db"] if s["rms_before_db"] is not None else -180.0),
                float(s["rms_after_db"] if s["rms_after_db"] is not None else -180.0),
                float(s["delta_db"] if s["delta_db"] is not None else 0.0),
                float(s["silence_before"]),
                float(s["silence_after"]),
                s["verdict"],
            )
        )
    return "\n".join(lines)


def selftest(shots_dir: Optional[str] = None, film_path: Optional[str] = None,
             film_cuts: Optional[Sequence[float]] = None, *, verbose: bool = True) -> Dict[str, Any]:
    """真跑：两条 15s 素材的 onset/锚点方案 + whoosh 音量 + 一条成片的接缝量化。

    素材目录默认 `%LOCALAPPDATA%\\VideoForge\\data\\outputs\\7b6ee913-...\\shots`
    （取 mtime 最新的两条 = Lance 抱怨的那一对），可用环境变量 `VF_SEAM_SHOTS` 覆盖。
    """
    import time as _time

    t0 = _time.time()
    shots_dir = shots_dir or os.environ.get("VF_SEAM_SHOTS") or DEFAULT_SHOTS_DIR
    files = sorted(glob.glob(os.path.join(shots_dir, "*.mp4")), key=os.path.getmtime)
    if len(files) < 2:
        raise FileNotFoundError("素材不足两条：%s" % shots_dir)
    pair = files[-2:]                                   # mtime 最新的两条（旧→新 = 时间轴顺序）
    film_path = film_path or os.environ.get("VF_SEAM_FILM") or DEFAULT_FILM

    report: Dict[str, Any] = {"shots_dir": shots_dir, "pair": pair, "film": film_path}

    profiles = [audio_profile(p) for p in pair]
    report["profiles"] = profiles

    junction = float(profiles[0].get("duration") or 0.0)
    anchor_plan = plan_sound_anchored_seams(pair, [junction])
    report["anchor_plan"] = anchor_plan

    paragraph_plan = plan_paragraph_seams(pair, None)
    report["paragraph_plan"] = paragraph_plan

    w = synth_whoosh(0.30, style="whoosh")
    wpath = write_whoosh(0.30, style="whoosh")
    back, wsr = _read_wav_mono(wpath)
    whoosh_info = {
        "path": wpath,
        "samples": int(w.size),
        "sample_rate": int(wsr),
        "seconds": round(float(w.size) / wsr, 4),
        "peak_dbfs": round(_db(float(np.max(np.abs(w)))), 3),
        "file_peak_dbfs": round(_db(float(np.max(np.abs(back)))), 3),
        "rms_dbfs": round(_db(_rms(w)), 2),
        "file_bytes": os.path.getsize(wpath),
        "deterministic": bool(np.array_equal(w, synth_whoosh(0.30, style="whoosh"))),
    }
    report["whoosh"] = whoosh_info

    seam_filter = build_seam_filter(paragraph_plan)
    report["seam_filter"] = seam_filter

    cuts = list(film_cuts) if film_cuts is not None else [3.0, 6.0, 8.1, 9.0]
    film_measure = measure_seams(film_path, cuts) if os.path.exists(film_path) else {
        "has_audio": False, "seams": [],
        "overall": {"n": 0, "verdict": "film_missing", "note": "成片不存在：%s" % film_path},
    }
    report["film_measure"] = film_measure
    report["film_cuts"] = cuts
    report["elapsed_s"] = round(_time.time() - t0, 2)

    if verbose:
        print("=" * 78)
        print("接缝门 selftest（真跑素材）")
        print("=" * 78)
        print("[1] 素材 profile（频谱通量 onset，48k 单声道）")
        for p in profiles:
            print("   " + _fmt_profile(p))
            print("      onsets(秒): %s%s" % (
                [round(o, 3) for o in (p.get("onsets") or [])[:14]],
                " ..." if len(p.get("onsets") or []) > 14 else ""))
        print()
        print("[2] 声音锚点方案（切点 = 两段素材接缝 %.3fs）" % junction)
        print(_fmt_plan(anchor_plan))
        print("    max|delta| before=%s after=%s  next_action=%s" % (
            anchor_plan["max_abs_delta_before"], anchor_plan["max_abs_delta_after"],
            anchor_plan["next_action"]))
        print()
        print("[3] 段落焊缝方案（J-cut + whoosh + 短转场）")
        print(_fmt_plan(paragraph_plan))
        print()
        print("[4] whoosh 波形（回读 wav 实测）")
        print("    %s  n=%d  %.3fs  peak=%.2fdBFS(文件实读)  rms=%.1fdBFS  %.1fKB  deterministic=%s"
              % (os.path.basename(wpath), whoosh_info["samples"], whoosh_info["seconds"],
                 whoosh_info["file_peak_dbfs"], whoosh_info["rms_dbfs"],
                 whoosh_info["file_bytes"] / 1024.0, whoosh_info["deterministic"]))
        print()
        print("[5] build_seam_filter 输出（可直接拼进 filter_complex）")
        print("    " + seam_filter["filter"])
        print("    " + seam_filter["input_note"])
        for jc in seam_filter["jcut"]:
            print("    jcut: seam#%d cut=%.3fs lead=%dms → 下一段 adelay %+dms"
                  % (jc["seam_index"], jc["cut"], jc["lead_ms"], jc["next_clip_adelay_delta_ms"]))
        print()
        print("[6] measure_seams 真量成片接缝（3/6/9s 是等分参考位；8.1s 是实测音频断点")
        print("    —— 该成片 8.1→12.05s 是数字静音，用来看 measure_seams 能不能抓出 gap/pop）")
        print("    " + os.path.basename(film_path))
        print(_fmt_measure(film_measure))
        print("    overall: %s" % json.dumps(film_measure.get("overall", {}), ensure_ascii=False))
        print()
        print("elapsed %.2fs" % report["elapsed_s"])
        print("=" * 78)
    return report


if __name__ == "__main__":  # `py -3.14 -X utf8 core\gate_seam.py`
    selftest()
