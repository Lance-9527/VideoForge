"""
可灵 Kling 3.0 via 官方 Kling MCP（klingai.com/mcp）

═══════════════════════════════════════════════════════════════════
为什么单独写这个 adapter
═══════════════════════════════════════════════════════════════════
用户已授权的"可灵"走的是**官方 Kling MCP**（OAuth 动态注册 + PKCE + JSON-RPC），
不是直连 api.klingai.com。

实测（2026-10-19）：用户设置里那个 `api_keys["kling"]` 已经失效 ——
直连返回 `HTTP 401 {"code":1000,"message":"Auth failed"}`，
而 MCP 侧 token 仍然有效（18 个工具可用，credits 正常）。

所以：直连的 `kling.py` 保留（用户随时可以更新 key 切回去），
**新增 `kling_mcp` provider** 走 MCP 这条真正可用的路。

═══════════════════════════════════════════════════════════════════
关键能力：角色一致性
═══════════════════════════════════════════════════════════════════
可灵官方明确：**文生视频不能带角色 Element**，只有 image_to_video /
image_to_image 可以。所以本 adapter 的策略是：

  · 有首帧图（场景图/角色图/合成首帧）→ image_to_video
  · 完全没图               → text_to_video
  · 有多张角色参考图         → image_to_video + images[]（角色 Element 路线）

这正好对上 VideoForge 的角色一致性诉求：分镜层已有角色参考图时，
走 image_to_video，角色脸不会每镜漂移。

═══════════════════════════════════════════════════════════════════
协议
═══════════════════════════════════════════════════════════════════
· 工具：text_to_video / image_to_video / query_tasks
· 创建：返回 generation_id
· 轮询：query_tasks(generationId=...) → status succeeded + 视频 URL
· 模型：kling-video-v3_0（可灵 3）
· 授权状态：%VIDEOFORGE_DATA_DIR%/kling_mcp.json（与 dev 目录共享）
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

from . import (
    VideoAdapter, VideoGenRequest, VideoGenResult, register,
)

logger = logging.getLogger("videoforge.adapters.kling_mcp")

DEFAULT_MODEL = "kling-video-v3_0"


@register
class KlingMcpAdapter(VideoAdapter):
    """可灵 3.0 走官方 Kling MCP。"""

    name = "kling_mcp"
    display_name = "可灵 Kling 3.0 (官方 MCP)"
    description = (
        "可灵 3.0，走 klingai.com 官方 MCP（用户已授权）。"
        "支持文生视频 / 图生视频 / 角色 Element 一致性。"
    )

    supported_aspect_ratios = ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"]
    supported_resolutions = ["480p", "720p", "1080p"]
    min_duration = 5
    max_duration = 10
    # ★ 基类用 supports_first_last_frame / supports_character_ref /
    #   supports_negative_prompt 这三个属性判断能力（supports_first_frame
    #   是基类的 property，会回落到 supports_first_last_frame）。
    supports_first_last_frame = True
    supports_character_ref = True
    supports_negative_prompt = False

    def __init__(self, api_key: str = "", config: Dict[str, Any] = None):
        super().__init__(api_key=api_key, config=config or {})
        # orchestrator 传的是 config={"model_name": ...}
        self.model = (self.config.get("model_name") or self.config.get("model") or DEFAULT_MODEL)
        self._tools = None

    # ── 鉴权预检 ──
    async def preflight(self) -> Dict[str, Any]:
        """检查 MCP 授权状态（不生成任何内容，不花钱）。"""
        try:
            from core import kling_mcp as K
            from core import kling_mcp_tools as T
            who = await T.who_am_i()
            return {"ok": True, "who": who}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── 生成 ──
    async def generate(self, req: VideoGenRequest) -> VideoGenResult:
        from core import kling_mcp_tools as T

        model = self.model or DEFAULT_MODEL
        prompt = req.prompt or ""
        duration = int(max(self.min_duration,
                           min(self.max_duration, req.duration or 5)))
        resolution = (req.resolution or "720p").lower()
        if resolution not in self.supported_resolutions:
            resolution = "720p"

        # 参考图：首帧优先，其次角色参考图
        first = req.first_frame or ""
        tail = req.last_frame or ""
        images: List[str] = [u for u in (req.reference_images or []) if u]
        # 角色参考图也带上（可灵的角色 Element 路线）
        for u in (req.character_reference or []):
            if u not in images:
                images.append(u)

        try:
            if first or tail or images:
                gid = await T.image_to_video(
                    prompt, model=model,
                    first_image=first, tail_image=tail, images=images or None,
                    duration=duration, aspect_ratio=req.aspect_ratio or "",
                    resolution=resolution,
                    rationale="VideoForge 按剧本生成分镜视频",
                )
            else:
                gid = await T.text_to_video(
                    prompt, model=model,
                    duration=duration, aspect_ratio=req.aspect_ratio or "16:9",
                    resolution=resolution,
                    rationale="VideoForge 按剧本生成分镜视频",
                )
        except Exception as e:
            logger.exception("可灵 MCP 提交失败")
            return VideoGenResult(
                success=False, provider=self.name, model=model,
                error=f"可灵 MCP 提交失败：{e}",
            )

        logger.info("可灵 MCP 已提交 generation_id=%s", gid)

        # 轮询（可灵排队可能较久）
        try:
            final = await T.wait_task(gid, timeout=1800.0, interval=8.0)
        except Exception as e:
            logger.exception("可灵 MCP 轮询失败")
            return VideoGenResult(
                success=False, provider=self.name, model=model, task_id=gid,
                error=f"可灵 MCP 生成失败：{e}",
            )

        # ★ extract_result 返回的是 **dict**（status/url/cover/error…），不是 tuple
        info = T.extract_result(final) or {}
        # ★ extract_result 的真实结构：{"status","done","failed","urls":[] ,
        #   "urls_no_watermark":[],"cover","messages":[],"raw","error"}
        #   视频地址在 **urls 列表**里（没有单数 url 字段）。
        _urls = list(info.get("urls") or []) or list(info.get("urls_no_watermark") or [])
        url = _urls[0] if _urls else ""
        if not url:
            return VideoGenResult(
                success=False, provider=self.name, model=model, task_id=gid,
                error=info.get("error") or "可灵没有返回视频地址",
                raw={"final": str(final)[:800]},
            )

        return VideoGenResult(
            success=True, video_url=url, provider=self.name, model=model,
            task_id=gid, 
            thumbnail_url=info.get("cover") or None,
            raw={"final": str(final)[:800]},
        )

    async def query_task(self, task_id: str) -> VideoGenResult:
        from core import kling_mcp_tools as T
        try:
            info = await T.query_task(task_id)
        except Exception as e:
            return VideoGenResult(success=False, provider=self.name,
                                  task_id=task_id, error=str(e))
        d = T.extract_result(info) or {}
        _us = list(d.get("urls") or []) or list(d.get("urls_no_watermark") or [])
        url = _us[0] if _us else ""
        return VideoGenResult(
            success=bool(url), video_url=url, provider=self.name,
            model=self.model, task_id=task_id, 
            error=None if url else (d.get("error") or "任务尚未完成"),
            raw={"info": str(info)[:600]},
        )
