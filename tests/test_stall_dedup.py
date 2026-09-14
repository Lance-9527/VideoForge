# -*- coding: utf-8 -*-
"""「冻结重演 / 运动停顿重启」的地面真值测试。

═══ 这是什么病 ═══
串联生成时，第 i 段的首帧 = 第 i-1 段的尾帧。模型往往**先把那个输入帧原样复现
几帧**才开始动 —— 于是拼接处出现一小段静止。观感就是
"动着动着定住 → 又慢慢启动"，也就是用户说的"运动停顿重启"。

**它与"跳变"是两种相反的病**：
    跳变：接缝帧差 **远大于** 内部典型帧差      → 治法是加长溶解
    停顿：接缝帧差 **接近 0**                  → 治法是裁掉重复的那几帧
上一批我一度把"裁沉降帧"当默认打开，结果 `seam_ratio` 反而变差（7.209 → 7.863）——
因为那个指标量的是"两侧像不像"，裁掉几帧只会让两侧**更不像**。
**指标选错了，结论就会反。** 所以这里必须用"停顿指标"来验证。

═══ 怎么造出地面真值 ═══
用**真实片段** C，从中点切开成 A、B；再人为把 A 的尾帧序列复制一份接在 B 前面
（`B_dup = 尾部重复 + B`）—— 这就是串联生成会产出的东西。
正确答案是已知的：裁掉重复头之后，`A + B_dup` 应该 ≈ 原片 C。

运行：python tests/test_stall_dedup.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "backend"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from analyze_seams import clips_for_project, default_db, probe  # noqa: E402

PASS, FAIL = [], []
FPS = 24


def check(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return cond


def ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def cut(src: str, dst: str, start: float, dur: float) -> str:
    subprocess.run([ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                    "-ss", f"{start:.4f}", "-i", src, "-t", f"{dur:.4f}",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                    "-pix_fmt", "yuv420p", "-an", dst], capture_output=True, timeout=900)
    return dst


def concat2(a: str, b: str, dst: str) -> str:
    """拼接两段（重编码）。

    两道保险，都是踩出来的：
      ① 不用 concat demuxer + `-c copy` —— 它要求两段编码参数**完全一致**，
         而其中一段是从 PNG 现编的（profile/level/timebase/sar 都可能不同），实测直接失败。
      ② 即使走 concat **滤镜**，也先把两路都归一化
         （fps / format / scale / setsar），否则滤镜会因为参数不一致报错或产错。
    """
    subprocess.run(
        [ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", a, "-i", b,
         "-filter_complex",
         "[0:v]fps=24,scale=1280:720:force_original_aspect_ratio=increase,"
         "crop=1280:720,setsar=1,format=yuv420p[v0];"
         "[1:v]fps=24,scale=1280:720:force_original_aspect_ratio=increase,"
         "crop=1280:720,setsar=1,format=yuv420p[v1];"
         "[v0][v1]concat=n=2:v=1:a=0[v]",
         "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "18", "-pix_fmt", "yuv420p", "-r", str(FPS), dst],
        capture_output=True, timeout=1800)
    return dst


def frames(path: str, n: int = 200):
    raw = path + ".__sf.raw"
    subprocess.run([ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", path,
                    "-vf", "fps=24,scale=160x90,format=gray", "-frames:v", str(n),
                    "-f", "rawvideo", "-pix_fmt", "gray", raw],
                   capture_output=True, timeout=900)
    a = np.fromfile(raw, dtype=np.uint8).astype(np.float32)
    try:
        os.remove(raw)
    except Exception:
        pass
    c = len(a) // (160 * 90)
    return a[:c * 160 * 90].reshape(c, 90, 160) if c else np.zeros((0, 90, 160), np.float32)


def diff(p1: str, p2: str) -> float:
    A, B = frames(p1), frames(p2)
    m = min(len(A), len(B))
    return float(np.abs(A[:m] - B[:m]).mean()) if m else -1.0


def min_delta_near_seam(movie: str, center: float, win: float = 1.0) -> float:
    """接缝 ±win 秒内**最小**的单帧变化量 —— 这就是"停顿"。"""
    f = frames(movie)
    if len(f) < 4:
        return -1.0
    d = np.abs(np.diff(f, axis=0)).mean(axis=(1, 2))
    c = int(round(center * 24))
    lo, hi = max(1, c - int(win * 24)), min(len(d), c + int(win * 24))
    return float(d[lo:hi].min()) if hi > lo else -1.0


async def main() -> int:
    from core import seamfix

    clips = [c["path"] for c in clips_for_project(
        "19636f68-9d0f-4555-b268-1088ca018b33", default_db())]
    if not clips:
        check("找到真实素材", False, "库里无片段")
        return 1
    src = clips[0]
    dur = probe(src)["duration"]
    print(f"\n源片段 {os.path.basename(src)}  {dur:.2f}s")

    tmp = tempfile.mkdtemp(prefix="vf_stall_")
    try:
        # ── A. 纯函数：重复头检测 ──
        print("\n" + "=" * 74)
        print("A. 重复头检测（不需要任何'这是串联段'的标记，纯看画面）")
        print("=" * 74)
        sh = (90, 160)
        prev = {"_last": np.zeros(sh), "motion_median": 10.0}
        head = np.stack([np.zeros(sh)] * 4 + [np.full(sh, 60.0)] * 3)
        cur = {"_head": head, "_first": head[0], "_last": head[-1],
               "motion_median": 10.0}
        d = seamfix.detect_dup_head(prev, cur)
        print(f"  重复 4 帧 → 检出 {d['dup_frames']} 帧 / {d['dup_seconds']}s  {d['verdict']}")
        check("A1 能检出重复头帧（4 帧完全相同 → 该裁 4 帧 = 0.333s）",
              d["dup_frames"] == 4, str(d["dup_frames"]))
        head2 = np.stack([np.full(sh, 60.0)] * 7)
        cur2 = {"_head": head2, "_first": head2[0], "_last": head2[-1],
                "motion_median": 10.0}
        d2 = seamfix.detect_dup_head(prev, cur2)
        check("A2 没有重复时不误判（正常起幅不会被误裁）",
              d2["dup_frames"] == 0, str(d2["dup_frames"]))
        check("A3 裁帧有上限（不会把整段删掉）",
              seamfix.MAX_DEDUP_SEC <= 0.8, f"{seamfix.MAX_DEDUP_SEC}s")

        # ── A4~A6. 片段**内部**的停顿（与"接缝处停顿"是两件事）──
        # 实测教训（2026-09-13）：段间拼点完全看不出来（内部最猛帧 1.02× p99），
        # 但那一段内部 t=7.25s 起有 2.88 秒几乎静止，而告警里一个字都没提。
        fps = seamfix.ANALYZE_FPS
        mc_stall = [5.0] * int(2 * fps) + [0.2] * int(3 * fps) + [5.0] * int(2 * fps)
        st_stall = {"motion_curve": mc_stall, "motion_median": 5.0,
                    "duration": len(mc_stall) / fps}
        w = seamfix.stall_windows(st_stall)
        check("A4 能检出**片段内部**的停顿窗口（造一段静止 3 秒）",
              len(w) == 1 and abs(w[0]["seconds"] - 3.0) <= 0.2,
              f"{w}")
        st_move = {"motion_curve": [5.0] * int(5 * fps), "motion_median": 5.0,
                   "duration": 5.0}
        check("A5 全程有运动的片段不误报", seamfix.stall_windows(st_move) == [], "")
        rep = seamfix.stall_report([st_stall, st_stall])
        check("A6 stall_report 里带上了内部停顿清单（freezes）",
              rep.get("freeze_count") == 2 and rep.get("freezes"),
              f"freeze_count={rep.get('freeze_count')} "
              f"共 {rep.get('freeze_total_seconds')}s")

        # ── A7~A9. 起幅沉降的裁剪量（治"硬切进来像卡住"）──
        import numpy as _np
        fps2 = seamfix.ANALYZE_FPS
        mc_h = [0.2] * int(0.5 * fps2) + [5.0] * int(4.5 * fps2)
        hs = seamfix.head_settle_seconds(mc_h, 5.0)
        check("A7 起幅静止 0.5s → 该裁 0.5s（不多裁也不少裁）",
              abs(hs["head_trim"] - 0.5) <= 0.1 and hs["head_settle"] < 0.4,
              f"{hs}")
        check("A8 正常起幅不动它（不误裁）",
              seamfix.head_settle_seconds([5.0] * int(5 * fps2), 5.0)["head_trim"] == 0.0,
              "正常起幅 head_trim=0")
        # ★ 这一条是**回归**：`head_settle_seconds` 原来写的是 `mc or []`，
        #   而传进来的可能是 numpy 数组 —— 数组真值判断会抛
        #   "truth value of an array is ambiguous"，异常被 compose 的 try/except
        #   吞掉，"裁死帧"一次都没生效过（功能写了却从没跑起来）。
        _np_out = seamfix.head_settle_seconds(_np.array(mc_h), 5.0)
        check("A9 传 **numpy 数组**也要正常工作（旧代码在这里抛异常、被静默吞掉）",
              _np_out["head_trim"] > 0.4, f"{_np_out}")

        # ── A10~A13. "剪掉中间冻结段"的数学（画面/配音/字幕三者靠它对齐）──
        check("A10 保留区间：删 2-4 与 6-7（总长 10）",
              seamfix.kept_ranges(10, [(2, 4), (6, 7)])
              == [(0.0, 2.0), (4.0, 6.0), (7.0, 10.0)],
              str(seamfix.kept_ranges(10, [(2, 4), (6, 7)])))
        check("A11 乱序 + 重叠的删除区间也要正确",
              seamfix.kept_ranges(10, [(6, 7), (2, 4), (3, 5)])
              == [(0.0, 2.0), (5.0, 6.0), (7.0, 10.0)],
              str(seamfix.kept_ranges(10, [(6, 7), (2, 4), (3, 5)])))
        check("A12 全删光 → 空列表（调用方要兜底，别输出 0 秒的片子）",
              seamfix.kept_ranges(5, [(0, 5)]) == [], "")
        check("A13 时间映射：删除段之前不动 / 之后前移 / 落在段内折到起点",
              seamfix.map_after_removals(1, [(2, 4)]) == 1.0
              and seamfix.map_after_removals(5, [(2, 4)]) == 3.0
              and seamfix.map_after_removals(3, [(2, 4)]) == 2.0,
              f"{seamfix.map_after_removals(1, [(2, 4)])} / "
              f"{seamfix.map_after_removals(5, [(2, 4)])} / "
              f"{seamfix.map_after_removals(3, [(2, 4)])}")

        # ── A14~A18. "自身运动尺度"这个参照不存在时，绝不许编数字 ──
        # ★★ 事故记录（2026-09-13）：项目 e62a182d 的 5 个分镜一个都没生成出
        #    视频，compose 退回**图片卡** → 全片零运动 → `motion_median = 0`。
        #    旧的 `base = max(1e-6, ...)` 于是算出
        #    **22307014.465 倍**，这个数字被原样写进用户能看到的告警，
        #    还 > 阈值 → 每一处接缝都被强行改成 0.9s 溶解，
        #    等于用一次**不存在的测量**推翻用户选的"换场硬切"。
        def _st(px, motion=0.0):
            """造一个片段统计：指定亮度、指定运动中位数。"""
            img = _np.full((90, 160), float(px))
            return {"motion_median": motion, "duration": 5.0,
                    "motion_curve": [motion] * int(5 * seamfix.ANALYZE_FPS),
                    "_last": img, "_first": img.copy(),
                    "_head": _np.stack([img, img, img]),
                    "rgb": {"mean": [float(px)] * 3}}

        s_a, s_b = _st(32), _st(180)      # 两张静止图片卡，亮度差很大
        check("A14 两段都没运动 → 比值返回 None（不是 2e7 那种天文数字）",
              seamfix.motion_base(s_a, s_b) is None
              and seamfix.seam_ratio_of(s_a, s_b) is None,
              f"motion_base={seamfix.motion_base(s_a, s_b)} "
              f"ratio={seamfix.seam_ratio_of(s_a, s_b)}")
        _rep = seamfix.seam_report([s_a, s_b])
        check("A15 接缝报告如实标『无从比较』，均值也不编",
              _rep["seams"][0]["seam_ratio"] is None
              and _rep["mean_seam_ratio"] is None
              and _rep["meaningful_count"] == 0
              and "无从比较" in _rep["seams"][0]["verdict"],
              f"verdict={_rep['seams'][0]['verdict']} "
              f"mean={_rep['mean_seam_ratio']} meaningful={_rep['meaningful_count']}")
        _ct = seamfix.choose_transition(s_a, s_b,
                                        {"type": "none", "seconds": 0.0, "respect": True})
        check("A16 参照不存在时不做测量驱动的摊薄 —— 用户选的硬切被保住",
              _ct["measured"] == "unmeasurable" and _ct["type"] == "none"
              and float(_ct["seconds"] or 0) == 0.0,
              f"measured={_ct['measured']} type={_ct['type']} sec={_ct['seconds']}")
        _dup = seamfix.detect_dup_head(s_a, s_b)
        check("A17 静止素材上不裁『重复头』（纯静止片段本来就没有冻结重演）",
              float(_dup.get("dup_seconds") or 0) == 0.0
              and "无从判断" in _dup.get("verdict", ""),
              f"dup={_dup.get('dup_seconds')}s verdict={_dup.get('verdict')}")

        # ★ 反向对照：**有**运动时必须照旧出数，别把指标一并废掉
        s_c, s_d = _st(32, motion=5.0), _st(180, motion=5.0)
        _r2 = seamfix.seam_ratio_of(s_c, s_d)
        check("A18 反向对照：有运动时照常给数值（没有把指标整体废掉）",
              isinstance(_r2, float) and _r2 > 1.25,
              f"ratio={_r2}")

        # ── B. 地面真值：真实片段切开 + 人为重复头 ──
        print("\n" + "=" * 74)
        print("B. 地面真值：真实片段切开 + 把尾帧重复一份接在后面")
        print("=" * 74)
        cut_at = round(dur / 2.0, 3)
        dup_len = 0.25
        A = cut(src, os.path.join(tmp, "A.mp4"), 0.0, cut_at)
        B = cut(src, os.path.join(tmp, "B.mp4"), cut_at, dur - cut_at)
        # ★ 正确的仿真：串联生成时模型是把**输入的那一帧原样复现几帧**
        #   （一个冻结的 hold），不是把前面的画面再接一遍。
        #   我第一次错写成"把 A 的尾段接在 B 前面"——那只是连续的画面，
        #   当然检测不到重复。搞错仿真物，测出来的结论全是假的。
        # ★ 用 `tpad=stop_mode=clone` 直接**克隆最后一帧**，不抽 PNG。
        #   原来抽 PNG 那步实测失败（last.png 根本没生成），而且抽帧本身还引入
        #   "取到的是结束前 0.02~0.06s 的帧、不是真正的最后帧"这种偏差。
        #   tpad 是 ffmpeg 原生能力，克隆的就是真正的最后一帧；
        #   再用 trim 把克隆出来的那 0.25s 单独切出来。
        hold = os.path.join(tmp, "hold.mp4")
        subprocess.run(
            [ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", A,
             "-vf", (f"tpad=stop_mode=clone:stop_duration={dup_len},"
                     f"trim=start={cut_at:.4f}:duration={dup_len:.4f},"
                     f"setpts=PTS-STARTPTS,fps={FPS}"),
             "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
             "-pix_fmt", "yuv420p", hold], capture_output=True, timeout=900)
        B_dup = concat2(hold, B, os.path.join(tmp, "Bdup.mp4"))
        print(f"  A = 前 {cut_at:.2f}s   "
              f"B_dup = A 的尾帧冻结 {dup_len}s + B（后 {dur-cut_at:.2f}s）")
        print(f"  中间件大小：hold.mp4="
              f"{os.path.getsize(hold) if os.path.exists(hold) else '缺失'}  "
              f"B.mp4={os.path.getsize(B) if os.path.exists(B) else '缺失'}  "
              f"B_dup={os.path.getsize(B_dup) if os.path.exists(B_dup) else '缺失'}")
        if not os.path.exists(B_dup) or os.path.getsize(B_dup) < 10240:
            check("B0 造出 B_dup", False, "concat 失败（大小见上）")
            return 1

        sa = await seamfix.analyze_clip(A)
        sb = await seamfix.analyze_clip(B_dup)
        rep = seamfix.stall_report([sa, sb])
        det = seamfix.detect_dup_head(sa, sb)
        print(f"  检测：重复 {det['dup_frames']} 帧 / {det['dup_seconds']}s  "
              f"（真值 {dup_len}s）")
        print(f"  停顿指标：{rep['seams'][0]['stall_ratio']}  "
              f"{rep['seams'][0]['verdict']}")
        check("B1 检出了人为造的重复头",
              det["dup_frames"] >= 3, f"{det['dup_frames']} 帧")
        check("B2 检出长度接近真值 0.25s",
              abs(det["dup_seconds"] - dup_len) < 0.14,
              f"检出 {det['dup_seconds']}s vs 真值 {dup_len}s")

        # 接缝处的"停顿"：接缝帧差应明显小于内部**典型**帧差
        seam_movie = concat2(A, B_dup, os.path.join(tmp, "raw.mp4"))
        min_raw = min_delta_near_seam(seam_movie, cut_at)
        # 基准用**中位数**，不能用最小值 —— 素材本身可能就有慢镜头，
        # 拿最小值当基准会把"正常慢"误当成"停顿"（我第一次就是这么踩的）。
        dA = np.abs(np.diff(frames(A), axis=0)).mean(axis=(1, 2))
        medA = float(np.median(dA))
        print(f"  接缝附近最小帧差 {min_raw:.3f}   A 内部运动中位 {medA:.3f}")
        check("B3 接缝附近确实存在'几乎不动'的帧（停顿真实存在）",
              min_raw < medA * 0.25, f"接缝最小 {min_raw:.3f} vs 内部中位 {medA:.3f}")

        # ── C. 跑管线：应该把重复头裁掉，接缝停顿消失 ──
        print("\n" + "=" * 74)
        print("C. 跑接缝管线：重复头应被自动裁掉，停顿消失")
        print("=" * 74)
        w1 = os.path.join(tmp, "fix")
        r = await seamfix.harmonize([A, B_dup], w1, scene_ids=["", ""],
                                    do_color=False, do_trim=False, do_texture=False,
                                    do_dedup=True)
        dd = r["stall"]["dedup"]
        print(f"  裁掉的重复头：{dd}s   （第 2 段）")
        for n in r["notes"]:
            if "重演" in n or "重复" in n:
                print(f"  说明：{n}")
        fixed = concat2(r["out"][0], r["out"][1], os.path.join(tmp, "fixed.mp4"))
        min_fixed = min_delta_near_seam(fixed, cut_at - dd[1])
        print(f"  修复后接缝附近最小帧差 {min_fixed:.3f}（修复前 {min_raw:.3f}）")
        check("C1 管线真的裁掉了重复头", dd[1] > 0.1, f"{dd[1]}s")
        check("C2 裁掉长度≈真值（没有多裁也没有少裁）",
              abs(dd[1] - dup_len) < 0.15, f"{dd[1]}s vs {dup_len}s")
        check("C3 修复后接缝的停顿明显缓解",
              min_fixed > min_raw * 1.25, f"{min_raw:.3f} -> {min_fixed:.3f}")

        # ── D. 透明性：修好的成片应逼近原片 ──
        print("\n" + "=" * 74)
        print("D. 透明性：修完的 A+B 应逼近原片（地面真值）")
        print("=" * 74)
        clean = concat2(A, B, os.path.join(tmp, "clean.mp4"))
        d_raw = diff(src, seam_movie)
        d_fix = diff(src, fixed)
        d_clean = diff(src, clean)
        # ★ 公平对照：把**本来就没有重复**的那一对也过一遍同样的管线。
        #   否则比较的是"1 次编码"vs"好几次编码"—— 差的那部分主要是
        #   编解码的代际损失，不是接缝残留，拿它当门槛就是在冤枉自己。
        w2 = os.path.join(tmp, "ctrl")
        rc2 = await seamfix.harmonize([A, B], w2, scene_ids=["", ""],
                                      do_color=False, do_trim=False,
                                      do_texture=False, do_dedup=True)
        ctrl = concat2(rc2["out"][0], rc2["out"][1], os.path.join(tmp, "ctrl.mp4"))
        d_ctrl = diff(src, ctrl)
        print(f"  与原片逐帧平均差：未修复 {d_raw:.3f}   修复后 {d_fix:.3f}   "
              f"（同样管线、本来无重复的对照 {d_ctrl:.3f}）")
        print(f"  同管线对照里管线本身引入的损失 = {d_ctrl - d_clean:.3f}")
        check("D1 修复后比未修复更接近原片",
              d_fix < d_raw * 0.6, f"{d_raw:.3f} -> {d_fix:.3f}")
        check("D2 修复后与『本来就没重复』的同样管线对照基本持平",
              d_fix < d_ctrl + 1.2, f"修复 {d_fix:.3f} vs 对照 {d_ctrl:.3f}")

        # ── E. 产品入口：concat_with_transitions 也要能自动裁重复头 ──
        print("\n" + "=" * 74)
        print("E. 产品入口：画幅/纹理都一致、只有冻结重演时也要治")
        print("=" * 74)
        # ★ 这是个真实的漏洞：几何那关和纹理那关都没触发时（画幅已统一、
        #   纹理本来就齐），如果就不跑 harmonize，重复头永远没人管 ——
        #   而这恰恰最常见（同一模型同一个项目，画幅纹理天然一致，
        #   但分段串联一定有重复头）。已加"仅裁重复头"这一支。
        from core.assemble import concat_with_transitions
        ework = os.path.join(tmp, "prod")
        os.makedirs(ework, exist_ok=True)
        eout = os.path.join(ework, "film.mp4")
        er = await concat_with_transitions(
            [A, B_dup], eout, [{"type": "none", "seconds": 0.0},
                               {"type": "none", "seconds": 0.0}],
            w=1280, h=720, fps=24, work_dir=os.path.join(ework, "w"),
            scene_ids=["", ""])
        hm = er.get("harmonize") or {}
        print(f"  harmonize 分支：applied={hm.get('applied')} "
              f"texture_only={hm.get('texture_only')} dedup_only={hm.get('dedup_only')}")
        for n in (hm.get("notes") or []):
            if "重演" in n or "重复" in n:
                print(f"    · {n}")
        # 断言"到底修没修"，而不是"走了哪个分支" ——
        # 第一次我把 E1 写成要求 `dedup_only==True`，结果因为画幅那一关先触发、
        # 重复头是在几何分支里顺带裁掉的，断言就失败。可实际上**已经修好了**。
        # 测的是效果，不是实现路径。
        _notes = " ".join(hm.get("notes") or [])
        check("E1 产品入口确实裁掉了重复头（无论走哪个分支）",
              ("重演" in _notes) or bool(hm.get("dedup_only")),
              f"applied={hm.get('applied')} dedup_only={hm.get('dedup_only')} "
              f"notes={_notes[:80]}")
        _st = (hm.get("stall") or {})
        if _st.get("dedup"):
            print(f"  裁掉的重复头：{_st['dedup']}s")
        if os.path.exists(eout):
            ev = probe(eout)["duration"]
            expect = probe(A)["duration"] + probe(B_dup)["duration"] - dup_len
            print(f"  成片 {ev:.2f}s   预期 ≈ {expect:.2f}s"
                  f"（= A + B_dup − 重复的 {dup_len}s）")
            check("E2 成片时长反映了『重复头已被裁掉』",
                  abs(ev - expect) < 0.7, f"{ev:.2f} vs {expect:.2f}")

        # ── E3. 图片卡（全片零运动）：接缝"量不了"，要如实说、且不许乱改转场 ──
        # ★ 真实事故（2026-09-13，项目 e62a182d：5 个分镜都没生成出视频、
        #   compose 退回图片卡）：4 处接缝全被报成"实测跳变是自身运动尺度的
        #   22307014.465 倍"并强行改成 0.9s 溶解。修好后应为：
        #   保留语义硬切 + `harmonize_info["unmeasurable"]` 里有 1 条。
        print("\n" + "=" * 74)
        print("E3. 图片卡素材：接缝强度**量不了**时不许编数字、也不许乱改转场")
        print("=" * 74)
        st1 = os.path.join(tmp, "still1.mp4")
        st2 = os.path.join(tmp, "still2.mp4")
        for _dst, _color in ((st1, "0x203040"), (st2, "0xB0C0D0")):
            subprocess.run(
                [ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", f"color=c={_color}:s=1280x720:d=2:r=24",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                 "-pix_fmt", "yuv420p", "-an", _dst],
                capture_output=True, timeout=300)
        e3out = os.path.join(tmp, "stills.mp4")
        e3 = await concat_with_transitions(
            [st1, st2], e3out,
            [{"type": "none", "seconds": 0.0}, {"type": "none", "seconds": 0.0}],
            w=1280, h=720, fps=24, work_dir=os.path.join(tmp, "w3"),
            scene_ids=["", ""])
        hm3 = e3.get("harmonize") or {}
        print(f"  softened={hm3.get('softened')}  "
              f"unmeasurable={hm3.get('unmeasurable')}")
        check("E3a 图片卡的接缝被标成『量不了』（不是 2e7 倍这种假结论）",
              len(hm3.get("unmeasurable") or []) == 1
              and not (hm3.get("softened") or []),
              f"unmeasurable={hm3.get('unmeasurable')}")
        check("E3b 量不了时不软化（没有拿一次不存在的测量推翻硬切）",
              (probe(e3out)["duration"] if os.path.exists(e3out) else 0) >= 3.9,
              f"成片 {(probe(e3out)['duration'] if os.path.exists(e3out) else 0):.2f}s"
              f"（两段各 2s，软化会吃掉时长）")
    finally:
        if not os.environ.get("VF_KEEP_STALL_OUT"):
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
