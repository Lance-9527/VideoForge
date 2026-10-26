"""Deterministic delivery conformance; preserve the provider's original video."""

from __future__ import annotations

import asyncio
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import uuid


def _clean_path(value: str) -> str:
    """Strip JSON/quote wrappers the settings layer may leave around a path."""
    text = str(value or "").strip()
    for _ in range(2):
        if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
            if text[0] == '"':
                try:
                    decoded = json.loads(text)
                except (ValueError, TypeError):
                    decoded = text[1:-1]
                text = str(decoded).strip()
            else:
                text = text[1:-1].strip()
        else:
            break
    return text


def _resolve_executable(value: str, env_names: tuple, tools: tuple) -> str:
    """Resolve a usable executable; never hand subprocess a name that cannot run.

    Accepts an explicit path (possibly quoted / JSON-encoded), an environment
    variable, a sibling of a resolved tool, or PATH.  Falling back to a bare
    tool name only happens when nothing better exists, and the caller then gets
    a clear error instead of a raw WinError.
    """
    seen: list[str] = []
    if value:
        seen.append(str(value))
    for name in env_names:
        env_value = os.environ.get(name)
        if env_value:
            seen.append(env_value)
    for candidate in seen:
        cleaned = _clean_path(candidate)
        if cleaned and os.path.isfile(cleaned):
            return cleaned
    for tool in tools:
        found = shutil.which(tool)
        if found:
            return found
    return tools[0]


def _sibling_tool(executable: str, tool: str) -> str:
    """Look for a companion tool (ffprobe next to ffmpeg) before giving up."""
    if executable and os.path.isfile(executable):
        folder = os.path.dirname(executable)
        for name in (tool, tool + ".exe"):
            candidate = os.path.join(folder, name)
            if os.path.isfile(candidate):
                return candidate
    return shutil.which(tool) or tool


def _run(command: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=300,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except FileNotFoundError as error:  # missing tool must not surface as raw WinError
        raise ValueError(f"找不到可执行文件：{command[0]}；生成原片保留") from error


def _probe(path: Path, executable: str) -> dict:
    result = _run([executable, "-v", "error", "-count_frames", "-show_entries",
                   "stream=codec_type,width,height,avg_frame_rate,r_frame_rate,duration,nb_frames,nb_read_frames:format=duration",
                   "-of", "json", str(path)])
    if result.returncode:
        raise ValueError("生成视频无法探测；原片保留，未交付")
    data = json.loads(result.stdout)
    streams = data.get("streams") or []
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if not video:
        raise ValueError("生成结果没有视频流；原片保留，未交付")
    try:
        fps = float(Fraction(video.get("avg_frame_rate") or video["r_frame_rate"]))
        frames = int(video.get("nb_read_frames") or video.get("nb_frames") or 0)
        duration = float(video.get("duration") or frames / fps)
        width, height = int(video["width"]), int(video["height"])
    except (ValueError, TypeError, KeyError, ZeroDivisionError) as error:
        raise ValueError("生成视频帧率/帧数信息不可用；原片保留，未交付") from error
    if not math.isfinite(fps) or fps <= 0 or frames < 2 or width < 2 or height < 2:
        raise ValueError("生成视频帧率/帧数信息无效；原片保留，未交付")
    if abs(duration - frames / fps) > 1.5 / fps:
        raise ValueError("生成视频不是可确认的恒定帧率；原片保留，需复核时间轴")
    return {"width": width, "height": height, "fps": fps, "frames": frames,
            "duration": duration, "has_audio": any(s.get("codec_type") == "audio" for s in streams)}


def _atempo(speed: float) -> str:
    factors = []
    while speed > 2.0:
        factors.append(2.0)
        speed /= 2.0
    while speed < 0.5:
        factors.append(0.5)
        speed /= 0.5
    factors.append(speed)
    return ",".join(f"atempo={factor:.12g}" for factor in factors)


def _conform(source_path: str, *, target_duration: float, aspect_ratio: str,
             fps: int, silent: bool, output_path: str | None,
             ffmpeg_path: str, ffprobe_path: str) -> dict:
    source = Path(source_path).resolve()
    if not source.is_file():
        raise ValueError("生成原片不存在")
    fps = int(fps)
    if fps not in (24, 25, 30, 48, 50, 60) or not math.isfinite(target_duration) or target_duration <= 0:
        raise ValueError("交付时长/帧率不合法")
    frame_count = round(target_duration * fps)
    if frame_count < 2 or abs(frame_count / fps - target_duration) > 0.0005:
        raise ValueError("交付时长不能精确表示为目标帧率的完整帧数")
    try:
        ratio = Fraction(aspect_ratio.replace(":", "/"))
        if ratio <= 0 or ratio.numerator > 100 or ratio.denominator > 100:
            raise ValueError()
    except (ValueError, ZeroDivisionError):
        raise ValueError("交付画幅须为明确比例，如16:9、9:16或1:1") from None
    ffmpeg = _resolve_executable(ffmpeg_path, ("VIDEOFORGE_FFMPEG",), ("ffmpeg", "ffmpeg.exe"))
    ffprobe = _resolve_executable(ffprobe_path, ("VIDEOFORGE_FFPROBE",), ("ffprobe", "ffprobe.exe"))
    if os.path.basename(ffprobe).lower() in ("ffprobe", "ffprobe.exe"):
        ffprobe = _sibling_tool(ffmpeg, "ffprobe")
    original = _probe(source, ffprobe)
    available = original["frames"] / original["fps"]
    if target_duration - available > 1.0 / fps + 0.0005:
        raise ValueError(f"生成视频仅{available:.3f}秒，缺口超过1帧；不会插帧或循环伪造内容")

    # Use even integer multiples so the aspect is exact with yuv420p, while
    # keeping the complete composition. 480p is the minimum short edge.
    a, b = ratio.numerator, ratio.denominator
    units = max(min(original["width"] / a, original["height"] / b), 480 / min(a, b))
    units = int(math.ceil(units / 2) * 2)
    width, height = a * units, b * units
    if max(width, height) > 8192:
        raise ValueError("交付画幅尺寸超出安全范围")
    fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()
    target = (Path(output_path).resolve() if output_path else
              source.with_name(f"{source.stem}.delivery-{frame_count}f-{width}x{height}.mp4"))
    if target == source:
        raise ValueError("交付路径不能覆盖生成原片")
    metadata_path = target.with_suffix(".delivery.json")
    contract = {"target_duration": target_duration, "fps": fps, "frames": frame_count,
                "width": width, "height": height, "silent": silent}
    if target.exists():
        try:
            cached = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            raise ValueError("交付路径已存在且缺少有效来源记录；不会覆盖") from None
        if cached.get("source_sha256") != fingerprint or cached.get("contract") != contract:
            raise ValueError("交付路径已存在且来源/要求不同；不会覆盖")
        actual = _probe(target, ffprobe)
        if actual != cached.get("delivered"):
            raise ValueError("已有交付视频与验收记录不一致；不会覆盖")
        return cached

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.stem + ".pending-" + uuid.uuid4().hex + ".mp4")
    # Map first/last source frame timestamps to the first/last delivery frames.
    # fps selects source frames; no optical-flow interpolation is performed.
    timestamp_scale = ((frame_count - 1) / fps) / ((original["frames"] - 1) / original["fps"])
    vf = (f"setpts=(PTS-STARTPTS)*{timestamp_scale:.15g},fps={fps}:round=near:eof_action=pass,"
          f"trim=end_frame={frame_count},scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
          f"pad={width}:{height}:trunc((ow-iw)/4)*2:trunc((oh-ih)/4)*2:black,setsar=1")
    command = [ffmpeg, "-n", "-hide_banner", "-loglevel", "error", "-i", str(source), "-map", "0:v:0",
               "-vf", vf, "-frames:v", str(frame_count), "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "18", "-pix_fmt", "yuv420p"]
    audio_present = original["has_audio"] and not silent
    if audio_present:
        command += ["-map", "0:a:0", "-af", f"asetpts=PTS-STARTPTS,{_atempo(1 / timestamp_scale)},atrim=end={target_duration:.12g}",
                    "-c:a", "aac"]
    else:
        command += ["-an"]
    command += ["-movflags", "+faststart", str(temporary)]
    try:
        encoded = _run(command)
        if encoded.returncode:
            raise ValueError("交付视频规格归一失败；生成原片保留")
        delivered = _probe(temporary, ffprobe)
        if (delivered["frames"] != frame_count or abs(delivered["duration"] - target_duration) > 0.0005
                or abs(delivered["fps"] - fps) > 0.001 or delivered["width"] != width or delivered["height"] != height
                or delivered["has_audio"] != audio_present):
            raise ValueError("交付视频的帧数/时长/画幅/音轨未通过核验；生成原片保留")
        metadata = {"schema": "videoforge.delivery/1", "path": str(target), "raw_path": str(source),
                    "metadata_path": str(metadata_path), "source_sha256": fingerprint, "contract": contract,
                    "original": original, "delivered": delivered, "duration": delivered["duration"],
                    "timestamp_scale": timestamp_scale, "retimed": abs(timestamp_scale - 1) > 0.00001,
                    "contain_and_pad": True, "cropped": False, "interpolation": False,
                    "audio_removed": original["has_audio"] and silent,
                    "audio": "retimed" if audio_present else "none",
                    "note": "交付片经整段变速、等比适配与补边；云端原生输出保留，内容遵循仍需画面验收。"}
        temporary.replace(target)
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return metadata
    finally:
        temporary.unlink(missing_ok=True)


async def conform_reference_delivery(source_path: str, *, target_duration: float,
                                     aspect_ratio: str = "16:9", fps: int = 24,
                                     silent: bool = False, output_path: str | None = None,
                                     ffmpeg_path: str = "", ffprobe_path: str = "") -> dict:
    """Return measured delivery metadata after conformance; raise on a material shortfall.

    This performs local work only. It preserves the original and never creates a
    model task, crops, interpolates movement, or claims native model adherence.
    """
    return await asyncio.to_thread(_conform, source_path, target_duration=float(target_duration),
                                   aspect_ratio=aspect_ratio, fps=fps, silent=bool(silent),
                                   output_path=output_path, ffmpeg_path=ffmpeg_path,
                                   ffprobe_path=ffprobe_path)
