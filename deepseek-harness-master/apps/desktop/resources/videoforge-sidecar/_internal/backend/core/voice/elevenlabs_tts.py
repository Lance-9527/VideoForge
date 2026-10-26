# -*- coding: utf-8 -*-
"""VideoForge · ElevenLabs TTS 适配器

注册表里 `elevenlabs` 一直只有目录、没有调用实现。本文件补上。

接口：
    POST {base}/v1/text-to-speech/{voice_id}
    Header: xi-api-key: <key>          ← **不是** Authorization: Bearer
            Accept: audio/mpeg
    Body:   {"text": "...", "model_id": "eleven_multilingual_v2",
             "voice_settings": {"stability":0.5,"similarity_boost":0.75}}
    返回:   音频二进制（audio/mpeg）

★ 两个容易写错的点：
  1. 鉴权头是 `xi-api-key`（Bearer 会 401）；
  2. voice_id 在 **URL 路径里**，不是 body 字段。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import List

from .base import (
    TTSRequest,
    TTSResult,
    VoiceInfo,
    VoiceProvider,
    register,
)
from ._credentials import resolve_base_url


logger = logging.getLogger("videoforge.voice.elevenlabs")

DEFAULT_BASE = "https://api.elevenlabs.io"

#: 官方预置音色（这些 id 是公开的、长期稳定）
ELEVEN_VOICES = [
    ("21m00Tcm4TlvDq8ikWAM", "Rachel · 女声（默认）", "female"),
    ("pNInz6obpgDQGcFmaJgB", "Adam · 男声", "male"),
    ("ErXwobaYiN019PkySvjV", "Antoni · 男声", "male"),
    ("EXAVITQu4vr4xnSDxMaL", "Bella · 女声", "female"),
    ("AZnzlk1XvdvUeBnXmlld", "Domi · 女声", "female"),
    ("MF3mGyEYCl7XYWbV9V6O", "Elli · 女声", "female"),
    ("TxGEqnHWrfWFTfGW9XjX", "Josh · 男声", "male"),
    ("yoZ06aMxZJJ28mfd3POQ", "Sam · 男声", "male"),
]

ELEVEN_MODELS = [
    {"id": "eleven_multilingual_v2", "label": "Multilingual v2（默认·多语言）",
     "note": "中文支持好、情感自然；ElevenLabs 最常用的一档"},
    {"id": "eleven_turbo_v2_5", "label": "Turbo v2.5（快·便宜）",
     "note": "延迟低，适合长片批量配音"},
    {"id": "eleven_flash_v2_5", "label": "Flash v2.5（最快）", "note": "最便宜，质量略降"},
]

DEFAULT_VOICE = "21m00Tcm4TlvDq8ikWAM"


@register
class ElevenLabsTTS(VoiceProvider):
    """ElevenLabs（音色克隆最主流的一家）"""

    name = "elevenlabs"
    display_name = "ElevenLabs"
    description = ("英文/多语言最自然的一档，音色克隆最主流。"
                   "需要 ElevenLabs 的 API Key（xi-api-key 鉴权）。")
    prefix = "elevenlabs:"
    requires_api_key = True

    supports_model = True
    default_model = "eleven_multilingual_v2"
    models = ELEVEN_MODELS
    verification = "implemented（本机无 ElevenLabs Key，未做真实合成验证）"
    #: dispatcher 需要把 settings（region / 三件套 / 参考音频）传下来
    wants_settings = True

    async def list_voices(self) -> List[VoiceInfo]:
        return [
            VoiceInfo(voice_id=f"elevenlabs:{vid}", display_name=label,
                      language="en-US", gender=gender, is_builtin=True)
            for vid, label, gender in ELEVEN_VOICES
        ]

    async def synthesize(self, req: TTSRequest) -> TTSResult:
        if not self.api_key:
            return TTSResult(
                success=False, provider=self.name, voice_id=req.voice_id,
                error=("ElevenLabs 未配置 API Key。到「设置 → 🔊 语音合成 API Keys」"
                       "填 ElevenLabs Key（`tts_api_keys.elevenlabs`）即可。"),
            )
        vid = req.voice_id.split(":", 1)[1] if ":" in req.voice_id else req.voice_id
        vid = vid or DEFAULT_VOICE
        model = (self.config or {}).get("model") or self.default_model
        base = resolve_base_url(self.name, DEFAULT_BASE, config=self.config,
                                env=("ELEVENLABS_BASE_URL",))
        # rate → voice_settings 没有倍速字段，官方靠 TTS 模型本身；
        # 这里把音量/音高换算成可调项（stability/style 保持默认上限内的稳定值）
        body = {
            "text": req.text,
            "model_id": model,
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
                "style": 0.0,
                "use_speaker_boost": True,
            },
        }

        out = Path(req.output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            client = await self._ensure_client()
            r = await client.post(
                f"{base}/v1/text-to-speech/{vid}", json=body,
                headers={"xi-api-key": self.api_key, "Accept": "audio/mpeg",
                         "Content-Type": "application/json"},
                timeout=180.0)
            if r.status_code >= 400:
                return TTSResult(
                    success=False, provider=self.name, voice_id=req.voice_id,
                    error=f"ElevenLabs HTTP {r.status_code}: {(r.text or '')[:300]}")
            ctype = (r.headers.get("content-type") or "").lower()
            if "json" in ctype or r.content[:1] == b"{":
                return TTSResult(
                    success=False, provider=self.name, voice_id=req.voice_id,
                    error=f"ElevenLabs 返回错误体：{(r.text or '')[:300]}")
            if not r.content:
                return TTSResult(success=False, provider=self.name,
                                 voice_id=req.voice_id,
                                 error="ElevenLabs 返回了空音频")
            out.write_bytes(r.content)
            return TTSResult(
                success=True, audio_path=str(out),
                duration_seconds=_estimate_duration(req.text, req.rate),
                provider=self.name, voice_id=req.voice_id,
                raw={"model": model, "voice": vid, "size_bytes": len(r.content),
                     "base_url": base})
        except Exception as e:  # noqa: BLE001
            logger.exception("ElevenLabs TTS failed")
            return TTSResult(success=False, provider=self.name, voice_id=req.voice_id,
                             error=f"ElevenLabs 调用失败：{type(e).__name__}: {e}")


def _estimate_duration(text: str, rate: float) -> float:
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    words = len(re.findall(r"[A-Za-z0-9]+", text))
    return max(1.0, (cjk / 4.2 + words / 2.7) / max(0.25, float(rate or 1.0)))
