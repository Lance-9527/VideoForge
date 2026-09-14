# -*- coding: utf-8 -*-
"""「按序重生成 + 尾帧串联」批量动作的端到端测试（用桩模型，不花钱）。

═══ 这个动作解决什么 ═══
尾帧串联是让多镜头像一镜到底最直接的一招，而它**只能靠重新生成**实现
（已有片段是各自独立生成的，后期补不上"它们本该连续"）。

现有「批量渲染」走的是**本地合成**（`render_shot_clip`），不调模型、也没串联；
要串只能一镜一镜手点，还得自己保证顺序（上一镜先有视频，尾帧才存在）。

所以要有一个批量动作把三件事做完：**按 order_index 顺序** + **chain=True** +
**前后各量一次接缝**。本文件就是它的端到端测试。

运行：python tests/test_regen_chained.py
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
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "backend"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from analyze_seams import clips_for_project, default_db  # noqa: E402

PASS, FAIL = [], []
STUB, MODEL = "stubchain", "stub-chain-1"


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


def req(port, method, path, body=None, timeout=300):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                               method=method,
                               headers={"Content-Type": "application/json"})
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(r, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    clips = [c["path"] for c in clips_for_project(
        "d5815c1e-c641-4f21-a2a4-888388f5157a", default_db())]
    if not clips:
        check("找到真实片段当素材", False, "库里无片段")
        return 1
    stub_clip = clips[0]

    tmp = tempfile.mkdtemp(prefix="vf_regen_")
    db_path = os.path.join(tmp, "videoforge.db")
    port = free_port()
    proc = None
    try:
        # ── 起桩后端（它会自己 seed 一个 4 镜头项目：1、2 同场景 A，3、4 同场景 B）──
        #   ★ 不在测试进程里建数据：实测那边 `Database(db_path)` 的表现和这里不一致
        #     （会列出真实项目列表、而且没在给定路径建文件）。放到同一个进程里建，
        #     DB 路径就只有一份，不会有分歧。
        #    ★★ 输出必须写**日志文件**，不能用 `subprocess.PIPE`：
        #      管道缓冲只有 64KB，而桩后端每调一次 ffmpeg 就会打一行
        #      "FFmpeg found (imageio-ffmpeg)…"。攒满 64KB 之后
        #      **子进程会阻塞在 write 上，它的事件循环跟着停摆** ——
        #      表现就是"状态接口突然超时、看起来像功能卡住"。
        #      这个坑 §16.16 在另一个测试里踩过（那次修的是 test_chain_to_film），
        #      这次是同一类：本轮多了一次 ffmpeg 调用（时长台账要量实际时长），
        #      日志刚好越过了 64KB 阈值。
        stub_log = os.path.join(os.path.dirname(db_path), "stub_server.log")
        log_fh = open(stub_log, "wb")
        proc = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "_stub_server.py"),
             str(port), db_path, stub_clip, "--seed"],
            stdout=log_fh, stderr=subprocess.STDOUT)
        up = False
        for _ in range(120):
            try:
                if req(port, "GET", "/api/health", timeout=3).get("data"):
                    up = True
                    break
            except Exception:
                time.sleep(0.5)
        check("A0 桩后端起来了（含 seed 数据）", up, f"port={port}")
        if not up:
            try:
                log_fh.flush()
                with open(stub_log, "rb") as f:
                    print((f.read() or b"")[-2000:].decode("utf-8", "replace"))
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
        check("A0b 拿到 seed 的项目 id", bool(pid), str(seed)[:120])
        if not pid:
            return 1
        print(f"\nseed 项目 {pid[:8]}：4 个镜头，镜头1/2 = 场景A，3/4 = 场景B；"
              f"镜头1、2 已有'旧视频'")

        # ── A. 预演：报清"调几次、串几次"，且不产生任何生成 ──
        print("\n" + "=" * 74)
        print("A. 预演（dry_run 默认）：只说清楚要花几次钱，不动任何东西")
        print("=" * 74)
        d = req(port, "POST", f"/api/projects/{pid}/regen-chained", {})["data"]
        print(f"  会调用模型 {d['will_call_model_times']} 次；"
              f"其中 {d['will_chain_times']} 次用上串联")
        for p in d["plan"]:
            print(f"    镜{p['index']} 串={p['will_chain']}  "
                  f"{p['chain_why'][:40]}")
        check("A1 预演报告了调用次数", d["will_call_model_times"] == 4,
              str(d["will_call_model_times"]))
        check("A2 预演保守地数出『至少会串几次』（镜4 的上一镜还没生成，预演算不出来）",
              d["will_chain_times"] >= 1, str(d["will_chain_times"]))
        _p4 = next((p for p in d["plan"] if p["index"] == 4), {})
        check("A2b 预演如实说明镜4 为什么暂时不算串（上一镜还没视频）",
              _p4.get("chain_skip") == "prev_no_video", str(_p4.get("chain_why"))[:60])
        check("A3 预演明确提示会花钱", "计费" in (d.get("cost_warning") or ""),
              (d.get("cost_warning") or "")[:60])
        _s3 = req(port, "GET", f"/api/shots/{seed['shot_ids'][2]}")["data"]
        _c3 = _s3.get("candidates")
        if isinstance(_c3, str):
            _c3 = json.loads(_c3 or "[]")
        check("A4 预演不改数据（镜头3 仍然没有视频）", not _c3,
              f"{len(_c3 or [])} 个候选")

        # ── B. 真跑：按序生成 + 串联 + 前后接缝对比 ──
        print("\n" + "=" * 74)
        print("B. 真跑（用桩模型）：按序生成、同场景自动串、给出前后对比")
        print("=" * 74)
        r = req(port, "POST", f"/api/projects/{pid}/regen-chained",
                {"dry_run": False, "only_missing": False})
        check("B0 启动成功", bool(r["data"].get("started")), str(r["data"])[:120])
        st = {}
        for _ in range(600):
            st = req(port, "GET", f"/api/projects/{pid}/regen-chained/status")["data"]
            if not st.get("running"):
                break
            time.sleep(1.0)
        res = st.get("results") or []
        print(f"  进度 {st.get('percent')}%  消息: {st.get('message')}")
        for x in res:
            print(f"    镜{x['index']} ok={x['ok']} 串={x.get('chained')} "
                  f"{('← ' + str(x.get('chain_from'))[:8]) if x.get('chain_from') else ''}"
                  f"{('  err=' + str(x.get('error'))[:50]) if x.get('error') else ''}")
        ok_n = sum(1 for x in res if x["ok"])
        check("B1 四个镜头都生成成功", ok_n == 4, f"{ok_n}/4")
        chained = [x["index"] for x in res if x.get("chained")]
        print(f"  实际串上的镜头：{chained}")
        check("B2 同场景的镜2、镜4 串上了（换了场景的镜3 不串）",
              chained == [2, 4], str(chained))
        check("B3 镜1 不串（全片第一镜）", 1 not in chained, str(chained))

        sb_ = st.get("seam_before") or {}
        sa_ = st.get("seam_after") or {}
        print(f"\n  接缝强度 before={sb_.get('mean_seam_ratio')} "
              f"after={sa_.get('mean_seam_ratio')}")
        check("B4 给出了 before 接缝指标（有旧视频可量）",
              sb_.get("mean_seam_ratio") is not None, str(sb_)[:100])
        check("B5 给出了 after 接缝指标", sa_.get("mean_seam_ratio") is not None,
              str(sa_)[:100])
        check("B6 串上后接缝强度下降（这就是『更像一镜到底』的量化证据）",
              (sa_.get("mean_seam_ratio") or 99) < (sb_.get("mean_seam_ratio") or 0),
              f"{sb_.get('mean_seam_ratio')} → {sa_.get('mean_seam_ratio')}")

        # 库里确实写进了新候选
        fresh = req(port, "GET", f"/api/shots/{seed['shot_ids'][1]}")["data"]
        cands = fresh.get("candidates")
        if isinstance(cands, str):
            cands = json.loads(cands or "[]")
        check("B7 新生成的视频写进了分镜候选",
              bool(cands) and any(c.get("source") == "api" for c in cands),
              f"{len(cands or [])} 个候选")
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except Exception:
                proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 74)
    print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
    for f in FAIL:
        print("  [x] " + f)
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
