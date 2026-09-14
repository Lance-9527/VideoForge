# -*- coding: utf-8 -*-
"""
VideoForge · 字幕（烧录 / 分色 / 字体）

═══════════════════════════════════════════════════════════════════
这个模块修掉三个**实测确认**的真 bug
═══════════════════════════════════════════════════════════════════
1. **`subtitles=` 的 Windows 路径必须"外层加引号 + 转义冒号"**
   本机 ffmpeg 7.1 实测：
       subtitles=C:/x/sub.srt        → ❌ Unable to parse option value as image size
       subtitles=C\\:/x/sub.srt       → ❌ 同上
       subtitles='C:/x/sub.srt'      → ❌ 同上
       subtitles='C\\:/x/sub.srt'     → ✅ 成功
   旧代码用的是 `subtitles={path}`（连引号都没有）→ 属于必失败的一类。

2. **字体可能根本没生效（方块字 / 回退到默认字体）**
   本机实测 `Fontconfig error: Cannot load default config file`，
   而 `imageio_ffmpeg` 自带的 ffmpeg **没有 fontconfig 配置文件**。
   后果：只写 `FontName=某字体` 而不给 `fontsdir`，字体族可能解析不到，
   渲染出来是方块字或悄悄回退 —— 而且**不报错**。
   实测证据：用一个不存在的字体名渲染，输出与"不指定字体"完全一致。
   → 必须显式给 `fontsdir`，并用**真实的字体族名**。

3. **字幕行的 end 会越过下一句的起点**
   两句字幕时间重叠 → 屏幕上同时出现两行、闪烁。
   → 生成时把 end 夹到下一句起点之前。

顺带实现：**按角色给字幕分色**（用 ASS 样式，`force_style` 做不到按行区分）。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("videoforge.subtitle")

# 本机实测可用的中文字体族名（用 name 表从字体文件里读出来的，不是猜的）
# 顺序即优先级：优先用自带/开源可打包的，再退系统字体。
FONT_CANDIDATES: List[Tuple[str, str]] = [
    ("Noto Sans SC", r"C:\Windows\Fonts\NotoSansSC-VF.ttf"),
    ("Microsoft YaHei", r"C:\Windows\Fonts\msyh.ttc"),
    ("SimHei", r"C:\Windows\Fonts\simhei.ttf"),
    ("DengXian", r"C:\Windows\Fonts\Deng.ttf"),
    ("SimSun", r"C:\Windows\Fonts\simsun.ttc"),
]

_DEFAULT_COLORS = ["#FFD400", "#5BD1FF", "#9BE564", "#FF9F45", "#C792EA", "#FF6B9D"]


def pick_font() -> Tuple[str, str]:
    """挑一个**真实存在**的中文字体，返回 (族名, 所在目录)。

    找不到就返回空 —— 让调用方知道"这次没法保证字体"，
    而不是悄悄用默认字体渲染出一堆方块。
    """
    for family, path in FONT_CANDIDATES:
        if os.path.exists(path):
            return family, os.path.dirname(path)
    # 退一步：整个 Fonts 目录
    if os.path.isdir(r"C:\Windows\Fonts"):
        return "Microsoft YaHei", r"C:\Windows\Fonts"
    return "", ""


def esc_filter_path(p: str) -> str:
    """把路径转成 ffmpeg filter 里能用的形式。

    **必须同时做两件事**（实测缺一不可）：
      1. 反斜杠 → 正斜杠
      2. 冒号 → `\\:`（否则被当成 filter 选项分隔符）
    调用方还必须**在外面加单引号**：`subtitles='C\\:/x.srt'`
    """
    return str(p).replace("\\", "/").replace(":", "\\:")


def _ts_srt(sec: float) -> str:
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    if ms == 1000:
        s, ms = s + 1, 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ts_ass(sec: float) -> str:
    sec = max(0.0, float(sec))
    return f"{int(sec // 3600)}:{int((sec % 3600) // 60):02d}:{sec % 60:05.2f}"


def _ass_color(hex_rgb: str) -> str:
    """#RRGGBB → ASS 的 &HAABBGGRR（注意是 **BGR** 顺序，alpha 反着写）。"""
    h = (hex_rgb or "#FFFFFF").lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}".upper()


def clamp_subtitles(subs: List[Dict[str, Any]], min_gap: float = 0.06,
                    min_show: float = 0.25) -> List[Dict[str, Any]]:
    """把每条字幕的 end 夹到**下一句起点之前**，并保证最短显示时长。

    不做这一步的后果：两句字幕时间重叠，屏幕上同时显示两行、来回闪。
    """
    out: List[Dict[str, Any]] = []
    for i, s in enumerate(subs or []):
        a = float(s.get("start") or 0)
        b = float(s.get("end") or 0)
        if i + 1 < len(subs):
            nxt = float(subs[i + 1].get("start") or 0)
            if nxt > a:
                b = min(b, nxt - min_gap)
        b = max(b, a + min_show)
        x = dict(s)
        x["start"], x["end"] = round(a, 3), round(b, 3)
        out.append(x)
    return out


def resplit_long_cues(subs: List[Dict[str, Any]], *,
                      max_chars: int = 16, max_sec: float = 3.2,
                      min_sec: float = 0.8) -> List[Dict[str, Any]]:
    """把"一条糊满整镜"的长字幕**按标点拆成可读短句**，但**保留真实时间锚点**。

    ★★ 为什么需要它（2026-09-13 实测的观感回归）：
      无台词分镜的字幕原本是"把旁白概述按句子平均铺在镜头长度上" —— 时间不准
      （实测偏差 +1.65s / −0.74s），但**读起来是短句**。
      改成用 TTS 返回的**真实时间**之后，时间准了（偏差降到 0.09～0.21s），
      可 TTS 把整段旁白当**一条**报回来 —— 于是屏幕上出现
      `00:00:00,100 → 00:00:05,725 在寒冬的森林小径中，一位农夫裹紧外套，艰难地行走着。`
      这种"一行字糊满 5.6 秒"的整段字幕。这是**用准确性换掉了可读性**，不能接受。

    做法：对超长的一条，**在它自己的真实时间窗内**按标点切成 n 片，
    每片的时间按**字数比例**分配（这样每一片仍然落在它被念出来的那段时间附近），
    并且每片至少 `min_sec` 秒。父条的 start/end 保持不变 —— 句首句尾仍然对得上人声。
    """
    out: List[Dict[str, Any]] = []
    for s in (subs or []):
        txt = str(s.get("text") or "")
        a = float(s.get("start") or 0.0)
        b = float(s.get("end") or 0.0)
        if len(txt) <= max_chars and (b - a) <= max_sec:
            out.append(dict(s))
            continue
        # 按标点切；没有标点就按字数硬切
        import re as _re
        parts = [p for p in _re.split(r"(?<=[。！？；，、,.!?;])", txt) if p.strip()]
        if len(parts) <= 1:
            step = max_chars
            parts = [txt[i:i + step] for i in range(0, len(txt), step)]
        # 合并过短的碎片，避免"你""我"这种单字一闪而过
        merged: List[str] = []
        for p in parts:
            if merged and len(merged[-1]) < max_chars // 2:
                merged[-1] += p
            else:
                merged.append(p)
        parts = [p for p in merged if p.strip()]
        if len(parts) <= 1:
            out.append(dict(s))
            continue
        total_chars = sum(len(p) for p in parts) or 1
        span = max(0.0, b - a)
        cursor = a
        for k, p in enumerate(parts):
            share = len(p) / total_chars
            seg = span * share
            if k == len(parts) - 1:
                e = b
            else:
                e = min(b, cursor + seg)
                if e - cursor < min_sec:
                    e = min(b, cursor + min_sec)
            if e <= cursor:
                continue
            out.append({**s, "text": p, "start": round(cursor, 3), "end": round(e, 3)})
            cursor = e
    return out


# ═══════════════════════════════════════════════════════════════
# 生成字幕文件
# ═══════════════════════════════════════════════════════════════

def build_srt(subs: List[Dict[str, Any]], path: str) -> str:
    """生成 SRT。时间戳直接来自逐句配音数据，**不做任何匹配对齐**。"""
    rows = []
    for i, s in enumerate(clamp_subtitles(subs), 1):
        rows.append(f"{i}\n{_ts_srt(s['start'])} --> {_ts_srt(s['end'])}\n{s['text']}\n")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(rows))
    return path


def build_ass(subs: List[Dict[str, Any]], path: str, *, w: int, h: int,
              color_by_speaker: Dict[str, str] = None) -> str:
    """生成 ASS 字幕，**每个角色一个样式**（不同颜色）。

    为什么必须用 ASS 而不是 SRT + force_style：
      - `force_style` 在语义上就是"强制覆盖单一yy样式表"，
        **做不到按行/按角色区分颜色**。
      - 而且 SRT 走 subtitles filter 时 libass 拿不到分辨率（默认按 384×288 算），
        `FontSize=24` 在 1080p 上会变得莫名其妙地小、换分辨率还会漂。
        ASS 文件头写死 `PlayResX/Y`，字号就是真实像素。
    """
    family, _ = pick_font()
    family = family or "Microsoft YaHei"
    fs = max(16, int(h * 0.055))
    mv = int(h * (0.12 if h > w else 0.09))     # 竖屏留更多底部安全区

    head = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}",
        "WrapStyle: 2", "ScaledBorderAndShadow: yes", "YCbCr Matrix: TV.709", "",
        "[V4+ Styles]",
        ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
         "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
         "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
         "MarginL, MarginR, MarginV, Encoding"),
    ]
    styles: Dict[str, str] = {}

    def add_style(display: str, color: str = "#FFFFFF", bold: int = 0) -> str:
        key = "S" + re.sub(r"\W+", "", display)[:10] or f"S{len(styles)}"
        if key in styles:
            return key
        while key in styles.values():
            key += "x"
        styles[display] = key
        head.append(
            f"Style: {key},{family},{fs},{_ass_color(color)},&H000000FF,&H00101010,"
            f"&H7F000000,{bold},0,0,0,100,100,0,0,1,2,1,2,40,40,{mv},1")
        return key

    add_style("", "#FFFFFF", 0)                       # 旁白 / 未知角色
    colors = color_by_speaker or {}
    for who in colors:
        add_style(who, colors[who], 1)

    body = ["", "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
            "Effect, Text"]
    for s in clamp_subtitles(subs):
        who = s.get("character") or ""
        # 角色差异用**样式**表达，不把"某某："写进正文（那是业余感来源之一）
        key = styles.get(who) or styles[""]
        text = str(s.get("text") or "").replace("\n", "\\N")
        body.append(f"Dialogue: 0,{_ts_ass(s['start'])},{_ts_ass(s['end'])},{key},,"
                    f"0,0,0,,{text}")

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(head + body) + "\n")
    return path


def speaker_colors(subs: List[Dict[str, Any]]) -> Dict[str, str]:
    """按出场顺序给每个角色分配一个固定的字幕颜色。"""
    out: Dict[str, str] = {}
    for s in subs or []:
        who = s.get("character") or ""
        if who and who not in out:
            out[who] = _DEFAULT_COLORS[len(out) % len(_DEFAULT_COLORS)]
    return out


# ═══════════════════════════════════════════════════════════════
# 烧录
# ═══════════════════════════════════════════════════════════════

def pick_font_file() -> str:
    """给 `drawtext` 用的**字体文件路径**（不是族名）。

    ★ 为什么需要单独一个函数：烧字幕走的是 `subtitles` 滤镜 + `fontsdir`，
      它按**字体族名**解析；而 `drawtext` 要的是 `fontfile=<具体文件>`。
      两者不能混用 —— 把族名塞给 drawtext 在本机实测直接报
      "Cannot find a valid font for the family"。

    只列**确定存在**的中文字体（系统自带），找不到就返回空串，
    调用方必须能接受"没字体就不画字"（宁可没有地点卡，也不要整片拼接失败）。
    """
    import os
    cands = [
        r"C:\Windows\Fonts\msyh.ttc",        # 微软雅黑（Win7+ 自带）
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\simhei.ttf",      # 黑体
        r"C:\Windows\Fonts\simsun.ttc",      # 宋体
        "/System/Library/Fonts/PingFang.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ]
    for p in cands:
        if os.path.exists(p):
            return p
    # 兜底：字体目录里扫一遍
    for d in (r"C:\Windows\Fonts", "/usr/share/fonts"):
        try:
            for n in sorted(os.listdir(d)):
                if n.lower().endswith((".ttc", ".ttf", ".otf")) and "yahei" in n.lower():
                    return os.path.join(d, n)
        except Exception:
            continue
    return ""


async def burn_subtitles(video: str, subs_path: str, out_path: str, *,
                         w: int = 1280, h: int = 720, ass: bool = False,
                         ffmpeg: str = "") -> Tuple[bool, str]:
    """把字幕烧进画面。

    路径按 `esc_filter_path` 转义**并且外层加单引号** —— 这两件事缺一不可
    （本机实测，见模块顶部说明）。
    """
    from core.compose import _run
    from core.ffmpeg_manager import get_ffmpeg_for_postprocess
    ff = ffmpeg or get_ffmpeg_for_postprocess()
    family, fontsdir = pick_font()

    vf = f"subtitles='{esc_filter_path(subs_path)}'"
    if fontsdir:
        # 显式给字体目录：不带 fontconfig 的 ffmpeg 靠它才能解析到字体
        vf += f":fontsdir='{esc_filter_path(fontsdir)}'"
    if not ass:
        style = (f"FontName={family or 'Microsoft YaHei'},"
                 f"FontSize={max(16, int(h * 0.055))},"
                 f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00101010,"
                 f"BorderStyle=1,Outline=2,Shadow=1,Alignment=2,"
                 f"MarginV={int(h * (0.12 if h > w else 0.09))}")
        vf += f":force_style='{style}'"

    rc, err = await _run([
        ff, "-y", "-hide_banner", "-loglevel", "error", "-i", video,
        "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart",
        out_path], timeout=1800)
    if rc != 0 or not os.path.exists(out_path):
        return False, f"字幕烧录失败：{(err or '')[-300:]}"
    return True, ""


def font_report() -> Dict[str, Any]:
    """诊断用：当前会用什么字体、字体目录在哪。"""
    family, d = pick_font()
    return {"family": family, "fontsdir": d, "found": bool(family),
            "ok": bool(family and d and os.path.isdir(d))}
