"""
Pika 适配器（Pika Labs）

参考：https://pika.art/api-docs
"""

import asyncio
from typing import Dict, Any
import httpx

from . import VideoAdapter, VideoGenRequest, VideoGenResult, register


@register
class PikaAdapter(VideoAdapter):
    name = "pika"
    display_name = "Pika Labs"
    description = "创意风格效果强，1.5/2.0"
    supported_aspect_ratios = ["16:9", "9:16", "1:1", "5:2", "2:5"]
    supported_resolutions = ["720p", "1080p"]
    max_duration = 10
    min_duration = 3
    supports_first_last_frame = False
    supports_character_ref = False
    supports_negative_prompt = True
    is_local = False
    requires_api_key = True

    BASE_URL = "https://api.pika.art/v1"

    def _default_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": "VideoForge/1.0",
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def generate(self, req: VideoGenRequest) -> VideoGenResult:
        if not self.api_key:
            return self.make_result(False, error="Pika API key 未配置")

        err = self.validate_request(req)
        if err:
            return self.make_result(False, error=err)

        model = self.config.get("model_name", "pika-2.0")
        aspect_map = {
            "16:9": "16:9", "9:16": "9:16", "1:1": "1:1",
            "5:2": "5:2", "2:5": "2:5",
        }
        payload = {
            "model": model,
            "prompt": req.prompt,
            "duration": min(req.duration, 10),
            "aspect_ratio": aspect_map.get(req.aspect_ratio, "16:9"),
        }
        if req.first_frame:
            payload["image"] = req.first_frame
        if req.negative_prompt:
            payload["negative_prompt"] = req.negative_prompt

        client = await self._ensure_client()
        try:
            r = await client.post(f"{self.BASE_URL}/generate", json=payload)
            data = r.json()
            if r.status_code != 200:
                return self.make_result(False, error=f"Pika error: {data}", raw=data)

            task_id = data.get("id")
            for _ in range(60):
                await asyncio.sleep(5)
                r2 = await client.get(f"{self.BASE_URL}/tasks/{task_id}")
                d2 = r2.json()
                if d2.get("status") == "success":
                    return self.make_result(
                        True,
                        video_url=d2.get("video_url"),
                        task_id=task_id, raw=d2,
                    )
                elif d2.get("status") == "failed":
                    return self.make_result(False, error=d2.get("error"), task_id=task_id, raw=d2)

            return self.make_result(False, error="Pika timeout", task_id=task_id)
        except Exception as e:
            return self.make_result(False, error=f"Error: {e}")

    async def query_task(self, task_id: str) -> VideoGenResult:
        client = await self._ensure_client()
        try:
            r = await client.get(f"{self.BASE_URL}/tasks/{task_id}")
            d = r.json()
            if d.get("status") == "success":
                return self.make_result(True, video_url=d.get("video_url"), task_id=task_id, raw=d)
            elif d.get("status") == "failed":
                return self.make_result(False, error=d.get("error"), task_id=task_id, raw=d)
            return self.make_result(False, error=f"Status: {d.get('status')}", task_id=task_id, raw=d)
        except Exception as e:
            return self.make_result(False, error=str(e))
