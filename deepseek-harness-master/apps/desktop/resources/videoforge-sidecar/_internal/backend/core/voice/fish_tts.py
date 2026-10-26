# -*- coding: utf-8 -*-
"""VideoForge · Fish Audio TTS 适配器

注册表里 `fish` 一直只有目录。本文件按官方 HTTP 协议接上。

接口：
    POST https://api.fish.audio/v1/tts
    Header: Authorization: Bearer <key>
            model: fish-speech-1.5        ← 模型放在**请求头**（不在 body 里）
    Body:   {"text": "...", "format": "mp3", "chunk_length": 200,
             "normalize": true, "latency": "normal",
             "reference_id": "<音色模型 id>"}    ← 可选
    返回:   音频二进制（audio/mpeg）

★ 注意：Fish 的模型是走 `model` 请求头的；body 里的 `reference_id`
  才是"用哪个克隆音色"（用户在 fish.audio 上训练/发布的声音 id）。
  没给 reference_id 就用默认音色。
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


logger = logging.getLogger("videoforge.voice.fish")

DEFAULT_BASE = "https://api.fish.audio"
API_PATH = "/v1/tts"

FISH_MODELS = [
    {"id": "fish-speech-1.5", "label": "fish-speech-1.5（默认）",
     "note": "官方当前主力型号；模型名走 `model` 请求头"},
    {"id": "speech-1.6", "label": "speech-1.6（新）",
     "note": "更新的型号（账号权限允许时可用）"},
    {"id": "s1", "label": "s1", "note": "官方短名写法，等价于最新 speech 系列"},
]


@register
class FishTTS(VoiceProvider):
    """Fish Audio（可克隆音色）"""

    name = "fish"
    display_name = "Fish Audio"
    description = ("Fish Audio 语音合成，支持自训练音色（reference_id）。"
                   "需要 fish.audio 的 API Key。")
    prefix = "fish:"
    requires_api_key = True

    supports_model = True
    default_model = "fish-speech-1.5"
    models = FISH_MODELS
    verification = "implemented（本机无 Fish Key，未做真实合成验证）"
    #: dispatcher 需要把 settings（region / 三件套 / 参考音频）传下来
    wants_settings = True

    async def list_voices(self) -> List[VoiceInfo]:
        # Fish 的音色是用户自己训练/收藏的 reference_id，没有"官方固定清单"。
        # 与其编一批假 id，不如给一个"默认音色"入口 + 说明怎么填自己的 id。
        return [
            VoiceInfo(voice_id="fish:", display_name="Fish 默认音色（不指定 reference_id）",
                      language="zh-CN", gender="unknown", is_builtin=True),
            VoiceInfo(voice_id="fish:custom", display_name="我的 Fish 音色（把 custom 换成 reference_id）",
                      language="zh-CN", gender="unknown", is_builtin=False),
        ]

    async def synthesize(self, req: TTSRequest) -> TTSResult:
        if not self.api_key:
            return TTSResult(
                success=False, provider=self.name, voice_id=req.voice_id,
                error=("Fish Audio 未配置 API Key。到「设置 → 🔊 语音合成 API Keys」"
                       "填 Fish Key（`tts_api_keys.fish`）即可。"),
            )
        rest = req.voice_id.split(":", 1)[1] if ":" in req.voice_id else req.voice_id
        ref_id = "" if rest in ("", "custom", "default") else rest
        model = (self.config or {}).get("model") or self.default_model
        base = resolve_base_url(self.name, DEFAULT_BASE, config=self.config,
                                env=("FISH_BASE_URL",))
        body = {
            "text": req.text,
            "format": "mp3",
            "chunk_length": 200,
            "normalize": True,
            "latency": "normal",
        }
        if ref_id:
            body["reference_id"] = ref_id

        out = Path(req.output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            client = await self._ensure_client()
            r = await client.post(
                f"{base}{API_PATH}", json=body,
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json",
                         "model": model},
                timeout=180.0)
            if r.status_code >= 400:
                return TTSResult(
                    success=False, provider=self.name, voice_id=req.voice_id,
                    error=f"Fish Audio HTTP {r.status_code}: {(r.text or '')[:300]}")
            ctype = (r.headers.get("content-type") or "").lower()
            if "json" in ctype or r.content[:1] == b"{":
                return TTSResult(
                    success=False, provider=self.name, voice_id=req.voice_id,
                    error=f"Fish Audio 返回错误体：{(r.text or '')[:300]}")
            if not r.content:
                return TTSResult(success=False, provider=self.name,
                                 voice_id=req.voice_id,
                                 error="Fish Audio 返回了空音频")
            out.write_bytes(r.content)
            return TTSResult(
                success=True, audio_path=str(out),
                duration_seconds=_estimate_duration(req.text, req.rate),
                provider=self.name, voice_id=req.voice_id,
                raw={"model": model, "reference_id": ref_id,
                     "size_bytes": len(r.content), "base_url": base})
        except Exception as e:  # noqa: BLE001
            logger.exception("Fish TTS failed")
            return TTSResult(success=False, provider=self.name, voice_id=req.voice_id,
                             error=f"Fish Audio 调用失败：{type(e).__name__}: {e}")


def _estimate_duration(text: str, rate: float) -> float:
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    words = len(re.findall(r"[A-Za-z0-9]+", text))
    return max(1.0, (cjk / 4.2 + words / 2.7) / max(0.25, float(rate or 1.0)))
