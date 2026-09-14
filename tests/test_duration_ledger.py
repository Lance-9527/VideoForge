"""时长台账测试：把「厂商静默给你更短的片」这件事变成规划层能用的实测量。

背景（同一类病在本项目里栽过三次）：
  · `video-01` 请求 1080P/10s → 厂商**接受**（status_code=0）却只给 1280×720/5.64s；
  · 老版本分镜标称 10s → 实际 5.6s，而库里/时间轴/字幕全按 10s 排；
  · 后果不是"少一点画面"，而是**成片缺内容**（要么留黑、要么循环重播＝卡带感）。
静态能力表追不上（换型号、换版本、厂商悄悄改），所以每次真实生成都记账，
规划层用**实测天花板**决定还能不能要那一档。

用法：python tests/test_duration_ledger.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f"  -- {detail}" if detail else ""))


def fresh():
    """每个场景用**独立的临时台账**，互不影响。"""
    os.environ["VIDEOFORGE_DURATION_LEDGER"] = os.path.join(
        tempfile.mkdtemp(prefix="vf_durled_"), "d.json")
    os.environ.pop("VIDEOFORGE_NO_DURALEDGER", None)
    from core import duraledger as D
    D._CACHE = None
    return D


def main() -> int:
    print("=" * 74)
    print("① 不猜：没观测过就什么都不动")
    print("=" * 74)
    D = fresh()
    check("①a 空台账 → 没有不可用档",
          D.unreliable_durations("hailuo", "X", "768P", [6, 10]) == [])
    D.record("hailuo", "X", "768P", 10, 5.6)
    check("①b 只观测 1 次 → 仍然不动（保守：一次可能是偶发/量错）",
          D.unreliable_durations("hailuo", "X", "768P", [6, 10]) == [])

    print()
    print("=" * 74)
    print("② 静默忽略被抓：这一档的实测天花板明显低于某个时长档")
    print("=" * 74)
    D = fresh()
    D.record("hailuo", "video-01", "1080P", 6, 5.64)
    D.record("hailuo", "video-01", "1080P", 10, 5.64)
    bad = D.unreliable_durations("hailuo", "video-01", "1080P", [6, 10])
    check("②a 要 10s 只给 5.64s（两次）→ 10s 被标为发不出",
          bad == [10], str(bad))
    check("②b 6s 档保留（天花板 5.64 已能覆盖 6s 的 80%）",
          6 not in bad, str(bad))

    print()
    print("=" * 74)
    print("③ 不误伤：正常型号（要 10s 真给 10.13s）必须原样保留")
    print("=" * 74)
    D = fresh()
    D.record("hailuo", "MiniMax-Hailuo-02", "768P", 6, 5.88)
    D.record("hailuo", "MiniMax-Hailuo-02", "768P", 10, 10.13)
    check("③a 健康型号没有任何档被裁",
          D.unreliable_durations("hailuo", "MiniMax-Hailuo-02", "768P", [6, 10]) == [])
    caps = {"768P": [6, 10]}
    new, notes = D.filter_caps(caps, "hailuo", "MiniMax-Hailuo-02")
    check("③b filter_caps 原样返回且不产生说明",
          new == caps and not notes, f"{new} / {notes}")

    print()
    print("=" * 74)
    print("④ 异常观测要被剔除（实际比请求长很多 = 多半量错了）")
    print("=" * 74)
    D = fresh()
    D.record("hailuo", "Y", "768P", 10, 10.1)      # 正常
    D.record("hailuo", "Y", "768P", 6, 30.0)       # 异常：要 6 给 30
    check("④ 剔除异常后天花板仍是 10.1 → 不裁任何档",
          D.unreliable_durations("hailuo", "Y", "768P", [6, 10]) == [])

    print()
    print("=" * 74)
    print("⑤ 接到能力表与规划：用户能看到「为什么给我拆成 6+6」")
    print("=" * 74)
    D = fresh()
    from core.model_catalog import caps_for, caps_for_notes
    from core.shotplan import plan_shot
    shot = {"id": "s", "duration_seconds": 20, "aspect_ratio": "16:9",
            "layer1_overview": "测试",
            "layer2_timeline": json.dumps(
                [{"start": 0, "end": 20, "action": "镜头推进"}], ensure_ascii=False)}
    before = caps_for("hailuo", "MiniMax-Hailuo-02")
    check("⑤a 观测前静态表是 [6, 10]", before.get("768P") == [6, 10], str(before))
    D.record("hailuo", "MiniMax-Hailuo-02", "768P", 10, 5.90)
    D.record("hailuo", "MiniMax-Hailuo-02", "768P", 10, 5.88)
    after = caps_for("hailuo", "MiniMax-Hailuo-02")
    check("⑤b 观测两次后 10s 档被实测裁掉", after.get("768P") == [6], str(after))
    notes = caps_for_notes("hailuo", "MiniMax-Hailuo-02")
    check("⑤c 给出人话说明（含实测值）",
          bool(notes) and "5.9" in notes[0] and "6s" in notes[0], str(notes)[:150])
    p = plan_shot(shot, model_entry={"id": "MiniMax-Hailuo-02", "label": "海螺02",
                                     "caps": after},
                  resolution="768P", provider="hailuo")
    check("⑤d 20s 的镜头改成按 6s 拆（而不是按 10s 排、最后只拿到 11.3s）",
          [s["duration"] for s in p["segments"]] == [6, 6, 6, 6],
          str([s["duration"] for s in p["segments"]]))
    check("⑤e 计划里带上了台账说明",
          any("实测" in str(a) for a in p["adjustments"]),
          str(p["adjustments"])[:160])

    print()
    print("=" * 74)
    print("⑥ 开关：VIDEOFORGE_NO_DURALEDGER=1 时完全不介入（调试/单测用）")
    print("=" * 74)
    D = fresh()
    D.record("hailuo", "Z", "768P", 10, 5.6)
    D.record("hailuo", "Z", "768P", 10, 5.6)
    os.environ["VIDEOFORGE_NO_DURALEDGER"] = "1"
    D._CACHE = None
    check("⑥ 关掉后不裁任何档",
          D.unreliable_durations("hailuo", "Z", "768P", [6, 10]) == [])
    os.environ.pop("VIDEOFORGE_NO_DURALEDGER", None)

    print()
    print("=" * 74)
    print("⑦ 健壮性：带 BOM 的台账要能读；坏文件要**出声**而不是静默失效")
    print("=" * 74)
    D = fresh()
    p = D.ledger_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8-sig") as f:      # ← 带 BOM（实测踩过）
        json.dump({"hailuo|Z|768P": {"obs": [[10.0, 5.6], [10.0, 5.6]],
                                     "n": 2, "max_actual": 5.6, "last_at": 0}}, f)
    D._CACHE = None
    check("⑦a 带 UTF-8 BOM 的台账能正常读（旧代码抛 Unexpected UTF-8 BOM 后被吞掉）",
          D.unreliable_durations("hailuo", "Z", "768P", [6, 10]) == [10],
          str(D.unreliable_durations("hailuo", "Z", "768P", [6, 10])))
    with open(p, "w", encoding="utf-8") as f:
        f.write("{这不是 JSON")
    D._CACHE = None
    import logging
    records = []

    class _Cap(logging.Handler):
        def emit(self, rec):
            records.append(rec.getMessage())

    lg = logging.getLogger("videoforge.duraledger")
    h = _Cap()
    lg.addHandler(h)
    got = D.load()
    lg.removeHandler(h)
    check("⑦b 坏文件不崩、返回空台账",
          got == {}, str(got))
    check("⑦c 并且**打日志**（台账失效必须看得见，不能静默）",
          any("台账" in m for m in records), str(records)[:120])

    print()
    print("=" * 74)
    print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
    for f in FAIL:
        print("  [x] " + f)
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
