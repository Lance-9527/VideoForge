"""
VideoForge · TTS dispatcher

借鉴 MPT voice.py 的 _single_tts 派发模式：
- 根据 voice_id 前缀（`edge:` / `siliconflow:` / `silent:`）路由到对应 provider
- 内置 voice_id 解析 + sentinel 判断（"silent:no-voice" / 空字符串 / "no-voice"）
- 统一从 settings 取 api_key
- 统一异常兜底
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Tuple

from .base import (
    TTSRequest,
    TTSResult,
    VoiceInfo,
    VoiceProvider,
    VoiceNotFoundError,
    PROVIDERS,
    get_provider as _base_get_provider,
)
from .silent import _estimate_no_voice_duration, _SILENT_VOICE_ID


logger = logging.getLogger("videoforge.voice.dispatcher")


# 「无配音」sentinels：用户可能写 "silent:no-voice" / "no-voice" / "" / None
_NO_VOICE_SENTINELS = {"", "no-voice", "none", "silent:no-voice", "silent:none"}


# ──────────── 公开 API ────────────


def parse_voice_id(voice_id: str) -> Tuple[str, str]:
    """解析 voice_id 为 (provider_name, voice_short_name)。

    例如：
    - "siliconflow:FunAudioLLM/CosyVoice2-0.5B:alex-Male" → ("siliconflow", "FunAudioLLM/CosyVoice2-0.5B:alex-Male")
    - "edge:zh-CN-XiaoxiaoNeural"                          → ("edge", "zh-CN-XiaoxiaoNeural")
    - "silent:no-voice"                                     → ("silent", "no-voice")
    - "no-voice"                                            → ("silent", "no-voice")  # 兼容
    """
    if not voice_id:
        return ("silent", "no-voice")
    if voice_id in _NO_VOICE_SENTINELS:
        return ("silent", "no-voice")

    parts = voice_id.split(":", 1)
    if len(parts) != 2 or not parts[0]:
        # 没前缀：默认按 edge 处理（向后兼容老的 voice_id 列表）
        return ("edge", voice_id)
    return parts[0], parts[1]


def is_no_voice(voice_id: Optional[str]) -> bool:
    """判断 voice_id 是否为「无配音」"""
    if not voice_id:
        return True
    return voice_id.strip().lower() in _NO_VOICE_SENTINELS


def estimate_no_voice_duration(text: str) -> float:
    """无配音模式时长估算（中文 4.2 字/秒、英文 2.7 词/秒）"""
    return _estimate_no_voice_duration(text)


def _resolve_api_key(provider_name: str, voice_id: str, settings: dict) -> str:
    """从 settings 取 api_key（按 provider 字段）

    settings 结构（来自前端设置页）：
    {
      "tts_api_keys": { "siliconflow": "...", "edge": "" },  # 语音专用 Key（优先）
      "llm_api_keys": { "MiniMax": "...", "deepseek": "..." },  # 兜底
      ...
    }

    ★ 兜底查找必须**大小写不敏感**：设置页里 MiniMax 存的是 `MiniMax`，
    而 provider 名是小写 `minimax` —— 区分大小写的话就会漏掉，
    用户明明配了 MiniMax Key，语音那块却一直说"未配置"。
    """
    def _ci_lookup(d, name: str) -> str:
        if not isinstance(d, dict):
            return ""
        for k, v in d.items():
            if str(k).strip().lower() == name and v:
                return str(v).strip()
        return ""

    keys = settings.get("tts_api_keys", {}) or {}
    key = _ci_lookup(keys, provider_name)
    if key:
        return key
    # 兜底：llm_api_keys / api_keys 里同名或同服务的 Key
    # （MiniMax 的对话与语音是同一个 Key；百炼的 CosyVoice 与通义千问同一个 Key）
    _ALIASES = {
        "dashscope": ("qwen", "wanx", "bailian"),
        "minimax": ("minimax",),
        "openai": ("openai",),
    }
    names = _ALIASES.get(provider_name, (provider_name,))
    for bucket in ("llm_api_keys", "api_keys"):
        d = settings.get(bucket) or {}
        for n in names:
            k = _ci_lookup(d, n)
            if k:
                return k
    return ""


async def synthesize(req: TTSRequest, settings: Optional[dict] = None) -> TTSResult:
    """统一入口：解析 voice_id → 找 provider → 调用 synthesize

    settings 用于取 api_key（siliconflow 需要）**和配音模型**。
    """
    settings = settings or {}

    # 1. 无配音优先
    if is_no_voice(req.voice_id):
        # ★ 重建请求时**必须**把韵律字段一起带上：漏字段会让"无配音"分支
        #   悄悄丢掉情绪/音高（下次改这里的人容易只看 text/rate/volume）。
        req = TTSRequest(
            text=req.text,
            voice_id=_SILENT_VOICE_ID,
            rate=req.rate,
            volume=req.volume,
            output_path=req.output_path or req.voice_id,  # 兜底
            language=req.language,
            pitch_hz=getattr(req, "pitch_hz", 0.0),
            pitch_semitones=getattr(req, "pitch_semitones", 0.0),
            emotion=getattr(req, "emotion", ""),
            style=getattr(req, "style", ""),
        )

    # 2. 解析 provider
    provider_name, _short = parse_voice_id(req.voice_id)
    if provider_name not in PROVIDERS:
        return TTSResult(
            success=False,
            voice_id=req.voice_id,
            error=f"未注册的 TTS provider：{provider_name}（已注册：{list(PROVIDERS.keys())}）",
        )

    # 3. 取 api_key
    api_key = _resolve_api_key(provider_name, req.voice_id, settings)

    # 4. 实例化 provider 并合成
    try:
        # ★★ 2026-09-14：把**用户选的配音模型**传下去。
        #   踩过的坑：dispatcher 一直只传 `api_key=...`，而 `minimax_tts.py`
        #   写的是 `(self.config or {}).get("model") or "speech-02-hd"` ——
        #   config 永远是空的，所以"模型"实际上**写死在代码里**，
        #   用户在界面上根本无从选择（这正是用户说的"真正的去选择配音模型"）。
        _model = resolve_tts_model(provider_name, settings)
        provider = _base_get_provider(provider_name, api_key=api_key,
                                     config={"model": _model})
        try:
            result = await provider.synthesize(req)
            # ★ 型号是**账号相关**的：老账号可能没开通最新型号。
            #   这时**不能**就让这句合成失败（失败 → 上层回退默认音色 →
            #   又变回用户抱怨的"全片一把 AI 女音"）。改成：按厂商声明的
            #   备用型号顺序**再试**，并且把"实际用了哪个型号"如实带回去
            #   （`raw.model_fallback`），上层会在告警里说出来。
            if (not result.success) and _model and _looks_like_model_error(result.error):
                for _fb in (getattr(provider, "model_fallbacks", None) or []):
                    if not _fb or _fb == _model:
                        continue
                    logger.warning("配音模型 %s 不可用（%s）→ 退回备用型号 %s",
                                   _model, str(result.error)[:80], _fb)
                    try:
                        provider.config = dict(provider.config or {})
                        provider.config["model"] = _fb
                    except Exception:
                        pass
                    _r2 = await provider.synthesize(req)
                    if _r2.success:
                        _raw = dict(_r2.raw or {})
                        _raw["model_fallback"] = {"from": _model, "to": _fb}
                        _raw["model_used"] = _fb
                        _r2.raw = _raw
                        return _r2
                    result = _r2
            return result
        finally:
            await provider.close()
    except VoiceNotFoundError as e:
        return TTSResult(success=False, voice_id=req.voice_id, error=str(e))
    except Exception as e:
        logger.exception("TTS dispatcher unexpected error")
        return TTSResult(
            success=False,
            voice_id=req.voice_id,
            error=f"TTS 调用异常：{type(e).__name__}: {e}",
        )


def resolve_tts_model(provider_name: str, settings: Optional[dict] = None) -> str:
    """**用户为这个厂商选的配音模型**（没有就返回厂商默认型号）。

    settings 结构：`{"tts_models": {"minimax": "speech-2.8-hd", ...}}`

    ★ 只接受该厂商**自己声明的**型号清单里的 id：
      设置里存了一个已经下线的型号时，宁可退回默认值也不要拿着一个
      来路不明的字符串去请求（那只会得到一句看不懂的接口错误）。
      但**不静默改用户选的**：清单里有的就照用，用户自己填的额外型号也放行
      （厂商上新时不用等我们发版），只是会在接口里标 `known=false`。
    """
    chosen = ""
    models = (settings or {}).get("tts_models") or {}
    if isinstance(models, dict):
        for k, v in models.items():
            if str(k).strip().lower() == str(provider_name).strip().lower() and v:
                chosen = str(v).strip()
                break
    cls = PROVIDERS.get(provider_name)
    default = str(getattr(cls, "default_model", "") or "") if cls else ""
    return chosen or default


def _looks_like_model_error(err: Optional[str]) -> bool:
    """这句报错像不像"型号不对/不可用"？（决定要不要试备用型号）

    ★ 只在**明确像型号问题**时才回退：把"余额不足""Key 无效"也回退一遍，
      只会多花一次调用、还把真正的原因盖掉。
    """
    t = str(err or "")
    if not t:
        return False
    return bool(re.search(r"(model|模型|型号|not\s*exist|not\s*found|invalid|unsupported|"
                          r"no\s*such|不合法|不存在|不支持)", t, re.I))


async def list_voices(settings: Optional[dict] = None) -> List[VoiceInfo]:
    """列出所有可用音色（前端下拉框）

    并发查询各 provider，速度更快。
    """
    import asyncio

    async def _fetch(p_name: str) -> List[VoiceInfo]:
        cls = PROVIDERS.get(p_name)
        if cls is None:
            return []
        api_key = _resolve_api_key(p_name, "", settings or {})
        p = cls(api_key=api_key)
        try:
            return await p.list_voices()
        except Exception as e:
            logger.warning("list_voices failed for %s: %s", p_name, e)
            return []
        finally:
            await p.close()

    # 排除无配音（它不是真正的音色）
    provider_names = [k for k in PROVIDERS.keys() if k != "silent"]

    results = await asyncio.gather(*[_fetch(name) for name in provider_names])
    voices = []
    for batch in results:
        voices.extend(batch)
    return voices


async def list_providers(settings: Optional[dict] = None) -> List[dict]:
    """列出所有 provider（含 silent）的元数据，前端按 provider 分组渲染下拉框

    ★ 2026-09-14 起同时返回**配音模型**信息（用户要求"真正可选"）：
      `supports_model` / `models` / `default_model` / `selected_model` / `model_note`。
    """
    out = []
    for name, cls in PROVIDERS.items():
        inst = cls()
        api_key = _resolve_api_key(name, "", settings or {})
        has_key = bool(api_key) or not inst.requires_api_key
        _models = inst.model_options() if hasattr(inst, "model_options") else []
        _sel = resolve_tts_model(name, settings or {})
        out.append({
            "name": name,
            "display_name": inst.display_name,
            "description": inst.description,
            "prefix": inst.prefix,
            "requires_api_key": inst.requires_api_key,
            "has_api_key": has_key,
            "supports_model": bool(getattr(inst, "supports_model", False)),
            "models": _models,
            "default_model": str(getattr(inst, "default_model", "") or ""),
            "selected_model": _sel,
            "model_known": (not _sel) or any(m.get("id") == _sel for m in _models)
            or not _models,
            "model_note": str(getattr(inst, "model_note", "") or ""),
        })
    return out
