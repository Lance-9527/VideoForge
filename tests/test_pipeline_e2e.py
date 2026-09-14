# -*- coding: utf-8 -*-
"""端到端测试后期流水线（不调用任何付费视频模型：base 步只用已有分镜/图片 + FFmpeg）"""
import io
import json
import sys
import time
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8766"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(method, path, body=None, timeout=180):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with OPENER.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read().decode("utf-8", "replace")[:600]}
    except Exception as e:
        return {"_err": f"{type(e).__name__}: {e}"}


PID = sys.argv[1] if len(sys.argv) > 1 else "d5815c1e-c641-4f21-a2a4-888388f5157a"

print("═" * 70)
print("项目:", PID)
proj = call("GET", f"/api/projects/{PID}")
print("项目信息:", json.dumps(proj.get("data", {}).get("project", {}), ensure_ascii=False)[:200]
      if proj.get("code") == "000000" else proj)

shots = call("GET", f"/api/projects/{PID}/shots")
sl = (shots.get("data") or {}).get("shots") or []
print(f"分镜数: {len(sl)}")
for s in sl[:8]:
    print(f"   #{s.get('order_index')} {str(s.get('layer1_overview') or '')[:44]!r}"
          f" dur={s.get('duration_seconds')} img={bool(s.get('image_path'))}"
          f" vid={bool(s.get('video_path'))}")

print("\n" + "═" * 70)
print("① 流水线状态（初始）")
st = call("GET", f"/api/projects/{PID}/pipeline")
print(json.dumps(st, ensure_ascii=False, indent=1)[:1800])

print("\n" + "═" * 70)
print("② 跑第 1 步 base（接底片）")
r = call("POST", f"/api/projects/{PID}/pipeline/run",
         {"step": "base", "params": {"transition": "fade", "transition_sec": 0.5,
                                     "prefer_generated": True, "auto_images": False}},
         timeout=60)
print("提交:", json.dumps(r, ensure_ascii=False))
if not (r.get("data") or {}).get("started"):
    print("!! 没能启动")
    raise SystemExit(1)

t0 = time.time()
last = ""
while True:
    time.sleep(2)
    s2 = call("GET", f"/api/projects/{PID}/pipeline/status")
    d = s2.get("data") or {}
    msg = f"[{time.time()-t0:6.1f}s] {d.get('percent')}% {d.get('message')}"
    if msg != last:
        print("  ", msg)
        last = msg
    if not d.get("running"):
        print("\n最终:", json.dumps(d.get("result") or d, ensure_ascii=False, indent=1)[:1200])
        break
    if time.time() - t0 > 900:
        print("!! 超时")
        break

print("\n" + "═" * 70)
print("③ 流水线状态（跑完 base 后）")
st = call("GET", f"/api/projects/{PID}/pipeline")
for s in (st.get("data") or {}).get("steps", []):
    print(f"   {s['key']:9s} done={s['done']!s:5s} stale={s['stale']!s:5s} "
          f"note={s['note']!r} dur={s['duration']} preview={s['preview_url']!r} "
          f"err={s['error'][:80]!r}")

print("\n" + "═" * 70)
print("④ 验证产物可访问（preview_url 真能播）")
for s in (st.get("data") or {}).get("steps", []):
    if not s.get("preview_url"):
        continue
    try:
        req = urllib.request.Request(BASE + s["preview_url"], method="GET")
        with OPENER.open(req, timeout=30) as rr:
            head = rr.read(2048)
            print(f"   {s['key']:9s} HTTP {rr.status} type={rr.headers.get('content-type')} "
                  f"len={rr.headers.get('content-length')} 头部字节={head[:12].hex()}")
    except Exception as e:
        print(f"   {s['key']:9s} !! {type(e).__name__}: {e}")

print("\n" + "═" * 70)
print("⑤ finalize → final.mp4")
fin = call("POST", f"/api/projects/{PID}/pipeline/finalize", {}, timeout=180)
print(json.dumps(fin, ensure_ascii=False, indent=1)[:800])
info = call("GET", f"/api/projects/{PID}/final/info")
print("final/info:", json.dumps(info, ensure_ascii=False, indent=1)[:800])
