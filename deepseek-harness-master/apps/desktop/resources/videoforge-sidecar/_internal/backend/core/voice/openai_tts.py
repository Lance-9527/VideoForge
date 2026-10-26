# -*- coding: utf-8 -*-
"""VideoForge · OpenAI TTS 适配器

用户已经有一把「中转站的 OpenAI 兼容 Key」（设置里存在 `llm_api_keys.openai`），
而注册表里 `openai` 这一项一直**没有 TTS 实现** —— 选了它只会得到
「未注册的 TTS provider：openai」。本文件把它接上。

接口（官方 / 各家 OpenAI 兼容网关一致）：
    POST {base}/v1/audio/speech
    Header: Authorization: Bearer <key>
    Body:   {"model":"gpt-4o-mini-tts","input":"要读的文本",
             "voice":"alloy","response_format":"mp3","speed":1.0}
    返回:   音频二进制（audio/mpeg）

★ 有一个坑：这个接口**失败时也常回 200 之外**，body 是 JSON
  `{"error":{"message":"..."}}`。直接把 body 当音频写盘，会得到一个
  几十字节的"假 mp3"，播放器一声不响 —— 所以这里先看 content-type。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, List

from .base import (
    TTSRequest,
    TTSResult,
    VoiceInfo,
    VoiceProvider,
    register,
)
from ._credentials import resolve_base_url, settings_snapshot


logger = logging.getLogger("videoforge.voice.openai")

DEFAULT_BASE = "https://api.openai.com"
API_PATH = "/v1/audio/speech"

#: OpenAI 的预置音色（官方文档里的那批；gpt-4o-mini-tts 还支持 instructions）
OPENAI_VOICES = [
    ("alloy", "Alloy · 中性", "unknown"),
    ("echo", "Echo · 男声", "male"),
    ("fable", "Fable · 叙事", "unknown"),
    ("onyx", "Onyx · 低沉男声", "male"),
    ("nova", "Nova · 女声", "female"),
    ("shimmer", "Shimmer · 柔和女声", "female"),
    ("coral", "Coral · 明亮女声", "female"),
    ("sage", "Sage · 沉稳女声", "female"),
    ("ash", "Ash · 中性男声", "male"),
]

#: 能选的型号（官方在售的三档）
OPENAI_MODELS = [
    {"id": "gpt-4o-mini-tts", "label": "gpt-4o-mini-tts（新·可给风格提示）",
     "note": "支持 instructions 字段描述语气；按字符计费"},
    {"id": "tts-1-hd", "label": "tts-1-hd（高保真）", "note": "音质更好，单价更高"},
    {"id": "tts-1", "label": "tts-1（快）", "note": "最便宜，延迟最低"},
]


@register
class OpenAITTS(VoiceProvider):
    """OpenAI TTS（也兼容任何 OpenAI 协议的语音网关）"""

    name = "openai"
    display_name = "OpenAI 语音"
    description = ("OpenAI 官方 TTS（gpt-4o-mini-tts / tts-1-hd）。"
                   "也兼容把语音挂成 OpenAI 协议的中转站。")
    prefix = "openai:"
    requires_api_key = True

    supports_model = True
    default_model = "gpt-4o-mini-tts"
    models = OPENAI_MODELS
    #: 诚实标注：代码已实现；本机那把 openai 桶的 Key 指向中转站，
    #: 未对 api.openai.com 做真实合成验证
    verification = "implemented（未用真实 OpenAI Key 验证）"
    #: dispatcher 需要把 settings（region / 三件套 / 参考音频）传下来
    wants_settings = True

    async def list_voices(self) -> List[VoiceInfo]:
        return [
            VoiceInfo(voice_id=f"openai:{vid}", display_name=label,
                      language="en-US", gender=gender, is_builtin=True)
            for vid, label, gender in OPENAI_VOICES
        ]

    async def synthesize(self, req: TTSRequest) -> TTSResult:
        if not self.api_key:
            return TTSResult(
                success=False, provider=self.name, voice_id=req.voice_id,
                error=("OpenAI 语音未配置 API Key。到「设置 → 🔊 语音合成 API Keys」"
                       "填 OpenAI Key（`tts_api_keys.openai`）即可。"),
            )
        vid = req.voice_id.split(":", 1)[1] if ":" in req.voice_id else req.voice_id
        vid = vid or "alloy"
        model = (self.config or {}).get("model") or self.default_model
        base = resolve_base_url(self.name, DEFAULT_BASE, config=self.config,
                                env=("OPENAI_TTS_BASE_URL",))
        body: Dict[str, object] = {
            "model": model,
            "input": req.text,
            "voice": vid,
            "response_format": "mp3",
            "speed": max(0.25, min(4.0, float(req.rate or 1.0))),
        }
        # gpt-4o-mini-tts 支持自然语言风格提示（有就发，没有就不发）
        settings = settings_snapshot(self.config)
        instructions = str(settings.get("openai_tts_instructions") or "").strip()
        if instructions and model.startswith("gpt-4o"):
            body["instructions"] = instructions

        out = Path(req.output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            client = await self._ensure_client()
            r = await client.post(f"{base}{API_PATH}", json=body,
                                  headers={"Authorization": f"Bearer {self.api_key}"},
                                  timeout=180.0)
            if r.status_code >= 400:
                return TTSResult(
                    success=False, provider=self.name, voice_id=req.voice_id,
                    error=f"OpenAI 语音 HTTP {r.status_code}: {(r.text or '')[:300]}")
            ctype = (r.headers.get("content-type") or "").lower()
            if "json" in ctype or (r.content[:1] == b"{"):
                # 业务错误被包成 JSON（上面那句 HTTP 检查挡不住这种情况）
                return TTSResult(
                    success=False, provider=self.name, voice_id=req.voice_id,
                    error=f"OpenAI 语音返回错误体：{(r.text or '')[:300]}")
            if not r.content:
                return TTSResult(success=False, provider=self.name,
                                 voice_id=req.voice_id,
                                 error="OpenAI 语音返回了空音频")
            out.write_bytes(r.content)
            return TTSResult(
                success=True, audio_path=str(out),
                duration_seconds=_estimate_duration(req.text, req.rate),
                provider=self.name, voice_id=req.voice_id,
                raw={"model": model, "voice": vid, "size_bytes": len(r.content),
                     "base_url": base})
        except Exception as e:  # noqa: BLE001
            logger.exception("OpenAI TTS failed")
            return TTSResult(success=False, provider=self.name, voice_id=req.voice_id,
                             error=f"OpenAI 语音调用失败：{type(e).__name__}: {e}")


def _estimate_duration(text: str, rate: float) -> float:
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    words = len(re.findall(r"[A-Za-z0-9]+", text))
    return max(1.0, (cjk / 4.2 + words / 2.7) / max(0.25, float(rate or 1.0)))
