# -*- coding: utf-8 -*-
r"""提示词审计的**正/负对照测试**。

为什么要负对照：这次改造之后，真实分镜从"16/16 不合格"变成"16/16 满分" ——
如果审计器本身是空的（什么都判合格），那这个对比毫无意义。
所以这里**故意构造 14 种坏提示词**，逐条要求审计器抓到；
再用正对照（好提示词、以及"地点词不含运镜"的边界情况）要求它不误报。

运行：python tests/test_prompt_audit.py
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core import promptaudit as pa  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return cond


def codes(rep):
    return {i["code"] for i in rep["issues"]}


GOOD = (
    "画面中恰好 1 个主体：农夫 各出现一次；未列出的角色不在画面内。"
    "起始构图（景别）：中景。镜头运动：固定机位（整个镜头只有这一个主运动）。"
    "画面内容：他蹲下身子，双手捧起一条冻僵的蛇，动作缓慢而小心。"
    "结束构图：落在「他把蛇放进怀里」这个状态上，主体仍在画面内、构图稳定。"
    "总时长：5 秒。音频：这一镜没有对白（也没有旁白）；保留现场环境底床；无配乐、无字幕烧录。"
    "风格：电影级写实。"
)


def main() -> int:
    print("=" * 74)
    print("A. 正对照：合格提示词必须通过（否则检查项太严、会天天误报）")
    print("=" * 74)
    rep = pa.audit_video_prompt(GOOD, duration=5)
    check("A1 合格提示词 0 error", rep["error_count"] == 0, str(codes(rep)))
    check("A2 合格提示词 valid_for_generation=True", rep["valid_for_generation"] is True)
    check("A3 合格提示词分数 100", rep["score"] == 100, str(rep["score"]))

    # ★ 边界：地点词里出现运镜字眼，不能误判（参考实现在全文匹配，会误报）
    loc = ("画面中恰好 1 个主体：布兰 各出现一次。起始构图（景别）：全景。"
           "镜头运动：固定机位（只有这一个主运动）。环境：城堡建筑环绕广场，人群聚集。"
           "画面内容：布兰抬头看向铁王座。结束构图：主体仍在画面内、构图稳定。"
           "总时长：5 秒。音频：无配乐；现场环境底床。")
    rep = pa.audit_video_prompt(loc, duration=5)
    check("A4 '城堡建筑环绕广场'（地点关系）不得被判成运镜",
          "P-CAMERA-CONFLICT" not in codes(rep), str(codes(rep)))

    print("\n" + "=" * 74)
    print("B. 负对照：每一种坏提示词都必须被抓到，且是 error 级")
    print("=" * 74)
    cases = [
        ("B1 空提示词", "", {}, "P-EMPTY"),
        ("B2 缺时长", GOOD.replace("总时长：5 秒。", ""), {}, "P-MISSING-DURATION"),
        ("B3 缺结束构图", GOOD.replace(
            "结束构图：落在「他把蛇放进怀里」这个状态上，主体仍在画面内、构图稳定。", ""),
         {}, "P-MISSING-CAMERA-END"),
        ("B4 缺音频边界", GOOD.replace(
            "音频：这一镜没有对白（也没有旁白）；保留现场环境底床；无配乐、无字幕烧录。", ""),
         {}, "P-MISSING-AUDIO"),
        ("B5 两个主运镜", GOOD.replace("镜头运动：固定机位（整个镜头只有这一个主运动）",
                                  "镜头运动：先缓慢推近，随后环绕主体"),
         {}, "P-CAMERA-CONFLICT"),
        ("B6 固定机位 + 运镜", GOOD.replace("镜头运动：固定机位（整个镜头只有这一个主运动）",
                                    "镜头运动：固定机位，同时缓慢推近"),
         {}, "P-CAMERA-CONFLICT"),
        ("B7 静止 + 持续奔跑冲突",
         GOOD.replace("画面内容：他蹲下身子，双手捧起一条冻僵的蛇，动作缓慢而小心。",
                      "画面内容：他保持不动，同时持续奔跑穿过广场。"),
         {}, "P-CONFLICT-MOTION"),
        ("B8 时间范围超出镜头时长",
         GOOD.replace("画面内容：他蹲下身子，双手捧起一条冻僵的蛇，动作缓慢而小心。",
                      "画面内容：0-9 秒他蹲下身子捧起蛇。"),
         {"duration": 5}, "P-TIMELINE-OVERFLOW"),
        ("B9 有判词但没写'对白不视觉化'", GOOD, {"dialogue": ["你为什么咬我"]},
         "P-DIALOGUE-VISUALIZATION"),
        ("B10 负面同义词重复", GOOD, {"negative": "无配乐，不要音乐，无音乐"}, "P-NEGATIVE-DUPLICATE"),
        ("B11 只有抽象词",
         "高级质感，震撼电影感，史诗般的画面，唯美梦幻。",
         {"duration": 5}, "P-ABSTRACT-ONLY"),
        ("B12 主提示词混进私有参数",
         GOOD + " seed=12345 steps=30",
         {"duration": 5}, "P-PLATFORM-MIXED"),
        ("B13 提到参考图但没写继承范围",
         GOOD.replace("画面内容：", "画面内容：参考 image_1 保持一致，"),
         {"duration": 5}, "P-REFERENCE-SCOPE"),
        ("B14 动作超载（5 秒 6 个动作 → 每个 0.83s，低于 1 秒下限）",
         GOOD.replace("画面内容：他蹲下身子，双手捧起一条冻僵的蛇，动作缓慢而小心。",
                      "画面内容：他先站起。然后转身。接着蹲下。随后拾起蛇。最后放进怀里。同时挥手。"),
         {"duration": 5}, "P-ACTION-OVERLOAD"),
    ]
    for name, prompt, kw, want in cases:
        rep = pa.audit_video_prompt(prompt, kw.get("negative", ""),
                                    duration=kw.get("duration"),
                                    dialogue=kw.get("dialogue"))
        got = codes(rep)
        check(f"{name} → {want}", want in got, f"实际 {sorted(got)}")

    print("\n" + "=" * 74)
    print("C. 打分与硬闸门（照抄参考的口径）")
    print("=" * 74)
    rep = pa.audit_video_prompt("", "")
    check("C1 空提示词分数为 0", rep["score"] == 0, str(rep["score"]))
    bad = pa.audit_video_prompt("画面内容：他蹲下。", duration=5)
    check("C2 有 error 时 valid_for_generation=False",
          bad["error_count"] > 0 and bad["valid_for_generation"] is False,
          f"errors={bad['error_count']} score={bad['score']}")
    check("C3 分数公式 = 100-15*err-5*warn",
          bad["score"] == max(0, 100 - bad["error_count"] * 15 - bad["warning_count"] * 5),
          f"{bad['score']} vs 100-15*{bad['error_count']}-5*{bad['warning_count']}")
    check("C4 审计器不吞异常：返回结构完整",
          all(k in rep for k in ("issues", "score", "valid_for_generation", "summary")))

    # ══════════════════════════════════════════════════════════════
    # D. 结构不变量：videoprompt 能吐出来的**每一个**运镜名，
    #    审计器都必须看成一个运镜（0 或 1 个），绝不能看成两个。
    #
    #    为什么必须有这条：2026-09-13 真实事故 —— 为了让运镜收成受控词汇，
    #    我把 handheld 的规范名写成「手持跟拍」，结果它同时命中审计器的
    #    `handheld`(手持) 和 `track`(跟拍) 两条规则 → 63 个真实分镜里
    #    **硬伤从 2 个涨到 5 个**。两个模块各改各的，谁都没错，凑一起就错。
    #    这条测试把"两边必须对齐"变成机器可检查的约束。
    # ══════════════════════════════════════════════════════════════
    from core.videoprompt import CAMERA_MOVES, _CN_CAMERA_MOVES
    names = sorted(set(CAMERA_MOVES.values()) | {c for _, c in _CN_CAMERA_MOVES})
    # 这几个是"不产生运动的机位/角度"，审计器有意不计数，单列出来
    MOTIONLESS = {"俯拍", "仰拍", "横移", "固定机位"}
    bad_names, unnamed = [], []
    for n in names:
        probe = (f"画面中恰好 1 个主体：农夫 各出现一次。起始构图（景别）：中景。"
                 f"镜头运动：{n}（整个镜头只有这一个主运动）。"
                 f"画面内容：他蹲下身子，动作缓慢而小心。"
                 f"结束构图：落在「他蹲着」这个状态上，主体仍在画面内、构图稳定。"
                 f"总时长：5 秒。音频：这一镜没有对白（也没有旁白）；无配乐、无字幕。")
        r = pa.audit_video_prompt(probe, "无文字", duration=5.0)
        if [i for i in r["issues"] if i["code"] == "P-CAMERA-CONFLICT"]:
            bad_names.append(f"{n}（被判成多个运镜）")
        if n not in MOTIONLESS and not pa.camera_context(probe):
            unnamed.append(n)
    check(f"D1 videoprompt 的 {len(names)} 个运镜规范名，逐一放进提示词都只算一个运镜",
          not bad_names, "；".join(bad_names))
    check(f"D2 除「不产生运动的机位」({len(MOTIONLESS)} 个) 外，规范名都能被审计器认出来",
          not unnamed, "；".join(unnamed))

    # ══════════════════════════════════════════════════════════════
    # E. 运镜规整（`_norm_camera`）的**真实输入**对照。
    #    下面每一条都是从真实项目里抄出来的 `camera` 字段原文 ——
    #    它们曾经原样进提示词，于是提示词里出现两个主运动。
    # ══════════════════════════════════════════════════════════════
    from core.videoprompt import _norm_camera
    cases = [
        # (原始 camera 字段, 期望的规范名)  —— 期望值都必须是"单一运动"
        ("低角度跟随拍摄，镜头快速推进", "跟拍"),
        ("手持跟拍，快速推近", "手持"),
        ("远景镜头，镜头缓慢拉远", "缓慢拉远"),
        ("低角度拍摄，镜头缓慢推近、镜头拉远，模糊处理", "缓慢推近"),
        ("缓慢推近的特写镜头、平稳横移镜头、缓慢拉远的镜头", "缓慢推近"),
        ("全景俯拍，镜头缓慢平移", "俯拍"),
        ("镜头拉远，展现两人对峙的全景", "拉远"),
        ("从背后缓慢跟随布兰的轮椅移动，光线昏暗，凸显厅堂的残破", "跟拍"),
        ("dolly_in", "缓慢推近"),
        ("pan_left", "向左横摇"),
        ("固定机位", "固定机位"),
        ("拉远", "拉远"),
        # 纯景别/角度描述：不该凭空编出一个运动
        ("特写镜头捕捉表情变化", ""),
        ("广角镜头展示全场氛围", ""),
    ]
    wrong = [f"{src!r}→{_norm_camera(src)!r}(期望 {exp!r})"
             for src, exp in cases if _norm_camera(src) != exp]
    check(f"E1 {len(cases)} 条真实 camera 字段都被规整成单一运动",
          not wrong, "；".join(wrong))

    # 规整结果还要能通过审计器（否则等于把问题挪了个地方）
    still_conflict = []
    for src, _exp in cases:
        cam = _norm_camera(src)
        if not cam:
            continue
        probe = (f"画面中恰好 1 个主体：农夫 各出现一次。起始构图（景别）：中景。"
                 f"镜头运动：{cam}（整个镜头只有这一个主运动）。"
                 f"画面内容：他蹲下身子，动作缓慢而小心。"
                 f"结束构图：落在「他蹲着」这个状态上，主体仍在画面内、构图稳定。"
                 f"总时长：5 秒。音频：这一镜没有对白（也没有旁白）；无配乐、无字幕。")
        r = pa.audit_video_prompt(probe, "无文字", duration=5.0)
        if [i for i in r["issues"] if i["code"] == "P-CAMERA-CONFLICT"]:
            still_conflict.append(f"{src!r}→{cam}")
    check("E2 规整后的运镜名进提示词不再触发 P-CAMERA-CONFLICT",
          not still_conflict, "；".join(still_conflict))

    # ══════════════════════════════════════════════════════════════
    # G. 2026-09-13 从**真实素材**里挖出来的三类缺陷（这一类必须留回归，
    #    因为它们都是"不报错、只是提示词悄悄变坏"）。
    # ══════════════════════════════════════════════════════════════
    from core.videoprompt import _strip_camera_clauses, _fit_to_budget

    # G1 词表单一事实来源：同一个"剪运镜从句"函数必须认得改前认不出的那些词。
    #    改前 `_CAM_CLAUSE_RE` 不认「平移/横移/俯视」，于是
    #    「镜头缓慢从建筑右侧平移至正门」原样留在"画面内容"里 —— 实测 6 个
    #    真实分镜中招（都在 19636f68），提示词里因此有两个运镜。
    leak_cases = [
        "镜头缓慢从建筑右侧平移至正门，随后行人从镜头前经过",
        "镜头从正门缓慢平移到红砖墙，聚焦在墙面的纹理上",
        "镜头从高处俯视街道，展示行人穿梭的繁忙景象",
        "布兰坐在轮椅上，镜头从背后跟随，进入大厅",
    ]
    leak_bad = []
    for c in leak_cases:
        t, w = _strip_camera_clauses(c)
        if re.search(r"(?:镜头|摄影机)[^。；]{0,24}?"
                     r"(?:平移|横移|俯视|俯瞰|仰视|平视|跟随|环绕|推进|推近|拉远)",
                     t):
            leak_bad.append(c[:26])
    check(f"G1 运镜/视角从句一律剪得掉（{len(leak_cases)} 条真实原文）",
          not leak_bad, "；".join(leak_bad))
    # 负对照：没有运镜的正常动作**不许**被误剪
    clean = "艾莉亚站在码头，凝视着远方的船只，海风吹动她的斗篷"
    check("G2 负对照：不含运镜的动作原样保留",
          _strip_camera_clauses(clean)[0] == clean, _strip_camera_clauses(clean)[0][:40])

    # G3 预算装配：**契约尾段永远不许被砍掉**（改前是从尾巴硬截，
    #    实测把"总时长：20 秒。音频：…"整段砍掉 → 平台自动配乐加旁白）
    long_seg = [
        "画面中恰好 2 个主体：甲、乙 各出现一次",
        "起始构图（景别）：中景",
        "镜头运动：跟拍（**整个镜头只有这一个主运动**）",
        "人物（严格保持以下外形与服装，不要改变）：" + "甲的外形描述，" * 120,
        "环境：" + "大理石地面布满裂痕，" * 60,
        "画面内容：" + "他缓缓走进大厅，" * 50,
        "承接上一镜（画面姿态要衔接）：" + "上一镜结尾状态，" * 40,
        "为下一镜留出衔接：" + "下一镜将从此继续，" * 40,
        "结束构图：落在「他站在大厅中央」这个状态上",
        "总时长：20 秒",
        "音频：这一镜没有对白（也没有旁白）；无配乐、无字幕烧录",
    ]
    fitted, hard = _fit_to_budget(long_seg, 1500)
    check("G3 超预算时契约尾段（总时长/音频）仍在",
          "总时长" in fitted and "音频" in fitted and len(fitted) <= 1500,
          f"len={len(fitted)} hard={hard}")
    check("G4 超预算时优先削的是「为下一镜」这种可选段",
          "为下一镜" not in fitted, fitted[-60:])
    short_seg = ["画面内容：他蹲下", "总时长：5 秒", "音频：无对白；无配乐"]
    f2, h2 = _fit_to_budget(short_seg, 1500)
    check("G5 负对照：不超预算时一个字都不动",
          f2 == "。".join(short_seg) and h2 is False, f2)

    # G6 导演语检查：真实原文要报出来，正常动作不许误报
    meta_hit = pa.audit_video_prompt(
        GOOD.replace("他蹲下身子，双手捧起一条冻僵的蛇，动作缓慢而小心",
                     "展示四层楼整体结构，随后镜头从正门平移"),
        "无文字", duration=5.0)
    check("G6 「展示…」这类导演语会被提示（P-META-VERB）",
          "P-META-VERB" in codes(meta_hit),
          str(sorted(codes(meta_hit))))
    meta_ok = pa.audit_video_prompt(GOOD, "无文字", duration=5.0)
    check("G7 负对照：正常动作描写不误报 P-META-VERB",
          "P-META-VERB" not in codes(meta_ok), str(sorted(codes(meta_ok))))

    print("\n" + "=" * 74)
    print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
    for f in FAIL:
        print("  [x] " + f)
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
