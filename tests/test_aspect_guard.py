"""画幅守卫测试（真实事故回归）。

事故：分镜 16:9，规划层为省调用把分辨率降到 768P，海螺返回 768x768（1:1），
成片归一化时静默裁掉 44% 画面，没有任何一层报警。

本测试覆盖：
  1) 纯函数：画幅解析 / 归类 / 偏差 / 裁切损失；
  2) 实测表：768P=1:1、1080P=16:9（海螺 2.3），据此能否给出替代分辨率；
  3) 真文件：ffmpeg 生成方形与宽屏片，`guard_clip` 必须判 fatal 并说明损失。

用法：python tests/test_aspect_guard.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core import aspect as A  # noqa: E402

PASS = 0
FAIL: list = []


def ok(cond: bool, msg: str) -> None:
    global PASS
    if cond:
        PASS += 1
    else:
        FAIL.append(msg)
        print(f"  ✗ {msg}")


def eq(got, want, msg: str) -> None:
    ok(got == want, f"{msg} —— 期望 {want!r}，实得 {got!r}")


def near(got, want, tol, msg: str) -> None:
    ok(abs(float(got) - float(want)) <= tol,
       f"{msg} —— 期望 ≈{want}（±{tol}），实得 {got}")


def gen(path: str, size: str) -> bool:
    import imageio_ffmpeg
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    p = subprocess.run(
        [ff, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc=size={size}:rate=24:duration=1",
         "-pix_fmt", "yuv420p", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    return os.path.exists(path) and os.path.getsize(path) > 1024


def gen_img(path: str, size: str) -> bool:
    """造一张**静态图**（场景参考图的真实形态就是 jpeg 静图）。"""
    import imageio_ffmpeg
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run(
        [ff, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc=size={size}:rate=1",
         "-frames:v", "1", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    return os.path.exists(path) and os.path.getsize(path) > 512


def main() -> None:
    # 台账指向临时文件：测试必须自给自足，不能被本机历史测量影响
    import tempfile as _tf
    _led = os.path.join(_tf.mkdtemp(prefix="vf_aspect_led_"), "ledger.json")
    os.environ["VIDEOFORGE_ASPECT_LEDGER"] = _led

    print("── 1. 画幅解析 ──")
    near(A.parse_aspect("16:9"), 16 / 9, 1e-6, "16:9")
    near(A.parse_aspect("9:16"), 9 / 16, 1e-6, "9:16")
    near(A.parse_aspect("1:1"), 1.0, 1e-6, "1:1")
    near(A.parse_aspect(" 4 x 3 "), 4 / 3, 1e-6, "4 x 3（容错写法）")
    eq(A.parse_aspect("竖屏"), None, "认不出的写法返回 None")
    eq(A.parse_aspect(None), None, "None 返回 None")

    print("── 2. 归类与偏差 ──")
    eq(A.classify(1920, 1080), "16:9", "1920x1080 → 16:9")
    eq(A.classify(768, 768), "1:1", "768x768 → 1:1")
    eq(A.classify(1080, 1920), "9:16", "1080x1920 → 9:16")
    ok(A.classify(1000, 333).startswith("other:"), "奇怪比例如实标 other")
    eq(A.classify(0, 0), "unknown", "量不到尺寸 → unknown")
    ok(A.is_match(1920, 1080, "16:9"), "1920x1080 与 16:9 相符")
    ok(A.is_match(1280, 720, "16:9"), "1280x720 与 16:9 相符")
    ok(not A.is_match(768, 768, "16:9"), "768x768 与 16:9 不符")
    ok(not A.is_match(1920, 1200, "16:9"), "16:10 与 16:9 不符")
    near(A.deviation(768, 768, "16:9"), 0.4375, 0.005, "1:1 对 16:9 的相对偏差")
    ok(A.deviation(768, 768, "16:9") > A.ASPECT_FATAL,
       "1:1 vs 16:9 属于结构性不符（> fatal 阈值）")

    print("── 3. 裁切损失（这就是画幅不符的真实代价）──")
    near(A.crop_loss(768, 768, "16:9"), 0.4375, 0.005, "方形铺满 16:9 → 丢 44%")
    near(A.crop_loss(1920, 1080, "16:9"), 0.0, 1e-6, "本来就 16:9 → 不丢")
    near(A.crop_loss(1080, 1920, "16:9"), 1 - (9 / 16) / (16 / 9), 0.01,
         "竖屏铺满 16:9 → 丢大部分")
    near(A.crop_loss(2560, 1080, "16:9"), 1 - (16 / 9) / (2560 / 1080), 0.01,
         "太宽的片铺满 16:9 → 裁左右")

    print("── 4. 实测表：海螺 2.3 / 02 / video-01 ──")
    eq(A.expected_aspect("hailuo", "MiniMax-Hailuo-2.3", "768P"), "1:1",
       "2.3 的 768P 实测出 1:1")
    eq(A.expected_aspect("hailuo", "MiniMax-Hailuo-2.3", "1080P"), "16:9",
       "2.3 的 1080P 实测出 16:9")
    eq(A.expected_aspect("hailuo", "MiniMax-Hailuo-02", "768P"), "16:9",
       "★ 同为 768P，海螺 02 出的是 16:9（1366x768）—— 画幅随型号变，不能跨型号套用")
    eq(A.expected_aspect("hailuo", "MiniMax-Hailuo-02", "1080P"), "16:9",
       "海螺 02 的 1080P 也是 16:9")
    eq(A.expected_aspect("hailuo", "video-01", "1080P"), "16:9",
       "video-01 的 1080P 是 1280x720（也算 16:9）")
    eq(A.resolution_conflicts_aspect("hailuo", "MiniMax-Hailuo-02", "768P", "16:9"),
       None, "★ 16:9 分镜用海螺 02 + 768P 不冲突（这是最省钱的合法组合）")
    eq(A.resolutions_matching_aspect("hailuo", "MiniMax-Hailuo-02", "16:9",
                                     {"768P": [6, 10], "1080P": [6]}),
       ["768P", "1080P"], "海螺 02 的两档分辨率都保持 16:9")
    eq(A.expected_aspect("hailuo", "MiniMax-Hailuo-2.3", "512P"), None,
       "没实测过的分辨率必须返回 None（不猜）")
    eq(A.expected_aspect("hailuo", "某个没测过的型号", "768P"), None,
       "没测过的型号返回 None")
    eq(A.resolution_conflicts_aspect("hailuo", "MiniMax-Hailuo-2.3", "768P", "16:9"),
       "1:1", "16:9 分镜用 768P → 冲突（会出 1:1）")
    eq(A.resolution_conflicts_aspect("hailuo", "MiniMax-Hailuo-2.3", "1080P", "16:9"),
       None, "16:9 分镜用 1080P → 不冲突")
    eq(A.resolutions_matching_aspect("hailuo", "MiniMax-Hailuo-2.3", "16:9",
                                     {"768P": [6, 10], "1080P": [6]}),
       ["1080P"], "16:9 分镜在 2.3 上只有 1080P 画幅相符")
    eq(A.resolutions_matching_aspect("hailuo", "MiniMax-Hailuo-2.3", "1:1",
                                     {"768P": [6, 10], "1080P": [6]}),
       ["768P"], "1:1 分镜则可以放心用 768P")

    print("── 5. 真文件测量（ffmpeg 造片）──")
    tmp = tempfile.mkdtemp(prefix="vf_aspect_")
    sq = os.path.join(tmp, "square.mp4")
    ws = os.path.join(tmp, "wide.mp4")
    if not (gen(sq, "768x768") and gen(ws, "1920x1080")):
        print("  ⚠ ffmpeg 造片失败，跳过真实文件部分")
    else:
        eq(A.probe_size(sq), (768, 768), "量到方形片真实尺寸")
        eq(A.probe_size(ws), (1920, 1080), "量到宽屏片真实尺寸")

        g = A.guard_clip(sq, "16:9", provider="hailuo",
                         model="MiniMax-Hailuo-2.3", resolution="768P")
        eq(g["ok"], False, "方形片对 16:9 分镜 → guard 不通过")
        eq(g["severity"], "fatal", "方形片对 16:9 → fatal（不是小警告）")
        near(g["crop_loss"], 0.4375, 0.01, "guard 报出 44% 裁切损失")
        ok("1080P" in (g["suggest"] or []),
           "guard 给出画幅相符的替代分辨率 1080P")
        ok("裁掉" in g["message"] and "768x768" in g["message"],
           "提示语把实测尺寸与代价都说清楚")

        g2 = A.guard_clip(ws, "16:9", provider="hailuo",
                          model="MiniMax-Hailuo-2.3", resolution="1080P")
        eq(g2["ok"], True, "宽屏片对 16:9 分镜 → 通过")
        eq(g2["severity"], "ok", "通过时严重度为 ok")
        eq(g2["crop_loss"], 0.0, "通过时不丢画面")

        g3 = A.guard_clip(os.path.join(tmp, "不存在.mp4"), "16:9")
        eq(g3["measured"], False, "文件不存在 → 如实说没量到")
        eq(g3["severity"], "unknown", "量不到不能算通过（severity=unknown）")

    print("── 6. 首帧归一化：方形场景图必须裁成目标画幅再喂给模型 ──")
    tmp2 = tempfile.mkdtemp(prefix="vf_conform_")
    sq2 = os.path.join(tmp2, "scene_1x1.jpg")
    ws2 = os.path.join(tmp2, "scene_16x9.jpg")
    if not (gen_img(sq2, "1024x1024") and gen_img(ws2, "1920x1080")):
        print("  ⚠ ffmpeg 造图失败，跳过首帧归一化部分")
    else:
        out = A.conform_image(sq2, "16:9")
        ok(out != sq2, "方形场景图被改写了（不是原样返回）")
        eq(A.probe_size(out), (1280, 720),
           "归一化后的首帧是 16:9（1280x720）")
        near(A.crop_loss(1024, 1024, "16:9"), 0.4375, 0.01,
             "并如实保留了『裁掉 44%』这个代价")
        same = A.conform_image(ws2, "16:9")
        eq(same, ws2, "本来就是 16:9 的图原样返回（不重编码、不掉画质）")
        p_out = A.conform_image(sq2, "9:16")
        w2, h2 = A.probe_size(p_out)
        ok(h2 > w2, f"竖画幅目标要出竖图，实得 {w2}x{h2}")
        eq(A.conform_image(os.path.join(tmp2, "不存在.jpg"), "16:9"),
           os.path.join(tmp2, "不存在.jpg"), "文件不存在时原样返回（不抛异常）")

    print("── 7. 实测台账：本机量到的事实优先于手写表 ──")
    # 一个手写表里没有的型号 —— 只靠"量一次、记一笔"，下回就能用
    eq(A.expected_aspect("hailuo", "某个没测过的型号", "1080P"), None,
       "记账前：没依据 → 不知道")
    got = A.record_measurement("hailuo", "某个没测过的型号", "1080P", 1920, 1080)
    eq(got, "16:9", "记账返回量到的画幅")
    eq(A.expected_aspect("hailuo", "某个没测过的型号", "1080P"), "16:9",
       "记账后：本机实测事实可用")
    eq(A.expected_aspect("hailuo", "某个没测过的型号", "768P"), None,
       "同一型号的其它分辨率仍未知 —— 不能顺推")
    ok(os.path.exists(_led), "台账落盘了")
    A.record_measurement("hailuo", "某个没测过的型号", "768P", 768, 768)
    eq(A.resolutions_matching_aspect("hailuo", "某个没测过的型号", "16:9"),
       ["1080P"], "台账能给出画幅相符的分辨率（768P 是方形，排除）")
    eq(A.resolution_conflicts_aspect("hailuo", "某个没测过的型号", "768P", "16:9"),
       "1:1", "台账也参与冲突判断")

    print(f"\n{'=' * 56}\n通过 {PASS} 项，失败 {len(FAIL)} 项\n{'=' * 56}")
    for f in FAIL:
        print("  ✗", f)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
