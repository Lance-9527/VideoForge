# -*- coding: utf-8 -*-
"""VideoForge · 导演门（把「节奏」变成**可报错的数字**）

★★ 为什么有这个文件（2026-10-24，Lance 拍板「全做」）：
    现有的门（`scriptaudit` / `dialogue_natural` / `gate_pacing` / `gate_seam`）
    都在管「写得好不好」「接得顺不顺」，**没有人管「节奏」**。
    节奏一直靠感觉，结果就是：`ab_gates_on.mp4` = 8 段 {3.425×4, 3.526×4}，
    distinct=2、同值占比 100%、max/median=1.01 —— 也就是一条**匀速幻灯片**，
    而所有门都是绿的。**能过门不等于好看，说明门没量到那件事。**

    本模块只吸收《策划导演大师》里**可计算**的判据（12 节拍打分那类主观项不进）：
      HOOK    00:00–00:05 必须有第一事件（首场有台词或动作）
      BUDGET  有台词片：台词念得完 / 没有长得离谱
      C13     同一分镜内 ≥4 片且同值时长占比 ≥70%（匀速片）
      C15     无台词片 >5s（观众在等）；整镜无台词且 >10s（空镜）
      T3      全片时长 distinct<3 / 同值占比 ≥70%（n≥6）/ 等分
      T4      同景别连用 >4（读不到景别就 skip，不猜）
      BREATH  max/median ≥1.5 且 min/median ≤0.7 才算「有长短」
      PACING  场景缺 pacing（plot/emotion/shot_density/size_focus）→ warn 不阻断

    **判据一律来自 skill 原文；本文件只把它们写成阈值常量并注明出处。**
    纯函数：不碰数据库 / 不碰网络 / 不读文件 / 不读时间 / 不 import 重依赖。
    （只 import 标准库 json/re/statistics/typing —— 故意不 import numpy。）

计分口径：`score = max(0, 100 - errors*15 - warnings*5)`，
与 `core/scriptaudit.py::_finish()`、`core/dialogue_natural.py::_finish()` 同一把尺子。
`ok = (errors == 0)`（= `valid_for_review`，与两个老门一致）；
`clean = (errors == 0 and warnings == 0)`（干净过门）。

**聚合口径**（重要，影响分数怎么读）：**一条判据一个 check**。
同一条判据打中多场/多片时，checks 里仍只有一条，最坏级别为准（error 盖 warn），
所有打中点都进 `detail` / `evidence` / `stats`。
所以 `errors + warnings` 是「坏了 N 条判据」，不是「坏了 N 处」。

对应关系（谁量什么）：
    audit_script  HOOK / BUDGET / T3 / BREATH / PACING / TOTAL
    audit_shots   HOOK / BUDGET / C13 / C15 / T3 / T4 / BREATH
    audit_cuts    C13 / T3 / BREATH（有 dialogue 字段时顺带 C15）
    line_budget   单片预算（BUDGET 的原子）
    duration_distribution  时长分布（T3/BREATH 的原子）
"""

from __future__ import annotations

import json
import re
import statistics
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


GATE_VERSION = "1.0.0"

__all__ = [
    "GATE_VERSION",
    "line_budget",
    "duration_distribution",
    "audit_script",
    "audit_shots",
    "audit_cuts",
    "selftest",
]


# ════════════════════════════════════════════════════════════════════════════
# 一、判据常量（改这些数字之前，请先回读 skill 原文重新核对，不要凭感觉调）
# ════════════════════════════════════════════════════════════════════════════

# ── HOOK：00:00–00:05 必须有第一事件 ─────────────────────────────────────────
# 出处：《策划导演大师》"前 5 秒必须发生一件事，否则观众划走"。
HOOK_WINDOW = 5.0                 # 秒：钩子窗口 [0, 5]
HOOK_EPS = 1e-6                   # 浮点容差

# ── BUDGET：有台词片的时间预算 ──────────────────────────────────────────────
# 出处：《策划导演大师》"台词预算" —— 慢 3.5 字/秒、快 5 字/秒（与
#   core/dialogue_natural.py 的 4.0 字/秒同源，这里取区间两端）。
# 公式（2026-10-24 按 Lead 修正后的边界，避免误杀正常长镜）：
#   need  = chars/5.0 + 0.3*句数 (+0.5 金句气口)      → 最快语速都念不完 = 塞不下
#   upper = chars/3.5 + 2.5                           → 长得很离谱才 warn
#   金句 +0.5s 只影响 need（气口让"念完"更慢），**不影响上限**。
#   上限给动作/留白 2.5s 余量：台词只占镜头前几秒、后面是动作/留白是合法的。
CPS_FAST = 5.0                    # 字/秒：最快语速（判定"塞不下"用）
CPS_SLOW = 3.5                    # 字/秒：慢速语速（估算台词本身占多久）
LINE_PAD_MIN = 0.3                # 秒/句：每句的起口/换气（下限侧）
LINE_PAD_MAX = 0.6                # 秒/句：每句的起口/换气（上限侧，仅 speech 参考量）
GOLDEN_PAD = 0.5                  # 秒：金句气口（只加到 need）
BUDGET_SLACK = 2.5                # 秒：动作/留白余量（只用于"拖太长"的上限）
BUDGET_EPS = 0.05                 # 秒：塞不下判定的容差
LONG_OK_INTENTS = ("ambient", "golden")   # 这两类片本来就该长，不报"拖太长"

# ── C13：同一分镜内匀速片 ───────────────────────────────────────────────────
C13_MIN_SEGMENTS = 4              # 片数下限：≥4 片才谈得上"匀速"
C13_SAME_RATIO = 0.70             # 同值时长占比阈值

# ── C15：无台词片 ───────────────────────────────────────────────────────────
C15_NODIA_SEG_MAX = 5.0           # 秒：无台词片 >5s → warn（观众在等）
C15_NODIA_SHOT_MAX = 10.0         # 秒：整镜无台词且 >10s → error（空镜）

# ── T3：全片时长分布 ────────────────────────────────────────────────────────
T3_MIN_DISTINCT = 3               # distinct < 3 → warn
T3_SAME_RATIO = 0.70              # 同值占比 ≥70% → warn
T3_SAME_RATIO_MIN_N = 6           # 但样本数要 ≥6 才有统计意义

# ── T4：同景别连用 ──────────────────────────────────────────────────────────
T4_SAME_SIZE_MAX = 4              # 连用 >4 → warn（读不到景别就 skip）

# ── BREATH：长短呼吸 ────────────────────────────────────────────────────────
BREATH_LONG = 1.5                 # max/median ≥ 1.5 → 有长镜
BREATH_SHORT = 0.7                # min/median ≤ 0.7 → 有短镜

# ── PACING：场景节奏字段 ────────────────────────────────────────────────────
PACING_KEYS = ("plot", "emotion", "shot_density", "size_focus")

# ── TOTAL：场景时长合计 vs 目标总时长 ───────────────────────────────────────
TOTAL_TOLERANCE_SECONDS = 0.05   # exact 策略只容许小于一帧量级的浮点/编码舍入

# 分档（pacing profile）。★ 目前两档同值：阈值来自 skill，不按体裁私自放宽；
# 留这个 dict 是为了让阈值集中在一处、将来按体裁分档时只改这里。
# 未知档位回落到 promo，并在 stats["pacing_fallback"] 里记下原值（不静默）。
PACING_PROFILES: Dict[str, Dict[str, Any]] = {
    "promo": {
        "hook_window": HOOK_WINDOW,
        "c13_min_segments": C13_MIN_SEGMENTS,
        "c13_same_ratio": C13_SAME_RATIO,
        "c15_seg_max": C15_NODIA_SEG_MAX,
        "c15_shot_max": C15_NODIA_SHOT_MAX,
        "t3_min_distinct": T3_MIN_DISTINCT,
        "t3_same_ratio": T3_SAME_RATIO,
        "t3_same_ratio_min_n": T3_SAME_RATIO_MIN_N,
        "t4_same_size_max": T4_SAME_SIZE_MAX,
        "breath_long": BREATH_LONG,
        "breath_short": BREATH_SHORT,
        "budget_slack": BUDGET_SLACK,
        "budget_eps": BUDGET_EPS,
        "golden_pad": GOLDEN_PAD,
    },
    "drama": {
        "hook_window": HOOK_WINDOW,
        "c13_min_segments": C13_MIN_SEGMENTS,
        "c13_same_ratio": C13_SAME_RATIO,
        "c15_seg_max": C15_NODIA_SEG_MAX,
        "c15_shot_max": C15_NODIA_SHOT_MAX,
        "t3_min_distinct": T3_MIN_DISTINCT,
        "t3_same_ratio": T3_SAME_RATIO,
        "t3_same_ratio_min_n": T3_SAME_RATIO_MIN_N,
        "t4_same_size_max": T4_SAME_SIZE_MAX,
        "breath_long": BREATH_LONG,
        "breath_short": BREATH_SHORT,
        "budget_slack": BUDGET_SLACK,
        "budget_eps": BUDGET_EPS,
        "golden_pad": GOLDEN_PAD,
    },
}
DEFAULT_PACING = "promo"


# ════════════════════════════════════════════════════════════════════════════
# 二、小工具（数值/文本/结构化）
# ════════════════════════════════════════════════════════════════════════════

def _f(v: Any, default: float = 0.0) -> float:
    """尽量变成 float；变成不了给 default（None/''/'abc' 都不炸）。"""
    try:
        if v is None or v == "":
            return float(default)
        x = float(v)
        if x != x or x in (float("inf"), float("-inf")):   # NaN / inf
            return float(default)
        return x
    except (TypeError, ValueError):
        return float(default)


def _clean(s: Any) -> str:
    """去掉所有空白（换行/全角空格都算），用于拼文本。"""
    return re.sub(r"\s", "", str(s or ""))


def _pure_len(s: Any) -> int:
    """只数「有效字」：汉字/字母/数字；标点、空白、省略号都不算。

    与 `core/dialogue_natural.py::_pure_len` 同一口径 ——
    「做片子，难的不是拍。」= 8 字（Lead 核对过：8/3.5=2.29）。
    """
    return len(re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", str(s or "")))


def _fmt(x: Any, nd: int = 3) -> str:
    """给人看的数字（3 位小数，去掉多余的 0）。"""
    try:
        return ("%." + str(nd) + "f") % float(x)
    except (TypeError, ValueError):
        return str(x)


def _median(xs: Sequence[float]) -> float:
    return float(statistics.median(xs)) if xs else 0.0


def _as_list(v: Any) -> List[Any]:
    """list 就 list；str 尝试当 JSON 解析；其它给 []。"""
    if isinstance(v, list):
        return v
    if isinstance(v, tuple):
        return list(v)
    if isinstance(v, str) and v.strip():
        try:
            got = json.loads(v)
        except (ValueError, TypeError):
            return []
        return got if isinstance(got, list) else []
    return []


def _strs_of(v: Any) -> List[str]:
    """把一个字段值规整成「文本行」列表。

    ★ 这里踩过坑：现场 `layer2_timeline` 的每一片是
      `{"action": "农夫挑着柴担…", "dialogue": null}` —— `action` 是**字符串**。
      一开始用 `_as_list()`（把 str 当 JSON 解析）去接它，纯中文串不是 JSON → 返回 []，
      于是 4 个镜头全被判成「没有第一事件」，HOOK 误报 error。
      所以字符串**直接**当文本，不要走 JSON 解析。
    """
    if isinstance(v, str):
        t = _clean(v)
        return [t] if t else []
    if isinstance(v, dict):
        t = _clean(v.get("text"))
        return [t] if t else []
    if isinstance(v, (list, tuple)):
        out: List[str] = []
        for x in v:
            t = _clean(x.get("text") if isinstance(x, dict) else x)
            if t:
                out.append(t)
        return out
    return []


def _texts_of(obj: Any) -> List[str]:
    """取出 obj 里所有能读到的「文本行」（dialogues / actions / text / action）。

    兼容三种现场形态（实测 `shots.layer2_timeline` 是 JSON 字符串）：
      - dict: {"dialogues":[{"text"}], "actions":[...]}
      - dict: {"dialogue": {...}|'...'|None, "action": '...'}   ← 现场就是这种
      - str:  直接当一段文本
    """
    if isinstance(obj, str):
        t = _clean(obj)
        return [t] if t else []
    if not isinstance(obj, dict):
        return []
    out: List[str] = []
    for key in ("dialogues", "lines", "dialogue"):
        out.extend(_strs_of(obj.get(key)))
    for key in ("actions", "action", "action_text"):
        out.extend(_strs_of(obj.get(key)))
    return out


def _dialogue_texts(obj: Any) -> List[str]:
    """只要台词文本（不要动作）。空字符串算「没台词」。"""
    if isinstance(obj, str):
        t = _clean(obj)
        return [t] if t else []
    if not isinstance(obj, dict):
        return []
    out: List[str] = []
    for key in ("dialogues", "lines", "dialogue", "dialogue_text"):
        out.extend(_strs_of(obj.get(key)))
    return out


def _action_texts(obj: Any) -> List[str]:
    """只要动作/画面文本（HOOK 判"有没有第一事件"用）。"""
    if isinstance(obj, str):
        t = _clean(obj)
        return [t] if t else []
    if not isinstance(obj, dict):
        return []
    out: List[str] = []
    for key in ("actions", "action", "action_text", "expression"):
        out.extend(_strs_of(obj.get(key)))
    return out


def _same_group(vals: Sequence[float]) -> Tuple[Optional[float], float, int]:
    """返回 (出现最多的那个时长值, 它的占比, 它的片数)。

    用 round(v, 3) 分组 —— 3.4250001 与 3.425 是同一个值（成片切点由帧算出）。
    """
    if not vals:
        return None, 0.0, 0
    counts: Dict[float, int] = {}
    for v in vals:
        k = round(float(v), 3)
        counts[k] = counts.get(k, 0) + 1
    best = max(counts.items(), key=lambda kv: (kv[1], -kv[0]))
    return best[0], best[1] / float(len(vals)), best[1]


def _shot_size_of(obj: Any) -> str:
    """读景别。**只读字段，不从文本推**（推了就是猜，T4 宁可 skip）。"""
    if not isinstance(obj, dict):
        return ""
    for key in ("shot_size", "shot_size_zh", "size"):
        v = obj.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    return ""


def _profile(pacing: Any) -> Tuple[str, Dict[str, Any]]:
    """取阈值档位；未知档回落 promo 并保留原值（不静默）。"""
    raw = str(pacing or DEFAULT_PACING).strip().lower()
    prof = PACING_PROFILES.get(raw)
    if prof is None:
        return DEFAULT_PACING, dict(PACING_PROFILES[DEFAULT_PACING], _fallback_from=raw)
    return raw, prof


# ── check 构造 ───────────────────────────────────────────────────────────────
def _mk(cid: str, level: str, title: str, detail: str = "",
        evidence: str = "") -> Dict[str, Any]:
    return {"id": cid, "level": level, "title": title,
            "detail": (detail or "")[:600], "evidence": (evidence or "")[:600]}


def _skip(cid: str, title: str, detail: str) -> Dict[str, Any]:
    """「读不到，不猜」也进 checks —— 但 level=ok，只是把 skip 写在 detail 里。"""
    return _mk(cid, "ok", title, "skip：" + detail, "")


def _suggestions(checks: Sequence[Dict[str, Any]], stats: Dict[str, Any]) -> List[str]:
    """把每条没过的判据翻译成一句能照着改的话（和 dialogue_natural 同风格）。"""
    out: List[str] = []
    for c in checks:
        cid, lvl = c["id"], c["level"]
        if lvl == "ok":
            continue
        if cid == "HOOK":
            out.append("把第一个事件（一句台词或一个动作）压进 00:00–00:05："
                       "开场空镜/前摇超过 5 秒就往后挪，或者砍掉")
        elif cid == "BUDGET":
            if lvl == "error":
                out.append("有台词片塞不下：照着最快 5 字/秒也念不完 —— "
                           "删字、拆到前后片，或者延长这一片的预算")
            else:
                out.append("有台词片拖得太长（台词只需要几秒，片却长得多）："
                           "把动作/留白写进这一段，或把台词补厚；"
                           "intent=ambient/golden 的片本来就该长，标上就不报")
        elif cid == "C13":
            out.append("同一分镜里有 ≥4 片时长同值：打散成不同长度"
                       "（3.5/4/4/3.5 这种），别让每片都 3.425")
        elif cid == "C15":
            out.append("无台词片超过 5 秒会让观众等：加一句话/环境声/动作，或者切短；"
                       "整镜超过 10 秒又没台词就是空镜，必须拆")
        elif cid == "T3":
            out.append("全片时长只有 %s 个不同值：让切点有长有短，别等分"
                       % stats.get("distinct_durations", stats.get("distinct", "?")))
        elif cid == "T4":
            out.append("同景别连用太多：中间插入特写/全景，别一个景别用到尾")
        elif cid == "BREATH":
            out.append("没有长短呼吸（max/median=%s）：至少留一个长镜（≥1.5×中位）"
                       "和一个短镜（≤0.7×中位）"
                       % _fmt(stats.get("max_over_median", 0), 2))
        elif cid == "PACING":
            out.append("给每场补 pacing：{plot, emotion, shot_density, size_focus}"
                       "（不阻断，但导演门读不到意图就没法判断节奏）")
        elif cid == "TOTAL":
            out.append("场景时长合计与目标总时长差得太多：按目标重排每场时长")
    return out


def _finish(checks: List[Dict[str, Any]], stats: Dict[str, Any],
            *, pacing: str = DEFAULT_PACING, note: str = "") -> Dict[str, Any]:
    """统一收口：计分口径照抄 core/scriptaudit.py::_finish()。"""
    errors = sum(1 for c in checks if c["level"] == "error")
    warnings = sum(1 for c in checks if c["level"] == "warn")
    score = max(0, 100 - errors * 15 - warnings * 5)
    return {
        "ok": errors == 0,                      # 硬门：和 scriptaudit/dialogue_natural 一致
        "clean": errors == 0 and warnings == 0,  # 干净过门（warn 也要清）
        "valid_for_review": errors == 0,
        "score": score,
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
        "stats": stats,
        "suggestions": _suggestions(checks, stats),
        "version": GATE_VERSION,
        "pacing": pacing,
        "note": note or ("导演门 v%s：节奏判据（HOOK/BUDGET/C13/C15/T3/T4/BREATH/PACING），"
                         "score=100-errors*15-warnings*5（与 scriptaudit/dialogue_natural 同口径）"
                         % GATE_VERSION),
    }


# ════════════════════════════════════════════════════════════════════════════
# 三、line_budget：单片台词预算（BUDGET 的原子）
# ════════════════════════════════════════════════════════════════════════════

def line_budget(chars: int, *, golden: bool = False, lines: int = 1) -> Dict[str, Any]:
    """一个「时间片」的台词时间预算。

    参数：
      chars   有效字数（`_pure_len` 口径：汉字/字母/数字，标点不算）
      golden  是不是金句（+0.5s 气口，**只加在 need 上**）
      lines   句数（默认 1；多句传进来，多句的起口/换气会累加）

    返回：
      need    最短需要多久（= chars/5 + 0.3*lines + 金句 0.5）
      upper   「拖太长」的上限（= chars/3.5 + 2.5，给动作/留白让路）
      speech  台词本身念完的理论时长（= chars/3.5 + 0.6*lines，仅参考，不判错）
      以及全部阈值/出处，方便前端把数字摊开给人看。

    ★ 2026-10-24 Lead 修正：**不要**用 chars/3.5+0.6*句数 硬卡上限。
      skill 原文只把「台词所需时长 > 该片预算」判超时，没把「镜头比台词长」判错。
      所以：塞不下用 need（最快语速都念不完），拖太长用 chars/3.5+2.5。
    """
    n_chars = max(0, int(chars or 0))
    n_lines = max(0, int(lines if lines is not None else 1))
    need = n_chars / CPS_FAST + LINE_PAD_MIN * n_lines
    if golden:
        need += GOLDEN_PAD
    upper = n_chars / CPS_SLOW + BUDGET_SLACK
    speech = n_chars / CPS_SLOW + LINE_PAD_MAX * n_lines
    return {
        "chars": n_chars,
        "lines": n_lines,
        "golden": bool(golden),
        "need": round(need, 3),
        "upper": round(upper, 3),
        "speech": round(speech, 3),
        "cps_fast": CPS_FAST,
        "cps_slow": CPS_SLOW,
        "line_pad_min": LINE_PAD_MIN,
        "line_pad_max": LINE_PAD_MAX,
        "golden_pad": GOLDEN_PAD if golden else 0.0,
        "slack": BUDGET_SLACK,
        "eps": BUDGET_EPS,
        "formula": "need=chars/5+0.3*lines(+0.5 金句); upper=chars/3.5+2.5",
        "basis": ("台词预算：慢 3.5 字/秒、快 5 字/秒（与 core/dialogue_natural 的 "
                  "4.0 字/秒同源）；2.5s 余量给动作/留白（Lead 2026-10-24 修正边界）"),
    }


# ════════════════════════════════════════════════════════════════════════════
# 四、duration_distribution：时长分布（T3 / BREATH 的原子）
# ════════════════════════════════════════════════════════════════════════════

def duration_distribution(durations: Any, *, min_distinct: int = T3_MIN_DISTINCT,
                          same_ratio_max: float = T3_SAME_RATIO,
                          long_ratio: float = BREATH_LONG,
                          short_ratio: float = BREATH_SHORT) -> Dict[str, Any]:
    """量一组时长（场景时长 / 镜头时长 / 成片切点）的「平不平」。

    返回字段：
      flat        T3 视角的「太平」：distinct < min_distinct
                  或（n ≥ 6 且同值占比 ≥ same_ratio_max）
      equal_split 等分（distinct == 1）
      breathing   有长短呼吸：max/median ≥ long_ratio **且** min/median ≤ short_ratio
      ok          = (not flat) and breathing
      reasons     人话版原因（[] = 完全健康）
    """
    vals: List[float] = []
    for d in _as_list(durations) if not isinstance(durations, (int, float)) else [durations]:
        if isinstance(d, dict):
            d = d.get("duration", d.get("duration_seconds"))
        x = _f(d, -1.0)
        if x >= 0:
            vals.append(round(x, 3))

    n = len(vals)
    if n == 0:
        return {"ok": False, "n": 0, "durations": [], "total": 0.0, "median": 0.0,
                "mean": 0.0, "min": 0.0, "max": 0.0, "distinct": 0,
                "same_value": None, "same_ratio": 0.0, "max_over_median": 0.0,
                "min_over_median": 0.0, "flat": True, "equal_split": False,
                "breathing": False, "reasons": ["没有时长可量（n=0）"],
                "thresholds": {"min_distinct": min_distinct,
                               "same_ratio_max": same_ratio_max,
                               "long_ratio": long_ratio, "short_ratio": short_ratio}}

    med = _median(vals)
    mx, mn = max(vals), min(vals)
    same_value, same_ratio, same_count = _same_group(vals)
    distinct = len({round(v, 3) for v in vals})
    max_over = round(mx / med, 3) if med > 0 else 0.0
    min_over = round(mn / med, 3) if med > 0 else 0.0

    flat_reasons: List[str] = []
    if n >= min_distinct:
        if distinct < min_distinct:
            flat_reasons.append("distinct=%d < %d（全片只有 %d 个不同时长）"
                                % (distinct, min_distinct, distinct))
    elif n >= 2:
        # 样本数 < min_distinct 时不可能有 min_distinct 个不同值（2 场戏最多 2 个值），
        # 硬套「distinct<3 就 warn」会让每个两场剧本恒 warn —— 门就失去信息量。
        # 这个区间只查「完全等分」，并且把口径写进 detail（不静默放宽）。
        if distinct == 1:
            flat_reasons.append("完全等分：%d 个时长都是 %s（n=%d < %d，只查等分）"
                                % (n, _fmt(vals[0], 3), n, min_distinct))
    if n >= T3_SAME_RATIO_MIN_N and same_ratio >= same_ratio_max:
        flat_reasons.append("同值时长占比 %s ≥ %s（n=%d，%s×%d）"
                            % (_fmt(same_ratio, 3), _fmt(same_ratio_max, 2),
                               n, _fmt(same_value, 3), same_count))
    flat = bool(flat_reasons)
    equal_split = distinct == 1

    breath_reasons: List[str] = []
    if max_over < long_ratio:
        breath_reasons.append("max/median=%s < %s：没有长镜"
                              % (_fmt(max_over, 3), _fmt(long_ratio, 2)))
    if min_over > short_ratio:
        breath_reasons.append("min/median=%s > %s：没有短镜"
                              % (_fmt(min_over, 3), _fmt(short_ratio, 2)))
    breathing = not breath_reasons

    return {
        "ok": (not flat) and breathing,
        "n": n,
        "durations": vals,
        "total": round(sum(vals), 3),
        "median": round(med, 3),
        "mean": round(sum(vals) / n, 3),
        "min": round(mn, 3),
        "max": round(mx, 3),
        "distinct": distinct,
        "same_value": same_value,
        "same_count": same_count,
        "same_ratio": round(same_ratio, 3),
        "max_over_median": max_over,
        "min_over_median": min_over,
        "flat": flat,
        "flat_reasons": flat_reasons,
        "equal_split": equal_split,
        "breathing": breathing,
        "breath_reasons": breath_reasons,
        "reasons": flat_reasons + breath_reasons,
        "thresholds": {"min_distinct": min_distinct,
                       "same_ratio_max": same_ratio_max,
                       "long_ratio": long_ratio,
                       "short_ratio": short_ratio,
                       "same_ratio_min_n": T3_SAME_RATIO_MIN_N},
    }


# ════════════════════════════════════════════════════════════════════════════
# 五、结构归一化（把现场各种形态喂成统一形状）
# ════════════════════════════════════════════════════════════════════════════

def _scenes_of(script: Any) -> Tuple[List[Dict[str, Any]], List[str]]:
    """从各种形态的 script 里取出 scenes。返回 (scenes, 解析问题列表)。

    兼容：
      {"scenes": [...]} / {"scenes": "<JSON 字符串>"} / {"outline": "<JSON 字符串>"}
      / 直接一个 list
    实测 `scripts.json` 的 scenes 字段就是 JSON 字符串（必须解析）。
    """
    problems: List[str] = []
    doc: Any = script
    if isinstance(doc, list):
        return [s for s in doc if isinstance(s, dict)], problems
    if not isinstance(doc, dict):
        return [], ["script 不是 dict/list（拿到 %s）" % type(script).__name__]

    raw = doc.get("scenes")
    if not raw:
        out = doc.get("outline")
        if isinstance(out, str) and out.strip():
            try:
                parsed = json.loads(out)
            except (ValueError, TypeError) as e:
                problems.append("outline 解析失败：%s" % e)
                parsed = None
            if isinstance(parsed, dict):
                raw = parsed.get("scenes")
            elif isinstance(parsed, list):
                raw = parsed
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError) as e:
            problems.append("scenes 是字符串但解析失败：%s" % e)
            raw = []
    scenes = [s for s in _as_list(raw) if isinstance(s, dict)]
    if not scenes:
        problems.append("没有读到任何场景（scenes 为空）")
    return scenes, problems


def _scene_duration(sc: Dict[str, Any]) -> float:
    for key in ("duration_seconds", "duration", "dur"):
        if sc.get(key) is not None:
            return max(0.0, _f(sc.get(key)))
    return 0.0


def _segments_of(shot: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """取出 shot 的 layer2_timeline（现场是 JSON 字符串）。返回 (segs, 问题)。"""
    raw = shot.get("layer2_timeline")
    if raw is None:
        raw = shot.get("segments")
    if raw is None:
        return [], None
    if isinstance(raw, str):
        if not raw.strip():
            return [], None
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError) as e:
            return [], "layer2_timeline 解析失败：%s" % e
    if isinstance(raw, dict):
        raw = raw.get("segments") or []
    segs = [s for s in _as_list(raw) if isinstance(s, dict)]
    return segs, None


def _seg_duration(seg: Dict[str, Any], fallback: float = 0.0) -> float:
    """一片的时长：优先 duration，其次 end-start，最后用 fallback。"""
    if seg.get("duration") is not None:
        return max(0.0, _f(seg.get("duration")))
    if seg.get("duration_seconds") is not None:
        return max(0.0, _f(seg.get("duration_seconds")))
    if seg.get("start") is not None and seg.get("end") is not None:
        return max(0.0, _f(seg.get("end")) - _f(seg.get("start")))
    return max(0.0, _f(fallback))


def _seg_start(seg: Dict[str, Any], fallback: float = 0.0) -> float:
    if seg.get("start") is not None:
        return max(0.0, _f(seg.get("start")))
    return max(0.0, _f(fallback))


# ════════════════════════════════════════════════════════════════════════════
# 六、audit_script：剧本侧的节奏门
# ════════════════════════════════════════════════════════════════════════════

def audit_script(script: Any, *, pacing: str = "promo",
                 total: Optional[float] = None) -> Dict[str, Any]:
    """剧本节奏门：HOOK / BUDGET / T3 / BREATH / PACING（+ 可选 TOTAL）。

    script：`{"scenes":[{scene_number,duration_seconds,dialogues,actions,
             beat,pacing,golden_line,intent}]}`（scenes 可以是 JSON 字符串）
    total：目标总时长（可选）；给了就顺带量「每场时长合计 vs 目标」。
    """
    pkey, prof = _profile(pacing)
    checks: List[Dict[str, Any]] = []
    scenes, problems = _scenes_of(script)
    stats: Dict[str, Any] = {
        "scenes": len(scenes),
        "problems": problems,
        "pacing": pkey,
        "pacing_fallback": prof.get("_fallback_from"),
        "target_total": total,
    }
    if not scenes:
        checks.append(_mk("SCENE", "error", "没有场景可量",
                          "；".join(problems) or "scenes 为空", ""))
        return _finish(checks, stats, pacing=pkey,
                       note="导演门：剧本侧节奏（没有输入 → 无法判断）")

    durs = [_scene_duration(sc) for sc in scenes]
    starts: List[float] = []
    acc = 0.0
    for d in durs:
        starts.append(acc)
        acc += d

    # ── HOOK：00:00–00:05 必须有第一事件 ────────────────────────────────────
    window = float(prof["hook_window"])
    first_event_idx: Optional[int] = None
    for i, sc in enumerate(scenes):
        if _dialogue_texts(sc) or _action_texts(sc):
            first_event_idx = i
            break
    if first_event_idx is None:
        checks.append(_mk("HOOK", "error", "全片没有第一事件",
                          "所有场景既没有台词也没有动作 —— 前 5 秒（乃至全片）什么都没发生",
                          "scenes=%d" % len(scenes)))
        stats["hook_first_event_at"] = None
    else:
        first_at = starts[first_event_idx]
        stats["hook_first_event_at"] = round(first_at, 3)
        stats["hook_first_event_scene"] = scenes[first_event_idx].get(
            "scene_number", first_event_idx + 1)
        if first_at >= window - HOOK_EPS:
            if first_event_idx > 0 and _scene_duration(scenes[0]) > window:
                detail = ("首场 %.2fs > %.0fs 且没有事件（台词/动作都没有），"
                          "第一事件被挤到 %.2fs" % (_scene_duration(scenes[0]),
                                                     window, first_at))
            else:
                detail = "第一事件在 %.2fs，已经出了 00:00–%.0fs 的钩子窗口" % (
                    first_at, window)
            checks.append(_mk("HOOK", "error", "钩子被挤到 5 秒之后", detail,
                              "首事件场次=%s；窗口=[0, %.0f]s"
                              % (stats["hook_first_event_scene"], window)))
        else:
            checks.append(_mk("HOOK", "ok", "钩子在 00:00–%.0fs 内" % window,
                              "第一事件发生在 %.2fs（第 %s 场）"
                              % (first_at, stats["hook_first_event_scene"]),
                              "窗口=[0, %.0f]s" % window))

    # ── BUDGET：有台词片塞不下 / 拖太长 ─────────────────────────────────────
    slack = float(prof["budget_slack"])
    eps = float(prof["budget_eps"])
    golden_pad = float(prof["golden_pad"])
    b_err: List[str] = []
    b_warn: List[str] = []
    budget_rows: List[Dict[str, Any]] = []
    n_dialogue_scenes = 0
    n_dialogue_lines = 0
    n_dialogue_chars = 0
    for i, sc in enumerate(scenes):
        texts = _dialogue_texts(sc)
        if not texts:
            continue
        n_dialogue_scenes += 1
        n_lines = len(texts)
        chars = sum(_pure_len(t) for t in texts)
        n_dialogue_lines += n_lines
        n_dialogue_chars += chars
        golden = bool(sc.get("golden_line"))
        intent = str(sc.get("intent") or "").strip().lower()
        dur = durs[i]
        need = chars / CPS_FAST + LINE_PAD_MIN * n_lines + (golden_pad if golden else 0.0)
        upper = chars / CPS_SLOW + slack
        dead = "%s 场 %.2fs / %d 字 %d 句%s" % (
            sc.get("scene_number", i + 1), dur, chars, n_lines,
            "（金句）" if golden else "")
        budget_rows.append({"scene": sc.get("scene_number", i + 1),
                            "duration": round(dur, 3), "chars": chars,
                            "lines": n_lines, "need": round(need, 3),
                            "upper": round(upper, 3), "intent": intent or None,
                            "golden": golden})
        if dur + eps < need:
            b_err.append("%s：最快 5 字/秒也要 %.2fs，塞不下" % (dead, need))
        elif dur > upper and intent not in LONG_OK_INTENTS \
                and not (sc.get("actions") or []):
            b_warn.append("%s：上限 %.2fs（台词 %.2fs + 余量 %.1fs），拖太长"
                          % (dead, upper, chars / CPS_SLOW, slack))
    stats["budget"] = budget_rows
    if b_err:
        checks.append(_mk("BUDGET", "error", "有台词片塞不下", "；".join(b_err[:6]),
                          "offenders=%d" % len(b_err)))
    elif b_warn:
        checks.append(_mk("BUDGET", "warn", "有台词片拖得太长",
                          "；".join(b_warn[:6]),
                          "offenders=%d；余量 %.1fs，intent∈%s 可豁免"
                          % (len(b_warn), slack, "|".join(LONG_OK_INTENTS))))
    elif n_dialogue_scenes:
        checks.append(_mk("BUDGET", "ok", "台词预算都在区间内",
                          "%d 场有台词（%d 字 / %d 句），全部落在 need…upper 之间"
                          % (n_dialogue_scenes, n_dialogue_chars, n_dialogue_lines),
                          "余量=%.1fs" % slack))
    else:
        checks.append(_skip("BUDGET", "没有有台词片", "全片没有台词 → 不需要量台词预算"))

    # ── T3：场景时长分布 ─────────────────────────────��──────────────────────
    feasible_variety = min(int(prof["t3_min_distinct"]), len(durs))
    try:
        from core.scriptplan import prompt_duration_contract
        duration_contract = prompt_duration_contract(round(sum(durs)), len(durs), pacing=pacing)
        if duration_contract["scene_count"] == len(durs):
            feasible_variety = min(feasible_variety, duration_contract["variety_target"])
    except (ValueError, TypeError):
        # Malformed/oversized input remains subject to the ordinary audit rather than receiving an exemption.
        duration_contract = None
    dd = duration_distribution(durs, min_distinct=max(1, feasible_variety),
                               same_ratio_max=float(prof["t3_same_ratio"]),
                               long_ratio=float(prof["breath_long"]),
                               short_ratio=float(prof["breath_short"]))
    stats.update({k: dd[k] for k in ("total", "median", "min", "max", "distinct",
                                     "same_value", "same_ratio", "max_over_median",
                                     "min_over_median", "equal_split")})
    if dd["n"] < 2 or feasible_variety <= 1:
        checks.append(_skip("T3", "当前场数或时长边界不要求变化",
                            "%d 个场景；当前可行秒数变化目标=%d" % (dd["n"], feasible_variety)))
    elif dd["flat"]:
        checks.append(_mk("T3", "warn", "场景时长太平（T3）",
                          "；".join(dd["flat_reasons"]),
                          "durations=%s" % _fmt_durs(durs)))
    else:
        checks.append(_mk("T3", "ok", "场景时长有变化（T3）",
                          "distinct=%d，同值占比 %s"
                          % (dd["distinct"], _fmt(dd["same_ratio"], 3)),
                          "durations=%s" % _fmt_durs(durs)))

    # ── BREATH：长短呼吸 ────────────────────────────────────────────────────
    if dd["n"] < 2 or feasible_variety <= 1:
        checks.append(_skip("BREATH", "当前场数或固定时长边界不量呼吸",
                            "%d 个场景；不以不可行的长短变化扣分" % dd["n"]))
    elif dd["breathing"]:
        checks.append(_mk("BREATH", "ok", "有长短呼吸",
                          "max/median=%s（≥%s），min/median=%s（≤%s）"
                          % (_fmt(dd["max_over_median"], 3), _fmt(prof["breath_long"], 2),
                             _fmt(dd["min_over_median"], 3), _fmt(prof["breath_short"], 2)),
                          "median=%.2fs max=%.2fs min=%.2fs"
                          % (dd["median"], dd["max"], dd["min"])))
    else:
        checks.append(_mk("BREATH", "warn", "没有长短呼吸",
                          "；".join(dd["breath_reasons"]),
                          "median=%.2fs max=%.2fs min=%.2fs"
                          % (dd["median"], dd["max"], dd["min"])))

    # ── PACING：场景节奏字段 ────────────────────────────────────────────────
    miss: List[str] = []
    miss_detail: List[str] = []
    for i, sc in enumerate(scenes):
        pac = sc.get("pacing")
        label = "第%s场" % sc.get("scene_number", i + 1)
        if isinstance(pac, dict):
            gone = [k for k in PACING_KEYS if pac.get(k) in (None, "", [])]
            if gone:
                miss.append(label)
                miss_detail.append("%s 缺 %s" % (label, "/".join(gone)))
        else:
            miss.append(label)
            miss_detail.append("%s pacing 整个没有" % label)
    stats["pacing_missing"] = miss
    if miss:
        checks.append(_mk("PACING", "warn", "有场景没写 pacing（不阻断）",
                          "%d/%d 场缺节奏字段；%s"
                          % (len(miss), len(scenes), "；".join(miss_detail[:6])),
                          "需要字段：%s" % "/".join(PACING_KEYS)))
    else:
        checks.append(_mk("PACING", "ok", "每场都写了 pacing",
                          "%d 场全有 %s" % (len(scenes), "/".join(PACING_KEYS)), ""))

    # ── TOTAL：可选的总时长核对 ─────────────────────────────────────────────
    sum_dur = dd["total"]
    if total is not None:
        tgt = _f(total)
        if tgt > 0:
            dev = abs(sum_dur - tgt) / tgt
            stats["total_deviation"] = round(dev, 4)
            stats["duration_policy"] = "exact"
            if abs(sum_dur - tgt) > TOTAL_TOLERANCE_SECONDS:
                checks.append(_mk("TOTAL", "warn", "场景时长合计不等于目标",
                                  "合计 %.2fs，目标 %.2fs，偏差 %.1f%%（舍入容差 %.2fs）"
                                  % (sum_dur, tgt, dev * 100, TOTAL_TOLERANCE_SECONDS),
                                  ""))
            else:
                checks.append(_mk("TOTAL", "ok", "场景时长合计符合目标",
                                  "合计 %.2fs，目标 %.2fs，偏差 %.1f%%"
                                  % (sum_dur, tgt, dev * 100), ""))

    return _finish(checks, stats, pacing=pkey)


def _fmt_durs(durs: Sequence[float], limit: int = 12) -> str:
    """时长列表 → 人看得懂的一行（长列表截断）。"""
    head = ", ".join("%.2f" % d for d in list(durs)[:limit])
    return "[" + head + (", …（%d 个）" % len(durs) if len(durs) > limit else "") + "]"


# ════════════════════════════════════════════════════════════════════════════
# 七、audit_shots：分镜侧的节奏门
# ════════════════════════════════════════════════════════════════════════════

def audit_shots(shots: Any, *, pacing: str = "promo") -> Dict[str, Any]:
    """分镜节奏门：HOOK / BUDGET / C13 / C15 / T3 / T4 / BREATH。

    shots：`[{id,order_index,duration_seconds,layer2_timeline,shot_size}]`
    layer2_timeline：JSON 字符串或 list；每片 `{start,end,dialogue,shot_size,action}`。
    """
    pkey, prof = _profile(pacing)
    checks: List[Dict[str, Any]] = []
    raw_shots = _as_list(shots)
    rows = [s for s in raw_shots if isinstance(s, dict)]
    if not rows:
        stats = {"shots": 0, "pacing": pkey, "pacing_fallback": prof.get("_fallback_from")}
        checks.append(_mk("SHOT", "error", "没有分镜可量", "shots 为空或不是 dict 列表", ""))
        return _finish(checks, stats, pacing=pkey,
                       note="导演门：分镜侧节奏（没有输入 → 无法判断）")

    # 按 order_index 排（现场 id 是 uuid，顺序只能看 order_index）
    rows.sort(key=lambda s: (_f(s.get("order_index"), 1e9), str(s.get("id") or "")))

    parse_errors: List[str] = []
    mismatch: List[Dict[str, Any]] = []
    shot_durs: List[float] = []
    seg_rows: List[Dict[str, Any]] = []      # 全片所有片，扁平化
    for si, sh in enumerate(rows):
        shot_id = str(sh.get("id") or ("#%d" % si))
        segs, err = _segments_of(sh)
        if err:
            parse_errors.append("%s：%s" % (shot_id[:8], err))
        declared = max(0.0, _f(sh.get("duration_seconds")))
        fb = (declared / len(segs)) if segs and declared > 0 else 0.0
        acc = 0.0
        seg_durs: List[float] = []
        for k, seg in enumerate(segs):
            d = _seg_duration(seg, fb)
            st = _seg_start(seg, acc)
            seg_durs.append(d)
            seg_rows.append({
                "shot_index": si, "shot_id": shot_id,
                "seg_index": k, "start": round(st, 3), "duration": round(d, 3),
                "dialogue": bool(_dialogue_texts(seg)),
                "shot_size": _shot_size_of(seg),
                "has_event": bool(_dialogue_texts(seg) or _action_texts(seg)),
            })
            acc = max(acc + (d if d > 0 else 0.0), st + d)
        seg_sum = sum(seg_durs)
        total_dur = declared if declared > 0 else seg_sum
        shot_durs.append(round(total_dur, 3))
        if segs and declared > 0 and abs(seg_sum - declared) > 0.5:
            mismatch.append({"shot": shot_id[:8], "declared": round(declared, 3),
                             "segments_sum": round(seg_sum, 3),
                             "delta": round(seg_sum - declared, 3)})
        rows[si]["_seg_durs"] = seg_durs

    # ── HOOK：全片绝对时间轴上，第一事件是否落在 5s 内 ──────────────────────
    window = float(prof["hook_window"])
    cursor = 0.0
    first_event_at: Optional[float] = None
    for si, sh in enumerate(rows):
        segs, _ = _segments_of(sh)
        declared = max(0.0, _f(sh.get("duration_seconds")))
        acc = 0.0
        for seg in segs:
            st = _seg_start(seg, acc)
            abs_at = cursor + st
            if first_event_at is None and (_dialogue_texts(seg) or _action_texts(seg)):
                first_event_at = abs_at
            acc = max(acc + _seg_duration(seg), st + _seg_duration(seg))
        # 镜头本身可能只有动作（没有 layer2 时看 shot 级文本）
        if first_event_at is None and segs == []:
            if _dialogue_texts(sh) or _action_texts(sh):
                first_event_at = cursor
        cursor += declared if declared > 0 else acc
    stats: Dict[str, Any] = {
        "shots": len(rows),
        "segments": len(seg_rows),
        "pacing": pkey,
        "pacing_fallback": prof.get("_fallback_from"),
        "parse_errors": parse_errors,
        "duration_mismatches": mismatch,
        "shot_durations": shot_durs,
    }
    if first_event_at is None:
        if not seg_rows:
            # 读不到 ≠ 没有：现场有几条测试分镜的 layer2_timeline 就是 '[]'。
            # 按「读不到就 skip，不猜」处理，不误判成"全片没有第一事件"。
            checks.append(_skip("HOOK", "没有时间片，读不到第一事件",
                                "所有镜头的 layer2_timeline 都为空/解析不出片 → 不猜"))
        else:
            checks.append(_mk("HOOK", "error", "全片没有第一事件",
                              "所有分镜的时间片里既没有台词也没有动作", ""))
    elif first_event_at >= window - HOOK_EPS:
        checks.append(_mk("HOOK", "error", "钩子被挤到 5 秒之后",
                          "第一事件在 %.2fs，已经出了 00:00–%.0fs 的窗口"
                          % (first_event_at, window), ""))
    else:
        checks.append(_mk("HOOK", "ok", "钩子在 00:00–%.0fs 内" % window,
                          "第一事件发生在 %.2fs" % first_event_at, ""))
    stats["hook_first_event_at"] = None if first_event_at is None else round(first_event_at, 3)

    # ── BUDGET：逐片量（片 = 时间片；片里有台词才算）────────────────────────
    slack = float(prof["budget_slack"])
    eps = float(prof["budget_eps"])
    golden_pad = float(prof["golden_pad"])
    mismatch_map = {m["shot"]: m for m in mismatch}
    b_err: List[str] = []
    b_warn: List[str] = []
    budget_rows: List[Dict[str, Any]] = []
    for si, sh in enumerate(rows):
        segs, _ = _segments_of(sh)
        sid = str(sh.get("id") or "")[:8]
        mm = mismatch_map.get(sid)
        mm_note = ""
        if mm:
            # 现场有这种数据：声明 5s，layer2 却排到 12s。这种镜头的片级预算判断会失真，
            # 不静默 —— 把矛盾写进 detail，让人去看数据本身。
            mm_note = "（该镜 layer2 合计 %.2fs ≠ 声明 %.2fs，判断可能失真）" % (
                mm["segments_sum"], mm["declared"])
        for k, seg in enumerate(segs):
            texts = _dialogue_texts(seg)
            if not texts:
                continue
            chars = sum(_pure_len(t) for t in texts)
            n_lines = len(texts)
            golden = bool(seg.get("golden_line") or seg.get("golden"))
            intent = str(seg.get("intent") or "").strip().lower()
            d = _seg_duration(seg)
            need = chars / CPS_FAST + LINE_PAD_MIN * n_lines + (golden_pad if golden else 0.0)
            upper = chars / CPS_SLOW + slack
            dead = "%s 片%d %.2fs / %d 字" % (sid, k, d, chars)
            budget_rows.append({"shot": sid, "seg": k,
                                "duration": round(d, 3), "chars": chars,
                                "lines": n_lines, "need": round(need, 3),
                                "upper": round(upper, 3), "intent": intent or None})
            if d + eps < need:
                b_err.append("%s：最快 5 字/秒也要 %.2fs，念不完%s"
                             % (dead, need, mm_note))
            elif d > upper and intent not in LONG_OK_INTENTS \
                    and not _action_texts(seg):
                b_warn.append("%s：上限 %.2fs，拖太长%s" % (dead, upper, mm_note))
    stats["budget"] = budget_rows
    if b_err:
        checks.append(_mk("BUDGET", "error", "有台词片塞不下", "；".join(b_err[:6]),
                          "offenders=%d" % len(b_err)))
    elif b_warn:
        checks.append(_mk("BUDGET", "warn", "有台词片拖得太长",
                          "；".join(b_warn[:6]), "offenders=%d" % len(b_warn)))
    elif budget_rows:
        checks.append(_mk("BUDGET", "ok", "台词预算都在区间内",
                          "%d 片有台词，全部落在 need…upper 之间" % len(budget_rows),
                          "余量=%.1fs" % slack))
    else:
        checks.append(_skip("BUDGET", "没有有台词片", "分镜里没有台词 → 不量预算"))

    # ── C13：同一分镜内 ≥4 片且同值占比 ≥70% ────────────────────────────────
    c13_hits: List[str] = []
    c13_rows: List[Dict[str, Any]] = []
    for si, sh in enumerate(rows):
        durs = rows[si].get("_seg_durs") or []
        if len(durs) < int(prof["c13_min_segments"]):
            continue
        val, ratio, cnt = _same_group(durs)
        c13_rows.append({"shot": str(sh.get("id") or "")[:8], "segments": len(durs),
                         "same_value": val, "same_ratio": round(ratio, 3),
                         "same_count": cnt})
        if ratio >= float(prof["c13_same_ratio"]):
            c13_hits.append("%s：%d 片里有 %d 片都是 %.3fs（%s）"
                            % (str(sh.get("id") or "")[:8], len(durs), cnt,
                               val, _fmt(ratio, 1)))
    stats["c13"] = c13_rows
    if c13_hits:
        checks.append(_mk("C13", "warn", "同一分镜内时长匀速（C13）",
                          "；".join(c13_hits[:6]),
                          "阈值：≥%d 片且同值占比 ≥%.0f%%"
                          % (prof["c13_min_segments"], prof["c13_same_ratio"] * 100)))
    elif c13_rows:
        checks.append(_mk("C13", "ok", "同一分镜内时长有变化（C13）",
                          "量了 %d 个 ≥%d 片的分镜，同值占比都 <%s"
                          % (len(c13_rows), prof["c13_min_segments"],
                             _fmt(prof["c13_same_ratio"], 2)), ""))
    else:
        checks.append(_skip("C13", "没有 ≥%d 片的分镜" % int(prof["c13_min_segments"]),
                            "所有分镜的片数都不到 %d，C13 不适用"
                            % int(prof["c13_min_segments"])))

    # ── C15：无台词片 >5s；整镜无台词且 >10s ────────────────────────────────
    c15_warn: List[str] = []
    c15_err: List[str] = []
    c15_rows: List[Dict[str, Any]] = []
    for r in seg_rows:
        if r["dialogue"]:
            continue
        c15_rows.append({"shot": r["shot_id"][:8], "seg": r["seg_index"],
                         "duration": r["duration"]})
        if r["duration"] > float(prof["c15_seg_max"]):
            c15_warn.append("%s 片%d 无台词 %.2fs（>%.0fs）"
                            % (r["shot_id"][:8], r["seg_index"], r["duration"],
                               prof["c15_seg_max"]))
    for si, sh in enumerate(rows):
        segs, _ = _segments_of(sh)
        has_dialogue = bool(_dialogue_texts(sh)) or any(
            _dialogue_texts(seg) for seg in segs)
        declared = max(0.0, _f(sh.get("duration_seconds"))) or sum(
            rows[si].get("_seg_durs") or [])
        if not has_dialogue and declared > float(prof["c15_shot_max"]):
            c15_err.append("%s 整镜无台词 %.2fs（>%.0fs）"
                           % (str(sh.get("id") or "")[:8], declared, prof["c15_shot_max"]))
    stats["c15"] = c15_rows
    if c15_err:
        checks.append(_mk("C15", "error", "整镜无台词且太长（空镜）",
                          "；".join(c15_err[:6]) +
                          (("；另有无台词片>%.0fs：%s" % (prof["c15_seg_max"],
                                                          "；".join(c15_warn[:3])))
                           if c15_warn else ""),
                          "阈值：整镜>%.0fs error / 片>%.0fs warn"
                          % (prof["c15_shot_max"], prof["c15_seg_max"])))
    elif c15_warn:
        checks.append(_mk("C15", "warn", "无台词片超过 5 秒（C15）",
                          "；".join(c15_warn[:6]),
                          "阈值：片>%.0fs warn（整镜>%.0fs 才是 error）"
                          % (prof["c15_seg_max"], prof["c15_shot_max"])))
    else:
        checks.append(_mk("C15", "ok", "无台词片都不超 5 秒（C15）",
                          "%d 片无台词，最长的也 ≤%.0fs"
                          % (len(c15_rows), prof["c15_seg_max"]), ""))

    # ── T3 / BREATH：镜头时长分布（全片视角）────────────────────────────────
    dd = duration_distribution(shot_durs, min_distinct=int(prof["t3_min_distinct"]),
                               same_ratio_max=float(prof["t3_same_ratio"]),
                               long_ratio=float(prof["breath_long"]),
                               short_ratio=float(prof["breath_short"]))
    stats.update({k: dd[k] for k in ("total", "median", "min", "max", "distinct",
                                     "same_value", "same_ratio", "max_over_median",
                                     "min_over_median", "equal_split")})
    if dd["n"] < 2:
        checks.append(_skip("T3", "镜头太少，T3 不适用", "只有 %d 个镜头" % dd["n"]))
    elif dd["flat"]:
        checks.append(_mk("T3", "warn", "镜头时长太平（T3）",
                          "；".join(dd["flat_reasons"]),
                          "durations=%s" % _fmt_durs(shot_durs)))
    else:
        checks.append(_mk("T3", "ok", "镜头时长有变化（T3）",
                          "distinct=%d，同值占比 %s"
                          % (dd["distinct"], _fmt(dd["same_ratio"], 3)),
                          "durations=%s" % _fmt_durs(shot_durs)))
    if dd["n"] >= 2 and dd["breathing"]:
        checks.append(_mk("BREATH", "ok", "有长短呼吸",
                          "max/median=%s，min/median=%s"
                          % (_fmt(dd["max_over_median"], 3),
                             _fmt(dd["min_over_median"], 3)),
                          "median=%.2fs max=%.2fs min=%.2fs"
                          % (dd["median"], dd["max"], dd["min"])))
    elif dd["n"] >= 2:
        checks.append(_mk("BREATH", "warn", "没有长短呼吸",
                          "；".join(dd["breath_reasons"]),
                          "median=%.2fs max=%.2fs min=%.2fs"
                          % (dd["median"], dd["max"], dd["min"])))

    # ── T4：同景别连用 >4 ───────────────────────────────────────────────────
    sizes: List[str] = []
    for si, sh in enumerate(rows):
        sz = _shot_size_of(sh)
        if not sz:
            segs, _ = _segments_of(sh)
            for seg in segs:
                sz = _shot_size_of(seg)
                if sz:
                    break
        sizes.append(sz)
    known = [s for s in sizes if s]
    stats["shot_sizes"] = sizes
    if not known:
        checks.append(_skip("T4", "读不到景别，T4 跳过",
                            "layer2.shot_size 与镜头 shot_size 全为空 —— 不从文本猜"))
    else:
        run_max, run_size = 0, ""
        cur, prev = 0, None
        for sz in sizes:
            if not sz:
                prev, cur = None, 0
                continue
            if sz == prev:
                cur += 1
            else:
                cur, prev = 1, sz
            if cur > run_max:
                run_max, run_size = cur, sz
        limit = int(prof["t4_same_size_max"])
        if run_max > limit:
            checks.append(_mk("T4", "warn", "同景别连用太多（T4）",
                              "「%s」连用 %d 次（阈值 >%d）" % (run_size, run_max, limit),
                              "shot_sizes=%s" % ",".join(sizes[:16])))
        else:
            checks.append(_mk("T4", "ok", "景别有变化（T4）",
                              "最长同景别连用 %d 次（阈值 >%d）；读到景别 %d/%d 个镜头"
                              % (run_max, limit, len(known), len(sizes)),
                              "shot_sizes=%s" % ",".join(sizes[:16])))

    return _finish(checks, stats, pacing=pkey)


# ════════════════════════════════════════════════════════════════════════════
# 八、audit_cuts：成片切点侧的节奏门（compose 拆出来的真实段）
# ════════════════════════════════════════════════════════════════════════════

def audit_cuts(segments: Any, *, pacing: str = "promo") -> Dict[str, Any]:
    """成片切点节奏门：C13 / T3 / BREATH（段里带 dialogue 时顺带 C15）。

    segments：`[{"duration": float, "shot_index": int?}]`，也接受纯数字、
    或带 `start/end` 的 dict（end-start 算时长）。
    C13 按 `shot_index` 分组；**一个 shot_index 都没有时按全片一组量并注明**
    （会漏掉"每镜内部匀速"，所以现场最好把 shot_index 带上）。

    这就是量 `ab_gates_on.mp4` 的那种输入：8 段 {3.425×4, 3.526×4}。
    """
    pkey, prof = _profile(pacing)
    checks: List[Dict[str, Any]] = []
    raw = segments
    if not isinstance(raw, (list, tuple)):
        raw = _as_list(raw)
    segs: List[Dict[str, Any]] = []
    for s in raw:
        if isinstance(s, dict):
            d = _seg_duration(s)
            si = s.get("shot_index", s.get("shot"))
            si = None if si is None else int(_f(si))
            segs.append({"duration": round(d, 3), "shot_index": si,
                         "shot_size": _shot_size_of(s),
                         "dialogue": bool(_dialogue_texts(s)),
                         "start": (None if s.get("start") is None
                                   else round(_f(s.get("start")), 3))})
        elif isinstance(s, (int, float)) and not isinstance(s, bool):
            segs.append({"duration": round(max(0.0, _f(s)), 3), "shot_index": None,
                         "shot_size": "", "dialogue": False, "start": None})
    if not segs:
        stats = {"segments": 0, "pacing": pkey}
        checks.append(_mk("CUT", "error", "没有切点可量",
                          "segments 为空或读不出时长", ""))
        return _finish(checks, stats, pacing=pkey,
                       note="导演门：成片切点（没有输入 → 无法判断）")

    durs = [s["duration"] for s in segs]
    has_shot_index = any(s["shot_index"] is not None for s in segs)
    stats: Dict[str, Any] = {
        "segments": len(segs),
        "total_duration": round(sum(durs), 3),
        "pacing": pkey,
        "pacing_fallback": prof.get("_fallback_from"),
        "shot_index_present": has_shot_index,
        "durations": durs,
    }

    # ── C13：同组（shot_index）内 ≥4 片且同值占比 ≥70% ──────────────────────
    groups: Dict[Any, List[float]] = {}
    for s in segs:
        groups.setdefault(s["shot_index"] if s["shot_index"] is not None else "all",
                          []).append(s["duration"])
    c13_hits: List[str] = []
    c13_rows: List[Dict[str, Any]] = []
    for gi, gd in groups.items():
        if len(gd) < int(prof["c13_min_segments"]):
            continue
        val, ratio, cnt = _same_group(gd)
        c13_rows.append({"group": gi, "segments": len(gd), "same_value": val,
                         "same_ratio": round(ratio, 3), "same_count": cnt})
        if ratio >= float(prof["c13_same_ratio"]):
            c13_hits.append("%s：%d 片里有 %d 片都是 %.3fs（%s）"
                            % ("全片" if gi == "all" else "镜%s" % gi,
                               len(gd), cnt, val, _fmt(ratio, 1)))
    stats["c13_groups"] = c13_rows
    fallback_note = ("" if has_shot_index else
                     "（没有 shot_index，按全片一组量——会漏掉每镜内部匀速）")
    if c13_hits:
        checks.append(_mk("C13", "warn", "同一分镜内时长匀速（C13）",
                          "；".join(c13_hits[:6]) + fallback_note,
                          "阈值：≥%d 片且同值占比 ≥%.0f%%%s"
                          % (prof["c13_min_segments"], prof["c13_same_ratio"] * 100,
                             "" if has_shot_index else "；组=全片")))
    elif c13_rows:
        checks.append(_mk("C13", "ok", "同一分镜内时长有变化（C13）",
                          "量了 %d 组 ≥%d 片，同值占比都 <%s"
                          % (len(c13_rows), prof["c13_min_segments"],
                             _fmt(prof["c13_same_ratio"], 2)) + fallback_note, ""))
    else:
        checks.append(_skip("C13", "没有 ≥%d 片的组" % int(prof["c13_min_segments"]),
                            "每组的片数都不到 %d，C13 不适用" % int(prof["c13_min_segments"])))

    # ── T3 / BREATH：全片切点分布 ───────────────────────────────────────────
    dd = duration_distribution(durs, min_distinct=int(prof["t3_min_distinct"]),
                               same_ratio_max=float(prof["t3_same_ratio"]),
                               long_ratio=float(prof["breath_long"]),
                               short_ratio=float(prof["breath_short"]))
    stats.update({k: dd[k] for k in ("median", "min", "max", "distinct",
                                     "same_value", "same_ratio", "max_over_median",
                                     "min_over_median", "equal_split")})
    if dd["n"] < 2:
        checks.append(_skip("T3", "切点太少，T3 不适用", "只有 %d 个切点" % dd["n"]))
    elif dd["flat"]:
        checks.append(_mk("T3", "warn", "全片切点太平（T3）",
                          "；".join(dd["flat_reasons"]),
                          "durations=%s" % _fmt_durs(durs)))
    else:
        checks.append(_mk("T3", "ok", "全片切点有变化（T3）",
                          "distinct=%d，同值占比 %s"
                          % (dd["distinct"], _fmt(dd["same_ratio"], 3)),
                          "durations=%s" % _fmt_durs(durs)))
    if dd["n"] < 2:
        checks.append(_skip("BREATH", "切点太少，量不出呼吸",
                            "只有 %d 个切点" % dd["n"]))
    elif dd["breathing"]:
        checks.append(_mk("BREATH", "ok", "有长短呼吸",
                          "max/median=%s（≥%s），min/median=%s（≤%s）"
                          % (_fmt(dd["max_over_median"], 3), _fmt(prof["breath_long"], 2),
                             _fmt(dd["min_over_median"], 3), _fmt(prof["breath_short"], 2)),
                          "median=%.2fs max=%.2fs min=%.2fs"
                          % (dd["median"], dd["max"], dd["min"])))
    else:
        checks.append(_mk("BREATH", "warn", "没有长短呼吸",
                          "；".join(dd["breath_reasons"]),
                          "median=%.2fs max=%.2fs min=%.2fs"
                          % (dd["median"], dd["max"], dd["min"])))

    # ── C15（可选）：段里带 dialogue 字段时才量 ─────────────────────────────
    if any("dialogue" in s for s in raw if isinstance(s, dict)) or any(
            isinstance(s, dict) and s.get("dialogue") is not None for s in raw):
        nodia = [s for s in segs if not s["dialogue"]]
        bad = [s for s in nodia if s["duration"] > float(prof["c15_seg_max"])]
        if bad:
            checks.append(_mk("C15", "warn", "无台词片超过 5 秒（C15）",
                              "；".join("%.2fs" % s["duration"] for s in bad[:6]),
                              "offenders=%d/%d 无台词片" % (len(bad), len(nodia))))
        else:
            checks.append(_mk("C15", "ok", "无台词片都不超 5 秒（C15）",
                              "%d 片无台词，最长 %.2fs"
                              % (len(nodia), max([s["duration"] for s in nodia] or [0])),
                              ""))
    else:
        checks.append(_skip("C15", "切点没带台词信息", "segments 里没有 dialogue 字段 → C15 跳过"))

    return _finish(checks, stats, pacing=pkey)


# ════════════════════════════════════════════════════════════════════════════
# 九、selftest：正反例（真跑，不 print 假数字）
# ════════════════════════════════════════════════════════════════════════════

def _ids(result: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {c["id"]: c for c in result.get("checks") or []}


def _cut_case(segs: Sequence[float], shot_index: Optional[Sequence[int]] = None
              ) -> List[Dict[str, Any]]:
    out = []
    for i, d in enumerate(segs):
        out.append({"duration": float(d),
                    "shot_index": (shot_index[i] if shot_index else None)})
    return out


def _shot(shot_id: str, dur: float, segs: Sequence[Dict[str, Any]],
          shot_size: str = "") -> Dict[str, Any]:
    return {"id": shot_id, "order_index": 0, "duration_seconds": dur,
            "layer2_timeline": json.dumps(list(segs), ensure_ascii=False),
            "shot_size": shot_size or None}


def selftest() -> Dict[str, Any]:
    """真跑一遍正反例。返回 {"ok":bool,"passed","failed","cases":[...]}。

    每个 case 都是「造输入 → 跑真函数 → 断言该报的报、不该报的不报」，
    没有 mock、没有 print 假数字。
    """
    cases: List[Dict[str, Any]] = []

    def case(name: str, cond: Any, detail: str = "") -> None:
        cases.append({"name": name, "ok": bool(cond), "detail": str(detail)[:300]})

    # ── 1. line_budget：字面量核对（8 字 → need 1.9 / upper 4.786）───────────
    lb = line_budget(8)
    case("line_budget(8).need == 1.9", abs(lb["need"] - 1.9) < 1e-6,
         "need=%s upper=%s speech=%s" % (lb["need"], lb["upper"], lb["speech"]))
    case("line_budget(8).upper == 8/3.5+2.5",
         abs(lb["upper"] - (8 / 3.5 + 2.5)) < 1e-3, "upper=%s" % lb["upper"])
    lbg = line_budget(8, golden=True)
    case("line_budget(8,golden) need +0.5、upper 不变",
         abs(lbg["need"] - (1.9 + GOLDEN_PAD)) < 1e-6 and abs(lbg["upper"] - lb["upper"]) < 1e-9,
         "need=%s（金句气口只加 need） upper=%s" % (lbg["need"], lbg["upper"]))
    # 7b6ee913 镜0 实测：8 字台词 / 4s 时间片 —— 不能被误报"拖太长"
    case("7b6ee913 镜0（8 字 / 4.0s）不误报",
         4.0 <= lb["upper"] and 4.0 >= lb["need"],
         "4.0s ∈ [%s, %s]" % (lb["need"], lb["upper"]))

    # ── 2. duration_distribution：现版缺陷（8 段 {3.425×4, 3.526×4}）─────────
    flat = duration_distribution([3.425] * 4 + [3.526] * 4)
    case("现版 8 段 {3.425×4,3.526×4} → flat（distinct=2）",
         flat["flat"] and flat["distinct"] == 2 and flat["same_ratio"] == 0.5,
         "distinct=%d same_ratio=%s" % (flat["distinct"], flat["same_ratio"]))
    case("现版 8 段 → 无呼吸（max/median≈1.01）",
         (not flat["breathing"]) and abs(flat["max_over_median"] - 1.014) < 0.002,
         "max/median=%s min/median=%s"
         % (flat["max_over_median"], flat["min_over_median"]))

    # ── 3. duration_distribution：有呼吸的正例 ──────────────────────────────
    good = duration_distribution([2.0, 3.0, 4.5, 7.0])
    case("有长短呼吸 [2,3,4.5,7] → ok",
         good["ok"] and good["breathing"] and not good["flat"],
         "median=%s max/median=%s min/median=%s"
         % (good["median"], good["max_over_median"], good["min_over_median"]))

    # ── 4. audit_cuts：现版必定 C13 + 无呼吸 ────────────────────────────────
    r = audit_cuts(_cut_case([3.425] * 4 + [3.526] * 4,
                             [0, 0, 0, 0, 1, 1, 1, 1]))
    ids = _ids(r)
    case("现版 cuts → C13 warn", ids.get("C13", {}).get("level") == "warn",
         ids.get("C13", {}).get("detail", "（没这条）"))
    case("现版 cuts → BREATH warn", ids.get("BREATH", {}).get("level") == "warn",
         ids.get("BREATH", {}).get("detail", "（没这条）"))
    case("现版 cuts → T3 warn（distinct=2<3）", ids.get("T3", {}).get("level") == "warn",
         ids.get("T3", {}).get("detail", "（没这条）"))
    case("现版 cuts → errors=0（都是 warn）", r["errors"] == 0 and r["warnings"] >= 2,
         "score=%s errors=%s warnings=%s" % (r["score"], r["errors"], r["warnings"]))

    # ── 5. audit_cuts：3.5/4/4/3.5+5/5/5 —— C13 与 T3 都不该报 ──────────────
    r2 = audit_cuts(_cut_case([3.5, 4, 4, 3.5, 5, 5, 5], [0] * 7))
    i2 = _ids(r2)
    case("3.5/4/4/3.5+5/5/5 → C13 不报", i2.get("C13", {}).get("level") == "ok",
         i2.get("C13", {}).get("detail", ""))
    case("3.5/4/4/3.5+5/5/5 → T3 不报（distinct=3）",
         i2.get("T3", {}).get("level") == "ok", i2.get("T3", {}).get("detail", ""))

    # ── 6. audit_cuts：有呼吸的切点 = 干净过门 ──────────────────────────────
    r3 = audit_cuts(_cut_case([2.0, 3.0, 4.5, 7.0]))
    case("干净切点 [2,3,4.5,7] → clean（无 error 无 warn）",
         r3["clean"] and r3["score"] == 100,
         "score=%s checks=%s" % (r3["score"],
                                 [(c["id"], c["level"]) for c in r3["checks"]]))

    # ── 7. audit_shots：C15（1 段 15s 无台词 = error；有台词长镜不是 error）──
    s_err = audit_shots([_shot("s1", 15.0, [{"start": 0, "end": 15,
                                             "action": "雪地空镜，无人"}])])
    case("1 段 15s 无台词 → C15 error",
         _ids(s_err).get("C15", {}).get("level") == "error",
         _ids(s_err).get("C15", {}).get("detail", ""))
    s_ok = audit_shots([_shot("s2", 12.0, [
        {"start": 0, "end": 4, "dialogue": None, "action": "农夫挑柴走近"},
        {"start": 4, "end": 8, "dialogue": {"text": "暖过来就好，暖过来就好。"},
         "action": "捧起蛇"},
        {"start": 8, "end": 12, "dialogue": None, "action": "塞进怀里"}])])
    case("有台词长镜（12s，含 10 字台词）→ C15 不报",
         _ids(s_ok).get("C15", {}).get("level") == "ok",
         _ids(s_ok).get("C15", {}).get("detail", ""))

    # ── 8. audit_shots：C13（同镜 4 片同值）+ T4（同景别连用 5 次）──────────
    s_c13 = audit_shots([_shot("s3", 12.0, [
        {"start": 0, "end": 3, "action": "a"}, {"start": 3, "end": 6, "action": "b"},
        {"start": 6, "end": 9, "action": "c"}, {"start": 9, "end": 12, "action": "d"}])])
    case("同镜 4 片同值 → C13 warn",
         _ids(s_c13).get("C13", {}).get("level") == "warn",
         _ids(s_c13).get("C13", {}).get("detail", ""))
    t4_shots = [_shot("t%d" % i, 3.0,
                      [{"start": 0, "end": 3, "dialogue": {"text": "嗯"},
                        "action": "x"}], shot_size="medium") for i in range(5)]
    s_t4 = audit_shots(t4_shots)
    case("同景别连用 5 次 → T4 warn",
         _ids(s_t4).get("T4", {}).get("level") == "warn",
         _ids(s_t4).get("T4", {}).get("detail", ""))
    t4_skip = audit_shots([_shot("u%d" % i, 3.0,
                                 [{"start": 0, "end": 3, "action": "x"}]) for i in range(2)])
    case("读不到景别 → T4 skip（level=ok 且 detail 有 skip）",
         _ids(t4_skip).get("T4", {}).get("detail", "").startswith("skip："),
         _ids(t4_skip).get("T4", {}).get("detail", ""))

    # ── 9. audit_script：BUDGET（拖太长 warn / 塞不下 error）────────────────
    base = [{"scene_number": 1, "duration_seconds": 15,
             "dialogues": [{"text": "做片子，难的不是拍。"},
                           {"text": "是先说清楚要什么。"}],
             "actions": [], "pacing": {"plot": "钩子", "emotion": "calm",
                                                 "shot_density": "low", "size_focus": "medium"}}]
    r_b = audit_script({"scenes": base})
    case("15s / 15 字 2 句 → BUDGET warn（拖太长）",
         _ids(r_b).get("BUDGET", {}).get("level") == "warn",
         _ids(r_b).get("BUDGET", {}).get("detail", ""))
    # 「我可怜你……是我错了……」= 8 字 → need=1.9s；给 1.0s 就是最快语速也念不完
    tight = [{"scene_number": 1, "duration_seconds": 1.0,
              "dialogues": [{"text": "我可怜你……是我错了……"}],
              "actions": ["仰倒"], "pacing": {"plot": "结局", "emotion": "regret",
                                              "shot_density": "low", "size_focus": "close"}}]
    r_be = audit_script({"scenes": tight})
    case("1.0s / 8 字 → BUDGET error（塞不下）",
         _ids(r_be).get("BUDGET", {}).get("level") == "error",
         _ids(r_be).get("BUDGET", {}).get("detail", ""))
    ambient = [{"scene_number": 1, "duration_seconds": 15,
                "dialogues": [{"text": "暖过来就好。"}], "actions": ["雪落"],
                "intent": "ambient",
                "pacing": {"plot": "留白", "emotion": "gentle",
                           "shot_density": "low", "size_focus": "wide"}}]
    r_amb = audit_script({"scenes": ambient})
    case("intent=ambient 的长片不报拖太长",
         _ids(r_amb).get("BUDGET", {}).get("level") == "ok",
         _ids(r_amb).get("BUDGET", {}).get("detail", ""))

    # ── 10. audit_script：HOOK（首场 15s 没事件 → error）─────────────────────
    hook_bad = [{"scene_number": 1, "duration_seconds": 15,
                 "dialogues": [], "actions": [],
                 "pacing": {"plot": "空镜", "emotion": "calm",
                            "shot_density": "low", "size_focus": "wide"}},
                {"scene_number": 2, "duration_seconds": 5,
                 "dialogues": [{"text": "喂"}], "actions": [],
                 "pacing": {"plot": "事件", "emotion": "calm",
                            "shot_density": "low", "size_focus": "medium"}}]
    r_hook = audit_script({"scenes": hook_bad})
    case("首场 15s 无事件、事件被挤到 15s → HOOK error",
         _ids(r_hook).get("HOOK", {}).get("level") == "error",
         _ids(r_hook).get("HOOK", {}).get("detail", ""))
    r_hook_ok = audit_script({"scenes": [{
        "scene_number": 1, "duration_seconds": 5,
        "dialogues": [{"text": "做片子，难的不是拍。"}], "actions": [],
        "pacing": {"plot": "钩子", "emotion": "calm",
                   "shot_density": "low", "size_focus": "medium"}}]})
    case("首场 5s 内有台词 → HOOK ok",
         _ids(r_hook_ok).get("HOOK", {}).get("level") == "ok",
         _ids(r_hook_ok).get("HOOK", {}).get("detail", ""))

    # ── 11. audit_script：PACING 缺字段 → warn（不阻断）──────────────────────
    r_pac = audit_script({"scenes": [{
        "scene_number": 1, "duration_seconds": 5,
        "dialogues": [{"text": "嗯"}], "actions": ["走"], "pacing": None}]})
    case("场景缺 pacing → PACING warn、但不是 error",
         _ids(r_pac).get("PACING", {}).get("level") == "warn" and r_pac["ok"],
         _ids(r_pac).get("PACING", {}).get("detail", ""))

    # ── 12. audit_script：scenes 是 JSON 字符串（现场形态）也要能读 ──────────
    r_str = audit_script({"scenes": json.dumps(base, ensure_ascii=False)})
    case("scenes 是 JSON 字符串 → 能解析出 1 场",
         r_str["stats"].get("scenes") == 1 and r_str["errors"] == 0,
         "scenes=%s problems=%s" % (r_str["stats"].get("scenes"),
                                    r_str["stats"].get("problems")))

    passed = sum(1 for c in cases if c["ok"])
    failed = len(cases) - passed
    return {
        "ok": failed == 0,
        "version": GATE_VERSION,
        "passed": passed,
        "failed": failed,
        "total": len(cases),
        "cases": cases,
        "note": "导演门自检：%d 个正反例（真跑 audit_/line_budget/duration_distribution）" % len(cases),
    }


# ════════════════════════════════════════════════════════════════════════════
# 十、直接跑：py core/director_gate.py 看自检
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":  # pragma: no cover
    import sys
    res = selftest()
    print(json.dumps({"ok": res["ok"], "passed": res["passed"],
                      "failed": res["failed"], "total": res["total"]},
                     ensure_ascii=False))
    for c in res["cases"]:
        print(("  [OK] " if c["ok"] else "  [FAIL] ") + c["name"] +
              ("" if c["ok"] else " → " + c["detail"]))
    sys.exit(0 if res["ok"] else 1)
