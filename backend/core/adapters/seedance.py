"""
豆包 Seedance 适配器（字节跳动）

APPSO 拍沙丘 3 用的是 Seedance 2.5，本适配器实现对应接口。
"""

import asyncio
from typing import Dict, Any
import httpx

from . import VideoAdapter, VideoGenRequest, VideoGenResult, register


@register
class SeedanceAdapter(VideoAdapter):
    name = "seedance"
    display_name = "豆包 Seedance（字节火山）"
    description = "APPSO 推荐，单段最长 30s，支持首尾帧"
    supported_aspect_ratios = ["16:9", "9:16", "1:1", "21:9", "4:3", "3:4"]
    supported_resolutions = ["480p", "720p", "1080p"]
    # ★ 官方：Seedance 2.0 / 2.0-fast 的 `duration` 范围是 **[4,15]**（或 -1 表示按
    #   参考素材自动）。旧代码写 `max_duration = 30`，会把 16~30 秒这种非法请求
    #   放过去（然后被平台拒）。1.0 系列是 [2,12]，由 `_snap_legal_duration`
    #   结合能力表再收窄。
    max_duration = 15
    min_duration = 4
    supports_first_last_frame = True
    supports_character_ref = True
    # ★ 角色参考图最多发几张（方舟的 content 数组可带多张 reference_image）。
    #   实测 2 个角色都被关联时，旧代码只发第一张 —— 第二个人物只能靠文字猜。
    max_reference_images = 4
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
            return self.make_result(False, error="Seedance API key 未配置")

        err = self.validate_request(req)
        if err:
            return self.make_result(False, error=err)

        model = self.config.get("model_name", "doubao-seedance-2-5-250928")

        # 构造 content
        # ★★ 2026-09-14 按**官方契约**重写（用户："我用了 seedance…关联了图，
        #    但生成的视频没按剧本走，该出现的人物没出现，场景有"）：
        #
        # ① **参数必须在 body 顶层**。官方《创建视频生成任务》文档里
        #    `ratio/resolution/duration/seed/watermark` 全是顶层字段，**没有
        #    `parameters` 这一层**；官方还写明两种校验强度：
        #      · 新方式（body 顶层字段）= **强校验**，写错会报错；
        #      · 旧方式（提示词后缀 `--ratio …`）= **弱校验**，写错**被忽略**。
        #    我们原来把参数塞进 `"parameters": {...}` —— 既不是合法字段、
        #    又落在"被忽略"那条路上：`ratio` 实际没生效，模型按默认 `adaptive`
        #    跟着那张**方形参考图**出了 960x960(1:1)，然后被我们自己的画幅守卫
        #    丢掉，退回本地幻灯片。这就是用户看到的那一幕。
        # ② **`first_frame/last_frame` 与 `reference_image` 是三种互斥场景，
        #    不能混用**。所以这里二选一（不是"都发"）：
        #      · 有角色参考图 → 走「多模态参考」：角色图 + 场景图**全部**作为
        #        `reference_image`（官方支持多张），人物身份与场景一起锁；
        #      · 没有角色图 → 走「首/尾帧」：first_frame(+last_frame) 负责构图衔接。
        from core.aspect import normalize_ratio as _nr
        content = [{"type": "text", "text": req.prompt}]
        _refs = [r for r in (req.character_reference or req.reference_images or []) if r]
        if _refs:
            # 多模态参考模式。★ 顺序：**角色图在前、场景图在后** ——
            #   官方建议用 `[图1]xxx，[图2]xxx` 把图与主体绑定，而"身份锚点放前面"
            #   才与人物的编号对得上（提示词里的 `[图N]` 就是按这个顺序生成的）。
            #   （ratio 现在走 body 顶层显式给定，不再需要"把场景图放前面来定画幅"。）
            _all = _refs + ([req.first_frame] if req.first_frame else [])
            for _r in _all[:max(1, int(self.max_reference_images or 1))]:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": _r},
                    "role": "reference_image",
                })
        else:
            if req.first_frame:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": req.first_frame},
                    "role": "first_frame",
                })
            if req.last_frame:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": req.last_frame},
                    "role": "last_frame",
                })

        payload = {
            "model": model,
            "content": content,
            # ★ 顶层字段（不是 parameters 里）—— 强校验路径
            "ratio": _nr(req.aspect_ratio),
            "resolution": req.resolution,
            "duration": req.duration,
        }
        if req.negative_prompt:
            payload["negative_prompt"] = req.negative_prompt

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
                    error=f"Seedance error: {data.get('error', r.text)}",
                    raw=data,
                )

            task_id = data.get("id") or data.get("task_id")
            if not task_id:
                return self.make_result(False, error=f"No task_id: {data}", raw=data)

            # Seedance 通常 30 秒内完成
            for _ in range(60):
                await asyncio.sleep(5)
                r2 = await client.get(
                    f"{self.BASE_URL}/contents/generations/tasks/{task_id}",
                )
                d2 = r2.json()
                status = d2.get("status")
                if status == "succeeded":
                    video_url = d2.get("content", {}).get("video_url")
                    return self.make_result(
                        True, video_url=video_url, duration=req.duration, task_id=task_id, raw=d2,
                    )
                elif status == "failed":
                    return self.make_result(False, error=d2.get("error"), task_id=task_id, raw=d2)

            return self.make_result(False, error="Seedance timeout", task_id=task_id)
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
