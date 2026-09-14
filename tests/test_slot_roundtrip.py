# -*- coding: utf-8 -*-
"""验证固定槽位能真的存进 DB 并原样读回（建临时角色 → 读 → 删，不留垃圾）"""
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


NAME = "__槽位自检角色__"
SLOTS = {
    "face_shape": "方脸，颧骨明显", "skin": "偏黄，粗糙", "eyes": "细长眼，锐利",
    "hair": "短寸黑发", "distinctive": "左眉骨旧疤", "height_build": "中等，精瘦",
    "costume_top": "灰蓝粗布对襟上衣", "costume_bottom": "深灰长裤",
    "costume_shoes": "黑色旧布鞋", "costume_accessories": ["布制绑腿", "帆布挎包"],
    "costume_palette": ["灰蓝", "深灰"], "signature": "旧疤+洗白上衣",
}

print("① 新建带槽位的角色")
c = req("POST", "/api/characters", {
    "project_id": PID, "name": NAME, "age": "40 岁上下",
    "description": "方脸，颧骨明显，偏黄，粗糙", "costume_main": "灰蓝粗布对襟上衣",
    "reference_features": {"slots": SLOTS},
})
cid = (c.get("data") or {}).get("character", {}).get("id") or (c.get("data") or {}).get("id")
print("   ", json.dumps(c, ensure_ascii=False)[:220])
if not cid:
    print("!! 创建失败")
    raise SystemExit(1)

print("\n② 读回并逐项比对")
lst = (req("GET", f"/api/projects/{PID}/characters").get("data") or {}).get("characters") or []
me = next((x for x in lst if x["id"] == cid), None)
if not me:
    print("!! 找不到刚建的角色")
    raise SystemExit(1)
rf = me.get("reference_features")
print("   reference_features 类型:", type(rf).__name__)
if isinstance(rf, str):
    rf = json.loads(rf)
got = (rf or {}).get("slots") or {}
same = 0
for k, v in SLOTS.items():
    ok = got.get(k) == v
    same += ok
    print(f"     {'✅' if ok else '❌'} {k:22s} {got.get(k)!r}")
print(f"   槽位一致: {same}/{len(SLOTS)}")

print("\n③ 更新槽位（模拟用户手改）→ 再读回")
SLOTS["face_shape"] = "国字脸，下颌更宽"
up = req("PATCH", f"/api/characters/{cid}", {
    "reference_features": {"slots": SLOTS, "personality": "沉稳寡言"},
})
print("   ", json.dumps(up, ensure_ascii=False)[:200])
lst = (req("GET", f"/api/projects/{PID}/characters").get("data") or {}).get("characters") or []
me = next((x for x in lst if x["id"] == cid), None)
rf = me.get("reference_features")
if isinstance(rf, str):
    rf = json.loads(rf)
print("   改后 face_shape:", ((rf or {}).get("slots") or {}).get("face_shape"))
print("   personality:", (rf or {}).get("personality"))

print("\n④ 清理")
d = req("DELETE", f"/api/characters/{cid}")
print("   ", json.dumps(d, ensure_ascii=False)[:150])
lst = (req("GET", f"/api/projects/{PID}/characters").get("data") or {}).get("characters") or []
print("   残留:", "无" if not any(x["id"] == cid for x in lst) else "!! 还在")
