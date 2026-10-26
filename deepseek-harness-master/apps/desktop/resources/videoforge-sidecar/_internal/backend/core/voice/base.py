"""
VideoForge · TTS Provider 抽象基类

借鉴 VideoAdapter 风格：
- 元数据类属性（name / display_name / description / requires_api_key）
- 统一接口：synthesize(req) → TTSResult
- 自动注册到 PROVIDERS 字典
- 前端通过 list_providers() / list_voices() 拉取元数据动态渲染
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any

import httpx


logger = logging.getLogger("videoforge.voice")


# ──────────── 数据结构 ────────────


@dataclass
class TTSRequest:
    """配音请求"""
    text: str                                # 要合成的文本
    voice_id: str                            # 形如 "siliconflow:FunAudioLLM/CosyVoice2-0.5B:alex"
    rate: float = 1.0                        # 倍速 0.5-2.0
    volume: float = 1.0                      # 音量 0.0-2.0
    output_path: str = ""                    # 本地保存路径
    language: str = "zh-CN"                  # 语言提示
    # ── 表演韵律（2026-09-13 新增）────────────────────────────────
    # ★ 在此之前 `TTSRequest` **没有**音高/情绪字段，而 `voicecast.synthesize_line`
    #   收了 `emotion` 参数却只写进返回值 —— 剧本写着"愤怒"，合成出来还是平铺直叙。
    #   这就是用户说的"像机器人"的直接技术原因。这三个字段是那条断链的补口。
    pitch_hz: float = 0.0                    # 音高偏移（Hz，按角色基线基频折算）
    pitch_semitones: float = 0.0             # 音高偏移（半音，供支持的引擎用）
    emotion: str = ""                        # 情绪标签（供支持 emotion 的引擎用）
    style: str = ""                          # 风格标签（mstts:express-as 一类）


@dataclass
class TTSResult:
    """配音结果"""
    success: bool
    audio_path: Optional[str] = None
    duration_seconds: Optional[float] = None
    # 与音频对齐的字幕片段（[{start, end, text}]），用于字幕对齐/烧字幕
    # Edge TTS 原生支持 SubMaker；其他 provider 由 dispatcher 后续用 whisper 对齐
    subtitles: Optional[List[Dict[str, Any]]] = None
    provider: str = ""
    voice_id: str = ""
    error: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None


@dataclass
class VoiceInfo:
    """单个音色元数据"""
    voice_id: str                            # 含前缀的完整 id
    display_name: str                        # 展示名（含性别 / 风格）
    language: str = "zh-CN"                  # zh-CN / en-US / ja-JP 等
    gender: str = "unknown"                  # male / female / unknown
    style: str = ""                          # 风格描述（仅 Gemini 等支持）
    is_builtin: bool = True                  # 是否系统内置（vs 用户克隆）


class VoiceNotFoundError(Exception):
    """音色不存在"""


class SynthesizeError(Exception):
    """合成失败（provider 返回的明确错误）"""


# ──────────── Provider 抽象基类 ────────────


class VoiceProvider(ABC):
    """TTS provider 抽象基类"""

    # 元数据
    name: str = "base"                       # 短名（用于 voice_id 前缀）
    display_name: str = "Base TTS"           # 展示名
    description: str = ""                    # 描述
    prefix: str = ""                          # voice_id 前缀（必须等于 name + ":"）
    requires_api_key: bool = True

    # ── 配音模型（2026-09-14 新增）────────────────────────────────
    # ★ 用户的原话：「最好是可以真正的去选择配音模型（模型新增 minimax M3 模型）」。
    #   在此之前**没有任何地方**能选 TTS 模型：`minimax_tts.py` 里
    #   `model` 写死成 `"speech-02-hd"`（`(self.config or {}).get("model")`，
    #   而 dispatcher 构造 provider 时**只传 api_key、从不传 config** ——
    #   所以那个 `.get("model")` 永远是空的，用户选不了、也换不了）。
    #   现在每个厂商自己声明**真实型号清单**，dispatcher 从设置里取值传进来。
    supports_model: bool = False               # 这个引擎的型号能不能选
    default_model: str = ""                    # 不选时用哪个（官方文档里的默认）
    models: List[Dict[str, str]] = []          # [{"id","label","note"}]，id 空串=无参数
    model_note: str = ""                       # 不能选型号时，如实说明为什么

    def model_options(self) -> List[Dict[str, str]]:
        """给前端/接口用的型号清单（拷贝一份，避免调用方改到类属性）。"""
        return [dict(m) for m in (self.models or [])]

    def __init__(self, api_key: str = "", config: Dict[str, Any] = None):
        self.api_key = api_key
        self.config = config or {}
        self._client: Optional[httpx.AsyncClient] = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=15.0, read=180.0, write=60.0, pool=10.0),
                headers=self._default_headers(),
            )
        return self._client

    def _default_headers(self) -> Dict[str, str]:
        return {"User-Agent": "VideoForge/1.0"}

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ──────────── 核心接口（子类必须实现）────────────

    @abstractmethod
    async def synthesize(self, req: TTSRequest) -> TTSResult:
        """合成语音。子类必须实现。

        返回：
        - TTSResult(success=True, audio_path, duration_seconds, subtitles, ...)
        - TTSResult(success=False, error="...") 不要抛异常（除非参数错误）
        """
        raise NotImplementedError

    @abstractmethod
    async def list_voices(self) -> List[VoiceInfo]:
        """列出可用音色。子类必须实现。"""
        raise NotImplementedError


# ──────────── 注册表 ────────────


PROVIDERS: Dict[str, type] = {}


def register(cls: type) -> type:
    """装饰器：注册 TTS provider"""
    instance = cls()
    if not instance.prefix:
        instance.prefix = f"{instance.name}:"
    PROVIDERS[instance.name] = cls
    return cls


def get_provider(name: str, api_key: str = "", config: Dict[str, Any] = None) -> VoiceProvider:
    """根据 provider 短名获取实例"""
    if name not in PROVIDERS:
        raise VoiceNotFoundError(
            f"Unknown TTS provider: {name}. Available: {list(PROVIDERS.keys())}"
        )
    return PROVIDERS[name](api_key=api_key, config=config)


# 注意：自动发现子 provider 的逻辑由 __init__.py 显式触发，
# 避免 base.py 自身 import 子模块时的循环依赖。
