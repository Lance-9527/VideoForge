"""
Seedance 2.0 via token6688 网关（agentai.token6688.com）

═══════════════════════════════════════════════════════════════════
为什么单独写一个 adapter
═══════════════════════════════════════════════════════════════════
用户要求接入 token6688 网关跑 Seedance 2.0。该网关对外文档明确写着
"按火山方舟官方 SDK / 文档对接的代码可直接使用官方路径与字段"，
但 BASE_URL 完全不同（`https://agentai.token6688.com/api/v3`），
模型 ID 走网关别名（`seedance-2-0-official` 而非 `doubao-seedance-2-0-260128`）。

直接复用 `seedance.py` 会让 BASE_URL 与 API key 同时绑死在文件里 —
既要改 BASE_URL，又要把用户的 token6688 key 改写进去 →
把 key 写进 source tree 的风险（git / 备份泄漏）。
**单独建 provider `seedance_t6`**（t6 = token6688）：
  · BASE_URL 硬编码在 adapter，不混用官方火山方舟
  · 用户的 key 走 `settings["api_keys"]["seedance_t6"]` / 环境变量，
    **绝不**写进本文件
  · 官方火山方舟 adapter `seedance` 保持原状，随时可切回对比

═══════════════════════════════════════════════════════════════════
API 协议（按 token6688 OpenAPI 实测）
═══════════════════════════════════════════════════════════════════
· 创建任务：POST {BASE_URL}/contents/generations/tasks
            body 顶层字段：model / content / ratio / resolution /
            duration / generate_audio / return_last_frame …
            required：[model, content]
· 查询任务：GET  {BASE_URL}/contents/generations/tasks/{id}
· 鉴权：    Authorization: Bearer <token6688 key>
· 模型名：  `seedance-2-0-official`（token6688 别名，对应方舟 doubao-seedance-2-0-260128）
· 时长限制：4~15 秒（方舟 Seedance 2.0 官方区间）
· 画幅：16:9 / 9:16 / 1:1 / 4:3 / 3:4 / 21:9 / adaptive
· 分辨率：480p / 720p / 1080p
· 多参考图：≤9 张（角色图 + 场景图可一起发）
· 首/尾帧：是
· 计费：按秒（token6688 后端各厂商通道不同，最便宜的约 0.42 元/秒）

═══════════════════════════════════════════════════════════════════
为什么 timeout 拉到 30 分钟
═══════════════════════════════════════════════════════════════════
用户原话："视频生成时间可能需要等好久（20分钟的都有）"，
token6688 的官方价格表里有些厂商通道平均响应时间是 380 秒 +
排队，实测 20 分钟（1200s）不奇怪。基类默认 `read=300s` 根本不够，
所以这里 httpx client 的 read timeout 直接设 1800s。
"""

from __future__ import annotations
import asyncio
from typing import Dict, Any, List, Optional
import httpx

from . import (
    VideoAdapter, VideoGenRequest, VideoGenResult, register,
    to_image_ref,
)


@register
class SeedanceT6Adapter(VideoAdapter):
    name = "seedance_t6"
    display_name = "Seedance 2.0 (token6688 网关)"
    description = (
        "Seedance 2.0 走 token6688 网关。按秒计费，最便宜通道约 0.42 元/秒。"
        "支持文生视频 / 图生视频 / 多参考图（≤9 张）/ 首尾帧 / 9:16 竖屏。"
        "响应时间波动较大（排队时可达 20 分钟），timeout 已拉到 30 分钟。"
    )
    supported_aspect_ratios = [
        "16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive",
    ]
    supported_resolutions = ["480p", "720p", "1080p"]
    # 方舟 Seedance 2.0 / 2.0 fast 官方区间：[4, 15]，或 -1 跟随素材
    max_duration = 15
    min_duration = 4
    supports_first_last_frame = True
    supports_character_ref = True
    # token6688 文档：多参考图 ≤9 张
    max_reference_images = 9
    supports_negative_prompt = True
    is_local = False
    requires_api_key = True

    BASE_URL = "https://agentai.token6688.com/api/v3"
    DEFAULT_MODEL = "seedance-2-0-official"

    # ──────────── 客户端：把 timeout 拉到 30 分钟 ────────────

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            # ★ read=1800s 对应用户场景："20 分钟的都有"
            #   pool=15s 留点余量；write=60s 不变（提交 body 不大）
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=15.0, read=1800.0, write=60.0, pool=15.0),
                headers=self._default_headers(),
            )
        return self._client

    def _default_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": "VideoForge/1.0",
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    # ──────────── 工具：把首帧 / 参考图归一为 data URI ────────────

    @staticmethod
    def _ref_image(src: Optional[str]) -> Optional[str]:
        if not src:
            return None
        # 基类 to_image_ref 已经处理：URL / data URI 原样返回；本地路径转 data URI
        #   这里复用，避免漏写
        return to_image_ref(src)

    # ──────────── 工具：把白膜 / 参考视频归一为公网 URL 或 data URI ────────────

    @staticmethod
    def _ref_video(src: Optional[str], max_bytes: int = 12 * 1024 * 1024) -> Optional[str]:
        """白膜 / 参考视频的 URI 归一。

        返回顺序：
          · URL 或 data URI → 原样返回
          · 本地路径 ≤12MB → 读文件转 base64 data URI
          · 本地路径 >12MB 或读不出来 → 返回 None（让上层报错）

        ★ 为什么限 12MB：
          方舟 / token6688 网关都没有明确文档说视频 data URI 上限，但实测
          5s 720p h264 mp4 约 3~5MB。限制 12MB 是为了安全熔断，避免送一个
          几百 MB 的全片进去炸网关。
        """
        if not src:
            return None
        s = str(src).strip()
        if s.startswith(("http://", "https://", "data:")):
            return s
        try:
            import base64 as _b64
            import os as _os
            if not _os.path.exists(s):
                return None
            raw = open(s, "rb").read()
            if len(raw) > max_bytes:
                return None  # 让上层报错，避免静默丢入
            ext = _os.path.splitext(s)[1].lower().lstrip(".")
            mime = {"mp4": "mp4", "mov": "quicktime", "webm": "webm",
                    "m4v": "mp4", "mkv": "matroska"}.get(ext, "mp4")
            return f"data:video/{mime};base64,{_b64.b64encode(raw).decode('ascii')}"
        except Exception:
            return None

    # ──────────── 校验 ────────────

    def validate_request(self, req: VideoGenRequest) -> Optional[str]:
        # 先跑基类校验（包含 caps / aspect / 分辨率 / 时长 / 首帧支持等）
        err = super().validate_request(req)
        if err:
            return err
        # ★ 二次拦截：参考图数量超限
        total_refs = (
            len(req.character_reference or [])
            + len(req.reference_images or [])
            + (1 if req.first_frame else 0)
            + (1 if req.last_frame else 0)
        )
        if total_refs > self.max_reference_images:
            return (
                f"参考图总数 {total_refs} 超过 token6688 网关上限 "
                f"{self.max_reference_images}（角色图 + 场景图 + 首尾帧合计）"
            )
        # ★ 白膜视频超限拦截在 generate() 里额外报：下面是限制说明
        #   token6688 文档：参考视频 ≤ 3 段、合计 ≤ 15s、每段 ≥ 2s。
        #   adapter 只判断 "原 video_url 字段只能传 1 个，多了上层自己挑"
        #   （用户传数组需要上层 split）。这里不拦，给上层最大自由。
        return None

    # ──────────── 创建任务 ────────────

    async def generate(self, req: VideoGenRequest) -> VideoGenResult:
        if not self.api_key:
            return self.make_result(False, error="token6688 API key 未配置（api_keys.seedance_t6）")

        err = self.validate_request(req)
        if err:
            return self.make_result(False, error=err)

        model = self.config.get("model_name") or self.DEFAULT_MODEL

        # 构造 content（与官方方舟 / seedance.py 同语义）
        # 优先级：角色参考图（多模态参考）> 首尾帧（构图衔接）
        #   · 有角色参考 → 角色图 + 场景图全部塞 reference_image
        #   · 没角色参考 → first_frame(+last_frame) 走首尾帧模式
        from core.aspect import normalize_ratio as _nr

        # ──────── 0. 白膜 / 参考视频（运镜参考） ────────
        # ★★ 必须放在所有图片与文字 **之前**：
        #   方舟官方推荐顺序是「视频控制信号在前 → 图片身份错点在后 →
        #   文字描述最后」，原因是模型会按位置优先级去「学习」控制信号。
        #   试验设计：如果第一项是 video_url，模型会优先遵循其运镜；
        #   如果 video_url 后面，反而会走参考图决定构图、运镜随机生成。
        # ★ payload 顶层加 `mode = "reference"`：告诉网关这是「参考生」模式，
        #   透传到方舟后是「全能参考生视频」路径。
        content: List[Dict[str, Any]] = []
        video_ref_present = False
        if req.video_url:
            _vu = self._ref_video(req.video_url)
            if _vu is None and str(req.video_url).strip().startswith(("http://", "https://")):
                # URL 校验失败
                return self.make_result(False, error="video_url 读取失败")
            if _vu is None:
                # 本地路径 >12MB 或读不出来
                return self.make_result(
                    False,
                    error=("video_url 本地文件读不出或 >12MB；"
                           "请先上传到公网可访问 URL，或压缩/裁短后再试"),
                )
            content.append({
                "type": "video_url",
                "video_url": {"url": _vu},
                "role": "reference_video",
            })
            video_ref_present = True

        content.append({"type": "text", "text": req.prompt})

        char_refs = [r for r in (req.character_reference or []) if r]
        scene_refs = [r for r in (req.reference_images or []) if r]

        if char_refs or scene_refs:
            # 多模态参考模式
            #   顺序：角色图在前 → 身份锚点更稳；
            #         场景图在后 → 构图基准（方舟文档建议）
            refs_in_order: List[str] = []
            for r in char_refs[: self.max_reference_images]:
                norm = self._ref_image(r)
                if norm:
                    refs_in_order.append(norm)
            for r in scene_refs[: max(0, self.max_reference_images - len(refs_in_order))]:
                norm = self._ref_image(r)
                if norm:
                    refs_in_order.append(norm)
            # ★ 关键：把 first_frame 也作为 reference_image 发（方舟 2.0 i2v
            #   模式同时支持 first_frame 字段 + reference_image。这里走 reference_image
            #   是因为 token6688 网关和方舟 2.0 都更稳）
            if req.first_frame:
                norm = self._ref_image(req.first_frame)
                if norm and len(refs_in_order) < self.max_reference_images:
                    refs_in_order.append(norm)
            for ref in refs_in_order:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": ref},
                    "role": "reference_image",
                })
        else:
            # 首尾帧模式（无角色参考）
            if req.first_frame:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": self._ref_image(req.first_frame)},
                    "role": "first_frame",
                })
            if req.last_frame:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": self._ref_image(req.last_frame)},
                    "role": "last_frame",
                })

        payload: Dict[str, Any] = {
            "model": model,
            "content": content,
            # 顶层字段（强校验路径，token6688 网关与官方方舟一致）
            "ratio": _nr(req.aspect_ratio),
            "resolution": req.resolution,
            "duration": req.duration,
        }
        # ★ 白膜视频存在时加 `mode=reference`，告诉网关走「参考生」模式。
        #   token6688 Web UI 也是这么传的。不加也不会爆，但可能走到
        #   「图生视频」分支被拆错。
        if video_ref_present:
            payload["mode"] = "reference"
        if req.negative_prompt:
            payload["negative_prompt"] = req.negative_prompt

        client = await self._ensure_client()
        try:
            r = await client.post(
                f"{self.BASE_URL}/contents/generations/tasks",
                json=payload,
            )
        except httpx.HTTPError as e:
            return self.make_result(False, error=f"HTTP error: {e}")
        except Exception as e:
            return self.make_result(False, error=f"submit error: {e}")

        # 读 body 即便非 2xx
        try:
            data = r.json()
        except Exception:
            return self.make_result(False, error=f"non-JSON response ({r.status_code}): {r.text[:300]}")

        if r.status_code not in (200, 201, 202):
            return self.make_result(
                False,
                error=f"token6688 error ({r.status_code}): {data.get('error') or r.text[:300]}",
                raw=data,
            )

        task_id = data.get("id") or data.get("task_id")
        if not task_id:
            return self.make_result(False, error=f"no task_id in response: {data}", raw=data)

        # ──────────── 轮询 ────────────
        # ★ 关键：max_attempts=1，避免自动重试烧钱。
        #   调用方如需重试请显式发起新的 generate()，dedup 由上层（MCP 层 +
        #   tool 层）负责，adapter 不主动重复。
        return await self._poll_once(task_id, req)

    # ──────────── 轮询：单次完成才返回，避免重复生成 ────────────

    async def _poll_once(self, task_id: str, req: VideoGenRequest) -> VideoGenResult:
        """轮询直到 succeeded / failed / timeout。

        ★ 为什么叫 `_poll_once`：
          这个方法的"一次完整轮询"是从调用起到任务终结为止的一次性等待，
          而不是"轮询一次 HTTP 调用"。为避免和 `_poll_once_http` 混淆，
          这里保留 `_poll_once` 命名，但语义是一次完整等待。

        设计要点：
          · 初始间隔 6s，每 3 次翻倍（避免一开始就压满网关）
          · 单次最大等待 25 分钟（远低于 read timeout，留缓冲）
          · 任何时刻如果 succeeded，立刻返回 —— 不再做任何重试
        """
        client = await self._ensure_client()
        delay = 6.0
        elapsed = 0.0
        max_wait = 25 * 60  # 1500s，留 5 分钟给后续下载 / 落盘
        while elapsed < max_wait:
            try:
                resp = await client.get(
                    f"{self.BASE_URL}/contents/generations/tasks/{task_id}",
                )
                d = resp.json()
            except httpx.HTTPError as e:
                # 一次网络抖动不算失败，再等一次
                await asyncio.sleep(min(delay, 30.0))
                elapsed += delay
                delay = min(delay * 1.5, 60.0)
                continue
            except Exception:
                await asyncio.sleep(min(delay, 30.0))
                elapsed += delay
                delay = min(delay * 1.5, 60.0)
                continue

            status = (d.get("status") or "").lower()
            if status in ("succeeded", "success", "completed", "done"):
                content_obj = d.get("content") or {}
                # 优先用 content.video_url，回退到顶层 video_url
                video_url = (
                    content_obj.get("video_url")
                    or d.get("video_url")
                )
                if not video_url:
                    return self.make_result(
                        False,
                        error=f"succeeded but no video_url in response: {d}",
                        task_id=task_id,
                        raw=d,
                    )
                return self.make_result(
                    True,
                    video_url=video_url,
                    duration=req.duration,
                    task_id=task_id,
                    raw=d,
                )
            if status in ("failed", "error", "cancelled"):
                err_msg = (
                    d.get("error")
                    or content_obj.get("error")
                    or f"task ended with status={status}"
                )
                return self.make_result(False, error=err_msg, task_id=task_id, raw=d)

            # 还在跑：queued / running / pending / processing
            await asyncio.sleep(delay)
            elapsed += delay
            delay = min(delay * 1.5, 30.0)  # 6 → 9 → 13.5 → 20 → 30 封顶

        return self.make_result(
            False,
            error=f"poll timeout after {int(elapsed)}s (task_id={task_id}); "
                  f"网关说视频可能要 20 分钟，建议调大 max_wait 或换通道",
            task_id=task_id,
        )

    # ──────────── 外部查询（用户从 UI 续查时用）────────────

    async def query_task(self, task_id: str) -> VideoGenResult:
        client = await self._ensure_client()
        try:
            r = await client.get(f"{self.BASE_URL}/contents/generations/tasks/{task_id}")
            d = r.json()
        except httpx.HTTPError as e:
            return self.make_result(False, error=f"HTTP error: {e}")
        except Exception as e:
            return self.make_result(False, error=f"query error: {e}")

        status = (d.get("status") or "").lower()
        if status in ("succeeded", "success", "completed", "done"):
            content_obj = d.get("content") or {}
            return self.make_result(
                True,
                video_url=content_obj.get("video_url") or d.get("video_url"),
                task_id=task_id,
                raw=d,
            )
        if status in ("failed", "error", "cancelled"):
            return self.make_result(
                False,
                error=d.get("error") or f"status={status}",
                task_id=task_id,
                raw=d,
            )
        return self.make_result(False, error=f"still running: {status}", task_id=task_id, raw=d)