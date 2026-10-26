# -*- coding: utf-8 -*-
"""
VideoForge · 图像生成服务（角色形象 / 场景概念图）

支持的图像生成后端（按 provider 名自动路由）：
  - minimax : MiniMax 图像生成  POST /v1/image_generation      （同步，返回 url / base64）
  - qwen    : 通义万相 DashScope POST .../text2image/image-synthesis（异步任务 + 轮询）
  - openai  : OpenAI 兼容       POST /v1/images/generations    （DALL·E / gpt-image / 兼容网关）

★ 2026-10-25：注册表里另外 4 家（ark 即梦 / gemini / zhipu / stability）以及
  本地 ComfyUI 的**真实协议**在 `core/image_providers.py`。本文件遇到旧表里
  没有的 provider 会自动转交给那一层，所以设置里 10+ 家平台都真的能调用
  （旧调用方式/旧签名保持兼容；新增可选参数 `settings` 在最后）。

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
        # ★★ 2026-09-28（用户第 7 条："一点击确认就弹出这样那样不允许"）
        #   实测根因：MiniMax 出图接口对提示词有**硬上限**，超了就整条失败：
        #     base_resp={'status_code': 2013,
        #                'status_msg': 'invalid params, prompt length must be less than 1500'}
        #   而我们的角色提示词会带上"剧本上下文 / 人物关系 / 外貌槽位"，
        #   一超 1500 就**直接被拒** —— 用户看到的就是"这个按钮不能用"。
        #   修法：把上限写进 provider 表，请求前**自动精简**（见 `fit_prompt`）。
        "prompt_max": 1500,
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
    # ★★ 本机出图（E:\出图 那套：ComfyUI 8188 + 出图服务 8189）
    #
    #   为什么单独列一个 provider 而不是让用户填 base_url：
    #   本机出图的**行为特征和云服务完全不同**，必须区别对待 ——
    #     · 一张图要 **2~3 分钟**（模型在本地跑，慢是正常的，不是卡住了）
    #     · 显存只有 8GB，模型放不下时会 OOM，而 ComfyUI 的 OOM 处理路径
    #       在 Windows 上会**直接把进程打崩**（实测：model_unload →
    #       unpatch_model → access violation，不是 Python 异常，捕获不到）
    #     · 端口没起时是连接被拒，不是 HTTP 报错
    #   所以这里配了：**更长的超时** + **生成前健康检查** + **崩溃识别**。
    #   这三样缺一个，用户看到的就是"等了 3 分钟然后一句没头没脑的超时"。
    "local": {
        "kind": "openai",
        "endpoint": "http://127.0.0.1:8189/v1/images/generations",
        "health_endpoint": "http://127.0.0.1:8189/health",
        #: 画图引擎本身。**必须单独探** —— 8189 活着不代表 8188 活着（实测）。
        "engine_endpoint": "http://127.0.0.1:8188/system_stats",
        "default_model": "qwen-image-2.1",
        "label": "本机出图（ComfyUI）",
        #: 本机出图的默认超时。**不能沿用云端那 180 秒** ——
        #: 实测单张 1280x720 要 141~180 秒，留够余量才不会把成功当超时。
        "default_timeout": 600.0,
        #: `E:\出图\api-server\qwen_api.py` 里的默认 key 就是 `local-qwen`
        #: （它允许空 key，但**不允许错的 key** —— 实测填别的值直接 401）。
        #: 所以这里直接带上默认值，用户开箱即用；改过 API_KEY 环境变量的话
        #: 在设置里覆盖即可。
        "default_api_key": "local-qwen",
    },
}

#: 别名：用户嘴里说的名字五花八门，都指到本机那个服务
PROVIDER_ALIASES = {
    "comfyui": "local", "comfy": "local", "本机": "local", "local_comfy": "local",
    "qwen-local": "local", "qwen_local": "local", "本地": "local",
}

#: 走本机出图时**必须**绕开系统代理。
#  实测过：系统代理开着时 httpx 会把 127.0.0.1 也丢给代理，
#  于是本机服务明明起着，请求却超时。视频那边（main.py）也踩过同一个坑。
LOCAL_HOSTS = ("127.0.0.1", "localhost", "0.0.0.0", "::1")

# 这些模型**不支持** image_size（传了会被忽略/报错），尺寸由输入图决定
_NO_IMAGE_SIZE_MODELS = {"Qwen/Qwen-Image-Edit-2509", "Qwen/Qwen-Image-Edit"}


def is_edit_model(model: str) -> bool:
    """这个模型是不是"指令式图像编辑"（能拿一张图去改）。"""
    m = (model or "").lower()
    return "image-edit" in m or "imageedit" in m


def resolve_provider(name: str) -> str:
    """把用户/配置里写的 provider 名规范成 IMAGE_PROVIDERS 里的键。"""
    k = (name or "").strip().lower()
    return PROVIDER_ALIASES.get(k, k)


def default_timeout_for(name: str) -> float:
    """这个 provider 该用多长超时。**本机出图要明显更长。**"""
    conf = IMAGE_PROVIDERS.get(resolve_provider(name)) or {}
    return float(conf.get("default_timeout") or 180.0)


def _is_local_endpoint(url: str) -> bool:
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host in LOCAL_HOSTS


async def local_service_health(provider: str = "local",
                               timeout: float = 6.0) -> Dict[str, Any]:
    """本机出图服务的健康检查（生成**之前**调，秒级返回）。

    ★★ 必须**两个端口都查**（实测踩到的假阳性）：
      出图接口（8189）和画图引擎（8188）是两个进程。引擎崩了之后，
      接口进程**还活着**，`/health` 照样返回 `comfyui: true`（它只是回显配置，
      并**没有真的去 ping 引擎**）。结果就是：
        崩溃后去查健康 → "服务在线" → 用户接着提交 → 又等十几秒才失败。
      所以这里自己再打一次 8188，引擎不通就如实说。
    """
    conf = IMAGE_PROVIDERS.get(resolve_provider(provider)) or {}
    url = conf.get("health_endpoint") or ""
    if not url:
        return {"ok": True, "skipped": "该 provider 没有健康检查端点"}
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as c:
            r = await c.get(url)
        if r.status_code >= 400:
            return {"ok": False, "status": r.status_code,
                    "error": f"本机出图接口返回 HTTP {r.status_code}"}
        body_txt = (r.text or "")[:300]
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "hint": "本机出图服务没起来。在 E:\\出图 里跑 "
                        "scripts\\start-qwen.bat（或 start-all.bat）再试。"}

    # 再确认画图引擎本身还在（8189 活着 ≠ 8188 活着）
    engine = conf.get("engine_endpoint") or ""
    if engine:
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as c:
                r2 = await c.get(engine)
            if r2.status_code >= 400:
                raise RuntimeError(f"HTTP {r2.status_code}")
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "stage": "engine", "interface": body_txt,
                    "error": f"画图引擎（ComfyUI 8188）不通：{type(e).__name__}: {e}",
                    "hint": ("出图接口还活着，但**画图引擎已经挂了** —— "
                             "最常见的原因是显存不够被 OOM 打崩。\n"
                             "重启：E:\\出图\\scripts\\start-qwen.bat\n"
                             "并且别再和视频生成抢显卡。")}
    return {"ok": True, "status": r.status_code, "body": body_txt}


def explain_local_failure(exc: BaseException, elapsed: float) -> str:
    """把本机出图的失败讲成"到底发生了什么 + 现在该做什么"。

    ★★ 这里的结论是 2026-09-24 第二次实测拿到的，比第一版准：

      第一版以为是"显存不够 → ComfyUI 卸模型 → 硬崩"，只对了一半。
      关掉 mmap 之后，同一个故障露出了**可捕获的真身**：

          OSError: 页面文件太小，无法完成操作。(os error 1455)

      所以真根因是 **Windows 提交内存（物理内存 + 页面文件）不够**，
      不是显存不够。之前那次原生 access violation 就是它的硬崩形态 ——
      同一件事，只是没被抓住。

      内存账（实测日志）：text encoder 8.9GB 常驻 CPU + 扩散模型 4.5GB 在显存，
      而本机物理内存 16GB、页面文件系统自动管理（当时约 10.9GB）。
      提交上限吃紧时，读权重的 mmap 操作就会失败。
    """
    name = type(exc).__name__
    text = str(exc)
    low = text.lower()

    # ① 真根因：页面文件 / 提交内存不够
    if "1455" in text or "页面文件太小" in text or "paging file" in low:
        return (
            f"本机出图的**系统内存不够**（用时 {elapsed:.0f} 秒后失败）。\n"
            "平台原始错误：`OSError: 页面文件太小，无法完成操作。(os error 1455)`\n"
            "为什么：本地模型很大 —— text encoder 要常驻约 8.9GB 内存，\n"
            "而物理内存 16GB 还要分给扩散模型和系统。**这不是显存问题。**\n"
            "怎么办（按见效快慢排）：\n"
            "  1. ★ 把 C 盘的虚拟内存调大（系统属性 → 高级 → 性能设置 →\n"
            "     高级 → 虚拟内存 → 更改）。C 盘空间不足时，把页面文件放到\n"
            "     空间更大的盘（比如 E 盘），并设一个较大的「初始大小」。\n"
            "  2. 出图时关掉占内存的程序（浏览器几十个标签页、剪辑软件）。\n"
            "  3. 确认画图引擎是**带 `--lowvram` 启动**的\n"
            "     （scripts\\start-qwen.bat 已经带上了）。\n"
            "  4. 别在出图的同时跑视频生成 —— 那是另一头内存大户。"
        )

    oom_markers = ("access violation", "connection reset", "remotedisconnected",
                   "server disconnected", "connection aborted", "10054",
                   "0xc0000005", "out of memory", "outofmemory")
    if any(m in low for m in oom_markers):
        return (
            f"本机出图服务在生成过程中**崩掉了**（不是超时，用时 {elapsed:.0f} 秒）。\n"
            "最可能的原因：**系统内存/页面文件不够**。已确认的崩溃链是——\n"
            "  内存不足 → ComfyUI 去卸/重装模型 → 在 Windows 上触发 access violation，\n"
            "  进程直接死掉（这是硬崩，Python 捕获不到，所以只能看到连接被重置）。\n"
            "  （关掉 mmap 时同一个故障会以更清楚的形态出现：\n"
            "    `OSError: 页面文件太小，无法完成操作。(os error 1455)`）\n"
            "怎么办：\n"
            "  1. ★ 把 C 盘虚拟内存调大，或把页面文件放到空间更大的盘；\n"
            "  2. 出图时关掉占内存的程序；\n"
            "  3. 用 scripts\\start-qwen.bat 重启画图引擎（它带 --lowvram，\n"
            "     让 text encoder 待在 CPU，就不会为腾显存把模型卸来卸去）。"
        )
    if any(m in low for m in ("connection refused", "10061", "connecterror",
                              "cannot connect", "actively refused")):
        return (
            "本机出图服务**没有启动**（连接被拒）。\n"
            "怎么办：到 E:\\出图 里跑 scripts\\start-qwen.bat（或 start-all.bat），\n"
            "等它打印「画图引擎已就绪」再回来。提示里的端口是 8188（引擎）和 8189（接口）。"
        )
    if "401" in text or "invalid api key" in low:
        return (
            "本机出图服务**拒绝了 API Key**（HTTP 401）。\n"
            "`E:\\出图\\api-server\\qwen_api.py` 的默认 key 是 `local-qwen`；\n"
            "如果你启动时设过环境变量 API_KEY，请把设置里的本机出图 Key 改成同一个值。"
        )
    if "execution error" in low or "exception during processing" in low:
        return (
            f"画图引擎在**执行工作流时报错了**（用时 {elapsed:.0f} 秒，服务还活着）。\n"
            "这类错误通常是内存/权重读取问题，平台原文里会有具体那一行。\n"
            "怎么办：到 E:\\出图\\comfyui\\src 看引擎窗口的最后一段报错；\n"
            "必要时用 scripts\\start-qwen.bat 重启引擎再试一次。\n"
            f"（平台原文：{text[:300]}）"
        )
    if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)) \
            or "timeout" in low:
        return (
            f"本机出图**超时**（等了 {elapsed:.0f} 秒）。\n"
            "本机跑一张图正常要 2~3 分钟（带 --lowvram 时可能到 5 分钟）；\n"
            "如果比这还久，通常是：\n"
            "  · 模型第一次加载（冷启动会多花 1~2 分钟，第二次就快）；\n"
            "  · 内存不够导致反复换入换出 —— 调大虚拟内存通常立刻见效；\n"
            "  · 有别的程序在占内存/显卡（游戏、另一个出图或出视频任务）。"
        )
    return f"本机出图失败（用时 {elapsed:.0f} 秒）：{name}: {text[:300]}"

#: 设置页里可选的尺寸（旧列表一直给这几个；新平台沿用同一套）
_UI_SIZES = ["1024x1024", "1280x720", "720x1280", "1024x768"]


def list_image_providers() -> list:
    """前端可选列表（供设置/弹窗展示）。

    ★ 2026-10-25：**补齐注册表里有、但旧表没实现调用的 4 家**
      （ark 即梦 / gemini / zhipu / stability）。
      在此之前用户在设置里能看到这 4 家、选中后一点生成却是
      「不支持的图像模型」—— 选项等于摆设。现在它们都由
      `core/image_providers.py` 真实实现，列表里也如实带 `status`
      （verified / implemented），不再把"能选"说成"能用"。
    """
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
            "sizes": list(_UI_SIZES),
        })

    # 补齐「注册表有、旧表没有」的平台（只读注册表，不改它）
    try:
        try:
            from . import image_providers as _ip
        except ImportError:  # pragma: no cover - 直接以单文件方式导入时
            import image_providers as _ip  # type: ignore
        rows = _ip.list_providers({})
    except Exception as e:  # noqa: BLE001 - 列表补不上也不能让设置页 500
        logger.warning("补齐图像平台列表失败（不影响旧列表）：%s", e)
        return out

    by_name = {row["name"]: row for row in out}
    for row in rows:
        pid = row["id"]
        if pid in by_name:
            # 旧表已经实现了这个平台：只补「验没验过」的诚实标注
            by_name[pid].update({
                "status": row["status"],
                "verification": row["verification"],
                "protocol": row["protocol"],
            })
            continue
        out.append({
            "name": pid,
            "label": row["label"],
            "display_name": row["label"],
            "default_model": row["default_model"],
            "default_size": "1024x1024",
            "sizes": list(_UI_SIZES),
            "protocol": row["protocol"],
            "has_key": row["has_key"],
            "usable": row["usable"],
            "requires_api_key": row["requires_api_key"],
            "status": row["status"],
            "verification": row["verification"],
            "note": row["notes"],
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

def fit_prompt(prompt: str, limit: int = 0) -> tuple:
    r"""把提示词**修剪到 provider 的长度上限内**，返回 `(新提示词, 说明)`。

    ★★ 2026-09-28（用户第 7 条：资产页按钮"一点击确认就弹出这样那样不允许"）

    实测：点「🎨 AI 生成形象」直接失败，上游原话是
        `MiniMax 未返回图片。base_resp={'status_code': 2013,
         'status_msg': 'invalid params, prompt length must be less than 1500'}`
    —— 提示词**超了 1500 字**（我们的角色提示词会带上剧本上下文/人物关系/外貌槽位）。
    用户看到的就是"这个按钮不能用"，而真正原因只是**长度**。

    修剪策略（**保住"画的是谁"**，砍掉"背景说明"）：
      1. 上限内直接用；
      2. 超了就按段落/句号切成句子，**从头往后装**（身份描述在前，戏份关系在后），
         装不下就停 —— 这样保住的是长相/服装/构图，砍掉的是剧情铺垫；
      3. 仍然超（单句就超长）就硬截断到上限。
    返回的说明会**如实告诉调用方砍了多少**（结果里可以带给用户看）。
    """
    p = str(prompt or "")
    try:
        limit = int(limit or 0)
    except (TypeError, ValueError):
        limit = 0
    if limit <= 0 or len(p) <= limit:
        return p, ""
    import re as _re
    parts = [x for x in _re.split(r"(?<=[。；;\.\!\?\n])", p) if x and x.strip()]
    out = ""
    for s in parts:
        if len(out) + len(s) > limit:
            break
        out += s
    if not out:
        out = p[:limit]
    note = (f"提示词原本 {len(p)} 字，超过该模型上限 {limit} 字，"
            f"已自动精简到 {len(out)} 字（保留长相/服装/构图，砍掉剧情铺垫）。"
            f"想要更精准可以在生成弹窗里手填提示词。")
    return out, note


# ──────────── 新平台路由（2026-10-25）────────────


def _registry_image_entry(provider_key: str):
    """从注册表取这个平台的声明（只读）。取不到返回 None。"""
    try:
        try:
            from . import provider_registry as _reg
        except ImportError:  # pragma: no cover
            import provider_registry as _reg  # type: ignore
        return _reg.find("image", provider_key)
    except Exception:  # noqa: BLE001
        return None


def _registry_model_ids(provider_key: str) -> list:
    entry = _registry_image_entry(provider_key)
    return [str(m.get("id") or "") for m in ((entry or {}).get("models") or [])]


def _find_foreign_model_owner(model: str, provider_key: str) -> str:
    """这个型号是不是**属于别的平台**？返回那个平台的 id（不属于任何一家则空）。

    ★ 为什么需要它：设置里的 `image_model` 是**全局**的（只有一份），
      用户把 `image_provider` 从 siliconflow 换成 minimax 时，
      `image_model` 还留着 `Qwen/Qwen-Image` —— 拿这个去请求 MiniMax，
      只会得到一句看不懂的「invalid model」。
      这里只在「该型号明确属于另一家」时才换成当前平台的默认型号，
      **用户自己填的、任何一家都没声明的型号一律不动**
      （厂商上新时不用等我们发版；硅基流动的 Qwen-Image-Edit 这类
       只存在于旧表的型号也不会被误伤）。
    """
    target = str(model or "").strip()
    if not target:
        return ""
    try:
        try:
            from . import provider_registry as _reg
        except ImportError:  # pragma: no cover
            import provider_registry as _reg  # type: ignore
        for entry in _reg.IMAGE_PROVIDERS:
            if entry.get("id") == provider_key:
                continue
            ids = [str(m.get("id") or "") for m in (entry.get("models") or [])]
            if target in ids:
                return str(entry.get("id") or "")
    except Exception:  # noqa: BLE001
        return ""
    return ""


def guard_model_for_provider(provider_key: str, model: str) -> tuple:
    """把「设置里的全局 image_model」纠正到当前平台，返回 `(model, note)`。

    只纠正**明确属于别家**的型号（见 `_find_foreign_model_owner`）；
    纠正了就如实给一句说明，不静默改。
    """
    used = str(model or "").strip()
    owner = _find_foreign_model_owner(used, provider_key)
    if not owner:
        return used, ""
    conf = IMAGE_PROVIDERS.get(provider_key) or {}
    fallback = str(conf.get("default_model") or "")
    entry = _registry_image_entry(provider_key) or {}
    fallback = str(entry.get("default_model") or fallback)
    if not fallback:
        return used, ""
    note = (f"设置里的图像模型「{used}」属于「{owner}」，不是「{provider_key}」的型号，"
            f"已改用当前平台的默认型号「{fallback}」。"
            f"（想用 {owner}，请在设置里把图像平台也切成它）")
    logger.warning("%s", note)
    return fallback, note


async def _delegate_to_provider_layer(
    provider_key: str,
    prompt: str,
    *,
    api_key: str,
    out_dir: str,
    size: str,
    model: str,
    base_url: str,
    timeout: float,
    reference_images: Optional[List[str]],
    negative_prompt: str,
    settings: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """把旧表没实现的平台交给 `core/image_providers.py`（统一接入层）。

    返回值形状**与旧的 generate_image 保持一致**（ok/path/provider/model/elapsed），
    这样 main.py 与 agent 工具不用改。
    """
    settings = dict(settings or {})
    try:
        try:
            from . import image_providers as _ip
        except ImportError:  # pragma: no cover
            import image_providers as _ip  # type: ignore
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "provider": provider_key,
                "error": f"图像接入层不可用：{type(e).__name__}: {e}"}

    # 型号：显式 > 设置里的 image_model（只在它不属于别家时采用）> 平台默认
    used_model = str(model or "").strip()
    if not used_model:
        cand = str(settings.get("image_model") or "").strip()
        if cand and not _find_foreign_model_owner(cand, provider_key):
            used_model = cand
    res = await _ip.generate_image(
        provider_key, prompt,
        size=size or "1024x1024", settings=settings, out_dir=out_dir,
        model=used_model, timeout=timeout, api_key=api_key,
        base_url=base_url, reference_images=reference_images,
        negative_prompt=negative_prompt)
    if res.get("ok"):
        return {
            "ok": True,
            "path": res.get("path"),
            "filename": res.get("filename"),
            "size_bytes": res.get("bytes"),
            "provider": res.get("provider"),
            "model": res.get("model"),
            "prompt": res.get("prompt") if res.get("prompt") is not None else prompt,
            "prompt_note": res.get("prompt_note") or "",
            "urls": res.get("urls") or [],
            "notes": res.get("notes") or [],
            "elapsed": res.get("elapsed"),
        }
    return {
        "ok": False,
        "provider": provider_key,
        "model": res.get("model") or "",
        "error": res.get("error") or "图像生成失败",
        "status": res.get("status") or 0,
        "stage": res.get("stage") or "",
        "elapsed": res.get("elapsed"),
    }


async def generate_image(
    provider: str,
    api_key: str,
    prompt: str,
    out_dir: str,
    size: str = "1024x1024",
    model: str = "",
    base_url: str = "",
    timeout: float = 0.0,
    reference_images: Optional[List[str]] = None,
    negative_prompt: str = "",
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """生成一张图片并保存到 out_dir，返回 {ok, path, provider, model, prompt, error?}

    ★ `reference_images` 给定时，能编辑的模型会走**指令式图像编辑**
      （拿你的图 + 一句"要改什么" → 改完的图），而不是凭空重画一张。
      这是"用户供图让 AI 按剧情修改"的实现路径。

    ★ 2026-10-25：新增可选 `settings`（**加在最后**，旧调用方式完全不受影响）。
      · `provider` 留空 → 按 `settings["image_provider"]` 路由；
      · 注册表里有、但旧表没实现的平台（ark/gemini/zhipu/stability）
        → 自动转交 `core/image_providers.py` 的真实实现；
      · `settings` 里能直接取 Key / base_url，脚本调用不用再把 Key 抄一遍。
    """
    settings = dict(settings or {})
    # ★ provider 留空 = 「按设置里的图像平台走」（以前会静默落到 minimax）
    provider = (str(provider or "").strip()
                or str(settings.get("image_provider") or "").strip()
                or "minimax")
    key = resolve_provider(provider)
    conf = IMAGE_PROVIDERS.get(key)
    if not conf:
        # 旧表没有实现 → 交给统一接入层（10 家注册表全覆盖）
        return await _delegate_to_provider_layer(
            key, prompt, api_key=api_key, out_dir=out_dir, size=size,
            model=model, base_url=base_url, timeout=timeout,
            reference_images=reference_images, negative_prompt=negative_prompt,
            settings=settings)
    # 本机服务有自己的默认 key（local-qwen）；云端没 key 就是真的没配
    if not api_key:
        api_key = conf.get("default_api_key") or ""
    if not api_key and settings:
        # settings 给全了就别让调用方再自己找 Key（与设置页同一套查找口径）
        try:
            try:
                from . import image_providers as _ip0
            except ImportError:  # pragma: no cover
                import image_providers as _ip0  # type: ignore
            api_key = _ip0.resolve_key(key, settings).get("key") or ""
        except Exception:  # noqa: BLE001 - 取不到就走原来的报错
            api_key = ""
    if not api_key and not conf.get("no_auth"):
        return {"ok": False, "error": f"未配置 {provider} 的 API Key（请到「设置」填写）"}
    if not api_key:
        api_key = "local"     # 兜底：请求头得有个值（该服务允许空 key）

    used_model = model or conf["default_model"]
    # ★ 全局 image_model 属于别家时纠正到本平台（否则上游只会回 invalid model）
    used_model, model_note = guard_model_for_provider(key, used_model)
    # ★★ 请求前按 provider 的上限**先精简**（别让上游用 2013 把我们打回来）
    prompt, prompt_note = fit_prompt(prompt, conf.get("prompt_max") or 0)
    if prompt_note:
        logger.info("出图提示词已精简：%s", prompt_note)
    # ★ 超时按 provider 决定，**默认值是 0 = "用这个 provider 的默认"**。
    #   踩过的坑：签名原来写死 `timeout: float = 180.0`，于是"没传 timeout"
    #   也变成了显式 180 秒，本机那 600 秒的默认值**永远轮不到** ——
    #   表现就是"本机出图明明在跑，180 秒就被判定超时"。
    #   这就是用户说的"本机出图要等 2-3 分钟"那个超时问题的根。
    if not timeout or timeout <= 0:
        timeout = default_timeout_for(key)
    out_root = Path(out_dir)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    kind = conf["kind"]
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    t_start = time.time()

    # ★ 本机服务：生成前先秒级探一下活。没起就立刻给"该启动什么"，
    #   而不是让用户干等几分钟才看到一句超时。
    if key == "local":
        hc = await local_service_health(key)
        if not hc.get("ok"):
            # ★ 2026-10-20：**把 hint 并进 error**。
            #   原来 hint 单独放在结果里，而调用方（agent 工具）只取 `error`
            #   ⇒ "在 E:\出图 里跑 scripts\start-qwen.bat" 这条**最有用的提示被丢掉了**，
            #   用户只看到「本机出图服务不可用：ConnectError」，无从下手。
            _hint = str(hc.get("hint") or "").strip()
            return {"ok": False, "provider": key, "stage": "health",
                    "error": ("本机出图服务不可用：" + str(hc.get("error"))
                              + (("\n" + _hint) if _hint else "")),
                    "hint": _hint}

    try:
        # trust_env=False：**本机地址绝不能被系统代理接管**（实测会超时）
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=20.0, read=timeout, write=60.0, pool=10.0),
                trust_env=not _is_local_endpoint(conf.get("endpoint") or "")) as client:
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
                    # 没给参考图却要���编辑模型
                    if is_edit_model(used_model):
                        _t2i = list(conf.get("t2i_models") or [])
                        # ★★ 2026-10-20 修（用户实机）：
                        #   这个编辑模型是**默认值**（用户没指定）时，**自动换成文生图模型**，
                        #   而不是直接失败。
                        #   原来硅基流动的 default_model = "Qwen/Qwen-Image-Edit-2509"，
                        #   于是「出场景图」这种纯文生图需求**一张都出不来**：
                        #   报「是指令式图像编辑模型，必须给一张参考图」——
                        #   而用户根本没有参考图可给。第一次用这个 provider 就撞墙。
                        #   用户**显式**选了编辑模型时才报错（那是他自己的选择）。
                        if used_model == conf.get("default_model") and _t2i:
                            used_model = _t2i[0]
                            body["model"] = used_model
                            if used_model not in _NO_IMAGE_SIZE_MODELS and size and "x" in size:
                                body["image_size"] = size
                            logger.info("出图模型 %s 是编辑模型且没有参考图 → 自动改用文生图模型 %s",
                                        conf.get("default_model"), used_model)
                        else:
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
                # ★ 本机出图支持"图生图/指令式编辑"（2026-09-24）：
                #   把参考图作为 data URI 放进 `image` 字段，
                #   8189 那边会用 TextEncodeQwenImageEdit 把原图编码进 conditioning，
                #   于是能"改一处、保其余"。
                #   ⚠ 这时**不要带 size** —— 让服务端按原图比例算，
                #     强行套 1024x1024 会把竖构图裁掉或拉变形。
                if key == "local" and reference_images:
                    ref = _to_data_uri(reference_images[0])
                    if ref:
                        body["image"] = ref
                        body.pop("size", None)
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

        logger.info("图像生成成功：%s → %s（用时 %.0f 秒）",
                    provider, out_path, time.time() - t_start)
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
            # 提示词被精简过就如实带出去（界面上可以说一句"为了适配模型上限已精简"）
            "prompt_note": prompt_note,
            # 型号被纠正过（设置里的 image_model 属于别家）也如实说
            "model_note": model_note,
            "elapsed": round(time.time() - t_start, 1),
        }
    except Exception as e:
        logger.exception("图像生成异常：%s", e)
        elapsed = time.time() - t_start
        if key == "local":
            # ★ 本机出图的失败必须"讲清楚"，因为它有三种完全不同的原因，
            #   而用户能采取的行动也完全不同（等 / 降分辨率 / 去启动服务）。
            return {"ok": False, "provider": key, "elapsed": round(elapsed, 1),
                    "error": explain_local_failure(e, elapsed)}
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "elapsed": round(elapsed, 1)}
