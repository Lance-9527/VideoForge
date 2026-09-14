# -*- coding: utf-8 -*-
"""
VideoForge · 图像生成服务（角色形象 / 场景概念图）

支持的图像生成后端（按 provider 名自动路由）：
  - minimax : MiniMax 图像生成  POST /v1/image_generation      （同步，返回 url / base64）
  - qwen    : 通义万相 DashScope POST .../text2image/image-synthesis（异步任务 + 轮询）
  - openai  : OpenAI 兼容       POST /v1/images/generations    （DALL·E / gpt-image / 兼容网关）

对外只暴露一个 generate_image()，内部处理各家的请求体与结果解析差异，
把图片落盘到本地并返回文件路径。
"""
import asyncio
import base64
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("videoforge.imagegen")

# ──────────── provider 配置 ────────────

IMAGE_PROVIDERS: Dict[str, Dict[str, Any]] = {
    "minimax": {
        "kind": "minimax",
        "endpoint": "https://api.minimaxi.com/v1/image_generation",
        "default_model": "image-01",
        "label": "MiniMax 图像",
    },
    "qwen": {
        "kind": "dashscope",
        "endpoint": "https://dashscope.aliyuncs.com/api/v1/services/aigc/text2image/image-synthesis",
        "task_endpoint": "https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}",
        "default_model": "wanx2.1-t2i-turbo",
        "label": "通义万相",
    },
    "wanx": {
        "kind": "dashscope",
        "endpoint": "https://dashscope.aliyuncs.com/api/v1/services/aigc/text2image/image-synthesis",
        "task_endpoint": "https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}",
        "default_model": "wanx2.1-t2i-turbo",
        "label": "通义万相",
    },
    "openai": {
        "kind": "openai",
        "endpoint": "https://api.openai.com/v1/images/generations",
        "default_model": "gpt-image-1",
        "label": "OpenAI 图像",
    },
    # ★ 硅基流动：用户已有可用 Key（同一个 Key 还用来做语音合成），
    #   而且它提供 **Qwen-Image-Edit** —— 指令式图像编辑（真图生图）。
    #   这是"后期 AI 再修改"能力的关键：本机实测可用（见
    #   tests/test_siliconflow_image_edit.py）。
    "siliconflow": {
        "kind": "siliconflow",
        "endpoint": "https://api.siliconflow.cn/v1/images/generations",
        "default_model": "Qwen/Qwen-Image-Edit-2509",   # 默认就用可编辑的版本
        "edit_models": ["Qwen/Qwen-Image-Edit-2509", "Qwen/Qwen-Image-Edit"],
        "t2i_models": ["Qwen/Qwen-Image", "Kwai-Kolors/Kolors",
                       "Tongyi-MAI/Z-Image-Turbo", "baidu/ERNIE-Image-Turbo"],
        "label": "硅基流动 SiliconFlow",
    },
}

# 这些模型**不支持** image_size（传了会被忽略/报错），尺寸由输入图决定
_NO_IMAGE_SIZE_MODELS = {"Qwen/Qwen-Image-Edit-2509", "Qwen/Qwen-Image-Edit"}


def is_edit_model(model: str) -> bool:
    """这个模型是不是"指令式图像编辑"（能拿一张图去改）。"""
    m = (model or "").lower()
    return "image-edit" in m or "imageedit" in m

# 前端可选列表（供设置/弹窗展示）
def list_image_providers() -> list:
    seen = set()
    out = []
    for name, conf in IMAGE_PROVIDERS.items():
        if conf["label"] in seen:
            continue
        seen.add(conf["label"])
        out.append({
            "name": name,
            "label": conf["label"],
            "display_name": conf["label"],
            "default_model": conf["default_model"],
            "default_size": "1024x1024",
            "sizes": ["1024x1024", "1280x720", "720x1280", "1024x768"],
        })
    return out


def _guess_ext(url: str) -> str:
    low = (url or "").lower().split("?")[0]
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        if low.endswith(ext):
            return ext
    return ".png"


def _to_data_uri(path: str, max_mb: float = 8.0) -> str:
    """把本地图片转成 `data:image/...;base64,...`（硅基流动的 image 字段接受这种形式）。

    超过 max_mb 就不转 —— 请求体会大到被网关拒掉，不如让调用方先压缩。
    """
    try:
        if not path or not os.path.exists(path):
            return ""
        size_mb = os.path.getsize(path) / 1048576.0
        if size_mb > max_mb:
            logger.warning("参考图过大（%.1fMB > %.1fMB），跳过：%s", size_mb, max_mb, path)
            return ""
        ext = (os.path.splitext(path)[1].lstrip(".") or "png").lower()
        if ext == "jpg":
            ext = "jpeg"
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        return f"data:image/{ext};base64,{b64}"
    except Exception as e:
        logger.warning("参考图转 data URI 失败 %s：%s", path, e)
        return ""


async def _download(client: httpx.AsyncClient, url: str, out_path: Path) -> Path:
    r = await client.get(url, timeout=httpx.Timeout(connect=15.0, read=120.0, write=60.0, pool=10.0))
    r.raise_for_status()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(r.content)
    return out_path


def _save_b64(b64: str, out_path: Path) -> Path:
    data = b64.split(",")[-1] if b64.startswith("data:") else b64
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(base64.b64decode(data))
    return out_path


# ──────────── 主入口 ────────────

async def generate_image(
    provider: str,
    api_key: str,
    prompt: str,
    out_dir: str,
    size: str = "1024x1024",
    model: str = "",
    base_url: str = "",
    timeout: float = 180.0,
    reference_images: Optional[List[str]] = None,
    negative_prompt: str = "",
) -> Dict[str, Any]:
    """生成一张图片并保存到 out_dir，返回 {ok, path, provider, model, prompt, error?}

    ★ `reference_images` 给定时，能编辑的模型会走**指令式图像编辑**
      （拿你的图 + 一句"要改什么" → 改完的图），而不是凭空重画一张。
      这是"用户供图让 AI 按剧情修改"的实现路径。
    """
    key = (provider or "minimax").strip().lower()
    conf = IMAGE_PROVIDERS.get(key)
    if not conf:
        return {"ok": False, "error": f"不支持的图像模型：{provider}（可选：{', '.join(IMAGE_PROVIDERS)}）"}
    if not api_key:
        return {"ok": False, "error": f"未配置 {provider} 的 API Key（请到「设置」填写）"}

    used_model = model or conf["default_model"]
    out_root = Path(out_dir)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    kind = conf["kind"]
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=20.0, read=timeout, write=60.0, pool=10.0)) as client:
            # ── MiniMax：同步返回 url / base64 ──
            if kind == "minimax":
                body = {
                    "model": used_model,
                    "prompt": prompt,
                    "n": 1,
                    "response_format": "url",
                    "prompt_optimizer": True,
                }
                r = await client.post(conf["endpoint"], headers=headers, json=body)
                if r.status_code >= 400:
                    return {"ok": False, "error": f"MiniMax 图像生成失败 HTTP {r.status_code}: {r.text[:200]}"}
                data = r.json()
                urls = ((data.get("data") or {}).get("image_urls")) or []
                b64s = ((data.get("data") or {}).get("image_base64")) or []
                if urls:
                    out_path = out_root / f"img-{stamp}{_guess_ext(urls[0])}"
                    await _download(client, urls[0], out_path)
                elif b64s:
                    out_path = _save_b64(b64s[0], out_root / f"img-{stamp}.png")
                else:
                    meta = data.get("metadata") or {}
                    base_resp = data.get("base_resp") or {}
                    hint = ""
                    failed = str(meta.get("failed_count", "0"))
                    if failed not in ("", "0"):
                        hint = "（提示词可能未通过内容审核，请调整描述后重试）"
                    logger.warning("MiniMax 未返回图片：%s", str(data)[:500])
                    return {"ok": False,
                            "error": f"MiniMax 未返回图片{hint}。metadata={meta} base_resp={base_resp}"}

            # ── 通义万相（DashScope 异步任务）──
            elif kind == "dashscope":
                h = dict(headers)
                h["X-DashScope-Async"] = "enable"
                body = {
                    "model": used_model,
                    "input": {"prompt": prompt},
                    "parameters": {"size": size if "x" in size else "1024*1024", "n": 1},
                }
                body["parameters"]["size"] = body["parameters"]["size"].replace("x", "*")
                r = await client.post(conf["endpoint"], headers=h, json=body)
                if r.status_code >= 400:
                    return {"ok": False, "error": f"通义万相创建任务失败 HTTP {r.status_code}: {r.text[:200]}"}
                task_id = (r.json().get("output") or {}).get("task_id")
                if not task_id:
                    return {"ok": False, "error": f"通义万相未返回 task_id：{r.text[:200]}"}
                # 轮询
                deadline = time.time() + timeout
                img_url = None
                while time.time() < deadline:
                    await asyncio.sleep(3)
                    tr = await client.get(conf["task_endpoint"].format(task_id=task_id), headers=headers)
                    td = tr.json()
                    status = (td.get("output") or {}).get("task_status")
                    if status == "SUCCEEDED":
                        results = (td.get("output") or {}).get("results") or []
                        if results:
                            img_url = results[0].get("url")
                        break
                    if status in ("FAILED", "CANCELED", "UNKNOWN"):
                        return {"ok": False, "error": f"通义万相任务失败：{td.get('output') or td}"}
                if not img_url:
                    return {"ok": False, "error": "通义万相任务超时未返回图片"}
                out_path = out_root / f"img-{stamp}{_guess_ext(img_url)}"
                await _download(client, img_url, out_path)

            # ── 硅基流动（含指令式图像编辑）──
            elif kind == "siliconflow":
                body = {"model": used_model, "prompt": prompt}
                if used_model not in _NO_IMAGE_SIZE_MODELS and size and "x" in size:
                    body["image_size"] = size
                if negative_prompt:
                    body["negative_prompt"] = negative_prompt
                # ★ 传了参考图就走**真图生图/指令式编辑**：
                #   把图作为 data URI 放进 `image`，prompt 就是"要改什么"。
                #   这是本模块最核心的能力 —— 用户说"支持用户自己提供图片
                #   AI 参考后根据剧情需要进行实际修改"，靠的就是这个字段。
                for idx, p in enumerate([reference_images or []] and
                                        (reference_images or [])[:3]):
                    ref = _to_data_uri(p)
                    if not ref:
                        continue
                    body["image" if idx == 0 else f"image{idx + 1}"] = ref
                if not any(k.startswith("image") for k in body):
                    # 没给参考图却指定了编辑模型 → 明确告诉用户，别让他以为改了
                    if is_edit_model(used_model):
                        return {"ok": False,
                                "error": f"「{used_model}」是指令式图像编辑模型，"
                                         f"必须给一张参考图才能工作。"
                                         f"请先上传/生成一张图，或改用文生图模型"
                                         f"（{', '.join(conf.get('t2i_models') or [])}）。"}
                h = dict(headers)
                h["X-Enable-Watermark"] = "0"    # 自己控制水印，避免叠在成片上
                r = await client.post(conf["endpoint"], headers=h, json=body)
                if r.status_code >= 400:
                    return {"ok": False,
                            "error": f"硅基流动图像生成失败 HTTP {r.status_code}: {r.text[:300]}"}
                data = r.json() or {}
                imgs = data.get("images") or data.get("data") or []
                if not imgs:
                    return {"ok": False,
                            "error": f"硅基流动未返回图片：{str(data)[:250]}"}
                first = imgs[0]
                url = first.get("url") or ""
                b64 = first.get("b64_json") or first.get("image_base64") or ""
                if url:
                    out_path = out_root / f"img-{stamp}{_guess_ext(url)}"
                    await _download(client, url, out_path)
                elif b64:
                    out_path = _save_b64(b64, out_root / f"img-{stamp}.png")
                else:
                    return {"ok": False, "error": f"硅基流动返回格式未知：{str(first)[:200]}"}

            # ── OpenAI 兼容 ──
            else:
                endpoint = base_url.rstrip("/") + "/v1/images/generations" if base_url else conf["endpoint"]
                body = {"model": used_model, "prompt": prompt, "n": 1, "size": size}
                r = await client.post(endpoint, headers=headers, json=body)
                if r.status_code >= 400:
                    return {"ok": False, "error": f"OpenAI 图像生成失败 HTTP {r.status_code}: {r.text[:200]}"}
                data = r.json().get("data") or []
                if not data:
                    return {"ok": False, "error": "OpenAI 未返回图片数据"}
                item = data[0]
                if item.get("url"):
                    out_path = out_root / f"img-{stamp}{_guess_ext(item['url'])}"
                    await _download(client, item["url"], out_path)
                elif item.get("b64_json"):
                    out_path = _save_b64(item["b64_json"], out_root / f"img-{stamp}.png")
                else:
                    return {"ok": False, "error": "OpenAI 返回格式未知"}

        logger.info("图像生成成功：%s → %s", provider, out_path)
        try:
            _size = os.path.getsize(out_path)
        except OSError:
            _size = 0
        return {
            "ok": True,
            "path": str(out_path),
            "filename": os.path.basename(str(out_path)),
            "size_bytes": _size,
            "provider": provider,
            "model": used_model,
            "prompt": prompt,
        }
    except Exception as e:
        logger.exception("图像生成异常：%s", e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
