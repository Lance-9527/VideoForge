"""
VideoForge · SiliconFlow 硅基流动 TTS 适配器

voice_id 格式：`siliconflow:<model>:<voice>`
例如：`siliconflow:FunAudioLLM/CosyVoice2-0.5B:alex`

API：
- POST https://api.siliconflow.cn/v1/audio/speech
- Body: { model, voice, input, response_format, speed, gain }
- 返回音频二进制
- 鉴权：Authorization: Bearer <api_key>
- 获取用户克隆音色：GET /v1/audio/voice/list

⚠️ 这里曾经有一个**真实缺陷**（用真 Key 实测才发现）：
    官方要求 `voice` 参数**必须写成 `<模型名>:<音色名>`**，例如
    `FunAudioLLM/CosyVoice2-0.5B:alex`。
    旧代码只发了 `alex`，服务端一律回 `400 {"code":20047,"message":"Invalid voice."}`
    —— 于是这 8 个音色在下拉里看着有、选了必失败，用户只能得出"是摆设"的结论。
    文档：https://docs.siliconflow.cn/cn/userguide/capabilities/text-to-speech
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
    SynthesizeError,
    register,
)


logger = logging.getLogger("videoforge.voice.siliconflow")

API_BASE = "https://api.siliconflow.cn/v1"

# CosyVoice2 的 8 个系统预置音色（官方文档 §2.1）
# 展示名用中文，让用户看得懂是什么声音，而不是一串英文代号。
COSYVOICE2_VOICES = [
    ("alex",     "沉稳男声", "male"),
    ("benjamin", "低沉男声", "male"),
    ("charles",  "磁性男声", "male"),
    ("david",    "欢快男声", "male"),
    ("anna",     "沉稳女声", "female"),
    ("bella",    "激情女声", "female"),
    ("claire",   "温柔女声", "female"),
    ("diana",    "欢快女声", "female"),
]

DEFAULT_MODEL = "FunAudioLLM/CosyVoice2-0.5B"


@register
class SiliconFlowTTS(VoiceProvider):
    """硅基流动 TTS"""

    name = "siliconflow"
    display_name = "硅基流动 SiliconFlow"
    description = ("CosyVoice2-0.5B 中文质量好、8 个预置音色、支持情感与方言，"
                   "约 ¥0.5/万字。需要 API Key（cloud.siliconflow.cn → API Keys）。")
    prefix = "siliconflow:"
    requires_api_key = True

    # ★ 配音模型：硅基流动的模型是**跟着音色走的** ——
    #   voice_id 本身就是 `siliconflow:<模型>:<音色>`（见本文件开头），
    #   所以"选模型"这件事在这里等价于"选音色"，设置里再放一个全局型号
    #   只会两边打架。如实说明，不做一个假的全局开关。
    supports_model = False
    model_note = ("模型跟着音色走（voice_id 形如 siliconflow:FunAudioLLM/"
                  "CosyVoice2-0.5B:alex），在音色里选即等价于选模型")

    async def list_voices(self) -> List[VoiceInfo]:
        out = [
            VoiceInfo(
                voice_id=f"siliconflow:{DEFAULT_MODEL}:{name}",
                display_name=f"{label}（{name}）· CosyVoice2",
                language="zh-CN",
                gender=gender,
                is_builtin=True,
            )
            for name, label, gender in COSYVOICE2_VOICES
        ]
        # 用户自己克隆的音色（需实名认证 + 上传参考音频）也一并列出来
        try:
            out.extend(await self.list_cloned_voices())
        except Exception as e:
            logger.info("拉取硅基流动克隆音色失败（不影响预置音色）：%s", e)
        return out

    async def list_cloned_voices(self) -> List[VoiceInfo]:
        """GET /v1/audio/voice/list → 用户预置/克隆的音色"""
        if not self.api_key:
            return []
        client = await self._ensure_client()
        r = await client.get(f"{API_BASE}/audio/voice/list",
                             headers={"Authorization": f"Bearer {self.api_key}"},
                             timeout=60.0)
        if r.status_code != 200:
            return []
        data = r.json() or {}
        items = data.get("result") if isinstance(data, dict) else data
        out: List[VoiceInfo] = []
        for it in (items or []):
            if not isinstance(it, dict):
                continue
            uri = it.get("uri") or ""
            if not uri:
                continue
            # 克隆音色的 uri 形如 speech:<name>:<id>:<hash>，直接当 voice 用
            out.append(VoiceInfo(
                voice_id=f"siliconflow:{uri}",
                display_name=f"[我的克隆音色] {it.get('customName') or it.get('name') or uri}",
                language="zh-CN", gender="unknown", is_builtin=False))
        return out

    async def synthesize(self, req: TTSRequest) -> TTSResult:
        if not self.api_key:
            return TTSResult(
                success=False,
                provider=self.name,
                voice_id=req.voice_id,
                error="硅基流动 TTS 未配置 API Key。到「设置 → 🔊 语音合成 API Keys」"
                      "填 SiliconFlow Key 即可（cloud.siliconflow.cn → API Keys）。",
            )

        # 解析 voice_id: "siliconflow:<model>:<voice>"；也兼容 "siliconflow:<uri>"
        rest = req.voice_id.split(":", 1)[1] if ":" in req.voice_id else req.voice_id
        if rest.startswith("speech:"):
            # 用户克隆音色：uri 本身就是完整的 voice 值
            model, voice = DEFAULT_MODEL, rest
        elif ":" in rest:
            model, voice = rest.split(":", 1)
        else:
            model, voice = DEFAULT_MODEL, rest
        voice = voice.strip()
        # ★ 官方要求：预置音色必须写成 "<模型名>:<音色名>"
        if not voice.startswith("speech:") and not voice.startswith(f"{model}:"):
            voice = f"{model}:{voice}"

        body = {
            "model": model,
            "voice": voice,
            "input": req.text,
            "response_format": "mp3",
            "sample_rate": 44100,
            # 官方取值范围 [0.25, 4.0]
            "speed": max(0.25, min(4.0, float(req.rate or 1.0))),
            "gain": max(-10.0, min(10.0, (float(req.volume or 1.0) - 1.0) * 10)),
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        out = Path(req.output_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        try:
            client = await self._ensure_client()
            r = await client.post(f"{API_BASE}/audio/speech", json=body, headers=headers)

            if r.status_code != 200:
                err_text = r.text[:300]
                err_text = re.sub(r"://[^/@\s]+:[^/@\s]+@", "://***:***@", err_text)
                hint = ""
                if "20047" in err_text or "Invalid voice" in err_text:
                    hint = (f"（提示：音色名要写成「模型名:音色名」，例如 "
                            f"{model}:alex；当前发的是「{voice}」）")
                elif "30001" in err_text or "balance" in err_text.lower():
                    hint = "（硅基流动账户余额不足，需到 cloud.siliconflow.cn 充值）"
                return TTSResult(
                    success=False, provider=self.name, voice_id=req.voice_id,
                    error=f"硅基流动 TTS HTTP {r.status_code}: {err_text}{hint}",
                )

            out.write_bytes(r.content)
            return TTSResult(
                success=True,
                audio_path=str(out),
                duration_seconds=self._estimate_duration(req.text, req.rate),
                provider=self.name,
                voice_id=req.voice_id,
                raw={"model": model, "voice": voice, "size_bytes": len(r.content)},
            )
        except SynthesizeError:
            raise
        except Exception as e:
            logger.exception("SiliconFlow TTS failed")
            return TTSResult(
                success=False, provider=self.name, voice_id=req.voice_id,
                error=f"硅基流动 TTS 调用失败：{type(e).__name__}: {e}",
            )

    @staticmethod
    def _estimate_duration(text: str, rate: float) -> float:
        """估算时长（中英文混合）"""
        cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
        words = len(re.findall(r"[A-Za-z0-9]+", text))
        # 中文 4.2 字/秒、英文 2.7 词/秒
        base = cjk / 4.2 + words / 2.7
        return max(1.0, base / max(0.25, float(rate or 1.0)))
