"""
VideoForge · LLM 客户端

支持：
- Ollama（本地，完全离线）
- DeepSeek（云端，性价比）
- OpenAI / GPT-4
- 任意 OpenAI 兼容 API（通过 base_url 配置）
"""

import asyncio
import copy
import json
import logging
import os
import re
from typing import Dict, Any, List, Optional
import httpx

logger = logging.getLogger("videoforge.llm")


# ──────────── 健壮性工具函数（借鉴 MPT llm.py）────────────
# 独立模块：from .llm_robust import ...
# 提供：sanitize_error_message / normalize_text_response / freeze_config /
#       is_retryable_http_status / retry_backoff_seconds / _MAX_RETRIES / _RETRY_BACKOFF
from .llm_robust import (
    sanitize_error_message as _sanitize_error_message,
    normalize_text_response as _normalize_text_response,
    freeze_config as _freeze_config,
    is_retryable_http_status as _is_retryable_http_status,
    retry_backoff_seconds as _retry_backoff_seconds,
    _MAX_RETRIES,
)

# ★ 2026-10-25：四种文本协议的客户端（openai / anthropic / gemini / ollama）。
#   为什么要单独一层：设置里能选的 14 家文本平台里，anthropic / gemini / ollama
#   的 HTTP 契约和 OpenAI 完全不同 —— 过去 `LLMClient.chat()` 只会拼
#   OpenAI 的 payload，于是「目录里有名字」不等于「选了能调通」。
from .llm_protocols import (
    DEFAULT_TIMEOUT as _PROTOCOL_DEFAULT_TIMEOUT,
    SUPPORTED_PROTOCOLS as _SUPPORTED_PROTOCOLS,
    call_protocol as _call_protocol,
)

# ★ 台词规范化（三层提示词的输出要用它统一成 {character,text,emotion}）
#   ⚠️ 这两个名字**必须在这里导入**：曾经漏了导入，而调用点在一个宽泛的
#   try/except 里 —— 结果是每次调用都抛 NameError，被 except 吞掉后走
#   "文本降级"兜底，把 **LLM 的原始 JSON 文本**塞进了 layer1_overview。
#   线上表现为：分镜的第 1 层是一大段 ```json {...}```，
#   第 2/3 层全空；送给视频模型的提示词因此是一段 JSON 垃圾，
#   用户看到的就是"生成的视频跟实际描述的完全不相干"。
from .dialogue import (
    normalize_timeline,
    timeline_dialogue_stats,
)


# ──────────── Provider 端点表（OpenAI 兼容，2026-09 校验）────────────
# 说明：httpx 的 base_url 与相对 path 拼接遵循绝对路径规则，
#       因此 base_url 不带末尾斜杠、path 以 / 开头即可得到正确完整 URL。
# 之前只处理 deepseek/openai/ollama，其余 provider（含 MiniMax）base_url 为空，
# 导致 httpx.UnsupportedProtocol: Request URL is missing 'http://' —— 剧本生成必失败。
PROVIDER_ENDPOINTS: Dict[str, Dict[str, str]] = {
    "deepseek":  {"base_url": "https://api.deepseek.com",                       "path": "/v1/chat/completions", "default_model": "deepseek-chat"},
    "openai":    {"base_url": "https://api.openai.com",                         "path": "/v1/chat/completions", "default_model": "gpt-4o"},
    "minimax":   {"base_url": "https://api.minimaxi.com",                       "path": "/v1/chat/completions", "default_model": "MiniMax-Text-01"},
    "qwen":      {"base_url": "https://dashscope.aliyuncs.com/compatible-mode", "path": "/v1/chat/completions", "default_model": "qwen-plus"},
    "dashscope": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode", "path": "/v1/chat/completions", "default_model": "qwen-plus"},
    "glm":       {"base_url": "https://open.bigmodel.cn/api/paas/v4",           "path": "/chat/completions",    "default_model": "glm-4-plus"},
    "zhipu":     {"base_url": "https://open.bigmodel.cn/api/paas/v4",           "path": "/chat/completions",    "default_model": "glm-4-plus"},
    "kimi":      {"base_url": "https://api.moonshot.cn",                        "path": "/v1/chat/completions", "default_model": "moonshot-v1-8k"},
    "moonshot":  {"base_url": "https://api.moonshot.cn",                        "path": "/v1/chat/completions", "default_model": "moonshot-v1-8k"},
    "doubao":    {"base_url": "https://ark.cn-beijing.volces.com/api/v3",       "path": "/chat/completions",    "default_model": ""},
    "hunyuan":   {"base_url": "https://api.hunyuan.cloud.tencent.com",          "path": "/v1/chat/completions", "default_model": "hunyuan-turbo"},
    # OpenRouter（OpenAI 兼容聚合；多个免费 model 标 ":free" 后缀）
    # key 放 .env 里 OPENROUTER_API_KEY；model 字段在设置里手动选（如 "nex-agi/nex-n2.5-pro:free"）
    "openrouter": {"base_url": "https://openrouter.ai/api/v1",                  "path": "/chat/completions",    "default_model": "nex-agi/nex-n2.5-mini:free"},
}

# provider 名规范化：前端传 "MiniMax" / "MiniMax-Text-01" 等大小写混写
_PROVIDER_ALIASES = {
    "minimax": "minimax", "minimaxi": "minimax", "abab": "minimax",
    "qwen": "qwen", "tongyi": "qwen", "aliyun": "qwen", "dashscope": "dashscope",
    "glm": "glm", "zhipu": "glm", "bigmodel": "glm", "chatglm": "glm",
    "kimi": "kimi", "moonshot": "moonshot",
    "doubao": "doubao", "volcengine": "doubao", "ark": "doubao",
    "hunyuan": "hunyuan", "tencent": "hunyuan",
    "deepseek": "deepseek", "openai": "openai", "ollama": "ollama",
    "openrouter": "openrouter", "or": "openrouter", "nex": "openrouter", "nex-agi": "openrouter",
    # ★ 2026-10-25：注册表里另外四家（各自协议不同，见 core/llm_protocols.py）
    "anthropic": "anthropic", "claude": "anthropic",
    "gemini": "gemini", "google": "gemini", "googleai": "gemini",
    "groq": "groq",
    "siliconflow": "siliconflow", "silicon": "siliconflow", "硅基流动": "siliconflow",
}


def normalize_provider(provider: str) -> str:
    p = (provider or "").strip().lower()
    if p in _PROVIDER_ALIASES:
        return _PROVIDER_ALIASES[p]
    # ★ 2026-10-25：设置里可能存的是注册表的 **显示名**（如 "Anthropic Claude"、
    #   "AgentAI 中转模型（一个 Key 通吃）"）。旧实现只认 id，显示名会被当成
    #   未知平台直接报错。先按 id / label 精确匹配一遍。
    for spec in _iter_text_providers():
        pid = str(spec.get("id") or "").strip().lower()
        if pid and p == pid:
            return pid
        label = str(spec.get("label") or "").strip().lower()
        if label and p == label:
            return pid
    # 模糊匹配（如 "minimax-Text-01" 之类的误填）
    for alias, canon in _PROVIDER_ALIASES.items():
        if alias in p:
            return canon
    return p


def _registry_text_provider(provider: str) -> Optional[Dict[str, Any]]:
    """从统一注册表取文本平台定义（平台 id、protocol、base_url、key_field、env…）。

    注册表是「设置里能选哪些平台」的唯一事实源；这里做**懒导入 + 容错**：
    即使某天 provider_registry 被单独拆走，`core.llm` 也应能按老端点表继续工作。
    """
    try:
        from . import provider_registry as _registry
        return _registry.find("text", provider)
    except Exception:  # noqa: BLE001
        return None


def _iter_text_providers() -> List[Dict[str, Any]]:
    """注册表里的全部文本平台（用于识别「过期 base_url」等判断）。"""
    try:
        from . import provider_registry as _registry
        return list(getattr(_registry, "TEXT_PROVIDERS", []) or [])
    except Exception:  # noqa: BLE001
        return []


def _norm_gateway_url(url: str) -> str:
    """把 base_url 归一成「去掉已知接口后缀」的可比形式。

    只用于两个判断：① 自定义 base_url 是否是另一家平台的官方地址；
    ② 哪些平台共用同一个网关（同一个 Key）。不做任何请求。
    """
    u = str(url or "").strip().rstrip("/").lower()
    for suffix in ("/v1/chat/completions", "/chat/completions",
                   "/v1/messages", "/v1beta"):
        if u.endswith(suffix):
            u = u[: -len(suffix)]
    return u.rstrip("/")


def _sibling_provider_ids(spec: Dict[str, Any]) -> List[str]:
    """同一网关下的兄弟平台 id。

    为什么需要：中转站（如 agentai）在文本/图像/视频/音频四类里共用**同一把 Key**，
    但 Key 只存在其中一个桶名/平台名下（真实库：`api_keys["seedance_t6"]` 就是
    agentai 的 Key，而文本平台 id 是 `agentai`）。按「同 base_url」找出兄弟平台，
    才能让 agentai 文本平台真正取到 Key，而不是 401。
    """
    base = _norm_gateway_url(spec.get("base_url"))
    if not base:
        return []
    ids: List[str] = []
    own_id = str(spec.get("id") or "")
    try:
        from . import provider_registry as _registry
        for attr in ("TEXT_PROVIDERS", "IMAGE_PROVIDERS",
                     "VIDEO_PROVIDERS", "AUDIO_PROVIDERS"):
            for other in getattr(_registry, attr, []) or []:
                other_id = str(other.get("id") or "")
                if not other_id or other_id == own_id:
                    continue
                if _norm_gateway_url(other.get("base_url")) == base and other_id not in ids:
                    ids.append(other_id)
    except Exception:  # noqa: BLE001
        return []
    return ids


def provider_key(settings: Dict[str, Any], provider: str) -> str:
    """按平台取已配置的 API Key（**大小写不敏感**，跨 secrets 桶，最后查环境变量）。

    ★ 为什么必须大小写不敏感（真实库实测）：
      应用库里存的是 `llm_api_keys={"MiniMax": "sk-cp-…"}`，
      而注册表里的平台 id 是 `minimax`。任何 `keys.get(provider)` 的精确匹配
      都会取不到 Key —— 现象是「设置里明明填了 Key，一切到 MiniMax 就 401」。
      更隐蔽的是：`tts_api_keys` 里恰好有一条小写 `minimax`（配音用的同一个 Key），
      精确匹配还可能拿到**另一把** Key。

    取 Key 顺序：
      1. 该平台自己声明的 `key_field`（如 siliconflow 的 Key 在 `tts_api_keys`）
      2. `llm_api_keys` → `api_keys` → `tts_api_keys`（都在 `BUCKETS` 里）
      3. **同一网关的兄弟平台**（如 agentai 的 Key 实际存在 `api_keys["seedance_t6"]`）
      4. 该平台声明的环境变量（`env`，如 ANTHROPIC_API_KEY）
    桶内先精确名、再大小写不敏感匹配；**只在自己/同网关的桶里找，不跨网关借 Key**。
    """
    settings = settings or {}
    spec = _registry_text_provider(normalize_provider(provider)) or {}
    bucket_order: List[str] = []
    declared = str(spec.get("key_field") or "").strip()
    if declared:
        bucket_order.append(declared)
    try:
        from . import provider_registry as _registry
        for bucket in getattr(_registry, "BUCKETS", ()) or ():
            if bucket not in bucket_order:
                bucket_order.append(str(bucket))
    except Exception:  # noqa: BLE001
        pass
    for bucket in ("llm_api_keys", "api_keys", "tts_api_keys"):
        if bucket not in bucket_order:
            bucket_order.append(bucket)

    names = []
    for candidate in (provider, normalize_provider(provider)):
        candidate = str(candidate or "").strip()
        if candidate and candidate not in names:
            names.append(candidate)
    sibling_ids = _sibling_provider_ids(spec) if spec else []

    for bucket in bucket_order:
        data = settings.get(bucket) or {}
        if not isinstance(data, dict):
            continue
        # ① 精确命中（保持有人故意大小写一致时的可预期行为）
        for name in names:
            value = data.get(name)
            if value:
                return str(value).strip()
        # ② 大小写不敏感命中（真实库的 `MiniMax` 就靠这一步）
        wanted = {name.lower() for name in names}
        for stored_name, value in data.items():
            if value and str(stored_name).strip().lower() in wanted:
                return str(value).strip()
        # ③ 同一网关的兄弟平台（agentai ← seedance_t6 这类共用 Key）
        for sibling in sibling_ids:
            value = data.get(sibling)
            if value:
                return str(value).strip()
        sibling_lower = {s.lower() for s in sibling_ids}
        for stored_name, value in data.items():
            if value and str(stored_name).strip().lower() in sibling_lower:
                return str(value).strip()

    for env_name in spec.get("env") or []:
        value = os.environ.get(str(env_name))
        if value:
            return str(value).strip()
    return ""




class LLMClient:
    """统一 LLM 客户端（所有 provider 均可解析出可用端点与协议）"""

    def __init__(self, provider: str = "deepseek",
                 api_key: str = "",
                 base_url: str = "",
                 model: str = ""):
        self.provider = provider
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        # ★ 2026-10-25：没显式给 Key 时，按**该平台自己声明的**环境变量兜底。
        #   只查同一平台的 env（如 ANTHROPIC_API_KEY），绝不跨平台借 Key ——
        #   借 Key 会把「配置错了」伪装成「能用」，比直接 401 更糟。
        if not self.api_key:
            spec = _registry_text_provider(normalize_provider(provider)) or {}
            for env_name in spec.get("env") or []:
                env_val = os.environ.get(str(env_name))
                if env_val:
                    self.api_key = str(env_val).strip()
                    break

    # ──────────── 设置 → 客户端（大小写不敏感的 Key 解析）────────────

    @classmethod
    def from_settings(cls, settings: Dict[str, Any]) -> "LLMClient":
        """从应用设置构造客户端：**按设置里选中的平台**路由，Key 大小写不敏感。

        ★ 为什么要有这个入口（真实库实测）：
          `main.py::_make_llm` 用的是 `llm_keys.get(provider)` 精确匹配，
          而真实库里 Key 存成 `{"MiniMax": "sk-cp-…"}`、平台 id 是 `minimax`
          ⇒ 一切到 MiniMax 就是空 Key → 401。`orchestrator` 那条链路走
          `chat.resolve_llm_settings()`（大小写不敏感）所以没事，两条链路行为不一致。
        本入口把「provider 规范化 → key_field → 三个 secrets 桶 → 环境变量」收成一处，
        main.py 的 owner 只要把构造改成 `LLMClient.from_settings(settings)` 即可。

        其它语义与现有实现保持一致：`enable_llm_local` 优先走 ollama；
        `llm_model` 为空时用**该平台自己的**默认模型（绝不回落到别家模型名）。
        """
        settings = settings or {}
        provider_raw = str(settings.get("llm_provider")
                           or settings.get("remote_llm_provider") or "deepseek").strip()
        if settings.get("enable_llm_local"):
            provider_raw = "ollama"
        canon = normalize_provider(provider_raw) or provider_raw
        spec = _registry_text_provider(canon) or {}
        model = str(settings.get("llm_model")
                    or settings.get("remote_llm_model") or "").strip()
        if not model:
            model = str(spec.get("default_model") or "")
        base_url = str(settings.get("llm_base_url") or "")
        if canon == "ollama":
            base_url = str(settings.get("ollama_url")
                           or settings.get("ollama_base_url") or base_url or "")
        return cls(provider=canon, api_key=provider_key(settings, provider_raw),
                   model=model, base_url=base_url)

    # ──────────── 平台识别 / 协议路由 ────────────

    @staticmethod
    def _norm_base_url(url: str) -> str:
        """把 base_url 归一成「去掉已知接口后缀」的可比形式（只用于判断是否过期设置）。"""
        return _norm_gateway_url(url)

    @classmethod
    def _base_url_is_stale(cls, canon: str, base_url: str) -> bool:
        """判断自定义 base_url 是不是**另一家平台的官方地址**（= 过期的全局设置）。

        ★ 真实场景（这是本轮读到用户库才发现的）：
          旧设置里 `llm_base_url` 被写死成 `https://api.deepseek.com`。
          之后用户在「模型」页把文本平台切成 openai / anthropic / gemini，
          `main.py::_make_llm` 仍会把这个**全局** base_url 传给 LLMClient ——
          于是请求被发到 DeepSeek 的地址上，报 401/404，看起来像「新平台不能用」。
        ★ 只忽略**能确定是另一家平台官方地址**的情况：
          真正的自建网关 / 中转地址（不等于任何一家的官方 base）照旧生效，
          不会误伤「用 openai 槽位接 deepseek 中转」这类合法用法。
        """
        mine = cls._norm_base_url(base_url)
        if not mine:
            return False
        for spec in _iter_text_providers():
            if str(spec.get("id")) == canon:
                continue
            if cls._norm_base_url(spec.get("base_url")) == mine:
                logger.warning(
                    "自定义 base_url=%s 是「%s」的官方地址，与当前平台「%s」不匹配，"
                    "判定为过期全局设置并忽略（请到设置里改回对应平台地址）",
                    base_url, spec.get("id"), canon)
                return True
        return False

    def resolve_protocol(self) -> str:
        """返回该平台应使用哪一种文本协议（openai / anthropic / gemini / ollama）。

        优先级（单一事实源在 `core/provider_registry.py`）：
          ① 注册表里该平台的 `protocol`
          ② 老端点表 `PROVIDER_ENDPOINTS` 里的平台 → openai（兼容历史）
          ③ 用户自填 base_url 的未知平台 → 视为 OpenAI 兼容
          ④ 都没有 → 抛明确错误
        **不做任何跨平台降级**：认不出就报错，绝不悄悄改用 deepseek。
        """
        canon = normalize_provider(self.provider)
        if canon == "ollama":
            return "ollama"
        spec = _registry_text_provider(canon)
        if spec:
            protocol = str(spec.get("protocol") or "").strip().lower()
            if protocol in _SUPPORTED_PROTOCOLS:
                return protocol
        if canon in PROVIDER_ENDPOINTS:
            return "openai"
        if (self.base_url or "").strip():
            return "openai"
        raise ValueError(
            f"未识别的文本平台「{self.provider}」：它不在平台注册表里，也没有 base_url。"
            f"设置里可选的文本平台：deepseek/openai/anthropic/gemini/qwen/kimi/glm/"
            f"doubao/hunyuan/minimax/openrouter/siliconflow/groq/ollama；"
            f"支持的协议：{'/'.join(_SUPPORTED_PROTOCOLS)}。"
            f"不会自动改用其他平台。"
        )

    def resolve_endpoint(self) -> tuple:
        """返回 (base_url, chat_path, model)。未知 provider 且无 base_url 时抛明确错误。"""
        canon = normalize_provider(self.provider)
        conf = PROVIDER_ENDPOINTS.get(canon)
        spec = _registry_text_provider(canon)

        # ★ 2026-10-25：自定义 base_url 若是**另一家平台的官方地址**，按过期设置忽略。
        #   见 `_base_url_is_stale` 的说明（这是"切换平台后 401/404"的一个真实入口）。
        custom = (self.base_url or "").strip().rstrip("/")
        if custom and self._base_url_is_stale(canon, custom):
            custom = ""

        if canon == "ollama":
            base = (custom
                    or (spec or {}).get("base_url")
                    or "http://localhost:11434")
            path = str((spec or {}).get("path") or "/api/chat")
            model = self.model or str((spec or {}).get("default_model") or "") or "qwen2.5:7b"
            return (base, path, model)

        if conf:
            base = custom or conf["base_url"]
            path = conf["path"]
            # ★★ 2026-10-20：**base_url 结尾已带 /v1 时不能重复拼**。
            #   用户接自建网关/中转时，习惯把地址填成 `.../v1`，
            #   而 openai 这类 provider 的 path 本身就是 `/v1/chat/completions`
            #   ⇒ 拼出来是 `/v1/v1/chat/completions`，上游回
            #     `Unknown request URL ... Your base_url appears to already include /v1`
            #   实测：白膜"按剧本生成 3D 规格"这一步就是因为这个 404 静默回退到兜底。
            if path.startswith("/v1") and base.endswith("/v1"):
                base = base[: -len("/v1")]
            # 用户把完整 chat 路径填在 base_url 里时的兼容处理
            if base.endswith("/v1/chat/completions") or base.endswith("/chat/completions"):
                if base.endswith("/v1/chat/completions"):
                    base = base[: -len("/v1/chat/completions")]
                else:
                    base = base[: -len("/chat/completions")]
                    path = "/chat/completions"
            model = self.model or conf.get("default_model") or \
                str((spec or {}).get("default_model") or "")
            return (base, path, model)

        # ★ 2026-10-25：注册表新增平台（anthropic / gemini / groq / siliconflow…）。
        #   之前这些名字在端点表里没有，会落到下面的「未知 provider」分支直接报错。
        if spec:
            base = custom or str(spec.get("base_url") or "")
            path = str(spec.get("path") or "")
            model = self.model or str(spec.get("default_model") or "")
            if not base:
                raise ValueError(
                    f"平台「{self.provider}」在注册表里没有 base_url，且未填写自定义地址，无法调用。")
            # Gemini 的路径里有 {model} 占位
            if "{model}" in path:
                path = path.replace("{model}", model)
            # 路径与 base 都带 /v1beta 或 /v1 时不重复拼
            if path.startswith("/v1beta") and base.endswith("/v1beta"):
                base = base[: -len("/v1beta")]
            if path.startswith("/v1") and base.endswith("/v1"):
                base = base[: -len("/v1")]
            return (base, path, model)

        # 未知 provider：必须由用户提供 base_url（OpenAI 兼容）
        base = custom
        if not base:
            raise ValueError(
                f"未识别的 LLM provider「{self.provider}」且未填写 base_url。"
                f"请在设置中选择已支持的 provider（DeepSeek/OpenAI/Anthropic/Gemini/"
                f"Qwen/Kimi/GLM/Doubao/混元/MiniMax/OpenRouter/SiliconFlow/Groq/Ollama），"
                f"或填写 OpenAI 兼容的 base_url。"
            )
        if not base.startswith(("http://", "https://")):
            raise ValueError(f"base_url 必须以 http:// 或 https:// 开头，当前为「{base}」")
        path = "/v1/chat/completions"
        if base.endswith("/v1/chat/completions"):
            base = base[: -len("/v1/chat/completions")]
        elif base.endswith("/v1"):
            # 与已知平台同样处理：base 已带 /v1 时不再重复拼成 /v1/v1/...
            base = base[: -len("/v1")]
        return (base, path, self.model or "gpt-3.5-turbo")


    def _client(self) -> httpx.AsyncClient:
        base, _path, _model = self.resolve_endpoint()
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        # ★★ 2026-09-29：read 超时 180 → 600 秒；并加 trust_env=False。
        #
        #   1) read 超时：实测 M3 写剧本 reasoning_tokens=7513、正文 3040 字符、
        #      耗时 118s。180 秒对「强制开思考 + 写长剧本」明显不够，
        #      超时后还会被 _MAX_RETRIES=5 的退避重试放大成几分钟空等。
        #
        #   2) trust_env=False：这是**实测修好的**。同一请求：
        #          curl --http1.1          → HTTP 200，105 秒，27144 字节 ✓
        #          httpx（带 trust_env=True） → RemoteProtocolError:
        #                                      Server disconnected without
        #                                      sending a response（64 秒）✗
        #      本机装了系统代理，httpx 默认会读环境变量走代理，
        #      长连接生成到一半被代理掐断。core/imagegen.py 早就加了
        #      trust_env=False，llm.py 漏了。
        return httpx.AsyncClient(
            base_url=base,
            timeout=httpx.Timeout(connect=15.0, read=600.0, write=60.0, pool=10.0),
            headers=headers,
            trust_env=False,
        )

    async def complete(self, messages: List[Dict[str, str]],
                       temperature: float = 0.7,
                       max_tokens: int = 4000,
                       *,
                       timeout: Optional[float] = None) -> Dict[str, Any]:
        """单次调用，返回 `core.llm_protocols` 的统一结构（**不重试、不降级**）。

        给「连通性测试 / 成本极小的真实探测」用：调用方拿得到 status_code、
        latency_ms、error 原文，而不是一个被包装过的异常。
        对 OpenAI 兼容平台同样适用（也会显式带上 platform 的 base/path/model）。
        """
        protocol = self.resolve_protocol()
        base, path, model = self.resolve_endpoint()
        return await _call_protocol(
            protocol, base_url=base, path=path, api_key=self.api_key, model=model,
            messages=messages, temperature=temperature, max_tokens=max_tokens,
            timeout=timeout or _PROTOCOL_DEFAULT_TIMEOUT)

    async def chat(self, messages: List[Dict[str, str]],
                   temperature: float = 0.7,
                   max_tokens: int = 4000,
                   response_format: Optional[Dict] = None,
                   reasoning_effort: Optional[str] = None) -> str:
        """
        发送聊天请求，返回文本。

        ★ 2026-10-25：**按平台协议路由**。OpenAI 兼容平台（deepseek/openai/qwen/
        kimi/glm/doubao/hunyuan/minimax/openrouter/siliconflow/groq…）继续走下面的
        原逻辑；anthropic / gemini / ollama 走 `core.llm_protocols` 的独立协议实现。
        保持签名与返回结构不变（有大量调用方与测试依赖）。

        ★ 2026-09-29 新增 `reasoning_effort`：M3 / M3.1 这类**强制开思考**的
        模型默认把大半预算用于思考（实测写剧本 reasoning_tokens=7513 vs
        正文 3040 字符）。传 `"low"` 可显著压缩思考量且不影响剧本质量 ——
        实测带剧本锁时 low 档 118s 出完整正文，不加则超时 10 分钟。
        对不支持该参数的 provider 直接忽略（多传一个字段无害）。
        """
        canon = normalize_provider(self.provider)
        # ★ 非 OpenAI 协议：请求/响应契约完全不同，不能继续拼 OpenAI 的 payload。
        protocol = self.resolve_protocol()
        if protocol != "openai":
            logger.debug("LLM 走 %s 协议（平台 %s，模型 %s）", protocol, canon, self.model)
            return await self._chat_with_protocol(
                protocol, canon, messages, temperature=temperature,
                max_tokens=max_tokens)
        base, path, model = self.resolve_endpoint()
        client = self._client()
        try:
            payload = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": False,
            }
            # ★ 思考深度：只在显式传入时带（见 chat() 的 docstring）
            eff = str(reasoning_effort or "").strip().lower()
            if eff in ("low", "medium", "high", "xhigh", "max"):
                payload["reasoning_effort"] = eff
            if response_format:
                payload["response_format"] = response_format

            # ★★★ 2026-10-20：**接上退避重试**（之前这些函数 import 了却没人用，
            #   实际是"只发一次、失败就死"）。
            #   实测事故：MiniMax 集群过载返回 529（正文写明"请稍后重试"），
            #   剧本这一步当场失败 → 用户点一次确认就白等一场，还以为软件坏了。
            #   现在：429/5xx 全部重试；**529 单独用更长的退避**
            #   （那是"集群过载"，等 1~2 秒重试没有意义，得给它喘息时间）。
            _last_detail = ""
            _last_code = 0
            for _attempt in range(_MAX_RETRIES + 1):
                r = await client.post(path, json=payload)
                if r.status_code < 400:
                    break
                # 把上游错误正文带出来，便于前端/日志定位（而不是只吐 HTTP 码）
                detail = ""
                try:
                    body = r.json()
                    detail = (
                        (body.get("error") or {}).get("message")
                        or body.get("message")
                        or json.dumps(body, ensure_ascii=False)[:300]
                    )
                except Exception:
                    detail = (r.text or "")[:300]
                detail = _sanitize_error_message(detail)
                _last_detail, _last_code = detail, r.status_code
                if not _is_retryable_http_status(r.status_code):
                    raise RuntimeError(
                        f"LLM 调用失败 [{canon} {model}] HTTP {r.status_code}: {detail}"
                    )
                if _attempt >= _MAX_RETRIES:
                    break
                if r.status_code == 529:
                    # "当前服务集群负载较高，请稍后重试" ⇒ 等久一点再试
                    _wait = (5.0, 10.0, 20.0, 35.0, 50.0)[min(_attempt, 4)]
                else:
                    _wait = _retry_backoff_seconds(_attempt)
                logger.warning("LLM 调用 HTTP %s，%.1fs 后重试（第 %s 次）: %s",
                               r.status_code, _wait, _attempt + 1, detail[:120])
                await asyncio.sleep(_wait)
            else:
                pass
            if r.status_code >= 400:
                raise RuntimeError(
                    f"LLM 调用失败 [{canon} {model}] HTTP {_last_code}: {_last_detail}"
                    f"（已自动重试 {_MAX_RETRIES} 次）"
                )
            data = r.json()
            content = data["choices"][0]["message"]["content"]

            # ★★ 2026-09-29：**思考吃光预算时自动救一次**（而不是直接抛错）。
            #
            #   这不是猜测，是本轮真实踩出来的：/script/generate 报
            #   「[minimax] returned empty text content after think-block
            #   cleanup」，而剧本其实**早就生成成功了**，是紧随其后的
            #   generate_three_layer_prompt 挂了，把整个请求打成 500。
            #
            #   根因：M3 / M3.1 强制开 thinking，思考 token 也计入
            #   max_tokens。预算给小了就是「想完了、没话说」。
            #   实测：4000 → reasoning 3999 / 正文 1；提到 16000 正常。
            #
            #   这里做两层保险：
            #     1) 调用方忘了给够预算时，自动翻倍重试一次；
            #     2) 同时把 effort 降到 low，把思考压下去。
            #   这样即使将来又有人新写一个 max_tokens=2000 的调用点，
            #   也不会再出现“内容其实没问题、结果整个流程 500”的情况。
            try:
                return _normalize_text_response(content, canon)
            except ValueError:
                u = data.get("usage") or {}
                d = u.get("completion_tokens_details") or {}
                reason_tok = d.get("reasoning_tokens") or 0
                if (reason_tok <= 0) or (int(max_tokens) >= 64000):
                    raise
                logger.warning(
                    "[%s] 思考吃光预算（reasoning=%s / max_tokens=%s）"
                    " → 加预算到 %s 并把 effort 降到 low 重试一次",
                    canon, reason_tok, max_tokens, min(64000, int(max_tokens) * 2))
                payload["max_tokens"] = min(64000, int(max_tokens) * 2)
                if "reasoning_effort" in payload:
                    payload["reasoning_effort"] = "low"
                r2 = await client.post(path, json=payload)
                if r2.status_code >= 400:
                    raise RuntimeError(
                        f"LLM 重试仍失败 [{canon} {model}] HTTP {r2.status_code}: "
                        f"{_sanitize_error_message(r2.text[:300])}"
                    )
                c2 = (r2.json().get("choices") or [{}])[0].get("message", {}).get("content")
                try:
                    return _normalize_text_response(c2, canon)
                except ValueError:
                    # 第二次还空：彻底换成 low（原来若没带 effort，这次一定要带）
                    payload["reasoning_effort"] = "low"
                    payload["max_tokens"] = 64000
                    r3 = await client.post(path, json=payload)
                    if r3.status_code >= 400:
                        raise RuntimeError(
                            f"LLM 二次重试仍失败 [{canon} {model}] "
                            f"HTTP {r3.status_code}: "
                            f"{_sanitize_error_message(r3.text[:300])}"
                        )
                    c3 = (r3.json().get("choices") or [{}])[0].get("message", {}).get("content")
                    return _normalize_text_response(c3, canon)
        finally:
            await client.aclose()

    async def _chat_with_protocol(self, protocol: str, canon: str,
                                  messages: List[Dict[str, str]], *,
                                  temperature: float = 0.7,
                                  max_tokens: int = 4000) -> str:
        """非 OpenAI 协议的聊天入口（anthropic / gemini / ollama）。

        与 OpenAI 分支保持**同一套失败语义**：可重试状态（429/5xx，529 特殊）
        按现有退避重试；其余失败直接抛 RuntimeError，错误里带平台/模型/HTTP 码
        与上游原文 —— 绝不悄悄换成别的平台再试一次。
        """
        base, path, model = self.resolve_endpoint()
        last: Dict[str, Any] = {}
        for attempt in range(_MAX_RETRIES + 1):
            last = await _call_protocol(
                protocol, base_url=base, path=path, api_key=self.api_key,
                model=model, messages=messages, temperature=temperature,
                max_tokens=max_tokens)
            if last.get("ok"):
                try:
                    return _normalize_text_response(last.get("text"), canon)
                except ValueError as exc:
                    raise RuntimeError(
                        f"LLM 返回空文本 [{canon} {model}]（{protocol} 协议）：{exc}"
                    ) from exc
            status_code = last.get("status_code")
            retryable = status_code is not None and _is_retryable_http_status(status_code)
            if not retryable or attempt >= _MAX_RETRIES:
                break
            wait = 5.0 if status_code == 529 else _retry_backoff_seconds(attempt)
            logger.warning("%s 协议调用 HTTP %s，%.1fs 后重试（第 %s 次）：%s",
                           protocol, status_code, wait, attempt + 1,
                           str(last.get("error") or "")[:160])
            await asyncio.sleep(wait)
        status_text = (" HTTP %s" % last.get("status_code")) if last.get("status_code") else ""
        raise RuntimeError(
            f"LLM 调用失败 [{canon} {model}]（{protocol} 协议）{status_text}: "
            f"{last.get('error') or '未知错误'}"
        )


# ──────────── 健壮 JSON 提取（LLM 输出容错）────────────
# LLM 常见问题：markdown 代码块、前后说明文字、尾随逗号、单/中文引号、
# Python 风格 True/False/None、截断（max_tokens 用尽）、不可见字符。
# 解析失败会直接导致"《解析失败》"剧本 —— 这里做多级容错。

_JSON_FIXES = (
    # 中文引号 → 英文
    ("\u201c", '"'), ("\u201d", '"'), ("\u2018", "'"), ("\u2019", "'"),
    # 全角符号
    ("\uff1a", ":"), ("\uff0c", ","),
)

_PY_LITERALS = (
    (re.compile(r"\bTrue\b"), "true"),
    (re.compile(r"\bFalse\b"), "false"),
    (re.compile(r"\bNone\b"), "null"),
)

_TRAILING_COMMA = re.compile(r",\s*([}\]])")
_LINE_COMMENT = re.compile(r"(?m)^\s*//.*$")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)

# 供 _apply_fixes_strict 使用：同样的映射，但只在**字符串字面量之外**生效
_FIX_CHARS = dict(_JSON_FIXES)


def _strip_fences(text: str) -> str:
    """去掉 markdown 代码块围栏（```json ... ``` / ``` ... ```）"""
    t = text.strip()
    # 去掉 BOM / 零宽字符
    t = t.replace("\ufeff", "").replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")
    if t.startswith("```"):
        lines = t.split("\n")
        lines = lines[1:]                                   # 去掉 ```json
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


def _balanced_slice(text: str, opener: str = "{", closer: str = "}") -> Optional[str]:
    """扫描出第一个括号平衡的 JSON 片段（跳过字符串内的括号）"""
    start = text.find(opener)
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    # 未闭合 → 返回剩余部分（可能被截断，交给修复器补全）
    return text[start:]


def _fix_single_quotes(s: str) -> str:
    """把 JSON 里的单引号键/值转成双引号（启发式，避免破坏已是双引号的字符串内容）"""
    out = re.sub(r"'([^'\"\\\n]*)'(\s*:)", r'"\1"\2', s)      # 'key':
    out = re.sub(r"(:\s*)'([^'\"\\\n]*)'", r'\1"\2"', out)      # :'value'
    out = re.sub(r"(,\s*)'([^'\"\\\n]*)'", r'\1"\2"', out)      # ,'value'
    out = re.sub(r"(\[\s*)'([^'\"\\\n]*)'", r'\1"\2"', out)     # ['value'
    return out


def _apply_fixes_strict(s: str) -> str:
    """把中文引号 / 全角标点换成 ASCII，但**不动字符串字面量里面的字符**。

    ★ 2026-09-25 修：老实现是 `str.replace` 全文盲替换，有两个真实后果——
      1) 台词/字幕里的中文标点被悄悄改成半角：
         `{"text":"别喊了，我听见了",}` → 修完变成 `别喊了,我听见了`
         （逗号变了，字幕跟着变）；
      2) 值里本来就带中文引号时**直接把 JSON 改坏**：
         `{"text":"他说“你好”",}` 修完值里凭空多出两个 ASCII 引号（越界定界符）——
         → 整个剧本走重试，重试还失败就是《解析失败》。
    这里只替换"引号之外"的字符，内容原样保留。
    """
    out: List[str] = []
    in_str = False
    esc = False
    for ch in s:
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        rep = _FIX_CHARS.get(ch)
        if rep is None:
            out.append(ch)
            if ch == '"':
                in_str = True
            continue
        out.append(rep)
        # 中文引号换成 ASCII 双引号之后，它就是一个真正的字符串定界符
        if rep == '"':
            in_str = True
    return "".join(out)


def _repair_tail(out: str) -> str:
    """`_repair_json` / `_repair_json_strict` 共用的后半段（顺序与历史一致）"""
    out = _BLOCK_COMMENT.sub("", out)
    out = _LINE_COMMENT.sub("", out)
    for pat, rep in _PY_LITERALS:
        out = pat.sub(rep, out)
    if "'" in out:                     # 存在单引号 → 尝试按 JSON 单引号风格修复
        out = _fix_single_quotes(out)
    out = _TRAILING_COMMA.sub(r"\1", out)
    return out


def _repair_json(s: str) -> str:
    """修复常见 JSON 语法问题（历史行为：中文引号/全角标点**全文盲替换**）"""
    out = s
    for bad, good in _JSON_FIXES:
        out = out.replace(bad, good)
    return _repair_tail(out)


def _repair_json_strict(s: str) -> str:
    """同 `_repair_json`，但中文引号/全角标点只在字符串**之外**替换"""
    return _repair_tail(_apply_fixes_strict(s))


def _repair_variants(s: str) -> List[str]:
    """严格修复优先，再退回历史盲替换。

    为什么两个都要试：模型把中文引号当**定界符**用时（`{“a”：“x”}`），
    严格模式不认它、修不好，必须靠盲替换；而中文引号是**内容**时
    （`{"t":"他说“你好”"}`），盲替换会把 JSON 改坏，只有严格模式对。
    严格模式先试——它只要解析成功，内容就是逐字正确的。
    """
    strict = _repair_json_strict(s)
    blind = _repair_json(s)
    return [strict] if strict == blind else [strict, blind]


def _in_open_string(s: str) -> bool:
    in_str = False
    esc = False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
    return in_str


def _close_truncated(s: str) -> str:
    """按括号栈补全被截断的 JSON（并闭合未结束的字符串 / 缺失的值）"""
    out = s.rstrip()
    if _in_open_string(out):
        out += '"'
    out = re.sub(r",\s*\"[^\"]*\"\s*:\s*$", "", out)   # 尾部不完整的 "key":
    out = re.sub(r":\s*$", ': ""', out)                # 值缺失
    out = re.sub(r",\s*$", "", out)

    stack: List[str] = []
    in_str = False
    esc = False
    for ch in out:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch == "}":
            if stack and stack[-1] == "{":
                stack.pop()
        elif ch == "]":
            if stack and stack[-1] == "[":
                stack.pop()
    for opener in reversed(stack):
        out += "}" if opener == "{" else "]"
    return out


def extract_json(text: str) -> Any:
    """从 LLM 输出中尽可能提取 JSON 对象；全部失败时抛出 ValueError（附原始片段）"""
    if text is None:
        raise ValueError("LLM 返回为空")
    raw = _strip_fences(str(text))

    # 1) 直接解析（原文 / 修复后）
    for candidate in (raw, *_repair_variants(raw)):
        try:
            return json.loads(candidate)
        except Exception:
            pass

    # 2) 平衡括号切片：对象优先；数组仅当文本本身就是数组根时才尝试
    slices = ["{", "}"] if raw.find("{") >= 0 else None
    attempts = []
    if slices:
        piece = _balanced_slice(raw, "{", "}")
        if piece:
            attempts.append(piece)
    if raw.lstrip().startswith("["):
        piece = _balanced_slice(raw, "[", "]")
        if piece:
            attempts.append(piece)

    for piece in attempts:
        for candidate in (piece, *_repair_variants(piece),
                          _close_truncated(_repair_json_strict(piece)),
                          _close_truncated(_repair_json(piece)),
                          _close_truncated(piece)):
            try:
                return json.loads(candidate)
            except Exception:
                pass

    # 3) 正则兜底：抓第一个 {...} 区块（跨行、非贪婪）
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        piece = m.group(0)
        for candidate in (piece, *_repair_variants(piece),
                          _close_truncated(_repair_json_strict(piece)),
                          _close_truncated(_repair_json(piece)),
                          _close_truncated(piece)):
            try:
                return json.loads(candidate)
            except Exception:
                pass

    raise ValueError(f"无法从模型输出中解析 JSON（前 200 字）：{raw[:200]!r}")


# ──────────── 提示词模板（剧本生成 + 抽象化）────────────

# ★★★ 2026-09-29：思考型模型预算（实测铁证）★★★���
#
#   原来四处调用都写死 max_tokens=4000。用打包版 VideoForge 复现剧本生成：
#       completion_tokens : 4000
#       reasoning_tokens  : 3999   ← 全部预算被思考吃掉
#       正文 token        : 1
#   VideoForge 报 "empty text content after think-block cleanup"
#   （清理掉 <think> 后什么都不剩）。
#
#   同一请求把预算提到 16000：
#       completion=4627  reasoning=2776  正文 3910 字符  48 秒  ✓
#
#   结论：**M3 / M3.1 这类强制开思考的模型，4000 不够写剧本。**
#   不要改回去。
_MAX_TOKENS_FOR_THINKING_MODELS = 16000

# ★ 2026-09-29：**剧本锁** system 追加段。
#   实测不加锁时，模型把定稿剧本整个改写：主角「陈砚」→「阿诚」、
#   老周从受害者变成「吞他家产的反派」、核心事件（识破秤钩/钱袋/暗线）丢失。
#   这段追加在原 system 之后，优先级高于其中的创作要求。
SCRIPT_LOCK_SYSTEM = """

════════════════════════════════════════════════════════════
【剧本锁·最高优先级】用户传来的内容是**已经定稿的剧本**，不是创意草稿。
你当前的任务**不是创作**，而是**转写**：把剧本落实成标准分镜 JSON。
· 人物名、人物关系、身份、立场：照抄，不得改
· 场次、事件、因果、结局：照抄，不得改
· 道具、场景、时间线：照抄，不得改
· 剧本没写的东西不要补。想不出来就留空，不要编。
你唯一被允许的「创作」是技术层面的：景别、机位、时长切分、字幕点题，
这些不影响剧情。
如果剧本本身有矛盾或你不认同，**照原样转写**，并在 JSON 之后另起一行
写「备注：你的疑问」。**绝对不要自己改。**
════════════════════════════════════════════════════════════"""


SCRIPT_GENERATION_SYSTEM = """你是专业 AI 短片编剧。

你将接收用户的创意想法，输出标准剧本格式（按场次组织）。

═══════════════════════════════════════════════════════════════
★ 写法要求（从一份**真实成品剧本**里量出来的，不是泛泛的"要专业"）
  参考物料：《40集农村微短剧剧本：寒潮抢收玉米（全集分集台词+镜头）》
  （用户提供，40 集全量实测：标题 4–10 字；每场 1–3 句台词；每场正文 32–83 字）
═══════════════════════════════════════════════════════════════
1. **每场标题是一个"钩子"**：4–10 字的短句，直接点出这一场的冲突或悬念。
   例：「寒潮预警，全村慌了」「有人带头拦着不卖」「大嘴彻底慌神」「全村和解」。
   不要写"第一场""开场""发展"这种没有信息量的标题。
2. **每场三件套**：`镜头`（一句画面：地点 + 主体动作 + 天光/景别）+
   `对白`（1–2 句，口语、短、带情绪）+ 需要时 `subtitle`（字幕点题或留悬念）。
3. **台词要短**：中文约 4 字/秒；一句台词不要超过这一场秒数能说完的长度。
   宁愿少说一句，也不要写长台词（长了会被迫变速，听起来很假）。
   可以在角色名后写表演提示，例如「阿诚（喇叭喊话）：…」「大嘴（高声煽动）：…」。
4. **每场只写一两句话的信息量**（正文 30–85 字）。不要写小说，不要写文学化心理描写，
   要写**能拍出来的东西**。
5. **主角必须有前史动机**：让观众明白他为什么做这个选择
   （成品：`我爹当年和你们一样，死守不卖，赌行情翻倍…最后八亩地五毛一斤甩卖`）。
   把这段前史放在一个**独立的揭示场**里，标题就是它的钩子。
6. **反派要有"行为方式"**，不是"他很坏"：写清他**怎么做**
   （成品：大嘴"爱煽动、赌行情" → 具体动作是拦住别人、当众喊话、事后翻脸不认账）。
7. **群像要摇摆**：群众不是背景板 —— 分化 → 动摇 → 后悔 → 和解，
   用「议论 / 两拨 / 犹豫 / 后悔 / 沉默」这类**具体反应**写出来。
8. **结尾金句点题**：最后一场用一句短而狠的话 + `subtitle` 收束
   （成品：阿诚终极旁白 + 字幕「不贪即是赚，止损方为赢」）。
9. 故事要有清晰的起承转合；每场结尾**留钩子**（有冲突、有留白），让人想追下一场。
10. 适合直接改写成视频提示词（避免文学化、抽象化表述）。

JSON Schema（`subtitle` / `hook` / `role` / `motivation` 是新增字段，
下游会用它们做剧本体检与字幕，请**尽量填**）：
{
  "title": "短片标题",
  "intent": "原样复述用户要达成的事（不许改写成别的目标）",
  "genre": "drama",
  "logline": "一句话概括",
  "style": "cinematic/documentary/anime/ad/mv",
  "scenes": [
    {
      "scene_number": 1,
      "title": "4-10 字的钩子标题",
      "beat": "这一场承担的节拍（钩子/承/转/合）",
      "prev_link": "与上一场的衔接：时间/地点/人物状态/因果（一句话）",
      "exit_state": "这一场结束时的状态：人物位置/动作/情绪（一句话）",
      "next_hook": "下一场的钩子（一句话）",
      "hook": "这一场靠什么抓住人（一句话）",
      "location": "具体地点",
      "duration_seconds": 10,
      "characters": ["角色A", "角色B"],
      "actions": ["动作1", "动作2"],
      "dialogues": [{"character": "A", "text": "台词（短、口语）", "emotion": "calm",
                     "delivery": "可选：表演提示，如 喇叭喊话"}],
      "subtitle": "可选：这一场的字幕（点题或留悬念）",
      "pacing": {"plot": "松/中/紧", "emotion": "轻/中/重",
                 "shot_density": "长镜头/快切/混合", "size_focus": "远全景/中景/特写/混合"},
      "golden_line": "本场金句原文（没有就空字符串）",
      "mood": "tense/calm/excited/..."
    }
  ],
  "characters": [
    {
      "name": "角色A",
      "role": "protagonist/antagonist/supporting",
      "description": "面部/体型/年龄描述",
      "motivation": "他的前史动机（反派则写他的行为方式）",
      "costume": "服装描述"
    }
  ],
  "scenes_meta": [
    {"name": "场景A", "lighting": "自然光", "color_palette": ["#xxx", "#xxx"]}
  ]
}
"""


# ★★ 2026-10-23（用户：「剧本设定既不围绕用户意图做好设定」）：
#   原来的 SCRIPT_GENERATION_SYSTEM 是**短剧公式写死的**（前史动机/反派/群像/
#   结尾金句），于是用户说"做个产品宣传片"，模型也去编反派与冲突 ——
#   剧本自然不围绕意图。这里按**体裁**分流：宣传片用宣传片写法，
#   剧情类沿用原来的短剧公式（那份是用户手册里的方法，不动）。
SCRIPT_GENERATION_SYSTEM_PROMO = """你是专业**商业短片**导演与编剧（宣传片 / 品牌片 / 口播 / 产品片）。

你收到用户的创意想法，输出标准剧本格式（按场次组织）。

═══════════════════════════════════════════════════════════════
★ 最高原则：**为用户要的那件事服务**
  1. 第一件事是**复述用户意图**：先想清楚"这条片子要让谁看、看完要相信什么、
     要他做什么"，把它写进 JSON 的 `intent` 字段，一字不改。
  2. 任何一场戏，如果删掉它用户要传达的信息就受损，那它就有用；
     反之，删掉它更好 —— 就别写。**不要为了"像一部片子"而加无关的场次。**
  3. 不许自行发明产品名、数据、承诺、奖项、用户评价。
     用户给了就原样用；用户没给，就用中性描述，不要编。
  4. 不要写"反派/前史/恩怨/反转" —— 宣传片没有敌人，痛点就是敌人。
═══════════════════════════════════════════════════════════════
★ 写法要求（宣传片的行货标准）
  1. **每场标题是钩子**：4–12 字，直接点出这一场要观众记住什么
     （"3 秒出方案""雪地里那道坎""他愣了一下"）。不要写"第一场""开场"。
  2. **每场三件套**：`镜头`（地点 + 主体动作 + 天光/景别）+
     `对白`（1–2 句，口语、短、带情绪）+ 需要时 `subtitle`（字幕点题）。
  3. **台词要短**：中文约 4 字/秒；一句不要超过这一场秒数能说完的长度。
     宁可少说一句，也不要写长台词（长了会被迫变速，听起来很假）。
  4. **每场只写一两句话的信息量**（正文 30–85 字）。不写小说，不写文学化心理描写。
  5. **证据比形容词值钱**：写"雪地里 12 只玉米袋""3 秒出一版方案"，
     不写"震撼""高端""极致体验"。
  6. **画面文字按需要给**：该出现（产品名、卖点字、字幕、包装上的字）就写清内容；
     不需要就一个字也别提（**不要写"不要出现文字"这类否定式**）。
  7. **衔接要清楚**：每场写 `prev_link`（与上一场怎么接：时间、地点、人物状态、因果）
     和 `exit_state`（这一场结束时人物在哪、在做什么、什么情绪），
     下一场从那里接着走 —— 不许人物在两场之间"闪身"换位置。

JSON Schema（`intent` / `genre` / `prev_link` / `exit_state` / `next_hook` / `beat`
是新加字段，下游会用来做剧本体检与分镜衔接，请**认真填**）：
{
  "title": "短片标题",
  "intent": "原样复述用户要达成的事（不要改写目标）",
  "genre": "promo",
  "logline": "一句话概括",
  "style": "cinematic/documentary/ad/mv",
  "scenes": [
    {
      "scene_number": 1,
      "title": "4-12 字的钩子标题",
      "beat": "这一场在节拍里的位置（钩子/痛点/方案/证据/信任/行动）",
      "hook": "这一场靠什么抓住人（一句话）",
      "location": "具体地点",
      "duration_seconds": 10,
      "characters": ["角色A", "角色B"],
      "actions": ["动作1", "动作2"],
      "dialogues": [{"character": "A", "text": "台词（短、口语）", "emotion": "calm",
                     "delivery": "可选：表演提示"}],
      "subtitle": "可选：这一场的字幕（点题）",
      "prev_link": "与上一场的衔接：时间/地点/人物状态/因果（一句话）",
      "exit_state": "这一场结束时的状态：人物位置/动作/情绪（一句话）",
      "next_hook": "下一场的钩子（一句话）",
      "pacing": {"plot": "松/中/紧", "emotion": "轻/中/重",
                 "shot_density": "长镜头/快切/混合", "size_focus": "远全景/中景/特写/混合"},
      "golden_line": "本场金句原文（没有就空字符串）",
      "mood": "calm/warm/urgent/..."
    }
  ],
  "characters": [
    {
      "name": "角色A",
      "role": "protagonist/supporting",
      "description": "面部/体型/年龄描述",
      "voice_hint": "可选：这个人适合什么语气/情绪基调",
      "costume": "服装描述"
    }
  ],
  "scenes_meta": [
    {"name": "场景A", "lighting": "自然光", "color_palette": ["#xxx", "#xxx"]}
  ]
}
"""

SCRIPT_GENERATION_SYSTEM_NEUTRAL = """你是专业 AI 短片编剧。

你收到用户的创意想法，输出标准剧本格式（按场次组织）。

═══════════════════════════════════════════════════════════════
★ 最高原则：先复述意图，再写片子
  1. JSON 里必须有 `intent` 字段，**原样复述**用户要达成的事。
  2. 结构按"意图 → 展开 → 收束"推进；体裁未指明时**不要自行加戏剧冲突**。
  3. 不许自行发明人物、数字、承诺、结局；用户没给就保持中性。
  4. 每场写 `prev_link`（与上一场怎么接）与 `exit_state`（结束时人物在哪、在做什么），
     相邻两场必须接得上（人物不许在两场之间换位置）。
  5. 每场标题是钩子（4–12 字）；每场三件套：镜头 + 对白 +（可选）字幕；
     台词短（中文约 4 字/秒），正文 30–85 字，不写小说。
  6. 画面文字按需要给（该出现就写内容，不需要就一个字不提，不写否定式）。

JSON Schema（`intent` / `genre` / `prev_link` / `exit_state` / `next_hook` / `beat` 是新加字段）：
{
  "title": "短片标题",
  "intent": "原样复述用户要达成的事",
  "genre": "generic",
  "logline": "一句话概括",
  "style": "cinematic/documentary/ad/mv",
  "scenes": [
    {
      "scene_number": 1,
      "title": "4-12 字的钩子标题",
      "beat": "这一场承担的节拍",
      "hook": "靠什么抓住人",
      "location": "具体地点",
      "duration_seconds": 10,
      "characters": ["角色A"],
      "actions": ["动作1"],
      "dialogues": [{"character": "A", "text": "台词（短、口语）", "emotion": "calm"}],
      "subtitle": "可选字幕",
      "prev_link": "与上一场的衔接（时间/地点/人物状态/因果）",
      "exit_state": "这一场结束时的状态（人物位置/动作/情绪）",
      "next_hook": "下一场的钩子",
      "pacing": {"plot": "松/中/紧", "emotion": "轻/中/重",
                 "shot_density": "长镜头/快切/混合", "size_focus": "远全景/中景/特写/混合"},
      "golden_line": "本场金句原文（没有就空字符串）",
      "mood": "calm/..."
    }
  ],
  "characters": [{"name": "角色A", "role": "protagonist/supporting",
                  "description": "描述", "costume": "服装"}],
  "scenes_meta": [{"name": "场景A", "lighting": "自然光"}]
}
"""

THREE_LAYER_PROMPT_SYSTEM = """你是 AI 视频提示词专家，同时兼任这部剧的**台词编剧与配音导演**。

把一个场次扩展成"三层提示词"，用于视频生成 API。

═══════════════════════════════════════════════════════════════
★ 台词是一等公民（这是最容易做错、也最影响成片质量的一环）
═══════════════════════════════════════════════════════════════
必须把**真正要说出口的话**逐字写进 `dialogue.text`。
绝对不要用"说出台词""喊道""低声说道"这类**描述代替台词本身** ——
下游要拿这段文本去合成语音、对齐字幕，写成描述就等于没有台词。

错误示范（严禁）：{"action": "李连长抬头看向小虎，语气坚定地说出台词"}
正确示范：{"action": "李连长抬头看向小虎", "dialogue": {"character": "李连长",
          "text": "小虎，天黑之前必须把阵地夺回来。", "emotion": "坚定"}}

规则：
1. 每个时间片最多一条 `dialogue`；没有说话的时间片 `dialogue` 为 `null`。
2. `dialogue.character` 必须用**场次里出现的确切人物名**（下游按名字匹配音色）。
3. 台词长度必须和该时间片的秒数匹配：**中文约每秒 4 个字**。
   例：3 秒的片子最多 12 个字。宁可短，不要长 —— 长了会被迫变速，听起来很假。
4. 台词要口语化、有性格、推进剧情；不要写成旁白说明。
5. 仅扩展原场次已经写明的对白或旁白。没有对白的纯动作、空镜、静物展示，
   全部 `dialogue` 为 `null`，`narration` 为空字符串；不得为了填字段加人声。
6. `speakers` 列出本场所有开口说话的角色名（没有就空数组）。
7. **时间片非等分**：同一镜 `layer2_timeline` 的片长**不许全部相同**；≥3 片时至少 3 个取值；
   金句片给最长、爆发片给最短。片长 ≈ 台词字数/4.0 + 气口（0.2s/句，金句 0.5s）+ 动作节拍。
8. 每片必须写 `"intent": "action|reaction|burst|golden|ambient"`（下游按它拆切点、定长短呼吸）。
9. **钩子落点**：第 1 镜的第 1 片必须是对白或动作事件，空镜铺垫不超过 2 秒。

═══════════════════════════════════════════════════════════════
★ 微表情是一等公民（biaoqing-yanyi skill 的核心要求）
═══════════════════════════════════════════════════════════════
只有本场有出镜人物时才使用以下微表情规则；无人物场景 `expression` 为空，
不得给物体添加眼睛、皮肤、呼吸或身体表演。角色库中的非本场人物不得加入画面。
每个时间片的 `expression` **不要**只写一个情绪标签（"坚定"/"愤怒"），
要写**看得见的微表情**：眼神往哪看、什么时候避开、什么时候回看、
嘴角/唇线怎么动、呼吸在哪里断、下颌和肩背的状态。

写作顺序（照这个顺序想，别直接从表情开始）：
    人格气质 → 表达态度 → 情绪波 → 声音节奏 → 身体动作 → 画面参数

必守规则：
1. **不许从情绪峰值起手。** 情绪波形状是
   「压住 → 微微泄漏 → 收回 → 再次泄漏 → 峰值 → 余味」；
   第一拍必须是**压住**（目光下垂/唇线压平/呼吸收住），
   峰值只能出现在中后段，最后要有余味。
2. **先动眼睛，再动大表情。** 眼睛比嘴早。
3. **先隐藏，后外露。** 内心的东西先压着，再漏出来。
4. 身体只允许**写实的小动作**（低头、抬眼、偏头、抿唇、捏袖口、
   收肩、略前倾），不要舞台式大动作、不要表情包、不要夸张鬼脸。
5. 写 `expression` 时先想清楚 **"这句话是说给谁听的"** ——
   是请求、试探、指责、让步、撒娇、拒绝、告别，还是自我保护？
   同一个听者关系决定了态度，态度决定了表情。
   听者优先取**本场次里确实在场的角色**（`characters` 字段里的人）。
6. 除非剧情明确要求，**不要随便加眼泪**。
7. 情绪已经有 `dialogue.emotion` 时，`expression` 要和它一致，
   但必须写成**看得见的行为**，不能把 emotion 那个词再抄一遍。

反面例子（严禁）：
  "expression": "很生气"
正面例子：
  "expression": "先抿住唇、目光压低不看对方，随后眼睑收紧、下颌线绷住，说出口时视线才抬起来"
═══════════════════════════════════════════════════════════════
画面部分
═══════════════════════════════════════════════════════════════
第 1 层（整体概述）：一段话描述本段故事 + 摄影风格 + 镜头语言 + 情绪 + 时长
第 2 层（分时段时间线）：按秒拆分动作、表情、镜头、道具、**台词**
第 3 层（约束条件）：不要出现/不要发生/必须保持/必须出现/必须发生

`layer1_overview` 是给**视频模型**看的，要写成**具体的画面描述**（谁、在哪、做什么、
镜头怎么运动、什么光线），不要写成剧情梗概 —— 模型看不懂"展现了他的悔恨"，
但看得懂"他低头看着自己流血的手，镜头缓慢推近"。

输出严格 JSON（不要 markdown 代码块包裹）：
{
  "layer1_overview": "string",
  "speakers": ["角色名", ...],
  "narration": "（仅保留原场次明确要求的旁白，否则空字符串）",
  "layer2_timeline": [
    {"start": 0, "end": 5, "action": "...", "expression": "看得见的微表情（含眼神走向，不是情绪标签）", "camera": "...",
     "props_used": [...],
     "intent": "action|reaction|burst|golden|ambient",
     "dialogue": {"character": "角色名", "text": "真正要说的话", "emotion": "坚定"}}
  ],
  "layer3_constraints": {
    "must_not_appear": [...],
    "must_not_happen": [...],
    "must_keep": [...],
    "must_appear": [...],
    "must_happen": [...]
  }
}
"""

SCENE_DETAIL_SYSTEM = """你是专业 AI 短片编剧，参照《AI 短片制作 Agent Skill 手册 v1.0》的【场次模板】补全单个场次的【剧本细节】。

你必须严格按以下 JSON 结构输出（不要额外解释、不要 markdown 代码块包裹）：

{
  "scene_number": <int>,
  "title": "<场次标题>",
  "location": "<具体地点>",
  "duration_seconds": <int>,
  "characters": ["<角色 A>", "<角色 B>", ...],
  "timeline": [
    {
      "start": <int, 秒>,
      "end": <int, 秒>,
      "action": "<人物在这个时段的具体动作，含位置/姿势/表情>",
      "expression": "<表情: calm / happy / angry / scared / sad / tense>",
      "camera": "<镜头: static / pan / tilt / zoom / dolly / tracking / close-up / wide>",
      "props_used": ["<道具名>"],
      "visual_focus": "<画面焦点: 人物 / 道具 / 环境 / 特写>"
    },
    ...
  ],
  "dialogues": [
    {
      "character": "<说话角色>",
      "text": "<台词原文>",
      "emotion": "<情绪>",
      "timing": "<大概发生的秒数>"
    },
    ...
  ],
  "mood": "<场次整体情绪基调: 紧张/平静/兴奋/悲伤/悬疑/温馨>",
  "lighting": "<光影: 自然光/霓虹灯/烛光/月光/路灯>",
  "key_props": ["<关键道具>"]
}

要求：
1. timeline 必须按 2-5 秒一段拆分覆盖整个 duration_seconds，每段必须有具体动作和位置
2. dialogues 如果原场次没有对白，数组可为空 []
3. 动作描写要明确【先做什么后做什么】，避免歧义（这是 Skill 手册中"能否直接改写成视频提示词"的验收标准）
4. timeline + dialogues 一起覆盖整段时间轴
5. characters 必须从原场次/项目角色库中出现，不要凭空创造
"""

ABSTRACTION_SYSTEM = """你是版权合规与创意改编专家。

把用户输入的参考（可能涉及电影/小说/IP）抽象化，生成原创设定：

1. 去掉具体角色名、品牌名、地名（用通用化名）
2. 去掉标志性颜色组合（如沙丘的橙+蓝眼）
3. 去掉标志性道具（如哈利波特的眼镜+魔杖）
4. 去掉标志性建筑/符号
5. 保留抽象元素：色调、空间尺度、光影质感、故事张力、情绪节奏
6. 生成全新的角色/场景/道具

输出严格 JSON：
{
  "title": "新标题",
  "logline": "新故事一句话",
  "removed_features": ["被去除的具体特征"],
  "preserved_features": ["保留的抽象特征"],
  "characters": [
    {
      "original": "原角色描述",
      "abstracted_name": "新名字",
      "description": "新的具体外形",
      "costume": "新的服装",
      "preserved_traits": ["保留下来的抽象特征"]
    }
  ],
  "scenes": [
    {
      "original": "原场景描述",
      "abstracted_name": "新名字",
      "description": "新场景描述",
      "preserved": ["保留的抽象元素"]
    }
  ],
  "props": [
    {"original": "...", "abstracted_name": "...", "description": "..."}
  ],
  "style_abstract": "整体抽象后的视觉风格"
}
"""


# ──────────── 导演门报告压缩（2026-10-24）────────────
#: 导演门报告压成「1~3 行」时优先取这些字段（实现可能只给其中几个）。
_DIRECTOR_GATE_KEYS = (
    "score", "pass", "ok", "hooks", "duration", "durations", "pacing",
    "golden_line", "scenes", "issues", "errors", "warnings", "report",
)


def _director_gate_lines(report: Any, max_lines: int = 3) -> List[str]:
    """把 `core.director_gate.audit_script` 的报告压成 1~3 行中文（给人看，不阻断）。

    报告结构由 director_gate 自己定（可能只有 score，也可能有 issues），
    所以这里**不假设字段都在**：认识的键压成 `键=值`，列表只取前 2 条，
    字典取前 3 个键值；都不认识就截断 repr —— 有信息比没有强。
    """
    if report is None:
        return []
    if not isinstance(report, dict):
        text = str(report).strip()
        return ["导演门：" + text[:200]] if text else []
    lines: List[str] = []
    for key in _DIRECTOR_GATE_KEYS:
        if key not in report:
            continue
        val = report.get(key)
        if val in (None, "", [], {}):
            continue
        if isinstance(val, bool) or isinstance(val, (int, float)):
            lines.append("%s=%s" % (key, val))
        elif isinstance(val, str):
            lines.append("%s=%s" % (key, val[:60]))
        elif isinstance(val, (list, tuple)):
            items = [str(x).strip() for x in val if str(x).strip()]
            if items:
                lines.append("%s(%d)：%s" % (key, len(val), "；".join(items[:2])[:80]))
        elif isinstance(val, dict):
            sub = ["%s=%s" % (k, str(v)[:40]) for k, v in list(val.items())[:3]
                   if str(v).strip()]
            if sub:
                lines.append("%s：%s" % (key, "，".join(sub)))
        if len(lines) >= max_lines:
            break
    if not lines:
        lines.append("报告：" + str(report)[:200])
    return ["导演门：" + line for line in lines[:max_lines]]


# ──────────── 高级业务方法 ────────────

def three_layer_cast(scene: Dict[str, Any], characters=None) -> List[Dict[str, Any]]:
    """Use scene membership, rather than the entire project character library."""
    from .shot_prompts import is_voice_only
    available = [c for c in (characters or []) if isinstance(c, dict)]
    if "characters" in scene:
        names = {str(c.get("name") or "") if isinstance(c, dict) else str(c)
                 for c in (scene.get("characters") or [])}
        available = [c for c in available if str(c.get("name") or "") in names]
        known = {str(c.get("name") or "") for c in available}
        available += [{"name": name} for name in names - known if name]
    return [c for c in available if not is_voice_only(c)]


def normalize_three_layer_prompt(data: Dict[str, Any], scene: Dict[str, Any],
                                 characters=None) -> Dict[str, Any]:
    """Enforce authored speech/cast policy without another API call.

    Existing model results can use the same correction. Keep original responses
    separately; policy_corrections records each deterministic correction here.
    """
    result = copy.deepcopy(data)
    duration = float(scene.get("duration_seconds") or 10)
    result["layer1_overview"] = result.get("layer1_overview") or result.get("overview", "")
    result["layer3_constraints"] = result.get("layer3_constraints") or {}
    timeline = normalize_timeline(result.get("layer2_timeline") or result.get("timeline"), duration)
    silent = str(scene.get("dialogue_policy") or "").lower() == "silent"
    authored_lines = scene.get("dialogues") or []
    authored_narration = str(scene.get("narration") or "").strip()
    allow_speech = not silent and bool(authored_lines or authored_narration)
    cast = three_layer_cast(scene, characters)
    changes = []
    if not allow_speech:
        if result.get("narration") or result.get("speakers") or any(it.get("dialogue") for it in timeline):
            changes.append("removed_unrequested_speech")
        result["narration"], result["speakers"] = "", []
        for item in timeline:
            item["dialogue"] = None
        result["dialogue_policy"] = "silent"
    else:
        result["narration"] = authored_narration
        result["speakers"] = list(dict.fromkeys(
            it["dialogue"]["character"] for it in timeline if it.get("dialogue")))
    if not cast:
        if any(it.get("performance") or it.get("expression") for it in timeline):
            changes.append("removed_character_performance_from_no_cast_scene")
        for item in timeline:
            item["expression"] = ""
            item.pop("performance", None)
        result["acting_report"] = {"skipped": True, "reason": "no_visible_cast"}
    result["layer2_timeline"] = timeline
    result["dialogue_stats"] = timeline_dialogue_stats(timeline, duration=duration)
    previous = result.get("policy_corrections") or []
    result["policy_corrections"] = list(dict.fromkeys(previous + changes))
    return result


class ScriptService:
    """剧本生成服务（基于 LLM）"""

    def __init__(self, llm: LLMClient):
        self.llm = llm

    async def generate_script(
        self,
        user_prompt: str,
        total_duration: int = 30,
        style: str = "cinematic",
        auto_split: bool = True,
        scene_count: Optional[int] = None,
        settings: Optional[dict] = None,
        intent: str = "",
    ) -> Dict[str, Any]:
        """生成完整剧本。

        ★ 2026-09-14：`scene_count` 由**上层**（`core.scriptplan`）给定，
          不再让这里"估一个"。
          为什么：用户实测「我选的 5 秒，生成剧本却给了 60 秒」——
          旧实现把"总时长 N 秒 / 各场秒数之和必须等于 N 秒"写进提示词就完了，
          **没有任何强制**，模型吐 5×12 秒我们就照收。现在场次数与每场秒数
          都由 `scriptplan` 决定后写进提示词，返回后再由 `fit_scenes` **强制**重排。
        """
        from core.scriptplan import scene_count_for, prompt_duration_contract
        from core.scriptskills import genre_of, genre_spec, intent_line, spec_brief
        from core.method_gate import pacing_for as gate_pacing_for
        _genre = genre_of(user_prompt)
        _pacing = gate_pacing_for(settings, _genre)
        _silent = str((settings or {}).get("voiceover_mode") or "").lower() == "off" or bool(
            re.search(r"无(?:台词|对白|旁白|人声)|不要(?:台词|对白|旁白|人声)|仅环境声|纯(?:产品|商品|静物)展示|no (?:dialogue|voiceover|narration|speech)",
                      user_prompt or "", re.IGNORECASE))
        if not scene_count or scene_count < 1:
            scene_count = scene_count_for(total_duration, pacing=_pacing)
        _contract = prompt_duration_contract(total_duration, scene_count, pacing=_pacing)
        num_scenes = int(_contract["scene_count"])
        _minimum = _contract["min_scene_seconds"]
        _maximum = _contract["max_scene_seconds"]
        _first_low, _first_high = _contract["first_scene_range"]
        _timing_note = (
            f"每场 {_minimum}~{_maximum} 秒，合计必须恰好 {total_duration} 秒；"
            f"首场在 {_first_low}~{_first_high} 秒范围内。"
            "前 5 秒内必须出现第一事件，事件时限不等于强制首场结束。"
            "在满足总时长、场数和边界的前提下按内容分配；"
            "时长变化是可行时的建议，不得为了不同秒数破坏这些硬约束。"
            f"可行分配示例：{_contract['example_seconds']} 秒。"
        )

        # ★ 2026-10-23：体裁分流 + 注入用户手册（提示词规范）+ 意图锚点。
        if _genre == "promo":
            _system = SCRIPT_GENERATION_SYSTEM_PROMO
        elif _genre == "drama":
            _system = SCRIPT_GENERATION_SYSTEM
        else:
            _system = SCRIPT_GENERATION_SYSTEM_NEUTRAL

        user_msg = f"""创意想法：{user_prompt}

总时长：{total_duration} 秒
风格：{style}
场次数量：**必须正好 {num_scenes} 场**
每场秒数：**按内容量给**（{_timing_note}）
"""
        # ① 意图锚点（模型必须原样复述，剧本体检会核对）
        user_msg += "\n" + intent_line(intent or user_prompt)
        # ② 用户自己的手册（提示词规范）—— 之前根本没进剧本提示词，
        #    所以"没遵照我的提示词规范"是机制性原因，不是模型不听话。
        try:
            _spec = spec_brief(settings, _genre)
            if _spec:
                user_msg += "\n" + _spec
        except Exception as _e:  # noqa: BLE001
            logger.warning("加载创作规范失败（不影响出剧本）：%s", _e)
        # ②b 内置 skill 的机器规则（罗臣臣线/波导翁线的剧本规范 + 电影风格 + 台词红线）
        #     ★ 与 ② 是同一个坑的两种形态：skill 写好了但剧本服务看不见，
        #     于是"学到了"和"用上了"是两回事。
        try:
            from core.scriptskills import builtin_spec_brief
            _bspec = builtin_spec_brief(settings, _genre,
                                        user_prompt=user_prompt,
                                        total_seconds=int(total_duration or 0))
            if _bspec:
                user_msg += "\n" + _bspec
        except Exception as _e:  # noqa: BLE001
            logger.warning("加载内置 skill 规范失败（不影响出剧本）：%s", _e)
        # ③ 衔接要求（用户：「不理清细节做好衔接」）
        user_msg += (
            "\n【衔接硬要求】\n"
            "  · 每场都要写 `prev_link`（与上一场怎么接：时间/地点/人物状态/因果）"
            "和 `exit_state`（这一场结束时人物在哪、在做什么、什么情绪）；\n"
            "  · 相邻两场必须接得上：人物**不能**在两场之间换位置（不许闪身）、"
            "道具不能凭空出现或消失、时间不能倒流；\n"
            "  · 场次顺序就是故事顺序，别把「后面发生的」写在「前面发生的」之前。\n"
        )
        # ③b 台词硬标准（2026-10-24 台词门同口径：写完自查，不达标就改）
        user_msg += (
            "\n【台词硬标准（写完自查，不达标就改）】\n"
            "  · 每句 6~18 字；超过 18 字必须在标点处拆成两句，拆开的地方就是气口；\n"
            "  · 口播按 3.5~5 字/秒（估算用 4 字/秒）：超过 5 字/秒是「赶」，低于 3.5 是「拖」；\n"
            "  · 每句台词都要写 tone 字段（低声 / 打断 / 笑着说 / 压着说 / 脱口而出…），"
            "先决定「怎么说」再写「说什么」；\n"
            "  · 金句单独成句，后面留 0.5 秒停顿，不要紧跟着下一句。\n"
        )
        # ③c 导演硬标准（2026-10-24 导演门同口径：钩子/场次存在性/时长守恒/每场 pacing）
        #    ★ 和 ③b 台词门同一个道理：**写完自查**，不达标就改，别等分镜/成片再修。
        #    只注可执行数字（总量 ≤2KB），不把方法论整本塞进来稀释已有硬规则。
        user_msg += (
            "\n【导演硬标准（写完自查，不达标就改）】\n"
            "  · 钩子：00:00-00:05 必须出现第一事件（动作或台词）；首场长度遵守上方可行区间，"
            "禁止把钩子放到 13 秒后；\n"
            "  · 场次存在性：在规划阶段合并或改写不推进内容的场次；输出场数仍须遵守上方约定。"
            "只服务节奏的垫场在 beat 里标「垫场」；\n"
            "  · 时长守恒：采用 exact 策略，合计必须等于目标，不允许缩短或超出。"
            "可行时钩子短、高潮/金句长；少于三场或预算受限时不强求三种秒数。"
            "用动作和信息推进填满必要时长，禁止拉长无事发生的空镜凑数；\n"
            "  · 每场必须写 `pacing` 对象：plot(松/中/紧)、emotion(轻/中/重)、"
            "shot_density(长镜头/快切/混合)、size_focus(远全景/中景/特写/混合)；"
            "有金句就写 `golden_line`（原文），没有就留空字符串。\n"
        )
        if auto_split:
            user_msg += (f"\n请自动拆分场次，每个场次标注秒数，"
                         f"各场秒数之和必须等于 {total_duration} 秒；"
                         f"场次数只能是 {num_scenes} 场，多写或少写都会被下游裁掉。")

        # ★★ 2026-09-29：**剧本锁**（用户要求「按剧本来，不要灵活度」）。
        #
        #   实测：不加这段时，模型把定稿剧本整个改写 —— 主角「陈砚」变成
        #   「阿诚」、老周从受害者翻转为「三年前吞他家产的冷血老周」、
        #   核心事件（识破秤钩断口 / 钱袋左右 / 案下暗线）全部丢失。
        #   根因是 SCRIPT_GENERATION_SYSTEM 要求「输出一个有冲突的完整故事」，
        #   模型在「创意想法」信息不足时**自行补全**。
        #   这里把用户原文框成不可改动区，并只授权「技术细节」层面的创作。
        if _genre == "promo":
            # 宣传片：锁"意图"而不是锁"剧本"（用户给的是想法，不是定稿剧本）
            user_msg += (
                "\n\n═══════════════════════════════════════════\n"
                "【最高优先级·意图锁】上面的「创意想法」是**用户唯一要的东西**。\n"
                "  · 你只能做：把意图拆成场次、补写技术性细节（景别/机位/字幕）、"
                "给每场写清楚衔接；\n"
                "  · 你**不能**做：改用户的产品名/数字/承诺；为了让片子「好看」而新增"
                "反派、前史、恩怨、群像、结局反转；把痛点写成别人的错。\n"
                "  · 如果你认为意图里某处不合理，**照原样写**，并在 JSON 之外用一行"
                "「备注：」说明你的疑问 —— 不要自行改动。\n"
                "═══════════════════════════════════════════\n"
            )
        else:
            user_msg += (
            "\n\n═══════════════════════════════════════════\n"
            "【最高优先级·剧本锁】上面的「创意想法」是**已经定稿的剧本**，"
            "不是灵感草稿。你**只能**做这些事：\n"
            "  1. 把剧本里已有的场次、人物、动作、对白、道具、因果关系"
            "**原样落实到分镜格式**；\n"
            "  2. 补写**技术性细节**（景别、机位、时长切分、字幕点题）；\n"
            "  3. 剧本里的对白可以润色措辞，但**不得改变台词含义与说话人**。\n"
            "绝对禁止：\n"
            "  · 改人物名（剧本里叫什么就叫什么）\n"
            "  · 改人物关系、身份、立场\n"
            "  · 新增剧本里没有的人物、地名、道具、组织\n"
            "  · 改动剧本给定的因果与结局\n"
            "  · 替换剧本的核心事件\n"
            "如果你认为剧本某处不合理，**照原样写**，并在 JSON 之外"
            "用一行「备注：」说明你的疑问——**不要自行改动**。\n"
            "═══════════════════════════════════════════\n"
            )

        user_msg += ("\n【本次执行约束优先于资料中的通用范例】\n"
                     + _timing_note
                     + ("\n本次明确不生成台词、对白或旁白，dialogues 必须为空数组；不得补写人声。" if _silent else ""))
        text = await self.llm.chat(
            messages=[
                {"role": "system",
                 "content": _system + (SCRIPT_LOCK_SYSTEM if _genre != "promo" else "")},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.8,
            max_tokens=_MAX_TOKENS_FOR_THINKING_MODELS,
            reasoning_effort="low",
        )

        # 解析 JSON（多级容错）；失败则用强化提示重试一次
        try:
            result = extract_json(text)
            if isinstance(result, dict):
                # ★ 2026-10-20：**台词强制**（见 _ensure_dialogues 的说明）
                return await self._method_gates(await self._ensure_dialogues(result, total_duration, allow_silent=_silent), _genre,
                                                settings=settings, pacing=_pacing, total=total_duration,
                                                duration_contract=_contract)
            raise ValueError(f"返回不是 JSON 对象：{type(result).__name__}")
        except Exception as first_err:
            logger.warning("剧本 JSON 解析失败，尝试重试：%s", first_err)
            retry_text = await self.llm.chat(
                messages=[
                    {"role": "system",
                     "content": _system + (SCRIPT_LOCK_SYSTEM if _genre != "promo" else "")},
                    {"role": "user", "content": user_msg},
                    {"role": "assistant", "content": str(text)[:1500]},
                    {"role": "user", "content":
                        "上面的输出无法被程序解析。请【只输出一个合法的 JSON 对象】，"
                        "不要任何解释、不要 markdown 代码块标记、不要注释。"
                        "所有字符串用双引号，不要在最后一个元素后加逗号。"
                        "必须包含字段：title, logline, style, scenes（数组，每项含 "
                        "scene_number, title, location, duration_seconds, characters, actions, dialogues, mood）。"},
                ],
                temperature=0.4,
                max_tokens=_MAX_TOKENS_FOR_THINKING_MODELS,
                # ★ 2026-09-29：这条重试路径原先**既没带剧本锁也没带 effort**，
                #   实测就是它把正文吃光（第一次解析失败 → 走这里 → 思考用满
                #   16000 → 「empty text content after think-block cleanup」）。
                reasoning_effort="low",
            )
            try:
                result = extract_json(retry_text)
                if isinstance(result, dict):
                    logger.info("剧本 JSON 重试解析成功")
                    return await self._method_gates(await self._ensure_dialogues(result, total_duration, allow_silent=_silent), _genre,
                                                    settings=settings, pacing=_pacing, total=total_duration,
                                                    duration_contract=_contract)
                raise ValueError("重试仍非 JSON 对象")
            except Exception as second_err:
                logger.error("剧本 JSON 重试仍失败：%s", second_err)
                # 不再返回"解析失败"这种内部错误标题，改为可读的降级结果
                return {
                    "title": "未命名剧本（AI 输出格式异常）",
                    "logline": user_prompt[:80],
                    "style": style,
                    "scenes": [],
                    "characters": [],
                    "_parse_error": str(second_err),
                    "_raw": str(retry_text or text)[:4000],
                }

    async def _method_gates(self, result: Dict[str, Any], genre: str = "drama", *,
                            settings: Optional[dict] = None, pacing: Optional[str] = None,
                            total: Optional[float] = None,
                            duration_contract: Optional[dict] = None) \
            -> Dict[str, Any]:
        """★ 2026-10-24：剧本落库前的**台词门**（方法论执行层）。

        用户反馈第 4 条「台词 AI 味重」。尺子来自 dialogue-doctor-7d：
        单句 6~18 字、口播 3.5~5 字/秒、金句留 0.5s 气口、每句必须标语气位。
        这里在**写完之后、落库之前**执行（而不是等配音/剪辑阶段再修）——
        这就是「把问题提前锁死」：句子太长、没气口、没决定怎么说，都在这一层暴露。

        失败一律不影响出剧本（只记 warning），门可以在设置里关掉。
        """
        try:
            from core import method_gate as _mg
            _new, _rep = _mg.repair_script(result, genre=genre, settings=settings)
            if isinstance(_new, dict):
                result = _new
            if _rep and not _rep.get("error"):
                result["_method_gate_report"] = {"reports": {"dialogue": _rep}}
                result["_method_gates"] = _mg.summarize({"dialogue": _rep})
                _st = _rep.get("stats") or {}
                logger.info(
                    "台词门：%s 句 score=%s errors=%s warn=%s 平均 %.1f 字 / %.2f 字每秒",
                    _st.get("lines"), _rep.get("score"), _rep.get("errors"),
                    _rep.get("warnings"), float(_st.get("chars_avg") or 0),
                    float(_st.get("cps_avg") or 0))
            elif _rep:
                logger.warning("台词门失败（保留原剧本）：%s", _rep.get("error"))
        except Exception as e:  # noqa: BLE001
            logger.warning("台词门异常（保留原剧本）：%s", e)
        # ★ 2026-10-24（导演门）：台词门之后再过一道**导演门**（钩子 0-5s / 场次存在性 /
        #   时长守恒 / 每场 pacing / golden_line）。与台词门同口径：
        #   lazy import（core.director_gate 还没落地就跳过）、失败一律不影响出剧本、
        #   报告压成 1~3 行追加到 `_method_gates`（下游 warnings 会显示给用户）。
        try:
            from core.method_gate import audit_final_script
            if duration_contract is not None:
                result["duration_contract"] = duration_contract
            result = audit_final_script(result, settings=settings, genre=genre,
                                        pacing=pacing, total=total, phase="generated")
            _dr = result.get("director") or {}
            _lines = _director_gate_lines(_dr)
            if _lines:
                logger.info("导演门：%s", " | ".join(_lines))
        except ImportError:
            logger.debug("导演门未安装（core.director_gate 不存在），跳过")
        except Exception as e:  # noqa: BLE001
            logger.warning("导演门异常（保留原剧本）：%s", e)
        return result

    async def _ensure_dialogues(self, result: Dict[str, Any],
                                 total_duration: int = 30, *,
                                 allow_silent: bool = False) -> Dict[str, Any]:
        """**保证每个场次都有台词**。

        ★★ 2026-10-20：实测病根。
          系统提示词的 JSON Schema 里写着 `dialogues`，规则第 2 条也写着"每场三件套含对白"
          —— 但真实生成的 2 个场次 `dialogues` **全是 []**。
          后果是整条下游链断掉：
            分镜 layer2_timeline[].dialogue = null
            → `dialogue.timeline_lines()` 取不到任何一句
            → 配音一步都走不到（"0 个镜头已配好"）
            → 成片 volumedetect −91 dB（纯静音），或被迫走旁白兜底。
          用户要的是"角色真的说话"，所以这里做三层兜底：
            ① 有台词就原样通过；
            ② 全场空 → 让模型**只补台词**（一次便宜调用，走 reasoning_effort=low）；
            ③ 还空 → 用「旁白」+ 该场标题/动作拼一句，保证成片一定有人声。
        """
        if allow_silent:
            result["dialogue_policy"] = "silent"
            corrected = []
            for index, scene in enumerate(result.get("scenes") or []):
                if isinstance(scene, dict) and scene.get("dialogues"):
                    corrected.append(scene.get("scene_number", index + 1))
                    scene["dialogues"] = []
            if corrected:
                result["dialogue_policy_corrections"] = {
                    "removed_unrequested_dialogue_from_scenes": corrected}
            return result
        scenes = [s for s in (result.get("scenes") or []) if isinstance(s, dict)]
        if not scenes:
            return result

        def _has_dlg(s: Dict[str, Any]) -> bool:
            for k in ("dialogues", "dialogue"):
                v = s.get(k)
                if isinstance(v, list) and any(x for x in v):
                    return True
                if isinstance(v, str) and v.strip():
                    return True
            return False

        # ★ 2026-10-20：**逐场**判断，不是"有一场有就都算有"。
        #   实测踩到：第 1 场 dialogues=[]、第 2 场有台词 → 整体判定"有台词"直接返回，
        #   随后 fit_scenes 按时长把**有台词的**第 2 场裁掉 → 存下来的剧本零台词，
        #   下游配音全空。所以缺一场补一场。
        _missing = [s for s in scenes if not _has_dlg(s)]
        if not _missing:
            return result

        # ① 让模型只补缺的那些场
        try:
            brief = json.dumps(
                [{"scene_number": s.get("scene_number"), "title": s.get("title"),
                  "duration_seconds": s.get("duration_seconds"),
                  "characters": s.get("characters"), "actions": s.get("actions")}
                 for s in _missing], ensure_ascii=False)
            _txt = await self.llm.chat(
                messages=[
                    {"role": "system",
                     "content": "你是短片编剧。只输出一个 JSON 对象，不要任何解释。"},
                    {"role": "user", "content": (
                        "下面这些场次**全都缺对白**，成片需要观众听到人物说话。\n"
                        "请为每一场补 1-2 句**口语短台词**（中文约 4 字/秒，"
                        "不要超过该场秒数），说话人必须出自该场的 characters。\n"
                        "只输出 JSON：{\"scenes\":[{\"scene_number\":1,"
                        "\"dialogues\":[{\"character\":\"男人\",\"text\":\"…\","
                        "\"emotion\":\"calm\"}]}]}\n"
                        f"场次数据：{brief}")},
                ],
                temperature=0.5, max_tokens=1200, reasoning_effort="low")
            fix = extract_json(_txt)
            by_num: Dict[Any, Any] = {}
            for x in ((fix or {}).get("scenes") or []):
                if isinstance(x, dict) and x.get("scene_number") is not None:
                    try:
                        by_num[int(x["scene_number"])] = x
                    except Exception:
                        continue
            filled = 0
            for s in _missing:
                try:
                    n = int(s.get("scene_number"))
                except Exception:
                    continue
                cand = (by_num.get(n) or {})
                dlg = cand.get("dialogues")
                if isinstance(dlg, list) and dlg:
                    s["dialogues"] = dlg
                    filled += 1
            if filled:
                logger.info("剧本台词补齐：%d/%d 场", filled, len(scenes))
                result["_dialogue_repair"] = f"模型初稿无台词，已补写 {filled} 场"
        except Exception as e:
            logger.warning("台词补写调用失败：%s", e)

        # ② 仍空 → 旁白兜底（保证一定有人声）
        _still = [s for s in scenes if not _has_dlg(s)]
        if _still:
            for s in _still:
                parts = [str(s.get("title") or "").strip()]
                acts = s.get("actions")
                if isinstance(acts, list):
                    parts += [str(a).strip() for a in acts[:2] if str(a).strip()]
                else:
                    parts.append(str(acts or "").strip())
                line = "，".join(p for p in parts if p).strip("，。 ")
                per = float(s.get("duration_seconds") or 0) or 5.0
                max_chars = max(8, int(per * 4))
                if line:
                    s["dialogues"] = [{"character": "旁白",
                                       "text": line[:max_chars], "emotion": "calm"}]
            result["_dialogue_repair"] = f"有 {len(_still)} 场仍无台词，已用旁白兜底"
            logger.info("剧本仍有 %d 场无台词，已旁白兜底", len(_still))
        return result

    async def generate_three_layer_prompt(
        self,
        scene_description: Dict[str, Any],
        characters: List[Dict[str, Any]] = None,
        scene_context: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """为单个场次生成三层提示词"""
        dur = int(scene_description.get("duration_seconds") or 10)
        ctx_parts = [f"场次：{scene_description.get('title', '')}"]
        ctx_parts.append(f"地点：{scene_description.get('location', '')}")
        ctx_parts.append(f"时长：{dur} 秒")
        ctx_parts.append(f"动作：{scene_description.get('actions', [])}")
        ctx_parts.append(f"对白：{scene_description.get('dialogues', [])}")
        ctx_parts.append(f"情绪：{scene_description.get('mood', 'neutral')}")
        visible_cast = three_layer_cast(scene_description, characters)
        if not visible_cast:
            ctx_parts.append("本场无出镜人物，expression 必须为空；不要添加人体表演。")
        if not scene_description.get("dialogues") and not scene_description.get("narration"):
            ctx_parts.append("本场未要求任何人声，speakers=[]、narration=''、dialogue=null。")
        # ★ 明确告知"每个时间片能装多少字"，模型才不会写出 5 秒念不完的长台词
        ctx_parts.append(
            f"\n台词字数硬约束：本场共 {dur} 秒，按中文 4 字/秒 计算，"
            f"**全场台词总字数不要超过 {max(4, dur * 4)} 个字**。"
            f"单个时间片的台词不超过「该片秒数 × 4」个字。")

        if visible_cast:
            ctx_parts.append(f"\n角色：")
            for c in visible_cast:
                ctx_parts.append(f"- {c.get('name', '')}: {c.get('description', '')} ({c.get('costume', '')})")

        if scene_context:
            ctx_parts.append(f"\n场景：{scene_context.get('description', '')}")

        text = await self.llm.chat(
            messages=[
                {"role": "system", "content": THREE_LAYER_PROMPT_SYSTEM},
                {"role": "user", "content": "\n".join(ctx_parts)},
            ],
            temperature=0.6,
            # ★ 2026-09-29：这里原来是 **3000**，而我先前只批量替换了
            #   `max_tokens=4000,` 的四处，把这一处漏了。
            #   代价是实打实的：main.py:712 在剧本生成之后**还会调**本函数
            #   （generate_three_layer_prompt，三层提示词），它一失败整个
            #   /script/generate 就 500，错误信息是
            #   「[minimax] returned empty text content after think-block cleanup」
            #   —— 看起来像剧本生成失败，其实剧本早就生成成功了，卡在下一步。
            #   3000 预算对 M3 这类强制开思考的模型不够（实测 4000 就已经
            #   reasoning 吃掉 3999、正文只剩 1 个 token）。
            max_tokens=_MAX_TOKENS_FOR_THINKING_MODELS,
            reasoning_effort="low",
        )

        try:
            data = extract_json(text)
            if not isinstance(data, dict):
                raise ValueError("not dict")
            data.setdefault("layer1_overview", data.get("layer1_overview") or data.get("overview", ""))
            data.setdefault("layer2_timeline", data.get("layer2_timeline") or data.get("timeline", []))
            data.setdefault("layer3_constraints", data.get("layer3_constraints") or {})
            # 台词规范化：把 LLM 各种可能的写法统一成 {character,text,emotion}，
            # 并把"用描述代替台词"的坏输出识别出来（下游要靠它配音+配字幕）。
            # ⚠️ 这几行**单独 try**：它们出错不应该把已经解析好的 JSON 一起丢掉
            #    （历史上就是这里 NameError，导致整份解析结果被"文本降级"覆盖）。
            try:
                data["layer2_timeline"] = normalize_timeline(
                    data.get("layer2_timeline"), duration=dur)
                data["speakers"] = data.get("speakers") or [
                    d["character"] for d in
                    (it.get("dialogue") for it in data["layer2_timeline"]
                     if isinstance(it, dict)) if d]
                data["dialogue_stats"] = timeline_dialogue_stats(
                    data["layer2_timeline"], duration=dur)
            except Exception as e:
                logger.warning("台词规范化失败（保留已解析内容）：%s", e)
            data = normalize_three_layer_prompt(data, scene_description, characters)
            if not visible_cast:
                return data
            # ── 微表情富化（biaoqing-yanyi skill）────────────────────────
            # 在规范化**之后**做：这样 `performance` 字段是最后一道加法，
            # 而不是先加再被 normalize 的白名单重建吞掉。
            # （`dialogue._PASSTHROUGH_KEYS` 也做了透传，两道保险。）
            #
            # 不阻断：富化失败时保留规范化结果，只记日志 ——
            # 一个增强不该让整个分镜生成失败。
            try:
                from .acting import enrich_timeline as _enrich_acting
                _en = _enrich_acting(
                    data["layer2_timeline"],
                    shot={"character_ids": [], "aspect_ratio": "16:9"},
                    scene={"mood": scene_description.get("mood") or "",
                           "characters": [c.get("name") for c in visible_cast
                                          if isinstance(c, dict)]},
                    characters=visible_cast,
                    script_scenes=[],
                    aspect_ratio="16:9",
                )
                data["layer2_timeline"] = _en["timeline"]
                data["acting_report"] = _en.get("report") or {}
            except Exception as e:
                logger.warning("微表情富化失败（保留已生成的时间线，但没有表演结构）：%s", e)
                data["acting_report"] = {"error": "%s: %s" % (type(e).__name__, e)}
            return data
        except Exception as e:
            # ★ 兜底**绝不能**把原始 JSON 文本塞进 layer1_overview。
            #   旧兜底干的就是这件事，结果提示词变成一段 ```json{...}```，
            #   视频模型完全看不懂 —— 这就是"画面与描述不相干"的根因之一。
            #   现在：先尝试从文本里捞出可读的叙述，实在不行宁可留空并明确报错。
            logger.warning("三层提示词解析失败：%s", e)
            prose = _salvage_prose(text)
            return {
                "layer1_overview": prose,
                "layer2_timeline": [],
                "layer3_constraints": {},
                "_parse_failed": True,
                "_parse_error": f"{type(e).__name__}: {e}",
                "_raw_len": len(text or ""),
            }


    # ★★ 这里**不能**插入模块级函数（2026-09-26 修正）
    #
    # 曾经在这里插了一个 `def _salvage_prose(...)`（写在第 0 列），
    # 于是 `class ScriptService` 的类体**在这一行就结束了** ——
    # 而紧跟其后的 `async def generate_scene_detail(self, ...)` 缩进 4 格，
    # 变成了**嵌在 `_salvage_prose` 里的局部函数**：
    #
    #   · 语法**完全合法**（`return` 之后再 `def` 只是"不可达"，Python 不报错）
    #   · 所以 `import core.llm` 正常、应用能启动、测试全绿
    #   · 但 `ScriptService.generate_scene_detail` **根本不存在**，
    #     调用它只会 `AttributeError`
    #
    # 后果正好打在**本目标的输入链**上：**场次细纲**（timeline / dialogues /
    # mood / lighting / key_props）是 casting 与 acting 两条链共同的上游 ——
    # 没有它，分镜只能回落到 `three_layer_prompts`，
    # 「场次 / 说给谁听 / 时代地域」这几项契合度都会变薄。
    #
    # 现在 `_salvage_prose` 挪到了**文件末尾**（模块级，与类无关）。
    # 守卫：`tests/test_module_shape.py` 用 AST 扫"嵌在函数里、且首参叫 `self`
    # 的函数定义" —— 那正是这个 bug 的形状（一个方法被留在了别人肚子里）。
    async def generate_scene_detail(
        self,
        scene: Dict[str, Any],
        characters: List[Dict[str, Any]] = None,
        scene_context: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """
        按 Skill 手册 v1.0 的【场次模板】为单个场次生成【剧本细节】。
        返回字段：scene_number/title/location/duration_seconds/characters/timeline(按秒)/
                  dialogues/mood/lighting/key_props。
        """
        ctx_parts = [
            "【场次基本信息】",
            f"场次标题：{scene.get('title', '')}",
            f"场次编号：{scene.get('scene_number', 1)}",
            f"时长：{scene.get('duration_seconds', 10)} 秒",
            f"地点：{scene.get('location', '')}",
            f"现有动作描述：{scene.get('actions', [])}",
            f"现有对白：{scene.get('dialogues', [])}",
            f"情绪基调：{scene.get('mood', 'neutral')}",
        ]
        if characters:
            ctx_parts.append("\n【角色】")
            for c in characters:
                ctx_parts.append(
                    f"- {c.get('name', '')}: {c.get('description', '')} "
                    f"(服装: {c.get('costume_main', '')})"
                )
        if scene_context:
            ctx_parts.append(f"\n【场景信息】{scene_context.get('description', '')}")

        text = await self.llm.chat(
            messages=[
                {"role": "system", "content": SCENE_DETAIL_SYSTEM},
                {"role": "user", "content": "\n".join(ctx_parts)},
            ],
            temperature=0.7,
            max_tokens=_MAX_TOKENS_FOR_THINKING_MODELS,
        )

        try:
            data = extract_json(text)
            if not isinstance(data, dict):
                raise ValueError("not dict")
            data.setdefault("scene_number", scene.get("scene_number", 1))
            data.setdefault("title", scene.get("title", ""))
            data.setdefault("location", scene.get("location", ""))
            data.setdefault("duration_seconds", scene.get("duration_seconds", 10))
            data.setdefault("characters", [])
            data.setdefault("timeline", [])
            data.setdefault("dialogues", [])
            data.setdefault("mood", scene.get("mood", "neutral"))
            data.setdefault("lighting", "")
            data.setdefault("key_props", [])
            return data
        except Exception as e:
            logger.warning("场次细节解析失败：%s", e)
            return {
                "scene_number": scene.get("scene_number", 1),
                "title": scene.get("title", ""),
                "location": scene.get("location", ""),
                "duration_seconds": scene.get("duration_seconds", 10),
                "characters": [],
                "timeline": [],
                "dialogues": [],
                "mood": scene.get("mood", "neutral"),
                "lighting": "",
                "key_props": [],
                "raw_text": text,
                "parse_error": True,
                "parse_error_detail": str(e),
            }


class AbstractionService:
    """版权抽象化服务"""

    def __init__(self, llm: LLMClient):
        self.llm = llm

    async def abstract(self, source_description: str) -> Dict[str, Any]:
        """把可能涉及版权的描述抽象化"""
        text = await self.llm.chat(
            messages=[
                {"role": "system", "content": ABSTRACTION_SYSTEM},
                {"role": "user", "content": f"原始描述：\n{source_description}"},
            ],
            temperature=0.7,
            max_tokens=_MAX_TOKENS_FOR_THINKING_MODELS,
        )

        try:
            data = extract_json(text)
            if isinstance(data, dict):
                return data
            raise ValueError("not dict")
        except Exception as e:
            logger.warning("抽象化结果解析失败：%s", e)
            return {
                "_parse_error": True,
                "_raw": text,
                "title": "版权抽象化（AI 输出格式异常）",
                "removed_features": [],
                "preserved_features": [],
                "characters": [],
                "scenes": [],
            }


# ──────────── 模块级工具（★ 必须放在**所有 class 之后**）────────────
#
# 2026-09-26：`_salvage_prose` 原来被插在 `class ScriptService` 的类体中间，
# 那一下把类截断了，`generate_scene_detail` 因此变成死函数（详见那里的长注释）。
# 放在文件末尾是**有意的**：模块级函数夹在类体里，是一个不会报错的严重错误。
#


def _salvage_prose(text: str, limit: int = 400) -> str:
    """解析失败时，从原始输出里捞出**人能读的一段话**当 layer1。

    绝不要把 JSON 原文塞进 layer1 —— 那是给视频模型看的提示词。
    """
    s = str(text or "")
    # 去掉 markdown 围栏
    s = re.sub(r"^\s*```[a-zA-Z]*\s*", "", s)
    s = re.sub(r"```\s*$", "", s).strip()
    # 优先从 JSON 里抓 layer1_overview 的值
    m = re.search(r'"layer1_overview"\s*:\s*"((?:[^"\\]|\\.)*)"', s, re.S)
    if m:
        try:
            return json.loads('"' + m.group(1) + '"')[:limit]
        except Exception:
            return m.group(1)[:limit]
    # 退而求其次：如果整段看着还是 JSON，就别硬塞
    head = s.lstrip()[:1]
    if head in ("{", "["):
        return ""
    return s[:limit]
