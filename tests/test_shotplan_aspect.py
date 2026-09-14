"""拍摄计划的**画幅硬约束**测试（真实事故回归）。

事故：为省一次调用把 16:9 分镜的分辨率从 1080P 降到 768P（1080P 只给 6s、
768P 给 10s），海螺 2.3 在 768P 下返回 768×768（1:1）→ 成片裁掉 44% 画面。
规划层必须把"降档不能跨越画幅"当成硬约束，而不是只比调用次数。

用法：python tests/test_shotplan_aspect.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.shotplan import plan_shot  # noqa: E402

PASS = 0
FAIL: list = []


def check(cond: bool, msg: str) -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  ✓ {msg}")
    else:
        FAIL.append(msg)
        print(f"  ✗ {msg}")


def mk_shot(nominal: float, aspect: str) -> dict:
    return {
        "id": "s1",
        "duration_seconds": nominal,
        "aspect_ratio": aspect,
        "layer1_overview": "临冬城的雪夜，镜头缓慢推进",
        "layer2_timeline": json.dumps([
            {"start": 0, "end": nominal, "action": "镜头缓慢推进",
             "dialogue": "布兰：我看见了龙。"}], ensure_ascii=False),
    }


# 产品里真实存在的海螺 2.3 能力（1080P 只给 6s、768P 给 6/10s）
H23 = {"id": "MiniMax-Hailuo-2.3", "label": "海螺 Hailuo 2.3",
       "caps": {"768P": [6, 10], "1080P": [6]}}
# 海螺 02：同为 768P，实测出的是 **16:9**（1366x768）—— 这正是最省钱的合法组合
H02 = {"id": "MiniMax-Hailuo-02", "label": "海螺 Hailuo 02",
       "caps": {"768P": [6, 10], "1080P": [6]}}
# 画幅映射**没有实测过**的型号，用来验证"不凭猜拦"
HX = {"id": "某个没测过画幅的型号", "label": "未验证型号",
      "caps": {"768P": [6, 10], "1080P": [6]}}


def main() -> None:
    print("── 1. 16:9 分镜：不许为了省钱降到 768P（实测是 1:1）──")
    p = plan_shot(mk_shot(20, "16:9"), model_entry=H23, resolution="1080P",
                  provider="hailuo")
    check(p["resolution"].upper() == "1080P",
          f"20s/16:9 的规划分辨率必须是 1080P，实得 {p['resolution']}")
    check(all(str(s["resolution"]).upper() == "1080P" for s in p["segments"]),
          "每一段的分辨率都必须是 1080P")
    txt = " ".join(p["adjustments"] + p["warnings"])
    check("768P" in txt and "1:1" in txt,
          "必须如实说明 768P 实测出 1:1 所以被排除")
    check("没有 768P" not in txt,
          "不能说『该模型没有 768P』——它存在，只是画幅不符（假话回归）")
    check(p["segment_count"] >= 2,
          f"画幅优先于调用次数：宁可多分段也不出方形片（实得 {p['segment_count']} 段）")
    check(p["aspect"]["ok"] is True and p["aspect"]["actual"] == "16:9",
          f"计划里如实标注实际画幅 16:9，实得 {p['aspect']}")

    print("── 2. 1:1 分镜：768P 合法，允许一次出完省钱 ──")
    p2 = plan_shot(mk_shot(20, "1:1"), model_entry=H23, resolution="1080P",
                   provider="hailuo")
    check(p2["resolution"].upper() == "768P",
          f"1:1 分镜用 768P 是画幅相符的，应允许降档，实得 {p2['resolution']}")
    check(p2["segment_count"] == 2,
          f"20s 用 2×10s 出完（省调用），实得 {p2['segment_count']} 段")
    check(p2["aspect"]["ok"] is True, "1:1 分镜的画幅自检应通过")

    print("── 3. 海螺 02 + 768P：同为 768P 却保持 16:9 → 允许降档省钱 ──")
    p3a = plan_shot(mk_shot(20, "16:9"), model_entry=H02, resolution="1080P",
                    provider="hailuo")
    check(p3a["resolution"].upper() == "768P",
          f"02 的 768P 实测是 16:9，应允许降档一次出完，实得 {p3a['resolution']}")
    check(p3a["segment_count"] == 2,
          f"20s 用 2×10s 出完，实得 {p3a['segment_count']} 段")
    check(p3a["aspect"]["ok"] is True and p3a["aspect"]["actual"] == "16:9",
          "并如实标注实测画幅 16:9")

    print("── 4. 没实测过画幅的型号：不拦、也不编 ──")
    p3 = plan_shot(mk_shot(20, "16:9"), model_entry=HX, resolution="1080P",
                   provider="hailuo")
    check(p3["resolution"].upper() in ("768P", "1080P"),
          f"没实测过的型号照旧规划，实得 {p3['resolution']}")
    check(p3["aspect"]["actual"] == "",
          "没实测过就如实说不知道（actual 为空），不能猜一个画幅出来")
    check(not any("1:1" in x for x in p3["adjustments"] + p3["warnings"]),
          "没实测过就不许声称它出 1:1")

    print("── 5. 有首帧 + 实测『首帧说了算』的型号：允许降档省钱 ──")
    p6 = plan_shot(mk_shot(20, "16:9"), model_entry=H23, resolution="1080P",
                   provider="hailuo", first_frame_governs=True)
    check(p6["resolution"].upper() == "768P",
          f"首帧决定画幅时，2.3 的 768P 不再是方形 → 应降档省调用，实得 {p6['resolution']}")
    check(p6["segment_count"] == 2,
          f"20s 用 2×10s 出完（4 次降到 2 次），实得 {p6['segment_count']} 段")
    check(p6["aspect"]["ok"] is True,
          "且不能被自己的画幅自检误报（首帧会决定画幅）")
    p7 = plan_shot(mk_shot(20, "16:9"), model_entry=H23, resolution="1080P",
                   provider="hailuo", first_frame_governs=False)
    check(p7["resolution"].upper() == "1080P",
          "同一型号没有首帧时（文生视频）仍必须躲开 768P 的方形")

    print("── 6. 全部分辨率都画幅不符：不能制造死路，但要明确告警 ──")
    p4 = plan_shot(mk_shot(12, "9:16"), model_entry=H23, resolution="1080P",
                   provider="hailuo")
    check(p4["segment_count"] >= 1,
          "仍要给出可执行的计划（不能因为画幅全不符就什么都不生成）")
    check(any("画幅" in w for w in p4["warnings"]),
          "必须给出画幅告警，让用户知道成片会裁画面")

    print("── 7. 不传 provider：行为与旧版一致（向后兼容）──")
    p5 = plan_shot(mk_shot(20, "16:9"), model_entry=H23, resolution="1080P")
    check(p5["resolution"].upper() == "768P",
          f"不传 provider 时不做画幅过滤（老调用点不受影响），实得 {p5['resolution']}")

    print(f"\n{'=' * 60}\n通过 {PASS} 项，失败 {len(FAIL)} 项\n{'=' * 60}")
    for f in FAIL:
        print("  ✗", f)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
