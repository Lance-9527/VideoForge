# -*- coding: utf-8 -*-
"""验收「视频参考」全链路：上传视频 → 抽帧 → 挑一张 → 成为形象基准。

同时验证修掉的那个真 bug：**上传视频不能再把 .mp4 写进 reference_image_path**。
"""
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
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


def post_file(path, filepath, field="file"):
    bnd = "----vf" + uuid.uuid4().hex
    fn = os.path.basename(filepath)
    with open(filepath, "rb") as f:
        payload = f.read()
    body = (f"--{bnd}\r\n"
            f'Content-Disposition: form-data; name="{field}"; filename="{fn}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + payload + \
           f"\r\n--{bnd}--\r\n".encode()
    r = urllib.request.Request(BASE + path, data=body, method="POST",
                               headers={"Content-Type":
                                        f"multipart/form-data; boundary={bnd}"})
    try:
        with O.open(r, timeout=900) as x:
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


# 找一个真实视频
video = ""
data = os.path.join(os.environ.get("LOCALAPPDATA", ""), "VideoForge", "data")
best = []
for root, _d, files in os.walk(data):
    for f in files:
        if f.lower().endswith(".mp4"):
            p = os.path.join(root, f)
            try:
                sz = os.path.getsize(p)
            except OSError:
                continue
            if sz > 300 * 1024:
                best.append((sz, p))
    if len(best) > 30:
        break
if best:
    best.sort()
    video = best[len(best) // 2][1]      # 取中等的，跑得快
print("测试视频:", video, f"{os.path.getsize(video)/1048576:.1f} MB")

h = req("GET", "/api/health")
if (h.get("data") or {}).get("status") != "ok":
    print("后端未起"); raise SystemExit(1)

scenes = (req("GET", f"/api/projects/{PID}/scenes").get("data") or {}).get("scenes") or []
if not scenes:
    print("该项目没有场景"); raise SystemExit(1)
sc = scenes[0]
print(f"用场景「{sc['name']}」({sc['id'][:8]}) 做测试")
before = sc.get("reference_image_path") or ""
print(f"  当前形象: {os.path.basename(before) if before else '(无)'}")

print("\n" + "═" * 76)
print("① 上传视频 → 抽帧")
r = req("POST", f"/api/projects/{PID}/scenes/{sc['id']}/video-reference"
        .replace("/scenes/", "/scenes/"), None)  # 占位（下面走 multipart）
r = post_file(f"/api/projects/{PID}/scenes/{sc['id']}/video-reference", video)
dd = r.get("data") or {}
ck("接口成功", dd.get("ok"), str(dd.get("error") or r.get("_body") or "")[:200])
frames = dd.get("frames") or []
print(f"     视频时长 {dd.get('duration')}s · 场景切换点 {dd.get('scene_points')} 个 "
      f"· 抽出 {len(frames)} 张")
ck("抽到多张候选画面", len(frames) >= 2, f"{len(frames)} 张")
for i, f in enumerate(frames, 1):
    print(f"       {i}. {f['time']:7.2f}s [{f['source']}] {os.path.basename(f['path'])}")
for w in dd.get("warnings") or []:
    print("     ⚠", w)

print("\n" + "═" * 76)
print("② 关键：视频**不能**被写进 reference_image_path（那是给图片用的字段）")
after = (req("GET", f"/api/projects/{PID}/scenes").get("data") or {}).get("scenes") or []
me = next((x for x in after if x["id"] == sc["id"]), None)
rip = (me or {}).get("reference_image_path") or ""
print(f"     现在 reference_image_path = {os.path.basename(rip) if rip else '(空)'}")
ck("不是视频文件", not rip.lower().endswith((".mp4", ".mov", ".webm")), rip)
rf = (me or {}).get("reference_features")
if isinstance(rf, str):
    try: rf = json.loads(rf)
    except Exception: rf = {}
vr = (rf or {}).get("video_reference") or {}
ck("视频被记在 reference_features.video_reference", bool(vr.get("path")),
   os.path.basename(vr.get("path") or ""))

print("\n" + "═" * 76)
print("③ 挑一张作为形象基准")
if frames:
    r2 = req("POST", f"/api/projects/{PID}/scenes/{sc['id']}/use-frame",
             {"frame_path": frames[1]["path"]})
    d2 = r2.get("data") or {}
    ck("设定成功", d2.get("ok"), str(d2.get("error") or r2.get("_body") or "")[:150])
    after2 = (req("GET", f"/api/projects/{PID}/scenes").get("data") or {}).get("scenes") or []
    me2 = next((x for x in after2 if x["id"] == sc["id"]), None)
    rip2 = (me2 or {}).get("reference_image_path") or ""
    ck("已指向挑中的那一帧", os.path.abspath(rip2) == os.path.abspath(frames[1]["path"]),
       os.path.basename(rip2))
    try:
        with O.open(BASE + f"/api/local-image?path={urllib.parse.quote(rip2)}",
                    timeout=60) as rr:
            ck("该帧可通过 HTTP 显示（前端能显示全貌）", rr.status == 200,
               rr.headers.get("content-type"))
    except Exception as e:
        ck("该帧可通过 HTTP 显示", False, str(e))

print("\n" + "═" * 76)
print("④ 按时间点现取一帧（用户可以自己指定时刻）")
mid = round((dd.get("duration") or 4) / 2, 2)
r3 = req("POST", f"/api/projects/{PID}/scenes/{sc['id']}/use-frame", {"time": mid})
d3 = r3.get("data") or {}
ck(f"在 {mid}s 取帧并设为基准", d3.get("ok"),
   str(d3.get("error") or r3.get("_body") or "")[:150])
if d3.get("ok"):
    print(f"     → {os.path.basename(d3.get('path') or '')}")

print("\n" + "═" * 76)
print("⑤ 负向：拿图片当视频上传应被拒绝并给出正确指引")
if frames:
    r4 = post_file(f"/api/projects/{PID}/scenes/{sc['id']}/video-reference",
                   frames[0]["path"])
    ck("拒绝非视频文件", r4.get("_http") == 400,
       str((r4.get("_body") or "")[:180]))

print("\n" + "═" * 76)
print("⑥ 还原：把场景形象改回测试前")
if before:
    req("PATCH", f"/api/scenes/{sc['id']}", {"reference_image_path": before})
    chk = (req("GET", f"/api/projects/{PID}/scenes").get("data") or {}).get("scenes") or []
    m3 = next((x for x in chk if x["id"] == sc["id"]), None)
    ck("已还原到测试前的形象图",
       os.path.abspath((m3 or {}).get("reference_image_path") or "")
       == os.path.abspath(before), os.path.basename(before))

print("\n" + "═" * 76)
print(f"结果: {ok} 通过 / {fail} 失败")
