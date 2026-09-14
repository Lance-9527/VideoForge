# -*- coding: utf-8 -*-
"""生成条件一致性测试 —— 治"多镜头拼起来一眼AI"的**第一现场**。

═══ 为什么这是第一现场 ═══
用眼睛看线上素材（`tests/_visual/seams.png`）：
    第 1 镜是 3D 卡通风（蓝色马甲、圆眼镜），第 2 镜是写实真人。
查库：项目 `19636f68` 的 6 个分镜用了 **3 种不同模型**
（video-01 × 2 / MiniMax-Hailuo-02 × 2 / kling-1.6 × 2）。

不同模型 = 不同画风 / 色彩科学 / 运动风格。**后期补不回来。**
所以"让拼接丝滑"这件事，一半的功夫在**生成之前**：
确保一部片子的所有镜头由同一个模型、同一画幅、同一分辨率画出来。

运行：python tests/test_consistency.py
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return cond


def mk(i, prov="hailuo", name="video-01", res="1080P", ar="16:9", vid=False):
    return {"id": f"s{i}", "order_index": i, "model_provider": prov,
            "model_name": name, "resolution": res, "aspect_ratio": ar,
            "candidates": json.dumps([{"path": "x.mp4"}]) if vid else "[]"}


def part_a():
    print("\n" + "=" * 74)
    print("A. 一致性分级与体检")
    print("=" * 74)
    from core.consistency import diff_shots, plan_unify, consistency_brief, \
        canonical_from_shots, severity, default_shot_conditions

    shots = [mk(0), mk(1, vid=True),
             mk(2, name="MiniMax-Hailuo-02", ar="1:1", vid=True),
             mk(3, res="720P")]
    canon = canonical_from_shots(shots)
    print(f"  基准 = {canon}")
    check("A1 基准取多数派模型（video-01 × 3）",
          canon["model_name"] == "video-01", canon["model_name"])
    check("A2 基准画幅取多数派（16:9 × 3）",
          canon["aspect_ratio"] == "16:9", canon["aspect_ratio"])

    # 平票时优先"已出过片"的条件
    tie = [mk(0, name="A"), mk(1, name="B", vid=True)]
    c2 = canonical_from_shots(tie)
    check("A3 平票时优先已出过片的模型（用户真看过的更可信）",
          c2["model_name"] == "B", c2["model_name"])

    # 分级
    lvl, why = severity(mk(0, name="其他模型"), canon)
    check("A4 换模型 = fatal（后期救不回来）", lvl == "fatal", f"{lvl} {why}")
    lvl2, why2 = severity(mk(0, ar="1:1"), canon)
    check("A5 换画幅 = major（能裁但代价大）", lvl2 == "major", f"{lvl2} {why2}")
    lvl3, why3 = severity(mk(0, res="720P"), canon)
    check("A6 换分辨率 = minor（缩放即可）", lvl3 == "minor", f"{lvl3} {why3}")
    lvl4, _ = severity(mk(0), canon)
    check("A7 完全一致 = ok", lvl4 == "ok", lvl4)

    rep = diff_shots(shots)
    print(f"  体检: {rep['counts']}  最差={rep['worst_level']}  一致={rep['consistent']}")
    check("A8 体检数出 2 个偏离（画幅 fatal + 分辨率 minor）",
          rep["counts"]["fatal"] == 1 and rep["counts"]["minor"] == 1,
          str(rep["counts"]))
    check("A9 有视频产物的偏离排在最前（它们不改就永远不一致）",
          rep["deviations"][0]["has_video"] is True,
          str([(d["index"], d["has_video"]) for d in rep["deviations"]]))

    plan = plan_unify(shots)
    print(f"  改法 {plan['action_count']} 条，需重生成 {plan['need_regenerate']} 个")
    for a in plan["actions"]:
        print(f"     #{a['index']} patch={a['patch']} 必须重生成={a['must_regenerate']}")
    check("A10 改法只改偏离的字段（不动一致的字段）",
          all(set(a["patch"]) <= {"model_provider", "model_name",
                                  "resolution", "aspect_ratio"}
              and a["patch"] for a in plan["actions"]),
          str([a["patch"] for a in plan["actions"]]))
    check("A11 已出过片的偏离被标记为『必须重新生成』",
          any(a["must_regenerate"] for a in plan["actions"]),
          str([(a["index"], a["must_regenerate"]) for a in plan["actions"]]))
    check("A12 告警说清了『不同模型后期补不回来』",
          any("补不回来" in w for w in plan["warnings"]),
          str(plan["warnings"])[:160])
    check("A13 告警提醒『改条件必须重新生成』",
          any("重新生成" in w for w in plan["warnings"]),
          str([w for w in plan["warnings"] if "重新生成" in w])[:160])
    print(f"  简报: {consistency_brief(shots)}")
    check("A14 简报指出不一致",
          "不一致" in consistency_brief(shots), consistency_brief(shots))
    check("A15 全一致时简报为『一致』",
          "一致" in consistency_brief([mk(0), mk(1)])
          and "不一致" not in consistency_brief([mk(0), mk(1)]),
          consistency_brief([mk(0), mk(1)]))

    # 新分镜默认条件：项目优先于全局
    d = default_shot_conditions({"default_model_name": "全局新模型"}, shots)
    check("A16 新分镜默认跟**项目内已有镜头**一致，而不是跟全局设置",
          d["model_name"] == "video-01", d["model_name"])
    d2 = default_shot_conditions({"default_model_name": "全局新模型"}, [])
    check("A17 项目为空时才用全局设置", d2["model_name"] == "全局新模型", d2["model_name"])
    return


def part_b():
    print("\n" + "=" * 74)
    print("B. 线上真实项目体检（这就是用户看到『两个世界』的原因）")
    print("=" * 74)
    from core.consistency import diff_shots, plan_unify
    db = os.path.join(os.environ.get("LOCALAPPDATA", ""), "VideoForge", "data",
                      "videoforge.db")
    if not os.path.exists(db):
        check("B0 找到用户库", False, db)
        return
    con = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM shots ORDER BY project_id, order_index")]
    by = {}
    for r in rows:
        by.setdefault(r["project_id"], []).append(r)

    bad = 0
    print(f"  {'项目':<10} {'分镜':>4} {'一致':>5} {'fatal':>6} {'major':>6} "
          f"{'minor':>6}  基准模型")
    for pid, shots in sorted(by.items(), key=lambda kv: -len(kv[1])):
        rep = diff_shots(shots)
        c = rep["counts"]
        if not rep["consistent"]:
            bad += 1
        print(f"  {pid[:8]:<10} {len(shots):>4} {str(rep['consistent']):>5} "
              f"{c['fatal']:>6} {c['major']:>6} {c['minor']:>6}  "
              f"{rep['canonical'].get('model_name') or '(未知)'}")

    # 那个有真实视频、且用混了 3 种模型的项目
    target = "19636f68-9d0f-4555-b268-1088ca018b33"
    if target in by:
        plan = plan_unify(by[target])
        print(f"\n  19636f68 的统一方案（{plan['action_count']} 条改动，"
              f"{plan['need_regenerate']} 个需重新生成）：")
        for a in plan["actions"][:6]:
            print(f"     #{a['index']} {a['reasons']} → {a['patch']}")
        for w in plan["warnings"]:
            print(f"     ⚠ {w}")
        check("B1 真实项目被判定为不一致", not plan["report"]["consistent"],
              str(plan["report"]["counts"]))
        check("B2 真实项目里确实有 fatal（换了模型）",
              plan["report"]["counts"]["fatal"] > 0,
              str(plan["report"]["counts"]))
    check("B3 体检覆盖了全部项目", len(by) > 0, f"{len(by)} 个项目")
    print(f"\n  {len(by)} 个项目里 {bad} 个生成条件不一致")
    return


def main() -> int:
    part_a()
    part_b()
    print("\n" + "=" * 74)
    print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
    for f in FAIL:
        print("  [x] " + f)
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
