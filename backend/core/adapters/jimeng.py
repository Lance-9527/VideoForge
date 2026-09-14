"""
即梦 Jimeng 适配器（字节跳动火山引擎）

参考：https://www.volcengine.com/docs/6444
"""

import asyncio
from typing import Dict, Any
import httpx

from . import VideoAdapter, VideoGenRequest, VideoGenResult, register


@register
class JimengAdapter(VideoAdapter):
    name = "jimeng"
    display_name = "即梦 Jimeng（字节火山）"
    description = "字节系，中文支持好，文生视频 / 图生视频，5s 单段"
    supported_aspect_ratios = ["16:9", "9:16", "1:1"]
    supported_resolutions = ["720p", "1080p"]
    max_duration = 5
    min_duration = 5
    supports_first_last_frame = False
    supports_character_ref = False
    supports_negative_prompt = True
    is_local = False
    requires_api_key = True

    BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"

    def _default_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": "VideoForge/1.0",
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def generate(self, req: VideoGenRequest) -> VideoGenResult:
        if not self.api_key:
            return self.make_result(False, error="即梦 API key 未配置")

        err = self.validate_request(req)
        if err:
            return self.make_result(False, error=err)

        # 强制 5 秒（即梦单段上限）
        duration = 5
        model = self.config.get("model_name", "jimeng-video-3.0")

        content = [{"type": "text", "text": req.prompt}]
        if req.first_frame:
            content.append({
                "type": "image_url",
                "image_url": {"url": req.first_frame},
            })
        # ★ 与 seedance 同样的问题（同属火山方舟接口族）：`16:9` 被换成 `16x9` 后
        #   平台不认（见 core/aspect.normalize_ratio 的注释）。
        from core.aspect import normalize_ratio as _nr

        payload = {
            "model": model,
            "content": content,
            # ★ 与 seedance 同族接口：参数是 **body 顶层字段**（不是 parameters 里）。
            #   原来的 `"ratio": req.aspect_ratio.replace(":", "x")` 会发出 `16x9`
            #   —— 非法取值；而且在 `parameters` 里时整段被忽略（弱校验）。
            "ratio": _nr(req.aspect_ratio),
            "resolution": req.resolution,
            "duration": duration,
        }

        client = await self._ensure_client()
        try:
            r = await client.post(
                f"{self.BASE_URL}/contents/generations/tasks",
                json=payload,
            )
            data = r.json()
            if r.status_code != 200:
                return self.make_result(
                    False,
                    error=f"Jimeng error: {data.get('error', r.text)}",
                    raw=data,
                )

            task_id = data.get("id") or data.get("task_id")
            if not task_id:
                return self.make_result(False, error=f"No task_id: {data}", raw=data)

            for _ in range(60):
                await asyncio.sleep(5)
                r2 = await client.get(
                    f"{self.BASE_URL}/contents/generations/tasks/{task_id}",
                )
                d2 = r2.json()
                status = d2.get("status")
                if status == "succeeded":
                    content_url = d2.get("content", {}).get("video_url")
                    return self.make_result(
                        True, video_url=content_url, duration=duration, task_id=task_id, raw=d2,
                    )
                elif status == "failed":
                    return self.make_result(False, error=d2.get("error"), task_id=task_id, raw=d2)

            return self.make_result(False, error="Jimeng timeout", task_id=task_id)
        except httpx.HTTPError as e:
            return self.make_result(False, error=f"HTTP error: {e}")
        except Exception as e:
            return self.make_result(False, error=f"Error: {e}")

    async def query_task(self, task_id: str) -> VideoGenResult:
        client = await self._ensure_client()
        try:
            r = await client.get(f"{self.BASE_URL}/contents/generations/tasks/{task_id}")
            d = r.json()
            if d.get("status") == "succeeded":
                return self.make_result(True, video_url=d.get("content", {}).get("video_url"), task_id=task_id, raw=d)
            elif d.get("status") == "failed":
                return self.make_result(False, error=d.get("error"), task_id=task_id, raw=d)
            return self.make_result(False, error=f"Status: {d.get('status')}", task_id=task_id, raw=d)
        except Exception as e:
            return self.make_result(False, error=str(e))
