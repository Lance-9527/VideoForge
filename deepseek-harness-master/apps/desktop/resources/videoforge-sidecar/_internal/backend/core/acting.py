# -*- coding: utf-8 -*-
"""VideoForge · biaoqing-yanyi 融合进「分镜生成时的角色微表情控制」

═══════════════════════════════════════════════════════════════
先说这个模块修的是什么真问题（有实测证据）
═══════════════════════════════════════════════════════════════
用户要的是"分镜生成时候角色微表情控制"。查下来发现链路**断在最后一公里**：

  · 分镜的 `layer2_timeline` 里**有** `expression` 字段（LLM 生成了，如「沉静而坚定」）
  · 但 `core/videoprompt.py` 里 `expression` 出现 **0 次** ——
    拼视频提示词时只读了 `action` / `camera` / `shot_size`，
    表情**从来没进过视频模型的提示词**。

实测对照（scripts/_probe_expression_reach.py）：
  两个只在 expression 上不同的镜头
    A: expression = "平静克制"
    B: expression = "歇斯底里地嚎啕大哭，脸崩成卡通表情"
  生成出来的视频提示词**逐字节完全相同**，都是 330 字符，
  A/B 的提示词里都找不到自己那句表情词。

所以「微表情控制不了」不是模型不行，是**那句话根本没送到**。

本模块做两件事：
  1. 把扁平的 `emotion` 标签（"resolute" / "坚定"）展开成 skill 要求的
     **按秒推进的表演结构**：人格气质 → 表达态度 → 情绪波 → 声音结构 →
     身体动作 → 视觉参数（含时间码）。
  2. 让这份结构**真的到达视频模型**（配合 `videoprompt.py` 的改动）。

═══════════════════════════════════════════════════════════════
「跟剧本设定要契合」
═══════════════════════════════════════════════════════════════
skill 的核心问题是 **"Who is this line spoken to?"**（这句话说给谁听）。
VideoForge 里有现成依据，本模块按证据强度依次取：

  1. 同一镜的 `character_ids` 里除说话人之外的角色 —— **最硬**（人确实在画面里）
  2. 说话人人物卡的 `relationships`（"李队长的得力部下，战斗中的主力"）——
     给出"关系位置"，决定态度是请求/试探/顶撞/告别
  3. 场次的 `characters` 列表
  4. 都没有 → 如实写「未确定听者」，**不编**

情绪基调同理：优先用台词的 `emotion`，其次场次 `mood`，都没有就按 skill 的
`no_emotion_default`（冷静克制）走，并在报告里标明**依据是哪一层**。

═══════════════════════════════════════════════════════════════
两条必须守住的工程约束
═══════════════════════════════════════════════════════════════
1. **`expression` 必须保持短。**
   `postprocess.generate_srt_from_timeline()` 在**没有台词**的时间片上会把
   `action +（expression）` 当字幕（该文件 447-461 行有记录：原来无条件这么干，
   字幕变成了"画面说明"）。把长段表演描述塞进 `expression` 会把字幕撑爆。
   所以：`expression` 只放**一句可观察的微表情**（≤ 约 30 字）；
   完整时间码表演放独立的 `performance` 字段，只有视频提示词构造器读。

2. **不能静默丢字段。**
   `dialogue.normalize_timeline()` 会用一个**固定白名单重建**每个时间片
   （start/end/action/expression/camera/props_used/dialogue）。
   新加的 `performance` 如果不进白名单，会在下一次规范化时**无声消失**。
   `dialogue.py` 已相应加了透传，并且 `tests/test_acting.py` 里有测试守着这条。

纯函数，不碰数据库、不碰网络。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger("videoforge.acting")


# ──────────────────────── 规则加载 ────────────────────────

_FALLBACK_RULES: Dict[str, Any] = {
    "_fallback": True,
    "core_chain": ["personality temperament", "expression attitude", "emotional wave",
                   "voice rhythm", "body action", "visual parameters"],
    "emotional_wave": ["hidden", "slight leak", "pull back", "leak again", "peak", "aftertaste"],
    "key_question": "Who is this line spoken to?",
    "voice_rules": {
        "low_frequency": "Low frequency = stable, restrained, suppressed, controlled.",
        "high_frequency": "High frequency = slight emotional lift, coquettishness, grievance, urgency.",
        "pause": "Pause = thinking, hesitation, self-control, or something unsaid.",
        "breath_point": "Breath point = where emotion leaks out.",
    },
    "standard_base": {"aspect_ratio_in_base": "9:16"},
    "timecode_patterns": {
        "6": [
            {"range": [0, 1], "beat": "hold the first emotional mask; eyes, lips, shoulders, and breath are restrained."},
            {"range": [1, 2], "beat": "emotion starts to surface through gaze, eyelids, breath, jaw, or mouth corner."},
            {"range": [2, 3], "beat": "body reacts slightly; head angle, weight, hand, or shoulders change."},
            {"range": [3, 5], "beat": "line is spoken with designed voice rhythm and emotional color."},
            {"range": [5, 6], "beat": "aftertaste; emotion cools, breaks, hardens, retreats, or softens."},
        ],
    },
    "formulas": {
        "cold_calm": {"label_zh": "冷静 / 克制",
                      "rule": "几乎不动。情绪压在眼神与呼吸节奏里：目光平直、眨眼少、唇线平。"},
    },
    "emotion_lexicon": {"冷静": "cold_calm", "calm": "cold_calm"},
    "no_emotion_default": "cold_calm",
    "negative_limits_zh": ["面部不崩不形变", "嘴不能张得过大", "不夸张开合",
                           "不重影、不脸漂、不身份漂移", "不出现多余人或肢体", "不烧字幕不打水印"],
    "never_start_at_peak": True,
}

_RULES_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def load_rules(*, force: bool = False) -> Dict[str, Any]:
    """从内置 skill 目录读 acting 规则。读不到就用兜底并**标记出来**。"""
    global _RULES_CACHE
    try:
        from . import skills as SK
        import os
        data_dir = os.environ.get("VIDEOFORGE_DATA_DIR") or os.getcwd()
        reg = SK.get_registry(data_dir)
        sk = reg.get("biaoqing-yanyi")
        if sk is None:
            raise SK.SkillError("注册表里没有 biaoqing-yanyi")
        mt = sk.rules_path.stat().st_mtime if sk.rules_path.is_file() else 0.0
        key = str(sk.rules_path)
        hit = _RULES_CACHE.get(key)
        if hit and not force and hit[0] == mt:
            return hit[1]
        r = reg.rules("biaoqing-yanyi")
        if "_missing" in r or "_error" in r:
            raise SK.SkillError(str(r.get("_missing") or r.get("_error")))
        r = dict(r)
        r["_source"] = str(sk.rules_path)
        _RULES_CACHE[key] = (mt, r)
        return r
    except Exception as e:
        logger.warning(
            "读不到 biaoqing-yanyi 的 rules.json（用内置安全兜底，"
            "情绪公式会降级成通用情绪波）：%s", e)
        fb = dict(_FALLBACK_RULES)
        fb["_fallback_reason"] = "%s: %s" % (type(e).__name__, e)
        return fb


# ──────────────────────── 工具 ────────────────────────


def _as_dict(v: Any) -> Dict[str, Any]:
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            o = json.loads(v)
        except Exception:
            return {}
        return o if isinstance(o, dict) else {}
    return {}


def _as_list(v: Any) -> List[Any]:
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            o = json.loads(v)
        except Exception:
            return []
        return o if isinstance(o, list) else []
    return []


def _norm(s: Any) -> str:
    return re.sub(r"[\s·・\.\-—_]+", "", str(s or ""))


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _rf_of(char: Dict[str, Any]) -> Dict[str, Any]:
    return _as_dict((char or {}).get("reference_features"))


# ──────────────────────── 说话人 / 听者 ────────────────────────


def resolve_listener(
    speaker: str,
    *,
    shot: Optional[Dict[str, Any]] = None,
    scene: Optional[Dict[str, Any]] = None,
    characters: Optional[Sequence[Dict[str, Any]]] = None,
    speaker_card: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """回答 skill 的核心问题：这句话**说给谁听**。

    按证据强度依次取，并把**依据**写进返回值 —— 让用户能核对，
    也能一眼看出这个是"画面里确实有人"还是"只能靠猜"。
    """
    shot = shot or {}
    scene = scene or {}
    characters = list(characters or [])
    by_id = {str(c.get("id")): c for c in characters if isinstance(c, dict)}
    by_name = {str(c.get("name") or ""): c for c in characters if isinstance(c, dict)}
    sn = _norm(speaker)

    # ★ 调用方可能没显式传 speaker_card（很容易忘）。这里自己从 `characters`
    #   里找一次 —— 少一个"忘了传就默默降级"的入口。
    if speaker_card is None and characters:
        speaker_card = _match_card(speaker, characters)

    # ── 1) 同镜出现的其它角色（最硬：人确实在画面里）──
    present: List[Dict[str, Any]] = []
    for cid in _as_list(shot.get("character_ids")):
        c = by_id.get(str(cid))
        if c and _norm(c.get("name")) != sn:
            present.append(c)
    if present:
        names = [str(c.get("name") or "") for c in present]
        return {
            "name": "、".join(n for n in names if n),
            "names": [n for n in names if n],
            "basis": "同镜角色（shot.character_ids）",
            "strength": "strong",
            "in_frame": True,
        }

    # ── 2) 场次角色表里除说话人之外的人 ──
    scene_names = [str(x) for x in _as_list(scene.get("characters"))]
    others = [n for n in scene_names if _norm(n) and _norm(n) != sn]
    if others:
        return {"name": "、".join(others), "names": others,
                "basis": "场次角色表（scene.characters）",
                "strength": "medium", "in_frame": None}

    # ── 3) 说话人自己的关系描述（决定"态度"，但没给出具体听者）──
    rel = str(_rf_of(speaker_card or {}).get("relationships") or "").strip()
    if rel:
        return {"name": "", "names": [], "basis": "只有人物关系描述，画面里没别人",
                "strength": "weak", "in_frame": False, "relationship": rel}

    # ── 4) 如实说不知道，不编 ──
    return {"name": "", "names": [], "basis": "未找到听者依据（镜头与场次都没有别的角色）",
            "strength": "none", "in_frame": False}


def _match_card(name: str,
                characters: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """在角色列表里按名字找卡（忽略空格/间隔号，双向包含，长度≥2 才认）。"""
    n = _norm(name)
    if len(n) < 2:
        return None
    for c in characters:
        if not isinstance(c, dict):
            continue
        cn = _norm(c.get("name"))
        if cn and (cn == n or n in cn or cn in n):
            return c
    return None


# ──────────────────────── 公式选择 ────────────────────────


def pick_formula(emotion: Any, *, text: str = "", scene_mood: Any = "",
                 rules: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """把情绪词映射到 skill 里的一个情绪公式。

    返回 {formula, label_zh, basis}，`basis` 说明这个判断是**从哪来的**：
      emotion 标签 / 场次情绪 / 默认值。不猜、不编。
    """
    rules = rules or load_rules()
    lex = {str(k).strip().lower(): v for k, v in (rules.get("emotion_lexicon") or {}).items()}
    formulas = rules.get("formulas") or {}
    default = str(rules.get("no_emotion_default") or "cold_calm")

    def look(raw: Any) -> Optional[str]:
        s = str(raw or "").strip()
        if not s:
            return None
        cands = [s.lower()]
        # 情绪有时写成 "curious/好奇" 或 "坚定,resolute"
        cands += [p.strip().lower() for p in re.split(r"[/,，、;；\s]+", s) if p.strip()]
        for c in cands:
            # ★ 情绪词直接等于公式名（"angry" / "wronged" / "resolute"）时直接命中。
            #   词表里没必要为每个公式再抄一遍自己 —— 抄了就容易漏（实测漏过
            #   angry/wronged，它们被静默降级成了默认的冷 calm）。
            if c in formulas:
                return c
            if c in lex:
                return lex[c]
        # 再做一次包含匹配（"略微委屈" → "委屈"；"紧张但坚定" → 紧张/坚定 二选一）
        #
        # ★ 命中规则必须**确定**，不能靠字典顺序。
        #   原来写的是 `for k, v in lex.items(): if k in s`，取到的是
        #   "字典里排在前面的那个" —— 换一次词表顺序，同一个情绪就会
        #   选到不同公式（实测 "紧张但坚定" 会先命中"坚定"，
        #   换成 anxious 还是 resolute 全看 lex 的插入顺序）。
        #   现在：先看**更长的键**（更具体），长度相同看**谁在句子里出现得更早**
        #   （通常先说出口的是主要情绪），再相同就按字典序，保证可复现。
        s_low = s.lower()
        cands_hit = [(k, v, s_low.find(k)) for k, v in lex.items() if k and k in s_low]
        if cands_hit:
            cands_hit.sort(key=lambda x: (-len(x[0]), x[2], x[0]))
            return cands_hit[0][1]
        return None

    for label, raw, basis in (("台词情绪", emotion, "台词的 emotion 字段"),
                              ("场次情绪", scene_mood, "场次的 mood 字段")):
        f = look(raw)
        if f:
            info = formulas.get(f) or {}
            return {"formula": f, "label_zh": str(info.get("label_zh") or f),
                    "basis": "%s（%s：%r → %s）" % (label, basis, str(raw)[:24], f)}

    info = formulas.get(default) or {}
    return {"formula": default, "label_zh": str(info.get("label_zh") or default),
            "basis": "没有情绪标签也没有场次情绪，按 skill 默认的克制基调（%s）" % default}


# ──────────────────────── 时间码 ────────────────────────


def _scale_pattern(pattern: Sequence[Dict[str, Any]], start: float, end: float
                   ) -> List[Dict[str, Any]]:
    """把模板的区间**按比例**映射到 [start, end]。

    模板自己的总长 = 最后一段的 range[1]；各段按占比缩放。
    """
    if not pattern or end <= start:
        return []
    total = 0.0
    for p in pattern:
        r = p.get("range") or [0, 0]
        if len(r) >= 2:
            total = max(total, _f(r[1]))
    if total <= 0:
        return []
    span = end - start
    out = []
    for p in pattern:
        r = p.get("range") or [0, 0]
        if len(r) < 2:
            continue
        a = start + span * (_f(r[0]) / total)
        b = start + span * (_f(r[1]) / total)
        item = {
            "start": round(a, 2), "end": round(b, 2),
            "beat": str(p.get("beat") or "").strip(),
            "orig_range": [r[0], r[1]],
        }
        # ★★ 必须把 beat_zh 一起带过来。
        #   这里原来是**重建** dict 的，只挑了几个字段 —— 于是 rules.json 里
        #   辛苦补的中文节拍在缩放时被丢掉，提示词里又变回英文
        #   （实测输出 "起手[未译]hold the first emotional mask"）。
        #   凡是"重建 dict"的地方都要当心这一类静默丢字段。
        if p.get("beat_zh"):
            item["beat_zh"] = str(p["beat_zh"]).strip()
        out.append(item)
    return out


def _beats_for(formula_info: Dict[str, Any], start: float, end: float,
               rules: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], str]:
    """挑时间码模板：优先用公式自带的 progression，退化到通用 6 秒/15 秒模板。"""
    prog = formula_info.get("progression")
    if isinstance(prog, list) and prog:
        return _scale_pattern(prog, start, end), "公式自带 progression"
    pats = rules.get("timecode_patterns") or {}
    span = max(0.1, end - start)
    # 选总长最接近的模板
    best_key, best_gap = None, None
    for k, v in pats.items():
        try:
            kn = float(k)
        except (TypeError, ValueError):
            continue
        gap = abs(kn - span)
        if best_gap is None or gap < best_gap:
            best_key, best_gap = k, gap
    if best_key is None:
        return [], "没有可用的时间码模板"
    return _scale_pattern(pats[best_key], start, end), "通用 %s 秒模板（按 %0.1f 秒缩放）" % (
        best_key, span)


# ──────────────────────── 情绪波 ────────────────────────


def _wave_for(beats: Sequence[Dict[str, Any]], rules: Dict[str, Any]) -> List[str]:
    """把 skill 的 6 段情绪波均匀分配到节拍上。

    ★ 无论节拍多少，**形状必须完整**：第一拍是 hidden（压住）、最后一拍是
      aftertaste（余味）。这也是 skill 的硬要求之一
      （"Starting at the emotional peak" 属于明确要避免的写法）。
      只做 `wave[:n]` 的截断会在节拍少于 6 个时把 aftertaste 整个丢掉 ——
      实测 5 拍时末段变成 peak，等于每条镜头都收在情绪最高点，
      这正是 skill 要求避免的"起手/收尾都在峰值"。
    """
    wave = [str(x) for x in (rules.get("emotional_wave") or [])] or \
        ["hidden", "slight leak", "pull back", "leak again", "peak", "aftertaste"]
    n = len(beats)
    if n <= 0:
        # ★ 没有节拍就**不该有情绪波**。
        #   这里原来返回 `wave[:3]` —— 于是一条"描述不存在节拍"的波形被写进
        #   `layers["3_wave"]`：报告里看着有「压住 → 微微泄漏 → 收回」，
        #   而实际没有任何时间码节拍承载它。消费方（比如审计里
        #   "不从情绪峰值起手"那条）还会因此**白白通过**。
        #   返回空列表 = 如实表达"这一镜没算出节拍"，而不是编一个形状。
        return []
    if n == 1:
        # 单个节拍承载整段，形状无法完整（起手与余味只能二选一）。
        # 取 hidden：skill 明确禁止"从情绪峰值起手"，这一头是硬要求。
        return [wave[0]]
    # 在完整波形上均匀取样：index 0 → wave[0]，index n-1 → wave[-1]
    return [wave[round(i * (len(wave) - 1) / (n - 1))] for i in range(n)]


# ──────────────────────── 表演构造 ────────────────────────


def build_performance(
    *,
    line: str,
    emotion: Any = "",
    speaker: str = "",
    speaker_card: Optional[Dict[str, Any]] = None,
    listener: Optional[Dict[str, Any]] = None,
    scene_mood: Any = "",
    start: float = 0.0,
    end: float = 0.0,
    aspect_ratio: str = "16:9",
    camera: str = "",
    shot_size: str = "",
    rules: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """按 skill 的六层结构，为一个时间片造出可执行的表演指令。

    ★ 视觉参数层里的宽高比**用镜头自己的比例**，不用 skill 基底里写死的 9:16 ——
      实测库里大量镜头是 16:9，照搬竖屏指令会和项目的横屏成片互相打架。
      `rules.standard_base.aspect_override_rule` 就是这么要求的。
    """
    rules = rules or load_rules()
    speaker_card = speaker_card or {}
    rf = _rf_of(speaker_card)
    formulas = rules.get("formulas") or {}

    picked = pick_formula(emotion, text=line, scene_mood=scene_mood, rules=rules)
    fi = formulas.get(picked["formula"]) or {}
    beats, beat_src = _beats_for(fi, start, end, rules)
    wave = _wave_for(beats, rules)

    # 层 1：人格气质（来自人物卡）
    personality = str(speaker_card.get("personality") or rf.get("personality") or "").strip()
    personality_basis = "人物卡 personality" if personality else ""

    # 层 2：表达态度（由"说给谁听" + 公式决定）
    listener = listener or {"name": "", "basis": "未提供", "strength": "none"}
    att_bits = []
    if listener.get("name"):
        att_bits.append("说给「%s」听" % listener["name"])
    elif listener.get("relationship"):
        att_bits.append("（听者未在画面中）人物关系：%s" % listener["relationship"])
    else:
        att_bits.append("听者未确定")
    # ★★ 中文优先（2026-09-26 补，和第 4/5 层与 beats 保持一致）
    #
    # `rules.json` 的每个 formula 同时给了英文原文和中文版：
    #     use_when    "Use when someone maintains dignity before admitting a painful truth."
    #     use_when_zh "用在一个人还想保住体面、却不得不承认一件痛苦事实的时候"
    #     key_details / key_details_zh（同理）
    # 而这里原来只读英文的 `rule` / `use_when`，`state_chain` 也是英文为主
    # （实测：["hurt","denial","excuse","forced smile","emotional backlash",
    #          "整理体面","truth"] —— 中英混在一根链子里）。
    #
    # ⚠ **现状**：层 2 目前**还没有消费方**（`performance_prompt_lines` 只用
    #    `formula_zh` / `beat_zh` / `expression_full` / `4_voice`；
    #    `audit_timeline` 也不读它）—— 和 `5_body` 一样。
    #    所以这一处**今天不会**把英文送进提示词；改它是为了两件事：
    #      ① 将来谁把层 2 接进提示词，不会重演"中英混杂"（本仓为
    #         beat / voice_rules / body_action_pool 三处都栽过一次）；
    #      ② 用户改了 skill 的 rules.json（skill 学习）时，中文版能生效。
    #    这条"没有消费方"的事实要如实记着，别以为它已经在影响出片。
    if fi.get("rule_zh"):
        att_bits.append(str(fi["rule_zh"]))
    elif fi.get("rule"):
        att_bits.append(str(fi["rule"]))
    elif fi.get("use_when_zh"):
        att_bits.append(str(fi["use_when_zh"]))
    elif fi.get("use_when"):
        att_bits.append(str(fi["use_when"]))
    _chain = fi.get("state_chain_zh") or fi.get("state_chain")
    if _chain:
        att_bits.append("状态链：" + " → ".join(map(str, _chain)))
    attitude = "；".join(att_bits)

    # 层 4：声音结构
    # ★ 声音规则也优先用中文（skill 原文是英文，规则里给了 voice_rules_zh）。
    #   中英混杂会让提示词读起来别扭，也影响模型对"这条指令在说什么"的判断。
    vr = rules.get("voice_rules_zh") or rules.get("voice_rules") or {}
    voice_bits = []
    for k in ("low_frequency", "high_frequency", "pause", "breath_point"):
        if vr.get(k):
            voice_bits.append(str(vr[k]))
    if fi.get("voice_zh"):
        voice_bits = [str(x) for x in fi["voice_zh"]] + voice_bits
    elif fi.get("voice"):
        voice_bits = [str(x) for x in fi["voice"]] + voice_bits
    voice_structure = " ".join(voice_bits)

    # 层 5：身体动作（只允许小幅写实动作）
    # ★ 与声音规则同理：skill 原文的 `body_action_pool` 是**英文**的
    #   （lowering head / lifting eyes …）。中文提示词必须用中文那一份，
    #   否则一旦有人把这个字段接进提示词或界面，就会重演 beat 那次的中英混杂
    #   （实测出现过 "起手hold the first emotional mask"）。
    #   ⚠ 现状说明：`5_body` 目前**还没有消费方** —— `performance_prompt_lines`
    #     和 `audit_timeline` 都没读它。带着它是因为它是 skill 六层结构的一部分，
    #     但这一条要如实记着，别以为它已经在影响出片。
    body_pool = [str(x) for x in (rules.get("body_action_pool_zh")
                                 or rules.get("body_action_pool") or [])]
    body = body_pool[:3]

    # 层 6：视觉参数（★ 比例用镜头的）
    base = rules.get("standard_base") or {}
    overridden = []
    if aspect_ratio and aspect_ratio != base.get("aspect_ratio_in_base"):
        overridden.append("aspect_ratio=%s（覆盖 skill 基底的 %s）"
                          % (aspect_ratio, base.get("aspect_ratio_in_base")))
    if camera and str(camera).lower() not in ("", "static"):
        overridden.append("camera=%s（以分镜写的运镜为准，不用基底的 fixed camera）" % camera)
    visual = {
        "aspect_ratio": aspect_ratio or "16:9",
        "camera": camera or "static",
        "shot_size": shot_size,
        "continuity": "保持同一身份、脸型、五官比例、发型、服装、妆容",
        "realism": "真实皮肤质感、真实眼球运动、真实呼吸、细微微表情",
        "overrides": overridden,
    }

    # 一句话微表情（**必须短** —— 它会进字幕回退路径）
    short = _short_expression(picked["formula"], fi, listener, line)
    # 给视频模型的时间码表演描述
    timeline_text = _timeline_text(beats, wave)

    return {
        "skill": "biaoqing-yanyi",
        "formula": picked["formula"],
        "formula_zh": picked["label_zh"],
        "formula_basis": picked["basis"],
        "listener": listener,
        "layers": {
            "1_personality": personality,
            "1_personality_basis": personality_basis,
            "2_attitude": attitude,
            "3_wave": wave,
            "4_voice": voice_structure,
            "5_body": body,
            "6_visual": visual,
        },
        "beats": beats,
        "beat_source": beat_src,
        "expression_short": short,
        "timeline_text": timeline_text,
        "negatives": [str(x) for x in (rules.get("negative_limits_zh") or [])],
        "never_start_at_peak": bool(rules.get("never_start_at_peak")),
    }


_QA_WORDS = ("吗", "呢", "吧", "?", "？")


def _short_expression(formula: str, fi: Dict[str, Any], listener: Dict[str, Any],
                      line: str) -> str:
    """压成**一句可观察的微表情**，目标 ≤ 30 字（要能安全进字幕回退路径）。

    ★ `line` 这个参数以前**收了却没用**，于是"视线落在谁身上"被无条件追加 ——
      对**没有台词的镜头**（可能是空镜、环境镜、道具特写）就凭空编了一个
      视线方向。实测那一段 action 是「远处尘土扬起」、`visual_focus=环境`，
      提示词里却写「视线主要落在李队长身上」—— 画面里根本没有人在看。
      「眼神路径」是 skill 的硬要求，但只在**确实有人在说话**时才有意义。
    """
    table = {
        "cold_calm": "目光平直、眨眼少、唇线压平",
        "resolute": "下颌微收、目光定住不闪避、唇形小而明确",
        "angry": "怒先压在眼里、眼睑收紧、抿唇、肩背绷住",
        "wronged": "眼睛发湿、视线下垂、先抿唇再松开",
        "restrained_crying": "眼眶发红忍住泪、喉部发紧、嘴角下压",
        "before_crying": "眼眶发红仍想微笑、先避开视线、呼吸发抖",
        "gentle_concern": "目光先落在对方身上、唇边一点笑意随即收平",
        "coquettish": "眼睛先软、嘴角后翘、头部略微偏斜",
        "playful": "眼神放轻、唇角小幅上扬",
        "disbelieving_laugh": "眼睛先露出不信、唇角随后才提",
        "disgusted": "微微后仰、眼睛眯起、唇动极小",
        "cold_farewell": "眼睑下垂、面部静止、不做挽留的目光",
        "restrained_joy": "眼睛先亮、嘴角随后动，笑意能收住",
        "anxious": "呼吸浅快、视线短促移动、先抿唇",
    }
    # ⚠ 死数据提示（2026-09-26 核对）：`rules.json` 的每个 formula 还给了
    #   `key_details` / `key_details_zh`（各约 6 条可观察细节，例如
    #   "眼眶发红，但还想笑一下" / "一开始避开直视"），而**没有任何代码读它们**
    #   —— `build_performance` / `performance_prompt_lines` / `audit_timeline`
    #   都不读。下面这张表其实是那份数据的**手写摘要**（14 条对 14 个公式）。
    #
    #   没有直接改成读 `key_details_zh`：那会改变 `expression_short` 的内容，
    #   而它同时喂**视频提示词**和**字幕回退路径**（见本函数说明），
    #   且 `tests/test_acting.py` 里有针对现有措辞的断言
    #   （例如"短表情里有视线信息"）。要改就得连着那些断言一起改，
    #   并且**必须能跑测试**。列为待办。
    s = table.get(formula)
    if not s:
        # ★★ 兜底也必须**中文优先**（2026-09-26 补）。
        #
        # 这一条比层 2 更要紧：`expression_short` 会进 `expression_full`，
        # 再进**视频提示词**；而 `enrich_timeline` 还会把它压到 30 字写回
        # `seg["expression"]`，那是**字幕回退路径**（无台词时间片上
        # `action +（expression）` 就是字幕）。
        # 原来这里只读英文 `rule` / `use_when` —— 今天恰好被上面那张
        # 14 条硬编码表挡住（表刚好覆盖 rules.json 现有的 14 个公式），
        # 所以**暂时**没漏。但只要 rules.json 多出一个公式（用户用
        # `update_skill` 追加经验、或换一份 skill 就是这种情况），
        # 这里就会把英文原文截 26 字塞进提示词和字幕里。
        # 本仓为这种"中英混杂"栽过一次：实测出现过 "起手hold the first emotional mask"。
        rule = str(fi.get("rule_zh") or fi.get("rule")
                   or fi.get("use_when_zh") or fi.get("use_when") or "").strip()
        s = rule[:26] if rule else "神情克制、以眼神变化为主"
    # ★ 只在**这一句真的有人说出口**时，才补"视线落在谁身上"。
    #   无台词段不知道谁在看谁，编一个视线方向就是给模型错的指令
    #   （而且它还会进字幕回退路径，见本模块 docstring 的约束 1）。
    if str(line or "").strip() and listener.get("in_frame") is True and listener.get("name"):
        s += "，视线主要落在%s身上" % str(listener["name"]).split("、")[0]
    return s[:60]


def _timeline_text(beats: Sequence[Dict[str, Any]], wave: Sequence[str]) -> str:
    """把节拍压成一行**给视频模型看**的时间码描述。"""
    if not beats:
        return ""
    parts = []
    for i, b in enumerate(beats):
        w = wave[i] if i < len(wave) else ""
        rng = "%gs-%gs" % (b["start"], b["end"])
        beat = str(b.get("beat") or "").strip()
        if len(beat) > 90:
            beat = beat[:87] + "..."
        parts.append("%s[%s] %s" % (rng, w, beat))
    return "；".join(parts)


# ──────────────────────── 时间线富化（主入口）────────────────────────


def _card_by_name(name: str,
                  characters: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    n = _norm(name)
    if len(n) < 1:
        return None
    for c in characters:
        if not isinstance(c, dict):
            continue
        cn = _norm(c.get("name"))
        if cn and (cn == n or (len(n) >= 2 and (n in cn or cn in n))):
            return c
    return None


def enrich_timeline(
    timeline: Any,
    *,
    shot: Optional[Dict[str, Any]] = None,
    scene: Optional[Dict[str, Any]] = None,
    characters: Optional[Sequence[Dict[str, Any]]] = None,
    script_scenes: Optional[Sequence[Dict[str, Any]]] = None,
    aspect_ratio: str = "",
    rules: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """给分镜时间线做微表情富化。

    返回 {"timeline": [...], "report": {...}}。
    `report` 必须能回答：**哪几段被富化了、依据是什么、哪几段没做成以及为什么**。
    没有这份报告，这个功能就会变成"看起来跑了但什么都没发生"的静默 no-op ——
    这个仓库里已经有过好几个这种 bug。

    不修改传入的原始对象（深拷贝语义），便于调用方对比前后。
    """
    rules = rules or load_rules()
    shot = shot or {}
    characters = list(characters or [])
    tl = _as_list(timeline)
    if not isinstance(timeline, list):
        timeline = tl
    aspect_ratio = aspect_ratio or str(shot.get("aspect_ratio") or "16:9")

    # 场次情绪：shot 里没有就按场号去剧本里找
    scene_mood = str((scene or {}).get("mood") or "")
    if not scene_mood and script_scenes:
        sn = (scene or {}).get("scene_number")
        for s in script_scenes:
            if isinstance(s, dict) and (sn is None or s.get("scene_number") == sn):
                scene_mood = str(s.get("mood") or "")
                if scene_mood:
                    break

    out: List[Dict[str, Any]] = []
    report: Dict[str, Any] = {
        "skill": "biaoqing-yanyi",
        "rules_source": rules.get("_source") or ("内置兜底" if rules.get("_fallback") else "?"),
        "segments_total": 0, "enriched": 0, "dialogue_segments": 0,
        "silent_segments": 0, "unrecognized": 0, "skipped": [],
        "details": [],
        "listener_strength": {},
    }
    if rules.get("_fallback"):
        report["rules_fallback_reason"] = rules.get("_fallback_reason", "")

    for it in timeline:
        if not isinstance(it, dict):
            # ★ 上游偶尔会把时间线写成**字符串数组**（`["他抬头", "他握枪"]`）。
            #   这种条目做不了微表情富化，只能原样透传 ——
            #   但**必须计数并说明**：否则报告里会出现
            #   「enriched == segments_total，全部富化」这种话，
            #   而实际上有几条根本没被处理。这正是本仓反复踩的
            #   "指标好看、事情没做"那一类。`skipped` 也不再是永远为空的死字段。
            report["unrecognized"] += 1
            if len(report["skipped"]) < 8:
                report["skipped"].append({
                    "reason": "时间线条目不是对象（%s），无法做微表情富化，已原样透传"
                              % type(it).__name__,
                    "value": str(it)[:60],
                })
            out.append(it)
            continue
        report["segments_total"] += 1
        seg = dict(it)                       # 浅拷贝，不原地改传进来的对象
        # ★★ 先抓住**上一轮的表演结构**（若有）。用途只有一个：保证幂等。
        #   必须在下面 `seg["performance"] = perf` **之前**取 —— 那行会把它覆盖掉。
        #   背景见 `old_expr` 那里的长注释。
        _prev_perf = _as_dict(seg.get("performance"))
        d = _as_dict(seg.get("dialogue"))
        start, end = _f(seg.get("start")), _f(seg.get("end"))
        if end <= start:
            end = start + 1.0

        speaker = str(d.get("character") or "").strip()
        line = str(d.get("text") or "").strip()
        card = _card_by_name(speaker, characters) if speaker else None

        if not line:
            report["silent_segments"] += 1
        else:
            report["dialogue_segments"] += 1

        listener = resolve_listener(speaker, shot=shot, scene=scene,
                                    characters=characters, speaker_card=card)
        report["listener_strength"][listener.get("strength", "none")] = \
            report["listener_strength"].get(listener.get("strength", "none"), 0) + 1

        # ★ 没有台词的时间片也要有微表情控制 —— 否则半条时间线没有表情指令。
        #   但依据不同：靠**场次情绪**推，报告里会标明。
        perf = build_performance(
            line=line, emotion=d.get("emotion") or ("" if line else scene_mood),
            speaker=speaker, speaker_card=card, listener=listener,
            scene_mood=scene_mood, start=start, end=end,
            aspect_ratio=aspect_ratio, camera=str(seg.get("camera") or ""),
            shot_size=str(seg.get("shot_size") or ""), rules=rules,
        )
        seg["performance"] = perf
        # ══════════════════════════════════════════════════════════
        # `expression` 用**合并**而不是替换，但要**分成两份**给不同的消费方。
        #
        # 为什么不能直接换成 `expression_short`：那是从**情绪公式**推出来的
        # 通用执行细节（"下颌微收、目光定住"），而分镜里原来那个
        # 是 LLM 针对这一场写的具体情绪（"歇斯底里地嚎啕大哭"）。
        # 直接覆盖 = 把更具体的信息换成更笼统的 —— 实测这一覆盖会让
        # 两个本来表情完全不同的镜头（"平静克制" vs "嚎啕大哭"）
        # 生成出**逐字节相同**的提示词，等于表情控制又失效了。
        #
        # ★★ 但两者长度要求相反，所以必须分开：
        #
        #   · `performance.expression_full` —— 完整的「原标签 + 可观察细节」，
        #     给**视频提示词**用。那边没有长度压力，越具体越好。
        #
        #   · `seg["expression"]` —— 必须**短**。因为
        #     `postprocess.generate_srt_from_timeline()` 在**无台词**时间片上
        #     会把 `action +（expression）` 直接当字幕。
        #     原来这里截到 80 字符 —— 一句 18 字的动作配上 80 字的"表情"，
        #     字幕会长到没法看，而本模块 docstring 自己写的是"≤ 约 30 字"。
        #     这是**我自己引入的**：改之前这里只有 LLM 的短标签。
        # ══════════════════════════════════════════════════════════
        # ★★★ 幂等：第二遍必须**回到最初那个标签**重算（2026-09-26 修）
        #
        # 为什么必须这样：`seg["expression"]` 在**本函数里被改写**成
        # `_clip(原标签；公式细节, 30)`。如果下一轮把**它**当成 `old_expr`：
        #
        #   · 合并结果 ≤30 字时，它**包含了** `short` → 第二遍走
        #     `elif short:` 分支 → `expression` 变成**只有公式细节**，
        #     LLM 写的那个具体情绪（"歇斯底里地嚎啕大哭"）**丢了** ——
        #     正是上面那段注释说"实测会让两个表情不同的镜头生成**逐字节
        #     相同**的提示词"的那个退化，被第二遍**重新引入**。
        #   · 合并结果 >30 字时，第二遍把截断后的串再拼一次 `；short`，
        #     内容逐轮漂移。
        #
        # ★ 这条不是假想 —— 有一条**真实可达**的路径：
        #   项目**没有场次细纲**时，分镜的 timeline 会回落到
        #   `three_layer_prompts`；而那份 tlp 是在
        #   `llm.generate_three_layer_prompt` 里**已经富化过一次**的。
        #   随后 `main.py` 的 `shots_from_script` 会**再富化一次** →
        #   就是第二遍，表情标签正好在这一步被换掉。
        #
        # 修法：优先复用上一次存下的**最初标签**（`perf["source_expression"]`
        # 本来就是这个用途）。于是重算结果与第一遍**逐字相同** ——
        # 既幂等，又仍然保留"重算刷新"的能力（改了人物卡再跑一遍照样生效）。
        old_expr = (str(_prev_perf.get("source_expression") or "").strip()
                    or str(seg.get("expression") or "").strip())
        short = str(perf["expression_short"] or "").strip()
        if old_expr and short and old_expr not in short and short not in old_expr:
            full = "%s；%s" % (old_expr, short)
        elif short:
            full = short
        else:
            full = old_expr
        perf["expression_full"] = full
        # `_clip` 按语义边界截（不切在词组中间），比硬 `[:30]` 好读
        seg["expression"] = _clip(full, 30) if full else ""
        perf["source_expression"] = old_expr          # 留档，便于审计对比

        report["enriched"] += 1
        report["details"].append({
            "range": [start, end],
            "speaker": speaker,
            "has_line": bool(line),
            "formula": perf["formula"],
            "formula_basis": perf["formula_basis"],
            "listener": perf["listener"].get("name") or "",
            "listener_basis": perf["listener"].get("basis") or "",
            "beats": len(perf["beats"]),
            "beat_source": perf["beat_source"],
            "expression": seg["expression"],
        })
        out.append(seg)

    # ── 结论要诚实 ──
    if report["enriched"] == 0:
        report["warning"] = ("时间线是空的或全是无法识别的条目，"
                             "微表情富化**什么都没做** —— 这不是「成功但没变化」。")
    elif report["dialogue_segments"] == 0:
        report["warning"] = ("这一镜**没有任何台词**，所有表演都是按场次情绪（%s）推的。"
                             "如果剧本里这场其实有对话，说明台词没被写进分镜 —— "
                             "见 scriptlines.py 的说明。" % (scene_mood or "无"))
    weak = report["listener_strength"].get("none", 0) + report["listener_strength"].get("weak", 0)
    if weak and report["dialogue_segments"]:
        report["warning"] = (report.get("warning", "") +
                             " 有 %d 段找不到画面内的听者，「表达态度」只能靠人物关系或默认基调 ——"
                             " 想让表演更准，请确保 shot.character_ids 填了同镜角色。" % weak).strip()
    if report["unrecognized"]:
        report["warning"] = (report.get("warning", "") +
                             " 另有 %d 条时间线条目不是对象（无法做微表情富化，已原样透传）。"
                             % report["unrecognized"]).strip()

    return {"timeline": out, "report": report}


# ──────────────────────── 给视频提示词用的一行 ────────────────────────


def _beat_text(b: Dict[str, Any]) -> str:
    """取节拍文本：**优先中文**。

    skill 原文的节拍是英文的。VideoForge 的提示词是中文，
    把英文原样塞进去会中英混杂，而且截断时容易切出半个单词
    （实测出现过 "收在aftertaste; emotion cools, breaks, harde"）。
    `rules.json` 里给了 `beat_zh`；实在没有中文时才退回英文，
    并在返回值里如实标注，便于发现漏译。
    """
    zh = str(b.get("beat_zh") or "").strip()
    if zh:
        return zh
    en = str(b.get("beat") or "").strip()
    return ("[未译]" + en) if en else ""


def _clip(text: str, limit: int) -> str:
    """按**语义边界**截断，不切在词中间。

    优先级：句末标点 > 逗号/分号 > 空格 > 硬截。
    硬截只在最后兜底，并且加省略号让人看得出被截了。
    """
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    head = s[:limit]
    for sep in ("。", "；", "！", "？", "…", "，", "、", ";", ",", " "):
        i = head.rfind(sep)
        if i >= max(8, limit // 3):
            return head[:i + (0 if sep in "，、;, " else 1)].rstrip("，、;, ")
    return head.rstrip() + "…"


def performance_prompt_lines(timeline: Any, *, duration: float = 0.0,
                             max_chars: int = 420) -> str:
    """把时间线里的表演压成**一段紧凑的**给视频模型的指令。

    ★ 为什么不是每段都完整写一遍：
      一条镜头动辄 4-6 段，每段都重复「基调 + 听者 + 声音风格 + 起手 + 收尾」
      会产出几百字的**重复文本**（实测见过同一串在一段提示词里出现 4 次），
      既吃预算又稀释注意力。这里改成：
        基调与声音（共同部分，只写一次）→ 每段只写**这一段独有**的东西。

    ★ 两条取数路径，缺一不可：
      · 有 `performance`（`enrich_timeline` 跑过）→ 用完整的六层结构
      · 只有 `expression`（**库里已有的老数据**，实测用户库 64 个镜头都是这种）
        → 至少要把它送出去，否则这些镜头会继续"表情被丢弃"

    ★ 无台词的时间片**不写「说给谁听」**：那句话不是它说的，
      写上去等于给模型一个错的指令（而且实测真的会写出
      「0s-3s：…说给李队长听」而这一段李队长自己没开口）。
    """
    tl = _as_list(timeline)
    if not tl:
        return ""
    segs: List[Dict[str, Any]] = []
    for it in tl:
        if not isinstance(it, dict):
            continue
        p = _as_dict(it.get("performance"))
        start, end = _f(it.get("start")), _f(it.get("end"))
        rec: Dict[str, Any] = {
            "range": "%gs-%gs" % (start, end),
            "expr": str(it.get("expression") or "").strip(),
            "has_line": bool(_as_dict(it.get("dialogue")).get("text")),
            "perf": p,
        }
        if p:
            if p.get("formula_zh"):
                rec["formula"] = str(p["formula_zh"])
            lis = _as_dict(p.get("listener"))
            # ★ 只在**这一段真的有人说话**时才说听者
            if rec["has_line"] and lis.get("name"):
                rec["listener"] = str(lis["name"]).split("、")[0]
            # ★★ 表情优先取 `expression_full`（完整的「原标签 + 可观察细节」）。
            #   不能再依赖 `it["expression"]` —— 它现在被**刻意**压到 30 字以内，
            #   那是给字幕回退路径用的（`postprocess.generate_srt_from_timeline`
            #   把 `action +（expression）` 当字幕）。视频提示词这边没有长度压力，
            #   取完整版才不丢细节。这个"一份数据两个长度要求"的坑，
            #   改的时候必须两边一起看。
            _full = str(p.get("expression_full") or "").strip()
            if _full:
                rec["expr"] = _full
            elif not rec["expr"]:
                rec["expr"] = str(p.get("expression_short") or "").strip()
            beats = p.get("beats") or []
            if beats:
                rec["first_beat"] = _beat_text(beats[0])
                if len(beats) > 1:
                    rec["last_beat"] = _beat_text(beats[-1])
        segs.append(rec)
    if not segs:
        return ""

    # ── 共同部分：基调（取第一段有台词的，没有就取第一段）──
    base_formula = ""
    for s in segs:
        if s.get("has_line") and s.get("formula"):
            base_formula = s["formula"]
            break
    if not base_formula:
        base_formula = next((s.get("formula") or "" for s in segs), "")
    # 声音结构：从第一段有 performance 的里取（skill 的 voice_rules 是全局的）
    voice = ""
    for s in segs:
        v = str(_as_dict(_as_dict(s["perf"]).get("layers")).get("4_voice") or "").strip()
        if v:
            voice = _clip(v, 90)
            break

    head_bits = []
    if base_formula:
        head_bits.append("基调" + base_formula)
    # 起手与收尾：最能定调的两拍
    first = next((s for s in segs if s.get("first_beat")), None)
    if first:
        head_bits.append("起手" + _clip(first["first_beat"], 46))
    last = next((s for s in reversed(segs) if s.get("last_beat")), None)
    if last:
        head_bits.append("收在" + _clip(last["last_beat"], 40))

    body_bits = []
    listeners: List[str] = []
    for s in segs:
        piece = s["range"] + " " + _clip(s["expr"] or "（无表情描述）", 34)
        if s.get("listener"):
            piece += "（说给%s）" % s["listener"]
            if s["listener"] not in listeners:
                listeners.append(s["listener"])
        body_bits.append(piece)

    # ══════════════════════════════════════════════════════════════
    # ★★ 段内顺序：基调 → 分秒 → 声音。
    #
    # 原来把「声音」放在「分秒」前面。这不是随便排的 ——
    # `videoprompt._fit_to_budget` 在提示词超预算时会把这一段
    # **从前往后截到 150 字符**（`parts[idx] = parts[idx][:keep]`）。
    # 算一下原来的字符数：
    #
    #     表演（按时间码执行）：          11
    #     基调坚定 / 决意                  9
    #     ，起手先压住第一层情绪面具：…     25
    #     ，收在余味：…                   22
    #     ；声音：压低声音＝…（4 条通用规则） 76   ← 到这里已 ~143
    #     ；分秒：…                        ← 只剩约 7 个字符
    #
    # 「声音」是**对每条镜头都一样的通用规则**，而「分秒」是**这一段该演什么**
    # —— 两者被截的代价完全不同。原来的顺序等于"最该执行的内容最先丢"，
    # 那正是用户反馈的"微表情控制不了"在超预算时的重演。
    # 现在把分秒提前、声音放最后：150 字符下能保住基调 + 若干段分秒。
    # ══════════════════════════════════════════════════════════════
    # ★ 分段拼装，而不是一路 `+=`。
    #   为什么：**老数据**（库里已有的镜头，只有 `expression`、没有
    #   `performance`）算出来的 `head_bits` 是空的 —— 一路 `+=` 会拼出
    #
    #       表演（按时间码执行）：；分秒：0s-6s 老数据里的表情词
    #                            ↑ 这个"：；"是标点噪声
    #
    #   而用户库里现在**正好都是这种老数据**（实测 64 个镜头全部没有
    #   performance）。和 casting 里修"双句号"是同一类问题：
    #   提示词的标点质量会影响模型读指令。空的那一段直接不拼。
    def _assemble(head: Sequence[str], body: Sequence[str], v: str) -> str:
        secs: List[str] = []
        if head:
            secs.append("，".join(head))
        if body:
            secs.append("分秒：" + "；".join(body))
        if v:
            secs.append("声音：" + v)
        return "表演（按时间码执行）：" + "；".join(secs)

    text = _assemble(head_bits, body_bits, voice)

    if len(text) > max_chars:
        # 保留"基调 + 起手"和**首尾两段**：中间段最不重要
        keep = body_bits[:1] + (["……"] + body_bits[-1:] if len(body_bits) > 1 else [])
        text = _assemble(head_bits, keep, voice)
        if len(text) > max_chars:
            text = _clip(text, max_chars - 1) + "…"
    return text


def performance_negatives(timeline: Any, *, rules: Optional[Dict[str, Any]] = None) -> List[str]:
    """把 skill 的必带负面限制收成一条去重列表（给 layer3 / 负向提示词）。"""
    rules = rules or load_rules()
    out: List[str] = []
    for x in (rules.get("negative_limits_zh") or []):
        s = str(x).strip()
        if s and s not in out:
            out.append(s)
    tl = _as_list(timeline)
    for it in tl:
        if isinstance(it, dict):
            p = _as_dict(it.get("performance"))
            for x in (p.get("negatives") or []):
                s = str(x).strip()
                if s and s not in out:
                    out.append(s)
    return out


# ──────────────────────── 审计（skill 的 Output Checklist）────────────────────────


def audit_timeline(timeline: Any, *, duration: float = 0.0,
                   aspect_ratio: str = "", rules: Optional[Dict[str, Any]] = None
                   ) -> Dict[str, Any]:
    """对着 skill 的 Output Checklist 逐条检查。

    检查项（skill 原文的 9 条，落成可判定的形式）：
      · 时间范围能覆盖请求时长
      · 每句有明确的听者和关系目的
      · 人格气质出现在面部细节之前（层级顺序）
      · 每个情绪节拍都改变了可见或可听的东西
      · 眼神路径明确（看哪里、何时避开、何时回看）
      · 口型细节（嘴角/唇线/开口大小/尾音）
      · 声音指令（重音/放轻/呼吸/音高/停顿/情绪呼吸点）
      · 身体动作足够小
      · 负面提示词挡住脸崩/过火/多人/字幕/水印

    ★ 和其它审计一样：**不阻断**，只如实报告。
    """
    rules = rules or load_rules()
    tl = _as_list(timeline)
    checklist: List[Dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checklist.append({"item": name, "ok": bool(ok), "detail": detail})

    starts = [_f(it.get("start")) for it in tl if isinstance(it, dict)]
    ends = [_f(it.get("end")) for it in tl if isinstance(it, dict)]
    covered = (min(starts) if starts else 0.0, max(ends) if ends else 0.0)
    if duration:
        gap = abs(covered[1] - duration)
        add("时间范围覆盖请求时长", gap <= 0.6,
            "覆盖 %gs-%gs，请求 %gs（差 %0.1fs）" % (covered[0], covered[1], duration, gap))
    else:
        add("时间范围覆盖请求时长", bool(tl), "未提供时长，跳过数值比对")

    has_perf = [it for it in tl if isinstance(it, dict) and _as_dict(it.get("performance"))]
    add("每个时间片都有表演结构", len(has_perf) == len([x for x in tl if isinstance(x, dict)]),
        "%d/%d 段带 performance" % (len(has_perf), len([x for x in tl if isinstance(x, dict)])))

    with_listener = [p for p in has_perf
                     if _as_dict(_as_dict(p.get("performance")).get("listener")).get("name")]
    add("有明确的听者", bool(with_listener),
        "%d/%d 段找到了画面内的听者" % (len(with_listener), len(has_perf)) if has_perf else "无表演段")

    with_persona = [p for p in has_perf
                    if str(_as_dict(_as_dict(p.get("performance")).get("layers")
                                    ).get("1_personality") or "").strip()]
    add("人格气质出现在面部细节之前", bool(with_persona) or not has_perf,
        "%d/%d 段带人格气质（来自人物卡）" % (len(with_persona), len(has_perf)) if has_perf else "无表演段")

    with_beats = [p for p in has_perf if _as_dict(p.get("performance")).get("beats")]
    add("每个情绪节拍都有可见/可听的变化", bool(with_beats) and len(with_beats) == len(has_perf),
        "%d/%d 段带时间码节拍" % (len(with_beats), len(has_perf)) if has_perf else "无表演段")

    # 眼神路径 / 口型 / 声音：从 expression_short 与 voice 层里找关键词
    eye_kw = ("视线", "目光", "眼", "眨")
    mouth_kw = ("唇", "嘴角", "口型", "嘴")
    voice_kw = ("尾音", "停顿", "呼吸", "压低", "放轻", "重音", "音")
    eye = sum(1 for p in has_perf if any(k in str(_as_dict(p["performance"]).get("expression_short") or "")
                                        for k in eye_kw))
    mouth = sum(1 for p in has_perf if any(k in str(_as_dict(p["performance"]).get("expression_short") or "")
                                          for k in mouth_kw))
    voice = sum(1 for p in has_perf
                if str(_as_dict(_as_dict(p["performance"]).get("layers")).get("4_voice") or "").strip())
    n = len(has_perf)
    add("眼神路径明确", eye == n if n else False, "%d/%d 段写了视线走向" % (eye, n) if n else "无表演段")
    add("口型/嘴角细节", mouth == n if n else False, "%d/%d 段写了唇部细节" % (mouth, n) if n else "无表演段")
    add("声音指令", voice == n if n else False, "%d/%d 段带了声音结构" % (voice, n) if n else "无表演段")

    negs = performance_negatives(tl, rules=rules)
    need = ("不脸崩" in "".join(negs) or "崩" in "".join(negs)) and \
           any("字幕" in x for x in negs) and any("多余人" in x for x in negs)
    add("负面限制覆盖脸崩/多人/字幕水印", need,
        "共 %d 条：%s" % (len(negs), "、".join(negs[:3])))

    # ★ 起手不能是峰值
    peak_first = 0
    for p in has_perf:
        beats = _as_dict(p["performance"]).get("beats") or []
        wave = _as_dict(_as_dict(p["performance"]).get("layers")).get("3_wave") or []
        if beats and wave and str(wave[0]).strip().lower() in ("peak",):
            peak_first += 1
    add("不从情绪峰值起手", peak_first == 0,
        "%d 段从峰值起手" % peak_first if peak_first else "全部从克制/隐藏起手")

    # 比例覆盖
    if aspect_ratio:
        wrong = []
        for p in has_perf:
            v = _as_dict(_as_dict(_as_dict(p["performance"]).get("layers")).get("6_visual"))
            ar = str(v.get("aspect_ratio") or "")
            if ar and ar != aspect_ratio:
                wrong.append(ar)
        add("视觉参数用镜头自己的比例（不用 skill 写死的 9:16）", not wrong,
            "全部是 %s" % aspect_ratio if not wrong else "出现了 %s" % set(wrong))

    passed = sum(1 for c in checklist if c["ok"])
    return {
        "passed": passed, "total": len(checklist), "checklist": checklist,
        "ok": passed == len(checklist),
        "failed": [c["item"] for c in checklist if not c["ok"]],
        "rules_source": rules.get("_source") or ("内置兜底" if rules.get("_fallback") else "?"),
    }


def summarize(report: Dict[str, Any]) -> str:
    """把富化报告压成一句人话。"""
    bits = ["%d/%d 段已富化" % (report.get("enriched", 0), report.get("segments_total", 0))]
    if report.get("dialogue_segments"):
        bits.append("其中 %d 段有台词" % report["dialogue_segments"])
    if report.get("silent_segments"):
        bits.append("%d 段无台词（按场次情绪推）" % report["silent_segments"])
    if report.get("unrecognized"):
        bits.append("★ %d 条不是对象、未处理" % report["unrecognized"])
    ls = report.get("listener_strength") or {}
    strong = ls.get("strong", 0)
    if strong:
        bits.append("%d 段听者确切" % strong)
    if report.get("warning"):
        bits.append("★ " + str(report["warning"])[:120])
    return "；".join(bits)
