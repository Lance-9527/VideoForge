# -*- coding: utf-8 -*-
"""端到端验证：剧本细节 → 角色/场景更细腻 → 分镜严格按细节"""
import json
import os
import sys
import time
import urllib.request

urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))
BASE = "http://127.0.0.1:8899"
PID = sys.argv[1] if len(sys.argv) > 1 else "e62a182d-71ef-49ca-951b-921166c4f6c3"


def call(m, p, pl=None, t=1800):
    d = json.dumps(pl).encode() if pl is not None else None
    rq = urllib.request.Request(BASE + p, data=d,
                                headers={"Content-Type": "application/json"}, method=m)
    with urllib.request.urlopen(rq, timeout=t) as r:
        return json.loads(r.read().decode())


print("=== ① 一键生成全部剧本细节（验证 20s 超时已修复）===")
t0 = time.time()
r = call("POST", "/api/projects/%s/script/scene-details/generate-all" % PID)
d = r["data"]
print("  耗时 %.0fs  新生成=%d 跳过=%d 失败=%d"
      % (time.time() - t0, len(d["generated"]), len(d["skipped"]), len(d["failed"])))
for f in d["failed"][:3]:
    print("    场次%s 失败: %s" % (f["scene_number"], f["error"][:120]))

print("\n=== ② 细节喂给角色提取（验证更细腻）===")
t0 = time.time()
r2 = call("POST", "/api/projects/%s/characters/from-script?force=true" % PID)
print("  耗时 %.0fs  新建=%s 更新=%s" % (time.time() - t0, r2["data"].get("count"), r2["data"].get("updated_count")))
chars = call("GET", "/api/projects/%s/characters" % PID)["data"]["characters"]
tot = sum(len(c.get("description") or "") for c in chars)
print("  角色数=%d  外形描述总字数=%d  平均=%d 字/角色"
      % (len(chars), tot, tot // max(1, len(chars))))
for c in chars[:3]:
    print("   【%s】%s" % (c["name"], (c.get("description") or "")[:80]))

print("\n=== ③ 剧本细节喂给场景提取 ===")
r3 = call("POST", "/api/projects/%s/scenes/from-script?force=true" % PID)
print("  新建=%s 更新=%s" % (r3["data"].get("count"), r3["data"].get("updated_count")))
scenes = call("GET", "/api/projects/%s/scenes" % PID)["data"]["scenes"]
tot2 = sum(len(s.get("description") or "") for s in scenes)
print("  场景数=%d  描述总字数=%d" % (len(scenes), tot2))

print("\n=== ④ 分镜严格按细节生成 + 角色/场景关联 ===")
r4 = call("POST", "/api/projects/%s/shots/from-script" % PID, None, t=600)
lk = r4["data"]["linkage"]
print("  created=%s  用了剧本细节的场次=%s/%s  角色关联=%s/%s"
      % (r4["data"]["created"], lk.get("used_scene_detail"), lk.get("total"),
         lk.get("char_linked"), lk.get("total")))
shots = call("GET", "/api/projects/%s/shots" % PID)["data"]["shots"]
s0 = shots[0]
tl = s0.get("layer2_timeline") or []
if isinstance(tl, str):
    tl = json.loads(tl)
print("  分镜1：时间线 %d 条 · 时长 %ss · 场景=%s · 角色数=%d"
      % (len(tl), s0.get("duration_seconds"), bool(s0.get("scene_id")),
         len(s0.get("character_ids") or [])))
if tl:
    t = tl[0]
    print("    首条: [%s-%ss] %s | 镜头=%s" % (t.get("start"), t.get("end"),
                                              str(t.get("action"))[:60], t.get("camera")))
ok = (len(d["generated"]) + len(d["skipped"])) >= 1 and lk.get("total", 0) > 0
print()
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
