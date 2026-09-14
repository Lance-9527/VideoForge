# -*- coding: utf-8 -*-
"""
VideoForge · MiniMax（海螺）语音合成适配器

为什么加这一家：用户已经有 MiniMax 的 Key（LLM 那块在用），
所以这是**零新增注册成本**就能用上的高质量中文音色。

接口（本机已用无效 Key 探过契约，返回 200 + status_code 1004 login fail，
说明 URL 与请求体形状正确）：
    POST https://api.minimaxi.com/v1/t2a_v2
    Header: Authorization: Bearer <key>
    Body:   {model, text, stream:false, voice_setting:{voice_id,speed,vol,pitch},
             audio_setting:{sample_rate,bitrate,format}}
    返回:   {"data":{"audio":"<hex 编码的音频>"}, "base_resp":{"status_code":0}}

注意 MiniMax 的音频是**十六进制字符串**而不是 base64 —— 这点容易写错。
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


logger = logging.getLogger("videoforge.voice.minimax")

API_BASE = "https://api.minimaxi.com/v1"

# MiniMax 系统音色（官方音色列表里的常用项）
MINIMAX_VOICES = [
    ("male-qn-qingse", "青涩青年音色", "male"),
    ("male-qn-jingying", "精英青年音色", "male"),
    ("male-qn-badao", "霸道青年音色", "male"),
    ("male-qn-daxuesheng", "青年大学生音色", "male"),
    ("female-shaonv", "少女音色", "female"),
    ("female-yujie", "御姐音色", "female"),
    ("female-chengshu", "成熟女性音色", "female"),
    ("female-tianmei", "甜美女性音色", "female"),
    ("presenter_male", "男性主持人", "male"),
    ("presenter_female", "女性主持人", "female"),
    ("audiobook_male_1", "男性有声书1", "male"),
    ("audiobook_female_1", "女性有声书1", "female"),
]

# ── 配音模型（2026-09-14）──────────────────────────────────────
# ★ 用户要求「可以真正的去选择配音模型」。这里列的是 MiniMax **语音**模型；
#   先把一件事说清楚，免得再被混淆：
#     **MiniMax M3 不是配音模型**。官方模型日志（2026-06-01）写的是
#     "the latest M-series **language model** for agentic reasoning, tool use,
#      coding, multimodal chat input, and long-context tasks" —— 它是文本/多模态
#     语言模型（`MiniMax-M3`），配音要用的是下面的 `speech-*` 系列。
#     （M3 已按用户要求加进**语言模型**的可选列表，见 `core/llm.py`。）
#
# 型号从哪来（不凭记忆编）：
#   · speech-2.8-hd      —— 官方「同步语音合成 HTTP」文档示例用的就是它
#   · speech-2.8-turbo   —— 官方「同步语音合成 WebSocket」文档示例
#   · speech-2.6-hd/turbo、speech-02-hd/turbo、speech-01-hd/turbo
#                        —— 2.6 见托管方 readme（minimax/speech-2.6-turbo）；
#                           02/01 是 MiniMax 一直在用的老型号，本适配器原先
#                           默认就是 speech-02-hd。
#   型号能不能用**取决于账号**：旧号可能没开通最新型号，届时接口会返回错误，
#   我们把它原样报出来并提示换一个，**不静默改型号**。
MINIMAX_MODELS = [
    {"id": "speech-2.8-hd", "label": "speech-2.8-hd（最新·高保真）",
     "note": "官方同步语音合成文档示例型号，自然度最好，单价最高"},
    {"id": "speech-2.8-turbo", "label": "speech-2.8-turbo（最新·快）",
     "note": "官方 WebSocket 文档示例型号，更快更便宜"},
    {"id": "speech-2.6-hd", "label": "speech-2.6-hd（上一代·高保真）",
     "note": "旧型号，账号未开通 2.8 时可退这一档"},
    {"id": "speech-2.6-turbo", "label": "speech-2.6-turbo（上一代·快）",
     "note": "旧型号，便宜"},
    {"id": "speech-02-hd", "label": "speech-02-hd（老型号·高保真）",
     "note": "本适配器原来的写死默认值，兼容保留"},
    {"id": "speech-02-turbo", "label": "speech-02-turbo（老型号·快）",
     "note": "老型号，便宜"},
]

# 情绪 → MiniMax `voice_setting.emotion` 枚举。
# ★ 官方同步语音合成文档里 `voice_setting` **确实支持 emotion**（示例：happy），
#   而本适配器以前**从来没发过这个字段** —— 剧本写着"愤怒/嘲讽"，声音还是平的。
#   只映射**有把握**的；映射不到就**不发这个参数**（宁可不演，也不要乱演）。
_MINIMAX_EMOTION = {
    "happy": "happy", "joyful": "happy", "excited": "happy", "hopeful": "happy",
    "兴奋": "happy", "希望": "happy", "喜悦": "happy", "开心": "happy",
    "sad": "sad", "grieving": "sad", "pain": "sad",
    "悲伤": "sad", "悲痛": "sad", "哽咽": "sad",
    "angry": "angry", "furious": "angry", "愤怒": "angry", "质问": "angry",
    "fearful": "fearful", "afraid": "fearful", "nervous": "fearful",
    "害怕": "fearful", "恐惧": "fearful", "惊慌": "fearful",
    "disgusted": "disgusted", "厌恶": "disgusted",
    "surprised": "surprised", "惊讶": "surprised", "震惊": "surprised",
    "neutral": "neutral", "calm": "neutral", "平静": "neutral",
    "中性": "neutral", "determined": "neutral", "resolute": "neutral",
    "坚定": "neutral", "低沉": "neutral",
}


@register
class MiniMaxTTS(VoiceProvider):
    """MiniMax（海螺）语音合成"""

    name = "minimax"
    display_name = "MiniMax 语音（海螺）"
    description = ("中文自然度高，情感表现好。用你已配的 MiniMax Key，"
                   "不用另外注册。按字符计费。")
    prefix = "minimax:"
    requires_api_key = True

    # ★ 配音模型可选（用户要求）：默认 speech-2.8-hd（官方文档示例型号）
    supports_model = True
    default_model = "speech-2.8-hd"
    models = MINIMAX_MODELS
    # ★ 型号是**账号相关**的：老账号可能没开通 2.8。真被拒了就按这个顺序
    #   退回（先退同代的上一档 hd，再退本适配器原来一直在用的 speech-02-hd），
    #   并且把"实际用了哪个型号"如实报出来 —— 绝不静默换型号。
    model_fallbacks = ["speech-2.6-hd", "speech-02-hd"]

    async def list_voices(self) -> List[VoiceInfo]:
        return [
            VoiceInfo(
                voice_id=f"minimax:{vid}",
                display_name=f"{label}",
                language="zh-CN",
                gender=gender,
                is_builtin=True,
            )
            for vid, label, gender in MINIMAX_VOICES
        ]

    async def synthesize(self, req: TTSRequest) -> TTSResult:
        if not self.api_key:
            return TTSResult(
                success=False, provider=self.name, voice_id=req.voice_id,
                error="MiniMax 语音未配置 API Key。到「设置 → 🔊 语音合成 API Keys」填上即可。",
            )
        vid = req.voice_id.split(":", 1)[1] if ":" in req.voice_id else req.voice_id
        model = (self.config or {}).get("model") or self.default_model or "speech-2.8-hd"
        # ★ 音高**真的发出去**（以前这里写死 `"pitch": 0`，韵律算出来的
        #   音高偏移在 MiniMax 上等于没算）。MiniMax 的 pitch 是**半音**，
        #   范围 -12~12；`prosody` 给的正是 `pitch_st`（半音）。
        _pitch = 0
        try:
            _pitch = int(round(float(getattr(req, "pitch_semitones", 0.0) or 0.0)))
        except Exception:
            _pitch = 0
        _pitch = max(-12, min(12, _pitch))
        voice_setting = {
            "voice_id": vid,
            "speed": max(0.5, min(2.0, float(req.rate or 1.0))),
            "vol": max(0.1, min(2.0, float(req.volume or 1.0))),
            "pitch": _pitch,
        }
        # ★ 情绪：官方支持 `emotion` 枚举；只发**有把握**的映射，
        #   映射不到就不发（宁可不演，也不要乱演）。
        _emo = _MINIMAX_EMOTION.get(str(getattr(req, "emotion", "") or "").strip().lower())
        if _emo:
            voice_setting["emotion"] = _emo
        body = {
            "model": model,
            "text": req.text,
            "stream": False,
            "voice_setting": voice_setting,
            "audio_setting": {"sample_rate": 32000, "bitrate": 128000, "format": "mp3"},
        }
        out = Path(req.output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            client = await self._ensure_client()
            r = await client.post(
                f"{API_BASE}/t2a_v2", json=body,
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                timeout=180.0)
            if r.status_code != 200:
                return TTSResult(
                    success=False, provider=self.name, voice_id=req.voice_id,
                    error=f"MiniMax 语音 HTTP {r.status_code}: {r.text[:300]}")
            data = r.json() or {}
            base = data.get("base_resp") or {}
            if int(base.get("status_code") or 0) != 0:
                code = base.get("status_code")
                msg = base.get("status_msg") or ""
                hint = ""
                if str(code) == "1008":
                    hint = ("（MiniMax 账号余额不足，需到 platform.minimaxi.com 充值。"
                            "充值前可先改用免费的 Edge TTS 音色。）")
                elif re.search(r"(model|模型)", str(msg), re.I):
                    # ★ 型号是**账号相关**的：老账号可能没开通 2.8。这时**不静默换型号**
                    #   （换了你听到的就不是你选的那个），而是明说 + 给可操作建议。
                    hint = (f"（当前账号可能没开通配音模型「{model}」—— "
                            f"到「设置 → 🎙 配音模型」换一个型号，"
                            f"如 speech-2.6-hd / speech-02-hd。）")
                return TTSResult(
                    success=False, provider=self.name, voice_id=req.voice_id,
                    error=f"MiniMax 语音失败 [{code}] {msg}{hint}")
            hex_audio = ((data.get("data") or {}).get("audio")) or ""
            if not hex_audio:
                return TTSResult(
                    success=False, provider=self.name, voice_id=req.voice_id,
                    error=f"MiniMax 没有返回音频数据：{str(data)[:200]}")
            # ★ MiniMax 返回的是 hex 字符串，不是 base64
            out.write_bytes(bytes.fromhex(hex_audio))
            return TTSResult(
                success=True, audio_path=str(out),
                duration_seconds=_estimate_duration(req.text, req.rate),
                provider=self.name, voice_id=req.voice_id,
                raw={"model": model, "voice": vid, "size_bytes": out.stat().st_size},
            )
        except ValueError as e:
            return TTSResult(success=False, provider=self.name, voice_id=req.voice_id,
                             error=f"MiniMax 返回的音频不是合法 hex：{e}")
        except Exception as e:
            logger.exception("MiniMax TTS failed")
            return TTSResult(success=False, provider=self.name, voice_id=req.voice_id,
                             error=f"MiniMax 语音调用失败：{type(e).__name__}: {e}")


def _estimate_duration(text: str, rate: float) -> float:
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    words = len(re.findall(r"[A-Za-z0-9]+", text))
    return max(1.0, (cjk / 4.2 + words / 2.7) / max(0.5, float(rate or 1.0)))
