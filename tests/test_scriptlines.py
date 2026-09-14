# -*- coding: utf-8 -*-
r"""剧本台词回填（`core.scriptlines`）的离线回归。

为什么值得为它写一整套测试（2026-09-14 用户实测反馈）：
    「我点击后还是单独的那个AI女音，**且第一个镜头没有声音（剧本是有对话的）**」

查真实数据（caa9904f「权力的游戏最终版」）后确认：第一镜静音**不是**合成失败，
而是 AI 写分镜时**漏了 `dialogue` 字段** —— shot#1 的三段只有
action / expression / camera，而剧本那一场（废墟中的铁王座：布兰 / 无面者刺客）
写着两句对话。没台词可念，于是静音。

同一个项目的坑还有第二个：**剧本被改过一稿** ——
`script_scene_details` 当前是「铁王座前的对峙：琼恩 / 瑟曦 / 丹妮莉丝」，
而分镜是旧稿（布兰线）生成的。所以"按场号把剧本台词塞进去"是**错的**：
那会把别人的台词配到布兰的画面上（参考库里 `F-DIALOGUE-VISUALIZED` 那类失败）。

这里锁死的三件事：
  ① 说话人**确实在这一镜里**才回填（镜头文本提到 / 在镜头角色表里），
     否则**一句都不填**并把理由如实带回；
  ② 只填**没有台词**的时间轴段，绝不覆盖已有台词；
  ③ 回填后的时间轴能被 `core.dialogue.timeline_lines` 解析出**带说话人**的台词
     （否则配音还是会落到默认音色 = 用户听到的"一把 AI 女音"）。
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))

from core.scriptlines import (                                  # noqa: E402
    collect_scene_sources, recover_shot_lines, inject_lines, recover_and_inject,
    shot_text, _mentions,
)
from core.dialogue import timeline_lines                        # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


# ──────────── 真实项目的形状（照 caa9904f 抄下来）────────────

# 旧稿（生成分镜时用的那版）：布兰 / 无面者 —— 和分镜画面一致
SCRIPT = {
    "scenes": [
        {"scene_number": 1, "title": "废墟中的铁王座",
         "location": "君临城 - 铁王座大厅",
         "characters": ["布兰·史塔克", "无面者刺客"],
         "actions": ["布兰坐在轮椅上，缓缓进入大厅。"],
         "dialogues": [
             {"character": "布兰", "text": "铁王座属于过去，新的时代即将到来。", "emotion": "calm"},
             {"character": "无面者", "text": "谁来统治这片土地？", "emotion": "curious"},
         ]},
        {"scene_number": 2, "title": "珊莎的决策",
         "location": "临冬城 - 大厅",
         "characters": ["珊莎·史塔克", "北方领主们"],
         "dialogues": [{"character": "珊莎", "text": "北境将独立。", "emotion": "determined"}]},
    ]
}

# 当前工作台稿：完全不同的一条线（琼恩 / 瑟曦 / 丹妮莉丝）
DETAILS = [
    {"scene_number": "1", "detail": {
        "scene_number": 1, "title": "铁王座前的对峙",
        "location": "君临城，铁王座大厅",
        "characters": ["琼恩·雪诺", "丹妮莉丝·坦格利安", "瑟曦·兰尼斯特"],
        "dialogues": [
            {"character": "琼恩", "text": "这一切该结束了，瑟曦。", "emotion": "determined", "timing": 2},
            {"character": "瑟曦", "text": "你真的以为你能夺走我的王座？", "emotion": "mocking", "timing": 7},
        ]}},
]

# shot#1：时间轴**没有** dialogue 字段，但画面/角色表里是布兰与无面者
SHOT = {
    "order_index": 0,
    "layer1_overview": "君临城铁王座大厅内，一片破败景象。布兰·史塔克坐在轮椅上，缓缓进入大厅。"
                       "无面者刺客站在铁王座旁，眼神警惕而好奇。",
    "layer2_timeline": [
        {"start": 0, "end": 4,
         "action": "布兰坐在轮椅上，缓缓进入铁王座大厅，镜头从背后跟随。",
         "expression": "沉静", "camera": "从背后缓慢跟随"},
        {"start": 4, "end": 8,
         "action": "无面者刺客站在铁王座旁，目光锐利地注视着布兰。",
         "expression": "警惕", "camera": "侧面镜头，缓慢推近"},
        {"start": 8, "end": 12,
         "action": "镜头环绕铁王座，展示厅堂的残破景象。",
         "expression": "坚定", "camera": "环绕镜头"},
    ],
    "layer3_constraints": {"must_appear": ["铁王座", "布兰的轮椅", "无面者"],
                           "must_keep": ["无面者的黑色斗篷"]},
}


def main():
    print("=" * 70)
    print("剧本台词回填（core.scriptlines）离线回归")
    print("=" * 70)

    # ── T1 收集剧本台词来源 ──
    srcs = collect_scene_sources(SCRIPT, DETAILS)
    check("T1.1 两版剧本里有台词的场都收进来了", len(srcs) == 3, f"got {len(srcs)}")
    check("T1.2 来源名区分得开（剧本场景 / 剧本细纲）",
          {s["source"] for s in srcs} == {"剧本场景", "剧本细纲"},
          str({s["source"] for s in srcs}))
    check("T1.3 没有台词的场不进候选",
          all(s["dialogues"] for s in srcs))

    # ── T2 真实场景：分镜是旧稿、剧本有两版 → 只填对得上的那版 ──
    rec = recover_shot_lines(SHOT, srcs, scene_name="君临城 - 铁王座大厅",
                             character_names=["布兰·史塔克", "无面者刺客"],
                             order_index=0)
    got = [d["text"] for d in rec["lines"]]
    check("T2.1 挑中了与画面一致的旧稿（剧本场景）", rec["source"] == "剧本场景", rec["source"])
    check("T2.2 布兰和无面者的两句都拿到了", len(got) == 2, str(got))
    check("T2.3 挑中的是布兰那句", "铁王座属于过去" in " ".join(got), str(got))

    # ── T3 只有"当前稿"时**一句都不填**（宁缺勿错）──
    only_new = collect_scene_sources(None, DETAILS)
    rec3 = recover_shot_lines(SHOT, only_new, scene_name="君临城 - 铁王座大厅",
                              character_names=["布兰·史塔克", "无面者刺客"], order_index=0)
    check("T3.1 说话人不在这一镜里 → 不填", rec3["lines"] == [], str(rec3["lines"]))
    check("T3.2 理由如实带回（拒绝硬塞）",
          "不硬塞" in (rec3.get("reason") or ""), rec3.get("reason"))
    check("T3.3 被跳过的台词也列出来了",
          any("琼恩" in (s.get("character") or "") for s in rec3.get("skipped") or []),
          str(rec3.get("skipped"))[:120])

    # ── T4 注入：只填空段、不覆盖已有台词 ──
    inj = inject_lines(SHOT["layer2_timeline"], rec["lines"])
    check("T4.1 两句都填进了空段", inj["added"] == 2, str(inj["added"]))
    check("T4.2 填的段确实变了、其他段没动",
          inj["timeline"][0]["dialogue"]["character"] == "布兰"
          and inj["timeline"][2].get("dialogue") is None,
          str([bool(s.get("dialogue")) for s in inj["timeline"]]))
    check("T4.3 原时间轴没有被就地改写",
          SHOT["layer2_timeline"][0].get("dialogue") is None)
    full = [dict(s, dialogue={"character": "甲", "text": "已有", "emotion": ""})
            for s in SHOT["layer2_timeline"]]
    inj4 = inject_lines(full, rec["lines"])
    check("T4.4 每段都有台词时一个都不覆盖", inj4["added"] == 0, str(inj4["added"]))

    # ── T5 回填后的时间轴必须能被配音链路读出"谁说的" ──
    lines = timeline_lines(inj["timeline"], 12.0)
    check("T5.1 配音链路读得出 2 句", len(lines) == 2, str(len(lines)))
    check("T5.2 每句都带说话人（否则又会落到默认音色）",
          [x["character"] for x in lines] == ["布兰", "无面者"],
          str([x["character"] for x in lines]))
    check("T5.3 情绪也带过去了",
          lines[0]["emotion"] == "calm", lines[0]["emotion"])

    # ── T6 名字归一化：布兰 ↔ 布兰·史塔克 ──
    check("T6.1 简称/全称互相认得出", _mentions("布兰·史塔克坐在轮椅上", "布兰"))
    check("T6.2 单字不误命中", not _mentions("他在看海", "水"))
    shot_no_name = dict(SHOT, layer1_overview="大厅空无一人。",
                        layer2_timeline=[{"start": 0, "end": 4, "action": "海面波光粼粼。"}],
                        layer3_constraints={})
    src_sailor = [{"source": "剧本场景", "scene_number": 3, "title": "艾莉亚的抉择",
                   "location": "维斯特洛海岸 - 码头",
                   "characters": ["艾莉亚·史塔克", "水手"],
                   "dialogues": [{"character": "水手", "text": "你真的决定离开？",
                                  "emotion": "concerned"}]}]
    rec6b = recover_shot_lines(shot_no_name, src_sailor, character_names=["水手"])
    check("T6.3 画面没提、但镜头角色表里有 → 也算在场",
          len(rec6b["lines"]) == 1 and rec6b["lines"][0]["character"] == "水手",
          str([d.get("character") for d in rec6b["lines"]]))
    rec6c = recover_shot_lines(shot_no_name, src_sailor, character_names=[])
    check("T6.4 画面和角色表里都没有 → 一句都不填（这是本轮最重要的一条）",
          rec6c["lines"] == [], str(rec6c.get("reason")))

    # ── T7 空输入不炸 ──
    check("T7.1 没有剧本来源时如实返回原因、不抛异常",
          recover_shot_lines(SHOT, [])["lines"] == [])
    check("T7.2 时间轴为空时注入不炸", inject_lines([], rec["lines"])["added"] == 0)
    check("T7.3 台词为空时注入不炸", inject_lines(SHOT["layer2_timeline"], [])["added"] == 0)

    # ── T8 一步到位接口 ──
    r8 = recover_and_inject(SHOT, srcs, scene_name="君临城 - 铁王座大厅",
                            character_names=["布兰·史塔克", "无面者刺客"], order_index=0)
    check("T8.1 一步到位填了 2 句并带回来源",
          r8["added"] == 2 and r8["source"] == "剧本场景", str(r8.get("added")))
    check("T8.2 shot_text 抓到了画面里的名字",
          "布兰" in shot_text(SHOT) and "无面者" in shot_text(SHOT))

    # ── T9 已有台词的镜头：只"补缺"，绝不"重念" ──
    #   真实形状（caa9904f 镜头2）：[0-4]有珊莎的台词、[4-8]空、[8-12]有领主A 的台词。
    #   剧本那一场同样是这两句。旧逻辑看到"有一个空段"就把剧本第一句又填了一次
    #   → 这一镜把同一句话念两遍，而该补的位置反而空着（干跑实测）。
    partial = [
        {"start": 0, "end": 4, "action": "珊莎站在领主们面前。",
         "dialogue": {"character": "珊莎", "text": "北境将独立，但我们会与新王合作。",
                      "emotion": "determined"}},
        {"start": 4, "end": 8, "action": "领主们低声讨论。", "dialogue": None},
        {"start": 8, "end": 12, "action": "珊莎举手示意安静。",
         "dialogue": {"character": "领主A", "text": "我们如何相信他？",
                      "emotion": "skeptical"}},
    ]
    shot2 = {"order_index": 1, "layer1_overview": "临冬城大厅里，珊莎与北方领主们。",
             "layer2_timeline": partial,
             "layer3_constraints": {"must_appear": ["临冬城大厅", "北方领主们"]}}
    src2 = [{"source": "剧本场景", "scene_number": 2, "title": "珊莎的决策",
             "location": "临冬城 - 大厅",
             "characters": ["珊莎·史塔克", "北方领主们"],
             "dialogues": [
                 {"character": "珊莎", "text": "北境将独立，但我们会与新王合作。",
                  "emotion": "determined"},
                 {"character": "领主A", "text": "我们如何相信他？", "emotion": "skeptical"},
             ]}]
    r9 = recover_and_inject(shot2, src2, scene_name="临冬城大厅",
                            character_names=["珊莎·史塔克", "北方领主们"], order_index=1)
    check("T9.1 已经说过的两句都不会被重复填进去", r9["added"] == 0, str(r9["added"]))
    check("T9.2 拒绝理由写清了是已经有了",
          any("已经有这句台词" in (s.get("reason") or "")
              or "已经在说话了" in (s.get("reason") or "")
              for s in r9.get("skipped") or []),
          str(r9.get("skipped"))[:140])
    lines9 = timeline_lines(r9["timeline"], 12.0)
    check("T9.3 这一镜仍然只有 2 句（没有变成 3 句）", len(lines9) == 2, str(len(lines9)))

    # 只缺一个人时：补那个人，不重复另一个。
    #   注意证据规则：要让「领主A」被回填，这一镜的**画面文字**里必须真有他
    #   （这里 seg3 的 action 写了他）—— 否则就是"没证据要不要硬塞"，
    #   那是下一条 T9.6 的拒绝场景。
    partial2 = [dict(partial[0]),
                {"start": 4, "end": 8, "action": "北方领主们互相低声讨论。", "dialogue": None},
                {"start": 8, "end": 12, "action": "领主A 站起来，当面质疑珊莎的决定。",
                 "dialogue": None}]
    shot2b = dict(shot2, layer2_timeline=partial2)
    r9b = recover_and_inject(shot2b, src2, scene_name="临冬城大厅",
                             character_names=["珊莎·史塔克", "北方领主们"], order_index=1)
    got9b = [p["character"] for p in r9b.get("placed") or []]
    check("T9.4 只补缺的那个人（领主A），不重复已有的珊莎那句",
          got9b == ["领主A"], str(got9b))
    lines9b = timeline_lines(r9b["timeline"], 12.0)
    check("T9.5 补完之后正好 2 句、说话人各不相同",
          sorted(x["character"] for x in lines9b) == sorted(["珊莎", "领主A"]),
          str([x["character"] for x in lines9b]))

    # T9.6 连"他在场"的证据都没有 → 一句都不填（宁可这段空着）
    partial3 = [dict(partial[0]),
                {"start": 4, "end": 8, "action": "北方领主们互相低声讨论。", "dialogue": None},
                {"start": 8, "end": 12, "action": "珊莎举手示意安静。", "dialogue": None}]
    shot2c = dict(shot2, layer2_timeline=partial3)
    r9c = recover_and_inject(shot2c, src2, scene_name="临冬城大厅",
                             character_names=["珊莎·史塔克", "北方领主们"], order_index=1)
    check("T9.6 画面里没有任何领主A 的证据 → 不填他（不硬塞）",
          r9c["added"] == 0
          and any("领主A" == (s.get("character") or "") for s in r9c.get("skipped") or []),
          str(r9c.get("placed")) + " / " + str(r9c.get("skipped"))[:90])

    print("\n" + "=" * 70)
    print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
    for f in FAIL:
        print("  [x] " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
