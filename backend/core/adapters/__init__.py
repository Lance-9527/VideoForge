"""
VideoForge · 视频生成模型适配器抽象基类

所有模型适配器必须实现此接口。统一调用方式，便于用户切换模型。
"""

from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any, AsyncIterator, Tuple
from dataclasses import dataclass, field
from pathlib import Path
import asyncio
import httpx


@dataclass
class VideoGenRequest:
    """视频生成请求"""
    prompt: str
    duration: int = 10                    # 5/10/30/60/任意
    aspect_ratio: str = "16:9"            # 16:9/9:16/1:1
    resolution: str = "1080p"             # 480p/720p/1080p

    # 参考图（图生视频、首尾帧）
    reference_images: List[str] = field(default_factory=list)
    first_frame: Optional[str] = None     # 首帧图 URL 或本地路径
    last_frame: Optional[str] = None      # 尾帧图

    # 角色一致性
    character_reference: List[str] = field(default_factory=list)

    # 负向
    negative_prompt: str = ""

    # 模型特定参数
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VideoGenResult:
    """视频生成结果"""
    success: bool
    video_url: Optional[str] = None       # CDN URL 或本地路径
    video_path: Optional[str] = None      # 本地保存路径
    thumbnail_url: Optional[str] = None
    duration_seconds: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    provider: str = ""
    model: str = ""
    task_id: Optional[str] = None         # 远程任务 ID（用于续查）
    error: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None  # 原始响应


def degrade_request(adapter: "VideoAdapter", req: "VideoGenRequest"
                    ) -> Tuple[Optional[str], List[str]]:
    """按能力表剥离**可选**输入，返回 `(剩余错误或 None, 已去掉的字段名列表)`。

    ★ 为什么抽成独立函数（2026-09-14）：这段逻辑原来写在 `main.py` 的一个
      闭包里，**没法单测** —— 我给它加上"参考图也要校验"之后立刻踩到一个
      真实缺陷：旧写法是"去掉一个就试，不行就还原"，当**两个字段触发同一条
      能力限制**时永远收敛不了（去掉 A，B 还在；再去掉 B，A 已还原），
      最后请求被判"参数不合法"**直接失败**，比静默降级更糟。
      抽出来之后 `tests/test_reference_wiring.py` 可以直接覆盖它。

    规则：按"最不重要 → 最重要"逐个去掉并**保留去掉的状态**，一旦能过就停
    （最小改动）；若全去完仍过不了，说明问题不在这些字段（比如画幅），
    那就**全部还原**、原样报错，绝不留下半残请求。
    """
    verr = adapter.validate_request(req)
    if not verr:
        return None, []
    removed: List[Tuple[str, Any]] = []
    for field in ("last_frame", "negative_prompt", "reference_images",
                  "character_reference", "first_frame"):
        if not getattr(req, field, None):
            continue
        removed.append((field, getattr(req, field)))
        setattr(req, field, None)
        if not adapter.validate_request(req):
            break
    verr = adapter.validate_request(req)
    if verr:
        for f, v in removed:
            setattr(req, f, v)
        return verr, []
    return None, [f for f, _ in removed]


def pick_first_frame(*, scene_ref: str = "", char_refs: Optional[List[str]] = None,
                     chain_ref: str = "", scene_path: str = "",
                     chain_path: str = "", priority: str = "auto") -> Dict[str, Any]:
    """决定**用哪张图当首帧**，返回 `{"first_frame", "kind", "path"}`。

    为什么要有这个函数：模型一般只吃**一张**首帧图，于是这三件事不可能同时保证 ——
    「跟上一镜衔接」「场景跟场景图一致」「人物跟角色图一致」。
    这个取舍以前是**写死在生成路径里**的（串联 > 场景图），
    用户既看不到也改不了 —— 于是"我关联了场景图/角色图，出片却不像"无解。
    现在摊成一个显式参数 `priority`：
      · `auto`（默认）—— 有上一镜尾帧就用它（衔接最好），否则场景图；
      · `scene`       —— 固定用场景图（不接尾帧）；
      · `character`   —— 用第一个角色的形象图。
    抽成纯函数是为了**可单测**（见 `tests/test_reference_wiring.py`）。
    """
    kind, path, ref = "none", "", ""
    chars = [c for c in (char_refs or []) if c]
    p = str(priority or "auto").strip().lower()
    if p == "character" and chars:
        ref, kind, path = chars[0], "character", ""
    elif p == "scene" and scene_ref:
        ref, kind, path = scene_ref, "scene", scene_path
    elif p == "auto" and chain_ref:
        ref, kind, path = chain_ref, "chain", chain_path
    elif scene_ref:
        ref, kind, path = scene_ref, "scene", scene_path
    return {"first_frame": ref or None, "kind": kind, "path": path}


class VideoAdapter(ABC):
    """视频生成适配器抽象基类"""

    # 元数据
    name: str = "base"
    display_name: str = "Base Adapter"
    description: str = ""
    supported_aspect_ratios: List[str] = ["16:9", "9:16", "1:1"]
    supported_resolutions: List[str] = ["720p", "1080p"]
    max_duration: int = 10               # 单次最大时长（秒）
    min_duration: int = 5
    supports_first_last_frame: bool = False
    supports_character_ref: bool = False
    supports_negative_prompt: bool = True
    is_local: bool = False                # 是否本地运行
    requires_api_key: bool = True

    def __init__(self, api_key: str = "", config: Dict[str, Any] = None):
        self.api_key = api_key
        self.config = config or {}
        self._client: Optional[httpx.AsyncClient] = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=15.0, read=300.0, write=60.0, pool=10.0),
                headers=self._default_headers(),
            )
        return self._client

    def _default_headers(self) -> Dict[str, str]:
        return {"User-Agent": "VideoForge/1.0"}

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ──────────── 核心接口（子类必须实现）────────────

    @abstractmethod
    async def generate(self, req: VideoGenRequest) -> VideoGenResult:
        """生成视频。子类必须实现。

        注意：返回的 video_url 可能是远程 URL，调用方负责下载到本地。
        """
        raise NotImplementedError

    @abstractmethod
    async def query_task(self, task_id: str) -> VideoGenResult:
        """查询异步任务状态。子类必须实现（同步型 API 可返回 succeed）。"""
        raise NotImplementedError

    # ──────────── 工具方法（子类可复用）────────────

    async def download_to_local(self, url: str, save_path: str) -> str:
        """下载视频到本地。

        ⚠ 必须用**不带鉴权头**的干净客户端：
        厂商返回的通常是对象存储（阿里云 OSS / 火山 TOS 等）的**签名 URL**，
        签名已经写在 query 里。若把 `Authorization: Bearer <厂商Key>` 一起带过去，
        OSS 会认为鉴权头与自己的签名方案冲突，直接返回 **403 Forbidden**
        （实测 MiniMax 海螺的视频下载就是这样失败的）。
        """
        if url.startswith("/") or not url.startswith(("http://", "https://")):
            # 本地路径直接返回
            return url

        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        # 只保留最基础的请求头，绝不复用适配器 client（它带着厂商鉴权头）
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=15, read=180, write=60, pool=15),
                                     follow_redirects=True) as dl:
            async with dl.stream("GET", url, headers={"User-Agent": "VideoForge/1.0"}) as response:
                response.raise_for_status()
                with open(save_path, "wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                        f.write(chunk)
        return save_path

    # 分辨率常见写法的归一键（分镜里存的是 "1080p"，适配器写的是 "1080P"）
    _RES_ALIASES = {
        "480P": "480P", "512P": "512P", "540P": "540P", "720P": "720P",
        "768P": "768P", "1080P": "1080P", "2K": "1080P", "4K": "1080P",
        "SD": "512P", "HD": "768P", "FHD": "1080P",
    }

    def normalize_resolution(self, res: Any) -> str:
        """把任意写法的分辨率归一到本适配器支持的档位。

        ⚠ 实测踩坑：分镜入库时写的是小写 `1080p`，而适配器声明的是 `1080P`，
        旧的 validate_request 用精确字符串比较 → 永远不匹配 →
        **所有视频模型都会在"参数校验"这一步就失败**（用户看到的是
        "配了 Key 也用不了"）。这里统一大小写并做常见别名映射。
        """
        s = str(res or "").strip().upper()
        s = self._RES_ALIASES.get(s, s)
        supported = [str(x).upper() for x in (self.supported_resolutions or [])]
        if s in supported:
            return self.supported_resolutions[supported.index(s)]
        # 退一步：按数值**就近**选一档（720 → 768 而不是 512，避免白白降质）
        import re as _re
        m = _re.match(r"^(\d{3,4})P?$", s)
        if m and supported:
            want = int(m.group(1))
            best = None
            for i, sup in enumerate(supported):
                mm = _re.match(r"^(\d{3,4})P?$", sup)
                if not mm:
                    continue
                v = int(mm.group(1))
                d = abs(v - want)
                if best is None or d < best[0]:
                    best = (d, self.supported_resolutions[i])
            if best:
                return best[1]
        # 最后兜底：取支持列表里最高的一档
        return self.supported_resolutions[-1] if self.supported_resolutions else "720p"

    # 是否可用于真正的视频生成。
    # ⚠ 有的适配器只是"本地 LLM 占位"（如名字叫 minimax 的那个 Ollama 包装），
    #   generate() 永远返回失败；如果它出现在模型下拉框里，
    #   用户选中后就必然"点了生成没反应"。这类适配器要排除在可选列表外。
    usable_for_video: bool = True

    @property
    def supports_first_frame(self) -> bool:
        """是否支持**图生视频（首帧）**。

        ⚠ 必须与"尾帧"分开判断：很多模型支持首帧但不支持尾帧。
        旧代码用同一个 `supports_first_last_frame` 一刀切，
        海螺（MiniMax）明明支持 `first_frame_image`，
        却因为该标记为 False 被直接拒绝 ——
        报错"does not support first/last frame"，视频模型完全用不上。
        """
        return bool(getattr(self, "supports_first_last_frame", False)
                    or getattr(self, "_supports_first_frame", False))

    @property
    def supports_last_frame(self) -> bool:
        return bool(getattr(self, "supports_first_last_frame", False))

    def validate_request(self, req: VideoGenRequest) -> Optional[str]:
        """校验请求，返回错误信息或 None（分辨率/比例做大小写无关匹配）

        ★★ 为什么这里要**查能力矩阵**（不是只看 max_duration 这个标量）：
          真实约束是"**某个分辨率下**只允许某些时长"，标量表达不了。
          线上实测 `MiniMax-Hailuo-2.3` 的能力是
              {'768P': [6, 10], '1080P': [6]}
          但适配器只写了 `max_duration=10`，于是
              **`validate_request(10s @1080P)` 返回 None（认为合法）** ——
          请求就这样发出去了，被厂商拒绝（或静默截成 6 秒）之后才暴露。
          用户看到的是"分镜的视频根本没生成"，而**在花钱之前本可以拦住**。
          现在先查能力表：不在允许集合里就如实说清"这个分辨率只允许哪些时长"，
          让上层去降分辨率或拆段（`core.shotplan` 已经会做），别等 API 报错。
        """
        ar = str(req.aspect_ratio or "").strip()
        if ar and ar not in self.supported_aspect_ratios:
            return f"Unsupported aspect ratio: {req.aspect_ratio}. Supported: {self.supported_aspect_ratios}"
        res = self.normalize_resolution(req.resolution)
        req.resolution = res          # 归一后写回，适配器实现直接用它
        if req.duration > self.max_duration:
            return f"Duration {req.duration}s exceeds max {self.max_duration}s"
        if req.duration < self.min_duration:
            return f"Duration {req.duration}s below min {self.min_duration}s"
        # 能力矩阵（若该模型有登记）：这个分辨率到底允许哪些时长
        try:
            from core.model_catalog import caps_for
            caps = caps_for(self.name, self.config.get("model_name") or "")
            if caps:
                up = {str(k).upper(): v for k, v in caps.items()}
                allowed = up.get(str(res).upper())
                if allowed:
                    allowed = sorted({int(d) for d in allowed})
                    if int(req.duration) not in allowed:
                        return (f"{self.display_name} 在 {res} 下只支持 "
                                f"{'/'.join(str(d) + 's' for d in allowed)}，"
                                f"不支持 {int(req.duration)}s")
        except Exception:
            pass
        if req.first_frame and not self.supports_first_frame:
            return f"{self.display_name} does not support first frame"
        if req.last_frame and not self.supports_last_frame:
            return f"{self.display_name} does not support last frame"
        # ★★ 2026-09-14 新增：**参考图也要校验**。
        #   用户反馈「AI 生成的角色/场景关联后，出片时人物形象跟角色图不符」，
        #   根因就在这里：`reference_images` / `character_reference` 两个字段
        #   原来**根本没进校验**，于是
        #     ① 上层那套"优雅降级"永远不知道有东西被丢；
        #     ② 10 个适配器里有 9 个**源码里压根没读这两个字段**（静默丢弃），
        #        只有 seedance 真的会用。
        #   结果：用户辛苦生成、并且已经关联到分镜上的角色图**一张都没发出去**，
        #   人物形象只能靠提示词里的文字签名 —— 当然"跟图不符"。
        #   现在如实报出来，让上层能明确告诉用户"这个模型用不了你的角色图"。
        if (req.reference_images or req.character_reference) \
                and not self.supports_character_ref:
            return (f"{self.display_name} 不支持人物参考图"
                    f"（reference_images / character_reference）")
        return None

    def make_result(
        self,
        success: bool,
        video_url: Optional[str] = None,
        video_path: Optional[str] = None,
        thumbnail_url: Optional[str] = None,
        duration: Optional[float] = None,
        task_id: Optional[str] = None,
        error: Optional[str] = None,
        raw: Optional[Dict[str, Any]] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> VideoGenResult:
        return VideoGenResult(
            success=success,
            video_url=video_url,
            video_path=video_path,
            thumbnail_url=thumbnail_url,
            duration_seconds=duration,
            provider=self.name,
            model=self.config.get("model_name", ""),
            task_id=task_id,
            error=error,
            raw=raw,
            width=width,
            height=height,
        )


# ──────────── 适配器注册表 ────────────

ADAPTERS: Dict[str, type] = {}


# ═══════════════════════════════════════════════════════════════
# 参考图归一化
#
# 绝大多数文生视频 API（火山 Ark / 可灵 / 通义万相 …）**不接受本地文件路径**，
# 只接受 http(s) URL 或 base64 data URI。而 VideoForge 的角色/场景参考图
# 全都是本地文件 → 直接把 `C:\...\img.jpeg` 塞进请求体，API 一定报错，
# 用户看到的现象就是"配了 Key 也用不了模型"。
# 这里统一转成 data URI。
# ═══════════════════════════════════════════════════════════════

_IMG_MIME = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp", "bmp": "bmp"}


def to_image_ref(path_or_url: str, max_bytes: int = 6 * 1024 * 1024) -> str:
    """把本地图片路径转成 base64 data URI；已是 URL/data URI 的原样返回。

    过大的图会先等比压到长边 1280 再编码（API 对体积通常有限制）。
    """
    if not path_or_url:
        return ""
    s = str(path_or_url).strip()
    if s.startswith(("http://", "https://", "data:")):
        return s
    try:
        import base64
        import os as _os
        if not _os.path.exists(s):
            return s
        ext = _os.path.splitext(s)[1].lower().lstrip(".")
        mime = _IMG_MIME.get(ext, "jpeg")
        raw = open(s, "rb").read()
        if len(raw) > max_bytes:
            try:
                import io
                from PIL import Image
                im = Image.open(io.BytesIO(raw)).convert("RGB")
                im.thumbnail((1280, 1280))
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=88)
                raw, mime = buf.getvalue(), "jpeg"
            except Exception:
                pass
        return f"data:image/{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    except Exception:
        return s


def register(cls: type) -> type:
    """装饰器：注册适配器"""
    instance = cls()
    ADAPTERS[instance.name] = cls
    return cls


def get_adapter(provider: str, api_key: str = "", config: Dict[str, Any] = None) -> VideoAdapter:
    """根据 provider 名字获取适配器实例"""
    if provider not in ADAPTERS:
        raise ValueError(f"Unknown provider: {provider}. Available: {list(ADAPTERS.keys())}")
    return ADAPTERS[provider](api_key=api_key, config=config)


def list_providers(include_unusable: bool = False) -> List[Dict[str, Any]]:
    """列出可用 provider（前端模型下拉框用）。

    默认**排除不能真正出视频的适配器**（`usable_for_video=False`，比如那个
    只是本地 LLM 占位、generate() 永远失败的 `minimax`），
    否则用户在下拉框里选中它，就必然"点了生成没反应"。
    """
    out = []
    for name, cls in ADAPTERS.items():
        inst = cls()
        if not include_unusable and not getattr(inst, "usable_for_video", True):
            continue
        out.append({
            "name": inst.name,
            "display_name": inst.display_name,
            "description": inst.description,
            "supported_aspect_ratios": inst.supported_aspect_ratios,
            "supported_resolutions": inst.supported_resolutions,
            "max_duration": inst.max_duration,
            "min_duration": inst.min_duration,
            "supports_first_last_frame": inst.supports_first_last_frame,
            "supports_character_ref": inst.supports_character_ref,
            "supports_negative_prompt": inst.supports_negative_prompt,
            "is_local": inst.is_local,
            "requires_api_key": inst.requires_api_key,
            "usable_for_video": getattr(inst, "usable_for_video", True),
        })
    return out


# 自动导入所有子适配器（触发 @register 装饰器）
# - 开发模式：扫描本目录下的 *.py，自动发现新适配器
# - 打包模式（PyInstaller）：adapters 位于 PYZ 内，目录不存在 → 回退到显式清单
#   （显式清单同时保证打包器把各适配器模块一并打入）
import os as _os
import importlib as _importlib

_KNOWN_ADAPTERS = [
    "cogvideox", "hailuo", "jimeng", "kling", "luma",
    "minimax", "pika", "runway", "seedance", "sora", "wanx",
]


def _autoload_adapters() -> None:
    names: List[str] = []
    try:
        _dir = _os.path.dirname(_os.path.abspath(__file__))
        names = [f[:-3] for f in _os.listdir(_dir)
                 if f.endswith(".py") and f != "__init__.py"]
    except Exception:
        names = []
    if not names:                       # 打包环境 / 目录不可读 → 使用显式清单
        names = list(_KNOWN_ADAPTERS)
    for _module_name in sorted(set(names) | set(_KNOWN_ADAPTERS)):
        try:
            _importlib.import_module(f"{__name__}.{_module_name}")
        except Exception as e:
            print(f"[adapters] Failed to import {_module_name}: {e}")


_autoload_adapters()
