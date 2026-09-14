# -*- coding: utf-8 -*-
"""
VideoForge · 时间线 / 台词规范化

═══════════════════════════════════════════════════════════════════
这个模块存在的唯一理由：让"台词"成为一等公民
═══════════════════════════════════════════════════════════════════
实测发现线上分镜长这样：

    {"start": 3, "end": 6,
     "action": "李连长抬头看向小虎，语气坚定地说出台词",
     "expression": "目光坚定"}

**根本没有台词本身**。于是下游只能：
  - 配音 → 无处可读，退化成朗读 `layer1_overview`（一段剧情概述）
  - 字幕 → 没有逐句文本，只能整段糊上去

用户看到的现象就是"语音+字幕要么不出现，要么特别不协调"。
根因不在配音模块，在这里：**源头就没产出可以念的句子**。

本模块负责三件事：
1. `normalize_timeline()`  —— 把 LLM 五花八门的输出统一成
   `{start, end, action, expression, camera, props_used, dialogue}`，
   其中 `dialogue` 恒为 `None` 或 `{character, text, emotion}`。
2. `sniff_fake_dialogue()` —— **识别"用描述冒充台词"**的坏输出
   （"说出台词""喊道""低声说"后面没有引号内容），标记出来让上层知道
   "这不是真台词"，而不是傻乎乎地把它念出来。
3. `timeline_dialogue_stats()` —— 给出可诊断的统计
   （几条真台词、总字数、每秒字数是否超限），
   前端据此提示用户"该补台词了"。

设计原则：**不编造台词**。识别不出来就如实标 `None`，
让上层明确知道"这里没有可念的内容"，而不是拿一段描述去糊。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("videoforge.dialogue")

# 中文配音的舒适语速（字/秒）。低于这个值会显得拖沓，高于会赶。
CHARS_PER_SEC = 4.0
# 超过这个倍率就算"念不完"，需要变速或改台词
OVERFLOW_TOLERANCE = 1.15

# 时间线里可能出现的台词字段名（LLM 各写各的）
_DIALOGUE_KEYS = ("dialogue", "line", "speech", "voiceover", "narration", "subtitle", "text")

# 「用描述代替台词」的特征：动词 + 说/喊/问，但整句里没有引号包裹的内容
# 注意：引号字符直接写进字符类容易把字符串本身截断，这里用 chr() 拼出来更稳。
_Q_OPEN = "「『" + chr(34) + chr(39) + "“”" + "\u2018\u2019"
# ★★ 2026-09-14 补：中文**单引号** ‘ ’（U+2018/U+2019）。
#   实测真实分镜里的台词是这么写的：
#     "李队长蹲在草丛中…同时低声对战士们说：‘兄弟们，沉住气，等他…’"
#   而这张表原来只有「『" ' “”，**不认 ‘ ’** —— 于是
#   `extract_quoted()` 返回空、这一条被当成"没有台词"，
#   整个镜头的对白直接消失（用户反馈的"第一个镜头没有声音"就是这个）。
_Q_CLOSE = "」』" + chr(34) + chr(39) + "“”" + "\u2018\u2019"
_FAKE_TALK = re.compile(
    "(说出|说|讲出|讲|喊道|喊|叫道|叫|问道|问|低声|喃喃|嘟囔|开口|回答|回应|念出|读出|唱着)"
    "[^" + re.escape(_Q_OPEN) + "]{0,6}(台词|对白|这句话|一句|道|着|：)?\\s*$"
)
_QUOTED = re.compile(
    "[" + re.escape(_Q_OPEN) + "]([^" + re.escape(_Q_CLOSE) + "]{2,})"
    "[" + re.escape(_Q_CLOSE) + "]"
)


def _as_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return " ".join(str(x) for x in v if x)
    return str(v).strip()


def sniff_fake_dialogue(text: str) -> bool:
    """判断一段文本是不是"用描述冒充台词"。

    返回 True 表示：这看着像"他说了句话"的描述，而不是话本身。
    例：
        "语气坚定地说出台词"          → True （纯描述）
        "李连长抬头看向小虎"          → False（动作描述，本来也不该当台词）
        "小虎，天黑之前夺回阵地。"      → False（真台词）
        "他喊道：「冲啊！」"           → False（引号里有真内容）
    """
    t = _as_text(text)
    if not t:
        return False
    if _QUOTED.search(t):
        return False            # 引号里有话 → 是真台词
    if t.endswith(("。", "！", "？", ".", "!", "?")) and not _FAKE_TALK.search(t):
        return False            # 像完整句子 → 当作台词
    return bool(_FAKE_TALK.search(t))


def extract_quoted(text: str) -> str:
    """从「他喊道："冲啊！"」里把引号内容抠出来；没有引号就返回空串。"""
    m = _QUOTED.search(_as_text(text))
    return m.group(1).strip() if m else ""


def normalize_dialogue(raw: Any, fallback_character: str = "") -> Optional[Dict[str, str]]:
    """把各种写法统一成 {character, text, emotion}；无法解析返回 None。

    支持：
      {"character": "A", "text": "话", "emotion": "坚定"}
      "话"                          （裸字符串，无说话人）
      {"speaker": "A", "line": "话"}
      "他喊道：「冲啊！」"            （从引号里抠出来）
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        t = raw.strip()
        if not t:
            return None
        q = extract_quoted(t)
        if q:
            return {"character": fallback_character, "text": q, "emotion": ""}
        if sniff_fake_dialogue(t):
            return None
        return {"character": fallback_character, "text": t, "emotion": ""}
    if not isinstance(raw, dict):
        return None

    text = ""
    for k in _DIALOGUE_KEYS:
        v = raw.get(k)
        if v and str(v).strip():
            text = str(v).strip()
            break
    if not text:
        return None
    # 有些模型会把台词写成 {"text": "他喊道：\"冲啊\""}，把引号内容抠出来
    q = extract_quoted(text)
    if q:
        text = q
    elif sniff_fake_dialogue(text):
        return None
    char = ""
    for k in ("character", "speaker", "role", "name", "who"):
        v = raw.get(k)
        if v and str(v).strip():
            char = str(v).strip()
            break
    emo = ""
    for k in ("emotion", "tone", "mood", "feeling"):
        v = raw.get(k)
        if v and str(v).strip():
            emo = str(v).strip()
            break
    return {"character": char or fallback_character, "text": text, "emotion": emo}


def normalize_timeline(timeline: Any, duration: float = 0.0) -> List[Dict[str, Any]]:
    """把时间线规范化。同时做两件修复：

    1. 补 start/end（有些模型只给一个）并保证单调递增、落在 duration 内。
    2. 把台词归一化；识别出的"描述冒充台词"丢弃（宁可没有，也不要念错）。
    """
    if isinstance(timeline, str):
        import json
        try:
            timeline = json.loads(timeline)
        except Exception:
            timeline = []
    if not isinstance(timeline, list):
        return []

    out: List[Dict[str, Any]] = []
    cursor = 0.0
    n = len(timeline)
    for i, it in enumerate(timeline):
        if isinstance(it, str):
            it = {"action": it}
        if not isinstance(it, dict):
            continue
        try:
            start = float(it.get("start", cursor))
        except (TypeError, ValueError):
            start = cursor
        try:
            end = float(it.get("end", 0) or 0)
        except (TypeError, ValueError):
            end = 0.0
        # 缺 end：按剩余时间均分，或给一个 3 秒默认
        if end <= start:
            remain = max(0.0, duration - start) if duration else 3.0
            left = max(1, n - i)
            end = start + max(1.0, remain / left) if remain else start + 3.0
        if duration and start >= duration:
            # 超出的条目压回末尾（LLM 偶尔会算错总长）
            start = max(0.0, duration - 1.0)
            end = duration
        if duration:
            end = min(end, float(duration))
        start = round(max(0.0, start), 3)
        end = round(max(start + 0.1, end), 3)

        entry: Dict[str, Any] = {
            "start": start,
            "end": end,
            "action": _as_text(it.get("action") or it.get("description") or it.get("visual")),
            "expression": _as_text(it.get("expression") or it.get("emotion")),
            "camera": _as_text(it.get("camera") or it.get("shot")),
            "props_used": it.get("props_used") if isinstance(it.get("props_used"), list) else [],
            "dialogue": None,
        }
        # 台词：先看显式字段，再退而看 action 里有没有引号内容
        for k in _DIALOGUE_KEYS:
            if k in it and it.get(k):
                d = normalize_dialogue(it.get(k))
                if d:
                    entry["dialogue"] = d
                    break
        if not entry["dialogue"]:
            q = extract_quoted(entry["action"])
            if q:
                entry["dialogue"] = {"character": "", "text": q, "emotion": ""}
        out.append(entry)
        cursor = end
    return out


def timeline_lines(timeline: Any, duration: float = 0.0) -> List[Dict[str, Any]]:
    """只取**有真台词**的条目，并标注每句的可用秒数与字数是否超限。

    这是配音/字幕的唯一数据源 —— 两条链路读同一个函数，
    所以"声音"和"字幕"天然对齐，不会各说各话。
    """
    entries = normalize_timeline(timeline, duration) if not isinstance(timeline, list) \
        or (timeline and not isinstance(timeline[0], dict)) else timeline
    # 已经规范化过的（含 dialogue 键）直接用，避免重复解析
    if entries and isinstance(entries[0], dict) and "dialogue" in entries[0]:
        pass
    else:
        entries = normalize_timeline(entries, duration)

    out: List[Dict[str, Any]] = []
    for it in entries:
        d = it.get("dialogue")
        if not d or not d.get("text"):
            continue
        span = max(0.1, float(it["end"]) - float(it["start"]))
        chars = len(re.sub(r"\s", "", d["text"]))
        need = chars / CHARS_PER_SEC          # 正常语速下需要几秒
        out.append({
            "start": it["start"], "end": it["end"], "span": round(span, 3),
            "character": d.get("character") or "",
            "text": d["text"], "emotion": d.get("emotion") or "",
            "chars": chars, "need_seconds": round(need, 2),
            "overflow": need > span * OVERFLOW_TOLERANCE,
            "speed": round(max(0.25, min(4.0, need / span)), 3) if span else 1.0,
            "camera": it.get("camera") or "", "action": it.get("action") or "",
        })
    return out


def timeline_dialogue_stats(timeline: Any, duration: float = 0.0) -> Dict[str, Any]:
    """给前端/诊断用的统计：让"这个词没有台词"变成看得见的数字。"""
    lines = timeline_lines(timeline, duration)
    total = sum(x["chars"] for x in lines)
    speakers = sorted({x["character"] for x in lines if x["character"]})
    return {
        "line_count": len(lines),
        "total_chars": total,
        "speakers": speakers,
        "overflow_count": sum(1 for x in lines if x["overflow"]),
        "duration": duration,
        # 有台词但一句都放不下 / 一句都没有，都属于"这段配不出好音"
        "has_dialogue": bool(lines),
        "chars_per_second": round(total / duration, 2) if duration else 0.0,
    }
