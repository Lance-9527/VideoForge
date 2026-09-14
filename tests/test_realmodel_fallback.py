# -*- coding: utf-8 -*-
"""验证：配了真实模型 Key 也能出片 —— 真实模型成功则用它，失败必须自动回退本地合成"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("VF_BASE", "http://127.0.0.1:8899")
PID = "e62a182d-71ef-49ca-951b-921166c4f6c3"
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
            return e.code, {"message": "?"}


st, r = call("GET", "/api/projects/%s/shots" % PID)
shots = (r.get("data") or {}).get("shots") or []
print("分镜数:", len(shots))
for s in shots:
    print("  #%s %s/%s  %ss  status=%s" % (s.get("order_index"), s.get("model_provider"),
                                           s.get("model_name"), s.get("duration_seconds"), s.get("status")))

# 找 seedance 分镜；没有就临时改一个
target = next((s for s in shots if s.get("model_provider") == "seedance"), None)
if not target and shots:
    target = shots[0]
    call("PATCH", "/api/shots/%s" % target["id"],
         {"model_provider": "seedance", "model_name": "doubao-seedance-1-0-pro-250528"})
    st, r = call("GET", "/api/shots/%s" % target["id"])
    target = r["data"]
    print("\n（已把分镜1 临时切到 seedance 做验证）")

check("找到 seedance 分镜", bool(target), target.get("model_name") if target else "")
if not target:
    sys.exit(1)

print("\n=== 用 mode=auto 渲染（有 Key → 先试真实模型）===")
st, r = call("POST", "/api/shots/%s/render" % target["id"],
             {"mode": "auto", "duration_seconds": 5, "with_voice": True, "auto_images": False})
check("已接受", (r.get("data") or {}).get("started") is True, r.get("message", ""))

last = ""
final = None
t0 = time.time()
while time.time() - t0 < 1200:
    st, r = call("GET", "/api/shots/%s/render/status" % target["id"])
    d = r["data"]
    line = "%5.1f%%  %s" % (d.get("percent", 0), d.get("message", ""))
    if line != last:
        print("      " + line)
        last = line
    if not d.get("running"):
        final = d
        break
    time.sleep(3)

res = (final or {}).get("result") or {}
print("\nused =", (final or {}).get("used"))
check("最终产出了视频", bool(res.get("ok")),
      "source=%s duration=%s" % (res.get("source"), res.get("duration")))
if res.get("warnings"):
    print("  warnings:")
    for w in res["warnings"]:
        print("    -", w)
if (final or {}).get("error"):
    print("  error:", final["error"])

# 无论是 api 还是 local，最终都要有一个可播放文件
st, r = call("GET", "/api/shots/%s" % target["id"])
s2 = r["data"]
cands = s2.get("candidates") or []
if isinstance(cands, str):
    cands = json.loads(cands)
check("分镜已登记候选视频", len(cands) > 0, "%d 个" % len(cands))
check("分镜状态为 completed", s2.get("status") == "completed", s2.get("status"))
if cands:
    p = cands[-1].get("path")
    check("候选文件真实存在", bool(p) and os.path.exists(p),
          "%.2f MB" % ((os.path.getsize(p) / 1048576) if p and os.path.exists(p) else 0))
    url = cands[-1].get("url")
    if url:
        with urllib.request.urlopen(BASE + url, timeout=60) as resp:
            head = resp.read(4096)
            check("可播放（HTTP + MP4）", resp.status in (200, 206) and b"ftyp" in head[:64],
                  "HTTP %s" % resp.status)

# 还要验证 mode=api 时失败能给出清晰原因（而不是静默）
print("\n=== mode=api 单独验证（应明确报错或成功）===")
st, r = call("POST", "/api/shots/%s/render" % target["id"], {"mode": "api", "duration_seconds": 5})
if (r.get("data") or {}).get("started"):
    t0 = time.time()
    while time.time() - t0 < 600:
        st, r = call("GET", "/api/shots/%s/render/status" % target["id"])
        if not r["data"].get("running"):
            break
        time.sleep(3)
    d = r["data"]
    res2 = d.get("result") or {}
    if res2.get("ok"):
        check("mode=api 成功（真实模型可用）", True, "source=%s" % res2.get("source"))
    else:
        err = d.get("error") or ""
        check("mode=api 失败时给出可读原因", len(err) > 10, err[:160].replace("\n", " "))
        check("mode=api 失败时提示了怎么办", ("建议" in err) or ("本地合成" in err), "")
else:
    check("mode=api 有明确响应", bool(r.get("message")), r.get("message", ""))

print()
print("=" * 60)
print("RESULT:", "ALL PASS" if ok else "HAS FAILURES")
sys.exit(0 if ok else 1)
