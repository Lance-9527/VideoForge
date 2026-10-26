"""
VideoForge · LLM 健壮性工具函数（借鉴 MoneyPrinterTurbo app/services/llm.py）

独立文件，避免污染 llm.py 主体；llm.py 通过 `from .llm_robust import ...` 复用。

包含：
- _MAX_RETRIES / _RETRY_BACKOFF — 指数退避参数
- _THINK_BLOCK_RE / _UNCLOSED_THINK_BLOCK_RE — reasoning 块清理正则
- _URL_USERINFO_RE / _SENSITIVE_QUERY_RE — URL 凭据清理正则
- _sanitize_error_message(error) — 清理异常信息中的 URL 凭据
- _normalize_text_response(content, provider) — 统一 LLM 响应（None 容错 + reasoning 清理）
- _freeze_config(provider, api_key, model, base_url) — 配置快照
- is_retryable_http_status(status_code) — 判断是否值得重试（5xx/429 是，其余不重试）
"""

import re
from datetime import datetime


# ──────────── 常量 ────────────


_MAX_RETRIES = 5
_RETRY_BACKOFF = (1.0, 2.0, 4.0, 8.0, 16.0)


# reasoning model 的思考块（DeepSeek R1 / Claude thinking / MiniMax M3 等）。
# 视频脚本和关键词只需要最终可朗读文本；如果不在服务层统一清理，WebUI、字幕
# 和配音都会把思考过程当正文处理。
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_UNCLOSED_THINK_BLOCK_RE = re.compile(r"<think>.*$", re.IGNORECASE | re.DOTALL)


# URL 凭据 + 查询参数敏感信息（防止 base_url 里的 password 泄露到 UI / 日志）
_URL_USERINFO_RE = re.compile(
    r"((?:https?|wss?)://)([^/\s?#@]*:[^/\s?#@]*@)",
    re.IGNORECASE,
)
_SENSITIVE_QUERY_RE = re.compile(
    r"([?&](?:api[_-]?key|access[_-]?token|token|key|secret|password)=)([^&#\s]+)",
    re.IGNORECASE,
)


# ──────────── 函数 ────────────


def is_retryable_http_status(status_code):
    """4xx 不重试（参数错误重试也没用），5xx/429 重试。"""
    if status_code == 429:
        return True
    return 500 <= int(status_code or 0) < 600


def sanitize_error_message(error):
    """清理错误信息中的 URL 凭据 / 查询参数，避免 custom base_url 中的密码泄露。

    一些 OpenAI-compatible SDK 会把请求 URL 原样拼进异常信息。如果用户为了代理网关
    配置了 https://user:pass@example.com/v1，直接返回 str(e) 会把密码暴露给
    前端 / API 调用方 / 日志。这里仅处理错误文案，不改变实际请求地址。
    """
    msg = str(error)
    msg = _URL_USERINFO_RE.sub(r"\1***:***@", msg)
    msg = _SENSITIVE_QUERY_RE.sub(r"\1***", msg)
    return msg


def normalize_text_response(content, provider=""):
    """统一清理 LLM 文本响应。

    1. None / 空字符串 / 非字符串 → 抛 ValueError（明示错误而非后续 .replace() 崩溃）
    2. 清理 reasoning 块
    3. 首尾空白清理；正文内的单换行 / 双换行保留（脚本按段、字幕按行读取依赖）。
    """
    if content is None:
        raise ValueError(f"[{provider or 'llm'}] returned empty text content")
    if not isinstance(content, str):
        raise TypeError(
            f"[{provider or 'llm'}] returned non-text content: {type(content).__name__}"
        )
    cleaned = _THINK_BLOCK_RE.sub("", content)
    cleaned = _UNCLOSED_THINK_BLOCK_RE.sub("", cleaned).strip()
    if not cleaned:
        raise ValueError(
            f"[{provider or 'llm'}] returned empty text content after think-block cleanup"
        )
    return cleaned


def freeze_config(provider, api_key, model, base_url=""):
    """配置快照：避免重试期间被另一个并发的设置更新切换 provider / base_url。

    返回的 dict 是调用瞬间的不可变视图，调用方后续只读这份快照，不去实时查 settings。
    """
    return {
        "provider": provider,
        "api_key": api_key,
        "model": model,
        "base_url": base_url,
        "frozen_at": datetime.now().isoformat(),
    }


def retry_backoff_seconds(attempt):
    """返回第 N 次重试前应等待的秒数（attempt 从 0 开始）。"""
    if attempt < 0:
        return 0.0
    if attempt >= len(_RETRY_BACKOFF):
        return _RETRY_BACKOFF[-1]
    return _RETRY_BACKOFF[attempt]
