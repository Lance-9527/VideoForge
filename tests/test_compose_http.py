# -*- coding: utf-8 -*-
"""HTTP 端到端：POST /compose → 轮询 → /final/info → /final 下载校验"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("VF_BASE", "http://127.0.0.1:8899")
PID = sys.argv[1] if len(sys.argv) > 1 else "e62a182d-71ef-49ca-951b-921166c4f6c3"
AUTO_IMAGES = os.environ.get("VF_AUTO_IMAGES", "") == "1"


def call(method, path, payload=None, timeout=120):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


ok = True


def check(name, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name, (" — " + str(detail)) if detail else ""))


# 健康
st, r = call("GET", "/api/health")
check("health", st == 200 and r["data"]["status"] == "ok")
check("ffmpeg 可用", r["data"].get("ffmpeg_available") is True)

# 音色
st, r = call("GET", "/api/voice/voices")
voices = r["data"]["voices"]
check("音色列表非空", len(voices) > 0, "%d 个" % len(voices))
check("含 edge 中文音色", any(v["voice_id"].startswith("edge:zh-CN") for v in voices))

# 启动合成
st, r = call("POST", "/api/projects/%s/compose" % PID, {
    "voice_id": "edge:zh-CN-XiaoxiaoNeural", "with_voice": True,
    "burn_subtitles": True, "bgm_volume": 0.25,
    "auto_images": AUTO_IMAGES,
    "image_provider": "minimax", "image_size": "1280x720",
})
check("POST /compose 已接受", st == 200 and r["data"].get("started") is True, r.get("message", ""))

# 轮询
t0 = time.time()
last = ""
final = None
while time.time() - t0 < 900:
    st, r = call("GET", "/api/projects/%s/compose/status" % PID)
    d = r["data"]
    line = "%5.1f%%  %s" % (d.get("percent", 0), d.get("message", ""))
    if line != last:
        print("      " + line)
        last = line
    if not d.get("running"):
        final = d
        break
    time.sleep(2)

check("合成流程结束", final is not None)
if final:
    res = final.get("result") or {}
    check("结果 ok", bool(res.get("ok")), final.get("error", ""))
    check("分镜数 > 0", (res.get("clip_count") or 0) > 0, res.get("clip_count"))
    check("时长 > 5s", (res.get("duration") or 0) > 5, res.get("duration"))
    if res.get("warnings"):
        print("      warnings:")
        for w in res["warnings"]:
            print("        -", w)

# 成片信息
st, r = call("GET", "/api/projects/%s/final/info" % PID)
info = r["data"]
check("/final/info exists", info.get("exists") is True, info)
if info.get("exists"):
    check("文件 > 100KB", info["size_bytes"] > 100_000, "%.2f MB" % (info["size_bytes"] / 1048576))
    check("时长 > 5s", info.get("duration", 0) > 5, "%.1fs" % info.get("duration", 0))
    check("有播放 url", bool(info.get("url")))

    # 下载前 256KB 验证是真 mp4
    req = urllib.request.Request(BASE + info["url"], headers={"Range": "bytes=0-262143"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        head = resp.read()
        ctype = resp.headers.get("Content-Type", "")
        code = resp.status
    check("/final 可下载", code in (200, 206) and len(head) > 100_000,
          "HTTP %s, %d bytes" % (code, len(head)))
    check("Content-Type 是 video", "video" in ctype, ctype)
    check("是合法 MP4（ftyp box）", b"ftyp" in head[:64], head[8:16])

print()
print("=" * 56)
print("RESULT:", "ALL PASS" if ok else "HAS FAILURES")
sys.exit(0 if ok else 1)
