# -*- coding: utf-8 -*-
"""对**打包版**做前端资源与新增接口的落地检查（确认 rebuild 真的带上了改动）"""
import io
import json
import sys
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
PORT = sys.argv[1] if len(sys.argv) > 1 else "8790"
PID = sys.argv[2] if len(sys.argv) > 2 else "d5815c1e-c641-4f21-a2a4-888388f5157a"
BASE = f"http://127.0.0.1:{PORT}"
O = urllib.request.build_opener(urllib.request.ProxyHandler({}))
ok = fail = 0


def text(path, timeout=60):
    try:
        with O.open(BASE + path, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        return f"__ERR__ {type(e).__name__}: {e}"


def js(path, timeout=60):
    try:
        with O.open(BASE + path, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        return f"__ERR__ {type(e).__name__}: {e}"


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}" + (f" — {detail}" if detail else ""))
    else:
        fail += 1
        print(f"  ❌ {name} — {detail}")


print("═" * 72)
print("① 打包版首页 HTML 含新增 UI")
h = text("/")
check("拿得到首页", "__ERR__" not in h, h[:80] if "__ERR__" in h else f"{len(h)} 字符")
for eid, desc in [
    ("pipelineList", "流水线步骤列表"),
    ("pipelineNextBtn", "跑下一步按钮"),
    ("pipelineAllBtn", "全部跑完按钮"),
    ("pipelineResetBtn", "从头再来按钮"),
    ("pipelinePreviewDownload", "单步产物下载"),
    ("pipelineOpenFolderBtn", "打开输出目录"),
    ("heroDurationHint", "主页时长时间提示"),
    ("charRefineBtn", "角色细化设定按钮"),
    ("sceneRefineBtn", "场景细化设定按钮"),
    ("pp-advanced", "高级工具折叠区"),
]:
    check(desc, f'id="{eid}"' in h or f'class="{eid}"' in h or eid in h)
# genAudioHint 是 renderGeneratePage() 运行时生成的，静态 HTML 里没有 —— 在 app.js 里查
check("生成页声音提示容器（在 app.js 中生成）", "genAudioHint" in js("/js/app.js"))

print("\n② 打包版 app.js 含新增逻辑")
a = js("/js/app.js")
check("拿得到 app.js", "__ERR__" not in a, f"{len(a)} 字符")
for k, desc in [
    ("async skip(key)", "跳过某步"),
    ("async unskip(key)", "取消跳过"),
    ("changeGenShotModel", "生成页换模型"),
    ("wireGenAudioHint", "生成页声音提示"),
    ("CHAR_FACE_SLOTS", "角色面部槽位"),
    ("SCENE_SLOTS", "场景槽位"),
    ("characterSlotFields", "角色槽位表单"),
    ("pipelineOpenFolderBtn", "打开输出目录绑定"),
]:
    check(desc, k in a)

print("\n③ 打包版 CSS 含新增样式")
c = js("/css/studio.css")
check("拿得到 studio.css", "__ERR__" not in c, f"{len(c)} 字符")
for k in [".s-tag {", ".pipeline-step", ".pipeline-step.skipped", ".pp-advanced", ".char-slot-chip"]:
    check(f"样式 {k}", k in c)

print("\n④ 打包版接口")
def api(p, method="GET", body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + p, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with O.open(r, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read().decode("utf-8", "replace")[:300]}
    except Exception as e:
        return {"_err": f"{type(e).__name__}: {e}"}


kn = api("/api/video-models/known")
check("known 接口 200（不再 500）", kn.get("code") == "000000",
      json.dumps(kn, ensure_ascii=False)[:140])
hail = ((kn.get("data") or {}).get("providers") or {}).get("hailuo") or []
check("known 返回海螺 9 个版本", len(hail) == 9, f"{len(hail)} 个")

st = api(f"/api/projects/{PID}/pipeline")
steps = (st.get("data") or {}).get("steps") or []
check("pipeline 接口正常", len(steps) == 6, f"{len(steps)} 步")

sk = api(f"/api/projects/{PID}/pipeline/skip", "POST", {"step": "base"})
check("①接底片拒绝跳过（400）", sk.get("_http") == 400, json.dumps(sk, ensure_ascii=False)[:120])

art = api(f"/api/projects/{PID}/pipeline/artifact/base")
check("单步产物接口存在（非 404 路由缺失）", art.get("_http") != 404 or "产物" in str(art),
      str(art)[:100])

print("\n" + "═" * 72)
print(f"结果: {ok} 通过 / {fail} 失败")
sys.exit(1 if fail else 0)
