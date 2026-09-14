"""
可灵 Kling 适配器（快手）

参考文档：https://klingai.kuaishou.com/dev/api
"""

import asyncio
import time
import json
from typing import Optional, Dict, Any
import httpx

from . import VideoAdapter, VideoGenRequest, VideoGenResult, register


@register
class KlingAdapter(VideoAdapter):
    """可灵 AI 视频生成"""

    name = "kling"
    display_name = "可灵 Kling（快手）"
    description = "国内主力，文生视频 / 图生视频 / 首尾帧，10s 单段，1.6/2.0 模型可选"
    supported_aspect_ratios = ["16:9", "9:16", "1:1"]
    supported_resolutions = ["720p", "1080p"]
    max_duration = 10
    min_duration = 5
    supports_first_last_frame = True
    supports_character_ref = False  # 通过 prompt + 参考图描述保持
    supports_negative_prompt = False
    is_local = False
    requires_api_key = True

    BASE_URL = "https://api.klingai.com"

    def _default_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": "VideoForge/1.0",
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def generate(self, req: VideoGenRequest) -> VideoGenResult:
        if not self.api_key:
            return self.make_result(False, error="可灵 API key 未配置")

        err = self.validate_request(req)
        if err:
            return self.make_result(False, error=err)

        model_name = self.config.get("model_name", "kling-v1-5")
        mode = "std" if req.duration <= 5 else "pro"  # 简化模式选择

        # 选择 endpoint
        if req.first_frame or req.last_frame:
            endpoint = f"{self.BASE_URL}/v1/videos/image2video"
            payload = {
                "model_name": model_name,
                "image": req.first_frame or "",
                "image_tail": req.last_frame or "",
                "prompt": req.prompt,
                "duration": str(req.duration),
                "aspect_ratio": req.aspect_ratio,
                "mode": mode,
            }
            if req.last_frame and not req.first_frame:
                payload.pop("image_tail")
            payload = {k: v for k, v in payload.items() if v}
        else:
            endpoint = f"{self.BASE_URL}/v1/videos/text2video"
            payload = {
                "model_name": model_name,
                "prompt": req.prompt,
                "duration": str(req.duration),
                "aspect_ratio": req.aspect_ratio,
                "mode": mode,
            }

        client = await self._ensure_client()
        try:
            r = await client.post(endpoint, json=payload)
            data = r.json()
            if r.status_code != 200 or data.get("code") != 0:
                return self.make_result(
                    False,
                    error=f"Kling API error: {data.get('message', r.text)}",
                    raw=data,
                )

            task_id = data["data"]["task_id"]

            # 轮询
            for _ in range(60):  # 5 分钟
                await asyncio.sleep(5)
                r2 = await client.get(
                    f"{self.BASE_URL}/v1/videos/{'image2video' if req.first_frame or req.last_frame else 'text2video'}/{task_id}"
                )
                d2 = r2.json()
                if d2.get("code") != 0:
                    continue
                task = d2.get("data", {})
                status = task.get("task_status")
                if status == "succeed":
                    videos = task.get("task_result", {}).get("videos", [])
                    if videos:
                        video_url = videos[0].get("url")
                        return self.make_result(
                            True,
                            video_url=video_url,
                            duration=req.duration,
                            task_id=task_id,
                            raw=d2,
                        )
                elif status == "failed":
                    return self.make_result(
                        False,
                        error=f"Kling task failed: {task.get('task_status_msg')}",
                        raw=d2,
                    )

            return self.make_result(False, error="Kling timeout", task_id=task_id)
        except httpx.HTTPError as e:
            return self.make_result(False, error=f"HTTP error: {e}")
        except Exception as e:
            return self.make_result(False, error=f"Error: {e}")

    async def query_task(self, task_id: str) -> VideoGenResult:
        # Kling 的 task id 自带 endpoint 信息（image2video/text2video），无法纯靠 id 路由
        # 简化：让 generate 内部处理
        return self.make_result(False, error="Use generate() directly")
