# -*- coding: utf-8 -*-
"""完整闭环验收：一句想法 → 剧本 → 分镜 → 一键成片 → 可播放 mp4

这是用户需求的最终验收：普通用户什么都不用配（除已有的 LLM Key），
从零开始走完全程并拿到成片。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("VF_BASE", "http://127.0.0.1:8899")
IDEA = "做一个 30 秒的赛博朋克侦探短片，主角在霓虹雨夜追查一桩失踪案"

ok = True


def check(name, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name, (" — " + str(detail)) if detail else ""))


def call(method, path, payload=None, timeout=600):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"message": e.read().decode()[:300]}


print("=" * 60)
print("闭环验收：一句想法 → 成片")
print("=" * 60)

# 1. 建项目
st, r = call("POST", "/api/projects", {"title": "【闭环验收】赛博朋克侦探", "description": IDEA})
check("创建项目", st == 200 and r.get("code") == "000000", r.get("message", ""))
pid = (r.get("data") or {}).get("id") or (r.get("data") or {}).get("project", {}).get("id")
check("拿到 project_id", bool(pid), pid)
if not pid:
    sys.exit(1)
print("      pid =", pid)

# 2. 生成剧本
t0 = time.time()
st, r = call("POST", "/api/projects/%s/script/generate" % pid, {
    "user_prompt": IDEA, "total_duration_seconds": 30,
    "style": "cinematic", "auto_split_scenes": True,
})
n = len(((r.get("data") or {}).get("script") or {}).get("scenes") or [])
if n == 0:
    # 响应层级可能是 data.scenes；以 GET /script 为准（更权威）
    st2, r2 = call("GET", "/api/projects/%s/script" % pid)
    sc = (r2.get("data") or {}).get("script") or (r2.get("data") or {})
    n = len(sc.get("scenes") or [])
check("生成剧本", st == 200 and n > 0,
      "%d 个场次 · %.1fs%s" % (n, time.time() - t0, "" if n else " | " + str(r.get("message", ""))[:160]))

# 3. 从剧本生成分镜（服务端接口）
st, r = call("POST", "/api/projects/%s/shots/from-script" % pid, {})
shots = ((r.get("data") or {}).get("shots") or [])
check("生成分镜", len(shots) > 0, "%d 个分镜%s" % (len(shots), "" if shots else " | " + str(r.get("message", ""))[:160]))
if not shots:
    sys.exit(1)

# 4. 一键成片（不配任何视频模型 Key）
st, r = call("POST", "/api/projects/%s/compose" % pid, {
    "voice_id": "edge:zh-CN-XiaoxiaoNeural", "with_voice": True,
    "burn_subtitles": True, "auto_images": False,
})
check("启动一键成片", (r.get("data") or {}).get("started") is True, r.get("message", ""))

last = ""
final = None
t0 = time.time()
while time.time() - t0 < 900:
    st, r = call("GET", "/api/projects/%s/compose/status" % pid)
    d = r["data"]
    line = "%5.1f%%  %s" % (d.get("percent", 0), d.get("message", ""))
    if line != last:
        print("      " + line)
        last = line
    if not d.get("running"):
        final = d
        break
    time.sleep(2)

res = (final or {}).get("result") or {}
check("成片完成", bool(res.get("ok")), (final or {}).get("error", ""))
check("有分镜被渲染", (res.get("clip_count") or 0) > 0, res.get("clip_count"))
check("时长 > 5s", (res.get("duration") or 0) > 5, res.get("duration"))

# 5. 成片可播放
st, r = call("GET", "/api/projects/%s/final/info" % pid)
info = r["data"]
check("成片文件存在", info.get("exists") is True)
if info.get("exists"):
    check("大小 > 100KB", info["size_bytes"] > 100_000, "%.2f MB" % (info["size_bytes"] / 1048576))
    req = urllib.request.Request(BASE + info["url"], headers={"Range": "bytes=0-65535"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        head = resp.read()
    check("HTTP 可取回 mp4", b"ftyp" in head[:64], head[8:16])
    print("      成片路径:", info["path"])
print()
print("=" * 60)
print("总耗时: %.0f 秒" % (time.time() - t0))
print("RESULT:", "ALL PASS" if ok else "HAS FAILURES")
sys.exit(0 if ok else 1)
