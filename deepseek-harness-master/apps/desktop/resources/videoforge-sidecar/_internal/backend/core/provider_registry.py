"""统一的「VideoForge 模型」注册表：文本 / 图像 / 视频 / 音频 四类平台。

为什么要有这一层（2026-10-25）：
  旧设置页的四份清单分别散在 `core/llm.PROVIDER_ENDPOINTS`、
  `core/imagegen.PROVIDERS`、`core/adapters/*`、`core/voice/*` 里，
  接口/字段命名各不相同，用户看不出「到底支持哪些平台、还差哪个 Key」。
  实测还发现：应用里已经存了 `anthropic` 的 Key，但文本端点表里根本没有
  anthropic 这一项 —— 典型的「配了却用不上」。

设计原则：
  · 纯数据 + 纯函数，不联网、不读写数据库；状态（是否配置/上次测试结果）由调用方合并。
  · `protocol` 决定用哪个客户端：openai / anthropic / gemini / ollama / native。
  · `status` 必须诚实：verified（用真实 Key 实测通过）/ implemented（代码在，未用真实 Key 验证）/ reserved（只有目录，尚未实现调用）。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

#: 四类模型
CATEGORIES: List[Dict[str, str]] = [
    {"id": "text", "label": "文本模型", "hint": "写剧本、分镜、台词、提示词润色"},
    {"id": "image", "label": "图像模型", "hint": "首帧、参考图、封面"},
    {"id": "video", "label": "视频模型", "hint": "分镜出片（多数按次计费）"},
    {"id": "audio", "label": "音频模型", "hint": "配音、声音克隆、转写"},
]

#: secrets 桶（设置里存 Key 的三个位置）
BUCKETS = ("llm_api_keys", "api_keys", "tts_api_keys")


def _p(cid: str, label: str, *, protocol: str, base_url: str, path: str = "",
       default_model: str = "", models: Optional[List[Dict[str, str]]] = None,
       bucket: str = "llm_api_keys", doc: str = "", status: str = "implemented",
       env: Optional[List[str]] = None, notes: str = "",
       supports: Optional[Dict[str, bool]] = None) -> Dict[str, Any]:
    """构造一条平台定义（内部用，保证字段齐全）。"""
    return {
        "id": cid,
        "label": label,
        "protocol": protocol,
        "base_url": base_url,
        "path": path,
        "default_model": default_model,
        "models": models or [],
        "key_field": bucket,
        "doc": doc,
        "status": status,
        "env": env or [],
        "notes": notes,
        "supports": supports or {},
    }


#: ─────────────────────────── 文本模型 ───────────────────────────
TEXT_PROVIDERS: List[Dict[str, Any]] = [
    _p("deepseek", "DeepSeek", protocol="openai", base_url="https://api.deepseek.com",
       path="/v1/chat/completions", default_model="deepseek-chat", doc="https://api-docs.deepseek.com",
       models=[{"id": "deepseek-chat", "label": "DeepSeek-V3 对话"},
               {"id": "deepseek-reasoner", "label": "DeepSeek-R1 推理"}],
       supports={"stream": True, "tools": True}),
    _p("openai", "OpenAI", protocol="openai", base_url="https://api.openai.com",
       path="/v1/chat/completions", default_model="gpt-4o", doc="https://platform.openai.com/docs",
       models=[{"id": "gpt-4o", "label": "GPT-4o"}, {"id": "gpt-4o-mini", "label": "GPT-4o mini"},
               {"id": "o3", "label": "o3 推理"}, {"id": "gpt-4.1", "label": "GPT-4.1"}],
       supports={"stream": True, "tools": True, "vision": True}),
    _p("anthropic", "Anthropic Claude", protocol="anthropic", base_url="https://api.anthropic.com",
       path="/v1/messages", default_model="claude-sonnet-4-5", doc="https://docs.anthropic.com",
       models=[{"id": "claude-sonnet-4-5", "label": "Claude Sonnet 4.5"},
               {"id": "claude-opus-4-1", "label": "Claude Opus 4.1"},
               {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5"}],
       env=["ANTHROPIC_API_KEY"], supports={"stream": True, "tools": True, "vision": True},
       notes="必须用 x-api-key + anthropic-version 头，不能走 OpenAI 兼容路径"),
    _p("gemini", "Google Gemini", protocol="gemini", base_url="https://generativelanguage.googleapis.com",
       path="/v1beta/models/{model}:generateContent", default_model="gemini-2.5-pro",
       doc="https://ai.google.dev/gemini-api/docs", env=["GEMINI_API_KEY", "GOOGLE_API_KEY"],
       models=[{"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro"},
               {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash"}],
       supports={"vision": True}, notes="Key 走 query 参数 key= 或 x-goog-api-key 头"),
    _p("qwen", "阿里通义千问", protocol="openai",
       base_url="https://dashscope.aliyuncs.com/compatible-mode", path="/v1/chat/completions",
       default_model="qwen-plus", doc="https://help.aliyun.com/zh/model-studio/",
       models=[{"id": "qwen-plus", "label": "Qwen Plus"}, {"id": "qwen-max", "label": "Qwen Max"},
               {"id": "qwen3-max", "label": "Qwen3 Max"}], supports={"stream": True, "tools": True}),
    _p("kimi", "Moonshot Kimi", protocol="openai", base_url="https://api.moonshot.cn",
       path="/v1/chat/completions", default_model="moonshot-v1-32k",
       doc="https://platform.moonshot.cn/docs",
       models=[{"id": "moonshot-v1-8k", "label": "Kimi 8K"},
               {"id": "moonshot-v1-32k", "label": "Kimi 32K"},
               {"id": "moonshot-v1-128k", "label": "Kimi 128K"}]),
    _p("glm", "智谱 GLM", protocol="openai", base_url="https://open.bigmodel.cn/api/paas/v4",
       path="/chat/completions", default_model="glm-4-plus", doc="https://open.bigmodel.cn/dev/api",
       models=[{"id": "glm-4-plus", "label": "GLM-4 Plus"}, {"id": "glm-4-flash", "label": "GLM-4 Flash 免费"}]),
    _p("doubao", "字节豆包（火山方舟）", protocol="openai",
       base_url="https://ark.cn-beijing.volces.com/api/v3", path="/chat/completions",
       default_model="doubao-pro-32k", doc="https://www.volcengine.com/docs/82379",
       models=[{"id": "doubao-pro-32k", "label": "豆包 Pro 32K"},
               {"id": "doubao-1.5-pro-256k", "label": "豆包 1.5 Pro 256K"}]),
    _p("hunyuan", "腾讯混元", protocol="openai", base_url="https://api.hunyuan.cloud.tencent.com",
       path="/v1/chat/completions", default_model="hunyuan-turbo",
       doc="https://cloud.tencent.com/document/product/1729",
       models=[{"id": "hunyuan-turbo", "label": "混元 Turbo"},
               {"id": "hunyuan-pro", "label": "混元 Pro"}]),
    _p("minimax", "MiniMax", protocol="openai", base_url="https://api.minimaxi.com",
       path="/v1/chat/completions", default_model="MiniMax-Text-01",
       doc="https://platform.minimaxi.com/document", models=[{"id": "MiniMax-Text-01", "label": "MiniMax Text 01"}]),
    _p("openrouter", "OpenRouter（聚合）", protocol="openai", base_url="https://openrouter.ai/api/v1",
       path="/chat/completions", default_model="anthropic/claude-sonnet-4.5",
       doc="https://openrouter.ai/docs", env=["OPENROUTER_API_KEY"],
       models=[{"id": "anthropic/claude-sonnet-4.5", "label": "Claude Sonnet 4.5（经 OpenRouter）"},
               {"id": "openai/gpt-4o", "label": "GPT-4o（经 OpenRouter）"},
               {"id": "deepseek/deepseek-chat", "label": "DeepSeek V3（经 OpenRouter）"}],
       notes="一个 Key 通吃多家，适合没有各厂账号时先跑通"),
    _p("siliconflow", "硅基流动 SiliconFlow", protocol="openai",
       base_url="https://api.siliconflow.cn", path="/v1/chat/completions",
       default_model="Qwen/Qwen2.5-72B-Instruct", doc="https://docs.siliconflow.cn",
       models=[{"id": "Qwen/Qwen2.5-72B-Instruct", "label": "Qwen2.5 72B"},
               {"id": "deepseek-ai/DeepSeek-V3", "label": "DeepSeek V3"}],
       bucket="tts_api_keys", notes="与配音/图像同一个 Key（账号级）"),
    _p("groq", "Groq", protocol="openai", base_url="https://api.groq.com/openai",
       path="/v1/chat/completions", default_model="llama-3.3-70b-versatile",
       doc="https://console.groq.com/docs", env=["GROQ_API_KEY"],
       models=[{"id": "llama-3.3-70b-versatile", "label": "Llama 3.3 70B（极快）"}]),
    _p("ollama", "本地 Ollama", protocol="ollama", base_url="http://127.0.0.1:11434",
       path="/api/chat", default_model="qwen2.5:latest", doc="https://ollama.com",
       models=[{"id": "qwen2.5:latest", "label": "Qwen2.5 本地"},
               {"id": "llama3.1:8b", "label": "Llama 3.1 8B 本地"}],
       notes="本机推理，不需要 Key，也不花钱"),
    _p("agentai", "AgentAI 中转模型（一个 Key 通吃）", protocol="openai",
       base_url="https://agentai.token6688.com", path="/v1/chat/completions",
       default_model="claude-sonnet-5", bucket="api_keys", doc="",
       models=[{"id": _id, "label": _lb} for _id, _lb in [("claude-sonnet-5", "Claude Sonnet 5（经中转）"), ("claude-opus-5", "Claude Opus 5（经中转）"), ("gpt-5.6-sol", "GPT-5.6 Sol（经中转）"), ("gpt-5.4-mini", "GPT-5.4 mini（经中转）"), ("gemini-3-pro", "Gemini 3 Pro（经中转）"), ("gemini-3.8-flash", "Gemini 3.8 Flash（经中转）"), ("grok-4.7", "Grok 4.7（经中转）"), ("qwen3.8-max", "Qwen3.8 Max（经中转）"), ("kimi-k3", "Kimi K3（经中转）"), ("glm-5.3", "GLM-5.3（经中转）"), ("minimax-m3", "MiniMax M3（经中转）"), ("deepseek-v4-pro", "DeepSeek V4 Pro（经中转）"), ("doubao-seed-2-0-pro", "豆包 Seed 2.0 Pro（经中转）")]],
       notes="国外平台本机直连会超时；这里有 Key 就能用，模型清单可动态拉取"),
]

#: ─────────────────────────── 图像模型 ───────────────────────────
IMAGE_PROVIDERS: List[Dict[str, Any]] = [
    _p("openai", "OpenAI 图像", protocol="openai", base_url="https://api.openai.com",
       path="/v1/images/generations", default_model="gpt-image-1", bucket="api_keys",
       doc="https://platform.openai.com/docs/guides/images",
       models=[{"id": "gpt-image-1", "label": "gpt-image-1"}, {"id": "dall-e-3", "label": "DALL·E 3"}]),
    _p("ark", "字节即梦 / Seedream", protocol="native", base_url="https://ark.cn-beijing.volces.com/api/v3",
       path="/images/generations", default_model="doubao-seedream-3-0-t2i", bucket="api_keys",
       doc="https://www.volcengine.com/docs/82379",
       models=[{"id": "doubao-seedream-3-0-t2i", "label": "Seedream 3.0 文生图"}]),
    _p("gemini", "Google Imagen / Gemini 图像", protocol="gemini",
       base_url="https://generativelanguage.googleapis.com",
       path="/v1beta/models/{model}:predict", default_model="imagen-4.0-generate-001",
       bucket="api_keys", doc="https://ai.google.dev/gemini-api/docs/image-generation",
       models=[{"id": "imagen-4.0-generate-001", "label": "Imagen 4"},
               {"id": "gemini-2.5-flash-image", "label": "Gemini 2.5 Flash 图像"}]),
    _p("wanx", "阿里通义万相", protocol="native",
       base_url="https://dashscope.aliyuncs.com/api/v1", path="/services/aigc/text2image/image-synthesis",
       default_model="wanx2.1-t2i-turbo", bucket="api_keys",
       doc="https://help.aliyun.com/zh/model-studio/", models=[{"id": "wanx2.1-t2i-turbo", "label": "万相 2.1 Turbo"}]),
    _p("qwen", "通义 Qwen-Image", protocol="native",
       base_url="https://dashscope.aliyuncs.com/api/v1", path="/services/aigc/multimodal-generation/generation",
       default_model="qwen-image", bucket="api_keys", doc="https://help.aliyun.com/zh/model-studio/",
       models=[{"id": "qwen-image", "label": "Qwen-Image"}]),
    _p("minimax", "MiniMax 图像", protocol="native", base_url="https://api.minimaxi.com",
       path="/v1/image_generation", default_model="image-01", bucket="api_keys",
       doc="https://platform.minimaxi.com/document", models=[{"id": "image-01", "label": "image-01"}]),
    _p("siliconflow", "硅基流动（Flux / SD）", protocol="openai", base_url="https://api.siliconflow.cn",
       path="/v1/images/generations", default_model="Tongyi-MAI/Z-Image-Turbo",
       bucket="tts_api_keys", doc="https://docs.siliconflow.cn",
       models=[{"id": "Tongyi-MAI/Z-Image-Turbo", "label": "Z-Image Turbo（实测验通）"},
               {"id": "Qwen/Qwen-Image", "label": "Qwen-Image"}]),
    _p("zhipu", "智谱 CogView", protocol="openai", base_url="https://open.bigmodel.cn/api/paas/v4",
       path="/images/generations", default_model="cogview-4", bucket="api_keys",
       doc="https://open.bigmodel.cn/dev/api", models=[{"id": "cogview-4", "label": "CogView-4"},
                                                       {"id": "cogview-3-flash", "label": "CogView-3 Flash 免费"}]),
    _p("stability", "Stability AI", protocol="native", base_url="https://api.stability.ai",
       path="/v2beta/stable-image/generate/core", default_model="sd3.5-large", bucket="api_keys",
       doc="https://platform.stability.ai/docs", models=[{"id": "sd3.5-large", "label": "SD 3.5 Large"}]),
    _p("local", "本地 ComfyUI / SD WebUI", protocol="native", base_url="http://127.0.0.1:8188",
       path="/prompt", default_model="local", bucket="api_keys",
       doc="", models=[{"id": "local", "label": "本地推理"}], notes="不花钱，需要自建服务"),
    _p("agentai", "AgentAI 中转图像", protocol="openai",
       base_url="https://agentai.token6688.com", path="/v1/images/generations",
       default_model="gemini-3-pro-image", bucket="api_keys", doc="",
       models=[{"id": "gemini-3-pro-image", "label": "Gemini 3 Pro Image（经中转）"}],
       notes="中转图像通道，一个 Key 共用"),
]

#: ─────────────────────────── 视频模型 ───────────────────────────
VIDEO_PROVIDERS: List[Dict[str, Any]] = [
    _p("seedance_t6", "Seedance（中转，已实测）", protocol="native", base_url="https://agentai.token6688.com",
       path="/api/v3/contents/generations/tasks", default_model="seedance-2-0-promo", bucket="api_keys",
       doc="", models=[{"id": "seedance-2-0-promo", "label": "Seedance 2.0 Pro（促销档）"},
                       {"id": "seedance-1-5-pro", "label": "Seedance 1.5 Pro"}],
       notes="当前主力通道；480p 实测可用，按次计费"),
    _p("seedance", "火山 Seedance 官方", protocol="native", base_url="https://ark.cn-beijing.volces.com/api/v3",
       path="/contents/generations/tasks", default_model="doubao-seedance-1-0-pro", bucket="api_keys",
       doc="https://www.volcengine.com/docs/82379", models=[{"id": "doubao-seedance-1-0-pro", "label": "Seedance 1.0 Pro"}]),
    _p("kling", "可灵 Kling", protocol="native", base_url="https://api.klingai.com",
       path="/v1/videos/image2video", default_model="kling-v2", bucket="api_keys",
       doc="https://app.klingai.com/cn/dev/document-api",
       models=[{"id": "kling-v2", "label": "Kling 2.0"}, {"id": "kling-v1-6", "label": "Kling 1.6"}]),
    _p("hailuo", "海螺 Hailuo（MiniMax）", protocol="native", base_url="https://api.minimaxi.com",
       path="/v1/video_generation", default_model="MiniMax-Hailuo-02", bucket="api_keys",
       doc="https://platform.minimaxi.com/document", models=[{"id": "MiniMax-Hailuo-02", "label": "Hailuo 02"}]),
    _p("sora", "OpenAI Sora", protocol="native", base_url="https://api.openai.com",
       path="/v1/videos", default_model="sora-2", bucket="api_keys", doc="https://platform.openai.com/docs/guides/video",
       models=[{"id": "sora-2", "label": "Sora 2"}]),
    _p("runway", "Runway Gen-4", protocol="native", base_url="https://api.dev.runwayml.com",
       path="/v1/image_to_video", default_model="gen4_turbo", bucket="api_keys",
       doc="https://docs.dev.runwayml.com", models=[{"id": "gen4_turbo", "label": "Gen-4 Turbo"}]),
    _p("luma", "Luma Dream Machine", protocol="native", base_url="https://api.lumalabs.ai",
       path="/dream-machine/v1/generations", default_model="ray-2", bucket="api_keys",
       doc="https://docs.lumalabs.ai", models=[{"id": "ray-2", "label": "Ray 2"}]),
    _p("pika", "Pika", protocol="native", base_url="https://api.pika.art",
       path="/v1/generate", default_model="pika-2.2", bucket="api_keys", doc="", models=[{"id": "pika-2.2", "label": "Pika 2.2"}]),
    _p("jimeng", "即梦 Jimeng", protocol="native", base_url="https://ark.cn-beijing.volces.com/api/v3",
       path="/contents/generations/tasks", default_model="jimeng-video", bucket="api_keys", doc="",
       models=[{"id": "jimeng-video", "label": "即梦视频"}]),
    _p("wanx", "通义万相视频", protocol="native", base_url="https://dashscope.aliyuncs.com/api/v1",
       path="/services/aigc/video-generation/video-synthesis", default_model="wan2.1-t2v-turbo",
       bucket="api_keys", doc="https://help.aliyun.com/zh/model-studio/", models=[{"id": "wan2.1-t2v-turbo", "label": "Wan 2.1 Turbo"}]),
    _p("cogvideox", "智谱 CogVideoX", protocol="native", base_url="https://open.bigmodel.cn/api/paas/v4",
       path="/videos/generations", default_model="cogvideox-3", bucket="api_keys",
       doc="https://open.bigmodel.cn/dev/api", models=[{"id": "cogvideox-3", "label": "CogVideoX-3"}]),
    _p("vidu", "生数 Vidu", protocol="native", base_url="https://api.vidu.cn/ent/v2",
       path="/text2video", default_model="viduq2", bucket="api_keys", doc="https://platform.vidu.cn/docs",
       models=[{"id": "viduq2", "label": "Vidu Q2"}]),
    _p("pixverse", "PixVerse", protocol="native", base_url="https://app-api.pixverse.ai/openapi/v2",
       path="/video/text/generate", default_model="v4.5", bucket="api_keys", doc="https://docs.platform.pixverse.ai",
       models=[{"id": "v4.5", "label": "PixVerse V4.5"}]),
    _p("veo", "Google Veo", protocol="gemini", base_url="https://generativelanguage.googleapis.com",
       path="/v1beta/models/{model}:predictLongRunning", default_model="veo-3.0-generate-001",
       bucket="api_keys", doc="https://ai.google.dev/gemini-api/docs/video",
       models=[{"id": "veo-3.0-generate-001", "label": "Veo 3"}, {"id": "veo-3.0-fast-generate-001", "label": "Veo 3 Fast"}]),
]

#: ─────────────────────────── 音频模型 ───────────────────────────
AUDIO_PROVIDERS: List[Dict[str, Any]] = [
    _p("edge", "Edge TTS（免费内置）", protocol="native", base_url="", path="",
       default_model="zh-CN-XiaoxiaoNeural", bucket="tts_api_keys", doc="",
       models=[{"id": "zh-CN-XiaoxiaoNeural", "label": "晓晓（女声）"},
               {"id": "zh-CN-YunxiNeural", "label": "云希（男声）"}],
       notes="微软 Edge 免费引擎，无需 Key，不花钱"),
    _p("openai", "OpenAI TTS", protocol="openai", base_url="https://api.openai.com",
       path="/v1/audio/speech", default_model="gpt-4o-mini-tts", bucket="tts_api_keys",
       doc="https://platform.openai.com/docs/guides/text-to-speech",
       models=[{"id": "gpt-4o-mini-tts", "label": "gpt-4o-mini-tts"}, {"id": "tts-1-hd", "label": "TTS-1 HD"}]),
    _p("elevenlabs", "ElevenLabs", protocol="native", base_url="https://api.elevenlabs.io",
       path="/v1/text-to-speech/{voice_id}", default_model="eleven_multilingual_v2", bucket="tts_api_keys",
       doc="https://elevenlabs.io/docs", env=["ELEVENLABS_API_KEY"],
       models=[{"id": "eleven_multilingual_v2", "label": "Multilingual v2"},
               {"id": "eleven_turbo_v2_5", "label": "Turbo v2.5"}], notes="音色克隆最主流的一家"),
    _p("azure", "Azure 语音服务", protocol="native",
       base_url="https://{region}.tts.speech.microsoft.com", path="/cognitiveservices/v1",
       default_model="zh-CN-XiaoxiaoNeural", bucket="tts_api_keys", doc="https://learn.microsoft.com/azure/ai-services/speech-service/",
       models=[{"id": "zh-CN-XiaoxiaoNeural", "label": "晓晓"},
               {"id": "zh-CN-YunyeNeural", "label": "云野"}],
       notes="需要同时填 region（区域）"),
    _p("minimax", "MiniMax 语音", protocol="native", base_url="https://api.minimaxi.com",
       path="/v1/t2a_v2", default_model="speech-2.8-hd", bucket="tts_api_keys",
       doc="https://platform.minimaxi.com/document", models=[{"id": "speech-2.8-hd", "label": "speech-2.8 HD"}]),
    _p("dashscope", "阿里 CosyVoice", protocol="native",
       base_url="https://dashscope.aliyuncs.com/api/v1", path="/services/aigc/multimodal-generation/generation",
       default_model="cosyvoice-v2", bucket="tts_api_keys", doc="https://help.aliyun.com/zh/model-studio/",
       models=[{"id": "cosyvoice-v2", "label": "CosyVoice v2"}]),
    _p("siliconflow", "硅基流动 CosyVoice", protocol="openai", base_url="https://api.siliconflow.cn",
       path="/v1/audio/speech", default_model="fnlp/MOSS-TTSD-v0.5", bucket="tts_api_keys",
       doc="https://docs.siliconflow.cn", models=[{"id": "fnlp/MOSS-TTSD-v0.5", "label": "MOSS-TTSD（实测 1.49s 出音）"}, {"id": "FunAudioLLM/CosyVoice2-0.5B", "label": "CosyVoice2 0.5B（服务端实测超时）"}]),
    _p("volc", "火山引擎（豆包语音）", protocol="native", base_url="https://openspeech.bytedance.com",
       path="/api/v1/tts", default_model="zh_female_shuangkuaisisi_moon_bigtts", bucket="tts_api_keys",
       doc="https://www.volcengine.com/docs/6561", models=[{"id": "zh_female_shuangkuaisisi_moon_bigtts", "label": "爽快思思"}]),
    _p("xfyun", "讯飞星火语音", protocol="native", base_url="https://tts-api.xfyun.cn",
       path="/v2/tts", default_model="xiaoyan", bucket="tts_api_keys", doc="https://www.xfyun.cn/doc/",
       models=[{"id": "xiaoyan", "label": "小燕"}], notes="需要 app_id + api_key + api_secret 三件套"),
    _p("fish", "Fish Audio", protocol="native", base_url="https://api.fish.audio",
       path="/v1/tts", default_model="fish-speech-1.5", bucket="tts_api_keys", doc="https://docs.fish.audio",
       models=[{"id": "fish-speech-1.5", "label": "Fish Speech 1.5"}]),
    _p("local", "本地 GPT-SoVITS / IndexTTS", protocol="native", base_url="http://127.0.0.1:9880",
       path="/tts", default_model="local", bucket="tts_api_keys", doc="",
       models=[{"id": "local", "label": "本地推理"}], notes="不花钱，需自建服务"),
    _p("agentai", "AgentAI 中转语音", protocol="native",
       base_url="https://agentai.token6688.com", path="/v1/audio/speech",
       default_model="edge:zh-CN-XiaoxiaoNeural", bucket="tts_api_keys", doc="",
       models=[{"id": "edge:zh-CN-XiaoxiaoNeural", "label": "中转 Edge 晓晓"}],
       notes="一个 Key 走中转，当前默认配音通道之一"),
]

_BY_CATEGORY: Dict[str, List[Dict[str, Any]]] = {
    "text": TEXT_PROVIDERS, "image": IMAGE_PROVIDERS,
    "video": VIDEO_PROVIDERS, "audio": AUDIO_PROVIDERS,
}


def _configured(provider: Dict[str, Any], settings: Dict[str, Any]) -> Dict[str, Any]:
    """把「是否已配置 Key / 环境变量」合并进条目（不暴露 Key 本身）。"""
    bucket = provider.get("key_field") or "llm_api_keys"
    keys = settings.get(bucket) or {}
    if not isinstance(keys, dict):
        keys = {}
    # 大小写不敏感匹配：实测库里存的是 "MiniMax"、端点键是 "minimax" —— 精确匹配会漏
    has_key = any(str(k).lower() == provider["id"] for k in keys)
    env_hit = [name for name in (provider.get("env") or []) if os.environ.get(name)]
    return {"configured": bool(has_key or env_hit), "env_present": env_hit,
            "key_bucket": bucket}


def registry(settings: Optional[Dict[str, Any]] = None,
             selected: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """返回四类平台的完整目录（含是否已配置 Key、当前激活的平台/模型）。

    `selected` 形如 {"text": {"provider": "deepseek", "model": "deepseek-chat"}, ...}
    """
    settings = settings or {}
    selected = selected or {}
    categories: List[Dict[str, Any]] = []
    counts: Dict[str, Dict[str, int]] = {}
    for cat in CATEGORIES:
        rows: List[Dict[str, Any]] = []
        configured = 0
        chosen = selected.get(cat["id"]) or {}
        for provider in _BY_CATEGORY.get(cat["id"], []):
            entry = dict(provider)
            entry.update(_configured(provider, settings))
            # 只有「当前激活的那一家」才回填 selected_model；否则同类别所有平台
            # 都会显示同一个全局模型名（实测文本页 14 家全显示 deepseek-chat，误导用户）
            is_active = chosen.get("provider") == entry["id"]
            entry["active"] = bool(is_active)
            entry["selected_model"] = (chosen.get("model") or "") if is_active else ""
            if entry["configured"]:
                configured += 1
            rows.append(entry)
        counts[cat["id"]] = {"total": len(rows), "configured": configured}
        categories.append({**cat, "providers": rows})
    return {"version": 1, "categories": categories, "counts": counts,
            "buckets": list(BUCKETS)}

def find(category: str, provider_id: str) -> Optional[Dict[str, Any]]:
    """按类别取单个平台定义。"""
    for provider in _BY_CATEGORY.get(category, []):
        if provider["id"] == provider_id:
            return provider
    return None


def build_url(provider: Dict[str, Any], model: str = "") -> str:
    """拼出完整请求地址（路径里的 {model} 占位替换）。"""
    base = (provider.get("base_url") or "").rstrip("/")
    path = provider.get("path") or ""
    if "{model}" in path:
        path = path.replace("{model}", model or provider.get("default_model") or "")
    return base + path

#: 只有目录、还没有可调用适配器的平台 —— UI 必须显示「预留」，不能假装已打通。
#: 队友（llm-bridge / media-bridge）落地后，把对应 id 从这里移除即可翻转成 implemented。
_RESERVED: Dict[str, set] = {
    "text": set(),  # anthropic/gemini/ollama/siliconflow/groq 已由 llm-bridge 落地
    "image": set(),  # ark/gemini/zhipu/stability 已由 media-bridge 落地（未用真实 Key 验证）
    "video": {"vidu", "pixverse", "veo"},
    "audio": set(),  # 7 家已由 media-bridge 落地（未用真实 Key 验证）
}

for _cat, _ids in _RESERVED.items():
    for _prov in _BY_CATEGORY.get(_cat, []):
        if _prov["id"] in _ids:
            _prov["status"] = "reserved"

#: 中转（agentai）走 OpenAI 兼容协议 —— 适配器就是通用 openai 客户端
for _cat in ("text", "image", "audio"):
    for _prov in _BY_CATEGORY.get(_cat, []):
        if _prov["id"] == "agentai":
            _prov["status"] = "implemented"
