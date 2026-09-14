# -*- coding: utf-8 -*-
"""验证：① 模型名不匹配自动纠正 ② 无 Key 时明确告知（不再静默降级）—— 用 local 模式，不产生 API 费用"""
import json
import os
import sys
import time
import urllib.request

urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))
BASE = "http://127.0.0.1:8899"
ok = True


def check(name, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name, (" — " + str(detail)) if detail else ""))


def call(m, p, pl=None, t=900):
    d = json.dumps(pl).encode() if pl is not None else None
    rq = urllib.request.Request(BASE + p, data=d,
                                headers={"Content-Type": "application/json"}, method=m)
    with urllib.request.urlopen(rq, timeout=t) as r:
        return json.loads(r.read().decode())


def wait(sid, timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        d = call("GET", "/api/shots/%s/render/status" % sid)["data"]
        if not d.get("running"):
            return d
        time.sleep(3)
    return {"running": True}


# 找一个 model_provider=hailuo 但 model_name=kling 的分镜（脏数据）
targets = []
for p in call("GET", "/api/projects")["data"]["projects"]:
    for s in call("GET", "/api/projects/%s/shots" % p["id"])["data"]["shots"]:
        if (s.get("model_provider") == "hailuo" and "kling" in str(s.get("model_name") or "")) \
           or (s.get("model_provider") == "kling"):
            targets.append((p, s))
print("找到待验证分镜 %d 个" % len(targets))
for p, s in targets[:4]:
    print("   %-22s %s / %s  has_key=%s" % ((p.get("title") or "")[:20],
                                            s.get("model_provider"), s.get("model_name"),
                                            s.get("has_api_key")))

check("分镜列表带 has_api_key 字段", all("has_api_key" in s for _, s in targets))

# ① 无 Key 的分镜：本地模式渲染，应给出"没有配置 API Key"的明确说明
nokey = [(p, s) for p, s in targets if not s.get("has_api_key")]
mismatch = [(p, s) for p, s in targets
            if s.get("model_provider") == "hailuo" and "kling" in str(s.get("model_name") or "")]

if mismatch:
    p, s = mismatch[0]
    print("\n--- ① 模型名不匹配纠正（%s / %s）---" % (s.get("model_provider"), s.get("model_name")))
    call("POST", "/api/shots/%s/render" % s["id"], {"mode": "local", "auto_images": False,
                                                    "duration_seconds": 5})
    d = wait(s["id"])
    res = d.get("result") or {}
    ws = " ".join(res.get("warnings") or [])
    check("提示了模型名不匹配并纠正", "不属于厂商" in ws, ws[:150])
    after = call("GET", "/api/shots/%s" % s["id"])["data"]
    check("库里的模型名已纠正", "kling" not in str(after.get("model_name") or ""),
          after.get("model_name"))
else:
    print("\n[SKIP] 没有找到模型名不匹配的分镜")

if nokey:
    p, s = nokey[0]
    print("\n--- ② 无 Key 明确告知（%s / %s）---" % (s.get("model_provider"), s.get("model_name")))
    call("POST", "/api/shots/%s/render" % s["id"], {"mode": "local", "auto_images": False,
                                                    "duration_seconds": 5})
    d = wait(s["id"])
    res = d.get("result") or {}
    print("      used =", d.get("used"), "| message =", str(d.get("message"))[:80])
    check("本地合成成功（不报错）", bool(res.get("ok")))
else:
    print("\n[SKIP] 所有分镜的厂商都配了 Key")

# ③ mode=api 且无 Key 时必须报错而不是静默降级
if nokey:
    p, s = nokey[0]
    print("\n--- ③ mode=api 且无 Key 应明确报错 ---")
    try:
        call("POST", "/api/shots/%s/render" % s["id"], {"mode": "api"})
        check("无 Key 强制 api 时被拒绝", False, "竟然接受了")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        check("无 Key 强制 api 时被拒绝", e.code == 400 and "API Key" in body, "HTTP %s" % e.code)

print()
print("RESULT:", "ALL PASS" if ok else "HAS FAILURES")
sys.exit(0 if ok else 1)
