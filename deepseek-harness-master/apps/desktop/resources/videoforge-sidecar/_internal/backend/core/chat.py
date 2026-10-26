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
import re
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
    # ★★ 2026-10-20 更正（实测推翻 2026-09-28 的结论）：
    #
    #   ① **模型名错了**：列表里写的是 "MiniMax-M3.1-Flash-Preview"，
    #      这个模型**根本不存在**。直连官方 API 实测（2026-10-19）：
    #        - "MiniMax-M3.1-Flash"        ✅ 存在
    #        - "MiniMax-M3.1-Flash-Preview" ❌ 404 / 不存在
    #      用户在「设置」里选了这个错的名字 → 每一轮调用都失败，
    #      表现为"模型「MiniMax-M3.1-Flash」不属于 MiniMax"（下拉列表里找不到）。
    #      2026-09-28 那次 A/B 测的其实是**一个不存在的模型**，结论不可信。
    #
    #   ② 结论正好相反：实测同名模型 MiniMax-M3.1-Flash
    #        · 12.3 万 token 上下文：M3 = 134.68s 且答案被 <think> 截断；
    #          M3.1-Flash = 3.60s，3 问全对（第 2999 个镜头原文精确引用）。
    #        · JSON 结构化输出：M3 的 <think> 混在 content 里，json.loads 必失败；
    #          M3.1-Flash 直接输出干净 JSON。
    #        · 思考链长度可控：reasoning_effort=low 时思考仅 542 字符（12.1s），
    #          medium 9093 字符（37.5s），high 11229 字符（46.2s）——
    #          low 反而**内容最全**（角色 4 个）、最快、且不会截断。
    #      即 M3.1-Flash 不是"多烧 token"，而是**思考链没被约束**才烧。
    #
    #   ③ 下面保留 "MiniMax-M3.1-Flash-Preview" 只是为了不让**老配置**
    #      显示成"未知模型"，不再是推荐项。
    default_model = "MiniMax-M3.1-Flash"
    available_models = [
        # —— M3.x（1M 上下文）——
        "MiniMax-M3.1-Flash",   # ★ 真存在的模型名（2026-10-20 实测）
        "MiniMax-M3",
        "MiniMax-M3.1-Flash-Preview",  # ⚠ 旧名，实际不存在，仅为兼容老配置
        # —— M2.x ——
        "MiniMax-M2.7", "MiniMax-M2.7-highspeed",
        "MiniMax-M2.5", "MiniMax-M2.5-highspeed",
        "MiniMax-M2.1", "MiniMax-M2.1-highspeed",
        "MiniMax-M2",
        # —— 老模型（仍可用，别删，否则老配置会显示成"未知模型"）——
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
            msg = (data.get("choices") or [{}])[0].get("message") or {}
            content = msg.get("content") or ""
            # ★★ 2026-09-28：M3.1/M3 这类**带思考**的模型，思考 token 也计入
            #   max_tokens。实测 max_tokens=16 时返回的是
            #   `content: ""` + `reasoning_content: "…"` + `finish_reason: "length"`
            #   —— 正文是空的。老代码会把空字符串当成"模型没说话"，
            #   用户只会看到"AI 不回我"。这里把真实原因说出来。
            #
            #   ⚠ 但**有工具调用时正文为空是正常的**（agent 那一轮本来就只回 tool_calls），
            #     所以必须先排除 tool_calls —— 否则会把"模型正常要求调工具"
            #     误报成"只思考没正文"，agent 那一步直接失败
            #     （`tests/e2e_agent.py` 实测就是这么红的）。
            has_tool_calls = bool(msg.get("tool_calls"))
            if (not content and not has_tool_calls
                    and (msg.get("reasoning_content") or "").strip()):
                # ★ 不把问题丢给用户：**自动加预算重试一次**。
                #   前端根本不传 max_tokens（走默认 4000），而带思考的模型
                #   （M3.1/M3）思考 token 也计入上限 —— agent 那一轮提示词又长，
                #   很容易"全花在思考上、正文为空"。用户实测点「确认」就是这么断的。
                if req.max_tokens < 48000:
                    bigger = min(64000, max(8192, int(req.max_tokens) * 4))
                    logger.warning("MiniMax 只思考没正文（max_tokens=%s）→ 自动加预算到 %s 重试",
                                   req.max_tokens, bigger)
                    r2 = await client.post(
                        f"{self.base_url}/text/chatcompletion_v2",
                        headers={"Authorization": f"Bearer {self.api_key}",
                                 "Content-Type": "application/json"},
                        json={"model": self.model,
                              "messages": [{"role": m.role, "name": "user",
                                            "content": m.content} for m in req.messages],
                              "temperature": req.temperature,
                              "max_tokens": bigger},
                    )
                    r2.raise_for_status()
                    d2 = r2.json()
                    b2 = d2.get("base_resp") or {}
                    if b2.get("status_code", 0) == 0:
                        m2 = (d2.get("choices") or [{}])[0].get("message") or {}
                        c2 = m2.get("content") or ""
                        if c2.strip():
                            return ChatResponse(content=c2, usage=d2.get("usage", {}),
                                                model=d2.get("model", self.model), raw=d2)
                        data = d2
                        msg = m2
                        content = c2
                if not content and not bool(msg.get("tool_calls")) \
                        and (msg.get("reasoning_content") or "").strip():
                    raise RuntimeError(
                        "模型把预算全用在「思考」上了（这个模型会先思考，思考 token 也计入 "
                        "max_tokens）。我已经自动加过一次预算仍不够 —— 请把思考深度"
                        "（llm_effort）降到 low/medium，或把 max_tokens 调大。")
            return ChatResponse(
                content=content,
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
    # 这个 provider 的模型名由用户/远端决定，不能用本地清单去卡 ——
    # 见 get_provider 里 allow_any_model 的说明。清单一卡就会把
    # 「接自己的端点」这种正常用法判成配置错误。
    allow_any_model = True
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
    # 这个 provider 的模型名由用户/远端决定，不能用本地清单去卡 ——
    # 见 get_provider 里 allow_any_model 的说明。清单一卡就会把
    # 「接自己的端点」这种正常用法判成配置错误。
    allow_any_model = True
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


class OpenRouterProvider(LLMProvider):
    """OpenRouter 多 LLM 聚合（nex-agi/nex-n2.5-pro:free、minimax-m3 等）

    ★ 2026-09-15 加：Lance 要用 Pro / Mini 等 OpenRouter 上免费模型。
      OpenRouter 是 OpenAI 兼容 API，所以这个 provider 直接 base_url
      指向 https://openrouter.ai/api/v1 + chat completions 端点。
    """
    # 这个 provider 的模型名由用户/远端决定，不能用本地清单去卡 ——
    # 见 get_provider 里 allow_any_model 的说明。清单一卡就会把
    # 「接自己的端点」这种正常用法判成配置错误。
    allow_any_model = True
    name = "openrouter"
    display_name = "OpenRouter（多 LLM 聚合，19 个 :free 模型）"
    description = "聚合多 LLM（nex-agi/nex-n2.5-pro:free、minimax-m3、nemotron-3-ultra-550b 等）"
    default_model = "nex-agi/nex-n2.5-mini:free"
    available_models = [
        "nex-agi/nex-n2.5-mini:free", "nex-agi/nex-n2.5-pro:free",
        "inclusionai/ling-3.0-flash-vl:free",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
        "google/gemma-4-31b-it:free", "google/gemma-4-26b-a4b-it:free",
        "qwen/qwen-2.5-72b-instruct:free",
        "mistralai/mistral-small-3.2-24b-instruct:free",
        "thinkingmachines/inkling:free", "thinkingmachines/inkling-small:free",
    ]
    base_url = "https://openrouter.ai/api/v1"

    async def chat(self, req: ChatRequest) -> ChatResponse:
        import httpx as _httpx
        # OpenRouter Pro 默认走 thinking mode（耗光 tokens 思考不返 content）。
        # VideoForge UI 不传 reasoning_effort 字段 —— 显式 default 关掉。
        body = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in req.messages],
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
            "stream": False,
            
        }
        # 如果 req 里有 explicit reasoning_effort，使用之（未来扩展）
        extra = getattr(req, "extra", None) or {}
        if isinstance(extra, dict) and "reasoning_effort" in extra:
            body["reasoning_effort"] = extra["reasoning_effort"]

        async with _httpx.AsyncClient(timeout=_httpx.Timeout(connect=10, read=120, write=30, pool=10)) as client:
            r = await client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=body,
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
        import httpx as _httpx
        import json as _json
        body = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in req.messages],
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
            "stream": True,
            
        }
        async with _httpx.AsyncClient(timeout=_httpx.Timeout(connect=10, read=300, write=30, pool=10)) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=body,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    if payload.strip() == "[DONE]":
                        break
                    try:
                        chunk = _json.loads(payload)
                        delta = _extract_delta(chunk)
                        if delta:
                            yield delta
                    except (_json.JSONDecodeError, KeyError, IndexError, AttributeError, TypeError):
                        continue


# ──────────── Anthropic 兼容 Provider ────────────
# 2026-09-28 加：用 Anthropic messages API 格式接入 MiniMax-M3 系列
# （1M 上下文；M3.1-Flash-Preview 另支持 thinking + signature 与思考深度可调）。
# 默认指向国内端点 https://api.minimaxi.cn/anthropic（实测直调 HTTP 200）。
# 不依赖 anthropic SDK —— 用 httpx 直拼协议，便于 PyInstaller 打包。

class AnthropicCompatibleProvider(LLMProvider):
    """Anthropic 兼容协议 (适用于 MiniMax-M3.1-Flash-Preview / M3 / M2.x)

    通过 Anthropic Messages API 格式调用 MiniMax 端点。
    支持 thinking block + signature + 1M 上下文（cache 自动）。
    输出只保留 text 块（thinking 由 raw 字段保留，便于二次开发）。

    ★★ 2026-09-28：端点与思考深度对齐官方文档
      （platform.minimax.cn/docs/api-reference/text-anthropic-api）：
        · base_url：文档写的是 `https://api.minimax.cn/anthropic`；
          实测**国际端点 `https://api.minimaxi.com/anthropic` 同样可用**，
          所以两个都试，谁通用谁（见 `_post_messages`）。
        · **MiniMax-M3.1-Flash-Preview 强制开 thinking**，
          `thinking.type="disabled"` 会被拒（错误码 2013）；
          所以这里固定发 `{"type": "adaptive"}`。
        · 思考深度：`output_config.effort`，可取 low/medium/high/xhigh/max，
          省略时官方默认 max。用户可在设置里改（`llm_effort`）。
    """
    allow_any_model = True
    name = "anthropic"
    display_name = "Anthropic 兼容（含 MiniMax M3, 1M 上下文）"
    description = "Anthropic 兼容协议 · 1M 上下文 · 适用于 MiniMax 端点"
    # ★ 2026-09-28：默认从 M3.1-Flash-Preview 切回 MiniMax-M3。
    #   实测 M3 在 token 效率和长上下文稳定性上更好（详见上方 MiniMax provider
    #   的注释与 .context/ab-test/REPORT.md）；M3.1 保留在下方列表可切回。
    # ★ 2026-10-20：同 MiniMaxProvider 一样纠正 ——
    #   "MiniMax-M3.1-Flash-Preview" 这个模型**不存在**（实测 404）。
    #   真名是 "MiniMax-M3.1-Flash"。
    default_model = "MiniMax-M3.1-Flash"
    available_models = [
        "MiniMax-M3.1-Flash",             # ★ 真名（1M context，思考深度可调）
        "MiniMax-M3",                    # 1M context, 不强制 thinking, token 更省
        "MiniMax-M3.1-Flash-Preview",    # ⚠ 旧名，实际不存在，仅兼容老配置
        "MiniMax-M2.7", "MiniMax-M2.7-highspeed",
        "MiniMax-M2.5", "MiniMax-M2.5-highspeed",
        "MiniMax-M2.1", "MiniMax-M2.1-highspeed",
        "MiniMax-M2",
    ]
    # 文档里的端点（国内）。国际端点作为备选，见 _post_messages。
    base_url = "https://api.minimax.cn/anthropic"
    #: 备选端点：主端点连不上（或返回 404/5xx）时自动换这个再试一次
    fallback_base_urls = ["https://api.minimaxi.com/anthropic"]
    #: 合法的思考深度档位（官方文档：低→高）
    EFFORTS = ("low", "medium", "high", "xhigh", "max")

    def __init__(self, api_key: str = "", model: str = "", config: Dict[str, Any] = None):
        super().__init__(api_key=api_key, model=model, config=config)
        if not self.base_url:
            self.base_url = "https://api.minimax.cn/anthropic"
        cfg = config or {}
        # 思考深度：允许 settings 里用 llm_effort / effort 覆盖
        eff = str(cfg.get("effort") or cfg.get("llm_effort") or "").strip().lower()
        self.effort = eff if eff in self.EFFORTS else "high"

    def _body_extra(self, model_id: str) -> Dict[str, Any]:
        """按模型补 thinking / 思考深度。M3.1 与 M3 都要 adaptive。"""
        import re as _re
        extra: Dict[str, Any] = {}
        # M3.1 / M3 系列：thinking 用 adaptive（M3.1 传 disabled 会 400）
        if _re.search(r"MiniMax-M3", model_id, _re.I):
            extra["thinking"] = {"type": "adaptive"}
            extra["output_config"] = {"effort": self.effort}
        return extra

    def _base_candidates(self) -> List[str]:
        out = [str(self.base_url or "").rstrip("/")]
        for b in self.fallback_base_urls:
            b = str(b or "").rstrip("/")
            if b and b not in out:
                out.append(b)
        return [b for b in out if b]

    async def _post_messages(self, body: Dict[str, Any], *, stream: bool = False):
        """带**端点回退**的 POST：主端点不通就换备选端点再试一次。

        为什么要回退：文档写的国内端点与国际端点在实测里**都可用**，
        但不同账号/网络下未必都通。写死一个的后果是"换台机器就报 404"，
        而报错完全看不出"是端点不对还是密钥不对"。
        """
        import httpx as _httpx
        last_err: Optional[Exception] = None
        for i, base in enumerate(self._base_candidates()):
            try:
                async with _httpx.AsyncClient(
                        timeout=_httpx.Timeout(connect=10, read=300, write=30, pool=10)) as client:
                    if stream:
                        return client, base     # 交给调用方以流式方式打开
                    r = await client.post(f"{base}/v1/messages",
                                          headers=self._headers(), json=body)
                    r.raise_for_status()
                    return r.json(), base
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning("Anthropic 端点 %s 失败（%s），%s", base, e,
                               "换备选端点" if i + 1 < len(self._base_candidates())
                               else "没有备选了")
        raise RuntimeError(f"Anthropic 兼容端点都连不上：{last_err}")

    def _headers(self) -> Dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "User-Agent": "VideoForge/1.0",
        }

    @staticmethod
    def _split_system(messages: List[Message]) -> tuple[str, List[Dict[str, str]]]:
        sys_text = ""
        rest = []
        for m in messages:
            if m.role == "system":
                sys_text += (("\n" if sys_text else "") + m.content)
            else:
                rest.append({"role": m.role, "content": m.content})
        return sys_text, rest

    async def chat(self, req: ChatRequest) -> ChatResponse:
        system_text, msgs = self._split_system(req.messages)
        model_id = self.model or self.default_model
        body: Dict[str, Any] = {
            "model": model_id,
            "max_tokens": req.max_tokens,
            "messages": msgs,
            "stream": False,
        }
        if system_text:
            body["system"] = system_text
        body.update(self._body_extra(model_id))

        data, used_base = await self._post_messages(body)
        text, thinking = self._extract_text(data)

        # ★★ 2026-09-28：**思考吃光预算就自动加预算重试**，而不是把错误丢给用户。
        #
        #   用户实测到的原话（他点了「确认」之后）：
        #       [错误] 模型调用失败：模型只产出了「思考」没有正文：M3.1 强制开 thinking，
        #              思考 token 也计入 max_tokens。把 max_tokens 调到 ≥2048…
        #   —— 技术上是这样，但**让用户去改参数是不对的**：agent 一轮里
        #   系统提示词 + 工具表 + 历史本就很长，M3.1 的思考又必然发生，
        #   max_tokens=4000（前端根本不传，走默认）很容易被思考吃光。
        #   正确做法是**这一层自己兜住**：加预算 + 降一档思考深度重试一次。
        if not text and thinking and req.max_tokens < 48000:
            bigger = min(64000, max(8192, int(req.max_tokens) * 4))
            retry = dict(body)
            retry["max_tokens"] = bigger
            if "output_config" in retry:
                # 降一档：max→xhigh→high→medium（低档位思考更省，先把话说完）
                retry["output_config"] = {"effort": "medium"}
            logger.warning("M3.1 只思考没正文（max_tokens=%s）→ 自动加预算到 %s 重试一次",
                           req.max_tokens, bigger)
            data, used_base = await self._post_messages(retry)
            text, thinking = self._extract_text(data)

        if not text and thinking:
            raise RuntimeError(
                "模型把预算全用在「思考」上了（M3.1 强制开 thinking，思考 token 也计入 "
                "max_tokens）。我已经自动加过一次预算仍不够。请把思考深度（llm_effort）"
                "降到 low/medium，或在设置里把 max_tokens 调大。")
        return ChatResponse(
            content=text,
            usage=data.get("usage", {}),
            model=data.get("model", model_id),
            raw=data,
        )

    @staticmethod
    def _extract_text(data: Dict[str, Any]) -> tuple:
        """从 Anthropic 响应里取 (正文, 思考)，两块分开。"""
        text_parts, thinking_parts = [], []
        for blk in data.get("content", []) or []:
            if blk.get("type") == "text":
                text_parts.append(blk.get("text", ""))
            elif blk.get("type") == "thinking":
                thinking_parts.append(blk.get("thinking") or "")
        return "\n".join(text_parts).strip(), "\n".join(thinking_parts).strip()

    async def stream_chat(self, req: ChatRequest) -> AsyncIterator[str]:
        """Anthropic SSE: content_block_delta.delta.type='text_delta' 增量吐字"""
        import httpx as _httpx
        import json as _json
        system_text, msgs = self._split_system(req.messages)
        model_id = self.model or self.default_model
        body: Dict[str, Any] = {
            "model": model_id,
            "max_tokens": req.max_tokens,
            "messages": msgs,
            "stream": True,
        }
        if system_text:
            body["system"] = system_text
        body.update(self._body_extra(model_id))

        # 流式也要端点回退。★ 只有在**一个字都还没吐出去**时才换端点 ——
        # 已经吐过字再重试会让用户看到重复内容。
        last_err: Optional[Exception] = None
        for base in self._base_candidates():
            yielded = False
            try:
                async with _httpx.AsyncClient(
                        timeout=_httpx.Timeout(connect=10, read=300, write=30, pool=10)) as client:
                    async with client.stream("POST", f"{base}/v1/messages",
                                             headers=self._headers(), json=body) as r:
                        r.raise_for_status()
                        async for line in r.aiter_lines():
                            if not line or not line.startswith("data:"):
                                continue
                            payload = line[5:].strip()
                            if not payload or payload == "[DONE]":
                                continue
                            try:
                                obj = _json.loads(payload)
                            except Exception:  # noqa: BLE001
                                continue
                            if obj.get("type") == "content_block_delta":
                                delta = obj.get("delta") or {}
                                if delta.get("type") == "text_delta":
                                    piece = delta.get("text", "")
                                    if piece:
                                        yielded = True
                                        yield piece
                return
            except Exception as e:  # noqa: BLE001
                last_err = e
                if yielded:
                    raise
                logger.warning("Anthropic 流式端点 %s 失败（%s），试备选端点", base, e)
        raise RuntimeError(f"Anthropic 兼容端点都连不上（流式）：{last_err}")


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
register_provider(OpenRouterProvider)
register_provider(AnthropicCompatibleProvider)


def get_provider(name: str, api_key: str = "", model: str = "",
                  config: Dict[str, Any] = None) -> LLMProvider:
    """获取 provider 实例。

    ★ 这里加了一道「模型名与 provider 是否配套」的检查。
      为什么值得单独写：实测踩到过一条完整的死路 ——
        设置里 Provider 选了 MiniMax，模型下拉却没跟着变，存进去是
        `llm_provider=MiniMax` + `llm_model=deepseek-chat`。
        之后每一次对话都是上游返回
            HTTP 400: invalid params, unknown model 'deepseek-chat'
        这个报错**完全没提"模型和厂商不匹配"**，用户只会觉得"AI 坏了"。

      所以：模型名不在该 provider 的清单里时，**不要**把它当成
      "上游的锅"原样抛出去 —— 直接说清楚是配置问题、去哪儿改。
      available_models 为空（自定义/OpenAI 兼容端点）时不做判断，
      否则会误伤"用户自己填模型名"的正常用法。
    """
    if name not in PROVIDERS:
        raise ValueError(f"Unknown LLM provider: {name}. Available: {list(PROVIDERS.keys())}")

    model = (model or "").strip()
    if model:
        cls = PROVIDERS[name]
        # ★ 这些 provider 必须允许**任意**模型名，不能拿清单去卡：
        #   · openai   —— 「OpenAI 兼容」是给用户接自己的端点用的，
        #                  模型名当然是用户自己填的（本地 vLLM / 中转 / 自建）。
        #                  拿 gpt-4o 清单去限制它，等于把这个功能废掉。
        #   · ollama   —— 本地拉的模型五花八门（qwen2.5:7b、llama3.2:3b…）。
        #   · openrouter —— 聚合站，几万个 `vendor/model`，清单永远是旧的。
        #   实测教训：第一版检查没排除 openai，把「自定义模型名」判成了错误。
        #   检查的**假阳性**比漏报更糟 —— 它会把本来能用的配置锁死。
        allow_any = bool(getattr(cls, "allow_any_model", False))
        known = list(getattr(cls, "available_models", None) or [])
        if known and not allow_any and model not in known:
            raise ValueError(
                f"模型「{model}」不属于 {name}（{cls.display_name}）。"
                f"这通常是在「设置」里换了 Provider、但模型下拉没跟着换造成的。"
                f"{name} 可用：{'、'.join(known[:6])}"
                + ("…" if len(known) > 6 else "")
                + f"。请到「设置 → 默认模型」重新选一个（或直接选 "
                  f"{cls.default_model}）。"
            )
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


# ──────────── ★ 从 settings 解析「用哪个 LLM」────────────
#
# 为什么必须收成一个函数（这是一个真实事故的修复）：
#
#   项目里有**两套**字段名，而解析逻辑散落在多处、各自写错：
#
#     · 界面 / 数据库用新名字：llm_provider / llm_model / llm_api_keys
#     · 旧代码读老名字：      remote_llm_provider / remote_llm_model / remote_llm_key
#
#   实际后果（在用户的真库里复现）：
#     库里是 llm_provider="MiniMax"、llm_model=""（空 = 想用厂商默认）。
#
#     ① `main.py::_make_llm` 写成
#          model = llm_model or remote_llm_model or "deepseek-chat"
#        —— 空字符串是假值，于是模型名被**硬编码兜底成 deepseek-chat**，
#        不管厂商是谁。provider=MiniMax + model=deepseek-chat →
#        上游 400 `unknown model 'deepseek-chat'`。
#        走这条路的是**剧本生成 / 分镜 / 抽取资产**，也就是整条主流程。
#
#     ② `orchestrator.py::_get_llm_from_settings` 只读老名字，而老名字
#        在新库里**根本不存在** → 落到 else 分支 → 永远用 DeepSeek + 空 Key。
#        用户配的 MiniMax 一次也没被用上。
#
#   两处的共同病根：**模型名的兜底值不该是某一个具体厂商的模型**。
#   空模型名的正确语义是"用这个厂商自己的默认模型"。
#   所以这里统一成一个函数，并让它在模型名为空时回落到
#   `PROVIDERS[provider].default_model`。

def resolve_llm_settings(settings: Dict[str, Any]) -> Dict[str, str]:
    """从 settings 里解析出最终要用的 (provider, model, api_key)。

    规则（顺序重要）：
      1. 本地 Ollama 打开时直接走 ollama，忽略云端配置
      2. provider：新名字 → 老名字 → deepseek（并规范化到 PROVIDERS 的名字）
      3. api_key ：在 llm_api_keys / api_keys / tts_api_keys 里**大小写不敏感**地找，
                   再退到老名字 remote_llm_key
      4. model   ：新名字 → 老名字 → **该 provider 自己的默认模型**
                   ★ 绝不回落到别的厂商的模型名
    """
    settings = settings or {}

    if settings.get("enable_llm_local"):
        return {
            "provider": "ollama",
            "model": str(settings.get("ollama_model")
                         or settings.get("ollama_model_name") or "qwen2.5:7b"),
            "api_key": "",
        }

    provider = (settings.get("llm_provider")
                or settings.get("remote_llm_provider") or "deepseek")
    provider = str(provider).strip() or "deepseek"

    # ── Key：大小写不敏感地在各 bucket 里找 ──
    api_key = ""
    want = provider.strip().lower()
    for bucket in ("llm_api_keys", "api_keys", "tts_api_keys"):
        d = settings.get(bucket) or {}
        if isinstance(d, dict):
            for k, v in d.items():
                if str(k).strip().lower() == want and v:
                    api_key = str(v).strip()
                    break
        if api_key:
            break
    if not api_key:
        api_key = str(settings.get("remote_llm_key") or "").strip()

    # ── provider 规范化：库里可能存成 "MiniMax"/"minimax"/显示名 ──
    canon = None
    for name, cls in PROVIDERS.items():
        if name.lower() == provider.lower():
            canon = name
            break
    if canon is None:
        for name, cls in PROVIDERS.items():
            if str(getattr(cls, "display_name", "")).strip().lower() == provider.lower():
                canon = name
                break

    # ── model：空值 = 用这个厂商自己的默认 ──
    model = str(settings.get("llm_model")
                or settings.get("remote_llm_model") or "").strip()
    if not model and canon:
        model = str(getattr(PROVIDERS[canon], "default_model", "") or "")
    # 认不出厂商（用户自填 base_url 的 OpenAI 兼容端点）时**不要瞎猜模型名**，
    # 留给 LLMClient 用它自己的兜底逻辑。

    return {"provider": canon or provider, "model": model, "api_key": api_key}


# ──────────── Agent 系统提示词 ────────────
#
# 历史包袱说明：这里原本是一段「客服话术」（"指导用户使用客户端各项功能"）。
# 工作区内所有生成按钮已经移除，改成对话驱动，所以这套话术不再成立——
# 模型现在**自己就能调工具干活**，不该再把用户往界面上引。
# 真正的工具目录与调用协议由 core/agent_tools.build_agent_prompt() 拼装，
# 下面这段只是兜底（比如前端显式传了 system 覆盖时）。

AGENT_SYSTEM_PROMPT = """你是 VideoForge 的短片制作 Agent，能直接操作这个平台，不只是聊天。

工作区界面里已经**没有**生成按钮了。用户要看进度、要生成、要改东西，全都是跟你说一句话，
你调工具去做。所以不要再说"请点击某某按钮"，那是在骗用户。

## 你的工作方式

需要执行动作时，**只输出一个 JSON 对象**，不要带任何其他文字：

{"tool": "工具名", "args": {"参数名": "值"}}

系统会执行它并把结果回给你，然后你再决定下一步。
如果只是在回答、解释、确认，**直接用自然语言回复**，不要输出 JSON。

## 规矩

1. 一次只调一个工具，等结果再迈下一步。
2. 缺参数就问用户，**不要瞎猜 id**。
3. 工具返回 ok=false 时，把 error 原文告诉用户，**不要假装成功**。
4. 生成类操作耗时几分钟到几十分钟，动手前用一句话说清你要干什么。
5. 不要编造工具名，只能用系统给你的工具。
"""


# ──────────── Agent 接线（把 provider 适配成 agent 能用的 llm_chat）────────────


def make_agent_llm(provider: "LLMProvider", *, temperature: float = 0.7,
                   max_tokens: int = 4000):
    """把一个 LLMProvider 包成 `async def chat(messages, **kw) -> str`。

    agent 循环要的是「给一串 dict，还一段文本」这么个朴素接口，
    而 provider.chat() 收的是 ChatRequest/Message 对象，这里做转换。

    为什么不用 provider.stream_chat：
        流式 chunk 拼出来的文本无法保证是完整 JSON（工具调用可能被切断），
        agent 每轮必须拿到完整输出才能解析动作。所以 agent 模式走非流式，
        「实时性」由 SSE 把每一步工具执行结果推给前端来保证。

    ★★ 2026-09-28：**带思考的模型要给它足够的预算**。
      用户实测点「确认」后直接断了：
          [错误] 模型只产出了「思考」没有正文：M3.1 强制开 thinking…
      根因不是模型不行，而是：前端**根本不传 max_tokens**（于是走默认 4000），
      而 agent 一轮的输入 = 系统提示词 + 50 多个工具签名 + 历史，
      M3.1 的思考 token **也计入** max_tokens ⇒ 思考没完就被截断，正文是空的。
      这里对"会思考的模型"抬一个下限（8K），从源头上不让它饿着。
      （provider 内部还有一层"加预算重试"的兜底，两层都留着。）
    """
    eff_tokens = int(max_tokens or 4000)
    try:
        _name = f"{getattr(provider, 'name', '')} {getattr(provider, 'model', '')}"
        if re.search(r"MiniMax-M3|Flash-Preview", _name, re.I):
            eff_tokens = max(eff_tokens, 8192)
    except Exception:  # noqa: BLE001
        pass

    async def _llm_chat(messages: List[Dict[str, str]], **_kw) -> str:
        req = ChatRequest(
            messages=[Message(role=m.get("role", "user"), content=m.get("content", ""))
                      for m in messages],
            temperature=temperature,
            max_tokens=eff_tokens,
        )
        resp = await provider.chat(req)
        return getattr(resp, "content", "") or ""

    return _llm_chat


async def run_agent_turn(
    *,
    provider: "LLMProvider",
    messages: List[Dict[str, str]],
    project_id: str = "",
    max_steps: int = 24,
    temperature: float = 0.7,
    max_tokens: int = 4000,
):
    """非流式跑一轮 agent，返回 core.agent.AgentRun。"""
    from .agent import run_agent
    from .agent_tools import build_project_context

    ctx = await build_project_context(project_id)
    return await run_agent(
        llm_chat=make_agent_llm(provider, temperature=temperature, max_tokens=max_tokens),
        messages=messages,
        max_steps=max_steps,
        system_extra=ctx,
        project_id=project_id,
    )


async def run_agent_turn_stream(
    *,
    provider: "LLMProvider",
    messages: List[Dict[str, str]],
    project_id: str = "",
    max_steps: int = 24,
    temperature: float = 0.7,
    max_tokens: int = 4000,
) -> AsyncIterator[Dict[str, Any]]:
    """流式跑一轮 agent，逐事件 yield（给 SSE 用）。

    事件类型：
        {"type":"step",   "index":1, "tool":"generate_script", "args":{...}}
        {"type":"result", "index":1, "ok":true, "data":{...}}
        {"type":"reply",  "text":"最终答复"}
        {"type":"error",  "text":"..."}
    """
    from .agent import run_agent_stream
    from .agent_tools import build_project_context

    ctx = await build_project_context(project_id)
    async for ev in run_agent_stream(
        llm_chat=make_agent_llm(provider, temperature=temperature, max_tokens=max_tokens),
        messages=messages,
        max_steps=max_steps,
        system_extra=ctx,
        project_id=project_id,
    ):
        yield ev
