# -*- coding: utf-8 -*-
"""灰模（previz）驱动出片 —— 独立通道。

为什么独立：`_try_api_video` 里的首帧决策链（场景图裁剪 → 多个分支互相覆盖）连续 3 轮
都没能把"白膜首帧"变成最终值（warnings 一直写"首帧取自场景图"，SSIM 0.44/0.45/0.47）。
而判定实验（只给灰模首帧+末帧 + 锚点提示词）SSIM 达到 **0.913 / 0.905**。
所以这里**完全绕开**那条链，用与实验 1:1 的请求：首帧+末帧+锚点提示词，不发参考视频、不发参考图。

仅适用于明确不支持参考视频的型号；seedance_t6的seedance-2-0-promo走参考视频通道。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import time
from typing import Any, Dict, List, Optional

import logging

logger = logging.getLogger("videoforge.previz_drive")

PROBE_PROMPT = (
    "Generate ONE continuous {DUR}-second photorealistic live-action shot. "
    "Use the first frame and the last frame as STRICT composition anchors. "
    "Preserve the subject count, prop positions, staging and screen direction between the two frames. "
    "The subject moves continuously from the start position to the end position; the camera performs "
    "the movement implied between them. Keep the same shot throughout: no cuts, no new characters, "
    "no extra props, no camera redesign, no teleportation."
)

ANCHOR = (
    "\n\n[FRAME ANCHOR — MUST FOLLOW] The first frame and the last frame are STRICT composition "
    "anchors taken from a previz (grey-block) video. Keep the subject count, prop positions, "
    "staging, shot size and screen direction exactly as they appear in those two frames. The "
    "subject moves from the start position to the end position with continuous natural motion, "
    "and the camera performs the movement implied between them. Do NOT redesign the set, do NOT "
    "add or remove props, do NOT change the camera framing, no cuts, no teleportation. "
    "Only replace the grey-block look with photorealistic materials, lighting and costumes."
)


# ─────────────────────────── 谁是"灰模驱动" ───────────────────────────

def should_use(shot: Dict[str, Any], provider: str, model_name: str) -> bool:
    """这个型号 + 这面镜子是否该走灰模首尾帧通道。"""
    try:
        from core import previz_ref as _pr
        if _pr.supports_reference_video_model(provider, model_name):
            return False                       # 真支持参考视频的型号走原路
        vid = _pr.white_model_video(shot)
        if not vid or not os.path.exists(vid):
            return False
        if str(shot.get("previz_status") or "").lower() in ("", "none", "failed"):
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("灰模驱动判定失败（按普通方式继续）：%s", exc)
        return False


# ─────────────────────────── 帧提取（带缓存） ───────────────────────────

def _cache_root() -> str:
    return os.path.join(os.environ.get("VIDEOFORGE_DATA_DIR", os.getcwd()), "cache", "previz_drive")


def frame_at(video: str, t: float) -> str:
    """取视频 t 秒处的帧（缓存）。"""
    root = _cache_root()
    os.makedirs(root, exist_ok=True)
    key = hashlib.md5(f"{video}|{os.path.getmtime(video):.0f}|{t:.3f}".encode()).hexdigest()[:16]
    dst = os.path.join(root, f"{key}.png")
    if os.path.exists(dst) and os.path.getsize(dst) > 2048:
        return dst
    ff = os.environ.get("VIDEOFORGE_FFMPEG") or "ffmpeg"
    subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error",
                    "-ss", f"{max(0.0, t):.3f}", "-i", video, "-frames:v", "1", dst],
                   check=True, timeout=120,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return dst if os.path.exists(dst) else ""


def video_duration(video: str) -> float:
    ff = os.environ.get("VIDEOFORGE_FFPROBE") or "ffprobe"
    try:
        out = subprocess.run(
            [ff, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=duration", "-of", "csv=p=0", video],
            capture_output=True, text=True, timeout=60)
        return float((out.stdout or "0").strip().split("\n")[0] or 0)
    except Exception:  # noqa: BLE001
        return 0.0


# ─────────────────────────── 提示词 ───────────────────────────

def _base_prompt(shot: Dict[str, Any]) -> str:
    """只用"这一镜要发生什么"的简短描述，避免长篇剧情把锚点冲淡。"""
    for k in ("layer1_overview", "action_desc", "note"):
        v = str(shot.get(k) or "").strip()
        if v:
            return v[:300]
    tl = shot.get("layer2_timeline")
    try:
        if isinstance(tl, str) and tl.strip():
            tl = json.loads(tl)
        for it in (tl or []):
            if isinstance(it, dict):
                act = str(it.get("action") or it.get("desc") or "").strip()
                if act:
                    return act[:300]
    except Exception:  # noqa: BLE001
        pass
    return "同一场景、同一构图内完成这一段动作与运镜"


# ─────────────────────────── 主流程 ───────────────────────────

async def generate(shot: Dict[str, Any], settings: Dict[str, Any], provider: str,
                   model_name: str, *, duration_override: Any = None,
                   dry_run: bool = False, **_ignored: Any) -> Dict[str, Any]:
    """按段生成并拼接。返回与 `_try_api_video` 相同的结构（ok/path/url/duration/warnings/errors）。"""
    from core import previz_ref as _pr
    from core.adapters import VideoGenRequest, get_adapter

    warnings: List[str] = []
    errors: List[str] = []
    vid = _pr.white_model_video(shot)
    if not vid:
        return {"ok": False, "error": "没有白膜视频", "warnings": warnings, "errors": errors}
    if _pr.supports_reference_video_model(provider, model_name):
        return {"ok":False,"error":"该型号支持参考视频，必须走参考视频通道，不能退化为首尾帧驱动",
                "warnings":warnings,"errors":errors}

    total = float(duration_override or shot.get("duration_seconds") or 5)
    seg = max(5.0, min(15.0, round(total)))          # 单段合法区间 [5,15]
    pv_dur = video_duration(vid) or total
    n = max(1, int(round(total / seg)))
    seg = round(total / n, 3)

    # Preserve the exact model selected by the user. A capability fallback never changes pricing tiers.
    _mdl = str(model_name or "")

    key = ((settings.get("api_keys") or {}).get(provider)) or ""
    adapter = get_adapter(provider, api_key=key, config={"model_name": _mdl})
    model_res = str(shot.get("resolution") or settings.get("default_video_resolution") or "720P")

    out_dir = os.path.join(settings.get("output_dir") or ".", shot.get("project_id") or "",
                           "shots", "api")
    os.makedirs(out_dir, exist_ok=True)
    clips: List[str] = []
    dry: List[Dict[str, Any]] = []

    for i in range(n):
        t0 = i * seg
        t1 = min(pv_dur - 0.05, (i + 1) * seg - 0.05)
        f1 = frame_at(vid, t0)
        f2 = frame_at(vid, max(t0 + 0.2, t1))
        if not f1 or not f2:
            errors.append(f"第 {i+1} 段取帧失败")
            continue
        # ★ 2026-10-24（第 3 次对照后的最终形态）：提示词与判定实验**逐字一致**。
        #   实测梯度：中文剧情+锚点在后 0.47 → 锚点在前+120字剧情 0.57 →
        #   探针原文（纯英文、无剧情、无参考图、无 reference_video）0.913。
        _scene = (" ".join(str(_base_prompt(shot)).split()))[:60]
        prompt = (PROBE_PROMPT.replace("{DUR}", str(int(round(seg))))
                  + ("\n\nSubject/scene hint (do NOT change layout from the frames): " + _scene if _scene else ""))
        if not f1 or not f2:
            errors.append(f"第 {i+1} 段取帧失败")
            continue
        # ★ 2026-10-24：锚点必须在**前面**，剧情压到一句。
        #   判定实验（SSIM 0.913）就是"锚点指令 → 再一句动作"；
        #   反过来写（中文剧情 + 追加锚点）实测只有 0.47。
        _scene = _base_prompt(shot)
        _scene = (" ".join(str(_scene).split()))[:120]
        prompt = ANCHOR + "\n\nScene content (subject/action only, layout comes from the frames): " + _scene
        # negative 只保留"风格类"，去掉可能改变构图的词
        _neg = str(shot.get("negative_prompt") or "")
        _neg = "、".join([t for t in _neg.replace("，", "、").split("、")
                          if t and not any(k in t for k in ("构图", "机位", "运镜", "场景"))])[:120]
        if _neg:
            prompt += "\n\nStyle negatives only: " + _neg
        req = VideoGenRequest(
            prompt=prompt,
            duration=int(round(seg)),
            aspect_ratio=str(shot.get("aspect_ratio") or "16:9"),
            resolution=model_res,
            first_frame=f1,
            last_frame=f2,
            video_url=None,            # 2.0 不认参考视频，发也是白费
            reference_images=[],       # 参考图会抢首帧约束
            character_reference=[],
            draft=False,
        )
        logger.info("灰模驱动 第 %d/%d 段：%.1fs→%.1fs", i + 1, n, t0, max(t0 + 0.2, t1))
        if dry_run:
            dry.append({
                "seg": i + 1, "t0": round(t0, 2), "t1": round(max(t0 + 0.2, t1), 2),
                "first_frame": f1, "last_frame": f2,
                "duration": int(round(seg)), "resolution": model_res,
                "video_url": None, "reference_images": 0,
                "prompt_tail": prompt[-120:],
            })
            continue
        res = await adapter.generate(req)
        if not getattr(res, "success", False):
            errors.append(f"第 {i+1} 段失败：{getattr(res, 'error', '')}")
            continue
        dst = os.path.join(out_dir,
                           f"previzdrive_{shot.get('id','')[:8]}_{int(time.time())}_{i:02d}.mp4")
        try:
            await adapter.download_to_local(res.video_url, dst)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"第 {i+1} 段下载失败：{exc}")
            continue
        clips.append(dst)

    if dry_run:
        return {"ok": True, "dry": dry, "segments": n, "seg_seconds": seg,
                "warnings": warnings, "errors": errors}

    if not clips:
        return {"ok": False, "error": "；".join(errors) or "全部段落失败",
                "warnings": warnings, "errors": errors}

    if len(clips) == 1:
        final = clips[0]
    else:
        final = os.path.join(out_dir,
                             f"previzdrive_{shot.get('id','')[:8]}_{int(time.time())}_merged.mp4")
        lst = final + ".txt"
        with open(lst, "w", encoding="utf-8") as fh:
            for c in clips:
                fh.write(f"file '{c.replace(chr(92), '/')}'\n")
        ff = os.environ.get("VIDEOFORGE_FFMPEG") or "ffmpeg"
        subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", final],
                       check=True, timeout=600,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    warnings.append(f"灰模驱动（首尾帧）共 {len(clips)} 段，每段 {seg:.1f}s；"
                    f"该型号不支持参考视频，已按首尾帧逐段驱动。")
    dur = 0.0
    try:
        dur = video_duration(final)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "path": final, "url": "", "duration": dur or total,
            "warnings": warnings, "errors": errors, "chain_from": ""}
