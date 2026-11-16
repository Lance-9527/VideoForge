# -*- coding: utf-8 -*-
"""语气提示词（【情绪标签】【发声方式：语速/气息/音调描述】）→ **可执行**的韵律参数。

为什么要有这个模块（2026-10-26，用户要求）：
    用户给了一套"语气提示词"写法（产品里要能直接用）::

        【紧张羞涩】：语速时快时慢，气息不稳，句首带吸气声，尾音音量骤减，例：那个……我、我有句话想跟你说……
        【满腹委屈】：声音发闷，鼻音浓重，语速变慢，每个字都带着憋住的哭腔，例：……
        【极度惊恐】：咬紧牙关低语，声音微颤喉咙发紧，声带紧绷，例：……
        【阴阳怪气】：语调起伏拉大，部分字咬得清晰，尾音带钩上扬，例：……
        【暧昧挑逗】：语气轻柔，尾音拉丝上扬，例：……
        【极度愤怒】：情绪激动有压迫感，重音下砸，例：……
        通用公式：【情绪标签】【发声方式：语速/气息/音调描述】台词内容

    这套写法在产品里**从来没有对应物**：剧本/分镜的台词只有一个短标签 ``emotion``，
    数据库没有语气列，TTS 请求体里也没有自由文本字段 ⇒ 写进去就丢，合成出来
    永远是"平铺直叙的同一把调子"（用户原话：感情不对）。

本模块只做三件事，各自有明确边界：

    ① **认标签**：把【…】里的情绪标签认出来；认不出就**如实说认不出**，不猜。
    ② **翻数值**：把"发声方式"里的语速/气息/音调描述翻译成引擎真能执行的旋钮
       （语速倍率 / 音高半音 / 音量倍率 / 前气口 / 后尾拍）。
       —— 自由文本引擎（不发声）根本不懂"咬紧牙关低语"，能懂的是数字。
    ③ **出指令**：产出一条自然语言语气指令，交给**支持自由文本**的引擎；
       不支持的引擎只吃数值（这不丢信息：数值就是那句话的可执行版）。

★ 铁律：**台词内容不能混进被朗读的文本**。
   这里只解析"提示词"；被朗读的那句话由 :func:`split_tone_prompt` 单独切出来。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Tuple

#: 情绪标签 → 韵律语义。
#: 数值口径与 ``core/prosody.py`` 的 ``_EMO`` 一致：
#:   rate 语速倍率 / pitch_st 音高半音 / volume 音量倍率 / lead 前气口秒 / tail 后尾拍秒
#:
#: 为什么给"标签"定死一套数值：标签是**用户的意图**，描述文字是**修饰**。
#: 同一个标签必须每次都念成同一个味道，否则用户会觉得"提示词没用"。
TONE_LABELS: Dict[str, Dict[str, Any]] = {
    "紧张羞涩": {
        "emotion": "nervous", "rate": 1.02, "pitch_st": 0.4, "volume": 0.92,
        "lead": 0.14, "tail": 0.26,
        "note": "语速时快时慢，气息不稳，句首带吸气声，尾音音量骤减",
    },
    "满腹委屈": {
        "emotion": "sad", "rate": 0.88, "pitch_st": -0.6, "volume": 0.88,
        "lead": 0.16, "tail": 0.36,
        "note": "声音发闷，鼻音浓重，语速变慢，每个字都带着憋住的哭腔",
    },
    "极度惊恐": {
        "emotion": "fearful", "rate": 0.98, "pitch_st": -0.2, "volume": 0.80,
        "lead": 0.02, "tail": 0.18,
        "note": "咬紧牙关低语，声音微颤喉咙发紧，声带紧绷",
    },
    "阴阳怪气": {
        "emotion": "sarcastic", "rate": 0.96, "pitch_st": 1.0, "volume": 1.02,
        "lead": 0.14, "tail": 0.28,
        "note": "语调起伏拉大，部分字咬得清晰，尾音带钩上扬",
    },
    "暧昧挑逗": {
        "emotion": "gentle", "rate": 0.90, "pitch_st": 0.6, "volume": 0.90,
        "lead": 0.16, "tail": 0.30,
        "note": "语气轻柔，尾音拉丝上扬",
    },
    "极度愤怒": {
        "emotion": "angry", "rate": 1.10, "pitch_st": 1.6, "volume": 1.18,
        "lead": 0.06, "tail": 0.22,
        "note": "情绪激动有压迫感，重音下砸",
    },
}

#: "发声方式"里的措辞 → 数值微调（在标签基线上再叠一层）。
#:
#: 为什么要有这层：用户写的描述往往比标签更具体（"咬紧牙关低语"比"极度惊恐"更狠）。
#: 只认标签会把这些信息丢掉；只认描述又会让标签失去主次。
_STYLE_RULES: Tuple[Tuple[str, Dict[str, float]], ...] = (
    (r"语速(变慢|放慢|慢下来|变缓)", {"rate": 0.92}),
    (r"语速(加快|变快|快一点|更快)", {"rate": 1.08}),
    (r"语速时快时慢", {"rate": 0.98}),
    (r"句首带吸气声|带吸气|吸气声", {"lead": 0.12}),
    (r"气息不稳|声音微颤|喉咙发紧|声带紧绷", {"lead": 0.04, "volume": 0.95}),
    (r"尾音音量骤减|音量骤减", {"volume": 0.90, "tail": 0.10}),
    (r"咬紧牙颈低语|咬紧牙关低语|低语|轻声细语", {"volume": 0.82, "rate": 0.97}),
    (r"声音发闷|鼻音浓重|发闷", {"pitch_st": -0.6, "volume": 0.95}),
    (r"语调起伏拉大|起伏拉大", {"pitch_st": 0.8}),
    (r"尾音带钩上扬|尾音拉丝上扬|尾音上扬|尾音钩起", {"pitch_st": 1.0}),
    (r"语气轻柔|轻柔", {"volume": 0.92, "pitch_st": 0.3}),
    (r"情绪激动有压迫感|重音下砸|重音", {"volume": 1.10, "pitch_st": -0.8}),
    (r"憋住的哭腔|哭腔", {"volume": 0.90, "rate": 0.94}),
    (r"咬字清晰|咬得清晰|字咬得清晰", {"volume": 1.04}),
)

#: 【】块里可能出现的前缀（这些不是情绪标签，是"这一块写的是什么"的说明）。
_BLOCK_PREFIX = re.compile(r"^(发声方式|语气|情绪标签|表演提示|情绪)[：:\s]*")


def _clean_label(candidate: str) -> str:
    """把一个【】块的内容洗成"标签候选"。"""
    return _BLOCK_PREFIX.sub("", str(candidate or "")).strip()


def _looks_like_style(text: str) -> bool:
    """这段文字像"发声方式描述"吗？（不像 ⇒ 它是台词，不能混进语气指令）"""
    probe = str(text or "")
    if not probe.strip():
        return False
    return any(re.search(pattern, probe) for pattern, _ in _STYLE_RULES)


def parse_tone_prompt(raw: str) -> Dict[str, Any]:
    """解析一条语气提示词。

    返回（始终是 dict，``ok`` 说明认没认出来）::

        {"ok": True, "label": "紧张羞涩", "style": "语速时快时慢，气息不稳",
         "emotion": "nervous", "rate": 1.02, "pitch_st": 0.4, "volume": 0.92,
         "lead": 0.14, "tail": 0.26, "instruction": "【紧张羞涩】语速时快时慢，气息不稳"}

    认不出标签时返回 ``{"ok": False, "reason": "…", "style": "…"}`` ——
    **不猜**：猜错的情绪比没有情绪更糟（用户听出来的就是"感情不对"）。
    """
    text = str(raw or "")
    if not text.strip():
        return {"ok": False, "reason": "没有语气提示词", "style": ""}

    blocks = re.findall(r"【([^】]{1,40})】", text)
    label = ""
    for candidate in blocks:
        cleaned = _clean_label(candidate)
        if cleaned in TONE_LABELS:
            label = cleaned
            break
    if not label:
        # 用户可能写成「极度愤怒：马上给我离开!」这种不带【】的。
        for name in TONE_LABELS:
            if name in text:
                label = name
                break

    # 发声方式描述 = 除标签外的其它【】块 + 紧跟标签的正文。
    style_parts = []
    for candidate in blocks:
        cleaned = _clean_label(candidate)
        if not cleaned or cleaned == label:
            continue
        style_parts.append(cleaned)
    tail = re.search(r"【[^】]+】\s*([^【\n]{0,120})", text)
    if tail and tail.group(1).strip():
        # ★ 只有**像语气描述**的尾巴才算"发声方式"。
        #   「【极度愤怒】马上给我离开!」里的尾巴是**台词**，不是描述 ——
        #   把它当描述会把台词混进语气指令（最后被念出来）。
        if _looks_like_style(tail.group(1)):
            style_parts.append(tail.group(1).strip())
    style = "；".join(style_parts).strip()

    if not label:
        return {"ok": False, "reason": "没有认出情绪标签（可用：" + "、".join(TONE_LABELS) + "）", "style": style}

    spec = dict(TONE_LABELS[label])
    rate = float(spec.get("rate") or 1.0)
    volume = float(spec.get("volume") or 1.0)
    pitch = float(spec.get("pitch_st") or 0.0)
    lead = float(spec.get("lead") or 0.0)
    tail_s = float(spec.get("tail") or 0.0)
    for pattern, delta in _STYLE_RULES:
        if re.search(pattern, style):
            rate *= float(delta.get("rate", 1.0))
            volume *= float(delta.get("volume", 1.0))
            pitch += float(delta.get("pitch_st", 0.0))
            lead += float(delta.get("lead", 0.0))
            tail_s += float(delta.get("tail", 0.0))

    return {
        "ok": True,
        "label": label,
        "style": style,
        "emotion": str(spec.get("emotion") or "neutral"),
        "rate": round(rate, 3),
        "pitch_st": round(pitch, 2),
        "volume": round(volume, 3),
        "lead": round(lead, 3),
        "tail": round(tail_s, 3),
        "instruction": f"【{label}】" + (style or str(spec.get("note") or "")),
        "note": str(spec.get("note") or ""),
    }


def split_tone_prompt(text: str) -> Tuple[str, str]:
    """把「【标签】【发声方式…】台词」切成 ``(台词, 提示词)``。

    只有**行首**连续的【…】块算提示词；台词里自带的方括号（`[停顿]`）不动。
    没有提示词时原样返回。
    """
    raw = str(text or "")
    match = re.match(r"^\s*((?:【[^】]{1,40}】\s*)+)", raw)
    if not match:
        return raw, ""
    prompt = match.group(1).strip()
    spoken = raw[match.end():].strip()
    if not spoken:
        return "", prompt
    return spoken, prompt
