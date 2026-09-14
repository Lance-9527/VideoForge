# -*- coding: utf-8 -*-
"""对新增的「质量层」接口做真实数据验收（不调用任何付费视频模型）。"""
import io
import json
import sys
import urllib.error
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:8766"
O = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PID = sys.argv[1] if len(sys.argv) > 1 else "d5815c1e-c641-4f21-a2a4-888388f5157a"
ok = fail = 0


def req(m, p, b=None, t=200):
    d = json.dumps(b).encode() if b is not None else None
    r = urllib.request.Request(BASE + p, data=d, method=m,
                               headers={"Content-Type": "application/json"})
    try:
        with O.open(r, timeout=t) as x:
            return json.loads(x.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read().decode("utf-8", "replace")[:300]}
    except Exception as e:
        return {"_err": f"{type(e).__name__}: {e}"}


def ck(n, c, d=""):
    global ok, fail
    if c:
        ok += 1
        print(f"  ✅ {n}" + (f" — {d}" if d else ""))
    else:
        fail += 1
        print(f"  ❌ {n} — {d}")


h = req("GET", "/api/health")
if (h.get("data") or {}).get("status") != "ok":
    print("后端未起:", h)
    raise SystemExit(1)

shots = (req("GET", f"/api/projects/{PID}/shots").get("data") or {}).get("shots") or []
if not shots:
    print("项目没有分镜")
    raise SystemExit(1)

print("═" * 74)
print("① 台词体检 /dialogue-audit（把『没有台词』变成看得见的数字）")
d = req("GET", f"/api/projects/{PID}/dialogue-audit")
dd = d.get("data") or {}
ck("接口正常", d.get("code") == "000000")
print(f"     分镜 {dd.get('shot_count')} 个 · 台词 {dd.get('total_lines')} 句 "
      f"· 共 {dd.get('total_chars')} 字")
print(f"     没有台词的分镜: {dd.get('shots_without_dialogue')}")
print(f"     建议: {str(dd.get('advice'))[:80]}")

print("\n" + "═" * 74)
print("② 分段规划 /shots/{sid}/plan（『分镜 12 秒但模型只给 6 秒』的正面回答）")
sid = shots[0]["id"]
p = req("GET", f"/api/projects/{PID}/shots/{sid}/plan?resolution=1080P")
pd = p.get("data") or {}
ck("接口正常", p.get("code") == "000000", str(p)[:80] if p.get("code") != "000000" else "")
print(f"     模式 {pd.get('provider')}/{pd.get('model_label') or pd.get('model')}")
print(f"     想要 {pd.get('requested')}s → 实际 {pd.get('effective')}s "
      f"@ {pd.get('resolution')} · {pd.get('segment_count')} 段")
print(f"     摘要: {pd.get('summary')}")
for a in (pd.get("adjustments") or []):
    print("     调整:", a.replace("<b>", "").replace("</b>", ""))
for w in (pd.get("warnings") or []):
    print("     警告:", str(w)[:90])
for s in (pd.get("segments") or []):
    print(f"       段{s['index']}: {s['start']}→{s['end']}s ({s['duration']}s) "
          f"台词{s['line_count']}条 {s['line_chars']}字")
ck("给了分段明细", bool(pd.get("segments")))
ck("说明了自己做了什么调整或确认无需调整",
   bool(pd.get("adjustments")) or pd.get("requested") == pd.get("effective"))

print("\n" + "═" * 74)
print("③ 衔接计划 /continuity（用户最在意的那条：不同视频之间怎么接）")
c = req("GET", f"/api/projects/{PID}/continuity")
cd = c.get("data") or {}
ck("接口正常", c.get("code") == "000000", str(c)[:100] if c.get("code") != "000000" else "")
print(f"     镜头 {cd.get('summary', {}).get('shot_count')} 个 · "
      f"串联 {cd.get('summary', {}).get('chained_count')} 个")
print(f"     转场分布: {json.dumps(cd.get('summary', {}).get('transitions'), ensure_ascii=False)}")
print(f"     成片总长 {cd.get('total')}s（转场吃掉 {cd.get('timeline_shrink')}s）")
for x in (cd.get("plan") or [])[:6]:
    t = x.get("transition") or {}
    print(f"       {x.get('order_index')}: {t.get('type')}/{t.get('level')} "
          f"({t.get('seconds')}s) 接尾帧={'是' if x.get('use_last_frame_of') else '否'}"
          f"  {str(t.get('why'))[:34]}")
ck("每个镜头都给了转场决策", len(cd.get("plan") or []) == len(shots))
ck("给了成片时间轴映射", bool(cd.get("timeline")))

print("\n" + "═" * 74)
print("④ 时间轴映射自洽性：每段 film_start 应等于上一段 film_end − 重叠")
tl = cd.get("timeline") or []
good = True
for i in range(1, len(tl)):
    prev, cur = tl[i - 1], tl[i]
    expect = round(prev["film_end"] - cur["overlap_in"], 3)
    if abs(expect - cur["film_start"]) > 0.02:
        good = False
        print(f"     ❌ 段{i}: 期望 {expect} 实际 {cur['film_start']}")
ck("时间轴映射自洽", good)
if tl:
    print(f"     首段 {tl[0]['film_start']}s 起 · 末段 {tl[-1]['film_end']}s 止")

print("\n" + "═" * 74)
print("⑤ 字体与字幕模块（防方块字）")
sys.path.insert(0, r"D:\VideoForge-dev\backend")
from core.subtitle import font_report, esc_filter_path      # noqa: E402
fr = font_report()
print("     字体探测:", json.dumps(fr, ensure_ascii=False))
ck("找到可用中文字体", fr.get("found"), fr.get("family"))
ck("已决定要用 Noto Sans SC（开源可打包、不依赖系统装了啥）",
   fr.get("family") == "Noto Sans SC", fr.get("family"))
print("     路径转义示例:", esc_filter_path(r"C:\Users\x\sub.srt"), "← 冒号已转义，调用时外层再加引号")

print("\n" + "═" * 74)
print(f"结果: {ok} 通过 / {fail} 失败")
sys.exit(1 if fail else 0)
