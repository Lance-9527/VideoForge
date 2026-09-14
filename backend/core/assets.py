# -*- coding: utf-8 -*-
"""
VideoForge · 剧本资产结构化提取（角色 / 场景）

═══════════════════════════════════════════════════════════════════
为什么重写（这是一次真实的质量事故）
═══════════════════════════════════════════════════════════════════
旧实现只是把剧本场次里的 `characters` 名字抄出来，然后：

    db.create_character(project_id=pid, name=name,
                        description="从剧本自动提取")

`description` 字面量就是"从剧本自动提取"这 7 个字，年龄/性别/服装/外貌/性格
**全是空的**。而生成形象图的提示词是按这些字段拼的 →

    角色设定图：李队长。从剧本自动提取 服装：符合角色身份。
    正面半身像，单人，简洁背景，电影级光影，高清写实

等于只给了模型一个名字。模型当然只能凭空编一个人 ——
用户看到的"完全不按剧本里的设定生成"就是这么来的。

本模块改成：把**完整剧本正文**交给 LLM，按固定 schema 抽取**可直接用于
图像生成的结构化人物卡**（外观/年龄/性别/身份/服装/性格/标志特征/道具），
并提供 `build_portrait_prompt()` 把人物卡拼成一条高质量出图提示词。
参考 Pavo 的 asset-extract 设计（大綱 → 角色/场景/道具）。

设计原则：
- LLM 失败不致命 → 调用方可以回落到旧的朴素提取
- 只输出 JSON，解析用项目已有的健壮 `extract_json()`
- 不编造：提示词里明确要求"剧本没写的不要虚构，留空即可"
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("videoforge.assets")

# ═══════════════════════════════════════════════════════════════════
# 角色卡的字段设计：**固定槽位**，不是一段自由文本
# ═══════════════════════════════════════════════════════════════════
# 为什么要把"外貌"和"服装"拆成一个个命名槽位：
#   如果只让模型写一段 appearance，它在第 1 场写"四十来岁、方正脸、短寸"，
#   到第 12 场就可能写成"中年男子、面庞刚毅"，于是同一个角色在不同镜头里
#   长相漂移 —— 这正是"角色不一致"的根因。
#   拆成固定字段后，每个镜头都注入**同一套字段值**，一致性来自结构而不是运气。
CHARACTER_SCHEMA = {
    "name": "角色名（剧本里的称呼，如「李队长」）",
    "aliases": ["别名/绰号，没有就空数组"],
    "role": "protagonist / antagonist / supporting / minor 四选一",
    "gender": "male / female / unknown",
    "age": "年龄或年龄段，如「40 岁上下」",
    # —— 面部（逐项填，每项一个短句）——
    "face_shape": "脸型与骨相，如「方脸，颧骨明显，下颌线硬朗」",
    "skin": "肤色与肤质，如「偏黄，风吹日晒的粗糙感，额头有细纹」",
    "eyes": "眼型与眼神，如「细长眼，眼神锐利，眼窝略深」",
    "hair": "发型+长度+颜色，如「短寸黑发，两鬓已花白，胡茬未刮净」",
    "distinctive": "最能认出他的特征（疤/痣/眼镜/胡须/口音）；没有就写「无」",
    "height_build": "身高体型与姿态，如「中等身高，精瘦，肩背挺直」",
    # —— 服装（按穿着部位拆，颜色+材质+款式都要有）——
    "costume_top": "上装：颜色+材质+款式，如「洗得发白的灰蓝粗布对襟上衣」",
    "costume_bottom": "下装：颜色+材质+款式，如「深灰粗布长裤，裤脚扎进绑腿」",
    "costume_shoes": "鞋：颜色+款式，如「黑色旧布鞋，鞋面有补丁」",
    "costume_accessories": ["配饰/装备，如 布制绑腿、帆布挎包、皮带；没有就空数组"],
    "costume_palette": ["服装主色，2-4 个，写色名或 #hex"],
    "signature": "一句话最独特的识别点（跨镜头必须保持不变的锚点）",
    "prop_detail": "随身道具的具体形制（颜色/材质/尺寸/新旧）",
    # —— 其他 ——
    "personality": "性格与说话风格",
    "relationships": "与其他角色的关系（一句话）",
    "first_appearance": "第一次出现在哪一场（场次序号，数字）",
    # —— 兼容字段：把上面槽位合成自然语言，供旧界面/旧数据展示 ——
    "appearance": "把 face_shape/skin/eyes/hair/distinctive/height_build 合成一段通顺的外貌描述",
    "costume": "把 costume_top/bottom/shoes/accessories 合成一段通顺的服装描述",
}

SCENE_SCHEMA = {
    "name": "地点名（简洁，如「山林小道」）",
    "location_type": "indoor / outdoor",
    "time_of_day": "day / night / dawn / dusk / noon",
    "weather": "clear / rainy / snowy / cloudy / foggy",
    "lighting": "natural / golden_hour / blue_hour / night / neon / studio / candle / overcast",
    # 场景同样拆槽位：换镜头时同一地点的地貌/建筑/陈设必须一致
    "terrain": "地形/地面，如「碎石土路，两侧是枯草与残雪」",
    "architecture": "建筑/构筑物，如「半塌的土坯院墙，木门已歪斜」",
    "furnishings": "陈设/植被/固定物件，如「院中一口枯井、两棵光秃的槐树」",
    "atmosphere": "氛围，如「清冷、萧瑟、空气里像有雪味」",
    "color_tone": "色调，如「冷灰蓝为主，唯一暖色是窗内的油灯光」",
    "space_scale": "空间尺度：large / medium / small",
    "description": "把上面几项合成一段通顺的环境描述",
    "props": ["场景中的关键物件"],
}

_SYS = (
    "你是影视剧组的美术指导与选角导演。你的任务分两步：\n"
    "第一步：判断这部剧的**美术基调**（题材、年代、地域、整体视觉风格）。\n"
    "第二步：为每个角色**设计**一套具体、可照着画、且彼此风格统一的视觉设定。\n\n"
    "关键要求：\n"
    "1. 剧本通常只写情节、不写外貌。**这时候你要根据题材/年代/地域/角色身份合理设计**——\n"
    "   这是美术指导的本职工作，不是编造。例如抗战题材的队长应是 1940 年代中国人的\n"
    "   面孔、发型、粗布军装、绑腿、布鞋，而不是现代潮牌或西方脸。\n"
    "2. 设定必须**与剧本不冲突**：剧本明确写了的信息（如「年轻人」「女护士」「日军军官」）\n"
    "   必须遵守，不得改写成别的性别/年龄/阵营。\n"
    "3. 面部与服装必须**逐槽位填满**（face_shape/skin/eyes/hair/distinctive/height_build、\n"
    "   costume_top/bottom/shoes/accessories）——不允许写成一段笼统的'外貌描述'。\n"
    "   每个槽位都要具体到能照着画，例：face_shape「方脸，颧骨明显，下颌线硬朗」，\n"
    "   而不是「面容刚毅」。\n"
    "4. `costume_*` 必须写清**颜色+材质+款式**"
    "（例：「洗得发白的灰蓝色粗布对襟上衣，黑色绑腿，旧布鞋」）。\n"
    "5. 同一部剧所有角色的风格、年代、色调必须**互相一致**，\n"
    "   且 `signature` 要写成一句话锚点（后续每个镜头都会原样带上它，保证长相不漂移）。\n"
    "6. 只输出一个 JSON 对象，不要解释文字、不要 markdown 代码块。\n"
    "7. 同一人的不同称呼要合并（如「李队长」与「老李」是同一人）。\n"
    "8. 最后必须把面部各槽位合成 `appearance`、服装各槽位合成 `costume` 两个自然语言字段\n"
    "   （内容必须与槽位一致，不得互相矛盾）。"
)

_ART_SYS = (
    "你是影视剧组的美术指导。请阅读剧本，判断整部剧的**美术基调**，只输出 JSON，"
    "不要解释文字、不要 markdown 代码块。"
)


async def detect_art_direction(llm, script: Dict[str, Any],
                                scene_details: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """先判断全剧美术基调（题材/年代/地域/色调），用于让所有角色风格统一。

    这就是 Pavo 的 style_id 思路：先定调，再逐个出资产。
    """
    body = _script_text(script, max_chars=9000, scene_details=scene_details)
    if not body.strip():
        return {}
    user = (
        "请判断这部剧的美术基调，输出 JSON：\n"
        '{"genre": "题材，如 抗战/都市/古装/科幻", '
        '"era": "年代，如 1940年代中国 / 当代 / 未来", '
        '"region": "地域，如 中国北方山区", '
        '"visual_style": "整体视觉风格一句话，含色调与质感", '
        '"style_id": "从 cinematic/anime/chinese_ink/cyberpunk/documentary/3d_render 中选一个最贴切的", '
        '"costume_notes": "主要角色的服装体系概述"}\n\n'
        "【剧本】\n" + body
    )
    data = await _ask_json(llm, _ART_SYS, user)
    if isinstance(data, dict):
        logger.info("美术基调：%s", {k: data.get(k) for k in ("genre", "era", "visual_style", "style_id")})
        return data
    return {}


def _script_text(script: Dict[str, Any], max_chars: int = 12000,
                 scene_details: Optional[List[Dict[str, Any]]] = None) -> str:
    """把剧本压成给 LLM 看的正文。

    ★ 如果已经生成过「剧本细节」（逐秒动作/表情/镜头/光影），优先把它喂进去 ——
    细节里往往含外貌、服装、环境质感的具体描述，角色卡与场景卡因此能做得更细
    （用户要求："剧本细节生成好后，角色+场景的生成会因此更加细腻"）。
    """
    parts: List[str] = []
    title = script.get("title") or ""
    if title:
        parts.append("【标题】" + str(title))
    for k in ("logline", "outline", "synopsis"):
        v = script.get(k)
        if v:
            parts.append("【" + k + "】" + str(v)[:2500])
    scenes = script.get("scenes") or []
    if isinstance(scenes, str):
        try:
            scenes = json.loads(scenes)
        except Exception:
            scenes = []
    for i, sc in enumerate(scenes if isinstance(scenes, list) else [], 1):
        if not isinstance(sc, dict):
            continue
        bits = [f"第 {sc.get('scene_number') or i} 场"]
        for key in ("location", "summary", "description", "content", "action", "dialogue"):
            v = sc.get(key)
            if v:
                bits.append(f"{key}: {v}")
        chars = sc.get("characters")
        if chars:
            bits.append("出场人物: " + ("、".join(map(str, chars)) if isinstance(chars, list) else str(chars)))
        parts.append(" | ".join(bits))
    # ★ 剧本细节（更细的一层，含逐秒动作/表情/镜头）
    for d in (scene_details or []):
        if not isinstance(d, dict):
            continue
        num = d.get("scene_number")
        head = [f"【第 {num} 场 · 细节】"]
        for k in ("title", "location", "summary", "atmosphere", "lighting", "color_tone"):
            v = d.get(k)
            if v:
                head.append(f"{k}: {v}")
        for c in (d.get("characters") or []):
            if isinstance(c, dict):
                nm = c.get("name")
                extra = "，".join(str(c.get(x)) for x in
                                  ("appearance", "costume", "age", "gender", "role") if c.get(x))
                if nm:
                    head.append(f"人物 {nm}: {extra}")
            elif c:
                head.append(f"人物: {c}")
        for t in (d.get("timeline") or [])[:40]:
            if isinstance(t, dict):
                head.append(f"[{t.get('start')}-{t.get('end')}s] {t.get('action') or ''}"
                            + (f"（{t.get('expression')}）" if t.get("expression") else "")
                            + (f" 镜头:{t.get('camera')}" if t.get("camera") else ""))
        parts.append("\n".join(head))

    # 三层提示词里也常含人物/环境描述
    tlp = script.get("three_layer_prompts") or {}
    if isinstance(tlp, dict):
        for k, v in list(tlp.items())[:20]:
            if isinstance(v, dict):
                ov = v.get("layer1_overview")
                if ov:
                    parts.append(f"[{k}] {ov}")
    text = "\n".join(parts)
    return text[:max_chars]


async def _ask_json(llm, system: str, user: str) -> Optional[Any]:
    from core.llm import extract_json
    try:
        raw = await llm.chat([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ])
    except Exception as e:
        logger.warning("资产提取 LLM 调用失败：%s", e)
        return None
    if not raw:
        return None
    data = extract_json(raw)
    if data is None:
        logger.warning("资产提取 JSON 解析失败，原文前 300 字：%s", str(raw)[:300])
    return data


def _clean_list(v) -> List[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [x.strip() for x in v.replace("，", ",").split(",") if x.strip()]
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [str(v)]


_FACE_SLOTS = ("face_shape", "skin", "eyes", "hair", "distinctive", "height_build")
_COSTUME_SLOTS = ("costume_top", "costume_bottom", "costume_shoes")


def _join_slots(it: Dict[str, Any], keys, lead: str = "") -> str:
    """把若干槽位拼成一段通顺的话（缺的跳过）。"""
    bits = []
    for k in keys:
        v = it.get(k)
        if isinstance(v, (list, tuple)):
            v = "、".join(str(x) for x in v if str(x).strip())
        v = str(v or "").strip()
        if v and v not in ("无", "none", "None", "未知", "unknown"):
            bits.append(v)
    if not bits:
        return ""
    return (lead + "：" if lead else "") + "，".join(bits) + "。"


def _character_slots(it: Dict[str, Any]) -> Dict[str, Any]:
    """把 LLM 返回的角色条目整理成"固定槽位"字典（存进 reference_features）。

    下游（出图提示词 / 视频提示词）直接读这些槽位，不再依赖 LLM 每次重新描述 ——
    这样同一个角色在第 1 个镜头和第 20 个镜头拿到的外貌描述**逐字相同**。
    """
    slots: Dict[str, Any] = {}
    for k in _FACE_SLOTS + _COSTUME_SLOTS:
        v = str(it.get(k) or "").strip()
        if v:
            slots[k] = v
    acc = _clean_list(it.get("costume_accessories"))
    if acc:
        slots["costume_accessories"] = acc
    pal = _clean_list(it.get("costume_palette"))
    if pal:
        slots["costume_palette"] = pal
    for k in ("signature", "prop_detail"):
        v = str(it.get(k) or "").strip()
        if v:
            slots[k] = v
    return slots


async def extract_characters(llm, script: Dict[str, Any],
                             art: Optional[Dict[str, Any]] = None,
                             scene_details: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """LLM 结构化提取 + 设计角色。失败返回空列表（调用方回落朴素提取）"""
    body = _script_text(script, scene_details=scene_details)
    if not body.strip():
        return []
    art_ctx = ""
    if art:
        art_ctx = ("【本剧美术基调（所有角色必须与之一致）】\n"
                   f"题材：{art.get('genre', '')}\n年代：{art.get('era', '')}\n"
                   f"地域：{art.get('region', '')}\n视觉风格：{art.get('visual_style', '')}\n"
                   f"服装体系：{art.get('costume_notes', '')}\n\n")
    user = (
        "请为下面剧本里的**全部角色**输出 JSON。外貌与服装**必须逐槽位填满**，"
        "每个槽位都要具体到能照着画出同一个人：\n"
        '{"characters": [' + json.dumps(CHARACTER_SCHEMA, ensure_ascii=False) + "]}\n\n"
        + art_ctx +
        "【剧本】\n" + body
    )
    data = await _ask_json(llm, _SYS, user)
    if not data:
        return []
    items = data.get("characters") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        if not name:
            continue
        slots = _character_slots(it)
        # description/costume_main 优先用槽位合成（保证与槽位一致），
        # LLM 没填槽位时才退回它自己写的整段文本
        face = _join_slots(it, _FACE_SLOTS) or str(it.get("appearance") or "").strip()
        costume = _join_slots(it, _COSTUME_SLOTS) or str(it.get("costume") or "").strip()
        acc = slots.get("costume_accessories") or []
        if acc:
            costume = (costume.rstrip("。") + "；配饰：" + "、".join(acc) + "。") if costume \
                else "配饰：" + "、".join(acc) + "。"
        props = _clean_list(it.get("props"))
        if not props and slots.get("prop_detail"):
            props = [slots["prop_detail"]]
        out.append({
            "name": name,
            "aliases": _clean_list(it.get("aliases")),
            "role": str(it.get("role") or "supporting").strip(),
            "gender": str(it.get("gender") or "unknown").strip(),
            "age": str(it.get("age") or "").strip(),
            "description": face,
            "costume_main": costume,
            "personality": str(it.get("personality") or "").strip(),
            "props": props,
            "relationships": str(it.get("relationships") or "").strip(),
            "first_appearance": it.get("first_appearance"),
            # ★ 固定槽位原样保留：出图/出视频提示词直接读它
            "slots": slots,
        })
    logger.info("LLM 提取角色 %d 个（含固定槽位 %d 项）",
                len(out), sum(len(c.get("slots") or {}) for c in out))
    return out


_SCENE_SYS = (
    "你是影视剧组的美术指导。请从剧本里提取场景，规则：\n"
    "1. **严格以剧本写到的地点为准**：剧本里出现过的地点才提取，"
    "**严禁凭空新增剧本里没有的地点**；不要把同一地点拆成多个不同名字。\n"
    "2. `description` 只能写剧本**确实写到**的环境内容（地形、建筑、陈设、植被、"
    "天气、氛围）。剧本没写的细节**留空**，不要脑补剧情或道具。\n"
    "3. 允许补充的只有**美术执行层面**的信息：光线、色调、时间、天气"
    "（用于出图，属于美术基调范畴，不得与剧本冲突）。\n"
    "4. 若剧本给出了年代/地域/题材，环境质感要与之一致（例：1940 年代中国北方山区，"
    "不应出现现代建筑、电线杆、水泥路）。\n"
    "5. 只输出一个 JSON 对象，不要解释文字、不要 markdown 代码块。"
)


async def extract_scenes(llm, script: Dict[str, Any],
                         art: Optional[Dict[str, Any]] = None,
                         scene_details: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    body = _script_text(script, scene_details=scene_details)
    if not body.strip():
        return []
    art_ctx = ""
    if art:
        art_ctx = ("【本剧美术基调（环境质感必须与之一致）】\n"
                   f"题材：{art.get('genre', '')}\n年代：{art.get('era', '')}\n"
                   f"地域：{art.get('region', '')}\n视觉风格：{art.get('visual_style', '')}\n\n")
    user = (
        "请提取下面剧本里的**全部场景/地点**（只提取剧本确实写到的地方），输出 JSON：\n"
        '{"scenes": [' + json.dumps(SCENE_SCHEMA, ensure_ascii=False) + "]}\n\n"
        + art_ctx +
        "【剧本】\n" + body
    )
    data = await _ask_json(llm, _SCENE_SYS, user)
    if not data:
        return []
    items = data.get("scenes") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        if not name:
            continue
        out.append({
            "name": name,
            "location_type": str(it.get("location_type") or "outdoor").strip(),
            "time_of_day": str(it.get("time_of_day") or "day").strip(),
            "weather": str(it.get("weather") or "clear").strip(),
            "lighting": str(it.get("lighting") or "natural").strip(),
            "space_scale": str(it.get("space_scale") or "medium").strip(),
            "description": (_join_slots(
                it, ("terrain", "architecture", "furnishings", "atmosphere", "color_tone"))
                or str(it.get("description") or "").strip()),
            "props": _clean_list(it.get("props")),
            "slots": {k: str(it.get(k) or "").strip()
                      for k in ("terrain", "architecture", "furnishings",
                                "atmosphere", "color_tone")
                      if str(it.get(k) or "").strip()},
        })
    logger.info("LLM 提取场景 %d 个", len(out))
    return out


# ═══════════════════════════════════════════════════════════════
# 提示词构造：把结构化人物卡拼成**能照着画**的出图提示词
# ═══════════════════════════════════════════════════════════════

_GENDER_ZH = {"male": "男性", "female": "女性", "unknown": ""}
_ROLE_ZH = {"protagonist": "主角", "antagonist": "反派",
            "supporting": "配角", "minor": "次要角色"}

STYLE_PRESETS = {
    "cinematic": "电影级写实风格，胶片质感，自然光影层次，浅景深，高细节，4K",
    "anime": "日式动画风格，干净线条，鲜明色彩，动画角色设定图",
    "chinese_ink": "中国水墨风格，写意留白，宣纸质感",
    "cyberpunk": "赛博朋克风格，霓虹光效，冷色调，未来都市质感",
    "documentary": "纪实摄影风格，自然光，真实质感，无明显修饰",
    "3d_render": "3D 渲染风格，次世代游戏角色质感，PBR 材质",
}


_FACE_LABEL = {
    "face_shape": "脸型", "skin": "肤色肤质", "eyes": "眼睛",
    "hair": "发型", "distinctive": "辨识特征", "height_build": "体型",
}
_COSTUME_LABEL = {
    "costume_top": "上装", "costume_bottom": "下装", "costume_shoes": "鞋",
}


def _slots_of(char: Dict[str, Any]) -> Dict[str, Any]:
    """取出角色卡里的固定槽位（可能被存成 JSON 字符串）。"""
    rf = char.get("reference_features")
    if isinstance(rf, str):
        try:
            rf = json.loads(rf)
        except Exception:
            rf = {}
    if not isinstance(rf, dict):
        return {}
    sl = rf.get("slots")
    return sl if isinstance(sl, dict) else {}


def build_portrait_prompt(char: Dict[str, Any], style: str = "cinematic",
                          view: str = "bust") -> str:
    """把角色卡拼成一条高质量「角色设定图」提示词。

    ★ 优先用**固定槽位**逐项写清楚：同一个角色无论在哪个镜头、
    哪一次生成，拿到的是逐字相同的面部/服装描述 —— 这是跨镜头一致性的基础。
    没有槽位（旧数据）才退回 description/costume_main 那两段自由文本。

    view: bust（半身像）/ full（全身）/ fourview（四视图，用于跨镜头一致性）
    """
    name = char.get("name") or "角色"
    bits: List[str] = []

    gender = _GENDER_ZH.get(str(char.get("gender") or "").lower(), "")
    age = str(char.get("age") or "").strip()
    role = _ROLE_ZH.get(str(char.get("role") or "").lower(), "")
    head = "，".join(x for x in (age, gender, role) if x)
    bits.append(f"《{name}》角色设定图" + (f"（{head}）" if head else ""))

    slots = _slots_of(char)
    face_bits = [f"{_FACE_LABEL[k]}：{slots[k]}" for k in _FACE_LABEL if slots.get(k)]
    costume_bits = [f"{_COSTUME_LABEL[k]}：{slots[k]}" for k in _COSTUME_LABEL if slots.get(k)]
    acc = slots.get("costume_accessories")
    if isinstance(acc, str):
        acc = [acc]
    if acc:
        costume_bits.append("配饰：" + "、".join(map(str, acc[:4])))
    pal = slots.get("costume_palette")
    if isinstance(pal, str):
        pal = [pal]
    if pal:
        costume_bits.append("主色：" + "、".join(map(str, pal[:4])))

    if face_bits:
        bits.append("；".join(face_bits))
    desc = str(char.get("description") or "").strip()
    if desc and not face_bits:
        bits.append("外貌：" + desc)

    if costume_bits:
        bits.append("；".join(costume_bits))
    costume = str(char.get("costume_main") or "").strip()
    if costume and not costume_bits:
        bits.append("服装：" + costume)

    sig = str(slots.get("signature") or "").strip()
    if sig:
        bits.append("识别锚点（各镜头必须保持不变）：" + sig)

    alt = str(char.get("costume_alternate") or "").strip()
    if alt:
        bits.append("备用服装：" + alt)

    pers = str(char.get("personality") or "").strip()
    if pers:
        bits.append("气质：" + pers)

    props = char.get("props")
    if props:
        if isinstance(props, str):
            try:
                props = json.loads(props)
            except Exception:
                props = [props]
        if isinstance(props, list) and props:
            bits.append("随身道具：" + "、".join(map(str, props[:4])))

    view_map = {
        "bust": "正面半身像，单人，居中构图，简洁纯色背景",
        "full": "正面全身像，单人，站姿，简洁纯色背景",
        "fourview": "角色四视图设定稿（正面 / 侧面 / 背面 / 面部特写并排），"
                    "同一人物同一服装，比例一致，白色背景",
    }
    bits.append(view_map.get(view, view_map["bust"]))
    bits.append(STYLE_PRESETS.get(style, STYLE_PRESETS["cinematic"]))
    bits.append("画面中不要出现任何文字、水印、logo")

    return "。".join(b for b in bits if b)


def build_scene_prompt(scene: Dict[str, Any], style: str = "cinematic") -> str:
    name = scene.get("name") or "场景"
    bits = [f"《{name}》场景概念图"]
    if scene.get("description"):
        bits.append("环境：" + str(scene["description"]))
    meta = []
    tod = {"day": "白天", "night": "夜晚", "dawn": "黎明", "dusk": "黄昏", "noon": "正午"}
    lt = {"natural": "自然光", "golden_hour": "黄金时刻", "blue_hour": "蓝调时刻",
          "night": "夜景灯光", "neon": "霓虹光", "studio": "棚拍布光",
          "candle": "烛光", "overcast": "阴天漫射光"}
    we = {"clear": "晴", "rainy": "雨", "snowy": "雪", "cloudy": "多云", "foggy": "雾"}
    for k, m in (("time_of_day", tod), ("lighting", lt), ("weather", we)):
        v = str(scene.get(k) or "").lower()
        if v in m:
            meta.append(m[v])
    if meta:
        bits.append("时间与光线：" + "、".join(meta))
    props = scene.get("props")
    if props and isinstance(props, list):
        bits.append("关键物件：" + "、".join(map(str, props[:5])))
    bits.append("广角电影感构图，无人物，高清写实，画面干净")
    bits.append(STYLE_PRESETS.get(style, STYLE_PRESETS["cinematic"]))
    bits.append("画面中不要出现任何文字、水印、logo")
    return "。".join(b for b in bits if b)
