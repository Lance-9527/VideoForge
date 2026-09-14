"""
VideoForge · Microsoft Edge TTS 适配器（免费 / 无需 API Key · fallback）

参考 MPT voice.py 的 edge_tts 处理：
- voice_id 格式：`edge:<voice_name>` 或 `edge:<voice_name>-<gender>`
- 例如：`edge:zh-CN-XiaoxiaoNeural` `edge:en-US-JennyNeural`
- 微软 edge-tts 库做底层（pip install edge-tts）

特点：
- 零配置、无 API Key、100+ 中英文音色
- 原生 SubMaker 支持返回 word-level 时间戳（字幕对齐）
- 高峰期偶发 429 限流
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import List

from .base import (
    TTSRequest,
    TTSResult,
    VoiceInfo,
    VoiceProvider,
    register,
)


logger = logging.getLogger("videoforge.voice.edge")


# 选出的高质常用音色（来自 MPT data/azure_voices.json 子集 + 实战常用）
EDGE_VOICES = [
    # 中文
    ("zh-CN-XiaoxiaoNeural",       "Female", "zh-CN", "晓晓 · 温柔女声"),
    ("zh-CN-YunxiNeural",          "Male",   "zh-CN", "云希 · 阳光男声"),
    ("zh-CN-YunjianNeural",        "Male",   "zh-CN", "云健 · 体育男声"),
    ("zh-CN-XiaoyiNeural",         "Female", "zh-CN", "晓伊 · 文艺女声"),
    ("zh-CN-YunyangNeural",        "Male",   "zh-CN", "云扬 · 专业男声（新闻）"),
    ("zh-CN-XiaochenNeural",       "Female", "zh-CN", "晓辰 · 甜美女声"),
    ("zh-CN-XiaohanNeural",        "Female", "zh-CN", "晓涵 · 情感女声"),
    ("zh-CN-XiaomengNeural",       "Female", "zh-CN", "晓梦 · 儿童女声"),
    ("zh-CN-XiaomoNeural",         "Female", "zh-CN", "晓墨 · 文艺女声"),
    ("zh-CN-XiaoruiNeural",        "Female", "zh-CN", "晓睿 · 成熟女声"),
    ("zh-CN-XiaoshuangNeural",     "Female", "zh-CN", "晓双 · 童趣女声"),
    ("zh-CN-XiaoxuanNeural",       "Female", "zh-CN", "晓萱 · 客服女声"),
    ("zh-CN-XiaoyanNeural",        "Female", "zh-CN", "晓颜 · 客服女声"),
    ("zh-CN-XiaozhenNeural",       "Female", "zh-CN", "晓甄 · 多情感女声"),
    # 英文
    ("en-US-JennyNeural",          "Female", "en-US", "Jenny · 客服女声"),
    ("en-US-GuyNeural",            "Male",   "en-US", "Guy · 新闻男声"),
    ("en-US-AriaNeural",           "Female", "en-US", "Aria · 通用女声"),
    ("en-US-DavisNeural",          "Male",   "en-US", "Davis · 通用男声"),
    ("en-US-SaraNeural",           "Female", "en-US", "Sara · 童趣女声"),
    ("en-US-TonyNeural",           "Male",   "en-US", "Tony · 体育男声"),
    # 日文
    ("ja-JP-NanamiNeural",         "Female", "ja-JP", "七海 · 通用女声"),
    ("ja-JP-KeitaNeural",          "Male",   "ja-JP", "慶太 · 通用男声"),
]


@register
class EdgeTTS(VoiceProvider):
    """微软 Edge TTS（免费，无需 API Key）"""

    name = "edge"
    display_name = "Edge TTS"
    description = "微软 Edge 浏览器内置 TTS · 免费 · 100+ 中英文音色 · 无需 API Key。"
    prefix = "edge:"
    requires_api_key = False

    # ★ 配音模型：Edge 是**免费**引擎，接口里没有"模型"这个参数
    #   （声音由音色 id 决定，版本随微软服务端更新）。
    #   与其编一个假型号让人选，不如如实说明 —— 想选型号就换有型号的引擎。
    supports_model = False
    model_note = "Edge 免费引擎没有模型参数（声音由音色决定，版本随服务端更新）"

    async def list_voices(self) -> List[VoiceInfo]:
        return [
            VoiceInfo(
                voice_id=f"edge:{name}",
                display_name=f"{label} ({gender})",
                language=lang,
                gender=gender.lower(),
                is_builtin=True,
            )
            for name, gender, lang, label in EDGE_VOICES
        ]

    async def synthesize(self, req: TTSRequest) -> TTSResult:
        # 懒加载 edge_tts（避免硬依赖）
        # 懒加载 edge_tts（避免硬依赖）
        try:
            import edge_tts
            from edge_tts import SubMaker
        except ImportError:
            return TTSResult(
                success=False,
                provider=self.name,
                voice_id=req.voice_id,
                error="未安装 edge-tts，请运行：pip install edge-tts",
            )

        # 解析 voice_id: "edge:<voice_name>" → "<voice_name>"
        voice_short_name = req.voice_id[len(self.prefix):].strip()

        # rate / volume / pitch 转 edge-tts 格式
        # ★ pitch 是补上的关键一环：edge-tts 7.x 的 Communicate 支持 `pitch="+0Hz"`，
        #   而旧代码根本没传 —— 于是"情绪"永远只体现在语速上，听起来还是同一个人
        #   平铺直叙。见 `core/prosody.py` 的说明。
        rate_pct = int((req.rate - 1.0) * 100)
        rate_str = f"{'+' if rate_pct >= 0 else ''}{rate_pct}%"
        vol_pct = int((req.volume - 1.0) * 100)
        vol_str = f"{'+' if vol_pct >= 0 else ''}{vol_pct}%"
        _hz = float(getattr(req, "pitch_hz", 0.0) or 0.0)
        pitch_str = f"{'+' if _hz >= 0 else ''}{int(round(_hz))}Hz"

        out = Path(req.output_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        # ★★ 重试：Edge TTS 是**免费公共服务**，短时间密集请求会被限流，抛
        #   `NoAudioReceived`。实测：一部片子逐句合成时（每句还要先量一次时长，
        #   请求量翻倍），一次合成里能撞上 4 次 —— 而**每次失败都意味着那一句台词
        #   在成片里没有声音**（用户听到的就是"有的角色没说话"）。
        #   所以必须自己重试，不能指望它稳定。
        result = None
        last_err = ""
        for _attempt in range(3):
            try:
                communicate = edge_tts.Communicate(
                    text=req.text, voice=voice_short_name,
                    rate=rate_str, volume=vol_str, pitch=pitch_str)
                subtitles: List[dict] = []
                got = 0
                # 流式写入 + 收集字幕
                #
                # ⚠ edge-tts 版本差异（实测 7.2.8）：事件类型是
                #   SentenceBoundary / WordBoundary（旧代码只判 WordBoundary），
                #   字段在 chunk 顶层而不在 chunk["data"] 里 —— 两种形态都兼容，
                #   并优先整句（更适合烧进画面的字幕）。
                with open(out, "wb") as audio_file:
                    async for chunk in communicate.stream():
                        ctype = chunk.get("type")
                        if ctype == "audio":
                            data = chunk.get("data") or b""
                            audio_file.write(data)
                            got += len(data)
                            continue
                        if ctype not in ("SentenceBoundary", "WordBoundary"):
                            continue
                        d = chunk.get("data") if isinstance(chunk.get("data"), dict) else chunk
                        offset = d.get("offset", 0) or 0
                        dur = d.get("duration", 0) or 0
                        txt = (d.get("text") or "").strip()
                        if not txt:
                            continue
                        subtitles.append({
                            "start": round(offset / 1e7, 3),
                            "end": round((offset + dur) / 1e7, 3),
                            "text": txt,
                            "kind": "sentence" if ctype == "SentenceBoundary" else "word",
                        })
                # ★ 服务返回"成功"但没有音频，是真实发生过的失败形态
                #   （`NoAudioReceived`），必须当成失败去重试。
                if got <= 512:
                    raise RuntimeError("服务没有返回音频（NoAudioReceived）")
                sentences = [s for s in subtitles if s["kind"] == "sentence"]
                if not sentences and subtitles:
                    sentences = self._merge_words_to_sentences(subtitles)
                final_subs = sentences or subtitles
                duration = final_subs[-1]["end"] if final_subs else None
                if not duration:
                    duration = self._probe_duration(str(out))
                result = TTSResult(
                    success=True,
                    audio_path=str(out),
                    duration_seconds=duration,
                    subtitles=final_subs if final_subs else None,
                    provider=self.name,
                    voice_id=req.voice_id,
                    raw={"voice": voice_short_name, "rate": rate_str,
                         "volume": vol_str, "pitch": pitch_str},
                )
                break
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                logger.warning("Edge TTS 第 %d 次失败（%s），重试中", _attempt + 1, last_err)
                await asyncio.sleep(0.8 * (_attempt + 1))
        if result is None:
            return TTSResult(
                success=False, provider=self.name, voice_id=req.voice_id,
                error=f"Edge TTS 合成失败（已重试 3 次）：{last_err}",
            )
        return result

    @staticmethod
    def _merge_words_to_sentences(words: List[dict], gap: float = 0.45, max_len: int = 22) -> List[dict]:
        """把 word 级时间轴合并成句子级字幕（遇到较长停顿或过长则断句）"""
        out: List[dict] = []
        buf: List[dict] = []
        for w in words:
            if buf and (w["start"] - buf[-1]["end"] > gap or len("".join(x["text"] for x in buf)) >= max_len):
                out.append({"start": buf[0]["start"], "end": buf[-1]["end"],
                            "text": "".join(x["text"] for x in buf), "kind": "sentence"})
                buf = []
            buf.append(w)
        if buf:
            out.append({"start": buf[0]["start"], "end": buf[-1]["end"],
                        "text": "".join(x["text"] for x in buf), "kind": "sentence"})
        return out

    @staticmethod
    def _probe_duration(audio_path: str) -> float:
        """用 ffprobe 兜底取音频真实时长（拿不到就返回 0）"""
        try:
            import subprocess
            from core.ffmpeg_manager import get_ffmpeg_for_postprocess
            ff = get_ffmpeg_for_postprocess()
            probe = str(ff).replace("ffmpeg.exe", "ffprobe.exe").replace("ffmpeg", "ffprobe")
            r = subprocess.run(
                [probe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", audio_path],
                capture_output=True, text=True, timeout=20)
            return round(float(r.stdout.strip()), 3)
        except Exception:
            return 0.0

    @staticmethod
    def _estimate_duration_from_subtitles(subtitles: List[dict]) -> float:
        """从字幕最后一帧估算音频时长"""
        if not subtitles:
            return 0.0
        return subtitles[-1].get("end", 0.0)
