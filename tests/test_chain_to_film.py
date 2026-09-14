# -*- coding: utf-8 -*-
"""整条链的集成验证：**按序重生成（尾帧串联）→ 一键成片 → 量最终成片的接缝**。

═══ 为什么还要这一层 ═══
前面每一段都单独验过了：
  · 串联决策（`test_chaining_decision` 11/11）
  · 按序重生成（`test_regen_chained` 15/15，接缝帧差 27.754 → 0.274）
  · 拼接/转场/调色/纹理/停顿（`test_seam_fix` 29/0）
  · 时长保真（`test_clip_duration_truth` 13/0）

但**没有验过"重生成之后再成片"这条完整路径** —— 而那才是用户真正点的那条：
他点「补全缺视频的分镜」，然后点「一键成片」，然后看片。

所以这里量的是**最终成片**（不是片段）的接缝：
在成片上逐帧算"单帧最大跳变 ÷ 自身 p99"。
  · 这个比值 ≈ 1 → 全片没有尖峰，接缝不是最猛的一帧 → 观感连续
  · 明显 > 1     → 有尖峰（接缝处"啪"地跳一下）

对比：**同一批镜头，先各自独立生成（旧），再串起来生成（新）**。

运行：python tests/test_chain_to_film.py
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "backend"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from analyze_seams import clips_for_project, default_db  # noqa: E402

PASS, FAIL = [], []
PID_SRC = "d5815c1e-c641-4f21-a2a4-888388f5157a"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return cond


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def req(port, method, path, body=None, timeout=600):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                               method=method,
                               headers={"Content-Type": "application/json"})
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(r, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def film_report(path: str) -> dict:
    """成片级接缝指标：单帧最大跳变、自身 p99、以及两者之比。

    ★ 为什么用 max/p99 而不是"接缝处帧差"：在**成片**里定位接缝位置要先算
      累积时长再减掉转场削减，一环算错就量错了对象（我在 §16.14 就栽过一次）。
      max/p99 不需要知道接缝在哪，而且正是眼睛感知到的东西：
      有尖峰 → max 远大于 p99；摊平了 → 两者接近。
    """
    import imageio_ffmpeg
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    raw = path + ".__film.raw"
    subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", path,
                    "-vf", "fps=24,scale=160x90,format=gray",
                    "-f", "rawvideo", "-pix_fmt", "gray", raw],
                   capture_output=True, timeout=1200)
    a = np.fromfile(raw, dtype=np.uint8).astype(np.float32)
    try:
        os.remove(raw)
    except Exception:
        pass
    if len(a) < 160 * 90 * 3:
        return {"max": -1.0, "p99": -1.0, "median": -1.0, "ratio": -1.0, "frames": 0}
    f = a.reshape(-1, 90, 160)
    d = np.abs(np.diff(f, axis=0)).mean(axis=(1, 2))
    med = float(np.median(d))
    p99 = float(np.percentile(d, 99))
    mx = float(d.max())
    return {"max": round(mx, 2), "p99": round(p99, 2), "median": round(med, 2),
            "ratio": round(mx / max(1e-6, p99), 3), "frames": int(len(d))}


def compose(port, pid, timeout_s=600):
    req(port, "POST", f"/api/projects/{pid}/compose",
        {"with_voice": False, "burn_subtitles": False})
    st = {}
    n_err = 0
    for _ in range(int(timeout_s)):
        try:
            st = req(port, "GET", f"/api/projects/{pid}/compose/status",
                     timeout=20)["data"]
            n_err = 0
        except Exception:
            # ★ 状态查询本身也可能超时（成片那一步在跑 ffmpeg，会占住时间片）。
            #   不能一次超时就判定失败 —— 记几次连续失败再放弃，并把最后状态带出去。
            n_err += 1
            if n_err >= 6:
                raise RuntimeError("compose 状态接口连续超时，可能卡住了")
            time.sleep(2)
            continue
        if not st.get("running"):
            break
        time.sleep(1.0)
    return st


def main() -> int:
    clips = [c["path"] for c in clips_for_project(PID_SRC, default_db())]
    if not clips:
        check("找到真实片段当素材", False, "库里无片段")
        return 1
    stub_clip = clips[0]

    tmp = tempfile.mkdtemp(prefix="vf_chainfilm_")
    db_path = os.path.join(tmp, "videoforge.db")
    port = free_port()
    proc = None
    try:
        # 起桩后端（自带 seed：4 个镜头，1/2 同场景 A，3/4 同场景 B；1、2 先有"旧视频"）
        # ★ 桩后端的输出**不能接 PIPE 不管**：它的日志（uvicorn + ffmpeg 的 stderr）
        #   一多就把 64KB 管道缓冲写满，子进程**阻塞在 write 上**，
        #   整个事件循环跟着停摆 → 状态接口开始超时，看起来像"compose 卡住了"。
        #   这是教科书式的管道死锁。改成写文件，只在失败时才去读。
        log_path = os.path.join(tmp, "stub.log")
        log_fp = open(log_path, "wb")
        proc = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "_stub_server.py"),
             str(port), db_path, stub_clip, "--seed", "--seed-all"],
            stdout=log_fp, stderr=subprocess.STDOUT)
        up = False
        for _ in range(120):
            try:
                if req(port, "GET", "/api/health", timeout=3).get("data"):
                    up = True
                    break
            except Exception:
                time.sleep(0.5)
        check("A0 桩后端起来了", up, f"port={port}")
        if not up:
            try:
                print(open(log_path, "rb").read().decode("utf-8", "replace")[-1500:])
            except Exception:
                pass
            return 1
        seed = {}
        for _ in range(40):
            if os.path.exists(db_path + ".seed.json"):
                with open(db_path + ".seed.json", encoding="utf-8") as f:
                    seed = json.load(f)
                break
            time.sleep(0.5)
        pid = seed.get("pid")
        check("A0b 拿到项目 id", bool(pid), str(seed)[:100])
        if not pid:
            return 1
        print(f"\nseed 项目 {pid[:8]}：4 个镜头（1、2 = 场景A；3、4 = 场景B）")

        # ── ① 成片（旧）：1、2 是同一段素材各自独立生成，3、4 还没视频 ──
        print("\n" + "=" * 76)
        print("① 先成一次片（旧的、各自独立生成的状态）")
        print("=" * 76)
        st1 = compose(port, pid)
        check("B1 第一次成片成功", bool((st1.get("result") or {}).get("ok")),
              str((st1.get("result") or {}).get("errors"))[:120])
        p1 = (st1.get("result") or {}).get("output_path")
        if not p1 or not os.path.exists(p1):
            check("B2 拿到成片文件", False, str(p1))
            return 1
        before = os.path.join(tmp, "film_before.mp4")
        shutil.copyfile(p1, before)
        r1 = film_report(before)
        print(f"  成片 {os.path.getsize(before)/1048576:.2f}MB  "
              f"单帧最大跳变={r1['max']}  p99={r1['p99']}  "
              f"max/p99={r1['ratio']}")
        check("B3 量出了成片级接缝指标", r1["frames"] > 30, str(r1))
        # ★ 成片级指标要**从接口里拿得到**，不能只存在于测试脚本里 ——
        #   用户点完「一键成片」应该直接看到这个数字，而不是拿个视频自己猜。
        _fm1 = (st1.get("result") or {}).get("film_metrics") or {}
        check("B4 接口返回里带了成片连贯度指标（film_metrics）",
              _fm1.get("ok") and _fm1.get("spike_ratio") is not None,
              str(_fm1)[:140])
        check("B5 而且有一句给人看的结论",
              any("连贯度" in w for w in ((st1.get("result") or {}).get("warnings") or [])),
              str(((st1.get("result") or {}).get("warnings") or []))[:160])

        # ── ② 按序重生成（启用尾帧串联），把 4 个镜头都补齐 ──
        print("\n" + "=" * 76)
        print("② 按序重生成 + 尾帧串联（把 4 个镜头补齐）")
        print("=" * 76)
        req(port, "POST", f"/api/projects/{pid}/regen-chained",
            {"dry_run": False, "only_missing": False})
        rst = {}
        for _ in range(900):
            rst = req(port, "GET", f"/api/projects/{pid}/regen-chained/status")["data"]
            if not rst.get("running"):
                break
            time.sleep(1.0)
        res = rst.get("results") or []
        chained = [x["index"] for x in res if x.get("chained")]
        print(f"  生成成功 {sum(1 for x in res if x['ok'])}/{len(res)}；"
              f"串上的镜头 {chained}")
        print(f"  片段级接缝强度 before={((rst.get('seam_before') or {}).get('mean_seam_ratio'))} "
              f"after={((rst.get('seam_after') or {}).get('mean_seam_ratio'))}")
        check("C1 四个镜头都生成成功", sum(1 for x in res if x["ok"]) == 4,
              f"{sum(1 for x in res if x['ok'])}/4")
        check("C2 同场景的 2、4 串上了，跨场景的 3 没串", chained == [2, 4], str(chained))

        # ── ③ 再成一次片，量最终成片 ──
        print("\n" + "=" * 76)
        print("③ 再成一次片（串过之后的镜头）")
        print("=" * 76)
        st2 = compose(port, pid)
        check("D1 第二次成片成功", bool((st2.get("result") or {}).get("ok")),
              str((st2.get("result") or {}).get("errors"))[:120])
        p2 = (st2.get("result") or {}).get("output_path")
        if not p2 or not os.path.exists(p2):
            check("D2 拿到成片文件", False, str(p2))
            return 1
        after = os.path.join(tmp, "film_after.mp4")
        shutil.copyfile(p2, after)
        r2 = film_report(after)
        print(f"  成片 {os.path.getsize(after)/1048576:.2f}MB  "
              f"单帧最大跳变={r2['max']}  p99={r2['p99']}  "
              f"max/p99={r2['ratio']}")

        print("\n" + "=" * 76)
        print("④ 成片级前后对比")
        print("=" * 76)
        print(f"  {'':10s} {'单帧最大跳变':>12} {'p99':>8} {'max/p99':>9}")
        print(f"  {'旧（独立生成）':10s} {r1['max']:>12.2f} {r1['p99']:>8.2f} "
              f"{r1['ratio']:>9.3f}")
        print(f"  {'新（串联之后）':10s} {r2['max']:>12.2f} {r2['p99']:>8.2f} "
              f"{r2['ratio']:>9.3f}")
        dl = "↓" if r2["max"] < r1["max"] else "↑"
        print(f"  单帧最大跳变 {dl} {r1['max']} → {r2['max']}；"
              f"max/p99 {r1['ratio']} → {r2['ratio']}（越接近 1 越好）")
        # ★ E1 原本断言"两个版本的成片都没有尖峰（max/p99 < 2.5）"。
        #   那条断言在"换场用溶解"的默认下成立，但 2026-09-13 用户看过两版成片后
        #   把默认改成了**换场硬切** —— 硬切会被计入"全片最猛的一帧"，
        #   成片级 max/p99 必然被抬高（同一部真片实测：硬切 10.65 / 溶解 1.45）。
        #   把阈值改大只是掩盖问题；正确做法是承认**这条指标不适合当接缝判据**
        #   （紧接着的注释就是在讲这件事）—— 真正的判据是下面的 E1b（接缝处帧差）。
        check("E1 记下成片级 max/p99（**不作为接缝判据**：换场硬切会把它抬高，"
              "指标看不见「切一刀」这件事）",
              r1["ratio"] is not None and r2["ratio"] is not None,
              f"旧 {r1['ratio']} / 新 {r2['ratio']}")
        # ★ 必须说清一个坑：**成片级的单帧最大跳变不适合用来衡量"串联"**。
        #   实测两个版本几乎一样（5.42 vs 5.56）—— 因为成片的 max 来自
        #   **片段内部的运动**（p99≈3.0、中位≈1.5 都是内容自身的动态），
        #   而接缝早被自适应转场摊平了，根本不会成为全片最猛的一帧。
        #   串联真正改变的是**素材本身连不连续**，那要看**接缝处的帧差**：
        #   `regen-chained` 报的片段级指标才是它的证据。
        #   教训：**指标要挑对对象** —— 想量接缝就去量接缝，
        #   拿全片 max 去推接缝会被内容运动盖住（§16.12 栽过同款）。
        _sb = (rst.get("seam_before") or {}).get("mean_seam_ratio")
        _sa = (rst.get("seam_after") or {}).get("mean_seam_ratio")
        print(f"\n  片段级接缝强度（串联真正的证据）：{_sb} → {_sa}")
        check("E1b 传进成片的素材，接缝处帧差大幅下降（这才是串联的效果）",
              _sb is not None and _sa is not None and _sa < _sb * 0.6,
              f"{_sb} → {_sa}")
        check("E2 成片的 max/p99 没有变差（不劣化）",
              r2["ratio"] <= r1["ratio"] * 1.15,
              f"{r1['ratio']} → {r2['ratio']}")
        check("E3 成片仍然是有画面有声音的正常文件",
              r2["frames"] > 30 and os.path.getsize(after) > 102400,
              f"{r2['frames']} 帧 / {os.path.getsize(after)/1024:.0f}KB")
        print(f"\n（对比成片留在 {tmp}：film_before.mp4 / film_after.mp4）")
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except Exception:
                proc.kill()

    print("\n" + "=" * 76)
    print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
    for f in FAIL:
        print("  [x] " + f)
    print("=" * 76)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
