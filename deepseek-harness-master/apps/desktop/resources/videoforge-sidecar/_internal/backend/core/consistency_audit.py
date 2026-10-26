# -*- coding: utf-8 -*-
"""VideoForge · 出片后的**视觉一致性自检**（advisory，不阻断）

═══════════════════════════════════════════════════════════════════
为什么需要这个文件
═══════════════════════════════════════════════════════════════════
用户的原话（第 4 条需求）：「角色 + 场景 + 声音一致性」。

声音那条已经落在 `voicecast` / `dubbing`（每个角色固定音色、落库、按台词合成）。
**画面这条原来只有"出图之前"的提示词体检**：

  · `agent_tools_skills.audit_character_image` —— 出图前**审提示词**，
    而且工具描述里明写「**不阻断出图**」；
  · `audit_character_batch` —— 比的是**角色卡的文本字段**，不是画面。

也就是说：**视频真出来之后，脸到底漂没漂，此前完全没人量。**
用户只能人眼一帧帧看 —— 12 个镜头 60 秒的片子看完一遍要十几分钟。

本模块补上这一环：拿**角色参考图**当基准，去和**每个镜头真实抽帧**里检出的人脸
做客观比对，把"哪一镜最不像"排在前面。

═══════════════════════════════════════════════════════════════════
它能量什么、不能量什么（先说清楚，别当成"人脸识别"）
═══════════════════════════════════════════════════════════════════
能：
  · 检出画面里**有没有脸**、脸有多大、在哪个位置（Haar 级联，cv2 自带）；
  · 脸区域与参考图的**感知相似度**（HSV 直方图相关性 + ORB 局部特征匹配）；
  · 跨镜头**排序**：哪一镜相对最不像（这正是人眼要花时间做的事）。

不能：
  · 不能判定"这是不是同一个人"（没有身份嵌入/人脸识别模型）；
  · 侧脸、遮挡、小脸、暗光会显著拉低分数 → **误报**；
  · 参考图是半身/全身时，人脸区域很小，基准本身就不稳。

所以结论一律写成"**建议复核**"，不写成"错了"。这是**辅助排序**，不是判决。
分数低于阈值时也不要自动重出片 —— 重出要花钱，必须人来决定。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("videoforge.consistency_audit")

#: 低于这个分数就进"建议复核"清单。0~1，越高越像。
SUSPECT_BELOW = 0.45
#: 单镜最多抽几帧（多了浪费，少了漏）。
MAX_FRAMES_PER_SHOT = 3


# ──────────────────────────── 基础工具 ────────────────────────────


def _cv2():
    try:
        import cv2  # noqa: F401
        return cv2
    except Exception as e:  # noqa: BLE001
        logger.warning("没有 cv2，视觉一致性自检不可用：%s", e)
        return None


def _face_cascade(cv2):
    """加载 OpenCV 自带的人脸级联（不联网、不额外下载）。"""
    try:
        p = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
        c = cv2.CascadeClassifier(p)
        return c if (c is not None and not c.empty()) else None
    except Exception as e:  # noqa: BLE001
        logger.warning("加载人脸级联失败：%s", e)
        return None


def _read_image(path: str):
    cv2 = _cv2()
    if not cv2 or not path or not os.path.exists(path):
        return None
    try:
        # 中文路径必须走 imdecode（cv2.imread 在 Windows 上读不了非 ASCII 路径）
        import numpy as np
        data = np.fromfile(path, dtype=np.uint8)
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    except Exception as e:  # noqa: BLE001
        logger.warning("读图失败 %s：%s", path, e)
        return None


def _extract_frames(video: str, count: int, out_dir: str) -> List[str]:
    """从视频里**均匀**抽 `count` 帧（避开首尾 5%，片头片尾常有黑场/转场）。"""
    try:
        from core.ffmpeg_util import ffmpeg_path  # type: ignore
    except Exception:
        ffmpeg_path = lambda: "ffmpeg"  # noqa: E731
    dur = _probe_duration(video)
    if dur <= 0.2:
        return []
    outs: List[str] = []
    for i in range(count):
        pos = dur * (0.08 + 0.84 * (i + 0.5) / max(1, count))
        out = os.path.join(out_dir, f"f{i:02d}.jpg")
        cmd = [ffmpeg_path(), "-y", "-v", "error", "-ss", f"{pos:.3f}",
               "-i", video, "-frames:v", "1", "-q:v", "2", out]
        try:
            subprocess.run(cmd, capture_output=True, timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:  # noqa: BLE001
            logger.warning("抽帧失败 %s @%.2fs：%s", video, pos, e)
            continue
        if os.path.exists(out) and os.path.getsize(out) > 2000:
            outs.append(out)
    return outs


def _probe_duration(video: str) -> float:
    try:
        from core.media_probe import probe_duration  # type: ignore
        return float(probe_duration(video) or 0.0)
    except Exception:
        pass
    try:
        from core.ffmpeg_util import ffmpeg_path  # type: ignore
    except Exception:
        return 0.0
    try:
        r = subprocess.run([ffmpeg_path(), "-hide_banner", "-i", video],
                           capture_output=True, text=True, timeout=40,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for line in (r.stderr or "").splitlines():
            if "Duration:" in line:
                hms = line.split("Duration:")[1].split(",")[0].strip()
                h, mn, s = hms.split(":")
                return int(h) * 3600 + int(mn) * 60 + float(s)
    except Exception:
        pass
    return 0.0


# ──────────────────────────── 人脸与相似度 ────────────────────────────


def _detect_faces(cv2, cascade, img) -> List[Tuple[int, int, int, int]]:
    if img is None or cascade is None:
        return []
    try:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        gray = cv2.equalizeHist(gray)
        faces = cascade.detectMultiScale(gray, scaleFactor=1.12,
                                         minNeighbors=5, minSize=(28, 28))
        return [tuple(int(x) for x in f) for f in faces]
    except Exception as e:  # noqa: BLE001
        logger.warning("人脸检测失败：%s", e)
        return []


def _largest(faces: List[Tuple[int, int, int, int]]):
    return max(faces, key=lambda f: f[2] * f[3]) if faces else None


def _crop(img, box):
    x, y, w, h = box
    H, W = img.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    return img[y0:y1, x0:x1]


def _hist_similarity(cv2, a, b) -> float:
    """HSV 直方图相关性（0~1）。对光照/像素级抖动比较稳，对"换个人"敏感度一般。"""
    try:
        import numpy as np
        ha = cv2.calcHist([cv2.cvtColor(a, cv2.COLOR_BGR2HSV)],
                          [0, 1], None, [32, 32], [0, 180, 0, 256])
        hb = cv2.calcHist([cv2.cvtColor(b, cv2.COLOR_BGR2HSV)],
                          [0, 1], None, [32, 32], [0, 180, 0, 256])
        cv2.normalize(ha, ha, 0, 1, cv2.NORM_MINMAX)
        cv2.normalize(hb, hb, 0, 1, cv2.NORM_MINMAX)
        v = float(cv2.compareHist(ha, hb, cv2.HISTCMP_CORREL))
        return max(0.0, min(1.0, (v + 1.0) / 2.0 if v < 0 else v))
    except Exception:
        return 0.0


def _orb_similarity(cv2, a, b) -> float:
    """ORB 局部特征匹配得分（0~1）。需要两张图尺寸可比，先统一到 128x128。"""
    try:
        orb = cv2.ORB_create(nfeatures=400)
        size = (128, 128)
        ga = cv2.resize(cv2.cvtColor(a, cv2.COLOR_BGR2GRAY), size)
        gb = cv2.resize(cv2.cvtColor(b, cv2.COLOR_BGR2GRAY), size)
        ka, da = orb.detectAndCompute(ga, None)
        kb, db_ = orb.detectAndCompute(gb, None)
        if da is None or db_ is None or len(ka) < 8 or len(kb) < 8:
            return 0.0
        bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
        ms = bf.match(da, db_)
        if not ms:
            return 0.0
        good = [m for m in ms if m.distance < 64]
        # 归一化：好匹配数 / min(特征点数)；一般同一个人 0.15~0.5，不同人 <0.08
        return max(0.0, min(1.0, len(good) / float(max(8, min(len(ka), len(kb)))) * 1.6))
    except Exception:
        return 0.0


def _blend(h: float, o: float) -> float:
    """直方图稳、ORB 判别力强。7:3 混，避免单一指标极端。"""
    return round(0.7 * h + 0.3 * o, 4)


# ──────────────────────────── 主流程 ────────────────────────────


def _chars_with_ref(db, pid: str) -> List[Dict[str, Any]]:
    out = []
    for c in (db.list_characters(pid) or []):
        ref = str(c.get("reference_image_path") or "").strip()
        if ref and os.path.exists(ref):
            out.append({"id": c.get("id"), "name": c.get("name"), "ref": ref})
    return out


def _shot_clip(shot: Dict[str, Any]) -> str:
    """取这一镜**已选中**的候选文件（没选中的不算成片素材）。"""
    cands = shot.get("candidates") or []
    if isinstance(cands, str):
        try:
            cands = json.loads(cands or "[]")
        except Exception:
            cands = []
    if not isinstance(cands, list):
        return ""
    sel = str(shot.get("selected_candidate_id") or "")
    for c in cands:
        if not isinstance(c, dict):
            continue
        if sel and str(c.get("candidate_id")) != sel:
            continue
        p = str(c.get("path") or "")
        if p and os.path.exists(p):
            return p
    for c in cands:
        if isinstance(c, dict):
            p = str(c.get("path") or "")
            if p and os.path.exists(p):
                return p
    return ""


def audit_visual_consistency(db, pid: str,
                             max_frames: int = MAX_FRAMES_PER_SHOT,
                             suspect_below: float = SUSPECT_BELOW) -> Dict[str, Any]:
    """对**已出片**的镜头做角色一致性自检。只读、不花钱、不阻断。"""
    cv2 = _cv2()
    if not cv2:
        return {"ok": False,
                "error": "本机没装 opencv（cv2），无法做视觉一致性自检。"
                         "装法：python -m pip install opencv-python-headless"}
    cascade = _face_cascade(cv2)
    if cascade is None:
        return {"ok": False, "error": "加载 OpenCV 人脸级联失败，无法自检。"}

    chars = _chars_with_ref(db, pid)
    if not chars:
        return {"ok": False,
                "error": "这个项目里没有「有参考图」的角色，没有基准可比。"
                         "先给角色出定妆照（make_looks）再来自检。"}
    shots = [s for s in (db.list_shots(pid) or [])
             if isinstance(s, dict) and _shot_clip(s)]
    if not shots:
        return {"ok": False,
                "error": "这个项目还没有任何**已出片**的镜头，没有画面可比。"}

    import numpy as np

    # 参考图为基准：基准图里检不出脸 → 该角色无法比对（如实说）
    refs: Dict[str, Dict[str, Any]] = {}
    unmeasurable: List[str] = []
    for c in chars:
        img = _read_image(c["ref"])
        f = _largest(_detect_faces(cv2, cascade, img)) if img is not None else None
        if f is None:
            unmeasurable.append(c["name"])
            continue
        refs[c["name"]] = {"crop": _crop(img, f), "path": c["ref"],
                           "face_px": int(f[2] * f[3])}

    if not refs:
        return {"ok": False,
                "error": "参考图里都检不出人脸（可能是全身/远景/侧脸/画风非写实），"
                         "无法作为基准比对。",
                "unmeasurable": unmeasurable}

    # 逐镜抽帧 → 检脸 → 与**这一镜关联的角色**比
    per_shot: List[Dict[str, Any]] = []
    suspects: List[Dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="vf_consist_") as td:
        for s in shots:
            clip = _shot_clip(s)
            frames = _extract_frames(clip, max_frames,
                                     td) if clip.lower().endswith((".mp4", ".mov", ".mkv")) \
                else [clip]
            best: Dict[str, Any] = {}
            no_face = 0
            for fp in frames:
                img = _read_image(fp)
                if img is None:
                    continue
                f = _largest(_detect_faces(cv2, cascade, img))
                if f is None:
                    no_face += 1
                    continue
                crop = _crop(img, f)
                if crop is None:
                    continue
                for name, base in refs.items():
                    if base["crop"] is None:
                        continue
                    sc = _blend(_hist_similarity(cv2, crop, base["crop"]),
                                _orb_similarity(cv2, crop, base["crop"]))
                    cur = best.get(name)
                    if cur is None or sc > cur["score"]:
                        best[name] = {"score": sc, "face_px": int(f[2] * f[3])}
            rec = {
                "shot_id": s.get("id"),
                "order_index": s.get("order_index"),
                "frames_sampled": len(frames),
                "frames_without_face": no_face,
                "scores": {k: v["score"] for k, v in best.items()},
            }
            # 按这一镜**引用了哪些角色**挑出要判的那个
            want = []
            try:
                ids = s.get("character_ids") or []
                if isinstance(ids, str):
                    ids = json.loads(ids or "[]")
                id2name = {c["id"]: c["name"] for c in chars}
                want = [id2name[i] for i in ids if i in id2name and id2name[i] in best]
            except Exception:
                want = []
            pool = want or [k for k in best if k in refs]
            if pool:
                worst = min(pool, key=lambda k: best[k]["score"])
                rec["verdict_character"] = worst
                rec["verdict_score"] = best[worst]["score"]
                if best[worst]["score"] < suspect_below:
                    rec["suspect"] = True
                    suspects.append({
                        "shot_index": s.get("order_index"),
                        "shot_id": s.get("id"),
                        "character": worst,
                        "score": best[worst]["score"],
                    })
            else:
                rec["verdict_character"] = ""
                rec["verdict_score"] = None
                rec["note"] = ("这一镜没检出可判定的人脸"
                               if no_face else "这一镜没有可比的参考角色")
            per_shot.append(rec)

    suspects.sort(key=lambda x: x.get("score") or 0)
    return {
        "ok": True,
        "project_id": pid,
        "characters_compared": list(refs),
        "unmeasurable_characters": unmeasurable,
        "shots_checked": len(per_shot),
        "suspect_below": suspect_below,
        "suspects": suspects[:12],
        "per_shot": per_shot,
        "method": ("人脸检测（OpenCV Haar）+ 人脸区域 HSV 直方图相关性(0.7) "
                   "与 ORB 特征匹配(0.3) 混合评分，与角色参考图比。"),
        "limits": ("这是**排序辅助**，不是人脸识别：不能判定是不是同一个人；"
                   "侧脸/遮挡/小脸/暗光会误报；参考图非正脸特写时基准不稳。"
                   "分数低只代表「值得复核」，不代表错了。"),
        "note": ("这是**只读自检**：不改任何分镜、不重出片、不花钱。"
                 "要重出请先自己看一眼再决定。"),
    }
