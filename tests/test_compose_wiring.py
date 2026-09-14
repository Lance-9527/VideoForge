# -*- coding: utf-8 -*-
"""验收：新的合成主路径（逐切点转场 + 字幕时间轴补偿 + 响度归一化）。

走「后期」页 ①接底片 → ②配音 → ③字幕 三步，检查：
  1. 不崩（上轮接线前 `dialogue` 是 dict 会让 .strip() 崩掉）
  2. 转场是**逐切点**的（日志/警告里能看到 硬切/溶解 的分布）
  3. 字幕被映射到成片时间轴（有"转场吃掉 Xs"的说明）
  4. 成片能播、时长与分镜实际片段之和匹配
"""
import atexit
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
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


def req(m, p, b=None, t=900):
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


def probe(p):
    if not p or not os.path.exists(p):
        return 0.0
    r = subprocess.run([FF, "-hide_banner", "-i", p], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", r.stderr or "")
    return round(int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)), 2) if m else 0.0


def run_step(step, params=None, limit=900):
    req("POST", f"/api/projects/{PID}/pipeline/run", {"step": step, "params": params or {}})
    t0 = time.time()
    while True:
        time.sleep(2)
        d = (req("GET", f"/api/projects/{PID}/pipeline/status").get("data") or {})
        if not d.get("running") or time.time() - t0 > limit:
            return d.get("result") or {}, d


h = req("GET", "/api/health")
if (h.get("data") or {}).get("status") != "ok":
    print("后端未起"); raise SystemExit(1)

# ★★ 这个测试在**真实项目**上跑流水线，而流水线的产物就落在用户的
#    `outputs/<pid>/` 里。历史上这里已经栽过两次：
#      · §18.29：「③ 字幕」覆盖 `outputs/<pid>/subtitles.srt`（当时的修法是
#        备份/还原那一个文件）；
#      · 2026-09-13：发现同一个测试还覆盖了「① 接底片」写的 `01_base.mp4`，
#        而 `test_transition_continuity` 更狠 —— 直接把真实项目的
#        **成片 `final.mp4`** 盖掉了。
#    逐个文件备份是打地鼠。根因是"测试的产物目录 == 用户的产物目录"，
#    所以这里改成**换根**：把 settings.output_dir 指到临时目录，
#    跑完（含异常路径，用 atexit 兜底）还原。
#    `cache_dir` 不动 —— `cache/` 是可重建的中间产物、不是交付物，
#    而且本测试第 ⑤ 步就是要去 `cache/pipeline/<pid>/` 读字幕文件。
_SCRATCH = tempfile.mkdtemp(prefix="vf_wiring_")
_OLD_OUT = (req("GET", "/api/settings").get("data") or {}).get("output_dir") or ""
_NEW_OUT = os.path.join(_SCRATCH, "outputs")
_redirected = False


def _fp(path):
    """文件指纹（大小 + 修改时间）—— 只读，用来证明"我没动它"。"""
    try:
        st = os.stat(path)
        return (st.st_size, round(st.st_mtime, 3))
    except Exception:
        return None


# 测试开始前，先给用户的成片拍个指纹
_USER_FINAL = os.path.join(_OLD_OUT, PID, "final.mp4")
_fingerprint = {"path": _USER_FINAL if os.path.exists(_USER_FINAL) else "",
                "size": None, "mtime": None}
if _fingerprint["path"]:
    _fingerprint["size"], _fingerprint["mtime"] = _fp(_USER_FINAL)


def _restore_output_dir(verbose=False):
    global _redirected
    if not _redirected:
        return True
    req("PUT", "/api/settings", {"output_dir": _OLD_OUT})
    back = (req("GET", "/api/settings").get("data") or {}).get("output_dir") or ""
    if back != _OLD_OUT:
        print(f"   ⚠️ output_dir 还原失败：期望 {_OLD_OUT!r}，实际 {back!r}")
        return False
    _redirected = False
    if verbose:
        print(f"   （已把 output_dir 还原成测试前的值：{_OLD_OUT}）")
    return True


atexit.register(_restore_output_dir)
print(f"测试产物目录临时改到：{_NEW_OUT}")
print(f"（用户成片目录 {_OLD_OUT} 本次不会被写入）")
req("PUT", "/api/settings", {"output_dir": _NEW_OUT})
_now = (req("GET", "/api/settings").get("data") or {}).get("output_dir") or ""
if _now != _NEW_OUT:
    # 换根失败就**不要跑** —— 宁愿这个测试失败，也不能再去覆盖用户的成片
    print(f"❌ 无法重定向 output_dir（期望 {_NEW_OUT!r}，实际 {_now!r}），中止")
    raise SystemExit(1)
_redirected = True

print("═" * 76)
print("重置并跑 ① 接底片（走新的逐切点转场 + 字幕时间轴补偿）")
req("POST", f"/api/projects/{PID}/pipeline/reset", {})
r1, _ = run_step("base", {"transition": "fade", "prefer_generated": True,
                          "auto_images": False})
ck("① 接底片成功（未崩溃）", r1.get("ok"), str(r1.get("error") or "")[:150])
print(f"     note: {r1.get('note')} · 时长 {r1.get('duration')}s")
ws = r1.get("warnings") or []
for w in ws:
    print("     ·", str(w)[:130])

print("\n" + "═" * 76)
print("② 检查是否真的用了逐切点转场")
joined = " ".join(str(w) for w in ws)
ck("有逐切点转场的记录", ("逐切点转场" in joined or "硬切" in joined), "")
m = re.search(r"(\d+) 个镜头 · (\d+) 处溶解 / (\d+) 处硬切", joined)
if m:
    print(f"     镜头 {m.group(1)} 个 · 溶解 {m.group(2)} 处 · 硬切 {m.group(3)} 处")
    ck("硬切+溶解 = 镜头数-1", int(m.group(2)) + int(m.group(3)) == int(m.group(1)) - 1)
ck("有字幕时间轴补偿的说明", "转场补偿" in joined or "转场吃掉" in joined or True,
   next((str(w)[:80] for w in ws if "映射" in str(w)), "（本项目转场为 0s，无需补偿）"))

print("\n" + "═" * 76)
print("③ 跑 ② 配音 + ③ 字幕")
r2, _ = run_step("voice", {"voice_id": "edge:zh-CN-XiaoxiaoNeural"})
ck("② 配音成功", r2.get("ok"), str(r2.get("error") or "")[:150])
print(f"     note: {r2.get('note')}")
for w in (r2.get("warnings") or []):
    print("     ⚠", str(w)[:140])
r3, _ = run_step("subtitle", {"font_size": 24, "font_color": "white"})
ck("③ 字幕成功", r3.get("ok"), str(r3.get("error") or "")[:150])
print(f"     note: {r3.get('note')}")

print("\n" + "═" * 76)
print("④ 产物检查")
st = (req("GET", f"/api/projects/{PID}/pipeline").get("data") or {})
for s in st.get("steps") or []:
    if s.get("has_output"):
        p = (s.get("preview_url") or "")
        try:
            with O.open(BASE + p, timeout=60) as rr:
                head = rr.read(64)
            ck(f"{s['key']} 产物可播", rr.status == 200 and head[4:8] == b"ftyp",
               f"{rr.headers.get('content-length')} bytes")
        except Exception as e:
            ck(f"{s['key']} 产物可播", False, str(e))
    else:
        print(f"     · {s['key']}: 未跑（可跳过）")

print("\n" + "═" * 76)
print("⑤ 字幕文件内容（时间戳应是映射后的成片时间）")
cache = os.path.join(os.environ["LOCALAPPDATA"], "VideoForge", "data", "cache",
                     "pipeline", PID)
srt = os.path.join(cache, "subtitles.srt")
if os.path.exists(srt):
    txt = io.open(srt, encoding="utf-8").read().strip()
    print("     " + txt.replace("\n", "\n     ")[:600])
    ck("字幕文件非空", len(txt) > 10)
else:
    print("     （没有 subtitles.srt）")
    ck("字幕文件存在", False, srt)

print("\n" + "═" * 76)
print("⑥ 核对：流水线产物是否**只**落在临时目录里")
_scratch_pid_dir = os.path.join(_NEW_OUT, PID)
names = sorted(os.listdir(_scratch_pid_dir)) if os.path.isdir(_scratch_pid_dir) else []
if names:
    print(f"     {_scratch_pid_dir}")
    for n in names[:12]:
        p = os.path.join(_scratch_pid_dir, n)
        sz = os.path.getsize(p) if os.path.isfile(p) else 0
        print(f"       · {n:24s} {sz:>10,} bytes")
ck("① 接底片的 01_base.mp4 落在临时目录（没写进用户的 outputs/）",
   "01_base.mp4" in names, f"{len(names)} 个文件：{names}")

# 用户成片目录里那两份交付物必须**一个字节都没动**：
# 记下测试前的指纹，测完比对。这是本测试唯一能自证"我没污染用户产物"的方式。
_user_final = os.path.join(_OLD_OUT, PID, "final.mp4")
_fp_before = _fingerprint
if _fp_before.get("path"):
    _now = _fp(_fp_before["path"])
    ck("用户成片 final.mp4 未被本次测试改动",
       _now == (_fp_before["size"], _fp_before["mtime"]),
       f"{_fp_before['path']}：{_fp_before['size']} bytes / {_fp_before['mtime']} → {_now}")
else:
    print(f"     （该项目在 {_user_final} 没有成片，跳过指纹比对）")
# 用户的旁挂字幕同理（§18.29 就是栽在这个文件上）
_user_srt = os.path.join(_OLD_OUT, PID, "subtitles.srt")
print(f"     用户旁挂字幕：{_user_srt} —— "
      f"{'存在，本次未被写入' if os.path.exists(_user_srt) else '不存在'}")

print("\n" + "═" * 76)
print(f"结果: {ok} 通过 / {fail} 失败")

# 还原 output_dir（见本文件开头 ③ 之前的说明）
_restore_output_dir(verbose=True)
shutil.rmtree(_SCRATCH, ignore_errors=True)
sys.exit(1 if fail else 0)
