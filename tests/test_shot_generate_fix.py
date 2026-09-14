# -*- coding: utf-8 -*-
"""验证：分镜页「生成」按钮的原始调用不再报 body.shot_id: Field required，
并且真的走完"真实模型 → 失败自动回退本地合成"全链路。"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))
BASE = "http://127.0.0.1:8899"
PID = sys.argv[1] if len(sys.argv) > 1 else None
ok = True


def check(name, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name, (" — " + str(detail)) if detail else ""))


def call(method, path, payload=None, timeout=300):
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


if not PID:
    st, r = call("GET", "/api/projects")
    projs = (r.get("data") or {}).get("projects") or []
    # 找有分镜的项目
    for p in projs:
        st, r2 = call("GET", "/api/projects/%s/shots" % p["id"])
        if ((r2.get("data") or {}).get("shots") or []):
            PID = p["id"]
            print("用项目:", (p.get("title") or p.get("name") or "")[:30], PID)
            break
if not PID:
    print("没有含分镜的项目"); sys.exit(1)

st, r = call("GET", "/api/projects/%s/shots" % PID)
shots = (r.get("data") or {}).get("shots") or []
print("分镜数:", len(shots))
sid = shots[0]["id"]
print("测试分镜:", sid, "provider=", shots[0].get("model_provider"), "model=", shots[0].get("model_name"))

# 1) 用前端原本的请求体（只有 num_candidates）—— 这正是报 body.shot_id 的那次调用
print("\n--- ① 复现前端原始调用 {num_candidates:4} ---")
st, r = call("POST", "/api/shots/%s/generate" % sid, {"num_candidates": 4})
msg = json.dumps(r, ensure_ascii=False)
check("不再报 body.shot_id: Field required", "shot_id" not in msg or "Field required" not in msg,
      "HTTP %s %s" % (st, msg[:160]))
check("请求被接受", st == 200 and (r.get("data") or {}).get("started") is not False,
      "HTTP %s" % st)

# 2) 轮询到结束
print("\n--- ② 轮询生成进度 ---")
last = ""
final = None
t0 = time.time()
while time.time() - t0 < 1500:
    st, r = call("GET", "/api/shots/%s/render/status" % sid)
    d = r.get("data") or {}
    line = "%5.1f%%  %s" % (d.get("percent", 0), d.get("message", ""))
    if line != last:
        print("      " + line)
        last = line
    if not d.get("running"):
        final = d
        break
    time.sleep(3)

res = (final or {}).get("result") or {}
print("\n  used =", (final or {}).get("used"))
check("最终产出视频", bool(res.get("ok")), "source=%s duration=%s" % (res.get("source"), res.get("duration")))
for w in (res.get("warnings") or []):
    print("      warning:", str(w)[:220])
if (final or {}).get("error"):
    print("      error:", str(final["error"])[:300])

# 3) 分镜状态 + 候选文件
st, r = call("GET", "/api/shots/%s" % sid)
s2 = r.get("data") or {}
cands = s2.get("candidates") or []
if isinstance(cands, str):
    cands = json.loads(cands)
check("分镜状态 completed", s2.get("status") == "completed", s2.get("status"))
check("登记了候选视频", len(cands) > 0, "%d 个" % len(cands))
if cands:
    p = cands[-1].get("path")
    check("候选文件真实存在", bool(p) and os.path.exists(p),
          "%.2f MB" % ((os.path.getsize(p) / 1048576) if p and os.path.exists(p) else 0))

print()
print("=" * 60)
print("RESULT:", "ALL PASS" if ok else "HAS FAILURES")
sys.exit(0 if ok else 1)
