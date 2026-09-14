# -*- coding: utf-8 -*-
"""第四批验收：①生成 API 打通 ②最短 5 秒 ③后期按钮全可用 ④流畅度相关接口

全部走真实 HTTP + 真实 FFmpeg，产出真实文件。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("VF_BASE", "http://127.0.0.1:8899")
PID = sys.argv[1] if len(sys.argv) > 1 else "e62a182d-71ef-49ca-951b-921166c4f6c3"
ok = True


def check(name, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name, (" — " + str(detail)) if detail else ""))


def call(method, path, payload=None, timeout=600):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"message": "?"}


def poll(path, key=None, timeout=900, label=""):
    t0 = time.time()
    last = ""
    while time.time() - t0 < timeout:
        st, r = call("GET", path)
        d = r.get("data") or {}
        line = "%5.1f%%  %s" % (d.get("percent", 0), d.get("message", ""))
        if line != last:
            print("      " + (label + " " if label else "") + line)
            last = line
        if not d.get("running"):
            return d
        time.sleep(2)
    return {"running": True, "error": "超时"}


print("=" * 62)
print("① 「生成」页 API 打通（本地合成，无需付费 Key）")
print("=" * 62)
st, r = call("GET", "/api/projects/%s/shots" % PID)
shots = (r.get("data") or {}).get("shots") or []
check("拿到分镜", len(shots) > 0, "%d 个" % len(shots))
if not shots:
    sys.exit(1)
sid = shots[0]["id"]

# —— 时长：显式给 5 秒，验证真的产出 5 秒左右 ——
print("\n-- 渲染 1 个分镜（指定 5 秒） --")
st, r = call("POST", "/api/shots/%s/render" % sid, {
    "duration_seconds": 5, "with_voice": True, "auto_images": False, "mode": "auto",
})
check("POST /api/shots/{sid}/render 已接受", (r.get("data") or {}).get("started") is True,
      r.get("message", ""))
d = poll("/api/shots/%s/render/status" % sid, label="分镜1")
res = d.get("result") or {}
check("分镜渲染成功", bool(res.get("ok")), d.get("error") or (res.get("errors") or ""))
dur1 = res.get("duration") or 0
check("② 时长为 5 秒（±0.6s）", abs(dur1 - 5) <= 0.6, "%.2fs" % dur1)
check("产出真实文件", os.path.exists(res.get("path") or ""),
      "%.2f MB" % ((res.get("size_bytes") or 0) / 1048576))

# —— 时长：再给 20 秒，验证也遵守 ——
print("\n-- 同分镜改成 20 秒 --")
st, r = call("POST", "/api/shots/%s/render" % sid, {"duration_seconds": 20, "auto_images": False})
d2 = poll("/api/shots/%s/render/status" % sid, label="分镜1(20s)")
res2 = d2.get("result") or {}
check("20 秒请求被遵守（±1.2s）", abs((res2.get("duration") or 0) - 20) <= 1.2,
      "%.2fs" % (res2.get("duration") or 0))

# —— 时长下限：给 1 秒，应被抬到 5 秒 ——
print("\n-- 给 1 秒，验证下限抬到 5 秒 --")
st, r = call("POST", "/api/shots/%s/render" % sid, {"duration_seconds": 1, "auto_images": False})
d3 = poll("/api/shots/%s/render/status" % sid, label="分镜1(1s→)")
res3 = d3.get("result") or {}
check("② 小于 5 秒自动抬到 ≥5 秒", (res3.get("duration") or 0) >= 4.9,
      "%.2fs" % (res3.get("duration") or 0))

# —— 真实视频模型（无 Key）应给出清晰指引，而不是静默失败 ——
st, r = call("POST", "/api/shots/%s/render" % sid, {"mode": "api", "model_provider": "kling"})
check("无 Key 走真实模型时给出明确提示",
      st == 400 and ("API Key" in json.dumps(r, ensure_ascii=False)), "HTTP %s" % st)

print("\n" + "=" * 62)
print("③ 后期：产出文件 / 比例导出 / BGM / 输出目录")
print("=" * 62)
st, r = call("GET", "/api/projects/%s/outputs" % PID)
outs = (r.get("data") or {}).get("outputs") or []
vids = [o for o in outs if o["kind"] == "video"]
check("产出文件列表可用", len(outs) > 0, "%d 个文件 / %d 个视频" % (len(outs), len(vids)))
check("含成片 final.mp4", any(o["is_final"] for o in outs))
check("每个视频都有播放 url", all(o.get("url") for o in vids))

# 逐个视频的 url 真的能取回文件（浏览器 <video> 走的就是这个）
for o in vids[:2]:
    try:
        with urllib.request.urlopen(BASE + o["url"], timeout=60) as resp:
            head = resp.read(65536)
            check("视频 url 可播放: " + o["name"], b"ftyp" in head[:64],
                  "HTTP %s, %d bytes" % (resp.status, len(head)))
    except Exception as e:
        check("视频 url 可播放: " + o["name"], False, str(e)[:120])

src = next((o["path"] for o in vids if o["is_final"]), vids[0]["path"] if vids else "")
check("有可用源视频", bool(src), src.split("\\")[-1] if src else "")

print("\n-- 比例导出 9:16 --")
st, r = call("POST", "/api/projects/%s/outputs/export-aspect" % PID,
             {"source_path": src, "target_aspect": "9:16"})
d = r.get("data") or {}
check("比例导出成功", st == 200 and os.path.exists(d.get("output_path") or ""),
      (d.get("output_path") or "").split("\\")[-1] or r.get("message", ""))

print("\n-- 输出目录设置（含不可写校验） --")
# 注：本机是管理员，System32 其实可写；用一个**不存在的盘符**才是可靠的非可写路径
bad = r"Z:\__vf_no_such_drive__\out"
st, r = call("POST", "/api/settings/output-dir", {"path": bad})
check("不存在的盘符被拒", st == 400, "HTTP %s %s" % (st, r.get("message", "")))
tmpdir = os.path.join(os.environ.get("TEMP", "."), "vf_out_test")
st, r = call("POST", "/api/settings/output-dir", {"path": tmpdir})
check("可写目录设置成功", st == 200 and (r.get("data") or {}).get("output_dir"), r.get("message", ""))
# 恢复默认
call("POST", "/api/settings/output-dir",
     {"path": os.path.join(os.environ["LOCALAPPDATA"], "VideoForge", "data", "outputs")})

print("\n" + "=" * 62)
print("④ 流畅度：批量生成 + 媒体服务 + 目录选择器存在性")
print("=" * 62)
st, r = call("POST", "/api/projects/%s/render-all" % PID, {"duration_seconds": 5, "auto_images": False})
check("批量生成接口可用", (r.get("data") or {}).get("started") is True, r.get("message", ""))
if (r.get("data") or {}).get("started"):
    d = poll("/api/projects/%s/render-all/status" % PID, label="批量")
    check("批量生成跑完", not d.get("running"), d.get("message", ""))
    check("批量无失败", not d.get("failed"), str(d.get("failed"))[:200])

with urllib.request.urlopen(BASE + "/api/media?path=" + urllib.parse.quote(src), timeout=60) as resp:
    head = resp.read(4096)
    check("媒体服务可达且是 MP4", resp.status in (200, 206) and b"ftyp" in head[:64],
          "HTTP %s, %d bytes" % (resp.status, len(head)))

# 越权访问必须被拒（媒体接口返回二进制，不能走 JSON 解析）
try:
    req = urllib.request.Request(BASE + "/api/media?path=" + urllib.parse.quote(r"C:\Windows\System32\drivers\etc\hosts"))
    with urllib.request.urlopen(req, timeout=30) as resp:
        check("媒体服务越权被拒", False, "竟然返回 HTTP %s" % resp.status)
except urllib.error.HTTPError as e:
    check("媒体服务越权被拒", e.code == 403, "HTTP %s" % e.code)

print()
print("=" * 62)
print("RESULT:", "ALL PASS" if ok else "HAS FAILURES")
sys.exit(0 if ok else 1)
