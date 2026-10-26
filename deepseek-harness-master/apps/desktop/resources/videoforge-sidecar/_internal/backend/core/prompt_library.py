# -*- coding: utf-8 -*-
"""
电影语言提示词库（150 条 cinematic techniques）

════════════════════════════════════════════════════════════════════
来源：crealens.ai 全站技术库（2026-09-29 完整抓取，150 条，零失败）
净化：已剥离器材品牌 / 胶片型号 / 导演名 —— 实测这些对出图模型**无视觉贡献**
      （MiniMax image-01，14 张对照出图验证）
有效性：129 条对静态出图有效；21 条（叙事 11 / 剪辑 10）对单张静态图无效
════════════════════════════════════════════════════════════════════

为什么做成「检索式」而不是把 150 条全塞进 system prompt：

    techniques.json  172 KB  → 全塞进 prompt 约 5 万 token，
                                会把项目上下文整个挤出去，而且模型
                                面对 150 条并列时选择质量明显下降。
    catalog.txt       18 KB  → 约 5 千 token，一行一条，够模型做「先选后取」。
    techniques.json   按需取  → 模型选定 slug 后才调 lookup 拿完整模板。

    所以工作流是：读 catalog 选技法 → lookup_cinematic_prompt 取模板
    → 替换 [Subject] → 交给已有的 generate_image 工具。
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from typing import Any, Dict, List, Optional

# 分类中文名
CAT_CN = {
    "camera": "摄影机与镜头",
    "composition": "构图",
    "lighting": "灯光",
    "editing": "剪辑",
    "genres": "类型美学",
    "storytelling": "叙事",
    "vfx": "视觉特效与质感",
}

# 短标签（给 catalog 用，省 token）
CAT_SHORT = {
    "camera": "摄影", "composition": "构图", "lighting": "灯光",
    "editing": "剪辑", "genres": "类型", "storytelling": "叙事",
    "vfx": "特效",
}

#: 哪些分类对静态出图有效
#   editing / storytelling 大部分是「时间结构」概念，单张图里没有对应物，
#   硬塞进去只会稀释真正起作用的词。这两类的少部分（freeze-frame、
#   time-lapse 等有单帧痕迹的）已在导出时单独标了 effective=image。
STATIC_SAFE_CATS = ("camera", "composition", "lighting", "genres", "vfx")

PLACEHOLDER = "[Subject]"

_CJK = re.compile(r"[\u4e00-\u9fff]")


def is_cjk(s: str) -> bool:
    """主体描述里有没有中文字符。"""
    return bool(_CJK.search(str(s or "")))


def subject_language_warning(subject: str) -> str:
    """
    ★ 中文主体直接填进**英文**模板会破句法 ——
    实测：「Choker shot framing 穿米色风衣的中年男人 from hairline to chin」
    夹在两个英文短语中间，模型读起来是断的。

    两条路（实测都成立）：
      · 出图模型吃英文 —— 把主体写成英文（a middle-aged man in a beige trench coat）
      · 确实要中文 —— 换个说法描述主体，或接受模型自己英译
    """
    if not is_cjk(subject):
        return ""
    return ("注意：你填的主体是中文，而模板是英文的。直接这样拼出来会是"
            "「Choker shot framing 穿米色风衣的中年男人 from hairline」这样断句，"
            "模型读不通。建议把主体改成英文描述（如 a middle-aged man in a "
            "beige trench coat），或者把整个提示词一起翻译后再出图。")

# ── 中英情绪/意图映射 ──
# 用户的说法是中文，库里的 when 是英文，字面匹配必然 miss。
# 这张表把常见中文意图翻成英文检索词，让 search() 能命中。
# （实测踩过：用户说「不安 压迫」，直接搜英文 when 一个都搜不到。）
CN_QUERY_MAP = {
    "孤独": "isolated lonely solitude", "寂寞": "lonely solitude",
    "不安": "unease anxiety tense", "紧张": "tense tension suspense",
    "压迫": "oppressive looming claustrophobic", "窒息": "claustrophobic",
    "恐惧": "dread fear terror horror", "恐怖": "horror menace",
    "悬疑": "suspense mystery", "神秘": "mystery enigmatic",
    "悲伤": "melancholy sad", "忧郁": "melancholy", "哀伤": "sorrow grief",
    "温暖": "warm tender", "温馨": "warm tender",
    "冷": "cold cool", "冰冷": "cold bleak", "清冷": "cool",
    "黑暗": "dark shadow low-key", "明亮": "bright high-key",
    "梦幻": "dream ethereal", "飘逸": "ethereal", "超现实": "surreal",
    "怀旧": "nostalgic vintage memory", "复古": "vintage retro",
    "紧张追逐": "chase action kinetic", "动感": "kinetic dynamic motion",
    "静谧": "still quiet calm", "平静": "calm serene",
    "亲密": "intimate close tenderness", "疏离": "distant detached alienated",
    "宏大": "epic vast grand", "渺小": "tiny dwarfed small",
    "神秘仪式": "ritual ceremonial", "宗教": "religious sacred",
    "懷舊": "nostalgic vintage",
    "忧郁反光": "melancholy",
    "逆光": "backlit backlight rim", "轮廓光": "rim light backlit",
    "逆光轮廓": "rim light backlit",
    "浅景深": "shallow depth of field bokeh", "虚化": "bokeh shallow",
    "特写": "close-up closeup", "近景": "close-up",
    "远景": "long shot wide establishing", "全景": "wide establishing",
    "俯拍": "high angle bird's eye overhead", "仰拍": "low angle worm's eye",
    "荷兰角": "dutch angle canted", "倾斜": "dutch angle canted tilted",
    "手持": "handheld", "稳定器": "steadicam tracking",
    "慢动作": "slow motion", "延时": "time-lapse",
    "黑白": "monochrome black and white", "单色": "monochrome",
    "青橙": "teal orange", "暖调": "warm amber", "冷调": "cool blue",
    "颗粒": "film grain", "胶片": "film grain celluloid",
    "剪影": "silhouette", "剪影轮廓": "silhouette",
    "烟雾": "smoke haze", "雾气": "haze fog mist",
    "雨夜": "rain night wet", "雨水": "rain wet",
    "城市": "urban city", "乡村": "rural countryside",
    "室内": "interior indoor", "室外": "exterior outdoor",
    "战争": "war", "末世": "post-apocalyptic", "废墟": "ruin decay",
    "浪漫": "romantic romanticism", "喜剧": "comedy comic",
    "动作": "action kinetic", "追逐": "chase",
    "纪录": "documentary verite handheld", "真实": "naturalistic documentary",
}


def _expand_query(q: str) -> List[str]:
    """把中文查询扩成英文检索词；英文原样保留。"""
    if not q:
        return []
    terms: List[str] = []
    for w in [x for x in q.replace(",", " ").replace("，", " ").split() if x]:
        wl = w.lower()
        terms.append(wl)
        if re.search(r"[\u4e00-\u9fff]", w):
            for zh, en in CN_QUERY_MAP.items():
                if zh in w:
                    for t in en.split():
                        if t not in terms:
                            terms.append(t)
    return terms



def _lib_dir() -> str:
    """提示词库目录。可用环境变量覆盖（测试/便携用）。"""
    env = os.environ.get("VF_PROMPT_LIB")
    if env:
        return env
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "prompt_library")


@lru_cache(maxsize=1)
def _load_techniques() -> List[Dict[str, Any]]:
    p = os.path.join(_lib_dir(), "techniques.json")
    if not os.path.exists(p):
        return []
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


@lru_cache(maxsize=1)
def _load_catalog() -> str:
    p = os.path.join(_lib_dir(), "catalog.txt")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return f.read()
        except Exception:  # noqa: BLE001
            return ""
    # 索引文件缺失时用 techniques.json 现拼一份，保证功能不挂
    lines = []
    for t in _load_techniques():
        flag = "" if t.get("effective") == "image" else "*"
        d = str(t.get("when") or "")[:88]
        lines.append(f'{t["slug"]}{flag}|{t.get("title","")}|'
                     f'{CAT_SHORT.get(t.get("cat",""), "")}|{d}')
    return "\n".join(lines)

def is_available() -> bool:
    return bool(_load_techniques())


def library_size() -> int:
    return len(_load_techniques())


def catalog_text(static_only: bool = True) -> str:
    """
    给 agent system prompt 用的目录。

    static_only=True 时只列对出图有效的条目 —— 模型是在"选一个出图技法"，
    把剪辑/叙事那 21 条摆出来只会诱导它选到无效项。

    ★ 「*」是**紧跟 slug** 的（catalog.txt 的第一个字段），
      不是行尾字符。判断要拆第一个字段去看，不能 endswith("")。
    """
    cat = _load_catalog()
    if not static_only:
        return cat
    out = []
    for line in cat.splitlines():
        head = line.split("|", 1)[0] if line else ""
        if head.endswith("*"):
            continue
        out.append(line)
    return "\n".join(out)


def get(slug: str) -> Optional[Dict[str, Any]]:
    slug = str(slug or "").strip().lower()
    if not slug:
        return None
    for t in _load_techniques():
        if t.get("slug") == slug:
            return t
    return None


def search(query: str, cat: str = "", static_only: bool = True,
           limit: int = 8) -> List[Dict[str, Any]]:
    """
    按关键词粗匹配「何时使用」与标题。
    命中词越靠前、出现越多，排得越前。

    这不是向量检索，是一个**够用且零依赖**的字面匹配 ——
    agent 的模型本身就能读懂 when 描述，工具只负责把候选缩到可读的十几条。
    """
    q = str(query or "").strip().lower()
    c = str(cat or "").strip().lower()
    out: List[Dict[str, Any]] = []

    for t in _load_techniques():
        if c and t.get("cat") != c:
            continue
        if static_only and t.get("effective") != "image":
            continue
        score = 0
        terms = _expand_query(q)
        if terms:
            hay = f'{t.get("title","")} {t.get("when","")} {t.get("prompt","")}'.lower()
            for w in terms:
                if w in hay:
                    score += 1
            # 标题命中权重更高
            if q and q in str(t.get("title", "")).lower():
                score += 5
            if score == 0:
                continue
        out.append((score, t))

    out.sort(key=lambda x: -x[0])
    return [t for _, t in out[: max(1, int(limit))]]


def fill_subject(template: str, subject: str) -> str:
    """
    把模板里的 [Subject] 换成主体描述。

    ★ 主体描述里如果自带逗号/分号，会把提示词的逗号分隔结构搞乱，
      所以这里做最小清洗：压掉换行、合并连续标点。
    """
    s = str(subject or "").strip()
    s = s.replace("\n", " ").replace("\r", " ")
    s = " ".join(s.split())
    s = s.strip(" ,;，、")
    if not s:
        return template
    return template.replace(PLACEHOLDER, s)


def render(slug: str, subject: str = "") -> Optional[str]:
    """取模板并填主体；slug 不存在返回 None。"""
    t = get(slug)
    if not t:
        return None
    return fill_subject(t.get("prompt", ""), subject) if subject else t.get("prompt", "")


def recommend(mood: str = "", cat: str = "", subject: str = "",
              limit: int = 4) -> List[Dict[str, Any]]:
    """给「我想要 XX 氛围」这类需求挑候选技法。"""
    return search(mood, cat=cat, static_only=True, limit=limit)
