# -*- coding: utf-8 -*-
"""VideoForge · 台词自然化体检（AI 味检测 + 可执行改写）

★★ 用户的要求（2026-10-24）：
    「ai味太重——台词自然」。
    要的不是一句「要自然」，而是**能查也能改**的台词体检器。

这个模块把「台词有 AI 味」拆成 **10 类可执行判据**（D1–D10），
每类都有**正则 / 阈值 / 统计量**，不许只写形容词：

    D1 书面议论句        书面比喻连接词 + 总结性议论
    D2 解释性对白        角色替观众解释刚发生的动作/情绪
    D3 四平八稳          句长变异系数过小 + 标点/逗号单一（统计量）
    D4 排比/对仗成瘾      连续三句同构模板（不是…而是 / 既…又 / 一边…一边…）
    D5 情绪直陈          把情绪词直接写出来，而不是让动作承载
    D6 万能过渡词        然后 / 接着 / 与此同时 / 于是乎 / 值得一提 …
    D7 句长超载          超出该场时长能装下的字数
    D8 每句都完整        缺少口语省略（语病 / 语气词 / 口头重复 / 自我更正）
    D9 金句密度失控      可截图金句的密度超过上限（每 20 句 1 句）
    D10 方言/网络梗缺席   按 genre + style 分档（罗臣臣线要方言+梗；波导翁线禁梗）

── 尺子从哪来 ───────────────────────────────────────────────
· **量法与计分口径照抄 `core/scriptaudit.py`**：
  `CHARS_PER_SECOND = 4.0`、`LENGTH_TOLERANCE = 1.25`、
  `score = 100 - errors*15 - warnings*5`（`_finish()` 同一把尺子）。
· **写法口径来自两份本机调研**（不联网、不臆造来源）：
  - 罗臣臣《我的妹妹不可爱》B 站 ASR 字幕（字面或有偏差）
    → 方言口语主语体、保留语病/错别字/自我更正/口头重复、每集 2–3 个网络梗
  - 波导翁（故事叔）B 站原片 whisper 转写（人工校正错字）
    → 4–12 字短句、方言尾巴每片 ≤3 处、禁网络梗、每 20–30 句 1 句金句

── 边界 ─────────────────────────────────────────────────────
纯函数、零网络、零数据库、零付费。同一段台词在 `style=chenchen`
与 `style=bodaoweng` 下**结论必须不同**（见 `selftest()` 的 style_diff 用例）。
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Dict, List, Optional, Tuple

# ──────────────────────── 常量（照抄 scriptaudit 的量法）────────────────────────
CHARS_PER_SECOND = 4.0          # 中文口语（与 core/dialogue.py 同源）
LENGTH_TOLERANCE = 1.25

# ──────────────────────── 分档阈值 ────────────────────────────────────────────
_GENRE_CFG: Dict[str, Dict[str, Any]] = {
    # drama：短剧/剧情短片 —— 口语是第一位的
    "drama": {
        "cv_min": 0.20,             # 句长变异系数下限（低于它 = 四平八稳）
        "golden_per_lines": 20,     # 每 N 句允许 1 句可截图金句
        "golden_max": 1,
        "max_line_chars": 20,       # 单句 ≤5 秒（罗臣臣：单句 ≤5 秒，一句只装一件事）
        "require_spoken": True,     # 全篇零口语痕迹 → 报
    },
    # promo：宣传片 —— 允许更工整一点，金句可以更密
    "promo": {
        "cv_min": 0.15,
        "golden_per_lines": 10,
        "golden_max": 2,
        "max_line_chars": 24,
        "require_spoken": False,
    },
}

_STYLE_CFG: Dict[str, Dict[str, Any]] = {
    # auto：中性基线（只查"过密"和"完全没有口语痕迹"）
    "auto": {
        "dialect_required": False,
        "meme_min": None,
        "meme_max": 3,
        "meme_banned": False,
        "dialect_tail_max": None,
    },
    # chenchen：罗臣臣群像 —— 方言口语为主语体 + 每集 2–3 个网络梗
    "chenchen": {
        "dialect_required": True,
        "meme_min": 1,
        "meme_max": 3,
        "meme_banned": False,
        "dialect_tail_max": None,
    },
    # bodaoweng：生活流 —— 禁网络梗，方言尾巴每片 ≤3 处
    "bodaoweng": {
        "dialect_required": False,
        "meme_min": None,
        "meme_max": None,
        "meme_banned": True,
        "dialect_tail_max": 3,
    },
}

# ──────────────────────── 词表 ──────────────────────────────────────────────────
# D1 书面议论：书面比喻连接词
_RE_D1_SIMILE = re.compile(
    r"(仿佛|彷佛|宛如|犹如|恰似|恍若|无异于|堪比|好像在诉说|似乎在诉说|"
    r"宛如在诉说|仿佛在诉说|活像一个|犹如一幅|宛如画卷)")
# D1 书面议论：书面连接/抒情套板
_RE_D1_CONN = re.compile(
    r"(不禁|不由自主|此时此刻|这一瞬间|在那一瞬间|仿佛定格|不知不觉中|"
    r"在无声中|空气仿佛|时间仿佛)")
# D1 书面议论：总结性议论
_RE_D1_SUMMARY = re.compile(
    r"(这一切|所有这一切|总而言之|综上所述|由此可见|不得不说|说到底也是|"
    r"这一切都|这一切不过)")

# D2 解释性对白：角色替观众解释
_RE_D2 = re.compile(
    r"(你看你现在|你看你这|你看看你这|你看看我这|我是说|我刚才就是想说|"
    r"也就是说|换句话说|换言之|这说明|这就叫|这意味着|"
    r"我告诉你(?:一个|个|你听|你)|让我来告诉(?:你|大家)|"
    r"你别怪我没提醒(?:你)?|你不觉得|刚才你(?:也)?(?:看到|听见|经历)|"
    r"你要知道|我这么跟你说的|让我解释一下|我帮你解释)")

# D5 情绪直陈：程度副词 + 情绪词（硬）
_EMOTION_WORDS = (
    "伤透了心", "心如死灰", "百感交集", "五味杂陈", "感慨万千",
    "愤怒", "生气", "难过", "伤心", "失落", "失望", "委屈", "害怕", "紧张",
    "崩溃", "绝望", "孤独", "开心", "高兴", "感动", "兴奋", "着急", "后悔",
    "痛苦", "悲伤", "心碎", "难受", "揪心", "火大", "心痛")
_EMO_ALT = "|".join(_EMOTION_WORDS)
_RE_D5_STRONG = re.compile(
    r"(非常|十分|无比|特别|极其|格外|真的好)(%s)" % _EMO_ALT)
_RE_D5_COPULA = re.compile(
    r"(感到|感觉|觉得|心里|心中|内心)(很|非常|十分|特别|无比|越发|越来越)?"
    r"(是)?(%s)" % _EMO_ALT)
_RE_D5_TAIL = re.compile(r"(%s)(极了|得很|得不行)" % _EMO_ALT)
# 弱（单独出现只提示）
_RE_D5_WEAK = re.compile(r"(很|真的|好)(%s)(了|呢|吧)?" % _EMO_ALT)
# 直接写情绪名（"她伤透了心"）
_RE_D5_NAME = re.compile(r"(%s)" % _EMO_ALT)

# D6 万能过渡词
_RE_D6 = re.compile(
    r"(然后|接着|与此同时|于是乎|值得一提|首先|其次|最后|总而言之|"
    r"总的来说|综上|除此之外|不仅如此|更重要的是|事实上|实际上|"
    r"换句话说|也就是说|与此相反|与此同时)")

# D4 排比/对仗模板（同构检测）
_PARALLEL_TEMPLATES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"不是[^，。！？!?]{2,14}[，,]?而是"), "不是…而是"),
    (re.compile(r"不是[^，。！？!?]{2,14}[，,]是"), "不是…是"),
    (re.compile(r"与其[^，。！？!?]{2,14}不如"), "与其…不如"),
    (re.compile(r"既[^，。！？!?]{2,12}[，,][^，。！？!?]{0,12}又[^，。！？!?]{2,12}"),
     "既…又"),
    (re.compile(r"一边[^，。！？!?]{2,12}[，,][^，。！？!?]{0,12}一边"), "一边…一边"),
    (re.compile(r"无论[^，。！？!?]{2,14}都"), "无论…都"),
    (re.compile(r"只要[^，。！？!?]{2,14}就"), "只要…就"),
    (re.compile(r"越[^，。！？!?]{2,10}越"), "越…越"),
    (re.compile(r"不但[^，。！？!?]{2,12}而且"), "不但…而且"),
    (re.compile(r"不仅[^，。！？!?]{2,12}还"), "不仅…还"),
    (re.compile(r"要么[^，。！？!?]{2,12}要么"), "要么…要么"),
]

# D9 金句：否定式/对仗式"可截图句"
_RE_GOLDEN = [
    (re.compile(r"不是[^，。！？!?]{2,14}[，,]?而是"), "不是…而是"),
    (re.compile(r"没有[^，。！？!?]{2,14}[，,]?只有"), "没有…只有"),
    (re.compile(r"与其[^，。！？!?]{2,14}不如"), "与其…不如"),
    (re.compile(r"只要[^，。！？!?]{2,14}就"), "只要…就"),
    (re.compile(r"无论[^，。！？!?]{2,14}都"), "无论…都"),
    (re.compile(r"离不开[^，。！？!?]{2,12}也离不开"), "离不开…也离不开"),
]
# 对仗检测：一句一个逗号 / 分号，两半长度差 ≤1 且共享功能词
_RE_PAIR_SPLIT = re.compile(r"[，,;；]")
_FUNCTION_WORDS = set("要也都就还不是而才得没有一")
# ★ 数字锚（"3623 年"）**不算**金句 —— 那是罗臣臣的设定锚点（调研 §8 F33），
#   把它算进"金句密度"会误伤真人写法。

# D10 方言 / 网络梗 / 方言尾巴
_DIALECT_CHENCHEN = (
    "俺", "恁", "恁们", "嘞", "不中", "中不中", "木大木小", "排场",
    "啥", "哈", "呗", "老表", "整这", "搁这", "嘎", "嘞么", "呗儿")
# ★ 「整」「中」「宝」这类单字太泛（整个/当中/宝贝），不单列 ——
#   放进方言表会把普通书面词误判成方言尾巴。
_DIALECT_TAIL_BOD = (
    "干吗", "咋这么", "咋", "我寻思", "寻思", "忒实", "啥子",
    "安逸", "巴适", "装不装得下", "忒", "么子")
_MEMES = (
    "吊装", "林妹妹", "老己", "走近科学", "破防", "绝了",
    "yyds", "栓Q", "芭比Q", "泰裤辣", "泰酷辣", "我嘞个豆", "蚌埠住了",
    "显眼包", "小丑竟是我自己", "听我说谢谢你", "city不city", "尊嘟假嘟",
    "已老实求放过", "有被爱到", "偷感很重", "创飞了", "主理人", "绝绝子",
    "挖鼻", "整活", "种草", "摆烂", "躺平", "内卷", "emo了", "扎心了",
    "cpu", "嘴替", "发疯文学")

_RE_DIALECT_CHENCHEN = re.compile("|".join(re.escape(w) for w in _DIALECT_CHENCHEN))
_RE_DIALECT_TAIL_BOD = re.compile("|".join(re.escape(w) for w in _DIALECT_TAIL_BOD))
_RE_MEME = re.compile("|".join(re.escape(w) for w in _MEMES))

# D8 口语粗糙标记（语气词 / 方言 / 口头重复 / 自我更正）
_RE_TONE = re.compile(
    r"(啊|呀|吧|呗|嘛|呢|哈|哟|哟|咯|咧|嘞|哇|嘿|唔|啦|咧|么|嗷|哎|诶|嗯|"
    r"嘛儿|呗儿)")
_RE_SELF_FIX = re.compile(r"(不是不是|不对不对|说错了|搞错了|我是说|不是[，,]是|"
                          r"我是说|啊不是|不不不)")
_RE_REPEAT_CHAR = re.compile(r"(.)\1{1,}")
_RE_REPEAT_WORD = re.compile(r"([一-龥]{2})[，,、 ]?\1")

# 句子里没有的标点 → 说明是字幕/口语原样
_RE_TERMINAL = re.compile(r"[。！？!?]$")


# ──────────────────────── 工具函数 ────────────────────────────────────────────
def _clean(s: Any) -> str:
    return re.sub(r"\s", "", str(s or ""))


def _pure_len(s: Any) -> int:
    """只数「有效字」：汉字/字母/数字，标点与空白不算。"""
    return len(re.sub(r"[^一-龥A-Za-z0-9]", "", str(s or "")))


def _split_sentences(text: Any) -> List[str]:
    """切句。**保留句末标点** —— D3 的「标点单一」判据要看它。"""
    out: List[str] = []
    for chunk in re.split(r"[\r\n]+", str(text or "")):
        for s in re.split(r"(?<=[。！？!?…])", chunk):
            s = s.strip()
            if s:
                out.append(s)
    return out


def _genre_cfg(genre: Any) -> Dict[str, Any]:
    return _GENRE_CFG.get(str(genre or "drama").strip().lower(), _GENRE_CFG["drama"])


def _style_cfg(style: Any) -> Dict[str, Any]:
    return _STYLE_CFG.get(str(style or "auto").strip().lower(), _STYLE_CFG["auto"])


def _stdev(xs: List[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mean = sum(xs) / n
    return (sum((x - mean) ** 2 for x in xs) / n) ** 0.5


def _add(checks: List[Dict[str, Any]], cid: str, level: str, title: str,
         detail: str = "", evidence: str = "", line_index: int = -1) -> None:
    checks.append({"id": cid, "level": level, "title": title, "detail": detail,
                   "evidence": (evidence or "")[:200], "line_index": line_index})


def _norm_lines(lines: Any) -> List[Tuple[int, str, Dict[str, Any]]]:
    """把各种形态的 lines 规整成 [(原始下标, 文本, 原始 dict)]。"""
    out: List[Tuple[int, str, Dict[str, Any]]] = []
    for i, raw in enumerate(lines or []):
        if isinstance(raw, str):
            d: Dict[str, Any] = {"text": raw}
        elif isinstance(raw, dict):
            d = raw
        else:
            continue
        t = str(d.get("text") or "").strip()
        if t:
            out.append((i, t, d))
    return out


def _template_of(sent: str) -> Optional[str]:
    for rx, name in _PARALLEL_TEMPLATES:
        if rx.search(sent):
            return name
    return None


def _is_golden(sent: str) -> Optional[str]:
    for rx, name in _RE_GOLDEN:
        if rx.search(sent):
            return name
    # 对仗：一句话切成两半，长度接近且共享功能词
    parts = _RE_PAIR_SPLIT.split(sent)
    if len(parts) == 2:
        a, b = parts[0].strip(), parts[1].strip()
        la, lb = _pure_len(a), _pure_len(b)
        if 3 <= la <= 20 and abs(la - lb) <= 1:
            shared = _FUNCTION_WORDS & set(a) & set(b)
            if shared:
                return "对仗(%s)" % "".join(sorted(shared))
    return None


def _has_spoken_marker(text: str) -> bool:
    """这一句有没有「人味」的痕迹（语气词 / 方言 / 口头重复 / 自我更正）。"""
    t = str(text or "")
    if _RE_TONE.search(t) or _RE_DIALECT_CHENCHEN.search(t) \
            or _RE_DIALECT_TAIL_BOD.search(t):
        return True
    if _RE_SELF_FIX.search(t):
        return True
    if _RE_REPEAT_CHAR.search(t) or _RE_REPEAT_WORD.search(t):
        return True
    return False


# ──────────────────────── 主检查：audit_lines ─────────────────────────────────
def audit_lines(lines: Any, *, genre: str = "drama", style: str = "auto",
                target_duration: Optional[float] = None) -> Dict[str, Any]:
    """给一段台词做「AI 味体检」，并按宿主的尺子计分。

    `lines`: `[{"text": str, "character": str, "duration_seconds": float|None}, ...]`
      （也接受纯字符串列表）

    返回：
      `{"ok", "score", "errors", "warnings", "checks", "stats", "suggestions", "note"}`
      其中 `checks` 每条 = `{id, level, title, detail, evidence, line_index}`
      （`line_index` = 原始 lines 下标；整段级统计量的检查用 -1）
    """
    g = _genre_cfg(genre)
    sc = _style_cfg(style)
    checks: List[Dict[str, Any]] = []

    items = _norm_lines(lines)
    if not items:
        _add(checks, "D0", "error", "没有台词可检查",
             "lines 为空或全是空文本", "")
        return _finish(checks, {"lines": 0}, target_duration, genre, style)

    # ── 收集句级信息 ────────────────────────────────────────────────
    sent_items: List[Tuple[int, str]] = []      # (原始行下标, 句子)
    for idx, text, _d in items:
        for s_txt in _split_sentences(text):
            sent_items.append((idx, s_txt))

    sent_lens = [_pure_len(s) for _, s in sent_items]
    line_lens = [_pure_len(t) for _, t, _d in items]
    total_chars = sum(line_lens)
    n_sent = len(sent_items)
    n_lines = len(items)

    # ── D1 书面议论句 ────────────────────────────────────────────────
    d1_hits: List[str] = []
    d1_lines: List[int] = []
    for idx, text, _d in items:
        ms = _RE_D1_SIMILE.findall(text) + _RE_D1_CONN.findall(text) \
            + _RE_D1_SUMMARY.findall(text)
        if ms:
            d1_lines.append(idx)
            d1_hits.extend([str(m) for m in ms])
    if d1_hits:
        _add(checks, "D1", "error", "书面议论句",
             "书面比喻/抒情连接词或总结性议论：写出来像文章不像人说话",
             "、".join(sorted(set(d1_hits))[:6]) + " ｜ 行 " +
             "、".join(str(i) for i in d1_lines[:6]), d1_lines[0])

    # ── D2 解释性对白 ────────────────────────────────────────────────
    d2_hits: List[str] = []
    d2_lines: List[int] = []
    for idx, text, _d in items:
        ms = _RE_D2.findall(text)
        if ms:
            d2_lines.append(idx)
            d2_hits.extend([str(m) for m in ms])
    if d2_hits:
        _add(checks, "D2", "error", "解释性对白",
             "角色替观众解释刚发生的动作/情绪，观众看得见的不用再说一遍",
             "、".join(sorted(set(d2_hits))[:6]) + " ｜ 行 " +
             "、".join(str(i) for i in d2_lines[:6]), d2_lines[0])

    # ── D5 情绪直陈 ──────────────────────────────────────────────────
    d5_hits: List[str] = []
    d5_lines: List[int] = []
    d5_strong = 0
    for idx, text, _d in items:
        strong = _RE_D5_STRONG.findall(text) + _RE_D5_COPULA.findall(text) \
            + _RE_D5_TAIL.findall(text)
        weak = _RE_D5_WEAK.findall(text)
        name = _RE_D5_NAME.findall(text)
        if strong or weak or name:
            d5_lines.append(idx)
            d5_hits.extend([str(a) for a in strong] + [str(b) for b in weak])
            d5_strong += len(strong)
    if d5_hits:
        level = "error" if (len(d5_hits) >= 2 or d5_strong >= 1) else "warn"
        _add(checks, "D5", level, "情绪直陈",
             "把情绪词直接写出来；真人写法让动作承载情绪（罗臣臣：保留不完美，"
             "不靠华丽）",
             "、".join(sorted(set(d5_hits))[:6]) + " ｜ 行 " +
             "、".join(str(i) for i in d5_lines[:6]), d5_lines[0])

    # ── D6 万能过渡词 ────────────────────────────────────────────────
    d6_hits: List[str] = []
    d6_lines: List[int] = []
    for idx, text, _d in items:
        ms = _RE_D6.findall(text)
        if ms:
            d6_lines.append(idx)
            d6_hits.extend([str(m) for m in ms])
    if d6_hits:
        level = "error" if len(d6_hits) >= 3 else "warn"
        _add(checks, "D6", level, "万能过渡词",
             "然后/接着/与此同时… 这些是模型的呼吸声，真人说话不靠它们接句",
             "、".join(sorted(set(d6_hits))[:6]) + " ｜ 行 " +
             "、".join(str(i) for i in d6_lines[:6]), d6_lines[0])

    # ── D3 四平八稳（统计量）────────────────────────────────────────
    # 先算「口语痕迹覆盖率」—— D3 的「标点单一」必须配合「零口语痕迹」，
    # 否则任何每句都打句号的正常剧本都会误报。
    marker_lines = [idx for idx, t, _d in items if _has_spoken_marker(t)]
    marker_ratio = (len(marker_lines) / float(n_lines)) if n_lines else 0.0
    cv = 0.0
    sig_a = sig_b = sig_c = False
    if n_sent >= 2:
        mean_len = (sum(sent_lens) / n_sent) if n_sent else 0.0
        sd = _stdev([float(x) for x in sent_lens])
        cv = (sd / mean_len) if mean_len else 0.0
    # (a) 句长变异系数过小（需要 ≥6 句才有统计意义）
    if n_sent >= 6 and cv < float(g["cv_min"]) and \
            (sum(sent_lens) / max(1, n_sent)) >= 6:
        sig_a = True
    # (b) 标点单一：≥80% 的句子以同一个终止标点收尾，且全篇零口语痕迹
    term = [s[-1] for _, s in sent_items if _RE_TERMINAL.search(s)]
    if n_sent >= 4 and len(term) >= 0.8 * n_sent and len(set(term)) == 1 \
            and marker_ratio == 0.0:
        sig_b = True
    # (c) 逗号单一：≥80% 的句子恰好一个逗号，且逗号在中间
    with_comma = 0
    for _, s in sent_items:
        if s.count("，") + s.count(",") == 1:
            pos = s.find("，") if "，" in s else s.find(",")
            if 0 < pos < len(s):
                with_comma += 1
    if n_sent >= 4 and with_comma >= 0.8 * n_sent:
        sig_c = True
    n_sig = int(sig_a) + int(sig_b) + int(sig_c)
    if n_sig >= 2:
        _add(checks, "D3", "warn", "四平八稳（句长/标点分布过均匀）",
             "句长变异系数 CV=%.3f（阈值 %.2f）；标点单一=%s；逗号单一=%s。"
             "真人说话的句长是忽长忽短的"
             % (cv, float(g["cv_min"]), "是" if sig_b else "否",
                "是" if sig_c else "否"),
             "CV=%.3f，%d 句平均 %.1f 字；口语痕迹覆盖率 %.0f%%"
             % (cv, n_sent, (sum(sent_lens) / max(1, n_sent)),
                marker_ratio * 100),
             -1)

    # ── D4 排比/对仗成瘾 ────────────────────────────────────────────
    run = 1
    max_run = 1
    run_tpl = ""
    cur_tpl = ""
    for _, s in sent_items:
        t = _template_of(s)
        if t and t == cur_tpl:
            run += 1
        elif t:
            cur_tpl, run = t, 1
        else:
            cur_tpl, run = "", 1
        if run > max_run:
            max_run, run_tpl = run, t
    total_tpl: Dict[str, int] = {}
    for _, s in sent_items:
        t = _template_of(s)
        if t:
            total_tpl[t] = total_tpl.get(t, 0) + 1
    if max_run >= 3 or (total_tpl and max(total_tpl.values()) >= 4):
        worst = max(total_tpl.items(), key=lambda kv: kv[1]) if total_tpl else ("", 0)
        _add(checks, "D4", "warn", "排比/对仗成瘾",
             "连续 %d 句同构（%s）——模型写对仗的本能，真人说话不这么整齐"
             % (max_run, run_tpl or worst[0]),
             "模板 %s 共 %d 次" % (worst[0], worst[1]) if worst[0] else "",
             -1)

    # ── D7 句长超载（沿用 scriptaudit 的 4 字/秒 + 1.25 容差）─────────
    over_lines: List[str] = []
    over_idx: List[int] = []
    for idx, text, d in items:
        n = _pure_len(text)
        dur = None
        try:
            dur = float(d.get("duration_seconds") or 0) or None
        except (TypeError, ValueError):
            dur = None
        if dur:
            cap = max(6, int(dur * CHARS_PER_SECOND * LENGTH_TOLERANCE))
            if n > cap:
                over_lines.append("%d字/%gs（上限%d字）「%s」"
                                  % (n, dur, cap, _clean(text)[:10]))
                over_idx.append(idx)
        else:
            # 没有时长就用 4 字/秒估；单句超过 max_line_chars = 装不进 5 秒
            for s_txt in _split_sentences(text):
                ns = _pure_len(s_txt)
                if ns > int(g["max_line_chars"]):
                    over_lines.append("%d字/无时长（单句上限%d字）「%s」"
                                      % (ns, int(g["max_line_chars"]),
                                         _clean(s_txt)[:10]))
                    over_idx.append(idx)
                    break
    if over_lines:
        _add(checks, "D7", "error", "句长超载",
             "台词比它的时长能装下的更长（%.1f 字/秒 × %.2f 容差），"
             "成片会被迫变速" % (CHARS_PER_SECOND, LENGTH_TOLERANCE),
             "、".join(over_lines[:5]), over_idx[0] if over_idx else -1)

    # 总时长口径
    if target_duration:
        try:
            tgt = float(target_duration)
        except (TypeError, ValueError):
            tgt = 0.0
        if tgt > 0:
            cap_total = tgt * CHARS_PER_SECOND * LENGTH_TOLERANCE
            if total_chars > cap_total:
                _add(checks, "D7", "error", "总台词量超出目标时长",
                     "目标 %gs 最多装 %.0f 字，实测 %d 字"
                     % (tgt, cap_total, total_chars), "%d 字" % total_chars, -1)

    # ── D8 每句都完整 ────────────────────────────────────────────────
    complete = 0
    complete_idx: List[int] = []
    for idx, text, _d in items:
        if _RE_TERMINAL.search(text) and not _has_spoken_marker(text):
            complete += 1
            complete_idx.append(idx)
    complete_ratio = complete / float(n_lines) if n_lines else 0.0
    if n_lines >= 4 and complete_ratio >= 0.8:
        _add(checks, "D8", "warn", "每句都完整（缺口语省略）",
             "%d/%d 句都是完整句且没有语病/语气词/口头重复/自我更正。"
             "真人口语是不完整的：省略主语、说一半、重复、自己更正"
             % (complete, n_lines),
             "完整句：行 " + "、".join(str(i) for i in complete_idx[:8]), -1)

    # ── D9 金句密度失控 ──────────────────────────────────────────────
    golden: List[str] = []
    golden_idx: List[int] = []
    for idx, s_txt in sent_items:
        gk = _is_golden(s_txt)
        if gk:
            golden.append(gk)
            golden_idx.append(idx)
    allowed = max(int(g["golden_max"]), int(n_sent // int(g["golden_per_lines"])))
    density = (len(golden) / float(n_sent)) if n_sent else 0.0
    if len(golden) > allowed:
        _add(checks, "D9", "warn", "金句密度失控",
             "%d 句可截图金句 / %d 句 = 每20句%.1f 句（上限 %d 句）。"
             "金句是留白，不是每句都发"
             % (len(golden), n_sent, density * 20, allowed),
             "、".join(golden[:4]) + " ｜ 行 " +
             "、".join(str(i) for i in golden_idx[:6]), -1)

    # ── D10 方言 / 网络梗（按 style + genre 分档）────────────────────
    whole = "\n".join(t for _i, t, _d in items)
    n_meme = len(_RE_MEME.findall(whole))
    n_dialect = len(_RE_DIALECT_CHENCHEN.findall(whole))
    n_tail = len(_RE_DIALECT_TAIL_BOD.findall(whole))
    d10: List[Tuple[str, str, str]] = []      # (level, detail, evidence)
    if sc["meme_banned"] and n_meme:
        d10.append(("error",
                    "这条线禁网络梗，但检出 %d 处" % n_meme,
                    "、".join(sorted(set(_RE_MEME.findall(whole)))[:6])))
    if sc["meme_min"] is not None and n_meme < int(sc["meme_min"]):
        d10.append(("warn",
                    "一条 %d 句的戏里一个网络梗都没有（这类线每集要 2–3 个梗调剂）"
                    % n_lines, "检出 0 个"))
    if sc["meme_max"] is not None and n_meme > int(sc["meme_max"]):
        d10.append(("warn",
                    "网络梗 %d 个，超过上限 %d 个（梗要稀，不是越多越好）"
                    % (n_meme, int(sc["meme_max"])),
                    "、".join(sorted(set(_RE_MEME.findall(whole)))[:6])))
    if sc["dialect_required"] and n_dialect == 0:
        d10.append(("warn",
                    "全篇没有一个方言口语标记（这类线的主语体是方言口语："
                    "俺/恁/嘞/呗/哈/排场…）", "检出 0 个"))
    if sc["dialect_tail_max"] is not None and n_tail > int(sc["dialect_tail_max"]):
        d10.append(("warn",
                    "方言尾巴 %d 处，超过每片 %d 处上限"
                    % (n_tail, int(sc["dialect_tail_max"])),
                    "、".join(sorted(set(_RE_DIALECT_TAIL_BOD.findall(whole)))[:6])))
    if g["require_spoken"] and marker_ratio == 0.0:
        d10.append(("warn",
                    "全篇没有一处口语痕迹（语气词/方言/口头重复/自我更正都没有），"
                    "读起来像稿子不像人说话", "0 处"))
    for lvl, detail, ev in d10:
        _add(checks, "D10", lvl, "方言/网络梗缺席（%s）" % style,
             detail, ev, -1)

    # ── stats ────────────────────────────────────────────────────────
    stats: Dict[str, Any] = {
        "lines": n_lines,
        "sentences": n_sent,
        "chars": total_chars,
        "chars_avg": round(total_chars / float(n_lines), 2) if n_lines else 0,
        "chars_max": max(line_lens) if line_lens else 0,
        "sentence_len_avg": round(sum(sent_lens) / float(n_sent), 2) if n_sent else 0,
        "sentence_len_stdev": round(_stdev([float(x) for x in sent_lens]), 2),
        "sentence_len_cv": round(cv, 3),
        "cv_min": float(g["cv_min"]),
        "golden_sentences": len(golden),
        "golden_allowed": allowed,
        "golden_density": round(density, 3),
        "complete_ratio": round(complete_ratio, 2),
        "spoken_marker_ratio": round(marker_ratio, 2),
        "meme_marks": n_meme,
        "dialect_marks": n_dialect,
        "dialect_tail_marks": n_tail,
        "genre": str(genre or "drama"),
        "style": str(style or "auto"),
    }
    return _finish(checks, stats, target_duration, genre, style)


def _finish(checks: List[Dict[str, Any]], stats: Dict[str, Any],
            target_duration: Optional[float],
            genre: str = "drama", style: str = "auto") -> Dict[str, Any]:
    """计分口径照抄 `core/scriptaudit.py` 的 `_finish()`。"""
    errors = sum(1 for c in checks if c["level"] == "error")
    warnings = sum(1 for c in checks if c["level"] == "warn")
    score = max(0, 100 - errors * 15 - warnings * 5)
    return {
        "ok": errors == 0,
        "score": score,
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
        "stats": stats,
        "suggestions": _suggestions(checks, stats),
        "note": ("台词 AI 味体检：量法与计分沿用 core/scriptaudit.py"
                 "（%.1f 字/秒 × %.2f 容差；score=100-errors*15-warnings*5）；"
                 "写法口径来自罗臣臣（B站ASR字幕）与波导翁（B站whisper转写）"
                 "两份本机调研"
                 % (CHARS_PER_SECOND, LENGTH_TOLERANCE)),
        "valid_for_review": errors == 0,
        "target_duration": target_duration,
    }


def _suggestions(checks: List[Dict[str, Any]], stats: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for c in checks:
        cid = c["id"]
        if cid == "D0":
            out.append("没有台词可检查：先确认 lines 里每一项都有 text")
        elif cid == "D1":
            out.append("把书面比喻/议论框架换成口语：仿佛→好像、宛如→像、"
                       "这一切都是→说到底就（保留后半句的具体内容）")
        elif cid == "D2":
            out.append("删掉替观众解释的那半句，让动作承担："
                       "「你看你现在多伤心」→ 直接写她手上的动作")
        elif cid == "D3":
            out.append("打散句长：把最长的一句拆成两句短的，"
                       "短句和长句交替（CV 现在 %.3f，阈值 %.2f）"
                       % (stats.get("sentence_len_cv", 0), stats.get("cv_min", 0.2)))
        elif cid == "D4":
            out.append("同构模板只留一次，后面的改成平铺直叙（不是…而是 / 既…又）")
        elif cid == "D5":
            out.append("情绪词换成动作：很愤怒→把桌子一拍；很难过→话说到一半卡住了")
        elif cid == "D6":
            out.append("删掉过渡词，句子直接接上（然后/接着/于是乎）")
        elif cid == "D7":
            out.append("把超载的句子拆开或压短：一句话只装一件事（单句 ≤5 秒）")
        elif cid == "D8":
            out.append("留一点粗糙：语气词（呗/啊/嘛）、口头重复（我我我）、"
                       "或者一个自我更正（不是…是…）")
        elif cid == "D9":
            out.append("金句留 %d 句就够，其余换成朴素的话"
                       % int(stats.get("golden_allowed", 1)))
        elif cid == "D10":
            out.append(str(c["detail"]))
    if not out:
        out.append("没有检出 AI 味特征：句长起伏、口语省略、金句密度都在线上")
    return out


# ──────────────────────── 改写：suggest_rewrite ────────────────────────────────
_EMOTION_ACTION = {
    "愤怒": "把桌子一拍",
    "生气": "把门摔上了",
    "火大": "把门摔上了",
    "难过": "话说到一半卡住了",
    "伤心": "眼睛一直盯着地上",
    "失落": "站在那儿没动",
    "失望": "把手机扣在桌上",
    "委屈": "鼻子一酸",
    "害怕": "手一直捏着衣角",
    "紧张": "话都说不利索",
    "崩溃": "蹲下去了",
    "绝望": "坐在地上没起来",
    "孤独": "屋里没开灯",
    "开心": "笑出了声",
    "高兴": "把手机举高了一点",
    "感动": "眼睛一下红了",
    "兴奋": "话都变快了",
    "着急": "一直在搓手",
    "后悔": "手在裤缝上蹭了两下",
    "痛苦": "半天没说出话",
    "悲伤": "背过身去了",
    "心碎": "把东西收了回去",
    "难受": "喉咙像堵了东西",
    "揪心": "又看了一眼那扇门",
    "心痛": "捂住胸口没说话",
    "伤透了心": "把碗扣在桌上",
    "心如死灰": "坐在台阶上没起来",
    "百感交集": "张了张嘴没说出话",
    "五味杂陈": "把烟按灭了",
    "感慨万千": "站在窗户边看了很久",
}

# 情绪词按长度倒序，避免 "伤心" 抢在 "伤透了心" 前面
_EMO_ALT_SORTED = "|".join(sorted(_EMOTION_WORDS, key=len, reverse=True))
_RE_EMO_CLAUSE = re.compile(
    r"^(?P<cop>感到|感觉|觉得)?(?P<pre>也|都|还|就|又|而且|并且)?\s*"
    r"(?P<subj>我|你|他|她|咱|咱们|我们|你们|他们|她们)?\s*"
    r"(?P<adv>很|非常|十分|特别|真的|无比|极其|格外)?\s*"
    r"(?P<emo>%s)(?P<tail>了|极了|得很|得很|的很)?$" % _EMO_ALT_SORTED)

_RE_EMO_CLAUSE_STRONG = re.compile(
    r"^(?P<subj>我|你|他|她|咱|咱们|我们|你们|他们|她们)?\s*"
    r"(?P<adv>非常|十分|无比|极其|特别|真的很)?\s*"
    r"(?P<emo>%s)(?P<tail>了)?$" % _EMO_ALT_SORTED)

_SIMILE_MAP = {
    "仿佛": "好像", "彷佛": "好像", "宛如": "像", "犹如": "像",
    "恰似": "像", "恍若": "像", "无异于": "跟…不一样", "堪比": "跟…一样",
    "仿佛在诉说": "好像在说", "似乎在诉说": "像是在讲",
    "似乎都在诉说": "像是在讲", "仿佛都在诉说": "好像在说",
    "宛如在诉说": "像是在讲", "好像在诉说": "好像在说",
    "仿佛定格": "愣住了", "宛如画卷": "跟画似的", "犹如一幅": "跟一幅",
    "不禁": "忍不住", "不由自主": "忍不住",
    "此时此刻": "这会儿", "这一瞬间": "就这么一下", "在那一瞬间": "那会儿",
    "不知不觉中": "不知不觉", "在无声中": "没吭声", "空气仿佛": "空气好像",
    "时间仿佛": "时间好像",
}

_SUMMARY_MAP = {
    "总而言之": "说白了", "综上所述": "说白了", "由此可见": "看下来",
    "不得不说": "其实", "这一切不过": "不过", "这一切都": "都",
    "这一切": "",
}

_TRANSITION_MAP = {
    "然后": "", "接着": "那", "与此同时": "这会儿", "于是乎": "然后",
    "值得一提": "再说", "首先": "", "其次": "", "最后": "",
    "总的来说": "说到底", "除此之外": "另外", "不仅如此": "而且",
    "更重要的是": "要紧的是", "事实上": "其实", "实际上": "其实",
    "与此相反": "反过来",
}

# D2 → 改写成动作/删解释
_EXPLAIN_RULES: List[Tuple[re.Pattern, str, str]] = [
    (re.compile(r"你看你现在多(?:伤心|难过|生气|愤怒|可怜|惨)[，,]?"), "",
     "删掉替观众解释情绪的那半句，只留后半句"),
    (re.compile(r"你看你现在[^。！？!?]{0,8}[，,]?"), "",
     "「你看…」是模型腔，删掉"),
    (re.compile(r"你看看你这[^。！？!?]{0,8}[，,]?"), "",
     "「你看看…」是模型腔，删掉"),
    (re.compile(r"我刚才就是想说[，,]?"), "",
     "「我就是想说」是解释性开场，删掉"),
    (re.compile(r"也就是说[，,]?"), "就是", "把解释性框架换成口语连接词"),
    (re.compile(r"换句话说[，,]?"), "就是", "把解释性框架换成口语连接词"),
    (re.compile(r"这说明[，,]?"), "说明", "把解释性框架换成口语连接词"),
    (re.compile(r"这就叫"), "这就是", "把解释性框架换成口语连接词"),
    (re.compile(r"这意味着[，,]?"), "就是说", "把解释性框架换成口语连接词"),
    (re.compile(r"我告诉你(?:一个|个|你听|你)[，,]?"), "我跟你说",
     "「我告诉你」是模型腔，换成口语"),
    (re.compile(r"让我来告诉(?:你|大家)[，,]?"), "我跟你说",
     "「让我来告诉你」是模型腔，换成口语"),
    (re.compile(r"你别怪我没提醒(?:你)?[，,]?"), "我先跟你说",
     "改成口语提醒"),
    (re.compile(r"你不觉得[，,]?"), "你说", "改成口语反问"),
    (re.compile(r"刚才你(?:也)?(?:看到|听见|经历)[^。！？!?]{0,8}[，,]?"), "",
     "删掉复述观众已经看到的动作"),
    (re.compile(r"你要知道[，,]?"), "", "删掉解释性开场"),
    (re.compile(r"我这么跟你说的[，,]?"), "", "删掉解释性开场"),
    (re.compile(r"让我解释一下[，,]?"), "", "删掉解释性开场"),
]


def _rw_explain(text: str, changes: List[Dict[str, str]]) -> str:
    for rx, repl, why in _EXPLAIN_RULES:
        while True:
            m = rx.search(text)
            if not m:
                break
            old = m.group(0)
            if not old:
                break
            text = text[:m.start()] + repl + text[m.end():]
            changes.append({"from": old, "to": repl or "（删掉）", "why": why})
    return text


def _rw_emotion(text: str, changes: List[Dict[str, str]]) -> str:
    """把「我很愤怒」这种情绪直陈，换成承载情绪的动作。"""
    parts = re.split(r"([，,。！？!?；;、])", text)
    for i, part in enumerate(parts):
        if not part or part in "，,。！？!?；;、":
            continue
        m = _RE_EMO_CLAUSE.match(part)
        if not m:
            m2 = _RE_EMO_CLAUSE_STRONG.match(part)
            if not m2:
                continue
            m = m2
        emo = m.groupdict().get("emo") or ""
        if emo not in _EMOTION_ACTION:
            continue
        subj = m.groupdict().get("subj") or ""
        action = _EMOTION_ACTION[emo]
        new = (subj + action) if subj else action
        if new == part:
            continue
        changes.append({"from": part, "to": new,
                        "why": "情绪直陈 → 动作承载（罗臣臣写法：情绪靠动作，"
                               "不靠形容词）"})
        parts[i] = new
    return "".join(parts)


def _rw_wordmap(text: str, mapping: Dict[str, str],
                changes: List[Dict[str, str]], why: str) -> str:
    for src in sorted(mapping, key=len, reverse=True):
        dst = mapping[src]
        while src in text:
            idx = text.index(src)
            text = text[:idx] + dst + text[idx + len(src):]
            changes.append({"from": src, "to": dst or "（删掉）", "why": why})
    return text


def _rw_similitude(text: str, changes: List[Dict[str, str]]) -> str:
    return _rw_wordmap(text, _SIMILE_MAP, changes,
                       "书面比喻/连接词换成口语")


def _rw_summary(text: str, changes: List[Dict[str, str]]) -> str:
    return _rw_wordmap(text, _SUMMARY_MAP, changes,
                       "总结性议论框架换成口语框架")


def _rw_transition(text: str, changes: List[Dict[str, str]]) -> str:
    return _rw_wordmap(text, _TRANSITION_MAP, changes,
                       "万能过渡词删掉，句子直接接上")


def _rw_deparallel(text: str, changes: List[Dict[str, str]]) -> str:
    """同构模板只留第一次，后面的拆散（去对仗）。

    ★ 单独一句里的「不是…而是…」也算模板（真人不会这么说话），
      所以短句（≤2 句）时第一次也拆。
    """
    counts: Dict[str, int] = {}
    n_sent = len(_split_sentences(text))
    solo = n_sent <= 2

    def _hit(name: str) -> bool:
        counts[name] = counts.get(name, 0) + 1
        if counts[name] > 1:
            return True
        return solo and name in ("不是…而是", "与其…不如")

    # 不是…而是 → 拆成短问句 + 回答
    def _not_but(m: "re.Match") -> str:
        if not _hit("不是…而是"):
            return m.group(0)
        a, b = m.group(1), m.group(2)
        new = "%s？%s" % (a, b)
        changes.append({"from": m.group(0), "to": new,
                        "why": "同构模板拆散：不是…而是… → 短问句+回答，"
                               "去掉工整对仗"})
        return new

    text = re.sub(r"不是([^，。！？!?]{2,14})[，,]?而是([^，。！？!?]{2,14})",
                  _not_but, text)

    def _tpl(rx: "re.Pattern", name: str, build) -> str:
        nonlocal text
        def _repl(m: "re.Match") -> str:
            if not _hit(name):
                return m.group(0)
            new = build(m)
            changes.append({"from": m.group(0), "to": new,
                            "why": "同构模板拆散：%s 不连着用" % name})
            return new
        text = rx.sub(_repl, text)

    _tpl(re.compile(r"与其([^，。！？!?]{2,14})不如([^，。！？!?]{2,14})"),
         "与其…不如", lambda m: "别%s了，%s" % (m.group(1), m.group(2)))
    _tpl(re.compile(r"既([^，。！？!?]{2,12})[，,]([^，。！？!?]{0,12})又([^，。！？!?]{2,12})"),
         "既…又", lambda m: "%s，%s%s" % (m.group(1), m.group(2), m.group(3)))
    _tpl(re.compile(r"一边([^，。！？!?]{2,12})[，,]([^，。！？!?]{0,12})一边([^，。！？!?]{2,12})"),
         "一边…一边", lambda m: "%s，%s" % (m.group(1), m.group(3)))
    _tpl(re.compile(r"无论([^，。！？!?]{2,14})都([^，。！？!?]{2,14})"),
         "无论…都", lambda m: "%s，%s" % (m.group(1), m.group(2)))
    _tpl(re.compile(r"只要([^，。！？!?]{2,14})就([^，。！？!?]{2,14})"),
         "只要…就", lambda m: "%s，%s" % (m.group(1), m.group(2)))
    return text


def _rw_split_long(text: str, changes: List[Dict[str, str]],
                   max_chars: int) -> str:
    """太长的句子在逗号处拆开 —— 一句只装一件事。"""
    out = []
    pos = 0
    for s_txt in re.split(r"([。！？!?\n])", text):
        if _pure_len(s_txt) > max_chars and s_txt.count("，") + s_txt.count(",") >= 1:
            sep = "，" if "，" in s_txt else ","
            head, tail = s_txt.split(sep, 1)
            if _pure_len(head) >= 4 and _pure_len(tail) >= 2:
                new = "%s。%s" % (head, tail)
                changes.append({"from": s_txt, "to": new,
                                "why": "拆句：单句超过 %d 字就装不进 5 秒，"
                                       "一句只装一件事" % max_chars})
                out.append(new)
                pos += len(s_txt)
                continue
        out.append(s_txt)
    return "".join(out)


def _rw_roughness(text: str, changes: List[Dict[str, str]], style: str,
                  notes: List[str]) -> str:
    """留一点人味：语气词/方言尾巴（按 style 的配额）。"""
    if _has_spoken_marker(text):
        return text
    st = str(style or "auto").strip().lower()
    if st == "chenchen":
        tail = "啊" if ("？" in text or "?" in text or "吗" in text) else "呗"
        m = re.search(r"[。！？!?…]$", text)
        if m:
            new = text[:m.start()] + tail + text[m.start():]
        else:
            new = text + tail
        changes.append({"from": text, "to": new,
                        "why": "太工整了，加一个方言语气词留粗糙"
                               "（罗臣臣：俺/恁/嘞/呗/哈）"})
        return new
    if st == "bodaoweng":
        notes.append("这句太工整：可加一个方言尾巴（干吗/咋/整/我寻思/忒实），"
                     "但整片不超过 3 处")
    else:
        notes.append("这句太工整：可加一个语气词（呗/啊/嘛）或一个口头重复"
                     "（我我我），或一个自我更正（不是…是…）")
    return text


def suggest_rewrite(text: str, *, genre: str = "drama",
                    style: str = "auto") -> Dict[str, Any]:
    """给一句/一段 AI 味台词出**可替换的具体句子**。

    返回 `{"text": str, "changes": [{"from": str, "to": str, "why": str}],
    "flags": [...]}`。

    ★ 改写原则（罗臣臣做法）：**宁可保留一点真人写作的粗糙和不完整**，
      也不改成工整书面语 —— 所以这里只拆 AI 模板，不做「润色」。
      语病、口头重复、自我更正都不动。
    """
    raw = str(text or "")
    if not raw.strip():
        return {"text": raw, "changes": [], "flags": ["空文本"]}

    changes: List[Dict[str, str]] = []
    notes: List[str] = []
    out = raw
    out = _rw_explain(out, changes)
    out = _rw_emotion(out, changes)
    out = _rw_similitude(out, changes)
    out = _rw_summary(out, changes)
    out = _rw_transition(out, changes)
    out = _rw_deparallel(out, changes)
    out = _rw_split_long(out, changes, int(_genre_cfg(genre)["max_line_chars"]))
    out = _rw_roughness(out, changes, style, notes)

    # flags：这句本身命中的判据 id（机器可读）
    audit = audit_lines([{"text": raw}], genre=genre, style=style)
    flags: List[str] = []
    for c in audit["checks"]:
        tag = "%s %s" % (c["id"], c["title"])
        if tag not in flags:
            flags.append(tag)
    return {"text": out, "changes": changes, "flags": flags + notes}


# ════════════════════════════ 台词门：导演过带（director pass）════════════════════════
# ★ 2026-10-24 新增（任务卡 2026-10-24-015「台词门」）。**纯新增**：
#   不改上面任何函数的现有行为（audit_lines / suggest_rewrite / selftest 的结论不变）。
#
# 口径（老李 skill · dialogue-doctor-7d）：
#   基准语速 3.5~5 字/秒；单句 6~18 字；金句留 0.5 秒气口；写台词时同时标语气位。
#   现有 D7 只卡「>20 字」且只有上限 —— 这里补的是**下限 + 气口 + 语气位**三件事。
#
# 规则编号（对应任务卡「规则 1–7」）：
#   R1 单句 >18 字 → 在标点/气口处拆成尽量均匀的 2 句；只补/换标点，不删字不改字。
#   R2 单句 <6 字不合并（戏剧上短句合法）；只有「连续 ≥3 句都 <6 字」才报碎句。
#   R3 没给 duration_seconds：speak_seconds = chars/4.0、cps = 4.0；
#      给了：speak_seconds = duration、cps = chars/duration。
#   R4 cps > 5.0 → error（赶）；cps < 3.5 → warn（拖）。
#   R5 金句沿用 `_is_golden()`；金句 pause_after=0.5，非金句 0.2。
#   R6 tone 为空 → 从 text/emotion/delivery 推断一个具体语气并写回，报 warn TONE_MISSING。
#      ★ 推断出来的 tone 带 `tone_source="inferred"`：第二遍仍按「没有人工决定」处理
#        （这是 apply_director_pass 幂等的前提，也诚实：推断 ≠ 决定）。
#   R7 score = 100 - errors*15 - warnings*5（与 core/scriptaudit.py 同口径）。

LINE_MIN_CHARS = 6
LINE_MAX_CHARS = 18
CPS_MIN = 3.5
CPS_MAX = 5.0
GOLDEN_PAUSE_SECONDS = 0.5
NORMAL_PAUSE_SECONDS = 0.2          # 非金句的气口（任务卡 §规则 5）

# 拆句切点：句读标点（优先级 0）/ 气口虚词后（1）/ 兜底字间（2）
_SPLIT_PUNCT = "，,、；;：:。！？!?…"
_TERMINAL_PUNCT = "。！？!?…"
_GAP_PARTICLES = "的了着过吧啊嘛呢呀哦哈呗么"

# 语气位词表：只收「具体怎么说」，不收「开心/难过」这种情绪名词（那是 emotion）
_TONE_BY_EMOTION: Dict[str, str] = {
    "calm": "低声", "平静": "低声", "gentle": "低声", "温柔": "低声",
    "pity": "低声", "怜惜": "低声", "warm": "低声", "关切": "低声",
    "低": "低声", "低沉": "低声", "sad": "低声", "难过": "低声",
    "伤心": "低声", "疲惫": "叹着气说", "tired": "叹着气说",
    "confident": "压着说", "自信": "压着说", "坚定": "压着说",
    "cold": "压着说", "冷漠": "压着说", "冷淡": "压着说", "冰冷": "压着说",
    "regret": "压着说", "后悔": "压着说", "愧疚": "压着说",
    "fear": "压着说", "害怕": "压着说", "worried": "压着说", "serious": "压着说",
    "angry": "打断", "愤怒": "打断", "生气": "打断",
    "happy": "笑着说", "开心": "笑着说", "love": "笑着说",
    "得意": "笑着说", "嘲讽": "笑着说",
    "nervous": "脱口而出", "紧张": "脱口而出", "surprise": "脱口而出",
    "惊讶": "脱口而出", "excited": "脱口而出", "急切": "脱口而出",
    "curious": "追问", "疑惑": "追问",
    "无奈": "叹着气说",
    "neutral": "平平地说", "迷茫": "低声", "感激": "低声",
}
# delivery 关键词 → 语气位（delivery 是现有剧本里已有的表演提示，白送的语气线索）
_TONE_BY_DELIVERY: Tuple[Tuple[str, str], ...] = (
    ("断断续续", "断断续续"), ("低声", "低声"), ("自语", "自语"),
    ("气音", "压着说"), ("嘶", "压着说"), ("沙哑", "低声"),
    ("喊", "脱口而出"), ("笑", "笑着说"), ("叹", "叹着气说"),
    ("喃", "低声"), ("哈着白气", "低声"), ("慢半拍", "自语"),
)


def _num(v: Any) -> Optional[float]:
    """把 duration_seconds / scene_seconds 这类值转成正数；空/0/非法 → None。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _segments(text: Any) -> List[str]:
    """把一行台词切成句，并把只有标点的碎片（如「……」）并回相邻句。

    ★ 不另写切句器：沿用 `_split_sentences()`，只在它把「冻成这样了……」切成
      「冻成这样了…」+「…」这种零字碎片时并回去（合起来的字面与原串完全一致）。
    """
    out: List[str] = []
    for part in _split_sentences(text):
        if out and (_pure_len(out[-1]) == 0 or _pure_len(part) == 0):
            out[-1] = out[-1] + part
        else:
            out.append(part)
    return out


def _infer_tone_with_basis(text: Any,
                           line: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
    """R6：tone 为空时按「句形 → delivery → emotion → 兜底」推断语气位。

    返回 `(tone, 依据)`；依据只写进 warn 的 evidence，便于人复核。
    """
    t = str(text or "")
    if "？" in t or "?" in t:
        return "追问", "句形（问句）"
    if "！" in t or "!" in t:
        return "脱口而出", "句形（感叹句）"
    if "…" in t or "..." in t:
        return "断断续续", "句形（省略号）"
    d = line or {}
    delivery = str(d.get("delivery") or "")
    if delivery:
        for kw, tone in _TONE_BY_DELIVERY:
            if kw in delivery:
                return tone, "delivery 关键词「%s」" % kw
    emo = str(d.get("emotion") or "").strip().lower()
    if emo in _TONE_BY_EMOTION:
        return _TONE_BY_EMOTION[emo], "emotion=%s" % emo
    return "平平地说", "句形/兜底"


def _infer_tone(text: Any, line: Optional[Dict[str, Any]] = None) -> str:
    """只要语气位、不要依据。"""
    return _infer_tone_with_basis(text, line)[0]


def _tone_basis(text: Any, line: Optional[Dict[str, Any]] = None) -> str:
    """只要依据、不要语气位（TONE_MISSING 的 evidence 用）。"""
    return _infer_tone_with_basis(text, line)[1]


def _split_long_sentence(text: Any) -> Optional[Tuple[str, str, bool]]:
    """R1：把 >18 字的一句在标点/气口处拆成尽量均匀的 2 句。

    返回 `(左句, 右句, both_ok)`：

    · `both_ok=True`：两边都落在 6–18 字。左句补/换一个终止标点（「。」），
      写回后就是**两句**；
    · `both_ok=False`：一刀切不成两句都在 18 内（例如 37 字）。左句只在拆点
      补一个气口逗号（或保留原有标点），写回后是「一句长句 + 一个气口」，
      由 LINE_STILL_LONG 报出来。
    · 任何情况下**纯字序列都不变**（只动标点）；做不到就返回 None。
    """
    raw = str(text or "")
    if _pure_len(raw) <= LINE_MAX_CHARS:
        return None
    n = len(raw)
    cuts: List[Tuple[int, int]] = []        # (优先级, 切点)：0=标点 1=气口 2=兜底字间
    i = 0
    while i < n - 1:
        ch = raw[i]
        if ch in _SPLIT_PUNCT:
            j = i
            while j + 1 < n and raw[j + 1] in _SPLIT_PUNCT:
                j += 1
            if j + 1 < n:                   # 标点串（如「……」）之后还有内容
                cuts.append((0, j + 1))
            i = j + 1
            continue
        if ch in _GAP_PARTICLES:
            cuts.append((1, i + 1))
        i += 1
    for k in range(1, n):
        a, b = raw[k - 1], raw[k]
        if a in _SPLIT_PUNCT or b in _SPLIT_PUNCT:
            continue
        if re.match(r"[A-Za-z0-9]", a) and re.match(r"[A-Za-z0-9]", b):
            continue                        # 不切在英文/数字词中间
        cuts.append((2, k))
    if not cuts:
        return None

    pairs = [(pri, k, _pure_len(raw[:k]), _pure_len(raw[k:])) for pri, k in cuts]
    ps_pairs = [p for p in pairs if p[0] < 2]        # 标点 / 气口切点
    gap_pairs = [p for p in pairs if p[0] == 2]      # 兜底字间切点

    # ① 标点/气口里：两边都 6~18 字（尽量均匀）
    ok = sorted((abs(lp - rp), -max(lp, rp), pri, k)
                for pri, k, lp, rp in ps_pairs
                if LINE_MIN_CHARS <= lp <= LINE_MAX_CHARS
                and LINE_MIN_CHARS <= rp <= LINE_MAX_CHARS)
    if ok:
        return _finish_split(raw, ok[0][3], True)
    # ② 标点/气口里：两边都 ≥6 字，先让「最长那边」尽量短（均匀第二）
    alt = sorted((max(lp, rp), abs(lp - rp), pri, k)
                 for pri, k, lp, rp in ps_pairs
                 if lp >= LINE_MIN_CHARS and rp >= LINE_MIN_CHARS)
    if alt:
        return _finish_split(raw, alt[0][3], False)
    # ③ 连一个可用的标点/气口切点都没有（整句既无标点也无虚词）：
    #    这时才允许在字间切；优先两边都 6~18，再退到都 ≥6，最后只要都有字。
    for floor in (LINE_MIN_CHARS, 1):
        cand = sorted((max(lp, rp), abs(lp - rp), k)
                      for _pri, k, lp, rp in gap_pairs
                      if lp >= floor and rp >= floor)
        if cand:
            k = cand[0][2]
            both_ok = (LINE_MIN_CHARS <= cand[0][0] <= LINE_MAX_CHARS)
            return _finish_split(raw, k, both_ok)
    return None


def _finish_split(raw: str, k: int, both_ok: bool) -> Optional[Tuple[str, str, bool]]:
    """`_split_long_sentence` 的收尾：补/换标点，并做「不删字」防呆。"""
    left, right = raw[:k].rstrip(), raw[k:].lstrip()
    if not left or not right:
        return None
    if both_ok:
        if left[-1] in _TERMINAL_PUNCT:
            new_left = left
        elif left[-1] in _SPLIT_PUNCT:
            new_left = left[:-1] + "。"        # 换标点：逗号→句号（不增删字）
        else:
            new_left = left + "。"             # 补标点
    else:
        if left[-1] in _SPLIT_PUNCT:
            new_left = left
        else:
            new_left = left + "，"             # 补气口
    if _pure_len(new_left) + _pure_len(right) != _pure_len(raw):
        return None                            # 防呆：纯字序列变了就不拆
    return new_left, right, both_ok


def _director_stats(items: List[Dict[str, Any]], n_lines: int,
                    n_tone_inferred: int, n_over: int, frag_runs: int,
                    scene_seconds: Optional[float], genre: str,
                    style: str) -> Dict[str, Any]:
    n = len(items)
    chars_total = sum(int(it["chars"]) for it in items) if items else 0
    speaks = [float(it["speak_seconds"]) for it in items] if items else []
    cpss = [float(it["cps"]) for it in items] if items else []
    speak_total = round(sum(speaks), 3) if speaks else 0.0
    ss = _num(scene_seconds)
    return {
        "lines": n_lines,
        "sentences": n,
        "chars": chars_total,
        "chars_avg": round(chars_total / float(n), 2) if n else 0,
        "chars_min": min((int(it["chars"]) for it in items), default=0),
        "chars_max": max((int(it["chars"]) for it in items), default=0),
        "cps_avg": round(sum(cpss) / float(n), 3) if n else 0,
        "cps_min": min(cpss) if cpss else 0,
        "cps_max": max(cpss) if cpss else 0,
        "speak_seconds_total": speak_total,
        "pause_total_seconds": round(sum(float(it["pause_after"]) for it in items), 3)
        if items else 0.0,
        "golden_sentences": sum(1 for it in items if it["golden"]),
        "golden_pause_seconds": GOLDEN_PAUSE_SECONDS,
        "normal_pause_seconds": NORMAL_PAUSE_SECONDS,
        "tone_inferred": n_tone_inferred,
        "over_max_chars": n_over,
        "fragment_runs": frag_runs,
        "scene_seconds": ss,
        "speak_fill_ratio": round(speak_total / ss, 3) if ss else None,
        "line_min_chars": LINE_MIN_CHARS,
        "line_max_chars": LINE_MAX_CHARS,
        "cps_min_limit": CPS_MIN,
        "cps_max_limit": CPS_MAX,
        "genre": str(genre or "drama"),
        "style": str(style or "auto"),
        # 复用现有分档配置，只做透明披露，不改变本门的硬阈值（卡片固定 6/18/3.5/5.0）
        "genre_cfg_max_line_chars": int(_genre_cfg(genre)["max_line_chars"]),
        "style_cfg": dict(_style_cfg(style)),
    }


_DIRECTOR_SUGGEST: Dict[str, str] = {
    "CPS_FAST": "语速超过 5 字/秒：给足时长，或删掉冗余词（一句只装一件事）",
    "CPS_SLOW": "语速低于 3.5 字/秒：合并短句、删一句，或把时长压短",
    "FRAGMENT_RUN": "连续 ≥3 句都不到 6 字：短句合法但不能连成三天，"
                    "把其中一句并进上一句，或补一个动作给短句支点",
    "TONE_MISSING": "给每句补语气位（低声/打断/笑着说/压着说/脱口而出…）——"
                    "「没决定怎么说」就是 AI 味的来源",
    "LINE_STILL_LONG": "两刀还超 18 字：这句得再删内容，或按 3 句改写（本门只拆 2 句）",
    "SCENE_OVERFLOW": "台词合计超过场景时长：删句或加时长，成片才不会被迫变速",
    "NO_LINES": "没有台词：先确认 dialogues 每一项都有 text",
}
_REPORT_DEFAULT_SUGGEST = ("逐句语速落在 3.5–5 字/秒、单句 6–18 字、语气位齐全、"
                           "金句留 0.5 秒气口：这条台词可以过门")


def _director_suggestions(issues: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    seen = set()
    for it in issues:
        cid = str(it.get("id") or "")
        if cid in seen or cid not in _DIRECTOR_SUGGEST:
            continue
        seen.add(cid)
        out.append(_DIRECTOR_SUGGEST[cid])
    if not out:
        out.append(_REPORT_DEFAULT_SUGGEST)
    return out


def _run_director(lines: Any, *, genre: str, style: str,
                  scene_seconds: Optional[float]
                  ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """director_pass 的内核：返回 `(report, plans)`。

    plans 与 `_norm_lines()` 之后的输入行一一对应（供 apply 写回）：
    `{"text", "tone", "tone_source", "pause_after", "golden"}`。
    """
    raw = _norm_lines(lines)
    items: List[Dict[str, Any]] = []
    issues: List[Dict[str, Any]] = []
    plans: List[Dict[str, Any]] = []
    n_tone_inferred = 0
    n_over = 0
    frag_runs = 0

    for _idx, text, d in raw:
        segs = _segments(text)
        seg_total = sum(_pure_len(s) for s in segs)
        dur = _num(d.get("duration_seconds"))
        char = str(d.get("character") or "").strip()
        tone_in = str(d.get("tone") or "").strip()
        tone_src = str(d.get("tone_source") or "").strip().lower()
        authored = bool(tone_in) and tone_src != "inferred"

        pieces: List[Tuple[str, Optional[float]]] = []
        for seg in segs:
            n_seg = _pure_len(seg)
            seg_dur = (dur * n_seg / float(seg_total)) if (dur and seg_total) else None
            if n_seg > LINE_MAX_CHARS:
                two = _split_long_sentence(seg)
                if two is not None:
                    left, right, _both_ok = two
                    la, lb = _pure_len(left), _pure_len(right)
                    if seg_dur:
                        pieces.append((left, seg_dur * la / float(la + lb)))
                        pieces.append((right, seg_dur * lb / float(la + lb)))
                    else:
                        pieces.append((left, None))
                        pieces.append((right, None))
                    continue
            pieces.append((seg, seg_dur))

        line_texts: List[str] = []
        line_tone = ""
        line_pause = NORMAL_PAUSE_SECONDS
        line_golden = False
        for p_text, p_dur in pieces:
            chars = _pure_len(p_text)
            if chars == 0:
                speak = round(p_dur, 3) if p_dur else 0.0
                cps = 0.0
            elif p_dur:
                speak = round(p_dur, 3)
                cps = round(chars / p_dur, 3)
            else:
                # R3：没给时长 → 4 字/秒，cps 恒等于 CHARS_PER_SECOND（4.0）
                speak = round(chars / CHARS_PER_SECOND, 3)
                cps = round(CHARS_PER_SECOND, 3)
            golden = bool(_is_golden(p_text))
            pause = GOLDEN_PAUSE_SECONDS if golden else NORMAL_PAUSE_SECONDS
            tone = tone_in if authored else _infer_tone(p_text, d)

            one: List[Dict[str, str]] = []
            # R4 语速（只有真的算了 duration 才有 3.5~5 的判据；无时长 cps=4.0 恒过）
            if chars > 0 and cps > CPS_MAX:
                one.append({"id": "CPS_FAST", "level": "error",
                            "detail": "「%s」%d 字 / %.2f 秒 = %.2f 字/秒 > 上限 %.1f（赶）"
                                      % (_clean(p_text)[:14], chars, speak, cps, CPS_MAX)})
            elif chars > 0 and cps < CPS_MIN:
                one.append({"id": "CPS_SLOW", "level": "warn",
                            "detail": "「%s」%d 字 / %.2f 秒 = %.2f 字/秒 < 下限 %.1f（拖）"
                                      % (_clean(p_text)[:14], chars, speak, cps, CPS_MIN)})
            # R1 拆后仍超（或拆不动）
            if chars > LINE_MAX_CHARS:
                n_over += 1
                one.append({"id": "LINE_STILL_LONG", "level": "warn",
                            "detail": "「%s」%d 字，两刀之后仍超过 %d 字；"
                                      "本门只拆 2 句，再短要删内容或按 3 句改写"
                                      % (_clean(p_text)[:14], chars, LINE_MAX_CHARS)})
            # R6 语气位
            if not authored:
                n_tone_inferred += 1
                one.append({"id": "TONE_MISSING", "level": "warn",
                            "detail": "「%s」tone 为空，已按 %s 推断为「%s」；"
                                      "「没决定怎么说」本身就是 AI 味来源"
                                      % (_clean(p_text)[:14], _tone_basis(p_text, d), tone)})

            items.append({
                "character": char,
                "text": p_text,
                "chars": chars,
                "speak_seconds": speak,
                "cps": cps,
                "tone": tone,
                "golden": golden,
                "pause_after": pause,
                "issues": one,
            })
            issues.extend(one)
            line_texts.append(p_text)
            if not line_tone:
                line_tone = tone
            line_pause = pause
            line_golden = golden

        plans.append({"text": "".join(line_texts),
                      "tone": line_tone,
                      "tone_source": "" if authored else "inferred",
                      "pause_after": line_pause,
                      "golden": line_golden})

    if not raw:
        issues.append({"id": "NO_LINES", "level": "error",
                       "detail": "lines 为空或全是空文本：没有台词可过门"})

    # R2 碎句：连续 ≥3 句都 <6 字才报（单句短不报）
    run: List[Dict[str, Any]] = []
    for it in items + [{"chars": LINE_MIN_CHARS, "text": ""}]:
        if int(it["chars"]) < LINE_MIN_CHARS:
            run.append(it)
            continue
        if len(run) >= 3:
            frag_runs += 1
            issues.append({"id": "FRAGMENT_RUN", "level": "warn",
                           "detail": "连续 %d 句都 < %d 字：「%s」；短句合法，"
                                     "但不能连着三句以上都这么短"
                                     % (len(run), LINE_MIN_CHARS,
                                        "」「".join(_clean(x["text"])[:8] for x in run[:6]))})
        run = []

    # 场景时长给了就核对装得下装不下（scene_seconds 的唯一用途）
    speak_total = round(sum(float(it["speak_seconds"]) for it in items), 3)
    ss = _num(scene_seconds)
    if ss and speak_total > ss:
        issues.append({"id": "SCENE_OVERFLOW", "level": "warn",
                       "detail": "台词合计 %.2f 秒 > 场景 %.2f 秒：成片会被迫变速或截断"
                                 % (speak_total, ss)})

    stats = _director_stats(items, len(raw), n_tone_inferred, n_over,
                            frag_runs, scene_seconds, genre, style)
    # 计分复用现有 _finish()（score=100-errors*15-warnings*5，与 scriptaudit 同口径）
    checks = [{"id": i["id"], "level": i["level"], "detail": i["detail"]}
              for i in issues]
    base = _finish(checks, stats, None, genre, style)
    report: Dict[str, Any] = {
        "ok": bool(base["ok"]),
        "items": items,
        "score": int(base["score"]),
        "errors": int(base["errors"]),
        "warnings": int(base["warnings"]),
        "issues": issues,
        "stats": stats,
        "suggestions": _director_suggestions(issues),
    }
    return report, plans


def director_pass(lines: Any, *, genre: str = "drama", style: str = "auto",
                  scene_seconds: Optional[float] = None) -> Dict[str, Any]:
    """台词门主判据：逐句给 chars / cps / tone / pause_after / golden，并出报告。

    `lines`: `[{"character", "text", "emotion", "tone", "duration_seconds"}, ...]`
      （也接受纯字符串列表；现有剧本里的 `delivery` 会作为语气位推断的线索）

    返回（结构照任务卡）：
      `{"ok": bool, "items": [...], "score": int, "errors": int, "warnings": int,
        "issues": [...], "stats": {...}, "suggestions": [str]}`

    规则：>18 字拆成 2 句（只动标点）、碎句（连续 ≥3 句 <6 字）、cps>5 error /
    cps<3.5 warn、金句 pause_after=0.5（非金句 0.2）、语气位缺失推断并 warn。
    纯函数：不写文件、不连网、不碰数据库。
    """
    report, _plans = _run_director(lines, genre=genre, style=style,
                                   scene_seconds=scene_seconds)
    return report


def apply_director_pass(script: Dict[str, Any], *, genre: str = "drama",
                        style: str = "auto") -> Dict[str, Any]:
    """把台词门的「拆句 / 语气位 / 气口」写回剧本 dialogues，返回新剧本 + 报告。

    · 只改 `script["scenes"][*]["dialogues"][*]` 的 `text` / `tone`
      （另加 `pause_after` 气口；推断出来的 tone 带 `tone_source="inferred"`），
      **不动场景结构**；
    · 幂等：跑两遍 script 与 report 完全一致（推断的 tone 靠 `tone_source` 标记，
      第二遍重新推断而不是当成人工写好的；拆句结果第二遍已 ≤18 字不会二次拆）；
    · `dialogues` 为 None / 缺省 / 非 list 时原样保留；`scenes` 是 JSON 字符串时
      解析后处理再按字符串写回（兼容导出文件；正常链路 db.get_script 已是 list）。
    """
    out = copy.deepcopy(script or {})
    scenes = out.get("scenes")
    as_json = False
    if isinstance(scenes, str):
        try:
            scenes = json.loads(scenes)
        except Exception:
            scenes = []
        as_json = True
    if not isinstance(scenes, list):
        scenes = []
    out["scenes"] = scenes

    flat: List[Dict[str, Any]] = []
    slots: List[Tuple[int, int, Any]] = []     # (scene_idx, dialogue_idx, 原引用)
    total_seconds = 0.0
    for si, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            continue
        sv = _num(scene.get("duration_seconds"))
        if sv:
            total_seconds += sv
        dlg = scene.get("dialogues")
        if not isinstance(dlg, list):
            continue
        for di, d in enumerate(dlg):
            if isinstance(d, dict):
                txt = str(d.get("text") or "").strip()
                if not txt:
                    continue
                flat.append({
                    "character": d.get("character"),
                    "text": txt,
                    "emotion": d.get("emotion"),
                    "tone": d.get("tone"),
                    "tone_source": d.get("tone_source"),
                    "duration_seconds": d.get("duration_seconds"),
                    "delivery": d.get("delivery"),
                })
                slots.append((si, di, d))
            elif isinstance(d, str) and d.strip():
                flat.append({"character": "", "text": d.strip()})
                slots.append((si, di, d))

    scene_seconds = total_seconds if total_seconds > 0 else None
    report, plans = _run_director(flat, genre=genre, style=style,
                                  scene_seconds=scene_seconds)

    for (si, di, ref), plan in zip(slots, plans):
        dlg = scenes[si]["dialogues"]
        if isinstance(ref, dict):
            ref["text"] = plan["text"]
            ref["tone"] = plan["tone"]
            if plan["tone_source"] == "inferred":
                ref["tone_source"] = "inferred"
            else:
                ref.pop("tone_source", None)
            ref["pause_after"] = plan["pause_after"]
            dlg[di] = ref
        else:
            dlg[di] = plan["text"]

    report["stats"]["scenes"] = len(scenes)
    if as_json:
        out["scenes"] = json.dumps(scenes, ensure_ascii=False)
    return {"script": out, "report": report}


# ──────────────────────── 自测 ────────────────────────────────────────────────
# 「AI 味样例」：按模型常见写法构造（不是任何真人的作品）
_AI_LINES = [
    {"text": "我觉得你现在的状态非常糟糕，仿佛整个世界都崩塌了。",
     "character": "罗臣臣", "duration_seconds": 2.0},
    {"text": "这一切似乎都在诉说一个残酷的事实。",
     "character": "罗臣臣", "duration_seconds": 4.0},
    {"text": "你看你现在多伤心，这都怪我。",
     "character": "一一", "duration_seconds": 3.0},
    {"text": "然后我又想到了一件事。",
     "character": "一一", "duration_seconds": 2.5},
    {"text": "我很愤怒，也很难过。",
     "character": "罗臣臣", "duration_seconds": 2.0},
    {"text": "不是我们不懂生活，而是生活不懂我们。",
     "character": "罗臣臣", "duration_seconds": 4.0},
    {"text": "不是他不懂珍惜，而是他从未拥有过。",
     "character": "一一", "duration_seconds": 4.0},
    {"text": "不是风吹散了花，而是花本就留不住。",
     "character": "罗臣臣", "duration_seconds": 4.0},
    {"text": "总之，一切都会好起来的。",
     "character": "罗臣臣", "duration_seconds": 2.5},
]

# 「真人味样例」：罗臣臣《我的妹妹不可爱》B 站 ASR 字幕（字面或有偏差）
_HUMAN_LUOCHENCHEN = [
    "哎宝宝恁闺女的照片你带了吗",
    "带了带了宝宝恁闺女长得可真排场啊",
    "俺儿能多个这样的妹妹，算他祖坟上冒青烟了",
    "不是老邓",
    "你都几十岁的人了，还搁这整黄昏恋",
    "能不能让我省点心啊",
    "恁临沂嘞，刚上大学的闺女",
    "哎我小灵通咋没电了",
    "不中我不要嘛",
    "咱俩往后保持点距离吧",
    "你懂不可",
    "老表你别吊装了",
    "天上掉下个林妹妹",
    "走近科学都爆了吗",
]

# 「真人味样例」：波导翁（故事叔）B 站原片 whisper 转写（人工校正错字）
_HUMAN_BODAWENG = [
    "小时候特别崇拜我爸，因为他跑得特别快。",
    "爸，加油，爸，冲啊，第一名！",
    "能不能别管我。",
    "不就一辆车吗？以后我肯定买。",
    "你想要也得靠自己本事去挣。挣就挣。",
    "装得下装不下得看你会不会装。",
    "你赶紧把门带上。",
    "那指定，造我一个多月工资呢，心疼了。",
    "北京也算南方啊？",
    "这吃不完吗？",
    "咱以后日子长着呢。",
    "饭要一口一口吃，路也要一步步走。天上哪有掉下饼的事。",
]

# 「四平八稳样例」：句长几乎一样的模型腔
_UNIFORM_LINES = [
    {"text": "这件事让我感到非常失望。"},
    {"text": "我决定离开这个地方生活。"},
    {"text": "希望你们以后都能幸福。"},
    {"text": "我会记住今天的这个时刻。"},
    {"text": "我们终将在某处再次相见。"},
    {"text": "所有的事情都有它的意义。"},
    {"text": "我愿意接受这样的安排。"},
    {"text": "这就是我的最终决定。"},
]

# 同一段台词在两种 style 下结论必须不同
_STYLE_DIFF_LINES = [
    {"text": "妈，今儿个天忒好了，我寻思把被子晒晒呗。"},
    {"text": "你搁那儿整啥呢，干吗不去搭把手。"},
    {"text": "恁闺女真排场，俺看着都喜欢。"},
    {"text": "这事儿你别管，我咋弄用不着你教。"},
    {"text": "老表你别吊装了成不成，天上掉下个林妹妹啊。"},
]


def _ids(res: Dict[str, Any]) -> List[str]:
    return sorted({c["id"] for c in res["checks"]})


def selftest() -> Dict[str, Any]:
    """自带一组「AI 味样例」与「真人味样例」，跑一遍并判定。

    返回 `{"passed": bool, "cases": [...]}`。
    """
    cases: List[Dict[str, Any]] = []

    # ① AI 味样例 → 必须报出 ≥3 类问题
    r = audit_lines(_AI_LINES, genre="drama", style="auto")
    ids = _ids(r)
    cases.append({
        "case": "ai_flavored_drama",
        "desc": "模型腔样例（书面议论/解释性对白/情绪直陈/万能过渡词/排比/金句堆）",
        "genre": "drama", "style": "auto",
        "categories": len(ids), "check_ids": ids,
        "errors": r["errors"], "warnings": r["warnings"], "score": r["score"],
        "evidence": ["%s %s｜%s" % (c["id"], c["level"], c["evidence"][:60])
                     for c in r["checks"]],
        "expect": "≥3 类", "passed": len(ids) >= 3,
    })

    # ② 真人味样例（罗臣臣）→ 误报 ≤1 类
    r2 = audit_lines([{"text": t} for t in _HUMAN_LUOCHENCHEN],
                     genre="drama", style="chenchen")
    ids2 = _ids(r2)
    cases.append({
        "case": "human_luochenchen",
        "desc": "罗臣臣《我的妹妹不可爱》B站 ASR 字幕（字面或有偏差）",
        "genre": "drama", "style": "chenchen",
        "categories": len(ids2), "check_ids": ids2,
        "errors": r2["errors"], "warnings": r2["warnings"], "score": r2["score"],
        "expect": "≤1 类", "passed": len(ids2) <= 1,
    })

    # ③ 真人味样例（波导翁）→ 误报 ≤1 类
    r3 = audit_lines([{"text": t} for t in _HUMAN_BODAWENG],
                     genre="drama", style="bodaoweng")
    ids3 = _ids(r3)
    cases.append({
        "case": "human_bodaoweng",
        "desc": "波导翁（故事叔）B站原片 whisper 转写（人工校正错字）",
        "genre": "drama", "style": "bodaoweng",
        "categories": len(ids3), "check_ids": ids3,
        "errors": r3["errors"], "warnings": r3["warnings"], "score": r3["score"],
        "expect": "≤1 类", "passed": len(ids3) <= 1,
    })

    # ④ 四平八稳样例 → 必须命中统计量判据 D3
    r4 = audit_lines(_UNIFORM_LINES, genre="drama", style="auto")
    ids4 = _ids(r4)
    cases.append({
        "case": "uniform_sentence_length",
        "desc": "每句长度几乎一样的模型腔（用统计量判，不是关键词）",
        "genre": "drama", "style": "auto",
        "categories": len(ids4), "check_ids": ids4,
        "cv": r4["stats"].get("sentence_len_cv"),
        "expect": "必须命中 D3", "passed": "D3" in ids4,
    })

    # ⑤ 风格差异：同一段台词，两种 style 结论必须不同
    r5a = audit_lines(_STYLE_DIFF_LINES, genre="drama", style="chenchen")
    r5b = audit_lines(_STYLE_DIFF_LINES, genre="drama", style="bodaoweng")
    sig_a = sorted((c["id"], c["level"]) for c in r5a["checks"])
    sig_b = sorted((c["id"], c["level"]) for c in r5b["checks"])
    diff_ok = (sig_a != sig_b
               and any(c["id"] == "D10" and c["level"] == "error"
                       for c in r5b["checks"])
               and not any(c["id"] == "D10" and c["level"] == "error"
                           for c in r5a["checks"]))
    cases.append({
        "case": "style_difference",
        "desc": "同一段台词（方言尾巴 + 网络梗）在 chenchen / bodaoweng 下结论不同",
        "genre": "drama",
        "style_chenchen": {"checks": sig_a, "score": r5a["score"]},
        "style_bodaoweng": {"checks": sig_b, "score": r5b["score"]},
        "evidence_bodaoweng": [c["detail"] for c in r5b["checks"]
                               if c["id"] == "D10"],
        "expect": "两种 style 的判定不同（bodaoweng 禁梗报错）",
        "passed": diff_ok,
    })

    # ⑥ 改写：必须给出可替换的具体句子
    rw = suggest_rewrite("你看你现在多伤心，这都怪我。我很愤怒，也很难过。",
                          genre="drama", style="chenchen")
    rw_ok = (len(rw["changes"]) >= 1
             and all(c["from"] and c["to"] and c["why"]
                     for c in rw["changes"])
             and rw["text"] != "你看你现在多伤心，这都怪我。我很愤怒，也很难过。"
             and "你看你现在" not in rw["text"])
    cases.append({
        "case": "suggest_rewrite",
        "desc": "改写要给出可替换的具体句子，并保留口语的粗糙",
        "from": "你看你现在多伤心，这都怪我。我很愤怒，也很难过。",
        "to": rw["text"],
        "changes": rw["changes"],
        "flags": rw["flags"],
        "expect": "给出 from/to/why 三件套，且解释性对白被拆掉",
        "passed": rw_ok,
    })

    return {"passed": all(c["passed"] for c in cases), "cases": cases}


if __name__ == "__main__":  # pragma: no cover
    import json
    print(json.dumps(selftest(), ensure_ascii=False, indent=2))
