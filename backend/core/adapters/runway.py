"""
Runway Gen-3 Alpha 适配器

参考：https://docs.dev.runwayml.com/api/
"""

import asyncio
from typing import Dict, Any
import httpx

from . import VideoAdapter, VideoGenRequest, VideoGenResult, register


@register
class RunwayAdapter(VideoAdapter):
    name = "runway"
    display_name = "Runway Gen-3 Alpha"
    description = "国外首选，文生视频 / 图生视频 / 首尾帧，单段最长 10s"
    supported_aspect_ratios = ["16:9", "9:16", "1:1"]
    supported_resolutions = ["720p", "1080p"]
    max_duration = 10
    min_duration = 5
    supports_first_last_frame = True
    supports_character_ref = False
    supports_negative_prompt = False
    is_local = False
    requires_api_key = True

    BASE_URL = "https://api.dev.runwayml.com/v1"

    def _default_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": "VideoForge/1.0",
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-Runway-Version": "2024-11-06",
        }

    async def generate(self, req: VideoGenRequest) -> VideoGenResult:
        if not self.api_key:
            return self.make_result(False, error="Runway API key 未配置")

        err = self.validate_request(req)
        if err:
            return self.make_result(False, error=err)

        model = self.config.get("model_name", "gen3a_turbo")

        ratio = req.aspect_ratio.replace(":", ":")  # Runway 用 "1280:768" 形式
        # Runway 比例格式不同
        ratio_map = {
            "16:9": "1280:768",
            "9:16": "768:1280",
            "1:1": "1024:1024",
        }
        runway_ratio = ratio_map.get(req.aspect_ratio, "1280:768")

        payload: Dict[str, Any] = {
            "model": model,
            "prompt": req.prompt,
            "duration": req.duration,
            "ratio": runway_ratio,
        }
        if req.first_frame:
            payload["promptImage"] = req.first_frame
        if req.last_frame:
            payload["lastFrameImage"] = req.last_frame

        client = await self._ensure_client()
        try:
            r = await client.post(
                f"{self.BASE_URL}/text_to_video",
                json=payload,
            )
            data = r.json()
            if r.status_code != 200:
                return self.make_result(False, error=f"Runway error: {data}", raw=data)

            task_id = data.get("id")
            if not task_id:
                return self.make_result(False, error=f"No task id: {data}", raw=data)

            for _ in range(60):
                await asyncio.sleep(5)
                r2 = await client.get(f"{self.BASE_URL}/tasks/{task_id}")
                d2 = r2.json()
                status = d2.get("status")
                if status == "SUCCEEDED":
                    return self.make_result(
                        True,
                        video_url=d2.get("output", [None])[0] if d2.get("output") else None,
                        task_id=task_id,
                        raw=d2,
                    )
                elif status in ("FAILED", "CANCELLED"):
                    return self.make_result(False, error=d2.get("failure"), task_id=task_id, raw=d2)

            return self.make_result(False, error="Runway timeout", task_id=task_id)
        except Exception as e:
            return self.make_result(False, error=f"Error: {e}")

    async def query_task(self, task_id: str) -> VideoGenResult:
        client = await self._ensure_client()
        try:
            r = await client.get(f"{self.BASE_URL}/tasks/{task_id}")
            d = r.json()
            if d.get("status") == "SUCCEEDED":
                return self.make_result(True, video_url=(d.get("output") or [None])[0], task_id=task_id, raw=d)
            elif d.get("status") in ("FAILED", "CANCELLED"):
                return self.make_result(False, error=d.get("failure"), task_id=task_id, raw=d)
            return self.make_result(False, error=f"Status: {d.get('status')}", task_id=task_id, raw=d)
        except Exception as e:
            return self.make_result(False, error=str(e))
