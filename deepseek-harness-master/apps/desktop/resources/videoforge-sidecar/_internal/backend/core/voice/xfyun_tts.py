# -*- coding: utf-8 -*-
"""VideoForge · 讯飞星火语音（iFlytek TTS）适配器

注册表里 `xfyun` 一直只有目录。本文件按官方 **WebSocket** 协议接上。

协议（不是 HTTP POST —— 这一家是长连接流式）：
    wss://tts-api.xfyun.cn/v2/tts?authorization=..&date=..&host=..
    必须自己算 HMAC-SHA256 签名（见 `build_signed_url`）。

三件套：**app_id / api_key / api_secret**。少一件服务端只会回
`{"code":401,...}`，用户看不出缺什么 —— 所以这里发请求前就报缺哪一项。

凭据怎么写（任选一种，见 `_credentials.py`）：
    ① tts_api_keys.xfyun = "app_id=...;api_key=...;api_secret=..."
    ② settings["xfyun_app_id"] / ["xfyun_api_key"] / ["xfyun_api_secret"]
    ③ tts_extra.xfyun = {...}
    ④ 环境变量 XFYUN_APP_ID / XFYUN_API_KEY / XFYUN_API_SECRET
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import time
from email.utils import formatdate
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from .base import (
    TTSRequest,
    TTSResult,
    VoiceInfo,
    VoiceProvider,
    register,
)
from ._credentials import credential_gaps, resolve_parts


logger = logging.getLogger("videoforge.voice.xfyun")

HOST = "tts-api.xfyun.cn"
PATH = "/v2/tts"

#: 三件套定义
_XFYUN_PARTS = [
    {"key": "app_id", "label": "app_id（应用 ID）",
     "aliases": ["app_id", "appid"], "settings": ["xfyun_app_id"],
     "env": ["XFYUN_APP_ID", "IFLYTEK_APP_ID"]},
    {"key": "api_key", "label": "api_key",
     "aliases": ["api_key", "apikey"], "settings": ["xfyun_api_key"],
     "env": ["XFYUN_API_KEY", "IFLYTEK_API_KEY"]},
    {"key": "api_secret", "label": "api_secret",
     "aliases": ["api_secret", "apisecret", "secret"], "settings": ["xfyun_api_secret"],
     "env": ["XFYUN_API_SECRET", "IFLYTEK_API_SECRET"]},
]

#: 讯飞在线合成常用发音人（vcn）
XFYUN_VOICES = [
    ("xiaoyan", "小燕 · 女声（默认）", "female"),
    ("aisjiuxu", "许久 · 男声", "male"),
    ("aisxping", "小萍 · 女声", "female"),
    ("aisjinger", "小婧 · 女声", "female"),
    ("aisbabyxu", "许小宝 · 童声", "unknown"),
    ("aisling_meng", "梦玲 · 多情感女声", "female"),
]

DEFAULT_VOICE = "xiaoyan"

#: 常见错误码 → 人话
ERROR_HINTS = {
    10105: "app_id 与 api_key 不匹配：确认三件套属于同一个应用",
    10106: "参数不合法（检查 vcn 音色名与文本编码）",
    10165: "app_id 不存在或未开通该服务",
    10160: "请求时间戳/签名过期（本机时钟偏差过大）",
    10161: "签名校验失败：api_secret 不对，或 host/date/path 与实际请求不一致",
    11200: "该账号没有可用的语音合成额度（未开通/已用尽）",
}


def build_signed_url(api_key: str, api_secret: str, *,
                     host: str = HOST, path: str = PATH,
                     date: Optional[str] = None) -> str:
    """生成带签名的 wss 地址（官方算法）。

    ★ 签名算法三行（照官方示例写，顺序不能变）：
        signature_origin = "host: <host>\\ndate: <date>\\nGET <path> HTTP/1.1"
        signature_sha    = base64(hmac_sha256(api_secret, signature_origin))
        authorization    = base64('api_key="..", algorithm="hmac-sha256",
                                   headers="host date request-line",
                                   signature="<signature_sha>"')
    :param date: 传了就固定（测试可复现）；不传用当前 GMT 时间。
    """
    if not date:
        date = formatdate(timeval=None, localtime=False, usegmt=True)
    signature_origin = f"host: {host}\ndate: {date}\nGET {path} HTTP/1.1"
    signature_sha = base64.b64encode(
        hmac.new(str(api_secret or "").encode("utf-8"),
                 signature_origin.encode("utf-8"),
                 digestmod=hashlib.sha256).digest()).decode("utf-8")
    authorization_origin = (
        f'api_key="{api_key}", algorithm="hmac-sha256", '
        f'headers="host date request-line", signature="{signature_sha}"')
    authorization = base64.b64encode(
        authorization_origin.encode("utf-8")).decode("utf-8")
    return (f"wss://{host}{path}?authorization={quote(authorization)}"
            f"&date={quote(date)}&host={quote(host)}")


def build_payload(app_id: str, text: str, voice: str, req: TTSRequest) -> Dict[str, Any]:
    """拼首帧请求体（业务参数 + base64 文本）。"""
    # 讯飞 speed/volume/pitch 都是 0~100（50 = 正常）
    speed = int(round(50 * max(0.25, min(4.0, float(req.rate or 1.0)))))
    volume = int(round(50 * max(0.1, min(3.0, float(req.volume or 1.0)))))
    pitch = int(round(50 * max(0.5, min(2.0, 2 ** (
        float(getattr(req, "pitch_semitones", 0.0) or 0.0) / 12.0)))))
    return {
        "common": {"app_id": app_id},
        "business": {
            "aue": "lame",       # lame = mp3（默认 raw = pcm）
            "sfl": 1,            # 流式返回
            "vcn": voice,
            "speed": max(0, min(100, speed)),
            "volume": max(0, min(100, volume)),
            "pitch": max(0, min(100, pitch)),
            "tte": "UTF8",
        },
        "data": {"status": 2, "text": base64.b64encode(
            str(text or "").encode("utf-8")).decode("utf-8")},
    }


@register
class XfyunTTS(VoiceProvider):
    """讯飞星火语音（在线合成 WebSocket）"""

    name = "xfyun"
    display_name = "讯飞星火语音"
    description = ("讯飞在线语音合成（WebSocket 流式），中文发音人丰富、"
                   "多情感音色多。需要 app_id / api_key / api_secret 三件套。")
    prefix = "xfyun:"
    requires_api_key = True

    supports_model = False
    model_note = ("讯飞接口没有 model 参数：发音人（vcn，如 xiaoyan）即模型；"
                  "不同发音人按账号开通情况有差异")
    verification = "implemented（本机无讯飞三件套，未做真实合成验证）"
    #: dispatcher 需要把 settings（region / 三件套 / 参考音频）传下来
    wants_settings = True

    def credential_gaps(self, settings: Dict[str, object] | None = None) -> List[str]:
        conf = {"settings": settings or {}}
        return credential_gaps(self.name, _XFYUN_PARTS, config=conf,
                               primary=self.api_key)

    async def list_voices(self) -> List[VoiceInfo]:
        return [
            VoiceInfo(voice_id=f"xfyun:{vid}", display_name=label,
                      language="zh-CN", gender=gender, is_builtin=True)
            for vid, label, gender in XFYUN_VOICES
        ]

    async def synthesize(self, req: TTSRequest) -> TTSResult:
        settings = (self.config or {}).get("settings") or {}
        values, missing, sources = resolve_parts(
            self.name, _XFYUN_PARTS, config={"settings": settings},
            primary=self.api_key)
        if missing:
            return TTSResult(
                success=False, provider=self.name, voice_id=req.voice_id,
                error=(f"讯飞语音**缺少 {', '.join(missing)}**。"
                       f"填法（三选一）："
                       f"① tts_api_keys.xfyun 写成 "
                       f"\"app_id=...;api_key=...;api_secret=...\"；"
                       f"② 设置里分别加 xfyun_app_id / xfyun_api_key / "
                       f"xfyun_api_secret；"
                       f"③ 环境变量 XFYUN_APP_ID / XFYUN_API_KEY / XFYUN_API_SECRET。"),
            )
        try:
            import websockets  # 延迟导入：没装也不会影响其他 provider
        except Exception as e:  # noqa: BLE001
            return TTSResult(
                success=False, provider=self.name, voice_id=req.voice_id,
                error=(f"讯飞语音需要 `websockets` 依赖，但导入失败："
                       f"{type(e).__name__}: {e}（pip install websockets 后重启）"),
            )

        vid = req.voice_id.split(":", 1)[1] if ":" in req.voice_id else req.voice_id
        vid = vid or DEFAULT_VOICE
        url = build_signed_url(values["api_key"], values["api_secret"])
        payload = build_payload(values["app_id"], req.text, vid, req)

        out = Path(req.output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        chunks: List[bytes] = []
        error_text = ""
        try:
            async with websockets.connect(url, open_timeout=20,
                                          close_timeout=10,
                                          max_size=16 * 1024 * 1024) as ws:
                await ws.send(json.dumps(payload, ensure_ascii=False))
                while True:
                    raw = await ws.recv()
                    try:
                        frame = json.loads(raw)
                    except Exception:  # noqa: BLE001
                        continue
                    code = int(frame.get("code") or 0)
                    if code != 0:
                        hint = ERROR_HINTS.get(code, "")
                        error_text = (f"讯飞语音失败 [{code}] "
                                      f"{frame.get('message') or ''}"
                                      + (f"（{hint}）" if hint else "")
                                      + f" sid={frame.get('sid') or ''}")
                        break
                    data = frame.get("data") or {}
                    audio = data.get("audio")
                    if audio:
                        chunks.append(base64.b64decode(audio))
                    if int(data.get("status") or 0) == 2:
                        break
            if error_text:
                return TTSResult(success=False, provider=self.name,
                                 voice_id=req.voice_id, error=error_text)
            if not chunks:
                return TTSResult(
                    success=False, provider=self.name, voice_id=req.voice_id,
                    error="讯飞语音没有返回音频数据（连接建立但没收到 audio 帧）")
            out.write_bytes(b"".join(chunks))
            return TTSResult(
                success=True, audio_path=str(out),
                duration_seconds=_estimate_duration(req.text, req.rate),
                provider=self.name, voice_id=req.voice_id,
                raw={"voice": vid, "chunks": len(chunks),
                     "credential_sources": sources,
                     "size_bytes": out.stat().st_size})
        except Exception as e:  # noqa: BLE001
            logger.exception("Xfyun TTS failed")
            return TTSResult(success=False, provider=self.name, voice_id=req.voice_id,
                             error=f"讯飞语音调用失败：{type(e).__name__}: {e}")


def _estimate_duration(text: str, rate: float) -> float:
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    words = len(re.findall(r"[A-Za-z0-9]+", text))
    return max(1.0, (cjk / 4.2 + words / 2.7) / max(0.25, float(rate or 1.0)))
