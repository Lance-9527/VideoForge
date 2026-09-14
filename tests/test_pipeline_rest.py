# -*- coding: utf-8 -*-
"""跑流水线剩下的步骤（voice/subtitle/bgm/aspect/trim）—— 全部本地，零 API 成本"""
import io
import json
import sys
import time
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:8766"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(method, path, body=None, timeout=180):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with OPENER.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read().decode("utf-8", "replace")[:600]}
    except Exception as e:
        return {"_err": f"{type(e).__name__}: {e}"}


PID = sys.argv[1]
STEPS = [
    ("voice", {"voice_id": "edge:zh-CN-XiaoxiaoNeural", "rate": 1.0, "keep_original": True}),
    ("subtitle", {"font_size": 24, "font_color": "white"}),
    ("aspect", {"aspect": "9:16"}),
    ("trim", {"seconds": 20}),
]

results = {}
for key, params in STEPS:
    print("═" * 70)
    print(f"▶ {key}  params={params}")
    t0 = time.time()
    r = call("POST", f"/api/projects/{PID}/pipeline/run", {"step": key, "params": params},
             timeout=60)
    if not (r.get("data") or {}).get("started"):
        print("  !! 未启动:", json.dumps(r, ensure_ascii=False)[:300])
        results[key] = "NOT_STARTED"
        continue
    last = ""
    while True:
        time.sleep(2)
        d = (call("GET", f"/api/projects/{PID}/pipeline/status").get("data") or {})
        msg = f"[{time.time()-t0:6.1f}s] {d.get('percent')}% {d.get('message')}"
        if msg != last:
            print("  ", msg)
            last = msg
        if not d.get("running"):
            res = d.get("result") or {}
            results[key] = "OK" if res.get("ok") else f"FAIL: {res.get('error')}"
            print("   →", results[key], "|", res.get("note"), "| dur=", res.get("duration"))
            for w in (res.get("warnings") or []):
                print("   ⚠", str(w)[:180])
            break
        if time.time() - t0 > 900:
            results[key] = "TIMEOUT"
            break

print("\n" + "═" * 70)
print("汇总")
for k, v in results.items():
    print(f"   {k:9s} {v}")

print("\n" + "═" * 70)
print("最终状态 + 产物 HTTP 校验")
st = (call("GET", f"/api/projects/{PID}/pipeline").get("data") or {})
for s in st.get("steps", []):
    print(f"   {s['key']:9s} done={s['done']!s:5s} stale={s['stale']!s:5s} "
          f"note={s['note'][:34]!r} dur={s['duration']}")
    if s.get("preview_url"):
        try:
            with OPENER.open(BASE + s["preview_url"], timeout=30) as rr:
                h = rr.read(2048)
            print(f"             HTTP {rr.status} {rr.headers.get('content-type')} "
                  f"{rr.headers.get('content-length')} bytes  magic={h[4:12]!r}")
        except Exception as e:
            print(f"             !! {type(e).__name__}: {e}")

print("\n" + "═" * 70)
print("finalize")
fin = call("POST", f"/api/projects/{PID}/pipeline/finalize", {}, timeout=180)
print(json.dumps(fin.get("data") or fin, ensure_ascii=False, indent=1)[:600])
print("\nfinal 下载头校验:")
try:
    req = urllib.request.Request(BASE + f"/api/projects/{PID}/final?download=1")
    with OPENER.open(req, timeout=30) as rr:
        print("   ", rr.status, rr.headers.get("Content-Type"),
              "|", rr.headers.get("Content-Disposition"),
              "|", rr.headers.get("Content-Length"), "bytes")
except Exception as e:
    print("   !!", e)
