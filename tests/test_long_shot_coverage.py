# -*- coding: utf-8 -*-
"""长分镜能不能被完整覆盖？—— 用户那句"剧本提到的内容根本没法完成"的正面回答。

═══ 问题 ═══
用户原话：
  "目前因为海螺H3模型的限制，每次最多生成6秒的视频，但是我的分镜剧本对应的
   都是10秒左右及以上的长度，所以我的分镜里面很多分镜分层的视频根本没有生成
   —— 所以剧本提到的内容根本没法完成！！！"

两件事要分开看，这里验证的是**规划与请求这一层**：

  ① **时长对不上**：真实约束是"某分辨率下只允许某些时长"
     （海螺 Hailuo-2.3 = {'768P': [6,10], '1080P': [6]}），
     而适配器只暴露一个标量 `max_duration=10`。于是 10s @1080P
     会被标量校验**放行**，发出去被厂商拒绝或**静默截成 6 秒**。
  ② **怎么补救**：要么**降一档分辨率**一次出完（768P 支持 10 秒），
     要么**拆段**（6+6）。`core.shotplan` 会选**调用次数最少**的那个。

本文件对每个海螺模型 × 每个分辨率 × 6~30 秒逐档验证：
  · 每一段都在该分辨率的**合法集合**里（发出去不会被拒）
  · **总覆盖时长 ≥ 分镜标称**（内容装得下，不会被截短）
  · **吸附到合法值时不会静默改短**（改了必须有说明）

运行：python tests/test_long_shot_coverage.py
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return cond


def mk_shot(dur):
    return {
        "id": "s", "project_id": "p", "duration_seconds": dur,
        "layer1_overview": "大厅里两个人对峙",
        "layer2_timeline": json.dumps(
            [{"index": 0, "start": 0, "end": dur, "camera": "中景",
              "action": "人物走动", "dialogue": {"character": "", "text": ""}}],
            ensure_ascii=False),
        "resolution": "1080p",
    }


def main() -> int:
    from core.model_catalog import VIDEO_CATALOG
    from core.shotplan import plan_shot, allowed_durations
    from core.adapters import get_adapter
    from main import _snap_legal_duration

    models = [m for m in (VIDEO_CATALOG.get("hailuo") or {}).get("models") or []]
    print("=" * 78)
    print("长分镜覆盖：每个海螺模型 × 每个分辨率 × 6~30 秒")
    print("=" * 78)

    bad_illegal, bad_short, bad_silent = [], [], []
    rows = 0
    for m in models:
        mid = m.get("id") or ""
        caps = m.get("caps") or {}
        for res in caps:
            for dur in (6, 8, 10, 12, 15, 18, 20, 24, 30):
                rows += 1
                p = plan_shot(mk_shot(dur), model_entry=m, resolution=res,
                              want_duration=dur)
                segs = p["segments"]
                used_res = p.get("resolution") or res
                allowed = allowed_durations(m, used_res)
                # ① 每段必须合法，且吸附后不变（变了就是静默改短）
                total = 0
                for s in segs:
                    sd = int(s["duration"])
                    total += sd
                    if allowed and sd not in allowed:
                        bad_illegal.append(f"{mid}@{used_res} 计划 {sd}s 不在 {allowed}")
                    a = get_adapter("hailuo", api_key="x", config={"model_name": mid})
                    snapped = _snap_legal_duration(a, "hailuo", mid, used_res, sd)
                    if snapped != sd:
                        bad_silent.append(f"{mid}@{used_res} 计划 {sd}s 被吸附成 {snapped}s")
                # ② 总覆盖必须 ≥ 分镜标称
                if total < dur:
                    bad_short.append(f"{mid}@{used_res} 标称 {dur}s 只覆盖 {total}s")

    print(f"  共验证 {rows} 组（{len(models)} 个模型 × 各自分辨率 × 9 档时长）\n")
    check("① 每一段都在该分辨率的合法时长集合里（发出去不会被拒）",
          not bad_illegal, str(bad_illegal[:4]))
    check("② 总覆盖时长 ≥ 分镜标称（内容装得下，不会被截短）",
          not bad_short, str(bad_short[:4]))
    check("③ 吸附到合法值时不会静默改短（规划出来的段长本来就合法）",
          not bad_silent, str(bad_silent[:4]))

    # ── 用户那个具体情形：海螺 2.3、10 秒分镜、默认 1080P ──
    print("\n" + "=" * 78)
    print("用户的具体情形：MiniMax-Hailuo-2.3（1080P 只给 6 秒）+ 10 秒以上分镜")
    print("=" * 78)
    H = next((m for m in models if m.get("id") == "MiniMax-Hailuo-2.3"), None)
    for dur in (10, 12, 20):
        p = plan_shot(mk_shot(dur), model_entry=H, resolution="1080p",
                      want_duration=dur)
        segs = [(int(s["duration"])) for s in p["segments"]]
        print(f"  {dur:2d}s 分镜 → {p.get('resolution')} · {len(segs)} 段 "
              f"{'+'.join(map(str, segs))} = {sum(segs)}s  "
              f"（{p.get('duration_source')}）")
        for a in p["adjustments"]:
            print(f"       · {a}")
    p10 = plan_shot(mk_shot(10), model_entry=H, resolution="1080p", want_duration=10)
    check("④ 10 秒分镜能一次出完（自动降到 768P，因为 1080P 只给 6 秒）",
          p10.get("resolution") == "768P"
          and [int(s["duration"]) for s in p10["segments"]] == [10],
          f"{p10.get('resolution')} {[int(s['duration']) for s in p10['segments']]}")
    check("⑤ 而且**明确告诉用户**换分辨率是为了什么",
          any("768P" in a for a in p10["adjustments"]),
          str(p10["adjustments"])[:120])

    p20 = plan_shot(mk_shot(20), model_entry=H, resolution="1080p", want_duration=20)
    t20 = sum(int(s["duration"]) for s in p20["segments"])
    check("⑥ 20 秒分镜也能完整覆盖（拆段或降档，总长 ≥20s）", t20 >= 20,
          f"{p20.get('resolution')} {[int(s['duration']) for s in p20['segments']]}")

    # ── 线上那 41 个"没生成"的分镜，用它们的真实标称时长复核一遍 ──
    print("\n" + "=" * 78)
    print("拿线上真实的标称时长复核（那些没生成的分镜是几秒？能覆盖吗？）")
    print("=" * 78)
    import sqlite3
    db = os.path.join(os.environ.get("LOCALAPPDATA", ""), "VideoForge", "data",
                      "videoforge.db")
    if os.path.exists(db):
        con = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        need = {}
        for r in con.execute("SELECT duration_seconds d, model_name m, resolution res, "
                             "candidates c FROM shots"):
            cands = r["c"] or "[]"
            try:
                cands = json.loads(cands) if isinstance(cands, str) else cands
            except Exception:
                cands = []
            if [x for x in (cands or []) if isinstance(x, dict) and x.get("path")]:
                continue
            need[(int(r["d"] or 0), r["m"] or "", r["res"] or "")] = \
                need.get((int(r["d"] or 0), r["m"] or "", r["res"] or ""), 0) + 1
        print(f"  没视频的分镜按（标称时长, 模型, 分辨率）归组：{len(need)} 类")
        okc = badc = 0
        for (d, mname, res), n in sorted(need.items(), key=lambda kv: -kv[1])[:10]:
            entry = next((x for x in models if x.get("id") == mname), None) or H
            p = plan_shot(mk_shot(d), model_entry=entry,
                          resolution=res or "1080p", want_duration=d)
            tot = sum(int(s["duration"]) for s in p["segments"])
            flag = "✅" if tot >= d else "❌"
            if tot >= d:
                okc += 1
            else:
                badc += 1
            print(f"   {flag} ×{n:2d}  {d:2d}s {mname[:22]:22s} @{res:6s} → "
                  f"{p.get('resolution')} {'+'.join(str(int(s['duration'])) for s in p['segments'])}"
                  f" = {tot}s")
        check("⑦ 线上没生成的分镜，规划层全部能覆盖住", badc == 0,
              f"{okc} 类可覆盖 / {badc} 类覆盖不了")
    else:
        check("⑦ 找到用户库", False, db)

    print("\n" + "=" * 78)
    print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
    for f in FAIL:
        print("  [x] " + f)
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
