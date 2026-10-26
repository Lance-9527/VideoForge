"""
可灵 Kling 适配器（快手）—— 按 **Kling 3.0 官方 API 契约**重写

═══════════════════════════════════════════════════════════════════
旧版为什么必须重写（三条都是会"静默少发东西"的错）
═══════════════════════════════════════════════════════════════════
旧实现（`/v1/videos/image2video` + 顶层 `image`/`image_tail` + `model_name` +
`mode=std/pro`）是**可灵 v1 那套旧接口**。3.0 的官方契约已经换成：

  · 请求体是 **`contents[]` 数组**（不是 `image`/`image_tail` 顶层字段）；
  · 模型**写在路径里**（`/image-to-video/kling-3.0`），body 里**没有** `model_name`；
  · 没有 `mode`（std/pro）这个概念 —— 3.0 按**秒**计费；
  · 查询任务走**统一的** `GET /tasks?task_ids=`，不再是 `/v1/videos/image2video/{id}`；
  · 接口域名从 `api.klingai.com` 换成了 `api-singapore.klingai.com`。

所以旧代码在 3.0 上：`image`/`image_tail` 发不进去（首尾帧丢掉）、
`mode` 是垃圾字段、轮询地址 404 —— 用户看到的是"配了 Key 也用不了"。

**唯一认证方式**（3.0）：`Authorization: Bearer <API Key>` [5]。
（那套 AccessKey+SecretKey 签 JWT 的是"旧版设计标准"，官方说明：
"模型版本信息位于路径中是新版，作为 `model_name` 参数值传递的是旧版" —— 与上面一致。[5]）

═══════════════════════════════════════════════════════════════════
联网核实记录（2026-09，全部用 web_fetch 读过原文）
═══════════════════════════════════════════════════════════════════
  [1] Kling 3.0 & 3.0 Omni - Image to Video
      https://kling.ai/document-api/api/video/3-0-omni/image-to-video.md
  [2] Kling 3.0 & 3.0 Omni - Text to Video
      https://kling.ai/document-api/api/video/3-0-omni/text-to-video.md
  [3] Kling 3.0 & 3.0 Omni - Element Management
      https://kling.ai/document-api/api/video/3-0-omni/elements.md
  [4] Video Capability Map（更新于 2026-05-19）
      https://kling.ai/document-api/guides/capability-map/video.md
  [5] Get Started - Authentication
      https://kling.ai/document-api/api/get-started/authentication.md
  [6] Basic APIs Pricing - Video
      https://kling.ai/document-api/pricing/base/video.md
  [7] Kling 3.0 Turbo - Image to Video / Text to Video
      https://kling.ai/document-api/api/video/3-0-turbo/image-to-video.md
      https://kling.ai/document-api/api/video/3-0-turbo/text-to-video.md
  [8] Get Started - Error Codes
      https://kling.ai/document-api/api/get-started/error-codes.md
  [9] Lip Sync（本适配器**未接入**，见文末"未接入"）
      https://kling.ai/document-api/api/video/lip-sync.md

已核实（逐条有出处）：
  · 创建任务：`POST /image-to-video/kling-3.0`（有首帧时）、
    `POST /text-to-video/kling-3.0`（纯文本）；域名为
    `https://api-singapore.klingai.com`（官方："端点已从 api.klingai.com 换到
    api-singapore.klingai.com，适合服务器在中国境外的用户"）。[1][2][5]
  · 请求体（图生视频）：`contents[]`，元素 `{"type":"prompt","text":…}` /
    `{"type":"first_frame","url":…}` / `{"type":"last_frame","url":…}` /
    `{"type":"element","element_id":…,"id":…}`；`settings` 里放
    `multi_shot` / `audio` / `resolution` / `duration`。[1]
  · 请求体（文生视频）：顶层 `prompt`（字符串）+ `settings`，
    `settings` 里**多一个** `aspect_ratio`（16:9/9:16/1:1，默认 16:9）。[2]
  · `settings.duration`：枚举 **3,4,…,15**，默认 5。[1][2] ⇒ `min_duration=3`
    `max_duration=15`（旧代码写 10/5 会把 3~4s、11~15s 全判成非法）。
  · `settings.resolution`：`720p` / `1080p` / `4k`，默认 720p（Omni 同）。[1][2]
  · `settings.multi_shot`：bool，**默认 true**；置 false 时多镜头 prompt 不会出多镜头。[1][2]
  · `settings.audio`：`native`（生成与画面匹配的原生音频）/ `off`，**默认 off**。[1][2]
  · **多镜头是官方原生能力**（Kling 3.0 / 3.0 Omni / 3.0 Turbo 支持；
    O1 / 2.6 / 2.5 Turbo **不支持**）[4]，prompt 模板固定为
    `"shot n, m, words; shot n, m, words;"`（**半角分号**分隔）：
      a. n = 镜头序号，最多 **6** 个（至少 1 个）；
      b. m = 该镜头秒数，**每镜 ≥1 秒**，且**各镜之和必须等于视频总时长**；
      c. words = 镜头描述，**≤512 字符**。[1][2][7]
    ★ 这是目前**唯一**官方"一次调用出多个镜头"的能力，对短剧最关键，见 `build_multi_shot_prompt()`。
  · `contents[].text`（prompt）**≤3072 字符**（官方建议 ≤2500）。[1]
  · 首/尾帧图：jpg / jpeg / png，**≤50MB**，宽高**最短边 ≥300px**，
    宽高比 **1:2.5 ~ 2.5:1**；**只支持"仅首帧"或"首帧+尾帧"，不支持只给尾帧**。[1]
  · Element（角色一致性）：`{"type":"element","element_id":"…","id":"element_1"}`；
    `element_id` 是**系统生成**的（在 Element 管理接口里注册后查询得到）；
    一次调用**最多 3 个** Element（"Up to 3 Elements can be specified"，
    capability map 同）；prompt 里用 **`@名字`** 指代（官方示例 `@Zhang`，
    并提醒"Element 名字不要互为子串"、"不要和 prompt 里已有内容撞车"）。[1][3][4]
  · Element 注册：`POST /v1/general/advanced-custom-elements`
    （`reference_type=image_refer`，`element_image_list.frontal_image` 必填 +
    1~3 张 `refer_images`；图 ≤10MB、最短边 ≥300px、宽高比 1:2.5~2.5:1）。[3]
  · 查询任务：`GET /tasks?task_ids=<id>`（也支持 `external_task_ids=`，二选一）；
    返回 `data` 是**数组**，元素含 `id` / `status` / `message` / `outputs[]`。[1]
  · 任务状态枚举：`submitted` / `processing` / `succeeded` / `failed`。[1]
  · 视频结果在 `outputs[]` 里 `type == "video"` 的那条上：`url` / `watermark_url` /
    `duration`（字符串，秒）；官方提醒"生成结果 **30 天后清理**，请及时保存"。[1]
  · 计费：3.0 按秒。720P 0.6 / 1080P 0.8 / 4K 3.0 Units 每秒（无原生音频）；
    带原生音频 720P 0.9 / 1080P 1.2 / 4K 3.0。[6]
    ⇒ 4K 是 1080P 的 **3.75 倍**价，所以"4k 被悄悄降级成 1080p"不是小事（见 `normalize_resolution`）。
  · 错误码：HTTP 401/400/403/429/5xx 与业务 code（1000~1004 鉴权、1100~1103 账户、
    1200~1203 参数、1300~1304 策略/限流、5000~5002 服务端）一一对应。[8]
  · Turbo（`/image-to-video/kling-3.0-turbo`）：settings 只有 `resolution`(720p/1080p) /
    `duration`(3~15)；**没有** `multi_shot` / `audio` 字段，**不支持**尾帧与 Element，
    **没有 4K**；i2v 的 `contents[].text` 上限写的是 **2500** 字符（t2v 那页写 3072/建议 2500）。[4][7]

**未核实到的（不编，逐条列明）**：
  · 图生视频能不能多传一个 `settings.aspect_ratio`：i2v 的请求体表里**没有**这个字段
    （只有 t2v 有）[1][2]，多传会被拒还是被忽略**文档没写、未核实**。
    ⇒ 本适配器在 i2v 上**不发** aspect_ratio（图生视频的画幅本来就由首帧决定）。
  · `/tasks` 返回里有没有宽高：`outputs[]` 只列了 `type/id/url/watermark_url/duration`[1]，
    **没有** width/height ⇒ 不猜，`VideoGenResult.width/height` 一律留空
    （真实画幅交给 `core.aspect.guard_clip()` 去量）。
  · Element 的 `id` 字段到底怎么用：文档写"input index ID, used to specify in prompt"
    （听起来 prompt 里引用的是 `id`），但官方示例里 `id` 是 `element_1`/`element_2`，
    而 prompt 用的是 `@Zhang` 这种**名字**；两处**自相矛盾、未核实**。
    ⇒ 本适配器**照官方示例**发 `id=element_N`，并把 prompt 里的 `@名字` **原样透传**
      （`core/platform_refs.py` 也是按"可灵用 Element 名字"实现的）。
  · Element 创建是否收费：计费表里**没有** element 相关条目[6]，**未核实**是否免费。
  · 负面提示词：3.0 的请求体里**没有** `negative_prompt` 字段（旧 v1 才有）；
    t2v 文档只说 prompt "可以同时包含正向与负向描述"[2]，但**没有**给负向部分的
    语法/模板 ⇒ **未核实**，所以本适配器**不擅自拼接**，只打日志把这件事说出来。
  · Turbo 的能力与请求体**互相矛盾**：capability map 说 Turbo 支持多镜头与原生音频[4]，
    但 Turbo 的请求体里既没有 `multi_shot` 也没有 `audio`[7] ⇒ 按"请求体文档更具体"
    处理（不发这两个字段）。
  · 3.0 是否返回 `watermark_url` 之外的清晰度信息、是否所有档位都真出 4K：未实测。

═══════════════════════════════════════════════════════════════════
没错过、但本项目**未接入**的官方能力（列出来免得下个人再翻一遍文档）
═══════════════════════════════════════════════════════════════════
  · 唇形同步 `POST /v1/videos/advanced-lip-sync`（需先调人脸识别拿 `session_id`）[9]
  · Omni 专用端点 `POST /omni-video/kling-3.0-omni`（`refer_image` / `feature_video` /
    `base_video` 视频参考与视频编辑）—— `VideoGenRequest` 里没有对应字段，没接。
  · 数字人 Avatar / 文生音 / 视频配音 —— 同上，不在视频适配器语义内。
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

from core.aspect import normalize_ratio

from . import VideoAdapter, VideoGenRequest, VideoGenResult, register, to_image_ref

logger = logging.getLogger("videoforge.adapter.kling")


# ═══════════════════════════════════════════════════════════════
# 型号档案
#
# ★ 3.0 把模型写进**路径**（body 里没有 model_name），所以"型号"决定的是
#   端点 + 能力边界，不是一个 body 字段。这里只登记**核实过**的三个 id。
# ═══════════════════════════════════════════════════════════════

DEFAULT_MODEL = "kling-3.0"

_PROFILE_3_0: Dict[str, Any] = {
    "id": "kling-3.0",
    "image2video": "/image-to-video/kling-3.0",     # [1]
    "text2video": "/text-to-video/kling-3.0",       # [2]
    "resolutions": ["720p", "1080p", "4k"],         # [1][2][4]
    "last_frame": True,                             # [1][4]
    "elements": True,                               # [1][3][4]
    "multi_shot": True,                             # settings 里有这个字段 [1][2]
    "audio": ["native", "off"],                     # [1][2]
    "prompt_max": 3072,                             # [1]
}

_PROFILE_TURBO: Dict[str, Any] = {
    "id": "kling-3.0-turbo",
    "image2video": "/image-to-video/kling-3.0-turbo",   # [7]
    "text2video": "/text-to-video/kling-3.0-turbo",     # [7]
    "resolutions": ["720p", "1080p"],                   # 没有 4K [4][7]
    "last_frame": False,                                # "only First Frame is supported" [7]
    "elements": False,                                  # Element Control: Not Supported [4]
    # ★ 请求体里没有 multi_shot / audio 字段 [7]，但 prompt 模板里的多镜头写法
    #   在 Turbo 的 prompt 文档里是有的 ⇒ 只影响"要不要发这个 settings 字段"。
    "multi_shot": False,
    "audio": [],
    # i2v 页写 2500，t2v 页写 3072/建议 2500 —— 取保守的 2500 [7]
    "prompt_max": 2500,
}

_MODEL_PROFILES: Dict[str, Dict[str, Any]] = {
    "kling-3.0": _PROFILE_3_0,
    "kling-3.0-omni": {**_PROFILE_3_0, "id": "kling-3.0-omni"},
    "kling-3.0-turbo": _PROFILE_TURBO,
}

_MODEL_ALIASES: Dict[str, str] = {
    "kling3": "kling-3.0", "kling3.0": "kling-3.0", "kling-3": "kling-3.0",
    "kling-3.0-omni": "kling-3.0-omni", "kling-3-omni": "kling-3.0-omni",
    "kling-omni": "kling-3.0-omni",
    "kling-3.0-turbo": "kling-3.0-turbo", "kling-3-turbo": "kling-3.0-turbo",
}

# 已知的**旧版**型号名（旧接口：model_name 放 body，端点 /v1/videos/*）。
# 本适配器只实现了新版 3.0 契约 —— 这些型号会被送到 3.0 接口，
# 但**绝不静默**：日志 + `raw["_videoforge_warnings"]` 里都会写明。
_LEGACY_MODEL_RE = re.compile(r"^kling-(v1|v2|1\.|2\.|o1)", re.I)


# ═══════════════════════════════════════════════════════════════
# 多镜头 prompt（纯函数，可单测）
#
# ★ 为什么要做成一组纯函数：官方模板有三条硬约束（≤6 镜、每镜 ≥1s、
#   **各镜之和 == 总时长**），其中"之和等于总时长"是**平台侧**才会校验的，
#   违反了要么报错要么退化成单镜头 —— 一次 10 秒的 4K 调用要 30 Units，
#   在花钱之前用纯函数拦住最划算。
# ═══════════════════════════════════════════════════════════════

# 官方模板："shot n, m, words;"（半角分号分隔）。这里也容忍全角逗号/分号，
# 因为中文剧本里手写时很容易打出全角标点 —— 但**生成**时统一输出半角。
_SHOT_SEG = re.compile(
    r"\s*shot\s*(\d{1,2})\s*[,，]\s*(\d{1,2})\s*[,，]\s*([^;；]*)\s*[;；]",
    re.I,
)
# 只用来判断"用户是不是想写多镜头"（提示用，不做校验）
_SHOT_HINT = re.compile(r"shot\s*\d+\s*[,，]\s*\d+\s*[,，]", re.I)

MAX_SHOTS = 6            # 官方：最多 6 个镜头 [1][2]
SHOT_TEXT_MAX = 512      # 官方：单个镜头描述 ≤512 字符 [1][2]

# 报错前缀（两处共用，别让同一条规则的文案漂移）
_MS_HINT = ("多镜头 prompt 不合法（官方模板 `shot n, m, 描述;`，"
            "≤6 镜、每镜 ≥1s、各镜之和 = 总时长、单镜描述 ≤512 字）：")


def parse_multi_shot(prompt: str) -> List[Dict[str, Any]]:
    """把 `"shot 1, 3, 描述; shot 2, 7, 描述;"` 解析成镜头列表。

    ★ 只在**整个** prompt 都是官方模板时才返回结果；只要有一段对不上就返回 `[]`
      —— 宁可不认，也不去猜用户那段散文里哪句是镜头（猜错会误拦正常请求）。
    """
    s = str(prompt or "")
    pos, out = 0, []
    while pos < len(s):
        if not s[pos:].strip():
            break
        m = _SHOT_SEG.match(s, pos)
        if not m:
            return []
        out.append({"number": int(m.group(1)),
                    "duration": int(m.group(2)),
                    "text": m.group(3).strip()})
        pos = m.end()
    return out


def validate_multi_shot(shots: Sequence[Dict[str, Any]],
                        total_duration: Optional[int] = None) -> List[str]:
    """校验官方多镜头模板的三条硬约束，返回**人话**问题列表（空 = 合法）。"""
    errs: List[str] = []
    if not shots:
        return ["多镜头模板是空的"]

    if len(shots) > MAX_SHOTS:
        errs.append(f"官方最多 {MAX_SHOTS} 个镜头，收到 {len(shots)} 个")

    nums = [int(s.get("number") or 0) for s in shots]
    if nums != list(range(1, len(shots) + 1)):
        errs.append(f"镜头序号必须从 1 连续递增（官方：shot 1…{MAX_SHOTS}），收到 {nums}")

    short = [(i + 1, int(s.get("duration") or 0)) for i, s in enumerate(shots)
             if int(s.get("duration") or 0) < 1]
    if short:
        errs.append("每个镜头时长必须 ≥1 秒（官方），第 "
                    + "、".join(f"{i} 镜是 {d}s" for i, d in short))

    long_ = [(i + 1, len(str(s.get("text") or ""))) for i, s in enumerate(shots)
             if len(str(s.get("text") or "")) > SHOT_TEXT_MAX]
    if long_:
        errs.append(f"单个镜头描述 ≤{SHOT_TEXT_MAX} 字符（官方），第 "
                    + "、".join(f"{i} 镜 {n} 字符" for i, n in long_))

    empty = [i + 1 for i, s in enumerate(shots) if not str(s.get("text") or "").strip()]
    if empty:
        errs.append("第 " + "、".join(str(i) for i in empty) + " 镜的描述是空的")

    if total_duration:
        got = sum(int(s.get("duration") or 0) for s in shots)
        # ★ 官方原文："the sum of all shot durations must equal the total video duration"
        if got != int(total_duration):
            errs.append(f"各镜时长之和 {got}s 必须**等于**视频总时长 "
                        f"{int(total_duration)}s（官方硬约束）—— "
                        f"请把分镜时长改成 {got}s，或调整各镜秒数")
    return errs


def build_multi_shot_prompt(shots: Sequence[Any],
                            total_duration: Optional[int] = None
                            ) -> Tuple[str, List[str]]:
    """按官方模板拼多镜头 prompt，返回 `(prompt, 问题列表)`。

    `shots` 接受 `{"duration": 3, "text": "…"}` / `(3, "…")` / `[3, "…"]` 三种写法，
    序号按给定顺序自动编号 1..N（官方要求连续）。

    注意：即便有问题也会**照常返回拼好的 prompt**（调用方自己决定是报错还是照发），
    这样调用方能把 prompt 和错误一起展示给用户看。
    """
    norm: List[Dict[str, Any]] = []
    errs: List[str] = []
    for i, item in enumerate(shots or []):
        if isinstance(item, dict):
            dur, text = item.get("duration"), item.get("text", item.get("prompt", ""))
            num = int(item.get("number") or (i + 1))
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            dur, text, num = item[0], item[1], i + 1
        else:
            errs.append(f"第 {i + 1} 个镜头写法不认识：{item!r}"
                        f"（要 {{'duration': 3, 'text': '…'}} 或 (3, '…')）")
            continue
        try:
            dur_i = int(dur)
        except (TypeError, ValueError):
            errs.append(f"第 {i + 1} 个镜头的时长不是整数：{dur!r}")
            continue
        norm.append({"number": num, "duration": dur_i, "text": str(text or "").strip()})

    errs = validate_multi_shot(norm, total_duration) + errs
    text = " ".join(f"shot {s['number']}, {s['duration']}, {s['text']};" for s in norm)
    return text, errs


# ═══════════════════════════════════════════════════════════════
# 错误码 → 人话（官方错误码表 [8]）
# ═══════════════════════════════════════════════════════════════

_ERROR_HINTS: Dict[int, str] = {
    1000: "鉴权失败：检查 Authorization 是否为 `Bearer <API Key>`",
    1001: "鉴权失败：Authorization 是空的（没配 Key？）",
    1002: "鉴权失败：Authorization 无效（Key 写错或已吊销）",
    1003: "鉴权失败：Authorization 还没生效",
    1004: "鉴权失败：Authorization 已过期（重新签发）",
    1100: "账户异常：账号状态不对",
    1101: "账户异常：**账户欠费**（后付费）",
    1102: "账户异常：**资源包用完或过期**（预付费）—— 去可灵控制台续包",
    1103: "无权限：这个账号没开通要调的 API / 模型",
    1200: "请求参数非法",
    1201: "请求参数非法（键名或值不对）—— 按 message 指出的字段改",
    1202: "请求方法或路径不对 —— 核对接口文档",
    1203: "请求的资源不存在（例如模型名不对）",
    1300: "被平台策略拦截",
    1301: "**命中内容安全策略** —— 改提示词/首帧图后重试",
    1302: "触发限流（QPS 超了）—— 降频重试",
    1303: "并发或 QPS 超过资源包上限 —— 降频重试或联系客服提额",
    1304: "命中 IP 白名单策略 —— 联系客服",
    5000: "可灵服务端内部错误 —— 稍后重试",
    5001: "可灵服务暂时不可用（多在维护）—— 稍后重试",
    5002: "可灵服务端超时（有积压）—— 稍后重试",
}


def describe_error(code: Any, message: Any = "", http_status: Any = None) -> str:
    """把可灵的 `code` + HTTP 状态 + message 拼成一句**能照着修**的中文错误。"""
    try:
        c: Optional[int] = int(code)
    except (TypeError, ValueError):
        c = None
    msg = str(message or "").strip() or "（平台没有给 message）"
    parts = ["可灵 API 错误"]
    if http_status is not None:
        parts.append(f"HTTP {http_status}")
    parts.append(f"code={code}")
    head = "，".join(parts)
    hint = _ERROR_HINTS.get(c) if c is not None else None
    out = f"{head}：{msg}"
    if hint:
        out += f"｜处理建议：{hint}"
    elif c not in (0, None):
        out += "｜处理建议：见官方错误码表 https://kling.ai/document-api/api/get-started/error-codes"
    return out


# ═══════════════════════════════════════════════════════════════
# 请求体（纯数据，build_payload 的返回值 —— 便于单测直接断言）
# ═══════════════════════════════════════════════════════════════


@dataclass
class KlingPayload:
    """一次可灵请求的完整描述。"""

    path: str                       # 相对 BASE_URL 的接口路径（`/image-to-video/kling-3.0`）
    body: Dict[str, Any]            # 真正 POST 出去的 JSON
    mode: str                       # image2video / text2video
    ratio: str                      # 已过 `normalize_ratio` 的画幅
    warnings: List[str] = field(default_factory=list)   # 必须让用户看见的降级/未发送说明


# 像 `element_id` 的字符串：系统生成的 id 是短 ASCII（官方示例是 "163"/"162"）[3]
_ELEMENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# 像图片/视频素材的字符串：URL / data URI / 带路径分隔符 / 带素材扩展名
_MEDIA_REF_RE = re.compile(
    r"^(data:|https?://)|[\\/]|\.(jpe?g|png|webp|bmp|gif|mp4|mov)$", re.I)


@register
class KlingAdapter(VideoAdapter):
    """可灵 AI 视频生成（Kling 3.0 / 3.0 Omni / 3.0 Turbo）"""

    name = "kling"
    display_name = "可灵 Kling（快手）"
    description = ("可灵 3.0：单段 3~15s、**原生多镜头**（一次调用出多个镜头）、"
                   "原生音频、Element 角色一致性（≤3 个）、首尾帧、最高 4K")

    # 文生视频的 `settings.aspect_ratio` 枚举就是这三个 [2]
    supported_aspect_ratios = ["16:9", "9:16", "1:1"]
    # ⚠ 这只是**默认型号**（kling-3.0）的能力；Turbo 没有 4k，
    #   真实取值见 `supported_resolutions` 属性（按型号收窄）。
    _DEFAULT_RESOLUTIONS = ["720p", "1080p", "4k"]

    # 官方 duration 枚举 3..15，默认 5 [1][2]
    max_duration = 15
    min_duration = 3
    supports_first_last_frame = True
    supports_character_ref = True
    # 一次调用最多引用 3 个 Element [1][3][4]
    max_character_refs = 3
    # 与 seedance 保持同名（参考素材张数上限），值同上
    max_reference_images = 3
    # 3.0 请求体里**没有** negative_prompt 字段（旧 v1 才有）[1][2]
    supports_negative_prompt = False
    is_local = False
    requires_api_key = True
    usable_for_video = True

    # ★ 官方："端点已从 api.klingai.com 换到 api-singapore.klingai.com" [5]
    BASE_URL = "https://api-singapore.klingai.com"
    TASKS_PATH = "/tasks"

    MAX_PROMPT_CHARS = 3072     # 3.0 [1]
    AUDIO_MODES = ("native", "off")

    # 基类 `VideoAdapter._RES_ALIASES` 把 "4K" 映射成了 **"1080P"**
    # ⚠ 这是个真陷阱：只要走基类的 normalize_resolution，`resolution="4k"`
    #   就会被**静默降级**成 1080p（4K 是 3.0 Units/s，1080P 是 0.8 —— 差 3.75 倍）。
    #   基类文件本轮由别人在改（不能动），所以在子类里把这几个写法**提前截住**。
    _4K_ALIASES = {"4K", "2160P", "2160", "UHD", "3840X2160", "QHD4K"}

    # ──────────── 型号档案 ────────────

    def _model_name(self) -> str:
        return str(self.config.get("model_name") or DEFAULT_MODEL).strip()

    def _profile_key(self) -> str:
        k = self._model_name().lower()
        return _MODEL_ALIASES.get(k, k)

    def _profile(self) -> Dict[str, Any]:
        """当前型号的能力档案。**认不出来就用 3.0 的档案**（并会在日志里说明）。"""
        return _MODEL_PROFILES.get(self._profile_key(), _PROFILE_3_0)

    def _is_known_model(self) -> bool:
        return self._profile_key() in _MODEL_PROFILES

    # ──────────── 能力（随型号变化）────────────

    @property
    def supported_resolutions(self) -> List[str]:        # type: ignore[override]
        return list(self._profile()["resolutions"])

    @property
    def supports_first_last_frame(self) -> bool:         # type: ignore[override]
        """是否有"尾帧"能力（Turbo 只有首帧 [7]）。"""
        return bool(self._profile()["last_frame"])

    @property
    def supports_first_frame(self) -> bool:
        """两个档案都支持首帧 —— 必须显式覆盖：基类是从
        `supports_first_last_frame` 推出来的，Turbo 上会误判成"不支持首帧"。"""
        return True

    @property
    def supports_character_ref(self) -> bool:            # type: ignore[override]
        """Element 只有 3.0 / 3.0 Omni 支持，Turbo 不支持 [4]。"""
        return bool(self._profile()["elements"])

    # ──────────── 分辨率归一 ────────────

    def normalize_resolution(self, res: Any) -> str:
        """把 4K 的常见写法归到 `4k`，**绝不落到基类 "4K"→"1080P" 的别名上**。

        基类 `_RES_ALIASES = {..., "2K": "1080P", "4K": "1080P", ...}` 是给
        "只有 480P~1080P 档位"的模型设计的；可灵 3.0 真的有 4K 档，
        照基类走就会把用户要的 4K **静默换成 1080P**（价格差 3.75 倍）。
        """
        s = str(res or "").strip().upper().replace(" ", "")
        if s in self._4K_ALIASES:
            return "4k"
        return super().normalize_resolution(res)

    def _default_headers(self) -> Dict[str, str]:
        # 3.0 只认 Bearer API Key [5]
        return {
            "User-Agent": "VideoForge/1.0",
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    # ──────────── 小工具 ────────────

    @staticmethod
    def _extra(req: VideoGenRequest) -> Dict[str, Any]:
        return dict(req.extra) if isinstance(req.extra, dict) else {}

    @staticmethod
    def _as_local_ref(ref: Any) -> str:
        """把首/尾帧统一成平台认的写法（URL / `data:` URI / 原样）。

        ★ 为什么适配器也要做这一步：`core/longvideo.py` 的分段续接是拿
          **本地抽帧文件**当下一段首帧的（`grab_last_frame()` 返回本地路径，
          见 longvideo.py:397 `kw["first_frame"] = prev_last_frame`），
          而可灵的 `contents[].url` 只吃 **URL 或 Base64**（官方原文：
          "The content of the input can be provided via URL or Base64"）[1]。
          直接把 `E:\\...\\last.jpg` 发过去平台一定拒 —— `__init__.py` 里的
          `to_image_ref()` 就是为这件事准备的。
        """
        s = str(ref or "").strip()
        if not s or s.startswith(("http://", "https://", "data:")):
            return s
        return to_image_ref(s) or s

    def _compose_text(self, req: VideoGenRequest) -> Tuple[str, List[str]]:
        """最终发给平台的 prompt 文本 + 问题列表。

        ★ 除了"上层直接给 `extra['shots']` 镜头列表"这一种情况会替它拼成官方
          多镜头模板外，**一个字都不改**：prompt 里的 `@角色名` 是可灵的
          **Element 引用语法**（官方：`@xxx`，如 `@Zhang`）[1]，一旦我们"顺手"
          把 `@沈落` 换成别的写法，Element 绑定就断了（模型只能自己猜主体）。
        """
        shots = self._extra(req).get("shots")
        if shots:
            text, errs = build_multi_shot_prompt(shots, req.duration)
            return text, errs
        return str(req.prompt or ""), []

    # ──────────── Element（角色一致性）────────────

    def _element_entries(self, req: VideoGenRequest,
                         warnings: List[str]) -> List[Dict[str, Any]]:
        """组装 `contents[]` 里的 `element` 条目。

        `element_id` 必须是**系统生成**的 id（官方：注册 Element 后查询得到）[3]，
        所以本适配器从两个地方取 id，**绝不去猜**：
          ① `req.extra["element_ids"]`（首选；字符串，或 `{"element_id":…, "id":…}`）；
          ② `req.character_reference` 里**形状像 id** 的字符串。
        其余（图片 URL / data URI / 本地路径）**一律不发** —— 把图片塞进
        `element_id` 平台一定报参数错误，比"少发一个 Element"更糟。

        ⚠ 为什么**不在这里顺手注册** Element（`POST /v1/general/advanced-custom-elements`）：
          ① 注册是**异步任务**（先拿 task_id，再轮询查询才得到 element_id），
             每次生成都现注册要多一轮往返 + 多一次调用；
          ② 官方注册接口只有 `element_name`，而 prompt 里的 `@名字` **必须**与
             Element 名字对得上 —— 适配器手里只有图片、**没有角色名**，
             自己编一个名字再改写 prompt 就等于"替用户改提示词"（明令禁止）；
          ③ 角色形象应该在「角色」页生成一次、注册一次、长期复用，
             而不是每个分镜注册一遍。
          所以：注册流程属于上层（角色表里存 element_id），本适配器只负责把
          **已经拿到的** element_id 正确地放进 `contents[]`。
        """
        if not self._profile()["elements"]:
            return []

        extra = self._extra(req)
        ids: List[Tuple[str, str]] = []          # (element_id, 显式 id 或 "")
        for item in (extra.get("element_ids") or []):
            if isinstance(item, dict):
                eid = str(item.get("element_id") or item.get("id") or "").strip()
                idx = str(item.get("index_id") or "").strip()
            else:
                eid, idx = str(item or "").strip(), ""
            if eid:
                ids.append((eid, idx))

        images: List[str] = []
        others: List[str] = []
        # `character_reference` 里"形状像 element_id"的才当 id；`reference_images`
        # 按语义就是图片（`__init__.py`：参考图 / 图生视频 / 首尾帧），一律不当 id。
        refs: List[Tuple[str, bool]] = (
            [(str(r or ""), True) for r in (req.character_reference or [])]
            + [(str(r or ""), False) for r in (req.reference_images or [])])
        for s, may_be_id in refs:
            s = s.strip()
            if not s:
                continue
            if may_be_id and _ELEMENT_ID_RE.match(s) and not _MEDIA_REF_RE.search(s):
                if s not in [i for i, _ in ids]:
                    ids.append((s, ""))
            elif _MEDIA_REF_RE.search(s):
                images.append(s)
            else:
                others.append(s)

        if images:
            warnings.append(
                f"可灵 Element 只能引用**已注册**的 element_id（官方：先调 "
                f"`POST /v1/general/advanced-custom-elements` 注册、查询后拿到 id）[3]，"
                f"本次收到的是 {len(images)} 张图片/URL，**一张都没有发出去** —— "
                f"把图片塞进 element_id 平台一定拒。prompt 里的 `@角色名` 已原样保留，"
                f"但平台没有对应的 Element 就不会锁定人物形象。"
                f"要真正用上角色一致性：先注册 Element，再把 element_id 放进 "
                f"`req.extra['element_ids']`"
                + (f"（第一张：{images[0][:60]}…）" if len(images[0]) > 60 else f"（第一张：{images[0]}）"))
        if others:
            warnings.append(
                f"`character_reference` 里有 {len(others)} 个既不像 element_id"
                f"（ASCII 字母数字）也不像图片的字符串，已跳过："
                f"{'、'.join(o[:40] for o in others[:3])}"
                + ("…" if len(others) > 3 else ""))

        limit = int(self.max_character_refs or 3)
        if len(ids) > limit:
            dropped = [i for i, _ in ids[limit:]]
            # ★★ 绝不静默截断：这条会同时进 logger.warning 和
            #    `raw["_videoforge_warnings"]`，上层能把它展示给用户。
            warnings.append(
                f"可灵单次最多引用 {limit} 个 Element（官方：Up to 3 Elements can be "
                f"specified）[1] —— 本次收到 {len(ids)} 个，**已丢弃** "
                f"{len(dropped)} 个：{'、'.join(dropped)}。"
                f"被丢弃的角色不会锁定形象（prompt 里的 @名字 还在，但平台没有对应 Element）")
            logger.warning("可灵 Element 超上限：收到 %d 个，只发前 %d 个，丢弃 %s",
                           len(ids), limit, dropped)
            ids = ids[:limit]

        out: List[Dict[str, Any]] = []
        for i, (eid, idx) in enumerate(ids):
            # 官方示例：{"type":"element","element_id":"163","id":"element_1"} [1]
            # `id` 是"输入索引 ID，同一任务内不能重复"；文档没说它必须是 prompt
            # 里的 @记号，prompt 引用用的是 Element **名字**（见文件头"未核实"）。
            out.append({"type": "element", "element_id": eid,
                        "id": idx or f"element_{i + 1}"})
        return out

    # ──────────── 校验 ────────────

    def validate_request(self, req: VideoGenRequest) -> Optional[str]:
        prof = self._profile()
        model = self._model_name()

        # ① 画幅：先过 `normalize_ratio`（"16x9"/"16X9"/"16×9" → "16:9"），
        #    免得后面因为一个写法差异把整条请求判死。
        _ar = normalize_ratio(req.aspect_ratio, default="")
        if _ar:
            req.aspect_ratio = _ar

        # ② prompt：`contents[].text` 是必填，且有硬上限 [1]
        text, shot_errs = self._compose_text(req)
        if not text.strip():
            return "可灵 3.0 的 `contents[].text`（prompt）是必填项，不能为空"
        if shot_errs:
            return _MS_HINT + "；".join(shot_errs)
        if len(text) > prof["prompt_max"]:
            return (f"prompt {len(text)} 字符，超过可灵 {prof['id']} 的上限 "
                    f"{prof['prompt_max']} 字符（官方）—— 请精简提示词")

        # ②′ 多镜头模板：**手写**的也要按官方硬约束校验。
        #    只在"整段都是模板"时才校验（`parse_multi_shot` 对不上就返回 []），
        #    免得把普通散文误判成多镜头。违反"各镜之和 = 总时长"时平台要么报错、
        #    要么退化成单镜头 —— 而一次 4K/10s 是 30 Units，**花钱之前**拦住。
        _shots = parse_multi_shot(text)
        if _shots:
            _ms_errs = validate_multi_shot(_shots, req.duration)
            if _ms_errs:
                return _MS_HINT + "；".join(_ms_errs)

        # ③ 首尾帧：官方**不支持"只给尾帧"**（"last-frame-only video generation
        #    is not supported"）[1]。旧代码这里会静默 pop 掉 image_tail，
        #    用户以为传了尾帧，其实什么都没传。
        if req.last_frame and not req.first_frame:
            return ("可灵 3.0 **不支持只给尾帧**（官方原文：only \"first frame only\" "
                    "and \"first frame + last frame\" are supported；"
                    "\"last frame only\" is not supported）—— "
                    "请补一张首帧图（场景图/上一镜尾帧都行），或去掉尾帧")

        # ④ 分辨率：按型号收窄（Turbo 没有 4K [4][7]）
        res = self.normalize_resolution(req.resolution)
        if res not in prof["resolutions"]:
            return (f"{prof['id']} 只支持 {'/'.join(prof['resolutions'])}，"
                    f"不支持 {req.resolution}"
                    + ("（Turbo 没有 4K 档，官方请求体只列了 720p/1080p）"
                       if prof is _PROFILE_TURBO else ""))

        # ⑤ 基类校验：时长上下限（3~15）、model_catalog 能力矩阵、
        #    首帧/尾帧/参考图能力开关。
        err = super().validate_request(req)
        if err:
            return err
        return None

    # ──────────── 构造请求体（纯函数，不碰网络）────────────

    def build_payload(self, req: VideoGenRequest) -> KlingPayload:
        """把 `VideoGenRequest` 组装成可灵 3.0 的请求体。

        ★ 结构完全照官方：**没有** `parameters` 这一层，**没有** `model_name`、
          **没有** `mode`（旧 v1 的三个字段在 3.0 上都不存在）[1][2]。
        """
        prof = self._profile()
        extra = self._extra(req)
        warnings: List[str] = []
        ratio = normalize_ratio(req.aspect_ratio, default="16:9")
        text, _errs = self._compose_text(req)

        mode = "image2video" if (req.first_frame or req.last_frame) else "text2video"

        settings: Dict[str, Any] = {
            "resolution": self.normalize_resolution(req.resolution) or "720p",
            "duration": int(req.duration),
        }
        if prof["multi_shot"]:
            # 官方默认就是 true [1][2]；这里显式发出去，省得"默认值变了"我们不知道
            settings["multi_shot"] = bool(extra.get("multi_shot", True))
        if prof["audio"]:
            audio = str(extra.get("audio") or self.config.get("audio") or "off").lower()
            if audio not in prof["audio"]:
                warnings.append(
                    f"`audio={audio}` 不是可灵认识的值（{'/'.join(prof['audio'])}），"
                    f"已按 `off`（无声）发出")
                audio = "off"
            settings["audio"] = audio
            if audio == "native":
                warnings.append(
                    "已开启 `audio=native`（原生音频）—— 计费更高：720P 0.9 / 1080P 1.2 "
                    "Units 每秒（无声是 0.6 / 0.8）[6]，而且成片自带人声，"
                    "「后期」里的配音/字幕要相应处理")

        # ★ Element 的组装与告警**两种模式都要跑**：文生视频的请求体是顶层
        #   `prompt` 字符串、**没有 `contents[]` 数组**，Element 无处可放 [2]
        #   —— 但这件事必须**说出来**，不能让"我关联了角色"变成静默丢弃。
        elements = self._element_entries(req, warnings)

        if mode == "text2video":
            # 只有文生视频有 aspect_ratio（图生视频的画幅由首帧决定）[2]
            settings["aspect_ratio"] = ratio
            body: Dict[str, Any] = {"prompt": text, "settings": settings}
            if elements:
                warnings.append(
                    f"可灵**文生视频**的请求体没有 `contents[]` 数组（官方 t2v 结构是"
                    f"顶层 `prompt` 字符串）[2]，Element **没有地方放** —— 本次 "
                    f"{len(elements)} 个 element_id **没有发出去**。"
                    f"要用 Element 锁住人物形象，请给这个分镜一张首帧图"
                    f"（那样就走图生视频端点，contents[] 里才有 element 位置）")
        else:
            contents: List[Dict[str, Any]] = [{"type": "prompt", "text": text}]
            first = self._as_local_ref(req.first_frame)
            last = self._as_local_ref(req.last_frame)
            if first:
                contents.append({"type": "first_frame", "url": first})
            if last:
                contents.append({"type": "last_frame", "url": last})
            contents.extend(elements)
            body = {"contents": contents, "settings": settings}

        # 负面提示词：3.0 没有这个字段 —— 不静默丢，明确说清楚（见文件头"未核实"）
        if str(req.negative_prompt or "").strip():
            warnings.append(
                "可灵 3.0 的请求体里**没有** `negative_prompt` 字段（旧 v1 才有）[1][2]，"
                "这段负面提示词**没有发出去**。官方只说 prompt 可以同时包含正向与负向"
                "描述，但没给负向部分的语法/模板（未核实），所以适配器不擅自拼接 —— "
                "需要负面约束请自己写进 prompt 文本")

        # prompt 里写了 "shot n, m," 但整段不是官方模板 → 多镜头很可能不生效
        if (_SHOT_HINT.search(text) and not parse_multi_shot(text)
                and settings.get("multi_shot")):
            warnings.append(
                "prompt 里出现了 `shot n, m,` 但**整段不符合**官方多镜头模板"
                "（必须是 `shot 1, 3, 描述; shot 2, 4, 描述;` 这样全部由分号分段）[1][2]，"
                "平台很可能只当普通提示词处理、**不会分镜**")

        # 型号不认得 / 是旧版型号 → 说清楚我们按什么契约发的，绝不静默换模型
        if not self._is_known_model():
            m = self._model_name()
            warnings.append(
                f"`config.model_name=「{m}」不在已核实的可灵 3.0 型号里"
                f"（{'/'.join(_MODEL_PROFILES)}），本适配器按 `{DEFAULT_MODEL}` 的契约"
                f"发到 `{prof['image2video']}` / `{prof['text2video']}`"
                + ("。⚠ 这是个**旧版**型号名（旧接口把 model_name 放 body），"
                   "本适配器已不再实现旧接口 —— 请把分镜的模型改成 `kling-3.0`"
                   if _LEGACY_MODEL_RE.match(m) else ""))

        for w in warnings:
            logger.warning("[kling] %s", w)

        return KlingPayload(path=prof[mode], body=body, mode=mode,
                            ratio=ratio, warnings=warnings)

    # ──────────── 结果解析 ────────────

    @staticmethod
    def _tag(raw: Any, notes: Sequence[str]) -> Any:
        """把"必须让用户看见"的说明挂进 raw（`VideoGenResult.raw`）。"""
        if not notes:
            return raw
        try:
            out = dict(raw) if isinstance(raw, dict) else {"raw": raw}
        except Exception:
            out = {"raw": raw}
        out["_videoforge_warnings"] = list(notes)
        return out

    @staticmethod
    def _msg_of(payload: Any) -> str:
        """安全取 `message`（网关/代理出错时可能返回数组甚至字符串，别让取字段本身炸掉）。"""
        return str(payload.get("message") or "") if isinstance(payload, dict) else ""

    @staticmethod
    def _pick_task(payload: Any) -> Optional[Dict[str, Any]]:
        """从 `/tasks` 的响应里取出任务对象。

        官方两种查询的 data 形状不同：按 `task_ids` 查是**数组**，
        按游标查是 `{"result": [...]}` —— 都认。[1]
        """
        if not isinstance(payload, dict):
            return None
        d = payload.get("data")
        if isinstance(d, dict):
            if isinstance(d.get("result"), list):
                d = d["result"]
            elif d.get("id"):
                d = [d]
            else:
                return None
        if isinstance(d, list) and d and isinstance(d[0], dict):
            return d[0]
        return None

    @staticmethod
    def _video_of(task: Dict[str, Any]) -> Tuple[str, Optional[float]]:
        """取 `outputs[]` 里 `type == "video"` 的地址与时长（官方结构）[1]。"""
        for o in (task.get("outputs") or []):
            if isinstance(o, dict) and str(o.get("type") or "").lower() == "video":
                url = str(o.get("url") or "").strip()
                try:
                    dur = float(str(o.get("duration"))) if o.get("duration") else None
                except (TypeError, ValueError):
                    dur = None
                return url, dur
        return "", None

    def _known_model_note(self) -> List[str]:
        if self._is_known_model():
            return []
        m = self._model_name()
        note = (f"`config.model_name=「{m}」不是已核实的可灵 3.0 型号，"
                f"本次按 `{DEFAULT_MODEL}` 的契约发出")
        logger.warning("[kling] %s", note)
        return [note]

    # ──────────── 生成 ────────────

    async def generate(self, req: VideoGenRequest) -> VideoGenResult:
        if not self.api_key:
            return self.make_result(False, error="可灵 API key 未配置")

        err = self.validate_request(req)
        if err:
            return self.make_result(False, error=err)

        # 多镜头模板已在 validate_request 里校验过，这里再确认一次"拼出来的文本"
        # 也合法（例如 shots 里混了认不出的写法）—— 花钱之前拦住。
        _text, shot_errs = self._compose_text(req)
        if shot_errs:
            return self.make_result(False, error=_MS_HINT + "；".join(shot_errs))

        p = self.build_payload(req)
        notes: List[str] = list(p.warnings)

        client = await self._ensure_client()
        try:
            r = await client.post(f"{self.BASE_URL}{p.path}", json=p.body)
            data = r.json()
            code = data.get("code") if isinstance(data, dict) else None
            if r.status_code != 200 or (code not in (0, None)):
                return self.make_result(
                    False,
                    error=describe_error(code, self._msg_of(data), r.status_code),
                    raw=self._tag(data, notes),
                )

            task_id = str(((data.get("data") or {}).get("id")
                           or (data.get("data") or {}).get("task_id") or "")).strip()
            if not task_id:
                return self.make_result(
                    False, error=f"可灵没有返回任务 id：{data}", raw=self._tag(data, notes))

            # 轮询：与 seedance 同一形态（每 5 秒一次、最多 60 次 = 5 分钟）
            for _ in range(60):
                await asyncio.sleep(5)
                r2 = await client.get(f"{self.BASE_URL}{self.TASKS_PATH}",
                                      params={"task_ids": task_id})
                d2 = r2.json()
                if not isinstance(d2, dict) or d2.get("code") not in (0, None):
                    continue            # 查询偶发失败 → 下一轮再试（旧逻辑保持）
                task = self._pick_task(d2)
                if not task:
                    continue
                status = str(task.get("status") or "").lower()
                if status == "succeeded":
                    url, dur = self._video_of(task)
                    if not url:
                        return self.make_result(
                            False,
                            error=("可灵任务成功，但 `outputs[]` 里没有 `type=video` 的地址"
                                   f"（官方结构见文档）：{task}"),
                            task_id=task_id, raw=self._tag(d2, notes))
                    return self.make_result(
                        True, video_url=url,
                        duration=dur if dur else float(req.duration),
                        task_id=task_id, raw=self._tag(d2, notes),
                    )
                if status == "failed":
                    return self.make_result(
                        False,
                        error=("可灵任务失败："
                               + (str(task.get("message") or "").strip()
                                  or "平台没有给出失败原因（常见于内容风控）")),
                        task_id=task_id, raw=self._tag(d2, notes))

            return self.make_result(
                False, error="可灵超时：已轮询 60 次 × 5 秒（5 分钟）仍未完成",
                task_id=task_id, raw=self._tag({"task_id": task_id}, notes))
        except httpx.HTTPError as e:
            return self.make_result(False, error=f"HTTP error: {e}")
        except Exception as e:
            return self.make_result(False, error=f"Error: {e}")

    # ──────────── 续查 ────────────

    async def query_task(self, task_id: str) -> VideoGenResult:
        """按 task_id 续查（3.0 走统一的 `GET /tasks?task_ids=`，与接口类型无关）[1]。

        ★ 旧实现直接返回 "Use generate() directly"，理由是"task id 自带 endpoint
          信息、无法纯靠 id 路由" —— 那是**旧接口**的限制（image2video / text2video
          两条查询路径）。3.0 的查询端点只有 `/tasks` 一个，这个理由已经不成立。
        """
        if not self.api_key:
            return self.make_result(False, error="可灵 API key 未配置",
                                    task_id=task_id)
        client = await self._ensure_client()
        try:
            r = await client.get(f"{self.BASE_URL}{self.TASKS_PATH}",
                                 params={"task_ids": task_id})
            d = r.json()
            code = d.get("code") if isinstance(d, dict) else None
            if r.status_code != 200 or (code not in (0, None)):
                return self.make_result(
                    False, error=describe_error(code, self._msg_of(d), r.status_code),
                    task_id=task_id, raw=d)
            task = self._pick_task(d)
            if not task:
                return self.make_result(
                    False, error=f"可灵查不到任务 {task_id}（返回里没有 data[]）：{d}",
                    task_id=task_id, raw=d)
            status = str(task.get("status") or "").lower()
            if status == "succeeded":
                url, dur = self._video_of(task)
                if not url:
                    return self.make_result(False, error=f"任务成功但没有视频地址：{task}",
                                            task_id=task_id, raw=d)
                return self.make_result(True, video_url=url, duration=dur,
                                        task_id=task_id, raw=d)
            if status == "failed":
                return self.make_result(
                    False,
                    error=("可灵任务失败：" + (str(task.get("message") or "").strip()
                                              or "平台没有给出失败原因")),
                    task_id=task_id, raw=d)
            return self.make_result(False, error=f"Status: {status}", task_id=task_id, raw=d)
        except httpx.HTTPError as e:
            return self.make_result(False, error=f"HTTP error: {e}", task_id=task_id)
        except Exception as e:
            return self.make_result(False, error=f"Error: {e}", task_id=task_id)
