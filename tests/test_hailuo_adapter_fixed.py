# -*- coding: utf-8 -*-
"""验证修复后的海螺适配器：真实调用 MiniMax 生成视频并落地为本地文件"""
import asyncio
import os
import sys
import time

BK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
sys.path.insert(0, BK)
DATA = os.path.join(os.environ["LOCALAPPDATA"], "VideoForge", "data")
os.environ["VIDEOFORGE_DATA_DIR"] = DATA

from core.db import Database                      # noqa: E402
from core.adapters import get_adapter, VideoGenRequest  # noqa: E402

db = Database(os.path.join(DATA, "videoforge.db"))
key = ""
for k, v in (db.get_all_settings().get("api_keys") or {}).items():
    if str(k).strip().lower() == "hailuo" and v:
        key = str(v).strip()

OUT = os.path.join(os.environ["TEMP"], "vf_hailuo_fixed.mp4")


async def main():
    print("适配器 BASE_URL 检查")
    ad = get_adapter("hailuo", api_key=key, config={"model_name": "MiniMax-Hailuo-02"})
    print("  BASE_URL =", ad.BASE_URL)
    assert "minimaxi.com" in ad.BASE_URL, "域名未修正！"
    print("  ✅ 域名已修正为 api.minimaxi.com")

    print("\n发起真实生成（约 1-2 分钟）…")
    t0 = time.time()
    req = VideoGenRequest(
        prompt="镜头：中景，缓慢推近。一只橘猫在窗台上伸懒腰打哈欠，"
               "午后阳光斜射，毛发细节清晰，浅景深。风格：电影级写实，高细节",
        duration=6, aspect_ratio="16:9", resolution="768P",
    )
    r = await ad.generate(req)
    print("  耗时 %.0fs" % (time.time() - t0))
    print("  success =", r.success)
    print("  error   =", r.error)
    print("  task_id =", r.task_id)
    print("  video_url =", (r.video_url or "")[:110])

    if r.success and r.video_url:
        p = await ad.download_to_local(r.video_url, OUT)
        size = os.path.getsize(p) if p and os.path.exists(p) else 0
        print("  已下载 =", p, size, "bytes")
        ok = size > 100_000
        print("\n" + ("=" * 56))
        print("RESULT:", "PASS — 海螺适配器已能真正出片" if ok else "FAIL — 文件过小")
        return 0 if ok else 1
    print("\nRESULT: FAIL — 生成未成功")
    return 1


sys.exit(asyncio.run(main()))
