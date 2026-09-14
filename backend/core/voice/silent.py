"""
VideoForge · 「无配音」模式

当用户明确选「无配音」（voice_id 以 `silent:` 开头）时：
- 不调任何外部 TTS
- 直接生成一段静音 WAV/MP3（用 FFmpeg anullsrc 或 numpy + wave）
- 时长按文本估算（中文 4.2 字/秒、英文 2.7 词/秒）

借鉴 MPT voice.py 的 is_no_voice / estimate_no_voice_duration / generate_silent_audio。
"""

from __future__ import annotations

import logging
import math
import re
import subprocess
import wave
from pathlib import Path
from typing import List

from .base import (
    TTSRequest,
    TTSResult,
    VoiceInfo,
    VoiceProvider,
    register,
)


logger = logging.getLogger("videoforge.voice.silent")


_SILENT_VOICE_ID = "silent:no-voice"


def _estimate_no_voice_duration(text: str) -> float:
    """无配音模式时长估算（中文 4.2 字/秒、英文 2.7 词/秒、其他 4.0 字符/秒）

    借鉴 MPT estimate_no_voice_duration：返回稳定的视频时间轴长度。
    """
    text = (text or "").strip()
    if not text:
        return 3.0

    cjk_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    words = len(re.findall(r"[A-Za-z0-9]+", text))
    other_chars = max(len(text) - cjk_chars - sum(len(w) for w in re.findall(r"[A-Za-z0-9]+", text)), 0)

    cjk_duration = cjk_chars / 4.2
    word_duration = words / 2.7
    other_duration = other_chars / 4.0

    # 句间停顿：按标点切分
    sentence_count = max(text.count("。") + text.count("！") + text.count("？")
                         + text.count(".") + text.count("!") + text.count("?"), 1)
    pause_duration = max(sentence_count - 1, 0) * 0.35

    return max(3.0, cjk_duration + word_duration + other_duration + pause_duration)


@register
class SilentProvider(VoiceProvider):
    """「无配音」provider — 生成静音"""

    name = "silent"
    display_name = "无配音"
    description = "不生成语音，输出静音占位音频（用于纯字幕视频）。"
    prefix = "silent:"
    requires_api_key = False

    async def list_voices(self) -> List[VoiceInfo]:
        return [
            VoiceInfo(
                voice_id=_SILENT_VOICE_ID,
                display_name="无配音（静音）",
                language="any",
                gender="unknown",
                is_builtin=True,
            )
        ]

    async def synthesize(self, req: TTSRequest) -> TTSResult:
        duration = _estimate_no_voice_duration(req.text)

        out = Path(req.output_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        try:
            if out.suffix.lower() == ".wav":
                self._write_wav_silence(out, duration)
            else:
                self._write_ffmpeg_silence(out, duration)
        except Exception as e:
            logger.exception("Silent audio generation failed")
            return TTSResult(
                success=False,
                provider=self.name,
                voice_id=req.voice_id,
                error=f"静音生成失败：{e}",
            )

        return TTSResult(
            success=True,
            audio_path=str(out),
            duration_seconds=duration,
            subtitles=None,
            provider=self.name,
            voice_id=req.voice_id,
            raw={"mode": "silent", "duration_estimated": duration},
        )

    @staticmethod
    def _write_wav_silence(path: Path, duration: float) -> None:
        """16-bit mono PCM WAV silence（无 ffmpeg 依赖）"""
        sample_rate = 24000
        num_samples = int(round(duration * sample_rate))
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(b"\x00\x00" * num_samples)

    @staticmethod
    def _write_ffmpeg_silence(path: Path, duration: float) -> None:
        """用 ffmpeg anullsrc 生成静音 mp3"""
        # 先尝试找 ffmpeg：环境变量 → imageio-ffmpeg → 系统
        ffmpeg = "ffmpeg"
        try:
            import imageio_ffmpeg
            bundled = imageio_ffmpeg.get_ffmpeg_exe()
            if bundled:
                ffmpeg = bundled
        except Exception:
            pass

        cmd = [
            ffmpeg, "-y",
            "-f", "lavfi",
            "-i", "anullsrc=r=44100:cl=mono",
            "-t", f"{duration:.3f}",
            "-codec:a", "libmp3lame",
            "-q:a", "4",
            str(path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"ffmpeg 静音生成失败：{(result.stderr or result.stdout or '').strip()[:300]}"
            )
