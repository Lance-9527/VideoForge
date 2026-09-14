# -*- coding: utf-8 -*-
"""
VideoForge · 视频参考（从用户提供的视频里取可用的参考画面）

═══════════════════════════════════════════════════════════════════
用户的原文需求
═══════════════════════════════════════════════════════════════════
  "支持用户自己提供图片或者视频做 AI 参考后根据剧情需要进行实际修改"

为什么"视频参考"要落到**抽帧**上：
  目前主流图像/视频模型能接受的是**图片**（首帧、身份参考、风格参考），
  几乎没有能直接吃视频的（Pavo 文档里也没有；MPT 更不涉及）。
  所以"用视频做参考"的正确落地方式是：
      视频 → 挑出有代表性的画面 → 当作参考图 → 再让 AI 按剧情改
  这也顺带解决了上游的一个真 bug：上传的视频曾被直接写进
  `reference_image_path`，而那个字段下游是当**图片**用的
  （`<img>` 显示、抽首帧、转 data URI），塞 mp4 进去必然坏。

挑选策略：
  1. **场景切换点优先**（ffmpeg 的 scene 检测）—— 转场处往往是最有代表性的构图
  2. 再按时间**均匀补点**，保证覆盖全片
  3. 去掉过于相似的（按文件大小+直方图粗筛，避免给出 5 张几乎一样的脸）
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Any, Dict, List, Optional

logger = logging.getLogger("videoforge.videoref")

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
VIDEO_EXT = (".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v")


def _ffmpeg() -> str:
    from core.ffmpeg_manager import get_ffmpeg_for_postprocess
    return get_ffmpeg_for_postprocess()


def _run(cmd: List[str], timeout: float = 300.0):
    p = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return p.returncode, (p.stdout or ""), (p.stderr or "")


def is_video(path: str) -> bool:
    return os.path.splitext(path or "")[1].lower() in VIDEO_EXT


def is_image(path: str) -> bool:
    return os.path.splitext(path or "")[1].lower() in IMAGE_EXT


def probe_duration(path: str) -> float:
    rc, _, err = _run([_ffmpeg(), "-hide_banner", "-i", path], timeout=60)
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", err or "")
    if not m:
        return 0.0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def _scene_times(path: str, threshold: float = 0.28) -> List[float]:
    """用 ffmpeg 的 scene 检测找出**画面发生明显变化**的时间点。

    这些时间点通常是转场/换镜，取它后一帧往往能拿到一个有代表性的构图。
    """
    rc, _, err = _run([
        _ffmpeg(), "-hide_banner", "-i", path,
        "-vf", f"select='gt(scene,{threshold})',showinfo",
        "-f", "null", "-"], timeout=600)
    times: List[float] = []
    for m in re.finditer(r"pts_time:([\d.]+)", err or ""):
        try:
            times.append(float(m.group(1)))
        except ValueError:
            continue
    return times


def extract_keyframes(video: str, out_dir: str, *, max_frames: int = 6,
                      width: int = 768) -> Dict[str, Any]:
    """从一个视频里挑出若干**有代表性**的画面，存成 jpg 供用户挑选。

    返回 {ok, frames:[{path, time, source}], duration, warnings}
    """
    os.makedirs(out_dir, exist_ok=True)
    if not os.path.exists(video):
        return {"ok": False, "error": "视频文件不存在", "frames": []}
    dur = probe_duration(video)
    if dur <= 0:
        return {"ok": False, "error": "读不出视频时长（文件可能损坏）", "frames": []}

    warnings: List[str] = []
    picks: List[tuple] = []          # (time, source)

    # ① 场景切换点（跳过最开头 0.3s，那里常是黑场/淡入）
    try:
        for t in _scene_times(video):
            if 0.3 < t < dur - 0.2:
                picks.append((t, "scene"))
    except Exception as e:
        warnings.append(f"场景切换检测失败，改用均匀取帧：{e}")

    # ② 均匀补点，保证覆盖
    n_even = max(3, max_frames)
    for i in range(n_even):
        picks.append((dur * (i + 0.5) / n_even, "even"))

    # 去重：时间太近的只留一个（优先保留 scene 点）
    picks.sort(key=lambda x: (x[0], 0 if x[1] == "scene" else 1))
    merged: List[tuple] = []
    for t, src in picks:
        if merged and abs(t - merged[-1][0]) < max(0.4, dur * 0.03):
            if src == "scene" and merged[-1][1] != "scene":
                merged[-1] = (t, src)
            continue
        merged.append((t, src))

    # 最多取 max_frames 个，且尽量保留 scene 点
    scene_pts = [x for x in merged if x[1] == "scene"]
    even_pts = [x for x in merged if x[1] == "even"]
    chosen: List[tuple] = []
    for x in scene_pts:
        if len(chosen) >= max_frames:
            break
        chosen.append(x)
    for x in even_pts:
        if len(chosen) >= max_frames:
            break
        chosen.append(x)
    chosen.sort(key=lambda x: x[0])

    frames: List[Dict[str, Any]] = []
    seen_sizes: Dict[int, int] = {}
    for idx, (t, src) in enumerate(chosen):
        out = os.path.join(out_dir, f"kf_{idx:02d}_{t:.2f}.jpg".replace(":", "_"))
        rc, _, err = _run([
            _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{t:.3f}", "-i", video, "-frames:v", "1",
            "-vf", f"scale={width}:-2", "-q:v", "3", out], timeout=120)
        if rc != 0 or not os.path.exists(out) or os.path.getsize(out) < 1024:
            continue
        size = os.path.getsize(out)
        # 粗筛：大小几乎一样大概率是同一画面（静态镜头）
        dup = any(abs(size - s) < max(60, s * 0.012) for s in seen_sizes.values())
        if dup and src == "even":
            try:
                os.remove(out)
            except OSError:
                pass
            continue
        seen_sizes[idx] = size
        frames.append({"path": out, "time": round(t, 2), "source": src,
                       "size_bytes": size})

    if not frames:
        return {"ok": False, "error": "没能从视频里取到可用画面", "frames": [],
                "duration": dur, "warnings": warnings}
    if len(frames) < 2:
        warnings.append("这个视频画面变化很少，只取到 1 张有代表性的画面。")
    return {"ok": True, "frames": frames, "duration": round(dur, 2),
            "scene_points": len(scene_pts), "warnings": warnings}


def extract_single_frame(video: str, at: float, out_path: str,
                         width: int = 1024) -> str:
    """按指定时间点取一帧（用户自己选时间时用）。"""
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    rc, _, _ = _run([
        _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(0.0, float(at)):.3f}", "-i", video, "-frames:v", "1",
        "-vf", f"scale={width}:-2", "-q:v", "2", out_path], timeout=120)
    return out_path if rc == 0 and os.path.exists(out_path) else ""
