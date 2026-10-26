# -*- coding: utf-8 -*-
"""VideoForge · 图像模型统一接入层（注册表 10 家全覆盖）

═══════════════════════════════════════════════════════════════════════
为什么要有这一层（2026-10-25）
═══════════════════════════════════════════════════════════════════════
`core/provider_registry.py` 在设置页里列了 **10 家**图像模型：
    openai / ark / gemini / wanx / qwen / minimax / siliconflow / zhipu /
    stability / local
但 `core/imagegen.py` 实际只实现了 5 家（minimax / qwen(=wanx) /
openai / siliconflow / local）。用户在设置里选了 ark / gemini / zhipu /
stability，点「生成」只会得到一句「不支持的图像模型」—— 典型的
「配了却用不上」。

本模块把 10 家**全部**接上真实协议，并提供统一入口：

    await generate_image(provider_id, prompt, *, size, settings, out_dir)
        -> {ok, path|urls, provider, model, latency_ms, error}

约定（与任务书一致）：
  · 统一返回里 **永远** 有 `ok` / `provider` / `model` / `latency_ms`；
    成功带 `path`（本地落盘）+ `urls`（原始远端链接）+ `bytes`；
    失败带 `error`（厂商原始错误码 + 响应前 200 字）与 `status`（HTTP 码）。
  · 不联网也能测：所有协议都支持 `base_url` 覆盖（settings 或参数），
    测试用本地假 HTTP 服务即可，**不花钱**。
  · 状态必须诚实：见文件末尾 `VERIFICATION` —— verified 只写在
    「用真实 Key 真跑通过」的那几家上，其余一律 implemented（代码在、未实测）。

各家协议依据（都是从厂商官方文档/实测契约来的，不凭记忆编）：
  · openai 系    POST {base}{path}  Authorization: Bearer
                 body {model,prompt,n,size}；返回 data[0].url 或 b64_json
                 （gpt-image-1 **不接受** response_format，永远回 b64）
  · siliconflow  POST /v1/images/generations；参数是 `image_size`（不是 size）
                 / `batch_size`；返回 images[0].url（也有 data 形态）
                 ★ 编辑模型（Qwen-Image-Edit）必须带 `image`（data URI）
  · ark 即梦     POST /api/v3/images/generations；body 要 `response_format:"url"`
                 与 `watermark`；返回 data[0].url
  · zhipu CogView POST /images/generations（v4 base）；返回 data[0].url
  · gemini Imagen POST /v1beta/models/{model}:predict，头 `x-goog-api-key`
                 body {instances:[{prompt}],parameters:{sampleCount}}
                 返回 predictions[0].bytesBase64Encoded
                 ★ gemini-*-flash-image 不是 predict 模型，走 :generateContent
                   + generationConfig.responseModalities=["IMAGE"]
  · wanx（DashScope）POST .../text2image/image-synthesis
                 header `X-DashScope-Async: enable`；返回 output.task_id
                 → 轮询 GET {base}/tasks/{id}（SUCCEEDED/FAILED/CANCELED）
  · qwen（DashScope 多模态）POST .../multimodal-generation/generation
                 body input.messages[*].content[*].text
                 同步形态回 output.choices[0].message.content[0].image；
                 异步形态回 task_id（两种都兼容）
  · minimax      POST /v1/image_generation；返回 data.image_urls / image_base64
                 并且**必须**检查 base_resp.status_code（200 里也会有业务错误）
  · stability    POST /v2beta/stable-image/generate/core（sd3 系列走 /sd3）
                 multipart 表单 + Accept: image/* → 直接回图片二进制
  · local ComfyUI POST /prompt（工作流 JSON）→ 轮询 GET /history/{id}
                 → GET /view?filename=... 取图。没有 checkpoint 时如实报错。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

#: 注册表是**只读**的（本模块只读它，绝不改它）
try:  # 正常路径：作为 core 包的子模块被导入
    from . import provider_registry as registry
except ImportError:  # pragma: no cover - 允许直接把 backend/core 塞进 sys.path
    import provider_registry as registry  # type: ignore


logger = logging.getLogger("videoforge.image_providers")


# ─────────────────────────── 常量 / 分类 ───────────────────────────

#: 走 OpenAI 兼容 `images/generations` 形状的四家（请求体各有小差异）
OPENAI_FAMILY = ("openai", "siliconflow", "zhipu", "ark")
#: DashScope 两条线（异步任务 + 轮询）
DASHSCOPE = ("wanx", "qwen")
#: 各自原生协议
NATIVE = ("minimax", "stability")
#: 本地 ComfyUI
LOCAL = ("local",)

#: 协议 → 平台集合（**从注册表现算**，注册表新增平台时不用改这里）
_BY_PROTOCOL: Dict[str, List[str]] = {}
for _p in registry.IMAGE_PROVIDERS:
    _BY_PROTOCOL.setdefault(str(_p.get("protocol") or ""), []).append(str(_p.get("id")))

#: OpenAI 兼容协议的**全部**平台（含中转站 agentai 这类后加的）
OPENAI_PROTOCOL = tuple(_BY_PROTOCOL.get("openai", []))

#: 本地地址清单：这些**必须**绕开系统代理（实测被代理接管就超时）
LOCAL_HOSTS = ("127.0.0.1", "localhost", "0.0.0.0", "::1")

#: 各家提示词硬上限（超了上游会整条拒掉；有依据的才写）
PROMPT_MAX = {"minimax": 1500}

#: 从 settings 里找 Key 的三个桶（与设置页一致）
_KEY_BUCKETS = ("llm_api_keys", "api_keys", "tts_api_keys")

#: provider_id → 可能存 Key 的名字（大小写不敏感匹配）
_KEY_ALIASES: Dict[str, Tuple[str, ...]] = {
    "openai": ("openai",),
    "siliconflow": ("siliconflow", "silicon", "硅基流动"),
    "zhipu": ("zhipu", "glm", "bigmodel", "智谱", "cogview"),
    "ark": ("ark", "volc", "doubao", "jimeng", "即梦", "seedream"),
    "gemini": ("gemini", "google", "google_api_key"),
    "wanx": ("wanx", "dashscope", "qwen", "bailian", "万相"),
    "qwen": ("qwen", "dashscope", "wanx", "bailian"),
    "minimax": ("minimax",),
    "stability": ("stability", "stabilityai", "stability_ai"),
    # local 不强制 Key（ComfyUI 默认不鉴权），留别名只为吸收用户显式填的值
    "local": ("comfyui", "comfy", "local"),
    # 中转站（OpenAI 兼容）：设置里那把 Key 存在 llm_api_keys["openai"] /
    # api_keys["seedance_t6"]（同一把中转 Key），所以这里带上 openai 别名，
    # 否则「配了却用不上」会再发生一次。
    "agentai": ("agentai", "token6688", "openai"),
}

#: 环境变量兜底（与注册表 env 字段呼应）
_ENV_KEYS: Dict[str, Tuple[str, ...]] = {
    "openai": ("OPENAI_API_KEY",),
    "siliconflow": ("SILICONFLOW_API_KEY",),
    "zhipu": ("ZHIPU_API_KEY", "BIGMODEL_API_KEY"),
    "ark": ("ARK_API_KEY", "VOLC_ACCESSKEY"),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "wanx": ("DASHSCOPE_API_KEY",),
    "qwen": ("DASHSCOPE_API_KEY",),
    "minimax": ("MINIMAX_API_KEY",),
    "stability": ("STABILITY_API_KEY",),
    "local": ("COMFYUI_API_KEY",),
    "agentai": ("AGENTAI_API_KEY", "OPENAI_API_KEY"),
}

#: 不需要 Key 的（本地服务；填了也照发）
NO_KEY_REQUIRED = ("local",)


# ─────────────────────────── 错误与判定 ───────────────────────────


class ImageProviderError(Exception):
    """统一归一化后的图像生成失败。

    调用方只需读 `str(e)`；排查时可读 `status` / `raw` / `stage` / `hint`。
    `raw` 保留厂商响应前 200 字 —— 报错时**不许吞掉上游原话**。
    """

    def __init__(self, message: str, *, status: int = 0, raw: str = "",
                 stage: str = "", hint: str = "") -> None:
        self.status = int(status or 0)
        self.raw = str(raw or "")[:200]
        self.stage = str(stage or "")
        self.hint = str(hint or "")
        text = str(message)
        if self.raw and self.raw not in text:
            text = f"{text}（上游原文：{self.raw}）"
        if self.hint:
            text = f"{text}\n{self.hint}"
        super().__init__(text)

    def as_dict(self) -> Dict[str, Any]:
        return {"error": str(self), "status": self.status,
                "raw": self.raw, "stage": self.stage, "hint": self.hint}


def _clip_status(r: "httpx.Response", provider_label: str) -> ImageProviderError:
    """把 HTTP >=400 归一化成 ImageProviderError（带状态码 + 原文前 200 字）。"""
    snippet = ""
    try:
        snippet = (r.text or "")[:200].replace("\n", " ")
    except Exception:  # noqa: BLE001 - 读不到 body 也不能让报错本身炸掉
        snippet = "<响应体不可读>"
    return ImageProviderError(
        f"{provider_label} 返回 HTTP {r.status_code}",
        status=r.status_code, raw=snippet, stage="http")


def _clip_json_error(provider_label: str, data: Any, status: int = 200) -> ImageProviderError:
    """厂商 200 但业务失败（MiniMax base_resp / DashScope task_status / Comfy node_errors）。"""
    try:
        raw = json.dumps(data, ensure_ascii=False)[:200]
    except Exception:  # noqa: BLE001
        raw = str(data)[:200]
    return ImageProviderError(f"{provider_label} 业务失败", status=status,
                              raw=raw, stage="business")


# ─────────────────────────── 工具 ───────────────────────────


def _is_local_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False
    return host in LOCAL_HOSTS


def _new_client(url: str, timeout: float) -> httpx.AsyncClient:
    """按目标地址决定要不要读系统代理。

    ★ 实测教训：系统代理开着时 httpx 会把 127.0.0.1 也丢给代理，
      本地服务明明起着却连接超时 —— 本地地址必须 trust_env=False。
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=20.0, read=max(30.0, timeout),
                              write=60.0, pool=10.0),
        trust_env=not _is_local_url(url),
    )


def _size_wh(size: str) -> Tuple[int, int]:
    """'1024x1024' / '1024*1024' / '1024X1024' → (1024, 1024)。解析不了回 1024x1024。"""
    text = str(size or "").lower().replace("*", "x").replace(" ", "")
    try:
        w, h = text.split("x", 1)
        w, h = int(float(w)), int(float(h))
        if w > 0 and h > 0:
            return w, h
    except Exception:  # noqa: BLE001
        pass
    return 1024, 1024


def _dashscope_size(size: str) -> str:
    w, h = _size_wh(size)
    return f"{w}*{h}"


def _aspect_ratio(size: str) -> str:
    """把 WxH 映射到厂商支持的画幅枚举（取最接近的）。"""
    w, h = _size_wh(size)
    ratio = w / float(h)
    table = [("1:1", 1.0), ("16:9", 16 / 9), ("9:16", 9 / 16), ("4:3", 4 / 3),
             ("3:4", 3 / 4), ("3:2", 3 / 2), ("2:3", 2 / 3)]
    return min(table, key=lambda kv: abs(kv[1] - ratio))[0]


def _fit_prompt(prompt: str, limit: int) -> Tuple[str, str]:
    """把提示词修剪到厂商上限内（保住开头＝长相/构图，砍掉后面的铺垫）。"""
    p = str(prompt or "")
    if not limit or len(p) <= limit:
        return p, ""
    import re as _re
    parts = [x for x in _re.split(r"(?<=[。；;\.\!\?\n])", p) if x and x.strip()]
    out = ""
    for seg in parts:
        if len(out) + len(seg) > limit:
            break
        out += seg
    if not out:
        out = p[:limit]
    return out, (f"提示词原本 {len(p)} 字，超过该模型上限 {limit} 字，"
                 f"已精简到 {len(out)} 字（保留开头设定，砍掉后面铺垫）。")


def _guess_ext(data: bytes, url: str = "", content_type: str = "") -> str:
    ct = (content_type or "").lower()
    if "jpeg" in ct or "jpg" in ct:
        return ".jpg"
    if "webp" in ct:
        return ".webp"
    low = (url or "").lower().split("?")[0]
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        if low.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    # 魔数兜底：PNG/JPEG/WebP 的头
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:2] == b"\xff\xd8":
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".png"


def _b64_to_bytes(b64: str) -> bytes:
    text = str(b64 or "").strip()
    if text.startswith("data:"):
        text = text.split(",", 1)[-1]
    return base64.b64decode(text)


def _local_file_to_data_uri(path: str, max_mb: float = 8.0) -> str:
    """参考图 → data URI（硅基流动 `image` 字段接受这种形式）。"""
    try:
        if not path or not os.path.exists(path):
            return ""
        if os.path.getsize(path) / 1048576.0 > max_mb:
            logger.warning("参考图过大（>%.0fMB），跳过：%s", max_mb, path)
            return ""
        ext = (os.path.splitext(path)[1].lstrip(".") or "png").lower()
        if ext == "jpg":
            ext = "jpeg"
        with open(path, "rb") as f:
            return f"data:image/{ext};base64,{base64.b64encode(f.read()).decode()}"
    except Exception as e:  # noqa: BLE001
        logger.warning("参考图转 data URI 失败 %s：%s", path, e)
        return ""


def _ci_lookup(mapping: Any, name: str) -> str:
    """大小写不敏感地在 dict 里找 key。

    ★ 实测：设置库里存的是 `MiniMax`，provider id 是小写 `minimax`；
      精确匹配会漏 → 用户明明配了 Key 却说没配。
    """
    if not isinstance(mapping, dict):
        return ""
    target = str(name or "").strip().lower()
    for k, v in mapping.items():
        if str(k).strip().lower() == target and v:
            return str(v).strip()
    return ""


# ─────────────────────────── Provider 定义 ───────────────────────────

_ENTRY: Dict[str, Dict[str, Any]] = {p["id"]: p for p in registry.IMAGE_PROVIDERS}

#: provider id → 展示名（注册表里的 label，去掉「图像」等后缀也够用）
def provider_ids() -> List[str]:
    """注册表里声明的全部图像平台 id（顺序保持注册表顺序）。"""
    return [p["id"] for p in registry.IMAGE_PROVIDERS]


def provider_entry(provider_id: str) -> Optional[Dict[str, Any]]:
    return _ENTRY.get(_normalize_provider_id(provider_id))


def _normalize_provider_id(provider_id: str) -> str:
    """把用户/设置里写的名字规范成注册表 id（含常见别名）。"""
    raw = str(provider_id or "").strip()
    low = raw.lower()
    aliases = {
        "jimeng": "ark", "即梦": "ark", "doubao": "ark", "seedream": "ark",
        "volc": "ark", "volcengine": "ark",
        "google": "gemini", "imagen": "gemini",
        "dashscope": "wanx", "bailian": "wanx", "百炼": "wanx",
        "cogview": "zhipu", "glm": "zhipu", "bigmodel": "zhipu",
        "comfyui": "local", "comfy": "local", "本机": "local", "本地": "local",
        "stabilityai": "stability", "stability_ai": "stability",
        "硅基流动": "siliconflow",
    }
    return aliases.get(low, low)


def resolve_key(provider_id: str, settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """找这个平台的 API Key，并**如实说明**从哪来的 / 为什么没找到。

    查找顺序（先显式、再通用、最后环境变量）：
      1. `settings["image_api_key_<provider>"]`（设置页的单模型覆盖位）
      2. `llm_api_keys` / `api_keys` / `tts_api_keys` 三个桶 × 别名（大小写不敏感）
      3. 环境变量（注册表 env 字段 / 各家标准变量名）

    :returns: {"key": str, "source": str, "required": bool, "checked": [..], "error": str}
    """
    pid = _normalize_provider_id(provider_id)
    settings = settings or {}
    required = pid not in NO_KEY_REQUIRED
    checked: List[str] = []

    # ① 显式单模型位
    explicit = str(settings.get(f"image_api_key_{pid}") or "").strip()
    if explicit:
        return {"key": explicit, "source": f"settings.image_api_key_{pid}",
                "required": required, "checked": checked, "error": ""}

    # ② 三个桶 × 别名
    aliases = _KEY_ALIASES.get(pid, (pid,))
    for bucket in _KEY_BUCKETS:
        checked.append(bucket)
        found = _ci_lookup(settings.get(bucket) or {}, pid)
        if found:
            return {"key": found, "source": f"{bucket}.{pid}",
                    "required": required, "checked": checked, "error": ""}
        for alias in aliases:
            found = _ci_lookup(settings.get(bucket) or {}, alias)
            if found:
                return {"key": found, "source": f"{bucket}.{alias}",
                        "required": required, "checked": checked, "error": ""}

    # ③ 环境变量
    for env_name in _ENV_KEYS.get(pid, ()):
        if os.environ.get(env_name):
            return {"key": str(os.environ[env_name]).strip(),
                    "source": f"env.{env_name}", "required": required,
                    "checked": checked, "error": ""}

    if not required:
        return {"key": "", "source": "", "required": False,
                "checked": checked, "error": ""}

    label = (_ENTRY.get(pid) or {}).get("label") or pid
    env_hint = " / ".join(_ENV_KEYS.get(pid, ())) or "（无环境变量约定）"
    return {
        "key": "", "source": "", "required": True, "checked": checked,
        "error": (f"未配置「{label}」的 API Key：请到「设置 → 对话 API Keys」"
                  f"（桶 {(_ENTRY.get(pid) or {}).get('key_field') or 'api_keys'}，"
                  f"键名 {pid}）填写，或设环境变量 {env_hint}。"),
    }


def resolve_base_url(provider_id: str, settings: Optional[Dict[str, Any]] = None,
                     override: str = "") -> str:
    """解析请求根地址：参数 > settings 覆盖 > 注册表默认。

    支持 `settings["image_base_url_<provider>"]` 与统一的
    `settings["image_base_url"]`（后者只在单模型位没写时生效），
    方便走中转站/自建网关 —— 这也是本地假服务测试的入口。
    """
    pid = _normalize_provider_id(provider_id)
    if override:
        return str(override).rstrip("/")
    settings = settings or {}
    for key in (f"image_base_url_{pid}", "image_base_url"):
        val = str(settings.get(key) or "").strip()
        if val:
            return val.rstrip("/")
    return str((_ENTRY.get(pid) or {}).get("base_url") or "").rstrip("/")


def provider_status(provider_id: str) -> Dict[str, Any]:
    """这个平台**到底验没验过**（诚实标注，不把 implemented 说成 verified）。"""
    pid = _normalize_provider_id(provider_id)
    return dict(VERIFICATION.get(pid, {
        "status": "implemented", "verification": "代码已实现，未用真实 Key 验证",
    }))


def list_providers(settings: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """设置页/诊断用：10 家平台的可用性与实现状态。

    与 `imagegen.list_image_providers()`（旧 UI 列表）不同，这里**只报告**，
    不改变旧列表的顺序与字段，避免影响现有前端。
    """
    rows: List[Dict[str, Any]] = []
    for entry in registry.IMAGE_PROVIDERS:
        pid = entry["id"]
        key_info = resolve_key(pid, settings or {})
        status = provider_status(pid)
        usable = (bool(key_info["key"]) or not key_info["required"])
        rows.append({
            "id": pid,
            "label": entry.get("label") or pid,
            "protocol": entry.get("protocol") or "",
            "base_url": entry.get("base_url") or "",
            "path": entry.get("path") or "",
            "default_model": entry.get("default_model") or "",
            "models": [dict(m) for m in (entry.get("models") or [])],
            "key_field": entry.get("key_field") or "",
            "has_key": bool(key_info["key"]),
            "key_source": key_info["source"],
            "requires_api_key": bool(key_info["required"]),
            "usable": usable,
            "status": status.get("status", "implemented"),
            "verification": status.get("verification", ""),
            "notes": entry.get("notes") or "",
            "unusable_reason": "" if usable else key_info["error"],
        })
    return rows


# ─────────────────────────── 各家请求实现 ───────────────────────────


async def _openai_family(client: httpx.AsyncClient, pid: str, url: str, key: str,
                         prompt: str, model: str, size: str, n: int,
                         negative_prompt: str, reference_images: List[str],
                         settings: Dict[str, Any]) -> Dict[str, Any]:
    """OpenAI 兼容的 `images/generations` 四家。

    ★ 为什么不能一把梭：四家的请求体**真的不一样** ——
      · openai:      {model,prompt,n,size}；gpt-image-1 不接受 response_format
      · siliconflow: `image_size` / `batch_size`（发 size 会被忽略，尺寸不生效）
      · ark:         必须 response_format=url，且带 watermark
      · zhipu:       {model,prompt,size}，返回 data[0].url
    """
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    notes: List[str] = []

    if pid == "siliconflow":
        body: Dict[str, Any] = {"model": model, "prompt": prompt,
                                "batch_size": int(n or 1)}
        # 编辑模型不支持 image_size（传了会被忽略/报错），且必须带参考图
        is_edit = "image-edit" in (model or "").lower() or "imageedit" in (model or "").lower()
        if size and not is_edit:
            body["image_size"] = size
        if negative_prompt:
            body["negative_prompt"] = negative_prompt
        for idx, ref_path in enumerate((reference_images or [])[:3]):
            ref = _local_file_to_data_uri(ref_path)
            if not ref:
                continue
            body["image" if idx == 0 else f"image{idx + 1}"] = ref
        if is_edit and not any(k.startswith("image") for k in body):
            raise ImageProviderError(
                f"「{model}」是指令式图像编辑模型，必须给一张参考图才能工作。",
                stage="param",
                hint="请先上传/生成一张图，或改用文生图模型（如 Qwen/Qwen-Image）。")
    elif pid == "ark":
        body = {"model": model, "prompt": prompt, "n": int(n or 1),
                "response_format": "url", "watermark": False}
        if size:
            body["size"] = size
        if negative_prompt:
            # Ark Seedream 没有独立负向词字段；如实告诉调用方被忽略了
            notes.append("ark（即梦/Seedream）不支持 negative_prompt，已忽略")
    elif pid == "zhipu":
        body = {"model": model, "prompt": prompt, "n": int(n or 1)}
        if size:
            body["size"] = size
        if negative_prompt:
            notes.append("智谱 CogView 不支持 negative_prompt，已忽略")
    else:  # openai 本体
        body = {"model": model, "prompt": prompt, "n": int(n or 1)}
        if size:
            body["size"] = size
        # gpt-image-* 永远返回 b64，且显式传 response_format 会 400
        if not (model or "").lower().startswith("gpt-image"):
            body["response_format"] = "url"
        if negative_prompt:
            notes.append("OpenAI 图像不支持 negative_prompt，已忽略")

    r = await client.post(url, headers=headers, json=body)
    if r.status_code >= 400:
        raise _clip_status(r, f"{pid} 图像生成")
    try:
        data = r.json()
    except Exception as e:  # noqa: BLE001
        raise ImageProviderError(f"{pid} 返回的不是 JSON", stage="parse",
                                 raw=(r.text or "")[:200]) from e

    # 硅基流动回 images[]，其余回 data[]（两种都兼容，别假设只有一种）
    items = []
    if isinstance(data, dict):
        items = data.get("images") or data.get("data") or []
    if not items:
        raise _clip_json_error(f"{pid} 未返回图片", data)
    first = items[0] if isinstance(items[0], dict) else {"url": str(items[0])}
    urls = [u for u in [first.get("url")] if u]
    b64 = first.get("b64_json") or first.get("image_base64") or ""
    if not urls and not b64:
        raise _clip_json_error(f"{pid} 返回里既没有 url 也没有 b64", first)
    return {"urls": urls, "b64": b64, "raw": data, "notes": notes}


async def _gemini(client: httpx.AsyncClient, base: str, key: str, prompt: str,
                  model: str, size: str, n: int,
                  settings: Dict[str, Any]) -> Dict[str, Any]:
    """Google Imagen / Gemini 图像。

    ★ 注册表里 path 写的是 `:predict`，但 **gemini-2.5-flash-image 不是
      predict 模型** —— 它走 `:generateContent` + responseModalities=["IMAGE"]。
      把两者混成一个 URL 的话，选 flash-image 必失败，所以这里按模型分流。
    """
    headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
    is_imagen = (model or "").lower().startswith("imagen")
    mode = str(settings.get("gemini_image_mode") or "").strip().lower()
    if not mode:
        mode = "predict" if is_imagen else "generateContent"

    if mode == "predict":
        url = f"{base}/v1beta/models/{model}:predict"
        body = {"instances": [{"prompt": prompt}],
                "parameters": {"sampleCount": int(n or 1)}}
        r = await client.post(url, headers=headers, json=body)
        if r.status_code >= 400:
            raise _clip_status(r, "Gemini Imagen")
        data = r.json() or {}
        preds = data.get("predictions") or []
        if not preds:
            raise _clip_json_error("Gemini Imagen 未返回 predictions", data)
        first = preds[0] or {}
        b64 = first.get("bytesBase64Encoded") or first.get("bytes_base64_encoded") or ""
        if not b64:
            reason = first.get("raiFilteredReason") or first.get("rai_filtered_reason") or ""
            raise ImageProviderError("Gemini Imagen 没有返回图片字节",
                                     stage="business",
                                     raw=json.dumps(first, ensure_ascii=False)[:200],
                                     hint=(f"内容审核拦截：{reason}" if reason else ""))
        return {"urls": [], "b64": b64, "raw": data, "notes": []}

    # generateContent 形态（gemini-*-image）
    url = f"{base}/v1beta/models/{model}:generateContent"
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}}
    r = await client.post(url, headers=headers, json=body)
    if r.status_code >= 400:
        raise _clip_status(r, "Gemini 图像")
    data = r.json() or {}
    candidates = data.get("candidates") or []
    parts = (((candidates[0] or {}).get("content") or {}).get("parts") or []) if candidates else []
    b64 = ""
    for part in parts:
        inline = (part or {}).get("inlineData") or (part or {}).get("inline_data") or {}
        if inline.get("data"):
            b64 = inline["data"]
            break
    if not b64:
        raise _clip_json_error("Gemini 图像没有返回 inlineData", data)
    return {"urls": [], "b64": b64, "raw": data, "notes": []}


async def _dashscope(client: httpx.AsyncClient, pid: str, base: str, path: str,
                     key: str, prompt: str, model: str, size: str, n: int,
                     negative_prompt: str, timeout: float,
                     settings: Dict[str, Any]) -> Dict[str, Any]:
    """阿里云百炼：异步任务 + 轮询（wanx 文生图 / qwen 多模态出图）。"""
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "X-DashScope-Async": "enable"}
    if pid == "wanx":
        body: Dict[str, Any] = {
            "model": model,
            "input": {"prompt": prompt},
            "parameters": {"size": _dashscope_size(size), "n": int(n or 1)},
        }
        if negative_prompt:
            body["input"]["negative_prompt"] = negative_prompt
    else:  # qwen（Qwen-Image，多模态生成协议）
        params: Dict[str, Any] = {"n": int(n or 1), "prompt_extend": True,
                                  "watermark": False, "size": _dashscope_size(size)}
        if negative_prompt:
            params["negative_prompt"] = negative_prompt
        body = {
            "model": model,
            "input": {"messages": [{"role": "user",
                                    "content": [{"text": prompt}]}]},
            "parameters": params,
        }

    url = f"{base}{path}"
    r = await client.post(url, headers=headers, json=body)
    if r.status_code >= 400:
        raise _clip_status(r, f"{pid} 创建任务")
    try:
        data = r.json()
    except Exception as e:  # noqa: BLE001
        raise ImageProviderError(f"{pid} 创建任务返回非 JSON", stage="parse",
                                 raw=(r.text or "")[:200]) from e
    out = (data or {}).get("output") or {}

    # ① 同步形态（qwen-image 有这条路径）：结果直接在 choices 里
    choices = out.get("choices") or []
    if choices:
        content = (((choices[0] or {}).get("message") or {}).get("content") or [])
        for item in content:
            if isinstance(item, dict) and item.get("image"):
                return {"urls": [item["image"]], "b64": "", "raw": data, "notes": []}
        raise _clip_json_error(f"{pid} 同步结果里没有 image", data)
    # ② 也有直接给 results 的
    if out.get("results"):
        urls = [x.get("url") for x in out["results"] if isinstance(x, dict) and x.get("url")]
        if urls:
            return {"urls": urls, "b64": "", "raw": data, "notes": []}

    task_id = out.get("task_id")
    if not task_id:
        raise _clip_json_error(f"{pid} 未返回 task_id", data)

    poll = float(settings.get("image_poll_interval") or 3.0)
    poll = max(0.01, poll)
    deadline = time.time() + max(5.0, float(timeout or 180.0))
    task_url = f"{base}/tasks/{task_id}"
    while time.time() < deadline:
        await asyncio.sleep(poll)
        tr = await client.get(task_url, headers={"Authorization": f"Bearer {key}"})
        if tr.status_code >= 400:
            raise _clip_status(tr, f"{pid} 查询任务")
        try:
            td = tr.json() or {}
        except Exception:  # noqa: BLE001
            continue
        tout = td.get("output") or {}
        status = str(tout.get("task_status") or "")
        if status == "SUCCEEDED":
            results = tout.get("results") or []
            urls = [x.get("url") for x in results if isinstance(x, dict) and x.get("url")]
            if not urls:
                raise _clip_json_error(f"{pid} 任务成功但没有图片", td)
            return {"urls": urls, "b64": "", "raw": td, "notes": []}
        if status in ("FAILED", "CANCELED", "UNKNOWN"):
            raise ImageProviderError(
                f"{pid} 任务失败（task_status={status}）", stage="task",
                raw=json.dumps(tout, ensure_ascii=False)[:200],
                hint=str(tout.get("message") or tout.get("code") or ""))
    raise ImageProviderError(f"{pid} 任务超时（{timeout:.0f} 秒内没等到结果）",
                             stage="timeout",
                             hint="异步出图通常 10~60 秒；请检查网络/配额后重试。")


async def _minimax(client: httpx.AsyncClient, base: str, path: str, key: str,
                   prompt: str, model: str, size: str, n: int,
                   timeout: float) -> Dict[str, Any]:
    """MiniMax 图像（同步返回 url / base64）。"""
    body: Dict[str, Any] = {
        "model": model, "prompt": prompt, "n": int(n or 1),
        "response_format": "url", "prompt_optimizer": True,
        "aspect_ratio": _aspect_ratio(size),
    }
    r = await client.post(f"{base}{path}", json=body,
                          headers={"Authorization": f"Bearer {key}",
                                   "Content-Type": "application/json"})
    if r.status_code >= 400:
        raise _clip_status(r, "MiniMax 图像")
    data = r.json() or {}
    # ★ MiniMax 的特色：HTTP 200 里也可能有业务错误（必须查 base_resp）
    base_resp = data.get("base_resp") or {}
    code = int(base_resp.get("status_code") or 0)
    if code != 0:
        msg = base_resp.get("status_msg") or ""
        raise ImageProviderError(
            f"MiniMax 图像失败 [{code}] {msg}", stage="business",
            raw=json.dumps(data, ensure_ascii=False)[:200])
    payload = data.get("data") or {}
    urls = [u for u in (payload.get("image_urls") or []) if u]
    b64s = [x for x in (payload.get("image_base64") or []) if x]
    if urls:
        return {"urls": urls, "b64": "", "raw": data, "notes": []}
    if b64s:
        return {"urls": [], "b64": b64s[0], "raw": data, "notes": []}
    raise _clip_json_error("MiniMax 未返回图片", data)


async def _stability(client: httpx.AsyncClient, base: str, path: str, key: str,
                     prompt: str, model: str, size: str, n: int,
                     negative_prompt: str) -> Dict[str, Any]:
    """Stability AI：multipart 表单，`Accept: image/*` 时直接回图片二进制。"""
    # sd3 系列走 /sd3（注册表 path 写的是 core），core 端点不接受 model 字段
    is_sd3 = (model or "").lower().startswith("sd3")
    url = f"{base}/v2beta/stable-image/generate/sd3" if is_sd3 else f"{base}{path}"
    form: Dict[str, Any] = {
        "prompt": (None, prompt),
        "output_format": (None, "png"),
        "aspect_ratio": (None, _aspect_ratio(size)),
    }
    if is_sd3:
        form["model"] = (None, model)
    if negative_prompt:
        form["negative_prompt"] = (None, negative_prompt)
    headers = {"Authorization": f"Bearer {key}", "Accept": "image/*"}
    r = await client.post(url, headers=headers, files=form,
                          timeout=httpx.Timeout(connect=20.0, read=180.0,
                                                write=60.0, pool=10.0))
    if r.status_code >= 400:
        raise _clip_status(r, "Stability AI")
    ctype = (r.headers.get("content-type") or "").lower()
    if ctype.startswith("image/"):
        return {"urls": [], "bytes": r.content, "raw": {}, "notes": []}
    # 万一网关把它包成 JSON（中转站常见）：{image: b64} / {errors:[...]}
    try:
        data = r.json() or {}
    except Exception as e:  # noqa: BLE001
        raise ImageProviderError("Stability 返回既不是图片也不是 JSON",
                                 stage="parse", raw=(r.text or "")[:200]) from e
    if data.get("image"):
        return {"urls": [], "b64": data["image"], "raw": data, "notes": []}
    raise _clip_json_error("Stability AI 未返回图片", data)


def _comfy_workflow(ckpt: str, prompt: str, negative: str, size: str,
                    settings: Dict[str, Any]) -> Dict[str, Any]:
    """标准 txt2img 工作流（ComfyUI /prompt 的 API 格式）。

    ★ 为什么在代码里**建图**而不是让用户先导出工作流：
      设置页的「本地」是一个"开箱即用"的选项，要求用户先去 ComfyUI 学
      怎么存 API 格式的 workflow，等于把这个选项变成摆设。
      给了 `comfy_workflow`（JSON 路径或 dict）时就用用户的图，只注入提示词。
    """
    custom = settings.get("comfy_workflow")
    if custom:
        wf: Optional[Dict[str, Any]] = None
        try:
            if isinstance(custom, dict):
                wf = json.loads(json.dumps(custom))
            elif os.path.exists(str(custom)):
                with open(str(custom), "r", encoding="utf-8") as f:
                    wf = json.load(f)
        except Exception as e:  # noqa: BLE001
            raise ImageProviderError(f"读取 comfy_workflow 失败：{e}", stage="param")
        if isinstance(wf, dict):
            node_id = str(settings.get("comfy_prompt_node") or "6")
            node = wf.get(node_id) or {}
            node.setdefault("inputs", {})["text"] = prompt
            if negative and str(settings.get("comfy_negative_node") or "") in wf:
                neg_node = wf[str(settings["comfy_negative_node"])]
                neg_node.setdefault("inputs", {})["text"] = negative
            return wf

    w, h = _size_wh(size)
    # ComfyUI 的 latent 尺寸要求 8 的倍数
    w, h = max(64, (w // 8) * 8), max(64, (h // 8) * 8)
    seed = int(settings.get("comfy_seed") or 0) or (int(time.time() * 1000) % 2147483647)
    steps = int(settings.get("comfy_steps") or 20)
    cfg = float(settings.get("comfy_cfg") or 5.0)
    return {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "6": {"class_type": "CLIPTextEncode",
              "inputs": {"text": prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode",
              "inputs": {"text": negative or "", "clip": ["4", 1]}},
        "5": {"class_type": "EmptyLatentImage",
              "inputs": {"width": w, "height": h, "batch_size": 1}},
        "3": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": steps, "cfg": cfg,
                         "sampler_name": str(settings.get("comfy_sampler") or "euler"),
                         "scheduler": "normal", "denoise": 1.0,
                         "model": ["4", 0], "positive": ["6", 0],
                         "negative": ["7", 0], "latent_image": ["5", 0]}},
        "8": {"class_type": "VAEDecode",
              "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": "videoforge", "images": ["8", 0]}},
    }


async def _comfyui(client: httpx.AsyncClient, base: str, key: str, prompt: str,
                   negative: str, size: str, timeout: float,
                   settings: Dict[str, Any]) -> Dict[str, Any]:
    """本地 ComfyUI：`POST /prompt` → 轮询 `/history/{id}` → `GET /view` 取图。"""
    headers: Dict[str, str] = {}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    # ① 先探活：没起就别让用户白等（ComfyUI 冷启动也可能几十秒，故给明确指引）
    try:
        hc = await client.get(f"{base}/system_stats", headers=headers, timeout=10.0)
        if hc.status_code >= 400:
            raise _clip_status(hc, "本地 ComfyUI")
    except ImageProviderError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ImageProviderError(
            f"本地 ComfyUI 连不上（{type(e).__name__}: {e}）", stage="health",
            hint=("ComfyUI 没启动或端口不对。默认地址 http://127.0.0.1:8188；"
                  "启动后在「设置 → 图像」把 base_url 指到它。")) from e

    # ② 拿一个 checkpoint（用户没指定就取第一个可用的，别让选项变摆设）
    ckpt = str(settings.get("comfy_checkpoint") or "").strip()
    if not ckpt:
        try:
            info = await client.get(f"{base}/object_info/CheckpointLoaderSimple",
                                    headers=headers)
            data = info.json() if info.status_code < 400 else {}
            node = (data or {}).get("CheckpointLoaderSimple") or {}
            required = ((node.get("input") or {}).get("required") or {})
            spec = required.get("ckpt_name") or []
            options = spec[0] if spec and isinstance(spec[0], list) else []
            ckpt = str(options[0]) if options else ""
        except Exception as e:  # noqa: BLE001
            logger.info("读取 ComfyUI checkpoint 列表失败：%s", e)
        if not ckpt:
            raise ImageProviderError(
                "ComfyUI 里没有可用的 checkpoint", stage="param",
                hint=("把模型放进 ComfyUI/models/checkpoints/ 后重试，"
                      "或在设置里写 comfy_checkpoint=<文件名>。"))

    # ③ 提交工作流
    wf = _comfy_workflow(ckpt, prompt, negative, size, settings)
    client_id = str(uuid.uuid4())
    r = await client.post(f"{base}/prompt", headers=headers,
                          json={"prompt": wf, "client_id": client_id})
    if r.status_code >= 400:
        raise _clip_status(r, "本地 ComfyUI 提交工作流")
    try:
        data = r.json() or {}
    except Exception as e:  # noqa: BLE001
        raise ImageProviderError("ComfyUI /prompt 返回非 JSON", stage="parse",
                                 raw=(r.text or "")[:200]) from e
    if data.get("node_errors") or data.get("error"):
        raise _clip_json_error("ComfyUI 工作流被拒", data)
    prompt_id = data.get("prompt_id") or ""
    if not prompt_id:
        raise _clip_json_error("ComfyUI 未返回 prompt_id", data)

    # ④ 轮询历史
    poll = max(0.01, float(settings.get("image_poll_interval") or 2.0))
    deadline = time.time() + max(10.0, float(timeout or 600.0))
    item: Dict[str, Any] = {}
    while time.time() < deadline:
        await asyncio.sleep(poll)
        hr = await client.get(f"{base}/history/{prompt_id}", headers=headers)
        if hr.status_code >= 400:
            raise _clip_status(hr, "本地 ComfyUI 查询历史")
        try:
            hist = hr.json() or {}
        except Exception:  # noqa: BLE001
            continue
        entry = hist.get(prompt_id) or {}
        if entry.get("outputs"):
            item = entry
            break
        st = (entry.get("status") or {})
        if st.get("status_str") == "error":
            raise _clip_json_error("ComfyUI 执行报错", st)
    if not item:
        raise ImageProviderError(f"本地 ComfyUI 出图超时（{timeout:.0f} 秒）",
                                 stage="timeout",
                                 hint="本机出图正常要 2~3 分钟；冷启动首次更久。")

    # ⑤ 取第一张 SaveImage 产物
    images: List[Dict[str, Any]] = []
    for node_out in (item.get("outputs") or {}).values():
        for img in ((node_out or {}).get("images") or []):
            if isinstance(img, dict) and img.get("filename"):
                images.append(img)
    if not images:
        raise _clip_json_error("ComfyUI 没有产出图片", item)
    first = images[0]
    vr = await client.get(f"{base}/view", headers=headers, params={
        "filename": first.get("filename"),
        "subfolder": first.get("subfolder") or "",
        "type": first.get("type") or "output",
    })
    if vr.status_code >= 400:
        raise _clip_status(vr, "本地 ComfyUI 取图")
    return {"urls": [], "bytes": vr.content, "raw": {"prompt_id": prompt_id,
                                                    "image": first}, "notes": []}


# ─────────────────────────── 主入口 ───────────────────────────


async def generate_image(
    provider_id: str,
    prompt: str,
    *,
    size: str = "1024x1024",
    settings: Optional[Dict[str, Any]] = None,
    out_dir: str = "",
    model: str = "",
    timeout: float = 0.0,
    api_key: str = "",
    base_url: str = "",
    reference_images: Optional[List[str]] = None,
    negative_prompt: str = "",
    n: int = 1,
) -> Dict[str, Any]:
    """统一出图入口：**10 家注册表平台**都能走这里。

    :param provider_id: 注册表 id（也接受常见别名：jimeng/doubao/comfyui/...）
    :param prompt:      提示词
    :param size:        "1024x1024" / "1024*1024" 都行
    :param settings:    应用设置（取 Key / base_url / 轮询间隔等）；
                        **不传就只用环境变量**，方便脚本/测试
    :param out_dir:     图片落盘目录（空则用 settings.cache_dir 或临时目录）
    :param model:       指定型号；空则用注册表默认
    :param timeout:     整个调用的时间预算（秒）；0 → 按平台默认（本地 600，其余 180）
    :param api_key:     显式覆盖 Key（主入口已解析过时用）
    :param base_url:    显式覆盖根地址（测试/自建网关用）
    :param reference_images: 参考图本地路径（仅 siliconflow 编辑模型等支持）
    :param negative_prompt: 负向词（不支持的平台会如实说明被忽略）
    :returns: {ok, path|urls, provider, model, latency_ms, error, ...}
    """
    t0 = time.time()
    settings = dict(settings or {})
    pid = _normalize_provider_id(provider_id)
    entry = _ENTRY.get(pid)

    def _fail(err: ImageProviderError) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "ok": False, "path": "", "urls": [], "provider": pid,
            "model": model or (entry.get("default_model") if entry else ""),
            "prompt": prompt,
            "latency_ms": int((time.time() - t0) * 1000),
        }
        payload.update(err.as_dict())
        return payload

    if entry is None:
        return _fail(ImageProviderError(
            f"不支持的图像平台：{provider_id}", stage="param",
            hint=f"可选：{', '.join(provider_ids())}"))

    # 型号：显式 > 注册表默认
    used_model = str(model or entry.get("default_model") or "")
    notes: List[str] = []

    # 提示词上限（有依据的才写；minimax 1500 是实测被 2013 拒出来的）
    limit = PROMPT_MAX.get(pid, 0)
    prompt, prompt_note = _fit_prompt(prompt, limit)
    if prompt_note:
        notes.append(prompt_note)

    # Key
    if not api_key:
        key_info = resolve_key(pid, settings)
        api_key = key_info["key"]
        if not api_key and key_info["required"]:
            return _fail(ImageProviderError(key_info["error"], stage="auth"))

    base = resolve_base_url(pid, settings, override=base_url)
    if not base:
        return _fail(ImageProviderError(f"「{pid}」没有可用的 base_url", stage="param"))
    path = str(entry.get("path") or "")
    # 超时：本地默认更长（云端 180 秒够；本地实测单张 141~180 秒，留 600）
    if not timeout or timeout <= 0:
        timeout = 600.0 if pid in LOCAL else 180.0

    client_url = f"{base}{path.replace('{model}', used_model)}"
    protocol = str(entry.get("protocol") or "")
    try:
        async with _new_client(client_url, timeout) as client:
            # ★ 分派按**协议**，不按硬编码 id：注册表后加的 OpenAI 兼容平台
            #   （如中转站 agentai）自动走同一条实现，不会再出现「新平台=没实现」。
            if pid in OPENAI_FAMILY or (protocol == "openai" and pid not in DASHSCOPE):
                res = await _openai_family(
                    client, pid, client_url, api_key, prompt, used_model, size,
                    n, negative_prompt, reference_images or [], settings)
            elif pid == "gemini" or protocol == "gemini":
                res = await _gemini(client, base, api_key, prompt, used_model,
                                    size, n, settings)
            elif pid in DASHSCOPE:
                res = await _dashscope(client, pid, base, path, api_key, prompt,
                                       used_model, size, n, negative_prompt,
                                       timeout, settings)
            elif pid == "minimax":
                res = await _minimax(client, base, path, api_key, prompt,
                                     used_model, size, n, timeout)
            elif pid == "stability":
                res = await _stability(client, base, path, api_key, prompt,
                                       used_model, size, n, negative_prompt)
            elif pid in LOCAL:
                res = await _comfyui(client, base, api_key, prompt,
                                     negative_prompt, size, timeout, settings)
            else:  # pragma: no cover - 注册表新增 native 平台但这里没接时会走到
                return _fail(ImageProviderError(
                    f"「{pid}」协议未接入（protocol={entry.get('protocol')}）",
                    stage="param",
                    hint="原生协议需要单独实现；OpenAI 兼容平台会自动复用现有实现。"))

            notes.extend(res.get("notes") or [])
            payload_bytes: bytes = res.get("bytes") or b""
            urls: List[str] = res.get("urls") or []
            if not payload_bytes and res.get("b64"):
                payload_bytes = _b64_to_bytes(res["b64"])
            if not payload_bytes and urls:
                # 远端链接 → 下载（这一家的图片有时效，必须落到本地）
                payload_bytes = await _download_image(client, urls[0])
            if not payload_bytes:
                return _fail(ImageProviderError(
                    f"「{pid}」没有返回可用的图片数据", stage="parse",
                    raw=str(res.get("raw"))[:200]))
    except ImageProviderError as e:
        return _fail(e)
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        return _fail(ImageProviderError(
            f"连不上「{(entry.get('label') or pid)}」({base})：{type(e).__name__}",
            stage="network",
            hint=("本地服务请先启动；云端请检查网络/代理设置。")))
    except Exception as e:  # noqa: BLE001 - 统一兜底，绝不让调用方拿到裸异常
        logger.exception("图像生成异常：%s", e)
        return _fail(ImageProviderError(f"{type(e).__name__}: {e}", stage="unexpected"))

    # 落盘
    dest_root = Path(out_dir or settings.get("cache_dir")
                     or os.path.join(tempfile.gettempdir(), "videoforge-images"))
    try:
        dest_root.mkdir(parents=True, exist_ok=True)
        ext = _guess_ext(payload_bytes, urls[0] if urls else "", "")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = dest_root / f"img-{pid}-{stamp}-{uuid.uuid4().hex[:6]}{ext}"
        dest.write_bytes(payload_bytes)
    except OSError as e:
        return _fail(ImageProviderError(f"图片落盘失败：{e}", stage="save",
                                        hint=f"目标目录：{dest_root}"))

    latency_ms = int((time.time() - t0) * 1000)
    logger.info("图像生成成功：%s(%s) → %s（%.1f 秒, %d 字节）",
                pid, used_model, dest, latency_ms / 1000.0, len(payload_bytes))
    return {
        "ok": True,
        "path": str(dest),
        "filename": dest.name,
        "urls": urls,
        "provider": pid,
        "model": used_model,
        "size": size,
        "bytes": len(payload_bytes),
        "latency_ms": latency_ms,
        "elapsed": round(latency_ms / 1000.0, 2),
        "prompt": prompt,
        "prompt_note": prompt_note,
        "notes": notes,
        "error": "",
        "status": 200,
    }


async def _download_image(client: httpx.AsyncClient, url: str) -> bytes:
    """下载远端图片（链接有时效，必须立刻落本地）。"""
    r = await client.get(url, timeout=httpx.Timeout(connect=20.0, read=120.0,
                                                   write=60.0, pool=10.0))
    if r.status_code >= 400:
        raise _clip_status(r, "下载图片")
    return r.content


async def probe_auth(provider_id: str, *,
                     settings: Optional[Dict[str, Any]] = None,
                     base_url: str = "",
                     timeout: float = 20.0) -> Dict[str, Any]:
    """**不花钱**的鉴权探测：只打 `GET /v1/models`（OpenAI 系）或轻量端点。

    用途：没有把握价格时，先证明 Key 有效/端点可达，而不是冒险付一次生成费。
    诚实边界：探测成功 ≠ 能出图（模型权限、余额都可能另有问题），
    所以返回里 `note` 会写清楚这一点。
    """
    t0 = time.time()
    pid = _normalize_provider_id(provider_id)
    entry = _ENTRY.get(pid)
    if entry is None:
        return {"ok": False, "provider": pid, "error": f"不支持的图像平台：{provider_id}"}
    key_info = resolve_key(pid, settings or {})
    key = key_info["key"]
    if not key and key_info["required"]:
        return {"ok": False, "provider": pid, "error": key_info["error"],
                "latency_ms": 0}
    base = resolve_base_url(pid, settings, override=base_url)
    protocol = str(entry.get("protocol") or "")

    # 各家的"免费鉴权探测"端点（只读，不产生生成费用）
    if pid == "local":
        url = f"{base}/system_stats"
    elif protocol == "openai" or protocol == "gemini":
        url = f"{base}/v1/models"
    elif pid in DASHSCOPE:
        url = f"{base}/models"
    elif pid == "minimax":
        url = f"{base}/v1/models"
    else:
        url = f"{base}"
    headers: Dict[str, str] = {}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if pid == "gemini" and key:
        headers["x-goog-api-key"] = key
    try:
        async with _new_client(url, timeout) as client:
            r = await client.get(url, headers=headers)
        ok = r.status_code < 400
        return {
            "ok": ok, "provider": pid, "status": r.status_code,
            "url": url, "key_source": key_info.get("source") or "",
            "latency_ms": int((time.time() - t0) * 1000),
            "body": (r.text or "")[:200],
            "error": "" if ok else f"HTTP {r.status_code}: {(r.text or '')[:200]}",
            "note": ("探测成功只证明 Key/端点可用；是否计费出图仍需一次真实生成，"
                     "见 VERIFICATION 里的状态标注。"),
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "provider": pid, "url": url,
                "error": f"{type(e).__name__}: {e}",
                "latency_ms": int((time.time() - t0) * 1000)}


# ─────────────────────────── 诚实状态表 ───────────────────────────
#
# ★ 规则（和注册表 status 同一口径）：
#   verified    = 用**真实 Key** 真跑通过一次，并留下了证据（路径/字节数/耗时）
#   implemented = 代码已实现、协议按官方文档写、单元测试用本地假服务验过，
#                 但**没有**真实 Key 实测 —— 不许写成 verified
#
# 2026-10-25 本机实测记录（细节见交付回执）：
#   siliconflow：真实 Key 生成成功（见回执「真实探测」一节）
#   其余：本机没有对应 Key，一律 implemented（不是不能用，是没验）
VERIFICATION: Dict[str, Dict[str, Any]] = {
    "openai": {"status": "implemented",
               "verification": "协议已实现；本机无可用 OpenAI 图像 Key（现有 openai 桶是中转站 Key，"
                               "指向 api.openai.com 会 401），未做真实生成"},
    "ark": {"status": "implemented",
            "verification": "协议已实现；本机没有 ark/即梦 Key，未做真实生成"},
    "gemini": {"status": "implemented",
               "verification": "协议已实现（predict 与 generateContent 双形态）；本机无 Gemini Key"},
    "wanx": {"status": "implemented",
             "verification": "异步任务+轮询已实现（本地假服务验过）；本机无 DashScope Key"},
    "qwen": {"status": "implemented",
             "verification": "异步/同步双形态已实现；本机无 DashScope Key"},
    "minimax": {"status": "implemented",
                "verification": "协议已实现（含 base_resp 业务错误检查）；本机 MiniMax Key 未用于出图验证"},
    "siliconflow": {"status": "verified",
                    "verification": "2026-10-25 真实 Key **生成已验证**（经 Lead 授权：单张、"
                                    "上限 ¥0.05）：model=Tongyi-MAI/Z-Image-Turbo，"
                                    "size=1024x1024，HTTP 200，5.33s，349,531 字节 PNG，"
                                    "PIL 打开校验 1024x1024 RGBA 且内容非空（RGB extrema "
                                    "140-255 / 0-255 / 0-255）。证据文件："
                                    "backend/data/cache/media_bridge_image_real.json、"
                                    "backend/data/cache/images/_live_probe/"
                                    "img-siliconflow-20261025-162115-ae7e31.png"
                                    "（sha256_16=866aecce6b300bee）。"
                                    "此前鉴权探测 GET /v1/models 也是 200。"
                                    "注：平台默认在图上加了「AI 生成」水印；旧 imagegen 的 "
                                    "siliconflow 分支会发 X-Enable-Watermark:0 关掉，"
                                    "本模块没发该头（保持验证版本不漂移，关不关水印由产品定）。"
                                    "另：注册表原默认型号 black-forest-labs/FLUX.1-schnell "
                                    "在该账号不可用，已由 Lead 改为 Tongyi-MAI/Z-Image-Turbo"},
    "zhipu": {"status": "implemented",
              "verification": "协议已实现；本机无智谱 Key"},
    "stability": {"status": "implemented",
                  "verification": "协议已实现（multipart + Accept:image/*）；本机无 Stability Key"},
    "local": {"status": "implemented",
              "verification": "ComfyUI 协议已实现（/prompt + /history + /view）；"
                              "本机未检测到 8188 服务"},
    "agentai": {"status": "implemented",
                "verification": "OpenAI 兼容协议已实现（复用统一实现）；"
                                "本机中转 Key 可用，但未花额度做真实出图验证"},
}


def verification_report() -> List[Dict[str, Any]]:
    """给交付回执用：一张表说清「哪家验过、哪家没验、为什么」。"""
    rows = []
    for pid in provider_ids():
        info = provider_status(pid)
        rows.append({"provider": pid, "status": info.get("status"),
                     "verification": info.get("verification", "")})
    return rows


__all__ = [
    "ImageProviderError",
    "provider_ids",
    "provider_entry",
    "provider_status",
    "resolve_key",
    "resolve_base_url",
    "list_providers",
    "generate_image",
    "probe_auth",
    "verification_report",
    "VERIFICATION",
    "OPENAI_FAMILY",
    "DASHSCOPE",
    "NATIVE",
    "LOCAL",
]
