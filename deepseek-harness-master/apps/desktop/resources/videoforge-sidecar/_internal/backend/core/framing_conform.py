"""给交付层加「取景归一」：按参考片（白膜）实测主体占画高，统一缩放并居中裁切。

为什么必须加（2026-10-25 实测）：
  提示词已写死「首帧构图=参考首帧、主体占画高 58%±5%、不得拉宽」，但 seedance-promo
  在 480p 下仍持续拉宽：第 4 轮成片主体 47.8%、第 5 轮 37.8%，且段内单调变焦（167%）。
  也就是说：**构图一致性不能只靠提示词，必须由交付层确定性地保证**。

做法（不生成内容、不插帧、不变速之外不改时间轴）：
  · 量参考片与成片的主体占画高（用 shot_consistency 的分割，同一套判据）
  · scale = clamp(ref/cur, 1.0, 1.8)，只放大不缩小（缩小会出现空边）
  · 放大后居中裁切到目标画幅（严格 16:9），保持完整主体
  · 元数据如实记录 framing_conformed / scale / crop / 变更前后占画比
"""
from pathlib import Path


def _ratio(video: str) -> float:
    from core.shot_consistency import evaluate_generated
    result = evaluate_generated(video, video, None)
    value = (result.get("metrics") or {}).get("subject_height_whitebox")
    return float(value) if value else 0.0


def framing_scale(reference_video: str, generated_video: str) -> dict:
    """算出把成片主体放大到参考片比例所需的缩放系数（只放大）。"""
    ref = _ratio(reference_video)
    cur = _ratio(generated_video)
    if not ref or not cur:
        return {"scale": 1.0, "reference_ratio": ref, "generated_ratio": cur, "reason": "测量失败"}
    scale = ref / cur
    if scale < 1.0:
        return {"scale": 1.0, "reference_ratio": ref, "generated_ratio": cur,
                "reason": "成片主体已不小于参考片（不缩小，避免空边）"}
    scale = min(scale, 1.8)
    return {"scale": round(scale, 4), "reference_ratio": round(ref, 4),
            "generated_ratio": round(cur, 4), "reason": ""}


def apply_framing_conform(source_video: str, reference_video: str, output_path: str,
                          *, fps: int = 24, ffmpeg: str = "") -> dict:
    """按参考片主体占画高做统一缩放 + 居中裁切，输出严格 16:9。

    数学：源主体高 cur*H(px)。放大 s=ref/cur 后为 ref*H；把画幅裁回原高度 H，
    主体占比恰好变成 ref，且不改变主体的相对位置（居中裁切）。
    """
    import json
    import subprocess
    info = framing_scale(reference_video, source_video)
    scale = float(info.get("scale") or 1.0)
    ff = ffmpeg or "ffmpeg"
    probe = subprocess.run([ff.replace("ffmpeg", "ffprobe") if ff.endswith("ffmpeg.exe") or ff.endswith("ffmpeg") else "ffprobe",
                            "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height,r_frame_rate,nb_frames",
                            "-show_entries", "format=duration", "-of", "json", source_video],
                           capture_output=True, text=True, timeout=120)
    try:
        meta = json.loads(probe.stdout or "{}")
        stream = (meta.get("streams") or [{}])[0]
        width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
    except (ValueError, KeyError):
        width = height = 0
    if not width or not height:
        raise ValueError("交付取景归一失败：无法探测成片尺寸")
    out_h = height - (height % 2)
    out_w = int(round(out_h * 16 / 9 / 2) * 2)
    scaled_w = int(round(width * scale / 2) * 2)
    scaled_h = int(round(height * scale / 2) * 2)
    if scaled_w < out_w or scaled_h < out_h:
        raise ValueError("交付取景归一失败：缩放后画幅不足以裁切到 16:9")
    vf = (f"scale={scaled_w}:{scaled_h}:flags=lanczos,"
          f"crop={out_w}:{out_h}:(iw-{out_w})/2:(ih-{out_h})/2,"
          f"setsar=1,fps={int(fps)}")
    command = [ff, "-y", "-hide_banner", "-loglevel", "error", "-i", source_video,
               "-vf", vf, "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
               "-pix_fmt", "yuv420p", "-movflags", "+faststart", output_path]
    done = subprocess.run(command, capture_output=True, text=True, timeout=600)
    if done.returncode:
        raise ValueError("交付取景归一失败：编码返回非零")
    verify = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                             "-show_entries", "stream=width,height,nb_frames",
                             "-show_entries", "format=duration", "-of", "json", output_path],
                            capture_output=True, text=True, timeout=120)
    out = json.loads(verify.stdout or "{}")
    stream = (out.get("streams") or [{}])[0]
    result = {
        "schema": "videoforge.framing/1",
        "path": str(Path(output_path).resolve()),
        "source": str(Path(source_video).resolve()),
        "reference": str(Path(reference_video).resolve()),
        "scale": scale,
        "reason": info.get("reason") or "",
        "subject_ratio_reference": info.get("reference_ratio"),
        "subject_ratio_source": info.get("generated_ratio"),
        "cropped": scale > 1.0001,
        "interpolation": False,
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "frames": int(stream.get("nb_frames") or 0),
        "duration": float((out.get("format") or {}).get("duration") or 0.0),
        "note": "按参考白膜的主体占画高统一缩放并居中裁切到 16:9；不生成内容、不插帧。",
    }
    Path(output_path).with_suffix(".framing.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result
