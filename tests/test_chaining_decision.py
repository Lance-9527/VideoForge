# -*- coding: utf-8 -*-
"""镜头间串联（尾帧 → 首帧）的决策测试。

═══ 为什么这件事值得单独测 ═══
把上一镜的尾帧当这一镜的首帧，是让多镜头看起来像一镜到底**最直接**的一招：
画面从上一镜里"长出来"，而不是重新开一个毫不相干的镜头。
而**串不串是纯逻辑**，必须先能测 —— 它就是"像不像一镜到底"的总开关。

线上事实（上一轮实测）：`d5815c1e` 这个项目 **1 个模型 / 1 个场景 / 5 个镜头**，
生成条件完全一致，本该最像一镜到底，可它的接缝仍是**自身运动尺度的 12~13 倍**
—— 说明这些镜头是各自独立生成的，串联根本没生效。

发现的问题：文档写着"同场景内的镜头优先串联"，但代码里**根本没判场景**。
跨场景硬串会把白天战壕的最后一帧当成夜晚宫殿的起点，模型被锚在错误的地点/光影上，
出来的画面既不像新场景也不像旧场景，**比不串更糟**。

修法：把决策从 `main.py` 的闭包里抽成 `core.continuity.chain_decision()` 纯函数，
顺手补上场景判断。本文件就是它的测试。

运行：python tests/test_chaining_decision.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return cond


def shot(i, scene_id, sid=None, prov="hailuo", model="video-01"):
    return {"id": sid or f"s{i}", "order_index": i, "scene_id": scene_id,
            "model_provider": prov, "model_name": model}


def main() -> int:
    from core.continuity import chain_decision

    A, B = "scene-A", "scene-B"
    yes = dict(prev_has_video=True, model_supports_first_frame=True)
    print("\n" + "=" * 74)
    print("串联决策（纯函数，不碰文件/DB）")
    print("=" * 74)

    d = chain_decision(None, shot(0, A), **yes)
    print(f"  ① 全片第一镜            → chain={d['chain']}  {d['why'][:40]}")
    check("① 全片第一个镜头不串联（没有可承接的画面）",
          not d["chain"] and d["skipped_reason"] == "first_shot", d["skipped_reason"])

    d = chain_decision(shot(0, A), shot(1, A), **yes)
    print(f"  ② 同场景、上一镜有视频  → chain={d['chain']}  {d['why'][:44]}")
    check("② 同场景 + 上一镜有视频 + 模型支持首帧 → 串联",
          d["chain"] is True, d["why"][:60])

    d = chain_decision(shot(1, A), shot(2, B), **yes)
    print(f"  ③ **换场景**            → chain={d['chain']}  {d['why'][:44]}")
    check("③ 换场景时不串联（本轮修的 bug）",
          not d["chain"] and d["skipped_reason"] == "scene_changed", d["skipped_reason"])
    check("③b 并说明理由", "场景" in d["why"], d["why"][:60])

    d = chain_decision(shot(0, A), shot(1, A),
                       prev_has_video=False, model_supports_first_frame=True)
    print(f"  ④ 上一镜还没视频        → chain={d['chain']}  {d['why'][:44]}")
    check("④ 上一镜没有视频时不串联（并说明，不静默跳过）",
          not d["chain"] and d["skipped_reason"] == "prev_no_video", d["skipped_reason"])

    d = chain_decision(shot(0, A), shot(1, A),
                       prev_has_video=True, model_supports_first_frame=False)
    print(f"  ⑤ 模型不支持首帧        → chain={d['chain']}  {d['why'][:44]}")
    check("⑤ 模型不支持首帧时不串联",
          not d["chain"] and d["skipped_reason"] == "model_no_first_frame",
          d["skipped_reason"])

    # 老数据没挂场景 → 按"未知"放过，别因为缺字段把该串的也拦掉
    d = chain_decision(shot(0, ""), shot(1, A), **yes)
    print(f"  ⑥ 上一镜场景为空        → chain={d['chain']}")
    check("⑥ 场景信息缺失时放过（宁可少拦，不要因为缺字段拦住该串的）",
          d["chain"] is True, d["skipped_reason"] or d["why"][:40])
    d = chain_decision(shot(0, A), shot(1, ""), **yes)
    check("⑥b 当前镜场景为空时也放过", d["chain"] is True, d["skipped_reason"] or "")

    # 场景比较不能靠字符串前缀之类的小聪明
    d = chain_decision(shot(0, A), shot(1, A + "-2"), **yes)
    check("⑦ 场景 id 只是前缀相同也算换了场景（必须精确比较）",
          not d["chain"] and d["skipped_reason"] == "scene_changed", d["skipped_reason"])

    # 优先级：换场景比"上一镜没视频"更先判（理由要说对）
    d = chain_decision(shot(0, A), shot(1, B),
                       prev_has_video=False, model_supports_first_frame=True)
    check("⑧ 同时满足多个'不串'条件时，理由优先说换场景",
          d["skipped_reason"] == "scene_changed", d["skipped_reason"])

    # ── ⑩~⑫ 换场景用"硬切"还是"溶解"：开关必须真的生效，且不许被静默覆盖 ──
    print("\n" + "=" * 74)
    print("换场转场开关（scene_change）：两种取向都要能被表达出来")
    print("=" * 74)
    from core.continuity import pick_transition
    sc_a = {"id": A, "name": "甲地", "time_of_day": "day"}
    sc_b = {"id": B, "name": "乙地", "time_of_day": "day"}

    t_def = pick_transition(shot(0, A), shot(1, B), sc_a, sc_b)
    t_dis = pick_transition(shot(0, A), shot(1, B), sc_a, sc_b, scene_change="dissolve")
    t_same = pick_transition(shot(0, A), shot(1, A), sc_a, sc_a)
    print(f"  ⑩ 跨场景默认 → {t_def['type']} respect={t_def.get('respect')}")
    check("⑩ 默认跨场景是硬切（2026-09-13 用户看过两版成片后定的）",
          t_def["type"] == "none" and float(t_def["seconds"]) == 0.0, str(t_def)[:120])
    check("⑩b 默认硬切也必须带 respect（否则会被自适应层改回溶解）",
          bool(t_def.get("respect")), str(t_def.get("respect")))
    print(f"  ⑪ 显式 scene_change=dissolve → {t_dis['type']} {t_dis['seconds']}s")
    check("⑪ 显式要溶解时给溶解（另一取向也必须能表达）",
          t_dis["type"] == "fade" and float(t_dis["seconds"]) > 0, str(t_dis)[:120])
    print(f"  ⑫ 同场景 → {t_same['type']}")
    check("⑫ 同场景内切镜本来就是硬切",
          t_same["type"] == "none", str(t_same))

    # ── 用真实项目的数据核一遍：这个项目到底该不该串 ──
    print("\n" + "=" * 74)
    print("真实项目：若同一场景内的镜头都串上，能覆盖多少个接缝")
    print("=" * 74)
    import json
    import sqlite3
    db = os.path.join(os.environ.get("LOCALAPPDATA", ""), "VideoForge", "data",
                      "videoforge.db")
    if os.path.exists(db):
        con = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM shots ORDER BY project_id, order_index")]
        by = {}
        for r in rows:
            by.setdefault(r["project_id"], []).append(r)
        tot = chained = 0
        for pid, shots in by.items():
            for i in range(1, len(shots)):
                tot += 1
                d2 = chain_decision(shots[i - 1], shots[i], **yes)
                if d2["chain"]:
                    chained += 1
        print(f"  全库相邻接缝 {tot} 个，其中**同一场景内**（可串联）{chained} 个 "
              f"= {chained/max(1,tot)*100:.0f}%")
        print(f"  剩下 {tot-chained} 个是换场景 —— 那些**不该**串，硬切才对")
        check("⑨ 能算出全库可串联的接缝比例", tot > 0, f"{chained}/{tot}")
    else:
        check("⑨ 找到用户库", False, db)

    print("\n" + "=" * 74)
    print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
    for f in FAIL:
        print("  [x] " + f)
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
