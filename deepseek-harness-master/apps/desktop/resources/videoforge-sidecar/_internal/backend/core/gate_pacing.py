# -*- coding: utf-8 -*-
"""VideoForge · 节奏门（Gate: Pacing）—— "两段 15 秒衔接差" 的判定与补救

═══════════════════════════════════════════════════════════════════
要解决的问题
═══════════════════════════════════════════════════════════════════
用户原话（2026-10-24）：「节奏慢」。
现场证据：项目 `7b6ee913`（产品宣传片 30 秒）只有 **2 个分镜 × 15 秒**，
16:02 / 16:05 两条素材各 15.104s —— 正片里 30 秒只有 **1 个切点**（平均 15 秒一切）。

这里要区分两个**不同**的标准，混为一谈会得出错误结论（本模块两个都量）：

  ① **「任意 3 秒窗口里要有东西变化」**（下限，`audit_film`）
     实测：静止画面的 3 秒首尾差 ≈ **0.000034**（编码噪声底），
     真实运动镜头的 3 秒首尾差 ≥ **0.025**，两者差 3 个数量级 → 阈值好定。
     这一条**慢推镜的长镜头也能过**（画面在动），所以它不是节奏标准，只是"没死"的下限。

  ② **切点密度 / 景别节奏**（真正的节奏标准，`audit_shot_plan`）
     30 秒只有 1 个切点 = 平均 15 秒一切，而 fast_drama 档要求 2.5 秒级；
     两个分镜的景别都是 medium_close_up（同一机位同一景别用到尾）。
     这一条才会判出「节奏塌陷」并给出切点方案。

  ③ **同一素材切出来的段必须像不同机位**（`build_cut_plan` / `apply_cut_plan`）
     光切不换构图 = 同一画面重播，观众仍然觉得"没剪"。
     所以每段带 `zoom`（1.00 → 1.12 渐进）并**交替推/拉**，用 zoompan 真渲染，
     保持原分辨率 / 帧率 / 音轨，再用 ffprobe 逐段校验时长误差 < 0.1s。

═══════════════════════════════════════════════════════════════════
契约（Lead 按这个接线）
═══════════════════════════════════════════════════════════════════
    audit_shot_plan(shots, *, pacing="fast_drama", window=3.0) -> dict
    plan_cut_points(clip_duration, timeline=None, *, pacing="promo",
                    cut_seconds=None, max_cuts_per_clip=3,
                    min_segment=1.2, max_segment=6.0) -> dict
    build_cut_plan(clips, *, pacing="fast_drama", cut_seconds=None,
                   max_cuts_per_clip=3, timelines=None) -> dict
    apply_cut_plan(clips, plan, out_dir) -> list
    audit_film(video_path) -> dict
    selftest() -> dict

★ 与任务卡的一处**事实校正**（照实写，不装作一致）：
  任务卡说档位来自 `core/scriptplan.py`。写这段代码时该文件里只有
  `MIN_SCENE_SECONDS=3` / `PREFERRED_SCENE_SECONDS=12`（**场**级，不是**切点**级），
  没有 fast_drama / promo / talking / ambient 这套档位，也没有 `_ffprobe()`。
  本模块因此做了**软引用**：`_resolve_tiers()` 先只读试读
  `core.scriptplan.PACING`（并行 teammate 随后确实在该文件里加了 `{"cut": 2.5 …}`），
  读得到就用它并把来源写进 `legend["pacing_source"]`；读不到 / 该文件正在改动报错，
  就用本模块内置的同名同值表（2.5 / 3.5 / 5.0 / 8.0）。两条路都不改现有文件。

★ 另一处**契约张力**（默认值取舍，照实写）：
  `max_cuts_per_clip=3` 的默认值对 15 秒素材意味着最多 4 段（每段 3.776 秒），
  达不到 fast_drama 的 2.5 秒档。想落 2.5 秒档必须把上限提到 ≥5
  （15.104 / 2.5 = 6 段 → 5 个切点）。`build_cut_plan` 会在 `note` 里**明说被上限截断**，
  不静默把 2.5 秒说成 3.776 秒。`selftest()` 两种都会跑。

★ 2026-10-24 追加：**非等分拆点**（任务卡 2026-10-24-021）
  均分的问题不是"切得不够多"，而是"切出来都一样"——30 秒 8 段全是 3.776 秒，
  同段同值 100%，被 C13/T3 判缺陷。而每镜的 `layer2_timeline` 本来就是内容驱动的
  2~5 秒时间片（3.5/4/4/3.5、5/5/5…），**它就是内容给出的天然切点**。
  所以 `plan_cut_points` 拿片长当权重、按比例映射到**实际** clip_duration 拆点：
  非等分、段数上限 `max_cuts_per_clip+1`、每段夹在 `[min_segment, max_segment]`、
  总长精确等于素材时长；`build_cut_plan(timelines=...)` 逐条素材喂时间片，
  `timelines=None` 时**一个字节的旧行为都不改**（回归：15.104s ×2 → 8 段 / 3.776s）。

纯离线、零付费、零网络；只用 numpy + ffmpeg。对项目内其它模块**全部只读**：
`core.seamfix._ffmpeg()` 借它的 ffmpeg 发现逻辑、`core.scriptplan.PACING` 借档位表、
`core.db` 只在导出的 shots.json 缺失时兜底查询 —— 一个字节都不改它们。
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger("videoforge.gate_pacing")

__all__ = [
    "PACING_TIERS",
    "audit_shot_plan",
    "plan_cut_points",
    "build_cut_plan",
    "apply_cut_plan",
    "audit_film",
    "selftest",
    "probe_media",
    "infer_shot_size",
]


# ═══════════════════════════════════════════════════════════════════
# 一、常量：全部写死，并注明依据（改之前先看依据还成不成立）
# ═══════════════════════════════════════════════════════════════════

# 切点档位（秒/切）。语义 = "这个类型多久该有一个切点"。
#   fast_drama 2.5  竖屏快节奏剧情/宣传：观众 3 秒内没看到变化就划走
#   promo      3.5  产品宣传：允许一个镜头里讲一句完整的话
#   talking    5.0  口播：一句话的时长，切太碎会抢戏
#   ambient    8.0  氛围/空镜：本来就该慢慢看
#
# ★ 下面这张表是**兜底**。写这段代码时 `core/scriptplan.py` 里还没有切点档位
#   （只有场级的 MIN_SCENE_SECONDS / PREFERRED_SCENE_SECONDS），随后并行 teammate
#   在该文件里新增了 `PACING = {"fast_drama": {"cut": 2.5, ...}, ...}`。
#   所以本模块对它是**软引用**：`_resolve_tiers()` 先试着读 scriptplan 的 PACING，
#   读得到就用（并在 legend 里标明来源），读不到/报错就用下面这张同名同值的表。
#   这样既"参考现有档位"、又不因为别的文件正在改动而让本模块挂掉。
PACING_TIERS: Dict[str, float] = {
    "fast_drama": 2.5,
    "promo": 3.5,
    "talking": 5.0,
    "ambient": 8.0,
}
DEFAULT_PACING = "fast_drama"

# 判定窗口（秒）。任务卡硬要求 3 秒：小于 3 秒观众来不及感知一次变化，
# 大于 3 秒则短片（12~30s）会被切成 2、3 个窗口，统计失去意义。
DEFAULT_WINDOW = 3.0

# 长镜头：超过档位多少倍就该拆。1.5 倍 ≈ 3.75 秒（fast_drama 档），
# 正好是一句短台词的长度；再长就是"一个镜头讲完一段话"。
LONG_TAKE_RATIO = 1.5

# 单段下限（秒）。1.2 秒 ≈ 29 帧 @24fps，短于这个连一次动作起势都放不完，
# 切出来像闪帧。所以均分时如果尾段塌到 1.2 秒以下，就少切一刀、把时间摊平。
MIN_SEGMENT_SECONDS = 1.2

# 单段上限（秒）。6.0 秒 = 一次「内容片段」的自然上限：观众对同一构图的耐心约 5~7 秒，
# 比 promo 档 3.5s 松、比 ambient 档 8.0s 紧。非等分拆点时，超过 6 秒的段会被
# 「均分最大片」继续拆（直到 max_cuts_per_clip 上限拦住，note 里如实写被截断）。
MAX_SEGMENT_SECONDS = 6.0

# ── 画面变化阈值 ──────────────────────────────────────────────────
# 依据（本机实测，2026-10-24，素材 = 项目 7b6ee913 的两条 15.104s 生成片）：
#   · 静止画面（单帧循环 3 秒，h264 crf18 编码）逐帧差 = 0.000003，3 秒首尾差 = 0.000034
#   · 真实运动镜头 3 秒首尾差 = 0.025 ~ 0.232（5 个窗口实测）
#   · 两者差 3 个数量级 → 0.030 取在"编码噪声之上、最弱真实运动之下"
#     （0.030 ≈ 7.6 个灰阶，人眼在 160x90 缩略图上刚好能看出块面变化）
FRAME_DELTA_CHANGED = 0.030
# 32 桶灰度直方图的 L1 距离（归一化后 0~2）。静止 ≈ 0；
# 实测真实变化窗口 0.09~0.62；0.15 用来抓"整体亮度/色调变了但均值抵消"的情况
# （例：镜头摇到屏幕上，平均亮度差不多，分布全变了）。
HIST_DELTA_CHANGED = 0.15

# ── 声音变化阈值 ─────────────────────────────────────────────────
# 相邻 3 秒窗口 RMS 落差（dB）。依据：节目素材响度的可辨差异约 1~3 dB，
# 6 dB ≈ 音量翻倍/减半，是"一定听得出"的量级；
# 实测同一条素材：台词落地 -0.02 / -1.36 dB（同一句里），
# 台词收住 -24.9 dB，环境声回来了 +2.7 dB → 6 dB 能把"台词/音效落地"和"没变"分开。
AUDIO_DELTA_CHANGED_DB = 6.0

# ── 硬切检测 ─────────────────────────────────────────────────────
# 判据 = 逐帧差既是**邻域内局部极大**，又同时满足：
#   · 绝对值 > 0.060（远高于编码噪声 0.000003，也高于慢推镜的逐帧差 0.02~0.04）
#   · 大于邻域中位数的 2.5 倍（相对判据，避免把快动作误判成切点）
# 实测校验：把 7b6ee913 两条 15s 素材硬接成 30 秒后，
# 在 15.17s 检出唯一一个切点（跳变 0.2734，局部中位 0.0113，**24 倍**）；
# 两条连续的 12 秒成片（无硬切、只有连续运动）检出 **0** 个切点 → 无误报。
CUT_JUMP_MIN = 0.060
CUT_JUMP_RATIO = 2.5
CUT_NEIGHBOR_FRAMES = 9  # 邻域半径（6fps → ±1.5 秒）

# ── 二次构图（渐进推拉） ──────────────────────────────────────────
# 1.00 = 原画幅；1.12 = 放大 12%。为什么要 12%：4K→HD 常用 1.1~1.2 的"数字变焦"区间，
# 12% 的构图差异人眼能看出是"换了个焦段"，同时又不到"裁掉了内容"的程度
# （864x496 裁 12% 后有效像素仍有 ~772x443，再缩放回 864x496 不糊）。
ZOOM_MIN = 1.00
ZOOM_MAX = 1.12
# 段内推/拉的幅度：2% ≈ 50 帧 @24fps 里慢慢变一点，观众感到"镜头在动"而不觉得"在缩放"
ZOOM_INTRASEG_STEP = 0.02
# 构图锚点横向偏移（相对缩放后画幅）。交替左右，让相邻两段连门框/桌面都不在同一条线上，
# 视觉上不会被认成"同一画面又播了一遍"。
ANCHOR_X = 0.040
# 纵向锚点偏移：交替上下（±2% 画幅高 ≈ 10px @496），模拟"机位高度不同"，
# 比只改缩放更像换了机位。2% 足够被看出来，又不会切掉人物头顶。
ANCHOR_Y = 0.020

# 切片规格校验容差（秒）。任务卡要求 < 0.1s；这里按帧留一点余量：
# 1 帧 @24fps = 0.0417s，容差 0.1s ≈ 2.4 帧。
DURATION_TOLERANCE = 0.10

# 分析用降采样（与 core/seamfix.py 的 ANALYZE_W/ANALYZE_H = 160x90 一致）。
# fps 取 6：3 秒窗口 = 18 帧，够算首尾差；比 seamfix 的 12fps 再省一半解码/内存。
ANALYZE_W, ANALYZE_H = 160, 90
ANALYZE_FPS = 6.0
# 窗口首/尾取多少帧做平均。0.5 秒 @6fps = 3 帧，抹掉单帧噪点又不至于把运动抹平。
HEAD_TAIL_FRAMES = 3
AUDIO_SR = 16000

# 判定阈值
COLLAPSE_RATIO_BAD = 0.50      # ≥ 一半窗口无变化 → 节奏塌陷
COLLAPSE_RATIO_SLOW = 0.30     # ≥ 三成 → 偏慢
CUT_DENSITY_TOLERANCE = 0.60   # 实际切点 ≥ 目标的 60% 才算"密度达标"（允许档位放宽一档）

# 景别阶梯（从紧到松）。用于"景别多样性"与"冲突前中近景→爆发特写→释放远景"的判断。
SIZE_LADDER = [
    "extreme_close_up",
    "close_up",
    "medium_close_up",
    "medium",
    "medium_wide",
    "wide",
    "extreme_wide",
]
SIZE_ZH = {
    "extreme_close_up": "大特写",
    "close_up": "特写",
    "medium_close_up": "中近景",
    "medium": "中景",
    "medium_wide": "中全景",
    "wide": "全景",
    "extreme_wide": "远景",
    "unknown": "未知",
}

# 中文/英文景别关键词 → 阶梯。**顺序即优先级**（"中近景"必须排在"近景"前，
# 否则 "中近景侧45度" 会被误判成特写级的近景）。
_SIZE_PATTERNS: List[Tuple[str, str]] = [
    (r"大特写|极特写|extreme\s*close", "extreme_close_up"),
    (r"特写|close[\-\s]?up|closeup", "close_up"),
    (r"大远景|extreme\s*wide", "extreme_wide"),
    (r"中远景", "medium_wide"),
    (r"远景|long\s*shot", "extreme_wide"),
    (r"中近景", "medium_close_up"),
    (r"近景|close\s*shot", "close_up"),
    (r"中全景", "medium_wide"),
    (r"全景|full\s*shot|wide\s*shot|establishing", "wide"),
    (r"中景|medium\s*shot", "medium"),
    (r"半身|上半身", "medium_close_up"),
    (r"全身|full\s*body", "wide"),
]


# ═══════════════════════════════════════════════════════════════════
# 二、ffmpeg / ffprobe 发现 + 进程调用
# ═══════════════════════════════════════════════════════════════════

def _ffmpeg() -> str:
    """复用 `core.seamfix._ffmpeg()` 的发现逻辑（imageio_ffmpeg → PATH）。"""
    try:
        from core.seamfix import _ffmpeg as _sf_ffmpeg  # 只读引用，不改它

        exe = _sf_ffmpeg()
        if exe and (os.path.sep in exe or shutil.which(exe)):
            return exe
    except Exception:  # pragma: no cover - 兜底路径
        pass
    return shutil.which("ffmpeg") or "ffmpeg"


def _ffprobe() -> str:
    """`core.seamfix` **没有** `_ffprobe()`（任务卡记错了），这里自己发现：
    先看同目录有没有跟 ffmpeg 一起装的 ffprobe，再退回 PATH。找不到就用
    ffmpeg -i 的 stderr 解析兜底（见 `probe_media`）。"""
    exe = _ffmpeg()
    try:
        cand = os.path.join(os.path.dirname(exe), "ffprobe.exe" if os.name == "nt" else "ffprobe")
        if exe and os.path.isfile(cand):
            return cand
    except Exception:
        pass
    return shutil.which("ffprobe") or "ffprobe"


def _run(cmd: Sequence[str], timeout: int = 600) -> Tuple[int, str]:
    """同步跑一个外部命令，返回 (rc, stdout+stderr)。Windows 下不弹窗。"""
    kwargs: Dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "stdin": subprocess.DEVNULL,
        "timeout": timeout,
    }
    if os.name == "nt":
        try:
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        except Exception:
            pass
    try:
        proc = subprocess.run([str(c) for c in cmd], **kwargs)
        out = (proc.stdout or b"") + (proc.stderr or b"")
        return int(proc.returncode or 0), out.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return -1, f"timeout after {timeout}s: {' '.join(str(c) for c in cmd[:3])}"
    except FileNotFoundError as exc:
        return -2, f"executable not found: {exc}"
    except Exception as exc:  # pragma: no cover
        return -3, f"{type(exc).__name__}: {exc}"


def _fps_of(rate: Optional[str]) -> float:
    """把 "24/1" 解析成 24.0；解析不了返回 0.0。"""
    if not rate:
        return 0.0
    try:
        if "/" in str(rate):
            num, den = str(rate).split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f else 0.0
        return float(rate)
    except Exception:
        return 0.0


def probe_media(path: str) -> Dict[str, Any]:
    """读规格：时长 / 宽高 / 帧率 / 有没有音轨 / 音轨采样率声道。

    优先 ffprobe（JSON，可靠）；ffprobe 不可用时退回 `ffmpeg -i` 的 stderr 正则
    （和 seamfix.probe 同思路）。两条路都失败时 duration=0.0，调用方自己判。
    """
    info: Dict[str, Any] = {
        "path": path, "duration": 0.0, "width": 0, "height": 0, "fps": 0.0,
        "has_audio": False, "audio_rate": 0, "audio_channels": 0,
        "nb_frames": 0, "video_codec": "", "audio_codec": "", "r_frame_rate": "",
        "probe": "none", "error": "",
    }
    if not path or not os.path.exists(path):
        info["error"] = f"文件不存在: {path}"
        return info

    rc, out = _run([_ffprobe(), "-v", "error", "-show_entries",
                    "format=duration:stream=codec_type,codec_name,width,height,"
                    "r_frame_rate,sample_rate,channels,nb_frames",
                    "-of", "json", path], timeout=120)
    if rc == 0 and out.strip().startswith("{"):
        try:
            data = json.loads(out)
            streams = data.get("streams") or []
            fmt = data.get("format") or {}
            try:
                info["duration"] = round(float(fmt.get("duration") or 0.0), 3)
            except Exception:
                info["duration"] = 0.0
            for st in streams:
                kind = st.get("codec_type")
                if kind == "video" and not info["width"]:
                    info["width"] = int(st.get("width") or 0)
                    info["height"] = int(st.get("height") or 0)
                    info["r_frame_rate"] = str(st.get("r_frame_rate") or "")
                    info["fps"] = round(_fps_of(info["r_frame_rate"]), 6)
                    info["video_codec"] = str(st.get("codec_name") or "")
                    try:
                        info["nb_frames"] = int(st.get("nb_frames") or 0)
                    except Exception:
                        info["nb_frames"] = 0
                elif kind == "audio" and not info["has_audio"]:
                    info["has_audio"] = True
                    info["audio_codec"] = str(st.get("codec_name") or "")
                    try:
                        info["audio_rate"] = int(st.get("sample_rate") or 0)
                    except Exception:
                        info["audio_rate"] = 0
                    try:
                        info["audio_channels"] = int(st.get("channels") or 0)
                    except Exception:
                        info["audio_channels"] = 0
            info["probe"] = "ffprobe"
            if info["duration"] > 0 and info["width"] > 0:
                return info
        except Exception as exc:
            info["error"] = f"ffprobe JSON 解析失败: {exc}"

    # 兜底：ffmpeg -i 的 stderr
    rc2, txt = _run([_ffmpeg(), "-hide_banner", "-i", path], timeout=120)
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", txt)
    if m:
        info["duration"] = round(int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)), 3)
    v = re.search(r"Video:.*?,\s*(\d+)x(\d+)", txt)
    if v:
        info["width"], info["height"] = int(v.group(1)), int(v.group(2))
    f = re.search(r"(\d+(?:\.\d+)?)\s*fps", txt)
    if f:
        info["fps"] = round(float(f.group(1)), 6)
    info["has_audio"] = "Audio:" in txt
    if info["has_audio"]:
        a = re.search(r"Audio:\s*[\w\-]+.*?(\d+)\s*Hz", txt)
        if a:
            info["audio_rate"] = int(a.group(1))
        ch = re.search(r"(\d+)\s*channels", txt) or re.search(r"stereo", txt)
        if ch:
            info["audio_channels"] = 2 if "stereo" in txt else int(ch.group(1))
    info["probe"] = "ffmpeg-stderr" if info["duration"] > 0 else "failed"
    if info["duration"] <= 0 and not info["error"]:
        info["error"] = (txt or "")[-300:]
    return info


def _temp_path(suffix: str) -> str:
    """唯一的临时文件名。

    ★ 用临时文件而不是管道：Windows 上 ffmpeg 写 pipe 偶发死锁（seamfix 也有这条注释），
      且并发分析两段素材时同名文件会互相踩。
    """
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        os.remove(path)  # ffmpeg 会自己建；这里只要名字唯一
    except OSError:
        pass
    return path


def _gray_seq(path: str, *, w: int = ANALYZE_W, h: int = ANALYZE_H,
              fps: float = ANALYZE_FPS) -> Any:
    """抽成 (N,H,W) float32 灰度 0..1。失败返回空数组（调用方判 len==0）。"""
    try:
        import numpy as np
    except Exception:  # pragma: no cover
        return []
    raw = _temp_path(".__gp_gray.raw")
    try:
        rc, txt = _run([_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", path,
                        "-vf", f"fps={fps},scale={w}:{h},format=gray",
                        "-an", "-f", "rawvideo", "-pix_fmt", "gray", raw], timeout=600)
        if rc != 0 or not os.path.exists(raw):
            logger.warning("抽帧失败 %s: %s", os.path.basename(path), (txt or "")[-200:])
            return []
        buf = np.fromfile(raw, dtype=np.uint8)
    except Exception as exc:
        logger.warning("抽帧异常 %s: %s", path, exc)
        return []
    finally:
        try:
            os.remove(raw)
        except OSError:
            pass
    n = len(buf) // (w * h)
    if n <= 0:
        return []
    return (buf[: n * w * h].reshape(n, h, w).astype(np.float32) / 255.0)


def _pcm_seq(path: str, *, sr: int = AUDIO_SR) -> Any:
    """抽成 1-D float32 PCM（-1..1）。无音轨返回空数组。"""
    try:
        import numpy as np
    except Exception:  # pragma: no cover
        return []
    raw = _temp_path(".__gp_pcm.raw")
    try:
        rc, _txt = _run([_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", path,
                         "-vn", "-ac", "1", "-ar", str(sr),
                         "-f", "s16le", "-acodec", "pcm_s16le", raw], timeout=600)
        if rc != 0 or not os.path.exists(raw):
            return []
        buf = np.fromfile(raw, dtype=np.int16)
    except Exception:
        return []
    finally:
        try:
            os.remove(raw)
        except OSError:
            pass
    if buf.size <= 0:
        return []
    return buf.astype(np.float32) / 32768.0


def _db(rms: float) -> float:
    """RMS → dBFS（数字静音给 -120，避免 log(0)）。"""
    return round(20.0 * math.log10(max(float(rms), 1e-6)), 2)


# ═══════════════════════════════════════════════════════════════════
# 三、景别推断（shot_size 现场就是 None，只能从文字推；推不出标 unknown）
# ═══════════════════════════════════════════════════════════════════

def infer_shot_size(shot: Dict[str, Any]) -> Tuple[str, str]:
    """返回 (shot_size, basis)。

    basis 说明这个判断**从哪来的**，方便人复核：
      "字段 shot_size" / "文本「camera 中近景侧45度…」" / "unknown（没有任何景别线索）"
    """
    if not isinstance(shot, dict):
        return "unknown", "unknown（不是 dict）"

    # 1) 字段直接给了（现场实测 shot_size 是 None）
    for key in ("shot_size", "shot_size_zh", "size"):
        val = shot.get(key)
        if isinstance(val, str) and val.strip():
            raw = val.strip()
            if raw in SIZE_ZH:
                return raw, f"字段 {key}"
            size, _ = _match_size(raw)
            if size != "unknown":
                return size, f"字段 {key}「{raw}」"

    # 2) 从文本推：camera 优先，其次 overview，再其次 timeline 里的 camera/action
    texts: List[Tuple[str, str]] = []
    for key in ("camera", "camera_text", "layer1_overview", "overview", "description", "prompt"):
        val = shot.get(key)
        if isinstance(val, str) and val.strip():
            texts.append((key, val))

    blocks = _parse_blocks(shot.get("layer2_timeline") or shot.get("timeline"))
    for i, blk in enumerate(blocks):
        for key in ("camera", "action"):
            val = blk.get(key)
            if isinstance(val, str) and val.strip():
                texts.append((f"timeline[{i}].{key}", val))

    for src, text in texts:
        size, hit = _match_size(text)
        if size != "unknown":
            snippet = hit.strip()
            if len(snippet) > 24:
                snippet = snippet[:24] + "…"
            return size, f"文本「{src}: {snippet}」（{SIZE_ZH.get(size, size)}）"

    # 3) 推不出就如实说 unknown，不瞎猜
    return "unknown", "unknown（没有任何景别线索）"


def _match_size(text: str) -> Tuple[str, str]:
    """在一段文字里按优先级找景别关键词，返回 (size, 命中的原句片段)。"""
    if not text:
        return "unknown", ""
    for pattern, size in _SIZE_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            # 取命中处前后一小段，方便人看依据
            start = max(0, m.start() - 8)
            return size, text[start: m.end() + 8]
    return "unknown", ""


def _parse_blocks(value: Any) -> List[Dict[str, Any]]:
    """把 layer2_timeline（可能是 JSON 字符串 / list / None）解析成 list[dict]。"""
    if not value:
        return []
    if isinstance(value, str):
        try:
            data = json.loads(value)
        except Exception:
            return []
    else:
        data = value
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    return [b for b in data if isinstance(b, dict)]


# ═══════════════════════════════════════════════════════════════════
# 四、镜头表归一化
# ═══════════════════════════════════════════════════════════════════

def _as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        f = float(value)
    except Exception:
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return f


def _normalize_shots(shots: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把各种来源的镜头行（DB 行 / 接口 dict / 精简 dict）归一成内部结构。

    内部结构：`{"shot_id","order_index","duration","start","end","shot_size",
               "size_basis","camera","dialogue","beats":[...]}`
    `beats` = 这条镜头内部的变化点（台词落地 / 机位或景别变化 / 动作变化），
    它们来自 layer2_timeline 的分块；没有 layer2_timeline 就只有镜头边界。
    """
    rows: List[Dict[str, Any]] = []
    for idx, raw in enumerate(shots or []):
        if not isinstance(raw, dict):
            continue
        order = int(_as_float(raw.get("order_index"), float(idx)) or idx)
        dur = _as_float(raw.get("duration_seconds"))
        if dur is None:
            dur = _as_float(raw.get("duration"))
        dur = max(0.0, float(dur or 0.0))
        sid = str(raw.get("shot_id") or raw.get("id") or f"shot_{order}")
        size, basis = infer_shot_size(raw)
        blocks = _parse_blocks(raw.get("layer2_timeline") or raw.get("timeline"))

        camera = str(raw.get("camera") or raw.get("camera_text") or "")
        dialogue = raw.get("dialogue")
        beats: List[Dict[str, Any]] = []
        prev_size: Optional[str] = None
        for i, blk in enumerate(blocks):
            bstart = max(0.0, float(_as_float(blk.get("start"), 0.0) or 0.0))
            bcam = str(blk.get("camera") or "")
            baction = str(blk.get("action") or "")
            bdlg = blk.get("dialogue")
            bsize, _ = _match_size(bcam) if bcam else ("unknown", "")
            # 台词**永远**登记，包括第一个分块（start=0）：台词经常正好落在切点上，
            # 它属于**新**窗口的内容；不登记会让新窗口被误判成"没变化"。
            if isinstance(bdlg, dict) and str(bdlg.get("text") or "").strip():
                beats.append({"t": round(bstart, 3), "kind": "dialogue",
                              "text": str(bdlg.get("text")).strip()})
            elif i > 0 and bsize != "unknown" and prev_size is not None and bsize != prev_size:
                beats.append({"t": round(bstart, 3), "kind": "size_change",
                              "text": f"景别 {SIZE_ZH.get(prev_size, prev_size)} → {SIZE_ZH.get(bsize, bsize)}"})
            elif i > 0 and (bcam or baction):
                snippet = (bcam or baction).strip()
                beats.append({"t": round(bstart, 3), "kind": "action",
                              "text": snippet[:28] + ("…" if len(snippet) > 28 else "")})
            if bsize != "unknown":
                prev_size = bsize

        # 镜头级台词（有的工程把台词挂在镜头而不是时间轴上）
        dialogues: List[str] = []
        if isinstance(dialogue, dict) and str(dialogue.get("text") or "").strip():
            dialogues.append(str(dialogue.get("text")).strip())
        elif isinstance(dialogue, str) and dialogue.strip():
            dialogues.append(dialogue.strip())
        for b in beats:
            if b["kind"] == "dialogue":
                dialogues.append(b["text"])

        rows.append({
            "shot_id": sid,
            "order_index": order,
            "duration": round(dur, 3),
            "start": 0.0,
            "end": 0.0,
            "shot_size": size,
            "size_basis": basis,
            "camera": camera,
            "dialogue": dialogues,
            "beats": sorted(beats, key=lambda b: b["t"]),
        })

    rows.sort(key=lambda r: (r["order_index"], r["shot_id"]))
    cursor = 0.0
    for row in rows:
        row["start"] = round(cursor, 3)
        cursor += row["duration"]
        row["end"] = round(cursor, 3)
    return rows


def _resolve_tiers() -> Tuple[Dict[str, float], str]:
    """切点档位表 + 来源说明。

    优先读 `core.scriptplan.PACING`（现有档位的唯一权威源，只读），
    读不到就用本模块内置的 `PACING_TIERS`（同名同值）。
    返回 `(tiers, source)`，source 会写进 `legend["pacing_source"]`，方便人复核。
    """
    merged = dict(PACING_TIERS)
    try:
        from core.scriptplan import PACING as _SP_PACING  # 只读引用，不改它

        if isinstance(_SP_PACING, dict):
            used = 0
            for name, spec in _SP_PACING.items():
                if isinstance(spec, dict):
                    val = _as_float(spec.get("cut"))
                else:
                    val = _as_float(spec)
                if val and val > 0:
                    merged[str(name)] = float(val)
                    used += 1
            if used:
                return merged, "core/scriptplan.py 的 PACING（只读引用）"
    except Exception as exc:  # scriptplan 可能在并行改动/尚未就绪
        logger.debug("读 core.scriptplan.PACING 失败，用内置档位: %s", exc)
    return merged, "gate_pacing 内置 PACING_TIERS（scriptplan 里没读到）"


def _tier_seconds(pacing: str, cut_seconds: Optional[float] = None,
                  *, tiers: Optional[Dict[str, float]] = None) -> float:
    if cut_seconds is not None:
        val = _as_float(cut_seconds)
        if val and val > 0:
            return float(val)
        raise ValueError(f"cut_seconds 必须是正数，收到 {cut_seconds!r}")
    table = tiers or _resolve_tiers()[0]
    key = str(pacing or DEFAULT_PACING)
    if key in table:
        return float(table[key])
    val = _as_float(key)
    if val and val > 0:
        return float(val)
    raise ValueError(f"未知 pacing 档位 {pacing!r}，可选 {sorted(table)} 或直接给秒数")


def _split_offsets(duration: float, target: float) -> List[float]:
    """一条 duration 秒的镜头按 target 秒均分时，切点相对镜头起点的偏移（不含 0）。

    取整段数而不是硬切 target 的整数倍，避免最后剩个 0.4 秒的碎段：
      · 15.104s / 2.5 → 6 段 × 2.517s，切点 [2.517, 5.034, 7.551, 10.068, 12.585]
      · 11s     / 2.5 → 4 段 × 2.75s， 切点 [2.75, 5.5, 8.25]
    段数按 round 但保证每段 ≥ MIN_SEGMENT_SECONDS。
    """
    if duration <= 0 or target <= 0:
        return []
    if duration <= target * 1.2:
        return []  # 本来就够短，不需要拆
    n = max(1, int(round(duration / target)))
    if n <= 1:
        return []
    # 段太碎就往回收，直到每段 ≥ 下限
    while n > 1 and (duration / n) < MIN_SEGMENT_SECONDS:
        n -= 1
    if n <= 1:
        return []
    seg = duration / n
    return [round(seg * i, 3) for i in range(1, n)]


# ═══════════════════════════════════════════════════════════════════
# 五、audit_shot_plan：节奏塌陷判定 + 切点方案
# ═══════════════════════════════════════════════════════════════════

def audit_shot_plan(shots: Iterable[Dict[str, Any]], *, pacing: str = DEFAULT_PACING,
                    window: float = DEFAULT_WINDOW) -> Dict[str, Any]:
    """审一份**镜头表**（还没切片前）：切点密度 + 景别节奏够不够。

    返回（字段名按任务卡）：
      `{"ok","score","mean_cut","median_cut","windows":[{"start","end","changed","why"}],
        "shot_sizes":[...],"long_takes":[...],"fixes":[...],"pacing_target":{...},"legend":{...}}`
    另外附：`"collapse"`, `"verdict"`, `"collapse_ratio"`, `"no_cut_window_ratio"`,
    `"cut_points"`, `"target_cuts"`, `"score_parts"`, `"shot_table"`。

    ★ 判定口径（两个都报，别混）：
      · `collapse` = **切点密度不足**（真实切点 < 目标的 60%）或 **景别用到尾**（只有 1 种）。
        这是"节奏塌陷"的真判据 —— 30 秒只有 1 个切点就是它。
      · `collapse_ratio` = 任务卡口径：3 秒窗口里**既没切点也没台词/动作变化**的比例。
        慢推镜的长镜头靠台词/动作能过这一条，所以它单独不能证明"节奏好"。
    """
    rows = _normalize_shots(shots)
    win = float(_as_float(window, DEFAULT_WINDOW) or DEFAULT_WINDOW)
    if win <= 0:
        win = DEFAULT_WINDOW
    tiers, tier_source = _resolve_tiers()
    target = _tier_seconds(pacing, tiers=tiers)
    total = round(sum(r["duration"] for r in rows), 3)
    pacing_key = pacing if str(pacing) in tiers else f"{target:g}s"

    # 空镜头表：明确判"不可审"，不给一个看起来很高的分（空表没有节奏可言）
    if not rows or total <= 0:
        return {
            "ok": False, "score": 0, "score_parts": {}, "mean_cut": 0.0, "median_cut": 0.0,
            "cut_intervals": [], "windows": [], "shot_sizes": [], "long_takes": [], "fixes": [],
            "pacing_target": {"pacing": pacing_key, "cut_seconds": target, "window": win,
                              "total_seconds": total, "shot_count": 0, "target_segments": 0,
                              "target_cuts": 0, "cut_points_now": 0, "cut_points_after_fix": 0,
                              "collapsed": True},
            "cut_points": 0, "cut_points_now": 0, "target_cuts": 0,
            "collapse": True, "collapse_ratio": 0.0, "no_cut_window_ratio": 0.0,
            "verdict": "不可审：镜头表为空或时长全为 0（没有分镜就没有节奏可言）",
            "shot_table": [],
            "legend": {"pacing_tiers": dict(tiers), "pacing_source": tier_source,
                       "pacing_used": pacing_key, "cut_seconds": target, "window_seconds": win,
                       "notes": ["空镜头表直接判 ok=False / score=0，不编数字"]},
        }

    # ── 现有切点 = 镜头边界 ──
    extent_cuts = [round(r["start"], 3) for r in rows[1:]]      # 镜头之间的接缝
    # 现有切点间隔（含首段与末段）→ mean/median_cut
    bounds = [0.0] + extent_cuts + [total]
    intervals = [round(b - a, 3) for a, b in zip(bounds, bounds[1:]) if b - a > 1e-6]
    mean_cut = round(sum(intervals) / len(intervals), 3) if intervals else 0.0
    median_cut = round(sorted(intervals)[len(intervals) // 2], 3) if intervals else 0.0

    # ── 目标切点数：total 秒按 target 秒一段需要几个切点 ──
    target_segments = max(1, int(round(total / target))) if total > 0 else 0
    target_cuts = max(0, target_segments - 1)

    # ── 长镜头与切点方案 ──
    long_takes: List[Dict[str, Any]] = []
    fixes: List[Dict[str, Any]] = []
    proposed: List[Dict[str, Any]] = []
    for row in rows:
        dur = float(row["duration"])
        offsets = _split_offsets(dur, target)
        if offsets:
            pieces = len(offsets) + 1
            long_takes.append({
                "shot_id": row["shot_id"],
                "duration": dur,
                "fix": (f"在镜头内 {offsets} 处下刀 → {pieces} 段 × {dur / pieces:.3f}s"
                        f"（{pacing_key} 档 {target:g}s）"),
            })
            fixes.append({"action": "split", "shot_id": row["shot_id"],
                          "at": offsets, "pieces": pieces,
                          "segment_seconds": round(dur / pieces, 3)})
            for k, off in enumerate(offsets):
                proposed.append({"shot_id": row["shot_id"], "at": off,
                                 "segment_index": k + 1, "pieces": pieces})
        elif dur > target * LONG_TAKE_RATIO:
            long_takes.append({"shot_id": row["shot_id"], "duration": dur,
                               "fix": "长镜头但按当前档位拆不出合规短段，建议整段重拍/换景别"})

    # 景别用到尾：相邻镜头同景别 → 再加一条"换景别"建议
    for a, b in zip(rows, rows[1:]):
        same = a["shot_size"] == b["shot_size"] and a["shot_size"] != "unknown"
        if same:
            fixes.append({
                "action": "vary_shot_size",
                "shot_id": b["shot_id"],
                "at": [],
                "note": (f"与上一个镜头 {a['shot_id']} 同为 {SIZE_ZH.get(b['shot_size'], b['shot_size'])}"
                         f"（{b['shot_size']}）—— 建议改特写或全景，别一个景别用到尾"),
            })

    all_cuts = sorted(set(extent_cuts) | {round(p["at"] + next(r["start"] for r in rows if r["shot_id"] == p["shot_id"]), 3)
                                          for p in proposed})
    cut_points_now = len(extent_cuts)
    cut_points_after = len(all_cuts)

    # ── 3 秒窗口（任务卡口径） ──
    # 切点用**当前**方案（镜头边界）；台词/动作变化来自 layer2_timeline 分块。
    # 归属规则（避免边界处的切点被算漏）：
    #   · 切点 t 属于 (start, end] 窗口（切点落在窗口末尾那一帧，算这个窗口的）
    #   · 台词/动作 t 属于 [start, end) 窗口（事件从窗口开头落地，算这个窗口的）
    windows: List[Dict[str, Any]] = []
    if total > 0:
        k = 0
        while k * win < total - 1e-6:
            w_start = round(k * win, 3)
            w_end = round(min(total, (k + 1) * win), 3)
            why: List[str] = []
            cuts_in = [c for c in extent_cuts if w_start < c <= w_end + 1e-9]
            for c in cuts_in:
                why.append(f"切点@{c:g}s")
            for row in rows:
                for beat in row["beats"]:
                    t = round(row["start"] + beat["t"], 3)
                    if w_start - 1e-9 <= t < w_end - 1e-9:
                        if beat["kind"] == "dialogue":
                            why.append(f"台词「{beat['text']}」@{t:g}s")
                        elif beat["kind"] == "size_change":
                            why.append(f"{beat['text']}@{t:g}s")
                        else:
                            why.append(f"动作/机位变化@{t:g}s")
            changed = bool(why)
            if not changed:
                why.append("3 秒内没有切点、没有台词、没有动作/机位变化")
            windows.append({"start": w_start, "end": w_end, "changed": changed,
                            "why": "；".join(why[:4])})
            k += 1

    total_windows = len(windows)
    changed_windows = sum(1 for w in windows if w["changed"])
    collapse_ratio = round((total_windows - changed_windows) / total_windows, 4) if total_windows else 0.0
    no_cut_windows = sum(1 for w in windows if not any(c for c in extent_cuts
                                                       if w["start"] < c <= w["end"] + 1e-9))
    no_cut_window_ratio = round(no_cut_windows / total_windows, 4) if total_windows else 0.0

    # ── 景别 ──
    shot_sizes = [{"shot_id": r["shot_id"], "order_index": r["order_index"],
                   "shot_size": r["shot_size"], "zh": SIZE_ZH.get(r["shot_size"], r["shot_size"]),
                   "basis": r["size_basis"], "duration": r["duration"]} for r in rows]
    known_sizes = [s["shot_size"] for s in shot_sizes if s["shot_size"] != "unknown"]
    distinct_sizes = len(set(known_sizes))

    # ── 打分：切点密度 50% + 有切点的窗口比例 30% + 景别多样性 20% ──
    cut_score = min(1.0, (cut_points_now / target_cuts)) if target_cuts else 1.0
    window_score = min(1.0, 1.0 - no_cut_window_ratio)
    want_sizes = min(3, max(1, len(rows)))
    size_score = min(1.0, distinct_sizes / want_sizes) if rows else 0.0
    score = int(round(100.0 * (0.5 * cut_score + 0.3 * window_score + 0.2 * size_score)))

    density_ok = cut_points_now >= max(1, int(round(target_cuts * CUT_DENSITY_TOLERANCE))) if target_cuts else True
    size_ok = distinct_sizes >= want_sizes
    collapse = (not density_ok) or (bool(rows) and distinct_sizes <= 1)

    reasons: List[str] = []
    if not density_ok:
        reasons.append(f"{total:g} 秒只有 {cut_points_now} 个切点（平均 {mean_cut:g} 秒一切），"
                       f"{pacing_key} 档要 {target_cuts} 个（{target:g} 秒级）")
    if rows and distinct_sizes <= 1:
        only = SIZE_ZH.get(known_sizes[0], known_sizes[0]) if known_sizes else "unknown"
        reasons.append(f"景别只有 {distinct_sizes} 种（{only}），一个景别用到尾")
    if collapse and len(long_takes) and not reasons:
        reasons.append(f"{len(long_takes)} 个长镜头超过 {target * LONG_TAKE_RATIO:g} 秒")
    if collapse:
        verdict = "节奏塌陷：" + "；".join(reasons)
        if long_takes:
            verdict += f"；方案：{cut_points_after} 个切点可把最长镜头压到 {target:g} 秒级"
    elif collapse_ratio >= COLLAPSE_RATIO_SLOW:
        verdict = f"偏慢：{int(round(collapse_ratio * 100))}% 的 3 秒窗口没有任何变化"
    else:
        verdict = "合格：切点密度与景别节奏都到位"

    legend = {
        "pacing_tiers": dict(tiers),
        "pacing_source": tier_source,
        "pacing_used": pacing_key,
        "cut_seconds": target,
        "window_seconds": win,
        "frame_delta_changed": FRAME_DELTA_CHANGED,
        "hist_delta_changed": HIST_DELTA_CHANGED,
        "audio_delta_changed_db": AUDIO_DELTA_CHANGED_DB,
        "long_take_ratio": LONG_TAKE_RATIO,
        "min_segment_seconds": MIN_SEGMENT_SECONDS,
        "cut_density_tolerance": CUT_DENSITY_TOLERANCE,
        "size_ladder": list(SIZE_LADDER),
        "notes": [
            f"窗口判定：切点 t 属于 (start,end]；台词/动作 t 属于 [start,end)（边界处不漏算）",
            f"长镜头 = 时长 > {target:g}s × {LONG_TAKE_RATIO} = {target * LONG_TAKE_RATIO:g}s",
            f"节奏塌陷判据 = 切点密度 < 目标×{CUT_DENSITY_TOLERANCE} 或 景别种类 ≤ 1（不是窗口比例）",
            f"collapse_ratio 是任务卡口径（3 秒窗口无变化的占比），慢推长镜头能靠台词/动作过",
        ],
    }

    return {
        "ok": not collapse,
        "score": score,
        "score_parts": {"cut_density": round(cut_score, 3), "windows": round(window_score, 3),
                        "shot_size_variety": round(size_score, 3),
                        "weights": "0.5/0.3/0.2"},
        "mean_cut": mean_cut,
        "median_cut": median_cut,
        "cut_intervals": intervals,
        "windows": windows,
        "shot_sizes": shot_sizes,
        "long_takes": long_takes,
        "fixes": fixes,
        "pacing_target": {
            "pacing": pacing_key,
            "cut_seconds": target,
            "window": win,
            "total_seconds": total,
            "shot_count": len(rows),
            "target_segments": target_segments,
            "target_cuts": target_cuts,
            "cut_points_now": cut_points_now,
            "cut_points_after_fix": cut_points_after,
            "collapsed": collapse,
        },
        "cut_points": cut_points_after,
        "cut_points_now": cut_points_now,
        "target_cuts": target_cuts,
        "collapse": collapse,
        "collapse_ratio": collapse_ratio,
        "no_cut_window_ratio": no_cut_window_ratio,
        "verdict": verdict,
        "shot_table": [{k: r[k] for k in ("shot_id", "order_index", "duration", "start", "end",
                                          "shot_size", "size_basis", "dialogue")} for r in rows],
        "legend": legend,
    }


# ═══════════════════════════════════════════════════════════════════
# 六、plan_cut_points：非等分拆点（layer2 时间片当切点）
# ═══════════════════════════════════════════════════════════════════
#
# 为什么不是均分：每镜的 layer2_timeline 本来就是内容驱动的 2~5 秒时间片
# （一个动作 / 一句话 / 一次景别变化），它是**内容给出的天然切点**。
# 旧版剪辑层把它丢掉、按固定 cut_seconds 等分，实测 30 秒 8 段只有 2 个取值
# （3.776s 重复），被判「同段同值 100%」。这里改成：
#   权重 = 每片 end-start → 按比例映射到**实际** clip_duration
#   （素材可能被裁过头尾，时间片里的绝对秒数不能直接搬）。
# 段数 / 夹取 / 总长精确的规则写在 plan_cut_points 的 docstring 里。

def _timeline_weights(timeline: Any) -> Tuple[List[float], str]:
    """把 timeline 解析成**每片权重**（end-start 秒）+ 解析说明。

    宽容接受（method_gate.plan_edit 传的是 list[dict{start,end,intent,dialogue}]）：
      · None → []（调用方走均分）
      · JSON 字符串 → 先 json.loads 再递归
      · dict 带 layer2_timeline / timeline / blocks → 取那个值再递归
      · dict 带 start/end → 单个时间片
      · list/tuple 元素：dict{start,end} / (start,end) / 纯数字（当片长）
    只保留 end>start 的片；不猜缺口，只用**片长**当权重。
    """
    if timeline is None:
        return [], "None"
    if isinstance(timeline, str):
        try:
            ws, why = _timeline_weights(json.loads(timeline or "null"))
            return ws, f"JSON字符串: {why}"
        except Exception:
            return [], "字符串但不是合法 JSON"
    if isinstance(timeline, dict):
        for key in ("layer2_timeline", "timeline", "blocks"):
            if key in timeline:
                ws, why = _timeline_weights(timeline.get(key))
                if ws:
                    return ws, f"dict.{key}: {why}"
        blocks: List[Any] = [timeline]
    elif isinstance(timeline, (list, tuple)):
        blocks = list(timeline)
    else:
        return [], f"不支持的 timeline 类型 {type(timeline).__name__}"

    weights: List[float] = []
    for item in blocks:
        w = 0.0
        if isinstance(item, dict):
            start = _as_float(item.get("start"), None)
            end = _as_float(item.get("end"), None)
            if start is not None and end is not None:
                w = float(end) - float(start)
            else:
                w = float(_as_float(item.get("duration"), 0.0) or 0.0)
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            start = _as_float(item[0], None)
            end = _as_float(item[1], None)
            if start is not None and end is not None:
                w = float(end) - float(start)
        else:
            w = float(_as_float(item, 0.0) or 0.0)
        if w > 0.05:
            weights.append(round(w, 4))
    return weights, f"{len(weights)} 片"


def _merge_to_count(groups: List[List[float]], target: int) -> Tuple[List[List[float]], int]:
    """把相邻片合并到 target 组：每次合并「权重和最小」的相邻对。

    「最小相邻对」不是随便挑：2~5 秒级的片本来就是内容节拍，合并掉最短的一对
    只吃掉最碎的那个节拍，大块叙事节拍照旧保留（任务卡：保留自然节拍）。
    """
    groups = [list(g) for g in groups]
    merged = 0
    target = max(1, int(target))
    while len(groups) > target:
        sums = [sum(g) for g in groups]
        i = min(range(len(groups) - 1), key=lambda k: (sums[k] + sums[k + 1], k))
        groups[i:i + 2] = [groups[i] + groups[i + 1]]
        merged += 1
    return groups, merged


def _split_until_count(groups: List[List[float]], total: float, min_seg: float,
                       max_seg: float, max_count: int) -> Tuple[List[List[float]], int]:
    """段太少（有段 > max_seg）时**均分当前最大的那片**，直到满足或到段数上限。"""
    groups = [list(g) for g in groups]
    splits = 0
    max_count = max(1, int(max_count))
    while len(groups) < max_count:
        sums = [sum(g) for g in groups]
        wsum = sum(sums) or 1.0
        durs = [s * total / wsum for s in sums]
        if not durs or max(durs) <= max_seg + 1e-9:
            break
        did = False
        for k in sorted(range(len(groups)), key=lambda k: -durs[k]):
            if durs[k] / 2.0 < min_seg - 1e-9:
                continue  # 再切就违反单段下限
            half = sums[k] / 2.0
            groups[k:k + 1] = [[half], [half]]
            splits += 1
            did = True
            break
        if not did:
            break
    return groups, splits


def _fit_segment_durations(weights: List[float], total: float,
                           min_seg: float, max_seg: float) -> List[float]:
    """按权重比例分配 total，再把每段夹进 [min_seg, max_seg] 且**总和不变**。

    做法（改进水位法）：越界段钉到边界，剩余段按原比例重分剩余时间，迭代到稳定。
    相比「先 clamp 再把差额摊到尾段」，这里每段都可能被抬/压，不会把尾巴切成碎段。
    """
    n = len(weights)
    if n <= 0:
        return []
    if n == 1:
        return [float(total)]
    wsum = sum(weights)
    d = [total * (w / wsum) for w in weights] if wsum > 0 else [total / n] * n
    for _ in range(32):
        fixed: Dict[int, float] = {}
        for i, v in enumerate(d):
            if v < min_seg - 1e-12:
                fixed[i] = float(min_seg)
            elif v > max_seg + 1e-12:
                fixed[i] = float(max_seg)
        if not fixed:
            break
        free = [i for i in range(n) if i not in fixed]
        if not free:
            break
        rem = total - sum(fixed.values())
        fw = sum(d[i] for i in free)
        new = list(d)
        for i in free:
            new[i] = rem * (d[i] / fw) if fw > 1e-12 else rem / len(free)
        for i, v in fixed.items():
            new[i] = v
        moved = max(abs(new[i] - d[i]) for i in range(n))
        d = new
        if moved < 1e-12:
            break
    diff = total - sum(d)
    if abs(diff) > 1e-9:
        i = max(range(n), key=lambda k: d[k])
        d[i] += diff
    return d


def _segment_look(i: int, n: int) -> Dict[str, Any]:
    """第 i/n 段的二次构图参数（与旧版 build_cut_plan **完全同一套规则**）。

    · zoom 沿整条素材单调 1.00 → 1.12（越切越近）
    · 段内 push / pull **交替**，相邻两段一个在推、一个在拉
    · 锚点 x/y 也交替左右、上下，让相邻段连背景线条都不在同一处
    """
    z_base = ZOOM_MIN + (ZOOM_MAX - ZOOM_MIN) * (i / (n - 1)) if n > 1 else ZOOM_MIN
    z_base = min(ZOOM_MAX - ZOOM_INTRASEG_STEP, max(ZOOM_MIN, z_base))
    direction = "push" if i % 2 == 0 else "pull"
    if direction == "push":
        z_from, z_to = z_base, min(ZOOM_MAX, z_base + ZOOM_INTRASEG_STEP)
    else:
        z_from, z_to = min(ZOOM_MAX, z_base + ZOOM_INTRASEG_STEP), z_base
    anchor = ANCHOR_X if i % 2 == 0 else -ANCHOR_X
    anchor_y = ANCHOR_Y if i % 2 == 0 else -ANCHOR_Y
    return {"zoom": round(z_base, 3), "zoom_from": round(z_from, 3),
            "zoom_to": round(z_to, 3), "direction": direction,
            "anchor_x": round(anchor, 4), "anchor_y": round(anchor_y, 4)}


def _cuts_from_bounds(bounds: Sequence[float], source: str) -> List[Dict[str, Any]]:
    """把 `[0, t1, ..., tN]` 时间边界变成带 zoom 的 cuts 数组（in/out 均为绝对秒）。"""
    n = len(bounds) - 1
    cuts: List[Dict[str, Any]] = []
    for i in range(n):
        start = round(float(bounds[i]), 3)
        end = round(float(bounds[i + 1]), 3)
        cut: Dict[str, Any] = {"in": start, "out": end,
                               "duration": round(end - start, 3),
                               "index": i, "source": source}
        cut.update(_segment_look(i, n))
        cuts.append(cut)
    return cuts


def plan_cut_points(clip_duration: float, timeline: Any = None, *, pacing: str = "promo",
                    cut_seconds: Optional[float] = None, max_cuts_per_clip: int = 3,
                    min_segment: float = MIN_SEGMENT_SECONDS,
                    max_segment: float = MAX_SEGMENT_SECONDS) -> Dict[str, Any]:
    """给**一条**素材算切点：有 layer2 时间片就非等分，没有就按档位均分。

    · timeline 给了 → weights = 每片 `end-start`，按比例映射到**实际** clip_duration；
        目标段数 = `min(len(timeline), max_cuts_per_clip + 1)`；
        片太多 → 反复合并「权重和最小的相邻对」（保留自然节拍）；
        段太少（有段会超 max_segment）→ 均分最大的那片，直到满足或到上限；
        每段夹在 `[min_segment, max_segment]`，末段收口 → 总长**精确**等于 clip_duration。
    · timeline=None → **完全走旧逻辑**（按 pacing 档位均分，输出与改动前一致）。
    · zoom 沿用旧规则（1.00→1.12 单调 + push/pull 交替 + 锚点交替）。

    返回 `{"cuts":[{"in","out","duration","zoom","index","source",...}],
           "source":"timeline|uniform","weights":[每段权重秒],"raw_weights":[原始片长],
           "segments":N,"target_segments":N,"merged_pairs":k,"splits":k,
           "ideal_segments":N,"truncated_by_cap":bool,"total_seconds":...,
           "in_bounds":bool,"note":...}`。
    """
    total = round(float(_as_float(clip_duration, 0.0) or 0.0), 3)
    min_seg = float(_as_float(min_segment, MIN_SEGMENT_SECONDS) or MIN_SEGMENT_SECONDS)
    max_seg = float(_as_float(max_segment, MAX_SEGMENT_SECONDS) or MAX_SEGMENT_SECONDS)
    if min_seg <= 0:
        min_seg = MIN_SEGMENT_SECONDS
    if max_seg < min_seg:
        max_seg = min_seg
    try:
        cap = int(max_cuts_per_clip)
    except Exception:
        cap = 3
    cap = max(0, cap)

    result: Dict[str, Any] = {
        "clip_duration": total, "cuts": [], "source": "uniform",
        "weights": [], "raw_weights": [], "segments": 0, "target_segments": 0,
        "merged_pairs": 0, "splits": 0, "ideal_segments": 0,
        "truncated_by_cap": False, "total_seconds": 0.0, "in_bounds": True,
        "note": "", "timeline_hint": "",
    }
    if total <= 0:
        result["note"] = f"clip_duration 非正（{clip_duration!r}），不给切点"
        return result

    raw, hint = _timeline_weights(timeline)
    result["timeline_hint"] = hint
    result["raw_weights"] = list(raw)

    if not raw:
        # ── 旧逻辑：按档位均分（回归就靠这条路径，别改这里的行为）──
        tiers, _src = _resolve_tiers()
        target = _tier_seconds(pacing, cut_seconds, tiers=tiers)
        ideal = max(1, int(round(total / target)))
        n = max(1, min(ideal, cap + 1))
        while n > 1 and (total / n) < min_seg:
            n -= 1
        seg = total / n
        bounds = [round(seg * k, 3) for k in range(n)] + [round(total, 3)]
        cuts = _cuts_from_bounds(bounds, "uniform")
        result.update({
            "cuts": cuts, "source": "uniform", "segments": len(cuts),
            "target_segments": n, "ideal_segments": ideal,
            "truncated_by_cap": ideal > cap + 1,
            "weights": [round(seg, 3)] * len(cuts),
            "total_seconds": round(sum(c["duration"] for c in cuts), 3),
        })
        note = (f"无 layer2 时间片，按 {pacing if str(pacing) else DEFAULT_PACING} 档均分 "
                f"{len(cuts)} 段 × {seg:.3f}s")
        if cuts and max(c["duration"] for c in cuts) > max_seg + 1e-9:
            note += (f"；均值超 max_segment={max_seg:g}s，但均分路径以 "
                     f"max_cuts_per_clip 上限优先，不额外增段（旧行为）")
        result["note"] = note
        return result

    # ── 非等分：时间片当权重 ──
    n_min_feasible = max(1, int(math.floor(total / min_seg + 1e-9)))
    target_n = max(1, min(len(raw), cap + 1, n_min_feasible))
    groups, merged = _merge_to_count([[w] for w in raw], target_n)
    n_need = max(1, int(math.ceil(total / max_seg - 1e-9))) if total > max_seg else 1
    groups, splits = _split_until_count(groups, total, min_seg, max_seg, cap + 1)

    seg_weights = [round(sum(g), 3) for g in groups]
    n_groups = len(seg_weights)
    # 可行性：n 段 × [min,max] 能不能装下总长。装不下时**不硬压**边界
    # （否则会出现「前面全 6s、尾段吞掉剩余」这种畸形分布），保持按权重比例，
    # 并在 in_bounds / note 里如实标出被上限截断。
    feasible = (n_groups * min_seg - 1e-9) <= total <= (n_groups * max_seg + 1e-9)
    if feasible:
        durs = _fit_segment_durations(seg_weights, total, min_seg, max_seg)
    else:
        wsum = sum(seg_weights) or 1.0
        durs = [total * (w / wsum) for w in seg_weights]
    bounds: List[float] = [0.0]
    cum = 0.0
    for d in durs[:-1]:
        cum += d
        bounds.append(round(cum, 3))
    bounds.append(round(total, 3))
    cuts = _cuts_from_bounds(bounds, "timeline")

    in_bounds = all(min_seg - 1e-3 <= c["duration"] <= max_seg + 1e-3 for c in cuts)
    truncated = bool((len(raw) > cap + 1) or (n_need > cap + 1))
    raw_txt = "/".join(f"{w:g}" for w in raw)
    dur_txt = "/".join(f"{c['duration']:.3f}" for c in cuts)
    out_txt = "/".join(f"{c['out']:g}" for c in cuts[:-1])
    note = (f"layer2 时间片 {len(raw)} 片（{raw_txt}s）→ {len(cuts)} 段"
            f"（{dur_txt}s），切点 {out_txt or '无（整段一刀不切）'}")
    if merged:
        note += f"；合并 {merged} 对相邻片（保留自然节拍）"
    if splits:
        note += f"；均分最大片 {splits} 次（有段超过 {max_seg:g}s 上限）"
    if not in_bounds:
        note += (f"；**警告**：有段越出 [{min_seg:g},{max_seg:g}]s，"
                 f"该参数组合本身不可行（总长 {total:g}s）")
    if truncated:
        note += (f"；**被上限截断**：max_cuts_per_clip={cap} 只允许 {cap + 1} 段，"
                 f"按 {max_seg:g}s 上限需要 {n_need} 段")
    result.update({
        "cuts": cuts, "source": "timeline", "segments": len(cuts),
        "target_segments": target_n, "ideal_segments": target_n,
        "merged_pairs": merged, "splits": splits,
        "weights": [round(w, 3) for w in seg_weights],
        "truncated_by_cap": truncated, "in_bounds": in_bounds,
        "total_seconds": round(sum(c["duration"] for c in cuts), 3),
        "note": note,
    })
    return result


# ═══════════════════════════════════════════════════════════════════
# 七、build_cut_plan：把素材拆成带渐进二次构图的段
# ═══════════════════════════════════════════════════════════════════

def build_cut_plan(clips: Iterable[Any], *, pacing: str = DEFAULT_PACING,
                   cut_seconds: Optional[float] = None,
                   max_cuts_per_clip: int = 3,
                   timelines: Optional[Iterable[Any]] = None) -> Dict[str, Any]:
    """给每条素材算出切点、每段 `zoom` 与推拉方向。

    `clips` 可以是路径字符串，也可以是 `{"path"/"src", "duration_seconds"}`。
    `cut_seconds` 省略时取 pacing 档位。`max_cuts_per_clip` = 单条素材**最多下几刀**
    （所以最多 `max_cuts_per_clip + 1` 段）。

    `timelines`（2026-10-24 新增，可选）：**逐条素材**的 layer2 时间片，与 `clips` 同序，
    形如 `[[{"start","end"},...], ...]`（也接受 JSON 字符串 / (start,end) 元组 / None）。
    某项有值 → 交给 `plan_cut_points` 做**非等分**拆点（时间片 = 天然切点）；
    某项为 None 或 `timelines=None`（默认）→ **完全走旧逻辑**（按档位均分），
    所以旧调用的输出（段数/时长/note）与改动前一致，只多了 2 个统计字段。

    ★ 上限与档位的冲突**明说**（在 `note` 里）：15.104 秒素材用 fast_drama 2.5 秒档
      本该 6 段（5 刀），如果 `max_cuts_per_clip=3` 就只切 4 段（每段 3.776 秒），
      note 会写"被上限截断"，不会把 3.776 秒冒充成 2.5 秒。
    """
    tiers, tier_source = _resolve_tiers()
    target = _tier_seconds(pacing, cut_seconds, tiers=tiers)
    pacing_key = pacing if str(pacing) in tiers else f"{target:g}s"
    try:
        cap = int(max_cuts_per_clip)
    except Exception:
        cap = 3
    cap = max(0, cap)
    max_segments = cap + 1

    # 逐条素材的 layer2 时间片；None = 该素材走旧均分逻辑
    tl_items: Optional[List[Any]] = None
    if timelines is not None:
        try:
            tl_items = list(timelines)
        except TypeError:
            tl_items = None

    cuts: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    per_clip: List[Dict[str, Any]] = []
    total = 0.0

    for ci, clip in enumerate(clips or []):
        if isinstance(clip, dict):
            src = str(clip.get("path") or clip.get("src") or clip.get("file") or "")
            dur_hint = _as_float(clip.get("duration_seconds") or clip.get("duration"))
        else:
            src = str(clip)
            dur_hint = None
        if not src or not os.path.exists(src):
            skipped.append({"src": src, "reason": "文件不存在"})
            continue
        info = probe_media(src)
        dur = float(info.get("duration") or 0.0)
        if dur <= 0 and dur_hint:
            dur = float(dur_hint)
        if dur <= 0:
            skipped.append({"src": src, "reason": f"读不到时长（probe={info.get('probe')}）"})
            continue

        # 逐条素材：有 layer2 时间片 → 非等分；没有 → 旧均分逻辑
        item = tl_items[ci] if (tl_items is not None and ci < len(tl_items)) else None
        cp = plan_cut_points(dur, item, pacing=pacing, cut_seconds=cut_seconds,
                             max_cuts_per_clip=cap, min_segment=MIN_SEGMENT_SECONDS,
                             max_segment=MAX_SEGMENT_SECONDS)
        n = max(1, int(cp.get("segments") or 1))
        ideal_segments = int(cp.get("ideal_segments") or n)
        truncated = bool(cp.get("truncated_by_cap"))
        clip_cuts: List[Dict[str, Any]] = []
        for c in cp.get("cuts") or []:
            start = round(float(c["in"]), 3)
            end = round(float(c["out"]), 3)
            if end - start <= 0.05:
                continue
            # 渐进二次构图参数由 plan_cut_points 的 _segment_look 给（旧规则原样）
            cut: Dict[str, Any] = {
                "src": src,
                "clip_index": ci,
                "index": len(cuts),
                "in": start,
                "out": end,
                "duration": round(end - start, 3),
                "zoom": c.get("zoom"),
                "zoom_from": c.get("zoom_from"),
                "zoom_to": c.get("zoom_to"),
                "direction": c.get("direction"),
                "anchor_x": c.get("anchor_x"),
                "anchor_y": c.get("anchor_y"),
                "source": cp.get("source"),
            }
            clip_cuts.append(cut)
            cuts.append(cut)
            total += cut["duration"]

        per_clip.append({
            "src": src, "duration": round(dur, 3), "segments": n,
            "segment_seconds": round(dur / n, 3) if n else None,
            "ideal_segments": ideal_segments,
            "truncated_by_cap": truncated,
            "cap_seconds_per_segment": round(dur / max_segments, 3) if max_segments else None,
            "source": cp.get("source"),
            "segment_durations": [c["duration"] for c in clip_cuts],
            "cut_points": [c["out"] for c in clip_cuts[:-1]],
            "timeline_weights": (list(cp.get("weights") or [])
                                 if cp.get("source") == "timeline" else None),
        })

    notes: List[str] = []
    any_timeline = any(pc.get("source") == "timeline" for pc in per_clip)
    if not cuts:
        notes.append("没有任何可用素材（全部 skipped），切点方案为空。")
    elif any_timeline:
        dur_vals = sorted({round(float(c["duration"]), 1) for c in cuts})
        notes.append(
            f"{len(per_clip)} 条素材 → {len(cuts)} 段 / {len(cuts) - len(per_clip)} 个切点，"
            f"合计 {total:.3f} 秒（按 layer2 时间片**非等分**拆点，"
            f"没有时间片的素材才按 {pacing_key} 档 {target:g} 秒/段均分）。"
            f"段长取值 {len(dur_vals)} 个：{'/'.join(f'{d:g}' for d in dur_vals)} 秒。")
    else:
        notes.append(f"{len(per_clip)} 条素材 → {len(cuts)} 段 / {len(cuts) - len(per_clip)} 个切点，"
                     f"合计 {total:.3f} 秒（{pacing_key} 档目标 {target:g} 秒/段）。")
    for pc in per_clip:
        if pc.get("source") == "timeline":
            durs = pc.get("segment_durations") or []
            outs = pc.get("cut_points") or []
            notes.append(
                f"{os.path.basename(pc['src'])}：时间片 {pc.get('timeline_weights')} → "
                f"{pc['segments']} 段（{'/'.join(f'{d:.3f}' for d in durs)}s），"
                f"切点 {'/'.join(f'{t:g}' for t in outs) or '整段不切'}（source=timeline）")
            if pc["truncated_by_cap"]:
                notes.append(
                    f"{os.path.basename(pc['src'])}：**被上限截断**——max_cuts_per_clip={cap} "
                    f"只允许 {max_segments} 段，内容时间片想要 {pc['ideal_segments']} 段。")
        elif pc["truncated_by_cap"]:
            notes.append(
                f"{os.path.basename(pc['src'])}：按 {target:g}s 档本该 {pc['ideal_segments']} 段，"
                f"但 max_cuts_per_clip={cap} 只允许 {pc['segments']} 段 → 每段 {pc['segment_seconds']:.3f}s"
                f"（**被上限截断**，要落 {target:g}s 档请把 max_cuts_per_clip 提到 "
                f"{max(1, pc['ideal_segments'] - 1)}+）")
    if skipped:
        notes.append(f"跳过 {len(skipped)} 条："
                     + "、".join(os.path.basename(s["src"]) or "(空)" for s in skipped[:4]))

    duration_values = sorted({round(float(c["duration"]), 1) for c in cuts})
    return {
        "cuts": cuts,
        "per_clip": per_clip,
        "skipped": skipped,
        "total_seconds": round(total, 3),
        "cuts_count": len(cuts) - len(per_clip),   # 切点数（段与段之间的刀口）
        "segments_count": len(cuts),               # 段数
        "pacing": pacing_key,
        "pacing_source": tier_source,
        "cut_seconds": target,
        "source_per_clip": [pc.get("source") for pc in per_clip],
        "distinct_durations": len(duration_values),
        "duration_values": duration_values,
        "note": " ".join(notes),
    }


# ═══════════════════════════════════════════════════════════════════
# 八、apply_cut_plan：ffmpeg 真切片 + 渐进二次构图，落盘并逐段校验
# ═══════════════════════════════════════════════════════════════════

def _reframe_filter(info: Dict[str, Any], cut: Dict[str, Any], *, progressive: bool = True) -> str:
    """生成"渐进二次构图"滤镜。

    主路径（progressive=True）用 zoompan 逐帧改缩放，得到段内**真的在推/拉**的镜头：
      · z 段内从 zoom_from 线性到 zoom_to（`on` = 输出帧号）
      · x 在居中基础上加一点交替偏移，让相邻两段连背景线条都不在同一处
    兜底（progressive=False，zoompan 在该 ffmpeg 构建上不可用时）：
      scale 放大 → crop 取窗口 → scale 回原尺寸（静态构图，仍然换焦段）
    """
    w = int(info.get("width") or 0) or 864
    h = int(info.get("height") or 0) or 496
    fps = float(info.get("fps") or 24.0) or 24.0
    dur = float(cut.get("duration") or 0.0) or (float(cut["out"]) - float(cut["in"]))
    frames = max(1, int(round(dur * fps)))
    z_from = float(cut.get("zoom_from", cut.get("zoom", ZOOM_MIN)))
    z_to = float(cut.get("zoom_to", cut.get("zoom", ZOOM_MIN)))
    kx = float(cut.get("anchor_x", 0.0))
    ky = float(cut.get("anchor_y", 0.0))
    fps_txt = f"{fps:g}"

    if progressive:
        # min/max 夹住，避免 zoom=1.0 时 x/y 越过画幅边界导致黑边
        z_expr = f"min({ZOOM_MAX:g},max({ZOOM_MIN:g},{z_from:.4f}+({z_to:.4f}-{z_from:.4f})*on/{frames}))"
        x_expr = f"min(iw-iw/zoom,max(0,(iw-iw/zoom)/2+(iw/zoom)*{kx:.4f}))"
        y_expr = f"min(ih-ih/zoom,max(0,(ih-ih/zoom)/2+(ih/zoom)*{ky:.4f}))"
        return (f"zoompan=z='{z_expr}':x='{x_expr}':y='{y_expr}'"
                f":d=1:s={w}x{h}:fps={fps_txt},format=yuv420p")

    zoom = max(z_from, z_to)
    zw, zh = int(round(w * zoom)), int(round(h * zoom))
    zw, zh = max(w, zw), max(h, zh)
    x_expr = f"min(iw-{w},max(0,(iw-{w})/2+{w}*{kx:.4f}))"
    y_expr = f"min(ih-{h},max(0,(ih-{h})/2+{h}*{ky:.4f}))"
    return (f"scale={zw}:{zh}:flags=bicubic,crop={w}:{h}:x='{x_expr}':y='{y_expr}',"
            f"scale={w}:{h}:flags=bicubic,format=yuv420p")


def _apply_one(src: str, cut: Dict[str, Any], dst: str, info: Dict[str, Any],
               *, progressive: bool = True) -> Tuple[bool, str, str]:
    """切一段并落盘。返回 (rc_ok, 滤镜串, stderr 尾部)。"""
    w = int(info.get("width") or 0) or 864
    h = int(info.get("height") or 0) or 496
    fps = float(info.get("fps") or 24.0) or 24.0
    fps_txt = info.get("r_frame_rate") or f"{fps:g}"
    start = float(cut["in"])
    dur = float(cut["out"]) - start
    vf = _reframe_filter(info, cut, progressive=progressive)
    cmd = [
        _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-i", src, "-t", f"{max(0.05, dur):.3f}",
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-r", fps_txt,
        "-c:a", "aac", "-b:a", "128k",
        "-ar", str(int(info.get("audio_rate") or 48000)),
        "-ac", str(int(info.get("audio_channels") or 2)),
        "-movflags", "+faststart",
        dst,
    ]
    rc, txt = _run(cmd, timeout=300)
    return rc == 0 and os.path.exists(dst) and os.path.getsize(dst) > 1024, vf, (txt or "")[-400:]


def apply_cut_plan(clips: Iterable[Any], plan: Dict[str, Any], out_dir: str) -> List[Dict[str, Any]]:
    """按 `build_cut_plan` 的结果**真切片**并落盘到 `out_dir`。

    参数 `clips` 保留只为签名一致（真正的源文件在 `plan["cuts"][i]["src"]` 里）。

    每段：ffmpeg `-ss/-t` 精确切 + zoompan 渐进二次构图，保持原分辨率/帧率/音轨；
    落盘后用 ffprobe 回读校验：时长误差 < `DURATION_TOLERANCE`(0.1s)、
    宽高/帧率与源一致、有音轨。zoompan 失败会自动退回静态 scale+crop 重试一次。
    """
    out_dir = str(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    results: List[Dict[str, Any]] = []
    cuts = list((plan or {}).get("cuts") or [])

    for cut in cuts:
        src = str(cut.get("src") or "")
        record: Dict[str, Any] = {
            "path": "", "src": src, "in": float(cut.get("in") or 0.0),
            "out": float(cut.get("out") or 0.0), "zoom": cut.get("zoom"),
            "zoom_from": cut.get("zoom_from"), "zoom_to": cut.get("zoom_to"),
            "direction": cut.get("direction"), "index": cut.get("index"),
            "duration": round(float(cut.get("out") or 0.0) - float(cut.get("in") or 0.0), 3),
            "direction": cut.get("direction"),
            "anchor_x": cut.get("anchor_x"), "anchor_y": cut.get("anchor_y"),
            "ok": False, "error": "", "filter": "", "fallback": False,
        }
        if not src or not os.path.exists(src):
            record["error"] = f"源文件不存在: {src}"
            results.append(record)
            continue

        info = probe_media(src)
        if not info.get("width"):
            record["error"] = f"读不到规格: {info.get('error')}"
            results.append(record)
            continue

        stem = os.path.splitext(os.path.basename(src))[0]
        name = (f"{stem}__seg{int(cut.get('index') or len(results)):02d}"
                f"_{float(cut.get('in') or 0):.3f}-{float(cut.get('out') or 0):.3f}"
                f"_z{float(cut.get('zoom') or 1.0):.2f}.mp4")
        dst = os.path.join(out_dir, name)

        ok, vf, err = _apply_one(src, cut, dst, info, progressive=True)
        if not ok:
            # zoompan 不可用/超参越界 → 静态构图兜底（仍然换焦段，只是段内不推拉）
            logger.warning("zoompan 失败，退回静态构图: %s | %s", name, err)
            ok, vf, err = _apply_one(src, cut, dst, info, progressive=False)
            record["fallback"] = True
        record["path"] = dst
        record["filter"] = vf
        if not ok:
            record["error"] = f"ffmpeg 失败: {err}"
            results.append(record)
            continue

        got = probe_media(dst)
        expect = record["duration"]
        got_dur = float(got.get("duration") or 0.0)
        err_s = round(got_dur - expect, 4)
        same_geo = (got.get("width") == info.get("width")
                    and got.get("height") == info.get("height"))
        fps_ok = abs(float(got.get("fps") or 0.0) - float(info.get("fps") or 0.0)) < 0.01
        record.update({
            "probe_duration": got_dur,
            "duration_error": err_s,
            "width": got.get("width"), "height": got.get("height"), "fps": got.get("fps"),
            "has_audio": bool(got.get("has_audio")),
            "audio_rate": got.get("audio_rate"),
            "same_geometry": same_geo,
            "fps_preserved": fps_ok,
            "ok": (abs(err_s) < DURATION_TOLERANCE) and same_geo and fps_ok
                  and bool(got.get("has_audio")) == bool(info.get("has_audio")),
        })
        if not record["ok"]:
            record["error"] = (f"校验未过: 时长误差 {err_s:+.3f}s（容差 {DURATION_TOLERANCE}s）, "
                               f"几何 {got.get('width')}x{got.get('height')} vs "
                               f"{info.get('width')}x{info.get('height')}, fps {got.get('fps')}")
        results.append(record)

    ok_n = sum(1 for r in results if r["ok"])
    logger.info("apply_cut_plan: %d/%d 段通过校验 → %s", ok_n, len(results), out_dir)
    return results


# ═══════════════════════════════════════════════════════════════════
# 九、audit_film：每 3 秒窗口的画面/声音变化 + 塌陷率
# ═══════════════════════════════════════════════════════════════════

def _frame_metrics(head: Any, tail: Any) -> Tuple[float, float]:
    """(灰度 MAD, 32 桶归一化直方图 L1 距离)。两个都在 0~1 / 0~2 量级。"""
    import numpy as np

    mad = float(np.abs(tail - head).mean())
    h0 = np.histogram(head, bins=32, range=(0.0, 1.0))[0].astype(np.float64)
    h1 = np.histogram(tail, bins=32, range=(0.0, 1.0))[0].astype(np.float64)
    h0 /= max(1.0, h0.sum())
    h1 /= max(1.0, h1.sum())
    hist = float(np.abs(h0 - h1).sum())
    return mad, hist


def _detect_cuts(frames: Any, fps: float) -> List[Dict[str, Any]]:
    """硬切检测（判据见 CUT_JUMP_MIN / CUT_JUMP_RATIO 注释）。"""
    import numpy as np

    n = len(frames)
    if n < 3:
        return []
    diffs = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2))
    cuts: List[Dict[str, Any]] = []
    for i in range(1, len(diffs) - 1):
        lo = max(0, i - CUT_NEIGHBOR_FRAMES)
        hi = min(len(diffs), i + CUT_NEIGHBOR_FRAMES + 1)
        neigh = np.concatenate([diffs[lo:i], diffs[i + 1:hi]])
        local = float(np.median(neigh)) if neigh.size else float(np.median(diffs))
        jump = float(diffs[i])
        if jump > max(CUT_JUMP_MIN, CUT_JUMP_RATIO * local) and jump >= diffs[i - 1] and jump >= diffs[i + 1]:
            cuts.append({"time": round((i + 1) / fps, 3), "jump": round(jump, 4),
                         "local": round(local, 4),
                         "ratio": round(jump / max(local, 1e-6), 1)})
    return cuts


def audit_film(video_path: str, *, window: float = DEFAULT_WINDOW) -> Dict[str, Any]:
    """审一条**成片**：每 3 秒窗口里的画面/声音变化 + 塌陷率 + 硬切。

    返回：
      `{"video","duration","windows":[{"start","end","changed","frame_delta","audio_delta"}],
        "collapse_ratio","verdict"}` + 附 `cuts` / `cut_count` / `longest_no_cut` /
      `frame_delta_max` / `audio_delta_max_db` / `thresholds` / `legend`。
    `audio_delta` 单位 dB（与上一个窗口 RMS 的绝对落差）；无音轨时为 None，不报错。
    """
    win = float(_as_float(window, DEFAULT_WINDOW) or DEFAULT_WINDOW)
    if win <= 0:
        win = DEFAULT_WINDOW
    info = probe_media(video_path)
    dur = float(info.get("duration") or 0.0)
    result: Dict[str, Any] = {
        "video": video_path, "duration": round(dur, 3), "window": win,
        "windows": [], "collapse_ratio": 1.0, "verdict": "",
        "has_audio": bool(info.get("has_audio")),
        "width": info.get("width"), "height": info.get("height"), "fps": info.get("fps"),
        "cuts": [], "cut_count": 0, "longest_no_cut": round(dur, 3),
        "target_cuts": 0, "need_cuts": 0, "cut_density_ok": None,
        "frame_delta_max": 0.0, "audio_delta_max_db": None,
        "thresholds": {"frame_delta": FRAME_DELTA_CHANGED, "hist_delta": HIST_DELTA_CHANGED,
                       "audio_delta_db": AUDIO_DELTA_CHANGED_DB,
                       "cut_jump_min": CUT_JUMP_MIN, "cut_jump_ratio": CUT_JUMP_RATIO},
        "error": info.get("error") or "",
    }
    if dur <= 0:
        result["verdict"] = f"无法判定：读不到时长（{info.get('error') or 'probe 失败'}）"
        return result

    frames = _gray_seq(video_path)
    fps = ANALYZE_FPS
    if len(frames) == 0:
        result["verdict"] = "无法判定：抽帧失败"
        return result
    pcm = _pcm_seq(video_path)
    cuts = _detect_cuts(frames, fps)
    result["cuts"] = cuts
    result["cut_count"] = len(cuts)

    total_windows = 0
    unchanged = 0
    prev_rms_db: Optional[float] = None
    k = 0
    # 只统计**完整**的 3 秒窗口：2 秒的尾窗不足以判节奏，硬算会给出一个假结论。
    while (k + 1) * win <= dur + 1e-6:
        w_start = round(k * win, 3)
        w_end = round(min(dur, (k + 1) * win), 3)
        i0 = int(round(w_start * fps))
        i1 = min(len(frames) - 1, int(round(w_end * fps)) - 1)
        if i0 >= len(frames) or i1 < i0:
            k += 1
            continue
        head = frames[i0: min(i0 + HEAD_TAIL_FRAMES, i1 + 1)].mean(axis=0)
        tail = frames[max(i0, i1 - HEAD_TAIL_FRAMES + 1): i1 + 1].mean(axis=0)
        frame_delta, hist_delta = _frame_metrics(head, tail)

        rms_db: Optional[float] = None
        audio_delta: Optional[float] = None
        if len(pcm):
            seg = pcm[int(w_start * AUDIO_SR): int(w_end * AUDIO_SR)]
            if seg.size:
                rms_db = _db(float((seg.astype("float64") ** 2).mean() ** 0.5))
        if rms_db is not None and prev_rms_db is not None:
            audio_delta = round(abs(rms_db - prev_rms_db), 2)
        if rms_db is not None:
            prev_rms_db = rms_db

        cuts_in = [c for c in cuts if w_start < c["time"] <= w_end + 1e-9]
        why: List[str] = []
        if frame_delta > FRAME_DELTA_CHANGED:
            why.append(f"画面差 {frame_delta:.3f}>{FRAME_DELTA_CHANGED:g}")
        if hist_delta > HIST_DELTA_CHANGED:
            why.append(f"直方图差 {hist_delta:.3f}>{HIST_DELTA_CHANGED:g}")
        if cuts_in:
            why.append("硬切@" + "/".join(f"{c['time']:g}s" for c in cuts_in))
        if audio_delta is not None and audio_delta > AUDIO_DELTA_CHANGED_DB:
            why.append(f"声音落差 {audio_delta:g}dB>{AUDIO_DELTA_CHANGED_DB:g}dB")
        changed = bool(why)
        total_windows += 1
        if not changed:
            unchanged += 1
            why.append("3 秒内画面/声音都没变化")
        result["windows"].append({
            "start": w_start, "end": w_end, "changed": changed,
            "frame_delta": round(frame_delta, 4),
            "hist_delta": round(hist_delta, 4),
            "audio_rms_db": rms_db,
            "audio_delta": audio_delta,
            "why": "；".join(why[:3]),
        })
        k += 1

    if total_windows:
        result["collapse_ratio"] = round(unchanged / total_windows, 4)
    result["tail_remainder"] = round(dur - total_windows * win, 3)
    result["frame_delta_max"] = max((w["frame_delta"] for w in result["windows"]), default=0.0)
    audio_vals = [w["audio_delta"] for w in result["windows"] if w["audio_delta"] is not None]
    result["audio_delta_max_db"] = max(audio_vals) if audio_vals else None

    bounds = [0.0] + [c["time"] for c in cuts] + [round(dur, 3)]
    result["longest_no_cut"] = round(max(b - a for a, b in zip(bounds, bounds[1:])), 3)

    # ── 判定 ──
    # 注意顺序：「3 秒窗口有没有变化」只是下限（慢推镜也能过），
    # 真正判"节奏塌陷"的是**切点密度**；两个都要报，否则会像只看前者那样把
    # "30 秒 1 个切点"的片子判成合格。
    cr = result["collapse_ratio"]
    fast = _resolve_tiers()[0].get("fast_drama", PACING_TIERS["fast_drama"])
    target_cuts = max(0, int(round(dur / fast)) - 1)
    need_cuts = max(1, int(round(target_cuts * CUT_DENSITY_TOLERANCE))) if target_cuts else 0
    density_ok = (result["cut_count"] >= need_cuts) if target_cuts else True
    result["target_cuts"] = target_cuts
    result["need_cuts"] = need_cuts
    result["cut_density_ok"] = bool(density_ok)
    if not total_windows:
        verdict = (f"无法判定：视频 {dur:g} 秒短于一个完整的 {win:g} 秒窗口"
                   f"（尾巴剩 {result['tail_remainder']:g} 秒），不足以按 {win:g} 秒窗口判节奏")
    elif cr >= COLLAPSE_RATIO_BAD:
        verdict = (f"节奏塌陷：{unchanged}/{total_windows} 个 3 秒窗口没有任何变化"
                   f"（塌陷率 {cr:.0%}）")
    elif cr >= COLLAPSE_RATIO_SLOW:
        verdict = f"偏慢：{unchanged}/{total_windows} 个 3 秒窗口没有变化（塌陷率 {cr:.0%}）"
    elif not density_ok:
        verdict = (f"变化达标但切点密度不足：整片 {dur:g} 秒只有 {result['cut_count']} 个硬切"
                   f"（fast_drama {fast:g}s 档目标 {target_cuts} 个 / 至少 {need_cuts} 个），"
                   f"最长连续 {result['longest_no_cut']:g} 秒无切点")
    else:
        verdict = (f"合格：{total_windows - unchanged}/{total_windows} 个 3 秒窗口有变化，"
                   f"{result['cut_count']} 个硬切（目标 {target_cuts} / 至少 {need_cuts}）")
    result["verdict"] = verdict
    result["legend"] = {
        "window": win,
        "frame_delta": "窗口首 0.5s 与尾 0.5s 灰度均值帧的 MAD（0~1）",
        "hist_delta": "同两帧 32 桶归一化灰度直方图的 L1 距离（0~2）",
        "audio_delta": "本窗口 RMS 与上一窗口 RMS 的绝对落差（dB）；无音轨 = None",
        "constants_basis": [
            f"frame_delta>{FRAME_DELTA_CHANGED}: 静止画面实测 3.4e-5，真实运动 ≥0.025",
            f"hist_delta>{HIST_DELTA_CHANGED}: 实测真实变化 0.09~0.62，静止≈0",
            f"audio_delta>{AUDIO_DELTA_CHANGED_DB}dB: 节目响度可辨差异 1~3dB，6dB≈音量翻倍",
            f"硬切: 逐帧差 > max({CUT_JUMP_MIN}, {CUT_JUMP_RATIO}×邻域中位) 且为局部极大",
            f"判定顺序: 先看塌陷率（3 秒窗口），再看切点密度（fast_drama "
            f"{fast:g}s 档，至少要到目标的 {CUT_DENSITY_TOLERANCE:.0%}）"
            f"—— 慢推长镜头能过前者，过不了后者",
            "局限性: 硬切检测是保守的（跳变 < 2.5 倍邻域运动的小反差切点会漏检），"
            "所以 longest_no_cut 可能比真实值偏大；漏检方向只会把片子判得更严，是安全方向",
        ],
    }
    return result


# ═══════════════════════════════════════════════════════════════════
# 十、selftest：用现场真实数据跑一遍
# ═══════════════════════════════════════════════════════════════════

PROJECT_PACING = "7b6ee913-5ea1-4b47-8895-856852717a48"   # 2 镜 × 15s（节奏塌陷现场）
PROJECT_NARRATIVE = "3ad3f169-fd41-4ed1-a034-a013eb066407"  # 农夫与蛇 45 秒（4 镜 × 5/11/11/11）
DEFAULT_DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", ""), "VideoForge", "data")
DEFAULT_SHOTS_JSON = os.path.join("D:\\tmp\\vfdata", "shots.json")
DEFAULT_OUT_DIR = os.path.join("D:\\tmp", "pacing_out")
DEFAULT_FILMS = [
    os.path.join("D:\\VideoForge-dsh", "dist", "成片", "雨夜便利店-12秒-480P-新配音.mp4"),
    os.path.join("D:\\VideoForge-dsh", "dist", "成片", "关键帧版成片.mp4"),
]


def _load_shots(project_id: str) -> List[Dict[str, Any]]:
    """优先读导出的 JSON（只读），没有再兜底走 core.db（也只读）。"""
    path = os.environ.get("VF_SHOTS_JSON") or DEFAULT_SHOTS_JSON
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                rows = json.load(fh)
            picked = [r for r in rows if str(r.get("project_id") or "") == project_id]
            if picked:
                return sorted(picked, key=lambda r: int(_as_float(r.get("order_index"), 0) or 0))
        except Exception as exc:
            logger.warning("读 %s 失败: %s", path, exc)
    try:  # 兜底：只读数据库
        from core.db import Database

        db_file = os.path.join(os.environ.get("VF_DATA_DIR") or DEFAULT_DATA_DIR, "videoforge.db")
        if os.path.isfile(db_file):
            return list(Database(db_file).list_shots(project_id) or [])
    except Exception as exc:
        logger.warning("core.db 兜底失败: %s", exc)
    return []


def _project_clips(project_id: str) -> List[str]:
    """项目素材目录里的 mp4，按修改时间取**最新两条**。

    ★ 为什么是"最新两条"而不是"全部"：7b6ee913 的 shots 目录下还留着 10-23 的
      旧素材（同项目多条候选），本次现场要修的是 10-24 16:02/16:05 出的两条
      15.104s 素材；用 mtime 取最新两条正好是它们（selftest 会把路径打出来）。
    """
    base = os.path.join(os.environ.get("VF_DATA_DIR") or DEFAULT_DATA_DIR,
                        "outputs", project_id, "shots")
    if not os.path.isdir(base):
        return []
    files = [os.path.join(base, f) for f in os.listdir(base) if f.lower().endswith(".mp4")]
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return files[:2]


def selftest() -> Dict[str, Any]:
    """用现场真实数据跑一遍：7b6ee913（塌陷判定+切点方案）、3ad3f169（对照）、
    两条 15.104s 素材真切片并逐段校验、两条现成成片的 3 秒窗口审计。

    返回 `{"ok","passed","failed","checks":[...],"summary","numbers"}`。
    """
    checks: List[Dict[str, Any]] = []
    numbers: Dict[str, Any] = {}

    def check(name: str, ok: bool, detail: str, data: Optional[Dict[str, Any]] = None) -> bool:
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "data": data or {}})
        return bool(ok)

    # ── 0. 档位表（软引用 scriptplan，读不到用内置；两路都必须是 2.5/3.5/5.0/8.0）──
    resolved_tiers, tier_source = _resolve_tiers()
    numbers["pacing_tiers"] = {"resolved": resolved_tiers, "source": tier_source,
                              "expected": dict(PACING_TIERS)}
    check("档位表 = fast_drama 2.5 / promo 3.5 / talking 5.0 / ambient 8.0",
          all(abs(float(resolved_tiers.get(k, -1)) - v) < 1e-6 for k, v in PACING_TIERS.items()),
          f"来源: {tier_source}；实际 {resolved_tiers}")

    # ── 1. 节奏塌陷项目 7b6ee913 ──
    shots = _load_shots(PROJECT_PACING)
    plan = audit_shot_plan(shots, pacing=DEFAULT_PACING) if shots else {}
    numbers["pacing_project"] = {
        "shots": len(shots),
        "total_seconds": plan.get("pacing_target", {}).get("total_seconds"),
        "cut_points_now": plan.get("cut_points_now"),
        "cut_points_after_fix": plan.get("cut_points"),
        "target_cuts": plan.get("target_cuts"),
        "mean_cut": plan.get("mean_cut"),
        "shot_sizes": [s["shot_size"] for s in plan.get("shot_sizes") or []],
        "score": plan.get("score"),
        "collapse": plan.get("collapse"),
        "collapse_ratio": plan.get("collapse_ratio"),
        "verdict": plan.get("verdict"),
        "window_changed": sum(1 for w in plan.get("windows") or [] if w["changed"]),
        "window_total": len(plan.get("windows") or []),
    }
    check("shots.7b6ee913 读到 2 个分镜 × 15s", len(shots) == 2,
          f"{len(shots)} 个分镜，合计 {numbers['pacing_project']['total_seconds']} 秒")
    check("audit_shot_plan.7b6ee913 判出节奏塌陷（ok=False）", plan.get("ok") is False,
          f"ok={plan.get('ok')} score={plan.get('score')} verdict={plan.get('verdict')}")
    check("audit_shot_plan.7b6ee913 切点方案 >= 10 个切点",
          int(plan.get("cut_points") or 0) >= 10,
          f"现方案 {plan.get('cut_points_now')} 个切点 → 修正后 {plan.get('cut_points')} 个切点"
          f"（目标 {plan.get('target_cuts')}）")
    check("audit_shot_plan.7b6ee913 指出景别单一",
          len({s["shot_size"] for s in plan.get("shot_sizes") or []}) == 1,
          f"景别 = {numbers['pacing_project']['shot_sizes']}")

    # ── 2. 对照项目 3ad3f169（4 镜，快节奏档下也该有问题，但不是塌陷级别） ──
    shots2 = _load_shots(PROJECT_NARRATIVE)
    plan2 = audit_shot_plan(shots2, pacing=DEFAULT_PACING) if shots2 else {}
    numbers["narrative_project"] = {
        "shots": len(shots2),
        "total_seconds": plan2.get("pacing_target", {}).get("total_seconds"),
        "cut_points_now": plan2.get("cut_points_now"),
        "cut_points_after_fix": plan2.get("cut_points"),
        "mean_cut": plan2.get("mean_cut"),
        "score": plan2.get("score"),
        "verdict": plan2.get("verdict"),
    }
    check("shots.3ad3f169 读到了（4 个分镜）", len(shots2) >= 4,
          f"{len(shots2)} 个分镜，合计 {numbers['narrative_project']['total_seconds']} 秒，"
          f"现 {plan2.get('cut_points_now')} 个切点 / 平均 {plan2.get('mean_cut')} 秒")

    # ── 3. 真切片：两条 15.104s 素材 ──
    clips = _project_clips(PROJECT_PACING)
    numbers["clips"] = [os.path.basename(c) for c in clips]
    infos = [probe_media(c) for c in clips]
    check("素材就位（2 条 15.104s / 864x496 / 24fps / 有音轨）",
          len(clips) == 2 and all(abs(i["duration"] - 15.104) < 0.05 for i in infos)
          and all(i["width"] == 864 and i["height"] == 496 and abs(i["fps"] - 24) < 0.01 for i in infos)
          and all(i["has_audio"] for i in infos),
          "；".join(f"{os.path.basename(c)} {i['duration']}s {i['width']}x{i['height']} "
                    f"{i['fps']:g}fps audio={i['has_audio']}" for c, i in zip(clips, infos)))

    cut_plan = build_cut_plan(clips, pacing=DEFAULT_PACING, max_cuts_per_clip=6) if clips else {}
    seg_secs = [c["duration"] for c in cut_plan.get("cuts") or []]
    numbers["cut_plan"] = {
        "segments": cut_plan.get("segments_count"),
        "cuts": cut_plan.get("cuts_count"),
        "total_seconds": cut_plan.get("total_seconds"),
        "segment_seconds": sorted(set(round(s, 3) for s in seg_secs)),
        "zooms": [c["zoom"] for c in cut_plan.get("cuts") or []],
        "notes": cut_plan.get("note"),
    }
    check("build_cut_plan 12 段 / 10 个切点，每段落在 2.5~3.5s",
          cut_plan.get("segments_count") == 12 and int(cut_plan.get("cuts_count") or 0) == 10
          and seg_secs and min(seg_secs) >= 2.5 and max(seg_secs) <= 3.5,
          f"{cut_plan.get('segments_count')} 段 × {sorted(set(round(s, 3) for s in seg_secs))} 秒，"
          f"切点 {cut_plan.get('cuts_count')} 个")
    zooms = [c["zoom"] for c in cut_plan.get("cuts") or []]
    check("build_cut_plan 每段 zoom 在 1.00~1.12 且不重复凑数",
          bool(zooms) and all(ZOOM_MIN - 1e-9 <= z <= ZOOM_MAX + 1e-9 for z in zooms)
          and len(set(zooms)) >= 4,
          f"zoom 序列 {zooms}")

    # 契约张力自检：默认 max_cuts_per_clip=3 时 15.104s 素材只能 4 段（3.776s/段），
    # 必须**在 note 里明说被上限截断**，不能把 3.776 秒冒充成 2.5 秒档。
    plan_cap3 = build_cut_plan(clips, pacing=DEFAULT_PACING, max_cuts_per_clip=3) if clips else {}
    seg3 = sorted({round(c["duration"], 3) for c in plan_cap3.get("cuts") or []})
    numbers["cut_plan_cap3"] = {"segments": plan_cap3.get("segments_count"),
                                "cuts": plan_cap3.get("cuts_count"),
                                "segment_seconds": seg3,
                                "note": plan_cap3.get("note")}
    check("build_cut_plan 默认 cap=3 时如实报告「被上限截断」",
          plan_cap3.get("segments_count") == 8 and "被上限截断" in str(plan_cap3.get("note"))
          and seg3 and abs(seg3[0] - 3.776) < 0.01,
          f"{plan_cap3.get('segments_count')} 段 × {seg3} 秒（不是 2.5s，note 已说明为什么）")

    # ── 3b. 非等分拆点：layer2 时间片当切点（任务卡 2026-10-24-021）──
    # 先跑纯逻辑（不碰 ffmpeg）：权重映射 / 合并最小相邻对 / 均分最大片 / 总长精确
    tl7 = [[0, 2.0], [2.0, 3.5], [3.5, 6.0], [6.0, 10.0], [10.0, 13.0],
           [13.0, 15.0], [15.0, 17.5]]                     # 7 片，跨 17.5s
    pc_merge = plan_cut_points(15.104, tl7, pacing="promo", max_cuts_per_clip=3)
    pc_uniform = plan_cut_points(15.104, None, pacing="promo", max_cuts_per_clip=3)
    pc_split = plan_cut_points(30.0, [[0, 15], [15, 30]], pacing="promo", max_cuts_per_clip=3)
    pc_short = plan_cut_points(2.0, [[0, 1], [1, 2]], pacing="promo")
    numbers["plan_cut_points_logic"] = {
        "merge": {"segments": pc_merge["segments"], "weights": pc_merge["weights"],
                  "merged_pairs": pc_merge["merged_pairs"],
                  "durations": [c["duration"] for c in pc_merge["cuts"]],
                  "total": pc_merge["total_seconds"]},
        "uniform": {"segments": pc_uniform["segments"],
                    "durations": [c["duration"] for c in pc_uniform["cuts"]]},
        "split": {"segments": pc_split["segments"],
                  "durations": [c["duration"] for c in pc_split["cuts"]],
                  "truncated_by_cap": pc_split["truncated_by_cap"],
                  "in_bounds": pc_split["in_bounds"], "note": pc_split["note"]},
        "short": {"segments": pc_short["segments"],
                  "durations": [c["duration"] for c in pc_short["cuts"]]},
    }
    check("plan_cut_points 7 片 cap=3 → 4 段（合并最小相邻对）且总长精确",
          pc_merge["segments"] == 4 and pc_merge["merged_pairs"] == 3
          and abs(pc_merge["total_seconds"] - 15.104) < 0.02,
          f"{pc_merge['segments']} 段，权重 {pc_merge['weights']} → "
          f"{[c['duration'] for c in pc_merge['cuts']]}s，合并 {pc_merge['merged_pairs']} 对")
    check("plan_cut_points timeline=None → 均分 4×3.776s（旧逻辑不变）",
          pc_uniform["segments"] == 4
          and all(abs(c["duration"] - 3.776) < 0.002 for c in pc_uniform["cuts"]),
          f"{pc_uniform['segments']} 段 × {[c['duration'] for c in pc_uniform['cuts']]}s")
    check("plan_cut_points 段太少 → 均分最大片（30s/2片 cap=3 → 4 段 7.5s，如实标被上限截断）",
          pc_split["segments"] == 4
          and all(abs(c["duration"] - 7.5) < 0.01 for c in pc_split["cuts"])
          and pc_split["truncated_by_cap"] and not pc_split["in_bounds"],
          f"{pc_split['segments']} 段 × {[c['duration'] for c in pc_split['cuts']]}s，"
          f"6s 上限被 cap=3 拦住（in_bounds={pc_split['in_bounds']}，note 已写明）")
    check("plan_cut_points 短素材不硬切碎（2.0s/2片 → 1 段 2.0s）",
          pc_short["segments"] == 1 and abs(pc_short["cuts"][0]["duration"] - 2.0) < 1e-6,
          f"{pc_short['segments']} 段 {[c['duration'] for c in pc_short['cuts']]}s")

    # 再用 7b6ee913 真实 layer2 时间片（镜0 3.5/4/4/3.5、镜1 5/5/5）真跑
    timelines: List[Any] = []
    for shot in shots[:len(clips)]:
        blocks = _parse_blocks(shot.get("layer2_timeline") or shot.get("timeline"))
        items = [{"start": float(_as_float(b.get("start"), 0.0) or 0.0),
                  "end": float(_as_float(b.get("end"), 0.0) or 0.0)} for b in blocks]
        timelines.append([i for i in items if i["end"] > i["start"] + 1e-6] or None)
    while len(timelines) < len(clips):
        timelines.append(None)
    plan_tl = build_cut_plan(clips, pacing="promo", timelines=timelines) if clips else {}
    tl_cuts = plan_tl.get("cuts") or []
    tl_by_clip = [[c for c in tl_cuts if c.get("clip_index") == i] for i in range(len(clips))]
    tl_pts = [[c["out"] for c in g[:-1]] for g in tl_by_clip]
    tl_durs = [[c["duration"] for c in g] for g in tl_by_clip]
    expect_pts = [[3.5, 7.5, 11.5], [5.0, 10.0]]
    numbers["cut_plan_timeline"] = {
        "timelines": [[round(i["end"] - i["start"], 3) for i in (t or [])] for t in timelines],
        "segments": plan_tl.get("segments_count"),
        "cuts": plan_tl.get("cuts_count"),
        "total_seconds": plan_tl.get("total_seconds"),
        "source_per_clip": plan_tl.get("source_per_clip"),
        "distinct_durations": plan_tl.get("distinct_durations"),
        "duration_values": plan_tl.get("duration_values"),
        "cut_points": tl_pts,
        "segment_durations": tl_durs,
        "note": plan_tl.get("note"),
    }
    pts_ok = (len(tl_pts) == len(expect_pts)
              and all(len(tl_pts[i]) == len(expect_pts[i])
                      and all(abs(a - b) < 0.15 for a, b in zip(tl_pts[i], expect_pts[i]))
                      for i in range(len(expect_pts))))
    check("build_cut_plan(timelines=...) 两镜 → 7 段 / 5 个切点（非等分）",
          plan_tl.get("segments_count") == 7 and plan_tl.get("cuts_count") == 5,
          f"{plan_tl.get('segments_count')} 段 / {plan_tl.get('cuts_count')} 个切点，"
          f"source_per_clip={plan_tl.get('source_per_clip')}")
    check("非等分切点 ≈ layer2 边界（3.5/7.5/11.5 + 5/10，比例映射到 15.104s）",
          pts_ok, f"实际切点 {tl_pts}")
    check("distinct_durations == 3（3.5/4/5 秒）",
          plan_tl.get("distinct_durations") == 3
          and plan_tl.get("duration_values") == [3.5, 4.0, 5.0],
          f"distinct={plan_tl.get('distinct_durations')} values={plan_tl.get('duration_values')}，"
          f"精确段长={sorted({round(c['duration'], 3) for c in tl_cuts})}")
    check("非等分总长精确 == 30.208s（误差 < 0.02）",
          abs(float(plan_tl.get("total_seconds") or 0)
              - sum(float(i["duration"]) for i in infos)) < 0.02,
          f"plan={plan_tl.get('total_seconds')} vs 素材合计="
          f"{round(sum(float(i['duration']) for i in infos), 3)}")
    check("非等分每段夹在 [1.2,6.0]s 且 zoom 1.00→1.12 / push-pull 交替",
          bool(tl_cuts)
          and all(MIN_SEGMENT_SECONDS - 1e-3 <= d <= MAX_SEGMENT_SECONDS + 1e-3
                  for d in sum(tl_durs, []))
          and all(ZOOM_MIN - 1e-9 <= c["zoom"] <= ZOOM_MAX + 1e-9 for c in tl_cuts)
          and all(c["direction"] == ("push" if k % 2 == 0 else "pull")
                  for k, c in enumerate(tl_cuts)),
          f"段长 {tl_durs}，zooms={[c['zoom'] for c in tl_cuts]}")
    tl_applied = (apply_cut_plan(clips, plan_tl, DEFAULT_OUT_DIR + "_timeline")
                  if tl_cuts else [])
    tl_errs = [abs(float(r["duration_error"])) for r in tl_applied
               if r.get("duration_error") is not None]
    numbers["applied_timeline"] = {
        "expected": len(tl_cuts),
        "written": sum(1 for r in tl_applied if r.get("path") and os.path.exists(r["path"])),
        "ok": sum(1 for r in tl_applied if r.get("ok")),
        "max_duration_error": round(max(tl_errs), 4) if tl_errs else None,
        "durations": [r.get("probe_duration") for r in tl_applied],
        "errors": [r.get("error") for r in tl_applied if not r.get("ok")],
    }
    check("apply_cut_plan(非等分 7 段) 全部落盘 + ffprobe 时长误差 < 0.1s",
          len(tl_applied) == 7 and numbers["applied_timeline"]["ok"] == 7 and tl_errs
          and max(tl_errs) < DURATION_TOLERANCE,
          f"落盘 {numbers['applied_timeline']['written']}/7，校验通过 "
          f"{numbers['applied_timeline']['ok']}/7，最大时长误差 "
          f"{numbers['applied_timeline']['max_duration_error']}s"
          + (f"，失败: {numbers['applied_timeline']['errors'][:2]}"
             if numbers["applied_timeline"]["errors"] else ""))

    applied = apply_cut_plan(clips, cut_plan, DEFAULT_OUT_DIR) if cut_plan.get("cuts") else []
    numbers["applied"] = {
        "expected": len(cut_plan.get("cuts") or []),
        "written": sum(1 for r in applied if r.get("path") and os.path.exists(r["path"])),
        "ok": sum(1 for r in applied if r.get("ok")),
        "max_duration_error": round(max((abs(r.get("duration_error") or 0) for r in applied), default=9),
                                   4),
        "durations": [r.get("probe_duration") for r in applied],
        "errors": [r.get("error") for r in applied if not r.get("ok")],
    }
    check("apply_cut_plan 12 段全部落盘且 ffprobe 校验通过",
          len(applied) == 12 and numbers["applied"]["ok"] == 12,
          f"落盘 {numbers['applied']['written']}/12，校验通过 {numbers['applied']['ok']}/12，"
          f"最大时长误差 {numbers['applied']['max_duration_error']}s"
          + (f"，失败: {numbers['applied']['errors'][:2]}" if numbers["applied"]["errors"] else ""))
    check("apply_cut_plan 时长误差全部 < 0.1s",
          numbers["applied"]["max_duration_error"] < DURATION_TOLERANCE,
          f"最大误差 {numbers['applied']['max_duration_error']}s（容差 {DURATION_TOLERANCE}s）")
    check("apply_cut_plan 保持原分辨率/帧率/音轨",
          bool(applied) and all(r.get("width") == 864 and r.get("height") == 496
                                and abs(float(r.get("fps") or 0) - 24) < 0.01
                                and r.get("has_audio") for r in applied),
          f"{applied[0].get('width') if applied else '?'}x{applied[0].get('height') if applied else '?'} "
          f"{applied[0].get('fps') if applied else '?'}fps audio="
          f"{all(r.get('has_audio') for r in applied) if applied else '?'}")

    # ── 4. 成片 3 秒窗口审计 ──
    films = [p for p in DEFAULT_FILMS if os.path.isfile(p)]
    numbers["films"] = []
    for film in films:
        rep = audit_film(film)
        numbers["films"].append({
            "video": os.path.basename(film),
            "duration": rep.get("duration"),
            "windows": len(rep.get("windows") or []),
            "changed": sum(1 for w in rep.get("windows") or [] if w["changed"]),
            "collapse_ratio": rep.get("collapse_ratio"),
            "cuts": rep.get("cut_count"),
            "longest_no_cut": rep.get("longest_no_cut"),
            "verdict": rep.get("verdict"),
        })
    check("audit_film 对现成成片给出每 3 秒窗口判定",
          bool(numbers["films"]) and all(f["windows"] >= 3 for f in numbers["films"]),
          "；".join(f"{f['video']} {f['duration']}s → {f['changed']}/{f['windows']} 个窗口有变化"
                    f"（塌陷率 {f['collapse_ratio']:.0%}），{f['cuts']} 个硬切，"
                    f"最长 {f['longest_no_cut']}s 无切点" for f in numbers["films"]))
    check("audit_film 无音轨时不报错（audio_delta=None）",
          _selftest_no_audio(),
          "用一条 ffmpeg 生成的纯视频（-an）跑 audit_film，audio_delta 全为 None 且 verdict 正常")

    passed = sum(1 for c in checks if c["ok"])
    failed = len(checks) - passed
    for c in checks:
        logger.info("selftest %s %s — %s", "PASS" if c["ok"] else "FAIL", c["name"], c["detail"])
    return {
        "ok": failed == 0,
        "passed": passed,
        "failed": failed,
        "checks": checks,
        "numbers": numbers,
        "summary": (f"{'PASS' if failed == 0 else 'FAIL'} {passed}/{len(checks)} —— "
                    + "；".join(f"{'✓' if c['ok'] else '✗'}{c['name']}" for c in checks)),
    }


def _selftest_no_audio() -> bool:
    """生成一条无音轨的小视频，确认 audit_film 不炸、audio_delta 全是 None。"""
    try:
        tmp = os.path.join(tempfile.gettempdir(), "_gp_selftest_noaudio.mp4")
        rc, _txt = _run([_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                         "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=7",
                         "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                         "-pix_fmt", "yuv420p", tmp], timeout=120)
        if rc != 0:
            return False
        rep = audit_film(tmp)
        ok = (rep.get("has_audio") is False and rep.get("windows")
              and all(w.get("audio_delta") is None for w in rep["windows"]))
        try:
            os.remove(tmp)
        except OSError:
            pass
        return bool(ok)
    except Exception as exc:
        logger.warning("无音轨自检失败: %s", exc)
        return False


if __name__ == "__main__":  # 手工跑：py -3.14 -X utf8 -m core.gate_pacing
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    out = selftest()
    print(json.dumps(out, ensure_ascii=False, indent=1))
