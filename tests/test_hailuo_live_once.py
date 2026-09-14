# -*- coding: utf-8 -*-
"""真实跑一次海螺（只跑一次，验证 NameError 已修 + 首帧可用）"""
import json
import os
import sys
import time
import urllib.request

urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))
BASE = os.environ.get("VF_BASE", "http://127.0.0.1:8899")
PID = sys.argv[1] if len(sys.argv) > 1 else "caa9904f-2425-41b1-b446-e033207e7e8d"


def c(m, p, pl=None, t=900):
    d = json.dumps(pl).encode() if pl is not None else None
    r = urllib.request.Request(BASE + p, data=d,
                               headers={"Content-Type": "application/json"}, method=m)
    with urllib.request.urlopen(r, timeout=t) as x:
        return json.loads(x.read().decode())


shots = c("GET", "/api/projects/%s/shots" % PID)["data"]["shots"]
# 找一个 hailuo 且已正确纠正模型名的分镜
tgt = None
for s in shots:
    if s.get("model_provider") == "hailuo" and s.get("has_api_key"):
        tgt = s
        break
if not tgt:
    tgt = shots[0]
print("目标分镜:", tgt["id"][:8], tgt.get("model_provider"), tgt.get("model_name"),
      "| has_key =", tgt.get("has_api_key"))

print("\n=== mode=api 强制真实模型（失败会明确报错，不静默降级）===")
st, r = 0, None
try:
    d = c("POST", "/api/shots/%s/render" % tgt["id"],
          {"mode": "api", "duration_seconds": 6, "layers": {"l1": True, "l2": True, "l3": True}})
    sid = tgt["id"]
except urllib.error.HTTPError as e:
    body = e.read().decode("utf-8", "ignore")
    print("  ❌ 提交即被拒：", body[:300])
    sys.exit(1)

print("  已提交，等待模型返回（海螺约 1-2 分钟）…")
t0 = time.time()
last = ""
while time.time() - t0 < 900:
    d = c("GET", "/api/shots/%s/render/status" % sid)["data"]
    line = "%5.1f%%  %s" % (d.get("percent", 0), d.get("message", ""))
    if line != last:
        print("      " + line)
        last = line
    if not d.get("running"):
        break
    time.sleep(3)

res = d.get("result") or {}
print("\n  used =", d.get("used"))
if d.get("error"):
    print("  ❌ error:", str(d["error"])[:400])
print("  ok =", res.get("ok"), "| source =", res.get("source"),
      "| provider =", res.get("provider"), "| duration =", res.get("duration"))
for w in (res.get("warnings") or []):
    print("  warning:", str(w)[:250])

s2 = c("GET", "/api/shots/%s" % sid)["data"]
cands = s2.get("candidates") or []
if isinstance(cands, str):
    cands = json.loads(cands)
lastc = cands[-1] if cands else {}
print("\n  最新候选: source=%s path=%s" % (lastc.get("source"), (lastc.get("path") or "")[-60:]))
print("  文件存在:", os.path.exists(lastc.get("path") or ""),
      "%.2f MB" % ((os.path.getsize(lastc["path"]) / 1048576) if lastc.get("path") and os.path.exists(lastc["path"]) else 0))

ok = bool(res.get("ok")) and res.get("source") == "api"
print()
print("RESULT:", "PASS — 海螺真实模型出片成功" if ok else "未成功（见上面的 error/warning）")
sys.exit(0 if ok else 1)
