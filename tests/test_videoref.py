# -*- coding: utf-8 -*-
"""验证「视频参考」：从用户提供的视频里挑出有代表性的画面。

用项目里**真实生成的视频**当输入 —— 比合成测试视频更能说明问题。
判定标准：
  1. 能读出时长
  2. 能挑出多张画面，且**不是同一张**（大小/位置有差异）
  3. 场景切换点被识别出来
  4. 能按指定时间点单独取一帧
"""
import io
import json
import os
import subprocess
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"D:\VideoForge-dev\backend")

from core.videoref import (extract_keyframes, extract_single_frame,   # noqa: E402
                           probe_duration, is_video, is_image)
from imageio_ffmpeg import get_ffmpeg_exe                            # noqa: E402

FF = get_ffmpeg_exe()
W = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_vref")
os.makedirs(W, exist_ok=True)

# ── 挑一个真实视频：优先项目输出，其次自己合成 ──
VIDEO = ""
cands = []
data = os.path.join(os.environ.get("LOCALAPPDATA", ""), "VideoForge", "data")
for root, _dirs, files in os.walk(data):
    for f in files:
        if f.lower().endswith(".mp4"):
            p = os.path.join(root, f)
            try:
                sz = os.path.getsize(p)
            except OSError:
                continue
            if sz > 200 * 1024:
                cands.append((sz, p))
    if len(cands) > 40:
        break
cands.sort(reverse=True)
for sz, p in cands:
    d = probe_duration(p)
    if 3.0 < d < 120:          # 找一个时长合适的
        VIDEO = p
        break
if not VIDEO and cands:
    VIDEO = cands[0][1]

if not VIDEO:
    print("没有找到现成视频，合成一个 4 段不同画面的测试视频")
    segs = []
    for i, col in enumerate(["0x2E4053", "0xB03A2E", "0x1E8449", "0x7D3C98"]):
        p = os.path.join(W, f"seg{i}.mp4")
        subprocess.run([FF, "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", f"color=c={col}:s=640x360:d=2:r=25",
                        "-vf", f"drawtext=text='SHOT {i+1}':fontsize=54:fontcolor=white:"
                               f"x=(w-text_w)/2:y=(h-text_h)/2",
                        "-c:v", "libx264", "-preset", "ultrafast",
                        "-pix_fmt", "yuv420p", "-an", p], capture_output=True)
        segs.append(p)
    VIDEO = os.path.join(W, "demo.mp4")
    inputs = []
    for s in segs:
        inputs += ["-i", s]
    filt = "".join(f"[{i}:v]" for i in range(len(segs))) + \
           f"concat=n={len(segs)}:v=1:a=0[o]"
    subprocess.run([FF, "-y", "-hide_banner", "-loglevel", "error"] + inputs +
                   ["-filter_complex", filt, "-map", "[o]",
                    "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", VIDEO], capture_output=True)

print("═" * 76)
print("测试视频")
print(f"   {VIDEO}")
print(f"   {os.path.getsize(VIDEO)/1048576:.1f} MB · {probe_duration(VIDEO):.2f}s")
print(f"   is_video={is_video(VIDEO)}  is_image={is_image(VIDEO)}")

print("\n" + "═" * 76)
print("① 抽代表性画面")
r = extract_keyframes(VIDEO, os.path.join(W, "kf"), max_frames=6)
print(f"   ok={r.get('ok')} · 时长 {r.get('duration')}s · 场景切换点 {r.get('scene_points')} 个")
if not r.get("ok"):
    print("   ❌", r.get("error"))
    raise SystemExit(1)
frames = r["frames"]
for i, f in enumerate(frames, 1):
    print(f"   {i}. {f['time']:7.2f}s  [{f['source']:5s}]  "
          f"{os.path.basename(f['path'])}  {f['size_bytes']} 字节")
for w in r.get("warnings") or []:
    print("   ⚠", w)

print("\n" + "═" * 76)
print("② 判定：画面确实不一样（不能给用户 6 张几乎相同的脸）")
import hashlib
hs = []
for f in frames:
    h = hashlib.md5(open(f["path"], "rb").read()).hexdigest()[:10]
    hs.append(h)
    print(f"   {os.path.basename(f['path']):26s} md5={h}")
uniq = len(set(hs))
print(f"\n   唯一画面 {uniq}/{len(frames)}  {'✅' if uniq == len(frames) else '⚠ 有重复'}")
times = [f["time"] for f in frames]
print(f"   时间点分散度: {[round(t,1) for t in times]}")
spread = (max(times) - min(times)) if times else 0
print(f"   覆盖跨度 {spread:.1f}s / 全片 {probe_duration(VIDEO):.1f}s  "
      f"{'✅' if spread > probe_duration(VIDEO) * 0.2 else '⚠ 取点过于集中'}")

print("\n" + "═" * 76)
print("③ 按指定时间点单独取一帧（用户可以自己选时刻）")
mid = probe_duration(VIDEO) / 2
out = extract_single_frame(VIDEO, mid, os.path.join(W, f"at_{mid:.1f}s.jpg"))
print(f"   在 {mid:.2f}s 取帧 → {'✅ ' + out if out else '❌ 失败'}")
if out:
    print(f"   大小 {os.path.getsize(out)} 字节")

print("\n" + "═" * 76)
print("④ 异常输入要说人话")
for bad, label in [(os.path.join(W, "nope.mp4"), "不存在的文件")]:
    rr = extract_keyframes(bad, os.path.join(W, "kf2"))
    print(f"   {label}: ok={rr.get('ok')} → {rr.get('error')}")
# 拿图片当视频喂进去（用户传错格式）
if frames:
    rr = extract_keyframes(frames[0]["path"], os.path.join(W, "kf3"))
    print(f"   把 jpg 当视频喂: ok={rr.get('ok')} → {str(rr.get('error'))[:80]}")

print("\n产物目录:", os.path.join(W, "kf"))
