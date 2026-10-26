# -*- coding: utf-8 -*-
"""VideoForge · 视频复刻（reference → remake spec）流水线

═══════════════════════════════════════════════════════════════════
这是什么
═══════════════════════════════════════════════════════════════════
给一段**参考视频**，输出一份**可执行的复刻规格**：切好的镜头表 + 每镜的
景别/运镜/色调/构图 + 落在该镜里的台词 + 一段可直接喂给视频模型的结构化
提示词。于是「照着这条片子做一条新的」从"看感觉"变成"照单施工"。

七环节（与调研报告 research-video-remake.md §2 一一对应）：
    ① 镜头边界检测 → shots[].start/end/duration
    ② 关键帧抽取   → shots[].keyframes[]
    ③ 台词转写     → shots[].dialogue[]（带时间戳，本地 whisper + 硅基流动校对）
    ④ 画面硬字幕 OCR → shots[].on_screen_text[]（rapidocr，可选）
    ⑤ 运镜/景别/构图 → shots[].camera_motion / shot_size / composition
    ⑥ 色调 + 节奏   → shots[].color + global.pacing
    ⑦ 提示词反推    → shots[].description / prompt_hint / prompt

═══════════════════════════════════════════════════════════════════
★ 三条硬纪律（都是踩过的坑）
═══════════════════════════════════════════════════════════════════
1. **运镜/景别低置信度时给 `unknown`，不猜**。
   调研实测：纯色画面下基于帧差的运镜判定 1/4 正确（推/摇全判 static）；
   换 goodFeaturesToTrack + LK + estimateAffinePartial2D(RANSAC) 后 5/5。
   所以本模块走光流法，并输出 `camera_confidence`；特征点太少 → `unknown`。

2. **景别靠人/脸检测，不靠"感觉"**。
   用 OpenCV 自带的 Haar cascade 找到人脸/上半身框 → 框高占比映射景别。
   检不到人 → `shot_size = "unknown"`，**不按细节密度瞎猜**。

3. **复刻结构 ≠ 复制内容**（合规边界，见 `compliance` 字段）。
   可以复刻：节奏 / 景别序列 / 运镜类型 / 色调数值 / 构图法则 / 台词节奏与字数密度。
   不能复刻：原片角色肖像与身份 / 逐字台词与歌词 / 原片具体画面与美术设计 /
             原片音乐音效字体素材 / logo 水印。
   （依据：17 U.S.C. §102；美国版权局 Compendium §310.2「版权局不看风格」、
    §310.4「look and feel 不受保护」。非法律意见。）

═══════════════════════════════════════════════════════════════════
花费
═══════════════════════════════════════════════════════════════════
除「硅基流动 ASR 校对」与「多模态提示词反推」外**全部本地 CPU 免费**。
两个花钱点都能关：`use_siliconflow=False` / `vision=False`。

═══════════════════════════════════════════════════════════════════
实测评测（本机，2026-10-24）
═══════════════════════════════════════════════════════════════════
· 镜头检测：ffmpeg `scdet=t=10` 一次跑完（398s 成片 → 39 个切点）
· ASR：硅基流动 SenseVoiceSmall，30s 音频 HTTP 200 / 3.3s / 返回纯文本
· 时间戳：本地 whisper small（CPU，模型已缓存）
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("videoforge.remake")

#: ffmpeg 查找顺序（系统版优先 —— imageio 自带那份可能缺 scdet 滤镜）
_FFMPEG_CANDIDATES = (
    "ffmpeg", "ffprobe",
)


# ══════════════════════════════════════════════════════════════════
# 基础工具
# ══════════════════════════════════════════════════════════════════


def _which(name: str) -> str:
    p = shutil.which(name)
    if p:
        return p
    # imageio 自带的
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if name == "ffmpeg":
            return exe
        d = os.path.dirname(exe)
        cand = os.path.join(d, name + ".exe")
        if os.path.isfile(cand):
            return cand
    except Exception:  # noqa: BLE001
        pass
    return ""


def _run(cmd: List[str], timeout: int = 900) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        return p.returncode, p.stdout or "", p.stderr or ""
    except FileNotFoundError:
        return 127, "", "找不到 %s" % (cmd[0] if cmd else "?")
    except subprocess.TimeoutExpired:
        return 124, "", "超时（%ss）" % timeout
    except Exception as e:  # noqa: BLE001
        return 1, "", "%s: %s" % (type(e).__name__, e)


# ══════════════════════════════════════════════════════════════════
# ① 探测
# ══════════════════════════════════════════════════════════════════


def probe(video_path: str) -> Dict[str, Any]:
    """ffprobe 拿时长/帧率/分辨率。"""
    exe = _which("ffprobe")
    if not exe:
        return {"ok": False, "error": "找不到 ffprobe"}
    code, out, err = _run([exe, "-v", "error", "-print_format", "json",
                           "-show_format", "-show_streams", video_path], timeout=120)
    if code != 0:
        return {"ok": False, "error": (err or out)[:300]}
    try:
        j = json.loads(out or "{}")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "ffprobe 输出不是 JSON：%s" % e}
    v = next((s for s in (j.get("streams") or []) if s.get("codec_type") == "video"), None)
    if not v:
        return {"ok": False, "error": "文件里没有视频流"}
    dur = 0.0
    try:
        dur = float((j.get("format") or {}).get("duration") or v.get("duration") or 0)
    except Exception:  # noqa: BLE001
        dur = 0.0
    fps = 0.0
    try:
        r = str(v.get("avg_frame_rate") or "0/1")
        if "/" in r:
            a, b = r.split("/", 1)
            fps = float(a) / float(b) if float(b) else 0.0
        else:
            fps = float(r)
    except Exception:  # noqa: BLE001
        fps = 0.0
    return {
        "ok": True, "error": "",
        "duration": round(dur, 3), "fps": round(fps, 3),
        "width": int(v.get("width") or 0), "height": int(v.get("height") or 0),
        "codec": str(v.get("codec_name") or ""),
        "has_audio": any(s.get("codec_type") == "audio" for s in (j.get("streams") or [])),
        "bitrate": int((j.get("format") or {}).get("bit_rate") or 0) or 0,
    }


# ══════════════════════════════════════════════════════════════════
# ② 镜头边界检测
# ══════════════════════════════════════════════════════════════════


def detect_shots(video_path: str, *, threshold: int = 10,
                 min_shot: float = 0.4) -> Dict[str, Any]:
    """用 ffmpeg 的 `scdet` 滤镜找切点。

    返回 {"ok":bool,"cuts":[float,...],"shots":[{"start","end","duration"}],"error":str}
    """
    exe = _which("ffmpeg")
    if not exe:
        return {"ok": False, "cuts": [], "shots": [], "error": "找不到 ffmpeg"}
    code, out, err = _run([exe, "-hide_banner", "-nostats", "-i", video_path,
                           "-vf", "scdet=t=%d" % int(threshold),
                           "-an", "-f", "null", "NUL"], timeout=1800)
    blob = (out or "") + (err or "")
    times: List[float] = []
    for m in re.finditer(r"lavfi\.scd\.time:\s*([0-9]+(?:\.[0-9]+)?)", blob):
        try:
            times.append(float(m.group(1)))
        except Exception:  # noqa: BLE001
            pass
    times = sorted(set(round(t, 3) for t in times))
    pr = probe(video_path)
    total = float(pr.get("duration") or 0) if pr.get("ok") else 0.0
    if not total:
        total = (times[-1] + 3.0) if times else 0.0
    bounds = [0.0] + [t for t in times if t > 0] + ([round(total, 3)] if total > 0 else [])
    shots: List[Dict[str, Any]] = []
    for i in range(len(bounds) - 1):
        s, e = bounds[i], bounds[i + 1]
        if e - s < min_shot:
            # 太短的段并进上一镜（避免"闪一下"被当成一个镜头）
            if shots:
                shots[-1]["end"] = e
                shots[-1]["duration"] = round(shots[-1]["end"] - shots[-1]["start"], 3)
            continue
        shots.append({"index": len(shots), "start": round(s, 3),
                      "end": round(e, 3), "duration": round(e - s, 3)})
    return {"ok": True, "error": "", "cuts": times, "shots": shots,
            "note": "切点来自 ffmpeg scdet（内容突变）；min_shot=%ss" % min_shot}


# ══════════════════════════════════════════════════════════════════
# ③ 关键帧抽取
# ══════════════════════════════════════════════════════════════════


def extract_keyframes(video_path: str, shots: List[Dict[str, Any]], out_dir: str,
                      *, per_shot: int = 3) -> Dict[str, Any]:
    """每镜抽 1–3 张关键帧（首/中/尾）。"""
    exe = _which("ffmpeg")
    os.makedirs(out_dir, exist_ok=True)
    ok = True
    err = ""
    if not exe:
        return {"ok": False, "error": "找不到 ffmpeg"}
    for sh in shots:
        dur = float(sh.get("duration") or 0)
        n = max(1, min(int(per_shot), 3))
        if n == 1 or dur <= 0.05:
            offsets = [float(sh.get("start") or 0)]
        elif n == 2:
            offsets = [float(sh.get("start") or 0), float(sh.get("end") or 0) - 0.05]
        else:
            s = float(sh.get("start") or 0)
            offsets = [s + 0.02, s + dur / 2.0, max(s, float(sh.get("end") or 0) - 0.06)]
        paths: List[str] = []
        for i, off in enumerate(offsets):
            p = os.path.join(out_dir, "shot%04d_f%d.jpg" % (int(sh.get("index") or 0), i))
            code, _o, e = _run([exe, "-y", "-loglevel", "error", "-ss", "%.3f" % max(0.0, off),
                               "-i", video_path, "-frames:v", "1", "-q:v", "3", p], timeout=120)
            if code == 0 and os.path.isfile(p) and os.path.getsize(p) > 500:
                paths.append(p)
            else:
                ok = False
                err = (err + " | " if err else "") + (e or "")[:120]
        sh["keyframes"] = paths
    return {"ok": ok, "error": err, "out_dir": out_dir}


# ══════════════════════════════════════════════════════════════════
# ⑤ 视觉量化：色调 / 景别 / 运镜 / 构图
# ══════════════════════════════════════════════════════════════════


def _load_gray(path: str):
    import cv2
    img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None, None
    h, w = img.shape[:2]
    scale = 480.0 / max(1, w)
    if scale < 1.0:
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    return img, cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


def color_stats(frame_path: str) -> Dict[str, Any]:
    """一帧的色调统计（可复刻的数值）。"""
    import cv2
    import numpy as np
    img = cv2.imread(frame_path, cv2.IMREAD_COLOR)
    if img is None:
        return {"ok": False}
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, s, v = hsv[..., 0].astype(np.float32), hsv[..., 1].astype(np.float32), hsv[..., 2].astype(np.float32)
    bright = float(v.mean()) / 255.0
    sat = float(s.mean()) / 255.0
    contrast = float(gray.std()) / 255.0
    # 主色（按饱和度加权，避免黑白像素主导）
    mask = s > 40
    hues = h[mask]
    hist = np.zeros(36, dtype=np.float32)
    if hues.size:
        idx = (hues / 10.0).astype(np.int32) % 36
        for i in idx:
            hist[i] += 1
    tot = float(hist.sum()) or 1.0
    top = np.argsort(-hist)[:4]
    dominant = [{"hue_bin": int(i * 10), "hue": int(i * 10 + 5),
                 "share": round(float(hist[i]) / tot, 3)} for i in top if hist[i] > 0]
    warm = sum(float(hist[i]) for i in range(0, 5)) + sum(float(hist[i]) for i in range(16, 18))
    cool = sum(float(hist[i]) for i in range(9, 15))
    neutral = tot - warm - cool
    if warm / tot > 0.12 and cool / tot > 0.08:
        palette = "暖橙vs青蓝对撞"
    elif warm / tot > 0.12:
        palette = "暖调"
    elif cool / tot > 0.08:
        palette = "冷青蓝"
    else:
        palette = "低饱和中性"
    # 暗部/亮部占比（电影感常用门）
    dark = float((v < 45).mean())
    blown = float((v > 245).mean())
    return {
        "ok": True, "brightness": round(bright, 3), "saturation": round(sat, 3),
        "contrast": round(contrast, 3), "dominant": dominant, "palette": palette,
        "warm_share": round(warm / tot, 3), "cool_share": round(cool / tot, 3),
        "neutral_share": round(max(0.0, neutral) / tot, 3),
        "dark_ratio": round(dark, 3), "blown_ratio": round(blown, 3),
    }


_FACE_CASCADE = None
_BODY_CASCADE = None


def _cascades():
    global _FACE_CASCADE, _BODY_CASCADE
    if _FACE_CASCADE is None:
        try:
            import cv2
            p = cv2.data.haarcascades
            f = os.path.join(p, "haarcascade_frontalface_default.xml")
            b = os.path.join(p, "haarcascade_upperbody.xml")
            _FACE_CASCADE = cv2.CascadeClassifier(f) if os.path.isfile(f) else None
            _BODY_CASCADE = cv2.CascadeClassifier(b) if os.path.isfile(b) else None
        except Exception:  # noqa: BLE001
            _FACE_CASCADE = _BODY_CASCADE = None
    return _FACE_CASCADE, _BODY_CASCADE


def detect_subject(frame_path: str) -> Dict[str, Any]:
    """找人脸/上半身 → 主体框 → 景别。检不到就说 unknown。"""
    import cv2
    img = cv2.imread(frame_path, cv2.IMREAD_COLOR)
    if img is None:
        return {"found": False, "reason": "读不到帧"}
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    face_c, body_c = _cascades()
    box = None
    kind = ""
    if face_c is not None and not face_c.empty():
        faces = face_c.detectMultiScale(gray, 1.15, 5, minSize=(int(h * 0.06), int(h * 0.06)))
        if len(faces):
            x, y, fw, fh = max(faces, key=lambda b: b[2] * b[3])
            box = (int(x), int(y), int(fw), int(fh))
            kind = "face"
    if box is None and body_c is not None and not body_c.empty():
        bodies = body_c.detectMultiScale(gray, 1.1, 5, minSize=(int(h * 0.20), int(h * 0.20)))
        if len(bodies):
            x, y, bw, bh = max(bodies, key=lambda b: b[2] * b[3])
            box = (int(x), int(y), int(bw), int(bh))
            kind = "upperbody"
    if box is None:
        return {"found": False, "reason": "没检到人脸/上半身", "shot_size": "unknown",
                "shot_size_confidence": 0.0}
    x, y, bw, bh = box
    ratio = bh / float(h)
    if ratio >= 0.55:
        size = "特写"        # close-up
    elif ratio >= 0.32:
        size = "近景"        # medium close
    elif ratio >= 0.16:
        size = "中景"        # medium
    else:
        size = "全景"        # wide
    # 构图：主体在左/中/右 + 是否居中
    cx = (x + bw / 2.0) / float(w)
    comp = "左三分" if cx < 0.38 else ("右三分" if cx > 0.62 else "居中")
    if bw / float(w) < 0.12:
        comp = "远景留白"
    return {
        "found": True, "subject": kind, "box": [x, y, bw, bh],
        "subject_height_ratio": round(ratio, 3),
        "shot_size": size, "shot_size_confidence": 0.6,
        "composition": comp,
        "note": "景别由 Haar 检测到的人脸/上半身框高度占比映射；检不到就是 unknown，不猜",
    }


def camera_motion(frames: List[str]) -> Dict[str, Any]:
    """运镜估计（光流法）。特征点不足 → unknown。"""
    import cv2
    import numpy as np
    if len(frames) < 2:
        return {"motion": "unknown", "confidence": 0.0, "reason": "帧不足"}
    a = cv2.imread(frames[0], cv2.IMREAD_GRAYSCALE)
    b = cv2.imread(frames[-1], cv2.IMREAD_GRAYSCALE)
    if a is None or b is None:
        return {"motion": "unknown", "confidence": 0.0, "reason": "读不到帧"}
    if a.shape != b.shape:
        b = cv2.resize(b, (a.shape[1], a.shape[0]))
    pts = cv2.goodFeaturesToTrack(a, maxCorners=320, qualityLevel=0.01,
                                  minDistance=18, blockSize=7)
    if pts is None or len(pts) < 12:
        return {"motion": "unknown", "confidence": 0.0,
                "reason": "特征点不足（低纹理/纯色画面）—— 按纪律不猜"}
    nxt, status, _err = cv2.calcOpticalFlowPyrLK(a, b, pts, None)
    if nxt is None or status is None:
        return {"motion": "unknown", "confidence": 0.0, "reason": "光流失败"}
    ok = status.ravel() == 1
    p0 = pts.reshape(-1, 2)[ok]
    p1 = nxt.reshape(-1, 2)[ok]
    if len(p0) < 8:
        return {"motion": "unknown", "confidence": 0.0, "reason": "有效光流点太少"}
    M, inliers = cv2.estimateAffinePartial2D(p0, p1, method=cv2.RANSAC,
                                             ransacReprojThreshold=3.0,
                                             maxIters=2000, confidence=0.995)
    if M is None:
        return {"motion": "unknown", "confidence": 0.0, "reason": "仿射拟合失败"}
    inl = float(inliers.sum()) if inliers is not None else 0.0
    ratio = inl / float(len(p0))
    dx = float(M[0, 2])
    dy = float(M[1, 2])
    sc = math.sqrt(abs(M[0, 0] * M[1, 1] - M[0, 1] * M[1, 0])) or 1.0
    W = float(a.shape[1])
    rel_x, rel_y, rel_sc = abs(dx) / W, abs(dy) / float(a.shape[1]), abs(sc - 1.0)
    conf = round(min(1.0, ratio * min(1.0, len(p0) / 60.0)), 3)
    if conf < 0.25:
        return {"motion": "unknown", "confidence": conf,
                "reason": "内点比例过低，判不可靠"}
    if rel_sc > 0.06:
        motion = "push_in" if sc > 1.0 else "pull_out"
    elif rel_x > 0.035:
        motion = "pan_right" if dx > 0 else "pan_left"
    elif rel_y > 0.035:
        motion = "tilt_down" if dy > 0 else "tilt_up"
    elif rel_x > 0.012 or rel_y > 0.012:
        motion = "轻微手持/微动"
    else:
        motion = "static"
    return {"motion": motion, "confidence": conf,
            "dx": round(dx, 2), "dy": round(dy, 2), "scale": round(sc, 4),
            "inlier_ratio": round(ratio, 3), "tracked_points": int(len(p0)),
            "note": "光流 + RANSAC 仿射；低纹理画面会判 unknown（不猜）"}


def ocr_text(frame_path: str) -> Dict[str, Any]:
    """硬字幕 OCR（rapidocr；没装就跳过，不报错）。

    ★ 保留 bbox（2026-10-24 修）：原来只留文本，把位置丢了 ——
      字幕几何门（居中？底部？小号副字幕？）就永远判不了。
    """
    try:
        from rapidocr_onnxruntime import RapidOCR
    except Exception:  # noqa: BLE001
        return {"ok": False, "texts": [], "error": "没装 rapidocr（可选）"}
    try:
        engine = RapidOCR()
        res, _ = engine(frame_path)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "texts": [], "error": str(e)[:160]}
    out = []
    for item in (res or []):
        try:
            txt = str(item[1])
            conf = float(item[2])
            box = item[0]
            rect = None
            try:
                xs = [float(p[0]) for p in box]
                ys = [float(p[1]) for p in box]
                rect = {"x": round(min(xs), 1), "y": round(min(ys), 1),
                        "w": round(max(xs) - min(xs), 1), "h": round(max(ys) - min(ys), 1)}
            except Exception:  # noqa: BLE001
                rect = None
            if txt.strip():
                out.append({"text": txt.strip(), "confidence": round(conf, 3),
                            "box": rect})
        except Exception:  # noqa: BLE001
            continue
    return {"ok": True, "texts": out[:12], "error": ""}


def subtitle_geometry(texts: List[Dict[str, Any]], frame_w: int, frame_h: int) -> Dict[str, Any]:
    """从 OCR 框推字幕版式（居中？底部？宽度占比？）。"""
    if not texts or not frame_w or not frame_h:
        return {"state": "unknown", "reason": "没有 OCR 框"}
    big = max(texts, key=lambda t: ((t.get("box") or {}).get("w") or 0) *
                                    ((t.get("box") or {}).get("h") or 0))
    box = big.get("box") or {}
    if not box:
        return {"state": "unknown", "reason": "OCR 没给框"}
    cx = (box.get("x", 0) + box.get("w", 0) / 2.0) / float(frame_w)
    cy = (box.get("y", 0) + box.get("h", 0) / 2.0) / float(frame_h)
    if abs(cx - 0.5) <= 0.08:
        align = "居中"
    elif cx < 0.5:
        align = "偏左"
    else:
        align = "偏右"
    if cy >= 0.80:
        pos = "底部"
    elif cy <= 0.20:
        pos = "顶部"
    else:
        pos = "中部"
    return {"state": "ok", "align": align, "position": pos,
            "center_x": round(cx, 3), "center_y": round(cy, 3),
            "width_ratio": round(box.get("w", 0) / float(frame_w), 3),
            "height_ratio": round(box.get("h", 0) / float(frame_h), 3),
            "note": "由最大一块 OCR 文本的框推；不是逐帧时序"}


def bokeh_measure(frame_path: str) -> Dict[str, Any]:
    """浅景深估计：背景区的清晰度 vs 全图清晰度。

    ★ 做法（诚实版）：把画面按 Laplacian 方差切成「实焦区」与「虚化区」，
      虚化占比高 ⇒ 有浅景深。**这是启发式，不是真的景深测量**。
    """
    import cv2
    import numpy as np
    img = cv2.imread(frame_path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return {"state": "unknown", "reason": "读不到帧"}
    h, w = img.shape[:2]
    if h < 64 or w < 64:
        return {"state": "unknown", "reason": "帧太小"}
    k = cv2.Laplacian(img, cv2.CV_64F)
    # 8x8 网格，逐格算清晰度
    gh, gw = 8, 8
    bh, bw = h // gh, w // gw
    sharp = []
    for r in range(gh):
        for c in range(gw):
            blk = k[r * bh:(r + 1) * bh, c * bw:(c + 1) * bw]
            sharp.append(float(np.abs(blk).mean()))
    sharp = np.array(sharp)
    thr = float(np.percentile(sharp, 40))
    soft_ratio = float((sharp <= thr).mean())
    spread = float(sharp.std() / (sharp.mean() + 1e-6))
    return {"state": "ok", "soft_area_ratio": round(soft_ratio, 3),
            "sharpness_spread": round(spread, 3),
            "verdict": ("有浅景深迹象" if soft_ratio >= 0.35 and spread >= 0.6
                        else "景深偏平（全画面较均匀）"),
            "note": "启发式（网格清晰度分布），**不是真景深测量**"}


def audio_envelope(wav_path: str, *, bucket: float = 0.5) -> Dict[str, Any]:
    """音频 RMS 包络（每 bucket 秒一格）—— 情绪曲线的素材。

    ★ 用 ffmpeg 直出 PCM 读，**不装 pydub**（本机 Python 3.14 移除了 audioop，
      pydub import 会炸，见 research-video-remake.md §3）。
    """
    import struct
    exe = _which("ffmpeg")
    if not exe:
        return {"state": "unknown", "reason": "找不到 ffmpeg"}
    code, _o, err = _run([exe, "-v", "error", "-i", wav_path, "-f", "s16le",
                          "-acodec", "pcm_s16le", "-ac", "1", "-ar", "8000", "-"], timeout=600)
    if code != 0:
        return {"state": "unknown", "reason": (err or "")[:140]}
    raw = _o.encode("latin-1", errors="ignore") if isinstance(_o, str) else b""
    if not raw:
        return {"state": "unknown", "reason": "没有 PCM 输出"}
    n = len(raw) // 2
    if n < 800:
        return {"state": "unknown", "reason": "音频太短"}
    import array
    arr = array.array("h")
    arr.frombytes(raw[:n * 2])
    if sys.byteorder == "big":
        arr.byteswap()
    step = max(1, int(8000 * bucket))
    env: List[Dict[str, Any]] = []
    for i in range(0, len(arr) - step, step):
        chunk = arr[i:i + step]
        if not chunk:
            break
        s = sum(float(x) * float(x) for x in chunk) / float(len(chunk))
        rms = (s ** 0.5) / 32768.0
        env.append({"t": round(i / 8000.0, 2), "rms": round(rms, 4)})
    if not env:
        return {"state": "unknown", "reason": "没有算出包络"}
    vals = [e["rms"] for e in env]
    mx = max(vals) or 1e-9
    peak_t = env[vals.index(mx)]["t"]
    return {"state": "ok", "bucket_sec": bucket, "points": env[:600],
            "count": len(env), "peak": round(mx, 4), "peak_at": peak_t,
            "mean": round(sum(vals) / len(vals), 4),
            "note": "RMS 能量包络；peak_at 可用来定位爆点/情绪高点"}


# ══════════════════════════════════════════════════════════════════
# ⑥ 节奏
# ══════════════════════════════════════════════════════════════════


def pacing(shots: List[Dict[str, Any]], duration: float,
           style_profile: str = "life-stream-cinematic") -> Dict[str, Any]:
    """节奏指标 + 与 cinematic-film-style skill 的「电影感门」对表。

    ★ 修掉的 bug（2026-10-24）：原来只有二值 pass，缺 unknown/skipped 三态 ——
      同一个阈值对两套风格档是不同的（横屏生活流 1.5–3s，竖屏年代感 2–4s），
      而规格里没有 style_profile，代码也不知道用哪把尺子，于是两套都判 fail。
      现在门按档取阈值，并显式区分 `pass` / `fail` / `unknown` / `skipped`。
    """
    ds = sorted(float(s.get("duration") or 0) for s in shots)
    n = len(ds)
    profs = style_profiles()
    prof = profs.get(style_profile) or profs.get("life-stream-cinematic") or {}
    lo, hi = prof.get("shot_mean_gate", (1.5, 3.0))
    if lo is None:
        lo = 1.5
    if hi is None:
        hi = 3.0
    med_gate = prof.get("shot_median_gate")     # 可能是 None（该档没这条阈值）
    max_gate = float(prof.get("shot_max_gate") or 8.0)
    if n == 0:
        return {"mean_shot": 0.0, "median_shot": 0.0, "max_shot": 0.0,
                "cuts_per_min": 0.0, "pace_class": "unknown", "style_profile": style_profile,
                "gates": []}
    mean = sum(ds) / n
    mid = ds[n // 2] if n % 2 else (ds[n // 2 - 1] + ds[n // 2]) / 2.0
    cpm = (n - 1) / (duration / 60.0) if duration > 0 else 0.0
    if mean <= 2.0:
        pace = "快切"
    elif mean <= 3.5:
        pace = "中速"
    elif mean <= 6.0:
        pace = "偏慢"
    else:
        pace = "长镜为主"
    gates = [
        {"id": "P1", "title": "平均镜头 %.1f–%.1f 秒" % (lo, hi),
         "value": round(mean, 2), "state": "pass" if lo <= mean <= hi else "fail",
         "target": "%.1f–%.1fs" % (lo, hi)},
        {"id": "P2", "title": "中位镜头 ≤%.1f 秒" % med_gate if med_gate else "中位镜头（该档无阈值）",
         "value": round(mid, 2),
         "state": ("pass" if mid <= med_gate else "fail") if med_gate else "skipped",
         "target": ("≤%.1fs" % med_gate) if med_gate else "该档不设此门"},
        {"id": "P3", "title": "单镜 ≤%.1f 秒（情绪长镜除外）" % max_gate,
         "value": round(max(ds), 2), "state": "pass" if max(ds) <= max_gate else "fail",
         "target": "≤%.1fs" % max_gate},
        {"id": "P4", "title": "爆点在最后 15–25%",
         "value": "n/a", "state": "unknown", "target": "15–25%",
         "note": "爆点位置要人判断，机器不判"},
    ]
    return {"mean_shot": round(mean, 2), "median_shot": round(mid, 2),
            "max_shot": round(max(ds), 2), "min_shot": round(min(ds), 2),
            "shots": n, "cuts_per_min": round(cpm, 1),
            "pace_class": pace, "style_profile": style_profile,
            "style_label": prof.get("label"),
            "gates": gates,
            "note": "门按风格档取阈值；state 三态：pass / fail / unknown（unknown=机器不判）"}


def dialogue_stats(segments: List[Dict[str, Any]], duration: float) -> Dict[str, Any]:
    """台词节奏三指标（调研指出这三项才是「台词节奏可复刻」的核心）。"""
    segs = [s for s in (segments or []) if str(s.get("text") or "").strip()]
    if not segs:
        return {"lines": 0, "chars": 0, "chars_per_sec": 0.0,
                "avg_line_chars": 0.0, "chars_per_min": 0.0, "line_len_cv": 0.0}
    lens = [len(str(s.get("text") or "")) for s in segs]
    chars = sum(lens)
    dur = float(duration or 0) or 1.0
    avg = chars / float(len(lens))
    var = sum((x - avg) ** 2 for x in lens) / float(len(lens))
    cv = math.sqrt(var) / avg if avg > 0 else 0.0
    return {
        "lines": len(segs), "chars": chars,
        "chars_per_sec": round(chars / dur, 2),
        "chars_per_min": round(chars / dur * 60.0, 1),
        "avg_line_chars": round(avg, 1),
        "line_len_cv": round(cv, 3),
        "note": "line_len_cv 低（<0.35）= 句长过于均匀 = AI 味的一个信号",
    }


# ══════════════════════════════════════════════════════════════════
# ⑦ 提示词反推（离线模板；可选多模态增强）
# ══════════════════════════════════════════════════════════════════

_SHOT_SIZE_EN = {"特写": "extreme close-up", "近景": "close-up",
                 "中景": "medium shot", "全景": "wide shot", "unknown": ""}
_CAMERA_EN = {"push_in": "slow dolly in", "pull_out": "slow dolly out",
              "pan_left": "slow pan left", "pan_right": "slow pan right",
              "tilt_up": "tilt up", "tilt_down": "tilt down",
              "轻微手持/微动": "subtle handheld drift", "static": "locked-off static shot"}
#: 运动标签 → 规范英文（比对运镜白/黑名单要用统一词表）
CAMERA_CANON = {"push_in": "push_in", "pull_out": "pull_out",
                "pan_left": "pan_left", "pan_right": "pan_right",
                "tilt_up": "tilt_up", "tilt_down": "tilt_down",
                "轻微手持/微动": "handheld", "static": "static",
                "unknown": "unknown"}
#: 运镜白名单 / 黑名单（来自 cinematic-film-style skill 的档 A）
CAMERA_WHITELIST = ("static", "handheld", "push_in", "pull_out",
                    "pan_left", "pan_right", "tilt_up", "tilt_down")
CAMERA_BLACKLIST = ("whip_pan", "orbit", "drone")
_PALETTE_EN = {"暖橙vs青蓝对撞": "warm amber key light against cool teal fill",
               "暖调": "warm amber tone", "冷青蓝": "cool teal-blue tone",
               "低饱和中性": "desaturated neutral tone"}

#: 两套风格档 —— **数值不在这里**！
#: `skills_builtin/cinematic-film-style/rules.json` 里的 `style_profiles` 与
#: `measurable_gates` 是唯一真相源（那个 skill 自己写着：
#: 「剧本生成与 core/video_remake.py 必须引用同一份 rules.json；
#:   不允许在代码里再抄一份常量」）。
#: 下面这份 FALLBACK 只在**读不到那个 skill** 时兜底（并会在结果里标明
#: `rules_source="fallback"`，不冒充是 skill 的规则）。
_FALLBACK_PROFILES = {
    "life-stream-cinematic": {
        "label": "生活流电影感", "aspect": "16:9 1280x720 30fps",
        "shot_mean_gate": (1.5, 3.0), "shot_median_gate": 2.5, "shot_max_gate": 8.0,
        "camera_blacklist": list(CAMERA_BLACKLIST),
    },
    "era-texture": {
        "label": "年代质感", "aspect": "9:16 1080x1920",
        "shot_mean_gate": (2.0, 4.0), "shot_median_gate": 3.0, "shot_max_gate": 8.0,
        "camera_blacklist": [],
    },
}
_PROFILES_CACHE: Optional[Dict[str, Any]] = None


def style_profiles() -> Dict[str, Any]:
    """从 cinematic-film-style 的 rules.json 读两套风格档（带缓存）。

    返回：`{"life-stream-cinematic": {...}, "era-texture": {...}, "_source": "rules.json"|"fallback"}`
    """
    global _PROFILES_CACHE
    if _PROFILES_CACHE is not None:
        return _PROFILES_CACHE
    out: Dict[str, Any] = {}
    src = "fallback"
    try:
        from .skills import builtin_rules
        rules = builtin_rules("cinematic-film-style")
        raw = (rules or {}).get("style_profiles") or {}
        gates = {g.get("name"): g for g in (rules or {}).get("measurable_gates") or []
                 if isinstance(g, dict)}
        for key, prof in raw.items():
            if not isinstance(prof, dict):
                continue
            canvas = prof.get("canvas") or {}
            aspect = str(canvas.get("aspect_ratio") or "")
            res = str(canvas.get("resolution") or "")
            fps = int(canvas.get("fps") or 0)
            g1 = (gates.get("shot_length_mean") or {}).get("threshold_by_profile") or {}
            g2 = (gates.get("shot_length_median") or {}).get("threshold_by_profile") or {}
            g3 = (gates.get("shot_length_max") or {}).get("threshold_by_profile") or {}
            t1 = (g1.get(key) or {})
            t2 = (g2.get(key) or {})
            t3 = (g3.get(key) or {})
            mean_lo = float(t1.get("min") or 0) or None
            mean_hi = float(t1.get("max") or 0) or None
            med_hi = float(t2.get("max") or 0) or None
            max_hi = float(t3.get("max") or 0) or None
            bits = [x for x in (aspect, res) if x]
            if fps:
                bits.append("%dfps" % fps)
            out[key] = {
                "label": str(prof.get("name_zh") or key),
                "aspect": " ".join(bits),
                "shot_mean_gate": (mean_lo, mean_hi),
                "shot_median_gate": med_hi,
                "shot_max_gate": max_hi,
                "camera_blacklist": list(CAMERA_BLACKLIST),
                "summary": str(prof.get("summary_zh") or "")[:120],
            }
        if out:
            src = "rules.json"
    except Exception as e:  # noqa: BLE001
        logger.warning("读 cinematic-film-style 规则失败，用兜底：%s", e)
    if not out:
        out = {k: dict(v) for k, v in _FALLBACK_PROFILES.items()}
        src = "fallback"
    out["_source"] = src
    _PROFILES_CACHE = out
    return out


def detect_style_profile(width: int, height: int) -> str:
    """按画幅猜风格档（横屏→生活流电影感，竖屏→年代质感）。"""
    try:
        return "life-stream-cinematic" if int(width or 0) >= int(height or 0) else "era-texture"
    except Exception:  # noqa: BLE001
        return "life-stream-cinematic"


def describe_shot(shot: Dict[str, Any], *, global_palette: str = "",
                  style_profile: str = "life-stream-cinematic") -> Dict[str, Any]:
    """把量化结果拼成一句**结构化**描述 + 一段提示词。

    ★ 修掉的 bug（2026-10-24，cinematic 队友复核发现）：
      原来结尾硬编码 `vertical 9:16` —— 16:9 的成片会被写成竖屏提示词。
      现在按**实际画幅 + 风格档**出，不猜。
    """
    size = shot.get("shot_size") or "unknown"
    motion = shot.get("camera_motion") or "unknown"
    color = shot.get("color") or {}
    comp = shot.get("composition") or ""
    palette = color.get("palette") or global_palette
    profs = style_profiles()
    prof = profs.get(style_profile) or profs.get("life-stream-cinematic") or {}
    bits: List[str] = []
    if size != "unknown":
        bits.append("景别%s" % size)
    if motion != "unknown":
        bits.append("运镜%s" % motion)
    if palette:
        bits.append("色调%s" % palette)
    if comp:
        bits.append("构图%s" % comp)
    desc = "、".join(bits) if bits else "画面信息不足（未检出人脸/特征点）"
    en: List[str] = []
    if size != "unknown":
        en.append(_SHOT_SIZE_EN.get(size, ""))
    if motion != "unknown":
        en.append(_CAMERA_EN.get(motion, ""))
    if palette:
        en.append(_PALETTE_EN.get(palette, ""))
    if color.get("saturation") is not None:
        en.append("low saturation, crushed shadows" if color.get("saturation", 1) < 0.45
                  else "natural saturation")
    aspect = str(prof.get("aspect") or "")
    prompt = (", ".join([x for x in en if x]) +
              ", shallow depth of field, natural light only, no camera tricks, "
              + aspect)
    return {"description": desc, "prompt": prompt.strip(),
            "prompt_hint": "参考该镜的景别/运镜/色调/构图四要素；人物与剧情另行设计",
            "note": "台词只用于理解意图，**绝不写进画面提示词**"}


def vision_describe(keyframe: str, *, api_key: str = "", model: str = "Qwen/Qwen3-VL-8B-Instruct",
                    timeout: int = 180) -> Dict[str, Any]:
    """可选：多模态反推（花钱）。失败就返回空，由调用方回落到离线模板。"""
    import base64
    key = (api_key or "").strip()
    if not key:
        return {"ok": False, "description": "", "error": "没有硅基流动 Key"}
    try:
        import requests
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "description": "", "error": str(e)}
    try:
        with open(keyframe, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "description": "", "error": str(e)}
    prompt = ("Describe this film frame for recreation. Output JSON with keys: "
              "subject, scene, shot_size, lighting, color, composition, mood, "
              "action. Be concrete and visual, no speculation.")
    body = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64,%s" % b64}},
            ],
        }],
        "temperature": 0.2,
        "max_tokens": 700,
    }
    try:
        r = requests.post("https://api.siliconflow.cn/v1/chat/completions",
                          headers={"Authorization": "Bearer %s" % key,
                                   "Content-Type": "application/json"},
                          json=body, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "description": "", "error": str(e)[:160]}
    if r.status_code != 200:
        return {"ok": False, "description": "", "error": "HTTP %s" % r.status_code}
    try:
        txt = r.json()["choices"][0]["message"]["content"]
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "description": "", "error": str(e)[:160]}
    return {"ok": True, "description": str(txt), "error": "", "model": model}


# ══════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════

COMPLIANCE = {
    "can_replicate": ["节奏（镜头时长分布/切点密度）", "景别序列", "运镜类型序列",
                      "色调数值（亮度/饱和/对比/主色相）", "构图法则",
                      "台词节奏与字数密度（语速/句长分布/字数密度）"],
    "must_not_replicate": ["原片角色肖像与身份", "逐字台词与歌词",
                           "原片具体镜头画面与美术设计", "原片音乐/音效/字体素材",
                           "logo 与水印"],
    "one_liner": ("复刻节奏、镜头结构与色调这类『通用手法』；"
                  "不复制原片的人物形象、逐字台词与具体美术设计。"),
    "basis": ("17 U.S.C. §102；美国版权局 Compendium §310.2「版权局不看风格」、"
              "§310.4「look and feel 不受保护」—— 非法律意见"),
}


def analyze(video_path: str, *, out_dir: str = "", project_id: str = "", db=None,
            use_siliconflow: bool = True, use_whisper: bool = True,
            whisper_size: str = "small", use_ocr: bool = True,
            vision: bool = False, max_shots: int = 0,
            threshold: int = 10,
            progress: Optional[Callable[[str, float], None]] = None) -> Dict[str, Any]:
    """跑完整条复刻流水线。

    返回一个**可序列化的规格**（spec），含 shots[]、全局节奏/色调、台词统计与合规边界。
    """
    def tick(msg: str, pct: float):
        if progress:
            try:
                progress(msg, pct)
            except Exception:  # noqa: BLE001
                pass
        logger.info("[remake] %s (%.0f%%)", msg, pct * 100)

    path = str(video_path or "").strip()
    if not path or not os.path.isfile(path):
        return {"ok": False, "error": "视频文件不存在：%s" % (path or "(空)")}
    if not out_dir:
        out_dir = os.path.join(tempfile.gettempdir(), "vf-remake",
                               os.path.splitext(os.path.basename(path))[0])
    os.makedirs(out_dir, exist_ok=True)
    warnings: List[str] = []

    tick("探测视频", 0.05)
    pr = probe(path)
    if not pr.get("ok"):
        return {"ok": False, "error": pr.get("error") or "探测失败"}

    tick("检测镜头边界", 0.15)
    sd = detect_shots(path, threshold=threshold)
    if not sd.get("ok"):
        return {"ok": False, "error": sd.get("error") or "镜头检测失败"}
    shots = sd.get("shots") or []
    if max_shots and len(shots) > max_shots:
        shots = shots[:max_shots]
        warnings.append("按 max_shots 截断到前 %d 镜" % max_shots)
    duration = float(pr.get("duration") or 0)

    tick("抽取关键帧", 0.30)
    kf = extract_keyframes(path, shots, os.path.join(out_dir, "keyframes"))
    if not kf.get("ok"):
        warnings.append("部分关键帧抽取失败：%s" % (kf.get("error") or "")[:120])

    tick("量化画面（色调/景别/运镜/构图）", 0.42)
    style_profile = detect_style_profile(int(pr.get("width") or 0), int(pr.get("height") or 0))
    fw = int(pr.get("width") or 0)
    fh = int(pr.get("height") or 0)
    for i, sh in enumerate(shots):
        frames = sh.get("keyframes") or []
        if frames:
            mid = frames[len(frames) // 2]
            sh["color"] = color_stats(mid)
            sub = detect_subject(mid)
            sh["shot_size"] = sub.get("shot_size", "unknown")
            sh["shot_size_confidence"] = sub.get("shot_size_confidence", 0.0)
            sh["composition"] = sub.get("composition", "")
            sh["subject"] = {k: v for k, v in sub.items() if k not in ("shot_size",)}
            cam = camera_motion(frames)
            sh["camera_motion"] = cam.get("motion", "unknown")
            sh["camera_confidence"] = cam.get("confidence", 0.0)
            sh["camera_detail"] = cam
            sh["camera_canonical"] = CAMERA_CANON.get(sh["camera_motion"], "unknown")
            sh["camera_allowed"] = (
                "unknown" if sh["camera_canonical"] == "unknown"
                else ("pass" if sh["camera_canonical"] in CAMERA_WHITELIST else "fail"))
            sh["bokeh"] = bokeh_measure(mid)
            if use_ocr:
                oc = ocr_text(mid)
                if oc.get("ok"):
                    sh["on_screen_text"] = [t["text"] for t in oc.get("texts", [])][:8]
                    sh["subtitle_geometry"] = subtitle_geometry(oc.get("texts", []), fw, fh)
                else:
                    sh["on_screen_text"] = []
                    sh["subtitle_geometry"] = {"state": "unknown",
                                               "reason": oc.get("error") or "OCR 不可用"}
            else:
                sh["on_screen_text"] = []
                sh["subtitle_geometry"] = {"state": "skipped", "reason": "use_ocr=False"}
        else:
            sh["color"] = {"ok": False}
            sh["shot_size"] = "unknown"
            sh["camera_motion"] = "unknown"
            sh["camera_confidence"] = 0.0
            sh["camera_canonical"] = "unknown"
            sh["camera_allowed"] = "unknown"
            sh["on_screen_text"] = []
            sh["subtitle_geometry"] = {"state": "unknown", "reason": "没有关键帧"}
        tick("量化画面 %d/%d" % (i + 1, len(shots)), 0.42 + 0.28 * (i + 1) / max(1, len(shots)))

    tick("提取台词（ASR）", 0.74)
    dialogue: List[Dict[str, Any]] = []
    sf_text = ""
    asr_info: Dict[str, Any] = {"enabled": False}
    wav = os.path.join(out_dir, "audio.wav")
    env: Dict[str, Any] = {"state": "skipped", "reason": "没有音轨"}
    if use_whisper or use_siliconflow:
        try:
            exe = _which("ffmpeg")
            if exe:
                code, _o, e = _run([exe, "-y", "-loglevel", "error", "-i", path,
                                    "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav],
                                   timeout=600)
                if code != 0 or not os.path.isfile(wav):
                    warnings.append("抽音轨失败：%s" % (e or "")[:120])
                else:
                    env = audio_envelope(wav)
                    from . import asr_siliconflow as ASR
                    if use_whisper:
                        r = ASR.transcribe_timed(wav, whisper_size=whisper_size,
                                                 use_siliconflow=use_siliconflow,
                                                 project_id=project_id, db=db)
                        dialogue = r.get("segments") or []
                        sf_text = r.get("siliconflow_text") or ""
                        asr_info = {"ok": r.get("ok"), "whisper_model": r.get("whisper_model"),
                                    "siliconflow_text_len": len(sf_text),
                                    "cost_yuan": r.get("cost_yuan"),
                                    "error": r.get("error")}
                        if r.get("error"):
                            warnings.append("ASR：%s" % str(r.get("error"))[:160])
                    elif use_siliconflow:
                        r = ASR.transcribe(wav, project_id=project_id, db=db)
                        sf_text = r.get("text") or ""
                        asr_info = {"ok": r.get("ok"), "cost_yuan": r.get("cost_yuan"),
                                    "error": r.get("error")}
        except Exception as e:  # noqa: BLE001
            warnings.append("ASR 异常：%s" % str(e)[:160])
    if not dialogue and sf_text:
        # 没有时间戳（whisper 没跑成）→ 整段文本塞进第一镜，标明没有时间对齐
        dialogue = [{"start": 0.0, "end": duration, "text": sf_text, "aligned": False}]

    # 台词落到镜头上
    for sh in shots:
        s, e = float(sh.get("start") or 0), float(sh.get("end") or 0)
        sh["dialogue"] = [dict(d, aligned=bool(d.get("aligned", True)))
                          for d in dialogue
                          if float(d.get("start") or 0) < e and float(d.get("end") or 0) > s]
    tick("汇总节奏", 0.86)
    pac = pacing(shots, duration, style_profile)
    dstat = dialogue_stats(dialogue, duration)

    tick("组装复刻规格", 0.92)
    gpalette = ""
    try:
        cols = [sh.get("color") or {} for sh in shots if (sh.get("color") or {}).get("ok")]
        if cols:
            gpalette = max(cols, key=lambda c: c.get("warm_share", 0) + c.get("cool_share", 0)
                           ).get("palette", "")
    except Exception:  # noqa: BLE001
        gpalette = ""
    for sh in shots:
        d = describe_shot(sh, global_palette=gpalette, style_profile=style_profile)
        sh["description"] = d["description"]
        sh["prompt"] = d["prompt"]
        sh["prompt_hint"] = d["prompt_hint"]
        if vision and sh.get("keyframes"):
            vd = vision_describe(sh["keyframes"][len(sh["keyframes"]) // 2])
            if vd.get("ok"):
                sh["vision_description"] = vd.get("description", "")

    spec = {
        "ok": True, "error": "",
        "source": {
            "path": path, "duration": duration, "fps": pr.get("fps"),
            "width": pr.get("width"), "height": pr.get("height"),
            "codec": pr.get("codec"), "has_audio": pr.get("has_audio"),
            "aspect": ("竖屏 %.0f:%.0f" % (pr.get("width") or 0, pr.get("height") or 0)
                       if (pr.get("height") or 0) > (pr.get("width") or 0) else
                       "横屏 %.0f:%.0f" % (pr.get("width") or 0, pr.get("height") or 0)),
        },
        "style_profile": style_profile,
        "style_profile_label": (style_profiles().get(style_profile) or {}).get("label", ""),
        "style_rules_source": style_profiles().get("_source", "fallback"),
        "shots": shots,
        "pacing": pac,
        "dialogue_stats": dstat,
        "audio_envelope": env,
        "global_palette": gpalette,
        "asr": asr_info,
        "siliconflow_full_text": sf_text,
        "compliance": COMPLIANCE,
        "warnings": warnings,
        "out_dir": out_dir,
        "note": ("这份规格复刻的是**手法**（节奏/景别/运镜/色调/构图/台词密度），"
                 "不是内容。落地时重新设计人物与剧情，别复制原片。"),
    }
    try:
        with open(os.path.join(out_dir, "remake_spec.json"), "w", encoding="utf-8") as f:
            json.dump(spec, f, ensure_ascii=False, indent=2)
        spec["spec_path"] = os.path.join(out_dir, "remake_spec.json")
    except Exception as e:  # noqa: BLE001
        warnings.append("规格落盘失败：%s" % e)
    tick("完成", 1.0)
    return spec
