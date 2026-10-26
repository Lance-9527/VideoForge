# -*- coding: utf-8 -*-
r"""试用版水印：给成片右下角打一个半透明标识（"VideoForge 试用版"）。

实现要点与踩坑记录：

* **画而不是 drawtext**：ffmpeg 的 `drawtext` 需要 freetype + 字体路径，
  中文还要 fontconfig 配好。这里改用 **Pillow 画好一张 PNG，再用 overlay 贴上去** ——
  Pillow 已经是依赖，字体走 `compose._FONT_CANDIDATES`（同一条中文渲染链路），
  行为可预测。实测打包内的 ffmpeg 两个滤镜都支持，但少一个依赖就少一类新电脑问题。
* **只打成片一次**：画幅导出、BGM 混音都是从成片派生的，会自然继承水印；
  在派生环节再打一次就会出现"两层水印"。
* **失败不能影响出片**：水印打不上时只写告警，成片照样交付
  （用户的优先级是"能出片"，不是"必须带水印"）。
* 参数保守：`-preset veryfast -crf 18 -c:a copy`，只多一遍编码；44 秒 720p
  实测几秒钟。
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from typing import Tuple

logger = logging.getLogger(__name__)


def _load_font(size: int):
    from PIL import ImageFont
    from core.compose import _FONT_CANDIDATES
    for p in _FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


_SIZE_RE = None


async def probe_size(path: str) -> Tuple[int, int]:
    """取视频宽高。

    ★ 不能用 ffprobe：imageio-ffmpeg 的 Windows 包**只含 ffmpeg.exe**
      （compose._ffprobe 的注释里也记着这件事），所以从 `ffmpeg -i` 的 stderr 里解析。
      拿不到就返回 (0, 0)，调用方按 720p 兜底 —— 水印小一点，不影响出片。
    """
    global _SIZE_RE
    import re
    if _SIZE_RE is None:
        _SIZE_RE = re.compile(r"(\d{2,5})x(\d{2,5})")
    from core.compose import _ffmpeg, _run
    try:
        _rc, err = await _run([_ffmpeg(), "-hide_banner", "-i", path], timeout=60)
        m = _SIZE_RE.search(err or "")
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return 0, 0


def make_badge(text: str, video_h: int, out_png: str) -> str:
    """画一张"半透明圆角底 + 白字"的水印图，字号按视频高度自适应。"""
    from PIL import Image, ImageDraw
    size = max(14, int(video_h / 30))          # 720p → 24px；1080p → 36px
    font = _load_font(size)
    pad_x, pad_y = int(size * 0.6), int(size * 0.35)
    try:
        box = font.getbbox(text)
        tw, th = box[2] - box[0], box[3] - box[1]
    except Exception:
        tw, th = len(text) * size, size
    w, h = tw + pad_x * 2, th + pad_y * 2 + 2
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 半透明黑底（保证在亮画面里也看得清），圆角
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=max(4, int(size * 0.35)),
                        fill=(0, 0, 0, 110))
    # 白字 + 一点阴影，避免糊在浅色背景上
    d.text((pad_x + 1, pad_y + 1), text, font=font, fill=(0, 0, 0, 120))
    d.text((pad_x, pad_y), text, font=font, fill=(255, 255, 255, 235))
    img.save(out_png, "PNG")
    return out_png


async def stamp_video(src: str, text: str = "", margin_ratio: float = 0.025,
                      timeout: int = 1800) -> Tuple[bool, str]:
    """把水印打进 `src`（原地替换）。返回 (是否成功, 给人看的一句话)。"""
    from core.trial import WATERMARK_TEXT
    from core.compose import _ffmpeg, _run, probe_duration
    text = text or WATERMARK_TEXT

    if not src or not os.path.exists(src):
        return False, f"试用版水印跳过：文件不存在（{src}）"
    try:
        dur = await probe_duration(src) or 0.0
        w, h = await probe_size(src)
        if not h:
            h = 720
        if not w:
            w = int(h * 16 / 9)
        tmpdir = tempfile.mkdtemp(prefix="vf_wm_")
        try:
            badge = make_badge(text, h, os.path.join(tmpdir, "badge.png"))
            mx, my = int((w or 1280) * margin_ratio), int(h * margin_ratio)
            dst = os.path.join(tmpdir, "stamped.mp4")
            cmd = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                   "-i", src, "-i", badge,
                   "-filter_complex", f"[0:v][1:v]overlay=W-w-{mx}:H-h-{my}:format=auto",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                   "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart",
                   dst]
            code, out = await _run(cmd, timeout=timeout)
            if code != 0 or not os.path.exists(dst) or os.path.getsize(dst) < 1024:
                return False, f"试用版水印未打上（ffmpeg 退出码 {code}）：{out[-200:]}"
            new_dur = await probe_duration(dst) or 0.0
            if dur > 1 and new_dur > 1 and abs(new_dur - dur) > 1.5:
                return False, f"试用版水印未打上（时长从 {dur:.1f}s 变成 {new_dur:.1f}s，已放弃）"
            shutil.move(dst, src)
            return True, (f"已在成片右下角加「{text}」标识（试用版）。"
                          f"这是版权署名的技术标记：正式版不带。")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception as e:      # pragma: no cover - 兜底
        logger.warning("试用版水印异常：%s", e)
        return False, f"试用版水印异常（不影响出片）：{e}"
