# -*- coding: utf-8 -*-
"""测 bgm 步骤：上传 BGM → 跑 bgm → 确认把下游标为 stale"""
import io
import json
import mimetypes
import os
import sys
import time
import urllib.request
import uuid

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:8766"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post_multipart(path, filepath, field="file"):
    boundary = "----vfboundary" + uuid.uuid4().hex
    fn = os.path.basename(filepath)
    ctype = mimetypes.guess_type(fn)[0] or "application/octet-stream"
    with open(filepath, "rb") as f:
        payload = f.read()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{fn}"\r\n'
        f"Content-Type: {ctype}\r\n\r\n"
    ).encode() + payload + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        BASE + path, data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with OPENER.open(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read().decode("utf-8", "replace")[:600]}
    except Exception as e:
        return {"_err": f"{type(e).__name__}: {e}"}


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
BGM = sys.argv[2]

print("① 上传 BGM")
up = post_multipart(f"/api/projects/{PID}/pipeline/bgm", BGM)
print("  ", json.dumps(up, ensure_ascii=False)[:300])
assert up.get("code") == "000000", "上传失败"

print("\n② 上传后应把 bgm 及下游标成 stale")
st = (call("GET", f"/api/projects/{PID}/pipeline").get("data") or {})
for s in st.get("steps", []):
    print(f"   {s['key']:9s} done={s['done']!s:5s} stale={s['stale']!s:5s} note={s['note'][:28]!r}")
print("   bgm_path:", st.get("bgm_path"))

print("\n③ 跑 bgm 步骤")
t0 = time.time()
r = call("POST", f"/api/projects/{PID}/pipeline/run",
         {"step": "bgm", "params": {"bgm_volume": 0.25, "fade_in": 1.5, "fade_out": 2.0,
                                    "bgm_path": st.get("bgm_path")}}, timeout=60)
print("   提交:", json.dumps(r, ensure_ascii=False)[:200])
last = ""
while True:
    time.sleep(2)
    d = (call("GET", f"/api/projects/{PID}/pipeline/status").get("data") or {})
    m = f"[{time.time()-t0:6.1f}s] {d.get('percent')}% {d.get('message')}"
    if m != last:
        print("  ", m)
        last = m
    if not d.get("running"):
        res = d.get("result") or {}
        print("   →", "OK" if res.get("ok") else "FAIL", "|", res.get("note") or res.get("error"))
        break
    if time.time() - t0 > 600:
        print("   !! 超时")
        break

print("\n④ 最终状态 + 产物校验")
st = (call("GET", f"/api/projects/{PID}/pipeline").get("data") or {})
for s in st.get("steps", []):
    line = (f"   {s['key']:9s} done={s['done']!s:5s} stale={s['stale']!s:5s} "
            f"note={s['note'][:30]!r} dur={s['duration']}")
    print(line)
    if s.get("preview_url"):
        try:
            with OPENER.open(BASE + s["preview_url"], timeout=30) as rr:
                h = rr.read(2048)
            print(f"             HTTP {rr.status} {rr.headers.get('content-length')} bytes "
                  f"magic={h[4:8]!r}")
        except Exception as e:
            print("             !!", e)
