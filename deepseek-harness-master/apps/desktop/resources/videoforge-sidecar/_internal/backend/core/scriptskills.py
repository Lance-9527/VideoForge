# -*- coding: utf-8 -*-
"""
VideoForge · 剧本「创作规范」注入（用户自己上传的手册 / 提示词规范）
==================================================================

用户原话（2026-10-23）：
    「你剧本设定既不围绕用户意图做好设定，又没遵照我提示词规范文本展开陈述，
      也不理清细节做好衔接」

## 根因（本轮查实，不是猜）

core/llm.py 里的 SCRIPT_GENERATION_SYSTEM 是**短剧公式写死的**：
    「主角必须有前史动机」「反派要有行为方式」「群像要摇摆」「结尾金句点题」
——于是用户说「做个产品宣传片」，模型也去编反派、前史、群像。
剧本自然不围绕用户意图（宣传片要讲的是产品，不是戏剧冲突）。

而用户上传的两份手册
（data/skills/short-drama-formula/SKILL.md 60KB、shaw-brothers-style/SKILL.md 25KB）
**根本没进剧本提示词** —— 它们只通过 vf_skill 工具给 agent 自己看，
剧本生成服务看不到。这就是「没遵照我的提示词规范」的机制性原因。

## 这一模块做什么

1. **找得到**：定位用户手册目录（打包版 / 开发版 / 自定义 data_dir 都试）。
2. **提炼得下**：60KB 手册不能整份塞进提示词（token 贵、还容易被模型忽略）。
   这里只抽「结构与规则」（标题 + 公式/规则/结构/模板 小节里的条目），
   案例库/目录/附录全部跳过 —— 手册里真正决定「怎么写」的是公式与规则。
   每份手册封顶 2.6KB，合计 5.2KB。
3. **按体裁给节拍**：宣传片 ≠ 短剧。genre_of() 用强关键词判体裁，
   genre_spec() 给对应节拍（宣传片：钩子→痛点→方案→证据→信任→行动，
   **不写反派/前史动机**）。
4. **意图锚点**：intent_line() 让剧本开头必须复述用户意图，
   剧本体检会核对（复述不出来 = 跑偏）。

纯本地、零费用、零网络。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("videoforge.scriptskills")

#: 单份手册提炼后的字数上限（两份额定约 5KB，塞进提示词不心疼）
PER_SKILL_CAP = 2600
#: 全部手册合计上限
TOTAL_CAP = 5200

#: 手册里「案例/目录/事实」这类段落对「怎么写」没有约束力，跳过
SKIP_HEADINGS = ("目录", "案例", "参考", "事实校正", "附录", "来源", "资料",
                 "速查", "调研", "延伸阅读", "引用")

#: 抽取时优先的段落关键词（命中就整段收）
KEEP_HINTS = ("公式", "规则", "结构", "模板", "节拍", "要求", "要点", "步骤",
              "清单", "规范", "写法", "指南", "原则", "节奏", "钩子", "衔接")

PROMO_WORDS = ("宣传", "广告", "推广片", "产品片", "品牌片", "发布会", "口播",
               "promo", "commercial", "tvc", "种草", "带货", "介绍片", "介绍视频",
               # ★ 2026-10-23：用户原话可能是"做个介绍视频/开箱/测评/教程/记录片"，
               #   这些都是**宣传片**体裁（不是剧情片），不认出来就会走中性模板。
               "开箱", "测评", "评测", "教程", "记录片", "纪录片", "vlog", "vlog片",
               "企业片", "形象片", "mv", "片头", "预告片", "海报")
DRAMA_WORDS = ("短剧", "微短剧", "剧情", "故事", "剧本集", "连续剧", "爽剧",
               "逆袭", "复仇", "重生", "穿越", "drama")


def skills_dir(settings: Optional[Dict[str, Any]] = None) -> str:
    """定位用户手册目录。"""
    cands: List[str] = []
    s = settings or {}
    for k in ("data_dir", "skills_dir", "output_dir"):
        v = str(s.get(k) or "").strip()
        if v:
            cands.append(os.path.join(v, "skills"))
    env = os.environ.get("VIDEOFORGE_DATA_DIR", "").strip()
    if env:
        cands.append(os.path.join(env, "skills"))
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        cands.append(os.path.join(local, "VideoForge", "data", "skills"))
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cands.append(os.path.join(here, "data", "skills"))
    for c in cands:
        try:
            if os.path.isdir(c) and os.listdir(c):
                return c
        except OSError:
            continue
    return ""


def _distill(text: str, cap: int = PER_SKILL_CAP) -> str:
    """从一份手册里抽「结构与规则」（跳过案例/目录/附录）。"""
    out: List[str] = []
    size = 0
    keep = False
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            head = line.lstrip("# ").strip()
            if any(k in head for k in SKIP_HEADINGS) and not any(k in head for k in KEEP_HINTS):
                keep = False
                continue
            keep = any(k in head for k in KEEP_HINTS) or not out
            if keep:
                out.append("\n## " + head)
                size += len(head) + 4
            continue
        if not keep:
            if not re.search(r"(公式|规则|必须|不要|禁止|要求|节拍|结构)", line):
                continue
        out.append(line.strip())
        size += len(line) + 1
        if size >= cap:
            out.append("…（其余略）")
            break
    return "\n".join(out).strip()


def load_specs(settings: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """读出用户手册的规范提纲。返回 [{slug, title, body}]。"""
    base = skills_dir(settings)
    if not base:
        return []
    out: List[Dict[str, Any]] = []
    for slug in sorted(os.listdir(base)):
        d = os.path.join(base, slug)
        md = os.path.join(d, "SKILL.md")
        if not (os.path.isdir(d) and os.path.isfile(md)):
            continue
        try:
            raw = open(md, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        title = ""
        m = re.search(r"^#\s+(.+)$", raw, re.M)
        if m:
            title = m.group(1).strip()
        body = _distill(raw)
        if body:
            out.append({"slug": slug, "title": title or slug, "body": body})
    return out


def genre_of(intent: str) -> str:
    """判体裁：promo（宣传/广告/口播）· drama（剧情短剧）· generic。"""
    t = (intent or "").lower()
    if any(w.lower() in t for w in PROMO_WORDS):
        return "promo"
    if any(w.lower() in t for w in DRAMA_WORDS):
        return "drama"
    return "generic"


#: 各体裁的节拍要求（写进提示词，替代「一律按短剧公式」）
GENRE_SPECS: Dict[str, str] = {
    "promo": (
        "本条是**产品宣传片/口播/品牌片**，不是剧情短剧。\n"
        "  · 结构按节拍推进：**钩子（前3秒抓住人）→ 痛点（用户现在有多难）→ "
        "方案（产品怎么解决，一句话说清）→ 证据（画面/数字/场景证明，不是形容词）→ "
        "信任（对比、细节、口碑式呈现）→ 行动（结尾一句话让用户知道下一步做什么）**。\n"
        "  · **绝对不要**写前史动机、反派、群像摇摆、恩怨反转 —— 宣传片没有敌人，"
        "痛点就是敌人；写成剧情就完全跑偏了。\n"
        "  · 每个节拍都要能**拍出来**：一句画面 + 一句台词/字幕，不要抽象形容词"
        "（震撼、高级感这类词一律不写）。\n"
        "  · 台词像真人说话：一句话一个信息点，4 字/秒以内；不要书面语、不要排比句、"
        "不要喊口号。\n"
        "  · 画面文字（如产品名、卖点字、字幕）要**按需要**给：该出现就写清内容，"
        "不需要就一个字不提。"
    ),
    "drama": (
        "本条是**剧情类短剧**。结构按：起（钩子）→ 承（升级）→ 转（反转/揭示）→ "
        "合（收束 + 金句）推进；每场标题是钩子，每场结尾留悬念。"
    ),
    "generic": (
        "本条体裁未指明：**先复述用户的真实意图**，再按意图选结构；"
        "不确定时按「意图 → 展开 → 收束」三段推进，不要自行加戏剧冲突。"
    ),
}


def genre_spec(genre: str) -> str:
    return GENRE_SPECS.get(genre or "generic", GENRE_SPECS["generic"])


def intent_line(intent: str) -> str:
    """让剧本必须回话用户意图的那一句（写进提示词，并被剧本体检核对）。"""
    it = (intent or "").strip()
    return (
        "【意图锚点】这条片子唯一要达成的是：%s\n"
        "  剧本开头必须有一行 `intent` 字段，**原样复述**你要达成的事"
        "（不许改写成别的目标）。\n"
        "  任何一场如果跟这个意图无关，就是跑偏 —— 不要为了戏剧性牺牲它。\n"
        % (it or "（用户未写明，按最贴近原话理解）")
    )


def spec_brief(settings: Optional[Dict[str, Any]] = None, genre: str = "generic",
               *, cap: int = TOTAL_CAP) -> str:
    """把「用户手册提纲 + 体裁节拍」拼成一段可注入的规范文本。"""
    parts: List[str] = []
    specs = load_specs(settings)
    used = 0
    for s in specs:
        if used >= cap:
            break
        room = cap - used
        body = s["body"][:room]
        if not body:
            continue
        parts.append("──── 《%s》 ────\n%s" % (s["title"], body))
        used += len(body)
    out = ""
    if parts:
        out += ("【你上传的创作规范（从你的手册里提炼，**优先于任何默认写法**）】\n"
                + "\n\n".join(parts)
                + "\n如果下面的【本条体裁】与手册有冲突，**以手册为准**；"
                  "手册没写到的部分才用默认写法。\n")
    out += "【本条体裁】\n" + genre_spec(genre) + "\n"
    # ★ 段落层（有总时长时给出具体秒数）
    # ★ 只在**叙事体裁**（剧情/短剧）且时长够长时才给段落表：
    #   这套比例是从一条 3.5 分钟的叙事短片量的；套到 45 秒宣传片上会出现
    #   「离别」「反转」这种荒谬段落 —— 宣传片走的是另一套节拍（钩子→痛点→…）。
    try:
        _total = int((settings or {}).get("_total_duration") or 0)
        if genre == "drama" and (_total >= 120 or _total == 0):
            out += segment_table(_total or 180) + "\n"
    except Exception:  # noqa: BLE001
        pass
    return out


# ══════════════════════════════════════════════════════════════════
# ★★ 2026-10-23：从一条被广泛转发的 AI 短片的**拉片**里量出来的段落结构
#   （《THE SCENE MAY CAUSE DISCOMFORT》逐段时长：人物出场10s / 舞蹈蒙太奇25s /
#     意外事件14s / 公路28s / 打斗24s / 邂逅35s / 爱情蒙太奇39s / 离别13s / 思念反转19s）
#
#   结论：AI 短片不是「按镜头」组织的，是**按功能段落**组织的（一段 10-39 秒，内部 3-6 镜）。
#   我们之前只有"镜头层"，没有"段落层" —— 这是"片子不成立"的结构性原因。
#   这里把比例固化，按总时长换算成秒，写进剧本提示词，并要求剧本按段落排。
# ══════════════════════════════════════════════════════════════════
SEGMENT_RATIOS = [
    ("人物出场", 0.05, "10 秒内立住人物：造型一眼可辨 + 一个标志性动作 + 定调"),
    ("性格蒙太奇", 0.12, "无台词，用**动作打点**剪（一个动作一拍），每 1-2 秒一个视觉变化"),
    ("意外事件", 0.07, "打破平衡，必须有一个具体事件（不是「气氛变紧张」）"),
    ("空间转移", 0.13, "换地方/换时态，用空镜或动势接力转场"),
    ("冲突", 0.12, "冲突要实体化：谁对谁、争什么、代价是什么"),
    ("相遇", 0.17, "关系建立，用**反应镜头**而不是台词说明关系"),
    ("情感蒙太奇", 0.19, "全片最长段：并列 3-5 个小情节堆密度，靠节奏与视觉推进"),
    ("离别", 0.06, "情绪落点，允许留白与长一点的空镜"),
    ("思念与反转", 0.09, "结尾必须有一个**信息反转**（观众以为 A，其实 B）"),
]


def segment_table(total_seconds: int = 60) -> str:
    """按总时长换算成"段落表"，写进剧本提示词（让剧本按段落组织，而不是平铺场次）。"""
    try:
        total = max(10, int(total_seconds or 60))
    except Exception:  # noqa: BLE001
        total = 60
    rows, acc = [], 0
    for i, (name, ratio, note) in enumerate(SEGMENT_RATIOS):
        if i == len(SEGMENT_RATIOS) - 1:
            sec = total - acc
        else:
            sec = max(2, int(round(total * ratio)))
            acc += sec
        rows.append("  %d. %s ≈ %d 秒 —— %s" % (i + 1, name, sec, note))
    return ("【段落结构（从一条爆款 AI 短片的拉片实测比例里换算的）】\n"
            + "\n".join(rows)
            + "\n  · 剧本的场次要**按上面这些功能段落排**，不要平铺一堆同类镜头；\n"
              "  · 开场段（人物出场）必须在前 10 秒内让观众记住这个人；\n"
              "  · 带「蒙太奇」的段落**不靠台词**推进，靠动作节奏与视觉变化；\n"
              "  · 最后一个段必须有信息反转。\n")


def spec_report(settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """体检用：当前到底加载了哪几份手册、各多少字。"""
    specs = load_specs(settings)
    return {
        "skills_dir": skills_dir(settings),
        "count": len(specs),
        "specs": [{"slug": s["slug"], "title": s["title"], "chars": len(s["body"])}
                  for s in specs],
    }


# ══════════════════════════════════════════════════════════════════
# ★ 2026-10-24：内置 skill 的规范也进剧本提示词
# ══════════════════════════════════════════════════════════════════
#
# 背景：Lance 要把罗臣臣/波导翁的方法论学进 VideoForge。三个内置 skill
# （short-drama-script / cinematic-film-style / natural-dialogue）写好了，
# 但**只被 `use_skill` 按需加载** —— 剧本生成服务看不到它们。
# 这跟 2026-10-23 那个「用户手册根本没进剧本提示词」是**同一个坑**，
# 只不过这次是内置 skill 版本的。
#
# 所以这里把三个内置 skill 的**机器规则**（rules.json）压成紧凑条目，
# 按体裁/风格档筛过之后拼进剧本提示词。
#
# ★ 数值单一真相源： cinematic-film-style 的阈值只在它的 rules.json 里存在
#   一份，本模块只**读**不抄（那个 skill 自己写着「不允许在代码里再抄一份常量」）。

#: 会进剧本提示词的内置 skill
BUILTIN_SCRIPT_SKILLS = ("short-drama-script", "cinematic-film-style", "natural-dialogue")

#: 注入总量上限（字符）。剧本提示词已经很长，这里必须封顶。
BUILTIN_SPEC_CAP = 4200

#: 选线判据（与 short-drama-script 的 genre 对齐）
GENRE_ENSEMBLE = "ensemble-series"      # 罗臣臣线：群像追更
GENRE_SINGLE = "single-episode"        # 波导翁线：生活流单集

_ENSEMBLE_HINTS = ("群像", "追更", "连载", "长线", "分集", "每集更新", "剧集")
_SINGLE_HINTS = ("生活流", "单集", "短片", "生活化", "亲情", "夫妻", "邻里")


def pick_line(user_prompt: str, total_seconds: int = 0) -> str:
    """判这条片子走哪条线：罗臣臣线（群像追更）还是波导翁线（生活流单集）。

    ★ 判据要诚实：
      · 用户话里明说 → 按用户说的；
      · 否则按**总时长**：罗臣臣的单集 6–19 分钟且逐集递增，
        波导翁的单集中位 2:14（73% 在 2.5 分钟内）。于是 ≥6 分钟判群像线，
        <6 分钟判生活流线。
    """
    t = str(user_prompt or "")
    lo = t.lower()
    if any(h in t for h in _ENSEMBLE_HINTS):
        return GENRE_ENSEMBLE
    if any(h in t for h in _SINGLE_HINTS):
        return GENRE_SINGLE
    try:
        if int(total_seconds or 0) >= 360:
            return GENRE_ENSEMBLE
    except Exception:  # noqa: BLE001
        pass
    return GENRE_SINGLE


def _rules_of(name: str) -> Dict[str, Any]:
    try:
        from .skills import builtin_rules
        return builtin_rules(name) or {}
    except Exception:  # noqa: BLE001
        return {}


def _drama_rules_brief(rules: Dict[str, Any], line: str, cap: int) -> str:
    """从 short-drama-script 的 rules.json 里抽「这条线」要用的规则。"""
    if not rules:
        return ""
    out: List[str] = []
    pri = rules.get("priority") or []
    if isinstance(pri, list):
        for x in pri[:6]:
            out.append("· %s" % str(x)[:110])
    # 规则是扁平键：luo_* / bdw_* —— **只取这条线的**，
    # 另一条线的规则混进来会让模型把两条线搅在一起（skill 明确要求"二选一，不可混写"）。
    want = ("luo_",) if line == GENRE_ENSEMBLE else ("bdw_",)
    other = "波导翁线" if line == GENRE_ENSEMBLE else "罗臣臣线"
    picked = 0
    for k, v in rules.items():
        if not isinstance(v, dict) or not str(k).startswith(want):
            continue
        txt = str(v.get("text") or "").strip()
        if not txt:
            continue
        out.append("· %s" % txt[:110])
        picked += 1
        if picked >= 34:
            break
    out.append("★ 本条只走%s；%s的特征**不要**出现在这份稿子里（不要混写）。"
               % ("罗臣臣线" if line == GENRE_ENSEMBLE else "波导翁线", other))
    red = rules.get("red_flags") or []
    if isinstance(red, list) and red:
        out.append("【红线（出现即判错线）】")
        for x in red[:5]:
            out.append("· %s" % str(x)[:110])
    txt = "\n".join(out)
    return txt[:cap]


def _cinematic_brief(rules: Dict[str, Any], line: str, cap: int) -> str:
    """从 cinematic-film-style 的 rules.json 里抽「生成时必须知道的规格 + 禁令」。"""
    if not rules:
        return ""
    out: List[str] = []
    prof = "life-stream-cinematic" if line == GENRE_SINGLE else "era-texture"
    raw = (rules.get("style_profiles") or {}).get(prof) or {}
    label = str(raw.get("name_zh") or prof)
    canvas = raw.get("canvas") or {}
    summary = str(raw.get("summary_zh") or "")[:110]
    out.append("【风格档：%s】%s" % (label, summary))
    out.append("· 画幅：%s %s %s fps"
               % (canvas.get("aspect_ratio"), canvas.get("resolution"), canvas.get("fps")))
    idx = rules.get("rule_index") or []
    if isinstance(idx, list):
        for x in idx[:16]:
            if isinstance(x, dict):
                out.append("· %s" % str(x.get("text_zh") or "")[:100])
    must_not = ((rules.get("script_generation_contract") or {}).get("must_not")) or []
    if isinstance(must_not, list) and must_not:
        out.append("【禁止】")
        for x in must_not[:4]:
            out.append("· %s" % str(x)[:100])
    return "\n".join(out)[:cap]


def _dialogue_brief(rules: Dict[str, Any], line: str, cap: int) -> str:
    """从 natural-dialogue 的 rules.json 里抽「写台词时的红线」。"""
    if not rules:
        return ""
    out: List[str] = []
    for k in ("banned_words", "banned_phrases", "forbidden", "red_flags",
              "no_skip_zones", "priority"):
        v = rules.get(k)
        if isinstance(v, list) and v:
            out.append("【%s】" % k)
            for x in v[:10]:
                if isinstance(x, dict):
                    out.append("· %s" % str(x.get("text") or x.get("rule") or x)[:100])
                else:
                    out.append("· %s" % str(x)[:100])
    return "\n".join(out)[:cap]


# ── 电商商品视频（promo 的特殊子类）──────────────────────────────

#: 判定「这条 promo 其实是带货/商品视频」的关键词。
#: ★ 为什么用子类而不是新增体裁：`genre_of()` 的三分（promo/drama/generic）
#:   同时被 scriptaudit 的尺子和 llm 的 system prompt 分流用着，
#:   直接加第四个体裁会让那两处都要改，风险大且没必要 —— 电商需要的只是
#:   **额外注入一套规范**，体裁分流逻辑不需要变。
ECOMMERCE_WORDS = (
    "带货", "商品视频", "商品卡", "主图视频", "详情页视频", "宝贝视频",
    "卖货", "电商", "抖店", "小店", "淘宝", "天猫", "京东", "拼多多",
    "视频号小店", "微信小店", "千川", "投流", "投放素材", "种草视频",
    "开箱视频", "测评视频", "产品演示", "sku", "SKU", "转化率",
)


def is_ecommerce(prompt: str) -> bool:
    """这条片子的真实体裁是不是「电商商品视频」。"""
    t = str(prompt or "")
    if not t:
        return False
    return any(w in t for w in ECOMMERCE_WORDS)


def _ecommerce_brief(cap: int) -> str:
    """从 ecommerce-product-video 的 rules.json 里抽「写带货脚本必须知道的」压成一段。"""
    rules = _rules_of("ecommerce-product-video")
    if not rules:
        return ""
    out: List[str] = []
    # ① 平台规格（只列最常用几个，列全了太占 token）
    plats = rules.get("platforms") or {}
    if plats:
        out.append("【平台规格（超了就被裁切/压缩，成片白做）】")
        for k in ("douyin_shop_main", "taobao_main", "jd_main", "qianchuan_vertical"):
            p = plats.get(k)
            if not isinstance(p, dict):
                continue
            d = p.get("duration_sec") or {}
            out.append("· %s：%s-%ss，%s（%s）" % (
                p.get("name_zh") or k, d.get("min"), d.get("max"),
                p.get("aspect_ratio"), p.get("first_frame") or ""))
    # ② 通用四段节拍
    beats = rules.get("default_beats") or []
    if beats:
        out.append("【结构：四段式（抛需求→给方案→验信任→催下单）】")
        for b in beats[:5]:
            if not isinstance(b, dict):
                continue
            out.append("· %s（%.0f%%）：%s；必须：%s；不许：%s" % (
                b.get("beat"), float(b.get("ratio") or 0) * 100,
                str(b.get("visual") or "")[:40],
                "、".join(str(x) for x in (b.get("must") or [])[:2]) or "—",
                "、".join(str(x) for x in (b.get("must_not") or [])[:2]) or "—"))
    # ③ 卖点翻译（这是电商与品牌片最大的方法论差异）
    tr = (rules.get("selling_point_translation") or {})
    tb = tr.get("table") or []
    if tb:
        out.append("【卖点必须变成画面/数字（不许写形容词）】")
        for x in tb[:4]:
            if isinstance(x, dict):
                out.append("· 「%s」→ %s" % (str(x.get("from"))[:12], str(x.get("to"))[:50]))
    # ④ 合规红线
    comp = rules.get("compliance") or {}
    ab = comp.get("absolute_terms") or []
    if ab:
        out.append("【合规红线·绝对化用语（命中即违规，不许出现在台词/字幕里）】")
        terms = [str(x.get("term")) for x in ab[:14] if isinstance(x, dict)]
        out.append("· " + "、".join(terms))
    out.append("【合规红线】禁用「全网最便宜/销量第一/国家级/治疗/抗菌/减肥」这类；"
               "食品不得治病、化妆品不得医疗暗示；价格必须与挂车实际一致。")
    # ⑤ AI 标识（法定义务）
    lab = rules.get("ai_labeling") or {}
    if lab.get("mandatory"):
        out.append("【AI 标识（法定义务，%s 起）】AI 生成的画面必须在起始画面加提示、"
                   "元数据写隐式标识、发布时主动声明。" % (lab.get("effective_from") or ""))
    return "\n".join(out)[:cap]


def builtin_spec_brief(settings: Optional[Dict[str, Any]] = None,
                       genre: str = "generic",
                       *, user_prompt: str = "", total_seconds: int = 0,
                       cap: int = BUILTIN_SPEC_CAP) -> str:
    """把内置 skill 的机器规则压成一段，注入剧本提示词。

    · 体裁是 promo（宣传片）时**只注入电影风格 + 台词自然化** ——
      宣传片没有"分集/反转/群像"那套，灌进去反而跑偏。
    · **但「带货/商品视频」是 promo 的一个特殊子类**：它有自己的平台规格、
      品类节拍和合规红线，所以单独判 `is_ecommerce()` 并注入电商规范。
    · 剧情类才注入 short-drama-script，并按 line 分档筛规则。
    """
    if genre == "promo":
        line = GENRE_SINGLE
    else:
        line = pick_line(user_prompt, total_seconds)
    parts: List[str] = []
    used = 0

    # ★ 电商子通道：promo 里凡是带货/商品视频的，先把平台规格与合规红线灌进去
    if genre == "promo" and is_ecommerce(user_prompt):
        b = _ecommerce_brief(int(cap * 0.55))
        if b:
            parts.append("──── 《电商商品视频规范》 ────\n%s" % b)
            used += len(b)

    if genre != "promo":
        b = _drama_rules_brief(_rules_of("short-drama-script"), line, int(cap * 0.45))
        if b:
            parts.append("──── 《短剧剧本规范》（%s） ────\n%s"
                         % ("罗臣臣线·群像追更" if line == GENRE_ENSEMBLE
                            else "波导翁线·生活流单集", b))
            used += len(b)
    room = max(600, cap - used)
    b = _cinematic_brief(_rules_of("cinematic-film-style"), line, int(room * 0.6))
    if b:
        parts.append("──── 《电影风格规范》 ────\n%s" % b)
        used += len(b)
    room = max(400, cap - used)
    b = _dialogue_brief(_rules_of("natural-dialogue"), line, room)
    if b:
        parts.append("──── 《台词自然化红线》 ────\n%s" % b)
    if not parts:
        return ""
    head = ("【内置创作规范（来自 VideoForge 的 short-drama-script / "
            "cinematic-film-style / natural-dialogue / ecommerce-product-video "
            "四个 skill 的机器规则）】\n"
            "这些规则**优先于任何默认写法**；与下面【本条体裁】冲突时以它们为准。\n")
    return head + "\n\n".join(parts) + "\n"
