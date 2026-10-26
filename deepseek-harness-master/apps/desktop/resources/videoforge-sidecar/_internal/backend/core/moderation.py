"""
VideoForge · 出片前的内容风险预检

════════════════════════════════════════════════════════════════════════
为什么要做这个（有实测代价）
════════════════════════════════════════════════════════════════════════
真跑时被平台拦过一次：

    OutputVideoSensitiveContentDetected.PolicyViolation
    The request failed because the output video may be related to
    copyright restrictions.

关键在于：**它跑了 93 秒才拒，而且照样计费** —— 模型已经出完片了，
审核是在输出阶段做的。也就是说这一次的钱完全白花。

同类失败还有输入阶段的（参考图里有人脸/侵权元素）和平台各自的审核码
（`IPInfringementSuspect` / `DataInspectionFailed` 等）。

**能省钱的唯一办法是在提交之前先看一眼。** 这个模块就干这个。

════════════════════════════════════════════════════════════════════════
设计原则（很重要，别改成硬拦截）
════════════════════════════════════════════════════════════════════════
1. **给建议，不给否决权。**
   风险词表永远是启发式的：`哈利波特` 不一定侵权（用户可能在做同人研究），
   `复仇者` 也可能只是普通词。硬拦会让正常创作卡住 —— 那比偶尔白花一次钱更糟。
   所以 `check()` 只产出"风险等级 + 理由"，具体做不做由用户说了算。

2. **只在"高风险"时要求用户确认一次**，不是每次都问。
   每次都弹确认，用户三天就会把确认点成肌肉记忆，等于没做。

3. **词表要能改。** 新的 IP 天天有，写死在代码里必然过期。
   支持从 `data/ip_blocklist.txt` 追加（一行一个词，`#` 开头是注释）。

4. **平台的报错码要能反查。**
   真被拒了之后，把平台的原文翻译成"为什么被拒 + 能怎么改"，
   而不是丢一段英文 JSON 给用户。见 `explain_platform_error()`。
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("videoforge.moderation")

# ── 风险等级 ──
LEVEL_OK = "ok"            # 没发现问题
LEVEL_WATCH = "watch"      # 有几个词值得留意，但可以先试
LEVEL_HIGH = "high"        # 很可能被拦，建议先改

# ── 高风险模式：明确的影视/动漫 IP 名 ──
# ★ 只放**辨识度很高、几乎必然是 IP 指代**的词。
#   不放"龙""魔法""侦探"这种普通词 —— 那会让所有人都被拦。
IP_TERMS = [
    # 影视
    "权力的游戏", "冰与火之歌", "绝命毒师", "行尸走肉", "老友记", "纸牌屋",
    "哈利波特", "哈利·波特", "指环王", "魔戒", "霍比特人", "加勒比海盗",
    "星球大战", "星际迷航", "复仇者联盟", "钢铁侠", "蜘蛛侠", "蝙蝠侠",
    "超人", "正义联盟", "X战警", "金刚狼", "死侍", "黑豹", "雷神",
    "侏罗纪公园", "阿凡达", "泰坦尼克号", "盗梦空间", "黑客帝国",
    "老友记", "生活大爆炸", "怪奇物语", "王冠", "西部世界",
    "甄嬛传", "琅琊榜", "三体", "流浪地球", "哪吒", "大圣归来", "姜子牙",
    "熊出没", "喜羊羊", "熊大", "光头强", "奥特曼", "迪迦",
    # 动漫
    "火影忍者", "海贼王", "航海王", "死神", "进击的巨人", "鬼灭之刃",
    "咒术回战", "名侦探柯南", "哆啦A梦", "机器猫", "龙珠", "七龙珠",
    "灌篮高手", "网球王子", "美少女战士", "新世纪福音战士", "EVA",
    "千与千寻", "龙猫", "天空之城", "你的名字",
    "原神", "崩坏", "明日方舟", "阴阳师", "王者荣耀", "英雄联盟",
    "塞尔达", "马里奥", "宝可梦", "神奇宝贝", "精灵宝可梦", "皮卡丘",
    # 游戏/文学
    "魔兽世界", "暗黑破坏神", "最终幻想", "生化危机", "刺客信条",
    "赛博朋克2077", "巫师3", "艾尔登法环", "黑神话",
    "西游记", "水浒传", "三国演义", "红楼梦",
]

# 真实公众人物（肖像权/名誉权风险）
REAL_PERSON_HINTS = [
    "习近平", "特朗普", "拜登", "普京", "马斯克", "比尔盖茨", "乔布斯",
    "马云", "马化腾", "雷军", "刘德华", "成龙", "周杰伦", "邓紫棋",
    "泰勒·斯威夫特", "霉霉", "C罗", "梅西", "詹姆斯", "库里",
]

# 品牌/商标（出现在画面里容易被判侵权）
BRAND_TERMS = [
    "可口可乐", "百事可乐", "麦当劳", "肯德基", "星巴克", "耐克", "阿迪达斯",
    "苹果手机", "iPhone", "华为手机", "小米手机", "特斯拉", "奔驰", "宝马",
    "法拉利", "兰博基尼", "路易威登", "LV", "古驰", "香奈儿", "爱马仕",
    "迪士尼", "漫威", "DC", "皮克斯", "梦工厂", "吉卜力",
]

# 平台明确会拦的敏感内容（暴力/色情/政治/违法）
SENSITIVE_TERMS = [
    "色情", "裸露", "裸体", "情色", "成人内容",
    "血腥", "肢解", "虐杀", "自杀", "自残",
    "毒品", "冰毒", "海洛因", "制毒", "吸毒",
    "枪支制造", "炸弹制作", "爆炸物配方",
    "赌博", "洗钱", "诈骗教程",
]

# 明确的活人肖像指向（配合"参考图"一起判断更准）
PHOTO_REAL_HINT = ["真人照片", "明星照片", "本人照片", "真人肖像", "身份证"]


def _load_extra_terms() -> List[str]:
    """从 `data/ip_blocklist.txt` 读用户自定义的词（一行一个）。

    ★ 词表写死在代码里必然过期 —— 新 IP 天天有。给用户一个不用改代码的入口。
    """
    out: List[str] = []
    for base in (os.environ.get("VIDEOFORGE_DATA_DIR"),
                 os.path.join(os.getcwd(), "data"),
                 os.path.join(os.path.dirname(__file__), "..", "..", "data")):
        if not base:
            continue
        p = Path(base) / "ip_blocklist.txt"
        try:
            if p.exists():
                for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
                    ln = ln.strip()
                    if ln and not ln.startswith("#"):
                        out.append(ln)
                break
        except Exception:
            continue
    return out


def check(
    *,
    prompt: str = "",
    negative_prompt: str = "",
    reference_images: Optional[List[str]] = None,
    character_names: Optional[List[str]] = None,
    scene_names: Optional[List[str]] = None,
    extra_terms: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """出片前扫一遍内容风险。

    Returns:
        {
          "level": "ok" | "watch" | "high",
          "hits": [{"term","kind","where","why"}],
          "advice": [str],          # 可执行的修改建议
          "need_confirm": bool,     # 是否该让用户明确点头一次
          "summary": str            # 一句话给用户看
        }

    **它不拦人**，只产出结论。是否继续由调用方/用户决定。
    """
    text = " ".join([str(prompt or ""), str(negative_prompt or ""),
                     " ".join(character_names or []),
                     " ".join(scene_names or [])])
    low = text.lower()
    hits: List[Dict[str, Any]] = []

    # 用户自定义词优先（说明他最在意这些）
    terms = _load_extra_terms() + list(extra_terms or [])

    def scan(words: List[str], kind: str, why: str) -> None:
        for w in words:
            if not w:
                continue
            # 用子串匹配：中文没有词边界，子串更实用
            if w.lower() in low:
                if not any(h["term"] == w for h in hits):
                    hits.append({"term": w, "kind": kind, "where": "prompt", "why": why})

    scan(terms, "custom", "你在自定义风险词表里列了这个词")
    scan(IP_TERMS, "ip", "这是明确的影视/动漫/游戏 IP 名，平台会判版权风险")
    scan(REAL_PERSON_HINTS, "person", "真实公众人物，有肖像权/名誉权风险")
    scan(BRAND_TERMS, "brand", "商标/品牌，出现在画面里容易被判侵权")
    scan(SENSITIVE_TERMS, "sensitive", "平台明确会拦的敏感内容")
    scan(PHOTO_REAL_HINT, "photo", "真人肖像指向，输入审核可能直接拒")

    # 参考图里有真人照片 → 额外提示（这也是实测被拒的一种）
    refs = [r for r in (reference_images or []) if r]
    if refs and any(h["kind"] == "person" for h in hits):
        hits.append({"term": "(参考图)", "kind": "ref_person",
                     "where": "reference_images",
                     "why": "提示词里有真实公众人物，同时带了参考图 —— "
                            "平台对「图+人名」的组合更容易判侵权"})

    # ── 定级 ──
    kinds = {h["kind"] for h in hits}
    if kinds & {"ip", "person", "sensitive", "ref_person", "custom"}:
        level = LEVEL_HIGH
    elif kinds & {"brand", "photo"}:
        level = LEVEL_WATCH
    elif hits:
        level = LEVEL_WATCH
    else:
        level = LEVEL_OK

    # ── 建议（要具体，不能只说"有风险"）──
    advice: List[str] = []
    if "ip" in kinds:
        advice.append(
            "**换掉 IP 专属名**：不要写「权力的游戏」「哈利波特」这类专名，"
            "改写成你要的那种**风格或设定**（例如「中世纪奇幻王国、贵族家族斗争、"
            "北境雪原」），角色也换成你自己起的名字。"
            "平台判的是「输出像不像那个 IP」，风格化描写能过，专名很难过。")
    if "person" in kinds:
        advice.append(
            "**别用真实人物**：换成虚构人物（改名字、改外貌特征），"
            "或者只描述「气质/职业/年龄」，不指向具体某个人。")
    if "sensitive" in kinds:
        advice.append("**敏感内容**：这类词平台几乎必拦，建议整个镜头换方向。")
    if "brand" in kinds:
        advice.append(
            "**品牌/商标**：把 logo 和品牌名去掉，改成泛化描述"
            "（「一个红色汽水罐」而不是「可口可乐」）。")
    if "photo" in kinds:
        advice.append("**真人肖像**：参考图换成 AI 生成的虚构人物图。")
    if "custom" in kinds:
        advice.append("这些是**你自己**列进风险词表的词 —— 按你的判断处理。")

    if level == LEVEL_OK:
        summary = "内容预检没发现问题。"
    elif level == LEVEL_WATCH:
        summary = (f"内容预检发现 {len(hits)} 处值得留意"
                   f"（大概率能过，但可能被拦）。")
    else:
        summary = (f"⚠ 内容预检发现 {len(hits)} 处**高风险** —— "
                   f"这类提示词容易被平台拦，而且**被拦也会计费**"
                   f"（实测跑完 93 秒才拒，钱照扣）。建议先按下面的建议改一版。")

    return {
        "level": level,
        "hits": hits,
        "advice": advice,
        "need_confirm": level == LEVEL_HIGH,
        "summary": summary,
    }


# 平台错误码 → 人话 + 可执行建议
ERROR_PLAYBOOK: List[Dict[str, Any]] = [
    {
        "match": ["OutputVideoSensitiveContentDetected", "copyright restrictions",
                  "PolicyViolation"],
        "title": "输出视频被判版权/内容风险",
        "why": "模型已经出片了，但平台审核认为成品可能涉及版权。"
               "**这类失败照样计费**（实测跑了 93 秒才拒）。",
        "fix": ["把提示词里的 IP/品牌专名换成风格化描述",
                "角色换成自己起的名字、改掉标志性外观",
                "参考图换成不含 IP 形象的图（参考图会把 IP 特征带进结果）"],
    },
    {
        "match": ["IPInfringementSuspect"],
        "title": "疑似侵犯知识产权",
        "why": "平台的输入或输出审核命中侵权特征。",
        "fix": ["去掉 IP 专名与标志性造型", "参考图换成原创素材"],
    },
    {
        "match": ["DataInspectionFailed", "sensitive", "SensitiveContent",
                  "disallowed content", "disallowed_content", "content policy",
                  "contentPolicy", "blocked by", "moderation"],
        "title": "内容审核不通过",
        "why": "命中平台的敏感内容策略。**平台是跑完才判的，判违规照样计费。**",
        "fix": ["降低暴力/血腥/成人元素", "换镜头表达方式（暗示而不是直接展示）",
                "先跑 moderation 预检（对话里生成前会自动做）"],
    },
    {
        "match": ["insufficient credit", "remaining credit", "credit is not enough",
                  "quota exceeded", "insufficient_quota", "balance",
                  "余额不足", "灵感值不足", "积分不足"],
        "title": "账号额度不足",
        "why": "平台余额/灵感值不够，**跟文案无关**，改提示词没用。",
        "fix": ["去平台充值",
                "可灵充值页：https://klingai.com/h5-app/membership-agent?r=agent_VideoForge",
                "换一个还有额度的渠道（对话里说「换个平台」）"],
    },
    {
        "match": ["InvalidEndpointOrModel", "model or endpoint", "does not exist"],
        "title": "模型 ID 不对或没开通",
        "why": "平台不认这个 model 名（**不是内容问题**）。",
        "fix": ["用 list_video_models 看可用型号",
                "火山方舟的 model 名形如 doubao-seedance-2-5-260628"],
    },
    {
        "match": ["must be provided as a web url", "invalid url scheme"],
        "title": "参考视频必须是公网 URL",
        "why": "参考视频不接受 base64 或本地路径（图片可以，视频不行）。",
        "fix": ["跑 scripts\\装隧道工具.ps1", "或在设置里配 public_base_url"],
    },
    {
        "match": ["duration specified in the request is not valid",
                  "duration", "exceeds max", "below min"],
        "title": "时长不在模型允许范围",
        "why": "每个模型的时长是**离散枚举**，不是任意值。",
        "fix": ["用 plan_long_video 先排片，它会算好合法时长",
                "各模型范围见 list_video_models"],
    },
    {
        "match": ["Timeout", "timeout", "timed out"],
        "title": "平台超时",
        "why": "任务在预期时间内没完成。",
        "fix": ["稍后重试", "缩短时长或降分辨率"],
    },
]


def explain_platform_error(raw: Any) -> Dict[str, Any]:
    """把平台的报错翻译成"为什么 + 怎么办"。

    不做这个的话，用户看到的是一段英文 JSON —— 他既不知道是自己写错了，
    还是平台抽风，也不知道下一步该改什么。
    """
    text = str(raw or "")
    for item in ERROR_PLAYBOOK:
        if any(m.lower() in text.lower() for m in item["match"]):
            return {"title": item["title"], "why": item["why"], "fix": item["fix"],
                    "matched": True, "raw": text[:500]}
    return {
        "title": "平台报错",
        "why": "没匹配到已知的错误类型，下面是平台原文。",
        "fix": ["把下面这段原文提供给排查（或交给 agent 看）"],
        "matched": False,
        "raw": text[:500],
    }
