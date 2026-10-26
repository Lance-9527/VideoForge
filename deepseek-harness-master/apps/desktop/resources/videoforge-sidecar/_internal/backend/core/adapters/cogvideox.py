"""
智谱 CogVideoX 适配器

参考：https://open.bigmodel.cn/dev/api
"""

import asyncio
from typing import Dict, Any
import httpx

from . import VideoAdapter, VideoGenRequest, VideoGenResult, register


@register
class CogVideoXAdapter(VideoAdapter):
    name = "cogvideox"
    display_name = "智谱 CogVideoX"
    description = "国产开源生态，文生视频，单段 6s"
    supported_aspect_ratios = ["16:9", "9:16", "1:1"]
    supported_resolutions = ["720p", "1080p"]
    max_duration = 6
    min_duration = 6
    supports_first_last_frame = False
    supports_character_ref = False
    supports_negative_prompt = False
    is_local = False
    requires_api_key = True

    BASE_URL = "https://open.bigmodel.cn/api/paas/v4"

    def _default_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": "VideoForge/1.0",
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def generate(self, req: VideoGenRequest) -> VideoGenResult:
        if not self.api_key:
            return self.make_result(False, error="智谱 API key 未配置")

        err = self.validate_request(req)
        if err:
            return self.make_result(False, error=err)

        model = self.config.get("model_name", "cogvideox")
        payload = {
            "model": model,
            "prompt": req.prompt,
            "with_audio": False,
        }
        if req.first_frame:
            payload["image_url"] = req.first_frame

        client = await self._ensure_client()
        try:
            r = await client.post(f"{self.BASE_URL}/videos/generations", json=payload)
            data = r.json()
            if r.status_code != 200:
                return self.make_result(False, error=f"CogVideoX error: {data}", raw=data)

            task_id = data.get("id") or data.get("task_id")
            for _ in range(60):
                await asyncio.sleep(5)
                r2 = await client.get(f"{self.BASE_URL}/videos/generations/{task_id}")
                d2 = r2.json()
                if d2.get("task_status") == "SUCCESS":
                    return self.make_result(
                        True,
                        video_url=d2.get("video_result", [{}])[0].get("url"),
                        task_id=task_id, raw=d2,
                    )
                elif d2.get("task_status") == "FAILURE":
                    return self.make_result(False, error=d2.get("error"), task_id=task_id, raw=d2)

            return self.make_result(False, error="CogVideoX timeout", task_id=task_id)
        except Exception as e:
            return self.make_result(False, error=f"Error: {e}")

    async def query_task(self, task_id: str) -> VideoGenResult:
        client = await self._ensure_client()
        try:
            r = await client.get(f"{self.BASE_URL}/videos/generations/{task_id}")
            d = r.json()
            if d.get("task_status") == "SUCCESS":
                return self.make_result(True, video_url=d.get("video_result", [{}])[0].get("url"), task_id=task_id, raw=d)
            elif d.get("task_status") == "FAILURE":
                return self.make_result(False, error=d.get("error"), task_id=task_id, raw=d)
            return self.make_result(False, error=f"Status: {d.get('task_status')}", task_id=task_id, raw=d)
        except Exception as e:
            return self.make_result(False, error=str(e))
