# -*- coding: utf-8 -*-
"""验证交叉溶解转场拼接 + 层间衔接提示词"""
import asyncio
import json
import os
import shutil
import sys
import tempfile

# ★ 直接跑这个套件时也要能打印产品告警里的任意字符。
#   踩过：产品告警里出现 `↔`，GBK 控制台下 print 直接 UnicodeEncodeError，
#   套件挂掉、看着像产品坏了。（门禁那边另外统一设了 PYTHONIOENCODING=utf-8。）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
sys.path.insert(0, BK)
DATA = os.path.join(os.environ["LOCALAPPDATA"], "VideoForge", "data")
os.environ["VIDEOFORGE_DATA_DIR"] = DATA

from core.db import Database          # noqa: E402
from core import compose              # noqa: E402
from core.videoprompt import build_video_prompt  # noqa: E402

PID = sys.argv[1] if len(sys.argv) > 1 else "e62a182d-71ef-49ca-951b-921166c4f6c3"
db = Database(os.path.join(DATA, "videoforge.db"))
settings = dict(db.get_all_settings() or {})

# ★★ 这个测试会**真的跑一遍完整合成**（`compose_project`），而它的产物目录
#    默认是 `outputs/<pid>/` —— 里面就躺着用户那个项目**已经出好的成片**
#    `final.mp4` 和旁挂字幕 `subtitles.srt`。
#
#    2026-09-13 实测：本套件在回归里跑过一次，就把真实项目
#    「我计划做一个抗日题材的短视频…」(e62a182d) 的 `final.mp4`
#    覆盖成了这次测试拼出来的版本。和 §18.29 是同一类错误，
#    区别只在于上次是流水线的 srt，这次直接把成片盖了。
#
#    `compose_project` 的落盘位置由 settings 的 `output_dir` 决定
#    （`compose.py:1210`），所以这里把它指到一个临时目录：
#    合成照跑、断言照做，用户的成片一个字节都不动。
#    `cache_dir` 故意不重定向 —— `cache/` 是可重建的中间产物、
#    不是交付物，重定向它会让本测试失去"复用已渲染片段"的能力。
_SCRATCH = tempfile.mkdtemp(prefix="vf_continuity_")
settings["output_dir"] = os.path.join(_SCRATCH, "outputs")
shots = sorted(db.list_shots(PID) or [], key=lambda s: s.get("order_index") or 0)
scenes = {s["id"]: s for s in (db.list_scenes(PID) or [])}
chars = {c["id"]: c for c in (db.list_characters(PID) or [])}

print("=== ① 层间衔接提示词验证 ===")
s1 = shots[1] if len(shots) > 1 else shots[0]
cids = s1.get("character_ids") or []
if isinstance(cids, str):
    cids = json.loads(cids)
vp = build_video_prompt(
    s1, scene=scenes.get(s1.get("scene_id")), characters=[chars[c] for c in cids if c in chars],
    duration=5, provider="local",
    prev_shot=shots[0] if len(shots) > 1 else None,
    next_shot=shots[2] if len(shots) > 2 else None)
txt = vp["prompt"]
print("  提示词长度:", len(txt))
print("  含「承接上一镜」:", "承接上一镜" in txt)
print("  含「为下一镜留出衔接」:", "为下一镜留出衔接" in txt)
if "承接上一镜" in txt:
    i = txt.index("承接上一镜")
    print("  片段:", txt[i:i + 120].replace("\n", " "))

print("\n=== ② 交叉溶解转场拼接验证 ===")
res = asyncio.run(compose.compose_project(
    PID, db=db, settings=settings,
    voice_id="edge:zh-CN-XiaoxiaoNeural", with_voice=True, burn_subtitles=True,
    auto_images=False, transition="fade", transition_sec=0.5,
    progress=lambda st, pct, msg: print("   %5.1f%%  %s" % (pct * 100, msg)) if pct in (0.75, 1.0) else None,
))
print("\n  ok =", res.ok, "时长 =", res.duration, "片段 =", res.clip_count)
for w in (res.warnings or [])[:5]:
    print("  warning:", str(w)[:160])
for e in (res.errors or [])[:3]:
    print("  error:", str(e)[:200])
if res.ok and os.path.exists(res.output_path):
    print("  文件: %.2f MB" % (os.path.getsize(res.output_path) / 1048576))
    rc, out = asyncio.run(compose._run([compose._ffmpeg(), "-hide_banner", "-i", res.output_path]))
    import re
    m = re.search(r"Duration:\s*([\d:.]+)", out)
    print("  ffmpeg 确认时长:", m.group(1) if m else "?")
print()
print("RESULT:", "PASS" if res.ok else "FAIL")
sys.exit(0 if res.ok else 1)
