# -*- coding: utf-8 -*-
r"""联络表（`core.framesheet`）的离线回归。

为什么要给这个"看起来只是拼图"的工具写测试：
2026-09-13 我差点被**自己的拼图工具**骗了 —— 第一版用 `scale=360:-2`，
各格高度随画幅变化（202/203…），而 `concat`+`tile` 要求同尺寸，
结果 12 格里 **5 格全黑、1 格看着像"倒过来了"**。
我差点把这两个**不存在的画面缺陷**写进结论，靠"先量化原片（近黑帧 0）"才拦住。

所以这里锁死三件事：
  ① 拼出来的图**尺寸 = 列数×格子宽 × 行数×格子高**（同尺寸是硬要求）；
  ② 图例条数 = 实际抽到的帧数（不能"图例说有 15 格、图里只有 7 格"）；
  ③ 一个坏片段**只跳过它**，不让整张表作废。
"""
import io
import os
import subprocess
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))

from imageio_ffmpeg import get_ffmpeg_exe                      # noqa: E402

from core.framesheet import build_sheet, FRAME_W, FRAME_H      # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def make_clip(path, color, dur=3.0):
    subprocess.run([get_ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi", "-i",
                    f"color=c={color}:s=320x180:r=24:d={dur}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", path],
                   capture_output=True)
    return path


def png_size(path):
    """读 PNG 的宽高（只看 IHDR，不依赖 PIL）。"""
    with open(path, "rb") as f:
        d = f.read(33)
    if len(d) < 33 or d[:8] != b"\x89PNG\r\n\x1a\n":
        return (0, 0)
    return (int.from_bytes(d[16:20], "big"), int.from_bytes(d[20:24], "big"))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="vf_sheet_")
    a = make_clip(os.path.join(tmp, "a.mp4"), "red")
    b = make_clip(os.path.join(tmp, "b.mp4"), "blue")
    check("T0 造出两段测试片段",
          os.path.exists(a) and os.path.exists(b))

    out = os.path.join(tmp, "sheet.png")
    r = build_sheet([("分镜1", a), ("分镜2", b)], out, per_clip=3, cols=6,
                    work_dir=tmp)
    check("T1 拼图成功", bool(r.get("ok")), str(r.get("error") or r.get("path")))
    if r.get("ok"):
        w, h = png_size(r["path"])
        check("T2 尺寸 = 列×格宽 × 行×格高（同尺寸硬要求）",
              (w, h) == (FRAME_W * 6, FRAME_H * 1), f"{w}x{h}")
        check("T3 图例条数 = 实际抽到的帧数",
              len(r["legend"]) == r["cells"] == 6,
              f"cells={r['cells']} legend={len(r['legend'])}")
        check("T4 图例带镜头标签与时间点",
              all(("分镜" in x and "t=" in x) for x in r["legend"]),
              r["legend"][0] if r["legend"] else "")
        check("T5 没有跳过任何片段", not r.get("skipped"), str(r.get("skipped")))

    # 一个坏片段只跳过它，不影响其余
    out2 = os.path.join(tmp, "sheet2.png")
    r2 = build_sheet([("分镜1", a), ("分镜2", os.path.join(tmp, "nope.mp4")),
                      ("分镜3", b)], out2, per_clip=2, cols=4, work_dir=tmp)
    check("T6 坏片段只跳过、其余照常出图",
          bool(r2.get("ok")) and r2["cells"] == 4 and len(r2["skipped"]) == 1,
          f"cells={r2.get('cells')} skipped={r2.get('skipped')}")

    # 负对照：全是坏路径 → 明确失败，而不是产出一张空图
    r3 = build_sheet([("分镜1", os.path.join(tmp, "x.mp4"))],
                     os.path.join(tmp, "sheet3.png"), work_dir=tmp)
    check("T7 全坏时明确返回失败（不产出空图）",
          (not r3.get("ok")) and not os.path.exists(os.path.join(tmp, "sheet3.png")),
          str(r3.get("error")))
    r4 = build_sheet([], os.path.join(tmp, "sheet4.png"), work_dir=tmp)
    check("T8 空列表不崩", not r4.get("ok"), str(r4.get("error")))

    print("\n" + "=" * 70)
    print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
    for f in FAIL:
        print("  [x] " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
