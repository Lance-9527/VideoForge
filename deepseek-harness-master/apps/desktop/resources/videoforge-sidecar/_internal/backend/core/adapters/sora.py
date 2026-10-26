"""
OpenAI Sora 适配器

参考：https://platform.openai.com/docs/guides/video-generation
"""

import asyncio
import base64
from pathlib import Path
from typing import Dict, Any
import httpx

from . import VideoAdapter, VideoGenRequest, VideoGenResult, register


@register
class SoraAdapter(VideoAdapter):
    name = "sora"
    display_name = "OpenAI Sora"
    description = "OpenAI 高质量视频生成，文生视频 / 图生视频"
    supported_aspect_ratios = ["16:9", "9:16", "1:1"]
    supported_resolutions = ["720p", "1080p"]
    max_duration = 20
    min_duration = 5
    supports_first_last_frame = False
    supports_character_ref = False
    supports_negative_prompt = False
    is_local = False
    requires_api_key = True

    BASE_URL = "https://api.openai.com/v1"

    def _default_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": "VideoForge/1.0",
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def generate(self, req: VideoGenRequest) -> VideoGenResult:
        if not self.api_key:
            return self.make_result(False, error="OpenAI API key 未配置")

        err = self.validate_request(req)
        if err:
            return self.make_result(False, error=err)

        model = self.config.get("model_name", "sora-1.0")
        size = "1280x720" if req.aspect_ratio == "16:9" else \
               "720x1280" if req.aspect_ratio == "9:16" else "1024x1024"

        # Sora 支持秒数
        seconds = "5" if req.duration <= 5 else "10" if req.duration <= 10 else "15" if req.duration <= 15 else "20"

        payload = {
            "model": model,
            "prompt": req.prompt,
            "size": size,
            "seconds": seconds,
        }
        if req.first_frame:
            # 上传参考图（简化：假设已经是 URL）
            payload["input_reference"] = req.first_frame

        client = await self._ensure_client()
        try:
            r = await client.post(
                f"{self.BASE_URL}/videos",
                json=payload,
            )
            data = r.json()
            if r.status_code != 200:
                return self.make_result(False, error=f"Sora error: {data}", raw=data)

            task_id = data.get("id")
            if not task_id:
                return self.make_result(False, error=f"No task id: {data}", raw=data)

            for _ in range(120):  # Sora 可能需要较长时间
                await asyncio.sleep(10)
                r2 = await client.get(f"{self.BASE_URL}/videos/{task_id}")
                d2 = r2.json()
                status = d2.get("status")
                if status == "completed":
                    return self.make_result(
                        True,
                        video_url=d2.get("url") or f"{self.BASE_URL}/videos/{task_id}/content",
                        duration=req.duration,
                        task_id=task_id,
                        raw=d2,
                    )
                elif status == "failed":
                    return self.make_result(False, error=d2.get("error"), task_id=task_id, raw=d2)

            return self.make_result(False, error="Sora timeout", task_id=task_id)
        except Exception as e:
            return self.make_result(False, error=f"Error: {e}")

    async def query_task(self, task_id: str) -> VideoGenResult:
        client = await self._ensure_client()
        try:
            r = await client.get(f"{self.BASE_URL}/videos/{task_id}")
            d = r.json()
            if d.get("status") == "completed":
                return self.make_result(
                    True,
                    video_url=d.get("url") or f"{self.BASE_URL}/videos/{task_id}/content",
                    task_id=task_id, raw=d,
                )
            elif d.get("status") == "failed":
                return self.make_result(False, error=d.get("error"), task_id=task_id, raw=d)
            return self.make_result(False, error=f"Status: {d.get('status')}", task_id=task_id, raw=d)
        except Exception as e:
            return self.make_result(False, error=str(e))
