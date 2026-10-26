# -*- coding: utf-8 -*-
"""`backend/tests/test_llm_protocols.py` —— 四种文本协议的单元测试（纯 mock / 本地假服务）

覆盖（对应任务验收点）：
  ① 四种协议的**请求构造**：URL / 请求头 / body 字段
  ② 四种协议的**响应解析**：正文、model、usage 归一化
  ③ 错误归一：401 / 403 / 上游 error 包装 / 超时 / 空响应 / thinking-only
  ④ 未知协议**明确报错**，绝不悄悄降级
  ⑤ 路由选择：`LLMClient.resolve_protocol()` 对 14 家文本平台各走哪种协议
  ⑥ `LLMClient.chat()` 对非 OpenAI 平台确实走新协议（且失败不跨平台重试）
  ⑦ **MiniMax 大小写**：库里存 `"MiniMax"`、平台 id 是 `minimax` 时能取到 Key
  ⑧ 过期全局 base_url 防护（切换平台后不会把请求发到别家地址）

纪律：
  · **不联网、不花钱**：HTTPS 请求全部走 `httpx.MockTransport`；
    端到端用例只连本机 `127.0.0.1` 的临时 HTTP 假服务（随机端口，跑完即关）。
  · 任何测试都不得读取真实 API Key / 真实数据库。

跑法（两种都可以）：
    python -X utf8 backend/tests/test_llm_protocols.py
    python -X utf8 -m pytest backend/tests/test_llm_protocols.py -q
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict
from unittest.mock import patch

import httpx

# 让「仓库根」和「backend 目录」两种跑法都能 import core.*
_BACKEND = Path(__file__).resolve().parents[1]
_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_BACKEND), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core import llm as llm_mod                      # noqa: E402
from core import llm_protocols as P                  # noqa: E402
from core.llm import LLMClient, normalize_provider, provider_key  # noqa: E402


# ──────────────────────────── 测试辅助 ────────────────────────────


def _run_with_mock(coro_factory: Callable[[httpx.AsyncClient], Any],
                   handler: Callable[[httpx.Request], httpx.Response]) -> Any:
    """用 MockTransport 跑一次调用（**不联网**）。"""

    async def _go() -> Any:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)
        try:
            return await coro_factory(client)
        finally:
            await client.aclose()

    return asyncio.run(_go())


def _capture(handler_fn: Callable[[httpx.Request], httpx.Response],
             captured: Dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    """包一层 handler，把请求的 method/url/headers/body 记下来。"""

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        raw = request.content or b""
        try:
            captured["json"] = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:  # noqa: BLE001
            captured["json"] = {"_raw": raw.decode("utf-8", errors="replace")}
        return handler_fn(request)

    return _handler


def _messages() -> list:
    return [
        {"role": "system", "content": "你是编剧。"},
        {"role": "user", "content": "写一句台词。"},
    ]


# ──────────────────────────── ① 请求构造 ────────────────────────────


def test_openai_request_construction() -> None:
    captured: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "model": "deepseek-chat",
            "choices": [{"message": {"role": "assistant", "content": "你好"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        })

    result = _run_with_mock(
        lambda client: P.call_openai(
            "https://api.deepseek.com", path="/v1/chat/completions",
            api_key="sk-test", model="deepseek-chat", messages=_messages(),
            temperature=0.3, max_tokens=7, client=client),
        _capture(handler, captured))

    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer sk-test"
    body = captured["json"]
    assert body["model"] == "deepseek-chat"
    assert body["max_tokens"] == 7
    assert body["temperature"] == 0.3
    assert body["messages"] == _messages(), "OpenAI 协议 messages 应原样透传（含 system）"
    assert result["ok"] is True and result["text"] == "你好"
    assert result["status_code"] == 200


def test_anthropic_request_construction() -> None:
    captured: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "model": "claude-haiku-4-5",
            "content": [{"type": "text", "text": "你好"}],
            "usage": {"input_tokens": 9, "output_tokens": 2},
        })

    _run_with_mock(
        lambda client: P.call_anthropic(
            "https://api.anthropic.com", path="/v1/messages",
            api_key="sk-ant-test", model="claude-haiku-4-5", messages=_messages(),
            temperature=0.4, max_tokens=5, client=client),
        _capture(handler, captured))

    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["x-api-key"] == "sk-ant-test"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert "authorization" not in captured["headers"], "Anthropic 不能用 Bearer Authorization"
    body = captured["json"]
    assert body["model"] == "claude-haiku-4-5"
    assert body["max_tokens"] == 5
    assert body["system"] == "你是编剧。", "system 必须是顶层字段"
    assert body["messages"] == [{"role": "user", "content": "写一句台词。"}], \
        "system 不能留在 messages 里"


def test_gemini_request_construction() -> None:
    captured: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "你好"}]}}],
            "usageMetadata": {"promptTokenCount": 6, "candidatesTokenCount": 2,
                              "totalTokenCount": 8},
        })

    _run_with_mock(
        lambda client: P.call_gemini(
            "https://generativelanguage.googleapis.com",
            path="/v1beta/models/{model}:generateContent",
            api_key="goog-test", model="gemini-2.5-flash", messages=_messages(),
            temperature=0.5, max_tokens=3, client=client),
        _capture(handler, captured))

    assert captured["url"] == (
        "https://generativelanguage.googleapis.com/"
        "v1beta/models/gemini-2.5-flash:generateContent")
    assert captured["headers"]["x-goog-api-key"] == "goog-test"
    body = captured["json"]
    assert "contents" in body and isinstance(body["contents"], list)
    assert body["contents"][-1]["parts"][0]["text"] == "写一句台词。"
    assert body["systemInstruction"]["parts"][0]["text"] == "你是编剧。"
    assert body["generationConfig"]["maxOutputTokens"] == 3


def test_ollama_request_construction() -> None:
    captured: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "model": "qwen2.5:7b",
            "message": {"role": "assistant", "content": "你好"},
        })

    _run_with_mock(
        lambda client: P.call_ollama(
            "http://127.0.0.1:11434", path="/api/chat",
            model="qwen2.5:7b", messages=_messages(),
            temperature=0.2, max_tokens=9, client=client),
        _capture(handler, captured))

    assert captured["url"] == "http://127.0.0.1:11434/api/chat"
    body = captured["json"]
    assert body["model"] == "qwen2.5:7b"
    assert body["stream"] is False
    assert body["options"]["num_predict"] == 9
    assert body["options"]["temperature"] == 0.2
    assert "authorization" not in captured["headers"], "本地 Ollama 不需要 Key"


# ──────────────────────────── ② 响应解析 / usage ────────────────────────────


def test_openai_response_usage_parsing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "model": "gpt-4o-mini",
            "choices": [{"message": {"content": "pong"}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 1, "total_tokens": 12},
        })

    result = _run_with_mock(
        lambda client: P.call_openai("https://api.openai.com", api_key="k",
                                     model="gpt-4o-mini", messages=[{"role": "user", "content": "ping"}],
                                     max_tokens=1, client=client),
        handler)
    assert result["ok"] is True
    assert result["text"] == "pong"
    assert result["model"] == "gpt-4o-mini"
    assert result["usage"] == {"prompt_tokens": 11, "completion_tokens": 1, "total_tokens": 12}
    assert isinstance(result["latency_ms"], int)
    assert result["error"] is None


def test_anthropic_response_usage_parsing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "model": "claude-haiku-4-5",
            "content": [{"type": "text", "text": "第一段"},
                        {"type": "text", "text": "第二段"}],
            "usage": {"input_tokens": 20, "output_tokens": 5},
        })

    result = _run_with_mock(
        lambda client: P.call_anthropic("https://api.anthropic.com", api_key="k",
                                        model="claude-haiku-4-5",
                                        messages=[{"role": "user", "content": "x"}], client=client),
        handler)
    assert result["ok"] is True
    assert result["text"] == "第一段\n第二段", "多个 text 块要按顺序拼接"
    assert result["usage"] == {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}


def test_gemini_response_usage_parsing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "modelVersion": "gemini-2.5-flash",
            "candidates": [{"content": {"parts": [{"text": "pong"}]}}],
            "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 1,
                              "totalTokenCount": 5},
        })

    result = _run_with_mock(
        lambda client: P.call_gemini("https://generativelanguage.googleapis.com",
                                     api_key="k", model="gemini-2.5-flash",
                                     messages=[{"role": "user", "content": "ping"}],
                                     client=client),
        handler)
    assert result["ok"] is True
    assert result["text"] == "pong"
    assert result["model"] == "gemini-2.5-flash"
    assert result["usage"] == {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5}


def test_ollama_response_usage_parsing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "model": "qwen2.5:7b",
            "message": {"role": "assistant", "content": "pong"},
            "prompt_eval_count": 8,
            "eval_count": 2,
        })

    result = _run_with_mock(
        lambda client: P.call_ollama("http://127.0.0.1:11434", model="qwen2.5:7b",
                                     messages=[{"role": "user", "content": "ping"}],
                                     client=client),
        handler)
    assert result["ok"] is True
    assert result["text"] == "pong"
    assert result["usage"] == {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10}


def test_usage_normalization_all_protocols() -> None:
    assert P.normalize_usage("openai", {"usage": {"prompt_tokens": 3, "completion_tokens": 4}}) \
        == {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}
    assert P.normalize_usage("anthropic", {"usage": {"input_tokens": 5, "output_tokens": 6}}) \
        == {"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11}
    assert P.normalize_usage("gemini", {"usageMetadata": {
        "promptTokenCount": 7, "candidatesTokenCount": 8}}) \
        == {"prompt_tokens": 7, "completion_tokens": 8, "total_tokens": 15}
    assert P.normalize_usage("ollama", {"prompt_eval_count": 1, "eval_count": 2}) \
        == {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
    assert P.normalize_usage("不存在的协议", {"usage": {}}) \
        == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


# ──────────────────────────── ③ 错误归一 ────────────────────────────


def test_openai_401_error_normalized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {
            "message": "Incorrect API key provided", "type": "invalid_request_error"}})

    result = _run_with_mock(
        lambda client: P.call_openai("https://api.openai.com", api_key="bad",
                                     model="gpt-4o-mini",
                                     messages=[{"role": "user", "content": "x"}], client=client),
        handler)
    assert result["ok"] is False
    assert result["status_code"] == 401
    assert result["text"] == ""
    assert "Incorrect API key provided" in result["error"]
    assert "HTTP 401" in result["error"]


def test_anthropic_401_error_normalized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {
            "type": "authentication_error", "message": "invalid x-api-key"}})

    result = _run_with_mock(
        lambda client: P.call_anthropic("https://api.anthropic.com", api_key="bad",
                                        model="claude-haiku-4-5",
                                        messages=[{"role": "user", "content": "x"}], client=client),
        handler)
    assert result["ok"] is False and result["status_code"] == 401
    assert "invalid x-api-key" in result["error"]


def test_gemini_403_error_normalized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {
            "code": 403, "message": "API key not valid. Please pass a valid API key.",
            "status": "PERMISSION_DENIED"}})

    result = _run_with_mock(
        lambda client: P.call_gemini("https://generativelanguage.googleapis.com",
                                     api_key="bad", model="gemini-2.5-flash",
                                     messages=[{"role": "user", "content": "x"}], client=client),
        handler)
    assert result["ok"] is False and result["status_code"] == 403
    assert "API key not valid" in result["error"]


def test_timeout_is_explicit_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("模拟连接超时")

    result = _run_with_mock(
        lambda client: P.call_openai("https://api.openai.com", api_key="k",
                                     model="gpt-4o-mini",
                                     messages=[{"role": "user", "content": "x"}],
                                     timeout=60.0, client=client),
        handler)
    assert result["ok"] is False
    assert result["status_code"] is None, "超时不是 HTTP 错误，status_code 必须为 None"
    assert "超时" in result["error"]
    assert result["text"] == ""


def test_empty_openai_response_is_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    result = _run_with_mock(
        lambda client: P.call_openai("https://api.openai.com", api_key="k",
                                     model="gpt-4o-mini",
                                     messages=[{"role": "user", "content": "x"}], client=client),
        handler)
    assert result["ok"] is False, "HTTP 200 但没正文不能算成功（否则空剧本会被当成功）"
    assert result["status_code"] == 200
    assert "choices" in (result["error"] or "")


def test_anthropic_thinking_only_is_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "model": "MiniMax-M3",
            "content": [{"type": "thinking", "thinking": "让我想想…"}],
            "usage": {"input_tokens": 10, "output_tokens": 1},
        })

    result = _run_with_mock(
        lambda client: P.call_anthropic("https://api.minimax.cn/anthropic", api_key="k",
                                        model="MiniMax-M3",
                                        messages=[{"role": "user", "content": "x"}], client=client),
        handler)
    assert result["ok"] is False
    assert "thinking" in (result["error"] or "").lower()


def test_gemini_safety_block_is_explicit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "promptFeedback": {"blockReason": "SAFETY"},
            "candidates": [],
        })

    result = _run_with_mock(
        lambda client: P.call_gemini("https://generativelanguage.googleapis.com",
                                     api_key="k", model="gemini-2.5-flash",
                                     messages=[{"role": "user", "content": "x"}], client=client),
        handler)
    assert result["ok"] is False
    assert "SAFETY" in (result["error"] or "")


def test_unknown_protocol_raises_no_fallback() -> None:
    """未知协议必须**直接报错**，不能「随便挑一个协议试」——那会把配置错误伪装成能用。"""
    raised = None
    try:
        asyncio.run(P.call_protocol("some-new-protocol", base_url="https://x.example",
                                    model="m", messages=[{"role": "user", "content": "x"}]))
    except ValueError as exc:
        raised = str(exc)
    assert raised is not None, "未知协议必须抛 ValueError"
    assert "some-new-protocol" in raised
    assert "不会自动降级" in raised


# ──────────────────────────── ④ 路由选择 ────────────────────────────


def test_llm_client_routes_all_text_platforms() -> None:
    from core import provider_registry as registry

    expected = {
        "deepseek": ("openai", "https://api.deepseek.com"),
        "openai": ("openai", "https://api.openai.com"),
        "qwen": ("openai", "https://dashscope.aliyuncs.com/compatible-mode"),
        "kimi": ("openai", "https://api.moonshot.cn"),
        "glm": ("openai", "https://open.bigmodel.cn/api/paas/v4"),
        "doubao": ("openai", "https://ark.cn-beijing.volces.com/api/v3"),
        "hunyuan": ("openai", "https://api.hunyuan.cloud.tencent.com"),
        "minimax": ("openai", "https://api.minimaxi.com"),
        "openrouter": ("openai", "https://openrouter.ai/api/v1"),
        "siliconflow": ("openai", "https://api.siliconflow.cn"),
        "groq": ("openai", "https://api.groq.com/openai"),
        "anthropic": ("anthropic", "https://api.anthropic.com"),
        "gemini": ("gemini", "https://generativelanguage.googleapis.com"),
        "ollama": ("ollama", "http://127.0.0.1:11434"),
        # agentai 是注册表后来加的文本平台（中转）；没有它时跳过，不让目录变动把测试搞红
        "agentai": ("openai", "https://agentai.token6688.com"),
    }
    for provider, (protocol, base) in expected.items():
        if registry.find("text", provider) is None:
            continue
        client = LLMClient(provider=provider)
        assert client.resolve_protocol() == protocol, "%s 协议判定错了" % provider
        resolved_base, _path, model = client.resolve_endpoint()
        assert resolved_base == base, "%s base_url 错了：%s" % (provider, resolved_base)
        assert model, "%s 没解析出默认模型" % provider

    # 显示名 / 别名也要认（设置里历史数据可能是 "MiniMax"/"claude"）
    assert normalize_provider("MiniMax") == "minimax"
    assert normalize_provider("Anthropic Claude") == "anthropic", "注册表显示名要能反查到 id"
    assert LLMClient(provider="MiniMax").resolve_protocol() == "openai"
    assert LLMClient(provider="claude").resolve_protocol() == "anthropic"
    assert LLMClient(provider="google").resolve_protocol() == "gemini"


def test_llm_client_chat_uses_platform_protocol() -> None:
    captured: Dict[str, Any] = {}

    async def fake_call(protocol: str, **kwargs: Any) -> Dict[str, Any]:
        captured["protocol"] = protocol
        captured["kwargs"] = kwargs
        return {"ok": True, "text": "你好", "model": kwargs.get("model"),
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                "latency_ms": 7, "error": None, "status_code": 200}

    with patch.object(llm_mod, "_call_protocol", fake_call):
        out = asyncio.run(LLMClient(provider="anthropic", api_key="sk-x",
                                    model="claude-haiku-4-5").chat(
            [{"role": "user", "content": "hi"}], temperature=0.2, max_tokens=8))

    assert out == "你好"
    assert captured["protocol"] == "anthropic"
    kwargs = captured["kwargs"]
    assert kwargs["base_url"] == "https://api.anthropic.com"
    assert kwargs["path"] == "/v1/messages"
    assert kwargs["model"] == "claude-haiku-4-5"
    assert kwargs["max_tokens"] == 8 and kwargs["temperature"] == 0.2


def test_llm_client_chat_does_not_fallback_on_401() -> None:
    calls = []

    async def fake_call(protocol: str, **kwargs: Any) -> Dict[str, Any]:
        calls.append(protocol)
        return {"ok": False, "text": "", "model": kwargs.get("model"), "usage": {},
                "latency_ms": 3, "error": "HTTP 401: invalid x-api-key",
                "status_code": 401}

    error = None
    with patch.object(llm_mod, "_call_protocol", fake_call):
        try:
            asyncio.run(LLMClient(provider="anthropic", api_key="bad",
                                  model="claude-haiku-4-5").chat(
                [{"role": "user", "content": "hi"}]))
        except RuntimeError as exc:
            error = str(exc)

    assert error is not None, "401 必须抛错，不能返回空文本当成功"
    assert "claude-haiku-4-5" in error and "401" in error
    assert calls == ["anthropic"], "失败后不许换别的平台/协议重试（否则会掩盖配置错误）"


# ──────────────────────────── ⑤ Key 大小写 / 设置解析 ────────────────────────────


def test_provider_key_case_insensitive_minimax() -> None:
    """真实库形状：`llm_api_keys={"MiniMax": ...}`，平台 id 是 `minimax`。"""
    settings = {"llm_api_keys": {"MiniMax": "sk-cp-mini", "deepseek": "sk-ds"}}
    # 旧写法（精确匹配）在真实库上就是拿不到：
    assert settings["llm_api_keys"].get("minimax") is None
    # 新入口：大小写不敏感
    assert provider_key(settings, "minimax") == "sk-cp-mini"
    assert provider_key(settings, "MiniMax") == "sk-cp-mini"
    assert provider_key(settings, "deepseek") == "sk-ds"
    assert provider_key(settings, "openai") == ""


def test_provider_key_registry_bucket_and_env() -> None:
    # siliconflow 的 Key 按注册表声明在 tts_api_keys 桶（与配音/图像共用）
    settings = {"llm_api_keys": {}, "tts_api_keys": {"siliconflow": "sk-sf"}}
    assert provider_key(settings, "siliconflow") == "sk-sf"
    assert provider_key(settings, "SiliconFlow") == "sk-sf"

    # 环境变量兜底只查**该平台自己声明**的 env
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-env-anthropic"}):
        assert provider_key({}, "anthropic") == "sk-env-anthropic"
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
        assert provider_key({}, "anthropic") == ""
        assert provider_key({"llm_api_keys": {"openai": "sk-o"}}, "openai") == "sk-o"


def test_provider_key_same_gateway_sibling_key() -> None:
    """同一网关共用 Key：agentai 文本平台的 Key 实际存在 `api_keys["seedance_t6"]`。"""
    from core import provider_registry as registry

    if registry.find("text", "agentai") is None:
        return  # 注册表里暂时没有这个平台时跳过（不把别人的目录变动变成红）
    settings = {"api_keys": {"seedance_t6": "sk-gateway-shared"}}
    assert provider_key(settings, "agentai") == "sk-gateway-shared"
    assert provider_key(settings, "AgentAI") == "sk-gateway-shared"


def test_llm_client_from_settings_minimax_uppercase_key() -> None:
    settings = {
        "llm_provider": "MiniMax",          # 库里真实值（大写 M）
        "llm_model": "",                    # 空 = 用该平台自己的默认模型
        "llm_api_keys": {"MiniMax": "sk-cp-mini"},
        "llm_base_url": "https://api.deepseek.com",   # 真实库里的过期全局地址
    }
    client = LLMClient.from_settings(settings)
    assert client.provider == "minimax"
    assert client.api_key == "sk-cp-mini", "大小写不敏感取 Key 失败 → 会 401"
    assert client.model == "MiniMax-Text-01", "空模型名要用本平台默认，不能回落到别家"
    assert client.resolve_protocol() == "openai"
    # 过期全局 base_url 必须被忽略，否则请求会打到 DeepSeek 官方地址上
    assert client.resolve_endpoint()[0] == "https://api.minimaxi.com"

    # 老字段名也要认（remote_llm_provider / remote_llm_model）
    legacy = LLMClient.from_settings({"remote_llm_provider": "deepseek",
                                      "remote_llm_model": "deepseek-reasoner"})
    assert legacy.provider == "deepseek" and legacy.model == "deepseek-reasoner"


def test_stale_global_base_url_ignored() -> None:
    # 另一家的官方地址 → 判定为过期全局设置，忽略
    c = LLMClient(provider="anthropic", api_key="k",
                  base_url="https://api.deepseek.com", model="claude-haiku-4-5")
    assert c.resolve_endpoint()[0] == "https://api.anthropic.com"

    # 真正的自建网关/中转地址 → 必须生效（不能误伤）
    c2 = LLMClient(provider="openai", api_key="k",
                   base_url="https://my-relay.example.com/v1", model="some-model")
    base, path, _ = c2.resolve_endpoint()
    assert base == "https://my-relay.example.com"
    assert path == "/v1/chat/completions"


def test_unknown_provider_clear_error() -> None:
    # 注意：名字里不能含 "or"（历史模糊别名会把含 "or" 的串认成 openrouter）
    raised = None
    try:
        LLMClient(provider="acme-llm").resolve_protocol()
    except ValueError as exc:
        raised = str(exc)
    assert raised is not None
    assert "acme-llm" in raised
    assert "不会自动改用其他平台" in raised

    # 未知平台但**用户自己填了 base_url** → 按 OpenAI 兼容端点处理（这是文档化用法）
    c = LLMClient(provider="my-gateway", base_url="https://gateway.example.com/v1",
                  model="my-model")
    assert c.resolve_protocol() == "openai"
    assert c.resolve_endpoint()[0] == "https://gateway.example.com"


# ──────────────────────────── ⑥ 本地假服务端到端 ────────────────────────────


def test_local_fake_server_openai_end_to_end() -> None:
    """真起一个 127.0.0.1 的 HTTP 假服务，验证 URL/头/body 真的按协议发出去了。"""
    seen: Dict[str, Any] = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server 的接口名
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            seen["path"] = self.path
            seen["authorization"] = self.headers.get("Authorization")
            seen["body"] = json.loads(raw.decode("utf-8"))
            payload = json.dumps({
                "model": "fake-model",
                "choices": [{"message": {"role": "assistant", "content": "本地假服务 OK"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: Any) -> None:  # 不打日志，保持测试输出干净
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = "http://127.0.0.1:%d" % server.server_port
        result = asyncio.run(P.call_openai(
            base_url=base, path="/v1/chat/completions", api_key="local-key",
            model="fake-model", messages=[{"role": "user", "content": "ping"}],
            max_tokens=1, timeout=10.0))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    assert result["ok"] is True, result
    assert result["text"] == "本地假服务 OK"
    assert seen["path"] == "/v1/chat/completions"
    assert seen["authorization"] == "Bearer local-key"
    assert seen["body"]["max_tokens"] == 1


# ──────────────────────────── 独立运行入口（非 pytest 也能跑）────────────────────────────


def _run_all() -> None:
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failed = []
    for name, func in tests:
        try:
            func()
            print("  [OK]   %s" % name)
        except Exception as exc:  # noqa: BLE001 - 测试运行器要汇总所有失败
            failed.append(name)
            print("  [FAIL] %s → %s: %s" % (name, type(exc).__name__, exc))
    print("\n%d/%d passed" % (len(tests) - len(failed), len(tests)))
    if failed:
        print("失败：\n  - " + "\n  - ".join(failed))
        raise SystemExit(1)


if __name__ == "__main__":
    _run_all()
