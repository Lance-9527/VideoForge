# -*- coding: utf-8 -*-
"""隔离验证 BGM 避让人声（ducking）真的生效。

为什么不能用"整段平均音量"来判定：人声比 BGM 高好几个 dB，
把 BGM 压低 18dB，混合后的总电平只变化不到 1dB —— 测不出来。
正确做法是**用带通滤波器只测 BGM 所在的频段**：
  BGM 用 60Hz 纯音（语音在 60Hz 几乎没能量），
  再 bandpass=f=60 测那一小段，得到的就是"BGM 自己的电平"。
"""
import asyncio
import io
import os
import re
import subprocess
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"D:\VideoForge-dev\backend")
from imageio_ffmpeg import get_ffmpeg_exe       # noqa: E402
from core.assemble import mux_film              # noqa: E402

FF = get_ffmpeg_exe()
W = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_duck_test")
os.makedirs(W, exist_ok=True)

PIC = os.path.join(W, "pic.mp4")
VOICE = os.path.join(W, "voice.m4a")
BGM = os.path.join(W, "bgm60.mp3")

# 人声：0-2s 静音、2-5s 说话、5-8s 静音、8-11s 说话、11-14s 静音（14 秒）
# BGM：60Hz 纯音，铺满 14 秒
VOICE_SCRIPT = [
    ("2.0", "1.0", "第一句话在这里说。"),
    ("8.0", "1.0", "第二句话在这里说。"),
]
TOTAL = 14.0


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def band_db(path, start, dur, freq=60, width=8):
    """只测 BGM 频段的电平（隔离出 BGM 自己的音量）。"""
    r = run([FF, "-hide_banner", "-nostats", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
             "-i", path, "-af", f"bandpass=f={freq}:width_type=h:w={width},volumedetect",
             "-f", "null", "-"])
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", r.stderr or "")
    return float(m.group(1)) if m else -999.0


def build_inputs():
    # 纯色画面
    run([FF, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", f"color=c=0x303030:s=640x360:d={TOTAL}:r=30",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-an", PIC])
    # 60Hz BGM
    run([FF, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", f"sine=frequency=60:duration={TOTAL}", "-c:a", "libmp3lame", BGM])
    # 人声：用两段 TTS 放到指定时间点（复用 voicecast 的绝对定位）
    from core.voicecast import assemble_track
    placements = []
    for i, (start, span, text) in enumerate(VOICE_SCRIPT):
        out = os.path.join(W, f"l{i}.mp3")
        if not os.path.exists(out):
            run([FF, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                 "-i", f"sine=frequency=1200:duration=1.0",
                 "-c:a", "libmp3lame", out])   # 用 1.2kHz 纯音模拟"人声占用中高频"
        placements.append({"path": out, "start": float(start), "span": span,
                           "text": text, "speed": 1.0})
    r = assemble_track(placements, TOTAL, VOICE)
    assert r.get("ok"), r
    return placements


async def main():
    print("═" * 74)
    print("准备素材")
    build_inputs()
    print(f"  画面 {PIC}\n  人声 {VOICE}\n  BGM(60Hz) {BGM}")

    print("\n" + "═" * 74)
    print("人声出现的时间：2.0-3.0s、8.0-9.0s；其余时间只有 BGM")
    print("（测量窗口必须**严格落在人声之内**，否则会被周围的静音稀释）")
    results = {}
    for duck in (False, True):
        out = os.path.join(W, f"out_{int(duck)}.mp4")
        await mux_film(PIC, VOICE, out, bgm=BGM, bgm_volume=0.5,
                       duck=duck, loudness_norm=False, total_duration=TOTAL)
        # 只测 60Hz 频段 = BGM 自己的电平
        silent = (band_db(out, 0.3, 1.2) + band_db(out, 4.0, 1.5)
                  + band_db(out, 11.5, 1.3)) / 3
        speech = (band_db(out, 2.15, 0.7) + band_db(out, 8.15, 0.7)) / 2
        results[duck] = (silent, speech)
        print(f"  ducking={duck!s:5s}  BGM静默段 {silent:7.1f} dB   "
              f"BGM说话段 {speech:7.1f} dB   压降 {silent - speech:5.1f} dB")

    print("\n" + "═" * 74)
    print("结论")
    s0, p0 = results[False]
    s1, p1 = results[True]
    drop_off = s1 - p1          # 开了 ducking 之后，说话时 BGM 被压低多少
    ok = drop_off >= 6.0 and abs(s1 - s0) < 1.5
    print(f"  开 ducking 后，说话时 BGM 比静默时低 {drop_off:.1f} dB")
    print(f"  静默段 BGM 是否被误压：{abs(s1-s0):.1f} dB（应 < 1.5）")
    print()
    print("  " + ("✅ ducking 生效：人一说话 BGM 自动让位，不说话时不受影响"
                  if ok else "❌ ducking 没达到预期"))


asyncio.run(main())
