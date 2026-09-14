"""
通义万相 Wanx 适配器（阿里云 DashScope）

参考文档：https://help.aliyun.com/zh/model-studio/developer-reference/api-details-9
"""

import asyncio
from typing import Dict, Any
import httpx

from . import VideoAdapter, VideoGenRequest, VideoGenResult, register


@register
class WanxAdapter(VideoAdapter):
    name = "wanx"
    display_name = "通义万相 Wanx（阿里）"
    description = "性价比，文生视频 / 图生视频，最长 5s"
    supported_aspect_ratios = ["16:9", "9:16", "1:1"]
    supported_resolutions = ["720p", "1080p"]
    max_duration = 5
    min_duration = 5
    supports_first_last_frame = False
    supports_character_ref = False
    supports_negative_prompt = True
    is_local = False
    requires_api_key = True

    BASE_URL = "https://dashscope.aliyuncs.com/api/v1"

    def _default_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": "VideoForge/1.0",
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        }

    async def generate(self, req: VideoGenRequest) -> VideoGenResult:
        if not self.api_key:
            return self.make_result(False, error="通义万相 API key 未配置")

        err = self.validate_request(req)
        if err:
            return self.make_result(False, error=err)

        # 强制 5 秒（万相单段上限）
        duration = 5

        model = self.config.get("model_name", "wanx2.1-t2v-turbo")
        if req.first_frame:
            model = "wanx2.1-i2v-turbo"

        if req.first_frame:
            payload = {
                "model": model,
                "input": {
                    "prompt": req.prompt,
                    "img_url": req.first_frame,
                },
                "parameters": {
                    "duration": duration,
                    "resolution": req.resolution,
                    "ratio": req.aspect_ratio,
                },
            }
            if req.negative_prompt:
                payload["parameters"]["negative_prompt"] = req.negative_prompt
        else:
            payload = {
                "model": model,
                "input": {"prompt": req.prompt},
                "parameters": {
                    "duration": duration,
                    "resolution": req.resolution,
                    "ratio": req.aspect_ratio,
                },
            }
            if req.negative_prompt:
                payload["parameters"]["negative_prompt"] = req.negative_prompt

        client = await self._ensure_client()
        try:
            r = await client.post(
                f"{self.BASE_URL}/services/aigc/video-generation/video-synthesis",
                json=payload,
            )
            data = r.json()
            if r.status_code != 200 or "output" not in data:
                return self.make_result(
                    False,
                    error=f"Wanx error: {data.get('message', r.text)}",
                    raw=data,
                )

            task_id = data["output"]["task_id"]

            for _ in range(60):
                await asyncio.sleep(5)
                r2 = await client.get(
                    f"{self.BASE_URL}/tasks/{task_id}",
                )
                d2 = r2.json()
                task = d2.get("output", {})
                status = task.get("taskStatus")
                if status == "SUCCEEDED":
                    return self.make_result(
                        True,
                        video_url=task.get("video_url"),
                        duration=duration,
                        task_id=task_id,
                        raw=d2,
                    )
                elif status == "FAILED":
                    return self.make_result(
                        False,
                        error=f"Wanx failed: {task.get('message', 'unknown')}",
                        raw=d2,
                    )

            return self.make_result(False, error="Wanx timeout", task_id=task_id)
        except httpx.HTTPError as e:
            return self.make_result(False, error=f"HTTP error: {e}")
        except Exception as e:
            return self.make_result(False, error=f"Error: {e}")

    async def query_task(self, task_id: str) -> VideoGenResult:
        client = await self._ensure_client()
        try:
            r = await client.get(f"{self.BASE_URL}/tasks/{task_id}")
            d = r.json()
            task = d.get("output", {})
            if task.get("taskStatus") == "SUCCEEDED":
                return self.make_result(True, video_url=task.get("video_url"), task_id=task_id, raw=d)
            elif task.get("taskStatus") == "FAILED":
                return self.make_result(False, error=task.get("message"), task_id=task_id, raw=d)
            else:
                return self.make_result(False, error=f"Status: {task.get('taskStatus')}", task_id=task_id, raw=d)
        except Exception as e:
            return self.make_result(False, error=str(e))
