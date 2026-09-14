# -*- coding: utf-8 -*-
"""TTS 链路真实自检：edge-tts 合成 + 试听 + 字幕时间轴"""
import asyncio
import os
import sys

BK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
sys.path.insert(0, BK)
os.chdir(BK)

from core.voice.base import TTSRequest          # noqa: E402
from core.voice import dispatcher                # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tts_probe.mp3")


async def main():
    print("--- providers ---")
    for p in await dispatcher.list_providers({}):
        print("   %-14s %-28s requires_key=%-5s has_key=%s"
              % (p["name"], p["display_name"], p["requires_api_key"], p["has_api_key"]))

    print("\n--- edge 音色列表 ---")
    voices = await dispatcher.list_voices({})
    print("   共 %d 个音色" % len(voices))
    for v in voices[:5]:
        print("   ", v)

    print("\n--- 真实合成 ---")
    req = TTSRequest(
        text="这是一次配音链路自检。VideoForge 现在可以自己说话了。",
        voice_id="edge:zh-CN-XiaoxiaoNeural",
        output_path=OUT,
    )
    r = await dispatcher.synthesize(req, {})
    print("   success      :", r.success)
    print("   audio_path   :", r.audio_path)
    print("   duration     :", r.duration_seconds)
    print("   provider     :", r.provider, "/", r.voice_id)
    print("   error        :", r.error)
    subs = getattr(r, "subtitles", None)
    print("   字幕条数     :", len(subs) if subs else 0)
    if subs:
        for s in subs[:4]:
            print("      ", s)
    if r.audio_path and os.path.exists(r.audio_path):
        print("   文件大小     :", os.path.getsize(r.audio_path), "bytes")

    print("\n--- 无配音模式 ---")
    r2 = await dispatcher.synthesize(
        TTSRequest(text="无配音测试", voice_id="silent:no-voice",
                   output_path=OUT.replace(".mp3", "_silent.wav")), {})
    print("   success:", r2.success, "| path:", r2.audio_path, "| duration:", r2.duration_seconds,
          "| error:", r2.error)


asyncio.run(main())
