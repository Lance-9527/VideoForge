# -*- coding: utf-8 -*-
"""
VideoForge · agentai 网关语音（中转站里最好用的那批 TTS）
=========================================================

用户原话（2026-10-23）：
    「声音有很大问题！后面出音色——**不要用 TTS 本地了**！要用好的声音模型，
      比如我的中转站里面的先进的模型，主要是要配好，声音这块省钱是其次！！！」

## 为什么要有这个 provider

原来的兜底是 **Edge TTS**（免费、本地、**不支持情感**）—— 整片"AI 感"主要来自这里。
付费的 MiniMax / SiliconFlow 要单独配 Key，用户还得逐家去开。

而用户的中转站（`https://agentai.token6688.com/v1`）**已经**把这些最好的语音模型
挂成 OpenAI 兼容接口了，实测这一轮探到的可用模型：

    speech-2.8            MiniMax 最新语音（中文自然度/情感最强）  ← 默认
    gpt-4o-mini-tts       OpenAI 语音
    gemini-2.5-pro-tts    Gemini 语音
    gemini-3.1-flash-tts  Gemini 轻量语音
    doubao-tts-2.0        火山豆包 TTS（音色 ID 走豆包自己那套，需单独映射）
    voice-clone / voice-design   MiniMax 声音克隆 / 声音设计（下一步接）

所以只要有一把 `agentai` Key，就能直接用上「先进语音模型」，
不需要再让用户去 MiniMax 官网单独充值。

## 实测（本机，2026-10-23）

    POST /v1/audio/speech  {model:"speech-2.8", voice:"female-chengshu",
                            input:"这是测试。", response_format:"mp3"}
    → 200  audio/mpeg  23,028 bytes  （ID3 头，可直接进混音）

    POST 同接口 {model:"doubao-tts-2.0", voice:"zh_female_cancan"} → 503
    `资源ID与发言者相关资源不匹配`（豆包的音色必须用豆包自己的 voice_type，
     不能拿 MiniMax 的音色名 —— 已记录，下轮映射）

## voice_id 约定

    agentai:<模型>:<音色>        例：agentai:speech-2.8:female-chengshu
    agentai:<音色>               例：agentai:female-chengshu  → 用默认模型

**同一角色跨镜一致**靠的是库里 `characters.voice_id` 这一列：
选角一次、写回角色卡，之后每一句都用同一个 `voice_id`（见 `core/voicecast.py`）。
本 provider 只负责"按 voice_id 忠实发出同一把嗓子"，不自己做随机挑。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import (
    TTSRequest,
    TTSResult,
    VoiceInfo,
    VoiceProvider,
    register,
)

logger = logging.getLogger(__name__)

API_BASE = "https://agentai.token6688.com/v1"

#: 中转站上实测可用的语音模型（顺序 = 默认优先级）
AGENTAI_TTS_MODELS = [
    ("speech-2.8", "MiniMax speech-2.8（中文/情感最强）"),
    ("gpt-4o-mini-tts", "OpenAI gpt-4o-mini-tts"),
    ("gemini-2.5-pro-tts", "Gemini 2.5 Pro TTS"),
    ("gemini-3.1-flash-tts", "Gemini 3.1 Flash TTS（便宜快）"),
]

#: 默认模型：MiniMax speech-2.8 —— 中文自然度与情感表现最好
DEFAULT_MODEL = "speech-2.8"

#: 内置音色（MiniMax 音色名直接可用；这是"配好"的重点：
#: 商业宣传片要成熟/主持音，不要少女音）
AGENTAI_VOICES = [
    # voice_key, 中文名, 性别, 适合
    ("female-chengshu", "成熟女性", "female", "宣传片/口播/企业片（默认）"),
    ("presenter_female", "女主持", "female", "发布会/产品片"),
    ("presenter_male", "男主持", "male", "发布会/产品片"),
    ("female-yujie", "御姐", "female", "都市/冷调"),
    ("female-tianmei", "甜美", "female", "轻快广告"),
    ("female-shaonv", "少女", "female", "青春/校园"),
    ("male-qn-jingying", "精英青年", "male", "商务/科技"),
    ("male-qn-badao", "霸道青年", "male", "强气场"),
    ("audiobook_female_1", "女性有声书", "female", "旁白/叙述"),
    ("audiobook_male_1", "男性有声书", "male", "旁白/叙述"),
]

#: 宣传/商业体裁的默认嗓子（与 `core/dubbing.py` 的体裁规则对齐）
PROMO_DEFAULT_VOICE = "female-chengshu"

#: ★ 2026-10-23（用户实拍：「女主的音色太离谱了，跟人物符合吗？」）
#:   角色画像 -> 音色。原来所有宣传片都默认「成熟女性(female-chengshu)」，
#:   那是**企业口播/播音**的质感，套到"高颜值都市创作者"身上就不像她。
#:   这里按人物描述里的年龄/气质词做匹配，匹配不到才回落到体裁默认。
VOICE_PROFILES = [
    # (命中词, 音色, 说明)
    (("少女", "学生", "校园", "青春", "00后", "二十出头"), "female-shaonv", "少女音"),
    (("甜美", "可爱", "萌", "轻快"), "female-tianmei", "甜美音"),
    (("御姐", "冷调", "都市", "职场", "成熟但年轻", "气质", "高级感", "创作者", "博主", "主播"),
     "female-yujie", "御姐音（都市/冷调/高级感）"),
    (("主持", "发布会", "口播", "新闻", "播报"), "presenter_female", "女主持音"),
    (("旁白", "叙述", "纪录片"), "audiobook_female_1", "女性有声书音"),
    (("中年", "母亲", "妈妈", "稳重", "企业"), "female-chengshu", "成熟女性音"),
]


def voice_for_profile(description: str = "", gender: str = "female",
                      default: str = PROMO_DEFAULT_VOICE) -> str:
    """按角色画像选音色：先看性别，再看描述里的年龄/气质词。"""
    d = str(description or "")
    g = str(gender or "").lower()
    if g == "male":
        for words, voice, _ in VOICE_PROFILES:
            if voice.startswith("female"):
                continue
            if any(w in d for w in words):
                return voice
        return "male-qn-jingying" if any(w in d for w in ("青年", "年轻", "精英", "科技", "商务")) else "audiobook_male_1"
    for words, voice, _ in VOICE_PROFILES:
        if not voice.startswith("female"):
            continue
        if any(w in d for w in words):
            return voice
    return default


#: prosody 的情绪键 → MiniMax 合法枚举的**语义近邻**。
#:
#: ★ 2026-10-23 实测：`core/prosody.py::_EMO` 有 82 个键，其中 **27 个**
#:   不是 `_MINIMAX_EMOTION` 的枚举（cold / concerned / gentle / skeptical /
#:   worried / tender / whisper / 温柔 / 紧张 / 呼喊 / 威严 / 怀疑 …）。
#:   旧代码只查一次映射表，查不到就**静默不传 emotion** —— 相当于三分之一
#:   台词的"感情"从来没发到引擎（用户听感就是"平铺直叙、感情不对"）。
#:   这张表只做"就近归到一个合法枚举"，不改 prosody 的表演参数。
_EMOTION_NEAREST: Dict[str, str] = {
    "cold": "low", "commanding": "determined", "concerned": "softly",
    "doubtful": "uncertain", "gentle": "softly", "mocking": "sarcastic",
    "murmur": "softly", "sarcastic": "mocking", "shout": "angry",
    "shouting": "angry", "skeptical": "uncertain", "tender": "softly",
    "whisper": "softly", "whispering": "softly", "worried": "nervous",
    "低语": "softly", "关切": "softly", "呼喊": "angry", "嘲讽": "mocking",
    "威严": "determined", "怀疑": "uncertain", "惊恐": "fearful",
    "温柔": "softly", "生气": "angry", "痛苦": "sad", "紧张": "nervous",
    "绝望": "sad",
    # 数据层（分镜 performance.formula / 剧本 emotion）真实在用的词
    "warm": "hopeful", "gentle_concern": "softly", "cold_farewell": "low",
    "cold_calm": "calm", "anxious": "nervous", "tense": "nervous",
    "resolved": "resolute", "calm_down": "relieved",
}


def parse_voice(voice_id: str) -> tuple:
    """把 `agentai:模型:音色` / `agentai:音色` 拆成 (模型, 音色)。"""
    raw = str(voice_id or "").strip()
    if raw.startswith("agentai:"):
        raw = raw.split(":", 1)[1]
    parts = [p for p in raw.split(":") if p]
    if not parts:
        return DEFAULT_MODEL, PROMO_DEFAULT_VOICE
    known = {m for m, _ in AGENTAI_TTS_MODELS}
    if len(parts) >= 2 and parts[0] in known:
        return parts[0], parts[1]
    if parts[0] in known and len(parts) == 1:
        return parts[0], PROMO_DEFAULT_VOICE
    return DEFAULT_MODEL, parts[-1]


@register
class AgentAITTS(VoiceProvider):
    """中转站语音：OpenAI 兼容 `/v1/audio/speech`（一把 key 吃多家最好模型）。"""

    name = "agentai"
    display_name = "中转站语音（agentai · 推荐）"
    description = ("用中转站（agentai）里最好的语音模型：默认 MiniMax speech-2.8，"
                   "另有 OpenAI / Gemini 语音。中文自然度与情感表现明显好于免费 Edge，"
                   "一把 Key 通吃，不用逐家注册。按字符计费。")
    prefix = "agentai:"
    requires_api_key = True

    supports_model = True
    default_model = DEFAULT_MODEL
    models = [{"value": m, "label": label} for m, label in AGENTAI_TTS_MODELS]

    async def list_voices(self) -> List[VoiceInfo]:
        out: List[VoiceInfo] = []
        for key, label, gender, use in AGENTAI_VOICES:
            out.append(VoiceInfo(
                voice_id=f"agentai:{DEFAULT_MODEL}:{key}",
                display_name=f"{label}（{use}）",
                language="zh-CN",
                gender=gender,
                is_builtin=True,
            ))
        # OpenAI 的几把通用音色也列出来（中文可用但不如 MiniMax）
        for key, label, gender in (("alloy", "alloy（中性）", "female"),
                                   ("nova", "nova（女声）", "female"),
                                   ("onyx", "onyx（男声）", "male")):
            out.append(VoiceInfo(
                voice_id=f"agentai:gpt-4o-mini-tts:{key}",
                display_name=f"{label} · OpenAI",
                language="zh-CN",
                gender=gender,
                is_builtin=True,
            ))
        return out

    async def synthesize(self, req: TTSRequest) -> TTSResult:
        if not self.api_key:
            return TTSResult(
                success=False, provider=self.name, voice_id=req.voice_id,
                error=("中转站语音没配 Key。到「设置 → 模型」把 agentai 的 Key 填上"
                       "（文本模型那把就是同一把，只差语音 Key 一栏也被识别）。"),
            )
        model, voice = parse_voice(req.voice_id)
        # 允许设置里覆盖模型（用户想换 GPT/Gemini 语音时不用改代码）
        model = str((self.config or {}).get("model") or model or DEFAULT_MODEL)
        body: Dict[str, Any] = {
            "model": model,
            "voice": voice,
            "input": req.text,
            "response_format": "mp3",
        }
        # ★ 2026-10-23 实测：`/audio/speech` **接受 `emotion` 字段**
        #   （speech-2.8 + emotion=happy → 200 audio/mpeg 27.6KB）。
        #   MiniMax 原生接口支持 emotion，网关做了透传 —— 所以"感情不对"
        #   这条终于能真的发出去，而不是只算在本地参数里。
        #   映射表复用 minimax_tts 的 7 个合法枚举，避免两处漂移。
        if model.startswith("speech-"):
            try:
                from .minimax_tts import _MINIMAX_EMOTION as _EMO
            except Exception:  # noqa: BLE001
                _EMO = {}
            _raw = str(getattr(req, "emotion", "") or "").strip().lower()
            _e = _EMO.get(_raw)
            if not _e and _raw:
                # ★ 不在枚举里不等于"没有情绪"：先归到语义近邻再发，
                #   而不是静默丢弃（见 _EMOTION_NEAREST 的注释）。
                _e = _EMO.get(_EMOTION_NEAREST.get(_raw, ""))
            if _e:
                body["emotion"] = _e
        # 语速：OpenAI 协议支持 speed（0.25~4.0）
        try:
            rate = float(req.rate or 1.0)
            if abs(rate - 1.0) > 0.01:
                body["speed"] = max(0.25, min(4.0, rate))
        except Exception:  # noqa: BLE001
            pass
        out = Path(req.output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            client = await self._ensure_client()
            r = await client.post(
                f"{API_BASE}/audio/speech", json=body,
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                timeout=180.0)
        except Exception as e:  # noqa: BLE001
            return TTSResult(success=False, provider=self.name, voice_id=req.voice_id,
                             error=f"中转站语音请求异常：{type(e).__name__}: {e}")
        if r.status_code != 200:
            hint = ""
            if r.status_code in (401, 403):
                hint = "（Key 无效或没权限：到设置里确认 agentai 的 Key）"
            elif r.status_code == 503:
                hint = ("（该模型的音色名不匹配：豆包用豆包音色、MiniMax 用 MiniMax 音色；"
                        "换 voice_id 或换模型重试）")
            return TTSResult(success=False, provider=self.name, voice_id=req.voice_id,
                             error=f"中转站语音 HTTP {r.status_code}{hint}: {r.text[:300]}")
        data = r.content or b""
        if len(data) < 800:
            return TTSResult(success=False, provider=self.name, voice_id=req.voice_id,
                             error=f"中转站语音返回内容过小（{len(data)}B），可能被拒：{data[:200]}")
        try:
            out.write_bytes(data)
        except Exception as e:  # noqa: BLE001
            return TTSResult(success=False, provider=self.name, voice_id=req.voice_id,
                             error=f"中转站语音落盘失败：{e}")
        # 情绪：OpenAI 语音协议**没有** emotion 字段（MiniMax 原生接口才有）。
        # 这里如实标注，避免上层以为"情绪已生效"。
        note = ""
        if str(getattr(req, "emotion", "") or "").strip():
            # ★ 2026-10-23 修：这句话原来写"不带 emotion 字段"，但同一行的
            #   `emotion_sent` 又如实报告已发送 —— 自相矛盾的注解。
            #   实测（speech-2.8，emotion=hopeful → 网关收到 happy）确实透传，
            #   所以这里按实际行为描述。
            note = ("speech-* 走 MiniMax 原生 emotion 透传：本地情绪先映射到"
                    "合法枚举再发送（见 _EMOTION_NEAREST）；"
                    "gpt-* 等 OpenAI 语音模型不带 emotion，靠文本韵律表达")
        return TTSResult(
            success=True, provider=self.name, voice_id=req.voice_id,
            audio_path=str(out), duration_seconds=0.0,
            raw={"model": model, "voice": voice,
                 "emotion_sent": body.get("emotion", ""), "note": note},
        )
