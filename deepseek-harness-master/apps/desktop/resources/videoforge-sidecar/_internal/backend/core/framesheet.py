# -*- coding: utf-8 -*-
r"""把一串片段抽帧拼成**联络表**（contact sheet），供人眼一次看完。

为什么这个功能必须有
────────────────────
2026-09-13 我拿真实素材做视觉核对，**眼看**发现了两件自动检测抓不住的事：
  · `19636f68` 第 5 镜末尾约 1.7 秒**整体倒过来**（街道灭点在上方）；
  · `46c75ddf` / `d5815c1e` 的"5 个镜头"其实是**同一张图**，
    只是每镜换了压在上面的那段文字（模型把提示词原文当字幕画进了画面）。

这两件事我试过三种自动判据（见 `tests/probe_flip_transition.py` 的失败记录），
**都不合格**：真正倒置的那条被漏报，正常镜头反而被误报。
所以正确的产品答案是：**别假装能自动判，给用户一个"一眼看全"的入口。**

这个模块只做一件事：把 N 个片段各抽 k 帧、缩成**完全统一的格子**拼成一张 PNG。
（格子尺寸必须统一：第一版用 `scale=360:-2`，高度随画幅变化，
`concat`+`tile` 就产出黑格甚至错切，看着像"画面倒过来了"——差点据此误报。）

产物：PNG + 一份图例（第几格 = 哪个镜头 / 哪个时间点 / 哪个文件）。
"""
from __future__ import annotations

import os
import re
import subprocess
from typing import Any, Dict, List, Optional, Sequence, Tuple

from imageio_ffmpeg import get_ffmpeg_exe

FRAME_W = 360
FRAME_H = 203          # 16:9；**所有格子必须同尺寸**，见模块说明
COLS = 6
PER_CLIP = 3


def _ffmpeg() -> str:
    try:
        return get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def probe_duration(path: str) -> float:
    p = subprocess.run([_ffmpeg(), "-hide_banner", "-i", path], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", p.stderr or "")
    if not m:
        return 0.0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def grab(path: str, t: float, dst: str, w: int = FRAME_W,
         h: int = FRAME_H) -> bool:
    r = subprocess.run([_ffmpeg(), "-y", "-v", "error", "-ss", f"{max(0.0, t):.2f}",
                        "-i", path, "-frames:v", "1", "-vf", f"scale={w}:{h}", dst],
                       capture_output=True)
    return r.returncode == 0 and os.path.exists(dst)


def tile(pngs: Sequence[str], out_png: str, cols: int = COLS) -> bool:
    if not pngs:
        return False
    rows = (len(pngs) + cols - 1) // cols
    lst = out_png + ".txt"
    try:
        with open(lst, "w", encoding="utf-8") as f:
            for i, p in enumerate(pngs):
                f.write("file '%s'\n" % p.replace("\\", "/"))
                if i < len(pngs) - 1:
                    f.write("duration 0.0400\n")      # 每张停留一帧
            f.write("file '%s'\n" % pngs[-1].replace("\\", "/"))
        r = subprocess.run([_ffmpeg(), "-y", "-v", "error", "-f", "concat", "-safe", "0",
                            "-i", lst, "-vf", f"tile={cols}x{rows}",
                            "-frames:v", "1", out_png], capture_output=True)
    finally:
        try:
            os.remove(lst)
        except Exception:
            pass
    return r.returncode == 0 and os.path.exists(out_png)


def build_sheet(clips: Sequence[Tuple[str, str]], out_png: str, *,
                per_clip: int = PER_CLIP, cols: int = COLS,
                w: int = FRAME_W, h: int = FRAME_H,
                work_dir: Optional[str] = None
                ) -> Dict[str, Any]:
    """`clips` = [(标签, 路径)] → 生成联络表。

    返回 `{"ok", "path", "cells", "cols", "rows", "legend": [...], "skipped": [...]}`。
    单个片段抽帧失败**只跳过它**，不影响其余（取证工具不该因为一条坏片段全废）。
    """
    work_dir = work_dir or os.path.dirname(out_png) or "."
    os.makedirs(work_dir, exist_ok=True)
    pngs: List[str] = []
    legend: List[str] = []
    skipped: List[str] = []
    for idx, (label, path) in enumerate(clips):
        if not path or not os.path.exists(path):
            skipped.append(f"{label}: 文件不存在")
            continue
        dur = probe_duration(path)
        if dur <= 0:
            skipped.append(f"{label}: 读不到时长")
            continue
        for k in range(max(1, per_clip)):
            frac = (k + 0.5) / max(1, per_clip)
            t = max(0.0, min(dur - 0.05, dur * frac))
            dst = os.path.join(work_dir, f"_cell_{idx:03d}_{k}.png")
            if grab(path, t, dst, w, h):
                pngs.append(dst)
                legend.append(f"第{len(pngs)}格 = {label} · t={t:.1f}s")
            else:
                skipped.append(f"{label}: t={t:.1f}s 抽帧失败")
    if not pngs:
        return {"ok": False, "error": "没有任何片段抽帧成功", "skipped": skipped,
                "legend": []}
    ok = tile(pngs, out_png, cols=cols)
    for d in pngs:
        try:
            os.remove(d)
        except Exception:
            pass
    if not ok:
        return {"ok": False, "error": "拼图失败", "skipped": skipped, "legend": legend}
    return {"ok": True, "path": out_png, "cells": len(pngs), "cols": cols,
            "rows": (len(pngs) + cols - 1) // cols,
            "legend": legend, "skipped": skipped,
            "note": (f"每镜抽 {per_clip} 帧、共 {len(clips)} 镜；"
                     f"格子按分镜顺序从左到右、从上到下")}
