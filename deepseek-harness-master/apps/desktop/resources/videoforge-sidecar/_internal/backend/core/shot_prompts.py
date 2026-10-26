# -*- coding: utf-8 -*-
"""VideoForge · 分镜提示词编译（三段式 + 角色/场景/音色锚定）

═══════════════════════════════════════════════════════════════
为什么要这个模块（2026-10-21 第 5 轮，用户第 5 条）
═══════════════════════════════════════════════════════════════
之前每镜只存一句话概述（`layer1_overview`），出片时把这句直接丢给视频模型：
  · 没有镜头语言（景别/机位/运动），模型自己编运镜 → 每条片子构图都不一样；
  · 没有角色锚定短语，同一个角色在不同镜里长相/服装漂移；
  · 没有场景锚定短语，同场景换机位后空间关系对不上（道具跑到另一边）；
  · 没有音色锚定，多镜之间语气断裂；
  · 没有负面词，出现字幕/多余路人/畸形手。

本模块把「分镜原始信息」编译成**三段专业提示词**，写回镜头：
    video_prompt     画面质感 + 场景 + 主体（角色锚定）+ 镜头 + 动作 + 构图 + 人数/禁文字
    negative_prompt  禁文字/多余人物/畸形手/穿模/镜头切换…
    audio_prompt     有无台词 + 环境音 + 音乐床与人声的 dB 关系
外加两个锚定短语（character_anchor / scene_anchor）与场景锚点表，
全部落在 `layer3_constraints` 的 JSON 里（不改表结构，旧数据照常能读）。

设计来自 claude-opus-5 的协议稿（2026-10-22），做了两处工程化收敛：
  · 场景锚点表**不用世界坐标**（模型看不懂 x/y），改用「方位逻辑」中文短语；
  · 锚定短语**由代码拼**（不靠模型自由发挥），保证跨镜逐字一致。
"""

from __future__ import annotations

import json
import logging
import re
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger("videoforge.shot_prompts")

#: 视频质感基底（可被项目设置覆盖，先给一条稳妥默认）
# ★ 2026-10-23（用户：「人物脸部还出现莫名其妙多了斑点」）：
#   原来这句里的**「轻微胶片颗粒」**就是斑点的主因 —— 颗粒是全画幅的，
#   480P + 面部特写时它就变成"脸上多出来的麻点"，而模型还会把它当成"皮肤质感"。
#   现在：**不要胶片颗粒**，改成"皮肤自然细腻"（想要的质感用光线/景深给，别用噪点给）。
#: ★ 2026-10-23：可选"非人脸主角"风格（doll/mecha/mask），作为一致性兜底。
#:   抖音拉片里那条爆款 AI 短片的主角就是「机器人牛仔 + 关节点人偶」——
#:   真人脸是 AI 生成里最容易崩的部分，玩偶/机械/面具反而更稳。
#:   取值来自项目设置（main.py 编译前注入）或单镜字段 `character_style`。
DEFAULT_CHARACTER_STYLE = ""

STYLE_BASE = "电影级写实质感，自然色彩，画面干净，皮肤自然细腻"
#: 负面词基底（平台通用，堆多了反而稀释，控制在 12 项内）
#  ★ 2026-10-23（用户要求）：「分镜不要提"不要文字"的负面词，文字这块得按剧情需要再设定」。
#    原来这里第一项就是「文字、字幕、水印、招牌字样」—— 等于每个镜头都先声明"别出字"。
#    问题是：**剧情真的需要字的时候**（招牌/标语/手机屏/包装/信纸/遗照），
#    这句话会把模型想画的东西一起压掉，出来的牌子是空白板。
#    所以文字不再走负面词，而是：
#      ① 剧情需要 → `text_intent()` 识别出载体，在**正文里正向写明**该出现什么字；
#      ② 剧情不需要 → 什么都不提（模型本来也不会主动加字，不需要一句负面去压）。
#    水印/logo 不是"剧情文字"，仍留在负面里（那是平台产物，不是画面内容）。
NEGATIVE_BASE = (
    "水印、logo 角标；多余人物、路人；畸形手指、多指；"
    "穿模、肢体扭曲；镜头切换、抖动；人物出画或被裁切；面部变形；"
    # ★ 2026-10-23：脸部伪影（用户：「人物脸部莫名其妙多了斑点」）。
    #   斑点/噪点/摩尔纹/过度锐化都会破坏人脸一致性 —— 负向显式压掉。
    "脸部噪点、皮肤斑点、色斑、颗粒感、摩尔纹、过度锐化、脸部脏污；"
    # ★ 2026-10-23：闪身步（用户：「单个分镜物体奇怪变化（闪身步）」）。
    #   人物在镜头内位置突变/瞬移，是最常见的"不丝滑"表现，负向显式压掉。
    "人物瞬移、位置突变、物体凭空消失或换位；"
    # ★ 2026-10-22（强模型规格）：接龙模式丢掉了参考图，身份只能靠文字锁，
    #   所以负面词必须显式禁止"换脸/换装"。
    "更换发型、发色、衣服、配饰、疤痕位置；角色年龄体型突变"
)

#: 画面内"文字载体"→ 怎么在提示词里正向描述。
#  ★ 只在剧情文本真的出现这些载体时才加这一句；否则一个字都不提。
#    这样"文字按剧情需要设定"，而不是靠负面词一刀切。
TEXT_CARRIERS = (
    ("招牌", "门头招牌"), ("牌匾", "牌匾"), ("横幅", "横幅"), ("标语", "标语"),
    ("海报", "海报"), ("广告牌", "广告牌"), ("屏幕", "屏幕"), ("手机", "手机屏幕"),
    ("包装", "产品包装"), ("标签", "标签"), ("说明书", "说明书"),
    ("信", "信纸"), ("字条", "字条"), ("纸条", "纸条"), ("通知", "通知单"),
    ("价格", "价签"), ("菜单", "菜单"), ("书", "书页"), ("报纸", "报纸"),
    ("遗照", "遗照"), ("证件", "证件"), ("合同", "合同"),
)


def text_intent(*texts: Any) -> str:
    """从剧情文本里判断"这一镜需不需要画面文字"，返回要追加的那句话（不需要就空串）。

    返回形如：`画面内{门头招牌}上的文字按剧情呈现（内容：「招工」），字迹清晰可辨；字幕由后期叠加，画面不重复叠字。`
    找不到载体时返回空串 —— **一个字都不提文字**（用户要求：别写"不要文字"）。
    """
    blob = " ".join(str(t or "") for t in texts if t)
    if not blob:
        return ""
    hit = ""
    for kw, label in TEXT_CARRIERS:
        if kw in blob:
            hit = label
            break
    if not hit:
        return ""
    # 尽量把括号/引号里的内容当成"要显示的字"
    quoted = ""
    for pat in ("「([^」]{1,16})」", "“([^”]{1,16})”", '"([^"]{1,16})"', "『([^』]{1,16})』"):
        m = re.search(pat, blob)
        if m:
            quoted = m.group(1).strip()
            break
    tail = ("（内容「%s」）" % quoted) if quoted else "（按剧情写清内容）"
    return ("画面内%s上的文字按剧情呈现%s，字迹清晰可辨；"
            "字幕由后期叠加，画面里不重复叠字。" % (hit, tail))



#: 内部枚举 → 中文（这些值来自场景卡，直接吐给视频模型等于没写）
_ENUM = {
    "outdoor": "室外", "indoor": "室内", "large": "开阔", "medium": "中等", "small": "狭小",
    "day": "白天", "night": "夜晚", "dusk": "黄昏", "dawn": "清晨",
    "sunny": "晴", "rainy": "雨", "snowy": "雪", "overcast": "阴", "cloudy": "多云", "foggy": "雾",
    "natural": "自然光", "warm": "暖光", "cool": "冷光", "dim": "弱光", "bright": "强光",
    "无服装": "", "none": "", "null": "",
}


def _cn(value: Any) -> str:
    """把枚举/英文标签翻成中文；不认识就原样返回。"""
    text = _s(value)
    if text == "":
        return ""
    return _ENUM.get(text.strip().lower(), _ENUM.get(text.strip(), text))

#: 「只出声不出镜」的角色标记（旁白/画外音/解说…）——
#:   这类角色**不进画面主体、不算人数**，只进音频提示词的音色锚定。
VOICE_ONLY_MARKS = ("旁白", "画外", "解说", "旁述", "声音角色", "不出镜", "无出镜", "只闻其声")


def is_voice_only(char: Dict[str, Any]) -> bool:
    """这个角色是不是「只出声不出镜」？"""
    name = _s(char.get("name"))
    costume = _s(char.get("costume_main")) + _s(char.get("description"))[:80]
    for mark in VOICE_ONLY_MARKS:
        if mark in name or mark in costume:
            return True
    return False


def _s(value: Any, default: str = "") -> str:
    return str(value or "").strip() or default


def character_anchor(char: Dict[str, Any]) -> str:
    """从角色卡生成 30-50 字的锚定短语（跨镜逐字复用）。"""
    name = _s(char.get("name"), "角色")
    desc = _s(char.get("description"))
    age = _s(char.get("age"))
    body = _s(char.get("body_type"))
    costume = _s(char.get("costume_main"))

    # ★ 2026-10-22（按强模型规格重写）：外貌特征要**给 3 个**，不是 1 个。
    #   规格：身份 → 外貌(3个特征) → 服装 → 状态，30-60 字；
    #   接龙（丢参考图）时外貌要扩到 20-25 字。
    features: List[str] = []
    for keyword in ("疤", "痣", "眼镜", "胡子", "白发", "秃", "辫", "短发", "长发",
                    "黑发", "皱纹", "雀斑", "国字脸", "圆脸", "浓眉", "酒窝", "胡茬"):
        if keyword in desc and len(features) < 3:
            idx = desc.find(keyword)
            window = desc[max(0, idx - 5): idx + len(keyword) + 3]
            # ★ 切出来的窗口会**跨逗号**（"中，豆大的黑痣"）⇒ 再按标点拆开，
            #   只留**含该关键词**的那一段（旧写法把"中""左""丝"这些别的字段也带进提示词）。
            piece = ""
            for chunk in re.split(r"[，。、；;\s]+", window):
                if keyword in chunk and len(chunk) >= 2:
                    piece = chunk
                    break
            if piece and piece not in features:
                features.append(piece)
    feature = "，".join(features) if features else ""
    parts = [f"{name}"]
    if feature:
        parts.append(feature)
    if age:
        # 年龄字段可能已经是"50 岁上下 / 成年 / 中年" —— 只在纯数字时才补"岁"
        parts.append(age if any(mark in age for mark in ("岁", "上下", "左右", "成年", "中年", "老年", "少年", "青年")) else f"{age}岁")
    if body:
        parts.append(body)
    costume_text = _s(_cn(costume)).strip("。．.、 ")
    # ★ 服装为空就别拼"穿无服装"（蛇/道具这类角色会出现脏话；
    #   注意角色卡里常写成"无服装。"带句号，要先去掉标点再判断）。
    if costume_text and costume_text not in ("无服装", "无", "不适用", "无衣物"):
        parts.append(f"穿{costume_text}")
    # ★ "保持同一张脸"只对**有外貌描述**的角色成立（蛇/物件不适用）。
    if feature or costume_text:
        parts.append("全程保持同一张脸、年龄、体型与服装")
    anchor = "，".join(parts)
    if len(anchor) > 60:  # 太长会稀释注意力
        anchor = anchor[:58] + "…"
    return anchor


def scene_anchor(scene: Dict[str, Any]) -> str:
    """从场景卡生成场景锚定短语（同场景所有镜头复用）。"""
    name = _s(scene.get("name"), "场景")
    location = _s(scene.get("location_type"))
    desc = _s(scene.get("description"))
    lighting = _s(scene.get("lighting"))
    when = _s(scene.get("time_of_day"))
    weather = _s(scene.get("weather"))

    # 只取描述里的**稳定空间信息**：地貌/材质/布局
    spatial = ""
    for keyword in ("路", "街", "巷", "门", "窗", "柜台", "货架", "桌", "椅", "墙", "雪", "雨", "树", "田", "河", "台阶"):
        if keyword in desc:
            idx = desc.find(keyword)
            spatial = desc[max(0, idx - 6): idx + 8].strip("，。、 ")
            break
    parts = [f"场景“{name}”"]
    location_cn = _cn(location)
    if location_cn:
        parts.append(location_cn)
    if spatial:
        parts.append(spatial)
    lighting_cn = _cn(lighting)
    when_cn = _cn(when)
    weather_cn = _cn(weather)
    if lighting_cn:
        # "夜晚"+"夜晚光" 这类重复要合并（lighting 字段常常就是 time_of_day）
        if lighting_cn == when_cn:
            parts.append(f"{lighting_cn}光")
        else:
            parts.append(f"{lighting_cn}光" if not lighting_cn.endswith("光") else lighting_cn)
    if when_cn and when_cn != lighting_cn:
        parts.append(when_cn)
    if weather_cn:
        parts.append(weather_cn)
    return "，".join(parts)


def scene_anchors(scene: Dict[str, Any], characters: List[Dict[str, Any]]) -> Dict[str, Any]:
    """场景锚点表（方位逻辑版，不含世界坐标）。

    只在**同场景有两镜以上**时才有价值：它约束"谁站在哪、道具在哪边、光从哪来"。
    """
    names = [ _s(c.get("name")) for c in characters if _s(c.get("name")) ]
    return {
        "scene_id": _s(scene.get("id")),
        "scene_name": _s(scene.get("name")),
        "layout": {
            "角色站位": {n: "按剧本在场景中的固定位置，跨镜不变" for n in names},
            "道具方位": "分镜里写到的道具方位一经确定，后续镜头不得左右对调",
            "光源方向": _s(scene.get("lighting")) or "统一光源方向",
        },
        "continuity": {
            "time_of_day": _s(scene.get("time_of_day")),
            "weather": _s(scene.get("weather")),
            "allowed_changes": ["机位", "景别", "镜头运动"],
        },
    }


def _dialogue_text(item: Dict[str, Any]) -> str:
    """从时间轴项里取台词文本（对话可能是字符串或 {character,text}）。"""
    raw = item.get("dialogue") or item.get("line") or item.get("voiceover") or ""
    if isinstance(raw, dict):
        who = _s(raw.get("character"))
        text = _s(raw.get("text") or raw.get("line"))
        return f"{who}：{text}" if who and text else text
    return _s(raw)


#: 「节奏约束句」的硬上限（字）。宁短不长：一句长指令会稀释前面所有画面约束。
DIRECTOR_RHYTHM_LIMIT = 120

#: 节奏档位 → 镜头密度描述（新剧本的 pacing 是对象，这里兼容旧的**字符串档位**）
_PACING_DENSITY = {
    "fast_drama": "快切",
    "promo": "快切",
    "talking": "长镜头",
    "ambient": "长镜头",
}


def director_rhythm_line(durations: Any = None, pacing: Any = None,
                         max_chars: int = DIRECTOR_RHYTHM_LIMIT) -> str:
    """★ 2026-10-24（导演门）：生成视频提示词末尾的**节奏约束句**（≤120 字）。

    为什么要有这一行：同一批分镜如果时间片全是等长（如 3/3/3），模型会匀速切分，
    金句镜没有停留、爆发镜拖满整镜 —— 节奏是"写进提示词"才存在的，不是剪辑时补的。

    数据来源（按优先级，都没有就退回"混合/中景"）：
      · `pacing`：新剧本的 pacing 对象（plot/emotion/shot_density/size_focus），
        或旧的字符串档位（fast_drama/promo/talking/ambient），或 None；
      · `durations`：本镜 `layer2_timeline` 的片长列表（秒）。
    只返回这一行文本，**不改动任何已有约束**。
    """
    density, focus = "混合", "中景"
    if isinstance(pacing, dict):
        density = _s(pacing.get("shot_density")) or _s(pacing.get("density")) or density
        focus = _s(pacing.get("size_focus")) or _s(pacing.get("size")) or focus
    elif isinstance(pacing, str) and pacing.strip():
        key = pacing.strip().lower()
        density = _PACING_DENSITY.get(key) or _s(pacing) or density
    durs: List[str] = []
    for d in (durations or []):
        try:
            v = round(float(d), 1)
        except (TypeError, ValueError):
            continue
        if v > 0:
            durs.append(str(int(v)) if v == int(v) else str(v))
    durs_txt = "/".join(durs) or "按内容给"
    line = ("【节奏】本镜%s；景别重心%s；时间片 %s；"
            "金句镜给足停留、爆发镜 1~2 秒完成；禁止匀速等长切分。"
            % (density, focus, durs_txt))
    if len(line) > max_chars:
        line = line[:max(0, max_chars - 1)] + "。"
    return line


def _authored_visual_prompt(shot: Dict[str, Any], timeline: List[dict], has_cast: bool) -> str:
    """Compile authored timing directly; do not invent action, lighting or camera beats."""
    parts = [_s(shot.get("layer1_overview"))]
    for item in timeline:
        if not isinstance(item, dict):
            continue
        fields = [_s(item.get("action")), _s(item.get("camera"))]
        if has_cast:
            fields.append(_s(item.get("expression")))
        parts.append("{}–{}秒：{}".format(item.get("start", ""), item.get("end", ""),
                                         "；".join(v for v in fields if v)))
    constraints = shot.get("layer3_constraints") or {}
    if isinstance(constraints, str):
        try:
            constraints = json.loads(constraints)
        except ValueError:
            constraints = {}
    if isinstance(constraints, dict):
        for key, label in (("must_keep", "必须保持"), ("must_appear", "必须出现"),
                           ("must_happen", "必须发生"), ("must_not_appear", "不得出现"),
                           ("must_not_happen", "不得发生")):
            values = constraints.get(key) or []
            if values:
                parts.append(label + "：" + "；".join(map(str, values if isinstance(values, list) else [values])))
    return "\n".join(p for p in parts if p)


def compile_shot_prompts(
    shot: Dict[str, Any],
    characters: Dict[str, Dict[str, Any]],
    scenes: Dict[str, Dict[str, Any]],
    *,
    style_base: str = STYLE_BASE,
    anchor_sentence: str = "",
    forbidden_note: str = "",
    prev_shot: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """一镜 → 三段提示词 + 两个锚定短语。

    @param shot 镜头行（含 layer1_overview / layer2_timeline / character_ids / scene_id）
    @param characters id → 角色卡
    @param scenes id → 场景卡
    @returns `{video_prompt, negative_prompt, audio_prompt, character_anchor, scene_anchor}`
    """
    timeline = shot.get("layer2_timeline")
    if isinstance(timeline, str):
        try:
            timeline = json.loads(timeline)
        except Exception:  # noqa: BLE001
            timeline = []
    timeline = timeline if isinstance(timeline, list) else []

    cast = []
    voice_only = []
    for cid in (shot.get("character_ids") or []):
        char = characters.get(str(cid))
        if not char:
            continue
        # ★ 只出声不出镜的角色（旁白等）单独拎出来：它们不构成画面主体。
        if is_voice_only(char):
            voice_only.append(char)
        else:
            cast.append(char)
    if (not cast and not voice_only and len(characters) == 1
            and "character_ids" not in shot and "cast" not in shot):
        # ★ 回退分支也要分流：只有一张角色卡时，若它是"只出声不出镜"的角色（旁白等），
        #   绝不能当成画面主体（否则模型会把旁白画成一个人站在街上）。
        only = list(characters.values())[0]
        if is_voice_only(only):
            voice_only.append(only)
        else:
            cast.append(only)
    scene = scenes.get(_s(shot.get("scene_id"))) or (list(scenes.values())[0] if len(scenes) == 1 else {})

    anchors = [character_anchor(c) for c in cast]
    scene_text = scene_anchor(scene) if scene else ""

    cameras = [_s(i.get("camera")) for i in timeline if isinstance(i, dict) and _s(i.get("camera"))]
    actions = [_s(i.get("action")) for i in timeline if isinstance(i, dict) and _s(i.get("action"))]
    lines = [_dialogue_text(i) for i in timeline if isinstance(i, dict)]
    lines = [line for line in lines if line]
    camera_contract = " ".join(cameras + [_s(shot.get("layer1_overview"))])
    preserve_timing = (bool(shot.get("camera_timing_locked"))
                       or shot.get("previz_camera") == "manual_track"
                       or any(word in camera_contract for word in
                              ("缓慢", "慢慢", "缓缓", "固定焦距", "不切镜", "一镜到底", "连续镜头", "轨迹", "停稳")))
    use_authored = not cast or preserve_timing

    # ══════════════════════════════════════════════════════════════
    # ★★ 2026-10-23 v2：video_prompt 改由**叙事驱动七段式**编译
    #   （core/prompt_spec.py：基调/主体处境/动作节拍/镜头动机/画面层级/声音）。
    #   为什么：旧版是一张"物料清单"（场景+主体+景别+一个动作，实测仅 424 字），
    #   实测后果就是"6 秒只有一只手在敲、宣传片像恐怖片"。
    #   旧的 主体/镜头/动作/overview 四行已被 v2 吸收，这里不再重复写。
    # ══════════════════════════════════════════════════════════════
    selected_style = (style_base.replace("，皮肤自然细腻", "")
                      if not cast and style_base == STYLE_BASE else style_base)
    parts: List[str] = [selected_style + "。"]
    _v2_checks: List[Dict[str, Any]] = []
    overview = _s(shot.get("layer1_overview"))
    try:
        if use_authored:
            parts.append("\n" + _authored_visual_prompt(shot, timeline, bool(cast)) + "\n")
            if preserve_timing:
                parts.append("严格保持上述每个时间段的运镜速度、起止点和停稳时刻，不追加运镜或剪切。")
        else:
            from core.prompt_spec import compile_shot as _v2_compile
            _style = str(shot.get("character_style") or DEFAULT_CHARACTER_STYLE or "").strip()
            _v2 = _v2_compile(shot, characters=characters, scenes=scenes,
            prev_shot=prev_shot,
            character_style=(_style or None),
            genre_hint=" ".join([overview, _s(shot.get("scene_name")),
            _s(shot.get("location")), _s(shot.get("genre"))]))
            parts.append("\n" + str(_v2.get("prompt") or "") + "\n")
            _v2_checks = _v2.get("checks") or []
    except Exception as _e:  # noqa: BLE001
        logger.warning("v2 提示词编译失败，回退到物料清单写法：%s", _e)
        if scene_text:
            parts.append(scene_text + "。")
        if anchors:
            parts.append("主体：" + "；".join(anchors) + "。")
        if cameras:
            parts.append("镜头：" + "；".join(cameras[:2]) + "。")
        if actions:
            parts.append("动作：" + "；".join(actions[:4]) + "。")
        if overview:
            parts.append("这一镜要讲清楚的是：" + overview + "。")
    if scene_text:
        parts.append(scene_text + "。")
    if not cast:
        # ★ 只有旁白/画外音时：**明确说本镜没有出镜人物**，
        #   否则模型会把"旁白"当成一个要出现在画面里的人。
        parts.append("本镜没有出镜人物，画面只交代环境与空镜。")
    elif anchors:
        # 人数只数"人"：蛇/狗这类是主体但不是人（写"共 2 人"会凭空多一个人）
        animal_marks = ("蛇", "狗", "猫", "鸟", "狼", "马", "牛", "羊", "兔", "鱼", "鼠")
        people = [c for c in cast if not any(mark in _s(c.get("name")) for mark in animal_marks)]
        if people:
            parts.append(f"画面内人物 {len(people)} 人。")
    # ★ 2026-10-23（用户要求）：这里原来写死「画面无任何文字、字幕、水印。」
    #   等于每镜都先声明"别出字" —— 剧情真要一块写着「招工」的牌子时，
    #   模型也会把牌子画成空白板。现在改成**按剧情判断**：
    #     · 文本里出现招牌/标语/手机屏/包装/信纸等载体 → 正向写明该出现什么字；
    #     · 没有这类载体 → 一个字都不提文字（不写负面词）。
    _ti = text_intent(shot.get("layer1_overview"), shot.get("layer2_timeline"),
                      shot.get("action"), shot.get("dialogue"),
                      shot.get("scene_name"), shot.get("note"))
    if _ti:
        parts.append(_ti)
    # ★ 2026-10-23（用户：「在各个分镜上做的不够丝滑」「闪身步」）：
    #   人物位置变化必须是**连续移动**，不是瞬移；镜头内物体不能凭空换位。
    if actions and cast:
        parts.append("人物位置变化是连续移动（走过去/走过来），不要瞬移或闪身步；"
                     "画面里的物体保持原位，不凭空消失或换位。")
    # ★ 2026-10-23（用户要求）：口型/唇语必须和台词同步。
    #   写进**视频提示词**（这是画面指令），不写负面词。
    #   变量名是 `lines`（第 302 行从时间轴抽出来的台词），不是 dialogue_lines。
    if lines:
        parts.append("台词逐字与口型对齐（唇语同步），说完即闭嘴，不要出现「声音停了嘴还在动」。")
    # ★ 场景锚点句（跨镜连贯）：同一场景的每一镜都原样注入同一段空间关系。
    if anchor_sentence:
        parts.append(anchor_sentence)


    # ★ 2026-10-24（用户：「提示词描述的节奏慢」—— 这是真病根）：
    #   把节奏指令拼进**视频提示词**。没有这段时，模型按"电影感"自己慢慢来，
    #   实测成片 30 秒只有 2 个镜头、每镜 15 秒缓慢推镜。
    _pacing_key = ""
    try:
        from core import scriptplan as _spc
        _pacing_key = str(shot.get("pacing") or "") or _spc.pacing_for(
            style=shot.get("style"),
            intent=" ".join([_s(overview), _s(shot.get("genre")),
                             _s(shot.get("scene_name")), _s(shot.get("location"))]))
        _brief = "" if preserve_timing else _spc.prompt_pacing_brief(_pacing_key)
        if _brief:
            parts.append(_brief)
        # 运镜写着"缓慢"的，加完成时限（不篡改创作意图，只钉时间）
        for _cam in ([] if preserve_timing else cameras[:2]):
            _note = _spc.camera_timing_note(_cam, _pacing_key)
            if _note:
                parts.append(_note)
                break
    except Exception as _e:  # noqa: BLE001
        logger.warning("节奏指令注入失败（不影响其余提示词）：%s", _e)

    # ★ 2026-10-24（导演门·节奏约束句）：**纯追加**——在视频提示词末尾加一行 ≤120 字
    #   的节奏约束，读本镜 `layer2_timeline` 的片长列表 + shot/scene 的 pacing
    #   （新剧本 pacing 是对象；没有就写"混合/中景"）。上面所有已有约束一律不动。
    try:
        _durs: List[float] = []
        for _it in timeline:
            if not isinstance(_it, dict):
                continue
            try:
                _len = float(_it.get("end")) - float(_it.get("start"))
            except (TypeError, ValueError):
                continue
            if _len > 0:
                _durs.append(_len)
        _rhythm = director_rhythm_line(
            _durs,
            shot.get("pacing") or shot.get("pacing_intent")
            or (scene or {}).get("pacing") or (scene or {}).get("pacing_intent"))
        if _rhythm and not preserve_timing:
            parts.append(_rhythm)
    except Exception as _e:  # noqa: BLE001
        logger.warning("节奏约束句追加失败（不影响其余提示词）：%s", _e)
    video_prompt = "".join(parts)

    audio_bits: List[str] = []
    # ★ 2026-10-23（用户要求）：声音要跟画面事件同步（敲键盘就有对应声音）。
    #   ★ 注意：这一段必须在 `audio_bits` 初始化**之后**，否则 NameError。
    if actions and cast:
        audio_bits.append("拟音：画面动作（%s）要有对应声音，与动作同起同落。"
                          % "、".join(actions[:3]))
    elif not cast:
        audio_bits.append("仅保留原稿明确要求的环境音与事件声，不为镜头运动或静止物体添加拟音。")
    # 音色锚定：同一角色跨镜必须同一声线（voice_id + 语速 + 基础情绪）
    voice_lines = []
    for char in list(cast) + list(voice_only):
        vid = _s(char.get("voice_id"))
        if vid:
            voice_lines.append(f"{_s(char.get('name'))}→{vid}（语速 0.92，情绪克制，跨镜保持一致）")
    if voice_lines:
        audio_bits.append("音色锚定：" + "；".join(voice_lines) + "。")
    if lines:
        audio_bits.append("台词：" + " / ".join(lines[:4]) + "（≤40字，语气自然、口型对得上）")
    else:
        audio_bits.append("本镜无台词。")
    audio_bits.append("环境音：与场景一致的真实环境声，作为**垫底**铺满全程，明显低于人声、不抢台词（成片里由环境声床自动补，这里只声明质感）。")
    if str(shot.get("bgm") or "").strip().lower() in ("1", "true", "yes"):
        audio_bits.append("音乐床：极轻，比人声低 10–15dB。")
    else:
        audio_bits.append("本镜无配乐；没有台词时不添加旁白、呼吸或人声。")
    audio_prompt = "".join(audio_bits)

    negative_prompt = NEGATIVE_BASE + (forbidden_note or "")
    # ★ 2026-10-24：快节奏档位补"慢动作"负向（慢是提示词写出来的，这里压掉）
    try:
        from core import scriptplan as _spn
        if not preserve_timing:
            negative_prompt += _spn.prompt_pacing_negative(_pacing_key)
    except Exception:
        pass
    return {
        "video_prompt": video_prompt,
        "negative_prompt": negative_prompt,
        "audio_prompt": audio_prompt,
        "character_anchor": "；".join(anchors),
        "scene_anchor": scene_text,
        # ★ 2026-10-23 v2：这一镜的自检（只有物件动作/没有动作节拍/没镜头动机/
        #   宣传片里出现暗调…），界面与体检直接显示，用户能在出片前就发现。
        "prompt_checks": _v2_checks,
        "compiled_by": "shot_prompts/authored-v1" if use_authored else "shot_prompts/v2.1",
    }


def attach_prompts(shot: Dict[str, Any], compiled: Dict[str, Any]) -> Dict[str, Any]:
    """把编译结果并进 `layer3_constraints`（保留原有约束，不覆盖）。"""
    layer3 = shot.get("layer3_constraints")
    if isinstance(layer3, str):
        try:
            layer3 = json.loads(layer3) if layer3.strip() else {}
        except Exception:  # noqa: BLE001
            layer3 = {"_raw": layer3}
    if not isinstance(layer3, dict):
        layer3 = {}
    prompts = layer3.get("prompts") if isinstance(layer3.get("prompts"), dict) else {}
    prompts.update(compiled)
    layer3["prompts"] = prompts
    return layer3


def stored_prompts(shot: Dict[str, Any]) -> Dict[str, Any]:
    """读回已编译的三段提示词（没有就返回空 dict）。"""
    layer3 = shot.get("layer3_constraints")
    if isinstance(layer3, str):
        try:
            layer3 = json.loads(layer3) if layer3.strip() else {}
        except Exception:  # noqa: BLE001
            return {}
    if not isinstance(layer3, dict):
        return {}
    prompts = layer3.get("prompts")
    return prompts if isinstance(prompts, dict) else {}


async def compile_project_prompts(
    db: Any,
    project_id: str,
    settings: Optional[Dict[str, Any]] = None,
    output_dir: str = "",
) -> int:
    """把一个项目的所有镜头重新编译三段提示词并写回。返回编译成功的镜数。

    ★ 2026-10-22：先按场景生成**空间锚点表**（一次 LLM 调用，缓存复用），
      再逐镜编译 —— 同一场景的每一镜注入**同一段**锚点句 ⇒ 跨镜不穿帮。
    """
    if not project_id:
        return 0
    characters = {str(c.get("id")): c for c in (db.list_characters(project_id) or [])}
    scenes = {str(s.get("id")): s for s in (db.list_scenes(project_id) or [])}
    shots = db.list_shots(project_id) or []
    settings = settings or {}
    output_dir = output_dir or str(settings.get("output_dir")
                                   or os.path.join(os.getcwd(), "data", "outputs"))

    # ① 每个场景一张锚点表
    anchors_by_scene: Dict[str, Dict[str, Any]] = {}
    try:
        from core import scene_anchors as _sa
        by_scene: Dict[str, List[Dict[str, Any]]] = {}
        for s in shots:
            by_scene.setdefault(str(s.get("scene_id") or ""), []).append(s)
        for scene_id, scene_shots in by_scene.items():
            if not scene_id or scene_id not in scenes:
                continue
            try:
                anchors_by_scene[scene_id] = await _sa.build_scene_anchors(
                    db, scenes[scene_id], scene_shots, settings, output_dir=output_dir)
            except Exception as e:  # noqa: BLE001
                logger.warning("场景 %s 锚点表失败（该场景不注入锚点）：%s", scene_id, e)
    except Exception as e:  # noqa: BLE001
        logger.warning("锚点表环节整体失败（不影响编译）：%s", e)

    # ② 逐镜编译（带锚点句 + 跨镜接龙判定）
    #
    # ★ 2026-10-22（按强模型规格）：接龙只在「同场景 + 同景别 + 无时间跳跃」时用；
    #   连续接龙满 2 镜后**强制回到独立镜**（重校准），防角色漂移。
    relay_plan: Dict[str, Dict[str, Any]] = {}
    try:
        from core import shot_chain as _sc
        _ordered = sorted(shots, key=lambda s: (s.get("order_index") or 0))
        _chain_len = 0
        for _i, _s in enumerate(_ordered):
            _prev = _ordered[_i - 1] if _i > 0 else None
            _sid = str(_s.get("id") or "")
            _decision = _sc.can_relay(_prev, _s)
            if not _decision.get("relay"):
                _chain_len = 0
                relay_plan[_sid] = {"relay": False, "reason": _decision.get("reason", "")}
                continue
            if _chain_len >= 2:
                _chain_len = 0
                relay_plan[_sid] = {"relay": False, "reason": "已连续接龙 2 镜，本镜强制重校准（回自己的关键帧 + 参考图）"}
                continue
            _chain_len += 1
            relay_plan[_sid] = {
                "relay": True,
                "reason": _decision.get("reason", ""),
                "brief": _sc.continuity_brief(_prev),
                "source_shot_id": str(_prev.get("id") or ""),
            }
    except Exception as e:  # noqa: BLE001
        logger.warning("接龙判定失败（全部按独立镜）：%s", e)
        relay_plan = {}

    done = 0
    _prev_shot = None   # ★ v2.2 动势接力：把上一镜传给编译器
    for shot in shots:
        try:
            scene_id = str(shot.get("scene_id") or "")
            anchors = anchors_by_scene.get(scene_id) or {}
            anchor_sentence = ""
            forbidden_note = ""
            if anchors:
                try:
                    from core import scene_anchors as _sa
                    anchor_sentence = _sa.render_anchor_sentence(anchors)
                    forbidden_note = _sa.render_forbidden_note(anchors)
                except Exception:  # noqa: BLE001
                    pass
            compiled = compile_shot_prompts(
                shot, characters, scenes,
                anchor_sentence=anchor_sentence,
                forbidden_note=forbidden_note,
                prev_shot=_prev_shot)
            # ★ 接龙信息写进提示词载荷：出片时据此取上一镜尾帧当首帧。
            _plan = relay_plan.get(str(shot.get("id") or "")) or {}
            compiled["chain_mode"] = "relay" if _plan.get("relay") else "independent"
            if _plan.get("relay"):
                compiled["relay_source_shot_id"] = _plan.get("source_shot_id", "")
                compiled["continuity_brief"] = _plan.get("brief", "")
                # ★ 缓解"首帧绑架"（强模型规格）：动作从**当前姿势**开始写清楚，
                #   模型就不会原地扭动。
                compiled["relay_action_hint"] = "从当前姿势继续，动作幅度小而连贯，不要重置人物位置" 
                _relay_first = ""
                try:
                    from core import shot_chain as _sc
                    _relay_first = _sc.find_last_frame(
                        db, str(shot.get("project_id") or ""),
                        str(_plan.get("source_shot_id") or ""), output_dir)
                except Exception:  # noqa: BLE001
                    _relay_first = ""
                if _relay_first:
                    # ★ 首帧冲突预检（安全带）：首帧的姿态与这一镜的动作打架时，
                    #   接龙会让模型原地扭动/穿帮 ⇒ 自动降级独立镜（用回自己的关键帧+参考图）。
                    _conflict = {"conflict": False, "reason": "未预检"}
                    try:
                        from core import shot_chain as _sc
                        _action = " ".join(
                            str(i.get("action") or "") for i in _timeline_items(shot))[:200]
                        _conflict = await _sc.detect_first_frame_conflict(
                            str(_plan.get("brief") or ""), _action, settings or {})
                    except Exception as _e:  # noqa: BLE001
                        logger.warning("首帧冲突预检失败（按不冲突继续）：%s", _e)
                    if _conflict.get("conflict"):
                        compiled["chain_mode"] = "independent"
                        compiled["chain_note"] = (
                            "首帧与本镜动作冲突（%s），已自动降级为独立镜（用回自己的关键帧）"
                            % str(_conflict.get("reason") or "")[:40])
                        compiled["relay_first_frame"] = ""
                    else:
                        compiled["relay_first_frame"] = _relay_first
                        compiled["first_frame_conflict_check"] = str(_conflict.get("reason") or "")[:40]
                else:
                    compiled["chain_mode"] = "independent"
                    compiled["chain_note"] = "上一镜尾帧还没生成，本镜先独立出片"
            db.update_shot(shot["id"], layer3_constraints=json.dumps(
                attach_prompts(shot, compiled), ensure_ascii=False))
            done += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("镜头 %s 提示词编译失败：%s", shot.get("id"), e)
    return done


#: 一致性规则（可逐条断言；来自 claude-opus-5 的协议稿，收敛成代码能跑的 8 条）
def check_project_prompts(db: Any, project_id: str) -> Dict[str, Any]:
    """检查一个项目的分镜提示词一致性，返回 {issues, counts, checked}。

    规则（每条都能用一句 if 判定，不写"注意一致"这种废话）：
      C01 每镜必须有编译好的 video_prompt
      C02 video_prompt 必须包含该镜下每个角色的锚定短语（去标点后比对关键片段）
      C03 video_prompt 必须包含该场景的锚定短语
      C04 同场景的 scene_anchor 必须完全相同（跨镜不许漂）
      C05 有台词的镜，audio_prompt 必须含"台词："
      C06 无台词的镜，audio_prompt 必须含"本镜无台词"
      C07 negative_prompt 非空且包含"文字"
      C08 人数声明与实际入镜人数一致（"画面内人物 N 人" 的 N == 角色卡里的人数）
    """
    characters = {str(c.get("id")): c for c in (db.list_characters(project_id) or [])}
    scenes = {str(s.get("id")): s for s in (db.list_scenes(project_id) or [])}
    shots = db.list_shots(project_id) or []
    issues: List[Dict[str, Any]] = []
    scene_anchors_seen: Dict[str, set] = {}

    def add(shot: Dict[str, Any], code: str, message: str) -> None:
        issues.append({
            "shot_id": shot.get("id"),
            "index": shot.get("order_index"),
            "code": code,
            "message": message,
        })

    for shot in shots:
        prompts = stored_prompts(shot)
        video = _s(prompts.get("video_prompt"))
        negative = _s(prompts.get("negative_prompt"))
        audio = _s(prompts.get("audio_prompt"))
        if video == "":
            add(shot, "C01", "这一镜还没有编译提示词（先跑一次编译）")
            continue

        # C02：入镜角色锚定短语是否整段出现在 video_prompt 里
        # ★ 只对**出镜角色**生效：旁白/画外音是"只出声不出镜"，
        #   它的锚定短语**不该**出现在画面提示词里（否则模型会画出一个人）。
        cast: List[Dict[str, Any]] = []
        voice_only_chars: List[Dict[str, Any]] = []
        for cid in (shot.get("character_ids") or []):
            char = characters.get(str(cid))
            if not char:
                continue
            if is_voice_only(char):
                voice_only_chars.append(char)
            else:
                cast.append(char)
        for char in voice_only_chars:
            # 声音角色：音色锚定必须出现在 audio_prompt 里（跨镜同一声线）
            vid = _s(char.get("voice_id"))
            if vid and vid not in audio:
                add(shot, "C09", f"声音角色「{_s(char.get('name'))}」的音色 {vid} 没写进 audio_prompt")
        for char in cast:
            anchor = character_anchor(char)
            head = anchor.split("，")[0] if anchor else ""
            if head and head not in video:
                add(shot, "C02", f"video_prompt 里没有角色「{_s(char.get('name'))}」的锚定短语开头『{head}』")

        # C03 / C04：场景锚定
        scene = scenes.get(_s(shot.get("scene_id"))) or {}
        if scene:
            anchor = scene_anchor(scene)
            if anchor and anchor not in video:
                add(shot, "C03", "video_prompt 里没有完整场景锚定短语（跨镜容易换空间）")
            scene_anchors_seen.setdefault(_s(scene.get("id")), set()).add(anchor)

        # C05 / C06：台词一致性
        timeline = shot.get("layer2_timeline")
        if isinstance(timeline, str):
            try:
                timeline = json.loads(timeline)
            except Exception:  # noqa: BLE001
                timeline = []
        has_line = any(_dialogue_text(i) for i in (timeline or []) if isinstance(i, dict))
        if has_line and "台词：" not in audio:
            add(shot, "C05", "这一镜台词有内容，但 audio_prompt 里没有『台词：』")
        if not has_line and "本镜无台词" not in audio:
            add(shot, "C06", "这一镜没有台词，但 audio_prompt 没写『本镜无台词』（可能被配上旁白）")

        # C07：负面词
        # ★ 2026-10-23（用户要求）：**不再要求负面词里必须出现「文字」**。
        #   文字按剧情需要设定（要文字就在正文正向写出来，见 text_intent()），
        #   靠"不要文字"这类负面词一刀切会把剧情需要的招牌/标语一起压掉。
        if negative == "":
            add(shot, "C07", "negative_prompt 是空的（至少要有水印/畸形手/穿模这类通用项）")
        elif "文字" in negative or "字幕" in negative:
            add(shot, "C07",
                "negative_prompt 里写了「文字/字幕」—— 文字不该靠负面词回避："
                "剧情需要就在正文正向写出来（招牌/标语/手机屏上的字），不需要就什么也别提")

        # C08：人数
        animal_marks = ("蛇", "狗", "猫", "鸟", "狼", "马", "牛", "羊", "兔", "鱼", "鼠")
        people = [c for c in cast if not any(mark in _s(c.get("name")) for mark in animal_marks)]
        if people and f"人物 {len(people)} 人" not in video:
            add(shot, "C08", f"镜头里有 {len(people)} 个人，但 video_prompt 的人数声明对不上")
        if not people and "没有出镜人物" not in video and "画面内人物" not in video:
            add(shot, "C08", "这一镜没有出镜人物，但 video_prompt 没写明（模型可能自己加人）")

    # C04（跨镜）：同一场景的锚定短语必须完全一致
    for scene_id, anchors in scene_anchors_seen.items():
        if len(anchors) > 1:
            issues.append({
                "shot_id": None, "index": None, "code": "C04",
                "message": f"场景 {scene_id} 的锚定短语在不同镜里不一致（{len(anchors)} 种写法）",
            })

    # C10 / C11：跨镜连贯 —— 必须含锚点句，且不得出现与锚点相反的方位
    try:
        from core import scene_anchors as _sa
        _out = str((db.get_all_settings() or {}).get("output_dir")
                   or os.path.join(os.getcwd(), "data", "outputs"))
        for shot in shots:
            _scene_id = str(shot.get("scene_id") or "")
            if not _scene_id:
                continue
            _anchors = _sa.load_anchors(_out, project_id, _scene_id)
            if not _anchors:
                continue
            _video = _s(stored_prompts(shot).get("video_prompt"))
            if _video == "":
                continue
            _sentence = _sa.render_anchor_sentence(_anchors)
            if _sentence and _sentence[:24] not in _video:
                add(shot, "C10", "这一镜的 video_prompt 没有该场景的空间锚点句（跨镜会穿帮）")
            for _c in _sa.find_conflicts(_anchors, _video):
                add(shot, "C11", _c)
    except Exception as e:  # noqa: BLE001
        logger.warning("锚点检查跳过：%s", e)

    return {
        "project_id": project_id,
        "shots": len(shots),
        "issues": issues,
        "ok": len(issues) == 0,
    }


def _timeline_items(shot: Dict[str, Any]) -> List[Dict[str, Any]]:
    """镜头的 layer2_timeline（字符串则解 JSON）。"""
    timeline = shot.get("layer2_timeline")
    if isinstance(timeline, str):
        try:
            timeline = json.loads(timeline)
        except Exception:  # noqa: BLE001
            timeline = []
    return [i for i in (timeline or []) if isinstance(i, dict)]
