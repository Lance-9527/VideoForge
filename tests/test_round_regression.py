# -*- coding: utf-8 -*-
"""本轮全部改动的回归冒烟测试（零 API 成本：只读接口 + 本地 FFmpeg 步骤）"""
import io
import json
import sys
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
# 用法: python test_round_regression.py [项目ID] [端口]
BASE = "http://127.0.0.1:" + (sys.argv[2] if len(sys.argv) > 2 else "8766")
O = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PID = sys.argv[1] if len(sys.argv) > 1 else "d5815c1e-c641-4f21-a2a4-888388f5157a"

ok = fail = 0


def get(p, timeout=60):
    try:
        with O.open(BASE + p, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read().decode("utf-8", "replace")[:300]}
    except Exception as e:
        return {"_err": f"{type(e).__name__}: {e}"}


def post(p, body=None, timeout=120):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(BASE + p, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with O.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read().decode("utf-8", "replace")[:300]}
    except Exception as e:
        return {"_err": f"{type(e).__name__}: {e}"}


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}" + (f" — {detail}" if detail else ""))
    else:
        fail += 1
        print(f"  ❌ {name} — {detail}")


print("═" * 72)
print("① 健康检查")
h = get("/api/health")
check("后端存活", (h.get("data") or {}).get("status") == "ok")
check("FFmpeg 可用", (h.get("data") or {}).get("ffmpeg_available") is True)

print("\n② 模型目录：每个模型都要有『看得懂』的说明")
cat = (get("/api/model-catalog?kind=video").get("data") or {}).get("catalog") or {}
check("目录有 10 家厂商", len(cat) == 10, f"{len(cat)} 家")
bad = []
for p, e in cat.items():
    for m in e.get("models") or []:
        if not m.get("label") or not m.get("caps") or not m.get("mode_zh"):
            bad.append(f"{p}/{m.get('id')}")
check("每个模型都有 中文名+能力+模式", not bad, ",".join(bad) or "全部齐全")
noaudio = [p for p, e in cat.items() if "audio" not in e]
check("每家都有『是否自带声音』标注", not noaudio, ",".join(noaudio) or "全部有")

print("\n③ 文本模型目录（用户要求：文本模型也要标清楚）")
lcat = (get("/api/model-catalog?kind=llm").get("data") or {}).get("catalog") or {}
check("LLM 目录非空", bool(lcat), f"{len(lcat)} 家")

print("\n④ 海螺：模型清单不再出现不存在的版本")
known = ((get("/api/video-models/known").get("data") or {}).get("providers") or {}).get("hailuo") or []
ids = [m["id"] for m in known]
check("Hailuo-02-Fast 已从清单剔除", "MiniMax-Hailuo-02-Fast" not in ids)
check("清单非空", bool(ids), f"{len(ids)} 个: {', '.join(ids[:4])}…")
check("每个都有中文标签+时长标注", all("｜" in m.get("label", "") for m in known))

print("\n⑤ 海螺：参数组合一定合法")
cases = [("MiniMax-Hailuo-02", "1080P", 10, "1080P", 6),
         ("MiniMax-Hailuo-02", "768P", 10, "768P", 10),
         ("video-01", "1080P", 10, "1080P", 10),
         ("T2V-01-Director", "768P", 10, "768P", 6)]
for model, wr, wd, er, ed in cases:
    d = (get(f"/api/model-catalog/resolve?provider=hailuo&model={model}"
             f"&resolution={wr}&duration={wd}").get("data") or {})
    check(f"{model} {wr}/{wd}s → {er}/{ed}s",
          str(d.get("resolution")) == er and int(d.get("duration") or 0) == ed,
          f"实际 {d.get('resolution')}/{d.get('duration')}s")

print("\n⑥ 后期流水线：6 步 + 跳过能力")
st = (get(f"/api/projects/{PID}/pipeline").get("data") or {})
steps = st.get("steps") or []
check("6 个步骤", len(steps) == 6, f"{len(steps)} 个")
check("第①步不可跳过", steps[0].get("skippable") is False if steps else False)
check("②③④⑤⑥ 可跳过", all(s.get("skippable") for s in steps[1:]) if len(steps) == 6 else False)
check("每步都有 what/why 说明", all(s.get("what") and s.get("why") for s in steps))
check("有产物的步骤带 preview_url",
      all(s.get("preview_url") for s in steps if s.get("has_output")))

print("\n⑦ 流水线产物真的能播")
for s in steps:
    if not s.get("preview_url"):
        continue
    try:
        with O.open(BASE + s["preview_url"], timeout=30) as r:
            head = r.read(64)
        check(f"{s['key']} 产物可访问",
              r.status == 200 and head[4:8] == b"ftyp",
              f"{r.headers.get('content-length')} bytes")
    except Exception as e:
        check(f"{s['key']} 产物可访问", False, str(e))

print("\n⑧ 角色固定槽位（item ② 的落地）")
chars = (get(f"/api/projects/{PID}/characters").get("data") or {}).get("characters") or []
print(f"     该项目角色数: {len(chars)}")
if chars:
    c0 = chars[0]
    rf = c0.get("reference_features")
    if isinstance(rf, str):
        try:
            rf = json.loads(rf)
        except Exception:
            rf = {}
    slots = (rf or {}).get("slots") or {}
    print(f"     角色「{c0.get('name')}」槽位数: {len(slots)}")
    print(f"     description: {str(c0.get('description'))[:70]}")
else:
    print("     （该项目没有角色，用单测 tests/test_character_slots.py 验证过）")

print("\n" + "═" * 72)
print(f"结果: {ok} 通过 / {fail} 失败")
sys.exit(1 if fail else 0)
