# -*- coding: utf-8 -*-
"""VideoForge · 火山引擎（豆包语音）TTS 适配器

注册表里 `volc` 这一项一直只有目录。本文件按官方「HTTP 语音合成」协议接上。

接口：
    POST https://openspeech.bytedance.com/api/v1/tts
    Header: Authorization: Bearer;<token>        ← 注意是 **Bearer;**
    Body:   {"app":   {"appid": "...", "token": "...", "cluster": "volcano_tts"},
             "user":  {"uid": "videoforge"},
             "audio": {"voice_type": "zh_female_shuangkuaisisi_moon_bigtts",
                       "encoding": "mp3", "speed_ratio": 1.0,
                       "volume_ratio": 1.0, "pitch_ratio": 1.0, "rate": 24000},
             "request": {"reqid": "<uuid>", "text": "...", "operation": "query"}}
    返回:   {"code": 3000, "message": "Success", "data": "<base64 音频>"}

★ 三件套：**appid / token / cluster**。少一件服务端只会回
  `{"code":3001,...,"message":"Invalid request"}` —— 用户完全看不出缺什么。
  所以本适配器在**发请求之前**就把缺的那一项名字报出来。

凭据怎么写（任选一种，见 `_credentials.py`）：
    ① tts_api_keys.volc = "appid=123456;token=abcdef;cluster=volcano_tts"
    ② settings["volc_appid"] / ["volc_token"] / ["volc_cluster"]
    ③ tts_extra.volc = {"appid":...,"token":...,"cluster":...}
    ④ 环境变量 VOLC_TTS_APPID / VOLC_TTS_TOKEN / VOLC_TTS_CLUSTER
  cluster 有官方默认值 volcano_tts（写死在这里，不算缺失）。
"""

from __future__ import annotations

import base64
import logging
import re
import uuid
from pathlib import Path
from typing import Dict, List

from .base import (
    TTSRequest,
    TTSResult,
    VoiceInfo,
    VoiceProvider,
    register,
)
from ._credentials import credential_gaps, resolve_base_url, resolve_parts


logger = logging.getLogger("videoforge.voice.volc")

DEFAULT_BASE = "https://openspeech.bytedance.com"
API_PATH = "/api/v1/tts"

#: 三件套定义（缺哪件就报哪件的名字）
_VOLC_PARTS = [
    {"key": "appid", "label": "appid（应用 ID）",
     "aliases": ["appid", "app_id", "app"],
     "settings": ["volc_appid", "volc_app_id"],
     "env": ["VOLC_TTS_APPID", "VOLC_APP_ID"]},
    {"key": "token", "label": "token（访问令牌）",
     "aliases": ["token", "access_token"],
     "settings": ["volc_token", "volc_access_token"],
     "env": ["VOLC_TTS_TOKEN", "VOLC_ACCESS_TOKEN"]},
    {"key": "cluster", "label": "cluster（集群，默认 volcano_tts）",
     "aliases": ["cluster"],
     "settings": ["volc_cluster"],
     "env": ["VOLC_TTS_CLUSTER"],
     "default": "volcano_tts"},
]

#: 官方音色（大模型/多情感的那批；id 直接可用作 voice_type）
VOLC_VOICES = [
    ("zh_female_shuangkuaisisi_moon_bigtts", "爽快思思 · 女声（默认）", "female"),
    ("zh_female_cancan_mars_bigtts", "灿灿 · 女声", "female"),
    ("zh_male_beijingxiaoye_moon_bigtts", "北京小爷 · 男声", "male"),
    ("zh_male_yangguangqingnian_moon_bigtts", "阳光青年 · 男声", "male"),
    ("zh_female_wanwanxiaohe_moon_bigtts", "湾湾小何 · 女声", "female"),
    ("zh_male_jingqiangkanye_moon_bigtts", "京腔侃爷 · 男声", "male"),
    ("zh_female_linjianvhai_moon_bigtts", "邻家女孩 · 女声", "female"),
    ("zh_male_shaonianzixin_moon_bigtts", "少年梓辛 · 男声", "male"),
]

DEFAULT_VOICE = "zh_female_shuangkuaisisi_moon_bigtts"

#: 常见错误码 → 人话（只写有把握的；其余原样带出）
ERROR_HINTS = {
    3001: "请求参数不合法（多半是 appid/token/cluster 三件套里有一项不对）",
    3003: "并发/QPS 超限，稍后重试",
    3005: "音色（voice_type）与账号不匹配：确认该音色已开通",
    3006: "文本长度超过该接口上限（单次建议 ≤300 字）",
    3010: "文本命中了内容审核，请调整措辞",
}


@register
class VolcTTS(VoiceProvider):
    """火山引擎豆包语音（HTTP 合成）"""

    name = "volc"
    display_name = "火山引擎（豆包语音）"
    description = ("字节火山引擎的豆包语音，中文自然、多情感音色多。"
                   "需要 appid + token + cluster 三件套。")
    prefix = "volc:"
    requires_api_key = True

    supports_model = False
    model_note = ("火山接口没有 model 参数：`voice_type`（音色）即模型，"
                  "cluster 决定能力档位（默认 volcano_tts）")
    verification = "implemented（本机无火山三件套，未做真实合成验证）"
    #: dispatcher 需要把 settings（region / 三件套 / 参考音频）传下来
    wants_settings = True

    def credential_gaps(self, settings: Dict[str, object] | None = None) -> List[str]:
        conf = {"settings": settings or {}}
        return credential_gaps(self.name, _VOLC_PARTS, config=conf,
                               primary=self.api_key)

    async def list_voices(self) -> List[VoiceInfo]:
        return [
            VoiceInfo(voice_id=f"volc:{vid}", display_name=label,
                      language="zh-CN", gender=gender, is_builtin=True)
            for vid, label, gender in VOLC_VOICES
        ]

    async def synthesize(self, req: TTSRequest) -> TTSResult:
        settings = (self.config or {}).get("settings") or {}
        values, missing, sources = resolve_parts(
            self.name, _VOLC_PARTS, config={"settings": settings},
            primary=self.api_key)
        if missing:
            return TTSResult(
                success=False, provider=self.name, voice_id=req.voice_id,
                error=(f"火山引擎语音**缺少 {', '.join(missing)}**。"
                       f"填法（三选一）："
                       f"① tts_api_keys.volc 写成 "
                       f"\"appid=...;token=...;cluster=volcano_tts\"；"
                       f"② 设置里分别加 volc_appid / volc_token / volc_cluster；"
                       f"③ 环境变量 VOLC_TTS_APPID / VOLC_TTS_TOKEN / VOLC_TTS_CLUSTER。"),
            )
        appid = values.get("appid", "")
        token = values.get("token", "")
        cluster = values.get("cluster", "volcano_tts")
        vid = req.voice_id.split(":", 1)[1] if ":" in req.voice_id else req.voice_id
        vid = vid or DEFAULT_VOICE
        base = str((self.config or {}).get("base_url") or "").strip().rstrip("/") \
            or resolve_base_url(self.name, DEFAULT_BASE, config=self.config,
                                env=("VOLC_TTS_BASE_URL",))

        body = {
            "app": {"appid": appid, "token": token, "cluster": cluster},
            "user": {"uid": "videoforge"},
            "audio": {
                "voice_type": vid,
                "encoding": "mp3",
                "speed_ratio": max(0.2, min(3.0, float(req.rate or 1.0))),
                "volume_ratio": max(0.1, min(3.0, float(req.volume or 1.0))),
                "pitch_ratio": max(0.5, min(2.0, 2 ** (
                    float(getattr(req, "pitch_semitones", 0.0) or 0.0) / 12.0))),
                "rate": 24000,
            },
            "request": {"reqid": str(uuid.uuid4()), "text": req.text,
                        "operation": "query"},
        }

        out = Path(req.output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            client = await self._ensure_client()
            r = await client.post(
                f"{base}{API_PATH}", json=body,
                headers={"Authorization": f"Bearer;{token}",
                         "Content-Type": "application/json"},
                timeout=180.0)
            if r.status_code >= 400:
                return TTSResult(
                    success=False, provider=self.name, voice_id=req.voice_id,
                    error=f"火山引擎语音 HTTP {r.status_code}: {(r.text or '')[:300]}")
            data = r.json() or {}
            code = int(data.get("code") or 0)
            if code != 3000:
                hint = ERROR_HINTS.get(code, "")
                return TTSResult(
                    success=False, provider=self.name, voice_id=req.voice_id,
                    error=(f"火山引擎语音失败 [{code}] {data.get('message') or ''}"
                           + (f"（{hint}）" if hint else "")
                           + f" 原始响应：{str(data)[:160]}"),
                )
            audio_b64 = str(data.get("data") or "")
            if not audio_b64:
                return TTSResult(
                    success=False, provider=self.name, voice_id=req.voice_id,
                    error=f"火山引擎没有返回音频数据：{str(data)[:200]}")
            out.write_bytes(base64.b64decode(audio_b64))
            return TTSResult(
                success=True, audio_path=str(out),
                duration_seconds=_estimate_duration(req.text, req.rate),
                provider=self.name, voice_id=req.voice_id,
                raw={"voice": vid, "cluster": cluster, "appid_tail": appid[-4:],
                     "credential_sources": sources,
                     "size_bytes": out.stat().st_size})
        except Exception as e:  # noqa: BLE001
            logger.exception("Volc TTS failed")
            return TTSResult(success=False, provider=self.name, voice_id=req.voice_id,
                             error=f"火山引擎语音调用失败：{type(e).__name__}: {e}")


def _estimate_duration(text: str, rate: float) -> float:
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    words = len(re.findall(r"[A-Za-z0-9]+", text))
    return max(1.0, (cjk / 4.2 + words / 2.7) / max(0.25, float(rate or 1.0)))
