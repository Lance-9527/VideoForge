"""
通义万相 Wanx 适配器（阿里云百炼 / DashScope）

★★ 2026-09-XX 按**官方文档**重写（上一版把三条接口线的字段名混在一起了）。

═══════════════════════════════════════════════════════════════════
万相有 **三条并存的接口线**，字段名**完全不同** —— 这是本文件存在的理由
═══════════════════════════════════════════════════════════════════
旧版只有一条路：不管什么模型，都发
    {"input": {"prompt":…, "img_url":…}, "parameters": {"ratio":…, "duration":…}}
于是：
  · 拿 `wan2.2-kf2v-flash`（首尾帧）去发 → 缺 `first_frame_url` / `last_frame_url`
    → 平台报参数错误，用户看到"配了 Key 也用不了"；
  · 拿 `wan2.7-r2v`（参考生视频）去发 → 根本没有 `input.media`，
    角色/场景参考图一张都传不出去 → 出片人物当然不像；
  · 给 kf2v 传了 `parameters.ratio` / 非 5 秒的 `duration` → 平台**忽略或报错**。

三条线（每条字段名、时长规则、端点都不一样）：

  线 ROUTE_KF2V   首尾帧 2.2   model `wan2.2-kf2v-flash`
    端点 `/services/aigc/image2video/video-synthesis`（**注意是 image2video**）
    `input.first_frame_url`（必填）、`input.last_frame_url`（必填）
    `parameters.resolution` 480P/720P/1080P，`parameters.seed` 0–2147483647
    **duration 固定 5 秒，不支持修改** —— 传别的值没有意义

  线 ROUTE_I2V    首帧 2.1–2.6  model `wan2.6-i2v-flash` 等
    端点 `/services/aigc/video-generation/video-synthesis`
    `input.img_url`（必填，**不叫 first_frame_url**）、`input.audio_url`
    `parameters.shot_type` = single/multi（**仅 2.6 系列**）
    duration 2–15（2.6-flash）/ 固定 5（2.2、2.1-plus）/ 3–5（2.1-turbo）

  线 ROUTE_R2V    参考生视频 2.7  model `wan2.7-r2v` / `wan2.7-r2v-2026-06-12`
    端点与首帧线**相同**（video-generation/video-synthesis），但请求体是**新版协议**
    `input.media[]` 数组，元素 `{"type", "url"}`，type ∈
        reference_image / reference_video / first_frame
    `reference_voice` 可给某个参考素材绑音色（仅 2.7）
    `parameters.ratio` 16:9/9:16/1:1/4:3/3:4；`resolution` 720P/1080P
    duration：不含视频参考时 2–15，**含视频参考时 2–10**

═══════════════════════════════════════════════════════════════════
prompt 里的参考指代**原样透传，适配器一个字都不改**
═══════════════════════════════════════════════════════════════════
官方（2.7 参考生视频）明确：中文提示词用「**图1、图2**」指代参考图片，用
「**视频1、视频2**」指代参考视频，编号顺序**与 `media` 数组顺序一致**，图和视频
**分别计数**。这正好对上 VideoForge 的 `core/platform_refs.py`
（`wanx` 的 `ref_style = "plain_index"`，`max_elems = 5`）。
所以本适配器**只负责把 media 数组按顺序摆好**，绝不去改写 prompt ——
一旦我们"顺手"把「图1」换成别的写法，绑定关系就断了（模型会自己猜主体）。

═══ 联网核实记录（2026-09，全部用 web_fetch 读过原文）═══
  [1] 首尾帧 2.2
      https://www.alibabacloud.com/help/zh/model-studio/legacy-image-to-video-by-first-and-last-frame-api-reference
  [2] 首帧 2.1–2.6
      https://www.alibabacloud.com/help/zh/model-studio/legacy-image-to-video-api-reference
  [3] 参考生视频 2.7
      https://www.alibabacloud.com/help/zh/model-studio/wan-video-to-video-api-reference

已核实（逐条有出处，见上面三篇）：
  · `X-DashScope-Async: enable` 是**必选请求头**；"HTTP请求只支持异步，必须设置为
    enable"，少了它报 "current user api does not support synchronous calls"。[1][2][3]
  · 三条线的创建任务端点、model 示例值、字段名如上。[1][2][3]
  · 查询任务：`GET {base}/api/v1/tasks/{task_id}`，Authorization 即可。[1][3]
  · task_status 枚举：PENDING / RUNNING / SUCCEEDED / FAILED / CANCELED / UNKNOWN。[1]
  · `task_id` 有效期 **24 小时**；`video_url` **只保留 24 小时**，官方原话
    "建议在获取链接后立即下载并转存至永久存储（如阿里云 OSS）"。[1][3]
  · kf2v duration："当前参数值固定为 5，且不支持修改。模型将始终生成 5 秒时长的视频。"[1]
  · kf2v 输出宽高比 = **首帧图的宽高比**；`resolution` 只改清晰度不改画幅；
    "输出视频的宽高比由输入首帧图像决定，但无法保证精确比例"（长宽须为 16 的倍数）。[1]
  · kf2v `resolution`：wan2.2-kf2v-flash 可选 480P/720P/1080P，默认 720P；
    wan2.1-kf2v-plus 仅 720P。[1]
  · 首帧线 `duration`：wan2.6-i2v-flash [2,15]（默认 5）；wan2.6-i2v [2,15]；
    wan2.6-i2v-us 仅 5/10/15；wan2.5-i2v-preview 仅 5/10；
    wan2.2-i2v-plus / 2.2-i2v-flash / 2.1-i2v-plus **固定 5 不可改**；2.1-i2v-turbo 可选 3/4/5。[2]
  · 首帧线 `resolution`：2.6-i2v-flash 720P/1080P（默认 1080P）；2.5-i2v-preview
    480P/720P/1080P；2.2-i2v-flash 480P/720P；2.2-i2v-plus 480P/1080P；
    2.1-i2v-turbo 480P/720P；2.1-i2v-plus 仅 720P。[2]
  · 首帧线图片：格式 JPEG/JPG/PNG(无透明)/BMP/WEBP，宽高 [240,8000]，
    ≤20MB（2.6/2.5 系列）/ ≤10MB（2.2/2.1 系列）；支持公网 URL 或
    `data:{MIME};base64,{data}` 的 base64。[2]
  · 首帧线 `audio_url`（2.6/2.5）：wav/mp3，时长 3–30s，≤15MB；超 `duration` 会被截断，
    不足则超出部分无声。[2]
  · 首帧线 `audio`（**仅 wan2.6-i2v-flash**）：是否出有声视频，默认 true；
    优先级 `audio > audio_url`，`audio=false` 时即传了 audio_url 也出无声（按无声计费）。[2]
  · 首帧线 `shot_type`（**仅 2.6 系列**）：single（默认）/ multi；
    **仅当 `prompt_extend=true` 时生效**；优先级 shot_type > prompt。[2]
  · 首帧线 prompt 长度：2.6/2.5 ≤1500 字符，2.2/2.1 ≤800 字符（超出自动截断）；
    negative_prompt ≤500 字符。[2]
  · kf2v prompt ≤800 字符，negative_prompt ≤500 字符；首帧图 [240,8000]px、≤10MB。[1]
  · `seed` 取值范围 [0, 2147483647]（三条线通用）。[1][2][3]
  · `prompt_extend` 默认 true；`watermark` 默认 false（三条线通用）。[1][2][3]
  · 2.7 `media`：`type` ∈ reference_image / reference_video / first_frame；
    **首帧图最多 1 张**；**参考图 + 参考视频 ≥1 且合计 ≤5**；
    "参考素材为主体角色时，仅包含单一角色"。[3]
  · 2.7 参考图：JPEG/JPG/PNG/BMP/WEBP，宽高 [240,8000]，**宽高比 1:8–8:1**，≤20MB。[3]
  · 2.7 参考视频：mp4/mov，时长 1–30s，宽高 [240,4096]，宽高比 1:8–8:1，≤100MB；
    "不推荐传入空镜视频"。[3]
  · 2.7 `reference_voice`：wav/mp3，1–10s，≤15MB；只在配 reference_image /
    reference_video 时生效；传了 reference_video 自带音频又给了 reference_voice 时，
    **reference_voice 覆盖原声**。[3]
  · 2.7 `ratio`：16:9(默认)/9:16/1:1/4:3/3:4；**传了首帧图则 ratio 被忽略**，
    按首帧图宽高比出片。[3]
  · 2.7 duration：默认 5；含视频参考 [2,10]，不含 [2,15]。[3]
  · 2.7 支持**一张九宫格故事板图**（`media` 里只放这一张 `reference_image`）+
    多分镜 prompt → 模型自动识别宫格逻辑并"智能补全镜头内容"，一次出多镜头；
    官方建议"单次仅输入一张多宫格图"。[3]
  · 内容审核：输入 prompt/图像与输出视频都过审，命中违规返回
    `IPInfringementSuspect` 或 `DataInspectionFailed`。[1][2]
  · 2.6 已核实的**已知缺陷**：生成圆环/齿轮/表盘这类持续旋转，约 3 秒后可能卡顿
    约 1 秒，官方建议改用 2.7。[2]

**未核实到（不编，逐条列明）**：
  · 2.7 的 `parameters` 里**没有** `negative_prompt` 字段 —— 但它到底会不会因为
    多传这个键而报错、还是静默忽略，文档没写，**未核实**。本适配器在 r2v 线上
    **不发** negative_prompt，只把它写进 raw 供排查（宁可少发一个字段）。
  · 2.7 是否支持 `shot_type`：官方 FAQ 明确"wan2.7 不支持 shot_type 参数"，
    多镜头改为在 prompt 里描述分镜脚本 [3]；但"多传 shot_type 会不会报错"未核实，
    本适配器在 r2v 线上**不发** shot_type。
  · "参考图+参考视频合计 ≤5"里的 **5** 是否含那张 `first_frame`：原文只写了
    "参考图像 + 参考视频 ≤ 5"，首帧另起一条"最多传入 1 张"，未说明是否占用同一配额，
    **未核实**。本适配器按**保守**解释：media 总条目（含 first_frame）≤ 5，
    首帧另限 1 张 —— 这样超限一定被发现，不会发出去被平台拒。
  · `parameters.ratio` 各档对应的**具体输出像素值**：文档正文写"见下方表格"，
    但该表格未出现在我抓到的正文里 → **未核实**，本适配器不据此做任何推算。
  · `video_url` 24 小时外**是否还能下载**：只说"24 小时后自动清除"，未核实
    是否有缓存兜底。本适配器按"随时会失效"处理（`VIDEO_URL_TTL_HOURS`）。
  · 上面所有**数值型参数的一致性**（如 2.7 传 duration=15 且带视频参考时到底
    报错还是静默截成 10）→ 文档没写"超限处理"，**未核实**；本适配器在
    `validate_request` 阶段就按"含视频参考 2–10"拦下，不赌平台行为。
  · **本适配器一行都没有真跑过**（手上没有 DashScope API Key）。
    异步轮询的形态照抄 `seedance.py`（已上线验证过的同款结构），
    请求体字段名逐条来自上面三篇官方文档。别把"照文档写"当成"实测过"。

其它实现说明：
  · `duration` / `resolution` / `max_duration` / `min_duration` / `supports_*`
    都是**按模型动态决定**的（见 `_spec_for`）。基类把它们定义成类属性常量，
    但"三条线时长规则完全不同"这件事标量表达不了 —— 所以这里用 **property**
    覆盖（property 是数据描述符，优先于类属性，`inst.max_duration` 拿到的就是
    按当前 model 算出来的值，`orchestrator` / `main.py` 里那些
    `adapter.max_duration` 读取点无需改动）。
"""

import asyncio
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

from . import VideoAdapter, VideoGenRequest, VideoGenResult, register, to_image_ref

try:                                    # 画幅归一（core.aspect 是纯函数模块，可用）
    from core.aspect import normalize_ratio as _normalize_ratio
except Exception:                       # pragma: no cover - 只在极端环境触发
    def _normalize_ratio(spec: Any, default: str = "16:9") -> str:
        t = str(spec or "").strip()
        if not t:
            return default
        m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*[:x×X/]\s*(\d+(?:\.\d+)?)\s*$", t)
        return f"{m.group(1)}:{m.group(2)}" if m else t


# ═══════════════════════════════════════════════════════════════
# 三条接口线的常量
# ═══════════════════════════════════════════════════════════════

ROUTE_R2V = "wan2.7-r2v"      # 2.7 参考生视频（新版协议：input.media[]）
ROUTE_I2V = "wan2.x-i2v"      # 2.1–2.6 首帧生视频（input.img_url）
ROUTE_KF2V = "wan2.x-kf2v"    # 2.2 首尾帧（input.first_frame_url + last_frame_url）

# 端点（注意 kf2v 走的是 image2video，不是 video-generation）
EP_VIDEO_SYNTHESIS = "/services/aigc/video-generation/video-synthesis"   # i2v + r2v
EP_IMAGE2VIDEO_SYNTHESIS = "/services/aigc/image2video/video-synthesis"  # kf2v
EP_TASK = "/tasks/{task_id}"

# ⚠ 官方：[1][3] "结果链接有效期为 24 小时，建议在获取链接后立即下载并转存"
VIDEO_URL_TTL_HOURS = 24

# 轮询参数：官方"建议采用轮询机制，并设置合理的查询间隔（如 15 秒）"，
# 且"视频生成过程约需数分钟"（正文另有一处写"通常为 1-5 分钟"）。
POLL_INTERVAL_SECONDS = 15
POLL_MAX_ATTEMPTS = 60        # 15s × 60 = 15 分钟，够覆盖排队 + 生成

# ── 模型名 → 线路（**按模型分派**，这是本文件的核心）──────────────
# 先精确匹配，再前缀匹配（覆盖 wan2.7-r2v-2026-06-12 这类带日期后缀的版本名）。
_MODEL_ROUTE: Dict[str, str] = {
    "wan2.7-r2v": ROUTE_R2V,
    "wan2.7-r2v-2026-06-12": ROUTE_R2V,
    "wan2.2-kf2v-flash": ROUTE_KF2V,
    "wan2.1-kf2v-plus": ROUTE_KF2V,
    "wan2.6-i2v-flash": ROUTE_I2V,
    "wan2.6-i2v": ROUTE_I2V,
    "wan2.6-i2v-us": ROUTE_I2V,
    "wan2.5-i2v-preview": ROUTE_I2V,
    "wan2.2-i2v-plus": ROUTE_I2V,
    "wan2.2-i2v-flash": ROUTE_I2V,
    "wan2.1-i2v-turbo": ROUTE_I2V,
    "wan2.1-i2v-plus": ROUTE_I2V,
    # 万相 2.1 **文生视频**（无首帧）。VideoForge 的 DEFAULT_MODEL_IDS / 模型下拉框
    # / 历史项目里都还留着它，必须继续能路由，否则老项目一选万相就报"未知线路"。
    "wanx2.1-t2v-turbo": ROUTE_I2V,
    "wanx2.1-t2v-plus": ROUTE_I2V,
}

# 路由表：每条线的「字段名 / 时长 / 分辨率 / 画幅」一次写清，避免散落在 if 里
_ROUTES: Dict[str, Dict[str, Any]] = {
    ROUTE_KF2V: {
        "endpoint": EP_IMAGE2VIDEO_SYNTHESIS,
        "image_field": "first_frame_url",     # [1] 必填
        "last_field": "last_frame_url",       # [1] 必填
        "video_field": None,
        "prompt_max": 800,                    # [1] 不超过 800 个字符
        "duration_fixed": 5,                  # [1] "固定为 5，且不支持修改"
        "duration_range": (5, 5),
        "resolutions": ["480P", "720P", "1080P"],
        "ratios": ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"],
        "supports_first_frame": True,
        "supports_last_frame": True,
        "supports_character_ref": False,      # 只吃首尾帧两张图，没有参考图入口
        "supports_shot_type": False,
        "requires_reference": False,
    },
    ROUTE_I2V: {
        "endpoint": EP_VIDEO_SYNTHESIS,
        "image_field": "img_url",             # [2] 必填，**不叫 first_frame_url**
        "last_field": None,
        "video_field": None,
        "prompt_max": 800,
        "duration_fixed": None,
        "duration_range": (2, 15),
        "resolutions": ["480P", "720P", "1080P"],
        "ratios": ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"],
        "supports_first_frame": True,
        "supports_last_frame": False,
        "supports_character_ref": False,      # 只有单张 img_url
        "supports_shot_type": False,
        "requires_reference": False,
    },
    ROUTE_R2V: {
        "endpoint": EP_VIDEO_SYNTHESIS,
        "image_field": None,                  # 走 media[]
        "last_field": None,
        "video_field": "media",
        "prompt_max": 5000,                   # [3] 不超过 5000 个字符
        "duration_fixed": None,
        "duration_range": (2, 15),            # [3] 不含视频参考
        "resolutions": ["720P", "1080P"],
        "ratios": ["16:9", "9:16", "1:1", "4:3", "3:4"],
        "supports_first_frame": True,         # media[].type = first_frame
        "supports_last_frame": False,         # 2.7 协议里没有尾帧
        "supports_character_ref": True,       # reference_image / reference_video
        "supports_shot_type": False,          # [3] FAQ："wan2.7 不支持 shot_type"
        "requires_reference": True,
    },
}

# 逐型号收窄（只写核实过的；没登记的型号用所属线路的默认值）
_MODEL_SPEC: Dict[str, Dict[str, Any]] = {
    # ── 首尾帧线 [1] ──────────────────────────────────────────
    "wan2.2-kf2v-flash": {"resolutions": ["480P", "720P", "1080P"]},  # 默认 720P
    "wan2.1-kf2v-plus": {"resolutions": ["720P"]},                    # 仅 720P
    # ── 首帧线 [2] ────────────────────────────────────────────
    "wan2.6-i2v-flash": {"duration_range": (2, 15), "resolutions": ["720P", "1080P"],
                         "supports_shot_type": True, "supports_audio_toggle": True,
                         "prompt_max": 1500, "image_max_mb": 20, "supports_audio_url": True},
    "wan2.6-i2v": {"duration_range": (2, 15), "resolutions": ["720P", "1080P"],
                   "supports_shot_type": True, "prompt_max": 1500, "image_max_mb": 20,
                   "supports_audio_url": True},
    "wan2.6-i2v-us": {"duration_range": (5, 15), "duration_allowed": [5, 10, 15],
                      "resolutions": ["720P", "1080P"], "supports_shot_type": True,
                      "prompt_max": 1500, "image_max_mb": 20, "supports_audio_url": True},
    "wan2.5-i2v-preview": {"duration_range": (5, 10), "duration_allowed": [5, 10],
                           "resolutions": ["480P", "720P", "1080P"], "prompt_max": 1500,
                           "image_max_mb": 20, "supports_audio_url": True},
    "wan2.2-i2v-flash": {"duration_range": (5, 5), "duration_fixed": 5,
                         "resolutions": ["480P", "720P"]},
    "wan2.2-i2v-plus": {"duration_range": (5, 5), "duration_fixed": 5,
                        "resolutions": ["480P", "1080P"]},
    "wan2.1-i2v-turbo": {"duration_range": (3, 5), "duration_allowed": [3, 4, 5],
                         "resolutions": ["480P", "720P"]},
    "wan2.1-i2v-plus": {"duration_range": (5, 5), "duration_fixed": 5,
                        "resolutions": ["720P"]},
    # ── 文生视频（**没有首帧**）─────────────────────────────────
    # ⚠ 这几个型号名来自 `backend/main.py: DEFAULT_MODEL_IDS["wanx"]`、
    #   `core/model_catalog.py` 的模型下拉框、以及 frontend/js/app.js——
    #   也就是说**历史项目和默认配置里就有它们**，适配器必须还能路由，
    #   否则老项目一选万相就整条失败（那是上一版的真实症状）。
    # ⚠ 但它们的官方契约**本次没有核实**：上面三篇文档分别是首尾帧 2.2、
    #   首帧 2.1–2.6、参考生视频 2.7；文生视频是另一篇
    #   `text-to-video-api-reference`，我**没有读**。
    #   → 所以这里**不编能力表**，只承认"这个型号不需要首帧"这一件事
    #     （`text_only_ok`: 无图时按纯文本发，有图时照发 img_url）。
    #     时长/分辨率沿用首帧线默认值 (2,15) + 三档，属于**未核实**；
    #     真跑起来若平台报参数错，错误码会如实回显。
    "wanx2.1-t2v-turbo": {"text_only_ok": True},
    "wanx2.1-t2v-plus": {"text_only_ok": True},
    "wan2.1-t2v-turbo": {"text_only_ok": True},
    "wan2.1-t2v-plus": {"text_only_ok": True},
}

# 型号名里带 `t2v`（text-to-video）→ 官方契约就不要求首帧。
# 用**名字特征**而不是白名单：用户可能填 `wan2.x-t2v-<新日期>`，
# 白名单会把一个新版本号判成"必须给首帧"，又变成"配了 Key 也用不了"。
_T2V_RE = re.compile(r"(^|[-_])t2v([-_]|$)", re.I)


def is_text_only_model(model_name: str) -> bool:
    """这个型号是否**不需要首帧**（文生视频）？纯函数，可单测。"""
    return bool(_T2V_RE.search(str(model_name or "")))


# ═══════════════════════════════════════════════════════════════
# 纯函数（可单测）
# ═══════════════════════════════════════════════════════════════

def route_for_model(model_name: str) -> str:
    """模型名 → 线路。认不出来**不猜**，返回空串（由调用方决定怎么报错）。"""
    name = str(model_name or "").strip().lower()
    if not name:
        return ""
    if name in _MODEL_ROUTE:
        return _MODEL_ROUTE[name]
    for known, route in _MODEL_ROUTE.items():       # 前缀匹配：wan2.7-r2v-2026-06-12
        if name.startswith(known):
            return route
    return ""


def spec_for_model(model_name: str) -> Dict[str, Any]:
    """取某个型号的**完整能力规格**：线路默认值 + 该型号的覆盖项。

    为什么要"逐型号"而不是"逐线路"：同一条首帧线里，
    `wan2.6-i2v-flash` 是 2–15 秒、`wan2.2-i2v-plus` 却**固定 5 秒**，
    `wan2.1-i2v-turbo` 又是 3/4/5 —— 只按线路给一个标量必然放行非法请求。[2]
    """
    route = route_for_model(model_name)
    if not route:
        return {}
    spec: Dict[str, Any] = dict(_ROUTES[route])
    spec["route"] = route
    spec.update(_MODEL_SPEC.get(str(model_name or "").strip().lower(), {}))
    # 文生视频型号：官方契约**不要求**首帧（`img_url` 在文生视频接口里根本不存在）。
    # 名字特征优先于白名单，见 `is_text_only_model` 的注释。
    if is_text_only_model(model_name):
        spec["text_only_ok"] = True
    return spec


def _error_of(body: Any) -> Optional[str]:
    """从 DashScope 响应里取出可读的错误。

    DashScope 的失败有两种落点（官方示例都有）[1]：
      · 创建任务失败 → 顶层 `{"code": "InvalidApiKey", "message": "…"}`；
      · 任务执行失败 → `output` 里 `{"task_status": "FAILED", "code": …, "message": …}`。
    还有一种更阴的：HTTP 200 但顶层带 `code`。所以两处都要看。
    """
    if not isinstance(body, dict):
        return None
    for src in (body, body.get("output") if isinstance(body.get("output"), dict) else {}):
        if not isinstance(src, dict):
            continue
        code = str(src.get("code") or "").strip()
        msg = str(src.get("message") or "").strip()
        if code or msg:
            return f"{code}: {msg}".strip(": ").strip() or None
    return None


_STATUS_ZH = {
    "PENDING": "排队中",
    "RUNNING": "处理中",
    "SUCCEEDED": "已成功",
    "FAILED": "已失败",
    "CANCELED": "已取消",
    "UNKNOWN": "任务不存在或已超过 24 小时有效期",
}


def _status_text(status: str) -> str:
    s = str(status or "").upper()
    return f"{s}（{_STATUS_ZH[s]}）" if s in _STATUS_ZH else (s or "未知")


def duration_limit(spec: Dict[str, Any], *, has_video_ref: bool = False) -> Tuple[int, int]:
    """该型号的合法时长区间 `(min, max)`。**纯函数**。

    ★ 为什么单独抽出来：2.7 的上限**取决于入参** —— 官方原文是
      "当参考素材中包含视频时：取值为 [2, 10]；当参考素材中不包含视频时：
       取值为 [2, 15]"。[3]
      这个"看请求内容才变"的上限必须**在生成前就能算出来**，
      否则 `max_duration` 这个 property、`validate_request` 的拦截文案、
      以及上层（`orchestrator` 切段 / `main.py` 吸附）会各说各话。
    """
    lo, hi = spec.get("duration_range") or (2, 15)
    lo, hi = int(lo), int(hi)
    if spec.get("route") == ROUTE_R2V and has_video_ref:
        hi = min(hi, 10)
    return (lo, hi)


def build_payload(spec: Dict[str, Any], req: VideoGenRequest, *, model: str,
                  duration: int, resolution: str) -> Dict[str, Any]:
    """按线路组装请求体。**纯函数**，测试直接断言返回的 dict。

    三条线的字段名差异全部收敛在这里 —— 这是本次重写的核心。
    """
    route = spec.get("route") or ""
    media_videos: List[Tuple[str, str]] = []      # (url, voice)
    for item in (req.extra or {}).get("reference_videos") or []:
        if isinstance(item, dict):
            url = str(item.get("url") or "")
            voice = str(item.get("reference_voice") or item.get("voice") or "")
        else:
            url, voice = str(item or ""), ""
        if url:
            media_videos.append((url, voice))

    inp: Dict[str, Any] = {"prompt": req.prompt or ""}
    if req.negative_prompt:
        # 三条线的 negative_prompt 都在 `input` 里（不是 parameters）。[1][2][3]
        # r2v 线上文档没这个字段 → 见文件头"未核实"，不发（但仍留在 raw 里）。
        if route != ROUTE_R2V:
            inp["negative_prompt"] = req.negative_prompt

    if route == ROUTE_R2V:
        # ── 2.7 新版协议：媒体素材一律进 input.media[] [3] ────────────
        media: List[Dict[str, Any]] = []
        # 顺序 = prompt 里「图1/图2/视频1」的编号顺序，图和视频**分别计数**。
        # ⚠ 绝不重排、绝不改写 prompt：编号错位等于把角色认成别人。
        # ⚠ 也**不能**写成 `character_reference or reference_images` ——
        #   那样一有角色图就会把**场景图整批丢掉**（seedance 那边踩过同款坑：
        #   用户关联了场景图却查不出为什么"场景不像"）。两张都要发。
        _imgs = [r for r in (req.character_reference or []) if r]
        _imgs += [r for r in (req.reference_images or []) if r]
        for raw in _imgs:
            media.append({"type": "reference_image", "url": to_image_ref(raw)})
        for url, voice in media_videos:
            item: Dict[str, Any] = {"type": "reference_video", "url": url}
            if voice:
                item["reference_voice"] = voice          # 仅 2.7，覆盖视频原声 [3]
            media.append(item)
        if req.first_frame:
            # type=first_frame，最多 1 张 [3]
            media.append({"type": "first_frame", "url": to_image_ref(req.first_frame)})
        if media:
            inp["media"] = media
        voice = str((req.extra or {}).get("reference_voice") or "")
        if voice and media and "reference_voice" not in media[0]:
            media[0]["reference_voice"] = voice
    elif route == ROUTE_KF2V:
        # ── 2.2 首尾帧：字段名是 first_frame_url / last_frame_url [1] ──
        inp["first_frame_url"] = to_image_ref(req.first_frame)
        if req.last_frame:
            inp["last_frame_url"] = to_image_ref(req.last_frame)
    else:
        # ── 2.1–2.6 首帧：字段名是 img_url（**不叫 first_frame_url**）[2] ──
        # ⚠ 文生视频型号（t2v）**不能塞一个空 img_url** —— 官方对 img_url 的约束是
        #   [240,8000]px 的图片，空串会被判参数不合法。没图就干脆不发这个字段。
        _img = req.first_frame or ((req.reference_images or [None])[0])
        if _img:
            inp["img_url"] = to_image_ref(_img)
        audio_url = str((req.extra or {}).get("audio_url") or "")
        if audio_url:                                  # 2.6 / 2.5 [2]
            inp["audio_url"] = audio_url

    params: Dict[str, Any] = {
        "resolution": resolution,
        # kf2v 的 duration 只能是 5；`duration` 传 5 与官方示例一致（不传也等同 5）[1]
        "duration": int(duration),
        "prompt_extend": bool((req.extra or {}).get("prompt_extend", True)),   # 默认 true [1][2][3]
        "watermark": bool((req.extra or {}).get("watermark", False)),          # 默认 false [1][2][3]
    }
    seed = (req.extra or {}).get("seed")
    try:
        if seed is not None and str(seed) != "":
            seed_i = int(seed)
            if not 0 <= seed_i <= 2147483647:          # [1][2][3]
                raise ValueError
            params["seed"] = seed_i
    except (TypeError, ValueError):
        pass                                           # 非法 seed 直接不发，别让平台报错

    if route == ROUTE_R2V:
        # ratio 只在 r2v 线有效；传了首帧图平台会自动忽略它 [3]
        params["ratio"] = _normalize_ratio(req.aspect_ratio)
    elif route == ROUTE_I2V and spec.get("supports_shot_type"):
        # shot_type 仅 2.6 系列，且**仅当 prompt_extend=true 时生效** [2]
        shot = str((req.extra or {}).get("shot_type") or "")
        if shot in ("single", "multi"):
            params["shot_type"] = shot
        if spec.get("supports_audio_toggle"):
            audio = (req.extra or {}).get("audio")
            if isinstance(audio, bool):
                params["audio"] = audio                # 仅 wan2.6-i2v-flash [2]

    return {"model": model, "input": inp, "parameters": params}


# ═══════════════════════════════════════════════════════════════
# 适配器
# ═══════════════════════════════════════════════════════════════

@register
class WanxAdapter(VideoAdapter):
    name = "wanx"
    display_name = "通义万相 Wanx（阿里）"
    description = ("三条接口线按模型分派：首尾帧 2.2（锁死 5s）／首帧 2.1–2.6（2–15s）／"
                   "参考生视频 2.7（media[] 多模态参考，prompt 用「图1/图2」指代）")
    supported_aspect_ratios = ["16:9", "9:16", "1:1", "4:3", "3:4"]
    supported_resolutions = ["480P", "720P", "1080P"]
    is_local = False
    requires_api_key = True

    BASE_URL = "https://dashscope.aliyuncs.com/api/v1"
    # ⚠ 官方建议迁移到业务空间专属域名（华北2 北京）：
    #     https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com
    #   现有 dashscope.aliyuncs.com 仍可用 [1][2][3]。要用专属域名就把 config
    #   {"base_url": "https://xxx.cn-beijing.maas.aliyuncs.com/api/v1"} 传进来。

    # ⚠ 重写前这里是 model_name → 硬编码 "wanx2.1-i2v-turbo"，
    #   于是用户在设置里选 wan2.7-r2v，适配器却拿 2.1 去请求。
    #   现在：**用户显式给的本厂商型号优先，认不出来的才回退**。
    #
    # ★ 回退值取 "wanx2.1-t2v-turbo"，**与仓库自己的登记保持一致**：
    #   `backend/main.py: DEFAULT_MODEL_IDS["wanx"]`、frontend/js/app.js 的
    #   PROVIDER_MODEL、`core/model_catalog.py` 下拉框第一项都是它。
    #   为什么这一点重要：它是**文生视频**型号（不需要首帧），而分镜未必配了
    #   首帧图 —— 拿一个"必须有首帧"的型号当默认值，会让"没配图的镜头"
    #   整条失败；反过来，选 wan2.6-i2v-flash 当默认又会把纯文本请求判死。
    #   （未核实项：t2v 型号的时长/分辨率表本次没查到，见 _MODEL_SPEC 里的说明。）
    DEFAULT_MODEL = "wanx2.1-t2v-turbo"

    # ★★ `max_duration` / `min_duration` 必须**按模型动态决定**，所以用 property：
    #   基类把它们写成类属性常量（`max_duration = 10`），但万相三条线的时长规则
    #   完全不同（5 / 2–15 / 2–10），一个标量无论如何都表达不了。
    #   property 是数据描述符，优先于类属性 → `adapter.max_duration` 在任何读取点
    #   （orchestrator 切段、main.py 吸附、agent_tools 展示）都自动拿到正确值，
    #   不需要改别人正在改的文件。下面的 `_duration_bounds` 用一个明确的
    #   "能不能断定它是 kf2v" 规则兜底：
    #     · 配置里**明确写着** kf2v 型号 → 断言 (5,5)；
    #     · 配置/默认型号能路由 → 用该型号的区间；
    #     · 型号名认不出来（用户自己填了个新版本）→ **不猜 5 秒**，
    #       用最宽的 (2,15)，真不合法时由 `validate_request` 报清楚。

    def _model_name(self) -> str:
        cfg = str(self.config.get("model_name") or "").strip()
        if cfg:
            return cfg
        return self.DEFAULT_MODEL

    def _spec(self) -> Dict[str, Any]:
        return spec_for_model(self._model_name())

    def _duration_bounds(self) -> Tuple[int, int]:
        spec = self._spec()
        if spec:
            lo, hi = spec.get("duration_range") or (2, 15)
            if spec.get("duration_fixed"):
                f = int(spec["duration_fixed"])
                return (f, f)
            return (int(lo), int(hi))
        m = str(self._model_name()).lower()
        if "kf2v" in m:
            # 名字里带 kf2v 但不在表里（新版本）→ 首尾帧线**永远**是 5 秒 [1]
            return (5, 5)
        return (2, 15)

    @property
    def max_duration(self) -> int:               # type: ignore[override]
        return self._duration_bounds()[1]

    @property
    def min_duration(self) -> int:               # type: ignore[override]
        return self._duration_bounds()[0]

    @property
    def supports_first_last_frame(self) -> bool:   # type: ignore[override]
        """kf2v 线才支持尾帧；首帧线/首帧字段是 `img_url`，2.7 是 `media[].first_frame`。"""
        return bool(self._spec().get("supports_last_frame"))

    @property
    def supports_character_ref(self) -> bool:      # type: ignore[override]
        """只有 2.7 参考生视频有参考素材入口（media[]）。"""
        return bool(self._spec().get("supports_character_ref"))

    @property
    def supports_first_frame(self) -> bool:
        spec = self._spec()
        return bool(spec.get("supports_first_frame")) if spec else True

    def _base_url(self) -> str:
        return (str(self.config.get("base_url") or "").strip().rstrip("/")
                or self.BASE_URL)

    def _default_headers(self) -> Dict[str, str]:
        return {"User-Agent": "VideoForge/1.0"}

    def _auth_headers(self) -> Dict[str, str]:
        """每次请求现算鉴权头（不在 client 上固化）。

        ★ 为什么不在 `_ensure_client()` 里挂：`generate()` 之后常常要
          `download_to_local()`，而 `__init__.py` 里明确写了**下载必须用干净客户端**
          （签名 URL 再带 Authorization 会被 OSS 判 403，实测 MiniMax 踩过）。
          另外 `DEFAULT_HEADERS` 是可变对象，挂上去再改会污染全局。
        """
        return {
            "User-Agent": "VideoForge/1.0",
            "Authorization": f"Bearer {self.api_key}" if self.api_key else "",
            "Content-Type": "application/json",
            # 官方：HTTP 请求只支持异步，**必须** X-DashScope-Async: enable，
            # 少了它报 "current user api does not support synchronous calls"。[1][2][3]
            "X-DashScope-Async": "enable",
        }

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=15.0, read=300.0, write=60.0, pool=10.0),
                follow_redirects=True,
            )
        return self._client

    # ──────────── 校验 ────────────

    def validate_request(self, req: VideoGenRequest) -> Optional[str]:
        model = self._model_name()
        spec = self._spec()
        if not spec:
            # **认不出来就不猜**：万相三条线字段名互不兼容，猜错会发出一份
            # 缺字段的请求，平台报的错用户看不懂。这里如实说清怎么修。
            return (f"万相适配器认不出模型「{model}」属于哪条接口线。"
                    f"请在设置里选一个已核实的型号："
                    f"wan2.2-kf2v-flash（首尾帧，固定 5 秒）／"
                    f"wan2.6-i2v-flash（首帧，2–15 秒）／"
                    f"wan2.7-r2v（参考生视频）")

        route = spec["route"]

        # ① 画幅：按线路给可选项（r2v 才有 parameters.ratio，且不含 21:9）[3]
        ar = str(req.aspect_ratio or "").strip()
        ratios = list(spec.get("ratios") or self.supported_aspect_ratios)
        if ar and ar not in ratios:
            return (f"Unsupported aspect ratio: {req.aspect_ratio}. "
                    f"{model} 支持：{ratios}")

        # ② 分辨率：按型号收窄（如 wan2.1-i2v-plus 只有 720P）[1][2]
        res = self.normalize_resolution(req.resolution)
        req.resolution = res
        avail = [str(r).upper() for r in (spec.get("resolutions") or [])]
        if avail and str(res).upper() not in avail:
            return (f"{model} 在 {req.resolution} 下不可用，"
                    f"该型号只支持 {'/'.join(avail)}")

        # ③ 时长：固定 5 的线路**必须拦**，不能"静默改成 5"
        #    2.7 的"参考素材里含视频 → 上限 10"依赖入参，所以先收集参考素材。
        _imgs, _vids = self._collect_refs(req, spec)
        lo, hi = duration_limit(spec, has_video_ref=bool(_vids))
        fixed = spec.get("duration_fixed")
        if fixed:
            if int(req.duration) != int(fixed):
                return (f"{model} 的视频时长**固定为 {int(fixed)} 秒，不支持修改**"
                        f"（官方原文：固定为 {int(fixed)}，且不支持修改）。"
                        f"收到的是 {int(req.duration)}s —— 请改分镜时长"
                        f"或换 wan2.6-i2v-flash / wan2.7-r2v（可选 2–15 秒）")
        else:
            allowed = spec.get("duration_allowed")
            if allowed:
                if int(req.duration) not in [int(x) for x in allowed]:
                    return (f"{model} 只支持 "
                            f"{'/'.join(str(int(x)) + 's' for x in allowed)}，"
                            f"不支持 {int(req.duration)}s")
            elif req.duration > hi:
                return (f"Duration {req.duration}s exceeds max {hi}s"
                        f"（{model}"
                        + ("；本次带参考视频，官方上限就是 10s"
                           if (route == ROUTE_R2V and _vids and hi == 10) else "")
                        + "）")
            elif req.duration < lo:
                return f"Duration {req.duration}s below min {lo}s（{model}）"

        # ④ 输入素材：三条线的"必填项"不同，这是最容易漏的一环
        if route == ROUTE_KF2V:
            if not req.first_frame:
                return (f"{model} 是**首尾帧**模型，input.first_frame_url 为必填 ——"
                        f"请给这个分镜指定首帧图（场景图／上一镜尾帧都可以）")
        elif route == ROUTE_I2V:
            if not (req.first_frame or req.reference_images) and not spec.get("text_only_ok"):
                return (f"{model} 是图生视频（首帧）模型，input.img_url 为必填 ——"
                        f"请给这个分镜指定首帧图"
                        f"（纯文生视频请选名字里带 t2v 的型号，如 wanx2.1-t2v-turbo）")
            if req.last_frame:
                return (f"{model} 只吃**首帧**（input.img_url），没有尾帧字段；"
                        f"尾帧请改用 wan2.2-kf2v-flash（首尾帧，固定 5 秒）")
            # 首帧线/首尾帧线**没有参考素材入口**（只有一张 img_url / 两张首尾帧）。
            # 必须如实报出来：否则用户关联的角色图会**静默消失**，
            # 出片"人物跟角色图不像"却查不出原因（`core/adapters/__init__.py`
            # 里那段注释记的就是这个坑）。上层的 `degrade_request` 会据此
            # 剥掉这两个字段并告诉用户，而不是发一份假装有参考的请求。
            if (req.reference_images or req.character_reference) \
                    and not spec.get("supports_character_ref"):
                return (f"{self.display_name}（{model}）不支持人物参考图"
                        f"（reference_images / character_reference）——"
                        f"这条线只接受首帧图。要做角色一致性请改用 wan2.7-r2v"
                        f"（参考生视频，prompt 里用「图1/图2」指代）")
        elif route == ROUTE_R2V:
            if req.last_frame and not req.first_frame:
                # 2.7 协议里没有尾帧，但用户可能只填了 last_frame —— 别静默丢掉
                return (f"{model} 的 media[] 只有 first_frame / reference_image / "
                        f"reference_video 三种类型，**没有尾帧**；"
                        f"尾帧请改用 wan2.2-kf2v-flash")
            imgs, vids = self._collect_refs(req, spec)
            if not imgs and not vids:
                return (f"{model} 是参考生视频模型，input.media 必填："
                        f"参考图 + 参考视频至少传 1 个"
                        f"（参考素材可以是角色图／场景图／道具图，也可以是一张九宫格故事板图）")
            n_first = 1 if req.first_frame else 0
            # ⚠ "≤5"是否含那张 first_frame 文档没写死 → 按保守解释（见文件头"未核实"）：
            #   太严的代价是用户少放一张图；太松的代价是整条请求被平台拒。
            total = len(imgs) + len(vids) + n_first
            if total > 5:
                return (f"参考图 + 参考视频 + 首帧合计 {total} 个，"
                        f"超过官方上限 5 个（{model}）——请精简到 5 个以内")
        return None

    # ──────────── 参考素材收集 ────────────

    @staticmethod
    def _collect_refs(req: VideoGenRequest, spec: Dict[str, Any]
                      ) -> Tuple[List[str], List[str]]:
        """把请求里的参考素材拆成 (图片列表, 视频列表) —— **顺序即编号顺序**。

        图片顺序 = `character_reference`（角色在前，人物身份锚点）再
        `reference_images`（场景/道具在后）。这与 `platform_refs.py` 里
        `wanx` 的 `ref_style="plain_index"` 一致，prompt 里的「图1/图2」因此对得上。[3]
        """
        imgs = [str(r) for r in (req.character_reference or []) if r]
        imgs += [str(r) for r in (req.reference_images or []) if r]
        vids: List[str] = []
        for item in (req.extra or {}).get("reference_videos") or []:
            url = item.get("url") if isinstance(item, dict) else item
            if url:
                vids.append(str(url))
        return imgs, vids

    def _planned_duration(self, req: VideoGenRequest, spec: Dict[str, Any]) -> int:
        """真正发给平台的时长。

        kf2v 线锁死 5 [1]；其余把请求值夹到该型号区间内
        （`_snap_legal_duration` 在上层已经吸附过，这里只是防御性收口）。
        2.7 带参考视频时上限收到 10 [3] —— 否则会把 15s 静默发出、被平台拒。
        """
        if spec.get("duration_fixed"):
            return int(spec["duration_fixed"])
        _, vids = self._collect_refs(req, spec)
        lo, hi = duration_limit(spec, has_video_ref=bool(vids))
        return int(max(lo, min(int(req.duration or lo), hi)))

    # ──────────── 生成 ────────────

    async def generate(self, req: VideoGenRequest) -> VideoGenResult:
        if not self.api_key:
            return self.make_result(False, error="通义万相 API key 未配置")

        err = self.validate_request(req)
        if err:
            return self.make_result(False, error=err)

        model = self._model_name()
        spec = self._spec()
        duration = self._planned_duration(req, spec)
        payload = build_payload(spec, req, model=model, duration=duration,
                                resolution=req.resolution)

        client = await self._ensure_client()
        try:
            r = await client.post(
                f"{self._base_url()}{spec['endpoint']}",
                json=payload,
                headers=self._auth_headers(),
            )
            try:
                data = r.json()
            except Exception:
                data = {"raw_text": r.text}
            if r.status_code != 200:
                return self.make_result(
                    False,
                    error=f"万相 HTTP {r.status_code}："
                          f"{_error_of(data) or r.text[:400]}",
                    raw=data,
                )
            # HTTP 200 也可能是失败（DashScope 顶层带 code 的情况）
            top_err = _error_of(data)
            task_id = (data.get("output") or {}).get("task_id")
            if not task_id:
                return self.make_result(
                    False,
                    error=f"万相没有返回 task_id：{top_err or data}",
                    raw=data,
                )
            if top_err:
                return self.make_result(False, error=f"万相：{top_err}",
                                        task_id=task_id, raw=data)

            result = await self._poll(
                task_id, duration=duration, raw_first=data, attempts=POLL_MAX_ATTEMPTS)
            if result.success:
                # ★ video_url 只有 24 小时 —— 上层必须尽快 download_to_local()
                result.raw = dict(result.raw or {})
                result.raw.setdefault("_videoforge_notice",
                                      f"video_url 有效期 {VIDEO_URL_TTL_HOURS} 小时，"
                                      f"请立即下载转存（官方原文：任务成功后返回视频链接，"
                                      f"有效期为 24 小时）")
            return result
        except httpx.HTTPError as e:
            return self.make_result(False, error=f"HTTP error: {e}")
        except Exception as e:
            return self.make_result(False, error=f"Error: {e}")

    async def _poll(self, task_id: str, *, duration: int, raw_first: Any = None,
                    attempts: int = POLL_MAX_ATTEMPTS) -> VideoGenResult:
        """轮询任务直到出结果。形态与 `seedance.py` 一致（那边已上线验证过）。"""
        client = await self._ensure_client()
        last: Dict[str, Any] = {}
        for _ in range(max(1, int(attempts))):
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            r = await client.get(
                f"{self._base_url()}{EP_TASK.format(task_id=task_id)}",
                headers=self._auth_headers(),
            )
            try:
                d = r.json()
            except Exception:
                d = {"raw_text": r.text}
            if not isinstance(d, dict):
                continue
            last = d
            out = d.get("output") if isinstance(d.get("output"), dict) else {}
            status = str(out.get("task_status") or "").upper()
            if status == "SUCCEEDED":
                video_url = out.get("video_url")
                return self.make_result(
                    True,
                    video_url=video_url,
                    duration=float(duration),
                    task_id=task_id,
                    raw=d,
                    error=None if video_url else
                    "平台返回 SUCCEEDED 但没有 video_url（请查 raw）",
                )
            if status in ("FAILED", "CANCELED"):
                # code/message 可能在 output 里，也可能在顶层 [1]
                return self.make_result(
                    False,
                    error=f"万相 {_status_text(status)}："
                          f"{_error_of(d) or '平台未给出 message'}",
                    task_id=task_id, raw=d,
                )
            if status == "UNKNOWN":
                # 大概率是 task_id 超过 24 小时有效期 [1][3]
                return self.make_result(
                    False,
                    error=f"万相 {_status_text(status)}："
                          f"请确认 task_id 是否已超过 24 小时有效期",
                    task_id=task_id, raw=d,
                )
        return self.make_result(
            False,
            error=f"万相轮询超时（{POLL_MAX_ATTEMPTS}×{POLL_INTERVAL_SECONDS}s）。"
                  f"任务可能仍在跑，可用 task_id 续查"
                  f"（注意 task_id 只保留 24 小时）",
            task_id=task_id, raw=last or raw_first,
        )

    async def query_task(self, task_id: str) -> VideoGenResult:
        """按 task_id 查一次（不轮询）。task_id 有效期 24 小时。[1][3]"""
        client = await self._ensure_client()
        try:
            r = await client.get(
                f"{self._base_url()}{EP_TASK.format(task_id=task_id)}",
                headers=self._auth_headers(),
            )
            try:
                d = r.json()
            except Exception:
                d = {"raw_text": r.text}
            if r.status_code != 200:
                return self.make_result(
                    False,
                    error=f"万相 HTTP {r.status_code}：{_error_of(d) or r.text[:400]}",
                    task_id=task_id, raw=d,
                )
            out = d.get("output") if isinstance(d.get("output"), dict) else {}
            status = str(out.get("task_status") or "").upper()
            if status == "SUCCEEDED":
                return self.make_result(True, video_url=out.get("video_url"),
                                        task_id=task_id, raw=d)
            if status in ("FAILED", "CANCELED", "UNKNOWN"):
                return self.make_result(
                    False,
                    error=f"万相 {_status_text(status)}："
                          f"{_error_of(d) or '平台未给出 message'}",
                    task_id=task_id, raw=d,
                )
            return self.make_result(False, error=f"Status: {_status_text(status)}",
                                    task_id=task_id, raw=d)
        except httpx.HTTPError as e:
            return self.make_result(False, error=f"HTTP error: {e}", task_id=task_id)
        except Exception as e:
            return self.make_result(False, error=str(e), task_id=task_id)
