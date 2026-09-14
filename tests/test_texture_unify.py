# -*- coding: utf-8 -*-
"""纹理统一：先标定 unsharp 的真实效果，再验证它能不能真的把差异压下去。

═══ 为什么必须先标定 ═══
`plan_texture_match` 里我写了一条假设：
    · 高频能量 ≈ 原来 × (1 + unsharp_amount)
也就是说 unsharp=0.5 应该让拉普拉斯标准差涨 50%。**这是猜的。**
ffmpeg 的 unsharp 是"原图 + amount×(原图−模糊)"，它对"拉普拉斯标准差"的
影响跟画面内容有关，不一定是线性的。所以先量真实曲线，再按量到的去改系数
—— 目标里要求"纹理不统一"是**可量化**的，那就不能拿一个没验证的公式去凑。

运行：python tests/test_texture_unify.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "backend"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from analyze_seams import clips_for_project, default_db, probe  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return cond


def ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


async def part_a_calibration(tmp: str):
    print("\n" + "=" * 76)
    print("A. 标定：unsharp 到底能把高频能量改多少")
    print("=" * 76)
    from core import seamfix

    clips = [c["path"] for c in clips_for_project(
        "19636f68-9d0f-4555-b268-1088ca018b33", default_db())]
    if not clips:
        check("A0 找到真实素材", False, "库里无片段")
        return None
    src = clips[0]
    base = await seamfix._texture_energy(src)
    print(f"  素材 {os.path.basename(src)}  基准高频能量 = {base:.3f}")
    print(f"\n  {'unsharp':>9} {'实测能量':>10} {'实测倍数':>9} {'线性预测':>9} {'误差':>8}")
    rows = []
    for a in (-0.8, -0.4, 0.0, 0.4, 0.8, 1.1):
        out = os.path.join(tmp, f"us_{a:+.1f}.mp4")
        subprocess.run([ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", src,
                        "-vf", f"unsharp=5:5:{a:.3f}:5:5:0", "-an",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                        "-pix_fmt", "yuv420p", out], capture_output=True, timeout=900)
        e = await seamfix._texture_energy(out) if os.path.exists(out) else 0.0
        got = e / base if base else 0
        pred = 1.0 + a
        rows.append((a, e, got, pred))
        print(f"  {a:>+9.2f} {e:>10.3f} {got:>9.3f} {pred:>9.3f} "
              f"{abs(got - pred):>8.3f}")
    # 线性假设成立吗？看两端误差
    worst = max(abs(g - p) for _, _, g, p in rows)
    print(f"\n  线性假设最大误差 = {worst:.3f}")
    check("A1 unsharp=0 时能量不变（标定自身可信）",
          any(abs(a) < 1e-6 and abs(g - 1.0) < 0.06 for a, _, g, _ in rows),
          str([(a, round(g, 3)) for a, _, g, _ in rows if abs(a) < 1e-6]))
    check("A2 unsharp 增加 → 高频能量单调上升",
          [g for _, _, g, _ in rows] == sorted(g for _, _, g, _ in rows),
          str([round(g, 3) for _, _, g, _ in rows]))
    # 实测灵敏度：中段斜率。用它反解 amount，而不是想当然的 1+a
    mid = [(a, g) for a, _, g, _ in rows]
    slope = (mid[-2][1] - mid[1][1]) / (mid[-2][0] - mid[1][0])
    from core import seamfix as _sf
    print(f"  实测中段灵敏度 = {slope:.3f}  （代码里写的是 "
          f"TEXTURE_SENSITIVITY = {_sf.TEXTURE_SENSITIVITY}）")
    check("A3 代码里的灵敏度与实测相符（±0.08）",
          abs(slope - _sf.TEXTURE_SENSITIVITY) < 0.08,
          f"实测 {slope:.3f} vs 代码 {_sf.TEXTURE_SENSITIVITY}")
    check("A4 如实承认可修范围有限（±33%），不是'想拉多齐就多齐'",
          0.25 < _sf.TEXTURE_SENSITIVITY * _sf.TEXTURE_SHARP_LIMIT < 0.45,
          f"可修 ±{_sf.TEXTURE_SENSITIVITY*_sf.TEXTURE_SHARP_LIMIT*100:.0f}%")
    print(f"  → 标定结论：线性假设是错的（误差 {worst:.3f}），"
          f"已改用实测灵敏度 {slope:.2f} 反解")
    return src


async def part_b_real(tmp: str):
    print("\n" + "=" * 76)
    print("B. 真实片段（3 种模型、3 种画幅）：纹理一致性能压到多少")
    print("=" * 76)
    from core import seamfix

    clips = [c["path"] for c in clips_for_project(
        "19636f68-9d0f-4555-b268-1088ca018b33", default_db())]
    if len(clips) < 2:
        check("B0 找到多段素材", False, "不足 2 段")
        return
    work = os.path.join(tmp, "tex_real")
    r = await seamfix.harmonize(clips, work, scene_ids=[""] * len(clips),
                                do_color=False, do_trim=False, do_texture=True)
    t = r["texture"]
    tb, ta = t["post_norm"], t["after"]
    print(f"  归一化前（仅供参考，画幅统一本身会改高频）：{t['raw']['values']} "
          f"ratio={t['raw']['texture_ratio']}")
    print(f"  归一化后·纹理处理前：能量 {tb['values']}  ratio = {tb['texture_ratio']}")
    print(f"  纹理统一后：       能量 {ta['values']}  ratio = {ta['texture_ratio']}")
    pl = t["plan"]
    print(f"  计划：target={pl['target']} amounts={pl['amounts']} grain={pl['grain']}")
    print(f"        {pl['note']}")
    for n in r["notes"]:
        if "纹理" in n:
            print(f"  说明：{n}")
    check("B1 处理前确实纹理不一致（问题真实存在）",
          (t["ratio_raw_before"] or 1) > 1.2 or (t["ratio_before"] or 1) > 1.2,
          f"归一化前 {t['ratio_raw_before']} / 归一化后 {t['ratio_before']}")
    check("B2 纹理统一后离散度下降",
          ta["texture_ratio"] < tb["texture_ratio"],
          f"{tb['texture_ratio']} -> {ta['texture_ratio']}")
    check("B3 没有把某一段压到接近 0（等化没有过火）",
          min(ta["values"]) > 0.3 * min(tb["values"]),
          f"min {min(tb['values'])} -> {min(ta['values'])}")
    check("B4 如实报告了理论上限（可修范围）",
          pl.get("achievable_floor") is not None,
          f"理论最多压到 {pl.get('achievable_floor')}x")


async def part_c_controlled(tmp: str):
    print("\n" + "=" * 76)
    print("C. 受控实验：把同一段素材的一半弄成'塑料感'，看能不能拉回来")
    print("=" * 76)
    from core import seamfix

    clips = [c["path"] for c in clips_for_project(
        "19636f68-9d0f-4555-b268-1088ca018b33", default_db())]
    if not clips:
        check("C0 找到素材", False, "无")
        return
    src = clips[0]
    # 一半"塑料感"（大幅柔化，模拟另一个模型磨皮感）+ 一半原样
    plastic = os.path.join(tmp, "plastic.mp4")
    subprocess.run([ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", src,
                    "-vf", "gblur=sigma=2.0", "-an", "-c:v", "libx264",
                    "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                    plastic], capture_output=True, timeout=900)
    clean = os.path.join(tmp, "clean.mp4")
    subprocess.run([ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", src,
                    "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                    "-pix_fmt", "yuv420p", clean], capture_output=True, timeout=900)

    e_clean = await seamfix._texture_energy(clean)
    e_plastic = await seamfix._texture_energy(plastic)
    print(f"  清晰段能量 {e_clean:.3f}   塑料感段能量 {e_plastic:.3f}   "
          f"相差 {e_clean/max(e_plastic,1e-6):.2f} 倍")

    def rep(stats):
        return seamfix.texture_report(stats)

    # 不做统一
    w0 = os.path.join(tmp, "tex_off")
    r0 = await seamfix.harmonize([clean, plastic], w0, scene_ids=["", ""],
                                 do_color=False, do_trim=False, do_texture=False)
    # 做统一
    w1 = os.path.join(tmp, "tex_on")
    r1 = await seamfix.harmonize([clean, plastic], w1, scene_ids=["", ""],
                                 do_color=False, do_trim=False, do_texture=True)
    t0, t1 = r0["texture"]["after"], r1["texture"]["after"]
    print(f"\n  不统一：能量 {t0['values']}  ratio = {t0['texture_ratio']}")
    print(f"  统一后：能量 {t1['values']}  ratio = {t1['texture_ratio']}")
    print(f"  统一计划：amounts={r1['texture']['plan']['amounts']} "
          f"grain={r1['texture']['plan']['grain']}")
    print(f"  （目标 1.0；实测可修范围只有 ±33%，所以不会到 1.0 —— 不吹这个牛）")
    check("C1 不做统一时差异明显", t0["texture_ratio"] > 1.3, f"{t0['texture_ratio']}")
    check("C2 统一后差异显著缩小",
          t1["texture_ratio"] < t0["texture_ratio"] * 0.85,
          f"{t0['texture_ratio']} -> {t1['texture_ratio']}")
    check("C3 统一没有把差异放大（方向正确）",
          t1["texture_ratio"] <= t0["texture_ratio"],
          f"{t0['texture_ratio']} -> {t1['texture_ratio']}")


async def main() -> int:
    tmp = tempfile.mkdtemp(prefix="vf_texture_")
    try:
        await part_a_calibration(tmp)
        await part_b_real(tmp)
        await part_c_controlled(tmp)
    finally:
        if not os.environ.get("VF_KEEP_TEXTURE_OUT"):
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"\n（产物保留在 {tmp}）")
    print("\n" + "=" * 76)
    print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
    for f in FAIL:
        print("  [x] " + f)
    print("=" * 76)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
