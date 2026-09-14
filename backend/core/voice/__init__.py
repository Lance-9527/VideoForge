"""
VideoForge · 配音 / TTS 模块

借鉴 MoneyPrinterTurbo 的 voice.py 设计：
- 字符串前缀分派（`edge:zh-CN-XiaoxiaoNeural` / `siliconflow:FunAudioLLM/CosyVoice2-0.5B:alex` / `silent`）
- 各 provider 独立可插拔
- 失败返回 TTSResult(success=False, error=...) 不抛异常（让 orchestrator 决定降级策略）
- 内置「无配音」 sentinel

公开 API：
- `synthesize(req)`            入口
- `list_voices()`              列出所有可用音色（前端下拉框）
- `list_providers()`           列出所有 provider（前端分组）
- `is_no_voice(voice_id)`      判断是否无配音
- `estimate_no_voice_duration(text)`  无配音时长估算（中文 4.2 字/秒等）

首发 provider：SiliconFlow（用户偏好）。
次选 / 兜底：Edge TTS（免费、无需 key）。
"""

from .base import (
    TTSRequest,
    TTSResult,
    VoiceProvider,
    VoiceInfo,
    VoiceNotFoundError,
    SynthesizeError,
    PROVIDERS,
    register,
    get_provider,
)
from .dispatcher import (
    synthesize,
    list_voices,
    list_providers,
    is_no_voice,
    estimate_no_voice_duration,
    parse_voice_id,
)


# ──────────── 显式触发各 provider 模块注册 ────────────
# 不在 base.py 里自动加载，避免循环 import。

def _autoload_providers() -> None:
    import importlib
    import os as _os

    _dir = _os.path.dirname(_os.path.abspath(__file__))
    # ★ 这个清单必须与 core/voice/ 下的 provider 模块**保持同步**：
    #   打包后（PyInstaller）模块被编译进 PYZ，目录里看不到 .py 文件，
    #   上面那次 listdir 会返回空 → 整体回退到这个硬编码清单。
    #   漏写一个，那个 provider 在打包版里就"消失"（开发版却正常，很难查）。
    _known = ["siliconflow_tts", "minimax_tts", "dashscope_tts",
              "edge_tts", "silent", "dispatcher"]
    names: list = []
    try:
        names = [f[:-3] for f in _os.listdir(_dir)
                 if f.endswith(".py") and f != "__init__.py" and f != "base.py" and f != "dispatcher.py"]
    except Exception:
        names = []
    if not names:
        names = list(_known)
    # 取并集：即使目录扫描成功，也保证 _known 里的模块一定被加载
    for _module_name in sorted(set(names) | set(_known)):
        try:
            importlib.import_module(f"{__name__}.{_module_name}")
        except Exception as e:
            print(f"[voice] Failed to import {_module_name}: {e}")


_autoload_providers()

__all__ = [
    "TTSRequest",
    "TTSResult",
    "VoiceProvider",
    "VoiceInfo",
    "VoiceNotFoundError",
    "SynthesizeError",
    "synthesize",
    "list_voices",
    "list_providers",
    "is_no_voice",
    "estimate_no_voice_duration",
    "parse_voice_id",
]
