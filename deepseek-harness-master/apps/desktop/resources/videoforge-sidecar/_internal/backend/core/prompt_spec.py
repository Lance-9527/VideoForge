# -*- coding: utf-8 -*-
"""
VideoForge · 分镜提示词编译器 v2（叙事驱动）
==============================================

## 为什么要重写（不是"再润色一下"）

用户实测（2026-10-23）：
    一条 6 秒镜头，画面里只有一只手在单独敲键盘，观感像恐怖片；
    用户原话：「一个宣传片被我做成了恐怖片。6s 都是手在单独敲」。

把旧提示词打出来，问题一目了然（实测 424 字）：
    电影级写实质感，自然色彩…场景「办公室」。主体：阿岚。画面内人物 1 人。
    镜头：近景。动作：右手在键盘上敲。这一镜要讲清楚的是：…
——这是一张**物料清单**，不是一段电影。缺的东西每一条都会让模型自由发挥：

  1. **没有情绪基调**：只有「电影级写实质感」，模型自己选了冷调+暗角 -> 恐怖感。
     宣传片要的是「明亮、通透、有掌控感」，这必须当成**全局约束**写死在第一句。
  2. **没有动作节拍**：6 秒只给「手在敲」一个动作 -> 模型只能拍手。
     6 秒里至少要 2-3 个视觉变化点（建立 -> 推进 -> 反应），否则就是空转。
  3. **没有主体的处境**：只写「主体：阿岚」，没写她在做什么任务、什么情绪、脸朝哪、
     看什么 -> 模型不拍脸、不拍关系，只给局部特写。
  4. **没有镜头动机**：只有「近景」。为什么切近景？讲信息还是讲情绪？
     没动机的景别 = 无意义空镜。
  5. **没有画面层级（staging）**：前景/主体/背景都没写 -> 空房间 + 一台屏幕 + 一只手。
  6. **声音只有一句拟音**：没有环境层/动作层/人声层的分工，也没有"先定声音再定画面"。

## v2 的结构（七段式 + 两处硬约束）

  ① TONE      基调（体裁级：情绪词 + 光比色温 + **明确排除的基调**）
  ② SUBJECT   主体处境（谁 / 此刻在做什么任务 / 想要什么 / 阻力 / 视线与表情）
  ③ BEATS     动作节拍（把时长切成 2-4 个点，每点必须有一个画面变化）
  ④ CAMERA    镜头（景别 + 机位 + 运动 + **动机**：这一镜要让观众获得什么）
  ⑤ STAGING   画面层级（前景 / 主体 / 背景各有什么，避免空镜与单物件）
  ⑥ LIGHT     光线色彩（光源方向 + 色温 + 对比，与 TONE 对齐）
  ⑦ SOUND     声音三层（环境底噪 / 动作拟音 / 人声，含情绪与口型时机）

硬约束：
  · **每镜至少一个状态改变**（信息/关系/位置/情绪），否则这一镜可有可无；
  · **基调从一而终**：体裁定了基调，单镜不许自己跑偏（宣传片不许出现恐怖/阴森）。

**正向写法优先**：要出现的东西直接写清楚；负面词只用于质量类瑕疵（畸形/穿模/
闪身/噪点），不写「不要文字」这类意图否定（越否越容易生成）。

纯函数、零网络、零费用；可被 `shot_prompts` 直接调用，也可单独跑自检。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("videoforge.prompt_spec")

#: 基调预设：体裁 -> 情绪词 / 光 / 色彩 / 明确排除的基调
TONE_PRESETS: Dict[str, Dict[str, str]] = {
    "promo": {
        "mood": "明亮、通透、干净、有掌控感、值得信任",
        "light": "高调照明（high-key），柔和顶光 + 大面积窗光，主体面部无硬阴影，"
                 "曝光略偏上，暗部保留细节但整体不压暗",
        "color": "自然偏暖的中性色，色温 5200-5600K，肤色准确，画面通透；"
                 "品牌色只作为点缀，不铺满；"
                 "色锚（跨镜保持一致）：暖米白、浅胡桃木、天空冷蓝",
        "avoid": "阴森、压抑、恐怖、悬疑、暗角、单色冷蓝、脏乱、低照度",
    },
    "drama": {
        "mood": "有张力但不做作，情绪克制、可信",
        "light": "自然光为主，明暗有层次，人物一侧受光，保留环境阴影",
        "color": "低饱和的自然色，色调统一；冷暖对比用来划分场景；"
                 "色锚（跨镜保持一致）：土黄、暗青、石灰白",
        "avoid": "过曝、塑料感、恐怖片式暗角（除非剧情要求）",
    },
    "generic": {
        "mood": "自然、可信、有生活感",
        "light": "自然光，柔和，主体清晰可辨",
        "color": "自然色彩，白平衡准确",
        "avoid": "恐怖、阴森、过度风格化",
    },
}

#: 体裁识别（与 scriptskills 一致的强词，避免两处漂移）
_PROMO_HINTS = ("宣传", "广告", "推广", "产品", "品牌", "发布会", "口播", "开箱",
                "测评", "教程", "介绍片", "介绍视频", "企业片", "形象片", "tvc", "promo")
_DRAMA_HINTS = ("短剧", "剧情", "故事", "悬疑", "恐怖", "惊悚", "逆袭", "复仇")
_HORROR_HINTS = ("恐怖", "惊悚", "阴森", "悬疑")


def tone_for(*texts: str) -> Dict[str, str]:
    """按题材给基调。宣传片 > 剧情 > 类型暗黑片；都没有则 generic。"""
    blob = " ".join(str(t or "") for t in texts).lower()
    if any(h in blob for h in _HORROR_HINTS) and not any(h in blob for h in _PROMO_HINTS):
        return {"mood": "阴冷、压抑、不安全感", "light": "低调照明，单一光源，大量暗部",
                "color": "冷蓝或去饱和，低照度", "avoid": "明亮、欢快、高饱和"}
    if any(h in blob for h in _PROMO_HINTS):
        return TONE_PRESETS["promo"]
    if any(h in blob for h in _DRAMA_HINTS):
        return TONE_PRESETS["drama"]
    return TONE_PRESETS["generic"]


def beat_count(duration: float) -> int:
    """按秒数决定这一镜要有几个动作点（视觉变化）。"""
    try:
        d = float(duration or 0)
    except Exception:  # noqa: BLE001
        d = 0.0
    if d <= 3.5:
        return 2
    if d <= 6.5:
        return 3
    if d <= 9.5:
        return 3
    return 4


_BEAT_ROLES = {
    2: ("建立", "结果"),
    3: ("建立", "推进", "反应"),
    4: ("建立", "推进", "转折", "收束"),
}


def _clean(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().strip("。；;，,")


def _shot_actions(shot: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    tl = shot.get("layer2_timeline")
    if isinstance(tl, str):
        import json
        try:
            tl = json.loads(tl)
        except Exception:  # noqa: BLE001
            tl = []
    for it in (tl or []):
        if not isinstance(it, dict):
            continue
        for k in ("action", "beat", "motion"):
            v = _clean(it.get(k))
            if v and v not in out:
                out.append(v)
    for k in ("actions", "action"):
        v = shot.get(k)
        if isinstance(v, str) and _clean(v) and _clean(v) not in out:
            out.append(_clean(v))
        elif isinstance(v, list):
            for x in v:
                if _clean(x) and _clean(x) not in out:
                    out.append(_clean(x))
    return out


#: 动作里可以剥掉的"身体部位限定"，剥掉后才能判断是不是同一个动作
_BODY_PREFIX = ("右手", "左手", "双手", "手指", "一只手", "右手在", "左手在", "双手在")


def _norm_action(a: str) -> str:
    a = _clean(a)
    for p in _BODY_PREFIX:
        if a.startswith(p):
            a = a[len(p):]
    return a.strip("在的着 ")


def _dedupe_actions(acts: List[str]) -> List[str]:
    """把「右手在键盘上敲」和「手指敲键盘」认成同一个动作（子串/2-gram 重叠）。"""
    out: List[str] = []
    normed: List[str] = []
    for a in acts:
        n = _norm_action(a)
        if not n:
            continue
        dup = False
        for m in normed:
            if n == m or (min(len(n), len(m)) >= 4 and (n in m or m in n)):
                dup = True
                break
            # 2-gram 重叠率 > 0.5 也算同一个动作
            g1 = {n[i:i + 2] for i in range(max(1, len(n) - 1))}
            g2 = {m[i:i + 2] for i in range(max(1, len(m) - 1))}
            if g1 and g2 and len(g1 & g2) / float(min(len(g1), len(g2))) >= 0.5:
                dup = True
                break
        if not dup:
            normed.append(n)
            out.append(_clean(a))
    return out


def _cast_names(shot: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    for c in (shot.get("cast") or []):
        n = _clean((c or {}).get("name")) if isinstance(c, dict) else _clean(c)
        if n and n not in names:
            names.append(n)
    return names


def _dialogue_bits(shot: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    tl = shot.get("layer2_timeline")
    if isinstance(tl, str):
        import json
        try:
            tl = json.loads(tl)
        except Exception:  # noqa: BLE001
            tl = []
    for it in (tl or []):
        if not isinstance(it, dict):
            continue
        d = it.get("dialogue")
        if isinstance(d, dict) and _clean(d.get("text")):
            out.append("%s：%s" % (_clean(d.get("character")) or "人物", _clean(d.get("text"))))
        elif isinstance(d, str) and _clean(d):
            out.append(_clean(d))
    return out


# ── 动势接力（相邻镜的运动方向/速度接续）────────────────────────
#   抖音拉片原话：「身体往前踩的**动势**」—— 上一个镜头的运动惯性要带进下一镜，
#   观众才会觉得"这是一段连续的动作"，而不是"两个摆拍拼起来"。
_DIR_WORDS = {
    # ★ 2026-10-23 修：原来把「前倾」当"向前运动"，于是**坐着不动的镜头**
    #   也被写成「带速度进画」—— 与白膜里 pose=sit、path 只有 2 点直接矛盾。
    #   动势只认**真实位移**：走/跑/冲/穿过/进画/起身/转身。
    "forward": ("向前", "前冲", "冲进", "走向", "走进去", "走进", "推门", "跨过", "扑向", "跑向", "冲出门"),
    "back": ("后退", "后退", "后仰", "转身走", "退回"),
    "left": ("向左", "左转", "左侧"),
    "right": ("向右", "右转", "右侧"),
    "up": ("起身", "站起", "跃", "抬头", "升起"),
    "down": ("蹲下", "坐下", "俯身", "低头", "落下"),
}
_DIR_CN = {"forward": "向前", "back": "向后", "left": "向左", "right": "向右",
           "up": "向上", "down": "向下"}


def motion_dir(shot: Dict[str, Any]) -> str:
    """从动作/机位文字里推断这一镜的运动方向。"""
    shot = shot or {}
    blob = " ".join([_clean(shot.get("action")),
                     " ".join(_shot_actions(shot)),
                     _clean(shot.get("camera")),
                     _clean(shot.get("layer1_overview")),
                     _clean(shot.get("motion"))])
    for d, words in _DIR_WORDS.items():
        if any(w in blob for w in words):
            return d
    return ""


_STATIC_HINTS = ("坐", "站", "停住", "停着", "静坐", "俯身", "低头", "躺着", "趴")


def _is_static(shot: Dict[str, Any]) -> bool:
    blob = " ".join([_clean((shot or {}).get("action")),
                     " ".join(_shot_actions(shot)),
                     _clean((shot or {}).get("layer1_overview"))])
    return any(h in blob for h in _STATIC_HINTS)


def momentum_line(prev_shot: Optional[Dict[str, Any]], shot: Dict[str, Any]) -> str:
    """写"这一镜怎么接住上一镜的动势"。没有上一镜时给"出场动势"。"""
    # ★ 人物是静止姿态（坐/站/停）就不谈动势 —— 否则提示词会要求"带速度进画"，
    #   而白膜里这人坐着不动，两者当场打架（用户实测"白膜跟提示词出入很大"）。
    if _is_static(shot):
        return "动势：本镜人物处于**静止姿态**（坐/站/停），不要让他带速度进画；变化发生在面部与手部。"
    cur = motion_dir(shot)
    prv = motion_dir(prev_shot) if prev_shot else ""
    if prv and cur and prv == cur:
        return ("动势接力：上一镜是%s运动，本镜**从同一方向接住**，速度不降、"
                "不要在切入点停住或反向（同向接力 = 观众感觉是一段连续动作）。"
                % _DIR_CN.get(prv, prv))
    if prv and cur and prv != cur:
        return ("动势接力：上一镜%s运动、本镜改为%s，转折要**有因**（被撞/看到/决定），"
                "中间用一个动作或一个空镜过渡，不要硬拐。"
                % (_DIR_CN.get(prv, prv), _DIR_CN.get(cur, cur)))
    if cur:
        return ("出场动势：本镜以%s运动进入（人物带着速度进画），"
                "不要静止起手再动。" % _DIR_CN.get(cur, cur))
    return ""


# ── 动作打点 / 切点落在动作中段（cut on action）──────────────────
CUT_ON_ACTION = ("切点落在动作**中段**（动接动）：上一个动作没做完就切，"
                 "下一个镜头从同一动作方向继续，观众看不出剪口。")


# ── 组合动作（悬停 → 执行 → 反应），替代"单一动作"──────────────
def combo_ladder(action: str) -> List[str]:
    """把一个动作拆成"起手→执行→结果"三段（拉片里说的组合表达）。"""
    a = _clean(action) or "动作"
    return ["%s之前先有一个**起手/悬停**（手停在半空、身体前倾，0.3-0.5 秒）" % a,
            "**执行**：%s（动作要看得清过程，不是一下就完成）" % a,
            "**结果**：动作带来的可见变化（屏幕反应/物件位移/人物表情变化）"]


# ── 蒙太奇段 ────────────────────────────────────────────────────
_MONTAGE_HINTS = ("蒙太奇", "montage", "快切", "混剪", "流水账", "并列小情节")


def is_montage(shot: Dict[str, Any]) -> bool:
    blob = " ".join([_clean((shot or {}).get("segment_type")),
                     _clean((shot or {}).get("layer1_overview")),
                     _clean((shot or {}).get("note")),
                     _clean((shot or {}).get("genre"))])
    return any(h in blob.lower() for h in _MONTAGE_HINTS)


# ── 非人脸主角（玩偶/机械/面具）——高一致性要求时的兜底风格 ─────────
NONHUMAN_STYLES = {
    "doll": ("关节人偶", "人物是**关节点人偶/球关节玩偶**：可见的关节缝隙、"
                         "哑光木材或瓷器质感、面部是手绘或空白，不追求真人皮肤"),
    "mecha": ("机械体", "人物是**机械体/机器人**：金属与漆面材质、面罩或发光眼替代人脸、"
                        "关节处有液压/铰链结构，反光可控"),
    "mask": ("面具", "人物戴**固定面具/头盔**：面具造型与材质跨镜完全一致，"
                     "表情靠身体与镜头语言表达，不露真脸"),
}


def nonhuman_line(style: str) -> str:
    s = NONHUMAN_STYLES.get(str(style or "").strip().lower())
    if not s:
        return ""
    return ("【造型（一致性兜底）】%s：%s。同一角色的关节/材质/面具纹样**逐镜一致**，"
            "这是为了在 AI 生成里稳住一致性（真人脸是最容易崩的部分）。" % (s[0], s[1]))


def _is_object_only(actions: List[str], names: List[str]) -> bool:
    """只有物件动作、没有人的镜头（旧版最常见毛病）。"""
    if names:
        return False
    blob = "".join(actions)
    return bool(blob)


def compile_shot(shot: Dict[str, Any], *,
                 characters: Optional[Dict[str, Any]] = None,
                 scenes: Optional[Dict[str, Any]] = None,
                 genre_hint: str = "",
                 prev_shot: Optional[Dict[str, Any]] = None,
                 character_style: Optional[str] = None) -> Dict[str, Any]:
    """把一镜编译成 v2 七段式。返回 {prompt, tone, beats, checks}。"""
    shot = shot or {}
    dur = float(shot.get("duration_seconds") or 6)
    actions = _dedupe_actions(_shot_actions(shot))
    names = _cast_names(shot)
    lines = _dialogue_bits(shot)
    loc = _clean(shot.get("location")) or _clean((scenes or {}).get(shot.get("scene_id"), {}).get("name"))
    overview = _clean(shot.get("layer1_overview")) or _clean(shot.get("narration"))
    camera = _clean(shot.get("camera")) or "中景"
    purpose = _clean(shot.get("purpose")) or overview

    tone = tone_for(genre_hint, overview, loc, " ".join(actions))
    montage = is_montage(shot)
    momentum = momentum_line(prev_shot, shot)
    nonhuman = nonhuman_line(character_style or shot.get("character_style") or "")
    combo = combo_ladder(actions[0]) if (actions and len(actions) <= 1 and dur >= 4) else []

    # ── ② 主体处境 ──
    who = "、".join(names) if names else ""
    subject_bits: List[str] = []
    if who:
        subject_bits.append("%s" % who)
    if loc:
        subject_bits.append("在%s" % loc)
    main_act = actions[0] if actions else ""
    if main_act:
        subject_bits.append("正在%s" % main_act)
    if purpose:
        subject_bits.append("这一镜要交代的是：%s" % purpose)
    subject = "，".join(subject_bits) if subject_bits else "画面主体在这一镜中处于动作进行中"

    # ── ③ 动作节拍 ──
    # ★ 蒙太奇段：不靠台词，靠**每 1-2 秒一次视觉变化**
    n = max(3, min(8, int(round(dur / 1.5)))) if montage else beat_count(dur)
    roles = _BEAT_ROLES.get(n, _BEAT_ROLES[3])
    # ★ v2.1（Seedance 2.5 官方）：时间轴必须是**连续整数秒**，单拍不超过 3 秒；
    #   拍数按时长上限收（4-6s 最多 3 拍，8s 最多 3-4 拍）。
    n = min(n, max(2, int(round(dur / 3.0)) + 1))
    if montage and n > 4:
        roles = tuple(["快切%02d" % (i + 1) for i in range(n)])
    else:
        roles = _BEAT_ROLES.get(n, _BEAT_ROLES[3])
    per = dur / float(n)
    beats: List[str] = []
    for i, role in enumerate(roles):
        t0, t1 = int(round(i * per)), int(round((i + 1) * per))
        if t1 <= t0:
            t1 = t0 + 1
        act = actions[min(i, len(actions) - 1)] if actions else ""
        if combo and i < len(combo):
            act = combo[i]
        if role.startswith("快切"):
            beats.append("%d-%ds 快切%02d：%s（换一个画面/景别/动作，1-2 秒一变）"
                         % (t0, max(t1, t0 + 1), int(role[-2:]),
                            (actions[(int(role[-2:]) - 1) % len(actions)] if actions
                             else "换一个视觉变化")))
        elif role == "建立":
            beats.append("%d-%ds 建立：%s（交代这是哪、谁、此刻什么状态；第一拍就给可看的动作次数，"
                         "例如「敲两下、停住」）" % (t0, t1, act or "主体就位并开始动作"))
        elif role == "推进":
            beats.append("%d-%ds 推进：%s，且**画面必须发生变化**（动作推进/物体位移/信息出现）"
                         % (t0, t1, act or "动作加深"))
        elif role == "转折":
            beats.append("%d-%ds 转折：出现一个意外（%s），情绪或信息翻转" % (t0, t1, act or "出现意外"))
        else:
            beats.append("%d-%ds 收束：%s，落到结果（人物表情变化/物体最终状态/画面定格）"
                         % (t0, t1, act or "给出结果"))

    # ── ④ 镜头动机 ──
    motive = purpose or "推进叙事"
    camera_line = "%s；机位与运动必须为「%s」服务，不能只为了好看" % (camera, motive)

    # ── ④b 电影手法：这一镜该用什么专业手法（prompt_craft 库）──
    craft_text, craft_names, craft_misuse = "", [], []
    try:
        from core.prompt_craft import craft_block, detect_function, misuse_warnings, pick_moves
        _fn = detect_function(overview, " ".join(actions), loc, purpose)
        _mv = pick_moves(_fn, genre_hint, dur, camera=camera)
        _prop = _clean(shot.get("product")) or _clean(shot.get("prop")) or ""
        craft_text = craft_block(_mv, subject=(names[0] if names else ""),
                                 prop=_prop)
        craft_names = [m.get("name") or m.get("key") for m in _mv]
        craft_misuse = misuse_warnings(_mv)
    except Exception as _e:  # noqa: BLE001
        logger.warning("手法库不可用（跳过）：%s", _e)

    # ── ⑤ staging ──
    staging_parts = []
    if names:
        staging_parts.append("主体（%s）位于画面视觉中心偏一侧，面部与手部同时可辨" % "、".join(names))
        staging_parts.append("前景可以有真实的工作物件做遮挡层次")
    else:
        staging_parts.append("主体占据画面主要面积，避免空镜")
    if loc:
        staging_parts.append("背景是%s，与主体有明确的空间纵深（不是纯色幕布）" % loc)
    staging = "；".join(staging_parts)

    # ── ⑦ 声音 ──
    sound_parts = [
        "环境层：%s的现场底噪，低电平铺底" % (loc or "场景"),
    ]
    if actions:
        sound_parts.append("动作层：%s 要有对应的真实拟音，与动作同起同落" % actions[0])
    if lines:
        sound_parts.append("人声层：%s 开口说话，情绪与画面一致，口型与台词同步" % "、".join(names) if names
                           else "人声层：有台词，口型与台词同步")
    else:
        sound_parts.append("人声层：本镜无台词，只有呼吸与细微动作声")
    sound = "；".join(sound_parts)

    # ★ v2.1（Sora 官方）：每镜必须写清**状态改变**（before -> after），这是"这一镜有用"的判据。
    _before = _clean(shot.get("state_before")) or ("%s的动作刚开始" % (names[0] if names else "主体"))
    _after = _clean(shot.get("state_after")) or ("%s的动作完成并出现结果" % (names[0] if names else "主体"))
    # ★ v2.1（Seedance/Veo 官方）：声音四轨，BGM 要穷举同义表述并在首尾各说一次。
    _bgm = "音乐层：本镜无音乐（无 BGM、无配乐、无器乐、无旋律垫底）"
    if str(shot.get("bgm") or "").strip() in ("1", "true", "yes"):
        _bgm = "音乐层：极轻的氛围音乐，比人声低 10-15dB，不抢台词"
    sound4 = "%s；%s；%s；%s" % (sound, _bgm, "台词按「角色（情绪）：台词」格式写，台词后不要复述语气",
                                 "无音乐时也要给一个节奏音（如一声轻响/衣料声）")
    prompt = (
        "【基调】%s。光影：%s。色彩：%s。**本镜禁止**：%s。\n"
        "【主体与处境】%s。\n"
        "【状态改变】开始：%s。结束：%s。（比的是画面/人物/信息，不是心情形容词）\n"
        "【动作节拍】%s。\n"
        "【镜头】%s。一镜只用一个运镜，不要叠加第二种运动。%s\n"
        "%s%s%s"
        "【画面层级】%s。\n"
        "【声音】%s%s。"
        % (tone.get("mood"), tone.get("light"), tone.get("color"), tone.get("avoid"),
           subject, _before, _after, "；".join(beats), camera_line,
           # ★ 动作打点：切点落在动作中段（拉片原话「插柜这个动作是很棒的打点和开关」）
           CUT_ON_ACTION,
           (("【动势】%s。\n" % momentum) if momentum else ""),
           (("【造型】%s。\n" % nonhuman) if nonhuman else ""),
           (("【手法】%s。\n" % craft_text) if craft_text else ""),
           staging, sound4,
           ("；蒙太奇段：全段无台词，靠剪辑节奏推进，每个节拍都要换画面" if montage else ""))
    )

    # ── 自检（能直接给用户看的"这一镜哪里还不对"）──
    checks: List[Dict[str, str]] = []
    if _is_object_only(actions, names):
        checks.append({"id": "Q1", "level": "error",
                       "title": "只有物件动作、画面里没有人",
                       "detail": "这一镜的动作是「%s」，但没指定人物。模型会拍成空镜或局部特写"
                                 "（用户实测就是这样出成了恐怖片）。补人物，或明确这是有意的空镜并说明理由。"
                                 % "、".join(actions[:2])})
    if len(actions) < 2 and dur >= 5:
        checks.append({"id": "Q2", "level": "warn",
                       "title": "%.0f 秒只有 1 个动作点" % dur,
                       "detail": "整镜没有视觉变化，看起来像空转。建议补到 2-3 个动作点（建立/推进/反应）。"})
    if not purpose:
        checks.append({"id": "Q3", "level": "warn", "title": "没有镜头动机",
                       "detail": "为什么要给这一镜？它对叙事/信息有什么贡献？没写清就会变成凑时长的空镜。"})
    dark = ("夜", "暗", "黑", "阴", "冷调", "低照度")
    if genre_hint and any(h in genre_hint.lower() for h in _PROMO_HINTS) and any(d in " ".join([loc, overview]) for d in dark):
        checks.append({"id": "Q4", "level": "error",
                       "title": "宣传片里出现暗调/夜戏描述",
                       "detail": "场景描述里带「%s」，模型会往阴冷/恐怖走（这是「宣传片做成恐怖片」的直接原因）。"
                                 "宣传片的夜戏也要用暖光和人造光托住。" % "、".join(d for d in dark if d in " ".join([loc, overview]))})
    if not names and not lines:
        checks.append({"id": "Q5", "level": "info", "title": "无人物也无台词",
                       "detail": "这是纯画面空镜。要么承担转场/氛围，要么删掉。"})

    # ── v2.1 自检：状态改变 / 信息必要性 ──
    if _clean(shot.get("state_before")) and _clean(shot.get("state_before")) == _clean(shot.get("state_after")):
        checks.append({"id": "Q7", "level": "error", "title": "状态没有改变",
                       "detail": "开头和结束的状态写成了同一句 —— 这一镜等于没发生任何事。"
                                 "至少改一个：信息/关系/位置/情绪/物体状态。"})
    if not (purpose or lines):
        checks.append({"id": "Q8", "level": "warn", "title": "删掉这一镜观众也不损失什么",
                       "detail": "这一镜没有承担信息，也没有台词。要么给它一个"
                                 "「删掉就不懂」的信息，要么合并到相邻镜头。"})

    # ── v2.2 自检 ──
    if montage and lines:
        checks.append({"id": "Q9", "level": "warn", "title": "蒙太奇段写了台词",
                       "detail": "蒙太奇段靠节奏与画面推进，台词会拖慢它。把台词挪到相邻的"
                                 "有对话的段落，这一段只留动作与视觉变化。"})
    if not montage and len(actions) <= 1 and dur >= 5:
        checks.append({"id": "Q10", "level": "info", "title": "单一动作已自动拆成组合动作",
                       "detail": "编译器已按「起手/执行/结果」补成三段；如果你要的是更复杂的动作，"
                                 "直接在分镜里写 2-3 个动作点更好。"})

    # ── 手法误用也变成自检提示（防止模型用错手法）──
    for _w in (craft_misuse or [])[:1]:
        checks.append({"id": "Q6", "level": "info", "title": "手法风险", "detail": str(_w)})

    return {"prompt": prompt, "tone": tone, "beats": beats, "checks": checks,
            "subject": subject, "sound": sound,
            "craft": craft_names, "craft_text": craft_text}


def audit_shot(shot: Dict[str, Any], *, genre_hint: str = "") -> List[Dict[str, str]]:
    """只要自检结果（给界面/体检用，不生成提示词）。"""
    return compile_shot(shot, genre_hint=genre_hint).get("checks") or []
