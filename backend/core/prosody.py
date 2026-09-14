# -*- coding: utf-8 -*-
r"""VideoForge · 表演韵律（Prosody）

═══════════════════════════════════════════════════════════════════
这个模块存在的唯一理由
═══════════════════════════════════════════════════════════════════
用户原话（2026-09-13）：

  "配音要角色来发音而不是从到到尾念稿子，角色自己说话且有自己的音色而不是
   都像机器人，很恶心。声音要跟人物形象符合！语速、说话节奏、情感都要
   跟随剧本场景"

在此之前，`voicecast.synthesize_line()` 收了 `emotion` 参数，
**却只把它原样写进返回值** —— 一个字节都没送到 TTS。
也就是说：剧本里写着 `emotion: "determined"`，合成出来还是平铺直叙的
默认朗读。这就是"像机器人"的**直接技术原因**（不是模型不行，是我们没让它演）。

本模块做三件事：

1. `plan_prosody(emotion, text, action)` —— 把"情绪"翻译成**可执行的三元组**
   `(语速倍率, 音高半音, 音量倍率)` + 台词前后的**气口**（吸气/静默尾拍）。
2. `infer_emotion(text, action)` —— 剧本没写情绪时，从标点和动词里**如实推断**
   （"为什么？我救了你！" → 质问/愤怒），并标注这是推断来的。
3. `actor_baseline(character)` —— 从角色卡的年龄/体型/气质推出**基线嗓音**：
   老者更慢更低、少年更快更高、魁梧者更沉。这让"声音跟人物形象符合"有据可依，
   而不是每人一把随机嗓子。

设计原则：**宁可不动，不要乱演。** 推断不出来就是 `neutral`（1.0/0/1.0），
绝不凭空调到夸张的参数上。

═══════════════════════════════════════════════════════════════════
参考依据
═══════════════════════════════════════════════════════════════════
`Hell-Grind-AIGC-Skill / references/dialogue-audio.md`（本地已克隆，见
`D:\VideoForge-dev\reference\Hell-Grind-AIGC-Skill`）明确要求每句台词写全：

    说话者：[稳定 ID]
    发声时间：[开始–结束]
    精确台词："……"
    语速/音量/情绪：[可听参数]
    口型：只在精确台词期间运动
    尾部：最后一个字后嘴唇静止并保留[时长]静默尾拍

以及节奏规则：台词前留吸气/决定开口的短停顿、台词后留静默尾拍、
"用自然语速朗读并实测，不按字符数猜"、对话轮次不无意重叠。
本模块就是"语速/音量/情绪 + 尾部静默尾拍"这一行的落地。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("videoforge.prosody")

# ── 半音 → 相对百分比的换算基准 ────────────────────────────────
# edge-tts 的 pitch 单位是 Hz 偏移，而 Hz 偏移对人耳的感受随基频变化：
# 女声基频高，同样 +20Hz 的听感变化远小于男声。所以这里统一用**半音**
# 表达"升高/降低多少"，再由 `semitones_to_hz()` 按角色的基线基频折算成 Hz。
# 这样"愤怒 +2 半音"在男声和女声上听起来是**同一种情绪强度**。
BASE_F0_MALE = 120.0      # 成年男声基频参考（Hz）
BASE_F0_FEMALE = 210.0    # 成年女声基频参考（Hz）

# 语速的硬边界：超出这个范围人耳就会觉得"快进音/拖沓"，
# 宁可让上层去改台词或加长镜头（`voicecast.synthesize_line` 也这么认为）。
RATE_MIN, RATE_MAX = 0.72, 1.45
# 音高的硬边界（半音）。±4 半音已经是"明显在演"的程度，
# 再大就会变成动画片配音腔 —— 而目标用户要的是"不像机器人"，不是"像卡通"。
PITCH_MIN_ST, PITCH_MAX_ST = -4.0, 4.0
# 音量边界
VOL_MIN, VOL_MAX = 0.65, 1.35

# ── 情绪表 ──────────────────────────────────────────────────────
# 字段：(语速倍率, 音高半音, 音量倍率, 前气口秒, 后静默尾拍秒)
#
# 数值来源：中文影视配音的通行做法 + 可听性约束，不是拍脑袋的随机数：
#   · 愤怒靠"提音量 + 略提音高 + 略快"，不是靠喊；纯提音量会破音
#   · 悲伤靠"慢 + 低 + 略轻"，并留更长的尾拍（情绪落地的空间）
#   · 犹豫靠"慢 + 前气口变长"，那个"吸气/决定开口"的停顿就是犹豫本身
#   · 惊恐靠"快 + 高 + 前气口极短"（来不及吸气就出声）
#   · 低语靠"轻 + 低"，语速反而略慢（气声说快会糊）
_EMO: Dict[str, Tuple[float, float, float, float, float]] = {
    # 中文键
    "愤怒":   (1.10,  2.0, 1.22, 0.06, 0.22),
    "生气":   (1.09,  1.8, 1.20, 0.06, 0.22),
    "质问":   (1.05,  1.4, 1.15, 0.10, 0.26),
    "坚定":   (0.96, -0.5, 1.10, 0.14, 0.26),
    "威严":   (0.92, -1.2, 1.10, 0.18, 0.30),
    "悲伤":   (0.86, -1.5, 0.92, 0.20, 0.40),
    "痛苦":   (0.84, -1.0, 1.00, 0.16, 0.36),
    "绝望":   (0.82, -1.8, 0.88, 0.26, 0.44),
    "温柔":   (0.90,  0.5, 0.95, 0.14, 0.30),
    "关切":   (0.95, -0.8, 1.00, 0.12, 0.28),
    "惊恐":   (1.14,  1.6, 1.06, 0.02, 0.18),
    "紧张":   (1.08,  0.9, 1.02, 0.06, 0.20),
    "犹豫":   (0.88, -0.3, 0.95, 0.30, 0.30),
    "怀疑":   (0.98,  0.8, 1.00, 0.16, 0.26),
    "喜悦":   (1.06,  1.8, 1.08, 0.06, 0.20),
    "兴奋":   (1.12,  2.2, 1.12, 0.04, 0.16),
    "希望":   (1.00,  1.0, 1.05, 0.10, 0.26),
    "嘲讽":   (0.94,  1.2, 1.02, 0.14, 0.28),
    "低语":   (0.92, -1.0, 0.72, 0.18, 0.32),
    "呼喊":   (1.12,  2.5, 1.28, 0.02, 0.18),
    "平静":   (1.00,  0.0, 1.00, 0.12, 0.24),
    "中性":   (1.00,  0.0, 1.00, 0.12, 0.24),
    "neutral": (1.00, 0.0, 1.00, 0.12, 0.24),
    # 英文键（真实项目里 LLM 就写过 determined / skeptical / concerned / hopeful）
    "angry":     (1.10,  2.0, 1.22, 0.06, 0.22),
    "furious":   (1.14,  2.6, 1.30, 0.04, 0.20),
    "determined": (0.96, -0.5, 1.10, 0.14, 0.26),
    "resolute":  (0.94, -0.8, 1.10, 0.16, 0.28),
    "sad":       (0.86, -1.5, 0.92, 0.20, 0.40),
    "grieving":  (0.82, -1.8, 0.88, 0.24, 0.44),
    "pain":      (0.84, -1.0, 1.00, 0.16, 0.36),
    "gentle":    (0.90,  0.5, 0.95, 0.14, 0.30),
    "tender":    (0.88,  0.6, 0.94, 0.16, 0.32),
    "concerned": (0.95, -0.8, 1.00, 0.12, 0.28),
    "worried":   (1.02,  0.4, 0.98, 0.10, 0.26),
    "fearful":   (1.14,  1.6, 1.06, 0.02, 0.18),
    "afraid":    (1.14,  1.6, 1.06, 0.02, 0.18),
    "nervous":   (1.08,  0.9, 1.02, 0.06, 0.20),
    "hesitant":  (0.88, -0.3, 0.95, 0.30, 0.30),
    "uncertain": (0.90, -0.2, 0.96, 0.26, 0.30),
    "skeptical": (0.98,  0.8, 1.00, 0.16, 0.26),
    "doubtful":  (0.98,  0.6, 1.00, 0.18, 0.26),
    "happy":     (1.06,  1.8, 1.08, 0.06, 0.20),
    "joyful":    (1.08,  2.0, 1.10, 0.05, 0.18),
    "excited":   (1.12,  2.2, 1.12, 0.04, 0.16),
    "hopeful":   (1.00,  1.0, 1.05, 0.10, 0.26),
    "hopeful ":  (1.00,  1.0, 1.05, 0.10, 0.26),
    "sarcastic": (0.94,  1.2, 1.02, 0.14, 0.28),
    "mocking":   (0.94,  1.3, 1.02, 0.14, 0.28),
    "whisper":   (0.92, -1.0, 0.72, 0.18, 0.32),
    "whispering": (0.92, -1.0, 0.72, 0.18, 0.32),
    "shouting":  (1.12,  2.5, 1.28, 0.02, 0.18),
    "shout":     (1.12,  2.5, 1.28, 0.02, 0.18),
    "calm":      (1.00,  0.0, 1.00, 0.12, 0.24),
    "commanding": (0.92, -1.2, 1.12, 0.18, 0.30),
    "cold":      (0.94, -1.0, 0.98, 0.16, 0.30),
}

# ── 从文本推断情绪 ──────────────────────────────────────────────
# ⚠ 只认**强证据**：标点 + 明确动词/形容词。宁可判 neutral。
_INFER_RULES: Tuple[Tuple[str, str], ...] = (
    # 愤怒类
    (r"(为什么|凭什么|怎么敢|竟敢|混账|混蛋|该死|住口|闭嘴)", "愤怒"),
    (r"(怒吼|怒视|咬牙|咆哮|吼道|厉声|怒道)", "愤怒"),
    (r"(质问|责问|逼问)", "质问"),
    # 惊恐类
    (r"(惊恐|恐惧|惊骇|骇然|尖叫|惊呼|吓得)", "惊恐"),
    (r"(颤抖|发抖|哆嗦|战栗|瑟瑟)", "紧张"),
    (r"(紧张|焦急|着急|慌了|慌忙)", "紧张"),
    # 悲苦类
    (r"(悲|哀|痛哭|落泪|泪水|哽咽|呜咽|凄凉|绝望)", "悲伤"),
    (r"(痛苦|剧痛|忍痛|呻吟|伤口)", "痛苦"),
    # 温柔/关切
    (r"(温柔|轻声|柔声|悄悄|低语|耳语)", "温柔"),
    (r"(小心|关切|担忧|没事吧|别怕|照顾好)", "关切"),
    # 犹豫/怀疑
    (r"(犹豫|迟疑|踌躇|欲言又止|沉默片刻)", "犹豫"),
    (r"(怀疑|疑惑|不信|真的吗|你确定|凭什么相信|如何相信)", "怀疑"),
    # 喜悦/希望
    (r"(微笑|一笑|欣喜|高兴|欢喜|笑声)", "喜悦"),
    (r"(希望|期待|憧憬|未来|自由|明天)", "希望"),
    # 嘲讽
    (r"(冷笑|嘲讽|讥讽|嗤笑|讽刺)", "嘲讽"),
    # 呼喊
    (r"(大喊|喊道|呼喊|叫道|高声)", "呼喊"),
)


def semitones_to_hz(semitones: float, base_f0: float = BASE_F0_MALE) -> float:
    """半音 → Hz 偏移：`f = base * (2^(st/12) - 1)`。

    用相对基频而不是固定 Hz，是为了让"升高 2 个半音"在男声/女声上是**同一种**
    听感强度 —— 固定 +20Hz 对男声是明显拔高，对女声几乎听不出来。
    """
    return float(base_f0) * (2.0 ** (float(semitones) / 12.0) - 1.0)


def infer_emotion(text: str, action: str = "") -> Tuple[str, str]:
    """剧本没写情绪时，从台词 + 动作里推断。返回 (情绪, 依据)。

    依据字符串会如实写进返回里（`推断：文本含「为什么」`），
    这样"这句为什么这么念"是可追溯的，而不是黑盒。
    """
    t = f"{text or ''}"
    blob = f"{t} {action or ''}"
    for pat, emo in _INFER_RULES:
        m = re.search(pat, blob)
        if m:
            where = "台词" if re.search(pat, t) else "动作描述"
            return emo, f"推断：{where}含「{m.group(0)}」"
    # 标点兜底：感叹号密集 → 情绪激动；问号结尾 → 疑问
    if t.count("！") + t.count("!") >= 2:
        return "愤怒", "推断：台词含两个以上感叹号（情绪激动）"
    if t.rstrip().endswith(("？", "?")):
        return "怀疑", "推断：台词以问号结尾"
    if t.count("！") + t.count("!") == 1:
        return "呼喊", "推断：台词含感叹号"
    return "neutral", "未给出情绪，且无强证据可推断 → 按中性朗读"


def _age_profile(age_text: str) -> Dict[str, float]:
    """从年龄描述推基线：越小越快越高，越大越慢越沉。"""
    a = (age_text or "").lower()
    if not a:
        return {}
    # 数值年龄
    m = re.search(r"(\d{1,2})", a)
    n = int(m.group(1)) if m else -1

    def band(lo: int, hi: int) -> bool:
        return n >= 0 and lo <= n <= hi

    if re.search(r"(儿童|幼|孩|童|child|kid)", a) or band(3, 12):
        return {"rate": 1.06, "pitch": 1.6, "why": "儿童/少年"}
    if re.search(r"(少年|teen|adolesc)", a) or band(13, 17):
        return {"rate": 1.03, "pitch": 1.0, "why": "少年"}
    if re.search(r"(青年|young adult|early 20|mid 20|20s)", a) or band(18, 29):
        return {"rate": 1.01, "pitch": 0.2, "why": "青年"}
    if re.search(r"(中年|middle|30s|40s)", a) or band(30, 49):
        return {"rate": 0.98, "pitch": -0.3, "why": "中年"}
    if re.search(r"(老年|老|elder|senior|50s|60s|70s|80s)", a) or band(50, 99):
        return {"rate": 0.90, "pitch": -0.9, "why": "中老年"}
    return {}


def _build_profile(desc_text: str) -> Dict[str, float]:
    """从外形描述推基线：魁梧/高大 → 更沉更低；纤细/瘦小 → 略轻略高。"""
    d = (desc_text or "").lower()
    if re.search(r"(魁梧|高大|健壮|结实|厚重|粗犷|宽阔|burly|heavy|broad)", d):
        return {"pitch": -0.6, "rate": 0.97, "why": "体格魁梧/厚重"}
    if re.search(r"(纤细|瘦小|娇小|单薄|slim|petite|thin|delicate)", d):
        return {"pitch": 0.5, "rate": 1.02, "why": "体格纤细"}
    if re.search(r"(苍老|饱经风霜|粗糙|沙哑|低沉|weathered|hoarse|gruff)", d):
        return {"pitch": -0.8, "rate": 0.95, "why": "嗓音苍老/沙哑"}
    return {}


def actor_baseline(character: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """从角色卡推出**基线嗓音**（语速倍率 / 音高半音 / 依据）。

    "声音要跟人物形象符合"这条要求必须有据可依，否则只能靠运气。
    这里只用角色卡里**真实存在**的字段：`age`、`description`、`body_type`、
    `costume_main`。读不到就返回中性基线并说明原因。
    """
    c = character or {}
    if not c:
        return {"rate": 1.0, "pitch": 0.0, "why": "没有角色卡 → 中性基线"}
    age = str(c.get("age") or "")
    prof = _age_profile(age)
    body = _build_profile(" ".join(str(c.get(k) or "") for k in
                                   ("description", "body_type", "costume_main")))
    rate = float(prof.get("rate", 1.0)) * float(body.get("rate", 1.0))
    pitch = float(prof.get("pitch", 0.0)) + float(body.get("pitch", 0.0))
    whys = [x for x in (prof.get("why"), body.get("why")) if x]
    return {
        "rate": round(rate, 3), "pitch": round(pitch, 2),
        "why": ("年龄「%s」→ %s" % (age, prof.get("why")) if prof.get("why") else "")
               + ("；" if prof.get("why") and body.get("why") else "")
               + ("外形→ %s" % body.get("why") if body.get("why") else "")
               or "角色卡没有年龄/外形线索 → 中性基线",
    }


def plan_prosody(emotion: str, text: str, action: str = "",
                 character: Optional[Dict[str, Any]] = None,
                 *, gender: str = "") -> Dict[str, Any]:
    """把情绪 + 角色基线合成**可执行**的韵律参数。

    返回的字段直接喂给 TTS 与排轨：
      rate        —— 相对 1.0 的语速倍率（会与"时间片塞不下"的变速相乘）
      pitch_st    —— 音高偏移（半音）
      pitch_hz    —— 折算好的 Hz 偏移（给 edge-tts 这类按 Hz 的引擎）
      volume      —— 音量倍率
      lead        —— 台词前气口（秒）：吸气 / 决定开口的停顿
      tail        —— 台词后静默尾拍（秒）：让表演和剪辑落地
      emotion / source —— 实际用的情绪，以及它是给定的还是推断的
    """
    emo = (str(emotion or "").strip().lower() or "")
    source = "剧本给定"
    if emo:
        # 剧本写中文、或英文，或 "determined（坚定）" 这种混写：先整体命中，再取子串
        hit = _EMO.get(emo)
        if hit is None:
            for k, v in _EMO.items():
                if k and (k in emo or emo in k):
                    hit = v
                    emo = k
                    break
        if hit is None:
            inferred, why = infer_emotion(text, action)
            if inferred != "neutral":
                emo, hit, source = inferred, _EMO.get(inferred), \
                    f"剧本给的「{emotion}」不在表里 → " + why
            else:
                hit = _EMO["neutral"]
                emo = "neutral"
                source = f"剧本给的「{emotion}」不在表里，且无法推断 → 中性"
    else:
        emo, why = infer_emotion(text, action)
        hit = _EMO.get(emo) or _EMO["neutral"]
        source = why
    rate, pitch, vol, lead, tail = hit

    base = actor_baseline(character)
    rate = rate * float(base.get("rate") or 1.0)
    pitch = pitch + float(base.get("pitch") or 0.0)

    base_f0 = (BASE_F0_FEMALE if str(gender).lower().startswith("f")
               or gender == "女" else BASE_F0_MALE)
    return {
        "emotion": emo,
        "source": source,
        "rate": round(max(RATE_MIN, min(RATE_MAX, rate)), 3),
        "pitch_st": round(max(PITCH_MIN_ST, min(PITCH_MAX_ST, pitch)), 2),
        "pitch_hz": round(semitones_to_hz(
            max(PITCH_MIN_ST, min(PITCH_MAX_ST, pitch)), base_f0), 1),
        "volume": round(max(VOL_MIN, min(VOL_MAX, vol)), 3),
        "lead": round(max(0.0, lead), 3),
        "tail": round(max(0.0, tail), 3),
        "baseline": base,
    }


def describe(plan: Dict[str, Any]) -> str:
    """人能读的一行说明（进告警/日志，让"为什么这么念"可追溯）。"""
    return (f"{plan.get('emotion')}（语速×{plan.get('rate')} "
            f"音高{plan.get('pitch_st'):+.1f}半音 "
            f"音量×{plan.get('volume')} 气口{plan.get('lead')}s/"
            f"尾拍{plan.get('tail')}s）—— {plan.get('source')}")
