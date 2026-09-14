# -*- coding: utf-8 -*-
"""用真实 Key 测试硅基流动 TTS 能不能通（费用约 ¥0.0005）"""
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
O = urllib.request.build_opener(urllib.request.ProxyHandler({}))
KEY = os.environ.get("SF_KEY", "")
if not KEY:
    print("!! 没拿到 Key")
    raise SystemExit(1)
print("Key:", KEY[:10] + "…" + KEY[-6:], f"(len={len(KEY)})")

BASE = "https://api.siliconflow.cn/v1"

print("\n" + "═" * 72)
print("① 先用 Key 拉模型清单（验证 Key 本身有效）")
try:
    req = urllib.request.Request(
        f"{BASE}/models?sub_type=text-to-speech",
        headers={"Authorization": f"Bearer {KEY}"})
    with O.open(req, timeout=60) as r:
        d = json.loads(r.read().decode())
    models = d.get("data") or []
    print(f"   HTTP {r.status} · 文本转语音模型 {len(models)} 个")
    for m in models[:10]:
        print("    -", m.get("id"))
except urllib.error.HTTPError as e:
    print(f"   HTTP {e.code}: {e.read(200).decode('utf-8','replace')}")
    models = []

print("\n" + "═" * 72)
print("② 真跑一次 TTS（CosyVoice2-0.5B / alex）")
TEXT = "深夜的城市霓虹灯下，侦探踏入未知的暗巷。"
t0 = time.time()
body = json.dumps({
    "model": "FunAudioLLM/CosyVoice2-0.5B",
    "voice": "alex",
    "input": TEXT,
    "response_format": "mp3",
    "speed": 1.0,
}).encode()
req = urllib.request.Request(
    f"{BASE}/audio/speech", data=body, method="POST",
    headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
try:
    with O.open(req, timeout=180) as r:
        audio = r.read()
    el = time.time() - t0
    print(f"   HTTP {r.status} · {len(audio)} bytes · {el:.1f}s")
    print(f"   Content-Type: {r.headers.get('content-type')}")
    print(f"   前 16 字节: {audio[:16].hex()}")
    ok = len(audio) > 2000
    is_mp3 = audio[:3] == b"ID3" or audio[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")
    print(f"   看起来是合法 MP3: {is_mp3}")
    if ok:
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_sf_tts_test.mp3")
        with open(out, "wb") as f:
            f.write(audio)
        print(f"   已保存: {out}")
        # 用 ffmpeg 验证能解码 + 拿到真实时长
        try:
            import subprocess
            from imageio_ffmpeg import get_ffmpeg_exe
            rc = subprocess.run(
                [get_ffmpeg_exe(), "-hide_banner", "-i", out],
                capture_output=True, text=True, encoding="utf-8", errors="replace")
            m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", rc.stderr or "")
            if m:
                dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                print(f"   ffmpeg 解码通过 · 时长 {dur:.2f} 秒")
                print(f"   原文 {len(TEXT)} 字 → {len(TEXT)/dur:.1f} 字/秒（正常语速约 4-5）")
            au = re.findall(r"Audio: (\w+)", rc.stderr or "")
            print(f"   音频编码: {au}")
        except Exception as e:
            print("   ffmpeg 校验跳过:", e)
except urllib.error.HTTPError as e:
    print(f"   HTTP {e.code}: {e.read(400).decode('utf-8','replace')}")

print("\n" + "═" * 72)
print("③ 再试一个中文女声（anna），确认音色参数真的起作用")
body = json.dumps({
    "model": "FunAudioLLM/CosyVoice2-0.5B", "voice": "anna",
    "input": "这是第二个音色的测试。", "response_format": "mp3", "speed": 1.0,
}).encode()
req = urllib.request.Request(
    f"{BASE}/audio/speech", data=body, method="POST",
    headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
try:
    with O.open(req, timeout=180) as r:
        a2 = r.read()
    print(f"   anna: HTTP {r.status} · {len(a2)} bytes · 与 alex 不同={a2 != audio}")
except urllib.error.HTTPError as e:
    print(f"   anna HTTP {e.code}: {e.read(300).decode('utf-8','replace')}")
