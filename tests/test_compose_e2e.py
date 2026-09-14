# -*- coding: utf-8 -*-
"""端到端成片测试：分镜 → 配音 → 画面 → 片段 → 拼接 → 字幕 → final.mp4"""
import asyncio
import os
import sys

BK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
sys.path.insert(0, BK)

DATA = os.path.join(os.environ["LOCALAPPDATA"], "VideoForge", "data")
os.environ["VIDEOFORGE_DATA_DIR"] = DATA

from core.db import Database            # noqa: E402
from core import compose                 # noqa: E402

PID = sys.argv[1] if len(sys.argv) > 1 else "e62a182d-71ef-49ca-951b-921166c4f6c3"


async def main():
    db = Database(os.path.join(DATA, "videoforge.db"))
    settings = db.get_all_settings()
    proj = db.get_project(PID)
    print("项目:", (proj or {}).get("title"))
    shots = db.list_shots(PID)
    print("分镜:", len(shots))
    for s in shots:
        n = compose.narration_for_shot(s)
        print("   [%s] %.0fs  配音文本(%d字): %s" % (
            s.get("order_index"), float(s.get("duration_seconds") or 0), len(n), n[:56]))

    print("\n开始合成…")
    res = await compose.compose_project(
        PID, db=db, settings=settings,
        voice_id="edge:zh-CN-XiaoxiaoNeural",
        with_voice=True, burn_subtitles=True,
        progress=lambda stage, pct, msg: print("   %5.1f%%  %s" % (pct * 100, msg)),
    )
    print("\n===== 结果 =====")
    print("ok       :", res.ok)
    print("成片     :", res.output_path)
    print("时长     :", res.duration, "秒")
    print("分镜数   :", res.clip_count)
    print("字幕文件 :", res.srt_path)
    print("warnings :")
    for w in res.warnings:
        print("   -", w)
    print("errors   :")
    for e in res.errors:
        print("   -", e)

    if res.ok and os.path.exists(res.output_path):
        print("\n文件大小: %.2f MB" % (os.path.getsize(res.output_path) / 1048576))
        rc, err = await compose._run([
            compose._ffprobe(), "-v", "error", "-show_entries",
            "stream=codec_type,codec_name,width,height,duration",
            "-show_entries", "format=duration,size",
            "-of", "json", res.output_path])
        print(err or "(ffprobe ok)")
        import json
        try:
            print(json.dumps(json.loads(err), ensure_ascii=False, indent=1))
        except Exception:
            pass
    return 0 if res.ok else 1


sys.exit(asyncio.run(main()))
