# -*- coding: utf-8 -*-
"""VideoForge · 本地 GPT-SoVITS / IndexTTS 适配器

注册表里 `local` 指向 `http://127.0.0.1:9880` + `/tts` —— 这是社区
**GPT-SoVITS api_v2.py** 的标准地址。本文件按那套协议接上。

接口（GPT-SoVITS api_v2）：
    POST http://127.0.0.1:9880/tts
    Body: {"text": "...", "text_lang": "zh",
           "ref_audio_path": "D:/voices/ref.wav",   ← **必填**
           "prompt_text": "参考音频对应的文本",      ← 强烈建议填（质量差别大）
           "prompt_lang": "zh",
           "media_type": "mp3", "streaming_mode": false,
           "speed_factor": 1.0}
    返回: 音频二进制

★ `ref_audio_path` 是**必需的**：GPT-SoVITS 是"零样本克隆"，没有参考音频
  就没有声音可模仿。少了它服务端只回 400 —— 所以本适配器先在本地把
  「缺参考音频」这件事说清楚，并告诉用户去哪配。

配置位置（任选）：
    · settings["gpt_sovits_ref_audio"] / ["local_ref_audio"]
    · tts_extra.local = {"ref_audio": "...", "prompt_text": "...", "prompt_lang": "zh"}
    · voice_id 里直接带：`local:D:/voices/ref.wav`
    · 环境变量 GPT_SOVITS_REF_AUDIO
"""

from __future__ import annotations

import logging
import os
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
from ._credentials import resolve_base_url, resolve_value


logger = logging.getLogger("videoforge.voice.local")

DEFAULT_BASE = "http://127.0.0.1:9880"
API_PATH = "/tts"

_REF_PART = {
    "key": "ref_audio",
    "label": "参考音频 ref_audio_path（GPT-SoVITS 零样本克隆必需）",
    "aliases": ["ref_audio", "ref_audio_path", "reference_audio"],
    "settings": ["gpt_sovits_ref_audio", "local_ref_audio", "gpt_sovits_ref"],
    "env": ["GPT_SOVITS_REF_AUDIO"],
}


@register
class LocalTTS(VoiceProvider):
    """本地 GPT-SoVITS / IndexTTS（不花钱，需要自建服务）"""

    name = "local"
    display_name = "本地 GPT-SoVITS"
    description = ("本机跑的 GPT-SoVITS（零样本音色克隆，不花钱、不出网）。"
                   "需要先启动 api_v2.py，并给一段参考音频。")
    prefix = "local:"
    # 本地服务默认不鉴权 → 不需要 Key（填了也照发）
    requires_api_key = False

    supports_model = False
    model_note = ("本地服务没有 model 参数：音色由 `ref_audio_path` 参考音频决定，"
                  "换参考音频就是换音色")
    verification = "implemented（本机未检测到 9880 服务，未做真实合成验证）"
    #: dispatcher 需要把 settings（region / 三件套 / 参考音频）传下来
    wants_settings = True

    def credential_gaps(self, settings: dict | None = None) -> List[str]:
        from ._credentials import credential_gaps as _gaps
        conf = {"settings": settings or {}}
        return _gaps(self.name, [_REF_PART], config=conf, primary=self.api_key)

    async def list_voices(self) -> List[VoiceInfo]:
        return [
            VoiceInfo(voice_id="local:", display_name="本地音色（用设置里的参考音频）",
                      language="zh-CN", gender="unknown", is_builtin=True),
        ]

    def _resolve_ref(self, req: TTSRequest) -> dict:
        """参考音频 / 提示文本 / 语言：voice_id 里带的优先，其次设置/环境变量。"""
        settings = (self.config or {}).get("settings") or {}
        conf = {"settings": settings}
        ref, src = resolve_value(self.name, _REF_PART, config=conf,
                                 primary=self.api_key)
        rest = req.voice_id.split(":", 1)[1] if ":" in req.voice_id else req.voice_id
        if rest and rest not in ("", "custom", "default"):
            ref, src = rest, "voice_id"
        # prompt_text / prompt_lang 从 tts_extra.local 里取（可选）
        extra = settings.get("tts_extra")
        per = (extra or {}).get("local") if isinstance(extra, dict) else {}
        per = per if isinstance(per, dict) else {}
        prompt_text = str(per.get("prompt_text") or settings.get("gpt_sovits_prompt_text") or "")
        prompt_lang = str(per.get("prompt_lang") or settings.get("gpt_sovits_prompt_lang") or "zh")
        text_lang = str(per.get("text_lang") or settings.get("gpt_sovits_text_lang") or "zh")
        return {"ref_audio": ref, "source": src, "prompt_text": prompt_text,
                "prompt_lang": prompt_lang, "text_lang": text_lang}

    async def synthesize(self, req: TTSRequest) -> TTSResult:
        info = self._resolve_ref(req)
        ref = info["ref_audio"]
        if not ref:
            return TTSResult(
                success=False, provider=self.name, voice_id=req.voice_id,
                error=("本地 GPT-SoVITS **缺少参考音频 ref_audio_path**。"
                       "填法（三选一）：① 设置里加 gpt_sovits_ref_audio=D:/voices/ref.wav；"
                       "② tts_extra.local = {\"ref_audio\":\"...\"}；"
                       "③ 音色 id 直接写 local:D:/voices/ref.wav。"
                       "（GPT-SoVITS 是零样本克隆，必须有参考音频才有声音可模仿）"),
            )
        base = resolve_base_url(self.name, DEFAULT_BASE, config=self.config,
                                env=("GPT_SOVITS_BASE_URL", "LOCAL_TTS_BASE_URL"))
        body = {
            "text": req.text,
            "text_lang": info["text_lang"],
            "ref_audio_path": ref,
            "prompt_text": info["prompt_text"],
            "prompt_lang": info["prompt_lang"],
            "media_type": "mp3",
            "streaming_mode": False,
            "speed_factor": max(0.25, min(4.0, float(req.rate or 1.0))),
        }

        out = Path(req.output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        # ★ 本地服务必须绕开系统代理：实测代理开着时 127.0.0.1 也会被吞，
        #   服务明明起着却连接超时（视频/图像那边都踩过同一个坑）。
        import httpx as _httpx
        try:
            async with _httpx.AsyncClient(
                    timeout=_httpx.Timeout(connect=10.0, read=300.0,
                                           write=60.0, pool=10.0),
                    trust_env=False) as client:
                r = await client.post(f"{base}{API_PATH}", json=body)
                if r.status_code >= 400:
                    return TTSResult(
                        success=False, provider=self.name, voice_id=req.voice_id,
                        error=(f"本地 GPT-SoVITS HTTP {r.status_code}: "
                               f"{(r.text or '')[:300]}"
                               f"（参考音频是否存在：{ref}；"
                               f"服务地址：{base}）"))
                ctype = (r.headers.get("content-type") or "").lower()
                if "json" in ctype or r.content[:1] == b"{":
                    return TTSResult(
                        success=False, provider=self.name, voice_id=req.voice_id,
                        error=f"本地 GPT-SoVITS 返回错误体：{(r.text or '')[:300]}")
                if not r.content:
                    return TTSResult(success=False, provider=self.name,
                                     voice_id=req.voice_id,
                                     error="本地 GPT-SoVITS 返回了空音频")
                out.write_bytes(r.content)
            return TTSResult(
                success=True, audio_path=str(out),
                duration_seconds=_estimate_duration(req.text, req.rate),
                provider=self.name, voice_id=req.voice_id,
                raw={"ref_audio": ref, "ref_source": info["source"],
                     "base_url": base, "size_bytes": len(r.content)})
        except Exception as e:  # noqa: BLE001
            logger.exception("Local GPT-SoVITS TTS failed")
            return TTSResult(
                success=False, provider=self.name, voice_id=req.voice_id,
                error=(f"本地 GPT-SoVITS 调用失败：{type(e).__name__}: {e}"
                       f"（服务地址 {base}；没启动的话先跑 api_v2.py）"))


def _estimate_duration(text: str, rate: float) -> float:
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    words = len(re.findall(r"[A-Za-z0-9]+", text))
    return max(1.0, (cjk / 4.2 + words / 2.7) / max(0.25, float(rate or 1.0)))
