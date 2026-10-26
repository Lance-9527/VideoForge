"""
豆包 Seedance 适配器（字节跳动）

APPSO 拍沙丘 3 用的是 Seedance 2.5，本适配器实现对应接口。
"""

import asyncio
import logging
import os
from typing import Dict, Any, List
import httpx

from . import VideoAdapter, VideoGenRequest, VideoGenResult, register

logger = logging.getLogger("videoforge.adapters.seedance")


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

    # ── 白膜/长视频两阶段（2026-09-23 补齐）────────────────────────
    # 草稿模式：官方的"先出 480P 样片、确认后再出成片"。
    # 官方数据：抽卡 4 次命中省 57%，20 次命中省 77%。
    supports_draft = True
    # 平台可直接返回尾帧，长视频续接优先用它（省掉本地抽帧，
    # 也避免"抽到的不是真正的末帧"）
    supports_last_frame_return = True

    BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"

    def _default_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": "VideoForge/1.0",
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def describe_limits(self) -> Dict[str, Any]:
        return {
            "duration": [self.min_duration, self.max_duration],
            "draft": getattr(self, "supports_draft", False),
            "return_last_frame": getattr(self, "supports_last_frame_return", False),
            "max_reference_images": getattr(self, "max_reference_images", 0),
            "note": "首帧/尾帧与参考图是互斥场景，不能混发（平台会静默只认一个）",
        }

    def _img(self, p: str) -> str:
        """图片字段过这一层：本地路径 → data URI。

        ★★ 为什么必须做（2026-09-23，在真调平台前的 dry-run 里抓到的）：
          火山方舟**只接受 http(s) URL 或 base64 data URI**，不接受本地文件路径。
          而 `core/longvideo.grab_last_frame()` 返回的、以及部分调用方直接塞进
          `first_frame` 的，都是**本地路径**（`C:\\...\\seg_01_last.jpg`）。
          直接发出去平台必然 400，而报错信息是一句平台侧的参数错误，
          用户完全看不出"其实是本地路径没转"——这正是历史上那句
          "配了 Key 也用不了模型"的成因。

          原来只有 `orchestrator.generate_shot_handler` 在**调用方**转了；
          只要有任何一条别的路径（长视频续接、agent 工具、以后新加的调用点）
          忘了转，就会踩同一个坑。所以兜在适配器这一层，一次修好所有调用方。
        """
        if not p:
            return ""
        try:
            from . import to_image_ref
            return to_image_ref(p) or p
        except Exception:
            return p

    @staticmethod
    def _is_video_ref(p: str) -> bool:
        """判断一个参考素材是**视频**还是图片。

        ★ 为什么需要：白膜（previz）是**视频**，而它和角色/场景参考图
          走的是同一个 `reference_images` 字段。全按 `role: reference_image`
          发出去，平台要么拒、要么把视频当图片处理。
          Seedance 官方把 clay render 视频列为一等参考类型 —— 它得走
          `reference_video` 才算数。
        """
        s = str(p or "").lower()
        if s.startswith("data:"):
            return s.startswith("data:video/")
        return os.path.splitext(s.split("?")[0])[1] in (
            ".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v")

    def build_payload(self, req: VideoGenRequest) -> Dict[str, Any]:
        """按官方契约构造请求体。抽成独立方法是为了**能单测**。

        ★★ 2026-09-14 按**官方契约**重写（用户："我用了 seedance…关联了图，
           但生成的视频没按剧本走，该出现的人物没出现，场景有"）：

        ① **参数必须在 body 顶层**。官方《创建视频生成任务》文档里
           `ratio/resolution/duration/seed/watermark` 全是顶层字段，**没有
           `parameters` 这一层**；官方还写明两种校验强度：
             · 新方式（body 顶层字段）= **强校验**，写错会报错；
             · 旧方式（提示词后缀 `--ratio …`）= **弱校验**，写错**被忽略**。
           我们原来把参数塞进 `"parameters": {...}` —— 既不是合法字段、
           又落在"被忽略"那条路上：`ratio` 实际没生效，模型按默认 `adaptive`
           跟着那张**方形参考图**出了 960x960(1:1)，然后被我们自己的画幅守卫
           丢掉，退回本地幻灯片。这就是用户看到的那一幕。

        ② **`first_frame/last_frame` 与 `reference_image` 是三种互斥场景，
           不能混用**。所以这里二选一（不是"都发"）：
             · 有角色参考图 → 走「多模态参考」：角色图 + 场景图**全部**作为
               `reference_image`（官方支持多张），人物身份与场景一起锁；
             · 没有角色图 → 走「首/尾帧」：first_frame(+last_frame) 负责构图衔接。
        """
        from core.aspect import normalize_ratio as _nr
        model = self.config.get("model_name") or "doubao-seedance-2-5-260628"

        content: List[Dict[str, Any]] = [{"type": "text", "text": req.prompt}]

        # ★ 不要写成 `req.character_reference or req.reference_images`：
        #   只要角色图非空，场景图就**整批被丢掉**（且不报错）。
        #   正确做法是两个都收，角色图在前、场景图在后 ——
        #   官方建议用 `[图1]xxx，[图2]xxx` 绑定图与主体，编号按这个顺序生成。
        refs: List[str] = []
        seen = set()
        for r in list(req.character_reference or []) + list(req.reference_images or []):
            if r and r not in seen:
                seen.add(r)
                refs.append(r)
        refs = [self._img(r) for r in refs if not self._is_video_ref(r)]
        # 视频类参考（白膜 clay render / 运镜参考）单独走 reference_video，
        # 不能混进 reference_image —— 见 `_is_video_ref` 的说明。
        video_refs: List[str] = []
        # ★★ 2026-10-24（用户：「彻底解决白膜→480p，这是招牌」）：
        #   白膜参考走的是 `req.video_url`（main.py:1778 唯一通道），
        #   原来这里只从 `req.reference_images` 里挑视频 —— 于是白膜 URL 从没被发出去。
        #   这行是关键修复，别删。
        _pv = str(getattr(req, "video_url", "") or "").strip()
        if _pv:
            video_refs.append(_pv)
        for r in list(req.reference_images or []):
            if r and self._is_video_ref(r) and r not in video_refs:
                video_refs.append(r)

        if refs or video_refs:
            all_refs = refs + ([self._img(req.first_frame)] if req.first_frame else [])
            cap = max(1, int(getattr(self, "max_reference_images", 1) or 1))
            for r in all_refs[:cap]:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": r},
                    "role": "reference_image",
                })
            # ★★ 2026-09-29（真跑撞到的第二堵墙）：**视频参考必须是平台能下载的 URL**。
            #
            #   实测原话（真跑第 1 镜，未计费）：
            #     The parameter `content[3].video_url.url` specified in the request is
            #     not valid: **resource download failed**
            #   —— 因为白膜（greybox）是**本地 mp4 文件**，我们直接把本地路径当 URL 发了，
            #   平台去下载当然失败，**整单 400**。
            #   而这一条 400 里没有 r2v/draft 的字样 ⇒ 降级链也救不回来（整单失败）。
            #
            #   修法：视频参考**只在它确实是 http(s) 可达地址时才发**；
            #   本地文件一律**不发视频参考**（并把这件事记进降级说明，
            #   让用户知道"这一条没有带白膜视频参考"）。
            #   图片那条路不受影响（`_img()` 会把本地图片转成 base64 data URI）。
            _video_ok = []
            for v in video_refs[:cap]:
                vs = str(v or "")
                if vs.startswith("http://") or vs.startswith("https://"):
                    _video_ok.append(vs)
                else:
                    logger.warning("白膜/视频参考是本地文件（平台取不到），"
                                   "这一条不发视频参考：%s", vs[-60:])
            self._skipped_local_video_ref = bool(video_refs) and not _video_ok
            for v in _video_ok:
                content.append({
                    "type": "video_url",
                    "video_url": {"url": v},
                    "role": "reference_video",
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

        payload: Dict[str, Any] = {
            "model": model,
            "content": content,
            "resolution": req.resolution,
            "duration": req.duration,
        }

        # ★★ `ratio` 只在**纯文生视频**时发（2026-09-23 真调平台后订正）。
        #   平台原话：
        #     "The parameter ratio specified in the request is not valid.
        #      For first-frame or first-last-frame generation, the output ratio
        #      follows the first-frame image."
        #   即：**给了首帧（或首尾帧）就不许再传 ratio**，画幅由首帧图决定。
        #
        #   这条约束以前一直没暴露，是因为旧代码把 ratio 塞在 `parameters` 里，
        #   落在"弱校验、写错被忽略"那条路上 —— 既不生效、也不报错。
        #   一旦按官方要求挪到顶层（强校验），它就浮出来了。
        #
        #   ★ 推论（很重要）：**首帧图的画幅直接决定成片画幅**。
        #     所以 previz 里那条"参考图必须裁成影片精确画幅"是**硬需求**，
        #     不是建议 —— 不裁的话白膜设计好的构图会被参考图整个带跑。
        if not (refs or req.first_frame or req.last_frame):
            payload["ratio"] = _nr(req.aspect_ratio)

        # ── 白膜/长视频两阶段 ──
        if req.draft_task_id:
            # 用已有草稿出成片：把草稿任务 id 交回去，模型继承草稿的
            # prompt / 参考素材 / seed / ratio / duration 二次推理。
            # ★ 传了 draft_task_id 就不要再传 duration/ratio —— 官方语义是
            #   "继承草稿"，再传会与草稿冲突。这里顺手摘掉，免得平台整单拒。
            payload["content"] = [
                {"type": "text", "text": req.prompt or ""},
                {"type": "draft_task", "draft_task": {"id": req.draft_task_id}},
            ]
            payload.pop("duration", None)
            payload.pop("ratio", None)
            payload["resolution"] = "1080p"
        elif req.draft:
            # 草稿模式锁 480P。传 1080p + draft 是自相矛盾的请求。
            payload["draft"] = True
            payload["resolution"] = "480p"

        if req.return_last_frame:
            payload["return_last_frame"] = True
        if req.negative_prompt:
            payload["negative_prompt"] = req.negative_prompt

        return payload

    async def generate(self, req: VideoGenRequest) -> VideoGenResult:
        if not self.api_key:
            return self.make_result(False, error="Seedance API key 未配置")

        err = self.validate_request(req)
        if err:
            return self.make_result(False, error=err)

        is_draft = bool(req.draft and not req.draft_task_id)
        try:
            payload = self.build_payload(req)
        except Exception as e:
            return self.make_result(False, error=f"构造请求失败：{e}")

        client = await self._ensure_client()
        # ★★ 2026-09-29（用户第 2、8、9 条：真跑 480P 时暴露）
        #
        #   原来的自愈是**两段写死的顺序**：先试"去 draft"，再试"去参考图"。
        #   实测《执棋者》第 1 镜的真实经过：
        #     ① r2v + draft → 报 `task_type r2v does not support model …`
        #        （不含 "draft" 字样）⇒ 走"去参考图"那一段 ⇒ 变成 t2v + **draft 仍在**
        #     ② t2v + draft → 报 `draft is not supported … in t2v`
        #        ⇒ "去 draft"那一段**已经过去了，不会再跑** ⇒ 直接失败 ✗
        #   也就是说：**需要连着降两次**的模型，一步都出不来。
        #   用户看到的正是「流程跑不通」。
        #
        #   修法：把降级改成**循环**（最多 3 次），每次按错误内容摘掉一个障碍，
        #   并把"摘掉了什么"记下来 —— 参考图被摘掉这件事**必须让用户知道**
        #   （他的原话就是"角色图必须真能用上"，悄悄不用是最坏的结果）。
        degraded: List[str] = []
        try:
            data, r = await self._post_with_degrade(
                client, payload, req, degraded, is_draft)
        except Exception as e:  # noqa: BLE001
            return self.make_result(False, error=f"Seedance 请求异常：{e}")
        if r.status_code != 200:
            return self.make_result(
                False,
                error=(f"Seedance error: {data.get('error', getattr(r, 'text', ''))}"
                       + (f"（已尝试降级：{'、'.join(degraded)}）" if degraded else "")),
                raw=data,
            )
        is_draft = is_draft and not degraded
        try:
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
                logger.info("API: status=%s | usage=%s",
                            status, d2.get("usage"))
                if status == "succeeded":
                    c = d2.get("content") or {}
                    video_url = c.get("video_url")
                    last_frame = c.get("last_frame_url") or d2.get("last_frame_url")
                    # ★ 把"这次被摘掉了什么"塞进 raw —— 上层（工具/界面）要能
                    #   如实告诉用户「这一条没有用上你的角色图」。
                    _raw = dict(d2 or {})
                    if degraded:
                        _raw["_degraded"] = list(degraded)
                    # ★★ 2026-10-06：算本次成本。generate() 走轮询不走 query_task，
                    #   这里算 cost 一次性算清。
                    _cost = self._estimate_cost(d2)
                    return self.make_result(
                        True, video_url=video_url, duration=req.duration,
                        task_id=task_id, raw=_raw,
                        last_frame_url=last_frame, is_draft=is_draft,
                        cost=_cost,
                    )
                elif status == "failed":
                    return self.make_result(
                        False, error=self._readable_error(d2), task_id=task_id, raw=d2,
                    )

            return self.make_result(False, error="Seedance timeout", task_id=task_id)
        except httpx.HTTPError as e:
            return self.make_result(False, error=f"HTTP error: {e}")
        except Exception as e:
            return self.make_result(False, error=f"Error: {e}")

    @staticmethod
    def _readable_error(d: Dict[str, Any]) -> str:
        """把平台的失败结构翻成人话（error 可能是 dict，也可能是字符串）。"""
        e = d.get("error")
        if isinstance(e, dict):
            code = e.get("code") or e.get("Code") or ""
            msg = e.get("message") or e.get("Message") or ""
            return f"{code} {msg}".strip() or str(e)[:300]
        if e:
            return str(e)[:300]
        return f"任务失败（原始响应：{str(d)[:200]}）"

    async def _post_with_degrade(self, client, payload: Dict[str, Any],
                                 req, degraded: List[str], is_draft: bool):
        r"""发请求；被平台以"这个模型不支持某参数"拒绝时**逐个摘掉**再重发。

        ★★ 2026-09-29：这是这次真跑 480P 撞出来的核心缺陷。

        实测《执棋者》第 1 镜（`doubao-seedance-1-0-pro-250528`）的真实经过：
          ① 带参考图 + draft（r2v）→ `task_type r2v does not support model …`
          ② 摘掉参考图 → 变成 t2v，**但 draft 还在** → `draft is not supported … in t2v`
          ③ 老的写法"去 draft"那一段**已经执行过了**（在①之前判的），不会再跑
             ⇒ 直接失败。**需要连着降两次的模型，一步都出不来。**

        现在改成**循环**：每次只摘一个障碍、最多 3 轮，直到成功或没有可摘的。
        `degraded` 里记下**每一次摘掉了什么** —— 这不是内部细节，
        「参考图被摘掉」必须让用户看见（他的要求就是"角色图必须真能用上"）。
        """
        attempt = 0
        data: Dict[str, Any] = {}
        while attempt < 3:
            attempt += 1
            r = await client.post(
                f"{self.BASE_URL}/contents/generations/tasks", json=payload)
            data = r.json()
            if r.status_code == 200:
                return data, r
            blob = str(data).lower() + " " + str(getattr(r, "text", "")).lower()
            # ① draft / 分辨率 被拒
            if "draft" in blob and ("draft" in payload or "resolution" in payload):
                logger.warning("该模型不支持 draft —— 摘掉重发（第 %d 次）", attempt)
                try:
                    from core import tiers as _tiers
                    _tiers.note_draft_rejected(
                        self.config.get("model_name") or "",
                        evidence=str(data.get("error") or data)[:400])
                except Exception as _e:  # noqa: BLE001
                    logger.warning("记录 draft 不支持失败：%s", _e)
                payload.pop("draft", None)
                payload.pop("resolution", None)
                degraded.append("draft（低价小样档）")
                continue
            # ② 参考图生视频（r2v）被拒
            #
            # ★★ 2026-09-29：**真正的修法不是把参考图扔掉**。
            #
            #   真跑实测（未计费）：
            #     带 `role: reference_image` 的请求 → 平台回答
            #       `InvalidParameter: the specified task_type r2v does not support
            #        model doubao-seedance-1-0-pro`
            #     —— 注意：**我们从来没发过 `task_type`**，是平台**按 content 里的
            #     `role: reference_image` 自己把这次请求归类成 r2v** 的。
            #     换成 `doubao-seedance-2-5-260628`（能力表里明明支持图生视频）
            #     **同样被拒** ⇒ 问题在"**我们用了 r2v 这个模态**"，不在模型。
            #
            #   而"图生视频"在方舟上的**通用**形态是 **首帧（first_frame / i2v）**：
            #     有首帧图 → 平台按 i2v 处理，画幅也由首帧图决定。
            #
            #   旧代码在这里做的是 `payload.pop(...)` 把参考图**全扔了** ——
            #   于是"资产里的角色图"**永远用不上**，钱还是照花。
            #   这正是用户第 2 条抱怨的根。
            #
            #   现在改成：**把第一张参考图降级成首帧**（i2v），
            #   图还在、人物身份还能锁；只是"多张参考图"这一路没了。
            #   并把这个降级**如实记下来**。
            if "task_type" in blob or "r2v" in blob or "reference_image" in blob:
                # ⚠ 只处理**还是 reference_image 的那些**：已经降级成 first_frame 之后
                #   再进这里就会**无限转换**（自测当场撞到：三次请求内容一模一样、
                #   最后整单失败）。所以必须按 role 判断，而不是"有没有 image_url"。
                _refs = [c for c in (payload.get("content") or [])
                         if isinstance(c, dict) and c.get("type") == "image_url"
                         and str(c.get("role") or "") == "reference_image"]
                _all_imgs = [c for c in (payload.get("content") or [])
                             if isinstance(c, dict) and c.get("type") == "image_url"]
                if _refs:
                    url = ((_refs[0].get("image_url") or {}).get("url")
                           or _refs[0].get("url") or "")
                    if url:
                        logger.warning("该模型不收 r2v（reference_image）—— "
                                       "改用**首帧 i2v** 保住那张图重发")
                        payload.pop("task_type", None)
                        payload["content"] = [
                            {"type": "text", "text": req.prompt or ""},
                            {"type": "image_url", "image_url": {"url": url},
                             "role": "first_frame"},
                        ]
                        # 首帧决定画幅 ⇒ 不能再传 ratio（平台会整单拒）
                        payload.pop("ratio", None)
                        _rest = len(_all_imgs) - 1
                        degraded.append(
                            "多模态参考（r2v）→ 改用**首帧 i2v**：保住了第 1 张参考图"
                            + (f"，另外 {_rest} 张没带上" if _rest > 0 else ""))
                        continue
                    # 拿不到 url：只能退回"丢掉参考图"（并且明说）
                    logger.warning("r2v 被拒且取不到图片地址 —— 只能去掉参考图重发")
                    payload.pop("task_type", None)
                    for _k in ("reference_images", "reference_image", "image", "images",
                               "first_frame", "last_frame", "content"):
                        payload.pop(_k, None)
                    payload["content"] = [{"type": "text", "text": req.prompt or ""}]
                    degraded.append("参考图（这一条**没有用上你的角色/场景图**）")
                    continue
                # 已经是 first_frame 还被说 r2v 不支持 → 这个模型连首帧都不收，
                # 只能如实失败（**不要**再空转重试）
                logger.warning("已是首帧仍被拒（r2v 不支持）：%s", str(data)[:200])
            break
        # ★★ 2026-09-29（真跑撞到，未计费）：平台的**内容策略**拒收要记下来。
        #   实测两类：
        #     · InputImageSensitiveContentDetected.PrivacyInformation
        #       "the input image 'content[1]' may contain real person"
        #     · OutputVideoSensitiveContentDetected.PolicyViolation
        #   记下来之后，**下一次**出片时卡片能提前说"这张图被拒过、建议换风格"，
        #   而不是让用户再撞一次同样的墙。
        try:
            from core import imagepolicy as _ipol
            _kind = _ipol.classify_error(data)
            if _kind:
                for _p in (list(req.character_reference or [])
                           + list(req.reference_images or [])):
                    if _p and not str(_p).startswith("data:"):
                        _ipol.note_rejected(str(_p), data, kind=_kind)
                logger.warning("平台按内容策略拒了这次请求（%s）", _kind)
        except Exception as _e:  # noqa: BLE001
            logger.debug("记内容策略失败：%s", _e)
        return data, r

    async def query_task(self, task_id: str) -> VideoGenResult:
        client = await self._ensure_client()
        try:
            r = await client.get(f"{self.BASE_URL}/contents/generations/tasks/{task_id}")
            d = r.json()
            logger.info("Seedance task 响应 keys: %s | usage=%s",
                        list(d.keys())[:10], d.get("usage"))
            if d.get("status") == "succeeded":
                c = d.get("content") or {}
                # 2026-10-06：推算本次成本（元）。Seedance 不直接返金额，
                # 但返 usage.total_tokens * 单价 = 价格。
                _cost = self._estimate_cost(d)
                return self.make_result(True, video_url=c.get("video_url"),
                                        task_id=task_id, raw=d,
                                        last_frame_url=c.get("last_frame_url"),
                                        cost=_cost)
            elif d.get("status") == "failed":
                # 失败也计费（凭据是 usage 字段，与成功同样位置）
                _cost = self._estimate_cost(d)
                return self.make_result(False, error=self._readable_error(d),
                                        task_id=task_id, raw=d, cost=_cost)
            return self.make_result(False, error=f"Status: {d.get('status')}",
                                    task_id=task_id, raw=d)
        except Exception as e:
            return self.make_result(False, error=str(e))

    def _estimate_cost(self, task_resp: dict) -> float:
        """
        按平台 usage × 官方单价推算本次成本（元）。
        Seedance 1.0 pro 480P/720P = 0.015 元/千 tokens
        Seedance 2.5 480P/720P = 0.07 元/千 tokens
        """
        try:
            # 平台用不同字段名（总 tokens / 完成 tokens），都试一下
            usage = (
                task_resp.get("usage")
                or task_resp.get("Usage")
                or (task_resp.get("data") or {}).get("usage")
                or {}
            )
            tokens = float(
                usage.get("total_tokens")
                or usage.get("completion_tokens")
                or usage.get("TotalTokens")
                or usage.get("InputTokens")
                or 0
            )
            # 备选：直接拿任务完成时间 * 平均 token/秒估算
            if tokens <= 0:
                # 一些平台不返 usage，用 duration × 经验值（1.0 pro 镜头 ~22 tokens/秒）
                dur = task_resp.get("duration") or task_resp.get("video_duration") or 0
                if dur > 0:
                    tokens = dur * 22  # 经验估算：1.0 pro 镜头 ~22 tokens/秒
            if tokens <= 0:
                logger.info("_estimate_cost: task_resp 找不到 usage 字段，task_resp keys=%s", list(task_resp.keys())[:10])
                return 0.0
            model = self.config.get("model_name", "") or ""
            # 火山方舟价格表（实际账单反推 2026-10-06）
            if "2-5" in model or "2.5" in model:
                per_k = 0.07   # ¥/千 tokens
            else:
                per_k = 0.015  # 1.0 pro 默认
            _cost = round(tokens * per_k / 1000.0, 4)
            logger.info("_estimate_cost: tokens=%s model=%s per_k=%s cost=%s",
                       tokens, model, per_k, _cost)
            return _cost
        except Exception as e:
            logger.info("_estimate_cost 异常：%s", e)
            return 0.0
