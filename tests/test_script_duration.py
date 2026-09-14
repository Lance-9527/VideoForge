# -*- coding: utf-8 -*-
r"""剧本「总时长」的离线回归。

用户实测反馈（2026-09-14）：
    「主页这块的总时长可以选择了，比如我选的 5 秒，但我生成剧本的时候发现
      没按我的要求来，居然给了 60 秒」

查真实数据后确认是**两处独立断点**：
  ① 主页选完**没有落库**（项目 `37f40d84` 的 settings 里没有 `default_total_duration`），
     而剧本页读的是 `settings.default_total_duration || 60` → 一律 60；
  ② 后端只是把"总时长 N 秒"写进提示词"请求"模型，**没有强制** ——
     模型吐 5×12 秒我们就照收。

本套件锁死：
  · 场次数由 `scriptplan` 决定（5 秒 → 1 场，60 秒 → 5 场，30 分钟 → 24 场）；
  · 每场秒数之和**必须精确等于**目标（不是"大约"）；
  · AI 多写的场次**不静默丢**：列进 `dropped` 并给出原因；
  · AI 少写时**不硬造**场次（造出来的场次没有内容），只摊时长并说明；
  · `duration_brief` 的 `ok` 只在真正对齐时为真。
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))

from core.scriptplan import (                                    # noqa: E402
    scene_count_for, distribute, fit_scenes, duration_brief,
    MIN_SCENE_SECONDS, SCENE_CAP,
)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def main():
    print("=" * 70)
    print("剧本总时长强制对齐（core.scriptplan）离线回归")
    print("=" * 70)

    # ── T1 场次数由我们决定（不再交给 LLM 估）──
    cases = {5: 1, 10: 1, 15: 1, 30: 2, 45: 4, 60: 5, 90: 8, 120: 10,
             180: 15, 300: 24, 600: 24, 900: 24, 1800: 24}
    bad = {t: (scene_count_for(t), n) for t, n in cases.items() if scene_count_for(t) != n}
    check("T1.1 各时长的场次数符合预期（含 5s→1场、60s→5场、30min→24场封顶）",
          not bad, str(bad))
    check("T1.2 场次数永不超过上限", all(scene_count_for(t) <= SCENE_CAP
                                          for t in range(5, 1900, 7)))
    check("T1.3 每场都不会短于下限 3 秒",
          all(t // scene_count_for(t) >= MIN_SCENE_SECONDS for t in (5, 6, 7, 8, 9, 10, 11)),
          str({t: (scene_count_for(t), t // scene_count_for(t)) for t in (5, 6, 7, 8)}))
    check("T1.4 非法/极小值不炸（0 秒 → 1 场）", scene_count_for(0) == 1)

    # ── T2 分秒必须**精确**等于目标 ──
    bad2 = {t: sum(distribute(t, scene_count_for(t)))
            for t in range(5, 1901, 13)
            if sum(distribute(t, scene_count_for(t))) != t}
    check("T2.1 任意时长分摊后之和精确等于目标（无累计误差）", not bad2, str(list(bad2)[:5]))
    check("T2.2 分摊不出现 0 秒场次", all(
        all(x >= 1 for x in distribute(t, scene_count_for(t))) for t in range(5, 1901, 11)))

    # ── T3 强制重排：真实患者的形状（5 场 × 12 秒 = 60 秒，用户要 5 秒）──
    llm_scenes = [{"scene_number": i + 1, "title": f"第{i+1}场",
                   "location": f"地点{i+1}", "duration_seconds": 12,
                   "actions": ["动作"], "dialogues": [{"character": "A", "text": "台词"}]}
                  for i in range(5)]
    f5 = fit_scenes(llm_scenes, 5)
    check("T3.1 要 5 秒 → 只剩 1 场", f5["scene_count"] == 1, str(f5["scene_count"]))
    check("T3.2 实际总时长 = 5 秒（不是 60 秒）", f5["actual"] == 5, str(f5["actual"]))
    check("T3.3 那一场就是 5 秒", f5["scenes"][0]["duration_seconds"] == 5,
          str(f5["scenes"][0]["duration_seconds"]))
    check("T3.4 被裁掉的 4 场**如实报出来**（不静默丢）", len(f5["dropped"]) == 4,
          str([d["title"] for d in f5["dropped"]]))
    check("T3.5 裁掉理由说清了为什么放不下",
          all("最多容得下" in d["reason"] for d in f5["dropped"]),
          f5["dropped"][0]["reason"][:60] if f5["dropped"] else "")
    check("T3.6 报告里写出了原来的总时长（60s）供人对照",
          f5["before_total"] == 60, str(f5["before_total"]))
    check("T3.7 报告人话说明白（提到你要的秒数）", "5 秒" in f5["note"], f5["note"][:60])

    # ── T4 恰好对齐时不该乱改 ──
    f60 = fit_scenes(llm_scenes, 60)
    check("T4.1 要 60 秒、AI 也写了 5×12 → 一个字节都不用改",
          (not f60["changed"]) and f60["actual"] == 60 and f60["scene_count"] == 5,
          f"changed={f60['changed']}")
    check("T4.2 该改的秒数只在真不一致时记录",
          not any("duration_seconds_before_fit" in s for s in f60["scenes"]))

    # ── T5 AI 少写场次：不硬造 ──
    f_short = fit_scenes([{"scene_number": 1, "title": "唯一一场",
                           "duration_seconds": 10, "actions": ["a"]}], 60)
    check("T5.1 AI 只写 1 场时**不硬造**场次", f_short["scene_count"] == 1,
          str(f_short["scene_count"]))
    check("T5.2 时长仍精确对齐到 60 秒", f_short["actual"] == 60, str(f_short["actual"]))
    check("T5.3 明确说明场次比规划少、已摊时长",
          "只写了" in f_short["note"] and "没有凭空造场次" in f_short["note"],
          f_short["note"][:80])

    # ── T6 边界与脏数据 ──
    check("T6.1 空场次列表不炸", fit_scenes([], 60)["scene_count"] == 0)
    check("T6.2 None 不炸", fit_scenes(None, 30)["actual"] == 0)
    f_dirty = fit_scenes([{"scene_number": 1, "duration_seconds": "abc", "actions": ["a"]},
                          {"scene_number": 2, "duration_seconds": None, "actions": ["b"]}], 30)
    check("T6.3 脏 duration 不影响对齐", f_dirty["actual"] == 30, str(f_dirty["actual"]))
    check("T6.4 不就地改动调用方传进来的对象",
          all("duration_seconds_before_fit" not in s for s in llm_scenes)
          and llm_scenes[0]["duration_seconds"] == 12)

    # ── T7 duration_brief：界面/接口看的就是它 ──
    b5 = duration_brief(f5)
    check("T7.1 brief 里 ok=True 只在真正对齐时成立",
          b5["ok"] and b5["target"] == 5 and b5["actual"] == 5, str(b5["ok"]))
    check("T7.2 brief 带上了被裁场次与说明",
          len(b5["dropped"]) == 4 and bool(b5["note"]))
    check("T7.3 brief 对空输入不炸", duration_brief(None)["ok"] is False)

    print("\n" + "=" * 70)
    print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
    for f in FAIL:
        print("  [x] " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
