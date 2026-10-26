# -*- coding: utf-8 -*-
"""VideoForge · 文本模型协议客户端（openai / anthropic / gemini / ollama）

═══════════════════════════════════════════════════════════════════
为什么单独一层（2026-10-25）
═══════════════════════════════════════════════════════════════════
`core/llm.py` 的 `LLMClient` 过去只会说一种语言：**OpenAI 兼容协议**。
于是设置里能选 14 家文本平台（`core/provider_registry.py`），但其中
anthropic / gemini / ollama 走的是完全不同的 HTTP 契约，`PROVIDER_ENDPOINTS`
里也没有它们 —— 结果就是「目录里有名字，选了就调不通」。

这一层只做一件事：**把四种协议的差异收在一个文件里**，对外给出统一结果。
上层（`LLMClient`）不再关心某家厂商的头怎么拼、响应怎么挖。

═══════════════════════════════════════════════════════════════════
统一契约（所有 call_* 函数都返回这 7 个键，不多不少）
═══════════════════════════════════════════════════════════════════
    {
      "ok": bool,            # 只有「HTTP 2xx + 解析出非空文本」才是 True
      "text": str,           # 模型正文（失败时为空字符串）
      "model": str,          # 实际模型名（优先用响应里的）
      "usage": {             # 统一成 OpenAI 的命名，方便上下层复用
          "prompt_tokens": int,
          "completion_tokens": int,
          "total_tokens": int,
      },
      "latency_ms": int,     # 从发请求到拿到响应（含失败）的墙钟耗时
      "error": str | None,   # 失败原因（已清洗 URL 凭据）
      "status_code": int | None,  # HTTP 状态码；网络错误/超时为 None
    }

设计红线：
  · **不悄悄降级**：`call_protocol("xxx")` 遇到未知协议直接 ValueError，
    绝不「这个不行就换 openai 试试」——那会把「Key 填错」伪装成「能用」。
  · **超时 60 秒**（默认 `DEFAULT_TIMEOUT`），超时是显式失败而不是静默空文本。
  · **空响应不算成功**：HTTP 200 但挖不出正文 → ok=False + 明确 error，
    避免上游把「空剧本」当成「生成成功」。
  · **不打印/不返回 Key**：错误信息统一过 `sanitize_error_message`。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

try:  # 在包内用相对导入；被单独当脚本/被测试拷走时退回本地清洗
    from .llm_robust import sanitize_error_message as _sanitize_error_message
except Exception:  # pragma: no cover - 仅在脱离包结构 import 时触发
    def _sanitize_error_message(error: Any) -> str:  # type: ignore[misc]
        return str(error)

logger = logging.getLogger("videoforge.llm_protocols")

#: 默认超时（秒）。任务书要求 60s；调用方可以显式覆盖（探测时用更短的）。
DEFAULT_TIMEOUT = 60.0

#: 默认预算（有的协议 max_tokens 必填；调用方一般都会显式传）
DEFAULT_MAX_TOKENS = 4000

#: 支持的协议名（顺序即报错里的展示顺序）
SUPPORTED_PROTOCOLS: Tuple[str, ...] = ("openai", "anthropic", "gemini", "ollama")


# ──────────────────────────── 公共小工具 ────────────────────────────


def _empty_usage() -> Dict[str, int]:
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _result(*, ok: bool, text: Any = "", model: Any = "", usage: Optional[Dict[str, int]] = None,
            latency_ms: float = 0.0, error: Optional[str] = None,
            status_code: Optional[int] = None) -> Dict[str, Any]:
    """按统一契约组装返回值（唯一的出口，保证字段齐全）。"""
    return {
        "ok": bool(ok),
        "text": str(text or ""),
        "model": str(model or ""),
        "usage": dict(usage or _empty_usage()),
        "latency_ms": int(latency_ms or 0),
        "error": error,
        "status_code": status_code,
    }


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def normalize_messages(messages: Any) -> List[Dict[str, str]]:
    """把调用方给的 messages 规整成 [{role, content}]。

    兼容三种历史写法：标准 dict、只有 role/content 的对象、以及纯字符串。
    content 非字符串时（多模态列表等）序列化成 JSON —— 本层只做纯文本调用，
    但**不能因为一个非文本 content 就崩**（ScriptService 的调用永远是字符串，
    这里兜的是其他业务线传进来的历史数据）。
    """
    out: List[Dict[str, str]] = []
    for item in list(messages or []):
        if isinstance(item, dict):
            role = str(item.get("role") or "user").strip() or "user"
            content = item.get("content")
        else:
            role = str(getattr(item, "role", "") or "user").strip() or "user"
            content = getattr(item, "content", item)
        if content is None:
            content = ""
        elif not isinstance(content, str):
            try:
                content = json.dumps(content, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                content = str(content)
        out.append({"role": role, "content": content})
    return out


def split_system(messages: Any) -> Tuple[str, List[Dict[str, str]]]:
    """拆出 system 提示（Anthropic / Gemini 的 system 不在 messages 里）。

    多条 system 按出现顺序用换行拼接（与 `chat.py` 的 Anthropic provider 同口径），
    其余消息保持原顺序 —— 轮次顺序会影响模型理解，不能重排。
    """
    system_parts: List[str] = []
    rest: List[Dict[str, str]] = []
    for msg in normalize_messages(messages):
        if msg["role"] in ("system", "developer"):
            if msg["content"]:
                system_parts.append(msg["content"])
        else:
            rest.append(msg)
    return "\n".join(system_parts), rest


def build_url(base_url: str, path: str = "", model: str = "") -> str:
    """拼完整 URL；`path` 里的 `{model}` 占位替换成模型名。

    约定与 `core/llm.py` 一致：base 不带末尾斜杠、path 以 / 开头。
    Gemini 的路径 `/v1beta/models/{model}:generateContent` 就靠这里替换。
    """
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("缺少 base_url（文本平台地址为空）")
    if not base.startswith(("http://", "https://")):
        raise ValueError("base_url 必须以 http:// 或 https:// 开头，当前为「%s」" % base)
    p = str(path or "")
    if "{model}" in p:
        p = p.replace("{model}", str(model or ""))
    if p and not p.startswith("/"):
        p = "/" + p
    return base + p


def _error_detail(response: httpx.Response) -> str:
    """从错误响应里挖出**人能看懂**的一句话（四种厂商的 error 包装都不一样）。"""
    raw_text = ""
    try:
        raw_text = response.text or ""
    except Exception:  # noqa: BLE001
        raw_text = ""
    try:
        data = response.json()
    except Exception:  # noqa: BLE001
        return _sanitize_error_message(raw_text[:300] or ("HTTP %s" % response.status_code))
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            msg = (err.get("message") or err.get("detail") or err.get("code")
                   or json.dumps(err, ensure_ascii=False))
            return _sanitize_error_message(str(msg)[:300])
        if isinstance(err, str) and err.strip():
            return _sanitize_error_message(err[:300])
        for key in ("message", "detail", "msg", "error_msg"):
            if data.get(key):
                return _sanitize_error_message(str(data[key])[:300])
        return _sanitize_error_message(json.dumps(data, ensure_ascii=False)[:300])
    return _sanitize_error_message(str(data)[:300])


async def _post_json(*, url: str, headers: Dict[str, str], body: Dict[str, Any],
                     timeout: float, client: Optional[httpx.AsyncClient]) -> httpx.Response:
    """发 POST；临时 client 自己建自己关，外部传入的 client 由调用方管生命周期。"""
    own_client = client is None
    if own_client:
        # trust_env=False：本机装了系统代理时，httpx 默认会走代理，长文本调用
        # 会被代理掐断（core/llm.py 里已踩过同样的坑）。
        client = httpx.AsyncClient(timeout=timeout, trust_env=False)
    try:
        return await client.post(url, headers=headers, json=body, timeout=timeout)
    finally:
        if own_client:
            await client.aclose()


async def _execute(*, protocol: str, url: str, headers: Dict[str, str],
                   body: Dict[str, Any], model: str, timeout: float,
                   client: Optional[httpx.AsyncClient],
                   parse: Callable[[Dict[str, Any]], Tuple[str, str, Dict[str, int], Optional[str]]]
                   ) -> Dict[str, Any]:
    """四种协议共用的「发请求 → 判状态 → 解析 → 组装」骨架。

    `parse(data)` 返回 `(text, model_out, usage, error)`：
      · error 非空 ⇒ 响应格式不对/被安全策略拦截/只有 thinking 没正文；
      · error 为空但 text 为空 ⇒ 由这里统一判成「空响应」失败。
    """
    started = time.perf_counter()
    try:
        response = await _post_json(url=url, headers=headers, body=body,
                                    timeout=timeout, client=client)
    except httpx.TimeoutException as exc:
        return _result(ok=False, model=model, latency_ms=_elapsed_ms(started),
                       error="请求超时（%gs）：%s: %s"
                             % (timeout, type(exc).__name__, exc))
    except httpx.HTTPError as exc:
        return _result(ok=False, model=model, latency_ms=_elapsed_ms(started),
                       error="网络错误：%s: %s"
                             % (type(exc).__name__, _sanitize_error_message(exc)))
    except Exception as exc:  # noqa: BLE001 - 任何意外都要变成统一结果，不能炸调用方
        return _result(ok=False, model=model, latency_ms=_elapsed_ms(started),
                       error="%s: %s" % (type(exc).__name__, _sanitize_error_message(exc)))

    latency = _elapsed_ms(started)
    if response.status_code >= 400:
        detail = _error_detail(response)
        return _result(ok=False, model=model, latency_ms=latency,
                       error="HTTP %s: %s" % (response.status_code, detail),
                       status_code=response.status_code)

    try:
        data = response.json()
    except Exception:  # noqa: BLE001
        return _result(ok=False, model=model, latency_ms=latency,
                       error="HTTP %s 返回的不是 JSON：%r"
                             % (response.status_code, (response.text or "")[:200]),
                       status_code=response.status_code)
    if not isinstance(data, dict):
        data = {"_raw": data}

    try:
        text, model_out, usage, parse_error = parse(data)
    except Exception as exc:  # noqa: BLE001
        return _result(ok=False, model=model, latency_ms=latency,
                       error="解析 %s 响应失败：%s: %s"
                             % (protocol, type(exc).__name__, exc),
                       status_code=response.status_code)

    if parse_error:
        return _result(ok=False, model=model_out or model, usage=usage, latency_ms=latency,
                       error=parse_error, status_code=response.status_code)
    if not str(text or "").strip():
        return _result(ok=False, model=model_out or model, usage=usage, latency_ms=latency,
                       error="HTTP %s 成功但返回空文本（%s 协议）"
                             % (response.status_code, protocol),
                       status_code=response.status_code)
    return _result(ok=True, text=text, model=model_out or model, usage=usage,
                   latency_ms=latency, status_code=response.status_code)


# ──────────────────────────── usage 归一化 ────────────────────────────
# 四种协议的 usage 字段名完全不同；统一成 OpenAI 命名后，上层（成本统计、
# 日志、前端用量展示）只认一种结构。

def _usage_openai(data: Dict[str, Any]) -> Dict[str, int]:
    usage = data.get("usage") or {}
    prompt = _as_int(usage.get("prompt_tokens"))
    completion = _as_int(usage.get("completion_tokens"))
    total = _as_int(usage.get("total_tokens")) or (prompt + completion)
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}


def _usage_anthropic(data: Dict[str, Any]) -> Dict[str, int]:
    usage = data.get("usage") or {}
    prompt = _as_int(usage.get("input_tokens"))
    completion = _as_int(usage.get("output_tokens"))
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion}


def _usage_gemini(data: Dict[str, Any]) -> Dict[str, int]:
    usage = data.get("usageMetadata") or {}
    prompt = _as_int(usage.get("promptTokenCount"))
    completion = _as_int(usage.get("candidatesTokenCount"))
    total = _as_int(usage.get("totalTokenCount")) or (prompt + completion)
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}


def _usage_ollama(data: Dict[str, Any]) -> Dict[str, int]:
    prompt = _as_int(data.get("prompt_eval_count"))
    completion = _as_int(data.get("eval_count"))
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion}


_USAGE_PARSERS: Dict[str, Callable[[Dict[str, Any]], Dict[str, int]]] = {
    "openai": _usage_openai,
    "anthropic": _usage_anthropic,
    "gemini": _usage_gemini,
    "ollama": _usage_ollama,
}


def normalize_usage(protocol: str, data: Dict[str, Any]) -> Dict[str, int]:
    """按协议把响应里的 usage 归一成 `{prompt_tokens, completion_tokens, total_tokens}`。"""
    parser = _USAGE_PARSERS.get(str(protocol or "").strip().lower())
    if parser is None:
        return _empty_usage()
    try:
        return parser(data if isinstance(data, dict) else {})
    except Exception:  # noqa: BLE001 - usage 解析失败不该让整次调用失败
        return _empty_usage()


# ──────────────────────────── openai 协议 ────────────────────────────
# 适用：deepseek / openai / qwen / kimi / glm / doubao / hunyuan / minimax /
#       openrouter / siliconflow / groq，以及用户自填的任意 OpenAI 兼容端点。

async def call_openai(base_url: str, *, path: str = "/v1/chat/completions",
                      api_key: str = "", model: str = "", messages: Any = None,
                      temperature: Optional[float] = 0.7, max_tokens: Optional[int] = DEFAULT_MAX_TOKENS,
                      timeout: float = DEFAULT_TIMEOUT,
                      client: Optional[httpx.AsyncClient] = None,
                      extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """OpenAI 兼容的 `/chat/completions` 调用（非流式）。"""
    msg_list = normalize_messages(messages)
    body: Dict[str, Any] = {"model": model, "messages": msg_list}
    if max_tokens is not None:
        body["max_tokens"] = int(max_tokens)
    if temperature is not None:
        body["temperature"] = float(temperature)

    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + str(api_key)
    if extra_headers:
        headers.update({str(k): str(v) for k, v in extra_headers.items()})
    url = build_url(base_url, path or "/v1/chat/completions", model)

    def _parse(data: Dict[str, Any]) -> Tuple[str, str, Dict[str, int], Optional[str]]:
        usage = _usage_openai(data)
        choices = data.get("choices") or []
        if not choices:
            return "", str(data.get("model") or model), usage, \
                "响应里没有 choices（openai 协议）"
        message = (choices[0] or {}).get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            # 少数兼容网关会把 content 拆成 parts 数组
            parts: List[str] = []
            for part in content:
                if isinstance(part, dict):
                    parts.append(str(part.get("text") or ""))
                else:
                    parts.append(str(part))
            content = "".join(parts)
        return (str(content or ""), str(data.get("model") or model), usage, None)

    return await _execute(protocol="openai", url=url, headers=headers, body=body,
                          model=model, timeout=timeout, client=client, parse=_parse)


# ──────────────────────────── anthropic 协议 ────────────────────────────
# 适用：Anthropic 官方（https://api.anthropic.com）以及 Anthropic 兼容端点
#       （例如 MiniMax 的 /anthropic 端点）。
# 注意：**不能**走 OpenAI 兼容路径 —— 头是 x-api-key + anthropic-version，
# body 里 system 是顶层字段，返回值在 content[] 数组里。

async def call_anthropic(base_url: str, *, path: str = "/v1/messages",
                        api_key: str = "", model: str = "", messages: Any = None,
                        temperature: Optional[float] = 0.7,
                        max_tokens: Optional[int] = DEFAULT_MAX_TOKENS,
                        timeout: float = DEFAULT_TIMEOUT,
                        client: Optional[httpx.AsyncClient] = None,
                        extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Anthropic Messages API（`POST /v1/messages`，非流式）。"""
    system_text, rest = split_system(messages)
    body: Dict[str, Any] = {
        "model": model,
        # Anthropic 的 max_tokens 是**必填**；缺省时给一个保守值而不是省略
        "max_tokens": int(max_tokens if max_tokens is not None else DEFAULT_MAX_TOKENS),
        "messages": rest,
    }
    if system_text:
        body["system"] = system_text
    if temperature is not None:
        body["temperature"] = float(temperature)

    headers: Dict[str, str] = {
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    if api_key:
        headers["x-api-key"] = str(api_key)
    if extra_headers:
        headers.update({str(k): str(v) for k, v in extra_headers.items()})
    url = build_url(base_url, path or "/v1/messages", model)

    def _parse(data: Dict[str, Any]) -> Tuple[str, str, Dict[str, int], Optional[str]]:
        usage = _usage_anthropic(data)
        blocks = data.get("content") or []
        text_parts: List[str] = []
        thinking_parts: List[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type") or "")
            if btype == "text":
                text_parts.append(str(block.get("text") or ""))
            elif btype == "thinking":
                thinking_parts.append(str(block.get("thinking") or ""))
        text = "\n".join(p for p in text_parts if p).strip()
        model_out = str(data.get("model") or model)
        if not text and thinking_parts:
            return "", model_out, usage, (
                "只有 thinking 块没有正文（max_tokens 可能被思考吃光；"
                "Anthropic 协议）")
        return text, model_out, usage, None

    return await _execute(protocol="anthropic", url=url, headers=headers, body=body,
                          model=model, timeout=timeout, client=client, parse=_parse)


# ──────────────────────────── gemini 协议 ────────────────────────────
# 适用：Google Generative Language API。
# 路径里带模型名：/v1beta/models/{model}:generateContent；Key 走 x-goog-api-key。

async def call_gemini(base_url: str, *,
                      path: str = "/v1beta/models/{model}:generateContent",
                      api_key: str = "", model: str = "", messages: Any = None,
                      temperature: Optional[float] = 0.7,
                      max_tokens: Optional[int] = DEFAULT_MAX_TOKENS,
                      timeout: float = DEFAULT_TIMEOUT,
                      client: Optional[httpx.AsyncClient] = None,
                      extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Gemini `generateContent`（非流式）。"""
    system_text, rest = split_system(messages)
    contents: List[Dict[str, Any]] = []
    for msg in rest:
        # Gemini 用 "model" 表示 assistant 轮次；system 走 systemInstruction
        role = "model" if msg["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": msg["content"]}]})
    body: Dict[str, Any] = {"contents": contents}
    if system_text:
        body["systemInstruction"] = {"parts": [{"text": system_text}]}
    generation_config: Dict[str, Any] = {}
    if max_tokens is not None:
        generation_config["maxOutputTokens"] = int(max_tokens)
    if temperature is not None:
        generation_config["temperature"] = float(temperature)
    if generation_config:
        body["generationConfig"] = generation_config

    headers: Dict[str, str] = {"content-type": "application/json"}
    if api_key:
        headers["x-goog-api-key"] = str(api_key)
    if extra_headers:
        headers.update({str(k): str(v) for k, v in extra_headers.items()})

    base = str(base_url or "").strip().rstrip("/")
    # 用户可能把 .../v1beta 填进 base_url，避免拼成 /v1beta/v1beta/...
    if base.endswith("/v1beta"):
        base = base[: -len("/v1beta")]
    url = build_url(base, path or "/v1beta/models/{model}:generateContent", model)

    def _parse(data: Dict[str, Any]) -> Tuple[str, str, Dict[str, int], Optional[str]]:
        usage = _usage_gemini(data)
        model_out = str(data.get("modelVersion") or model)
        candidates = data.get("candidates") or []
        if not candidates:
            feedback = data.get("promptFeedback") or {}
            block_reason = feedback.get("blockReason")
            if block_reason:
                return "", model_out, usage, "请求被安全策略拦截：%s（gemini 协议）" % block_reason
            return "", model_out, usage, "响应里没有 candidates（gemini 协议）"
        cand = candidates[0] or {}
        content = cand.get("content") or {}
        parts = content.get("parts") or []
        text_parts = [str(p.get("text") or "") for p in parts if isinstance(p, dict)]
        text = "\n".join(p for p in text_parts if p).strip()
        return text, model_out, usage, None

    return await _execute(protocol="gemini", url=url, headers=headers, body=body,
                          model=model, timeout=timeout, client=client, parse=_parse)


# ──────────────────────────── ollama 协议 ────────────────────────────
# 适用：本机/局域网 Ollama（默认 http://127.0.0.1:11434），不需要 Key。

async def call_ollama(base_url: str = "http://127.0.0.1:11434", *,
                      path: str = "/api/chat", api_key: str = "", model: str = "",
                      messages: Any = None, temperature: Optional[float] = 0.7,
                      max_tokens: Optional[int] = DEFAULT_MAX_TOKENS,
                      timeout: float = DEFAULT_TIMEOUT,
                      client: Optional[httpx.AsyncClient] = None,
                      extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Ollama `POST /api/chat`（非流式）。"""
    body: Dict[str, Any] = {
        "model": model,
        "messages": normalize_messages(messages),
        "stream": False,
    }
    options: Dict[str, Any] = {}
    if temperature is not None:
        options["temperature"] = float(temperature)
    if max_tokens is not None:
        # Ollama 用 num_predict 表示最大生成长度
        options["num_predict"] = int(max_tokens)
    if options:
        body["options"] = options

    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if api_key:  # 只有套了反代鉴权的 Ollama 才需要
        headers["Authorization"] = "Bearer " + str(api_key)
    if extra_headers:
        headers.update({str(k): str(v) for k, v in extra_headers.items()})
    url = build_url(base_url or "http://127.0.0.1:11434", path or "/api/chat", model)

    def _parse(data: Dict[str, Any]) -> Tuple[str, str, Dict[str, int], Optional[str]]:
        usage = _usage_ollama(data)
        if data.get("error"):
            return "", str(data.get("model") or model), usage, \
                "Ollama 返回错误：%s" % data.get("error")
        message = data.get("message") or {}
        return (str(message.get("content") or ""),
                str(data.get("model") or model), usage, None)

    return await _execute(protocol="ollama", url=url, headers=headers, body=body,
                          model=model, timeout=timeout, client=client, parse=_parse)


# ──────────────────────────── 统一入口 ────────────────────────────

#: 协议名 → 实现函数。**新增平台必须落在这四个协议桶之一**，不允许「兜底到 openai」。
_DISPATCH: Dict[str, Callable[..., Any]] = {
    "openai": call_openai,
    "anthropic": call_anthropic,
    "gemini": call_gemini,
    "ollama": call_ollama,
}


async def call_protocol(protocol: str, **kwargs: Any) -> Dict[str, Any]:
    """按协议名调用，返回统一结构。

    未知协议**直接报错**（附支持清单），绝不猜一个协议去试 ——
    「猜」会把配置错误变成一次看起来成功的请求，是比失败更糟的结果。
    """
    proto = str(protocol or "").strip().lower()
    func = _DISPATCH.get(proto)
    if func is None:
        raise ValueError(
            "未知的文本协议「%s」；支持：%s。不会自动降级到其他平台/协议。"
            % (protocol, " / ".join(SUPPORTED_PROTOCOLS))
        )
    return await func(**kwargs)


__all__ = [
    "DEFAULT_TIMEOUT",
    "DEFAULT_MAX_TOKENS",
    "SUPPORTED_PROTOCOLS",
    "normalize_messages",
    "split_system",
    "build_url",
    "normalize_usage",
    "call_openai",
    "call_anthropic",
    "call_gemini",
    "call_ollama",
    "call_protocol",
]
