# -*- coding: utf-8 -*-
"""用真实 Key 端到端测试 SiliconFlow TTS（走项目自己的 dispatcher，不是裸 HTTP）"""
import asyncio
import io
import os
import re
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"D:\VideoForge-dev\backend")

KEY = os.environ.get("SF_KEY", "")
assert KEY, "需要 SF_KEY"

from core.voice.base import TTSRequest                      # noqa: E402
from core.voice import dispatcher as voice_dispatcher       # noqa: E402
import core.voice as V                                      # noqa: E402

SETTINGS = {"tts_api_keys": {"siliconflow": KEY}}
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_sf_tts")


async def main():
    print("═" * 72)
    print("① provider 列表（Key 已生效吗）")
    for p in await V.list_providers(SETTINGS):
        if p["name"] in ("siliconflow", "edge", "minimax", "dashscope", "silent"):
            print(f"   {p['name']:12s} {p['display_name']:24s} "
                  f"需Key={p['requires_api_key']!s:5s} 已配={p['has_api_key']}")

    print("\n" + "═" * 72)
    print("② 硅基流动音色清单（含克隆音色）")
    vs = [v for v in await V.list_voices(SETTINGS) if v.voice_id.startswith("siliconflow:")]
    for v in vs:
        print(f"   {v.voice_id:58s} {v.display_name}")
    if not vs:
        print("   !! 一个都没有")

    print("\n" + "═" * 72)
    print("③ 逐个音色真跑一遍（每个约 12 字）")
    TEXT = "深夜的城市霓虹灯下，侦探踏入未知的暗巷。"
    os.makedirs(OUT, exist_ok=True)
    results = []
    for v in vs:
        short = v.voice_id.rsplit(":", 1)[-1]
        out = os.path.join(OUT, f"{short}.mp3")
        t0 = time.time()
        r = await voice_dispatcher.synthesize(
            TTSRequest(text=TEXT, voice_id=v.voice_id, output_path=out, rate=1.0),
            SETTINGS)
        el = time.time() - t0
        if r.success and os.path.exists(out):
            size = os.path.getsize(out)
            results.append((short, True, size, el, ""))
            print(f"   ✅ {short:12s} {size:7d} bytes  {el:5.1f}s  {r.duration_seconds:.2f}s(估)")
        else:
            results.append((short, False, 0, el, r.error or ""))
            print(f"   ❌ {short:12s} {el:5.1f}s  {(r.error or '')[:150]}")

    ok = sum(1 for x in results if x[1])
    print(f"\n   成功 {ok}/{len(results)}")

    print("\n" + "═" * 72)
    print("④ 用 ffmpeg 校验音频真的能解码（不是空壳/错误页）")
    good = [x for x in results if x[1]]
    if good:
        import subprocess
        from imageio_ffmpeg import get_ffmpeg_exe
        ff = get_ffmpeg_exe()
        for name, _, size, _, _ in good[:3]:
            p = os.path.join(OUT, f"{name}.mp3")
            rc = subprocess.run([ff, "-hide_banner", "-i", p],
                                capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
            m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", rc.stderr or "")
            au = re.search(r"Audio: ([^,]+), (\d+) Hz", rc.stderr or "")
            if m:
                dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                print(f"   ✅ {name:10s} 时长 {dur:.2f}s · {au.group(1) if au else '?'} "
                      f"{au.group(2) if au else '?'}Hz · 语速 {len(TEXT)/dur:.1f} 字/秒")
            else:
                print(f"   ❌ {name}: ffmpeg 读不出时长")

    print("\n" + "═" * 72)
    print("⑤ 换语速 1.5x，确认参数真的生效")
    out = os.path.join(OUT, "speed15.mp3")
    r = await voice_dispatcher.synthesize(
        TTSRequest(text=TEXT, voice_id=vs[0].voice_id, output_path=out, rate=1.5),
        SETTINGS)
    if r.success:
        import subprocess
        from imageio_ffmpeg import get_ffmpeg_exe
        rc = subprocess.run([get_ffmpeg_exe(), "-hide_banner", "-i", out],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
        m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", rc.stderr or "")
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0
        print(f"   1.5x 时长 {dur:.2f}s（1.0x 应更长 → 参数生效）")
    else:
        print("   失败:", r.error)

    print("\n" + "═" * 72)
    print("⑥ 故意用错的音色名，确认报错可读（错误提示是否帮到用户）")
    r = await voice_dispatcher.synthesize(
        TTSRequest(text="测试", voice_id="siliconflow:FunAudioLLM/CosyVoice2-0.5B:不存在的音色",
                   output_path=os.path.join(OUT, "bad.mp3")),
        SETTINGS)
    print(f"   success={r.success}")
    print(f"   error={r.error}")
    print(f"   含可读提示: {'音色名要写成' in (r.error or '')}")


asyncio.run(main())
