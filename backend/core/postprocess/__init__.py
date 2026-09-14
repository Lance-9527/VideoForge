"""
VideoForge · 后期处理（FFmpeg）

功能：
- 视频拼接（多个 clip → 一个视频）
- 字幕烧录（ass/srt → 内嵌字幕）
- 多比例导出（16:9 / 9:16 / 1:1）
- BGM 混音
- 视频截取、加速/减速
"""

import asyncio
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple


def check_ffmpeg(ffmpeg_path: str = "ffmpeg") -> bool:
    """检查 ffmpeg 是否可用"""
    try:
        r = subprocess.run(
            [ffmpeg_path, "-version"],
            capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


async def stitch_videos(
    video_paths: List[str],
    output_path: str,
    ffmpeg_path: str = "ffmpeg",
    method: str = "concat",
    target_resolution: Optional[str] = None,
    total_duration: Optional[float] = None,
) -> str:
    """拼接多个视频

    method:
    - concat: 简单拼接（要求所有视频分辨率/编码/音轨一致）
    - reencode: 重编码拼接（兼容性最好）

    ⚠ 两个真实缺陷已在这里修掉（对照 MoneyPrinterTurbo 源码排查发现）：

    1. **reencode 分支假设每段都有音轨**：旧代码写死
       `[{i}:a]asetpts=PTS-STARTPTS[a{i}]`，只要有一段的视频模型
       没出音轨（本地合成的文字卡、或纯画面模型），整条命令直接失败。
       现在先逐段探测音轨，缺的用 anullsrc 补静音。
    2. **没有时长钳制**：转码后总长可能比理论值短几十毫秒，导致最后一句
       旁白没画面/末帧黑屏。现在支持 `total_duration` 输出侧 `-t` 截断。
    """
    if not video_paths:
        raise ValueError("No videos to stitch")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    if method == "concat" and len(video_paths) > 1:
        # 创建 concat list 文件
        list_file = Path(output_path).with_suffix(".txt")
        with open(list_file, "w", encoding="utf-8") as f:
            for p in video_paths:
                # 转义路径（反斜杠统一为 /，单引号转义）—— ffmpeg concat 的格式要求
                safe = str(p).replace("\\", "/").replace("'", "'\\''")
                f.write(f"file '{safe}'\n")

        cmd = [
            ffmpeg_path,
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(list_file),
            "-c", "copy",
        ]
        if total_duration and total_duration > 0:
            cmd.extend(["-t", f"{float(total_duration):.3f}"])
        cmd.append(output_path)

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        # concat 在参数不匹配时会失败，自动 fallback 到 reencode
        if proc.returncode != 0:
            return await stitch_videos(video_paths, output_path, ffmpeg_path, "reencode",
                                       target_resolution, total_duration)

        list_file.unlink(missing_ok=True)
    else:
        # reencode：逐段探测，缺音轨的补静音
        inputs: List[str] = []
        filter_parts = []
        for i, p in enumerate(video_paths):
            inputs.extend(["-i", p])
            has_audio = await _probe_has_audio(p, ffmpeg_path)
            if not has_audio:
                inputs.extend(["-f", "lavfi", "-i",
                               "anullsrc=channel_layout=stereo:sample_rate=48000"])
            a_src = f"{i}:a" if has_audio else f"{len(video_paths)}:a"
            filter_parts.append(f"[{i}:v]setpts=PTS-STARTPTS,format=yuv420p[v{i}];"
                                f"[{a_src}]asetpts=PTS-STARTPTS[a{i}]")
        filter_str = "".join(filter_parts) + \
                     "".join(f"[v{i}][a{i}]" for i in range(len(video_paths))) + \
                     f"concat=n={len(video_paths)}:v=1:a=1[outv][outa]"

        cmd = [ffmpeg_path, "-y"] + inputs + [
            "-filter_complex", filter_str,
            "-map", "[outv]", "-map", "[outa]",
        ]
        if target_resolution:
            cmd.extend(["-s", target_resolution])
        if total_duration and total_duration > 0:
            cmd.extend(["-t", f"{float(total_duration):.3f}"])
        cmd.extend(["-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart", output_path])

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"FFmpeg stitch failed: {stderr.decode()[:500] if stderr else 'unknown'}")

    return output_path


async def _probe_has_audio(path: str, ffmpeg_path: str) -> bool:
    """探测某段视频有没有音轨（决定拼接时要不要补静音）。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg_path, "-hide_banner", "-i", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        return b"Audio:" in (err or b"")
    except Exception:
        return False


async def burn_subtitles(
    video_path: str,
    subtitle_path: str,
    output_path: str,
    ffmpeg_path: str = "ffmpeg",
    font: str = "Microsoft YaHei",
    font_size: int = 24,
    font_color: str = "white",
) -> str:
    """烧录字幕到视频（硬字幕）"""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    # 转义 Windows 路径
    safe_sub_path = subtitle_path.replace("\\", "/").replace(":", "\\:")

    cmd = [
        ffmpeg_path, "-y",
        "-i", video_path,
        "-vf", f"subtitles={safe_sub_path}:force_style='FontName={font},FontSize={font_size},PrimaryColour=&H00{_color_to_ass(font_color)}'",
        "-c:v", "libx264",
        "-c:a", "copy",
        output_path,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg subtitle failed: {stderr.decode()[:500] if stderr else 'unknown'}")

    return output_path


def _color_to_ass(name: str) -> str:
    """颜色名 → ASS 格式（&H00BBGGRR）"""
    table = {
        "white": "FFFFFF", "black": "000000",
        "red": "0000FF", "green": "00FF00", "blue": "FF0000",
        "yellow": "00FFFF", "cyan": "FFFF00",
    }
    return table.get(name.lower(), "FFFFFF")


async def export_aspect_ratio(
    video_path: str,
    target_aspect: str,            # "16:9" / "9:16" / "1:1"
    output_path: str,
    ffmpeg_path: str = "ffmpeg",
) -> str:
    """导出指定比例的视频（中心裁切）"""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    # 计算目标尺寸（保持原视频分辨率）
    # ⚠ 实测：imageio-ffmpeg 的 Windows 包不带 ffprobe.exe，且 PATH 里通常也没有，
    #   旧代码两个分支都指向不存在的 ffprobe → 比例导出必定失败。
    #   这里改为：有 ffprobe 就用，没有就从 `ffmpeg -i` 的输出里解析分辨率。
    w = h = 0
    probe = shutil.which("ffprobe") or ""
    if not probe:
        cand = str(ffmpeg_path).replace("ffmpeg.exe", "ffprobe.exe").replace("ffmpeg", "ffprobe")
        if os.path.exists(cand):
            probe = cand
    if probe:
        try:
            proc = await asyncio.create_subprocess_exec(
                probe, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "csv=p=0", video_path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                w, h = map(int, stdout.decode().strip().split(","))
        except Exception:
            w = h = 0

    if not w or not h:
        proc = await asyncio.create_subprocess_exec(
            str(ffmpeg_path), "-hide_banner", "-i", video_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, stderr = await proc.communicate()
        m = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", (stderr or b"").decode("utf-8", "ignore"), re.S)
        if m:
            w, h = int(m.group(1)), int(m.group(2))
    if not w or not h:
        raise RuntimeError("无法获取源视频分辨率（ffprobe 不可用且 ffmpeg -i 解析失败）")

    target_w, target_h = _compute_target(w, h, target_aspect)

    cmd = [
        ffmpeg_path, "-y",
        "-i", video_path,
        "-vf", f"crop={target_w}:{target_h}:({w}-{target_w})/2:({h}-{target_h})/2,scale={target_w}:{target_h}",
        "-c:v", "libx264", "-crf", "18",
        "-c:a", "copy",
        output_path,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg crop failed: {stderr.decode()[:500] if stderr else 'unknown'}")

    return output_path


def _compute_target(w: int, h: int, target_aspect: str) -> Tuple[int, int]:
    """根据目标比例计算输出尺寸（最大覆盖原视频）"""
    parts = target_aspect.split(":")
    target_ratio = float(parts[0]) / float(parts[1])
    source_ratio = w / h

    if source_ratio > target_ratio:
        # 源更宽，按高度裁
        new_w = int(h * target_ratio)
        new_h = h
    else:
        # 源更高，按宽度裁
        new_w = w
        new_h = int(w / target_ratio)

    # 取偶数（H.264 要求）
    return (new_w // 2 * 2, new_h // 2 * 2)


async def mix_bgm(
    video_path: str,
    bgm_path: str,
    output_path: str,
    ffmpeg_path: str = "ffmpeg",
    bgm_volume: float = 0.3,
) -> str:
    """混入 BGM

    ★ `normalize=0` 必须显式写。`amix` 的 `normalize` **默认为 1**，
    会把每一路都乘 1/N —— 两路混合时人声直接掉 6dB（本机实测 -5.6dB）。
    这正是用户反馈"一加 BGM 人声就变小、听不清台词"的根因。
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    # 视频本身没有音轨时 [0:a] 不存在 → 直接补上 BGM 作为唯一音轨
    has_audio = await _probe_has_audio(video_path, ffmpeg_path)
    if not has_audio:
        cmd = [
            ffmpeg_path, "-y", "-i", video_path, "-i", bgm_path,
            "-filter_complex", f"[1:a]volume={bgm_volume}[aout]",
            "-map", "0:v", "-map", "[aout]", "-shortest",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", output_path,
        ]
    else:
        cmd = [
            ffmpeg_path, "-y",
            "-i", video_path,
            "-i", bgm_path,
            "-filter_complex",
            f"[1:a]volume={bgm_volume}[bgm];"
            f"[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=0:"
            f"normalize=0[aout]",
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            output_path,
        ]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg mix failed: {stderr.decode()[:500] if stderr else 'unknown'}")

    return output_path


async def get_video_info(video_path: str, ffmpeg_path: str = "ffmpeg") -> dict:
    """获取视频元信息。

    ⚠ 实测：imageio-ffmpeg 的 Windows 包只带 ffmpeg.exe，**不带 ffprobe.exe**，
    旧实现会直接抛 FileNotFoundError（导致 /api/postprocess/info 之类接口 500）。
    这里：ffprobe 存在就用 ffprobe，否则退回解析 `ffmpeg -i` 的输出。
    """
    import json
    import os
    import re

    ffprobe = ""
    cand = ffmpeg_path.replace("ffmpeg.exe", "ffprobe.exe").replace("ffmpeg", "ffprobe")
    if os.path.exists(cand):
        ffprobe = cand
    else:
        ffprobe = shutil.which("ffprobe") or ""

    if ffprobe:
        try:
            proc = await asyncio.create_subprocess_exec(
                ffprobe, "-v", "error", "-show_format", "-show_streams",
                "-of", "json", video_path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                return json.loads(stdout.decode("utf-8", "ignore"))
        except Exception:
            pass

    # 退路：解析 `ffmpeg -i`
    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg_path, "-hide_banner", "-i", video_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        text = (stderr or b"").decode("utf-8", "ignore")
    except Exception:
        return {}

    info: dict = {"format": {}, "streams": []}
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        info["format"]["duration"] = str(round(dur, 3))
    m = re.search(r"bitrate:\s*(\d+)\s*kb/s", text)
    if m:
        info["format"]["bit_rate"] = str(int(m.group(1)) * 1000)
    try:
        info["format"]["size"] = str(os.path.getsize(video_path))
    except OSError:
        pass
    m = re.search(r"Video:\s*([a-zA-Z0-9_]+).*?(\d{2,5})x(\d{2,5})", text, re.S)
    if m:
        info["streams"].append({
            "codec_type": "video", "codec_name": m.group(1),
            "width": int(m.group(2)), "height": int(m.group(3)),
        })
    if re.search(r"Audio:\s*([a-zA-Z0-9_]+)", text):
        info["streams"].append({
            "codec_type": "audio",
            "codec_name": re.search(r"Audio:\s*([a-zA-Z0-9_]+)", text).group(1),
        })
    return info


def generate_srt_from_timeline(
    timeline: list,             # [{start, end, action, expression, ...}]
    output_path: str,
) -> str:
    """从分镜时间线生成 SRT 字幕"""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        for i, item in enumerate(timeline, 1):
            start = _fmt_srt_time(item.get("start", 0))
            end = _fmt_srt_time(item.get("end", 0))
            text_parts = [item.get("action", "")]
            if item.get("expression"):
                text_parts.append(f"（{item['expression']}）")
            text = "\n".join(text_parts)
            f.write(f"{i}\n{start} --> {end}\n{text}\n\n")
    return output_path


def _fmt_srt_time(seconds: float) -> str:
    """秒 → SRT 时间格式 00:00:00,000"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
