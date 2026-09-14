# -*- coding: utf-8 -*-
"""
VideoForge · 视频生成提示词构造器

═══════════════════════════════════════════════════════════════════
为什么重写（用户原话："分镜的生成不是念稿子，那样真的侮辱和浪费了视频模型"）
═══════════════════════════════════════════════════════════════════
旧实现把三层提示词**原样拼**给视频模型：

    在一片紧张的山林小道上…（第1层概述）
    分镜时间线：
    [0-3秒] 李队长蹲在草丛中 | 表情:坚毅且冷静 | 镜头:static | 道具:大刀
    [3-7秒] … | 表情:紧张 | 镜头:handheld | 道具:
    约束条件：
    不要出现: 现代化装备、汽车、飞机
    必须保持: 服装一致

这是**给人读的分镜表**，不是给视频模型的提示词。视频模型需要的是
**一个连续镜头的电影化描述**：景别 + 运镜 + 主体 + 动作 + 环境 + 光线 + 风格。
把分镜表、表情字段、约束清单一股脑塞进去，只会让模型：

- 理解成"多段拼接"→ 画面跳切、动作不连贯
- 把"表情:坚毅"这种键值当画面文字 → 出奇怪的构图
- 被"不要出现 XXX"干扰 → 反而更容易生成 XXX（负面提示应放 negative_prompt）

本模块按**真实视频模型的输入习惯**重建提示词，并且：
- **严格以角色卡 + 场景卡为准**（用户要求："不要 AI 自定义，角色场景不要瞎换"）
- 把角色**固定的外形与服装**写进每个镜头 → 跨镜头同一张脸
- 负面约束走 `negative_prompt` 通道，不混进正文
- 按厂商追加参数（火山方舟用文末 `--ratio --resolution --duration`）
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("videoforge.videoprompt")

SHOT_SIZES = {
    "extreme_wide": "大远景", "wide": "远景", "full": "全景", "long": "全景",
    "medium": "中景", "medium_close": "中近景", "close": "特写", "close_up": "特写",
    "extreme_close": "大特写", "over_shoulder": "过肩镜头", "pov": "主观视角",
}
CAMERA_MOVES = {
    "static": "固定机位", "still": "固定机位", "fixed": "固定机位",
    "pan": "横摇", "pan_left": "向左横摇", "pan_right": "向右横摇",
    "tilt": "俯仰摇", "tilt_up": "上摇", "tilt_down": "下摇",
    "dolly": "推轨", "dolly_in": "缓慢推近", "dolly_out": "缓慢拉远",
    "push": "推镜", "pull": "拉镜", "zoom_in": "变焦推近", "zoom_out": "变焦拉远",
    "track": "跟拍", "tracking": "跟拍", "follow": "跟拍",
    "handheld": "手持", "crane": "摇臂升降", "drone": "航拍",
    "arc": "环绕运镜", "orbit": "环绕运镜", "steadicam": "斯坦尼康稳定跟拍",
}
LIGHTING = {
    "natural": "自然光", "golden_hour": "黄金时刻暖光", "blue_hour": "蓝调时刻冷光",
    "night": "夜色灯光", "neon": "霓虹光", "studio": "专业布光",
    "candle": "烛光", "overcast": "阴天漫射光",
}
TIMEOFDAY = {"day": "白天", "night": "夜晚", "dawn": "黎明", "dusk": "黄昏", "noon": "正午"}
WEATHER = {"clear": "晴", "rainy": "雨", "snowy": "雪", "cloudy": "多云", "foggy": "雾"}
STYLE = {
    "cinematic": "电影级写实，胶片质感，自然光影层次，浅景深，高细节",
    "anime": "日式动画风格，干净线条，鲜明色彩",
    "chinese_ink": "中国水墨写意风格",
    "cyberpunk": "赛博朋克风格，霓虹光效，冷色调",
    "documentary": "纪实摄影风格，自然光，真实质感",
    "3d_render": "3D 渲染，次世代游戏质感，PBR 材质",
}

# 视频模型通用画质词（写进正文末尾，避免堆在最前面干扰主体描述）
QUALITY = "电影级画质，清晰锐利，运动自然流畅，无画面畸变"

_TIME_RE = re.compile(r"\[\s*\d+(\.\d+)?\s*[-–~]\s*\d+(\.\d+)?\s*s?\s*\]")
_META_RE = re.compile(r"(表情|镜头|道具|服装|景别|运镜|音效|台词)\s*[:：]")
_WS_RE = re.compile(r"\s+")
# 单个角色签名（外形+服装+识别点）的**上限**。见 `_character_signature` 的说明：
# 它是在源头防止"长签名把契约尾段挤出预算"的第一道闸；
# `_fit_to_budget` 是第二道闸（削可选段），最后才允许硬截并如实上报。
SIG_MAX_CHARS = 260

# ── 运镜**受控词汇**（单一事实来源）────────────────────────────
#   为什么要把词表提到这里、并让下面所有正则都从它派生：
#   2026-09-13 真实事故 —— 这个文件里曾经有**两套词表**：
#     `_CAM_CLAUSE_RE`/`_CAM_WORD_RE` 认「横摇/摇摄」但**不认「平移/横移」**，
#     而 `_norm_camera` 认。于是「镜头缓慢从建筑右侧平移至正门」这句
#     **剪不掉**，原样留在"画面内容"里，和"镜头运动"小节同时描述运镜 ——
#     模型真的会去叠加运动，就是用户说的"太扯"。
#     实测 63 个真实分镜里 9 个中招（全在 `19636f68`）。
#   现在：词表只有一份，正则从它派生，改一处即全生效。
#   ★ 长词必须排在短词前面（"跟随拍摄" 先于 "跟随"）。
_CN_CAMERA_MOVES = [
    ("跟随拍摄", "跟拍"), ("斯坦尼康", "斯坦尼康稳定跟拍"),
    ("手持跟拍", "手持"),
    ("缓慢推近", "缓慢推近"), ("缓慢拉远", "缓慢拉远"),
    ("固定机位", "固定机位"), ("锁定机位", "固定机位"),
    ("摇臂升降", "摇臂升降"), ("俯仰摇", "俯仰摇"),
    ("跟拍", "跟拍"), ("跟随", "跟拍"), ("追踪", "跟拍"),
    ("环绕", "环绕运镜"), ("旋转", "环绕运镜"),
    ("快速推进", "推镜"), ("推进", "推镜"), ("推近", "缓慢推近"), ("推镜", "推镜"),
    ("快速拉远", "拉镜"), ("拉远", "拉远"), ("拉出", "拉远"), ("拉镜", "拉镜"),
    ("横摇", "横摇"), ("摇摄", "横摇"), ("平移", "横移"), ("横移", "横移"),
    ("上摇", "上摇"), ("下摇", "下摇"), ("升降", "摇臂升降"), ("摇臂", "摇臂升降"),
    ("航拍", "航拍"), ("俯拍", "俯拍"), ("仰拍", "仰拍"), ("变焦", "变焦"),
    ("手持", "手持"), ("晃动", "手持"),
    ("固定", "固定机位"), ("锁定", "固定机位"), ("静止", "固定机位"),
]
#   英文名（模型提示词里也会出现），与中文词一起进"识别运镜"的正则
_EN_CAMERA_WORDS = ("zoom_in", "zoom_out", "dolly_in", "dolly_out", "zoom", "track",
                    "follow", "orbit", "pan", "tilt", "crane", "dolly", "push",
                    "pull", "arc", "handheld", "steadicam")
#   **视角词**：不是"运动"，但出现在「镜头从高处俯视街道」这种句子里时，
#   它同样是**摄影机指令**而不是画面内容，必须一起剪掉
#   （实测 `19636f68` 的「镜头从高处俯视街道，展示行人穿梭的繁忙景象」）。
#   它们**不**参与"主运镜"的判定（不产生运动），所以单独一张表。
_CAM_VIEW_WORDS = ("俯视", "俯瞰", "仰视", "平视", "鸟瞰")
_CAM_TOKEN_ALT = "|".join([t for t, _ in _CN_CAMERA_MOVES]
                          + list(_EN_CAMERA_WORDS) + list(_CAM_VIEW_WORDS))
#   单字"推/拉/移"只有在**极短**的从句里才认（否则"拉手""推进剂"都会被当成运镜）
_BARE_MOVE_RE = re.compile(r"^[^，,。；;]{0,3}(推|拉|移)[^，,。；;]{0,2}$")

# ★★ 动作文本里**混着运镜描述**，必须剥掉。
#   真实分镜长这样：「布兰坐在轮椅上，缓缓进入铁王座大厅，**镜头从背后跟随**，展示他沉静的姿态。」
#   "镜头从背后跟随"是**摄影机指令**，不是画面内容。把它留在"画面内容"里会有两个后果：
#     ① 审计器会看到两个运镜（"画面内容"里一个、镜头段里一个）→ 误判 `P-CAMERA-CONFLICT`；
#     ② 更重要的是，提示词里同时出现两处运镜描述，模型真会去叠加运动。
#   所以：把这类从句**从动作里剪掉**，并把里面提到的运镜**提取出来**参与运镜决策。
_CAM_CLAUSE_RE = re.compile(
    r"[，,。；;]?\s*(?:镜头|摄影机|camera)\s*(?:从|由|在)?[^，,。；;]{0,24}?"
    r"(?:" + _CAM_TOKEN_ALT + r")"
    r"[^，,。；;]{0,16}")
_CAM_WORD_RE = re.compile("(" + _CAM_TOKEN_ALT + ")", re.I)


def _strip_camera_clauses(text: str) -> Tuple[str, List[str]]:
    """把动作文本里的运镜从句剪掉，并返回其中提到的运镜词。"""
    if not text:
        return "", []
    found = _CAM_WORD_RE.findall(text)
    t = _CAM_CLAUSE_RE.sub("", text)
    # 只剩"镜头"两个字或空壳的片段一并清掉
    t = re.sub(r"[，,。；;]?\s*(?:镜头|摄影机)\s*(?:保持|不变|稳定)?\s*(?=[，,。；;]|$)", "", t)
    t = re.sub(r"[,，]\s*[,，]+", "，", t)
    t = re.sub(r"\s+", " ", t).strip(" ，,。;；")
    return t, found


def _t(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        return " ".join(_t(x) for x in v.values() if x)
    if isinstance(v, (list, tuple)):
        return " ".join(_t(x) for x in v)
    return str(v).strip()


def _clean_action(text: Any) -> str:
    """把分镜表里的一句动作清成可以直接进提示词的自然语言"""
    t = _t(text)
    t = _TIME_RE.sub("", t)
    t = re.sub(r"^\s*[-•·\d]+[\.、)]?\s*", "", t)
    t = t.replace("|", "，").replace("｜", "，")
    # 去掉 "表情:xxx" 这类键值对残留
    t = _META_RE.sub("", t)
    t = re.sub(r"[,，]\s*[,，]+", "，", t)
    t = _WS_RE.sub(" ", t).strip(" ，,。;；")
    return t


# ── 中文运镜词 → 规范名 ──────────────────────────────────────────
#   词表本身已提到文件顶部（单一事实来源，见 `_CN_CAMERA_MOVES` 上方的说明）。
#   这里只说明**取值策略**：先按分隔符切从句，每个从句里按词表顺序取
#   最长/最具体的那个词，只认第一个从句，后面的运镜一律丢弃
#   （由 `camera_moves_seen` 如实上报，不静默）。


def _cn_camera_of(clause: str) -> str:
    """从一个从句里取出**唯一一个**运镜并规整成规范名；取不到就空。"""
    for tok, canon in _CN_CAMERA_MOVES:
        if tok in clause:
            return canon
    m = _BARE_MOVE_RE.match(clause.strip())
    if m:
        return {"推": "推镜", "拉": "拉远", "移": "横移"}[m.group(1)]
    return ""


def _norm_camera(v: Any) -> str:
    s = _t(v)
    if not s:
        return ""
    key = s.lower().replace(" ", "_")
    if key in CAMERA_MOVES:
        return CAMERA_MOVES[key]
    for k, zh in CAMERA_MOVES.items():
        if len(k) > 3 and k in key:        # 短键（arc/pan）别做子串匹配，太容易误伤
            return zh

    # ★★ 分镜的 `camera` 字段里经常一口气写**两个运镜**：
    #    「低角度跟随拍摄，镜头快速推进」/「手持跟拍，快速推近」。
    #    旧代码把整串当成"一个运镜"写进提示词，于是提示词里实际有两个主运动，
    #    审计器判 `P-CAMERA-CONFLICT`（实测 63 个真实分镜里有 2 个中招，
    #    它们正是用户看到的"太扯"的那几镜）。
    #    方法论（`camera-editing-language.md`）：一个镜头只有一个主运动，
    #    想要两个就**拆镜头**。所以这里按分隔符切成从句，
    #    只取**第一个**含运镜的从句，并规整成受控词汇里的规范名。
    #
    #    ★ 为什么连「低角度」这种限定词也一起丢掉：
    #      试过保留限定词（`低角度` + `跟拍`），但限定词本身可能就含第二个运镜词
    #      （"平稳横移镜头"既是限定词又是运镜）→ 绕回去又变成两个主运动。
    #      角度信息由"起始构图/景别"承担，运镜段只负责**一件事**。
    for p in [x.strip() for x in re.split(r"[，,、；;/|]+", s) if x.strip()]:
        z = _cn_camera_of(p)
        if z:
            return z
    return _cn_camera_of(s)


def _norm_shot_size(v: Any) -> str:
    s = _t(v).lower().replace(" ", "_")
    if not s:
        return ""
    if s in SHOT_SIZES:
        return SHOT_SIZES[s]
    for k, zh in SHOT_SIZES.items():
        if k in s:
            return zh
    if re.search(r"[\u4e00-\u9fff]", s):
        return _t(v)
    return ""


def _timeline_of(shot: Optional[Dict[str, Any]]) -> List[dict]:
    if not shot:
        return []
    tl = shot.get("layer2_timeline")
    if isinstance(tl, str):
        try:
            tl = json.loads(tl)
        except Exception:
            tl = []
    return [t for t in (tl or []) if isinstance(t, dict)]


def _first_action(shot: Optional[Dict[str, Any]]) -> str:
    tl = _timeline_of(shot)
    if tl:
        return _clean_action(tl[0].get("action") or tl[0].get("description") or "")
    return _clean_action((shot or {}).get("layer1_overview") or "")


def _last_action(shot: Optional[Dict[str, Any]]) -> str:
    tl = _timeline_of(shot)
    if tl:
        return _clean_action(tl[-1].get("action") or tl[-1].get("description") or "")
    return _clean_action((shot or {}).get("layer1_overview") or "")


def character_signature(char: Dict[str, Any]) -> str:
    """把角色卡压成一句**固定不变**的外形签名，用于跨镜头保持同一张脸。

    ★ 优先读**固定槽位**（脸型/肤色/眼睛/发型/体型/上装/下装/鞋/配饰/识别锚点）：
      槽位是逐字复用的，所以第 1 个镜头和第 20 个镜头拿到的是同一份描述，
      角色长相不会漂移。没有槽位（旧数据）才退回 description/costume_main。
    """
    bits: List[str] = []
    name = _t(char.get("name"))
    age = _t(char.get("age"))
    slots = _slots_of(char)

    face = "，".join(slots[k] for k in
                    ("face_shape", "skin", "eyes", "hair", "height_build", "distinctive")
                    if slots.get(k))
    costume = "，".join(slots[k] for k in
                       ("costume_top", "costume_bottom", "costume_shoes")
                       if slots.get(k))
    acc = slots.get("costume_accessories")
    if isinstance(acc, str):
        acc = [acc]
    if acc:
        costume = (costume + "，" if costume else "") + "配饰：" + "、".join(map(str, acc[:4]))

    # 没有槽位才退回自由文本
    if not face:
        face = _t(char.get("description"))
    if not costume:
        costume = _t(char.get("costume_main"))

    if name and (face or costume):
        head = f"{name}（{age}）" if age else name
        parts = [p for p in (face, costume) if p]
        bits.append(f"{head}：{'，'.join(parts)}")
    elif name:
        bits.append(name)

    sig = _t(slots.get("signature"))
    if sig:
        bits.append(f"{name or '该角色'}的固定识别点：{sig}")
    out = "；".join(bits)
    # ★ 单个角色的签名要**限长**。实测 `caa9904f` 的布兰·史塔克一张卡
    #   拼出 700+ 字（外形槽位是逐条英文长句），两个角色就把整条提示词
    #   顶到 1500 字上限，于是**末尾的"总时长/音频边界"被砍掉** ——
    #   平台看不到音频边界就会自动配乐加旁白（`F-AUDIO-POLLUTION`）。
    #   在**源头**限长，比在拼装末尾削更安全：这里砍掉的是同一条描述里的
    #   次要尾部（体型/配饰在后），而"总时长/音频/镜头运动"永远不会被砍。
    if len(out) > SIG_MAX_CHARS:
        out = out[:SIG_MAX_CHARS].rstrip("，,；;、 ") + "…"
    return out


def _slots_of(char: Dict[str, Any]) -> Dict[str, Any]:
    """取出角色卡里的固定槽位（DB 里存成 JSON 字符串）。"""
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


# 场景槽位的中文名（写进提示词时用，让模型看得懂这一项是什么信息）
SCENE_SLOT_ZH = {
    "terrain": "地形地面",
    "architecture": "建筑构筑",
    "furnishings": "陈设物件",
    "atmosphere": "氛围",
    "color_tone": "色调",
}


def _fit_to_budget(seg: List[str], max_chars: int) -> Tuple[str, bool]:
    """把各段拼进 `max_chars` 预算里，返回 (提示词, 是否只能硬截)。

    ★★ 为什么不能直接 `"".join(seg)[:max_chars]`：
      那是**从尾巴上砍**，而契约里"总时长"和"音频边界"恰好排在最后。
      实测 `caa9904f` 第 1 镜（20 秒长镜、两个角色的外形签名很长）被砍成
      「…这段状态上，主体仍」——**整段"总时长：20 秒。音频：…"没了**。
      平台看不到音频边界就会**自动配乐、加旁白**，正是方法论里的
      `F-AUDIO-POLLUTION`；而这条提示词在体检里是"合格"的
      （因为体检当时根本没看到末尾）。这个缺陷是 2026-09-13
      把审计口径改成"要发出去的那一条"之后才暴露出来的。

    削的**顺序**按"每字价值"排：对接、人物签名、环境、（最后）画面内容；
    "主体数量 / 景别 / 镜头运动 / 结束构图 / 总时长 / 音频"**永远不削**。
    实在削不动才硬截，并且**如实返回 True**，让上层能告警。
    """
    parts = [s for s in seg if s]

    def _joined() -> str:
        return _WS_RE.sub(" ", "。".join(parts))

    text = _joined()
    if len(text) <= max_chars:
        return text, False
    for pref, keep in (
        ("为下一镜", 0),        # 与下一镜的衔接是最可以牺牲的（下一镜自己会承接）
        ("承接上一镜", 90),
        ("人物（", 170),        # 外形签名很长，但**不能整段删**（删了会换脸）
        ("环境：", 110),
        ("画面内容：", 220),    # 核心内容，放到最后才削
    ):
        idx = next((i for i, s in enumerate(parts) if s.startswith(pref)), -1)
        if idx < 0:
            continue
        if keep <= 0:
            parts.pop(idx)
        else:
            parts[idx] = parts[idx][:keep]
        text = _joined()
        if len(text) <= max_chars:
            return text, False
    return text[:max_chars], True


def build_video_prompt(
    shot: Dict[str, Any],
    *,
    scene: Optional[Dict[str, Any]] = None,
    characters: Optional[List[Dict[str, Any]]] = None,
    art: Optional[Dict[str, Any]] = None,
    style: str = "cinematic",
    provider: str = "",
    duration: int = 5,
    aspect_ratio: str = "16:9",
    resolution: str = "720p",
    max_chars: int = 1500,
    prev_shot: Optional[Dict[str, Any]] = None,
    next_shot: Optional[Dict[str, Any]] = None,
    layers: Optional[Dict[str, bool]] = None,
    ref_scope: bool = False,
) -> Dict[str, Any]:
    """构造视频模型提示词。

    `layers` 支持**按层开关**（用户要求"每个分镜的每一层可独立勾选"）：
      {"l1": bool, "l2": bool, "l3": bool}
      关掉的层不进提示词 —— 便于"只要动作时间线、不要概述"这类精细控制。

    返回 {"prompt", "negative_prompt", "meta"}。
    `prompt` 是一段**连续镜头的电影化描述**（不是分镜表）。
    """
    timeline = shot.get("layer2_timeline")
    if isinstance(timeline, str):
        try:
            timeline = json.loads(timeline)
        except Exception:
            timeline = []
    timeline = timeline if isinstance(timeline, list) else []

    # ── 1. 主体动作：取时间线上信息量最大的一条，而不是把整条时间线倒进去 ──
    #     层开关：l2=第2层（逐秒动作/镜头），l1=第1层（场面概述）
    use_l1 = True if not layers else bool(layers.get("l1", True))
    use_l2 = True if not layers else bool(layers.get("l2", True))
    use_l3 = True if not layers else bool(layers.get("l3", True))
    if not use_l2:
        timeline = []
    action = ""
    cameras: List[str] = []
    sizes: List[str] = []
    if timeline:
        scored = []
        for it in timeline:
            if isinstance(it, dict):
                a = _clean_action(it.get("action") or it.get("description") or "")
                # ★ 记下时间戳：必须**按时间顺序**讲事情
                try:
                    st = float(it.get("start") or 0)
                except (TypeError, ValueError):
                    st = 0.0
                scored.append((st, a))
                c = _norm_camera(it.get("camera") or it.get("camera_move") or "")
                if c:
                    cameras.append(c)
                z = _norm_shot_size(it.get("shot_size") or it.get("shot") or it.get("景别") or "")
                if z:
                    sizes.append(z)
            elif isinstance(it, str):
                scored.append((float(len(scored)), _clean_action(it)))
        if scored:
            # ══════════════════════════════════════════════════════
            # ★ 这里曾经是一个**真错**：旧代码 `scored.sort(reverse=True)`
            #   按**文字长度**倒序取最长的两条，再用"随后"拼起来 ——
            #   结果是把后面发生的事讲到了前面，而且中间的动作全丢。
            #   例：12 秒里 4 个动作，取到的是第 3、4 个，
            #   写出来变成"（9-12s 的事），随后（6-9s 的事）"，
            #   模型拿到一段**倒叙**的指令，画面自然跟描述对不上。
            #   现在：严格按时间排序，保留**发生顺序**。
            # ══════════════════════════════════════════════════════
            scored.sort(key=lambda x: x[0])
            acts = [a for _, a in scored if a]
            if len(acts) == 1:
                action = acts[0]
            elif acts:
                # 时间太长就压缩，但**始终保持先后次序**：
                # 保留第一个 + 最后一个（首尾最能说明"这一段演什么"），
                # 中间若有更重要的（更长）动作，替换掉第 2 条之前的位置不动。
                #
                # ★ 拼接前先把每条末尾的句号/分号去掉。
                #   分镜里的 action 是带句号的整句，直接拼会写出
                #   "…展示他沉静的姿态。，随后无面者站在…" 这种
                #   "。，随后" 的双重标点。模型看到这种标点会读到断句混乱，
                #   而提示词的标点质量直接影响生成质量。
                def _trim_end(t: str) -> str:
                    return re.sub(r"[。．.；;，,、\s]+$", "", (t or "").strip())

                first, last = _trim_end(acts[0]), _trim_end(acts[-1])
                mid = [_trim_end(x) for x in acts[1:-1] if _trim_end(x)]
                if mid and len("，随后".join([first, last])) < 60:
                    # 还有余量就把中间最详细的一条插在中间，顺序不变
                    pick = max(mid, key=len)
                    action = f"{first}，随后{pick}，最后{last}"
                elif first and last and first != last:
                    action = f"{first}，随后{last}"
                else:
                    action = first or last
    if not action and use_l1:
        action = _clean_action(shot.get("layer1_overview") or "")

    # ★ 把动作里混着的运镜从句剪掉（见 `_strip_camera_clauses` 的说明），
    #   并把里面提到的运镜并入候选 —— 剧本想要的运镜不该被丢掉，
    #   但也不该以"画面内容"的形式留在提示词里。
    action, _cam_in_action = _strip_camera_clauses(action)

    # ── 2. 镜头语言 ──
    #    运镜候选 = 时间线显式给的 + 动作文本里提到的（后者刚从动作里剪出来）。
    #    ★ 只输出**一个**主运镜：契约要求"一个镜头只有一个主摄影机运动"。
    for _w in (_cam_in_action or []):
        _z = _norm_camera(_w)
        if _z:
            cameras.append(_z)
    cam = cameras[0] if cameras else _norm_camera(shot.get("camera") or "") or "固定机位"
    size = sizes[0] if sizes else _norm_shot_size(shot.get("shot_size") or "") or ""
    # 用整条时间线里出现过的运镜做参考，但只输出一个主运镜，避免"多段拼接"感
    if len(set(cameras)) > 1 and not sizes:
        cam = cameras[0]

    # ── 3. 环境（严格来自场景卡）──
    env_bits: List[str] = []
    if scene:
        if scene.get("name"):
            env_bits.append(_t(scene["name"]))
        # ★ 优先用场景的**固定槽位**（地形/建筑/陈设/氛围/色调）：
        #   同一地点在每个镜头里拿到的环境描述逐字相同，场景才不会一个镜头一个样。
        ssl = _slots_of(scene)
        slot_order = ("terrain", "architecture", "furnishings", "atmosphere", "color_tone")
        slot_txt = [f"{SCENE_SLOT_ZH[k]}：{ssl[k]}" for k in slot_order if ssl.get(k)]
        if slot_txt:
            env_bits.append("；".join(slot_txt))
        elif scene.get("description"):
            env_bits.append(_t(scene["description"]))
        for k, m in (("time_of_day", TIMEOFDAY), ("lighting", LIGHTING), ("weather", WEATHER)):
            v = _t(scene.get(k)).lower()
            if v in m:
                env_bits.append(m[v])

    # ── 4. 角色签名（严格来自角色卡，固定不变 → 跨镜头一致性）──
    # 只保留**本镜头确实出场**的角色：把全剧组塞进每个镜头会稀释提示词重点，
    # 还可能让模型把没出场的人也画进去。
    # ★ 2026-09-14：名字匹配必须**忽略空格/间隔号**。实测角色卡叫「AI神」，
    #   而动作描述里写的是「AI 神」—— 旧代码 `n in action` 直接判不中，
    #   于是这个角色被当成"没出场"，签名不进提示词、图也不绑定。
    def _nkey(x: Any) -> str:
        return re.sub(r"[\s·・\.\-—_（）()]+", "", str(x or ""))

    all_sigs = [(c.get("name") or "", character_signature(c), c)
                for c in (characters or []) if isinstance(c, dict)]
    all_sigs = [(n, s, c) for n, s, c in all_sigs if s]
    _act_key = _nkey(action)
    mentioned = [s for n, s, _c in all_sigs
                 if n and _nkey(n) and (_nkey(n) in _act_key or n in action)]
    sigs = mentioned if mentioned else [s for _n, s, _c in all_sigs[:2]]

    # ★★ 图与人**绑定编号**（官方建议：`[图1]xxx，[图2]xxx` 才是指令遵循最好的写法）。
    #   只写角色名、不说哪张图是谁，模型只能自己猜 —— 多张参考图时尤其糟。
    #   编号顺序 = 适配器实际发送顺序（角色图在前、场景图在后），所以必须同源。
    _img_of: Dict[str, int] = {}
    _scene_img = 0
    if ref_scope:
        _k = 0
        for _n, _s, _c in all_sigs:
            if _c.get("reference_image_path"):
                _k += 1
                _img_of[_n] = _k
        if scene and scene.get("reference_image_path"):
            _scene_img = _k + 1

    # ── 5. 组装成一段连续镜头的描述 ──
    #
    # ★★ 2026-09-13：按参考方法论的**十二段镜头契约**补齐结构。
    #   旧版只写"镜头：<景别>，<运镜>。人物：…。环境：…。画面内容：…。"
    #   审计实测：16 条真实提示词**全部**因为缺"结束构图 / 音频边界"被判不合格
    #   （`P-MISSING-CAMERA-END` / `P-MISSING-AUDIO`，平均分 59.7）。
    #   缺这两样的直接后果就是"模型自己编结尾、平台自己加音乐或旁白"。
    seg: List[str] = []
    # ② 精确主体数量与允许集合（契约 #2：`画面中恰好[N]个主体：[ID]各出现一次；
    #    未列出的角色不在画面和空间中`）。为什么不写不行：不写数量，
    #    模型爱加几个加几个 —— 这正是"多出一个人 / 换了个主角"的来源。
    subject_names = [n for n, _s, _c in all_sigs
                     if n and (_nkey(n) in _act_key or n in action)]
    if not subject_names:
        subject_names = [n for n, _s, _c in all_sigs[:2] if n]
    if subject_names:
        # 有参考图时把编号写在名字前面（`[图1] 用户`），模型才知道哪张图是谁
        _labeled = [(f"[图{_img_of[n]}] {n}" if _img_of.get(n) else n) for n in subject_names]
        seg.append(f"画面中恰好 {len(subject_names)} 个主体：{'、'.join(_labeled)} "
                   f"各出现一次；未列出的角色不在画面内，也不要出现在倒影或背景里")
    if size:
        seg.append("起始构图（景别）：" + size)
    seg.append(f"镜头运动：{cam}（**整个镜头只有这一个主运动**，不要再叠加第二个运镜）")
    if sigs:
        seg.append("人物（严格保持以下外形与服装，不要改变）：" + "；".join(sigs[:4]))
    # ★★ 参考图**必须写明继承什么、排除什么**（2026-09-14，学自 Hell-Grind 方法论的
    #   `reference-asset-control.md:64-81` / 审计规则 `P-REFERENCE-SCOPE`）：
    #   只写"参考这张图"是**不可控指令** —— 模型会把原图的构图/机位/背景/光线/调色
    #   一起带进来（用户实测现象就是"场景我看到了（其实来自参考图），
    #   该出现的人物却没有"）。参考图只负责**身份**，场景由文字与首帧负责。
    if ref_scope:
        _who = "、".join(f"[图{i}]={n}" for n, i in _img_of.items() if i)
        seg.append("参考图对应关系：" + (_who + "；" if _who else "")
                   + (f"[图{_scene_img}]=场景环境。" if _scene_img else "")
                   + "人物**必须**与对应参考图是同一个人（面部身份/发型/体型/服装一致），"
                     "不要另造一个相似但不同的人，也不要把参考图里没出现的角色画进来。")
        seg.append("参考图的使用范围：**只继承**该人物的面部身份、发型、体型比例与服装；"
                   "**必须排除**参考图原始的姿态、构图、机位、背景、光线与调色 ——"
                   "人物要按本镜描述重新站位与打光，画面里不要出现参考图的背景")
    if env_bits:
        seg.append("环境：" + ("，".join(env_bits)
                              + (f"（以 [图{_scene_img}] 为环境参考）" if _scene_img else "")))
    if action:
        seg.append("画面内容：" + action)

    # ★ 层间衔接：把上一镜的结尾状态带进来，让相邻镜头连得上（用户要求"考虑分镜不同层之间的衔接"）
    #
    # ★★ 2026-09-13 修：衔接句里**也必须剪掉运镜从句**。
    #   实测（63 个真实分镜，生成口径审计）：不剪的话有 3 个分镜被判
    #   `P-CAMERA-CONFLICT` —— 因为上一镜的尾动作常写成
    #   "镜头环绕铁王座，展示厅堂的残破景象"，原样搬进"承接上一镜"，
    #   提示词里就同时有了两个运镜。而这 3 个分镜在**体检接口的旧口径**
    #   （没传相邻分镜）里全是"0 硬伤" —— 闸门根本看不到。
    #   衔接该交代的是**姿态/位置/光线**，不是摄影机。
    if prev_shot:
        prev_end, _ = _strip_camera_clauses(_last_action(prev_shot))
        if prev_end:
            seg.append(f"承接上一镜（画面开始时人物姿态/位置/光线要与此衔接）：{prev_end[:160]}")
    if next_shot:
        nxt, _ = _strip_camera_clauses(_first_action(next_shot))
        if nxt:
            seg.append(f"为下一镜留出衔接（结尾姿态不要突变，下一镜将从这里继续）：{nxt[:120]}")

    # ⑥ 结束构图（契约 #6 的 camera_end：结束景别、主体关系、对焦、下一镜接点）。
    #    审计把它列为 error：只写"怎么动"、不写"停在哪"，模型会把镜头甩到任意地方收尾。
    end_hint = re.sub(r"[。．.；;，,、\s]+$", "", (_last_action(shot) or action or "").strip())
    # ★ 结束构图里也**不能出现运镜词**：实测最后一秒的动作常写成
    #   "镜头环绕铁王座，展示厅堂的残破景象…"，直接搬进"结束构图"
    #   会让提示词里出现第二个运镜（审计判 `P-CAMERA-CONFLICT`）。
    end_hint, _ = _strip_camera_clauses(end_hint)
    seg.append(
        "结束构图：" + (f"落在「{end_hint[:70]}」这个状态上，主体仍在画面内、构图稳定，"
                        f"尾帧不要突然甩开主体或切到别处" if end_hint
                        else "主体保持在画面内，构图稳定，尾帧清晰可读"))

    # ⑫ 交付规格（契约 #12）：时长。写进提示词，模型才知道动作要摊在几秒里。
    try:
        seg.append(f"总时长：{int(duration)} 秒")
    except Exception:
        pass

    # ⑩ 音频边界（契约 #10：说话者与精确台词、尾部静默、对白是否仅为声音、
    #    音乐/字幕/旁白边界）。★ "对白不视觉化"这一条直接对应
    #    "主角从冰雪里爬出来"那类画面：台词里提到的过去/别处，
    #    模型会真的把它画出来（闪回/额外场景）。
    _dlg: List[str] = []
    try:
        from core.dialogue import timeline_lines as _tlv
        _dlg = [str(x.get("text") or "").strip() for x in
                _tlv(shot.get("layer2_timeline"), float(duration or 5))]
        _dlg = [d for d in _dlg if d]
    except Exception:
        _dlg = []
    if _dlg:
        seg.append("音频：人物逐字台词为「" + "」「".join(_dlg[:3]) + "」；"
                   "台词**只作为声音**，不触发闪回、不出现台词里提到的额外人物或地点；"
                   "台词说完后嘴唇静止；无配乐、无旁白、无字幕烧录")
    else:
        seg.append("音频：这一镜没有对白（也没有旁白）；保留现场环境底床；无配乐、无字幕烧录")

    art_style = ""
    if art:
        art_style = _t(art.get("visual_style"))
    seg.append("风格：" + "；".join(x for x in (art_style, STYLE.get(style, STYLE["cinematic"])) if x))
    seg.append(QUALITY)

    prompt, hard_cut = _fit_to_budget(seg, int(max_chars))

    # ── 6. 负面提示：约束走独立通道（第 3 层）──
    neg: List[str] = []
    cons = shot.get("layer3_constraints") if use_l3 else None
    if isinstance(cons, str):
        try:
            cons = json.loads(cons)
        except Exception:
            cons = {}
    if isinstance(cons, dict):
        for k in ("must_not_appear", "must_not_happen"):
            v = cons.get(k)
            if isinstance(v, (list, tuple)):
                neg.extend(_t(x) for x in v if _t(x))
            elif _t(v):
                neg.append(_t(v))
    # 通用质量负面词
    neg.extend(["画面中出现文字", "水印", "logo", "字幕", "画面撕裂", "人物变形",
                "多余手指", "面部崩坏", "画面卡顿", "低分辨率"])
    negative = "，".join(dict.fromkeys(n for n in neg if n))

    # ── 7. 厂商参数：**不在提示词里写**（单一来源 = 适配器的 body 字段）──
    # ★★ 2026-09-14 真实事故复盘：这里原来会给火山系（seedance/jimeng）追加
    #   `--ratio 16x9 --resolution … --duration …`。官方文档写明两种传参方式：
    #     · 新方式（body 顶层字段）= **强校验**，写错会报错；
    #     · 旧方式（提示词后缀 `--[parameters]`）= **弱校验**，写错**被忽略**。
    #   我们**同时**写了两处，而且两处都把 `16:9` 错写成 `16x9`：
    #   body 那份被塞进不存在的 `"parameters": {...}` 里整段忽略，
    #   后缀那份落在弱校验路径被忽略 → `ratio` 缺省为 `adaptive` →
    #   跟着那张方形参考图出了 960x960(1:1) → 被画幅守卫丢掉 → 退回本地幻灯片。
    #   现在参数**只在适配器的 body 顶层**写一次（`normalize_ratio` 统一成 `16:9`），
    #   提示词因此保持**模型无关**（这也是 Hell-Grind 方法论的要求：
    #   主提示词里不得混入平台私有参数，`prompt-architecture.md:236-242`）。
    if provider in ("seedance", "jimeng"):
        try:
            from core.aspect import normalize_ratio as _nr
            meta_params = {"ratio": _nr(aspect_ratio), "resolution": resolution,
                           "duration": int(duration)}
        except Exception:
            meta_params = {}
    else:
        meta_params = {}

    meta = {
        "shot_size": size, "camera": cam,
        # 厂商参数（供上层核对；**不写进提示词**，见第 7 段注释）
        "vendor_params": meta_params,
        # ★ 时间线里出现过不止一种运镜 → 只保留了第一个，**必须让上层知道**
        #   （契约：一个镜头只有一个主运动；被丢掉的那些应当由上层决定
        #   是拆镜还是接受）。以前这里是静默丢弃。
        "camera_moves_seen": list(dict.fromkeys(cameras)),
        "camera_conflict": len(set(cameras)) > 1,
        "characters": [c.get("name") for c in (characters or []) if isinstance(c, dict) and c.get("name")],
        "scene": (scene or {}).get("name") if scene else "",
        "prompt_chars": len(prompt), "negative_chars": len(negative),
        # ★ `has_action=False` 是一个**必须让上层看到的信号**：
        #   分镜既没有 timeline 动作、layer1 也是空的 → 提示词里根本没有
        #   "画面内容"这一句，模型只能自由发挥。用户看到的就是
        #   "生成的视频跟实际描述的完全不相干" —— 而它其实**不是模型的问题**，
        #   是我们给了一段没有内容的提示词。以前这种情况一声不响，
        #   现在上层可以据此拦住它、告诉用户去补内容。
        "has_action": bool(action),
        "action_chars": len(action or ""),
    }

    # ── 8. 自检（新增）：把提示词过一遍审计器，结果随返回值一起给上层。
    #    ★ 这一步的意义是"**在花钱之前**发现问题"：审计不过的提示词
    #      送去生成，基本就是废片（而且钱已经花了）。
    audit: Dict[str, Any] = {}
    try:
        from core import promptaudit
        audit = promptaudit.audit_video_prompt(
            prompt, negative, duration=float(duration) if duration else None,
            dialogue=_dlg,
            extra={"camera_conflict": len(set(cameras)) > 1,
                   "camera_moves_seen": list(dict.fromkeys(cameras))})
        meta["audit_score"] = audit.get("score")
        meta["audit_errors"] = audit.get("error_count")
        meta["audit_warnings"] = audit.get("warning_count")
        meta["valid_for_generation"] = audit.get("valid_for_generation")
    except Exception as e:      # 审计器自身出问题不许拦住主流程
        logger.warning("提示词自检失败（不拦截）：%s", e)
        audit = {"error": f"{type(e).__name__}: {e}"}

    return {"prompt": prompt, "negative_prompt": negative, "meta": meta,
            "audit": audit}


def build_scene_video_prompt(scene: Dict[str, Any], art: Optional[Dict[str, Any]] = None,
                             style: str = "cinematic", camera: str = "缓慢推近") -> str:
    """场景空镜视频的提示词"""
    bits = []
    bits.append(f"环境空镜，{_norm_camera(camera) or camera}")
    if scene.get("name"):
        bits.append(_t(scene["name"]))
    if scene.get("description"):
        bits.append(_t(scene["description"]))
    meta = []
    for k, m in (("time_of_day", TIMEOFDAY), ("lighting", LIGHTING), ("weather", WEATHER)):
        v = _t(scene.get(k)).lower()
        if v in m:
            meta.append(m[v])
    if meta:
        bits.append("，".join(meta))
    bits.append("画面中无人物，镜头运动平稳")
    if art and art.get("visual_style"):
        bits.append(_t(art["visual_style"]))
    bits.append(STYLE.get(style, STYLE["cinematic"]))
    return "。".join(b for b in bits if b)
