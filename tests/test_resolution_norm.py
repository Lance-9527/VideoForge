# -*- coding: utf-8 -*-
"""验证分辨率归一化：不同写法都能通过所有适配器的参数校验"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))
from core.adapters import get_adapter, VideoGenRequest  # noqa: E402
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CASES = [("hailuo", "1080p"), ("hailuo", "720p"), ("hailuo", "1080P"),
         ("kling", "1080p"), ("seedance", "1080p"), ("wanx", "720p"),
         ("jimeng", "1080p"), ("runway", "720p"), ("sora", "1080p"),
         ("pika", "720p"), ("luma", "1080p"), ("cogvideox", "720p"), ("minimax", "1080p")]

bad = 0
for prov, res in CASES:
    try:
        a = get_adapter(prov)
    except Exception as e:
        print("  %-10s 适配器加载失败: %s" % (prov, e))
        bad += 1
        continue
    if not getattr(a, "usable_for_video", True):
        print("  [SKIP] %-10s 非视频适配器（已被排除在下拉框外，符合预期）" % prov)
        continue
    dur = max(5, a.min_duration or 5)
    if a.max_duration and dur > a.max_duration:
        dur = a.max_duration
    r = VideoGenRequest(prompt="x", duration=dur, aspect_ratio="16:9", resolution=res)
    err = a.validate_request(r)
    flag = "OK " if not err else "ERR"
    if err:
        bad += 1
    print("  [%s] %-10s %-7s -> 归一=%-7s %s" % (flag, prov, res, r.resolution, err or ""))

print()
print("RESULT:", "ALL PASS" if bad == 0 else "%d 个失败" % bad)
sys.exit(0 if bad == 0 else 1)
