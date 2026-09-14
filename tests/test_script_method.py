# -*- coding: utf-8 -*-
r"""剧本写法（向用户提供的**成品剧本**学习）的离线回归。

用户的要求（2026-09-14）：
    「剧本生成的话，之前我给过你直接的项目成品了。剧本上可以多向它学习，
      方法再融入到我们剧本生成里面」

那份成品就在本机（`%LOCALAPPDATA%\VideoForge\data\cache\imports\...`）：
**《40集农村微短剧剧本：寒潮抢收玉米（全集分集台词+镜头）》**（426KB PDF，
用户导入过 6 次，DB 里 outline 是它的全文抽取）。

本套件干三件事，全部扣着**真实物料**：
  ① 把成品剧本里的原文段落当**基准**：用 `scriptaudit` 量它，必须没有硬问题；
  ② 用一把"泛泛而谈的 AI 剧本"当**反例**：必须被指出问题（否则这把尺子没用）；
  ③ 校验**生成要求**（`llm.SCRIPT_GENERATION_SYSTEM`）里真的写了这些方法
     —— 方法不能只活在我的注释里，得进提示词、也得能被检验。

顺带把成品的**统计签名**也锁住（标题 3–14 字、每场 1–3 句台词、正文 15–140 字），
将来谁改宽了尺子，这里会红。
"""
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))

from core.scriptaudit import (                                   # noqa: E402
    audit_script, HOOK_TITLE_MIN, HOOK_TITLE_MAX, LINES_PER_SCENE_MAX,
    BODY_CHARS_MIN, BODY_CHARS_MAX,
)
from core.llm import SCRIPT_GENERATION_SYSTEM                     # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def scenes_of(audit):
    return {c["id"]: c for c in audit["checks"]}


# ──────────── 成品剧本的真实片段（逐字抄自导入的 PDF 全文）────────────
# 8 集覆盖了成品的全部关键手法：钩子标题 / 镜头说明 / 短台词 / 煽动 / 摇摆 /
# 前史揭示 / 金句收尾。
GOOD = {
    "title": "寒潮抢收玉米",
    "logline": "一场赌粮价的生死局：寒潮来临前，收粮贩子阿诚良心兜底，村头能人大嘴煽动全村囤粮。",
    "style": "cinematic",
    "characters": [
        {"name": "阿诚", "role": "protagonist",
         "description": "30 岁上下，返乡收粮贩子，皮肤黝黑，穿旧夹克",
         "motivation": "父亲当年惜售烂粮亏光，他看透行情风险，坚持良心高价兜底收玉米",
         "costume": "旧夹克、胶鞋"},
        {"name": "大嘴", "role": "antagonist",
         "description": "40 多岁，村头能人，爱煽动、赌行情、见不得别人赚钱",
         "motivation": "靠当众喊话、拦住卖粮的人、事后翻脸不认账来维持自己的面子",
         "costume": "皮夹克、金链子"},
        {"name": "老实叔", "role": "supporting",
         "description": "50 多岁，勤恳农户，胆小求稳",
         "motivation": "代表普通庄稼人，怕赌又怕亏", "costume": "旧棉袄"},
    ],
    "scenes": [
        {"scene_number": 1, "title": "寒潮预警，全村慌了", "hook": "天灾要来，全村先乱了阵脚",
         "location": "村口大喇叭 / 玉米地", "duration_seconds": 15,
         "characters": ["阿诚", "村民"],
         "actions": ["村口大喇叭，阴天冷风，玉米地青黄未干透"],
         "dialogues": [{"character": "阿诚", "text": "各位村民注意！后天大寒潮、连夜霜冻！地里嫩玉米全部会冻坏烂芯！",
                        "emotion": "urgent", "delivery": "喇叭喊话"}],
         "subtitle": "一场赌粮价的生死局，即将开始", "mood": "tense"},
        {"scene_number": 2, "title": "高价开收，全村围观", "hook": "有人真敢现钱收粮",
         "location": "玉米地地头", "duration_seconds": 15,
         "characters": ["阿诚", "老实叔", "村民"],
         "actions": ["三轮车停地头，阿诚摆秤、装袋"],
         "dialogues": [
             {"character": "阿诚", "text": "趁现在没降温，我一块二一斤现结收湿玉米，不扣杂、不压水，给钱就拉！",
              "emotion": "steady"},
             {"character": "老实叔", "text": "这价可以！我先卖！", "emotion": "eager"}],
         "mood": "hopeful"},
        {"scene_number": 3, "title": "有人带头拦着不卖", "hook": "第一次正面冲突",
         "location": "玉米地地头", "duration_seconds": 15,
         "characters": ["大嘴", "老实叔"],
         "actions": ["大嘴快步上前拦住老实叔"],
         "dialogues": [
             {"character": "大嘴", "text": "你急什么？寒潮一来玉米减产，过两天绝对涨价！现在卖纯纯亏！",
              "emotion": "sly"},
             {"character": "老实叔", "text": "可天气预报说连夜霜冻啊！", "emotion": "worried"}],
         "mood": "tense"},
        {"scene_number": 4, "title": "煽动全村囤粮", "hook": "群众被带着走",
         "location": "村口空地", "duration_seconds": 15,
         "characters": ["大嘴", "村民"],
         "actions": ["围观村民聚拢，议论纷纷"],
         "dialogues": [
             {"character": "大嘴", "text": "都听我的！稳住别卖！往年降温都涨价，今年肯定翻倍！谁卖谁傻子！",
              "emotion": "loud", "delivery": "高声煽动"},
             {"character": "村民", "text": "对，再等等！", "emotion": "swayed"}],
         "mood": "chaotic"},
        {"scene_number": 5, "title": "爆出父辈旧伤疤", "hook": "主角的伤口被揭开",
         "location": "玉米地边", "duration_seconds": 15,
         "characters": ["阿诚", "村民"],
         "actions": ["阿诚眼神落寞，看向玉米地"],
         "dialogues": [{"character": "阿诚", "text": "你们只知道赌涨价，可谁还记得十年前那场寒潮？",
                        "emotion": "bitter"}],
         "mood": "somber"},
        {"scene_number": 6, "title": "十年前那场惨案", "hook": "他为什么不敢赌",
         "location": "玉米地（闪回空镜）", "duration_seconds": 15,
         "characters": ["阿诚"],
         "actions": ["快速闪回旧空镜：冻坏发黑的玉米、烂在地里的庄稼"],
         "dialogues": [
             {"character": "阿诚", "text": "我爹当年和你们一样，死守不卖，赌行情翻倍。", "emotion": "heavy"},
             {"character": "阿诚", "text": "结果寒潮一夜冻透，玉米芯烂、籽粒发僵！", "emotion": "heavy"}],
         "mood": "grief"},
        {"scene_number": 7, "title": "全村后悔莫及", "hook": "群众开始动摇",
         "location": "村口", "duration_seconds": 15,
         "characters": ["老实叔", "村民"],
         "actions": ["村民全员懊悔低头，小声议论、互相埋怨"],
         "dialogues": [{"character": "老实叔", "text": "早听阿诚的话，何至于亏成这样！", "emotion": "regret"}],
         "mood": "regretful"},
        {"scene_number": 8, "title": "大结局：血汗不赌命", "hook": "金句收束全片",
         "location": "夕阳下的玉米地", "duration_seconds": 15,
         "characters": ["阿诚"],
         "actions": ["夕阳洒遍玉米地，温暖治愈"],
         "dialogues": [{"character": "阿诚",
                        "text": "外行赌涨跌，内行懂风险。种地如此，人生亦然。",
                        "emotion": "calm", "delivery": "终极旁白"}],
         "subtitle": "不贪即是赚，止损方为赢", "mood": "warm"},
    ],
}


def main():
    print("=" * 70)
    print("剧本写法（向成品剧本学习）离线回归")
    print("=" * 70)

    # ── T1 成品剧本当基准：必须没有硬问题 ──
    a = audit_script(GOOD, 120)
    by = scenes_of(a)
    check("T1.1 成品写法 → 0 个硬问题（valid_for_review）",
          a["errors"] == 0 and a["valid_for_review"], f"errors={a['errors']}")
    check("T1.2 时长核对通过（8 场 × 15s = 120s）",
          by.get("S3", {}).get("level") == "ok", str(by.get("S3", {}).get("title")))
    check("T1.3 每场都有画面/动作（S1 不报）", "S1" not in by)
    check("T1.4 每场都有可听内容（S2 不报）", "S2" not in by)
    check("T1.5 台词句数在成品区间内（S4 不报）", "S4" not in by)
    check("T1.6 标题都是短钩子（S7 不报）", "S7" not in by)
    check("T1.7 有前史动机（S8 不报）", "S8" not in by)
    check("T1.8 反派写了行为方式（S9 不报）", "S9" not in by)
    check("T1.9 有群像摇摆（S10 不报）", "S10" not in by)
    check("T1.10 结尾金句点题（S11 不报）", "S11" not in by)
    check("T1.11 分数在可用区间（>=90）", a["score"] >= 90, str(a["score"]))
    check("T1.12 统计里能看出台词密度（每场 1–2 句）",
          1.0 <= a["stats"].get("lines_avg", 0) <= 2.5, str(a["stats"].get("lines_avg")))

    # ── T2 尺子本身要跟真实物料对得上 ──
    titles = [len(re.sub(r"\s", "", s["title"])) for s in GOOD["scenes"]]
    bodies = [len(re.sub(r"\s", "", s["title"] + s["location"]
                         + "".join(s.get("actions") or [])
                         + "".join(d["text"] for d in s.get("dialogues") or [])
                         + str(s.get("subtitle") or ""))) for s in GOOD["scenes"]]
    check("T2.1 所有标题落在体检允许的字数区间里",
          all(HOOK_TITLE_MIN <= t <= HOOK_TITLE_MAX for t in titles), str(titles))
    check("T2.2 所有场次正文落在体检允许的字数区间里",
          all(BODY_CHARS_MIN <= b <= BODY_CHARS_MAX for b in bodies), str(bodies))
    check("T2.3 每场台词不超过成品上限",
          all(len(s.get("dialogues") or []) <= LINES_PER_SCENE_MAX for s in GOOD["scenes"]))

    # ── T3 反例：泛泛而谈的 AI 剧本必须被挑出来 ──
    BAD = {
        "title": "无题",
        "logline": "一个关于成长的故事。",
        "scenes": [
            {"scene_number": 1, "title": "第一场开场介绍背景和人物关系以及时代氛围",
             "location": "", "duration_seconds": 10,
             "actions": [], "dialogues": []},
        ],
        "characters": [],
    }
    b = audit_script(BAD, 10)
    bad_ids = {c["id"] for c in b["checks"] if c["level"] in ("error", "warn")}
    check("T3.1 没有画面/动作 → 报硬问题 S1", "S1" in bad_ids, str(sorted(bad_ids)))
    check("T3.2 没有台词也没有字幕 → 报硬问题 S2", "S2" in bad_ids)
    check("T3.3 标题不是钩子 → 报建议 S7", "S7" in bad_ids)
    check("T3.4 没有前史动机 → 报建议 S8", "S8" in bad_ids)
    check("T3.5 反例不可放行（valid_for_review=False）", b["valid_for_review"] is False)
    check("T3.6 反例分数明显低于成品", b["score"] < a["score"] - 20,
          f"{b['score']} vs {a['score']}")

    # ── T4 时长不对必须报硬问题（这是用户报的那个 bug 的体检版）──
    c = audit_script(GOOD, 5)
    check("T4.1 目标 5 秒但剧本 120 秒 → 报硬问题 S3",
          scenes_of(c).get("S3", {}).get("level") == "error",
          str(scenes_of(c).get("S3", {}).get("detail")))
    check("T4.2 时长不符时不可放行", c["valid_for_review"] is False)

    # ── T5 生成要求里**真的**写了这些方法 ──
    sysmsg = SCRIPT_GENERATION_SYSTEM
    must = {
        "钩子标题": "钩子",
        "镜头说明": "镜头",
        "前史动机": "前史动机",
        "反派行为方式": "行为方式",
        "群像摇摆": "群像",
        "金句收尾": "金句",
        "4 字/秒": "4 字/秒",
        "成品出处": "寒潮抢收玉米",
        "新字段 subtitle": "subtitle",
        "新字段 motivation": "motivation",
        "新字段 role": "role",
    }
    missing = [k for k, v in must.items() if v not in sysmsg]
    check("T5.1 生成要求里写全了成品的方法（不是只写在注释里）", not missing, str(missing))
    check("T5.2 生成要求里明确要求标题是 4-10 字", "4–10 字" in sysmsg or "4-10 字" in sysmsg)

    # ── T6 空输入 ──
    e = audit_script(None, None)
    check("T6.1 空剧本不炸且报硬问题", e["errors"] >= 1 and "S0" in scenes_of(e))
    check("T6.2 outline 是 JSON 字符串时也能吃", audit_script(
        {"outline": '{"scenes":[{"scene_number":1,"title":"测试钩子","location":"x",'
                    '"duration_seconds":5,"actions":["a"],'
                    '"dialogues":[{"character":"A","text":"话"}]}]}'}, 5)["errors"] == 0)

    print("\n" + "=" * 70)
    print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
    for f in FAIL:
        print("  [x] " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
