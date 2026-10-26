# -*- coding: utf-8 -*-
"""VideoForge · 剧本时长强制归一化（"我选了 5 秒，结果给我 60 秒"）

★★ 用户实测反馈（2026-09-14）：
    「主页这块的总时长可以选择了，比如我选的 5 秒，但我生成剧本的时候发现
      没按我的要求来，居然给了 60 秒」

查真实数据后确认，这不是"AI 不听话"这么简单，而是**两处独立的断点**：

  ① **选择根本没存下来**。主页 `createFromHero` 把 5 秒发给了这一次生成请求，
     但**没有写进项目设置** `default_total_duration`。而剧本页的「💡 想法生成大纲 /
     重新生成」走的是 `settings.default_total_duration || 60` ——
     实测项目 `37f40d84` 的 settings 里**没有这个键**，于是任何一次重新生成
     都变成 60 秒。用户看到的 5 场 × 12 秒 = 60 秒，正是 `total=60` 的产物。

  ② **时长只是"请求"，没有强制**。`ScriptService.generate_script` 把
     "总时长：N 秒 / 各场秒数之和必须等于 N 秒"写进提示词就结束了 ——
     模型吐回 5×12 秒，我们就照单全收。**提示词不是保证**（这正是参考库里
     `F-*` 那套失败清单反复强调的事）。

所以本模块做**服务端强制**：场次数由**我们**决定，每场秒数由**我们**分配，
总和必须等于用户要的秒数；做不到的（例如 5 秒放不下 5 场）**如实报告被裁掉的场次**，
绝不静默改数。

纯函数，不碰数据库、不碰网络。
"""

from __future__ import annotations

import logging
import math
import unicodedata
from typing import Any, Dict, List, Optional


logger = logging.getLogger("videoforge.scriptplan")


# 一场戏短于 3 秒连一句台词都说不完（中文约 4 字/秒），所以这是硬下限。
MIN_SCENE_SECONDS = 3
# 短视频的常见节奏：一场 10-15 秒（也是 VideoForge 一直以来的默认值）。
PREFERRED_SCENE_SECONDS = 12
# 单次剧本生成真能吐完 JSON 的场次上限（原实现也是 24）。
SCENE_CAP = 24

# ──────────── 节奏档位（2026-10-24 用户反馈「视频倍速好慢」）────────────
# 为什么要有它：`PREFERRED_SCENE_SECONDS = 12` 是"一场戏 12 秒"的老默认，
# 60 秒只切 5 镜；而短视频/短剧的实际节奏是 2~3.5 秒一镜（60 秒 18~24 镜）。
# 差 3~5 倍的切点密度，观众的第一感受就是"慢、看不下去"。
#
# 每个档位 = 一场戏（≈ 一个镜头）的**目标秒数** + 硬下限。
# 下限不能太小：一场 2 秒的戏连一句中文台词都塞不下（约 4 字/秒）。
PACING: Dict[str, Dict[str, Any]] = {
    # ★ 2026-10-24 修正（用户给的硬约束：seedance2.0 特价按秒计费，**最少 5 秒**；
    #   `model_catalog` 里 seedance 2.0/2.5 的 caps 最小档同样是 5）。
    #
    #   因此必须把两件事分开，不能混为一个数：
    #     · `gen`  = 一次生成的时长（成本单元，也是模型必须接受的合法值）
    #     · `cut`  = 观众看到的切点长度（剪辑层实现：一条 gen 素材拆成 gen/cut 段）
    #   混在一起就会出现"按 2.5 秒去请求 → 被厂商抬高/拒绝，或者多花一倍钱"。
    "fast_drama": {"cut": 2.5, "min": 5, "cap": 40,
                   "label": "短剧快切（抖音/爽点向）：5s 一条素材，剪成 2 个 2.5s 切点"},
    "promo":      {"cut": 3.5, "min": 5, "cap": 32,
                   "label": "宣传片/产品：5s 素材剪成 1~2 个切点"},
    "talking":    {"cut": 5.0, "min": 5, "cap": 24,
                   "label": "口播/讲解：一条 5s 素材就是一切点"},
    "ambient":    {"cut": 8.0, "min": 5, "cap": 16,
                   "label": "氛围/纪录：10s 长镜，尽量不切"},
    "legacy":     {"cut": 12.0, "min": 5, "cap": 24,
                   "label": "旧默认（12 秒/镜，仅兼容用）"},
}


def choose_gen_for_total(total: Any, pacing: Any = None) -> int:
    """在合法区间 [5, 15] 里挑一个**不浪费素材**的生成时长。

    为什么不能只用 cut×2：按秒计费时，60 秒成片如果每条生成 7 秒，
    就要 9 条 = 63 秒素材（还行）；但 60 秒按 10 秒生成只要 6 条 = 60 秒 ——
    刚好整除。所以按"余数最小 → 生成条数最少"挑，能少买素材。

    约束：
      · 必须落在 [5, 15]（seedance2.0特价按秒计的上下限，用户给定）；
      · 不短于一个切点 cut（否则一条素材装不下一个完整切点）；
      · 同等浪费下取更小的 gen（调用次数少一点、单条更短，失败重试更便宜）。
    """
    spec = pacing_spec(pacing)
    cut = float(spec.get("cut") or 5)
    total = float(total or 0)
    lo, hi = 5, 15
    if total <= 0:
        return int(max(lo, min(hi, round(cut * 2))))
    best = None
    # 一条素材最多切 3 段：再多就是“同一个机位反复推近”的抖动感，不是快切。
    max_gen = min(hi, max(lo, int(round(cut * 3))))
    for gen in range(lo, max_gen + 1):
        if gen < cut * 0.99:
            continue                      # 一条素材装不下一个切点
        n = max(1, int(round(total / gen)))
        waste = abs(n * gen - total)      # 与成片时长的差（多买/少买）
        score = (waste, n, gen)           # 浪费最小 → 条数最少 → gen 更小
        if best is None or score < best[0]:
            best = (score, gen)
    return int(best[1]) if best else int(max(lo, min(hi, round(cut * 2))))


def pacing_gen_seconds(pacing: Any = None) -> int:
    """一次生成**首选**几秒。合法区间 [5, 15]（seedance2.0特价按秒计费的最短/最长）。

    规则（2026-10-24，用户给定上下限后重写）：
      · 先按"一条素材装 2 个切点"取 gen ≈ cut × 2；
      · 再**夹进 [5, 15]**：低于 5 抬到 5（厂商不收 <5s 的请求），高于 15 压到 15；
      · 最后仍会由 `main._snap_legal_duration` 按具体型号/分辨率的能力表吸附
        （不同型号允许值不同，比如并非所有型号都吃 15）。
    这样 5/6/7/8/9/10/12/15 都能用上，不是写死某一个值。
    """
    spec = pacing_spec(pacing)
    want = round(float(spec.get("cut") or 5) * 2)
    return int(max(5, min(15, want)))


def pacing_cut_seconds(pacing: Any = None) -> float:
    """观众感知的切点长度（剪辑层实现）。"""
    return float(pacing_spec(pacing).get("cut") or 5.0)


def cuts_per_generation(pacing: Any = None) -> int:
    """一条生成素材该剪成几个切点（至少 1；四舍五入，2.5s/5s → 2）。"""
    spec = pacing_spec(pacing)
    gen = float(spec.get("gen") or 5)
    cut = float(spec.get("cut") or gen)
    if cut <= 0:
        return 1
    return max(1, int(round(gen / cut)))


def cuts_for(total: int, pacing: Any = None) -> int:
    """成片该有多少个切点（观众感知）。"""
    spec = pacing_spec(pacing)
    cut = float(spec.get("cut") or 5)
    total = max(0, int(total))
    if cut <= 0:
        return 0
    return max(1, int(round(total / cut)))


def pacing_spec(name: Any = None) -> Dict[str, Any]:
    """取档位定义；未知/None 一律回退 legacy（保持旧行为，不静默改变别人的出片）。"""
    key = str(name or "").strip().lower()
    return dict(PACING.get(key) or PACING["legacy"], key=key if key in PACING else "legacy")


def pacing_for(style: Any = None, intent: Any = None, explicit: Any = None) -> str:
    """决定用哪个节奏档位。

    优先级：显式指定 > 用户意图关键词 > style 映射 > fast_drama（短视频默认）。
    ★ 默认从 legacy(12s) 改成 fast_drama(2.5s)：用户要的是"能看下去"，
      真需要慢节奏的在意图里说「氛围/纪录」就会被识别到。
    """
    name = str(explicit or "").strip().lower()
    if name in PACING:
        return name
    text = str(intent or "").lower()
    for key, words in _PACING_HINTS:
        if any(w in text for w in words):
            return key
    st = str(style or "").strip().lower()
    if st in ("documentary", "anime"):
        return "ambient" if st == "documentary" else "promo"
    if st in ("ad", "mv"):
        return "promo"
    return "fast_drama"


# ──────────── 视频提示词用的「节奏指令」（2026-10-24 用户指出的真正病根）────────────
# 为什么必须写进提示词：实测成片 30 秒只有 2 个镜头、每镜 15 秒缓慢推镜。
# 查下来提示词里**一句节奏指令都没有**，负向词反而写着「夸张大幅动作、变焦、抖动」，
# 运镜文字又直接来自分镜的「缓慢推镜」—— 模型照做，于是"慢"是提示词写出来的，
# 不是时长规划造成的。所以节奏必须在**生成提示词**里说清楚。
PACING_PROMPT: Dict[str, str] = {
    "fast_drama": (
        "【节奏（硬要求）】画面在 0.5~1 秒内就出现可辨识的动作或变化；"
        "此后每 1.5~2 秒至少有一次可见变化（动作推进、表情变化、构图变化或光影变化）；"
        "运镜必须在 1~1.5 秒内完成并停稳，不要拖满整镜；"
        "全片是快节奏短剧，动作干脆、起落明确，不是慢条斯理的文艺镜头。"
    ),
    "promo": (
        "【节奏（硬要求）】前 1.5 秒建立主体与卖点，此后每 2~3 秒一次可见变化；"
        "运镜在 1.5~2 秒内完成，干净利落，不拖沓。"
    ),
    "talking": (
        "【节奏】以口型与表情为主，每 3~4 秒一次表情或手势变化；镜头基本固定，只允许轻微呼吸感运镜。"
    ),
    "ambient": (
        "【节奏】允许缓慢运动与呼吸感，但每 4~6 秒要有一次可见变化，不要全程静止。"
    ),
    "legacy": "",
}

# 「慢」的负向词：把这几种"电影感慢动作"显式压掉（只在快节奏档位加）
PACING_NEGATIVE = {
    "fast_drama": "慢动作、慢镜头、缓慢推轨、缓慢平移、静止长镜、定格、大幅缓慢摇镜；",
    "promo": "慢动作、静止长镜、定格；",
    "talking": "",
    "ambient": "",
    "legacy": "",
}

# 分镜里写"缓慢/慢慢/缓缓"的运镜，在快节奏档位要加完成时限
_SLOW_CAMERA_WORDS = ("缓慢", "慢慢", "缓缓", "悠缓", "徐徐", "长镜")


def prompt_pacing_brief(pacing: Any = None) -> str:
    """给视频提示词用的节奏指令（空串 = 不加）。"""
    return PACING_PROMPT.get(pacing_spec(pacing).get("key") or "legacy", "")


def prompt_pacing_negative(pacing: Any = None) -> str:
    """给负向提示词补的"慢动作"项（空串 = 不加）。"""
    return PACING_NEGATIVE.get(pacing_spec(pacing).get("key") or "legacy", "")


def camera_timing_note(camera_text: Any, pacing: Any = None) -> str:
    """分镜写了"缓慢推镜"时，给一个**完成时限**（快节奏档位才加）。

    注意不是把用户的运镜改掉（那是创作意图），而是把它从"拖满整镜"变成
    "在前 1~1.5 秒做完，然后停稳" —— 同样是推镜，观感完全不同。
    """
    text = str(camera_text or "").strip()
    if text == "":
        return ""
    if not any(w in text for w in _SLOW_CAMERA_WORDS):
        return ""
    key = pacing_spec(pacing).get("key") or "legacy"
    if key not in ("fast_drama", "promo"):
        return ""
    return f"（上述运镜在 1~1.5 秒内完成，之后保持稳定，不要缓慢拖满整镜）"


def pacing_report(shots: List[Dict[str, Any]], pacing: Any = None) -> Dict[str, Any]:
    """镜长体检：给已有项目算平均镜长，并与档位目标比对。

    为什么不只看"总时长对不对"：总时长对得上、但 5 个 12 秒长镜拼 60 秒，
    节奏依然是错的。这里直接量**切点密度**。
    """
    spec = pacing_spec(pacing)
    durs: List[float] = []
    for s in shots or []:
        try:
            d = float(s.get("duration_seconds") or 0)
        except Exception:
            d = 0.0
        if d > 0:
            durs.append(d)
    total = round(sum(durs), 2)
    n = len(durs)
    avg = round(total / n, 2) if n else 0.0
    target = float(spec.get("seconds") or 0) or 12.0
    suggested = max(1, int(round(total / target))) if total > 0 else 0
    # 判定：平均镜长超过目标 1.6 倍算"偏慢"，超过 2.5 倍算"明显慢"
    ratio = (avg / target) if target > 0 and avg > 0 else 0.0
    verdict = "ok"
    if ratio >= 2.5:
        verdict = "too_slow"
    elif ratio >= 1.6:
        verdict = "slow"
    elif ratio and ratio <= 0.5:
        verdict = "too_fast"
    return {
        "pacing": spec.get("key"), "label": spec.get("label"),
        "target_shot_seconds": target,
        "shots": n, "total_seconds": total, "avg_shot_seconds": avg,
        "expected_shots_at_target": suggested,
        "ratio": round(ratio, 2) if ratio else None,
        "verdict": verdict,
        "note": (
            f"{spec.get('label')}：目标 {target:g}s/镜；当前 {n} 镜、平均 {avg:g}s。"
            + ("" if verdict == "ok" else
               f" 同样 {total:g} 秒按目标节奏应该切成约 {suggested} 镜 —— "
               f"现在是 {n} 镜，切点少了 {max(0, suggested - n)} 个，观感会拖。")
        ),
    }


def scene_count_for(total: int, *, min_scene=None,
                      preferred=None, cap=None,
                      pacing=None) -> int:
    """**我们**决定这个时长该写几场戏（不把这个决定权交给 LLM）。

    例：5 秒 → 1 场；60 秒 → 5 场；90 秒 → 8 场；30 分钟 → 24 场（封顶）。
    """
    if pacing is not None or preferred is None or min_scene is None or cap is None:
        _spec = pacing_spec(pacing)
        preferred = float(choose_gen_for_total(total, pacing)) if preferred is None else preferred
        min_scene = int(_spec.get("min") or MIN_SCENE_SECONDS) if min_scene is None else min_scene
        cap = int(_spec.get("cap") or SCENE_CAP) if cap is None else cap
    try:
        total = int(total)
    except Exception:
        total = 0
    total = max(int(min_scene), total)
    n = int(round(total / float(max(1, preferred))))
    n = max(1, n)
    # 每场不能短于下限：5 秒最多 1 场，7 秒最多 2 场
    n = min(n, max(1, total // int(min_scene)))
    return max(1, min(int(cap), n))


# ★ 2026-10-24：`distribute` 是**旧行为**，保留原样（等分兜底与兼容用）。
#   按内容分配请用 `distribute_by_weight` —— 等分正是"段长 ÷ 镜数"，
#   30 秒 8 场会得到 [4,4,4,4,4,4,3,3]（8 场几乎一样长，节奏是平的）。
def distribute(total: int, n: int) -> List[int]:
    """把 `total` 秒**整数**分摊到 n 场，和恰好等于 total（余数给前几场）。"""
    total, n = int(total), max(1, int(n))
    base, rem = divmod(total, n)
    return [base + (1 if i < rem else 0) for i in range(n)]


def prompt_duration_contract(total: int, scene_count: int, *, pacing: Any = None) -> Dict[str, Any]:
    """Return a feasible exact integer duration contract and a concrete witness.

    Scene variety and a short first scene are conditional on the total and bounds;
    they never override the requested exact duration or require impossible values.
    """
    total = int(total)
    if total < 1:
        raise ValueError("剧本目标时长必须大于 0 秒")
    minimum = min(int(pacing_spec(pacing).get("min") or MIN_SCENE_SECONDS), total)
    maximum = 15
    requested = max(1, int(scene_count))
    count = min(max(requested, math.ceil(total / maximum)), max(1, total // minimum))
    count = min(count, SCENE_CAP)
    if math.ceil(total / maximum) > count:
        raise ValueError(f"单次剧本最多 {SCENE_CAP} 场、每场最多 {maximum} 秒；请把目标分段生成")
    first_low = max(minimum, total - maximum * (count - 1))
    first_high = min(maximum, total - minimum * (count - 1))
    hook_low, hook_high = max(3, first_low), min(5, first_high)
    short_hook_feasible = hook_low <= hook_high
    first_range = [hook_low, hook_high] if short_hook_feasible else [first_low, first_high]
    first = first_range[0]
    witness = [first] + (distribute(total - first, count - 1) if count > 1 else [])
    variety = min(3, count)
    while variety > 1 and not (count * minimum + variety * (variety - 1) // 2 <= total
                              <= count * maximum - variety * (variety - 1) // 2):
        variety -= 1
    return {"policy": "exact", "target_seconds": total, "requested_scene_count": requested,
            "scene_count": count, "min_scene_seconds": minimum, "max_scene_seconds": maximum,
            "first_scene_range": first_range, "short_hook_feasible": short_hook_feasible,
            "variety_target": variety, "example_seconds": witness}


# ──────────── 分镜时长按内容倒推（2026-10-24；禁止「段长÷镜数」）────────────
# 为什么不能等分：30 秒 8 场等分 = [4,4,4,4,4,4,3,3] —— 8 段一样长，没有钩子、
# 没有高潮、没有留白，节奏是平的。人写分镜从来不是"总时长 ÷ 镜数"：钩子 2~3 秒
# 就够，高潮/金句要给足，过场一闪而过。
#
# 这里拆成两个纯函数：
#   · `scene_weights`        把"这场戏有多少内容"折算成可比的权重；
#   · `distribute_by_weight` 按权重分整数秒：和精确 = 总时长、逐项夹 [min,max]、
#                            且（n≥3 时）尽量给 ≥3 个不同取值，否则还是一眼看穿的等分。
#
# 读不到内容一律返回 None / 退回等分并留 note —— **不猜**。

# beat 关键词 → 时长系数。顺序即优先级（"金句收尾"按金句算：少给时长才是错的）。
_BEAT_FACTORS: List[Any] = [
    ("钩子", 0.75),
    ("高潮", 1.25), ("爆发", 1.25), ("转折", 1.25),
    ("金句", 1.30),
    ("过场", 0.70), ("空镜", 0.70),
    ("收尾", 1.00), ("结局", 1.00),
]

# 节奏密度系数：快切镜头短、长镜头长。对**所有**场一起乘 → 不改变相对比例，
# 只影响 `golden_line` 这种绝对加秒的相对分量。
_DENSITY_FACTORS: Dict[str, float] = {
    "fast": 0.85, "fast_cut": 0.85, "快切": 0.85,
    "long": 1.15, "long_take": 1.15, "长镜头": 1.15,
}
# 兼容既有档位名（`PACING` 的 key）：快节奏档 = 快切，氛围档 = 长镜头。
_PACING_DENSITY = {"fast_drama": 0.85, "promo": 0.85, "ambient": 1.15}


def _shot_density_factor(pacing: Any) -> float:
    """节奏档 → 密度系数（快切 0.85 / 长镜头 1.15 / 其余 1.0）。

    两种写法都认：`pacing={"shot_density": "快切"}`（任务卡口径），
    或 `pacing="fast_drama"`（现有 `PACING` 档位名 / `pacing_for()` 的返回）。
    """
    raw = pacing.get("shot_density") if isinstance(pacing, dict) else pacing
    key = str(raw or "").strip().lower()
    if key in _DENSITY_FACTORS:
        return _DENSITY_FACTORS[key]
    return float(_PACING_DENSITY.get(key, 1.0))


def _spoken_chars(text: Any) -> int:
    """台词有效字数：去掉空白/标点/符号（标点不吃时间，中文约 4 字/秒）。"""
    n = 0
    for ch in str(text or ""):
        if ch.isspace():
            continue
        if unicodedata.category(ch)[0] in ("P", "Z", "S"):
            continue
        n += 1
    return n


def _dialogue_texts(scene: Dict[str, Any]) -> List[str]:
    """取一场戏的全部台词文本（兼容 dict 条目 / 纯字符串 / 单数 dialogue 字段）。"""
    out: List[str] = []
    for d in scene.get("dialogues") or []:
        if isinstance(d, dict):
            t = d.get("text") or d.get("line") or d.get("content") or ""
        else:
            t = d
        t = str(t or "").strip()
        if t:
            out.append(t)
    if not out:
        one = scene.get("dialogue") or scene.get("monologue") or ""
        if isinstance(one, str) and one.strip():
            out.append(one.strip())
    return out


def _action_count(scene: Dict[str, Any]) -> int:
    """一场戏的动作条数（`actions` 写成一个字符串时算 1 条）。"""
    acts = scene.get("actions")
    if isinstance(acts, str):
        return 1 if acts.strip() else 0
    try:
        return sum(1 for a in (acts or []) if str(a or "").strip())
    except TypeError:
        return 0


def _scene_weights_detail(scenes: Optional[List[Dict[str, Any]]], *,
                          pacing: Any = None):
    """`scene_weights` 的实现体：返回 `(weights, source, detail)`。

    `source`：`"llm"`（沿用 LLM 给的相对秒数）/ `"content"`（按内容量）/ `"equal"`（读不到）。
    `weights` 为 None 表示**读不到内容** —— 调用方退回等分，不猜。
    """
    src = [s for s in (scenes or []) if isinstance(s, dict)]
    if not src:
        return None, "equal", "没有场次"

    # ① LLM 已经给了每场秒数、且**不是全部相同** → 用它的相对比例（尊重编剧意图）。
    llm = [_dur(s.get("duration_seconds")) for s in src]
    if all(d is not None for d in llm) and len(set(llm)) > 1:
        return ([float(d) for d in llm], "llm",
                "沿用 LLM 给的各场秒数作相对比例（" + "/".join(str(d) for d in llm) + "）")

    # ② 内容量：2.5 + 台词字数/4.0 + 1.2×动作条数，× beat 系数，× 节奏密度，金句 +1.5s。
    density = _shot_density_factor(pacing)
    weights: List[float] = []
    readable = 0
    parts: List[str] = []
    for s in src:
        texts = _dialogue_texts(s)
        chars = sum(_spoken_chars(t) for t in texts)
        acts = _action_count(s)
        beat = str(s.get("beat") or "")
        if texts or acts or beat.strip() or str(s.get("hook") or "").strip():
            readable += 1
        factor, hit = 1.0, ""
        for word, f in _BEAT_FACTORS:
            if word in beat:
                factor, hit = f, word
                break
        w = (2.5 + chars / 4.0 + 1.2 * acts) * factor * density
        golden = bool(s.get("golden_line"))
        if golden:
            w += 1.5
        weights.append(max(0.01, float(w)))
        parts.append(f"{chars}字+{acts}动作"
                     + (f"×{factor:g}（{hit}）" if hit else "")
                     + ("+金句1.5s" if golden else ""))
    if readable == 0:
        return None, "equal", "场次里读不到台词/动作/beat，无法按内容分权重"

    shown = "；".join(parts[:6]) + ("…（共 %d 场）" % len(parts) if len(parts) > 6 else "")
    detail = ("按内容量分配（2.5 + 台词字数/4s + 动作条数×1.2s"
              + ("" if density == 1.0 else f"，节奏密度×{density:g}")
              + f"）：{shown}")
    return [round(w, 3) for w in weights], "content", detail


def scene_weights(scenes: Optional[List[Dict[str, Any]]], *,
                  pacing: Any = None) -> Optional[List[float]]:
    """每场戏该占多少时长的**相对权重**；读不到内容返回 None（调用方退回等分）。

    优先级（任务卡 2026-10-24-022）：
      1. LLM 每场 `duration_seconds` 都在、且**不全相同** → 直接用它的比例
         （编剧已经想好了哪场该长，我们不该拿公式覆盖它）；
      2. 否则按内容量估：`2.5 + 台词字数/4.0 + 1.2×动作条数`，
         再 × beat 系数（钩子 0.75 / 高潮·爆发 1.25 / 金句 1.3 / 过场 0.7 / 收尾 1.0），
         再按节奏密度修正（快切 ×0.85 / 长镜头 ×1.15），有 `golden_line` 再 +1.5 秒；
      3. 什么都读不到 → None。

    例（真剧本 7b6ee913 两场都写 15 秒）：全部相同 → 走内容量，两场台词/动作不同，
    权重不同 → 不再是 15/15 等分。
    """
    weights, _source, _detail = _scene_weights_detail(scenes, pacing=pacing)
    return weights


class _SecondsPlan(list):
    """`List[int]` 再加一行说明（`.note` / `.source`）。

    为什么不是返回 dict：`distribute_by_weight` 的契约就是"秒数数组"，
    调用方要能直接 `sum(durs)`、`'/'.join(map(str, durs))`、`json.dumps` 出去。
    但"退回等分/凑不出 3 个取值"这种决定**必须留下痕迹**，所以挂在 list 子类的
    属性上 —— 列表行为、相等比较、JSON 序列化全部与普通 list 一致。
    """

    note: str = ""
    source: str = "weight"

    def __repr__(self) -> str:  # 保持和普通 list 一模一样的打印，日志/贴证据不跑偏
        return list.__repr__(self)


def _spread_three_values(vals: List[int], min_s: int, max_s: int,
                         want: int = 3) -> List[int]:
    """分配后若不同取值不足 `want` 个，在**合法区间内 ±1 秒**凑出 `want` 个取值。

    做法：只动一场（+1 秒），另找一场 -1 秒 → 总和不变，逐项仍夹在 [min,max]。
    在所有合法的一对里，优先选"不同取值最多、跨度最小"的那一步（改动最小、
    不把按权重分好的比例改花）。一对多少种组合都试（同值的两场互相挪 1 秒也算 ——
    `[2,2,2,5]` 就是靠 `5-1` 与 `2+1` 变成 `[2,2,3,4]` 才有 3 个取值）。

    n<3 或本来就 ≥3 个取值直接返回；**凑不出来就原样返回**（例如合法区间只容得下
    1~2 个相邻取值）—— 不硬凑、不造假。
    """
    vals = [int(v) for v in vals]
    n = len(vals)
    if n < want or len(set(vals)) >= want:
        return vals
    original = len(set(vals))
    best = None
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            for step in (1, -1):
                a, b = vals[i] + step, vals[j] - step
                if not (min_s <= a <= max_s and min_s <= b <= max_s):
                    continue
                trial = list(vals)
                trial[i], trial[j] = a, b
                d = len(set(trial))
                if d <= original:          # 没有变得更丰富，不算候选
                    continue
                span = max(trial) - min(trial)
                score = (d, -span)         # 先取值多，再跨度小
                if best is None or score > best[0]:
                    best = (score, trial)
    if best is not None and len(set(best[1])) >= want:
        return best[1]
    return vals


def distribute_by_weight(total: int, weights, *, min_seconds: int = 2,
                         max_seconds: int = 15) -> List[int]:
    """按权重把 `total` 秒分成整数秒数组：和**精确** = total，逐项夹 [min,max]。

    规则（任务卡 2026-10-24-022）：
      · 比例分配整数秒（最大余数法），和必须精确等于 total；
      · 逐项夹在 [min_seconds, max_seconds]；
      · 不可行（`min*n > total`，或 `total > max*n`）→ 退回 `distribute()` 等分，
        并在返回值的 `.note` 里写明原因（不静默改数）；
      · 分完若不同取值 <3 且 n≥3，用 ±1 秒微调凑出 3 个取值（总和不变）。

    返回值是 `List[int]`（`_SecondsPlan`，行为与 list 完全一致），额外带
    `.note`（一行说明）和 `.source`（`"weight"` / `"equal"`）。

    例：30 秒 / 8 场 / 权重 1..8 → `[2, 2, 2, 3, 4, 5, 6, 6]`（5 个取值，和 30）。
    """
    plan = _SecondsPlan()
    try:
        total = int(total)
    except Exception:
        total = 0
    try:
        min_s, max_s = int(min_seconds), int(max_seconds)
    except Exception:
        min_s, max_s = 2, 15
    min_s = max(0, min_s)
    if max_s < min_s:
        max_s = min_s
    try:
        ws = [float(w) for w in (weights or [])]
    except Exception:
        ws = []
    n = len(ws)
    if n == 0:
        plan.note = "没有权重，分不出秒数"
        plan.source = "equal"
        return plan

    # 权重不可用（0/负数/NaN/inf）→ 等分兜底，不猜。
    if any((not math.isfinite(w)) or w <= 0 for w in ws):
        fallback = _SecondsPlan(distribute(total, n))
        fallback.source, fallback.note = "equal", (
            f"权重里有 0/负数/非有限值，退回等分：{list(fallback)} 秒")
        logger.warning("distribute_by_weight: %s", fallback.note)
        return fallback

    # 区间不可行 → 等分兜底 + note（和仍然 = total）。
    if total < min_s * n or total > max_s * n:
        fallback = _SecondsPlan(distribute(total, n))
        fallback.source, fallback.note = "equal", (
            f"{total} 秒分给 {n} 场做不到每场都在 [{min_s},{max_s}] 秒"
            f"（合法和值区间 {min_s * n}~{max_s * n} 秒），退回等分：{list(fallback)} 秒")
        logger.warning("distribute_by_weight: %s", fallback.note)
        return fallback

    # ① 比例 → 取整（floor 后夹区间）。可行前提下 floor 之和 ≤ total，差额靠余数补；
    #    某些场次被抬到下限时 floor 之和可能 > total，差额靠回撤。
    sw = sum(ws)
    raw = [total * w / sw for w in ws]
    frac = [r - math.floor(r) for r in raw]
    clamped = [r < min_s or r > max_s for r in raw]
    vals = [min(max_s, max(min_s, int(math.floor(r)))) for r in raw]
    diff = total - sum(vals)

    guard = 0
    while diff != 0 and guard <= n * (max_s - min_s + 1) + n:
        moved = False
        if diff > 0:
            # 先给"没被夹过"的场次补 1（它们的余数才有意义），再向被抬到下限的场次补。
            order = (sorted([i for i in range(n) if not clamped[i]],
                            key=lambda i: (-frac[i], i))
                     + sorted([i for i in range(n) if clamped[i]],
                              key=lambda i: (-frac[i], i)))
            for i in order:
                if diff <= 0:
                    break
                if vals[i] < max_s:
                    vals[i] += 1
                    diff -= 1
                    moved = True
        else:
            # 有人被抬到下限导致总额超了 → 余数最小的先回撤。
            for i in sorted(range(n), key=lambda i: (frac[i], i)):
                if diff >= 0:
                    break
                if vals[i] > min_s:
                    vals[i] -= 1
                    diff += 1
                    moved = True
        guard += 1
        if not moved:
            break
    if diff != 0:  # 理论上不会发生（区间可行性已检查过）；发生了就走等分，不硬凑
        fallback = _SecondsPlan(distribute(total, n))
        fallback.source, fallback.note = "equal", (
            f"按权重分配后残留 {diff} 秒无法消掉（区间约束冲突），退回等分：{list(fallback)} 秒")
        logger.warning("distribute_by_weight: %s", fallback.note)
        return fallback

    # ② 取值太单调（distinct<3）→ ±1 秒微调凑 3 个取值，总和不变。
    spread = _spread_three_values(vals, min_s, max_s)
    note = (f"按权重 {total} 秒 → {'/'.join(str(v) for v in spread)} 秒"
            f"（{len(set(spread))} 个取值，合计 {sum(spread)} 秒）")
    if len(set(spread)) < 3 and n >= 3:
        note += "；区间/总时长把取值空间压死了，凑不出 3 个不同取值"
    out = _SecondsPlan(spread)
    out.note, out.source = note, "weight"
    return out


def _dur(v: Any) -> Optional[int]:
    try:
        f = float(v)
    except Exception:
        return None
    if f <= 0:
        return None
    return int(round(f))


def fit_scenes(scenes: Optional[List[Dict[str, Any]]], total: int,
               *, min_scene: Optional[int] = None,
               preferred: Any = None,
               cap: Optional[int] = None,
               pacing: Any = None,
               use_weights: bool = True) -> Dict[str, Any]:
    """把 LLM 给的场次**强制**排成"总时长 = 目标时长"，并如实报告改了什么。

    返回：
      `{"scenes": [...], "target", "actual", "scene_count", "planned_scene_count",
        "dropped": [{"title","duration_seconds","reason"}], "changed": bool,
        "before_total": n, "note": "...",
        "weights": [float] | None,
        "distribution": {"distinct": n, "values": [...], "source": "llm|content|equal",
                         "per_scene": [...]}}`

    `weights` / `distribution` 是 2026-10-24 新增（其余键不变）：
      · `weights`    = `scene_weights()` 算出的相对权重（读不到内容时 None）；
      · `distribution.values`    = 分配后**去重升序**的秒数取值；
      · `distribution.per_scene` = 每场实际秒数（和 `scenes[i].duration_seconds` 一致）；
      · `distribution.source`    = `llm`（沿用 LLM 相对时长）/ `content`（按内容量）/ `equal`（等分兜底）。

    规则：
      · 场次数上限 = `scene_count_for(total)`（5 秒 → 1 场）；
      · 超出的场次**不静默丢**：列进 `dropped` 并给出原因（时长放不下）；
      · `use_weights=True`（默认）时按**内容量**分秒（钩子短/高潮长/快切密），
        **不是**等分；`use_weights=False` 或权重读不到 → 退回旧的 `distribute()` 等分；
      · 每场秒数按目标重新分配，和 = 目标；LLM 原来写的秒数只作为"它的意图"记在
        `duration_seconds_before_fit` 里，方便人对照；
      · LLM 给的场次**少于**规划时**不硬造**场次（造出来的场次没有内容），
        只把时长摊到现有的场次上，并在 `note` 里说明。
    """
    src = [s for s in (scenes or []) if isinstance(s, dict)]
    total = int(total)
    _spec = pacing_spec(pacing)
    if preferred is None:
        preferred = float(choose_gen_for_total(total, pacing))
    if min_scene is None:
        min_scene = int(_spec.get("min") or MIN_SCENE_SECONDS)
    if cap is None:
        cap = int(_spec.get("cap") or SCENE_CAP)
    want_n = scene_count_for(total, min_scene=min_scene, preferred=preferred, cap=cap)
    before_total = 0
    for s in src:
        d = _dur(s.get("duration_seconds"))
        if d:
            before_total += d

    kept = [dict(s) for s in src[:want_n]]
    dropped_src = src[want_n:]
    n = len(kept)
    notes: List[str] = []

    if n == 0:
        return {"scenes": [], "target": total, "actual": 0, "scene_count": 0,
                "planned_scene_count": want_n,
                "dropped": [], "changed": False, "before_total": before_total,
                "weights": None,
                "distribution": {"distinct": 0, "values": [], "source": "equal",
                                 "per_scene": []},
                "note": "剧本没有场次，无法按时长重排。"}

    # ── ① 按内容量分秒（不是"段长 ÷ 镜数"）──────────────────────────────
    # 权重来源优先级：LLM 给了不等时长 → 用它；否则台词/动作/beat → 内容量；
    # 读不到内容 → None，退回旧等分（不猜）。
    weights: Optional[List[float]] = None
    source = "equal"
    basis = "秒数退回等分（读不到内容或不按权重）"
    durs: Optional[List[int]] = None
    if use_weights:
        weights, wsource, detail = _scene_weights_detail(kept, pacing=pacing)
        if weights:
            # 每场秒数的上限 = `total - min×(n-1)`（别人都只拿硬下限时，某一场最多能占多少），
            # 且不低于 15。为什么不用写死 15：30 秒 2 场在 [5,15] 里的唯一合法和值是 15+15，
            # 那等于又回到等分；一场 >15 秒是合法的（这条素材会拆成多次生成，见 cuts_for）。
            # 这个上限是从"和 = total、每场 ≥ min"推出来的，保证区间**一定可行**，
            # 不会因为我们自己的 max 把总时长卡成"无解 → 退回等分"。
            # ★ 2026-10-24：单次生成上限 15s（seedance 2.0/2.5）。生成端还没接
            #   `cuts_per_generation` 拆段，所以规划层不能给出 >15s 的单场，否则
            #   会被厂商拒或抬价。2 场 × 30s 的唯一合法解就是 15+15（等于旧行为）。
            max_sec = min(15, max(int(min_scene), total - int(min_scene) * (n - 1)))
            plan = distribute_by_weight(total, weights,
                                        min_seconds=int(min_scene),
                                        max_seconds=max_sec)
            durs = list(plan)
            if getattr(plan, "source", "") == "equal":
                source, basis = "equal", str(getattr(plan, "note", "") or basis)
            else:
                source = wsource
                basis = detail
                # 秒数本身下面会写，这里只并入额外的结论（例如"凑不出 3 个取值"）。
                extra = str(getattr(plan, "note", "") or "")
                if "凑不出" in extra:
                    basis += "；" + extra
    if durs is None:
        durs = distribute(total, n)
        source = "equal"
        if not use_weights:
            basis = "use_weights=False，按等分（旧行为）"

    for i, s in enumerate(kept):
        before = _dur(s.get("duration_seconds"))
        if before is not None and before != durs[i]:
            s["duration_seconds_before_fit"] = before
        s["duration_seconds"] = durs[i]

    values = sorted(set(durs))
    distribution = {"distinct": len(values), "values": [int(v) for v in values],
                    "source": source, "per_scene": [int(d) for d in durs]}
    notes.append(
        f"{basis} —— 每场 {'/'.join(str(d) for d in durs)} 秒"
        f"（合计 {sum(durs)} 秒，{len(values)} 个不同取值"
        + (f"：{'/'.join(str(v) for v in values)}" if values else "")
        + f"；source={source}）。")

    dropped = []
    for s in dropped_src:
        dropped.append({"title": str(s.get("title") or f"第{s.get('scene_number')}场"),
                        "duration_seconds": _dur(s.get("duration_seconds")),
                        "reason": (f"总时长只有 {total} 秒，按每场至少 {min_scene} 秒"
                                   f"最多容得下 {want_n} 场，这一场没排进来")})

    if dropped:
        notes.append(
            f"你要的是 {total} 秒，最多排 {want_n} 场（每场至少 {min_scene} 秒）；"
            f"AI 写了 {len(src)} 场，已只保留前 {n} 场、并把秒数改成 "
            f"{'/'.join(str(d) for d in durs)} 秒；"
            f"被裁掉的 {len(dropped)} 场："
            + "、".join(f"「{d['title']}」" for d in dropped[:6])
            + ("…" if len(dropped) > 6 else "")
            + "。想保留它们就把总时长调大，或让 AI 减少场次重新生成。")
    if n < want_n:
        notes.append(
            f"AI 只写了 {n} 场（按时长本可以排 {want_n} 场）—— 没有凭空造场次，"
            f"而是把 {total} 秒摊到现有场次上（每场 "
            f"{'/'.join(str(d) for d in durs)} 秒）。")
    if not notes:
        notes.append(f"已按 {total} 秒重排：{n} 场，每场 "
                     f"{'/'.join(str(d) for d in durs)} 秒（合计 {sum(durs)} 秒）。")

    changed = bool(dropped) or any(
        _dur(src[i].get("duration_seconds")) != durs[i] for i in range(n))
    return {"scenes": kept, "target": total, "actual": sum(durs),
            "scene_count": n, "planned_scene_count": want_n,
            "dropped": dropped, "changed": changed, "before_total": before_total,
            "weights": [float(w) for w in weights] if weights else None,
            "distribution": distribution,
            "note": " ".join(notes)}


def duration_brief(fit: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """给接口/界面用的精简版（不把整份场景再传一遍）。"""
    f = fit or {}
    return {"target": f.get("target"), "actual": f.get("actual"),
            "scene_count": f.get("scene_count"),
            "planned_scene_count": f.get("planned_scene_count"),
            "before_total": f.get("before_total"),
            "dropped": f.get("dropped") or [], "changed": bool(f.get("changed")),
            "note": f.get("note") or "",
            "ok": (f.get("target") is not None and f.get("actual") == f.get("target"))}
