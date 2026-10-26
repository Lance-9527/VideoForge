# -*- coding: utf-8 -*-
"""
VideoForge · 阿里云百炼（DashScope）语音合成适配器

覆盖 CosyVoice 与 Qwen-Audio-TTS 两条线，同一个 Key（DashScope API Key）。

接口（本机已用无效 Key 探过契约：返回 401 InvalidApiKey，
说明 URL 与请求体形状正确）：
    POST https://dashscope.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer
    Header: Authorization: Bearer <key>
    Body:   {model, input:{text, voice, format, sample_rate}}
    返回:   {"output":{"audio":{"url":"https://..."}}}   ← 音频是一个 24 小时有效的 URL

也就是说这一家返回的是**下载链接**而不是二进制，需要再拉一次。
文档：https://help.aliyun.com/zh/model-studio/cosyvoice-tts-http-api
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


logger = logging.getLogger("videoforge.voice.dashscope")

ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer"

# CosyVoice 系统音色（官方音色列表常用项）
COSYVOICE_VOICES = [
    ("longxiaochun_v2", "龙小淳 · 知性女声", "female"),
    ("longxiaoxia_v2", "龙小夏 · 沉稳女声", "female"),
    ("longxiaocheng_v2", "龙小诚 · 男声", "male"),
    ("longxiaobai_v2", "龙小白 · 亲和女声", "female"),
    ("longlaotie_v2", "龙老铁 · 东北男声", "male"),
    ("longshu_v2", "龙书 · 有声书男声", "male"),
    ("longshuo_v2", "龙硕 · 新闻男声", "male"),
    ("longjing_v2", "龙婧 · 知性女声", "female"),
    ("longmiao_v2", "龙妙 · 可爱女声", "female"),
    ("longyue_v2", "龙悦 · 温柔女声", "female"),
]


@register
class DashScopeTTS(VoiceProvider):
    """阿里云百炼 CosyVoice / Qwen-Audio-TTS"""

    name = "dashscope"
    display_name = "阿里云百炼（CosyVoice）"
    description = ("CosyVoice 中文自然、支持情感与方言；与通义千问共用同一个 "
                   "DashScope API Key。按字符计费。")
    prefix = "dashscope:"
    requires_api_key = True

    # ★ 配音模型可选。只列**我们有依据**的型号：`cosyvoice-v2` 是本适配器一直在用、
    #   且已在阿里云文档里存在的版本。百炼还会持续出新版本，届时在这里加一行即可
    #   —— **不凭记忆编型号**。
    supports_model = True
    default_model = "cosyvoice-v2"
    models = [
        {"id": "cosyvoice-v2", "label": "cosyvoice-v2（默认）",
         "note": "本适配器一直使用的版本，中文自然、支持情感"},
        {"id": "cosyvoice-v1", "label": "cosyvoice-v1（旧版）",
         "note": "更早的版本，音色较少；账号没开通 v2 时可试"},
    ]

    async def list_voices(self) -> List[VoiceInfo]:
        return [
            VoiceInfo(
                voice_id=f"dashscope:{vid}",
                display_name=f"{label}",
                language="zh-CN",
                gender=gender,
                is_builtin=True,
            )
            for vid, label, gender in COSYVOICE_VOICES
        ]

    async def synthesize(self, req: TTSRequest) -> TTSResult:
        if not self.api_key:
            return TTSResult(
                success=False, provider=self.name, voice_id=req.voice_id,
                error="阿里云百炼未配置 API Key。到「设置 → 🔊 语音合成 API Keys」"
                      "填 DashScope Key 即可（和通义千问是同一个 Key）。",
            )
        vid = req.voice_id.split(":", 1)[1] if ":" in req.voice_id else req.voice_id
        model = (self.config or {}).get("model") or "cosyvoice-v2"
        body = {
            "model": model,
            "input": {
                "text": req.text,
                "voice": vid,
                "format": "mp3",
                "sample_rate": 22050,
                "rate": max(0.5, min(2.0, float(req.rate or 1.0))),
                "volume": int(max(0, min(100, float(req.volume or 1.0) * 50))),
            },
        }
        out = Path(req.output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            client = await self._ensure_client()
            r = await client.post(
                ENDPOINT, json=body,
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                timeout=180.0)
            if r.status_code != 200:
                return TTSResult(
                    success=False, provider=self.name, voice_id=req.voice_id,
                    error=f"阿里云百炼语音 HTTP {r.status_code}: {r.text[:300]}")
            data = r.json() or {}
            audio = ((data.get("output") or {}).get("audio")) or {}
            url = audio.get("url") or ""
            if not url:
                return TTSResult(
                    success=False, provider=self.name, voice_id=req.voice_id,
                    error=f"阿里云百炼没有返回音频链接：{str(data)[:200]}")
            # 这一家给的是 24 小时有效的下载链接，需要再拉一次
            audio_resp = await client.get(url, timeout=180.0)
            if audio_resp.status_code != 200:
                return TTSResult(
                    success=False, provider=self.name, voice_id=req.voice_id,
                    error=f"下载合成音频失败 HTTP {audio_resp.status_code}")
            out.write_bytes(audio_resp.content)
            chars = ((data.get("usage") or {}).get("characters"))
            return TTSResult(
                success=True, audio_path=str(out),
                duration_seconds=_estimate_duration(req.text, req.rate),
                provider=self.name, voice_id=req.voice_id,
                raw={"model": model, "voice": vid, "billed_chars": chars,
                     "size_bytes": out.stat().st_size},
            )
        except Exception as e:
            logger.exception("DashScope TTS failed")
            return TTSResult(success=False, provider=self.name, voice_id=req.voice_id,
                             error=f"阿里云百炼语音调用失败：{type(e).__name__}: {e}")


def _estimate_duration(text: str, rate: float) -> float:
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    words = len(re.findall(r"[A-Za-z0-9]+", text))
    return max(1.0, (cjk / 4.2 + words / 2.7) / max(0.5, float(rate or 1.0)))
