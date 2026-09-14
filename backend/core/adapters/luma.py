"""
Luma Dream Machine 适配器

参考：https://docs.lumalabs.ai/docs/api
"""

import asyncio
from typing import Dict, Any
import httpx

from . import VideoAdapter, VideoGenRequest, VideoGenResult, register


@register
class LumaAdapter(VideoAdapter):
    name = "luma"
    display_name = "Luma Dream Machine"
    description = "速度快，质量好，单段 5s"
    supported_aspect_ratios = ["16:9", "9:16", "1:1"]
    supported_resolutions = ["720p", "1080p"]
    max_duration = 9
    min_duration = 5
    supports_first_last_frame = True
    supports_character_ref = False
    supports_negative_prompt = False
    is_local = False
    requires_api_key = True

    BASE_URL = "https://api.lumalabs.ai/v1"

    def _default_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": "VideoForge/1.0",
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def generate(self, req: VideoGenRequest) -> VideoGenResult:
        if not self.api_key:
            return self.make_result(False, error="Luma API key 未配置")

        err = self.validate_request(req)
        if err:
            return self.make_result(False, error=err)

        model = self.config.get("model_name", "ray-2")
        aspect_map = {"16:9": "16:9", "9:16": "9:16", "1:1": "1:1"}

        payload: Dict[str, Any] = {
            "model": model,
            "prompt": req.prompt,
            "aspect_ratio": aspect_map.get(req.aspect_ratio, "16:9"),
            "loop": False,
        }
        if req.first_frame:
            payload["keyframes"] = {"frame0": {"type": "image", "url": req.first_frame}}
            if req.last_frame:
                payload["keyframes"]["frame1"] = {"type": "image", "url": req.last_frame}

        client = await self._ensure_client()
        try:
            r = await client.post(f"{self.BASE_URL}/generations", json=payload)
            data = r.json()
            if r.status_code != 200:
                return self.make_result(False, error=f"Luma error: {data}", raw=data)

            task_id = data.get("id")
            for _ in range(60):
                await asyncio.sleep(5)
                r2 = await client.get(f"{self.BASE_URL}/generations/{task_id}")
                d2 = r2.json()
                state = d2.get("state")
                if state == "completed":
                    return self.make_result(
                        True,
                        video_url=d2.get("assets", {}).get("video"),
                        task_id=task_id, raw=d2,
                    )
                elif state == "failed":
                    return self.make_result(False, error=d2.get("failure_reason"), task_id=task_id, raw=d2)

            return self.make_result(False, error="Luma timeout", task_id=task_id)
        except Exception as e:
            return self.make_result(False, error=f"Error: {e}")

    async def query_task(self, task_id: str) -> VideoGenResult:
        client = await self._ensure_client()
        try:
            r = await client.get(f"{self.BASE_URL}/generations/{task_id}")
            d = r.json()
            if d.get("state") == "completed":
                return self.make_result(True, video_url=d.get("assets", {}).get("video"), task_id=task_id, raw=d)
            elif d.get("state") == "failed":
                return self.make_result(False, error=d.get("failure_reason"), task_id=task_id, raw=d)
            return self.make_result(False, error=f"State: {d.get('state')}", task_id=task_id, raw=d)
        except Exception as e:
            return self.make_result(False, error=str(e))
