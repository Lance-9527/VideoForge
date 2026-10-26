"""白膜 / 480p 成片 / 交付物 的客观一致性验收器（v1）。

为什么需要它（2026-10-25）：
  旧链路只有 SSIM 一条验收线，机制上分不清「机位类型错了」「主体悬浮」「推镜变成环绕」
  这三种硬伤 —— 实测同一条 480p 成片与错误白膜（悬浮 + push_in）的 SSIM 反而比与正确
  白膜的 SSIM 更高。本模块把「契约 → 白膜 → 成片 → 交付」压成三组可复算数字：

    A. evaluate_whitebox(spec_shot, contract)
       契约 ↔ 白膜（几何级，完全不依赖像素）：弧线半径漂移 / 焦距恒定 / 弧角 /
       机位高度恒定 / 看向点恒定 / 主体接触（不悬浮）/ 时长与关键帧末帧一致 /
       几何运镜分类 vs 契约 movement。

    B. evaluate_generated(whitebox_mp4, generated_mp4, contract)
       白膜 ↔ 成片（像素 / 运动级）：ffmpeg SSIM、主体轮廓 IoU、主体占画高比例、
       变焦启发式、全局平移方向余弦、弧线相似度；另加一项结构相似度 edge_ncc
       （Sobel 幅值图的归一化互相关）——因为白膜是灰模、成片是写实渲染，SSIM 受
       亮度/对比度差压制，edge_ncc 只比结构，判别力明显更强（见 tests 里的实测数字）。

    C. evaluate_delivery(mp4, contract)
       成片内容合规（低成本硬检查）：时长 / 帧数 / 宽高比 / 静音 / 底部字幕带启发式。

统一返回：
    {"ok": bool, "metrics": {...}, "failures": [{"name","want","got","note"}], "limitations": [...]}

工程约束：
  · 纯 numpy + ffmpeg/ffprobe 子进程；不联网、不调用付费 API、不写数据库。
  · 全程无随机数；同输入必然同输出（浮点在输出前统一 round 到固定小数）。
  · 只读契约（core/shot_contract.py），不修改任何业务代码。
  · 阈值默认取契约里的 THRESHOLDS，可用 thresholds= 参数覆盖（用于分级放行）。

坐标约定：
  本工程 spec 的相机关键帧是 [frame, x, y, z, lookx, looky, lookz, lens]，
  **z 是高度、水平面是 (x, y)**（与 set.props 的 z/h、anchor=base 一致；
  与 2026-10-25 之后的 shot_contract 一致）。up_axis 可显式覆盖。

实测基线（2026-10-25 本机素材；tests/test_shot_consistency.py 已冻结为断言）：
  正确白膜 vs 第 2 轮 480p：SSIM 0.833、轮廓 IoU 0.581、edge_ncc 0.502、
      主体占画高 55.8% vs 55.6%（Δ0.3%）；但成片全局平移相干度 0.09 vs 白膜 0.99
      → 判「机位移动没被复现」。
  旧（悬浮）白膜 vs 同一条成片：SSIM 0.822、IoU 0.238、edge_ncc 0.081、
      主体占画高 47.2% vs 55.6%（Δ8.3%）+ 白膜有推拉而片中没有。
  交叉验证：第 1 轮 480p 由旧白膜生成 → 旧白膜↔第1轮 IoU 0.549 / edge_ncc 0.555，
      正确白膜↔第1轮 IoU 0.289 / edge_ncc 0.046。
  结论：SSIM 在这类「灰模 ↔ 写实渲染」跨风格对比中几乎分不开（0.833 vs 0.822），
  真正有判别力的是 edge_ncc（6.2×）、轮廓 IoU（2.4×）与主体占画高差（30×）。
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

__all__ = [
    "ConsistencyError",
    "probe_media",
    "evaluate_whitebox",
    "evaluate_generated",
    "evaluate_delivery",
    "DEFAULT_THRESHOLDS",
    "EXTRA_THRESHOLDS",
]

# --------------------------------------------------------------------------- #
# 阈值：以契约为唯一真源；本模块只补充契约里没有的辅助阈值
# --------------------------------------------------------------------------- #
try:  # 包内导入（sys.path 指向 backend）
    from .shot_contract import THRESHOLDS as _CONTRACT_THRESHOLDS  # type: ignore
except ImportError:  # 直接以顶层模块导入
    try:
        from shot_contract import THRESHOLDS as _CONTRACT_THRESHOLDS  # type: ignore
    except ImportError:  # 独立运行时保底（值与 shot_contract 保持一致）
        _CONTRACT_THRESHOLDS = {
            "radius_drift_ratio": 0.02,
            "lens_drift_mm": 0.5,
            "subject_ratio_drift": 0.05,
            "ssim_min": 0.80,
            "silhouette_iou_min": 0.65,
            "motion_cosine_min": 0.75,
            "arc_angle_tolerance_deg": 12.0,
        }

DEFAULT_THRESHOLDS: Dict[str, float] = dict(_CONTRACT_THRESHOLDS)

#: 契约里没有、由验收器补充的辅助阈值（可用 thresholds= 覆盖）
EXTRA_THRESHOLDS: Dict[str, float] = {
    # 结构相似度（Sobel 幅值图 NCC）：灰模 ↔ 写实渲染的跨域结构指标
    "edge_ncc_min": 0.30,
    # 运动：成片相干位移 / 白膜相干位移 的下限（1.0 = 等幅）
    "motion_energy_min_ratio": 0.35,
    # 相干度 = |Σ位移| / Σ|位移|；低于此值视为「没有单一方向机位移动」
    "motion_coherence_min": 0.40,
    # 白膜相干位移下限（px，320x180 分析画布），太小则不拿运动判罚
    "motion_min_cumulative_px": 8.0,
    # 主体占画高比例（白膜 vs 成片）允许差（默认 = subject_ratio_drift）
    "subject_height_delta_max": 0.05,
    # 同一段视频内主体高度单调变化的判据（span/mean 与秩相关）
    "zoom_span_ratio": 0.05,
    "zoom_trend_min": 0.80,
    #: 交付：宽高比容差（相对），帧数容差（帧）
    "delivery_aspect_tolerance": 0.02,
    "delivery_frame_tolerance": 1.0,
    # 底部 12% 字幕/水印启发式（无正样本可校准，故意设得很保守，只告警）：
    "watermark_edge_ratio_warn": 3.0,   # 底部边缘密度 / 全帧边缘密度
    "watermark_row_peak_warn": 0.30,    # 底部 3 行滑窗最大边缘密度（文字笔画边缘密集）
    "watermark_bright_ratio_warn": 0.12,  # 底部近白像素占比（字幕常见白字）
}

FPS_DEFAULT = 24.0
#: 主体分割 / 轮廓 IoU 的分析画布（拉伸到固定尺寸，保证两段视频可比）
SEG_CANVAS: Tuple[int, int] = (320, 180)
#: SSIM 的对齐画布（ffmpeg 滤镜内完成缩放 / 变帧率 / 裁前 N 帧）
SSIM_CANVAS: Tuple[int, int] = (480, 270)
#: 抽帧数量（轮廓 / 面积统计）
SEG_SAMPLE_FRAMES = 8
#: 运动方向比较的最小单帧位移（px）；低于它视为静止帧，不参与余弦平均
MOTION_STEP_MIN_PX = 0.25
#: 主体分割：前景阈值 = median + k * (p99 - median)
SEG_K = 0.40
#: 主体分割：连通域填充率下限（排除细长的光斑/灯带）
SEG_FILL_MIN = 0.60

_FFMPEG: Optional[str] = None
_FFPROBE: Optional[str] = None


class ConsistencyError(RuntimeError):
    """媒体不可读 / ffmpeg 缺失等工作性错误。"""


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _ffmpeg() -> str:
    global _FFMPEG
    if _FFMPEG is None:
        _FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
    return _FFMPEG


def _ffprobe() -> str:
    global _FFPROBE
    if _FFPROBE is None:
        _FFPROBE = shutil.which("ffprobe") or "ffprobe"
    return _FFPROBE


def _run(cmd: Sequence[str], *, timeout: float = 120.0) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(list(cmd), capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - 环境缺 ffmpeg
        raise ConsistencyError(f"无法执行 {cmd[0]}: {exc}") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or b"")[-600:].decode("utf-8", "replace")
        raise ConsistencyError(f"{cmd[0]} 失败（{proc.returncode}）: {tail}")
    return proc


def _r(value: Any, nd: int = 4) -> Any:
    """把 numpy / float 收敛成 JSON 友好的定点数（保证输出可复算、可比对）。"""
    if value is None:
        return None
    if isinstance(value, (bool, int)):
        return value
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        if math.isnan(float(value)) or math.isinf(float(value)):
            return None
        return round(float(value), nd)
    if isinstance(value, (list, tuple)):
        return [_r(v, nd) for v in value]
    return value


def _fail(name: str, want: Any, got: Any, note: str = "") -> Dict[str, Any]:
    return {"name": str(name), "want": want, "got": got, "note": note}


def _result(metrics: Dict[str, Any], failures: List[Dict[str, Any]],
            limitations: List[str]) -> Dict[str, Any]:
    return {
        "ok": not failures,
        "metrics": {k: _r(v, 6) for k, v in metrics.items()},
        "failures": failures,
        "limitations": [str(x) for x in limitations if x],
    }


def _merge_thresholds(contract: Optional[Dict[str, Any]],
                      overrides: Optional[Dict[str, float]]) -> Dict[str, float]:
    thr: Dict[str, float] = dict(DEFAULT_THRESHOLDS)
    if isinstance(contract, dict):
        extra = contract.get("thresholds")
        if isinstance(extra, dict):
            thr.update({k: v for k, v in extra.items() if isinstance(v, (int, float))})
    thr.update(EXTRA_THRESHOLDS)
    if overrides:
        thr.update({k: v for k, v in overrides.items() if isinstance(v, (int, float))})
    return thr


def _need_file(path: str) -> str:
    if not path or not isinstance(path, str):
        raise ConsistencyError(f"缺少媒体路径: {path!r}")
    try:
        with open(path, "rb") as fh:
            fh.read(1)
    except OSError as exc:
        raise ConsistencyError(f"媒体不可读: {path} ({exc})") from exc
    return path


# --------------------------------------------------------------------------- #
# 媒体探测
# --------------------------------------------------------------------------- #
def probe_media(path: str) -> Dict[str, Any]:
    """ffprobe 一次拿到宽高 / 帧率 / 帧数 / 时长 / 音轨。纯只读。"""
    _need_file(path)
    cmd = [_ffprobe(), "-v", "error", "-print_format", "json",
           "-show_streams", "-show_format", path]
    data = json.loads(_run(cmd).stdout or b"{}")
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        raise ConsistencyError(f"没有视频流: {path}")
    fps = 0.0
    for key in ("r_frame_rate", "avg_frame_rate"):
        raw = str(video.get(key) or "")
        if "/" in raw:
            num, den = raw.split("/", 1)
            try:
                if float(den) > 0:
                    fps = float(num) / float(den)
                    break
            except ValueError:
                pass
        elif raw:
            try:
                fps = float(raw)
                break
            except ValueError:
                pass
    frames = None
    for key in ("nb_frames", "nb_read_frames"):
        try:
            frames = int(str(video.get(key)))
            break
        except (TypeError, ValueError):
            continue
    duration = None
    for raw in (data.get("format", {}).get("duration"), video.get("duration")):
        try:
            duration = float(raw)
            break
        except (TypeError, ValueError):
            continue
    if duration is None and frames and fps > 0:
        duration = frames / fps
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    return {
        "path": path,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": round(fps, 6),
        "frames": frames,
        "duration": round(duration, 6) if duration else None,
        "video_codec": video.get("codec_name"),
        "pix_fmt": video.get("pix_fmt"),
        "has_audio": bool(audio),
        "audio_streams": len(audio),
        "audio_codec": audio[0].get("codec_name") if audio else None,
        "audio_channels": audio[0].get("channels") if audio else None,
        "audio_sample_rate": audio[0].get("sample_rate") if audio else None,
        "size_bytes": int(data.get("format", {}).get("size") or 0),
        "aspect_ratio": round(int(video.get("width") or 0) / float(video.get("height") or 1), 6),
    }


def _decode(path: str, width: int, height: int, frames: Optional[int],
            *, gray: bool = False) -> np.ndarray:
    """把视频解码成 (N,H,W[,3]) uint8。scale 固定 → 结果与源分辨率无关、可复算。"""
    vf = f"scale={width}:{height}"
    pix = "gray" if gray else "rgb24"
    cmd = [_ffmpeg(), "-v", "error", "-i", path, "-vf", vf,
           "-f", "rawvideo", "-pix_fmt", pix]
    if frames:
        cmd += ["-frames:v", str(int(frames))]
    cmd += ["-"]
    raw = _run(cmd).stdout
    ch = 1 if gray else 3
    per = width * height * ch
    count = len(raw) // per
    if count <= 0:
        raise ConsistencyError(f"解码失败/空帧: {path}")
    arr = np.frombuffer(raw[: count * per], dtype=np.uint8)
    if gray:
        return arr.reshape(count, height, width).astype(np.float32)
    return arr.reshape(count, height, width, 3)


def _sample_indices(total: int, count: int) -> List[int]:
    if total <= 0 or count <= 0:
        return []
    if count == 1:
        return [0]
    step = (total - 1) / float(count - 1)
    return sorted({int(round(i * step)) for i in range(count)})


def _sobel(gray: np.ndarray) -> np.ndarray:
    gx = np.zeros_like(gray)
    gy = np.zeros_like(gray)
    gx[:, 1:-1] = (gray[:, 2:] - gray[:, :-2]) * 0.5
    gy[1:-1, :] = (gray[2:, :] - gray[:-2, :]) * 0.5
    return np.hypot(gx, gy)


def _norm0(x: np.ndarray) -> np.ndarray:
    x = x - float(x.mean())
    std = float(x.std())
    return x / std if std > 1e-9 else np.zeros_like(x)


# --------------------------------------------------------------------------- #
# 主体分割（自适应阈值 + 连通域）
# --------------------------------------------------------------------------- #
def _components(mask: np.ndarray, *, min_area: int = 20) -> List[Dict[str, Any]]:
    """4 邻域连通域（纯 Python BFS，确定性；画布只有 320x180）。"""
    h, w = mask.shape
    seen = np.zeros((h, w), dtype=bool)
    out: List[Dict[str, Any]] = []
    ys, xs = np.nonzero(mask)
    for y0, x0 in zip(ys.tolist(), xs.tolist()):
        if seen[y0, x0]:
            continue
        stack = [(y0, x0)]
        seen[y0, x0] = True
        pts_y: List[int] = []
        pts_x: List[int] = []
        while stack:
            y, x = stack.pop()
            pts_y.append(y)
            pts_x.append(x)
            for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                ny, nx = y + dy, x + dx
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        area = len(pts_y)
        if area < min_area:
            continue
        ymin, ymax = min(pts_y), max(pts_y)
        xmin, xmax = min(pts_x), max(pts_x)
        bw, bh = xmax - xmin + 1, ymax - ymin + 1
        out.append({
            "area": area,
            "bbox": (xmin, ymin, xmax, ymax),
            "bbox_w": bw,
            "bbox_h": bh,
            "fill": area / float(bw * bh),
            "cx": sum(pts_x) / float(area),
            "cy": sum(pts_y) / float(area),
            "border": bool(ymin == 0 or ymax == h - 1 or xmin == 0 or xmax == w - 1),
        })
    out.sort(key=lambda c: (-c["area"], c["bbox"]))
    return out


def _segment_subject(frame_rgb: np.ndarray) -> Optional[Dict[str, Any]]:
    """从单帧里估计主体 bbox。

    做法（确定性）：
      1. 灰度化；背景亮度 = 帧内中位值，主体亮度上界 = p99；
      2. 前景 = gray > median + k*(p99-median)（自适应，不写死绝对亮度）；
      3. 去掉贴边连通域（墙/地/顶面一定贴边），并按填充率筛掉细长光斑；
      4. 在「面积前三」的候选里取离画面中心最近的一个（主体始终在构图中心）。

    注意：灰模白膜是平光 low-contrast 渲染，亮度分割拿到的是主体的**受光面**，
    不是完整轮廓；对「位置 / 高度」足够稳，对「面积」会随视角变化 —— 见 limitations。
    """
    gray = frame_rgb.astype(np.float32).mean(axis=-1) if frame_rgb.ndim == 3 else frame_rgb.astype(np.float32)
    h, w = gray.shape
    med = float(np.median(gray))
    p99 = float(np.percentile(gray, 99))
    thr = med + SEG_K * (p99 - med)
    comps = [c for c in _components(gray > thr, min_area=max(20, int(0.001 * h * w)))
             if not c["border"] and c["area"] >= 0.002 * h * w]
    if not comps:
        return None
    solid = [c for c in comps if c["fill"] >= SEG_FILL_MIN] or comps
    pool = sorted(solid, key=lambda c: -c["area"])[:3]
    pool.sort(key=lambda c: ((c["cx"] - w / 2.0) / w) ** 2 + ((c["cy"] - h / 2.0) / h) ** 2)
    pick = dict(pool[0])
    pick["thr"] = thr
    pick["median"] = med
    pick["p99"] = p99
    pick["area_ratio"] = pick["area"] / float(h * w)
    pick["height_ratio"] = pick["bbox_h"] / float(h)
    pick["width_ratio"] = pick["bbox_w"] / float(w)
    return pick


def _bbox_iou(a: Sequence[float], b: Sequence[float]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    iw = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    ih = max(0.0, min(ay1, by1) - max(ay0, by0))
    inter = iw * ih
    union = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter
    return float(inter / union) if union > 0 else 0.0


# --------------------------------------------------------------------------- #
# 全局平移估计（相位相关 + 抛物线亚像素）
# --------------------------------------------------------------------------- #
def _phase_shift(g0: np.ndarray, g1: np.ndarray) -> Tuple[float, float, float]:
    """估计把 g1 对齐到 g0 需要的平移（亚像素）。

    返回 (dx, dy, peak)：dx>0 表示 g1 相对 g0 需要向右移才能对齐，
    也就是画面内容向左移动了 dx。调用方用 -dx 表示「内容视位移」。
    """
    h, w = g0.shape
    wy = np.hanning(h)[:, None]
    wx = np.hanning(w)[None, :]
    a = _norm0(g0) * wy * wx
    b = _norm0(g1) * wy * wx
    fa = np.fft.rfft2(a)
    fb = np.fft.rfft2(b)
    cross = fa * np.conj(fb)
    cross /= (np.abs(cross) + 1e-9)
    corr = np.fft.irfft2(cross, s=(h, w))
    idx = int(np.argmax(corr))
    py, px = divmod(idx, w)

    def _sub(vm: float, v0: float, vp: float) -> float:
        denom = vm - 2.0 * v0 + vp
        return 0.5 * (vm - vp) / denom if abs(denom) > 1e-12 else 0.0

    sy = _sub(float(corr[(py - 1) % h, px]), float(corr[py, px]), float(corr[(py + 1) % h, px]))
    sx = _sub(float(corr[py, (px - 1) % w]), float(corr[py, px]), float(corr[py, (px + 1) % w]))
    dy = py + sy
    dx = px + sx
    if dy > h / 2.0:
        dy -= h
    if dx > w / 2.0:
        dx -= w
    return float(dx), float(dy), float(corr[py, px])


def _motion_track(gray_frames: np.ndarray) -> Dict[str, Any]:
    """逐帧全局平移 → 内容视位移序列（并给出相干度 / 累积位移）。"""
    n = int(gray_frames.shape[0])
    disp = np.zeros((max(0, n - 1), 2), dtype=np.float64)
    peaks = np.zeros(max(0, n - 1), dtype=np.float64)
    for i in range(1, n):
        dx, dy, peak = _phase_shift(gray_frames[i - 1], gray_frames[i])
        # 内容视位移 = -对齐位移
        disp[i - 1, 0] = -dx
        disp[i - 1, 1] = -dy
        peaks[i - 1] = peak
    mag = np.linalg.norm(disp, axis=1) if len(disp) else np.zeros(0)
    total = float(mag.sum())
    cumulative = disp.sum(axis=0) if len(disp) else np.zeros(2)
    cum_mag = float(np.linalg.norm(cumulative))
    coherence = cum_mag / total if total > 1e-9 else 0.0
    path = np.cumsum(disp, axis=0) if len(disp) else np.zeros((0, 2))
    return {
        "disp": disp,
        "peaks": peaks,
        "mag": mag,
        "cumulative": cumulative,
        "cumulative_px": cum_mag,
        "total_path_px": total,
        "coherence": coherence,
        "mean_step_px": float(mag.mean()) if len(mag) else 0.0,
        "max_step_px": float(mag.max()) if len(mag) else 0.0,
        "path": path,
        "peak_mean": float(peaks.mean()) if len(peaks) else 0.0,
    }


def _regional_cumulative(gray_frames: np.ndarray, *, grid: int = 3,
                         min_peak: float = 0.05) -> np.ndarray:
    """把画面切成 grid×grid 块，逐块做相位相关，取每帧中位位移再积分。

    用途：当「整帧相位相关」被某一强结构（墙角 / 灯带）带跑时，分块中位数会给出
    另一条独立估计；两者一致才认为运动测量可信（见 _motion_confidence）。
    """
    n = int(gray_frames.shape[0])
    total = np.zeros(2, dtype=np.float64)
    if n <= 1:
        return total
    h, w = gray_frames.shape[1], gray_frames.shape[2]
    rh, rw = max(8, h // grid), max(8, w // grid)
    boxes = [(gy * rh, min(h, (gy + 1) * rh), gx * rw, min(w, (gx + 1) * rw))
             for gy in range(grid) for gx in range(grid)]
    for i in range(1, n):
        values = []
        for (y0, y1, x0, x1) in boxes:
            if y1 - y0 < 8 or x1 - x0 < 8:
                continue
            dx, dy, peak = _phase_shift(gray_frames[i - 1][y0:y1, x0:x1],
                                        gray_frames[i][y0:y1, x0:x1])
            if peak >= min_peak:
                values.append((-dx, -dy))
        if values:
            total += np.median(np.asarray(values, dtype=np.float64), axis=0)
    return total


def _motion_confidence(global_track: Dict[str, Any],
                       regional_vector: np.ndarray) -> Dict[str, Any]:
    """判断「整帧相位相关」这次估计能不能拿来判罚。

    判据（全部确定性数值）：
      · 整帧估计自身相干（|Σd|/Σ|d| ≥ 0.5）—— 说明确有单一方向位移；
      · 分块中位估计与整帧估计方向一致（余弦 ≥ 0.8）；
      · 两者幅度同量级（0.5 ≤ 分块/整帧 ≤ 2.5）。
    这些条件在低纹理 / 重复结构的画面上经常不成立 —— 实测在 864x496 写实成片上
    「整帧累积 +66px」与「分块中位 ≈0px」会互相打脸，故不可直接当运镜证据。
    """
    cum_vec = np.asarray(global_track["cumulative"], dtype=np.float64)
    cum = float(np.linalg.norm(cum_vec))
    reg = float(np.linalg.norm(regional_vector))
    cos = _cosine(cum_vec, regional_vector)
    ratio = (reg / cum) if cum > 1e-9 else None
    # 分块幅度会比整帧小（旋转/视差会让不同区域的位移互相抵消），所以只要求分块估计
    # 不退化、且方向与整帧一致；真正的硬条件还是「整帧估计自身相干」。
    confident = bool(
        global_track["coherence"] >= 0.50
        and cum >= 4.0
        and reg >= 1.0
        and (cos is None or cos >= 0.80)
    )
    return {
        "regional_cumulative_px": reg,
        "global_regional_cosine": cos,
        "regional_ratio": ratio,
        "confident": confident,
    }




def _line_residual(points: np.ndarray) -> float:
    if len(points) < 2:
        return 0.0
    centered = points - points.mean(axis=0)
    try:
        _, _, vt = np.linalg.svd(centered, full_matrices=False)
    except np.linalg.LinAlgError:  # pragma: no cover
        return 0.0
    direction = vt[0]
    proj = np.outer(centered @ direction, direction)
    resid = centered - proj
    return float(np.sqrt((resid * resid).sum(axis=1).mean()))


def _circle_residual(points: np.ndarray) -> float:
    """Kasa 代数圆拟合的代数残差（px）。"""
    if len(points) < 4:
        return 0.0
    x = points[:, 0]
    y = points[:, 1]
    a = np.c_[2.0 * x, 2.0 * y, np.ones(len(x))]
    b = x * x + y * y
    try:
        sol, *_ = np.linalg.lstsq(a, b, rcond=None)
    except np.linalg.LinAlgError:  # pragma: no cover
        return 0.0
    cx, cy = float(sol[0]), float(sol[1])
    r = math.sqrt(max(1e-12, float(sol[2]) + cx * cx + cy * cy))
    d = np.hypot(x - cx, y - cy) - r
    return float(np.sqrt((d * d).mean()))


def _cosine(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    na = math.hypot(float(a[0]), float(a[1]))
    nb = math.hypot(float(b[0]), float(b[1]))
    if na < 1e-9 or nb < 1e-9:
        return None
    return float((float(a[0]) * float(b[0]) + float(a[1]) * float(b[1])) / (na * nb))


# --------------------------------------------------------------------------- #
# SSIM（ffmpeg 滤镜，按契约要求解析输出）
# --------------------------------------------------------------------------- #
def _ssim_ffmpeg(path_a: str, path_b: str, frames: int, *, fps: float) -> Optional[float]:
    w, h = SSIM_CANVAS
    chain_a = f"[0:v]scale={w}:{h},setsar=1,fps={fps:g},trim=end_frame={int(frames)},setpts=PTS-STARTPTS[a]"
    chain_b = f"[1:v]scale={w}:{h},setsar=1,fps={fps:g},trim=end_frame={int(frames)},setpts=PTS-STARTPTS[b]"
    cmd = [_ffmpeg(), "-v", "info", "-i", path_a, "-i", path_b,
           "-filter_complex", f"{chain_a};{chain_b};[a][b]ssim", "-an", "-f", "null", "-"]
    proc = _run(cmd)
    text = (proc.stderr or b"").decode("utf-8", "replace")
    values = [m.group(1) for m in re.finditer(r"All:([0-9.]+)", text)]
    return float(values[-1]) if values else None


def _edge_ncc(frames_a: np.ndarray, frames_b: np.ndarray) -> Tuple[float, float]:
    """Sobel 幅值图的逐帧归一化互相关（结构相似度，不受整体亮度/对比度影响）。"""
    n = min(len(frames_a), len(frames_b))
    if n <= 0:
        return 0.0, 0.0
    values = []
    for i in range(n):
        ga = _sobel(frames_a[i].astype(np.float32).mean(axis=-1))
        gb = _sobel(frames_b[i].astype(np.float32).mean(axis=-1))
        values.append(float((_norm0(ga) * _norm0(gb)).mean()))
    arr = np.asarray(values, dtype=np.float64)
    return float(arr.mean()), float(arr.std())


def _trend(values: Sequence[float]) -> float:
    """秩相关（Spearman，无 scipy）：时间序 vs 数值序，衡量单调性。"""
    n = len(values)
    if n < 3:
        return 0.0
    order = np.argsort(np.asarray(values, dtype=np.float64), kind="stable")
    ranks = np.empty(n, dtype=np.float64)
    ranks[order] = np.arange(n, dtype=np.float64)
    t = np.arange(n, dtype=np.float64)
    t = t - t.mean()
    r = ranks - ranks.mean()
    denom = math.sqrt(float((t * t).sum()) * float((r * r).sum()))
    return float((t * r).sum() / denom) if denom > 1e-12 else 0.0


# --------------------------------------------------------------------------- #
# A. 契约 ↔ 白膜（几何级）
# --------------------------------------------------------------------------- #
def _props_of(spec_shot: Dict[str, Any]) -> List[Dict[str, Any]]:
    props = ((spec_shot or {}).get("set") or {}).get("props") or []
    return [p for p in props if isinstance(p, dict)]


def _detect_up_axis(spec_shot: Dict[str, Any]) -> int:
    """判断哪个分量是「高度」。

    优先用 props 的物理语义：z 是高度（z=0.75 的箱子坐在 h=0.75 的桌上）。
    退路：看关键帧里哪个分量恒定（水平环绕的机位高度恒定）。
    """
    props = _props_of(spec_shot)
    if any(abs(float(p.get("z") or 0.0)) > 1e-9 for p in props):
        return 2
    if any(abs(float(p.get("y") or 0.0)) > 1e-9 for p in props):
        return 1
    cam = (spec_shot or {}).get("camera") or {}
    moves = [m for m in (cam.get("moves") or []) if isinstance(m, list) and len(m) == 8]
    if len(moves) >= 2:
        span1 = max(m[2] for m in moves) - min(m[2] for m in moves)
        span2 = max(m[3] for m in moves) - min(m[3] for m in moves)
        return 1 if span1 < span2 else 2
    return 2  # 本工程缺省 z-up


def _geom_axes(up_axis: int) -> Tuple[int, int]:
    """返回 (水平轴1, 水平轴2) 在 moves 里的下标（0-based 已含 frame 偏移）。"""
    if up_axis == 2:
        return 1, 2  # (x, y) 水平；z 高度
    return 1, 3      # (x, z) 水平；y 高度


def evaluate_whitebox(spec_shot: Dict[str, Any],
                      contract: Optional[Dict[str, Any]] = None,
                      *,
                      thresholds: Optional[Dict[str, float]] = None,
                      up_axis: Optional[int] = None) -> Dict[str, Any]:
    """A. 契约 ↔ 白膜：只看 spec 几何，不依赖任何像素。

    参数
      spec_shot : 单镜 spec（含 camera.moves 关键帧、set.props）
      contract  : build_contract(...) 的产物（可缺省；缺省时只用内建阈值）
    """
    limitations: List[str] = []
    failures: List[Dict[str, Any]] = []
    metrics: Dict[str, Any] = {}
    contract = contract or {}
    thr = _merge_thresholds(contract, thresholds)
    spec_shot = spec_shot or {}
    cam = spec_shot.get("camera") or {}
    raw_moves = cam.get("moves") or []
    moves = [list(m) for m in raw_moves if isinstance(m, list) and len(m) == 8]
    if len(moves) < 2:
        failures.append(_fail("camera_moves", "≥2 个 8 列关键帧 [frame,x,y,z,lx,ly,lz,lens]",
                              f"{len(moves)} 个", "白膜没有可用机位关键帧，几何验收无法进行"))
        return _result(metrics, failures, limitations)

    up = int(up_axis) if up_axis in (1, 2) else _detect_up_axis(spec_shot)
    h1, h2 = _geom_axes(up)
    look = [float(v) for v in (cam.get("look_at") or [0.0, 0.0, 1.0])[:3]]
    center = (look[h1 - 1], look[h2 - 1])
    metrics["up_axis"] = "z" if up == 2 else "y"
    metrics["horizontal_axes"] = ["x", "y" if up == 2 else "z"]

    # ---- 弧线半径漂移 ----
    radii = [math.hypot(float(m[h1]) - center[0], float(m[h2]) - center[1]) for m in moves]
    mean_r = sum(radii) / len(radii)
    radius_drift = (max(radii) - min(radii)) / mean_r if mean_r > 1e-9 else 0.0
    metrics.update({
        "radius_mean_m": mean_r,
        "radius_min_m": min(radii),
        "radius_max_m": max(radii),
        "radius_drift_ratio": radius_drift,
    })
    if radius_drift > thr["radius_drift_ratio"]:
        failures.append(_fail(
            "radius_drift_ratio", f"≤{thr['radius_drift_ratio']:g}", round(radius_drift, 6),
            f"弧线半径 {min(radii):.3f}~{max(radii):.3f} m（均值 {mean_r:.3f}），"
            "环绕不是等距圆周 → 机位路径与「弧线横移」契约不符"))

    # ---- 焦距恒定 ----
    lens_values = [round(float(m[7]), 4) for m in moves]
    lens_drift = max(lens_values) - min(lens_values)
    metrics.update({
        "lens_min_mm": min(lens_values),
        "lens_max_mm": max(lens_values),
        "lens_drift_mm": lens_drift,
        "lens_values": sorted(set(lens_values)),
    })
    if lens_drift > thr["lens_drift_mm"]:
        failures.append(_fail("lens_drift_mm", f"≤{thr['lens_drift_mm']:g}", round(lens_drift, 6),
                              f"关键帧焦距 {sorted(set(lens_values))} 不是定焦（不能变焦）"))

    # ---- 弧角（首末机位相对看向点的方位角差）----
    a0 = math.atan2(float(moves[0][h2]) - center[1], float(moves[0][h1]) - center[0])
    a1 = math.atan2(float(moves[-1][h2]) - center[1], float(moves[-1][h1]) - center[0])
    deg = abs(math.degrees(a1 - a0))
    arc_degrees = 360.0 - deg if deg > 180.0 else deg
    metrics["arc_degrees"] = arc_degrees
    contract_arc = contract.get("arc_degrees")
    # 契约的弧角若与 spec 不同源（渲染实测 / 另一份 spec）才有判别力；同源时差值≈0
    if isinstance(contract_arc, (int, float)):
        delta = abs(arc_degrees - float(contract_arc))
        metrics["arc_delta_vs_contract_deg"] = delta
        contract_height = contract.get("camera_height_m")
        # moves 列 = [frame, x, y, z, ...]；up=2 表示 z 是高度 → 高度列下标 3 = up+1
        height_col = up + 1
        other_col = 2 if up == 2 else 3
        same_convention = True
        if isinstance(contract_height, (int, float)):
            same_convention = (abs(float(contract_height) - float(moves[0][height_col])) <
                               abs(float(contract_height) - float(moves[0][other_col])))
        if not same_convention:
            limitations.append(
                "契约的相机高度与该 spec 的高度轴不一致（契约可能用另一套坐标约定或渲染采样），"
                "弧角/半径的契约对比被跳过")
        elif delta > thr["arc_angle_tolerance_deg"]:
            failures.append(_fail(
                "arc_degrees", f"契约 {float(contract_arc):.2f}°±{thr['arc_angle_tolerance_deg']:g}°",
                round(arc_degrees, 3),
                "白膜实际弧角偏离契约；注意若契约由同一份 spec 派生，此项恒等（见 limitations）") )
    intent_arc = contract.get("arc_degrees_intent")
    if isinstance(intent_arc, (int, float)):
        delta_intent = abs(arc_degrees - float(intent_arc))
        metrics["arc_delta_vs_intent_deg"] = delta_intent
        if delta_intent > thr["arc_angle_tolerance_deg"]:
            failures.append(_fail(
                "arc_degrees_intent", f"剧本意图 {float(intent_arc):g}°±{thr['arc_angle_tolerance_deg']:g}°",
                round(arc_degrees, 3), "白膜弧角与剧本写明的弧角不一致"))

    # ---- 机位高度恒定 / 看向点恒定 ----
    heights = [float(m[up + 1]) for m in moves]
    height_span = max(heights) - min(heights)
    metrics.update({"camera_height_m": sum(heights) / len(heights),
                    "camera_height_span_m": height_span})
    if height_span > 1e-3:
        failures.append(_fail("camera_height", "恒定（span ≤0.001m）", round(height_span, 6),
                              "环形/横移镜头机位高度必须恒定，否则等于叠加升降"))

    look_cols = [[float(m[4 + i]) for m in moves] for i in range(3)]
    look_span = max(max(col) - min(col) for col in look_cols)
    metrics["look_at_span"] = look_span
    if look_span > 1e-6:
        failures.append(_fail("look_at", "全程锁定同一看向点（span ≤1e-6）", round(look_span, 8),
                              "看向点漂移 = 构图/视线漂移"))

    # ---- 时长 / 帧率 / 关键帧末帧 ----
    fps = float(contract.get("fps") or spec_shot.get("fps") or FPS_DEFAULT)
    duration = float(contract.get("duration") or spec_shot.get("duration") or 0.0)
    expected_frames = duration * fps
    last_frame = int(moves[-1][0])
    frame_delta = last_frame - expected_frames
    metrics.update({"duration_s": duration, "fps": fps,
                    "expected_frames": expected_frames, "last_key_frame": last_frame,
                    "frame_delta": frame_delta, "first_key_frame": int(moves[0][0])})
    if duration > 0 and abs(frame_delta) > 1:
        failures.append(_fail("duration_frames", f"末帧≈duration*fps={expected_frames:g}（±1 帧）",
                              last_frame, "关键帧时长与 spec.duration 对不上"))

    # ---- 主体接触（不悬浮）----
    props = _props_of(spec_shot)
    subject = None
    for prop in props:
        if prop.get("on_surface"):
            subject = prop
            break
    if subject is None:
        for prop in props:
            if str(prop.get("type")) in ("box", "cube", "subject", "product"):
                subject = prop
                break
    support = None
    if subject is not None:
        support_id = subject.get("on_surface")
        if support_id:
            support = next((p for p in props if str(p.get("id")) == str(support_id)), None)
        if support is None:
            support = next((p for p in props if str(p.get("type")) in ("table", "desk", "surface")), None)
    if subject is None:
        failures.append(_fail("subject_prop", "set.props 里有主体（type=box，带 on_surface）",
                              [p.get("type") for p in props],
                              "没有主体就无法判定接触关系；至少应是 type=box + on_surface"))
        limitations.append("白膜 spec 未标注主体/支撑面，接触关系未验收")
    else:
        def _base_height(prop: Dict[str, Any]) -> float:
            z = float(prop.get("z") or 0.0)
            h = float(prop.get("h") or 0.0)
            return z if str(prop.get("anchor") or "base") == "base" else z - h / 2.0

        def _top_height(prop: Dict[str, Any]) -> float:
            z = float(prop.get("z") or 0.0)
            h = float(prop.get("h") or 0.0)
            return z + h if str(prop.get("anchor") or "base") == "base" else z + h / 2.0

        subject_base = _base_height(subject)
        support_top = _top_height(support) if support else float("nan")
        gap = subject_base - support_top if support else float("nan")
        metrics.update({
            "subject_type": subject.get("type"),
            "subject_base_z": subject_base,
            "subject_on_surface": subject.get("on_surface"),
            "support_type": (support or {}).get("type"),
            "support_top_z": support_top,
            "contact_gap_m": gap,
        })
        if not subject.get("on_surface"):
            failures.append(_fail("subject_on_surface", "主体带 on_surface（显式支撑关系）",
                                  None, "旧白膜缺 on_surface，主体与台面的关系无从校验（悬浮风险）"))
        if support is None:
            failures.append(_fail("support_prop", "存在支撑面 prop（table/desk）",
                                  [p.get("type") for p in props], "找不到支撑面"))
        elif float(support.get("h") or 0.0) <= 0:
            failures.append(_fail("support_height", "支撑面 h>0", float(support.get("h") or 0.0),
                                  "支撑面高度为 0（几何退化），接触关系不可信"))
        if support is not None and math.isfinite(gap) and abs(gap) > 1e-3:
            failures.append(_fail(
                "subject_contact", "|主体底面 − 支撑面顶面| ≤0.001m", round(gap, 6),
                f"主体底面 z={subject_base:g}，台面顶面 z={support_top:g}；"
                f"间隙 {gap:+.3f}m → 主体悬浮/穿模"))

    # ---- 几何运镜分类 vs 契约 movement ----
    dx = float(moves[-1][h1]) - float(moves[0][h1])
    dy = float(moves[-1][h2]) - float(moves[0][h2])
    travel = math.hypot(dx, dy)
    if travel < 1e-3 and height_span < 1e-3:
        geometric = "static"
    elif radius_drift <= thr["radius_drift_ratio"] and arc_degrees >= 5.0:
        geometric = "orbit"
    elif height_span > 0.05:
        geometric = "crane"
    elif radius_drift > thr["radius_drift_ratio"] and arc_degrees < 30.0:
        geometric = "track"
    else:
        geometric = "unknown"
    contract_move = contract.get("movement")
    metrics.update({"movement_geometric": geometric, "movement_contract": contract_move,
                    "travel_m": travel})
    if geometric == "unknown":
        limitations.append("几何运镜无法归入 orbit/track/crane/static，未做 movement 对比")
    elif isinstance(contract_move, str) and contract_move in (
            "orbit", "track", "static", "dolly_in", "dolly_out", "crane", "pan_rotate"):
        if contract_move != geometric:
            failures.append(_fail(
                "movement", f"契约 movement={contract_move}", geometric,
                f"按关键帧几何：半径漂移 {radius_drift:.1%}、弧角 {arc_degrees:.1f}°、"
                f"位移 {travel:.3f}m、高度变化 {height_span:.3f}m → 判定为 {geometric}"))

    # ---- 契约自洽性说明 ----
    if isinstance(contract.get("radius_drift_ratio"), (int, float)):
        metrics["radius_drift_contract"] = float(contract["radius_drift_ratio"])
        if abs(float(contract["radius_drift_ratio"]) - radius_drift) > 0.01:
            limitations.append(
                f"契约 radius_drift_ratio={float(contract['radius_drift_ratio']):.4f} 与 spec 几何"
                f" {radius_drift:.4f} 不同源（契约可能用了渲染实测相机）")
    if contract.get("lens_rewritten") or (
            isinstance(contract.get("lens_declared_mm"), (int, float)) and
            isinstance(contract.get("lens_mm"), (int, float)) and
            abs(float(contract["lens_declared_mm"]) - float(contract["lens_mm"])) > thr["lens_drift_mm"]):
        limitations.append(
            f"渲染器为取景改写了焦距：声明 {contract.get('lens_declared_mm')}mm → 实际 "
            f"{contract.get('lens_mm')}mm；白膜几何仍按关键帧焦距验收（{metrics['lens_min_mm']}mm）")
    if contract and not contract.get("conflicts"):
        limitations.append(
            "本项用同一份 spec 派生契约与几何，弧角/半径/帧数为同源自洽复核；"
            "独立证据来自 intent（剧本）与渲染相机采样（camera_samples），有则参与对比")

    return _result(metrics, failures, limitations)


# --------------------------------------------------------------------------- #
# B. 白膜 ↔ 480p 成片（像素 / 运动级）
# --------------------------------------------------------------------------- #
def evaluate_generated(whitebox_mp4: str, generated_mp4: str,
                       contract: Optional[Dict[str, Any]] = None,
                       *,
                       thresholds: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """B. 白膜视频 ↔ 生成成片：结构、轮廓、主体尺度、运动方向、弧线/变焦启发式。"""
    limitations: List[str] = []
    failures: List[Dict[str, Any]] = []
    metrics: Dict[str, Any] = {}
    contract = contract or {}
    thr = _merge_thresholds(contract, thresholds)
    try:
        white = probe_media(whitebox_mp4)
        gen = probe_media(generated_mp4)
    except ConsistencyError as exc:
        failures.append(_fail("media_unavailable", "两段可读的 mp4", str(exc), ""))
        return _result(metrics, failures, limitations)

    fps = float(contract.get("fps") or gen.get("fps") or white.get("fps") or FPS_DEFAULT)
    n_white = int(white.get("frames") or 0)
    n_gen = int(gen.get("frames") or 0)
    if n_white <= 1 or n_gen <= 1:
        failures.append(_fail("media_frames", "帧数可探测", [n_white, n_gen], "ffprobe 读不到帧数"))
        return _result(metrics, failures, limitations)
    n = min(n_white, n_gen)

    metrics.update({
        "whitebox": {k: white[k] for k in ("width", "height", "fps", "frames", "duration")},
        "generated": {k: gen[k] for k in ("width", "height", "fps", "frames", "duration")},
        "frames_compared": n,
        "duration_delta_s": (gen.get("duration") or 0) - (white.get("duration") or 0),
    })
    if abs(white["aspect_ratio"] - gen["aspect_ratio"]) > 0.02:
        limitations.append(
            f"两段宽高比不一致：白膜 {white['width']}x{white['height']}="
            f"{white['aspect_ratio']:.4f}，成片 {gen['width']}x{gen['height']}="
            f"{gen['aspect_ratio']:.4f}；分析时统一拉伸到同一画布，尺寸带有 ~"
            f"{abs(1 - gen['aspect_ratio'] / white['aspect_ratio']) * 100:.1f}% 几何畸变")

    # ---- 1) ffmpeg SSIM（契约要求）----
    ssim = _ssim_ffmpeg(whitebox_mp4, generated_mp4, n, fps=fps)
    metrics["ssim"] = ssim
    if ssim is None:
        failures.append(_fail("ssim", f">={thr['ssim_min']:g}", None, "ffmpeg ssim 无输出"))
    elif ssim < thr["ssim_min"]:
        failures.append(_fail("ssim", f">={thr['ssim_min']:g}", round(ssim, 4),
                              "白膜与成片的整体结构相似度不足"))

    # ---- 解码（统一画布）----
    rgb_w = _decode(whitebox_mp4, *SEG_CANVAS, frames=n)
    rgb_g = _decode(generated_mp4, *SEG_CANVAS, frames=n)
    n = min(n, len(rgb_w), len(rgb_g))
    idx = _sample_indices(n, SEG_SAMPLE_FRAMES)

    # ---- 2) 主体轮廓 IoU / 面积 / 高度 ----
    white_segs = [_segment_subject(rgb_w[i]) for i in idx]
    gen_segs = [_segment_subject(rgb_g[i]) for i in idx]
    ious, height_w, height_g, area_w, area_g = [], [], [], [], []
    for sw, sg in zip(white_segs, gen_segs):
        if sw and sg:
            ious.append(_bbox_iou(sw["bbox"], sg["bbox"]))
        if sw:
            height_w.append(sw["height_ratio"])
            area_w.append(sw["area_ratio"])
        if sg:
            height_g.append(sg["height_ratio"])
            area_g.append(sg["area_ratio"])
    metrics["silhouette_frames"] = len(ious)
    if ious:
        iou_mean = float(np.mean(ious))
        metrics.update({
            "silhouette_iou_mean": iou_mean,
            "silhouette_iou_min": float(np.min(ious)),
            "silhouette_iou_std": float(np.std(ious)),
            "silhouette_iou_series": ious,
        })
        if iou_mean < thr["silhouette_iou_min"]:
            failures.append(_fail(
                "silhouette_iou", f">={thr['silhouette_iou_min']:g}", round(iou_mean, 4),
                "主体 bbox IoU 不足 → 成片的主体位置/大小与白膜不一致"))
    else:
        failures.append(_fail("silhouette_iou", ">=可分割的主体轮廓", None,
                              "两段视频至少一段分割不出主体（画面过暗/无主体）"))

    if height_w and height_g:
        hw = float(np.median(height_w))
        hg = float(np.median(height_g))
        delta = abs(hw - hg)
        metrics.update({
            "subject_height_whitebox": hw,
            "subject_height_generated": hg,
            "subject_height_delta": delta,
            "subject_height_series_whitebox": height_w,
            "subject_height_series_generated": height_g,
            "subject_area_whitebox_mean": float(np.mean(area_w)) if area_w else None,
            "subject_area_generated_mean": float(np.mean(area_g)) if area_g else None,
        })
        if delta > thr["subject_height_delta_max"]:
            failures.append(_fail(
                "subject_height_ratio", f"|Δ占画高| ≤{thr['subject_height_delta_max']:g}",
                round(delta, 4),
                f"白膜主体高 {hw * 100:.1f}% vs 成片 {hg * 100:.1f}% → 景别/距离不一致"
                "（多为推拉/变焦或主体尺寸被改写）"))

        # 变焦启发式：同一段视频内主体高度单调变化
        def _zoom(series: Sequence[float]) -> Tuple[bool, float, float]:
            arr = np.asarray(series, dtype=np.float64)
            mean = float(arr.mean())
            span = float((arr.max() - arr.min()) / mean) if mean > 1e-9 else 0.0
            trend = _trend(arr)
            return (span > thr["zoom_span_ratio"] and abs(trend) >= thr["zoom_trend_min"]), span, trend

        zoom_w, span_w, trend_w = _zoom(height_w)
        zoom_g, span_g, trend_g = _zoom(height_g)
        metrics.update({
            "zoom_whitebox": zoom_w, "zoom_span_whitebox": span_w, "zoom_trend_whitebox": trend_w,
            "zoom_generated": zoom_g, "zoom_span_generated": span_g, "zoom_trend_generated": trend_g,
        })
        if zoom_g and not zoom_w:
            failures.append(_fail(
                "suspected_zoom", "成片与白膜一样不变焦", f"成片主体高度 {span_g:.1%} 单调变化",
                "成片主体面积持续变化而白膜稳定 → 疑似推拉/变焦（契约要求定焦）"))
        elif zoom_w and not zoom_g:
            failures.append(_fail(
                "zoom_not_reproduced", "成片复现白膜的推拉", f"白膜 {span_w:.1%} 单调变化，成片 "
                f"{span_g:.1%}", "白膜有推拉趋势而成片没有 → 运镜没被复现"))
    else:
        limitations.append("主体高度序列为空，景别/变焦未判定")

    # ---- 3) 运动方向一致性（全局平移 + 分块交叉验证）----
    gray_w = rgb_w.astype(np.float32).mean(axis=-1)
    gray_g = rgb_g.astype(np.float32).mean(axis=-1)
    motion_w = _motion_track(gray_w)
    motion_g = _motion_track(gray_g)
    regional_w = _regional_cumulative(gray_w)
    regional_g = _regional_cumulative(gray_g)
    conf_w = _motion_confidence(motion_w, regional_w)
    conf_g = _motion_confidence(motion_g, regional_g)
    metrics.update({
        "motion_cumulative_whitebox_px": motion_w["cumulative_px"],
        "motion_cumulative_generated_px": motion_g["cumulative_px"],
        "motion_total_path_whitebox_px": motion_w["total_path_px"],
        "motion_total_path_generated_px": motion_g["total_path_px"],
        "motion_coherence_whitebox": motion_w["coherence"],
        "motion_coherence_generated": motion_g["coherence"],
        "motion_mean_step_whitebox_px": motion_w["mean_step_px"],
        "motion_mean_step_generated_px": motion_g["mean_step_px"],
        "motion_peak_whitebox": motion_w["peak_mean"],
        "motion_peak_generated": motion_g["peak_mean"],
        "motion_regional_cumulative_whitebox_px": np.linalg.norm(regional_w),
        "motion_regional_cumulative_generated_px": np.linalg.norm(regional_g),
        "motion_global_regional_cosine_whitebox": conf_w["global_regional_cosine"],
        "motion_global_regional_cosine_generated": conf_g["global_regional_cosine"],
        "motion_confidence_whitebox": conf_w["confident"],
        "motion_confidence_generated": conf_g["confident"],
    })
    disp_w, disp_g = motion_w["disp"], motion_g["disp"]
    m = min(len(disp_w), len(disp_g))
    cosines = []
    for i in range(m):
        aw = math.hypot(float(disp_w[i, 0]), float(disp_w[i, 1]))
        ag = math.hypot(float(disp_g[i, 0]), float(disp_g[i, 1]))
        if aw >= MOTION_STEP_MIN_PX and ag >= MOTION_STEP_MIN_PX:
            c = _cosine(disp_w[i], disp_g[i])
            if c is not None:
                cosines.append(c)
    motion_cosine = float(np.mean(cosines)) if cosines else None
    dir_cosine = _cosine(motion_w["cumulative"], motion_g["cumulative"])
    metrics.update({
        "motion_cosine": motion_cosine,
        "motion_cosine_frames": len(cosines),
        "motion_cumulative_dir_cosine": dir_cosine,
    })
    if motion_w["cumulative_px"] > 1e-9:
        metrics["motion_energy_ratio"] = motion_g["cumulative_px"] / motion_w["cumulative_px"]

    motion_verified = bool(conf_w["confident"] and conf_g["confident"])
    metrics["motion_verified"] = motion_verified
    # 「成片根本没有相干机位移动」：相干度低 且 累积位移远小于白膜
    wb_cum = motion_w["cumulative_px"]
    gen_weak = bool(
        motion_g["coherence"] < 0.50
        and motion_g["cumulative_px"] < max(4.0, 0.25 * wb_cum)
    )
    metrics["motion_generated_weak"] = gen_weak
    if motion_verified:
        if dir_cosine is not None and dir_cosine < thr["motion_cosine_min"]:
            failures.append(_fail(
                "motion_cosine", f">={thr['motion_cosine_min']:g}", round(dir_cosine, 4),
                f"累计位移方向：白膜 ({motion_w['cumulative'][0]:.1f},"
                f"{motion_w['cumulative'][1]:.1f})px vs 成片 "
                f"({motion_g['cumulative'][0]:.1f},{motion_g['cumulative'][1]:.1f})px"))
        ratio = metrics.get("motion_energy_ratio")
        if isinstance(ratio, float) and ratio < thr["motion_energy_min_ratio"]:
            failures.append(_fail(
                "motion_energy", f"成片相干位移 ≥{thr['motion_energy_min_ratio']:g}× 白膜",
                round(ratio, 4), "成片机位移动幅度远小于白膜（运镜被削弱）"))
    elif conf_w["confident"] and gen_weak:
        failures.append(_fail(
            "motion_not_reproduced",
            f"成片与白膜同量级的单一方向平移（≥{thr['motion_min_cumulative_px']:g}px）",
            f"成片 累计 {motion_g['cumulative_px']:.1f}px / 相干度 "
            f"{motion_g['coherence']:.2f}",
            f"白膜 累计 {motion_w['cumulative_px']:.1f}px / 相干度 "
            f"{motion_w['coherence']:.2f} → 成片里看不到与白膜对应的机位平移"
            "（480p 成片常见：运镜被削弱成近似静止 / 只剩灯光变化）"))
        limitations.append(
            "motion_not_reproduced 依据「整帧相位相关相干度」：白膜 "
            f"{motion_w['coherence']:.2f}（逐帧同向）vs 成片 "
            f"{motion_g['coherence']:.2f}（方向随机）。该判据对低纹理画面偏保守，"
            "建议人工复核一次成片的前后帧；分块估计见 metrics.motion_regional_*")
    else:
        limitations.append(
            "运动方向一致性**未判定**：估计未通过相干度门槛（白膜 累计 "
            f"{motion_w['cumulative_px']:.1f}px / 相干度 {motion_w['coherence']:.2f}；"
            f"成片 累计 {motion_g['cumulative_px']:.1f}px / 相干度 "
            f"{motion_g['coherence']:.2f}）→ motion_cosine / arc_likeness 只报数字，不判罚")




    # ---- 4) 弧线相似度（由位移序列积分的相机平移轨迹）----
    path_w = motion_w["path"]
    path_g = motion_g["path"]
    if len(path_w) >= 4 and len(path_g) >= 4:
        line_w, line_g = _line_residual(path_w), _line_residual(path_g)
        circ_w, circ_g = _circle_residual(path_w), _circle_residual(path_g)
        metrics.update({
            "arc_line_residual_whitebox_px": line_w,
            "arc_circle_residual_whitebox_px": circ_w,
            "arc_likeness_whitebox": circ_w / max(line_w, 0.05),
            "arc_line_residual_generated_px": line_g,
            "arc_circle_residual_generated_px": circ_g,
            "arc_likeness_generated": circ_g / max(line_g, 0.05),
        })
        limitations.append(
            "弧线判定用的是**画面全局平移轨迹**，不是相机世界轨迹：绕主体环绕时主体始终居中，"
            "全局位移近似直线，arc_likeness 会偏向「非弧」；弧角应由 A 组（spec 几何）判定")
        if str(contract.get("movement") or "") == "orbit":
            limitations.append(
                "契约 movement=orbit，但 B 组无法从像素独立证明弧角；这里只报告 "
                f"arc_likeness（白膜 {metrics['arc_likeness_whitebox']:.2f} / "
                f"成片 {metrics['arc_likeness_generated']:.2f}）")

    # ---- 5) 结构相似度 edge_ncc（灰模 ↔ 写实渲染的补充判据）----
    edge_mean, edge_std = _edge_ncc(rgb_w, rgb_g)
    metrics.update({"edge_ncc": edge_mean, "edge_ncc_std": edge_std})
    if edge_mean < thr["edge_ncc_min"]:
        failures.append(_fail(
            "edge_ncc", f">={thr['edge_ncc_min']:g}", round(edge_mean, 4),
            "Sobel 结构互相关过低 → 成片的构图/物体边缘与白膜对不上（跨渲染风格的结构判据）"))

    # ---- 6) 运动/分割总体局限 ----
    limitations.extend([
        "主体分割取的是灰模/成片里的**受光面**（低对比灰模的暗面与墙面亮度重叠，无法稳定取全轮廓），"
        "因此 bbox 高度可信、面积只做趋势参考",
        "SSIM 对灰模↔写实渲染的亮度/对比度差敏感，跨风格时偏低；判别力以 edge_ncc + 轮廓 IoU 为主",
        "本组不做语义/材质/光影判断（不判断「像不像产品」），只比几何与运动一致性",
    ])
    return _result(metrics, failures, limitations)


# --------------------------------------------------------------------------- #
# C. 成片内容合规（低成本硬检查）
# --------------------------------------------------------------------------- #
def evaluate_delivery(mp4: str, contract: Optional[Dict[str, Any]] = None,
                      *,
                      thresholds: Optional[Dict[str, float]] = None,
                      reference_image: Optional[str] = None) -> Dict[str, Any]:
    """C. 成片交付硬检查：时长 / 帧数 / 宽高比 / 静音 / 底部字幕带启发式。"""
    limitations: List[str] = []
    failures: List[Dict[str, Any]] = []
    metrics: Dict[str, Any] = {}
    contract = contract or {}
    thr = _merge_thresholds(contract, thresholds)
    try:
        info = probe_media(mp4)
    except ConsistencyError as exc:
        failures.append(_fail("media_unavailable", "可读的 mp4", str(exc), ""))
        return _result(metrics, failures, limitations)

    fps = float(contract.get("fps") or info.get("fps") or FPS_DEFAULT)
    duration = float(contract.get("duration") or 0.0)
    expected_frames = duration * fps if duration > 0 else None
    metrics.update({
        "width": info["width"],
        "height": info["height"],
        "aspect_ratio": info["aspect_ratio"],
        "fps": info["fps"],
        "frames": info["frames"],
        "duration_s": info["duration"],
        "contract_duration_s": duration or None,
        "contract_fps": fps,
        "expected_frames": expected_frames,
        "has_audio": info["has_audio"],
        "audio_streams": info["audio_streams"],
        "audio_codec": info["audio_codec"],
        "size_bytes": info["size_bytes"],
    })

    # ---- 时长 / 帧数（容忍 1 帧）----
    tolerance = float(thr["delivery_frame_tolerance"])
    if expected_frames:
        delta = float(info["frames"] or 0) - expected_frames
        metrics["frame_delta"] = delta
        if abs(delta) > tolerance:
            failures.append(_fail(
                "delivery_frames", f"{expected_frames:g} 帧（±{tolerance:g}）", info["frames"],
                f"契约 {duration:g}s × {fps:g}fps；成片 {info['frames']} 帧 / "
                f"{info['duration']}s"))
        if duration > 0 and info["duration"]:
            dur_delta = float(info["duration"]) - duration
            metrics["duration_delta_s"] = dur_delta
            if abs(dur_delta) > tolerance / fps + 1e-6:
                failures.append(_fail(
                    "delivery_duration", f"{duration:g}s（±{tolerance / fps:.4f}s）",
                    info["duration"], "成片时长与契约不一致"))
    else:
        limitations.append("契约没有 duration，时长/帧数未对照")

    # ---- 宽高比 ----
    target_aspect = 16.0 / 9.0
    aspect_hint = contract.get("aspect_ratio")
    if isinstance(aspect_hint, (int, float)) and aspect_hint > 0:
        target_aspect = float(aspect_hint)
    rel = abs(info["aspect_ratio"] - target_aspect) / target_aspect
    metrics.update({"target_aspect_ratio": target_aspect, "aspect_rel_error": rel})
    if rel > thr["delivery_aspect_tolerance"]:
        failures.append(_fail(
            "delivery_aspect", f"{target_aspect:.4f}（±{thr['delivery_aspect_tolerance']:g}）",
            round(info["aspect_ratio"], 4),
            f"{info['width']}x{info['height']}；宽高比与 16:9 不符（常见于编码对齐补边）"))
    if contract.get("resolution"):
        try:
            want_w, want_h = (int(v) for v in contract["resolution"][:2])
            metrics["resolution_expected"] = [want_w, want_h]
            if (info["width"], info["height"]) != (want_w, want_h):
                failures.append(_fail("delivery_resolution", f"{want_w}x{want_h}",
                                      f"{info['width']}x{info['height']}", "分辨率与契约不一致"))
        except (TypeError, ValueError):
            pass

    # ---- 静音（本轮要求 no audio）----
    if info["has_audio"]:
        failures.append(_fail(
            "delivery_silent", "无音轨（silent）",
            f"{info['audio_streams']} 条 {info['audio_codec']} "
            f"{info['audio_sample_rate']}Hz/{info['audio_channels']}ch",
            "本轮交付要求静音；有音轨说明走了带配音的旧链路或拼接残留"))

    # ---- 底部 12% 字幕带 / 水印启发式（只告警，不下结论）----
    probe_n = min(int(info["frames"] or 0), 48)
    if probe_n >= 2:
        rgb = _decode(mp4, *SEG_CANVAS, frames=probe_n)
        grays = rgb.astype(np.float32).mean(axis=-1)
        h, w = grays.shape[1], grays.shape[2]
        band = grays[:, int(h * 0.88):, :]
        edges = np.asarray([_sobel(g) for g in grays])
        edges_band = np.asarray([_sobel(b) for b in band])
        scale = max(6.0, float(np.percentile(edges, 95)) * 0.25)
        density_all = float((edges > scale).mean())
        density_band = float((edges_band > scale).mean())
        ratio = density_band / density_all if density_all > 1e-9 else 0.0
        # 底部逐行边缘密度：字幕/水印 = 窄条内边缘密集（3 行滑窗峰值高）
        row_density = (edges_band > scale).mean(axis=2)
        kernel = np.ones(3, dtype=np.float64) / 3.0
        row_peak = 0.0
        if row_density.size:
            window = np.asarray([np.convolve(row, kernel, mode="same") for row in row_density])
            row_peak = float(window.max())
        # 字幕常见「近白文字」：底部近白像素占比
        med = float(np.median(grays))
        p99 = float(np.percentile(grays, 99))
        bright_thr = med + 0.60 * (p99 - med)
        bright_ratio = float((band > bright_thr).mean())
        suspected = bool(
            (ratio > thr["watermark_edge_ratio_warn"] and row_peak > thr["watermark_row_peak_warn"])
            or (bright_ratio > thr["watermark_bright_ratio_warn"]
                and row_peak > thr["watermark_row_peak_warn"])
        )
        metrics.update({
            "bottom_band_edge_density": density_band,
            "frame_edge_density": density_all,
            "bottom_band_edge_ratio": ratio,
            "bottom_band_row_peak_density": row_peak,
            "bottom_band_bright_ratio": bright_ratio,
            "watermark_suspected": suspected,
        })
        if suspected:
            limitations.append(
                "底部 12% 出现字幕条/水印特征（band/frame 边缘密度比 "
                f"{ratio:.2f}，3 行滑窗峰值 {row_peak:.2f}，近白占比 {bright_ratio:.3f}）"
                "→ 仅告警，不做 OCR/结论；该启发式没有正样本校准，宁可漏报")

    # ---- 参考图：不做语义判断 ----
    if reference_image:
        metrics["reference_image"] = reference_image
        limitations.append(
            "提供参考图/白膜之外的参考素材，但本检查只做客观几何与文件级硬检查，"
            "不做语义/材质/风格相似度判断（无模型、无联网）")
    limitations.extend([
        "分辨率/宽高比只校验 16:9 与（可选）contract.resolution；不判断像素级清晰度/压缩质量",
        "音频只检查是否存在，不做响度/爆音/音画同步判断",
        "字幕/水印为启发式告警，不构成通过/失败依据",
    ])
    return _result(metrics, failures, limitations)
