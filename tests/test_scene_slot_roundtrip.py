# -*- coding: utf-8 -*-
"""场景固定槽位的持久化回归（建临时场景 → 读 → 改 → 删）"""
import io
import json
import sys
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:8766"
O = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PID = sys.argv[1]


def req(method, path, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with O.open(r, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read().decode("utf-8", "replace")[:400]}
    except Exception as e:
        return {"_err": f"{type(e).__name__}: {e}"}


NAME = "__槽位自检场景__"
SLOTS = {
    "terrain": "碎石土路，两侧枯草与残雪",
    "architecture": "半塌的土坯院墙，木门已歪斜",
    "furnishings": "院中一口枯井、两棵光秃的槐树",
    "atmosphere": "清冷、萧瑟",
    "color_tone": "冷灰蓝为主，唯一暖色是窗内油灯光",
}

print("① 新建带槽位的场景")
c = req("POST", "/api/scenes", {
    "project_id": PID, "name": NAME, "location_type": "outdoor",
    "time_of_day": "day", "weather": "snowy", "lighting": "overcast",
    "description": "碎石土路，两侧枯草与残雪；半塌的土坯院墙",
    "reference_features": {"slots": SLOTS},
})
print("   ", json.dumps(c, ensure_ascii=False)[:200])
sid = (c.get("data") or {}).get("id")
if not sid:
    print("!! 创建失败（可能是场景表还没有 reference_features 列）")
    raise SystemExit(1)

print("\n② 读回逐项比对")
lst = (req("GET", f"/api/projects/{PID}/scenes").get("data") or {}).get("scenes") or []
me = next((x for x in lst if x["id"] == sid), None)
rf = me.get("reference_features") if me else None
if isinstance(rf, str):
    rf = json.loads(rf)
got = (rf or {}).get("slots") or {}
same = sum(1 for k, v in SLOTS.items() if got.get(k) == v)
for k, v in SLOTS.items():
    print(f"     {'✅' if got.get(k) == v else '❌'} {k:14s} {got.get(k)!r}")
print(f"   一致: {same}/{len(SLOTS)}")

print("\n③ 更新槽位")
SLOTS["terrain"] = "结冰的土路，脚印清晰"
req("PATCH", f"/api/scenes/{sid}", {"reference_features": {"slots": SLOTS}})
lst = (req("GET", f"/api/projects/{PID}/scenes").get("data") or {}).get("scenes") or []
me = next((x for x in lst if x["id"] == sid), None)
rf = me.get("reference_features")
if isinstance(rf, str):
    rf = json.loads(rf)
print("   改后 terrain:", ((rf or {}).get("slots") or {}).get("terrain"))

print("\n④ 清理")
print("   ", json.dumps(req("DELETE", f"/api/scenes/{sid}"), ensure_ascii=False)[:120])
lst = (req("GET", f"/api/projects/{PID}/scenes").get("data") or {}).get("scenes") or []
print("   残留:", "无" if not any(x["id"] == sid for x in lst) else "!! 还在")
