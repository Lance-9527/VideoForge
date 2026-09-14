# -*- coding: utf-8 -*-
"""「容器时长 vs 视频轨时长」—— 一个让成片凭空少掉 1/3 的坑。

═══ 事故 ═══
线上实测（`19636f68` 一键成片）：
    clip_00.mp4   容器时长 = 10.00s   视频轨真实长度 = 5.64s
    clip_03.mp4   容器时长 = 10.00s   视频轨真实长度 = 10.00s
根因：`render_clip` 把**视频**当**静图**喂（`-loop 1 -framerate 30 -i x.mp4`）。
`-loop 1` 对 mp4 不像对 PNG 那样无限循环 → 视频轨停在源片长度；
而 `anullsrc` 静音音轨被 `-t 10` 拉满 → **容器 10s / 视频 5.64s**。
于是 `probe_duration()`（读容器）报 10s，"想渲 10/实测 10"的检查完全抓不到；
等到拼接丢音轨重编码，视频缩回 5.64s → 6 段声明 60s，成片只剩 **39.93s**，
而字幕时间轴还按 60s 排。

修法：① 视频输入用 `-stream_loop -1`（真循环填满）② 不套 Ken Burns（视频自带运动）
③ 新增 `probe_video_duration()`（解码到 null 读 `time=`）来量**视频轨**。

运行：python tests/test_clip_duration_truth.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "backend"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from analyze_seams import clips_for_project, default_db  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return cond


def make_still(path: str, seconds: float = 0.0) -> str:
    import imageio_ffmpeg
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    import subprocess
    subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=0x224466:s=640x360",
                    "-frames:v", "1", path], capture_output=True, timeout=120)
    return path


async def main() -> int:
    from core.compose import render_clip, probe_video_duration
    from core.compose import probe_duration as probe_container

    clips = [c["path"] for c in clips_for_project(
        "19636f68-9d0f-4555-b268-1088ca018b33", default_db())]
    if not clips:
        check("找到真实素材", False, "库里无片段")
        return 1
    # 挑一个**短于**目标时长的真实生成视频（就是要循环填充的那种）
    src, sdur = None, 0.0
    for c in clips:
        d = await probe_video_duration(c)
        if 3.0 < d < 8.0:
            src, sdur = c, d
            break
    if src is None:
        src, sdur = clips[0], await probe_video_duration(clips[0])
    print(f"\n源素材 {os.path.basename(src)}  视频轨 {sdur:.2f}s")
    check("A0 找到一段短于目标的真实视频", 3.0 < sdur < 9.5, f"{sdur:.2f}s")

    tmp = tempfile.mkdtemp(prefix="vf_clipdur_")
    try:
        # ── A. 视频输入 → 视频轨必须真的填满目标时长 ──
        print("\n" + "=" * 74)
        print("A. 视频素材循环填充：视频轨必须 = 目标时长（不是容器时长）")
        print("=" * 74)
        target = 10.0
        out = os.path.join(tmp, "vid.mp4")
        warns = await render_clip(src, "", target, out, 1280, 720, 30)
        cd = await probe_container(out)
        vd = await probe_video_duration(out)
        print(f"  目标 {target}s → 容器 {cd:.2f}s   视频轨 {vd:.2f}s")
        for w in warns:
            print(f"    warn: {w}")
        check("A1 视频轨真的填满了目标时长（旧写法会停在源片长度）",
              vd > target - 0.6, f"视频轨 {vd:.2f}s vs 目标 {target}s")
        check("A2 容器时长与视频轨一致（不再有'假时长'）",
              abs(cd - vd) < 0.6, f"容器 {cd:.2f} 视频轨 {vd:.2f}")
        check("A3 如实告知了素材被循环填充",
              any("循环" in w for w in warns), str(warns)[:120])

        # ── B. 静图输入 → Ken Burns 仍要正常工作（别把静图那条路改坏）──
        print("\n" + "=" * 74)
        print("B. 静图输入：Ken Burns 那条路不能被改坏")
        print("=" * 74)
        img = make_still(os.path.join(tmp, "still.png"))
        out2 = os.path.join(tmp, "still.mp4")
        w2 = await render_clip(img, "", 6.0, out2, 1280, 720, 30)
        vd2 = await probe_video_duration(out2)
        print(f"  静图目标 6s → 视频轨 {vd2:.2f}s   warns={w2}")
        check("B1 静图仍能渲染满目标时长", vd2 > 5.4, f"{vd2:.2f}s")
        check("B2 静图没有被误判成视频", not any("循环" in w for w in w2), str(w2))

        # ── C. 目标比素材长很多 → 循环要真的循环 ──
        print("\n" + "=" * 74)
        print("C. 目标远长于素材：必须真的循环（旧写法缺口会一直留着）")
        print("=" * 74)
        out3 = os.path.join(tmp, "long.mp4")
        target3 = round(sdur * 2.5, 1)
        await render_clip(src, "", target3, out3, 1280, 720, 30)
        vd3 = await probe_video_duration(out3)
        print(f"  素材 {sdur:.2f}s → 目标 {target3}s → 视频轨 {vd3:.2f}s")
        check("C1 目标远超素材时视频轨仍填满（真循环）",
              vd3 > target3 - 0.6, f"{vd3:.2f}s vs {target3}s")

        # ── D. 端到端：6 段声明时长之和 = 成片时长 + 转场削减 ──
        print("\n" + "=" * 74)
        print("D. 端到端守恒：各段视频轨之和 − 转场 = 成片时长")
        print("=" * 74)
        from core.assemble import concat_with_transitions
        six = []
        for k in range(6):
            p = os.path.join(tmp, f"c{k}.mp4")
            await render_clip(src if k % 2 == 0 else img, "", 10.0, p, 1280, 720, 30)
            six.append(p)
        vsum = 0.0
        for p in six:
            vsum += await probe_video_duration(p)
        out6 = os.path.join(tmp, "six.mp4")
        n = len(six)
        etrans = [{"type": "none", "seconds": 0.0}] + \
                 [{"type": "fade", "seconds": 0.35} for _ in range(n - 1)]
        r6 = await concat_with_transitions(six, out6, etrans, w=1280, h=720, fps=30,
                                          work_dir=os.path.join(tmp, "w6"),
                                          scene_ids=[""] * n)
        cuts = sum(c for c in (r6.get("cuts") or []) if c)
        got = await probe_video_duration(out6)
        print(f"  各段视频轨之和 {vsum:.2f}s − 转场 {cuts:.2f}s = "
              f"预期 {vsum - cuts:.2f}s；成片 {got:.2f}s")
        check("D1 成片时长守恒（没有静默丢时长）",
              abs((vsum - cuts) - got) < 0.8, f"缺口 {(vsum - cuts) - got:.2f}s")
        check("D2 成片比单段长得多（确认不是只剩第一段）",
              got > 30.0, f"{got:.2f}s")
        # ── E. compose 真正走的那条路 `_normalize_clip`（不是 render_clip）──
        print("\n" + "=" * 74)
        print("E. compose 的真实路径：_normalize_clip（外部生成视频规范化）")
        print("=" * 74)
        # ★ 这才是线上出问题的那条：已有生成视频的分镜走 `_normalize_clip`，
        #   而不是 `render_clip`。旧代码只 `-t`（只能截断、不能延长）→
        #   视频轨停在 5.64s，静音轨被拉满 10s，容器报 10s，
        #   拼接一丢音轨就缩回 5.64s。我第一次只修了 render_clip，
        #   结果成片还是 39.93s —— 因为根本没走到那个函数。
        from core.compose import _normalize_clip
        out4 = os.path.join(tmp, "norm.mp4")
        w4 = await _normalize_clip(src, out4, 1280, 720, 30, 10.0)
        cd4 = await probe_container(out4)
        vd4 = await probe_video_duration(out4)
        print(f"  _normalize_clip 目标 10s → 容器 {cd4:.2f}s  视频轨 {vd4:.2f}s")
        for w in w4:
            print(f"    warn: {w}")
        check("E1 _normalize_clip 的视频轨也填满目标（这条是线上真正走的）",
              vd4 > 9.4, f"视频轨 {vd4:.2f}s vs 目标 10s")
        check("E2 容器时长与视频轨一致",
              abs(cd4 - vd4) < 0.6, f"容器 {cd4:.2f} 视频轨 {vd4:.2f}")
        check("E3 如实告知循环补足", any("循环" in w for w in w4), str(w4)[:120])

        # 比目标长的素材 → 应该截断（别把这条也改坏）
        out5 = os.path.join(tmp, "norm_short.mp4")
        w5 = await _normalize_clip(src, out5, 1280, 720, 30, 3.0)
        vd5 = await probe_video_duration(out5)
        print(f"  素材 {sdur:.2f}s → 目标 3s → 视频轨 {vd5:.2f}s")
        check("E4 素材比目标长时按目标截断",
              abs(vd5 - 3.0) < 0.5, f"{vd5:.2f}s")
    finally:
        if not os.environ.get("VF_KEEP_CLIPDUR_OUT"):
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"\n（产物保留在 {tmp}）")

    print("\n" + "=" * 74)
    print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
    for f in FAIL:
        print("  [x] " + f)
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
