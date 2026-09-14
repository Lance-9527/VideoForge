# -*- coding: utf-8 -*-
"""验收：在「生成」页选不同音色 → 生成同一分镜 → 成片里的配音真的不同。

后端 narration 一致、画面一致，唯一变量是 voice_id。
如果两个成片的音轨完全一样，说明音色下拉还是摆设。
"""
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:8766"
O = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PID = sys.argv[1]


def req(method, path, body=None, timeout=900):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with O.open(r, timeout=timeout) as x:
            return json.loads(x.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read().decode("utf-8", "replace")[:400]}
    except Exception as e:
        return {"_err": f"{type(e).__name__}: {e}"}


shots = (req("GET", f"/api/projects/{PID}/shots").get("data") or {}).get("shots") or []
if not shots:
    print("!! 该项目没有分镜")
    raise SystemExit(1)
sid = shots[0]["id"]
print("用分镜:", sid, "|", str(shots[0].get("layer1_overview") or "")[:50])

VOICES = [
    ("edge:zh-CN-XiaoxiaoNeural", "晓晓 · 温柔女声（免费默认）"),
    ("siliconflow:FunAudioLLM/CosyVoice2-0.5B:claire", "温柔女声 claire（硅基流动）"),
    ("minimax:female-yujie", "御姐音色（MiniMax）"),
]

results = []
for vid, label in VOICES:
    print("\n" + "═" * 72)
    print(f"▶ 用音色：{label}\n   {vid}")
    payload = {"mode": "local", "voice_id": vid, "with_voice": True,
               "auto_images": False, "duration_seconds": 8}
    r = req("POST", f"/api/shots/{sid}/render", payload)
    rd = r.get("data") or {}
    if not rd.get("started"):
        print("   !! 未启动:", json.dumps(r, ensure_ascii=False)[:200])
        continue
    t0 = time.time()
    while True:
        time.sleep(2)
        st = (req("GET", f"/api/shots/{sid}/render/status").get("data") or {})
        if not st.get("running"):
            break
        if time.time() - t0 > 600:
            print("   !! 超时")
            break
    res = st.get("result") or {}
    if not res.get("ok"):
        print("   ❌ 失败:", st.get("error") or res.get("errors"))
        continue
    path = res.get("path")
    size = os.path.getsize(path) if path and os.path.exists(path) else 0
    # 把音轨单独抽出来算哈希 —— 画面相同，只有声音不同
    audio_hash = audio_dur = None
    if path and os.path.exists(path):
        from imageio_ffmpeg import get_ffmpeg_exe
        ff = get_ffmpeg_exe()
        aout = path + ".aac"
        subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error",
                        "-i", path, "-vn", "-c:a", "copy", aout],
                       capture_output=True)
        if os.path.exists(aout):
            with open(aout, "rb") as f:
                audio_hash = hashlib.md5(f.read()).hexdigest()[:12]
            rc = subprocess.run([ff, "-hide_banner", "-i", path],
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace")
            m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", rc.stderr or "")
            if m:
                audio_dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    results.append((label, vid, size, audio_hash, audio_dur, res.get("narration")))
    print(f"   ✅ 成片 {size/1024:.0f} KB · {audio_dur:.2f}s · 音轨指纹 {audio_hash}")
    print(f"      朗读文本: {str(res.get('narration') or '')[:60]}")

print("\n" + "═" * 72)
print("结论")
hashes = [x[3] for x in results if x[3]]
for label, vid, size, h, d, n in results:
    print(f"   {label:32s} {size/1024:6.0f} KB  {d:5.2f}s  音轨={h}")
print()
if len(set(hashes)) == len(hashes) and len(hashes) >= 2:
    print("   ✅ 三个音色产生的音轨各不相同 —— 音色下拉**真的起作用**")
else:
    print("   ❌ 音轨有重复 —— 音色可能没生效")
