# -*- coding: utf-8 -*-
"""验收「真·后期 AI 再修改」：走 API 端点，用真实角色图改一次。

这是用户明确要的能力：
  "支持角色图片后期AI再修改改进精准"
  "支持用户自己提供图片AI参考后根据剧情需要进行实际修改"
关键区别：**在原图上改**（不是重画一张）。验证点：
  1. 端点可用、真返回一张新图
  2. 新图与旧图**不同**（确实改了）
  3. 旧图被备份（改坏了能退回）
  4. 资产的 reference_image_path 指向新图
"""
import io
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"D:\VideoForge-dev\backend")
from imageio_ffmpeg import get_ffmpeg_exe       # noqa: E402
FF = get_ffmpeg_exe()

BASE = "http://127.0.0.1:8766"
O = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PID = sys.argv[1] if len(sys.argv) > 1 else "d5815c1e-c641-4f21-a2a4-888388f5157a"
ok = fail = 0


def req(m, p, b=None, t=600):
    d = json.dumps(b).encode() if b is not None else None
    r = urllib.request.Request(BASE + p, data=d, method=m,
                               headers={"Content-Type": "application/json"})
    try:
        with O.open(r, timeout=t) as x:
            return json.loads(x.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read().decode("utf-8", "replace")[:400]}
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
    print("后端未起"); raise SystemExit(1)

print("═" * 76)
print("① 图像 provider 里应出现「硅基流动」（用户已有可用 Key）")
d = req("GET", "/api/image/providers")
ps = (d.get("data") or {}).get("providers") or []
names = [p["name"] for p in ps]
ck("4 家 provider（含 siliconflow）", "siliconflow" in names, ",".join(names))
for p in ps:
    print(f"     {p['name']:12s} {p['display_name']:20s} 可用={p['usable']}")

print("\n" + "═" * 76)
print("② 挑一个有形象图的角色，对它做「在原图上修改」")
chars = (req("GET", f"/api/projects/{PID}/characters").get("data") or {}).get("characters") or []
withimg = [c for c in chars if c.get("reference_image_path")
           and os.path.exists(c["reference_image_path"])]
print(f"     角色 {len(chars)} 个，其中有形象图的 {len(withimg)} 个")
if not withimg:
    print("     （没有带形象图的角色 —— 先造一个，用现有图当底图）")
    if not chars:
        print("!! 该项目没有角色"); raise SystemExit(1)
    # 用一张现成的分镜图当底图，临时挂到角色上
    shots = (req("GET", f"/api/projects/{PID}/shots").get("data") or {}).get("shots") or []
    cand = ""
    for s in shots:
        cs = s.get("candidates")
        if isinstance(cs, str):
            try: cs = json.loads(cs)
            except Exception: cs = []
        for c in (cs or []):
            if isinstance(c, dict) and c.get("path") and os.path.exists(c["path"]):
                cand = c["path"]; break
        if cand: break
    if not cand:
        print("!! 也找不到现成图片"); raise SystemExit(1)
    # 抽一帧当底图
    frame = os.path.join(os.path.dirname(cand), "refine_base.jpg")
    subprocess.run([FF, "-y", "-hide_banner", "-loglevel", "error", "-ss", "1",
                    "-i", cand, "-frames:v", "1", "-q:v", "3", frame], capture_output=True)
    print(f"     用分镜画面抽帧当底图: {frame} ({os.path.getsize(frame)} 字节)")
    req("PATCH", f"/api/characters/{chars[0]['id']}",
        {"reference_image_path": frame})
    withimg = [{"id": chars[0]["id"], "name": chars[0]["name"],
                "reference_image_path": frame}]

c0 = withimg[0]
old_path = c0["reference_image_path"]
old_size = os.path.getsize(old_path)
print(f"     角色「{c0['name']}」 当前形象: {os.path.basename(old_path)} ({old_size} 字节)")

INSTRUCTION = "把画面调成电影感：加强对比与暗部层次，人物轮廓更清晰，色调偏冷"
KEEP = ["人物位置", "构图", "背景结构"]
print(f"     要改：{INSTRUCTION}")
print(f"     保留：{'、'.join(KEEP)}")

r = req("POST", f"/api/projects/{PID}/characters/{c0['id']}/refine-image",
        {"instruction": INSTRUCTION, "keep": KEEP,
         "provider": "siliconflow", "model": "Qwen/Qwen-Image-Edit-2509"})
dd = r.get("data") or {}
ck("修改成功", dd.get("ok"), str(dd.get("error") or "")[:200])
if not dd.get("ok"):
    print("    完整响应:", json.dumps(r, ensure_ascii=False)[:400])
    raise SystemExit(1)
print(f"     用模型: {dd.get('provider')} / {dd.get('model')} · {dd.get('elapsed')}s")
print(f"     新图: {dd.get('path')}")

print("\n" + "═" * 76)
print("③ 验证「真的改了」且「旧图留了底」")
new_path = dd.get("path") or ""
ck("新图存在", os.path.exists(new_path), f"{os.path.getsize(new_path) if os.path.exists(new_path) else 0} 字节")
ck("新图与原图不是同一个文件", os.path.abspath(new_path) != os.path.abspath(old_path))
if os.path.exists(new_path):
    import hashlib
    h_old = hashlib.md5(open(old_path, "rb").read()).hexdigest()[:12]
    h_new = hashlib.md5(open(new_path, "rb").read()).hexdigest()[:12]
    ck("内容确实不同（md5 不一样）", h_old != h_new, f"{h_old} → {h_new}")
    # 分辨率（编辑模型不保证同尺寸，如实报告）
    def dim(p):
        rr = subprocess.run([FF, "-hide_banner", "-i", p], capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
        m = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", rr.stderr or "", re.S)
        return f"{m.group(1)}x{m.group(2)}" if m else "?"
    print(f"     尺寸: 原 {dim(old_path)} → 新 {dim(new_path)}")

# 旧图备份
cache = os.path.join(os.environ["LOCALAPPDATA"], "VideoForge", "data", "cache",
                     "images", PID, "characters", "refined")
backups = [f for f in os.listdir(cache)] if os.path.isdir(cache) else []
prev = [f for f in backups if f.startswith("prev_")]
ck("旧图已备份（可退回）", bool(prev), f"{len(prev)} 个备份")
print(f"     备份目录文件: {sorted(backups)[:6]}")

print("\n" + "═" * 76)
print("④ 资产已指向新图 + 能被浏览")
after = (req("GET", f"/api/projects/{PID}/characters").get("data") or {}).get("characters") or []
me = next((c for c in after if c["id"] == c0["id"]), None)
ck("reference_image_path 已更新", me and os.path.abspath(me.get("reference_image_path") or "")
   == os.path.abspath(new_path))
try:
    with O.open(BASE + f"/api/local-image?path={urllib.parse.quote(new_path)}", timeout=60) as rr:
        b = rr.read(64)
    ck("新图可通过 HTTP 访问（前端能显示）", rr.status == 200 and len(b) > 8,
       f"{rr.headers.get('content-type')}")
except Exception as e:
    ck("新图可通过 HTTP 访问", False, str(e))

print("\n" + "═" * 76)
print("⑤ 负向：用非编辑模型应被拒绝（防止『以为改了其实重画了』）")
r2 = req("POST", f"/api/projects/{PID}/characters/{c0['id']}/refine-image",
         {"instruction": "改一下", "provider": "siliconflow",
          "model": "Kwai-Kolors/Kolors"})
ck("拒绝非编辑模型", r2.get("_http") == 400 or not (r2.get("data") or {}).get("ok"),
   str(r2.get("_body") or r2.get("data", {}).get("error") or "")[:150])

print("\n" + "═" * 76)
print(f"结果: {ok} 通过 / {fail} 失败")
