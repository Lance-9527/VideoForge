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


_FFMPEG_OK_CACHE: dict = {}


def moov_at_end(path: str) -> bool:
    r"""`moov`（元数据）是不是被写在了文件**末尾** —— 这决定网页播放器会不会卡在 0:00。

    ★★ 2026-09-28 第 73 轮。用户原话「在对话框里面的视频看不了」，
    截图里播放器是 **0:00 + 一个播放键**（元数据压根没读到）。

    `<video preload="metadata">` 想显示时长必须先拿到 `moov` 盒子。若它被写在
    `mdat`（画面数据）**之后**（= 没加 `-movflags +faststart`），浏览器就得把
    **整个文件**读完才知道时长 —— 40 MB 的成片在本地也表现为"卡在 0:00"。
    这和"HTTP 支不支持 Range"是**两件独立的事**（Range 正常照样会中招）。

    只读文件头的 box 结构，秒级、免费。非 mp4 / 读不了时返回 False（不误报）。
    """
    import struct
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            pos, seen_mdat = 0, False
            for _ in range(16):
                if pos >= size:
                    break
                f.seek(pos)
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                sz = struct.unpack(">I", hdr[:4])[0]
                typ = hdr[4:8].decode("latin-1", "replace")
                if sz == 1:
                    sz = struct.unpack(">Q", f.read(8))[0]
                elif sz == 0:
                    sz = size - pos
                if typ == "moov":
                    return seen_mdat          # moov 出现在 mdat 之后 ⇒ 需要 faststart
                if typ == "mdat":
                    seen_mdat = True
                if sz < 8:
                    break
                pos += sz
    except Exception:  # noqa: BLE001
        return False
    return False


async def ensure_faststart(path: str, ffmpeg_path: str = "ffmpeg") -> dict:
    r"""把 `moov` 挪到文件前面（**不重编码**，只重封装）。

    返回 `{changed, skipped, error}`。文件不存在/不需要时 `changed=False`。
    `-c copy` 所以是秒级、无损 —— 画面和音轨一个字节都不动。
    """
    p = str(path or "")
    if not p or not os.path.exists(p):
        return {"changed": False, "skipped": "文件不存在"}
    if not moov_at_end(p):
        return {"changed": False, "skipped": "已经是 faststart"}
    tmp = p + ".faststart.tmp.mp4"
    cmd = [ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error",
           "-i", p, "-c", "copy", "-movflags", "+faststart", tmp]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
    except Exception as e:  # noqa: BLE001
        return {"changed": False, "error": f"重封装起不来：{e}"}
    if proc.returncode != 0 or not os.path.exists(tmp):
        try:
            os.remove(tmp)
        except Exception:  # noqa: BLE001
            pass
        return {"changed": False,
                "error": (err or b"").decode("utf-8", "replace")[-200:]}
    try:
        os.replace(tmp, p)
    except Exception as e:  # noqa: BLE001
        return {"changed": False, "error": f"替换失败：{e}"}
    return {"changed": True}


def check_ffmpeg(ffmpeg_path: str = "ffmpeg") -> bool:
    """检查 ffmpeg 是否可用。

    ★ 2026-09-14 性能（用户："开这个软件的时候…卡顿"）：
      这个函数原来**每调一次就 spawn 一次 `ffmpeg -version`**（本机约 2 秒！），
      而 `/api/health` 每次请求要调它 1–2 次 —— 启动器还用 0.25s 的间隔轮询
      `/api/health` 等后端就绪。也就是说**开机那几秒里每秒要起 4~8 个 ffmpeg 进程**，
      CPU/磁盘被占满，界面自然卡。
      现在：结果按「路径 + 文件 mtime」缓存 120 秒 —— 探测本身是只读且稳定的，
      反复 spawn 毫无意义。
    """
    key = str(ffmpeg_path or "ffmpeg")
    try:
        import os as _os
        import time as _time
        st = _os.stat(key) if (_os.sep in key or "/" in key) else None
        sig = (st.st_mtime, st.st_size) if st else None
        hit = _FFMPEG_OK_CACHE.get(key)
        if hit and hit[0] > _time.time() and hit[1] == sig:
            return bool(hit[2])
    except Exception:
        sig = None
    ok = False
    try:
        r = subprocess.run(
            [ffmpeg_path, "-version"],
            capture_output=True, timeout=5,
        )
        ok = r.returncode == 0
    except Exception:
        ok = False
    try:
        import time as _time
        _FFMPEG_OK_CACHE[key] = (_time.time() + 120, sig, ok)
    except Exception:
        pass
    return ok


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

    # ★★ Windows 路径转义（2026-09-23 修，实测踩到）
    #
    # 把字幕文件的**绝对路径**塞进 `subtitles=` 滤镜时，路径里的盘符冒号
    # （`C:`）会被 ffmpeg 的滤镜解析器当成"参数分隔符"，于是必须转义成 `\:`。
    # 原代码写的是 `.replace(":", "\\:")` —— 在 Python 里那是**一个反斜杠**
    # （`"\\:"` 是 `\` + `:`），看起来对。
    #
    # 但实测报错是这个：
    #     Unable to open C:UsersADMINI~1AppDataLocalTemptmpkysqrv8ft.srt
    # 注意：**冒号和所有反斜杠都没了** —— 说明那个反斜杠在传给 ffmpeg 的
    # 过程中被吃掉了（libass 最终拿到的是一个裸的 `C:Users...`）。
    # 结果就是烧字幕**永远失败**，而且报错信息（一大段 ffmpeg configure）
    # 完全看不出真正原因。
    #
    # 与其去猜"几层反斜杠才能活下来"，不如**根本不用绝对路径**：
    # ffmpeg 有 `-i` 输入文件，我们可以干脆切到字幕所在目录、只用文件名 ——
    # 没有盘符就没有冒号，没有冒号就不需要转义。这条在 Windows 上最稳。
    sub_dir = str(Path(subtitle_path).resolve().parent)
    sub_name = Path(subtitle_path).name
    safe_sub_name = sub_name.replace("'", r"\'")

    cmd = [
        ffmpeg_path, "-y",
        "-i", video_path,
        "-vf", f"subtitles={safe_sub_name}:force_style='FontName={font},FontSize={font_size},PrimaryColour=&H00{_color_to_ass(font_color)}'",
        "-c:v", "libx264",
        "-c:a", "copy",
        # ★★ 2026-09-28 第 73 轮（用户原话：「在对话框里面的视频看不了」）。
        #   这条命令原来**没有** `-movflags +faststart`，于是烧完字幕的成片
        #   把 `moov`（元数据）写在了文件**末尾**：
        #       执棋者-第1集-有声成片.sub.mp4   box 顺序 ftyp → free → mdat → moov
        #   而同一批里走了别的路径的 `…-有声成片.mp4` 是 ftyp → moov → …（正常）。
        #   后果：浏览器 `<video preload="metadata">` 读不到时长 ⇒
        #   播放器**卡在 0:00 + 一个播放键**，也就是用户截图里的那一幕
        #   （他要点开看的正好是带字幕的那一版）。
        #   本仓其他所有出片路径都写了 faststart，只有这一条漏了 —— 这就是"看不了"的根因。
        "-movflags", "+faststart",
        output_path,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        # ★ cwd 切到字幕所在目录，这样滤镜里的相对文件名就能被解析到。
        #   绝对路径 + 转义那条路实测不通（见上）。
        cwd=sub_dir,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = (stderr or b"").decode("utf-8", "replace")
        # 只挑有用的行。整段 stderr 是 ffmpeg 的 configure 横幅，
        # 真正的失败原因（Unable to open / Error initializing filters）
        # 被淹在里面 —— 用户看到那坨东西等于没有信息。
        keep = [ln.strip() for ln in err.split("\n")
                if any(k in ln for k in ("Unable to open", "Error initializing",
                                         "No such file", "Invalid argument",
                                         "Error opening", "fontselect", "Fontconfig"))]
        detail = " | ".join(keep[:4]) or err.strip()[-400:]
        raise RuntimeError(f"FFmpeg 烧字幕失败：{detail}")

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
    timeline: list,             # [{start, end, text|action, expression, ...}]
    output_path: str,
) -> str:
    """从（分镜）时间线生成 SRT 字幕。

    ★ 2026-09-14：**优先用 `text`（真正的台词）**，只有拿不到台词时才退回
      动作描述 + 表情。
      为什么（用户："你检查下…包括后期处理什么的"）：这里原来无条件写
      `action` + `（expression）` —— 于是"字幕"变成
      「布兰坐在轮椅上，缓缓进入大厅（沉静）」这种**画面说明**，
      而同一部片子在「一键成片」里用的却是**台词**。
      同一部片子两套字幕口径，字幕对不上说话，观感很差
      （而且这正是用户否决过的"旁白念剧情"写法）。
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        n = 0
        for item in timeline:
            # ★★ 2026-09-26 第 31 轮补：**非字典条目**是这个仓库记录过的真实形状
            #    —— `acting.enrich_timeline()` 专门为它加了计数与告警、并**原样透传**，
            #    而 `shots_from_script` 那条路**不做规范化**。
            #    原来这里直接 `item.get(...)`：一条字符串混进时间线 →
            #    AttributeError → **整条字幕一个文件都写不出来**。
            #    同一个形状在 `orchestrator._compose_full_prompt`（第 11 轮）
            #    已经栽过一次，修法一样：跳过它，别让一个字幕都写不出。
            #    ★ 顺带：`timeline` 本身若是字符串，`for` 会**逐字符**迭代 ——
            #      有了这道守卫就变成"写出空字幕"，而不是崩掉。
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                # ★ 台词优先，而且 `dialogue` **通常是字典**
                #   （`{character, text, emotion}` —— 见 core/dialogue.py 的约定；
                #     `film.py::collect_subtitle_segments()` 也确实是按那个形状取的）。
                #   这里原来写的是 `item.get("dialogue")` **直接当字符串**用：
                #   一旦有人把原始时间片传进来，`str(dict)` 就会把
                #   `{'character': '甲', 'text': '…', …}` 整段写进字幕文件。
                #   现在按形状取：字典取它的 `text`，字符串原样用。
                #   （已核实：`film.py` 那条路只传带 `text` 的段，走不到这里 ——
                #     这是给**其它调用方**的加固，代价为零、只会更对。）
                d = item.get("dialogue")
                if isinstance(d, dict):
                    text = str(d.get("text") or "").strip()
                elif isinstance(d, str):
                    text = d.strip()
            if not text:
                parts = [str(item.get("action") or "")]
                if item.get("expression"):
                    parts.append(f"（{item['expression']}）")
                text = "\n".join(p for p in parts if p).strip()
            if not text:
                continue
            n += 1
            # ★ 同一轮：时间戳**可能是字符串**（LLM 写成 "0s" / "1.5" 这种）。
            #   原来直接进 `_fmt_srt_time()`，里面做的是 `seconds // 3600` ——
            #   字符串会 TypeError，后果同样是"整条字幕写不出来"。
            #   `acting._f()` / `dialogue` 那边各有一个小工具就是为这个形状写的，
            #   这里原来漏了。
            start = _fmt_srt_time(_srt_sec(item.get("start")))
            end = _fmt_srt_time(_srt_sec(item.get("end")))
            f.write(f"{n}\n{start} --> {end}\n{text}\n\n")
    return output_path


def _fmt_srt_time(seconds: float) -> str:
    """秒 → SRT 时间格式 00:00:00,000"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _srt_sec(v) -> float:
    """把时间戳收敛成 float（取不到数字就 0.0 —— 不编时间，也不崩）。

    ★ 2026-09-26 第 31 轮补：本仓的时间线里数字**可能是字符串**。
      `acting._f()` 与 `dialogue` 那边各有一个同名小工具，
      而 `generate_srt_from_timeline()` 原来直接把 `item.get("start")`
      丢给 `_fmt_srt_time()`（里面是 `seconds // 3600`）——
      一个 `"0s"` 就会 TypeError，整条字幕写不出来。
    """
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(?:\.\d+)?", str(v or ""))
    return float(m.group(0)) if m else 0.0
