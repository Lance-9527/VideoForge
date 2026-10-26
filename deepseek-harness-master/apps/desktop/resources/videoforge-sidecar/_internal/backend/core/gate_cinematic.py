"""电影感门（cinematic gate）：**全片一套 look + 统一画幅 + 统一轻颗粒 + 色偏量化**。

Lance 反馈第 3 条「电影感不足」。诊断不是"缺滤镜"，而是**每一帧都在做选择**：

  · 同一部片里每段各自平衡白/曝光 → 观众看到的是"两台机器拍的"（色偏漂移）；
  · 画幅不统一 → 有的 16:9、有的更宽，拼在一起像素材带；
  · AI 生成的锐边太干净 → 缺胶片的那层"底噪"，一眼假。

本模块只做三件**全片统一**的事，并把它们变成可复算的数字：

  ① `measure_look()`  —— 逐段抽中间帧，量 `mean_rgb / luma / saturation /
     warmth(r-b) / contrast(std)`，再给出**片段间极差 drift = max-min**。
     这是"色偏/冷暖差"的量化口径：不是看平均值，而是看**段与段之间的差**。
  ② `plan_look()`     —— 全片选**一个** look（teal_orange / bleach_bypass /
     warm_film 三选一），每段只允许 ±0.1 EV 的曝光微调；
     并在本机 ffmpeg 上**真跑一遍**（抽帧套滤镜再量）给出 `expected_drift_after`。
  ③ `aspect_filter()` / `build_grain_filter()` —— 2.39:1 画幅（letterbox / crop）
     与统一颗粒（强度默认 8，固定 seed）。

━━━ 三条硬约束（这是本门与"逐镜配平"（`core.seamfix`）的分工边界）━━━━━━━━━

1. **一个 look 用到尾**：所有段共用同一条 LUT/滤镜链，只有曝光可以按段微调
   （±0.1 EV）。不许各段调不同色温 —— 那正是 seamfix 的 `plan_texture_match` /
   `plan_luma_bridge` 在做的事（逐镜同场景配平），本模块**刻意不碰**。
2. **只修"片间漂移"，不重画风格**：全片共用一条 LUT 时，色偏的压低上限
   由 LUT 的**色度增益**决定（`saturation≈色度增益`）：cast 的极差大约按 s 缩放。
   本模块如实报告"能压到多少"，不吹成"归零"。
3. **滤镜必须在本机真跑通**：`selftest()` 用 lavfi 合成源真跑每一条链；
   `plan_look(verify=True)` 会抽帧套滤镜再量。不是只拼字符串。

━━━ 本机实测（ffmpeg 8.1.1-full / 备用 7.1，见 `selftest()["ffmpeg"]`）━━━━━━

* `curves=red='0/0 0.25/0.2 …':green=…:blue=…` —— 逐通道控制点，可用；
* `lutrgb=r='clip(val*1.07,0,255)'` —— 可用（与 `seamfix.lut_filter` 同一写法）；
  注意本机 `lutrgb` 里写 `t`/`n` 会报 "Undefined constant"（见 seamfix 注释）；
* `eq=saturation=…`、`noise=alls=…:allf=t+u:all_seed=…`、`pad`/`crop` 均可用；
* `pad` / `crop` 的 **w/h/x/y 支持表达式**（`iw`/`ih`/`ow`/`oh`），
  所以 `source_size=None` 时也能给出与分辨率无关的通用滤镜。

━━━ 画幅几何：2.39:1 到底是"上下黑边"还是"左右黑边"━━━━━━━━━━━━━━━━━━

**这不是选风格，是几何。** 目标画幅 T 比素材宽（T > w/h）时：

  · `crop`     ：裁掉上下的画面（**唯一能真的得到 2.39:1 画面**的做法）；
  · `letterbox`：不裁画面。要把 1.74:1 的整幅画面装进 2.39:1 的画布，
                 只能**加宽画布** → 黑边在**左右**（pillarbox）。
                 只有当素材本身**比目标更宽**（如 21:9 → 2.39:1）时，
                 letterbox 的黑边才在上下。

本机素材 864x496 = 1.742:1（比 16:9 还窄一点），所以：
  · `aspect_filter("2.39:1", mode="crop")`      → 864x362，画面 2.39:1，**无黑边**；
  · `aspect_filter("2.39:1", mode="letterbox")` → 1184x496，画面 1.74:1 完整保留，
                                                   左右各 160px 黑边。

字幕安全区（`SUBTITLE_SAFE_RATIO = 0.12`）：
  · **黑边里不放字幕** —— 平台/播放器对黑边的处理各不相同（裁边、拉伸、加模糊），
    放进去就会被切掉或糊掉；
  · 字幕只落在**画面区**内，且距画面底 ≥ 画面高的 12%（`subtitle_safe_area()`
    给出像素数）—— 底边 12% 是字幕/安全框的通行下限，低于它容易被进度条、
    "相关推荐"遮住。

零付费、零联网：只调本机 ffmpeg，不下载 LUT 文件（LUT 用控制点**现场生成**）。
"""

from __future__ import annotations

import glob as _glob
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "GATE_VERSION",
    "LOOKS",
    "ASPECT_TARGETS",
    "SUBTITLE_SAFE_RATIO",
    "GRAIN_DEFAULT",
    "GRAIN_SEED",
    "EXPOSURE_LIMIT_EV",
    "choose_look",
    "look_curves",
    "look_chain",
    "measure_look",
    "plan_look",
    "build_look_filter",
    "build_grain_filter",
    "aspect_filter",
    "aspect_geometry",
    "subtitle_safe_area",
    "apply_filter_to_clip",
    "render_plan",
    "selftest",
]

GATE_VERSION = "1.0.0"

# ═══════════════════════════════════════════════════════════════════════════
# 一、三个 look：全片共用的一条链（curves 控制点 + 色度增益）
# ═══════════════════════════════════════════════════════════════════════════
#
# 为什么是 **逐通道控制点（curves）** 而不是 lutrgb 表达式：
#   · curves 的控制点就是"LUT 的采样点"，写进代码即可复现，不需要下载 .cube 文件；
#   · 逐通道曲线能表达 split-tone：r 暗部压低/亮部抬高、b 反向，
#     就是"暗部青、亮部橙"的经典 teal-orange；
#   · 数值全是 0..1，跨 ffmpeg 版本稳定（8.1.1 与 7.1 都实测过）。
#
# ★★ 一个反直觉但必须写死的约束：**三条通道在"工作区间"里必须有相同斜率**。
#   只有斜率相同，通道均值（mean_c）与亮度（luma）才按同一个倍数缩放，
#   `cast = mean_c − luma` 才是"纯色偏"、才会按色度增益 s **收缩**。
#   一旦逐通道斜率不同（比如 r 用陡 S、b 用缓 S），那么"这一段整体比那段亮"
#   就会被折算成"这一段比那段更偏橙" —— 色偏读数反而**变大**。
#   实测踩过：teal_orange 第一版给 r/b 各配一条自己的 S 曲线，
#   cast 极差 7.33 → 10.37 级、冷暖极差 12.55 → 16.41（look 把色偏放大了 41%）。
#   所以现在的结构是：
#     · `base`  ：三通道**共用**的胶片曲线（抬黑位 + 轻压高光 + 工作区间缓斜率）；
#     · `tint`  ：每通道一个**常数偏移**（改成"整体偏青/偏橙"的色温，不动斜率）；
#     · `ends`  ：只改**首末控制点**的 split-tone（暗部/亮部各偏一点，
#                 也就是真正的 teal-orange；中间工作区间斜率不动）。
#   这条不变式由 `selftest()` 断言（"三通道中点斜率相等"）。
#
# 为什么每个 look 都带 `saturation < 1`：
#   全片共用一条 LUT 时，**色偏极差能被压低的倍数 ≈ 色度增益**（几何上：
#   cast = 通道均值 − luma，eq 的 saturation 正是绕 luma 缩放 cast）。
#   所以 s 是"这版 look 能修掉多少色偏"的显式参数，而不是顺手的调色口味。
#   AI 素材普遍偏艳，轻微去饱和本身也是"电影感"的一部分。
#
# curve 格式：`base` 为 [(x, y), ...]，x/y 均 0..1、x 严格递增、首点 x=0、末点 x=1；
# `tint` 为每通道常数偏移（-0.1..0.1）；`ends` 为每通道 (暗部偏移, 亮部偏移)。
LOOKS: Dict[str, Dict[str, Any]] = {
    "teal_orange": {
        "zh": "青橙（teal-orange）",
        "why": "现代剧情/都市/科技片的标准 look：暗部偏青、亮部偏橙，肤色被推到暖侧。",
        "saturation": 0.78,
        "base": [(0.00, 0.035), (0.15, 0.145), (0.50, 0.452), (0.85, 0.768), (1.00, 0.975)],
        "tint": {"red": 0.012, "green": 0.000, "blue": -0.018},
        "ends": {"red": (-0.020, 0.006), "green": (0.000, 0.000), "blue": (0.040, -0.055)},
    },
    "bleach_bypass": {
        "zh": "漂白（bleach bypass）",
        "why": "高对比 + 低饱和 + 提黑位，纪实/战争/压抑段落用；修色偏最狠的一档。",
        "saturation": 0.45,
        "base": [(0.00, 0.055), (0.15, 0.150), (0.50, 0.470), (0.85, 0.800), (1.00, 0.975)],
        "tint": {"red": 0.000, "green": 0.000, "blue": 0.000},
        "ends": {"red": (0.000, 0.000), "green": (0.000, 0.000), "blue": (0.000, 0.000)},
    },
    "warm_film": {
        "zh": "暖调胶片（warm film）",
        "why": "整体偏暖、黑位发灰的怀旧感；治愈/家庭/回忆段落用。",
        "saturation": 0.88,
        "base": [(0.00, 0.050), (0.15, 0.160), (0.50, 0.470), (0.85, 0.780), (1.00, 0.980)],
        "tint": {"red": 0.025, "green": 0.008, "blue": -0.030},
        "ends": {"red": (0.000, 0.000), "green": (0.000, 0.000), "blue": (0.010, -0.010)},
    },
}

_CURVE_KEYS = ("red", "green", "blue")


def look_curves(look: str) -> Dict[str, List[Tuple[float, float]]]:
    """把 look 的 `base + tint + ends` 展开成三条**逐通道控制点**。

    `tint` 加在**所有**控制点上（常数偏移 → 不动斜率）；
    `ends` 只加在 x=0 / x=1 两个端点（split-tone 只发生在暗部与亮部）。
    这样中间工作区间的三通道斜率严格相等 —— 见上面那条"反直觉约束"。
    """
    if look not in LOOKS:
        raise ValueError(f"未知 look: {look!r}（可选: {sorted(LOOKS)}）")
    spec = LOOKS[look]
    base = [(float(x), float(y)) for x, y in spec["base"]]
    tint = spec.get("tint") or {}
    ends = spec.get("ends") or {}
    out: Dict[str, List[Tuple[float, float]]] = {}
    for ch in _CURVE_KEYS:
        off = float(tint.get(ch, 0.0))
        e0, e1 = (ends.get(ch) or (0.0, 0.0))
        pts: List[Tuple[float, float]] = []
        for x, y in base:
            d = off + (float(e0) if x <= 0.0 else (float(e1) if x >= 1.0 else 0.0))
            pts.append((x, round(_clamp(y + d, 0.0, 1.0), 6)))
        out[ch] = pts
    return out

#: 素材画幅 → 目标画幅的常用档
ASPECT_TARGETS: Dict[str, float] = {
    "2.39:1": 2.39,
    "2.40:1": 2.40,
    "2.35:1": 2.35,
    "2.20:1": 2.20,
    "2.00:1": 2.00,
    "1.85:1": 1.85,
    "1.66:1": 1.66,
    "16:9": 16.0 / 9.0,
    "4:3": 4.0 / 3.0,
}

#: 字幕底边安全线（画面高的比例）。0.12 是字幕/安全框的通行下限：
#: 再往下会被播放器进度条、"相关推荐"遮住；黑边里更是绝对不能放（见模块 docstring）。
SUBTITLE_SAFE_RATIO = 0.12

#: 统一颗粒强度（0..100 的 ffmpeg noise 尺度）。
#: 与 `core.seamfix.TEXTURE_GRAIN` 对齐（selftest 会断言两者相等）：
#: 8 在 1080p 下是"轻微胶片底噪"，再高就是明显噪点 —— 目的是统一纹理，不是做风格。
GRAIN_DEFAULT = 8
#: 固定 seed：同一条链在两次渲染里必须得到同一层颗粒，否则前后对比不可复现。
GRAIN_SEED = 20261024

#: 每段允许的曝光微调上限（EV）。±0.1 EV ≈ ±7.2% 亮度：
#: 够吃掉"这一段比别段亮半档"的编码/生成抖动，又不会变成"每段自己调曝光"。
EXPOSURE_LIMIT_EV = 0.1

#: 抽帧位置（片长比例）：取中段，避开起幅/收尾的淡入淡出与转场。
ANALYZE_AT = 0.5
#: 逐段抽几帧（>1 时在 0.5±0.1 片长内均匀取样再平均，压低单帧噪声）。
ANALYZE_FRAMES = 1

#: drift 判读阈值（工程口径，不是测量结果）：
#:   cast    = 三通道"均值−luma"极差（0..255 级），<3 级肉眼基本不可辨；
#:   warmth  = (r−b) 极差，<6 属同一色温，>14 一眼看出来。
VERDICT_GOOD = {"cast": 3.0, "warmth": 6.0}
VERDICT_VISIBLE = {"cast": 6.0, "warmth": 14.0}


# ═══════════════════════════════════════════════════════════════════════════
# 二、ffmpeg 定位与调用
# ═══════════════════════════════════════════════════════════════════════════

_FFMPEG_CACHE: Optional[str] = None


def _ffmpeg() -> str:
    """本机 ffmpeg 路径。

    优先级：环境变量 `VF_FFMPEG` / `GATE_FFMPEG` → PATH 上的 ffmpeg
    （本机为 **8.1.1-full**，任务卡的验收口径）→ `core.seamfix._ffmpeg()`
    （imageio-ffmpeg 自带的 7.1，作为没有 PATH 时的兜底）。
    另可用 `VF_FFPROBE` 指定 ffprobe；本模块**不强依赖 ffprobe**（用 -i 解析规格即可）。
    """
    global _FFMPEG_CACHE
    if _FFMPEG_CACHE:
        return _FFMPEG_CACHE
    exe = ""
    for key in ("VF_FFMPEG", "GATE_FFMPEG"):
        v = (os.environ.get(key) or "").strip()
        if v:
            exe = v
            break
    if not exe:
        exe = shutil.which("ffmpeg") or ""
    if not exe:
        try:  # 只读复用，不修改 seamfix
            from .seamfix import _ffmpeg as _sf_ffmpeg  # type: ignore

            exe = _sf_ffmpeg()
        except Exception:
            try:
                from core.seamfix import _ffmpeg as _sf_ffmpeg2  # type: ignore

                exe = _sf_ffmpeg2()
            except Exception:
                exe = "ffmpeg"
    _FFMPEG_CACHE = exe or "ffmpeg"
    return _FFMPEG_CACHE


def _ffmpeg_version() -> str:
    rc, txt = _run([_ffmpeg(), "-version"], timeout=30)
    if rc != 0 or not txt:
        return "unknown"
    line = (txt.splitlines() or [""])[0]
    m = re.search(r"ffmpeg version (\S+)", line)
    return m.group(1) if m else line.strip()[:80]


def _run(cmd: Sequence[str], timeout: float = 600) -> Tuple[int, str]:
    """同步跑一个命令，返回 (returncode, stdout+stderr)。"""
    try:
        p = subprocess.run(
            list(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except FileNotFoundError:
        return -2, f"not found: {cmd[0]}"
    out = (p.stdout or b"").decode("utf-8", "replace")
    err = (p.stderr or b"").decode("utf-8", "replace")
    return (p.returncode or 0), out + err


_SIZE_RE = re.compile(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})")
_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):([\d.]+)")
_FPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*fps")


def probe_clip(path: str) -> Dict[str, Any]:
    """读一个片段的规格：宽/高/时长/fps/有无音轨。

    用 `ffmpeg -i` 解析（不依赖 ffprobe，Windows 上更省事；与 seamfix.probe 同口径）。
    """
    rc, txt = _run([_ffmpeg(), "-hide_banner", "-i", path], timeout=90)
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    m = _SIZE_RE.search(txt)
    d = _DUR_RE.search(txt)
    f = _FPS_RE.search(txt)
    dur = 0.0
    if d:
        dur = int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3))
    info = {
        "path": os.path.abspath(path),
        "w": int(m.group(1)) if m else 0,
        "h": int(m.group(2)) if m else 0,
        "duration": round(dur, 3),
        "fps": float(f.group(1)) if f else 0.0,
        "has_audio": "Audio:" in txt,
        "ok": bool(m) and dur > 0,
    }
    if not info["ok"]:
        raise RuntimeError(f"读不到规格（不是视频？）: {path} :: {txt[-200:]}")
    return info


def _clip_list(clips: Any) -> List[str]:
    """把各种入参归一成**有序的片段路径列表**。

    接受：单文件路径 / 通配符 / 目录（取其下 *.mp4）/ 以上任意组合的 list。
    排序保证可复现（目录与通配符都按名字排序）。
    """
    raw: List[str] = []
    items = clips if isinstance(clips, (list, tuple)) else [clips]
    for it in items:
        if it is None:
            continue
        s = str(it)
        if os.path.isdir(s):
            hits = sorted(_glob.glob(os.path.join(s, "*.mp4")) + _glob.glob(os.path.join(s, "*.MP4")))
            raw.extend(hits)
        elif any(ch in s for ch in "*?["):
            raw.extend(sorted(_glob.glob(s)))
        else:
            raw.append(s)
    out: List[str] = []
    for p in raw:
        if os.path.isfile(p):
            q = os.path.abspath(p)
            if q not in out:
                out.append(q)
    if not out:
        raise ValueError(f"没有可用素材: {clips!r}")
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 三、把"一段画面"变成数字：抽帧 + 统计
# ═══════════════════════════════════════════════════════════════════════════


def _grab_rgb(path: str, t: float, chain: Optional[str] = None) -> Any:
    """抽一帧 → (H, W, 3) float32 RGB。`chain` 是可选的前置滤镜串（用于"套完 look 再量"）。

    用**临时文件**而不是管道：Windows 上 ffmpeg 写 pipe 偶发死锁
    （这条经验与 `seamfix._gray_frames` 一致）。
    """
    import numpy as np

    info = probe_clip(path)
    w, h = int(info["w"]), int(info["h"])
    fd, raw = tempfile.mkstemp(suffix=".__gate.raw")
    os.close(fd)
    try:
        vf = "format=rgb24" if not chain else f"{chain},format=rgb24"
        cmd = [
            _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{max(0.0, float(t)):.3f}", "-i", path,
            "-frames:v", "1", "-vf", vf,
            "-f", "rawvideo", "-pix_fmt", "rgb24", raw,
        ]
        rc, txt = _run(cmd, timeout=180)
        if rc != 0 or not os.path.exists(raw):
            raise RuntimeError(f"抽帧失败 {os.path.basename(path)} @{t:.2f}s :: {txt[-300:]}")
        buf = np.fromfile(raw, dtype=np.uint8)
    finally:
        try:
            os.remove(raw)
        except Exception:
            pass
    k = w * h * 3
    if buf.size < k:
        raise RuntimeError(f"抽帧数据不足 {os.path.basename(path)}: {buf.size} < {k}")
    return buf[:k].reshape(h, w, 3).astype(np.float32)


def frame_stats(arr: Any) -> Dict[str, Any]:
    """单帧统计（**纯函数**，可单测）。

    · `mean_rgb`  ：逐通道均值（0..255）；
    · `luma`      ：Rec.709 亮度均值（0.2126R + 0.7152G + 0.0722B）；
    · `saturation`：HSV 口径的平均饱和度（%，0..100）—— 不依赖 numpy 之外的东西；
    · `warmth`    ：`mean_r − mean_b`（正=偏暖，负=偏冷）—— 冷暖差的直接口径；
    · `contrast`  ：亮度标准差（0..~110，越大越硬）；
    · `cast`      ：逐通道 `均值 − luma`（亮度不变量）。这是"色偏"的干净口径：
                    把"这段整体暗"从"这段偏绿/偏蓝"里剥离开。
    """
    import numpy as np

    a = arr
    flat = a.reshape(-1, 3)
    mean = flat.mean(axis=0)
    luma_px = flat[:, 0] * 0.2126 + flat[:, 1] * 0.7152 + flat[:, 2] * 0.0722
    luma = float(luma_px.mean())
    mx = flat.max(axis=1)
    mn = flat.min(axis=1)
    sat = float(((mx - mn) / np.maximum(mx, 1.0)).mean() * 100.0)
    contrast = float(luma_px.std())
    m = [float(x) for x in mean]
    cast = [round(m[i] - luma, 3) for i in range(3)]
    return {
        "mean_rgb": [round(x, 2) for x in m],
        "luma": round(luma, 2),
        "saturation": round(sat, 2),
        "warmth": round(m[0] - m[2], 2),
        "contrast": round(contrast, 2),
        "cast": cast,
    }


def _frame_stats_cached(path: str, t: float, chain: Optional[str]) -> Dict[str, Any]:
    """带缓存的抽帧统计（plan_look 会对同一帧反复量，缓存省掉重复解码）。"""
    key = (os.path.abspath(path), round(float(t), 3), chain or "")
    hit = _STATS_CACHE.get(key)
    if hit is not None:
        return hit
    st = frame_stats(_grab_rgb(path, t, chain))
    _STATS_CACHE[key] = st
    return st


_STATS_CACHE: Dict[Tuple[str, float, str], Dict[str, Any]] = {}


def _spread(vals: Sequence[float]) -> float:
    """极差 max−min（drift 的定义：**段与段之间差多少**，不是平均值）。"""
    if len(vals) < 2:
        return 0.0
    return round(float(max(vals) - min(vals)), 3)


def _drift_of(per: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """把逐段统计聚合成"全片均值 + 各量极差"。"""
    n = len(per)
    if n == 0:
        raise ValueError("没有逐段统计")
    keys = ("luma", "saturation", "warmth", "contrast")
    mean = {}
    for k in keys:
        mean[k] = round(sum(float(p[k]) for p in per) / n, 2)
    mean["mean_rgb"] = [
        round(sum(float(p["mean_rgb"][i]) for p in per) / n, 2) for i in range(3)
    ]
    drift: Dict[str, float] = {k: _spread([float(p[k]) for p in per]) for k in keys}
    for i, ch in enumerate("rgb"):
        drift[f"channel_{ch}"] = _spread([float(p["mean_rgb"][i]) for p in per])
    for i, ch in enumerate("rgb"):
        drift[f"cast_{ch}"] = _spread([float(p["cast"][i]) for p in per])
    cast_drift = max(drift["cast_r"], drift["cast_g"], drift["cast_b"])
    warmth_drift = drift["warmth"]
    luma_drift = drift["luma"]
    #: 综合色偏（0..1）：色偏与冷暖差里**最严重的那一项** ÷ 255。
    #: 故意不把亮度差算进来 —— 一个夜景近景本来就该暗，那是内容不是色偏。
    drift_score = round(max(cast_drift, warmth_drift) / 255.0, 5)
    return {
        "drift": drift,
        "mean": mean,
        "cast_drift": round(cast_drift, 3),
        "warmth_drift": round(warmth_drift, 3),
        "luma_drift": round(luma_drift, 3),
        "sat_drift": round(drift["saturation"], 3),
        "contrast_drift": round(drift["contrast"], 3),
        "drift_score": drift_score,
    }


def _verdict(cast_drift: float, warmth_drift: float) -> str:
    if cast_drift <= VERDICT_GOOD["cast"] and warmth_drift <= VERDICT_GOOD["warmth"]:
        return "色偏已很小（同一色温/同一白平衡，肉眼基本不可辨）"
    if cast_drift <= VERDICT_VISIBLE["cast"] and warmth_drift <= VERDICT_VISIBLE["warmth"]:
        return "轻微色偏（看得出来段与段不同，但不到出戏）"
    return "明显色偏/冷暖跳变（会被观众读成'两台机器拍的'）"


def measure_look(
    clips: Any,
    *,
    at: float = ANALYZE_AT,
    frames: int = ANALYZE_FRAMES,
    chain: Optional[str] = None,
) -> Dict[str, Any]:
    """**逐段抽中间帧 → 量化"片段间色偏/冷暖差"**（本门的第一性测量）。

    参数
    ----
    clips : 单路径 / 通配符 / 目录 / 路径列表
    at    : 抽帧位置（片长比例），默认 0.5（中段，避开起幅淡入与转场）
    frames: 每段抽几帧（>1 时在 `at ± 0.1` 内均匀取样后**按帧平均**，压低单帧噪声）
    chain : 可选前置滤镜串。传进去就是"先套这条链再量" —— 用于量套完 look 的效果。

    返回
    ----
    `{"clips":[...], "mean":{...}, "drift":{...}, "cast_drift", "warmth_drift",
      "luma_drift", "drift_score", "n", "verdict", "notes"}`

    口径说明（很重要，别把两件事混了）：
      · **drift = max−min**：段与段之间差多少。全片统一 look 的目标就是压它；
      · **cast**（通道均值 − luma）把"整体暗"从"偏色"里剥开 —— 否则一个夜景
        近景会把"色偏"虚报成十几个点（本机素材上实测：通道极差 62 级，
        其中亮度贡献 60 级，真正的 cast 只有 5 级）。
    """
    paths = _clip_list(clips)
    frames = max(1, int(frames))
    per: List[Dict[str, Any]] = []
    for p in paths:
        info = probe_clip(p)
        dur = float(info["duration"])
        ts: List[float]
        if frames == 1:
            ts = [dur * float(at)]
        else:
            lo = max(0.0, min(1.0, float(at) - 0.1))
            hi = max(0.0, min(1.0, float(at) + 0.1))
            ts = [dur * (lo + (hi - lo) * (i / (frames - 1))) for i in range(frames)]
        stats = [_frame_stats_cached(p, t, chain) for t in ts]
        if frames > 1:
            avg = {
                "mean_rgb": [round(sum(s["mean_rgb"][i] for s in stats) / len(stats), 2) for i in range(3)],
                "luma": round(sum(s["luma"] for s in stats) / len(stats), 2),
                "saturation": round(sum(s["saturation"] for s in stats) / len(stats), 2),
                "warmth": round(sum(s["warmth"] for s in stats) / len(stats), 2),
                "contrast": round(sum(s["contrast"] for s in stats) / len(stats), 2),
                "cast": [round(sum(s["cast"][i] for s in stats) / len(stats), 3) for i in range(3)],
            }
        else:
            avg = dict(stats[0])
        per.append(
            {
                "path": p,
                "name": os.path.basename(p),
                "index": len(per),
                "t": round(ts[0], 3),
                "size": [info["w"], info["h"]],
                "duration": round(dur, 3),
                **avg,
            }
        )
    agg = _drift_of(per)
    notes = [
        f"抽了 {len(per)} 段 × {frames} 帧（片长 ×{at:.2f} 处）",
        f"片段间极差：亮度 {agg['luma_drift']:.1f} 级、饱和度 {agg['sat_drift']:.2f}、"
        f"对比度 {agg['contrast_drift']:.1f}",
        f"色偏（通道均值−luma 的极差）{agg['cast_drift']:.2f} 级；"
        f"冷暖差 (r−b) 极差 {agg['warmth_drift']:.2f}（0..255 级）",
    ]
    if agg["luma_drift"] > agg["warmth_drift"] * 3:
        notes.append(
            "亮度极差远大于冷暖差：主要是**内容本身的明暗差**（不同场景/近景），"
            "不是白平衡漂移 —— 这类差不该用 LUT 抹平（曝光只给 ±0.1 EV 微调）。"
        )
    return {
        "gate": "cinematic",
        "kind": "measure_look",
        "version": GATE_VERSION,
        "clips": per,
        "n": len(per),
        **agg,
        "verdict": _verdict(agg["cast_drift"], agg["warmth_drift"]),
        "notes": notes,
    }


# ═══════════════════════════════════════════════════════════════════════════
# 四、选 look（类型 → look 名，可被 spec 覆盖）
# ═══════════════════════════════════════════════════════════════════════════

_GENRE_LOOKS = {
    "teal_orange": (
        "drama", "剧情", "都市", "商业", "悬疑", "thriller", "cinematic", "电影",
        "现代", "科技", "办公室", "职场", "雨夜", "犯罪", "noir", "科幻", "sci-fi",
        "产品", "发布会", "广告", "promo", "default", "",
    ),
    "bleach_bypass": (
        "documentary", "纪实", "纪录", "war", "战争", "历史", "压抑", "严肃",
        "desaturated", "粗粝", "废墟", "灾难", "惊悚", "horror", "末日", "post-apoc",
    ),
    "warm_film": (
        "romance", "爱情", "治愈", "家庭", "family", "怀旧", "回忆", "童年",
        "年代", "温暖", "日常", "咖啡", "旅行", "vlog", "治愈系", "温情",
    ),
}


def choose_look(genre: str = "drama", spec: Optional[Dict[str, Any]] = None) -> str:
    """按类型选 **一个** look（三选一）。

    `spec` 可显式指定：`{"look": "bleach_bypass"}`（须在 `LOOKS` 里），
    或 `{"genre": "..."}` 覆盖 `genre` 参数。
    匹配方式是**关键词包含**（中英文都可以），命中优先级：
    spec.look > spec.genre > genre 参数 > 默认 teal_orange。
    """
    spec = spec or {}
    want = spec.get("look")
    if want:
        if want not in LOOKS:
            raise ValueError(f"未知 look: {want!r}（可选: {sorted(LOOKS)}）")
        return str(want)
    g = str(spec.get("genre") or genre or "").strip().lower()
    for name, keys in _GENRE_LOOKS.items():
        for k in keys:
            if k and k in g:
                return name
    return "teal_orange"


# ═══════════════════════════════════════════════════════════════════════════
# 五、滤镜串构造
# ═══════════════════════════════════════════════════════════════════════════


def _curve_expr(points: Sequence[Tuple[float, float]]) -> str:
    """控制点 → ffmpeg curves 的 x/y 串（值为 0..1，ffmpeg 自动按归一化处理）。"""
    pts = [(float(x), float(y)) for x, y in points]
    if len(pts) < 2:
        raise ValueError(f"曲线至少要两个点: {points!r}")
    xs = [p[0] for p in pts]
    if any(b <= a for a, b in zip(xs, xs[1:])):
        raise ValueError(f"曲线控制点 x 必须严格递增: {points!r}")
    if abs(pts[0][0]) > 1e-9 or abs(pts[-1][0] - 1.0) > 1e-9:
        raise ValueError(f"曲线首末点必须是 x=0 与 x=1: {points!r}")
    if any(y < 0.0 or y > 1.0 for _, y in pts):
        raise ValueError(f"曲线 y 必须在 0..1: {points!r}")
    return " ".join(f"{x:.4f}/{y:.4f}" for x, y in pts)


def look_chain(look: str) -> str:
    """一个 look 的**全片共用**滤镜链（不含曝光、不含颗粒/画幅）。"""
    if look not in LOOKS:
        raise ValueError(f"未知 look: {look!r}（可选: {sorted(LOOKS)}）")
    spec = LOOKS[look]
    curves = look_curves(look)
    # ★ 一个滤镜内多选项用 `:` 分隔（`,` 是"滤镜链"的分隔符）。
    #   写错成 `curves=red='…',green='…'` 会被 ffmpeg 读成"新滤镜 green" →
    #   报 `No option name near '0.25/0.2450 …'`（实测踩过）。
    args = ":".join(f"{k}='{_curve_expr(curves[k])}'" for k in _CURVE_KEYS)
    sat = float(spec["saturation"])
    return f"curves={args},eq=saturation={sat:.3f},format=yuv420p"


def _exposure_filter(gain: float) -> str:
    """曝光微调：三通道同增益（**同一色温**，只动亮度）。

    `gain = 2**EV`。用 `lutrgb` + `clip` 与 `seamfix.lut_filter` 同一写法
    （本机 lutrgb 不支持 `t`/`n` 常量，但 `val`/`clip` 可以）。
    """
    g = float(gain)
    if abs(g - 1.0) < 1e-6:
        return ""
    ch = ":".join(f"{n}='clip(val*{g:.5f},0,255)'" for n in ("r", "g", "b"))
    return f"lutrgb={ch}"


def build_look_filter(
    plan: Dict[str, Any],
    clip_index: Optional[int] = None,
) -> str:
    """把计划变成**可直接串进 `filter_complex`** 的 video 滤镜串（用 `,` 连接）。

    · `clip_index=None` → 返回**全片共用**的那条 look 链（不含逐段曝光）；
    · `clip_index=i`    → 返回第 i 段的完整链（look + 该段 ±0.1 EV 曝光）。

    返回值末尾带 `format=yuv420p`：本门所有链都按 8-bit 4:2:0 交付
    （`lutrgb`/`curves`/`noise` 在 4:2:0 上都实测可用）。
    """
    if not isinstance(plan, dict):
        return look_chain(str(plan))
    if clip_index is None:
        return str(plan.get("look_chain") or look_chain(str(plan["look"])))
    filters = plan.get("filters") or []
    try:
        return str(filters[int(clip_index)])
    except Exception:
        raise IndexError(f"plan 里没有第 {clip_index} 段的 filter（共 {len(filters)} 段）")


def build_grain_filter(strength: int = GRAIN_DEFAULT, *, seed: int = GRAIN_SEED) -> str:
    """统一轻颗粒：`noise` 滤镜，强度默认 8（与 `seamfix.TEXTURE_GRAIN` 对齐）、固定 seed。

    · 强度 0..100（ffmpeg noise 尺度）。0 返回 `null`（等于不加）；
    · `allf=t+u`：temporal + uniform —— 逐帧变化的均匀噪点，最接近胶片底噪；
      注意**不要**用 `p`（pattern），那是固定网格，看起来像屏纹；
    · `all_seed` 固定：同一段素材两次渲染得到同一层颗粒，前后对比才可复现。
    · 位置：**在画幅处理之前**加（这样黑边是干净的纯黑，颗粒只落在画面里）。
    """
    s = int(round(float(strength)))
    s = max(0, min(100, s))
    if s == 0:
        return "null"
    return f"noise=alls={s}:allf=t+u:all_seed={int(seed)}"


def _ratio(target: Any) -> float:
    """`"2.39:1"` / `"2.39"` / `2.39` → float。"""
    if isinstance(target, (int, float)):
        r = float(target)
        if r <= 0:
            raise ValueError(f"画幅必须为正: {target!r}")
        return r
    s = str(target).strip().replace("：", ":").replace("x", ":").replace("X", ":").replace("×", ":")
    if ":" in s:
        a, b = s.split(":", 1)
        try:
            r = float(a) / float(b)
        except Exception:
            raise ValueError(f"看不懂的画幅: {target!r}")
    else:
        try:
            r = float(s)
        except Exception:
            raise ValueError(f"看不懂的画幅: {target!r}")
    if r <= 0:
        raise ValueError(f"画幅必须为正: {target!r}")
    return r


def _even(n: float) -> int:
    """最接近 `n` 的**偶数**（yuv420p 的宽高必须是偶数）。

    ★ 不是"四舍五入后减一"：1920/2.39 = 803.35，四舍五入得 803（奇数），
      减一得 802，但**804 才是更近的偶数**（误差 0.084% vs 0.17%）。
      精确形式与表达式形式（`round(x/2)*2`）必须给出同一个数，否则两边算出来的
      画幅对不上 —— selftest 里两种写法都要过。
    """
    x = float(n)
    k = int(math.floor(x + 0.5))
    if k % 2 == 0:
        return k
    return (k - 1) if (x - (k - 1)) <= ((k + 1) - x) else (k + 1)


def _ratio_err(ratio: float, target: float) -> float:
    return round(abs(ratio - target) / target * 100.0, 3)


def aspect_geometry(
    target: Any = "2.39:1",
    source_size: Optional[Sequence[int]] = None,
    *,
    mode: str = "letterbox",
) -> Dict[str, Any]:
    """算 2.39:1（或任意目标画幅）的**几何**：输出尺寸、黑边、字幕安全区。

    · `mode="crop"`      ：裁画面（不缩放）。保留宽度、裁高度；
                           素材比目标更宽时改成保留高度、裁宽度。
    · `mode="letterbox"` ：不裁画面、不缩放，只补黑边（`pad`）。
                           素材比目标窄 → 画布加宽（**左右黑边**）；
                           素材比目标宽 → 画布加高（**上下黑边**）。

    返回 `{"out":[w,h], "bars":{...}, "ratio", "ratio_err_pct", "picture":[w,h],
    "subtitle":{...}, "note":...}`。`source_size=None` 时返回 `out=None`，
    只给比值层面的结论与"用表达式写的通用滤镜"。
    """
    T = _ratio(target)
    mode = str(mode or "letterbox").lower()
    if mode not in ("letterbox", "crop"):
        raise ValueError(f"mode 只能是 letterbox / crop: {mode!r}")
    geo: Dict[str, Any] = {"target": str(target), "target_ratio": round(T, 4), "mode": mode}
    if not source_size:
        geo.update({"out": None, "picture": None, "bars": None, "ratio": None,
                    "ratio_err_pct": None, "source": None})
        geo["note"] = ("未给 source_size：滤镜用表达式写成与分辨率无关的形式；"
                       "具体像素数请传 source_size 再看 subtitle_safe_area()。")
        return geo
    w, h = int(source_size[0]), int(source_size[1])
    if w <= 0 or h <= 0:
        raise ValueError(f"source_size 非法: {source_size!r}")
    S = w / h
    bars = {"top": 0, "bottom": 0, "left": 0, "right": 0}
    if mode == "letterbox":
        if S >= T:      # 素材更宽 → 画布加高（上下黑边）
            ow, oh = w, max(h, _even(w / T))
        else:           # 素材更窄 → 画布加宽（左右黑边）
            ow, oh = max(w, _even(h * T)), h
        bars["top"] = bars["bottom"] = (oh - h) // 2
        bars["left"] = bars["right"] = (ow - w) // 2
        picture = [w, h]
    else:
        if S >= T:      # 素材更宽 → 裁宽度
            ow, oh = min(w, _even(h * T)), h
        else:           # 素材更窄 → 裁高度
            ow, oh = w, min(h, _even(w / T))
        picture = [ow, oh]
    out_ratio = ow / oh
    pic_h = picture[1]
    # 字幕：落在画面区内，距画面底 ≥ 12% 画面高；letterbox 时还要加上黑边高度
    safe_from_pic_bottom = int(math.ceil(SUBTITLE_SAFE_RATIO * pic_h))
    geo.update(
        {
            "source": [w, h],
            "source_ratio": round(S, 4),
            "out": [ow, oh],
            "picture": picture,
            "bars": bars,
            "ratio": round(out_ratio, 4),
            "ratio_err_pct": _ratio_err(out_ratio, T),
            "subtitle": {
                "safe_ratio": SUBTITLE_SAFE_RATIO,
                "safe_from_picture_bottom_px": safe_from_pic_bottom,
                "safe_from_frame_bottom_px": bars["bottom"] + safe_from_pic_bottom,
                "picture_area_only": True,
                "in_bars": False,
                "note": ("字幕只放在画面区（黑边里不放：播放器/平台会裁边或加模糊）；"
                         f"距画面底 ≥{int(SUBTITLE_SAFE_RATIO*100)}% 画面高 = {safe_from_pic_bottom}px"),
            },
        }
    )
    if mode == "letterbox" and bars["left"]:
        geo["note"] = (f"素材 {w}x{h}（{S:.3f}:1）比目标 {T:g}:1 窄，"
                       f"letterbox 只能加宽画布 → {ow}x{oh}，**左右**各 {bars['left']}px 黑边；"
                       "想要上下的电影黑边请用 mode='crop'（会裁掉画面）。")
    elif mode == "letterbox" and bars["top"]:
        geo["note"] = (f"素材比目标宽，letterbox → {ow}x{oh}，上下各 {bars['top']}px 黑边。")
    else:
        geo["note"] = (f"crop → {ow}x{oh}（{out_ratio:.3f}:1，误差 {geo['ratio_err_pct']:.2f}%），"
                       f"裁掉画面高的 {100.0*(h-oh)/h:.1f}%；无黑边。")
    return geo


def aspect_filter(
    target: Any = "2.39:1",
    *,
    mode: str = "letterbox",
    source_size: Optional[Sequence[int]] = None,
) -> str:
    """2.39:1 画幅滤镜（letterbox 补黑边 / crop 裁画面），可直接串进 filter_complex。

    两种模式（几何见 `aspect_geometry` 与本模块 docstring 的"画幅几何"一节）：

    · `mode="letterbox"`（默认）：**不裁画面、不缩放**，用 `pad` 把画布补到目标画幅。
      素材比目标窄时黑边在**左右**；比目标宽时黑边在**上下**。
    · `mode="crop"`：**裁画面到目标画幅**（保留最大面积），无黑边。
      对 1.74:1 素材套 2.39:1 会裁掉约 27% 的画面高。

    `source_size`：给了就输出**精确整数**的 `pad=`/`crop=`；不给就用 ffmpeg 的
    表达式（`iw`/`ih`/`ow`/`oh`，本机 8.1.1 与 7.1 都实测可用），与分辨率无关。

    字幕安全区：见 `subtitle_safe_area()`。**黑边里不放字幕**，字幕距画面底
    ≥ `SUBTITLE_SAFE_RATIO`(12%) 画面高。
    """
    T = _ratio(target)
    mode = str(mode or "letterbox").lower()
    if mode not in ("letterbox", "crop"):
        raise ValueError(f"mode 只能是 letterbox / crop: {mode!r}")
    geo = aspect_geometry(target, source_size, mode=mode)
    if source_size:
        ow, oh = geo["out"]  # type: ignore[misc]
        w, h = int(source_size[0]), int(source_size[1])
        if (ow, oh) == (w, h):
            return "setsar=1"
        if mode == "letterbox":
            # x/y 用 -1 = 居中（pad 的 -1 语义），避免 (ow-w)//2 的取整偏一手感
            return f"pad={ow}:{oh}:-1:-1:color=black,setsar=1"
        return f"crop={ow}:{oh}:(iw-ow)/2:(ih-oh)/2,setsar=1"
    # ── 无 source_size：表达式形式，与输入分辨率无关 ─────────────────────
    # ★ 用 `round(x/2)*2` 而不是 `trunc(x/2)*2`：前者与 `_even()` 的"取最近偶数"一致，
    #   两种写法必须给出同一个尺寸（1920x1080 crop → 两边都是 1920x804）。
    if mode == "letterbox":
        return (
            "pad="
            f"w='if(gte(iw/ih,{T}),iw,round(ih*{T}/2)*2)':"
            f"h='if(gte(iw/ih,{T}),round(iw/{T}/2)*2,ih)':"
            "x=-1:y=-1:color=black,setsar=1"
        )
    return (
        "crop="
        f"w='if(gte(iw/ih,{T}),round(ih*{T}/2)*2,iw)':"
        f"h='if(gte(iw/ih,{T}),ih,round(iw/{T}/2)*2)':"
        "x='(iw-ow)/2':y='(ih-oh)/2',setsar=1"
    )


def subtitle_safe_area(
    target: Any = "2.39:1",
    source_size: Optional[Sequence[int]] = None,
    *,
    mode: str = "letterbox",
) -> Dict[str, Any]:
    """字幕安全区（给成片/字幕团队的硬约束）。

    规则（两条都是**硬**的）：
      1. **黑边里不放字幕**：平台会把黑边裁掉/虚化/贴别的东西，字幕放进去必丢；
      2. 字幕基线距**画面底** ≥ 12% 画面高（`SUBTITLE_SAFE_RATIO`），
         再往下会被进度条、"相关推荐"压住。
    """
    geo = aspect_geometry(target, source_size, mode=mode)
    out: Dict[str, Any] = {
        "target": geo.get("target"),
        "mode": geo.get("mode"),
        "out": geo.get("out"),
        "picture": geo.get("picture"),
        "bars": geo.get("bars"),
        "safe_ratio": SUBTITLE_SAFE_RATIO,
        "rules": [
            "黑边内不放字幕（平台/播放器会裁边、加模糊或贴推荐条）",
            f"字幕基线距画面底 ≥ {int(SUBTITLE_SAFE_RATIO * 100)}% 画面高",
        ],
    }
    if geo.get("out") and geo.get("bars") is not None:
        out.update(geo["subtitle"])
    else:
        out["note"] = "没给 source_size：只给比例规则；要像素数请传 source_size。"
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 六、计划：全片一套 look + 每段 ±0.1 EV
# ═══════════════════════════════════════════════════════════════════════════


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


def _plan_candidate(
    paths: Sequence[str],
    times: Sequence[float],
    name: str,
) -> Dict[str, Any]:
    """试一个 look：抽帧套链 → 算每段曝光 → 再套一次 → 量 drift（**真跑 ffmpeg**）。"""
    chain = look_chain(name)
    after_look = [_frame_stats_cached(p, t, chain) for p, t in zip(paths, times)]
    lumas = [float(s["luma"]) for s in after_look]
    target = sorted(lumas)[len(lumas) // 2] if lumas else 0.0
    evs: List[float] = []
    gains: List[float] = []
    filters: List[str] = []
    for s in after_look:
        cur = float(s["luma"])
        ev = 0.0
        if cur > 1.0 and target > 0.0:
            ev = _clamp(math.log2(target / cur), -EXPOSURE_LIMIT_EV, EXPOSURE_LIMIT_EV)
            if abs(ev) < 0.005:          # 0.005 EV ≈ 0.35%，远低于肉眼阈，别为它多编码一次
                ev = 0.0
        g = 2.0 ** ev
        evs.append(round(ev, 4))
        gains.append(round(g, 5))
        exp = _exposure_filter(g)
        filters.append(chain if not exp else f"{chain},{exp}")
    after = [_frame_stats_cached(p, t, f) for p, t, f in zip(paths, times, filters)]
    agg = _drift_of(after)
    return {
        "look": name,
        "zh": LOOKS[name]["zh"],
        "saturation": float(LOOKS[name]["saturation"]),
        "look_chain": chain,
        "target_luma": round(float(target), 2),
        "per_clip_exposure": evs,
        "per_clip_gain": gains,
        "filters": filters,
        "luma_after": [round(float(s["luma"]), 2) for s in after],
        **agg,
    }


def plan_look(
    clips: Any,
    look: Optional[str] = None,
    *,
    genre: str = "drama",
    spec: Optional[Dict[str, Any]] = None,
    verify: bool = True,
    at: float = ANALYZE_AT,
    frames: int = ANALYZE_FRAMES,
) -> Dict[str, Any]:
    """**全片锁一套 look**，每段只允许 ±0.1 EV 曝光微调；并在本机真跑一遍量效果。

    参数
    ----
    clips  : 素材（路径/通配符/目录/列表）
    look   : 指定 look 名（None = 按 `genre`/`spec` 用 `choose_look()` 选）
    verify : True（默认）时把另外两个 look 也试一遍，给出 alternatives 对照表；
             全部用真实 ffmpeg 抽帧套滤镜再量，不是估算。
    at/frames : 同 `measure_look`

    返回（关键字段）
    ----
    `look` / `look_chain` / `filter`（全片共用链）/ `filters`（逐段完整链）/
    `per_clip_exposure`（EV，±0.1）/ `expected_drift_after`（0..1 综合色偏）/
    `before` / `after_preview` / `alternatives` / `notes`

    `expected_drift_after` 的定义：套完 look+曝光后，**用真帧量出来的**
    `max(cast 极差, 冷暖极差)/255`。注意它是"预览值"——整段编码后再量会有
    极小的编码差异（yuv420p 舍入），所以真实交付后应再用 `measure_look()` 复核。
    """
    paths = _clip_list(clips)
    before = measure_look(paths, at=at, frames=frames)
    times = [float(c["t"]) for c in before["clips"]]
    look_name = look or choose_look(genre, spec)
    if look_name not in LOOKS:
        raise ValueError(f"未知 look: {look_name!r}（可选: {sorted(LOOKS)}）")
    chosen = _plan_candidate(paths, times, look_name)
    alternatives: List[Dict[str, Any]] = []
    if verify:
        for name in LOOKS:
            if name == look_name:
                continue
            try:
                alternatives.append(_plan_candidate(paths, times, name))
            except Exception as exc:  # 一个候选试失败不该毁掉整个计划
                alternatives.append({"look": name, "error": f"{type(exc).__name__}: {exc}"})
    after = {
        "drift": chosen["drift"],
        "mean": chosen["mean"],
        "cast_drift": chosen["cast_drift"],
        "warmth_drift": chosen["warmth_drift"],
        "luma_drift": chosen["luma_drift"],
        "sat_drift": chosen["sat_drift"],
        "contrast_drift": chosen["contrast_drift"],
        "drift_score": chosen["drift_score"],
    }
    b, a = before, after

    def _chg(x: float, y: float) -> str:
        """相对变化（带符号，负数=下降）。"""
        if not x:
            return "—"
        return f"{100.0 * (y - x) / x:+.1f}%"

    notes = [
        f"look={look_name}（{LOOKS[look_name]['zh']}）：{LOOKS[look_name]['why']}",
        f"全片共用一条链，只有曝光按段微调（±{EXPOSURE_LIMIT_EV} EV）→ "
        f"曝光 {chosen['per_clip_exposure']}",
        f"色偏 cast 极差 {b['cast_drift']:.2f} → {a['cast_drift']:.2f} 级"
        f"（{_chg(b['cast_drift'], a['cast_drift'])}）",
        f"冷暖 (r−b) 极差 {b['warmth_drift']:.2f} → {a['warmth_drift']:.2f}"
        f"（{_chg(b['warmth_drift'], a['warmth_drift'])}）",
        f"亮度极差 {b['luma_drift']:.1f} → {a['luma_drift']:.1f}"
        f"（{_chg(b['luma_drift'], a['luma_drift'])}；曝光微调上限只有 ±0.1 EV，"
        "主要是内容明暗差，别指望它归零）",
        f"综合色偏 drift_score {b['drift_score']:.4f} → {a['drift_score']:.4f}"
        f"（{_chg(b['drift_score'], a['drift_score'])}）",
        "单条 LUT 压色偏的上限≈它的色度增益（saturation）；要归零只能逐段白平衡，"
        "那正是「每一帧都在做选择」，与本门契约冲突。",
    ]
    if any(abs(float(e)) >= EXPOSURE_LIMIT_EV - 1e-6 for e in chosen["per_clip_exposure"]):
        notes.append(
            f"有段位的曝光微调撞到了 ±{EXPOSURE_LIMIT_EV} EV 上限 —— "
            "说明段间亮度差大于本门允许修的幅度（属于内容/场景差，不是本门该修的）。"
        )
    return {
        "gate": "cinematic",
        "kind": "plan_look",
        "version": GATE_VERSION,
        "look": look_name,
        "genre": genre,
        "look_chain": chosen["look_chain"],
        "filter": chosen["look_chain"],
        "filters": chosen["filters"],
        "per_clip_exposure": chosen["per_clip_exposure"],
        "per_clip_gain": chosen["per_clip_gain"],
        "exposure_limit_ev": EXPOSURE_LIMIT_EV,
        "target_luma": chosen["target_luma"],
        "before": {
            "drift": b["drift"], "mean": b["mean"],
            "cast_drift": b["cast_drift"], "warmth_drift": b["warmth_drift"],
            "luma_drift": b["luma_drift"], "sat_drift": b["sat_drift"],
            "contrast_drift": b["contrast_drift"], "drift_score": b["drift_score"],
        },
        "after_preview": {
            "drift": a["drift"], "mean": a["mean"],
            "cast_drift": a["cast_drift"], "warmth_drift": a["warmth_drift"],
            "luma_drift": a["luma_drift"], "sat_drift": a["sat_drift"],
            "contrast_drift": a["contrast_drift"], "drift_score": a["drift_score"],
            "luma_after": chosen["luma_after"],
            "verdict": _verdict(a["cast_drift"], a["warmth_drift"]),
        },
        "expected_drift_after": float(a["drift_score"]),
        "expected_cast_drift_after": float(a["cast_drift"]),
        "expected_warmth_drift_after": float(a["warmth_drift"]),
        "alternatives": [
            {
                k: alt.get(k) for k in (
                    "look", "zh", "saturation", "cast_drift", "warmth_drift",
                    "luma_drift", "drift_score", "per_clip_exposure", "error",
                ) if k in alt
            }
            for alt in alternatives
        ],
        "clips": [c["path"] for c in before["clips"]],
        "measured_at": times,
        "notes": notes,
    }


# ═══════════════════════════════════════════════════════════════════════════
# 七、真渲染（给"套一遍后重新 measure"用）
# ═══════════════════════════════════════════════════════════════════════════


def apply_filter_to_clip(
    src: str,
    dst: str,
    chain: str,
    *,
    crf: int = 18,
    preset: str = "veryfast",
    audio: str = "copy",
) -> Dict[str, Any]:
    """把一条 video 滤镜链**真烧**进一个片段（视频重编码、音轨默认 copy）。

    返回 `{"ok","dst","bytes","chain","cmd","log"}`；失败不抛异常（返回 ok=False，
    便于门禁汇总），调用方自己决定是否 raise。
    """
    os.makedirs(os.path.dirname(os.path.abspath(dst)) or ".", exist_ok=True)
    info = probe_clip(src)
    cmd = [
        _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
        "-i", src, "-vf", chain,
        "-c:v", "libx264", "-preset", preset, "-crf", str(int(crf)),
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
    ]
    if audio == "copy" and info.get("has_audio"):
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-an"]
    cmd += [dst]
    rc, txt = _run(cmd, timeout=1800)
    ok = rc == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0
    return {
        "ok": ok,
        "src": src,
        "dst": dst,
        "bytes": os.path.getsize(dst) if os.path.exists(dst) else 0,
        "chain": chain,
        "cmd": " ".join(cmd),
        "log": ("" if ok else (txt or "")[-600:]),
    }


def render_plan(
    plan: Dict[str, Any],
    outdir: str,
    *,
    aspect: Optional[str] = None,
    aspect_mode: str = "letterbox",
    source_size: Optional[Sequence[int]] = None,
    grain: Optional[int] = GRAIN_DEFAULT,
    crf: int = 18,
    preset: str = "veryfast",
    suffix: str = "_look",
) -> Dict[str, Any]:
    """按计划把每段素材真渲一遍：`look + 曝光 → 颗粒 → 画幅`，产物落 `outdir`。

    顺序是刻意的：
      1. look/曝光先做（在原始分辨率、未加黑边时调色，颜色决策与画幅无关）；
      2. 颗粒第二（**在画幅之前**：黑边保持纯黑，颗粒只落在画面里）；
      3. 画幅最后（pad/crop 不改颜色，只改画布）。

    返回 `{"outdir","outputs":[...],"filters":[...]}`；`drift_after` 不在这里算 ——
    要"套完重新 measure"请把 `outputs` 的 dst 丢回 `measure_look()`（这才是真数据）。
    """
    paths = _clip_list(plan.get("clips") or [])
    if len(paths) != len(plan.get("filters") or []):
        raise ValueError("plan 与素材数量不一致（plan 是不是给别的素材生成的？）")
    os.makedirs(outdir, exist_ok=True)
    grain_chain = build_grain_filter(int(grain)) if grain is not None else ""
    aspect_chain = ""
    if aspect:
        aspect_chain = aspect_filter(aspect, mode=aspect_mode, source_size=source_size)
    outputs = []
    for i, (src, base) in enumerate(zip(paths, plan["filters"])):
        chain = str(base)
        if grain_chain and grain_chain != "null":
            chain += f",{grain_chain}"
        if aspect_chain and aspect_chain != "null":
            chain += f",{aspect_chain}"
        stem = os.path.splitext(os.path.basename(src))[0]
        dst = os.path.join(outdir, f"{i + 1:02d}_{stem}{suffix}.mp4")
        outputs.append(apply_filter_to_clip(src, dst, chain, crf=crf, preset=preset))
    return {
        "gate": "cinematic",
        "kind": "render_plan",
        "version": GATE_VERSION,
        "look": plan.get("look"),
        "outdir": os.path.abspath(outdir),
        "grain": grain,
        "aspect": aspect,
        "aspect_mode": aspect_mode if aspect else None,
        "outputs": outputs,
        "ok": all(o.get("ok") for o in outputs),
    }


# ═══════════════════════════════════════════════════════════════════════════
# 八、自测：每条链都在本机真跑一遍
# ═══════════════════════════════════════════════════════════════════════════


def _ff_check(name: str, chain: str, *, size: Optional[str] = "864x496",
              expect_size: Optional[Sequence[int]] = None,
              extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """用 lavfi 合成源真跑一条链；`expect_size` 给了就顺便校验输出尺寸。"""
    src = f"testsrc2=s={size}:d=0.2" if size else "testsrc2=s=864x496:d=0.2"
    cmd = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", src, "-vf", chain, "-frames:v", "2", "-f", "null", "-"]
    rc, txt = _run(cmd, timeout=180)
    out = {"name": name, "ok": rc == 0, "chain": chain, "detail": ""}
    if rc != 0:
        out["detail"] = (txt or "").strip().splitlines()[-1][:400] if txt else "ffmpeg failed"
        return out
    if expect_size:
        fd, png = tempfile.mkstemp(suffix=".__gate.png")
        os.close(fd)
        try:
            cmd2 = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", src, "-vf", chain, "-frames:v", "1", png]
            rc2, t2 = _run(cmd2, timeout=180)
            if rc2 != 0:
                out.update(ok=False, detail=f"出图失败: {(t2 or '')[-200:]}")
                return out
            got = _png_size(png)
            out["size"] = got
            if got and (got[0] != int(expect_size[0]) or got[1] != int(expect_size[1])):
                out.update(ok=False, detail=f"尺寸不符: {got} != {list(expect_size)}")
        finally:
            try:
                os.remove(png)
            except Exception:
                pass
    if extra:
        out.update(extra)
    return out


def _png_size(path: str) -> Optional[Tuple[int, int]]:
    """读 PNG 头里的宽高（不依赖 PIL，也不整张解码）。"""
    try:
        with open(path, "rb") as f:
            head = f.read(33)
        if head[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        w = int.from_bytes(head[16:20], "big")
        h = int.from_bytes(head[20:24], "big")
        return (w, h) if w > 0 and h > 0 else None
    except Exception:
        return None


def selftest() -> Dict[str, Any]:
    """自测：**不写业务文件**（只写系统临时文件并清理），全部真跑 ffmpeg。

    覆盖：三个 look 的链、逐段曝光链、颗粒、letterbox/crop 两种画幅（给定尺寸与
    表达式两种写法）、完整交付链（look+grain+aspect）、抽帧统计路径、纯函数几何。
    返回 `{"ok","passed","failed","checks":[...],"ffmpeg":...}`。
    """
    checks: List[Dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "", **kw: Any) -> None:
        c = {"name": name, "ok": bool(ok), "detail": detail}
        c.update(kw)
        checks.append(c)

    # ── 1. 纯函数层 ─────────────────────────────────────────────────────
    add("LOOKS 三选一", sorted(LOOKS) == ["bleach_bypass", "teal_orange", "warm_film"],
        f"keys={sorted(LOOKS)}")
    try:
        add("choose_look(drama)", choose_look("drama") == "teal_orange", choose_look("drama"))
        add("choose_look(纪实)", choose_look("纪实") == "bleach_bypass", choose_look("纪实"))
        add("choose_look(治愈)", choose_look("治愈") == "warm_film", choose_look("治愈"))
        add("choose_look(spec.look)", choose_look("drama", {"look": "warm_film"}) == "warm_film",
            "spec 覆盖优先")
        bad_ok = False
        try:
            choose_look("drama", {"look": "nope"})
        except ValueError:
            bad_ok = True
        add("choose_look 拒绝未知 look", bad_ok, "未知 look 应抛 ValueError")
    except Exception as exc:
        add("choose_look", False, f"{type(exc).__name__}: {exc}")

    # curve 表达式的合法性（纯函数）+ "三通道工作区间斜率相等"这条不变式
    for name in LOOKS:
        try:
            chain = look_chain(name)
            ok = chain.startswith("curves=") and "eq=saturation=" in chain and chain.endswith("format=yuv420p")
            add(f"look_chain({name}) 形态", ok, chain[:90] + "…")
        except Exception as exc:
            add(f"look_chain({name})", False, f"{type(exc).__name__}: {exc}")
        try:
            cs = look_curves(name)
            slopes = {}
            for ch in _CURVE_KEYS:
                pts = cs[ch]
                # 工作区间 [0.15, 0.85] 的两段斜率（0.15→0.5 与 0.5→0.85）
                s1 = (pts[2][1] - pts[1][1]) / (pts[2][0] - pts[1][0])
                s2 = (pts[3][1] - pts[2][1]) / (pts[3][0] - pts[2][0])
                slopes[ch] = (round(s1, 6), round(s2, 6))
            # 比较的是"同一段内不同通道"，不是"同一通道的不同段"
            same = all(
                max(slopes[ch][i] for ch in _CURVE_KEYS) - min(slopes[ch][i] for ch in _CURVE_KEYS) < 1e-6
                for i in (0, 1)
            )
            add(f"{name} 三通道工作区间斜率相等（色偏只会被压缩）", same, json.dumps(slopes))
        except Exception as exc:
            add(f"{name} 斜率不变式", False, f"{type(exc).__name__}: {exc}")

    # grain
    g8 = build_grain_filter(8)
    add("build_grain_filter(8) 固定 seed",
        g8 == "noise=alls=8:allf=t+u:all_seed=20261024", g8)
    add("build_grain_filter(0) → null", build_grain_filter(0) == "null", build_grain_filter(0))
    add("build_grain_filter(200) 夹到 100", "alls=100" in build_grain_filter(200), build_grain_filter(200))
    add("build_grain_filter 默认=8", "alls=8" in build_grain_filter(), build_grain_filter())
    try:
        from .seamfix import TEXTURE_GRAIN as _TG  # type: ignore
    except Exception:
        try:
            from core.seamfix import TEXTURE_GRAIN as _TG  # type: ignore
        except Exception:
            _TG = GRAIN_DEFAULT
    add("颗粒强度与 seamfix.TEXTURE_GRAIN 对齐", int(_TG) == int(GRAIN_DEFAULT),
        f"seamfix={_TG} gate={GRAIN_DEFAULT}")

    # ── 2. 几何纯函数 ───────────────────────────────────────────────────
    try:
        gl = aspect_geometry("2.39:1", (864, 496), mode="letterbox")
        gc = aspect_geometry("2.39:1", (864, 496), mode="crop")
        add("letterbox(864x496) 几何",
            gl["out"] == [1186, 496] and gl["bars"] == {"top": 0, "bottom": 0, "left": 161, "right": 161}
            and gl["ratio_err_pct"] < 0.2, json.dumps(gl["out"]) + json.dumps(gl["bars"]))
        add("crop(864x496) 几何",
            gc["out"] == [864, 362] and gc["ratio_err_pct"] < 0.2,
            f"{gc['out']} ratio={gc['ratio']} err={gc['ratio_err_pct']}%")
        g16 = aspect_geometry("2.39:1", (1920, 1080), mode="crop")
        add("crop(1920x1080) → 1920x804", g16["out"] == [1920, 804], str(g16["out"]))
        add("letterbox 字幕安全区数字",
            gl["subtitle"]["safe_from_picture_bottom_px"] == 60
            and gl["subtitle"]["picture_area_only"] is True,
            json.dumps(gl["subtitle"], ensure_ascii=False)[:200])
        add("crop 字幕安全区 ≥12% 画面高",
            gc["subtitle"]["safe_from_picture_bottom_px"] == math.ceil(0.12 * 362),
            str(gc["subtitle"]["safe_from_picture_bottom_px"]))
        add("_ratio 解析", abs(_ratio("2.39:1") - 2.39) < 1e-9 and abs(_ratio(1.85) - 1.85) < 1e-9,
            "2.39:1 / 1.85")
        ss = subtitle_safe_area("2.39:1", (864, 496), mode="crop")
        add("subtitle_safe_area 规则齐", len(ss["rules"]) == 2 and ss["safe_ratio"] == 0.12,
            json.dumps(ss["rules"], ensure_ascii=False))
    except Exception as exc:
        add("几何纯函数", False, f"{type(exc).__name__}: {exc}")

    # ── 3. 真跑 ffmpeg ──────────────────────────────────────────────────
    try:
        add("ffmpeg 可用", _run([_ffmpeg(), "-version"], timeout=30)[0] == 0, _ffmpeg())
    except Exception as exc:
        add("ffmpeg 可用", False, f"{type(exc).__name__}: {exc}")

    for name in LOOKS:
        try:
            checks.append(_ff_check(f"真跑 look_chain({name})", look_chain(name)))
        except Exception as exc:
            add(f"真跑 look_chain({name})", False, f"{type(exc).__name__}: {exc}")

    # 曝光微调（±0.1 EV 的两端）
    for ev in (-EXPOSURE_LIMIT_EV, EXPOSURE_LIMIT_EV):
        g = 2.0 ** ev
        exp = _exposure_filter(g)
        if exp:
            checks.append(_ff_check(f"真跑曝光 {ev:+.2f}EV (gain={g:.4f})", exp))
    add("曝光 0EV 不出滤镜", _exposure_filter(1.0) == "", "_exposure_filter(1.0) == ''")

    checks.append(_ff_check("真跑颗粒(8)", build_grain_filter(8)))
    checks.append(_ff_check("真跑 letterbox(864x496)", aspect_filter("2.39:1", source_size=(864, 496)),
                            expect_size=(1186, 496)))
    checks.append(_ff_check("真跑 crop(864x496)", aspect_filter("2.39:1", mode="crop", source_size=(864, 496)),
                            expect_size=(864, 362)))
    checks.append(_ff_check("真跑 letterbox(表达式/1080p)",
                            aspect_filter("2.39:1"), size="1920x1080", expect_size=(2582, 1080)))
    checks.append(_ff_check("真跑 crop(表达式/1080p)",
                            aspect_filter("2.39:1", mode="crop"), size="1920x1080",
                            expect_size=(1920, 804)))

    # 完整交付链：look + 颗粒 + 画幅（这才是成片里真正会串的串法）
    for mode in ("letterbox", "crop"):
        for name in LOOKS:
            chain = (f"{look_chain(name)},{build_grain_filter(8)},"
                     f"{aspect_filter('2.39:1', mode=mode, source_size=(864, 496))}")
            exp = (1186, 496) if mode == "letterbox" else (864, 362)
            checks.append(_ff_check(f"真跑完整链 {name}+grain+{mode}", chain, expect_size=exp))

    # ── 4. 抽帧统计路径（造假素材 → 抽帧 → 量）──────────────────────────
    fd, tmpmp4 = tempfile.mkstemp(suffix=".__gate.mp4")
    os.close(fd)
    fd, tmpcolor = tempfile.mkstemp(suffix=".__gate2.mp4")
    os.close(fd)
    try:
        rc, txt = _run([
            _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=0x808080:s=320x180:d=0.6",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", tmpmp4,
        ], timeout=120)
        if rc != 0:
            add("抽帧统计路径", False, f"造样本失败: {(txt or '')[-200:]}")
        else:
            st = measure_look(tmpmp4)
            clip = st["clips"][0]
            add("measure_look 能抽帧并出数字",
                abs(clip["mean_rgb"][0] - 128) < 2 and abs(clip["luma"] - 128) < 2
                and clip["warmth"] == 0.0,
                f"mean_rgb={clip['mean_rgb']} luma={clip['luma']} warmth={clip['warmth']}")
            add("单段 drift = 0（没法算片间差）", st["drift_score"] == 0.0 and st["n"] == 1,
                f"n={st['n']} drift_score={st['drift_score']}")
        # 彩色样本：验证 `measure_look(chain=)` 真的把滤镜套上了（不是空跑）
        rc2, txt2 = _run([
            _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=s=320x180:d=0.6",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", tmpcolor,
        ], timeout=120)
        if rc2 != 0:
            add("measure_look(chain=) 套链后颜色变化", False, f"造样本失败: {(txt2 or '')[-200:]}")
        else:
            c0 = measure_look(tmpcolor)["clips"][0]
            c1 = measure_look(tmpcolor, chain=look_chain("teal_orange"))["clips"][0]
            add("measure_look(chain=) 套链后颜色变化",
                c1["warmth"] > c0["warmth"] + 3 and c1["saturation"] < c0["saturation"],
                f"warmth {c0['warmth']}→{c1['warmth']} / sat {c0['saturation']}→{c1['saturation']}")
            c2 = measure_look(tmpcolor, chain=look_chain("bleach_bypass"))["clips"][0]
            add("bleach_bypass 真的去饱和",
                c2["saturation"] < c0["saturation"] * 0.75,
                f"sat {c0['saturation']} → {c2['saturation']}")
    except Exception as exc:
        add("抽帧统计路径", False, f"{type(exc).__name__}: {exc}")
    finally:
        for p in (tmpmp4, tmpcolor):
            try:
                os.remove(p)
            except Exception:
                pass

    passed = sum(1 for c in checks if c.get("ok"))
    failed = len(checks) - passed
    return {
        "gate": "cinematic",
        "kind": "selftest",
        "version": GATE_VERSION,
        "ffmpeg": _ffmpeg(),
        "ffmpeg_version": _ffmpeg_version(),
        "ok": failed == 0,
        "passed": passed,
        "failed": failed,
        "checks": checks,
    }


# ═══════════════════════════════════════════════════════════════════════════
# 九、命令行入口（给人工复核/门禁用）
# ═══════════════════════════════════════════════════════════════════════════


def _main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m core.gate_cinematic",
        description="电影感门：measure / plan / render / selftest（本机 ffmpeg 真跑）",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("selftest", help="自测（真跑每条滤镜链）")

    p = sub.add_parser("measure", help="逐段抽中间帧量色偏/冷暖差")
    p.add_argument("clips", nargs="+")
    p.add_argument("--at", type=float, default=ANALYZE_AT)
    p.add_argument("--frames", type=int, default=ANALYZE_FRAMES)

    p = sub.add_parser("plan", help="选一个 look + 每段 ±0.1EV，并量套后 drift")
    p.add_argument("clips", nargs="+")
    p.add_argument("--look", default=None, choices=sorted(LOOKS))
    p.add_argument("--genre", default="drama")
    p.add_argument("--no-verify", action="store_true", help="不试其它 look（更快）")

    p = sub.add_parser("render", help="按 plan 真渲（look+颗粒+可选画幅）")
    p.add_argument("clips", nargs="+")
    p.add_argument("--outdir", required=True)
    p.add_argument("--look", default=None, choices=sorted(LOOKS))
    p.add_argument("--genre", default="drama")
    p.add_argument("--aspect", default=None, help='如 "2.39:1"；不给就只调色')
    p.add_argument("--aspect-mode", default="letterbox", choices=["letterbox", "crop"])
    p.add_argument("--grain", type=int, default=GRAIN_DEFAULT)
    p.add_argument("--crf", type=int, default=18)
    p.add_argument("--preset", default="veryfast")

    args = ap.parse_args(list(argv) if argv is not None else None)

    if args.cmd == "selftest":
        res = selftest()
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["ok"] else 1

    if args.cmd == "measure":
        res = measure_look(args.clips, at=args.at, frames=args.frames)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "plan":
        res = plan_look(args.clips, look=args.look, genre=args.genre, verify=not args.no_verify)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "render":
        plan = plan_look(args.clips, look=args.look, genre=args.genre)
        first = probe_clip(plan["clips"][0])
        src_size = (first["w"], first["h"])
        res = render_plan(plan, args.outdir, aspect=args.aspect,
                          aspect_mode=args.aspect_mode, source_size=src_size,
                          grain=args.grain, crf=args.crf, preset=args.preset)
        aft = None
        if res["ok"]:
            aft = measure_look([o["dst"] for o in res["outputs"]])
        print(json.dumps({
            "plan": {k: plan[k] for k in (
                "look", "per_clip_exposure", "expected_drift_after",
                "expected_cast_drift_after", "expected_warmth_drift_after")},
            "before": plan["before"], "render": res,
            "after_measured": aft,
        }, ensure_ascii=False, indent=2))
        return 0 if res["ok"] else 1
    return 2


if __name__ == "__main__":
    sys.exit(_main())
