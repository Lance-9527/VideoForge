"""可灵官方 MCP 的高层封装 —— **不用 API key** 就能生成视频/图片。

════════════════════════════════════════════════════════════════════════
一句话
════════════════════════════════════════════════════════════════════════
`kling_mcp` 负责"授权 + 裸调用"；本模块负责"把它变成 VideoForge 能用的功能"：
上传本地文件、按模型约束拼参数、建角色 Element、把白膜视频当动作参考、
轮询任务、取回成品。

════════════════════════════════════════════════════════════════════════
这些约束全部来自官方 MCP 的 `who_am_i` / 工具描述，**不是猜的**
（原文已存进 docs/可灵-MCP-能力清单.{md,json}）
════════════════════════════════════════════════════════════════════════
1. **一切输入都要先 file_upload**。官方原文：
   "Must be url returned by file_upload tool, no local file path or
    external url allowed."（image_to_video 的 first_image）
   → 所以白膜视频**根本不需要公网隧道**，直接传上去就行。
2. **时长**：3.0 / 3.0-omni / 3.0-turbo = 3–15 秒；2.5 / 2.6 = 只能 5 或 10；
   o1 = 3–10。（来自 who_am_i 的 allowedValues）
3. **分辨率**：视频模型 allowedValues 只有 `720p`；只有 motion_control
   能选 720p/1080p。
4. **Element（角色一致性）不能用在天生文生/文生图**。官方原文：
   "text_to_image and text_to_video support no element at all —
    never pass 'elements' or <<<id>>> there."
   → 想用角色 Element，必须走 **image_to_video**（视频元素）
     或 **image_to_image**（图片元素）。
5. **视频 Element** 只能用在 `kling-video-v3_0_omni` / `kling-video-v3_0`
   的 image_to_video 上；**图片 Element** 可用在 image_to_image（含 o1）
   和列出 elements 的 image_to_video 上。
3. **白膜在可灵上不能当像素级控制信号**（试错两次，都是真花钱试的）：
   · ❌ `motion_control` 的 video 输入要**含真人的动作视频**；白膜是灰方块，
     平台判 `UNQUALIFIED_INPUT / No valid characters detected in the video`。
   · ❌ `image_to_video` 的 first_image 用白膜首帧 → 生成会成功，但
     **画面就是白膜本身**（灰方块 + 鼻子标记 + 三分线全在）——
     可灵忠实地把输入图动了起来，白膜画风渗进成片。
   · ✅ 正确做法：first_image 用**角色定妆照**，白膜的贡献是**文字化的
     运镜/走位/节奏**（`white_model_to_video` 就是这么做的）。
   根本原因：可灵的 image_to_video **只有图片输入，没有参考视频入口**。
   想要像素级运动控制得换平台 —— 即梦/Seedance 的 `reference_video`
   接受公网 URL（本项目实测过）。
7. 生成结果 URL **24 小时后过期**，必须及时下载。
8. 灵感值不足时官方给的充值页（带客户端来源参数）：
   https://klingai.com/h5-app/membership-agent?r=agent_VideoForge
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import kling_mcp as K

logger = logging.getLogger(__name__)

# ──────────────────────── 官方约束（照抄 who_am_i，别改）────────────────────────

#: 各视频模型的时长取值范围（来自 who_am_i 的 allowedValues）
DURATION_RANGE: Dict[str, Tuple[int, int]] = {
    "kling-video-v2_5": (5, 10),
    "kling-video-v2_6": (5, 10),
    "kling-video-o1": (3, 10),
    "kling-video-v3_0": (3, 15),
    "kling-video-v3_0_omni": (3, 15),
    "kling-video-v3_0_turbo": (3, 15),
}

#: 只能取离散值的模型（2.5/2.6 只支持 5 或 10）
DURATION_DISCRETE: Dict[str, List[int]] = {
    "kling-video-v2_5": [5, 10],
    "kling-video-v2_6": [5, 10],
}

#: 视频输出分辨率（官方 allowedValues 只有 720p）
VIDEO_RESOLUTIONS = ["720p"]
#: motion_control 才有多档
MOTION_RESOLUTIONS = ["720p", "1080p"]

#: 能用"视频 Element"的 image_to_video 模型（官方原文点名）
VIDEO_ELEMENT_MODELS = {"kling-video-v3_0", "kling-video-v3_0_omni"}

#: element_create 的标签必须从这个目录里选（官方原文点名）
ELEMENT_TAGS = ["角色", "动物", "道具", "服饰", "场景", "特效", "其他"]

#: file_upload 允许的 MIME（官方原文）
UPLOAD_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".mp4": "video/mp4",
}

#: 各模型的默认选择（挑"能力最全"的，用户明确要求时再换）
DEFAULT_VIDEO_MODEL = "kling-video-v3_0"
DEFAULT_IMAGE_MODEL = "kling-image-v3_0"

CREDITS_URL = "https://klingai.com/h5-app/membership-agent?r=agent_VideoForge"


# ──────────────────────── 基础：身份 / 额度 / 模型清单 ────────────────────────


async def who_am_i(tools: Optional[List[str]] = None) -> Dict[str, Any]:
    """拿账号身份 + 各工具的模型与参数 schema。**调任何生成之前先调它。**"""
    args: Dict[str, Any] = {}
    if tools:
        args["tools"] = tools
    return await K.call_tool_json("who_am_i", args)


async def credits() -> Dict[str, Any]:
    """查会员等级和剩余灵感值。**0 就一定生成不出来，先告诉用户去充值。**"""
    d = await K.call_tool_json("query_membership_and_credits", {})
    remain = float(d.get("availableRemainCredits") or 0)
    return {
        "user_id": d.get("userId"),
        "membership": d.get("membershipType"),
        "credits": remain,
        "enough": remain > 0,
        "topup_url": CREDITS_URL,
        "note": ("灵感值为 0，任何生成都会被拒。充值页：" + CREDITS_URL)
                if remain <= 0 else "灵感值可用",
    }


def models_for(who: Dict[str, Any], tool: str) -> List[str]:
    """从 who_am_i 的结果里取某个工具支持的模型名列表。"""
    am = who.get("availableModels") or {}
    return [m.get("model") for m in (am.get(tool) or {}).get("models", [])]


def model_spec(who: Dict[str, Any], tool: str, model: str) -> Dict[str, Any]:
    """取某个工具+模型的参数规格（拿不到就抛，避免瞎编参数）。"""
    am = who.get("availableModels") or {}
    for m in (am.get(tool) or {}).get("models", []):
        if m.get("model") == model:
            return m
    av = [m.get("model") for m in (am.get(tool) or {}).get("models", [])]
    raise ValueError(f"可灵 {tool} 没有模型 {model}（可选：{av}）")


def clamp_duration(model: str, want: float) -> int:
    """把想要的时长收进该模型的合法取值 —— 宁可改成合法值，也不要被平台拒。"""
    if model in DURATION_DISCRETE:
        allowed = DURATION_DISCRETE[model]
        return min(allowed, key=lambda v: abs(v - want))
    lo, hi = DURATION_RANGE.get(model, (3, 15))
    return max(lo, min(hi, int(round(want))))


# ──────────────────────── 上传本地文件（白膜/定妆照的关键）────────────────────────


def _upload_cache_path() -> Path:
    return K._store_path().parent / "kling_uploads.json"


def _load_upload_cache() -> Dict[str, Any]:
    p = _upload_cache_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_upload_cache(d: Dict[str, Any]) -> None:
    try:
        _upload_cache_path().write_text(
            json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logger.warning("可灵上传缓存写不进去（不影响本次使用）", exc_info=True)


def _cache_key(path: Path) -> str:
    st = path.stat()
    return f"{path.resolve()}|{st.st_size}|{int(st.st_mtime)}"


async def upload_file(path: str | Path, *, use_cache: bool = True,
                      timeout: float = 300.0) -> str:
    """把本地文件传给可灵，拿回一个能当输入用的 URL。

    ★ 这一步就是"白膜当参考素材"的全部答案：官方 MCP 自带 file_upload，
      所以**不需要公网隧道、不需要域名、不占本机端口**。
      官方原文：ticket 一次性、会过期；同样的本地文件应当复用已上传的 URL。
    """
    import httpx

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"要上传的文件不存在：{p}")
    ext = p.suffix.lower()
    mime = UPLOAD_MIME.get(ext)
    if not mime:
        raise ValueError(f"可灵只收 PNG/JPG/MP4，这个不行：{p.name}（{ext}）")
    size = p.stat().st_size
    if mime != "video/mp4" and size > 30 * 1024 * 1024:
        raise ValueError(f"可灵的图片上限 30MB，这个 {size/1048576:.1f}MB")

    key = _cache_key(p)
    cache = _load_upload_cache()
    if use_cache:
        hit = cache.get(key) or {}
        # 记了时间就能判断有没有过期（官方说 URL 会过期，默认按 20 小时保守处理）
        if hit.get("url") and (time.time() - float(hit.get("at") or 0)) < 20 * 3600:
            logger.info("可灵：复用已上传的文件 %s", p.name)
            return hit["url"]

    ticket = await K.call_tool_json("file_upload", {
        "filename": p.name, "contentType": mime, "size": int(size),
    })
    upload_url = ticket.get("upload_url") or ticket.get("uploadUrl") or ""
    tk = ticket.get("ticket") or ""
    if not upload_url or not tk:
        raise RuntimeError(f"可灵 file_upload 没给出上传地址：{str(ticket)[:300]}")

    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as c:
        r = await c.post(
            upload_url,
            data={"ticket": tk},
            files={"file": (p.name, p.read_bytes(), mime)},
        )
        if r.status_code not in (200, 201):
            raise RuntimeError(f"上传到可灵失败 HTTP {r.status_code}：{r.text[:300]}")
        try:
            out = r.json()
        except Exception:
            out = {}
        # 返回结构可能是 {url:...} 也可能再包一层 data
        url = (out.get("url") or (out.get("data") or {}).get("url")
               or (out.get("data") or {}).get("file_url") or "")
        if not url:
            raise RuntimeError(f"上传成功但没解析出 URL：{str(out)[:300]}")

    cache[key] = {"url": url, "at": time.time(), "name": p.name,
                  "size": size, "mime": mime}
    _save_upload_cache(cache)
    logger.info("可灵：已上传 %s（%.1f MB）", p.name, size / 1048576)
    return url


async def upload_many(paths: List[str | Path]) -> List[str]:
    """按顺序上传多个文件。"""
    out = []
    for p in paths:
        out.append(await upload_file(p))
    return out


def is_http(v: str) -> bool:
    return str(v or "").startswith(("http://", "https://"))


async def ensure_url(v: str) -> str:
    """`http(s)://…` 直接用；本机路径先上传。

    ★ 放在这一层而不是让调用方各自处理 —— 实测踩到：
      调用方忘了转，本机路径就被原样当成 URL 发给平台，报一句很难懂的错。
      官方原文也明确：inputs 只收 file_upload 返回的 URL 或公网 URL。
    """
    if not v:
        return ""
    return v if is_http(v) else await upload_file(v)


# ──────────────────────── 生成：文生视频 / 图生视频 ────────────────────────


def _args(pairs: Dict[str, Any]) -> List[Dict[str, str]]:
    """把 dict 变成官方要的 `[{name,value}]`（值必须是字符串）。"""
    return [{"name": k, "value": str(v)} for k, v in pairs.items()
            if v is not None and v != ""]


def _inputs(pairs: Dict[str, str]) -> List[Dict[str, str]]:
    return [{"name": k, "inputType": "URL", "url": v}
            for k, v in pairs.items() if v]


async def text_to_video(prompt: str, *, model: str = "",
                        duration: float = 5, aspect_ratio: str = "16:9",
                        resolution: str = "720p", enable_audio: bool = False,
                        rationale: str = "") -> str:
    """文生视频。返回 generation_id。

    ⚠ 官方明确：**文生视频不能用角色 Element**。要角色一致就去 image_to_video。
    """
    model = model or DEFAULT_VIDEO_MODEL
    d = clamp_duration(model, duration)
    a = _args({
        "prompt": prompt, "duration": d, "aspect_ratio": aspect_ratio,
        "resolution": resolution if resolution in VIDEO_RESOLUTIONS else "720p",
        "imageCount": 1,
    })
    # enable_audio 只有部分模型有；不加判断地传会被拒
    if model in ("kling-video-v2_5", "kling-video-v2_6", "kling-video-v3_0",
                 "kling-video-v3_0_omni"):
        a.extend(_args({"enable_audio": "true" if enable_audio else "false"}))
    res = await K.call_tool_json("text_to_video", {
        "model": model, "arguments": a,
        "rationale": rationale or f"按剧本生成 {d} 秒视频镜头",
    }, timeout=180)
    return _gid(res)


async def image_to_video(prompt: str, *, model: str = "",
                         first_image: str = "", tail_image: str = "",
                         images: Optional[List[str]] = None,
                         element_ids: Optional[List[str]] = None,
                         element_bind: Optional[Dict[str, str]] = None,
                         duration: float = 5, aspect_ratio: str = "",
                         resolution: str = "720p", enable_audio: bool = False,
                         rationale: str = "") -> str:
    """图生视频，支持角色 Element。返回 generation_id。

    ★ **这是可灵这条路上"角色一致性"的正解**：官方原文说文字/文生图
      都不能带 Element，只有 image_to_video（和 image_to_image）可以。
    ★ 元素写法：prompt 里用 `<<<id>>>` 占位，同时把 id 放进 `elements`。
    ★ 图片可以给本机路径，会自动上传。
    """
    model = model or DEFAULT_VIDEO_MODEL
    # 本机路径 → 上传成 URL（官方只收 URL）
    first_image = await ensure_url(first_image)
    tail_image = await ensure_url(tail_image)
    images = [await ensure_url(u) for u in (images or []) if u]

    d = clamp_duration(model, duration)
    pairs = {"prompt": prompt, "duration": d, "resolution":
             resolution if resolution in VIDEO_RESOLUTIONS else "720p",
             "imageCount": 1}
    # ★ 不管 first_image 是不是空，都不要传 aspect_ratio（可灵 kling-video-v3_0 不接受）
    if False and aspect_ratio and not (first_image or tail_image or images):
        pairs["aspect_ratio"] = aspect_ratio
    if model in ("kling-video-v2_5", "kling-video-v2_6", "kling-video-v3_0",
                 "kling-video-v3_0_omni"):
        pairs["enable_audio"] = "true" if enable_audio else "false"
    import logging as _lg
    _lg.getLogger("videoforge.kling_mcp_tools").error(
        "image_to_video pairs=%r, aspect_ratio=%r, first=%r", pairs, aspect_ratio, first_image)

    # —— inputs：按模型声明的名字来 ——
    inp: Dict[str, str] = {}
    if model in ("kling-video-o1", "kling-video-v3_0_omni"):
        # 这两个模型收 image_1..image_N（不是 first_image）
        seq = list(images or [])
        if first_image:
            seq.insert(0, first_image)
        for i, u in enumerate(seq[:7], start=1):
            inp[f"image_{i}"] = u
    else:
        if first_image:
            inp["first_image"] = first_image
        if tail_image:
            inp["tail_image"] = tail_image
        # ★★ 2026-09-29 修真 bug：**非 omni 模型收到 `images=[...]` 会被拒**。
        #   旧代码在这里写的是：
        #       for i, u in enumerate(images or [], start=1):
        #           inp.setdefault(f"image_{i}", u)
        #   但非 omni 模型（含默认的 `kling-video-v3_0`）**只声明
        #   `first_image` / `tail_image` 两个输入**，根本没有 `image_1`。
        #   于是调用方只要用 `images=[场景图, 角色图]` 这种写法，官方就回：
        #       Unknown input 'image_1' for this model.
        #       Declared inputs: first_image, tail_image.
        #   ——而且这是**提交阶段就拒**，不消耗灵感值，但用户完全跑不通。
        #   正确做法：在非 omni 模型上，把 images 按顺序落到它真有的槽位：
        #       images[0] → first_image（首帧）
        #       images[1] → tail_image（尾帧）
        #       多余的忽略（这两个模型只吃两张图）。
        _seq = [u for u in (images or []) if u]
        if not inp.get("first_image") and _seq:
            inp["first_image"] = _seq.pop(0)
        if not inp.get("tail_image") and _seq:
            inp["tail_image"] = _seq.pop(0)
        if _seq:
            logger.info("可灵 %s 只接受 first_image/tail_image 两张输入，"
                        "已忽略多余的 %d 张图", model, len(_seq))

    # —— elements：只在这个模型支持时带 ——
    if element_ids:
        if model not in VIDEO_ELEMENT_MODELS:
            raise ValueError(
                f"{model} 不支持角色 Element（只有 "
                f"{sorted(VIDEO_ELEMENT_MODELS)} 支持）。"
                f"要角色一致请换 kling-video-v3_0 —— 官方原文："
                f"视频元素只能用在 v3_0 / v3_0_omni 的 image_to_video。")
        elems = [{"id": e, "bindName": (element_bind or {}).get(e, f"角色{i+1}")}
                 for i, e in enumerate(element_ids)]
        pairs["elements"] = json.dumps(elems, ensure_ascii=False)

    res = await K.call_tool_json("image_to_video", {
        "model": model, "arguments": _args(pairs),
        "inputs": _inputs(inp),
        "rationale": rationale or f"以定妆照/首帧为准生成 {d} 秒视频镜头",
    }, timeout=180)
    return _gid(res)


async def image_to_image(prompt: str, images: List[str], *, model: str = "",
                         element_ids: Optional[List[str]] = None,
                         aspect_ratio: str = "16:9", count: int = 1,
                         rationale: str = "") -> str:
    """图生图（含图片 Element）。**图片 Element 在这里是官方支持的正路。**"""
    model = model or DEFAULT_IMAGE_MODEL
    images = [await ensure_url(u) for u in (images or []) if u]
    pairs: Dict[str, Any] = {"prompt": prompt, "imageCount": count,
                             "aspect_ratio": aspect_ratio}
    if element_ids:
        elems = [{"id": e, "bindName": f"主体{i+1}"}
                 for i, e in enumerate(element_ids)]
        pairs["elements"] = json.dumps(elems, ensure_ascii=False)
    inp = {f"image_{i}": u for i, u in enumerate(images[:10], start=1)}
    res = await K.call_tool_json("image_to_image", {
        "model": model, "arguments": _args(pairs), "inputs": _inputs(inp),
        "rationale": rationale or "按参考图生成/改写画面",
    }, timeout=180)
    return _gid(res)


async def motion_control(image: str, *, video: str = "", motion_id: str = "",
                         prompt: str = "",
                         motion_direction: str = "motion_direction",
                         resolution: str = "720p",
                         keep_original_sound: bool = False,
                         element_ids: Optional[List[str]] = None,
                         model: str = "kling-video-v3_0",
                         rationale: str = "") -> str:
    """**白膜的标准接法**：把白膜视频当"动作来源"，套到角色定妆照上。

    ★ 为什么用这个而不是 reference_video：
      · `inputs.video` 直接吃我们上传的白膜 mp4（file_upload，无需公网）；
      · `motion_direction` = 完全跟随参考视频的动作/运镜；
      · 角色一致性用 `elements`（v3_0 支持 1 个）。
    ★ 官方限制：`image_direction` 只支持 3–10 秒的动作；`motion_direction`
      跟随参考视频，没有这条限制 —— 白膜一般更长，所以默认用 motion_direction。
    ★ image / video 都可以给本机路径，会自动上传。
    """
    image = await ensure_url(image)
    video = await ensure_url(video)
    pairs: Dict[str, Any] = {
        "motionDirection": motion_direction,
        "resolution": resolution if resolution in MOTION_RESOLUTIONS else "720p",
        "keepOriginalSound": "true" if keep_original_sound else "false",
    }
    if prompt:
        pairs["prompt"] = prompt
    if motion_id:
        pairs["motionId"] = motion_id
    if element_ids and model == "kling-video-v3_0":
        pairs["elements"] = json.dumps(
            [{"id": element_ids[0], "bindName": "主角"}], ensure_ascii=False)

    inp: Dict[str, str] = {"image": image}
    if video:
        inp["video"] = video
    res = await K.call_tool_json("motion_control", {
        "model": model, "arguments": _args(pairs), "inputs": _inputs(inp),
        "rationale": rationale or "按白膜预演的动作与运镜生成正式镜头",
    }, timeout=180)
    return _gid(res)


def _gid(res: Any) -> str:
    """从生成工具的返回里取 generation_id（结构可能有几种，都兜住）。"""
    if isinstance(res, dict):
        for k in ("generation_id", "generationId", "id", "task_id", "taskId"):
            if res.get(k):
                return str(res[k])
        for v in res.values():
            if isinstance(v, dict):
                for k in ("generation_id", "generationId", "id"):
                    if v.get(k):
                        return str(v[k])
    raise RuntimeError(f"可灵没有返回 generation_id：{str(res)[:300]}")


# ──────────────────────── 任务状态 ────────────────────────


def extract_result(res: Any) -> Dict[str, Any]:
    """把 query_tasks 的返回整理成统一结构（状态 + 视频/图片 URL + 失败原因）。

    ★★ 失败原因一定要挖出来（2026-09-24 真花钱时踩到的）：
      可灵把"为什么失败"放在 `works[].message` / `works[].status` 里，
      而**顶层 status 只有一个 FAIL**。第一版没读 works 里的 message，
      于是用户（和我）只看到「状态 fail」四个字 —— 钱没扣、原因不明，
      完全没法排查。实测那次是：

          works: [{status: "UNQUALIFIED_INPUT",
                   message: "No valid characters detected in the video"}]

      这句话直接指出了问题（参考视频里没有人），价值极高。
    """
    d = res if isinstance(res, dict) else {}
    status = str(d.get("status") or d.get("state") or "").lower()
    urls = d.get("url") or d.get("urls") or []
    if isinstance(urls, str):
        urls = [urls]
    nowm = d.get("urlWithoutWatermark") or d.get("url_without_watermark") or []
    if isinstance(nowm, str):
        nowm = [nowm]
    cover = d.get("cover") or d.get("cover_url") or ""
    # 有些返回把结果埋在 works / data 里
    msgs: List[str] = []
    for key in ("works", "data", "result", "results"):
        v = d.get(key)
        if isinstance(v, list) and v and isinstance(v[0], dict):
            for w in v:
                u = w.get("url") or w.get("video_url") or w.get("resource")
                if u and u not in urls:
                    urls.append(u)
                # ★ 去水印版与封面**只在作品级**（实测真返回里顶层根本没有），
                #   第一版只读顶层，于是"取到视频却取不到封面/去水印版"。
                for k in ("urlWithoutWatermark", "url_without_watermark"):
                    nw = w.get(k)
                    if nw and nw not in nowm:
                        nowm.append(nw)
                if not cover:
                    cover = (w.get("coverUrl") or w.get("cover_url")
                             or w.get("cover") or "")
                # 作品级的状态与消息就是失败原因所在
                m = w.get("message") or w.get("error") or w.get("error_message")
                ws = w.get("status") or ""
                if m:
                    msgs.append(f"[{ws}] {m}" if ws else str(m))
                elif ws and str(ws).lower() not in (
                        "succeed", "success", "succeeded", "ok", "finished"):
                    msgs.append(f"[{ws}]（平台没给具体原因）")
        elif isinstance(v, dict):
            m = v.get("message") or v.get("error")
            if m:
                msgs.append(str(m))

    failed = status in ("failed", "fail", "error")
    base_err = K.error_of(d) if isinstance(d, dict) else ""
    err = base_err or ("；".join(msgs) if failed else "")
    return {
        "status": status,
        "done": status in ("succeed", "success", "succeeded", "finished", "completed"),
        "failed": failed,
        "urls": urls,
        "urls_no_watermark": nowm,
        "cover": cover,
        "messages": msgs,
        "raw": d,
        "error": err,
    }


async def query_task(generation_id: str) -> Dict[str, Any]:
    """查一次任务状态。返回里会带原始结构，便于排查。"""
    res = await K.call_tool_json("query_tasks", {"generationId": generation_id})
    return extract_result(res)


async def wait_task(generation_id: str, *, timeout: float = 900.0,
                    interval: float = 6.0,
                    on_tick=None) -> Dict[str, Any]:
    """轮询到出结果或超时。**官方原文：生成结果 URL 24 小时后过期。**

    ★★ 一个花钱买来的教训（2026-09-24）：
      平台会先回 `status=COMPLETED`、**过一会儿才把 `works` 挂上去**。
      我原来一看到 done 就立刻返回，于是出现过「状态完成、灵感值扣了 30、
      但 urls 是空的」——脚本报"没拿到成品"，而东西其实是好的。
      现在：**done 但没有 URL 也不算数**，继续轮询，直到拿到 URL 或超时。
      （失败没有 URL 是正常的，那种情况立刻返回。）
    """
    t0 = time.time()
    last: Dict[str, Any] = {}
    while time.time() - t0 < timeout:
        try:
            last = await query_task(generation_id)
        except Exception as e:  # noqa: BLE001
            last = {"status": "error", "failed": True, "error": str(e), "urls": []}
        if last.get("failed"):
            last["elapsed"] = round(time.time() - t0, 1)
            return last
        if last.get("done") and (last.get("urls") or last.get("urls_no_watermark")):
            last["elapsed"] = round(time.time() - t0, 1)
            return last
        if on_tick:
            try:
                on_tick(last, time.time() - t0)
            except Exception:
                pass
        import asyncio
        await asyncio.sleep(interval)
    last.update({"elapsed": round(time.time() - t0, 1), "timeout": True})
    if last.get("done") and not last.get("urls"):
        # 状态完成了但始终没等到产物 —— 如实说清楚，别让调用方以为成功了
        last["error"] = ("平台报完成，但一直没给出成品地址"
                         "（可能还在转码，稍后用 query_task 再查一次这个任务）")
    return last


# ──────────────────────── Element（角色一致性）────────────────────────


async def element_list() -> List[Dict[str, Any]]:
    res = await K.call_tool_json("element_list", {})
    if isinstance(res, list):
        return res
    for k in ("elements", "items", "data", "list"):
        if isinstance(res.get(k), list):
            return res[k]
    return []


async def element_get(element_id: str) -> Dict[str, Any]:
    res = await K.call_tool_json("element_get", {"id": str(element_id)})
    return res if isinstance(res, dict) else {"raw": res}


async def element_create(name: str, description: str, tags: List[str], *,
                         cover_url: str = "",
                         secondary_urls: Optional[List[str]] = None,
                         video_url: str = "", voice_url: str = "") -> str:
    """建一个可复用的"主体"（角色/场景/道具）。

    ★ 图片主体：cover + 1~3 张 secondary，**至少要一个标签**，
      标签必须从 ELEMENT_TAGS 里选（官方点名）。
    ★ 视频主体：只给 resource.video（时长 3–60s、fps>23.8、短边≥700px）。
    """
    tags = [t for t in (tags or []) if t in ELEMENT_TAGS] or ["角色"]
    res: Dict[str, Any] = {}
    if video_url:
        res["video"] = video_url
    else:
        if not cover_url:
            raise ValueError("图片 Element 必须有 cover（先 file_upload 拿 URL）")
        res["cover"] = cover_url
        sec = []
        for i, u in enumerate((secondary_urls or [])[:3]):
            sec.append({"name": f"参考图{i+1}", "inputType": "URL", "url": u})
        if sec:
            res["secondary"] = sec
    if voice_url:
        res["voice"] = voice_url

    out = await K.call_tool_json("element_create", {
        "name": name, "description": description or name,
        "tags": tags, "resource": res,
    }, timeout=180)
    if isinstance(out, dict):
        for k in ("id", "element_id", "elementId"):
            if out.get(k):
                return str(out[k])
        for v in out.values():
            if isinstance(v, dict) and (v.get("id") or v.get("element_id")):
                return str(v.get("id") or v.get("element_id"))
    raise RuntimeError(f"可灵没返回 element id：{str(out)[:300]}")


async def ensure_character_element(character: Dict[str, Any],
                                   image_paths: List[str]) -> str:
    """把 VideoForge 里的一个角色变成可灵 Element（幂等：已有就复用）。

    复用规则：角色行里存了 `element_id` 且 element 还查得到，就直接用。
    """
    from . import db as _db
    existing = (character.get("element_id") or "").strip()
    if existing:
        try:
            got = await element_get(existing)
            if got and (got.get("id") or got.get("element_id")):
                return existing
        except Exception:
            logger.info("可灵 Element %s 已失效，重建", existing)

    urls = await upload_many(image_paths)
    eid = await element_create(
        name=character.get("name") or "角色",
        description=(character.get("description")
                     or character.get("feature_text") or "")[:400],
        tags=["角色"],
        cover_url=urls[0],
        secondary_urls=urls[1:],
    )
    if character.get("id"):
        try:
            _db.update_character(character["id"], element_id=eid,
                                 element_status="kling_mcp")
        except Exception:
            logger.warning("Element id 存回角色失败（不影响生成）", exc_info=True)
    return eid


# ──────────────────────── 白膜 → 正式镜头（本模块的重点）────────────────────────


async def white_model_to_video(previz_video: str, subject_image: str,
                               prompt: str = "", *, model: str = "kling-video-v3_0",
                               resolution: str = "720p",
                               element_ids: Optional[List[str]] = None,
                               spec: Optional[Dict[str, Any]] = None) -> str:
    """白膜 → 正式镜头（**首帧用定妆照，白膜只提供文字化的运镜/走位/节奏**）。

    ══════════════════════════════════════════════════════════════════
    ★★ 这条路我试错了两次，两次都是真花钱试出来的。结论以这里为准。
    ══════════════════════════════════════════════════════════════════

    **第一次（错）**：接到 `motion_control`，把白膜当 `inputs.video`。
      平台判：`UNQUALIFIED_INPUT / No valid characters detected in the video`
      —— 因为 motion_control 要的是**含真人的动作视频**，白膜是灰方块。

    **第二次（也错）**：把白膜抽首帧当 `image_to_video` 的 `first_image`。
      生成成功了（5.04 秒 / 1280x720），但**画面就是白膜本身**：
      灰方块、鼻子标记、三分线全在。可灵忠实地把输入图"动"了起来 ——
      白膜的画风渗进了成片。这正是 previz 模块反复警告过的那件事
      （"白膜颜色会渗进成片"），我却在把它当输入图时忘了。

    **正确做法（本函数）**：白膜在可灵上**不能当像素级控制信号**，
    因为可灵的 image_to_video 只有图片输入（first_image/tail_image/image_1..N），
    **没有参考视频入口**。所以：
      · `first_image` = **角色定妆照**（画面质感与长相由它决定）
      · 白膜的贡献 = **文字化的运镜 / 走位 / 节奏**（由 `spec` 生成）
      · `elements` = 角色一致性
    白膜的价值回到它最该在的位置：**让人在花钱前确认调度**。

    ★ 想要"像素级运动控制"，得换平台：即梦/Seedance 的 `reference_video`
      接受公网 URL（本项目实测过），白膜在那边可以当真正的参考视频。
    """
    note = _previz_text_note(previz_video, spec)
    full = (prompt or "生成这一段镜头").strip()
    if note:
        full = f"{full}。{note}"
    return await image_to_video(
        full, model=model, first_image=subject_image,
        element_ids=element_ids, resolution=resolution,
        rationale="用角色定妆照作为首帧保证画面质感与人物一致性；"
                  "白膜提供运镜/走位/节奏的调度依据（可灵无参考视频入口，"
                  "故以文字形式注入）")


def _previz_text_note(previz_video: str, spec: Optional[Dict[str, Any]]) -> str:
    """把白膜的调度信息写成一句能塞进 prompt 的话。

    ★ 为什么要这一步：可灵的 image_to_video **没有参考视频入口**，
      白膜的运动轨迹没法以像素形式约束生成。能做的是把它的结论
      （运镜名、主体走位、时长）写进提示词 —— 这也是 previz_ref 里
      "降级路径"的同一个思路：白膜约束不了运动时，至少约束文字。
    """
    bits: List[str] = []
    cam = ""
    if isinstance(spec, dict):
        cam = str(spec.get("camera") or "")
    if cam:
        try:
            from core.previz import LABELS_CAMERA
            bits.append(f"运镜：{LABELS_CAMERA.get(cam, cam)}")
        except Exception:
            bits.append(f"运镜：{cam}")
    try:
        dur = _probe_seconds(previz_video)
        if dur:
            bits.append(f"时长约 {dur:.0f} 秒")
    except Exception:
        pass
    if not bits:
        return ""
    return "；".join(bits) + "（依据白膜预演的调度）"


def _probe_seconds(path: str) -> float:
    try:
        from core.previz import probe_duration
        return float(probe_duration(path) or 0)
    except Exception:
        return 0.0


def _previz_first_frame(previz_video: str, *, out_dir: str = "") -> str:
    """从白膜视频里抽第一帧（PNG）。

    ⚠ 保留这个函数是为了**诊断与留档**用途，**不要**再拿它当
      `image_to_video` 的 first_image —— 实测那样会把白膜的灰模画风
      直接带进成片（见 white_model_to_video 的说明）。
    """
    import subprocess
    import tempfile
    src = Path(previz_video)
    if not src.exists():
        raise FileNotFoundError(f"白膜文件不存在：{previz_video}")
    d = out_dir or tempfile.mkdtemp(prefix="vf_previz_frame_")
    Path(d).mkdir(parents=True, exist_ok=True)
    out = str(Path(d) / (src.stem + "_first.png"))
    if os.path.exists(out):
        return out
    from core.ffmpeg_manager import get_ffmpeg_for_postprocess
    p = subprocess.run(
        [get_ffmpeg_for_postprocess(), "-y", "-v", "error", "-i", str(src),
         "-frames:v", "1", out],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120)
    if p.returncode != 0 or not os.path.exists(out):
        raise RuntimeError(f"白膜抽首帧失败 rc={p.returncode}: {(p.stderr or '')[:200]}")
    return out


def explain_error(text: str) -> Dict[str, str]:
    """按官方错误表把平台错误翻成人话 + 给出下一步。"""
    t = (text or "").lower()
    table = [
        (("insufficient", "credit", "灵感值不足", "余额不足"),
         "灵感值不够", f"去充值：{CREDITS_URL}"),
        (("moderation", "disallowed", "审核", "违规", "sensitive"),
         "内容被审核拒绝", "改prompt/换参考图，去掉真人/IP/敏感词"),
        (("not supported", "not support", "不支持"),
         "该模型不支持这组参数", "换模型或去掉该参数（比如时长/分辨率）"),
        (("timeout", "timed out"), "生成超时", "稍后重试，参数原样保留"),
        (("service", "unavailable", "busy"), "服务暂时不可用", "稍后再试"),
        (("parameter", "invalid", "format"), "参数格式不对", "照 who_am_i 的取值改"),
    ]
    for keys, kind, fix in table:
        if any(k in t for k in keys):
            return {"kind": kind, "fix": fix}
    return {"kind": "未知错误", "fix": "把原始错误贴出来，先别自动重试（官方要求：不自动重发）"}


async def preflight() -> Dict[str, Any]:
    """生成之前的一次性体检：授权 → 额度 → 模型清单。"""
    st = K.status()
    out: Dict[str, Any] = {"authorized": bool(st.get("token_valid")
                                              or st.get("has_refresh")),
                           "store": st.get("store")}
    if not out["authorized"]:
        out["ok"] = False
        out["how"] = "还没授权：跑 kling_mcp_auth 拿链接，用户在浏览器点同意"
        return out
    try:
        c = await credits()
        out["credits"] = c
    except Exception as e:  # noqa: BLE001
        out["credits"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        who = await who_am_i(["text_to_video", "image_to_video", "motion_control"])
        out["video_models"] = models_for(who, "image_to_video")
        out["motion_models"] = models_for(who, "motion_control")
        out["mcp_version"] = who.get("mcpVersion")
    except Exception as e:  # noqa: BLE001
        out["models_error"] = f"{type(e).__name__}: {e}"
    out["ok"] = bool((out.get("credits") or {}).get("enough"))
    if not out["ok"]:
        out["topup_url"] = CREDITS_URL
        out["note"] = "灵感值为 0 —— 现在提交任何生成都会被拒。"
    return out
