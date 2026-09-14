"""
MiniMax 海螺（Hailuo）视频适配器

═══════════════════════════════════════════════════════════════════
修复记录（2026-09-11）：原来三处全错，导致"配了 Key 也用不了"
═══════════════════════════════════════════════════════════════════
旧实现：
  1. BASE_URL = https://api.minimaxi.chat/v1   ← 域名错误
     正确是 https://api.minimaxi.com/v1
     后果：同一个 Key 在 .chat 域返回 base_resp 2049 "invalid api key"，
     用户看到的就是"我明明开通了却提示 Key 无效"。
  2. 查询任务用 GET /video_generation/{task_id}  ← 端点错误
     正确是 GET /query/video_generation?task_id=xxx
  3. 把返回的 file_id 直接当成视频 URL  ← 缺一步
     正确是再调 GET /files/retrieve?file_id=xxx 拿 file.download_url

正确流程（已实测跑通，产出 910KB 真实 mp4）：
  POST /video_generation                {model, prompt, duration, resolution} → task_id
  GET  /query/video_generation?task_id= → status: Preparing|Processing|Success|Fail, file_id
  GET  /files/retrieve?file_id=         → file.download_url
  GET  download_url                     → mp4 字节
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

import httpx

from . import VideoAdapter, VideoGenRequest, VideoGenResult, register

logger = logging.getLogger("videoforge.adapter.hailuo")

# 正确的 API 域名（.chat 是旧域，对视频接口会返回 2049 invalid api key）
API_BASE = "https://api.minimaxi.com/v1"

# 时长 → MiniMax 允许的档位（6s / 10s，按官方文档向上取整）
_ALLOWED_DURATIONS = (6, 10)


@register
class HailuoAdapter(VideoAdapter):
    name = "hailuo"
    display_name = "海螺 Hailuo（MiniMax）"
    description = "MiniMax 海螺视频，中文语义理解好，最长 10s"
    supported_aspect_ratios = ["16:9", "9:16", "1:1"]
    # 实测 MiniMax 只有这三档（此前写的 720P 是错的，会导致参数归一失败）
    supported_resolutions = ["512P", "768P", "1080P"]
    max_duration = 10
    min_duration = 6
    # ⚠ 海螺**支持图生视频**（first_frame_image），但不支持尾帧。
    #   旧代码用 supports_first_last_frame=False 一刀切 → 场景首帧一传进去
    #   就被校验拒绝（报"does not support first/last frame"），
    #   用户看到的现象就是"海螺在分镜和生成两个区域都用不了"。
    supports_first_last_frame = False
    _supports_first_frame = True
    supports_character_ref = False
    supports_negative_prompt = False
    is_local = False
    requires_api_key = True

    BASE_URL = API_BASE

    def _default_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    # ──────────── 内部工具 ────────────

    @staticmethod
    def _norm_duration(d: Any, resolution: str = "768P") -> int:
        """把请求时长归一到厂商允许的档位。

        ⚠ 实测坑：MiniMax 的时长上限**跟分辨率挂钩** ——
        1080P 只支持 6s；768P/512P 支持 6s 与 10s。
        之前不分分辨率一律给 10s，1080P 请求直接被拒：
        `[2013] invalid params, param 'duration' only support 6s for model MiniMax-Hailuo-02 1080P`
        """
        try:
            d = int(d)
        except (TypeError, ValueError):
            d = 6
        if str(resolution).upper() == "1080P":
            return 6
        return 6 if d <= 6 else 10

    def resolve_params(self, want_res: str, want_dur: int) -> Dict[str, Any]:
        """按**实测能力矩阵**挑一组合法参数（不同版本上限不同）。

        本机真实账号实测：
          video-01 / T2V-01       : 512P·768P·1080P 都支持 6s 与 10s
          MiniMax-Hailuo-02 / 2.3 : 768P 支持 6/10s，**1080P 只支持 6s**
          T2V-01-Director         : 512P 支持 6/10s，768P 只支持 6s

        用户要"1080P + 10 秒"时，旧行为是直接报
        `[2013] invalid params, param 'duration' only support 6s...`；
        现在自动挑一组能跑的并说明调整了什么，不让用户撞墙。
        """
        model = self.config.get("model_name") or "MiniMax-Hailuo-02"
        try:
            from core.model_catalog import pick_valid_combo
            return pick_valid_combo("hailuo", model, want_res, int(want_dur or 6))
        except Exception:
            return {"resolution": self._norm_resolution(want_res),
                    "duration": self._norm_duration(want_dur, want_res), "adjusted": False}

    @staticmethod
    def _norm_resolution(res: Any) -> str:
        r = str(res or "").upper()
        if r in ("512P", "768P", "1080P"):
            return r
        if r in ("480P", "540P"):
            return "512P"
        return "768P"

    async def _retrieve_url(self, client: httpx.AsyncClient, file_id: Any) -> str:
        """file_id → 可下载 URL（这一步旧代码完全缺失）"""
        if not file_id:
            return ""
        r = await client.get(f"{self.BASE_URL}/files/retrieve",
                             headers=self._default_headers(),
                             params={"file_id": file_id})
        d = r.json()
        return ((d.get("file") or {}).get("download_url")) or ""

    # ──────────── 主流程 ────────────

    def check_model_mode(self, has_first_frame: bool) -> Optional[str]:
        """按该版本的**真实模式**校验（文生视频 / 图生视频）。

        实测结论：
          I2V-01 / I2V-01-Director / I2V-01-live / video-01-live2d
            → **图生视频，必须传 first_frame_image**，否则厂商报
              `invalid params, I2V-01: first_frame_image is required`
          T2V-01 / T2V-01-Director / video-01 / Hailuo-02 / Hailuo-2.3
            → 文生视频，不传首帧也能跑

        以前没这层校验，用户选到 I2V 版本而该分镜又没有场景图时，
        只能看到厂商的英文报错，然后被静默降级成本地合成。
        """
        model = self.config.get("model_name") or ""
        try:
            from core.model_catalog import VIDEO_CATALOG
            conf = VIDEO_CATALOG.get("hailuo") or {}
            m = next((x for x in (conf.get("models") or []) if x["id"] == model), None)
            # 明确不可用的版本名，直接拦掉并说明原因
            un = next((x for x in (conf.get("unavailable") or []) if x["id"] == model), None)
            if un:
                return f"「{model}」不可用：{un.get('reason')}。请换一个版本。"
            if m and m.get("mode") == "i2v" and not has_first_frame:
                return (f"「{model}」是**图生视频**版本，必须有一张首帧图。"
                        f"该分镜当前没有可用的场景图/角色图 —— "
                        f"请先到「场景」页用「🎨 AI 生成场景图」生成场景图，"
                        f"或改用文生视频版本（video-01 / T2V-01 / Hailuo-02 / Hailuo-2.3）。")
        except Exception:
            pass
        return None

    async def generate(self, req: VideoGenRequest) -> VideoGenResult:
        if not self.api_key:
            return self.make_result(False, error="未配置 MiniMax（海螺）API Key")

        # 模式校验：图生视频版本缺首帧时，给出**中文可操作**的提示
        mode_err = self.check_model_mode(bool(req.first_frame))
        if mode_err:
            return self.make_result(False, error=mode_err)

        model = self.config.get("model_name") or "MiniMax-Hailuo-02"
        # ★ 按该版本的真实能力矩阵挑合法组合（1080P+10s 这类会**自动调整**而不是报错）
        rp = self.resolve_params(req.resolution, req.duration)
        self._last_adjust = rp
        if rp.get("adjusted"):
            logger.info("海螺 %s 参数已按能力矩阵调整：请求 %s/%ss → 实际 %s/%ss（该版本可用：%s）",
                        model, req.resolution, req.duration,
                        rp["resolution"], rp["duration"], rp.get("all_caps"))
        payload: Dict[str, Any] = {
            "model": model,
            "prompt": (req.prompt or "")[:2000],
            "duration": rp["duration"],
            "resolution": rp["resolution"],
        }
        # 图生视频：MiniMax 用 first_frame_image（URL 或 data URI）
        if req.first_frame:
            payload["first_frame_image"] = req.first_frame

        client = await self._ensure_client()
        try:
            r = await client.post(f"{self.BASE_URL}/video_generation",
                                  headers=self._default_headers(), json=payload)
            try:
                data = r.json()
            except Exception:
                return self.make_result(False, error=f"MiniMax 返回非 JSON：{r.text[:200]}")

            base = data.get("base_resp") or {}
            code = base.get("status_code")
            if r.status_code != 200 or code not in (0, None):
                msg = base.get("status_msg") or (r.text or "")[:200]
                hint = ""
                if code == 2049:
                    hint = ("（Key 无效。请确认用的是 api.minimaxi.com 控制台的 Key；"
                            "对话 Key 与视频 Key 可能不是同一个）")
                elif code == 1002:
                    hint = "（触发限流，稍后重试）"
                elif code == 1008:
                    hint = "（余额不足）"
                return self.make_result(
                    False, error=f"MiniMax 创建任务失败 [{code}] {msg}{hint}", raw=data)

            task_id = data.get("task_id")
            if not task_id:
                return self.make_result(False, error=f"MiniMax 未返回 task_id：{data}", raw=data)

            # 轮询：Preparing → Processing → Success / Fail（最长约 10 分钟）
            for _ in range(100):
                await asyncio.sleep(6)
                q = await client.get(f"{self.BASE_URL}/query/video_generation",
                                     headers=self._default_headers(),
                                     params={"task_id": task_id})
                d2 = q.json()
                status = d2.get("status")
                if status == "Success":
                    url = await self._retrieve_url(client, d2.get("file_id"))
                    if not url:
                        return self.make_result(
                            False,
                            error=f"任务成功但取不到下载地址（file_id={d2.get('file_id')}）",
                            task_id=task_id, raw=d2)
                    return self.make_result(True, video_url=url,
                                            duration=req.duration, task_id=task_id, raw=d2)
                if status in ("Fail", "Failed"):
                    return self.make_result(
                        False,
                        error="MiniMax 生成失败："
                              + str((d2.get("base_resp") or {}).get("status_msg") or d2),
                        task_id=task_id, raw=d2)
                # Preparing / Processing → 继续等
            return self.make_result(False, error="MiniMax 任务超时（>10 分钟）", task_id=task_id)
        except httpx.HTTPError as e:
            return self.make_result(False, error=f"网络错误：{e}")
        except Exception as e:
            logger.exception("hailuo generate failed")
            return self.make_result(False, error=f"{type(e).__name__}: {e}")

    async def query_task(self, task_id: str) -> VideoGenResult:
        client = await self._ensure_client()
        try:
            q = await client.get(f"{self.BASE_URL}/query/video_generation",
                                 headers=self._default_headers(),
                                 params={"task_id": task_id})
            d = q.json()
            status = d.get("status")
            if status == "Success":
                url = await self._retrieve_url(client, d.get("file_id"))
                return self.make_result(True, video_url=url, task_id=task_id, raw=d)
            if status in ("Fail", "Failed"):
                return self.make_result(False, error=str(d), task_id=task_id, raw=d)
            return self.make_result(False, error=f"仍在生成中：{status}", task_id=task_id, raw=d)
        except Exception as e:
            return self.make_result(False, error=str(e))
