# -*- coding: utf-8 -*-
"""
VideoForge · 成片渲染器（compose�?
══════════════════════════════════════════════════════════════════�?为什么需要这个模块（这是本项目此�?跑不�?的根治点�?══════════════════════════════════════════════════════════════════�?VideoForge 原有 11 个视频生成模型适配器（可灵/万相/海螺/Sora/…）�?**全部需要付�?API Key**。用户一�?Key 都没�?�?「生成视频」永远失�?�?整条链路在最后一步断掉，做不出任何成品�?
MoneyPrinterTurbo 之所�?开箱即�?，关键不在它的界面，而在它的**素材策略**�?它不调用视频生成大模型，而是用「图�?stock 素材 + TTS 配音 + 字幕 + FFmpeg 拼接�?合成视频。这条路**零付�?Key 就能出片**�?
本模块照此思路实现 VideoForge 的自有成片路径：

    分镜文本 ─┬─ Edge TTS ──�?语音 + 句级字幕时间�?              └─ 画面来源 ──�?场景�?/ 角色�?/ 自动生成的文字卡
                              �?                    FFmpeg 图片运镜(Ken Burns) + 配音 �?分段视频
                              �?                        拼接 �?烧字�?�?�?BGM �?final.mp4

设计原则�?- **绝不因为缺素材而失�?*：没场景图就用角色图，没角色图就自动生成文字卡�?- **绝不因为没配音而失�?*：可�?silent provider 出静音片段�?- **每一步都留退�?*：Ken Burns 失败退化为静态图；烧字幕失败则保�?.srt 旁挂文件�?"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("videoforge.compose")

FPS = 30
DEFAULT_W, DEFAULT_H = 1280, 720
MIN_SHOT_SECONDS = 1.5
MAX_NARRATION_CHARS = 220

_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\arial.ttf",
]

ProgressFn = Optional[Callable[[str, float, str], None]]


# ══════════════════════════════════════════════════════════════�?# 结果结构
# ══════════════════════════════════════════════════════════════�?
@dataclass
class ClipInfo:
    index: int
    narration: str = ""
    image: str = ""
    image_source: str = ""          # scene / character / text_card
    audio: str = ""
    duration: float = 0.0
    subtitle_count: int = 0
    trim_head: float = 0.0           # 起幅死帧裁掉了多少秒（配音轨与字幕要同步平移�?    trim_tail: float = 0.0           # 收尾定住裁掉了多少秒（只需缩短，无平移�?    stall_cut: float = 0.0           # 中间冻结段剪掉了多少秒（画面会出现一次跳切）
    audio_precut: bool = False       # 配音轨已随画面同量剪过（摆位时不要再 skip�?    removals: List[Any] = field(default_factory=list)   # 实际剪掉的区间（原始本镜时间�?    warnings: List[str] = field(default_factory=list)


@dataclass
class ComposeResult:
    ok: bool = False
    output_path: str = ""
    duration: float = 0.0
    srt_path: str = ""
    clip_count: int = 0
    voice_id: str = ""
    with_voice: bool = False
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    clips: List[dict] = field(default_factory=list)
    # 成片级连贯度指标（全片单帧最大跳�?/ 自身 p99 / 比值）�?    # �?必须显式声明 + �?to_dict：不然接口里看不到，用户只能拿到一个视频文�?    #   自己�?这次到底顺不�?。目标要�?可量化的接缝指标"，这一步是把它交付出去�?    film_metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "output_path": self.output_path,
            "duration": round(self.duration, 2),
            "srt_path": self.srt_path,
            "clip_count": self.clip_count,
            "voice_id": self.voice_id,
            "with_voice": self.with_voice,
            "warnings": self.warnings,
            "errors": self.errors,
            "clips": self.clips,
            "film_metrics": self.film_metrics or {},
        }


# ══════════════════════════════════════════════════════════════�?# 工具
# ══════════════════════════════════════════════════════════════�?
def _ffmpeg() -> str:
    from core.ffmpeg_manager import get_ffmpeg_for_postprocess
    return get_ffmpeg_for_postprocess()


def _ffprobe() -> str:
    """�?ffprobe；找不到返回空串�?
    �?实测：imageio-ffmpeg �?Windows �?**只含 ffmpeg.exe，不�?ffprobe.exe**�?    而系�?PATH 里也通常没有 ffprobe。所以所有依�?ffprobe 的地方都必须有退路，
    �?probe_duration() �?`ffmpeg -i` 解析方案�?    """
    ff = _ffmpeg()
    d, base = os.path.split(ff)
    for cand in ("ffprobe.exe", "ffprobe"):
        p = os.path.join(d, cand)
        if os.path.exists(p):
            return p
    found = shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    return found or ""


async def _run(cmd: List[str], timeout: int = 900) -> Tuple[int, str]:
    """跑外部命令（统一�?core.proc：不闪窗 + 干净环境，避�?0xc0000142）�?
    注意：stderr �?FFmpeg �?正常输出渠道"（`-i` 探测、进度都打在这里），
    所以这里把 stdout/stderr 一并返回，而不是丢掉�?    """
    from core.proc import run as _proc_run
    return await _proc_run(cmd, timeout=timeout)


_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


async def _probe_duration_via_ffmpeg(path: str) -> float:
    """�?`ffmpeg -i` 的输出解析时长（不需�?ffprobe�?""
    rc, err = await _run([_ffmpeg(), "-hide_banner", "-i", path], timeout=60)
    m = _DUR_RE.search(err or "")
    if not m:
        return 0.0
    h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return round(h * 3600 + mi * 60 + s, 3)


async def probe_duration(path: str) -> float:
    """取媒体时长（秒）：优�?ffprobe，没有则退�?`ffmpeg -i` 解析。任何异常都返回 0"""
    if not path or not os.path.exists(path):
        return 0.0
    probe = _ffprobe()
    if probe:
        try:
            rc, out = await _run([
                probe, "-v", "error", "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", path,
            ], timeout=60)
            for line in reversed((out or "").strip().splitlines()):
                try:
                    v = float(line.strip())
                    if v > 0:
                        return round(v, 3)
                except ValueError:
                    continue
        except Exception:
            pass
    try:
        return await _probe_duration_via_ffmpeg(path)
    except Exception:
        logger.warning("probe_duration failed for %s", path, exc_info=True)
        return 0.0


def _load_font(size: int):
    from PIL import ImageFont
    for p in _FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def make_text_card(out_path: str, title: str, body: str = "",
                   w: int = DEFAULT_W, h: int = DEFAULT_H,
                   badge: str = "") -> str:
    """生成一张渐变文字卡（当没有任何可用画面时的兜底，保证一定能出片�?""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h), (18, 20, 32))
    d = ImageDraw.Draw(img)
    # 竖向渐变
    for y in range(h):
        t = y / max(1, h - 1)
        d.line([(0, y), (w, y)],
               fill=(int(18 + 26 * t), int(20 + 30 * t), int(38 + 62 * t)))
    # 左侧强调�?    d.rectangle([0, 0, max(6, w // 110), h], fill=(88, 166, 255))

    title = (title or "分镜").strip()
    body = (body or "").strip()

    def wrap(text: str, font, max_w: int) -> List[str]:
        lines, cur = [], ""
        for ch in text:
            if d.textlength(cur + ch, font=font) <= max_w:
                cur += ch
            else:
                lines.append(cur)
                cur = ch
        if cur:
            lines.append(cur)
        return lines

    ft = _load_font(max(30, w // 26))
    fb = _load_font(max(20, w // 46))
    margin = int(w * 0.09)
    maxw = w - margin * 2

    tl = wrap(title, ft, maxw)[:2]
    bl = wrap(body, fb, maxw)[:5] if body else []

    th = sum(int(ft.size * 1.35) for _ in tl)
    bh = sum(int(fb.size * 1.55) for _ in bl)
    total = th + (int(fb.size * 0.9) if bl else 0) + bh
    y = max(margin, (h - total) // 2)

    for ln in tl:
        d.text((margin, y), ln, font=ft, fill=(240, 244, 255))
        y += int(ft.size * 1.35)
    if bl:
        y += int(fb.size * 0.9)
        for ln in bl:
            d.text((margin, y), ln, font=fb, fill=(170, 182, 205))
            y += int(fb.size * 1.55)
    if badge:
        fbadge = _load_font(max(16, w // 62))
        bw = d.textlength(badge, font=fbadge)
        d.rectangle([margin, h - margin - 40, margin + bw + 26, h - margin], fill=(88, 166, 255))
        d.text((margin + 13, h - margin - 34), badge, font=fbadge, fill=(10, 14, 24))

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    img.save(out_path, "PNG")
    return out_path


def _as_text(v: Any) -> str:
    """把任意字段安全转成文本�?
    �?实测：shots.layer3_constraints �?**JSON 字典**（如
    {'must_not_appear': [...], 'must_keep': [...]}），
    旧代码直�?`v[:120]` 会抛 `KeyError: slice(None, 120, None)` 把整条合成链路炸掉�?    这里统一做类型归一�?    """
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        pref = ("must_keep", "style", "description", "summary", "mood", "note", "action")
        parts = [_as_text(v[k]) for k in pref if v.get(k)]
        if not parts:
            parts = [_as_text(x) for x in v.values()]
        return " ".join(p for p in parts if p)
    if isinstance(v, (list, tuple, set)):
        return " ".join(p for p in (_as_text(x) for x in v) if p)
    return str(v)


def _clean_narration(text: Any) -> str:
    """清掉分镜文本里的时间码标记等杂质，让配音读起来像旁白"""
    if not text:
        return ""
    t = _as_text(text)
    t = re.sub(r"\[\s*\d+(\.\d+)?\s*[-–~]\s*\d+(\.\d+)?\s*s?\s*\]", "", t)   # [0-3s]
    t = re.sub(r"^\s*[-•·]\s*", "", t, flags=re.M)
    t = re.sub(r"[*#`>]+", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def narration_for_shot(shot: Dict[str, Any], index: int = 0) -> str:
    """从分镜里提炼配音文本：优先对�?�?分镜概述 �?动作描述 �?兜底"""
    timeline = shot.get("layer2_timeline")
    if isinstance(timeline, str):
        try:
            timeline = json.loads(timeline)
        except Exception:
            timeline = []
    dialogues: List[str] = []
    actions: List[str] = []
    if isinstance(timeline, list):
        for it in timeline:
            if isinstance(it, dict):
                for k in ("dialogue", "line", "speech", "voiceover", "narration", "subtitle"):
                    v = it.get(k)
                    if not v:
                        continue
                    # �?台词�?**{character,text,emotion} 结构�?*，不是字符串�?                    #   旧代�?`str(v).strip()` 会把它变�?                    #   `{'character': '珊莎', 'text': '...'}` 这种字面�?—�?                    #   实测成片里的"旁白"就真的去念这个字典，字幕也是这么显示的�?                    if isinstance(v, dict):
                        _t = str(v.get("text") or v.get("line") or "").strip()
                        _c = str(v.get("character") or "").strip()
                        if _t:
                            dialogues.append(f"{_c}：{_t}" if _c else _t)
                        break
                    if isinstance(v, (list, tuple)):
                        _s = _as_text(v).strip()
                        if _s:
                            dialogues.append(_s)
                        break
                    _s = str(v).strip()
                    if _s:
                        dialogues.append(_s)
                    break
                a = it.get("action") or it.get("description") or it.get("visual")
                if a and str(a).strip():
                    actions.append(str(a).strip())
            elif isinstance(it, str):
                actions.append(it)

    text = ""
    if dialogues:
        text = " ".join(dialogues)
    if not text:
        text = _clean_narration(shot.get("layer1_overview") or "")
    if not text and actions:
        text = _clean_narration(" ".join(actions))
    if not text:
        text = _clean_narration(shot.get("layer3_constraints") or "")
    text = _clean_narration(text)
    if len(text) > MAX_NARRATION_CHARS:
        cut = text[:MAX_NARRATION_CHARS]
        for sep in ("�?, "�?, "�?, "�?, ".", "!", "?"):
            pos = cut.rfind(sep)
            if pos > MAX_NARRATION_CHARS * 0.6:
                cut = cut[:pos + 1]
                break
        text = cut
    return text or ""


def _pick_image(shot: Dict[str, Any], scenes: Dict[str, dict],
                chars: Dict[str, dict]) -> Tuple[str, str]:
    """挑一�?*与该分镜直接相关**的画面：分镜自带 �?场景�?�?角色图�?
    刻意不做"随便拿项目里任意一张图"的兜底：
    实测那样会让 5 个分镜全用同一张角色图（同一画面重复 5 次）�?    观感比按分镜生成的文字卡更差。没有直接相关画面时交给
    调用方去「自动生成画面」或「生成文字卡」�?    """
    for k in ("reference_image_path", "image_path", "first_frame_path"):
        p = shot.get(k)
        if p and os.path.exists(str(p)):
            return str(p), "shot"

    sid = shot.get("scene_id")
    if sid and sid in scenes:
        p = scenes[sid].get("reference_image_path")
        if p and os.path.exists(str(p)):
            return str(p), "scene"

    cids = shot.get("character_ids")
    if isinstance(cids, str):
        try:
            cids = json.loads(cids)
        except Exception:
            cids = []
    if isinstance(cids, list):
        for cid in cids:
            c = chars.get(cid)
            if c and c.get("reference_image_path") and os.path.exists(str(c["reference_image_path"])):
                return str(c["reference_image_path"]), "character"
    return "", ""


def image_prompt_for_shot(shot: Dict[str, Any], scenes: Dict[str, dict],
                          chars: Dict[str, dict]) -> str:
    """根据分镜内容造一条图像生成提示词（用于「按分镜自动生成画面」）"""
    bits: List[str] = []
    sid = shot.get("scene_id")
    if sid and sid in scenes:
        sc = scenes[sid]
        if sc.get("name"):
            bits.append(f"场景：{sc['name']}")
        if sc.get("description"):
            bits.append(str(sc["description"]))
    ov = _clean_narration(shot.get("layer1_overview") or "")
    if ov:
        bits.append(ov[:120])
    l3 = _clean_narration(shot.get("layer3_constraints") or "")
    if l3:
        bits.append(l3[:160])
    cids = shot.get("character_ids")
    if isinstance(cids, str):
        try:
            cids = json.loads(cids)
        except Exception:
            cids = []
    if isinstance(cids, list):
        names = [chars[c]["name"] for c in cids if c in chars and chars[c].get("name")]
        if names:
            bits.append("出场人物�? + "�?.join(names[:3]))
    body = "�?.join(b for b in bits if b)
    if not body:
        body = "电影感场�?
    return (
        f"{body}。电影级构图，写实风格，光影层次丰富，高清细节，"
        f"无文字、无水印、无字幕，画面干净"
    )


async def _auto_generate_image(shot: Dict[str, Any], scenes: Dict[str, dict],
                               chars: Dict[str, dict], out_path: str,
                               provider: str, size: str, settings: dict) -> Tuple[str, str]:
    """调用图像模型按分镜生成一张画面。返�?(路径, 错误信息)"""
    try:
        from core.imagegen import generate_image
        prompt = image_prompt_for_shot(shot, scenes, chars)
        api_key = ""
        try:
            from core.voice.base import PROVIDERS  # noqa: F401  仅为确认 voice 模块已加�?        except Exception:
            pass
        # 复用主程序的大小写不敏感 Key 查找逻辑
        keys = {}
        for k in ("image_api_keys", "llm_api_keys", "api_keys"):
            v = settings.get(k)
            if isinstance(v, dict):
                keys.update(v)
        for k, v in keys.items():
            if str(k).strip().lower() == provider.lower() and v:
                api_key = str(v)
                break
        if not api_key:
            return "", f"未配置「{provider}」的图像 API Key"
        r = await generate_image(
            provider=provider, api_key=api_key, prompt=prompt,
            out_dir=os.path.dirname(out_path), size=size, model="",
        )
        if r.get("ok") and r.get("path") and os.path.exists(r["path"]):
            return str(r["path"]), ""
        return "", str(r.get("error") or "图像生成失败")[:200]
    except Exception as e:
        return "", f"{type(e).__name__}: {e}"[:200]


async def _fit_audio(src: str, dst: str, target: float) -> float:
    """把配音塞进目标时长：超长就变速（atempo 0.5~2.0）再截断，偏短就补静音�?
    返回实际得到的目标时长。这�?用户想要 5 秒就 5 �?的关键：
    配音文本长度不可控，但成片段长必须可控�?    """
    cur = await probe_duration(src)
    target = max(1.0, float(target))
    if cur <= 0:
        return target
    filters = []
    if cur > target + 0.05:
        tempo = max(0.5, min(2.0, cur / target))
        filters.append(f"atempo={tempo:.4f}")
    filters.append("apad")
    cmd = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", src,
           "-filter:a", ",".join(filters), "-t", f"{target:.3f}",
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", dst]
    rc, err = await _run(cmd, timeout=300)
    if rc != 0 or not os.path.exists(dst):
        logger.warning("fit_audio failed: %s", err[-200:])
        return cur
    return target


async def render_shot_clip(
    shot: Dict[str, Any],
    *,
    db=None,
    settings: Optional[dict] = None,
    voice_id: str = "edge:zh-CN-XiaoxiaoNeural",
    with_voice: bool = True,
    target_seconds: Optional[float] = None,
    auto_images: bool = False,
    image_provider: str = "minimax",
    image_size: str = "1280x720",
    out_path: str = "",
    ken_burns: bool = True,
    idx: int = 0,
    scenes: Optional[Dict[str, dict]] = None,
    chars: Optional[Dict[str, dict]] = None,
    progress: ProgressFn = None,
) -> Dict[str, Any]:
    """�?*单个分镜**渲染成一个可播放 mp4（不需要付费视频模�?Key）�?
    这是「生成」页那个按钮的真正实现：借鉴 MPT 的思路 —�?    用「画面（现成/AI 生成/文字卡）+ 配音 + 运镜 + 字幕」合成，
    而不是去调文生视频大模型�?    """
    settings = settings or {}
    def step(pct, msg):
        if progress:
            try:
                progress("shot", pct, msg)
            except Exception:
                pass

    warnings: List[str] = []
    errors: List[str] = []

    if scenes is None:
        scenes = {}
        if db is not None and shot.get("project_id"):
            scenes = {s["id"]: s for s in (db.list_scenes(shot["project_id"]) or [])}
    if chars is None:
        chars = {}
        if db is not None and shot.get("project_id"):
            chars = {c["id"]: c for c in (db.list_characters(shot["project_id"]) or [])}

    if not out_path:
        root = settings.get("cache_dir") or os.path.join(
            os.environ.get("VIDEOFORGE_DATA_DIR", os.getcwd()), "cache")
        out_path = os.path.join(root, "shots", shot["id"], "shot.mp4")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    w, h = _res_from_shot(shot, settings)

    # 1) 画面
    step(0.05, "准备画面�?)
    img, src = _pick_image(shot, scenes, chars)
    if not img and auto_images:
        step(0.15, "AI 生成画面�?)
        gen = os.path.join(os.path.dirname(out_path), "auto.png")
        img, gerr = await _auto_generate_image(shot, scenes, chars, gen,
                                               image_provider, image_size, settings)
        if img:
            src = "ai_generated"
        elif gerr:
            warnings.append(f"自动生成画面失败，改用文字卡：{gerr}")
    narration = narration_for_shot(shot, idx)
    if not img:
        img = os.path.join(os.path.dirname(out_path), "card.png")
        make_text_card(img, title=narration or f"分镜 {idx + 1}",
                       body=_clean_narration(shot.get("layer1_overview") or "")[:110],
                       w=w, h=h, badge=f"分镜 {idx + 1}")
        src = "text_card"
        warnings.append("该分镜无现成画面，已生成文字�?)

    # 2) 配音
    audio = ""
    subs: List[dict] = []
    natural = float(shot.get("duration_seconds") or 5.0)
    if with_voice and narration:
        step(0.30, "合成配音�?)
        try:
            from core.voice.base import TTSRequest
            from core.voice import dispatcher as voice_dispatcher
            raw = os.path.join(os.path.dirname(out_path), "voice.mp3")
            r = await voice_dispatcher.synthesize(
                TTSRequest(text=narration, voice_id=voice_id, output_path=raw), settings)
            if r.success and r.audio_path and os.path.exists(r.audio_path):
                audio = r.audio_path
                subs = list(r.subtitles or [])
                natural = float(r.duration_seconds or 0) or await probe_duration(audio) or natural
            else:
                warnings.append(f"配音失败，改用静音：{r.error}")
        except Exception as e:
            warnings.append(f"配音异常，改用静音：{e}")
    elif with_voice:
        warnings.append("该分镜没有可配音文本，使用静�?)

    # 3) 时长：用户指定优先（最�?5 秒）
    MIN_SEC = 5.0
    final_seconds = natural
    if target_seconds:
        final_seconds = max(MIN_SEC, float(target_seconds))
    final_seconds = max(MIN_SEC, round(final_seconds, 2))
    if audio and abs(final_seconds - natural) > 0.25:
        step(0.45, f"对齐时长�?{final_seconds:.0f} 秒�?)
        fitted = os.path.join(os.path.dirname(out_path), "voice_fit.m4a")
        await _fit_audio(audio, fitted, final_seconds)
        if os.path.exists(fitted):
            audio = fitted
    # 字幕若超出片段长度，按比例压缩到片段�?    if subs and final_seconds > 0 and subs[-1]["end"] > final_seconds + 0.1:
        scale = final_seconds / subs[-1]["end"]
        subs = [{"start": round(s["start"] * scale, 3),
                 "end": round(min(s["end"] * scale, final_seconds), 3),
                 "text": s["text"]} for s in subs]

    # 4) 渲染片段（含烧字幕，单片也烧，保证观感一致）
    step(0.60, "渲染视频�?)
    srt = ""
    if subs:
        srt = write_srt(subs, os.path.join(os.path.dirname(out_path), "shot.srt"))
    try:
        warnings.extend(await render_clip(img, audio, final_seconds, out_path, w, h, FPS, ken_burns))
    except Exception as e:
        errors.append(f"渲染失败：{e}")
        return {"ok": False, "path": "", "duration": 0.0, "warnings": warnings,
                "errors": errors, "narration": narration, "image_source": src,
                "subtitles": subs, "srt_path": srt}

    if srt:
        burned = os.path.join(os.path.dirname(out_path), "shot_burned.mp4")
        okb, note = await _burn(out_path, srt, burned, w, h)
        if okb:
            try:
                shutil.move(burned, out_path)
            except Exception:
                pass
        else:
            warnings.append(f"字幕未烧入（已保�?srt）：{note}")

    dur = await probe_duration(out_path) or final_seconds
    step(1.0, f"完成（{dur:.1f}s�?)
    return {"ok": True, "path": out_path, "duration": dur, "warnings": warnings,
            "errors": errors, "narration": narration, "image_source": src,
            "subtitles": subs, "srt_path": srt, "width": w, "height": h,
            "size_bytes": os.path.getsize(out_path) if os.path.exists(out_path) else 0}


def _res_from_shot(shot: Dict[str, Any], settings: dict) -> Tuple[int, int]:
    """按分镜的 aspect_ratio（或项目默认）决定输出分辨率"""
    ar = shot.get("aspect_ratio") or settings.get("default_aspect_ratio") or "16:9"
    return {"16:9": (1280, 720), "9:16": (720, 1280), "1:1": (1024, 1024)}.get(ar, (1280, 720))


# ══════════════════════════════════════════════════════════════�?# 片段渲染
# ══════════════════════════════════════════════════════════════�?
async def probe_video_duration(path: str) -> float:
    """�?*视频�?*的真实长度（不是容器时长）�?
    ★★ 为什么必须有这个函数（线上真实事故）�?      `render_clip` 曾经�?*视频**�?*静图**喂进去（`-loop 1 -framerate 30 -i x.mp4`），
      `-loop 1` �?mp4 并不像对 PNG 那样无限循环，于是视频轨停在源片长度�?.64s），
      �?`anullsrc` 出来的静音音轨被 `-t 10` 拉满 10 �?�?      **容器时长 = 10.00s，视频流真实长度 = 5.64s**�?      �?`probe_duration()` 读的是容器时长，所�?想渲 10s / 实测 10s"的检�?      **完全抓不�?*。等到拼接阶段丢掉音轨（`-an`）重编码，视频就"�?�?5.64s —�?      6 段声�?60 秒的成片只剩 39.93 秒，字幕时间轴还�?60 秒排�?
    ffmpeg 自带�?`-i` 只报容器时长，所以这�?*解码�?null** 再读最后一�?`time=`�?        ffmpeg -i x.mp4 -map 0:v:0 -f null -
    这是唯一能拿�?视频轨到底有多长"的可靠办法�?    """
    rc, txt = await _run([_ffmpeg(), "-hide_banner", "-i", path,
                          "-map", "0:v:0", "-f", "null", "-"], timeout=900)
    times = re.findall(r"time=(\d+):(\d+):([\d.]+)", txt or "")
    if not times:
        return 0.0
    h, m, s = times[-1]
    return round(int(h) * 3600 + int(m) * 60 + float(s), 3)


def _is_video_input(path: str) -> bool:
    """这个"素材"到底是视频还是静�?—�?两者的 ffmpeg 喂法**完全不同**�?
    搞错就会出现上面那个"容器 10 秒、视频轨 5.6 �?的事故�?    """
    try:
        from core.videoref import is_video as _iv
        return bool(_iv(path))
    except Exception:
        return str(path).lower().endswith(
            (".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"))


async def render_clip(image: str, audio: str, duration: float, out_path: str,
                      w: int, h: int, fps: int = FPS, ken_burns: bool = True) -> List[str]:
    """把「素材（静图 **�?* 视频片段�? 一段音频」渲染成一个标准化的视频片段�?
    返回 warning 列表（空表示一切顺利）�?
    �?静图与视频必须分开处理（这是踩过的坑）�?      · **静图**：`-loop 1 -framerate fps -i img` 无限供帧，可�?Ken Burns 推拉
      · **视频**：`-stream_loop -1 -i clip` 让源�?*循环**着填满目标时长�?        绝不能用 `-loop 1`（对 mp4 不起作用 �?视频轨停在源片长度，
        容器时长却被静音音轨撑满 �?"假的 10 �?）；也不该套 Ken Burns
        （视频本身已经有运动，再叠推拉是双重运动，看着晕）�?        当源�?*短于**目标时长时，循环填充�?留一段黑"�?截短"都好�?        用户要的是这一�?10 秒，宁可素材循环也不要时间轴塌掉�?    """
    warnings: List[str] = []
    duration = max(MIN_SHOT_SECONDS, round(float(duration or 0), 2))
    frames = max(1, int(round(duration * fps)))

    is_vid = bool(image) and os.path.exists(image) and _is_video_input(image)

    # 输入
    if is_vid:
        src_vdur = await probe_video_duration(image)
        if src_vdur > 0.05 and src_vdur + 0.35 < duration:
            warnings.append(
                f"素材只有 {src_vdur:.1f}s，要填满 {duration:.1f}s —�?"
                f"已循环播放补足（不会留黑，也不会把时间轴缩短�?)
        elif src_vdur > duration + 0.35:
            warnings.append(
                f"素材�?{src_vdur:.1f}s，只取前 {duration:.1f}s")
        v_in = ["-stream_loop", "-1", "-i", image]
    elif image and os.path.exists(image):
        v_in = ["-loop", "1", "-framerate", str(fps), "-i", image]
    else:
        v_in = ["-f", "lavfi", "-i", f"color=c=0x121420:s={w}x{h}:r={fps}"]
        warnings.append("无可用画面，使用纯色�?)

    has_audio = bool(audio) and os.path.exists(str(audio))
    if has_audio:
        a_in = ["-i", str(audio)]
    else:
        # 无配音也要有音轨，否�?concat 时各段时间轴不齐
        a_in = ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]

    base = [v_in, a_in]
    base = [x for sub in base for x in sub]

    scale_pad = (
        f"scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},setsar=1"
    )
    kb = (
        f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase,"
        f"crop={w * 2}:{h * 2},"
        f"zoompan=z='min(1+0.0016*on,1.22)'"
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d={frames}:s={w}x{h}:fps={fps},"
        f"setsar=1,format=yuv420p"
    )
    static = f"{scale_pad},format=yuv420p"

    enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
           "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.0",
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
           "-movflags", "+faststart", "-t", f"{duration}"]

    attempts = []
    if is_vid:
        # 视频输入：只�?scale_pad。Ken Burns（zoompan）是给静图用的，
        # 套在视频上会变成"每帧再推一�?，画面怪而且帧数会爆炸�?        attempts.append(("video", static))
    else:
        if ken_burns and image and os.path.exists(image):
            attempts.append(("ken_burns", kb))
        attempts.append(("static", static))

    last_err = ""
    for tag, vf in attempts:
        cmd = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error"] + base + \
              ["-vf", vf, "-t", f"{duration}"] + enc + [out_path]
        rc, err = await _run(cmd, timeout=600)
        if rc == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
            if tag == "static" and ken_burns and not is_vid:
                warnings.append("运镜失败，已退化为静态画�?)
            # �?渲完立刻�?*视频�?*真实长度，容器时长不算数（见 probe_video_duration�?            vd = await probe_video_duration(out_path)
            if vd > 0.05 and vd + 0.5 < duration:
                warnings.append(
                    f"�?片段视频轨只�?{vd:.1f}s，目标是 {duration:.1f}s —�?"
                    f"时间轴已按实际长度排，避免字幕压到不存在的画面上")
            return warnings
        last_err = err
        logger.warning("clip render [%s] failed rc=%s: %s", tag, rc, err[-400:])

    raise RuntimeError(f"分镜片段渲染失败：{last_err[-400:]}")


def _fmt_srt_time(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _split_subtitle_text(text: Any, max_chars: int = 18,
                         max_parts: int = 4) -> List[str]:
    """把一段概述切成适合当字幕的短句�?
    两个要点（都是实测出来的）：
    1. 字幕不是文章。一行超�?~18 个汉字就糊成一片，手机上看不清�?       �?合并成更少的�?只会把行拉得更长，所以超量时**保留前几�?*�?       把剩下的丢掉，而不是合并�?    2. 分镜概述里混着大量**提示词残�?*�?时长�?0�?"镜头语言以缓慢推进为�?
       "整体色调偏冷"）—�?这些是给模型看的，不是给观众看的，当字幕很滑稽�?       这里按模式过滤掉�?    """
    t = _clean_narration(text)
    if not t:
        return []
    import re as _re
    # 过滤提示词残留（元信息），它们对观众没有意义
    META = _re.compile(
        r"(时长|时长控制在|整体时长|时长为|镜头语言|摄影风格|色调|画面风格|整体氛围|"
        r"滤镜|分辨率|帧率|景别|运镜|构图|�?|s$)")
    parts = [x.strip() for x in _re.split(r"(?<=[。！�??�?])", t) if x.strip()]
    kept: List[str] = []
    for p in parts:
        if META.search(p) and len(p) < 30:
            continue                      # 整句都是元信�?�?丢掉
        if len(p) <= max_chars:
            kept.append(p)
            continue
        for seg in _re.split(r"(?<=[�?、])", p):
            seg = seg.strip()
            if not seg or (META.search(seg) and len(seg) < 30):
                continue
            while len(seg) > max_chars:
                kept.append(seg[:max_chars])
                seg = seg[max_chars:]
            if seg:
                kept.append(seg)
    if not kept:
        # 全被过滤掉了（整段都是元信息）→ 退回不过滤的短�?        kept = [x.strip() for x in parts if x.strip()][:max_parts]
    # 超量就保留前几条（前几句通常是在交代"发生了什�?�?    if len(kept) > max_parts:
        kept = kept[:max_parts]
        if kept:
            kept[-1] = kept[-1].rstrip("�?�?) + "�?
    return [x for x in kept if x]


def write_srt(entries: List[dict], out_path: str) -> str:
    """�?SRT。entries: [{start, end, text}]（已含全局偏移�?""
    lines = []
    for i, e in enumerate(entries, 1):
        text = (e.get("text") or "").strip()
        if not text:
            continue
        lines.append(str(i))
        lines.append(f"{_fmt_srt_time(e['start'])} --> {_fmt_srt_time(e['end'])}")
        lines.append(text)
        lines.append("")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return out_path


def _selected_candidate_path(shot: Dict[str, Any]) -> str:
    """取该分镜**已生成好的视�?*（优先用户选中的那个候选）�?
    同时兼容两种键名：本地候选写 `path`，适配器候选写 `video_path`�?    """
    cands = shot.get("candidates")
    if isinstance(cands, str):
        try:
            cands = json.loads(cands)
        except Exception:
            cands = []
    if not isinstance(cands, list) or not cands:
        return ""
    cands = [c for c in cands if isinstance(c, dict)]
    sel_id = shot.get("selected_candidate_id")
    chosen = next((c for c in cands if c.get("candidate_id") == sel_id), None)
    if not chosen:
        chosen = cands[-1]
    for key in ("path", "video_path", "local_path"):
        p = chosen.get(key)
        if p and os.path.exists(str(p)) and os.path.getsize(str(p)) > 10240:
            return str(p)
    return ""


async def _mux_clip_audio(video: str, audio: str, out: str, duration: float) -> bool:
    """�?*配音�?*替换进片段（生成视频自带的要么没有音轨、要么是静音轨）�?
    ★★ 为什么必须单独有这一步（2026-09-13 真实事故）：
      `_normalize_clip` 只处�?源片自己的音�?/ 没有就补静音"�?      它并不知道我们为这一镜合成了配音 —�?于是 `ci.audio` **算出来了却从来没进过成片**�?      实测成片 `final.mp4` 只有 `Stream #0:0` 视频流，中间产物里的音轨�?      2 kb/s 的静音，最后响度归一化那一步干脆把音轨丢了�?      本地合成路径没这个问�?—�?它的音频是在 `render_clip` 里直接烧进片段的�?      所�?已生成视�?这条路必须补上同样一步�?    """
    if not (video and os.path.exists(video) and audio and os.path.exists(audio)):
        return False
    cmd = [
        _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
        "-i", video, "-i", audio,
        "-filter_complex", "[1:a]apad,aresample=48000,"
                           "aformat=channel_layouts=stereo[a]",
        "-map", "0:v:0", "-map", "[a]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
    ]
    if duration and duration > 0:
        cmd += ["-t", f"{float(duration):.3f}"]
    cmd += [out]
    rc, err = await _run(cmd, timeout=900)
    if rc == 0 and os.path.exists(out) and os.path.getsize(out) > 1024:
        return True
    logger.warning("配音轨并进画面失败：%s", (err or "")[-200:])
    return False


async def _cut_ranges(src: str, removals, out: str, dur: float,
                      *, audio: bool = False) -> bool:
    """�?`removals` 剪掉若干段，把剩下的接起来（视频或音频）�?
    用途只有一个：**剪掉片段中间那段真冻�?*（`probe_stall_mid_windows`）�?    剪中间是**动刀** —�?画面会出现一次跳切，所以只在上层显式开启时才做
    （`cut_stalls=true`），并且默认关闭�?    """
    try:
        from core import seamfix as _sf
        keep = _sf.kept_ranges(float(dur or 0), removals)
    except Exception:
        return False
    if len(keep) < 1 or (len(keep) == 1 and abs(keep[0][0]) < 1e-3
                         and abs(keep[0][1] - float(dur)) < 1e-3):
        return False                       # 没什么可剪的
    parts, labels = [], []
    for i, (a, b) in enumerate(keep):
        if audio:
            parts.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},"
                         f"asetpts=PTS-STARTPTS[a{i}]")
            labels.append(f"[a{i}]")
        else:
            parts.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},"
                         f"setpts=PTS-STARTPTS[v{i}]")
            labels.append(f"[v{i}]")
    kind = "a" if audio else "v"
    filt = (";".join(parts) + ";" + "".join(labels)
            + f"concat=n={len(keep)}:v={0 if audio else 1}:a={1 if audio else 0}[o]")
    cmd = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", src,
           "-filter_complex", filt, "-map", "[o]"]
    cmd += (["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
            if audio else
            ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-pix_fmt", "yuv420p"])
    cmd += ["-movflags", "+faststart", out]
    rc, err = await _run(cmd, timeout=1800)
    if rc == 0 and os.path.exists(out) and os.path.getsize(out) > 1024:
        return True
    logger.warning("剪段失败�?s）：%s", kind, (err or "")[-200:])
    return False


async def _normalize_clip(src: str, dst: str, w: int, h: int, fps: int,
                          duration: float = 0.0, head_trim: float = 0.0) -> List[str]:
    """把一�?*外部生成的视�?*（尺�?帧率/音轨都可能不同）规范化，
    使其能与本地合成的片段无缝拼接�?
    - 画面：等比缩放填充到目标尺寸 + 统一帧率 + 统一像素格式
    - 音频：没有音轨就补静音（否则 concat/xfade 会因流不一致失败）
    - 时长�?*不够就循环补足，多了就截�?*

    ★★ 「不够就循环补足」是这一版才补上的（线上真实事故）：
      视频模型按标�?10 秒的任务，经常只�?5.6~5.9 秒。旧代码只有
      `-t 10`（只�?*截断**，不�?*延长**），于是�?          视频�?= 5.64s（源片长度）
          静音�?= 10.00s（anullsrc �?-t 拉满�?          容器时长 = 10.00s   �?probe_duration 读这个，所�?检查通过"
      拼接阶段一旦丢音轨重编码，视频�?�?�?5.64s —�?      6 段声�?60 秒的成片只剩 **39.93 �?*，而字幕时间轴还按 60 秒排�?      后半段字幕全压在不存在的时间上�?      修法�?`render_clip` 一致：�?`-stream_loop -1` 真循环，
      宁可素材循环也不要时间轴塌掉�?    """
    warns: List[str] = []
    d = max(1.0, float(duration or 0)) if duration else 0
    ht = max(0.0, float(head_trim or 0.0))
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
          f"crop={w}:{h},setsar=1,fps={fps},format=yuv420p")
    # �?起幅死帧：在**缩放之前**裁掉，再�?PTS 拉回 0（否则会留一段黑）�?    #   旋到 vf 的最前面是有意的：先 trim 省掉后面的缩放计算�?    if ht > 0.02:
        vf = f"trim=start={ht:.3f},setpts=PTS-STARTPTS," + vf

    # 先量**视频�?*真实长度（容器时长会被静音轨撑长，不能用来判断）
    src_vdur = 0.0
    try:
        src_vdur = float(await probe_video_duration(src) or 0.0)
    except Exception:
        src_vdur = 0.0
    # 裁掉�?ht 秒之后，真正可用的素材长度要减掉�?—�?否则�?以为够长、其实不�?
    avail = max(0.0, src_vdur - ht) if src_vdur > 0.05 else src_vdur
    need_loop = bool(d and avail > 0.05 and avail + 0.35 < d)

    cmd = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error"]
    if need_loop:
        cmd += ["-stream_loop", "-1"]
    cmd += ["-i", src]
    # 没有音轨的输入，�?anullsrc 补齐
    rc_probe, info = await _run([_ffmpeg(), "-hide_banner", "-i", src], timeout=60)
    has_audio = "Audio:" in (info or "")
    if not has_audio:
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
        warns.append("该生成视频没有音轨，已补静音轨以保持时间轴对�?)
    cmd += ["-vf", vf, "-map", "0:v:0", "-map", ("0:a:0" if has_audio else "1:a:0"),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
    if d:
        cmd += ["-t", f"{d:.3f}"]
    cmd += ["-movflags", "+faststart", dst]
    rc, err = await _run(cmd, timeout=900)
    if rc != 0 or not os.path.exists(dst) or os.path.getsize(dst) < 10240:
        raise RuntimeError(f"规范化失败：{(err or '')[-300:]}")

    if need_loop:
        warns.append(
            f"生成视频只有 {src_vdur:.1f}s，要填满 {d:.1f}s —�?已循环播放补�?
            f"（不留黑、也不把时间轴缩短）")
    elif d and src_vdur > d + 0.35:
        warns.append(f"生成视频�?{src_vdur:.1f}s，按 {d:.1f}s 截断")

    # 渲完核一�?*视频�?*：容器时长会被静音轨撑长，只有解码到底才看得到真�?    out_vdur = 0.0
    try:
        out_vdur = float(await probe_video_duration(dst) or 0.0)
    except Exception:
        pass
    if d and out_vdur > 0.05 and out_vdur + 0.5 < d:
        warns.append(
            f"�?规范化后视频轨仍只有 {out_vdur:.1f}s（目�?{d:.1f}s）—�?"
            f"时间轴会按实际长度排，避免字幕压到不存在的画面上")
    return warns


async def _concat(clips: List[str], out_path: str, transition: str = "fade",
                  transition_sec: float = 0.5) -> None:
    """拼接分镜片段�?
    `transition`�?      - "none"  硬切（concat demuxer，最快、无损）
      - "fade"  **交叉溶解（xfade + acrossfade）—�?相邻镜头丝滑衔接**，默�?      - "auto"  �?fade，片段过短时自动退化为硬切

    转场是成片观感的关键一环（用户要求"转场衔接做到丝滑"）：
    硬切会让一组镜头像 PPT 翻页，交叉溶解才有连续影像的感觉�?    """
    trans = (transition or "fade").lower()
    if trans not in ("none", "fade", "auto"):
        trans = "fade"

    if trans in ("fade", "auto") and len(clips) >= 2 and transition_sec > 0:
        durs = []
        for c in clips:
            try:
                durs.append(await probe_duration(c))
            except Exception:
                durs.append(0.0)
        _valid = [d for d in durs if d > 0]
        if not _valid or min(_valid) <= transition_sec * 2 + 0.2:
            logger.info("片段过短（最�?%.2fs），转场退化为硬切", min(_valid) if _valid else 0)
            trans = "none"
        else:
            inputs: List[str] = []
            for c in clips:
                inputs += ["-i", c]
            parts = []
            v_prev, a_prev = "[0:v]", "[0:a]"
            offset = 0.0
            for i in range(1, len(clips)):
                offset += max(0.1, (durs[i - 1] or 0) - transition_sec)
                v_out, a_out = f"[vx{i}]", f"[ax{i}]"
                parts.append(f"{v_prev}[{i}:v]xfade=transition=fade:"
                             f"duration={transition_sec}:offset={offset:.3f}{v_out}")
                parts.append(f"{a_prev}[{i}:a]acrossfade=d={transition_sec}:"
                             f"c1=tri:c2=tri{a_out}")
                v_prev, a_prev = v_out, a_out
            cmd = ([_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error"] + inputs +
                   ["-filter_complex", ";".join(parts),
                    "-map", v_prev, "-map", a_prev,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart", out_path])
            rc, err = await _run(cmd, timeout=1800)
            if rc == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
                logger.info("已用 xfade 交叉溶解拼接 %d 个片段（%.1fs 转场�?,
                            len(clips), transition_sec)
                return
            logger.warning("xfade 转场失败，回退硬切�?s", (err or "")[-300:])

    listfile = out_path + ".concat.txt"
    with open(listfile, "w", encoding="utf-8") as f:
        for c in clips:
            f.write("file '%s'\n" % str(c).replace("\\", "/").replace("'", "'\\''"))
    rc, err = await _run([
        _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", listfile,
        "-c", "copy", "-movflags", "+faststart", out_path,
    ], timeout=900)
    if rc != 0 or not os.path.exists(out_path):
        # 退路：重编码拼接（时间戳不一致时 copy 会失败）
        rc2, err2 = await _run([
            _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", listfile,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-c:a", "aac", "-b:a", "192k", out_path,
        ], timeout=1200)
        if rc2 != 0:
            raise RuntimeError(f"拼接失败：{(err2 or err)[-400:]}")
    try:
        os.remove(listfile)
    except OSError:
        pass


async def _burn(video: str, srt: str, out_path: str, w: int, h: int) -> Tuple[bool, str]:
    """烧字幕。返�?(是否成功, 备注)"""
    if not srt or not os.path.exists(srt):
        return False, "没有字幕内容"
    font_size = max(16, int(w / 34))
    # subtitles 滤镜�?Windows 上要转义盘符冒号与反斜杠
    sub_path = str(srt).replace("\\", "/").replace(":", "\\:")
    style = (
        f"FontName=Microsoft YaHei,FontSize={font_size},"
        f"PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,"
        f"BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV={int(h * 0.05)}"
    )
    vf = f"subtitles='{sub_path}':force_style='{style}'"
    rc, err = await _run([
        _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", video,
        "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-c:a", "copy", "-movflags", "+faststart", out_path,
    ], timeout=900)
    if rc == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
        return True, ""
    return False, (err or "")[-300:]


# ══════════════════════════════════════════════════════════════�?# 主入�?# ══════════════════════════════════════════════════════════════�?
def _scene_card_text(scene: Optional[Dict[str, Any]]) -> str:
    """换场字幕卡的文本�?地点 · �?外景 · 时段"�?
    �?只写场景卡里**真实存在**的字段。一个都没有就返回空�?�?      这一处不打卡（宁可没有卡，也不要编一个不存在的地点）�?    这是方法论仓库里 "提示�?画面不得创建未登记的地点"
    （video-prompt-contract.md�?提示词不得创建未登记的主要角色、怪物、地点或关键道具*�?    �?*后期**这一侧的对应做法�?    """
    if not scene:
        return ""
    bits: List[str] = []
    name = _clean_narration(scene.get("name") or "")
    if name:
        bits.append(name)
    lt = str(scene.get("location_type") or "").strip().lower()
    if lt in ("indoor", "interior", "内景", "室内"):
        bits.append("内景")
    elif lt in ("outdoor", "exterior", "外景", "室外"):
        bits.append("外景")
    tod = str(scene.get("time_of_day") or "").strip()
    _TOD = {"day": "�?, "night": "�?, "dawn": "拂晓", "dusk": "黄昏",
            "morning": "清晨", "evening": "傍晚", "afternoon": "午后",
            "golden_hour": "黄金时刻", "noon": "正午"}
    if tod:
        bits.append(_TOD.get(tod.lower(), tod))
    return " · ".join(bits)


async def compose_project(
    project_id: str,
    *,
    db,
    settings: Optional[dict] = None,
    voice_id: str = "edge:zh-CN-XiaoxiaoNeural",
    with_voice: bool = True,
    burn_subtitles: bool = True,
    bgm_path: str = "",
    bgm_volume: float = 0.25,
    target_resolution: str = "",
    output_name: str = "final.mp4",
    auto_images: bool = False,
    image_provider: str = "minimax",
    image_size: str = "1280x720",
    transition: str = "fade",
    transition_sec: float = 0.5,
    scene_transition: str = "auto",
    scene_card: bool = True,
    cut_stalls: bool = False,
    narration_fallback: bool = False,
    shot_duration: Optional[float] = None,
    prefer_generated: bool = True,
    progress: ProgressFn = None,
) -> ComposeResult:
    """把一个项目的分镜合成为成片�?
    不依赖任何付费视频模�?Key —�?全程只用本地图片 + Edge TTS + FFmpeg�?    """
    res = ComposeResult(voice_id=voice_id if with_voice else "", with_voice=bool(with_voice))
    settings = settings or {}

    def step(pct: float, msg: str):
        logger.info("[compose %.0f%%] %s", pct * 100, msg)
        if progress:
            try:
                progress("compose", pct, msg)
            except Exception:
                pass

    # ── 0. 准备 ──
    shots = db.list_shots(project_id) or []
    shots = sorted(shots, key=lambda s: s.get("order_index") or 0)
    if not shots:
        res.errors.append("该项目还没有分镜。请先在「剧本」页生成剧本，再到「分镜」页点「自动从剧本生成」�?)
        return res

    scenes = {s["id"]: s for s in (db.list_scenes(project_id) or [])}
    chars = {c["id"]: c for c in (db.list_characters(project_id) or [])}

    if target_resolution and "x" in target_resolution.lower():
        try:
            w, h = [int(x) for x in target_resolution.lower().split("x")]
        except Exception:
            w, h = DEFAULT_W, DEFAULT_H
    else:
        proj = db.get_project(project_id) or {}
        ps = proj.get("settings") or {}
        if isinstance(ps, str):
            try:
                ps = json.loads(ps)
            except Exception:
                ps = {}
        aspect = ps.get("default_aspect_ratio") or "16:9"
        w, h = {"16:9": (1280, 720), "9:16": (720, 1280), "1:1": (1024, 1024)}.get(aspect, (1280, 720))

    out_root = settings.get("output_dir") or os.path.join(
        os.environ.get("VIDEOFORGE_DATA_DIR", os.getcwd()), "outputs")
    work = os.path.join(settings.get("cache_dir") or os.path.join(
        os.environ.get("VIDEOFORGE_DATA_DIR", os.getcwd()), "cache"), "compose", project_id)
    os.makedirs(work, exist_ok=True)
    out_dir = os.path.join(out_root, project_id)
    os.makedirs(out_dir, exist_ok=True)

    step(0.02, f"准备合成：{len(shots)} 个分�?· {w}x{h}")

    # ══════════════════════════════════════════════════════════════
    # 整片选角：一次把"每个角色用哪把嗓�?定死�?026-09-13�?    # ══════════════════════════════════════════════════════════════
    # 用户原话�?配音要角色来发音……角色自己说话且有自己的音色…�?    #            声音要跟人物形象符合"�?    # 旧代码是**逐分�?*挑音色（`used=set(cast_used.values())` 在分镜内局部）�?    # 于是同一个角色在不同分镜里可能拿到不同的嗓子 —�?这等�?角色没有自己的声�?�?    # 这里改成整片一次定，并�?*落库**（写进角色卡�?reference_features.voice_id），
    # 这样：① 重合成也稳定；② 用户在角色页能看到并手动改�?    cast_map: Dict[str, str] = {}
    cast_reasons: Dict[str, str] = {}
    try:
        from core.voicecast import cast_plan as _cast_plan
        _cp = _cast_plan(list(chars.values()), voice_id)
        cast_map = _cp.get("voices") or {}
        cast_reasons = _cp.get("reasons") or {}
        if cast_map:
            res.warnings.append(
                "配音选角�? + "�?.join(
                    f"{n}→{cast_map[n]}" for n in list(cast_map)[:6])
                + ("�? if len(cast_map) > 6 else ""))
        for _c in _cp.get("collisions") or []:
            res.warnings.append(
                f"配音选角提醒：{('�?.join(_c['characters']))} 拿到了同一把嗓�?
                f"（{_c['voice']}）—�?多角色对白会听起来像一个人在自言自语�?
                f"建议到「角色」页给其中一个手选音色�?)
        # 落库（只补空位，绝不覆盖用户已选的音色�?        for _n, _v in cast_map.items():
            _c = chars.get(_n) or next(
                (x for x in chars.values() if (x.get("name") or "") == _n), None)
            if not _c or not _v:
                continue
            try:
                _rf = _c.get("reference_features")
                if isinstance(_rf, str):
                    _rf = json.loads(_rf or "{}")
                _rf = dict(_rf or {})
                if not (_rf.get("voice_id") or _rf.get("voice")):
                    _rf["voice_id"] = _v
                    _rf["voice_source"] = "videoforge_cast_plan"
                    db.update_character(_c["id"], reference_features=_rf)
            except Exception as _e:
                logger.debug("选角落库失败（不影响本次合成）：%s", _e)
    except Exception as _e:
        logger.warning("整片选角失败（退回逐句默认音色）：%s", _e)

    # ── 1. 逐分镜：配音 + 画面 + 片段 ──
    clip_paths: List[str] = []
    srt_entries: List[dict] = []
    # 「素材短于标称」逐镜收集，最�?*汇总成一�?*警告 —�?    # 逐镜各发一条会把后面的重要结论（逐切点转�?/ 纹理统一 / 连贯度）
    # 挤出管线结果的警告上限（`pipeline._step_base` 只取�?5 条）�?    _short_notes: List[str] = []
    _no_dialogue_shots: List[int] = []
    _narration_used = 0
    timeline_cursor = 0.0
    clip_infos: List[ClipInfo] = []
    # 每个分镜�?未做转场前的拼接�?上的位置，用于把字幕从分镜内相对时间
    # 映射到成片绝对时间（转场会让成片�?Σdur 短，不映射就会整体错位）
    shot_bounds: List[dict] = []

    for i, shot in enumerate(shots):
        ci = ClipInfo(index=i, narration=narration_for_shot(shot, i))

        # ══════════════════════════════════════════════════════════
        # ★★�?最高优先级：用「生成」页已经渲染好的分镜视频 ★★�?        #
        # 这是本模块最大的设计缺陷的修复：此前 compose 完全无视用户
        # 在「生成」页用真实视频模型（可灵/海螺/Seedance…）生成好的片段�?        # 一律从图片重新"本地合成"，于是用户花钱生成的高质量画�?        # �?一键成�?时被整个丢掉，最后得到的还是只有字幕+AI语音的东西�?        #
        # 正确顺序：已生成的分镜视�?> 分镜/场景�?+ 运镜 > 文字�?        # ══════════════════════════════════════════════════════════
        cand_path = _selected_candidate_path(shot) if prefer_generated else ""
        if cand_path:
            ci.image, ci.image_source = cand_path, "generated_video"
            try:
                cdur = await probe_duration(cand_path)
            except Exception:
                cdur = 0.0
            # 时长�?*素材真实长度优先，不循环重播**�?            #
            # ★★ 真实事故�?026-09-13，项�?caa9904f）：
            #   旧逻辑�?素材与标称差 >0.3s 就用标称时长"，于�?5.88s 的生成素�?            #   �?`-stream_loop -1` 拉到标称 12s —�?同一段运�?*重演一�?*�?            #   在成片里就是"演到一半突然跳回开�?。实测这一版成片的
            #   `spike_ratio` 高达 **6.016**（≈1 才算接缝看不出来），
            #   而全片最大跳变就来自这些**循环回跳�?*，不是接缝本身�?            #   这属�?一眼AI"里最刺眼的一类：不是画质问题，是运动不连续�?            #
            # 现在的规则：
            #   · 用户**显式指定**�?每段统一时长" �?尊重他，循环补足（并如实警告�?            #   · 否则 �?按素材真实长度收尾，宁可这一镜短一点，也不重播
            target_len = float(shot.get("duration_seconds") or 0)
            explicit_len = bool(shot_duration and float(shot_duration) > 0)
            if explicit_len:
                target_len = float(shot_duration)
            if explicit_len:
                use_dur = target_len
                if cdur > 0.2 and cdur + 0.3 < use_dur:
                    _w = (f"这一镜指�?{use_dur:.1f}s，但生成素材只有 {cdur:.1f}s —�?"
                          f"已循环补足（同一段运动会出现第二次；不想要就取消"
                          f"『统一时长』，让它按素材真实长度收尾）")
                    ci.warnings.append(_w)
            elif cdur > 0.2:
                use_dur = cdur
                if target_len and cdur + 0.3 < target_len:
                    _w = (f"分镜标称 {target_len:.1f}s，但生成素材只有 {cdur:.1f}s —�?"
                          f"成片�?*素材真实长度**收尾，不循环重播"
                          f"（循环会让同一段运动重演一次，看着像卡带）�?
                          f"要更长请把这一镜的时长调到模型能力范围内，"
                          f"或让它多分几段生成�?)
                    ci.warnings.append(_w)
                    _short_notes.append(
                        f"�?{i + 1} �?{cdur:.1f}s（标�?{target_len:.1f}s�?)
            else:
                use_dur = target_len or 5.0
            clip = os.path.join(work, f"clip_{i:02d}.mp4")
            step(0.05 + 0.65 * i / len(shots),
                 f"使用已生成视�?{i + 1}/{len(shots)}（{cdur:.1f}s�?)

            # ══════════════════════════════════════════════════════════════
            # ★★ 配音：按角色逐句念台�?            # ══════════════════════════════════════════════════════════════
            # 真实事故�?026-09-13）：�?已生成视�?的分�?*完全没有音轨** —�?            # 实测成片 `final.mp4` 只有 `Stream #0:0` 视频流，整条片子是无声的
            # （只有烧进画面的字幕）。原因不是没实现，而是
            # **`voicecast.dub_shot` 写好了却从来没被任何地方调用�?*�?            #
            # 时间轴必须先�?*和生成时同一套规�?*重排，不能直接把草稿塞进去：
            # 草稿是按标称 12s 铺的，而画面只�?5.9s；`normalize_timeline` 对超�?            # 时长的条目是"压回最后一�?（不是等比缩放）�?            # 直接�?12s 的轴�?5.9s 用会让后面几�?*叠在最后一秒里**�?            # 所以这里调 `plan_shot(audio_seconds=实测台词时长)` 拿到重排后的时间轴，
            # 它正是当初生成这一镜时用的那份�?            _dub: Dict[str, Any] = {}
            _audio_sec = 0.0
            _has_lines = False
            try:
                from core.dialogue import timeline_lines as _tl_lines
                _draft = _tl_lines(shot.get("layer2_timeline"),
                                   float(shot.get("duration_seconds") or use_dur))
                _has_lines = bool(_draft)
                if _has_lines:
                    from core.voicecast import measure_lines_duration
                    _audio_sec = float(await measure_lines_duration(
                        _draft, list(chars.values()), settings) or 0.0)
            except Exception as e:
                logger.warning("量台词时长失败（按草稿时间轴继续）：%s", e)

            _planned_dur = 0.0
            _seg_shot = shot
            if _has_lines and _audio_sec > 0.2:
                try:
                    from core.shotplan import plan_shot
                    from core.model_catalog import find_video_model
                    _entry = find_video_model(shot.get("model_name") or "",
                                              shot.get("model_provider") or "") or {}
                    _pl = plan_shot(shot, model_entry=_entry,
                                    resolution=shot.get("resolution") or "1080p",
                                    audio_seconds=_audio_sec,
                                    provider=shot.get("model_provider") or "")
                    _segs = _pl.get("segments") or []
                    _planned_dur = float(_pl.get("effective") or 0.0)
                    if len(_segs) == 1 and _segs[0].get("timeline"):
                        _seg_shot = dict(shot)
                        _seg_shot["layer2_timeline"] = _segs[0]["timeline"]
                except Exception as e:
                    logger.warning("重排台词时间轴失败（按草稿继续）�?s", e)

            if with_voice:
                try:
                    from core.voicecast import dub_shot
                    # �?台词要排�?*画面真实的长�?*（不是标称、也不是规划值）�?                    #   排不下时 `synthesize_line` 会在 MIN/MAX_SPEED 内温和变速�?                    #   并如实报溢出�?*绝不能反过来把画面拉�?* —�?                    #   拉长就意味着 `-stream_loop` 重播，实测那正是"卡带�?的元�?                    #   （为让台词说完而延长画面后，spike_ratio �?1.687 反弹�?4.034）�?                    _dub = await dub_shot(
                        _seg_shot, characters=list(chars.values()), settings=settings,
                        out_dir=os.path.join(work, f"dub_{i:02d}"),
                        default_voice=voice_id,
                        duration=use_dur,
                        cast=cast_map)
                    if _dub.get("ok"):
                        ci.warnings.extend(_dub.get("warnings") or [])
                        _cast = _dub.get("cast") or {}
                        if _cast:
                            ci.warnings.append(
                                "按角色配音：" + "�?.join(f"{k}→{v}" for k, v in _cast.items()))
                    else:
                        ci.warnings.append(
                            f"按角色配音未完成（{_dub.get('error')}�?)
                except Exception as e:
                    logger.exception("dub_shot 失败")
                    ci.warnings.append(f"按角色配音异常（{e}�?)

            # 语音自然需要的长度：超出画面就**如实报告**，不动画�?            _speech_end = 0.0
            if _dub.get("ok"):
                try:
                    _speech_end = max([float(sb.get("end") or 0)
                                       for sb in (_dub.get("subtitles") or [])] or [0.0])
                except Exception:
                    _speech_end = 0.0
            if _speech_end and _speech_end > use_dur + 0.35:
                ci.warnings.append(
                    f"台词说到 {_speech_end:.1f}s，画面只�?{use_dur:.1f}s —�?"
                    f"结尾�?{_speech_end - use_dur:.1f}s 会被切掉�?
                    f"�?*不再**把画面拉长来迁就台词：拉长等于让画面重播一次，"
                    f"看着像卡带。）彻底解决要重新生成这一镜、或把台词改短�?)

            # ★★ 起幅死帧：I2V 模型常常前几帧几乎不动（从静图里"苏醒"）�?            #   换场默认改成硬切之后这段死的起幅**不再被溶解盖�?* —�?            #   实测同一部片子第 5 镜起幅只�?0.26× 全片中位，观感就�?切过来卡住半�?�?            #   治法�?*裁掉死帧**，并且把这一镜的配音轨与字幕**同量平移**�?            #   否则画面提前�?h 秒、声音没提前 �?声画错位（这是不能省的第二步）�?            _sres = {}
            if not explicit_len:
                try:
                    from core import seamfix as _sfx
                    _sres = await _sfx.probe_settle(cand_path)
                except Exception as _e:
                    logger.warning("沉降测量失败（不裁）�?s", _e)
                    _sres = {}
            _ht = float((_sres or {}).get("head_trim") or 0.0)
            # �?尾巴也要管：实测另一个项目（d5815c1e�? 个镜�?*全都�?*结尾
            #   0.33s 定住不动 —�?模型的收尾习惯。只裁头，这些就永远留在片子里�?            #   但尾部裁�?*不能切掉台词**：只有在人声已经说完之后才裁�?            #   否则宁可留着（如实说明），也不把最后一句话尾切掉�?            _tt = float((_sres or {}).get("tail_trim") or 0.0)
            if _tt > 0.02:
                if _speech_end and (_speech_end > use_dur - _tt + 0.15):
                    ci.warnings.append(
                        f"结尾�?{_tt:.2f}s 定住不动，但台词说到 {_speech_end:.1f}s�?
                        f"裁掉会把话尾切掉 —�?已保留�?)
                    _tt = 0.0
                elif use_dur - _ht - _tt < 2.0:
                    _tt = 0.0
            if _ht > 0.02 and use_dur - _ht - _tt >= 2.0:
                use_dur = round(use_dur - _ht, 3)
                _w = (f"起幅几乎静止（头 0.4 秒运动只有本段中位的 "
                      f"{(_sres.get('head_settle') or 0):.2f} 倍）—�?已裁掉开�?"
                      f"{_ht:.2f}s 死帧（配音与字幕同步前移，不会声画错位）�?
                      f"不裁的话，硬切进来会像「卡住半秒」�?)
                ci.warnings.append(_w)
                res.warnings.append(f"分镜{i + 1}：{_w}")
            else:
                _ht = 0.0
            if _tt > 0.02:
                use_dur = round(use_dur - _tt, 3)
                _w2 = (f"结尾�?{_tt:.2f}s 定住不动（尾部运动只有本段中位的 "
                       f"{(_sres.get('tail_settle') or 0):.2f} 倍）—�?已裁掉，"
                       f"否则观感是「演完了画面还赖着不走」�?)
                ci.warnings.append(_w2)
                res.warnings.append(f"分镜{i + 1}：{_w2}")

            # ★★ 中间那段**真冻�?*：只能剪掉（这是"动刀"，默认关闭）
            #   实测背景：同一提示词重生成**不会**让它消失�?.92s �?4.42s，反而更长）�?            #   它是内容/提示词驱动的。管线能做的只有两件：如实报出来，或者剪掉�?            #   剪的做法：画面与配音**同量�?*（否则声画错位），字幕同量平移�?            _removals: List[Any] = []
            _mid_stalls = list((_sres or {}).get("mid_stalls") or [])
            if _mid_stalls and not cut_stalls:
                # 不剪也要**告诉用户有这个选项** —�?否则他只会看�?画面停了几秒"
                # 而不知道该传什么�?                _tot = float((_sres or {}).get("mid_stall_seconds") or 0.0)
                _w_hint = (f"中间�?{_tot:.2f}s 画面几乎不动（模型没动）�?
                           f"这种停顿**重生成也不会消失**（实测同提示词重跑反而更长）—�?
                           f"要么改这一镜的提示�?拆成两镜，要么传 cut_stalls=true 直接剪掉"
                           f"（画面会出现一次跳切）�?)
                ci.warnings.append(_w_hint)
                res.warnings.append(f"分镜{i + 1}：{_w_hint}")
            if cut_stalls and not explicit_len:
                # 复用 probe_settle 已经算好的那条曲线（不再抽一次帧�?                for _w3 in _mid_stalls:
                    _removals.append((float(_w3["at"]),
                                      float(_w3["at"]) + float(_w3["seconds"])))
                if _ht > 0.02:
                    _removals.append((0.0, _ht))     # 头部死帧也一并剪�?                _dur0 = float(cdur or use_dur)
                if _removals:
                    try:
                        from core import seamfix as _sfx3
                        _keep = _sfx3.kept_ranges(_dur0, _removals)
                        _removed = round(_dur0 - sum(b - a for a, b in _keep), 3)
                    except Exception:
                        _removed = 0.0
                    # 兜底：剪太多�?50%）或太少�?0.5s）都不做，别把片子剪�?                    if 0.5 <= _removed < _dur0 * 0.5:
                        _cp = os.path.join(work, f"cutstall_{i:02d}.mp4")
                        if await _cut_ranges(cand_path, _removals, _cp, _dur0):
                            cand_path = _cp
                            use_dur = round(use_dur - _removed, 3)
                            ci.stall_cut = _removed
                            ci.removals = list(_removals)
                            ci.audio_precut = True
                            _ht = 0.0            # 画面已剪过，_normalize_clip 不再裁头
                            _w4 = (f"中间�?{_removed:.2f}s 画面几乎不动（模型没动）"
                                   f"—�?�?*剪掉**，画面在那里是一次跳切�?
                                   f"这比让画面停住自然，但确实是动刀�?)
                            ci.warnings.append(_w4)
                            res.warnings.append(f"分镜{i + 1}：{_w4}")
                        if ci.stall_cut and ci.audio and os.path.exists(ci.audio):
                            _ap = os.path.join(work, f"cutstall_{i:02d}_a.m4a")
                            _adur = 0.0
                            try:
                                _adur = float(await probe_duration(ci.audio) or 0.0)
                            except Exception:
                                _adur = 0.0
                            if _adur > 0.2 and await _cut_ranges(ci.audio, _removals,
                                                                 _ap, _adur,
                                                                 audio=True):
                                ci.audio = _ap
                            else:
                                ci.warnings.append(
                                    "配音轨没能同量剪（本镜可能轻微声画错位）")

            try:
                # 统一尺寸/帧率/音轨，保证后面能无缝拼接
                warns = await _normalize_clip(cand_path, clip, w, h, FPS, use_dur,
                                              head_trim=_ht)
                ci.warnings.extend(warns)
                ci.trim_head = _ht
                ci.trim_tail = _tt
                clip_paths.append(clip)
            except Exception as e:
                ci.warnings.append(f"已生成视频规范化失败，回退本地合成：{e}")
                res.warnings.append(f"分镜{i + 1} 已生成视频不可用，已回退本地合成")
                cand_path = ""      # 走下面的本地合成路径
            if cand_path:
                ci.audio, ci.duration = "", use_dur
                # �?字幕：必须走统一的台词解析�?                #   `dialogue` 现在�?{character,text,emotion} **结构�?*�?                #   旧代�?`(it.get("dialogue") or "").strip()` 会对 dict �?.strip()
                #   �?AttributeError，直接把整条合成流程搞崩�?                #   这里存的�?*分镜内相对时�?*，拼接完成后再用
                #   continuity.final_timeline 映射到成片绝对时�?                #   （转场会吃掉时长，不映射就会整体错位）�?                from core.dialogue import timeline_lines as _tl_lines2
                added = 0
                # �?画面裁过头的 `_ht` 秒、以及中间剪掉的 `ci.removals` 段，
                #   字幕时间轴都必须**同量平移**，否则从这一镜起字幕会比画面晚�?                #   头部是均匀平移，中间剪切是**分段**平移 —�?用统一映射函数算�?                _rem: List[Any] = list(ci.removals or [])
                if not _rem and _ht > 0.02:
                    _rem = [(0.0, float(_ht))]
                # 草稿时间轴的窗口：成片时�?+ 被剪掉的总量（否则后面几句会被夹掉）
                _shift = float(sum(float(b) - float(a) for a, b in _rem)) \
                    if _rem else float(_ht or 0.0)

                def _maploc(t: float) -> float:
                    try:
                        from core import seamfix as _sfx4
                        return float(_sfx4.map_after_removals(float(t), _rem))
                    except Exception:
                        return max(0.0, float(t) - float(_ht or 0.0))
                if _dub.get("ok") and (_dub.get("subtitles") or []):
                    # 字幕与语�?*同源**：用实际放下的位置（不是草稿时间轴）
                    ci.audio = _dub["path"]
                    for sb in _dub["subtitles"]:
                        st_ = _maploc(float(sb.get("start") or 0))
                        en_ = _maploc(float(sb.get("end") or 0))
                        if en_ <= 0.05 or st_ >= use_dur or (en_ - st_) < 0.05:
                            continue
                        srt_entries.append({
                            "start": st_, "end": min(en_, use_dur),
                            "text": str(sb.get("text") or ""),
                            "character": sb.get("character") or "",
                            "_shot": i,
                        })
                        added += 1
                else:
                    for ln in _tl_lines2(shot.get("layer2_timeline"), use_dur + _shift):
                        st_, en_ = _maploc(float(ln["start"])), _maploc(float(ln["end"]))
                        if en_ <= 0.05 or st_ >= use_dur or (en_ - st_) < 0.05:
                            continue                # 超出本镜长度的裁掉，免得串到下一�?                        srt_entries.append({
                            "start": st_, "end": min(en_, use_dur),
                            "text": ln["text"],
                            "character": ln.get("character") or "",
                            "_shot": i,
                        })
                        added += 1
                    # 没有台词的分镜：把画面概�?*切成短句**分条显示�?                    # 旧做法是把整段概述（常常 100+ 字）当一条字幕糊满整个镜�?—�?                    # 屏幕上糊一大片字�?0 秒不换，比没有字幕还难看�?                    # �?但这只是**估算**（按句子平均铺）。真正准的时间在下面 TTS �?                    #   返回值里（`_r.subtitles` 是词级时间）—�?实测�?估算"的偏差：
                    #   同一个镜头开头偏 +1.65s、中间偏 �?.74s，�?TTS 真实时间
                    #   能把偏差压到 0.1s 量级。所�?TTS 成功后要**替换**掉这里的估算�?                    _est_mark = len(srt_entries)
                    if not added and ci.narration:
                        chunks = _split_subtitle_text(ci.narration)
                        n = max(1, len(chunks))
                        seg = use_dur / n
                        for k, ck_ in enumerate(chunks):
                            st_ = k * seg
                            if st_ >= use_dur:
                                break
                            srt_entries.append({
                                "start": st_, "end": min(st_ + seg, use_dur),
                                "text": ck_, "character": "", "_shot": i,
                            })
                            added += 1
                    # 没有台词时也要有声音：用默认音色念画面概�?                    # （与本地合成路径一致；否则这一镜就是静音，一对比�?�?�?                    #
                    # ★★ 2026-09-13 用户裁定�?*这条默认关掉�?*�?                    #    用户原话�?配音要角色来发音而不是从到到尾念稿子"�?                    #    方法论依据（`Hell-Grind-AIGC-Skill / failure-diagnosis.md`）：
                    #    `F-AUDIO-POLLUTION` = "自动音乐�?*旁白**、字幕或无关�?�?                    #    也就�?在用户没要求旁白时自动加旁白"本身就是一�?*明列的失�?*�?                    #    而且这条旁白念的�?`layer1_overview`（剧情概述）�?                    #    正是 `F-DIALOGUE-VISUALIZED` 想避免的"把叙述当台词"�?                    #
                    #    正确做法：没有台�?�?**这一镜就是没有对�?*（静�?只剩环境与音乐）�?                    #    并在告警里明确指�?要让角色说话，去补台�?�?                    #    想要旧行为可以显式传 `narration_fallback=True`（保留给
                    #    "纪录片式旁白"这种**用户主动选择**的形态）�?                    if with_voice and not ci.audio and ci.narration and narration_fallback:
                        try:
                            from core.voice.base import TTSRequest
                            from core.voice import dispatcher as _vd
                            # �?旁白文本必须先按**画面长度**截短�?                            #   实测一�?20.25s 的镜头，概述念完�?**28.8s**�?                            #   结果结尾 8.5s 的话全被切掉（用户听感就�?话没说完"）�?                            #   4.3 �?秒是略保守的中文语速估计，�?0.6s 收尾�?                            _avail = max(16, int((use_dur - 0.6) * 4.3))
                            _txt = ci.narration
                            _trimmed = 0
                            if len(_txt) > _avail:
                                _cut = _txt[:_avail]
                                for _sep in ("�?, "�?, "�?, "�?, "�?, ".", "!", "?", ","):
                                    _pos = _cut.rfind(_sep)
                                    if _pos >= _avail * 0.6:
                                        _cut = _cut[:_pos + 1]
                                        break
                                _trimmed = len(_txt) - len(_cut)
                                _txt = _cut
                            _nv = os.path.join(work, f"nar_{i:02d}.mp3")
                            _r = await _vd.synthesize(
                                TTSRequest(text=_txt, voice_id=voice_id,
                                           output_path=_nv), settings)
                            if _r.success and _r.audio_path and os.path.exists(_r.audio_path):
                                ci.audio = _r.audio_path
                                _narration_used += 1
                                # �?�?TTS 返回�?*真实**时间替换掉上面的估算字幕
                                _tsubs = [s for s in (getattr(_r, "subtitles", None) or [])
                                          if str(s.get("text") or "").strip()]
                                if _tsubs:
                                    del srt_entries[_est_mark:]
                                    added = 0
                                    try:
                                        from core.subtitle import resplit_long_cues as _rsl
                                        _tsubs = _rsl(_tsubs)
                                    except Exception as _e:
                                        logger.warning("字幕拆句失败（按整条输出）：%s", _e)
                                    for _s in _tsubs:
                                        _st = max(0.0, float(_s.get("start") or 0) - _shift)
                                        _en = float(_s.get("end") or 0) - _shift
                                        if _en <= 0.05 or _st >= use_dur:
                                            continue
                                        srt_entries.append({
                                            "start": _st, "end": min(_en, use_dur),
                                            "text": str(_s.get("text") or ""),
                                            "character": "", "_shot": i,
                                        })
                                        added += 1
                                _nd = float(_r.duration_seconds or 0) or await probe_duration(
                                    _r.audio_path)
                                if _trimmed:
                                    ci.warnings.append(
                                        f"旁白按画面长度截短（去掉 {_trimmed} 字）�?
                                        f"否则会超出这一镜�?)
                                if _nd > use_dur + 0.35:
                                    ci.warnings.append(
                                        f"旁白仍然比画面长（{_nd:.1f}s > {use_dur:.1f}s），"
                                        f"结尾会被切掉一点�?)
                        except Exception as e:
                            ci.warnings.append(f"旁白配音失败：{e}")
                ci.subtitle_count = added
                timeline_cursor += use_dur
                clip_infos.append(ci)
                continue

        # 画面：先用现成的（分镜图/场景�?角色图）；没有就按分镜自动生成，再没有才用文字卡
        img, src = _pick_image(shot, scenes, chars)
        if not img and auto_images:
            step(0.05 + 0.65 * i / len(shots), f"分镜 {i + 1}：正在生成画面�?)
            gen_path = os.path.join(work, f"auto_{i:02d}.png")
            img, gerr = await _auto_generate_image(
                shot, scenes, chars, gen_path, image_provider, image_size, settings)
            if img:
                src = "ai_generated"
            elif gerr:
                ci.warnings.append(f"自动生成画面失败，改用文字卡：{gerr}")
                res.warnings.append(f"分镜{i + 1} 自动生成画面失败：{gerr}")
        if not img:
            img = os.path.join(work, f"card_{i:02d}.png")
            make_text_card(
                img,
                title=ci.narration or f"分镜 {i + 1}",
                body=_clean_narration(shot.get("layer1_overview") or "")[:110]
                     or _as_text(shot.get("layer3_constraints"))[:110],
                w=w, h=h,
                badge=f"分镜 {i + 1}",
            )
            src = "text_card"
            ci.warnings.append("该分镜无可用画面，已自动生成文字�?)
        ci.image, ci.image_source = img, src

        # 配音�?*只念真台�?*，不再默认朗读画面概�?—�?见上面的长篇说明�?        audio_path = ""
        subs: List[dict] = []
        est = float(shot.get("duration_seconds") or 4.0)
        _lines_here = []
        try:
            from core.dialogue import timeline_lines as _tlx
            _lines_here = _tlx(shot.get("layer2_timeline"), est)
        except Exception:
            _lines_here = []
        if with_voice and _lines_here:
            try:
                from core.voicecast import dub_shot as _dub_local
                _d = await _dub_local(
                    shot, characters=list(chars.values()), settings=settings,
                    out_dir=os.path.join(work, f"dub_{i:02d}"),
                    default_voice=voice_id, duration=est, cast=cast_map)
                if _d.get("ok"):
                    audio_path = _d.get("path") or ""
                    subs = list(_d.get("subtitles") or [])
                    for _w in (_d.get("warnings") or []):
                        ci.warnings.append(str(_w))
                else:
                    ci.warnings.append(
                        f"该分镜的台词合成失败，本段改用静音：{_d.get('error')}")
            except Exception as e:
                audio_path = ""
                ci.warnings.append(f"配音异常，本段改用静音：{e}")
                logger.exception("dub_shot failed for shot %s", shot.get("id"))
        elif with_voice and not _lines_here:
            # �?这一�?*没有台词**。旧行为是拿默认音色把画面概述念一遍，
            #   用户的原话是"从头到尾念稿子，很恶�?。现在如实静音并告诉他怎么办�?            _no_dialogue_shots.append(i + 1)
            ci.warnings.append("该分镜没有台词（不做旁白朗读；要让人物说话请补台词）")

        ci.audio, ci.duration = audio_path, max(MIN_SHOT_SECONDS, round(est, 2))
        ci.subtitle_count = len(subs)

        # 字幕：TTS 给的�?*分镜内相对时�?*，这里存相对时间�?        # 拼接完统一映射到成片绝对时间（转场会吃掉时长，不映射必错位�?        for s in subs:
            t = (s.get("text") or "").strip()
            if not t:
                continue
            srt_entries.append({
                "start": float(s.get("start") or 0),
                "end": float(s.get("end") or 0),
                "text": t, "character": "", "_shot": i,
            })

        # 渲染片段
        clip = os.path.join(work, f"clip_{i:02d}.mp4")
        try:
            warns = await render_clip(img, audio_path, ci.duration, clip, w, h, FPS)
            ci.warnings.extend(warns)
            clip_paths.append(clip)
        except Exception as e:
            res.errors.append(f"分镜{i + 1} 渲染失败：{e}")
            logger.exception("clip render failed")
            return res

        # ══════════════════════════════════════════════════════════════
        # ★★ 片段时长必须**实测**，不能用"我打算渲染多�?
        # ══════════════════════════════════════════════════════════════
        # 踩到的真事（和第十五章那�?假时�?同一族）�?        #   分镜标称 10 秒，但模型只生成�?5.88 秒的视频�?        #   `render_clip(..., 10, ...)` 变不出那 4.12 秒，产物只有 5.88 秒，
        #   �?`ci.duration` 仍然写着 10.0�?        #   后果：`timeline_cursor`/`shot_bounds` �?60 秒排�?        #   字幕映射出来的成片长度是 58.95s�?*而真正的成片只有 39.93s**
        #   —�?后半段字幕全压在不存在的时间上�?        #   实测 6 个片段声�?60.0s、成�?39.93s，差�?20 秒就是这里来的�?        #   所以：渲完立刻 probe，把**实际**时长写回去，后面一律用它�?        real_dur = 0.0
        try:
            # �?必须�?*视频�?*长度，不能用容器时长�?            #   容器时长会被静音音轨撑长（实�?clip_00：容�?10.00s / 视频�?5.64s），
            #   拿容器时长来对账等于"用一个假的数去校验另一个假的数"�?            real_dur = float(await probe_video_duration(clip) or 0.0)
            if real_dur <= 0.2:
                real_dur = float(await probe_duration(clip) or 0.0)
        except Exception as e:
            logger.warning("探测片段时长失败 %s�?s", clip, e)
        if real_dur > 0.2 and abs(real_dur - ci.duration) > 0.35:
            res.warnings.append(
                f"分镜{i + 1}：想�?{ci.duration:.1f}s，实际只�?{real_dur:.1f}s"
                f"（素材本身就没那么长）—�?时间轴已�?*实际时长**排，"
                f"避免字幕压到不存在的画面上�?)
            ci.warnings.append(
                f"计划 {ci.duration:.1f}s，实�?{real_dur:.1f}s（按实际记录�?)
            ci.duration = round(real_dur, 2)
        elif real_dur > 0.2:
            ci.duration = round(real_dur, 2)

        shot_bounds.append({"index": i, "start": timeline_cursor,
                            "dur": ci.duration, "ends_at": timeline_cursor + ci.duration})
        timeline_cursor += ci.duration
        clip_infos.append(ci)
        step(0.05 + 0.65 * (i + 1) / len(shots),
             f"已渲�?{i + 1}/{len(shots)} 个分镜（{ci.duration:.1f}s · {src}�?)

    # 汇�?素材短于标称"这一条（逐镜详情在各 clip �?warnings 里，这里只给一条总账�?    if _short_notes:
        res.warnings.append(
            f"�?{len(_short_notes)} 个分镜的生成素材**短于标称时长**，成片按素材真实长度"
            f"收尾、不循环重播�? + "�?.join(_short_notes) +
            "。要更长请把这些分镜的时长调到模型能力范围内，或让它多分几段生成�?)
    res.clip_count = len(clip_paths)
    res.clips = [ci.__dict__ for ci in clip_infos]

    # ── 2. 拼接�?*逐切�?*选转场（不再一�?fade）──
    #
    # 为什么不再一律溶解：真实影片�?*同一场戏的镜头之间几乎都是硬�?*�?    # 处处溶解反而一眼假（用户反馈的"情景转化衔接一眼AI"里有它一份）�?    # 规则：同场景�?�?硬切；跨场景 �?短溶解；时间/地点大跳 �?长溶解�?    step(0.75, "按镜头关系选择转场并拼接�?)
    merged = os.path.join(work, "merged.mp4")
    cplan: List[Dict[str, Any]] = []
    # 实际生效的转场计划（`assemble` 的自适应摊薄会改转场时长�?    # �?`concat_with_transitions` �?`cuts` 回灌；没有它时间轴会整体偏）
    cplan_actual: List[Dict[str, Any]] = []
    n_hard_cuts = 0        # 成片�?换场硬切"的处数（给成片连贯度指标做语境说明）
    try:
        from core.continuity import chain_plan as _chain_plan
        cplan = _chain_plan(shots, scenes=(scenes or {}),
                            scene_change=scene_transition)
        if len(cplan) > len(clip_paths):
            cplan = cplan[:len(clip_paths)]
    except Exception as e:
        logger.warning("衔接计划计算失败，回退统一转场�?s", e)
        cplan = []
    try:
        if cplan:
            try:
                from core.assemble import concat_with_transitions
                # �?把每个镜头的场景 id 传下去：接缝处理只在**同一场景�?*做调色匹�?                #   （白天和夜晚本来就该不一样，跨场景强行拉平反而假）�?                _scene_ids = [str(s.get("scene_id") or "") for s in shots[:len(clip_paths)]]
                # �?地点字幕卡文本（换场时打在黑场上）：从场景卡里取
                #   "地点 · �?外景 · 时段"。场景卡缺字段就退化成场景名，
                #   一个字段都没有就不打卡（不许编地点）�?                _card_texts = [_scene_card_text(scenes.get(_sid)) for _sid in _scene_ids]
                cres = await concat_with_transitions(
                    clip_paths, merged, [p.get("transition") or {} for p in cplan],
                    w=w, h=h, fps=FPS, work_dir=work, scene_ids=_scene_ids,
                    scene_transition_mode=scene_transition,
                    scene_card=bool(scene_card),
                    card_texts=_card_texts)
                # ★★ 实际生效的转场时长（`cuts`�?*必须回灌**给时间轴映射�?                #   真实事故�?026-09-13，项�?d5815c1e）：片段总长 25.68s�?                #   成片只有 **21.58s**，差 4.1s —�?因为 `assemble` 里的
                #   **自适应摊薄�?*�?4 处硬切改成了 0.9s 溶解�?                #   而字幕映射与配音摆放用的�?`cplan` �?硬切 = 0 �?的版本�?                #   结果声音与字�?*整体比画面晚�?4 �?*�?                #   末几条字幕落�?22.2�?4.5s，而片�?21.58s 就结束了 —�?                #   实测声画同步 9/17，字幕压在空气中�?                _cuts_actual = [float(c or 0.0) for c in (cres.get("cuts") or [])]
                cplan_actual = []
                for _i, _p in enumerate(cplan):
                    _q = dict(_p)
                    _sec = _cuts_actual[_i] if _i < len(_cuts_actual) else 0.0
                    _t = dict(_q.get("transition") or {})
                    _t["seconds"] = _sec
                    _t["type"] = "fade" if _sec > 0.01 else "none"
                    _q["transition"] = _t
                    cplan_actual.append(_q)
                cuts = [c for c in _cuts_actual if c > 0]
                n_hard_cuts = max(0, len(clip_paths) - 1 - len(cuts))
                res.warnings.append(
                    f"逐切点转场：{len(clip_paths)} 个镜�?· "
                    f"{len(cuts)} 处溶�?/ {len(clip_paths) - 1 - len(cuts)} 处硬�?
                    + (f" · 转场合计削减 {sum(cuts):.2f}s" if cuts else ""))
                # ★★ 换场处理：哪些跨场景接缝被判�?明暗突变"、上了黑�?地点卡�?                #   必须报出�?—�?用户点名的就�?突然特别�?特别�?�?                #   如果系统悄悄处理了却不说，他既看不到效果也没法复核�?                _hm0 = cres.get("harmonize") or {}
                for _st in (_hm0.get("scene_treatment") or []):
                    if _st.get("treatment") == "dip":
                        res.warnings.append(
                            f"换场 {_st.get('seam')}：{_st.get('why')}")
                    elif _st.get("luma_delta") is not None:
                        res.warnings.append(
                            f"换场 {_st.get('seam')}：{_st.get('why')}")
                # �?把接缝处理的结论如实带出来：画幅是否统一过、裁了多少�?                #   接缝强度从多少降到多少、哪些切点被"摊薄"了�?                #   用户要能看到"为什么这次更顺了"——不然改了也等于没改�?                _hm = cres.get("harmonize") or {}
                if _hm.get("applied"):
                    _t = _hm.get("target") or {}
                    _bt = (_hm.get("before") or {}).get("mean_seam_ratio")
                    _at = (_hm.get("after") or {}).get("mean_seam_ratio")
                    res.warnings.append(
                        f"接缝预处理：画幅已统一�?{_t.get('w')}x{_t.get('h')}"
                        f" @ {_t.get('fps'):g}fps"
                        + (f"，接缝强�?{_bt} �?{_at}（越接近 1 越自然）"
                           if _bt is not None and _at is not None else ""))
                    for _n in (_hm.get("notes") or []):
                        res.warnings.append("接缝�? + str(_n))
                    for _wn in (_hm.get("warnings") or []):
                        res.warnings.append("接缝�? + str(_wn))
                # �?转场摊薄的结�?*不管有没有归一化都要报** —�?                #   画幅本来就统一�?`applied` �?False，但自适应转场照样在起作用
                #   （实测线上有的项目画幅统一、却全用硬切，接缝照样跳）�?                for _s in (_hm.get("softened") or []):
                    res.warnings.append(
                        f"接缝 {_s.get('seam')}：实测跳变是自身运动尺度�?"
                        f"{_s.get('seam_ratio')} 倍，已改�?{_s.get('seconds')}s "
                        f"溶解摊薄（硬切会明显跳）")
                if _hm.get("respected_cuts"):
                    res.warnings.append(
                        f"按设置保留了 {len(_hm['respected_cuts'])} 处硬�?
                        f"（未做自适应摊薄）—�?这些切点实测跳变较大�?
                        f"但「换场硬切」本身就是成立的电影语法�?
                        f"而且硬切不存在「两张画面叠在一起」的问题�?
                        f"想要溶解就传 scene_transition=dissolve�?)
                # �?「量不了」要单独说，绝不能默不作�?—�?                #   沉默会被读成"量过、很自然"。这里的分母�?片段自身的运动尺�?�?                #   图片�?纯静止素材根本没有这个尺度（�?`seamfix.motion_base`）�?                for _u in (_hm.get("unmeasurable") or []):
                    res.warnings.append(
                        f"接缝 {_u.get('seam')} 的强�?*没量出来**：{_u.get('why')}"
                        f"因此这一处按镜头关系处理，而不是按某个实测倍数�?
                        f"（注意：这不等于「这处接缝很自然」，只是没有参照物。）")
                # �?片段**内部**的停顿：接缝看不出来 �?片子里没有卡住的地方�?                #   实测�?1 镜内部有 2.88 秒几乎静�?—�?这一条以前没有任何告警�?                _st = _hm.get("stall") or {}
                _fz = _st.get("freezes") or ((_st.get("after") or {}).get("freezes") or [])
                if _fz:
                    _by: Dict[Any, list] = {}
                    for _f in _fz:
                        _by.setdefault(_f.get("clip"), []).append(_f)
                    _parts = []
                    for _k, _v in sorted(_by.items(), key=lambda kv: str(kv[0])):
                        # �?必须**逐窗�?*如实描述：早先这里写的是
                        #   "最�?{max(seconds)} @ {min(at)}"，把两个不同窗口的数�?                        #   拼成了一�?—�?实测那句变成"最�?2.9s @ t=0.1s"�?                        #   �?2.9s 其实�?t=7.3s。假数字比不说更糟�?                        _mid = [x for x in _v if 0.6 < float(x.get("at") or 0)
                                and float(x.get("at") or 0) + float(x.get("seconds") or 0)
                                < float(x.get("clip_seconds") or 1e9) - 0.6]
                        _head = [x for x in _v if float(x.get("at") or 0) <= 0.6]
                        _tail = [x for x in _v if x not in _mid and x not in _head]
                        _bits = []
                        if _mid:
                            _w = max(_mid, key=lambda x: float(x.get("seconds") or 0))
                            _bits.append(f"片中 t={_w.get('at')}s �?"
                                         f"{_w.get('seconds')}s 几乎不动")
                        if _head:
                            _w2 = max(_head, key=lambda x: float(x.get("seconds") or 0))
                            _bits.append(f"片头 {_w2.get('seconds')}s 起幅偏静")
                        if _tail:
                            _w3 = max(_tail, key=lambda x: float(x.get("seconds") or 0))
                            _bits.append(f"片尾定住 {_w3.get('seconds')}s"
                                         f"（t={_w3.get('at')}s 起）")
                        _parts.append(f"第{_k}�?" + "�?.join(_bits))
                    _n_mid = sum(1 for f in _fz
                                 if 0.6 < float(f.get("at") or 0)
                                 and float(f.get("at") or 0) + float(f.get("seconds") or 0)
                                 < float(f.get("clip_seconds") or 1e9) - 0.6)
                    res.warnings.append(
                        f"�?片段内部停顿：{len(_fz)} 处「几乎不动」的段落（其�?{_n_mid} �?
                        f"在片段中间）—�?" + "�?.join(_parts) + "�?
                        f"�?*不是**接缝问题（接缝可能完全看不出来），是模型生成时自�?
                        f"卡住了，观众的感受是「画面停了几秒」。片中卡住建议重新生成该镜�?
                        f"或把长镜头拆成两镜；片头偏静若紧接在转场之后，多数已被溶解盖住�?)
                # �?纹理统一的结果也要报：用户要能看�?为什么这次更像一个片�?�?                _tx = _hm.get("texture") or {}
                _rb, _ra = _tx.get("ratio_before"), _tx.get("ratio_after")
                if _rb is not None and _ra is not None and _rb > 1.15:
                    res.warnings.append(
                        f"纹理统一：各段高频纹理能量相�?{_rb:.2f} �?�?"
                        f"{_ra:.2f} 倍（1.0 = 各段纹理完全一致）�?
                        f"手段是逐段等化微细�?+ 统一叠加颗粒�?
                        f"让不同模型出来的片段看起来像同一批胶片�?)
                if _hm.get("error"):
                    res.warnings.append(
                        f"接缝预处理失败（已按原片拼接，接缝可能不平滑）：{_hm['error']}")
            except Exception as e:
                logger.warning("逐切点转场失败，回退统一转场�?s", e)
                res.warnings.append(f"逐切点转场失败，已回退统一 {transition} 转场：{e}")
                await _concat(clip_paths, merged, transition=transition,
                              transition_sec=transition_sec)
                cplan = []
        else:
            await _concat(clip_paths, merged, transition=transition,
                          transition_sec=transition_sec)
    except Exception as e:
        res.errors.append(str(e))
        return res

    # �?字幕时间轴映射：转场让成片比 Σdur �?(n�?)×t 秒�?    #   不做这一步，从第二个镜头起所有字幕都会整体提前、越往后偏越多
    #   —�?这正�?语音字幕和画面对不上"的机制之一�?    if cplan and srt_entries:
        try:
            from core.continuity import final_timeline as _ftl
            real_durs = [await probe_duration(c) or 0 for c in clip_paths]
            # �?�?`cplan_actual`（实际生效的转场时长），不是 `cplan`�?            #   自适应摊薄会把硬切改成溶解，用旧计划会让字幕整体偏后（见上面的注释）�?            tl = _ftl(shots[:len(clip_paths)], cplan_actual or cplan, real_durs)
            by_shot = {s["index"]: s for s in tl["shots"]}
            remapped = []
            for e in srt_entries:
                b = by_shot.get(e.get("_shot"))
                if not b:
                    continue
                x = dict(e)
                # �?关键：end 必须夹到**该段真实片长**之内�?                #   分镜标称 10s、实际片段只�?5.6s 时，不夹的话字幕�?                #   一直显示到下一个镜头里去（实测字幕 0�?0s 盖住了第 2 个镜头）�?                limit = b["local_duration"]
                x["start"] = round(b["film_start"] + min(float(e["start"]), limit), 3)
                x["end"] = round(b["film_start"]
                                 + max(min(float(e["end"]), limit), min(float(e["start"]), limit) + 0.3), 3)
                x.pop("_shot", None)
                remapped.append(x)
            if remapped:
                srt_entries = remapped
                if tl["timeline_shrink"] > 0.01:
                    res.warnings.append(
                        f"字幕已按转场补偿映射到成片时间轴"
                        f"（成�?{tl['total']}s，转场吃�?{tl['timeline_shrink']}s�?)
        except Exception as e:
            logger.warning("字幕时间轴映射失败，按未补偿时间输出�?s", e)
            srt_entries = [{k: v for k, v in e.items() if k != "_shot"}
                           for e in srt_entries]
            res.warnings.append(f"字幕时间轴映射失败（可能有轻微偏移）：{e}")
    else:
        srt_entries = [{k: v for k, v in e.items() if k != "_shot"} for e in srt_entries]

    # ── 3. 字幕 ──
    current = merged
    if srt_entries:
        # 用统一的字幕模块写 SRT：它会把每条 end 夹到下一句起点之前�?        # 并保证最短显示时长（不夹的话两句字幕会重叠、屏幕上一起闪）�?        try:
            from core.subtitle import build_srt as _build_srt
            res.srt_path = _build_srt(srt_entries, os.path.join(out_dir, "subtitles.srt"))
        except Exception as e:
            logger.warning("统一字幕模块写入失败，回退基础写入�?s", e)
            res.srt_path = write_srt(srt_entries, os.path.join(out_dir, "subtitles.srt"))
        if burn_subtitles:
            step(0.85, "烧录字幕�?)
            burned = os.path.join(work, "burned.mp4")
            ok, note = await _burn(merged, res.srt_path, burned, w, h)
            if ok:
                current = burned
            else:
                res.warnings.append(f"字幕烧录失败（已保留 subtitles.srt 旁挂文件）：{note}")
        else:
            res.warnings.append("按设置跳过字幕烧录，仅生�?subtitles.srt")
    else:
        res.warnings.append("没有生成任何字幕（可能未启用配音�?)

    # ══════════════════════════════════════════════════════════════
    # ★★ 配音轨：�?*成片绝对时间**把每一镜的音轨摆好，混成一�?    # ══════════════════════════════════════════════════════════════
    # 为什么必须这样做（而不是把音轨烧进每个片段）：
    #   `assemble.normalize_picture` **明确 `-an` 丢掉音轨** —�?中间产物一律无音轨
    #   （避免素材原声串味、避�?concat 流不一致），设计上音频就是"最后一次性挂上�?    #   共同原点 t=0 �?天然同步"。所以片段自带的音轨**活不到成�?*�?    #   必须在这里按绝对时间重建一条�?    #   而在此之�?compose 从来没建过这条轨：`mux_film` 两次调用传的都是
    #   `audio=""`，于是成片实测只有一条视频流 —�?**整条片子是无声的**�?    #   位置�?`continuity.final_timeline`：它和字幕映射用的是同一套（转场吃掉�?    #   时长会被算进去），否则声音与画面会从第二个镜头起越差越多�?    voice_track = ""
    dur_mapped = await probe_duration(merged) or 0.0
    if with_voice:
        starts: Dict[int, float] = {}
        if cplan:
            try:
                from core.continuity import final_timeline as _ftl2
                _rd = [await probe_duration(c) or 0 for c in clip_paths]
                # �?同样必须�?实际生效的转�?——配音与字幕要用**同一�?*映射
                _tl = _ftl2(shots[:len(clip_paths)], cplan_actual or cplan, _rd)
                starts = {int(s["index"]): float(s.get("film_start") or 0.0)
                          for s in (_tl.get("shots") or [])}
            except Exception as e:
                logger.warning("配音轨时间映射失败（�?0 起点排列）：%s", e)
        placements = []
        _cursor = 0.0
        for ci in clip_infos:
            if not ci.audio or not os.path.exists(ci.audio):
                _cursor += float(ci.duration or 0)
                continue
            st = starts.get(int(ci.index), _cursor)
            placements.append({"path": ci.audio, "start": round(st, 3),
                               "span": float(ci.duration or 0), "speed": 1.0,
                               # 画面裁掉 h 秒死�?�?声音同量前移，否则这一镜声画错位�?                               # 但若配音�?*已经随画面剪�?*（`audio_precut`�?                               # 即中间冻结段那次剪切），就不能再 skip：那会重复平移�?                               "skip": (0.0 if getattr(ci, "audio_precut", False)
                                        else float(getattr(ci, "trim_head", 0.0) or 0.0)),
                               "text": "", "character": ""})
            _cursor = st + float(ci.duration or 0)
        if placements:
            try:
                from core.voicecast import assemble_track as _assemble
                _vt = os.path.join(work, "voice_track.m4a")
                _tr = _assemble(placements, max(dur_mapped, 1.0), _vt)
                if _tr.get("ok"):
                    voice_track = _tr["path"]
                    res.warnings.append(
                        f"配音已挂上：{len(placements)} 个分镜的音轨按成片时间轴混成一�?
                        f"（{len(clip_infos) - len(placements)} 个分镜没有配音）"
                        if len(placements) < len(clip_infos) else
                        f"配音已挂上：{len(placements)} 个分镜的音轨按成片时间轴混成一�?)
                else:
                    res.warnings.append(f"配音轨混音失败：{_tr.get('error')}")
            except Exception as e:
                logger.exception("配音轨混音失�?)
                res.warnings.append(f"配音轨混音失败：{e}")
        elif with_voice:
            res.warnings.append("没有可用的配音音轨（各镜都没合成出配音）")

    # ── 4. 声音：BGM 避让人声 + 整体响度归一�?──
    #
    # 旧实现直接用 `mix_bgm`（amix 相加）：既没有避让（人一说话音乐糊上�?    # 压住对白），也没有响度归一化（不同 TTS 厂商电平差近 10dB�?    # 一段响一段轻）。用户说�?特别不协�?这两条都占�?    dur_now = await probe_duration(current) or 0
    if bgm_path and os.path.exists(bgm_path):
        step(0.92, "混入背景音乐（自动避让人声）�?)
        mixed = os.path.join(work, "with_bgm.mp4")
        try:
            from core.assemble import mux_film
            mr = await mux_film(current, voice_track, mixed, bgm=bgm_path,
                                bgm_volume=bgm_volume, duck=True,
                                loudness_norm=True, total_duration=dur_now)
            if mr.get("ok") and os.path.exists(mixed) and os.path.getsize(mixed) > 1024:
                current = mixed
                if mr.get("ducked"):
                    res.warnings.append("背景音乐已做「人声优先」自动避�?+ 响度归一�?)
            else:
                res.warnings.append("BGM 混流未产出文件，已跳�?)
        except Exception as e:
            res.warnings.append(f"BGM 混流失败（已跳过）：{e}")
    elif with_voice and voice_track:
        # 有配音无 BGM：仍做一次响度归一化，避免各段配音忽大忽小
        step(0.92, "统一整体响度�?)
        normd = os.path.join(work, "loudness.mp4")
        try:
            from core.assemble import mux_film
            mr = await mux_film(current, voice_track, normd, bgm="", duck=False,
                                loudness_norm=True, total_duration=dur_now)
            if mr.get("ok") and os.path.exists(normd) and os.path.getsize(normd) > 1024:
                current = normd
        except Exception as e:
            logger.warning("响度归一化失败（不影响出片）�?s", e)

    # ── 5. 落地 ──
    final = os.path.join(out_dir, output_name)
    try:
        if os.path.abspath(current) != os.path.abspath(final):
            shutil.copy2(current, final)
    except Exception as e:
        res.errors.append(f"写入成片失败：{e}")
        return res

    res.output_path = final
    res.duration = await probe_duration(final) or timeline_cursor
    if not os.path.exists(final) or os.path.getsize(final) < 1024:
        res.errors.append("成片文件未生成或过小")
        return res

    # ══════════════════════════════════════════════════════════════
    # ★★ 最后再量一�?*成片本身**：全片有没有"某一帧突然跳一�?
    # ══════════════════════════════════════════════════════════════
    # 前面所有的接缝处理（归一�?/ 调色 / 转场摊薄 / 纹理统一 / 裁重复头�?    # 最终都要落�?这条成片看起来连不连�?上。这里给出那个数字，
    # 用户在成片结果里就能看到，而不是只有一个视频文件让他自己猜�?    try:
        from core import seamfix as _sf
        fm = await _sf.film_report(final)
        res.film_metrics = fm
        if fm.get("ok"):
            # �?语境很重要：**换场硬切会被算进「全片最猛的一帧�?*�?            #   实测同一部片子：4 处硬�?�?spike 10.65�? �?0.9s 溶解 �?spike 1.45�?            #   只报数字会让人以�?硬切版是坏的"，而换场硬切其实是最基本�?            #   电影语法 —�?指标只是看不�?换场"这件事。所以有硬切时必须说清�?            _extra = ""
            if n_hard_cuts:
                _extra = (f"（本片有 {n_hard_cuts} �?*换场硬切**：它们会被计�?
                          f"「全片最大跳变」—�?那是「切一刀」，不是接缝缺陷�?
                          f"想改用溶解就�?scene_transition=dissolve�?)
            res.warnings.append(
                f"成片连贯度：全片单帧最大跳�?{fm['max_delta']}�?
                f"自身常态上�?{fm['p99_delta']}，比�?{fm['spike_ratio']}"
                f"（≈1 表示全片最猛的一帧只是普通运动、接缝看不出来）—�?"
                f"{fm['verdict']}{_extra}")
    except Exception as e:
        logger.warning("成片连贯度测量失败（不影响出片）�?s", e)

    res.ok = True
    step(1.0, f"成片完成：{os.path.basename(final)}（{res.duration:.1f}s�?)
    return res
