# -*- coding: utf-8 -*-
"""测「跳过」：模型自带对白/字幕时，跳过 ②配音 和 ③字幕，链路仍然能出成片。"""
import io
import json
import sys
import time
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:8766"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PID = sys.argv[1]


def call(method, path, body=None, timeout=180):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with OPENER.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read().decode("utf-8", "replace")[:400]}
    except Exception as e:
        return {"_err": f"{type(e).__name__}: {e}"}


def show(tag):
    st = (call("GET", f"/api/projects/{PID}/pipeline").get("data") or {})
    print(f"  ── {tag} ──")
    for s in st.get("steps", []):
        flag = "跳过" if s["skipped"] else ("完成" if s["done"] else ("脏" if s["stale"] else "未跑"))
        print(f"     {s['key']:9s} [{flag}] skippable={s['skippable']!s:5s} "
              f"note={s['note'][:30]!r}")
    return st


print("═" * 72)
print("① 重置 → 只跑 base")
call("POST", f"/api/projects/{PID}/pipeline/reset", {})
r = call("POST", f"/api/projects/{PID}/pipeline/run",
         {"step": "base", "params": {"transition": "none", "prefer_generated": True,
                                     "auto_images": False}})
while True:
    time.sleep(2)
    d = (call("GET", f"/api/projects/{PID}/pipeline/status").get("data") or {})
    if not d.get("running"):
        break
print("   base:", (d.get("result") or {}).get("ok"))
show("跑完 base")

print("\n" + "═" * 72)
print("② 跳过 ②配音（模拟『模型自带对白』）")
r = call("POST", f"/api/projects/{PID}/pipeline/skip",
         {"step": "voice", "reason": "模型自带对白，不需要配音"})
print("  ", json.dumps(r.get("data") or r, ensure_ascii=False)[:300])
show("跳过配音后")

print("\n" + "═" * 72)
print("③ 跳过 ③字幕")
r = call("POST", f"/api/projects/{PID}/pipeline/skip",
         {"step": "subtitle", "reason": "模型自带字幕"})
print("  ", json.dumps(r.get("data") or r, ensure_ascii=False)[:300])
show("跳过字幕后")

print("\n" + "═" * 72)
print("④ ④bgm 也跳过 → 直接 finalize")
print("  ", json.dumps((call("POST", f"/api/projects/{PID}/pipeline/skip",
                             {"step": "bgm", "reason": "不要 BGM"}).get("data")) or {},
                        ensure_ascii=False)[:200])
print("  ", json.dumps((call("POST", f"/api/projects/{PID}/pipeline/skip",
                             {"step": "aspect", "reason": "画幅已合适"}).get("data")) or {},
                        ensure_ascii=False)[:200])
print("  ", json.dumps((call("POST", f"/api/projects/{PID}/pipeline/skip",
                             {"step": "trim", "reason": "时长已合适"}).get("data")) or {},
                        ensure_ascii=False)[:200])
st = show("全部跳过")
nxt = next((s for s in st.get("steps", []) if not s["done"]), None)
print("   还有未完成步骤吗:", nxt["key"] if nxt else "没有 → 可以直接出成片")

fin = call("POST", f"/api/projects/{PID}/pipeline/finalize", {}, timeout=180)
print("\n   finalize:", json.dumps(fin.get("data") or fin, ensure_ascii=False)[:400])
info = (call("GET", f"/api/projects/{PID}/final/info").get("data") or {})
print("   final 存在:", info.get("exists"), "| 大小", info.get("size_bytes"),
      "| 时长", info.get("duration"))

print("\n" + "═" * 72)
print("⑤ 取消跳过 ②配音 → 应回到未跑，并且下游变脏")
r = call("POST", f"/api/projects/{PID}/pipeline/skip", {"step": "voice", "undo": True})
print("  ", json.dumps(r.get("data") or r, ensure_ascii=False)[:200])
show("取消跳过配音后")

print("\n" + "═" * 72)
print("⑥ 不该跳过的步骤（①接底片）必须拒绝跳过")
r = call("POST", f"/api/projects/{PID}/pipeline/skip", {"step": "base"})
print("  ", json.dumps(r, ensure_ascii=False)[:300])
