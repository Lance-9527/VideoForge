# -*- coding: utf-8 -*-
r"""联网核对：**音色池里的每一把，引擎到底提不提供**。

这个脚本存在的唯一原因，是 2026-09-13 那个真实事故：
免费 Edge 端点当时只提供 8 个 zh-CN 音色，而池子里写着 18 个 ——
**多出来的 10 个根本不存在的音色**，被分到的角色整句话合成失败、
只在警告里留一行字。用户听到的是"有的角色不说话"。

池子是一份**引擎能力清单**，不是愿望清单。这个脚本就是去核对清单：
  1. 列一次真实音色（`edge_tts.list_voices()`）；
  2. 池子里每一个 edge 音色必须出现在列表里；
  3. 顺带报出"池子没有覆盖哪些年龄段" —— 那是能力缺口，不是 bug，
     但必须让人看见（`cast_plan` 会据此给出降级告警）。

**需要联网**，所以它不在离线回归门禁里（`run_round_regression.py`）。
断网时明确报"跳过"，不冒充通过。

用法：python tests/test_voice_pool_online.py
"""
import asyncio
import io
import sys
import os

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))

from core import voicecast  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


async def main() -> int:
    try:
        import edge_tts
    except Exception as e:
        print(f"➖ 跳过：装不上 edge_tts（{e}）")
        return 0
    try:
        voices = await asyncio.wait_for(edge_tts.list_voices(), timeout=45)
    except Exception as e:
        print(f"➖ 跳过：拉不到音色列表（{type(e).__name__}: {e}）")
        print("   —— 这是**跳过**，不是通过。要判定池子是否有效必须联网。")
        return 0

    served = {v["ShortName"] for v in voices if str(v.get("Locale", "")).startswith("zh-CN")}
    print(f"引擎当前提供的 zh-CN 音色：{len(served)} 个")
    for s in sorted(served):
        print("   ", s)

    pool = list(voicecast._MALE_POOL) + list(voicecast._FEMALE_POOL)
    print(f"\n池子里声明了 {len(pool)} 个音色：")
    missing = []
    for vid, gender, band, style, label in pool:
        short = vid.split(":", 1)[1] if ":" in vid else vid
        ok = short in served
        if not ok:
            missing.append(f"{vid}（{label}）")
        print(f"  [{'PASS' if ok else 'FAIL'}] {vid:<42}{gender}/{band:<7}{style:<9}{label}")
    check(f"池子里 {len(pool)} 个音色全部真实存在", not missing,
          "不存在：" + "；".join(missing))

    # 年龄段覆盖：这是**能力缺口**，只需要让人看见
    have = {b for _v, _g, b, _s, _l in pool}
    gap = [b for b in ("child", "teen", "young", "adult", "mature", "senior")
           if b not in have]
    print(f"\n年龄段覆盖：{sorted(have)}")
    print(f"缺口（引擎没有对应音色）：{gap or '无'}")
    if gap:
        print("  → 这不判失败：它是端点的能力边界。"
              "`cast_plan` 会对落到这些段的角色给出**降级告警**，不会假装配上。")

    # 反向核对：引擎提供的音色里，有没有我们"漏掉"的可用中文音色
    unused = sorted(s for s in served
                    if s not in {v.split(":", 1)[1] for v, *_ in pool})
    if unused:
        print(f"\n提示：引擎还有 {len(unused)} 个 zh-CN 音色没进池子：{unused}")
        print("  → 加进来可以提升区分度；但必须先用本脚本确认它在列表里，"
              "再加进池子（顺序不能反）。")

    print("\n" + "=" * 70)
    print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
