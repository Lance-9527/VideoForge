# -*- coding: utf-8 -*-
"""VideoForge · Azure 语音服务（Speech Services）适配器

注册表里 `azure` 的 base_url 是 `https://{region}.tts.speech.microsoft.com`
—— 带 `{region}` 占位，说明这一家**必须有 region**。本文件把
「region 从哪来、缺了怎么报」写清楚。

接口：
    POST https://{region}.tts.speech.microsoft.com/cognitiveservices/v1
    Header: Ocp-Apim-Subscription-Key: <key>     ← 不是 Bearer
            Content-Type: application/ssml+xml
            X-Microsoft-OutputFormat: audio-24khz-48kbitrate-mono-mp3
    Body:   SSML（**不是 JSON**）
    返回:   音频二进制（audio/mpeg）

★ region 的解析（按优先级）：
    ① `tts_api_keys.azure` 写成组合串 "region=eastasia;key=xxxx"
    ② settings["azure_region"] / settings["azure_region_name"]
    ③ 环境变量 AZURE_SPEECH_REGION / SPEECH_REGION
    ④ `tts_extra.azure.region`
  缺 region 时的报错会**直说缺 region**（以及去哪填），不会只回一句参数错误。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, List
from xml.sax.saxutils import escape as _xml_escape

from .base import (
    TTSRequest,
    TTSResult,
    VoiceInfo,
    VoiceProvider,
    register,
)
from ._credentials import credential_gaps, resolve_parts


logger = logging.getLogger("videoforge.voice.azure")

OUTPUT_FORMAT = "audio-24khz-48kbitrate-mono-mp3"

#: region 这件"凭据"的定义（给 _credentials 解析用）
_REGION_PART = {
    "key": "region",
    "label": "区域 region（如 eastasia / eastus）",
    "aliases": ["region", "region_name", "location"],
    "settings": ["azure_region", "azure_region_name", "azure_location"],
    "env": ["AZURE_SPEECH_REGION", "SPEECH_REGION", "AZURE_REGION"],
}

#: Azure 神经音色（官方音色表里的常用项；中文/英文各取一批）
AZURE_VOICES = [
    ("zh-CN-XiaoxiaoNeural", "晓晓 · 温暖女声", "zh-CN", "female"),
    ("zh-CN-YunxiNeural", "云希 · 阳光男声", "zh-CN", "male"),
    ("zh-CN-YunjianNeural", "云健 · 体育男声", "zh-CN", "male"),
    ("zh-CN-XiaoyiNeural", "晓伊 · 文艺女声", "zh-CN", "female"),
    ("zh-CN-YunyangNeural", "云扬 · 新闻男声", "zh-CN", "male"),
    ("zh-CN-YunyeNeural", "云野 · 沉稳男声", "zh-CN", "male"),
    ("zh-CN-XiaochenNeural", "晓辰 · 甜美女声", "zh-CN", "female"),
    ("en-US-JennyNeural", "Jenny · 女声", "en-US", "female"),
    ("en-US-GuyNeural", "Guy · 男声", "en-US", "male"),
    ("en-US-AriaNeural", "Aria · 女声", "en-US", "female"),
]

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"


@register
class AzureTTS(VoiceProvider):
    """Azure 语音服务（SSML）"""

    name = "azure"
    display_name = "Azure 语音服务"
    description = ("微软 Azure 神经语音（SSML，能控语速/音高/风格）。"
                   "需要订阅 Key **+ region** 两样。")
    prefix = "azure:"
    requires_api_key = True

    # ★ Azure 的"模型"就是音色本身（接口没有 model 字段），不做假开关
    supports_model = False
    model_note = ("Azure 接口没有 model 参数：音色（如 zh-CN-XiaoxiaoNeural）"
                  "即模型，选音色就是选模型")
    verification = "implemented（本机无 Azure Key/region，未做真实合成验证）"
    #: dispatcher 需要把 settings（region / 三件套 / 参考音频）传下来
    wants_settings = True

    def credential_gaps(self, settings: Dict[str, object] | None = None) -> List[str]:
        """还缺哪些多件套（dispatcher 用它把 usable 标诚实）。"""
        conf = {"settings": settings or {}}
        return credential_gaps(self.name, [_REGION_PART], config=conf,
                               primary=self.api_key)

    async def list_voices(self) -> List[VoiceInfo]:
        return [
            VoiceInfo(voice_id=f"azure:{vid}", display_name=label,
                      language=lang, gender=gender, is_builtin=True)
            for vid, label, lang, gender in AZURE_VOICES
        ]

    @staticmethod
    def _rate_attr(rate: float) -> str:
        """倍速 → SSML prosody rate（百分比）。"""
        pct = int(round((max(0.25, min(4.0, float(rate or 1.0))) - 1.0) * 100))
        return f"{pct:+d}%"

    @staticmethod
    def _pitch_attr(semitones: float) -> str:
        """半音 → SSML prosody pitch（百分比；0 就不写）。"""
        st = float(semitones or 0.0)
        if abs(st) < 0.01:
            return ""
        pct = int(round(((2 ** (st / 12.0)) - 1.0) * 100))
        return f"{pct:+d}%"

    def build_ssml(self, voice: str, text: str, req: TTSRequest,
                   language: str = "zh-CN") -> str:
        """拼 SSML。**文本必须转义** —— 中文引号没事，但 `<` `&` 会直接让请求 400。"""
        rate = self._rate_attr(req.rate)
        pitch = self._pitch_attr(getattr(req, "pitch_semitones", 0.0))
        inner = _xml_escape(str(text or ""))
        prosody_attrs = f' rate="{rate}"'
        if pitch:
            prosody_attrs += f' pitch="{pitch}"'
        return (
            f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
            f'xml:lang="{language}">'
            f'<voice name="{voice}">'
            f'<prosody{prosody_attrs}>{inner}</prosody>'
            f"</voice></speak>"
        )

    async def synthesize(self, req: TTSRequest) -> TTSResult:
        if not self.api_key:
            return TTSResult(
                success=False, provider=self.name, voice_id=req.voice_id,
                error=("Azure 语音未配置 API Key。到「设置 → 🔊 语音合成 API Keys」"
                       "填 Azure 订阅 Key（`tts_api_keys.azure`）即可。"),
            )
        # region：多件套解析（缺就明确说缺哪一项）
        values, missing, sources = resolve_parts(
            self.name, [_REGION_PART],
            config={"settings": (self.config or {}).get("settings") or {}},
            primary=self.api_key)
        if missing:
            return TTSResult(
                success=False, provider=self.name, voice_id=req.voice_id,
                error=(f"Azure 语音**缺少 {', '.join(missing)}**。"
                       f"填法（三选一）："
                       f"① tts_api_keys.azure 写成 \"region=eastasia;key=你的Key\"；"
                       f"② 设置里加 azure_region=eastasia；"
                       f"③ 环境变量 AZURE_SPEECH_REGION。"),
            )
        region = values.get("region", "")
        # ★ 组合串写法下，`self.api_key` 是整串 "region=..;key=.."，
        #   真正要发给 Azure 的是其中那件 key —— 这里取出来，别把整串当订阅 Key。
        from ._credentials import parse_credential_blob
        blob = parse_credential_blob(self.api_key)
        subscription_key = (blob.get("key") or blob.get("subscription_key")
                            or blob.get("api_key") or self.api_key)
        vid = req.voice_id.split(":", 1)[1] if ":" in req.voice_id else req.voice_id
        vid = vid or DEFAULT_VOICE
        base = str((self.config or {}).get("base_url") or "").strip().rstrip("/")
        if not base:
            base = f"https://{region}.tts.speech.microsoft.com"
        ssml = self.build_ssml(vid, req.text, req, language=req.language or "zh-CN")

        out = Path(req.output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            client = await self._ensure_client()
            r = await client.post(
                f"{base}/cognitiveservices/v1", content=ssml.encode("utf-8"),
                headers={"Ocp-Apim-Subscription-Key": subscription_key,
                         "Content-Type": "application/ssml+xml",
                         "X-Microsoft-OutputFormat": OUTPUT_FORMAT,
                         "User-Agent": "VideoForge/1.0"},
                timeout=180.0)
            if r.status_code >= 400:
                return TTSResult(
                    success=False, provider=self.name, voice_id=req.voice_id,
                    error=f"Azure 语音 HTTP {r.status_code}: {(r.text or '')[:300]}")
            if not r.content or r.content[:1] == b"{":
                return TTSResult(
                    success=False, provider=self.name, voice_id=req.voice_id,
                    error=f"Azure 语音返回错误体：{(r.text or '')[:300]}")
            out.write_bytes(r.content)
            return TTSResult(
                success=True, audio_path=str(out),
                duration_seconds=_estimate_duration(req.text, req.rate),
                provider=self.name, voice_id=req.voice_id,
                raw={"voice": vid, "region": region, "region_source": sources.get("region"),
                     "base_url": base, "size_bytes": len(r.content),
                     "output_format": OUTPUT_FORMAT})
        except Exception as e:  # noqa: BLE001
            logger.exception("Azure TTS failed")
            return TTSResult(success=False, provider=self.name, voice_id=req.voice_id,
                             error=f"Azure 语音调用失败：{type(e).__name__}: {e}")


def _estimate_duration(text: str, rate: float) -> float:
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    words = len(re.findall(r"[A-Za-z0-9]+", text))
    return max(1.0, (cjk / 4.2 + words / 2.7) / max(0.25, float(rate or 1.0)))
