"""
即梦 / Seedance 适配器（字节跳动 · 火山方舟 Ark）

════════════════════════════════════════════════════════════════════════
这个文件原来写的是什么（以及为什么必须重写）
════════════════════════════════════════════════════════════════════════
原版把即梦当成"5 秒定长、不支持首尾帧、不支持角色参考图"的老接口：

    max_duration = 5
    min_duration = 5
    supports_first_last_frame = False
    supports_character_ref = False
    duration = 5            # 硬编码

这四条**全都是错的**，而且错得贵：

  · 硬编码 5 秒 → 用户要 12 秒的镜头，被切成 3 段去拼，白多两个接缝；
    要 15 秒的镜头，实际只出 5 秒，成片长度对不上、字幕全错位。
  · `supports_first_last_frame=False` → 长视频续接（首尾帧）整条路被
    `validate_request` 直接拒掉。也就是说"长视频分段生成"这个功能
    在即梦这条线上**从来就没通过**。
  · `supports_character_ref=False` → 角色参考图被校验拦下，用户辛苦做的
    定妆照一张都发不出去，人物一致性只能靠文字签名。

真实能力（即梦与 Seedance 同属火山方舟 Ark 的
`/api/v3/contents/generations/tasks` 接口族，字段完全一致）：

    时长        4–30 秒（-1 = 按参考素材自动）
    分辨率      480p / 720p / 1080p
    画幅        16:9 / 4:3 / 1:1 / 3:4 / 9:16 / 21:9 / adaptive
    首尾帧      支持，且是三个**互斥**场景之一
    参考图      支持多张（`role: reference_image`）
    草稿模式    body 顶层 `draft: true` → 480P 样片
    尾帧返回    body 顶层 `return_last_frame: true`

「即梦」和「Seedance」在 VideoForge 里是两个 provider 名（前端两处入口），
但**底层是同一个接口族**，所以能力应当一致 —— 差别只在默认 model_name。
把差异写死成两个能力不同的适配器，是纯粹的自找麻烦。
"""

import asyncio
from typing import Any, Dict, List

import httpx

from . import VideoAdapter, VideoGenRequest, VideoGenResult, register


@register
class JimengAdapter(VideoAdapter):
    name = "jimeng"
    display_name = "即梦 Jimeng（字节火山）"
    description = "字节系，中文好，单段 4–30s，支持首尾帧/参考图/草稿样片"
    supported_aspect_ratios = ["16:9", "9:16", "1:1", "21:9", "4:3", "3:4"]
    supported_resolutions = ["480p", "720p", "1080p"]

    # ★ 4–30：不是 5。
    max_duration = 30
    min_duration = 4

    supports_first_last_frame = True
    supports_character_ref = True
    max_reference_images = 4
    supports_negative_prompt = True
    is_local = False
    requires_api_key = True

    # 草稿模式：官方的"先出 480P 样片、确认后再出成片"。
    # 官方数据：抽卡 4 次命中省 57%，20 次命中省 77%。
    supports_draft = True
    # 平台可直接返回尾帧（长视频续接优先用它，省掉本地抽帧）
    supports_last_frame_return = True

    BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"

    DEFAULT_MODEL = "doubao-seedance-2-5-260628"

    def _default_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": "VideoForge/1.0",
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    # ──────────── 能力自述 ────────────

    def describe_limits(self) -> Dict[str, Any]:
        return {
            "duration": [self.min_duration, self.max_duration],
            "draft": self.supports_draft,
            "return_last_frame": self.supports_last_frame_return,
            "max_reference_images": self.max_reference_images,
            "note": "首帧/尾帧与参考图是互斥场景，不能混发（平台会静默只认一个）",
        }

    def _img(self, p: str) -> str:
        """图片字段统一过这一层：本地路径 → data URI。

        火山方舟只接受 http(s) URL 或 base64 data URI，**不接受本地路径**。
        长视频续接拿到的尾帧（`longvideo.grab_last_frame` 的产物）就是本地路径，
        直接发会被平台 400，而报错看起来像"参数写错"，实际是路径没转。
        兜在适配器这一层，任何调用方都不会再踩。
        """
        if not p:
            return ""
        try:
            from . import to_image_ref
            return to_image_ref(p) or p
        except Exception:
            return p

    # ──────────── 请求体构造（抽出来是为了能单测）────────────

    def build_payload(self, req: VideoGenRequest) -> Dict[str, Any]:
        """按官方契约构造请求体。

        ★★ 两条硬规则，都踩过：

        ① **参数必须在 body 顶层**。官方文档里 `ratio / resolution /
           duration / seed / watermark / draft / return_last_frame` 全是顶层
           字段，**没有 `parameters` 这一层**。而且官方明说两种校验强度：
             · 新方式（顶层字段）= **强校验**，写错会报错
             · 旧方式（提示词后缀 `--ratio ...`）= **弱校验**，写错**被忽略**
           把参数塞进 `parameters` 就是"既非法、又被静默忽略"——
           ratio 实际没生效，模型按默认 adaptive 跟着参考图出画幅，
           然后被我们自己的画幅守卫丢掉，用户看到的是"没生成"。

        ② **首尾帧 与 参考图 互斥**。官方把 first_frame / last_frame /
           reference_image 列为三种互斥场景。所以这里**二选一**：
             · 有角色/场景参考图 → 全部作为 reference_image（身份+场景一起锁）
             · 没有参考图 → 走 first_frame(+last_frame) 负责构图衔接
           两个都发，平台会静默只认一个（实测就是这种"不报错但没用"）。
        """
        if not self.api_key:
            raise ValueError("即梦 API key 未配置")

        from core.aspect import normalize_ratio as _nr

        model = self.config.get("model_name") or self.DEFAULT_MODEL
        content: List[Dict[str, Any]] = [{"type": "text", "text": req.prompt}]

        # ★★ 不要写成 `req.character_reference or req.reference_images`！
        #   那是个静默丢数据的 bug（wanx 适配器同时踩过，独立发现）：
        #   只要 character_reference 非空，reference_images **整批被丢掉**。
        #   用户把角色图和场景图都关联上了，场景图却一张没发出去，
        #   现象是"场景完全不像" —— 而且不报错，极难定位。
        #   正确做法：两个都收，**角色图在前、场景图在后**（prompt 里的
        #   `[图N]` 按这个顺序编号，身份锚点必须对得上号）。
        refs: List[str] = []
        seen = set()
        for r in list(req.character_reference or []) + list(req.reference_images or []):
            if r and r not in seen:
                seen.add(r)
                refs.append(r)
        refs = [self._img(r) for r in refs]

        if refs:
            # 多模态参考。顺序：角色图在前、场景图在后 —— prompt 里的
            # `[图N]` 就是按这个顺序编号的，身份锚点必须对得上号。
            all_refs = refs + ([self._img(req.first_frame)] if req.first_frame else [])
            cap = max(1, int(self.max_reference_images or 1))
            for r in all_refs[:cap]:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": r},
                    "role": "reference_image",
                })
        else:
            if req.first_frame:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": self._img(req.first_frame)},
                    "role": "first_frame",
                })
            if req.last_frame:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": self._img(req.last_frame)},
                    "role": "last_frame",
                })

        duration = int(req.duration)
        payload: Dict[str, Any] = {
            "model": model,
            "content": content,
            # 顶层字段（强校验路径）—— 见上面 ①
            "ratio": _nr(req.aspect_ratio),
            "resolution": req.resolution,
            "duration": duration,
        }

        # ── 白膜/长视频两阶段：草稿与尾帧 ──
        if req.draft_task_id:
            # 用已有草稿出成片：把草稿任务 id 交回去，模型继承草稿的
            # prompt / 参考素材 / seed / ratio / duration 二次推理。
            # ★ 传了 draft_task_id 就**不要再传 duration/ratio**：官方语义是
            #   "继承草稿"，再传参数会与草稿冲突。这里顺手把参数摘掉，
            #   免得平台按"参数不一致"整个拒掉。
            payload["content"] = [
                {"type": "text", "text": req.prompt} if req.prompt else {"type": "text", "text": ""},
                {"type": "draft_task", "draft_task": {"id": req.draft_task_id}},
            ]
            payload.pop("duration", None)
            payload.pop("ratio", None)
            payload["resolution"] = "1080p"      # 成片档
        else:
            if req.draft:
                # 草稿模式锁 480P。传 1080p + draft 是矛盾请求。
                payload["draft"] = True
                payload["resolution"] = "480p"

        if req.return_last_frame:
            payload["return_last_frame"] = True
        if req.negative_prompt:
            payload["negative_prompt"] = req.negative_prompt

        return payload

    # ──────────── 生成 ────────────

    async def generate(self, req: VideoGenRequest) -> VideoGenResult:
        if not self.api_key:
            return self.make_result(False, error="即梦 API key 未配置")

        err = self.validate_request(req)
        if err:
            return self.make_result(False, error=err)

        is_draft = bool(req.draft and not req.draft_task_id)
        try:
            payload = self.build_payload(req)
        except ValueError as e:
            return self.make_result(False, error=str(e))

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
                    error=f"即梦 error: {data.get('error', r.text)}",
                    raw=data,
                )

            task_id = data.get("id") or data.get("task_id")
            if not task_id:
                return self.make_result(False, error=f"No task_id: {data}", raw=data)

            # 与 seedance 同一形态：每 5 秒查一次，最多 60 次（≈5 分钟）
            for _ in range(60):
                await asyncio.sleep(5)
                r2 = await client.get(
                    f"{self.BASE_URL}/contents/generations/tasks/{task_id}",
                )
                d2 = r2.json()
                status = d2.get("status")
                if status == "succeeded":
                    c = d2.get("content") or {}
                    video_url = c.get("video_url")
                    last_frame = c.get("last_frame_url") or d2.get("last_frame_url")
                    return self.make_result(
                        True, video_url=video_url,
                        duration=req.duration, task_id=task_id, raw=d2,
                        last_frame_url=last_frame, is_draft=is_draft,
                    )
                elif status == "failed":
                    return self.make_result(
                        False, error=self._readable_error(d2), task_id=task_id, raw=d2,
                    )

            return self.make_result(False, error="即梦超时（5 分钟未完成）", task_id=task_id)
        except httpx.HTTPError as e:
            return self.make_result(False, error=f"HTTP error: {e}")
        except Exception as e:
            return self.make_result(False, error=f"Error: {e}")

    @staticmethod
    def _readable_error(d: Dict[str, Any]) -> str:
        """把平台的失败结构翻成人话。

        方舟的失败响应有两种形态：`{"error": {"code":..., "message":...}}`
        和 `{"error": "字符串"}`。直接 str() 出来是一坨字典，用户在界面上
        根本看不出是内容审核还是参数错。
        """
        e = d.get("error")
        if isinstance(e, dict):
            code = e.get("code") or e.get("Code") or ""
            msg = e.get("message") or e.get("Message") or ""
            return f"{code} {msg}".strip() or str(e)[:300]
        if e:
            return str(e)[:300]
        return f"任务失败（原始响应：{str(d)[:200]}）"

    async def query_task(self, task_id: str) -> VideoGenResult:
        client = await self._ensure_client()
        try:
            r = await client.get(f"{self.BASE_URL}/contents/generations/tasks/{task_id}")
            d = r.json()
            if d.get("status") == "succeeded":
                c = d.get("content") or {}
                return self.make_result(True, video_url=c.get("video_url"),
                                        task_id=task_id, raw=d,
                                        last_frame_url=c.get("last_frame_url"))
            elif d.get("status") == "failed":
                return self.make_result(False, error=self._readable_error(d),
                                        task_id=task_id, raw=d)
            return self.make_result(False, error=f"Status: {d.get('status')}",
                                    task_id=task_id, raw=d)
        except Exception as e:
            return self.make_result(False, error=str(e))
