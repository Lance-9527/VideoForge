"""
VideoForge · 通用 LLM 对话客户端（v2）

支持：
- DeepSeek（默认推荐，性价比高）
- 通义千问 Qwen（阿里 DashScope）
- 智谱 GLM / Kimi（月之暗面 Moonshot）
- Ollama 本地（qwen2.5/llama3 等）
- OpenAI 兼容 API（任何兼容 OpenAI 协议的服务）
- MiniMax（已通过 Ollama 走）

每个模型对应一个 Provider 类，统一接口：
  - chat(messages) → str
  - stream_chat(messages) → iterator of str chunks
"""

import asyncio
import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, AsyncIterator
import httpx


logger = logging.getLogger("videoforge.chat")


@dataclass
class Message:
    role: str        # "system" | "user" | "assistant"
    content: str


@dataclass
class ChatRequest:
    messages: List[Message]
    temperature: float = 0.7
    max_tokens: int = 4000
    stream: bool = False


@dataclass
class ChatResponse:
    content: str
    usage: Dict[str, int] = field(default_factory=dict)
    model: str = ""
    raw: Any = None


def _extract_delta(chunk: Dict[str, Any]) -> str:
    """从流式 chunk 中稳健提取增量文本。

    不同厂商格式不同：
    - OpenAI/DeepSeek/Qwen/GLM/Kimi/豆包/混元：choices[0].delta.content (dict)
    - MiniMax v2 流式：choices[0].delta 直接是 string！
    - Ollama：message.content
    这里统一兼容。
    """
    try:
        # Ollama：top-level message.content
        top_msg = chunk.get("message")
        if isinstance(top_msg, dict):
            c = top_msg.get("content")
            if isinstance(c, str):
                return c
        choices = chunk.get("choices") or [{}]
        c0 = choices[0] if isinstance(choices, list) and choices else {}
        if not isinstance(c0, dict):
            return ""
        delta = c0.get("delta")
        if isinstance(delta, str):
            return delta
        if isinstance(delta, dict):
            return delta.get("content", "") or ""
        msg = c0.get("message")
        if isinstance(msg, dict):
            return msg.get("content", "") or ""
        return ""
    except Exception:
        return ""


# ──────────── 抽象基类 ────────────

class LLMProvider(ABC):
    """所有 LLM provider 的基类"""

    name: str = "base"
    display_name: str = "Base"
    description: str = ""
    default_model: str = ""
    available_models: List[str] = []
    is_local: bool = False
    requires_api_key: bool = True
    base_url: str = ""

    def __init__(self, api_key: str = "", model: str = "", config: Dict[str, Any] = None):
        self.api_key = api_key
        self.model = model or self.default_model
        self.config = config or {}

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json", "User-Agent": "VideoForge/1.0"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    @abstractmethod
    async def chat(self, req: ChatRequest) -> ChatResponse:
        """同步调用，返回完整结果"""
        raise NotImplementedError

    @abstractmethod
    async def stream_chat(self, req: ChatRequest) -> AsyncIterator[str]:
        """流式调用，逐 chunk yield"""
        raise NotImplementedError
        yield ""  # 让这个方法变成生成器


# ──────────── DeepSeek ────────────

class DeepSeekProvider(LLMProvider):
    name = "deepseek"
    display_name = "DeepSeek（推荐）"
    description = "国产，性价比极高，中文好，长上下文（64K）"
    default_model = "deepseek-chat"
    available_models = ["deepseek-chat", "deepseek-reasoner", "deepseek-coder"]
    base_url = "https://api.deepseek.com"

    async def chat(self, req: ChatRequest) -> ChatResponse:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=120, write=30, pool=10)) as client:
            r = await client.post(
                f"{self.base_url}/v1/chat/completions",
                headers=self._headers(),
                json={
                    "model": self.model,
                    "messages": [{"role": m.role, "content": m.content} for m in req.messages],
                    "temperature": req.temperature,
                    "max_tokens": req.max_tokens,
                    "stream": False,
                },
            )
            r.raise_for_status()
            data = r.json()
            return ChatResponse(
                content=data["choices"][0]["message"]["content"],
                usage=data.get("usage", {}),
                model=data.get("model", self.model),
                raw=data,
            )

    async def stream_chat(self, req: ChatRequest) -> AsyncIterator[str]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=300, write=30, pool=10)) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/v1/chat/completions",
                headers=self._headers(),
                json={
                    "model": self.model,
                    "messages": [{"role": m.role, "content": m.content} for m in req.messages],
                    "temperature": req.temperature,
                    "max_tokens": req.max_tokens,
                    "stream": True,
                },
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    if payload.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                        delta = _extract_delta(chunk)
                        if delta:
                            yield delta
                    except (json.JSONDecodeError, KeyError, IndexError, AttributeError, TypeError):
                        continue


# ──────────── Qwen（通义千问 / DashScope）────────────

class QwenProvider(LLMProvider):
    name = "qwen"
    display_name = "通义千问 Qwen"
    description = "阿里 DashScope，国产老牌，多尺寸模型可选"
    default_model = "qwen-plus"
    available_models = [
        "qwen-max", "qwen-plus", "qwen-turbo",
        "qwen-long",          # 1M 上下文
        "qwen-coder-plus",
        "qwen-vl-max",        # 多模态
    ]
    base_url = "https://dashscope.aliyuncs.com/api/v1"

    def _headers(self) -> Dict[str, str]:
        h = super()._headers()
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def chat(self, req: ChatRequest) -> ChatResponse:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=120, write=30, pool=10)) as client:
            r = await client.post(
                f"{self.base_url}/services/aigc/text-generation/generation",
                headers=self._headers(),
                json={
                    "model": self.model,
                    "input": {"messages": [{"role": m.role, "content": m.content} for m in req.messages]},
                    "parameters": {
                        "temperature": req.temperature,
                        "max_tokens": req.max_tokens,
                        "result_format": "message",
                    },
                },
            )
            r.raise_for_status()
            data = r.json()
            return ChatResponse(
                content=data["output"]["choices"][0]["message"]["content"],
                usage=data.get("usage", {}),
                model=self.model,
                raw=data,
            )

    async def stream_chat(self, req: ChatRequest) -> AsyncIterator[str]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=300, write=30, pool=10)) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/services/aigc/text-generation/generation",
                headers=self._headers(),
                json={
                    "model": self.model,
                    "input": {"messages": [{"role": m.role, "content": m.content} for m in req.messages]},
                    "parameters": {
                        "temperature": req.temperature,
                        "max_tokens": req.max_tokens,
                        "result_format": "message",
                        "incremental_output": True,
                    },
                },
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:]
                    if payload.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                        delta = chunk.get("output", {}).get("choices", [{}])[0].get("message", {}).get("content", "")
                        if delta:
                            yield delta
                    except (json.JSONDecodeError, KeyError, IndexError, AttributeError, TypeError):
                        continue


# ──────────── 智谱 GLM ────────────

class GLMProvider(LLMProvider):
    name = "glm"
    display_name = "智谱 GLM"
    description = "智谱 AI，国产老牌，工具调用能力强"
    default_model = "glm-4-plus"
    available_models = [
        "glm-4-plus", "glm-4-air", "glm-4-flash",
        "glm-4v-plus",       # 多模态
        "glm-z1-air",         # 推理模型
    ]
    base_url = "https://open.bigmodel.cn/api/paas/v4"

    async def chat(self, req: ChatRequest) -> ChatResponse:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=120, write=30, pool=10)) as client:
            r = await client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json={
                    "model": self.model,
                    "messages": [{"role": m.role, "content": m.content} for m in req.messages],
                    "temperature": req.temperature,
                    "max_tokens": req.max_tokens,
                },
            )
            r.raise_for_status()
            data = r.json()
            return ChatResponse(
                content=data["choices"][0]["message"]["content"],
                usage=data.get("usage", {}),
                model=data.get("model", self.model),
                raw=data,
            )

    async def stream_chat(self, req: ChatRequest) -> AsyncIterator[str]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=300, write=30, pool=10)) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json={
                    "model": self.model,
                    "messages": [{"role": m.role, "content": m.content} for m in req.messages],
                    "temperature": req.temperature,
                    "max_tokens": req.max_tokens,
                    "stream": True,
                },
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:]
                    if payload.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                        delta = _extract_delta(chunk)
                        if delta:
                            yield delta
                    except (json.JSONDecodeError, KeyError, IndexError, AttributeError, TypeError):
                        continue


# ──────────── Kimi（月之暗面 Moonshot）────────────

class KimiProvider(LLMProvider):
    name = "kimi"
    display_name = "Kimi（月之暗面）"
    description = "Moonshot 出品，超长上下文（128K-200K），中文顶尖"
    default_model = "moonshot-v1-128k"
    available_models = [
        "moonshot-v1-128k",   # 128K 上下文
        "moonshot-v1-32k",
        "moonshot-v1-8k",
        "moonshot-v1-auto",    # 自动选择
    ]
    base_url = "https://api.moonshot.cn/v1"

    async def chat(self, req: ChatRequest) -> ChatResponse:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=180, write=30, pool=10)) as client:
            r = await client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json={
                    "model": self.model,
                    "messages": [{"role": m.role, "content": m.content} for m in req.messages],
                    "temperature": req.temperature,
                    "max_tokens": req.max_tokens,
                },
            )
            r.raise_for_status()
            data = r.json()
            return ChatResponse(
                content=data["choices"][0]["message"]["content"],
                usage=data.get("usage", {}),
                model=data.get("model", self.model),
                raw=data,
            )

    async def stream_chat(self, req: ChatRequest) -> AsyncIterator[str]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=300, write=30, pool=10)) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json={
                    "model": self.model,
                    "messages": [{"role": m.role, "content": m.content} for m in req.messages],
                    "temperature": req.temperature,
                    "max_tokens": req.max_tokens,
                    "stream": True,
                },
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:]
                    if payload.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                        delta = _extract_delta(chunk)
                        if delta:
                            yield delta
                    except (json.JSONDecodeError, KeyError, IndexError, AttributeError, TypeError):
                        continue


# ──────────── MiniMax ────────────

class MiniMaxProvider(LLMProvider):
    name = "MiniMax"
    display_name = "MiniMax"
    description = "Minimax 全球领先的多模态大模型，超长上下文，视频生成顶级"
    default_model = "MiniMax-Text-01"
    available_models = [
        "MiniMax-Text-01",
        "MiniMax-VL-01",
    ]
    # ⚠ 旧域名 api.MiniMax.chat 与视频接口一样会挑 Key（返回 invalid api key）。
    #   统一用控制台当前域名 api.minimaxi.com。
    base_url = "https://api.minimaxi.com/v1"

    async def chat(self, req: ChatRequest) -> ChatResponse:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=120, write=30, pool=10)) as client:
            r = await client.post(
                f"{self.base_url}/text/chatcompletion_v2",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [{"role": m.role, "name": "user", "content": m.content} for m in req.messages],
                    "temperature": req.temperature,
                    "max_tokens": req.max_tokens,
                },
            )
            r.raise_for_status()
            data = r.json()
            # MiniMax v2 错误格式：{"base_resp":{"status_code":1004,"status_msg":"..."}}
            # 成功时：{"base_resp":{"status_code":0,...}, "choices":[...]}
            base = data.get("base_resp") or {}
            if base.get("status_code", 0) != 0:
                raise RuntimeError(f"MiniMax: {base.get('status_msg', 'unknown error')}")
            return ChatResponse(
                content=data["choices"][0]["message"]["content"],
                usage=data.get("usage", {}),
                model=data.get("model", self.model),
                raw=data,
            )

    async def stream_chat(self, req: ChatRequest) -> AsyncIterator[str]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=300, write=30, pool=10)) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/text/chatcompletion_v2",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [{"role": m.role, "name": "user", "content": m.content} for m in req.messages],
                    "temperature": req.temperature,
                    "max_tokens": req.max_tokens,
                    "stream": True,
                },
            ) as response:
                response.raise_for_status()
                first_checked = False
                async for line in response.aiter_lines():
                    payload = line[5:].strip() if line.startswith("data:") else line.strip()
                    if payload == "[DONE]":
                        break
                    if not payload:
                        continue
                    try:
                        chunk = json.loads(payload)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    # MiniMax 错误时返回纯 JSON（不是 SSE），检查一次 base_resp
                    if not first_checked:
                        first_checked = True
                        base = chunk.get("base_resp") or {}
                        if base.get("status_code", 0) != 0 and not chunk.get("choices"):
                            raise RuntimeError(f"MiniMax: {base.get('status_msg', 'unknown error')}")
                    delta = _extract_delta(chunk)
                    if delta:
                        yield delta


# ──────────── 豆包 / 字节跳动 ────────────

class DoubaoProvider(LLMProvider):
    name = "doubao"
    display_name = "豆包 Doubao（字节）"
    description = "字节火山引擎，豆包大模型，中文好"
    default_model = "doubao-pro-32k"
    available_models = [
        "doubao-pro-32k",
        "doubao-pro-128k",
        "doubao-lite-32k",
        "doubao-1-5-pro-32k",
    ]
    base_url = "https://ark.cn-beijing.volces.com/api/v3"

    async def chat(self, req: ChatRequest) -> ChatResponse:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=120, write=30, pool=10)) as client:
            r = await client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json={
                    "model": self.model,
                    "messages": [{"role": m.role, "content": m.content} for m in req.messages],
                    "temperature": req.temperature,
                    "max_tokens": req.max_tokens,
                },
            )
            r.raise_for_status()
            data = r.json()
            return ChatResponse(
                content=data["choices"][0]["message"]["content"],
                usage=data.get("usage", {}),
                model=data.get("model", self.model),
                raw=data,
            )

    async def stream_chat(self, req: ChatRequest) -> AsyncIterator[str]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=300, write=30, pool=10)) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json={
                    "model": self.model,
                    "messages": [{"role": m.role, "content": m.content} for m in req.messages],
                    "temperature": req.temperature,
                    "max_tokens": req.max_tokens,
                    "stream": True,
                },
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:]
                    if payload.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                        delta = _extract_delta(chunk)
                        if delta:
                            yield delta
                    except (json.JSONDecodeError, KeyError, IndexError, AttributeError, TypeError):
                        continue


# ──────────── 腾讯混元 ────────────

class HunyuanProvider(LLMProvider):
    name = "hunyuan"
    display_name = "腾讯混元 Hunyuan"
    description = "腾讯混元大模型，中文优秀"
    default_model = "hunyuan-pro"
    available_models = [
        "hunyuan-pro",
        "hunyuan-standard",
        "hunyuan-lite",
        "hunyuan-vision",
    ]
    base_url = "https://api.hunyuan.cloud.tencent.com/v1"

    async def chat(self, req: ChatRequest) -> ChatResponse:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=120, write=30, pool=10)) as client:
            r = await client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [{"role": m.role, "content": m.content} for m in req.messages],
                    "temperature": req.temperature,
                    "max_tokens": req.max_tokens,
                },
            )
            r.raise_for_status()
            data = r.json()
            return ChatResponse(
                content=data["choices"][0]["message"]["content"],
                usage=data.get("usage", {}),
                model=data.get("model", self.model),
                raw=data,
            )

    async def stream_chat(self, req: ChatRequest) -> AsyncIterator[str]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=300, write=30, pool=10)) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [{"role": m.role, "content": m.content} for m in req.messages],
                    "temperature": req.temperature,
                    "max_tokens": req.max_tokens,
                    "stream": True,
                },
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:]
                    if payload.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                        delta = _extract_delta(chunk)
                        if delta:
                            yield delta
                    except (json.JSONDecodeError, KeyError, IndexError, AttributeError, TypeError):
                        continue


# ──────────── Ollama 本地 ────────────

class OllamaProvider(LLMProvider):
    name = "ollama"
    display_name = "Ollama（本地）"
    description = "本地运行，完全离线，推荐 qwen2.5:7b / llama3.1:8b"
    default_model = "qwen2.5:7b"
    available_models = [
        "qwen2.5:7b", "qwen2.5:14b", "qwen2.5:32b",
        "llama3.1:8b", "llama3.2:3b",
        "mistral", "mixtral",
        "deepseek-coder-v2",
        "gemma2:9b",
        "phi3:medium",
    ]
    is_local = True
    requires_api_key = False

    def __init__(self, api_key: str = "", model: str = "", config: Dict[str, Any] = None):
        super().__init__(api_key, model, config)
        self.base_url = (config or {}).get("base_url", "http://localhost:11434")

    async def chat(self, req: ChatRequest) -> ChatResponse:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=300, write=30, pool=10)) as client:
            r = await client.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": [{"role": m.role, "content": m.content} for m in req.messages],
                    "stream": False,
                    "options": {
                        "temperature": req.temperature,
                        "num_predict": req.max_tokens,
                    },
                },
            )
            r.raise_for_status()
            data = r.json()
            return ChatResponse(
                content=data["message"]["content"],
                usage={
                    "prompt_tokens": data.get("prompt_eval_count", 0),
                    "completion_tokens": data.get("eval_count", 0),
                },
                model=self.model,
                raw=data,
            )

    async def stream_chat(self, req: ChatRequest) -> AsyncIterator[str]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=600, write=30, pool=10)) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": [{"role": m.role, "content": m.content} for m in req.messages],
                    "stream": True,
                    "options": {
                        "temperature": req.temperature,
                        "num_predict": req.max_tokens,
                    },
                },
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                        delta = chunk.get("message", {}).get("content", "")
                        if delta:
                            yield delta
                        if chunk.get("done"):
                            break
                    except (json.JSONDecodeError, KeyError):
                        continue


# ──────────── OpenAI 兼容（GPT/Claude via proxy 等）────────────

class OpenAICompatibleProvider(LLMProvider):
    """任何 OpenAI 兼容 API（GPT-4、Claude via proxy、自部署等）"""
    name = "openai"
    display_name = "OpenAI 兼容（自定义）"
    description = "任何 OpenAI 协议兼容服务（GPT-4、Claude、自建）"
    default_model = "gpt-4o-mini"
    available_models = ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo"]
    base_url = "https://api.openai.com/v1"

    def __init__(self, api_key: str = "", model: str = "", config: Dict[str, Any] = None):
        super().__init__(api_key, model, config)
        # 支持自定义 base_url
        if config and config.get("base_url"):
            self.base_url = config["base_url"]
        if config and config.get("available_models"):
            self.available_models = config["available_models"]

    async def chat(self, req: ChatRequest) -> ChatResponse:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=120, write=30, pool=10)) as client:
            r = await client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json={
                    "model": self.model,
                    "messages": [{"role": m.role, "content": m.content} for m in req.messages],
                    "temperature": req.temperature,
                    "max_tokens": req.max_tokens,
                },
            )
            r.raise_for_status()
            data = r.json()
            return ChatResponse(
                content=data["choices"][0]["message"]["content"],
                usage=data.get("usage", {}),
                model=data.get("model", self.model),
                raw=data,
            )

    async def stream_chat(self, req: ChatRequest) -> AsyncIterator[str]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=300, write=30, pool=10)) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json={
                    "model": self.model,
                    "messages": [{"role": m.role, "content": m.content} for m in req.messages],
                    "temperature": req.temperature,
                    "max_tokens": req.max_tokens,
                    "stream": True,
                },
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:]
                    if payload.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                        delta = _extract_delta(chunk)
                        if delta:
                            yield delta
                    except (json.JSONDecodeError, KeyError, IndexError, AttributeError, TypeError):
                        continue


# ──────────── Provider 注册表 ────────────

PROVIDERS: Dict[str, type] = {}


def register_provider(cls: type) -> type:
    """注册 provider"""
    instance = cls()
    PROVIDERS[instance.name] = cls
    return cls


# 自动注册
register_provider(DeepSeekProvider)
register_provider(QwenProvider)
register_provider(GLMProvider)
register_provider(KimiProvider)
register_provider(MiniMaxProvider)
register_provider(DoubaoProvider)
register_provider(HunyuanProvider)
register_provider(OllamaProvider)
register_provider(OpenAICompatibleProvider)


def get_provider(name: str, api_key: str = "", model: str = "",
                  config: Dict[str, Any] = None) -> LLMProvider:
    """获取 provider 实例"""
    if name not in PROVIDERS:
        raise ValueError(f"Unknown LLM provider: {name}. Available: {list(PROVIDERS.keys())}")
    return PROVIDERS[name](api_key=api_key, model=model, config=config)


def list_providers() -> List[Dict[str, Any]]:
    """列出所有 provider"""
    out = []
    for name, cls in PROVIDERS.items():
        inst = cls()
        out.append({
            "name": inst.name,
            "display_name": inst.display_name,
            "description": inst.description,
            "default_model": inst.default_model,
            "available_models": inst.available_models,
            "is_local": inst.is_local,
            "requires_api_key": inst.requires_api_key,
            "base_url": inst.base_url,
        })
    return out


# ──────────── Agent 系统提示词 ────────────

AGENT_SYSTEM_PROMPT = """你是 VideoForge AI 助手，一个专业的 AI 短片制作 Agent。

你的能力：
1. 帮用户规划和拆解短片创意（剧本、场次、镜头）
2. 指导用户使用 VideoForge 客户端的各项功能
3. 提供视频生成的提示词优化建议
4. 解释角色一致性、版权抽象化等专业概念
5. 协助后期拼接、字幕、配音等工作流

你的回答应该：
- 专业、简洁、有条理
- 涉及具体步骤时给出可操作指引
- 不确定时明确说明
- 涉及视频生成参数时，给出推荐值和理由

如果用户想开始做一个新短片，可以引导他们：
1. 描述创意想法（题材、时长、风格）
2. 选择生成模型（可灵/通义万相/Runway/Sora 等）
3. 先做版权抽象化（如参考了某 IP）
4. 建立资产库（角色/场景/道具）
5. 生成分镜并生成视频
6. 拼接 + 多比例导出

简洁友好，技术准确。
"""
