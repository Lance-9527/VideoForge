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

# ★ 2026-10-25：视频提示词的"电影摄影层"拆到独立模块（契约块/负向分层/审计补充）。
#   为什么必须拆：运镜段落要**只**来自 `core.shot_contract` 的契约块，
#   而表演块/声音块/负向人物层要按"这一镜到底有没有演员出镜"分流 ——
#   这些是独立于"分镜表怎么拼"的规则，混在本文件里会再次长回"取第一个运镜词"。
try:
    from core import prompt_film as _film
except Exception as _e:                      # pragma: no cover - 仅打包缺文件时触发
    _film = None                             # type: ignore[assignment]
    logger.warning("prompt_film 不可用（提示词将退回旧运镜逻辑）：%s", _e)

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
# ★ 2026-10-25：**不再拼进提示词**。这句话是"电影级写实/清晰锐利"这类空话，
#   现在由 `prompt_film.film_layers` 的摄影层（景别焦段/布光/质感/颗粒/亮度）
#   用具体名词表达。常量保留仅为兼容可能的外部 import，新代码不要再用。
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

    削的**顺序**按"每字价值"排：2026-10-25 新增的电影摄影层（景别焦段/布光/质感/
    颗粒/亮度）最先让位，其次是对接；对接、人物签名、环境、（最后）画面内容；
    "契约镜头块 / 主体数量 / 景别 / 结束构图 / 总时长 / 音频"**永远不削**。
    实在削不动才硬截，并且**如实返回 True**，让上层能告警。
    """
    parts = [s for s in seg if s]

    def _joined() -> str:
        return _WS_RE.sub(" ", "。".join(parts))

    text = _joined()
    if len(text) <= max_chars:
        return text, False
    for pref, keep in (
        # ── ① 装饰性最强、删掉最不影响可控性：电影摄影层的分项 ──
        ("景别与焦段", 0),
        ("布光：", 0),
        ("质感：", 0),
        ("颗粒与动态模糊", 0),
        ("亮度层次", 0),
        ("为下一镜", 0),        # 与下一镜的衔接是最可以牺牲的（下一镜自己会承接）
        ("主体与道具", 0),
        ("承接上一镜", 90),
        ("人物（", 170),        # 外形签名很长，但**不能整段删**（删了会换脸）
        ("环境：", 110),
        ("主体形态", 0),
        ("画面内容：", 220),    # 核心内容，放到最后才削
        # 微表情表演：排在最后削 = 最受保护的可削项（仍让位于"永不削"那批契约项）。
        # 保留前 150 字符足够覆盖「基调 + 说给谁听 + 起手节拍」这部分最能定调的内容。
        ("表演（", 150),
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
    # ★ 2026-10-25 新增（全部可选，旧调用方不需要改）：
    #   `shot_spec` 是白膜 spec 里本镜的那一份；不传则按 shot["previz_path"]
    #   自动找 spec.json / camera-samples.json（见 `core/shot_contract.py`）。
    #   提示词里的运镜、焦距、半径、弧角、机位高度、占画比**只**从这里来。
    shot_spec: Optional[Dict[str, Any]] = None,
    script_text: str = "",
    camera_samples: Optional[List[Dict[str, Any]]] = None,
    subject_height_ratio_measured: Optional[float] = None,
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

    # ── 2. 镜头语言：**唯一权威 = 镜头契约**（2026-10-25 重写）──
    #
    # ★★ 这里以前是 `cam = cameras[0]` —— 分镜里同一个字段写两个运镜词
    #   （实测真实数据：「固定机位，横移」）就按分隔符切从句、**只保留第一个**。
    #   结果：剧本和白膜都是 orbit（半径 2.83m、85mm 定焦、弧线横移），
    #   提示词却写死「固定机位（整个镜头只有这一个主运动，不要再叠加第二个运镜）」。
    #   这是把机位写反，成片必然跟剧本对不上。
    #   现在：运镜段落**只**来自 `core/shot_contract.camera_prompt_block(contract)`；
    #   分镜里的旧写法只进 `meta`（如实上报），不再参与决策。
    raw_camera_names: List[str] = []
    for _raw in ([it.get("camera") or it.get("camera_move") for it in timeline
                  if isinstance(it, dict)] + [shot.get("camera")]):
        for _p in re.split(r"[，,、；;/|]+", _t(_raw)):
            _p = _p.strip()
            if not _p:
                continue
            # ★ 逐从句取规范名（旧 `_norm_camera` 只返回第一个，正是缺陷 ① 的来源；
            #   这里只做**上报**，不参与决策，所以多出来的运镜词必须保留下来）
            _z = _cn_camera_of(_p) or _norm_camera(_p)
            if _z and _z not in raw_camera_names:
                raw_camera_names.append(_z)
    for _w in (_cam_in_action or []):
        _z = _norm_camera(_w)
        if _z:
            cameras.append(_z)
            if _z not in raw_camera_names:
                raw_camera_names.append(_z)
    size = sizes[0] if sizes else _norm_shot_size(shot.get("shot_size") or "") or ""

    #   契约的意图文本：调用方给了 script_text 就用它，否则按契约层同样口径拼。
    contract_text = script_text or (
        _film.shot_script_text(shot) if _film is not None else
        " ".join(str(shot.get(k) or "") for k in
                 ("layer1_overview", "layer2_timeline", "layer3_constraints")))
    contract: Dict[str, Any] = {}
    contract_error = "prompt_film 不可用"
    if _film is not None:
        contract, contract_error = _film.resolve_contract(
            shot, shot_spec=shot_spec, script_text=contract_text,
            camera_samples=camera_samples,
            subject_height_ratio_measured=subject_height_ratio_measured)
    contract = contract or {}
    if contract_error:
        logger.warning("镜头契约不可用（%s），运镜段退化为保守描述", contract_error)
    cam = ""
    if _film is not None and contract:
        cam = _film.movement_zh(contract)
    cam = cam or (_t(contract.get("movement_zh")) if contract else "") or \
        (cameras[0] if cameras else "固定机位")

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
    # ③ 镜头契约块：**运镜描述的唯一来源**（`core/shot_contract.camera_prompt_block`）。
    #    焦距 / 半径 / 弧角 / 机位高度 / 接触关系全部来自白膜 spec
    #    或渲染后实测相机（`camera-samples.json`），不再由分镜文本里的运镜词决定。
    if _film is not None:
        seg.append(_film.camera_block_for_prompt(contract))
    else:                                     # pragma: no cover - 缺模块时的最小降级
        seg.append("镜头运动：" + cam)
    # ③b 单主运动声明（沿用旧验收口径：审计与人工都按这句判"没有第二个运镜"）。
    #    契约层已保证一个镜头只有一个主运动，这句是**结论**而不是祈祷。
    seg.append("主运动约束：整个镜头只有这一个主运动，不要再叠加第二个运镜")
    # ③c 电影摄影层：契约块之后补 景别与焦段用途 / 布光方向与软硬色温 /
    #    质感与调色倾向 / 颗粒与动态模糊 / 亮度层次。
    #    ★ 全部用定性但具体的名词，不写契约里没有的数字（不编色温/角度）。
    #    ★ 无演员出镜时这里还会补「主体与道具数量 / 主体形态」，
    #      替掉被删除的表演层 —— 静物镜头靠这几句保持主体可控。
    has_cast = False
    if _film is not None:
        has_cast = _film.visible_cast(shot, timeline=timeline,
                                      on_screen_names=subject_names, action=action)
        for _layer in _film.film_layers(contract, shot=shot, scene=scene, art=art,
                                        action=action, style=style,
                                        shot_size=size, has_cast=has_cast):
            seg.append(_layer)
    else:                                     # pragma: no cover
        has_cast = bool(subject_names)
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

    # ⑬ 微表情表演（biaoqing-yanyi skill）
    # ══════════════════════════════════════════════════════════════
    # ★★ 2026-09-25：这里以前**根本没有表情这一项**。
    #   实测（scripts/_probe_expression_reach.py）：两个只在 `expression` 上
    #   不同的镜头（"平静克制" vs "歇斯底里地嚎啕大哭"），生成出来的提示词
    #   **逐字节完全相同**（都是 330 字符），两句话都找不到 ——
    #   因为本文件当时读的是 action/camera/shot_size，从没读过 expression。
    #   用户反馈的"微表情控制不了"，根因就在这最后一公里：
    #   分镜里生成了、存库了，拼提示词时被丢了。
    #
    #   现在把 core/acting.py 算出的表演结构压成一段按时间码执行的指令。
    #   注意 `timeline`（而不是重新读 shot）—— 这样 l2 层开关关掉时，
    #   表演也会跟着关掉，不会绕过用户的层开关。
    #
    # ★★ 2026-10-25：表演层**必须先判"这一镜到底有没有演员出镜"**。
    #   实测真实静物镜头（白色立方体/黑色台面，characters=[]）的提示词里
    #   出现了「表演（按时间码执行）：基调冷静/克制…眼、唇、肩、呼吸全都收着」
    #   与「声音：压低声音＝稳定、克制」—— 因为库里老数据的时间线被富化过
    #   `expression`/`performance`，而这里以前**无条件**拼表演段。
    #   静物镜头没有可以表演的脸，这段文本只会把模型往人像方向拽。
    #   `has_cast` 只在"有角色卡出镜 / 动作文本里明确指人 / 有非旁白的说话者"
    #   时为 True；**不把 expression/performance 字段本身当证据**（那正是脏数据）。
    if has_cast and use_l2:
        try:
            from core.acting import performance_prompt_lines as _ppl
            _perf_txt = _ppl(timeline, duration=float(duration or 0))
            if _perf_txt:
                seg.append(_perf_txt)
        except Exception as e:
            # 表演拼不出来不该毁掉整条提示词，但**必须留下痕迹**
            logger.warning("拼装微表情表演失败（本镜提示词将不含表演指令）：%s", e)

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
                   "台词说完后嘴唇静止；无配乐、无旁白")
    else:
        # ★ 2026-10-25：无对白时的音频边界**不写表演/配音说明**。
        #   静物镜头上「声音：压低声音＝稳定、克制」这类技艺文本是纯噪声
        #   （它属于表演层，已由 `has_cast` 分流挡掉）。
        #   但"音频边界"本身必须保留：审计的 `P-MISSING-AUDIO` 是 error，
        #   而且不写边界平台会自动配乐/加旁白（`F-AUDIO-POLLUTION`）。
        _bed = "人物呼吸、衣料摩擦等细节声" if has_cast else "主体与台面的接触/摩擦声"
        seg.append("音频：这一镜没有对白（也没有旁白）；保留现场环境底床与" + _bed + "；无配乐")

    art_style = ""
    if art:
        art_style = _t(art.get("visual_style"))
    # ★ 2026-10-25：把「电影级写实，胶片质感，自然光影层次…」这类**空话**换成
    #   具体的实拍质感要求（真正的摄影层在 ③c，这里只定风格基调）。
    #   非写实风格（动画/水墨/赛博…）仍用各自的风格词，不硬套写实质感。
    _style_bits = [art_style]
    if style == "cinematic":
        _style_bits.append("实拍质感优先：真实镜头语言、物理正确的光影与材质响应，"
                           "拒绝塑料 CG 感与过度调色")
    else:
        _style_bits.append(STYLE.get(style, STYLE["cinematic"]))
    seg.append("风格：" + "；".join(x for x in _style_bits if x))

    prompt, hard_cut = _fit_to_budget(seg, int(max_chars))

    # ── 6. 负面提示：**分层**走独立通道（2026-10-25 重写）──
    #    旧实现把三样东西一口气塞进 negative：
    #      第 3 层约束 + 一锅通用质量词 + `acting.performance_negatives`。
    #    最后那组是**人物镜头**的模板（面部不崩 / 嘴不能张得过大 / 不烧字幕），
    #    静物镜头照收 → 用户看到的 negative_prompt 里出现
    #    「面部不崩不形变，嘴不能张得过大…除非要求，不烧字幕」这种纯噪声。
    #    现在分三层：
    #      通用层（文字/水印/畸变/切镜/抖动/画幅跳变）—— 所有镜头都要；
    #      静物层（不得出现人物/多余道具/主体位移形变/悬浮）—— 仅无演员镜头；
    #      人物层（脸崩/手指/口型/身份漂移 + 微表情 skill 的必带限制）—— 仅有演员镜头。
    #    层名随 meta["negative_layers"] 返回，方便上层核对"这层该不该在"。
    cons = shot.get("layer3_constraints") if use_l3 else None
    neg_layer_names: List[str] = ["constraints"] if cons else []
    if _film is not None:
        negative, neg_layer_names = _film.negative_layers(
            has_cast=has_cast, timeline=timeline, constraints=cons,
            max_chars=int(max_chars))
    else:                                     # pragma: no cover - 缺模块时的最小降级
        negative = "，".join(dict.fromkeys(
            ["文字", "水印", "画面畸变", "切镜", "画面抖动", "画幅跳变"]
            + ([] if not has_cast else ["面部崩坏", "多余手指"])))

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

    # ── 运镜来源核对（只如实上报，不参与决策）────────────────────────
    #   `camera_moves_seen`：契约认定的运镜排第一，后面是分镜里出现过的旧写法
    #     （旧逻辑"取第一个"丢掉的那些，现在**一个都不丢**地报给上层）；
    #   `camera_conflict`：契约有冲突记录，或分镜 camera 字段与契约互相矛盾；
    #   `camera_conflict_hard`：只有「剧本意图 vs 白膜 movement」这种硬冲突
    #     （lead 口径：声明 85 / 实际 46.75 属于正常适配，不算硬冲突）。
    raw_incompatible: List[str] = []
    contract_conflicts: List[str] = []
    hard_conflict = False
    if _film is not None and contract:
        raw_incompatible = _film.incompatible_moves(contract, raw_camera_names)
        contract_conflicts = list(contract.get("conflicts") or [])
        hard_conflict = _film.has_hard_conflict(contract, contract_text)
    if _film is not None and contract:
        seen_moves = _film.camera_moves_seen(contract, raw_camera_names)
    else:                                     # pragma: no cover
        seen_moves = list(dict.fromkeys(cameras)) or [cam]
    camera_conflict = bool(contract_conflicts) or bool(raw_incompatible) or \
        (not contract and len(set(cameras)) > 1)

    meta = {
        "shot_size": size, "camera": cam,
        # 厂商参数（供上层核对；**不写进提示词**，见第 7 段注释）
        "vendor_params": meta_params,
        # ★ 运镜来源核对（见上面注释）。以前这里是"只保留了第一个"的静默丢弃。
        "camera_moves_seen": seen_moves,
        "camera_conflict": camera_conflict,
        "camera_conflict_hard": hard_conflict,
        "camera_conflicts": contract_conflicts,
        # ★ 契约关键数字：提示词里的运镜段落就是按这些写的，上层可直接核对
        "camera_contract": ({
            "movement": contract.get("movement"),
            "movement_zh": contract.get("movement_zh"),
            "lens_mm": contract.get("lens_mm"),
            "lens_declared_mm": contract.get("lens_declared_mm"),
            "lens_rewritten": bool(contract.get("lens_rewritten")),
            "arc_degrees": contract.get("arc_degrees"),
            "radius_m": contract.get("radius_m"),
            "camera_height_m": contract.get("camera_height_m"),
            "subject_height_ratio": contract.get("subject_height_ratio"),
            "subject_ratio_source": contract.get("subject_ratio_source"),
            "subject_on_surface": contract.get("subject_on_surface"),
            "no_zoom_confirmed": contract.get("no_zoom_confirmed"),
            # ★ 几何来源：samples(渲染实测) / path(白膜关键帧) / camera(仅机位) / none(无白膜)
            #   none 时提示词不写任何焦距/半径数字（见 prompt_film.camera_block_for_prompt）
            "geometry_source": contract.get("_geometry_source"),
            "path_present": bool(contract.get("_path_present")),
            # ★ 白膜原始 movement 枚举 vs 归一后（旧 spec 的 push_in/pan 等，见 prompt_film）
            "spec_movement_raw": contract.get("_spec_movement_raw"),
            "spec_movement_mapped": contract.get("_spec_movement_mapped"),
        } if contract else {}),
        "film_layer_version": getattr(_film, "FILM_LAYER_VERSION", "") if _film else "",
        # ★ 这一镜画面上有没有演员：决定表演层/声音说明/负向人物层是否出现
        "has_cast": has_cast,
        "negative_layers": neg_layer_names,
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
        # 超预算硬截断以前是**白丢**的返回值：上层看不到"这条提示词被砍过"
        "prompt_hard_cut": bool(hard_cut),
        "contract_error": contract_error or "",
    }

    # ── 8. 自检（新增）：把提示词过一遍审计器，结果随返回值一起给上层。
    #    ★ 这一步的意义是"**在花钱之前**发现问题"：审计不过的提示词
    #      送去生成，基本就是废片（而且钱已经花了）。
    audit: Dict[str, Any] = {}
    try:
        from core import promptaudit
        # ★ 送审前把**否定式运镜词**归一（见 `prompt_film.neutralize_negated_camera`）：
        #   promptaudit 的运镜词表不感知"不…"：契约块里的「不推拉、不变焦」
        #   会被它当成一个 `zoom` 主运镜，和真正的 orbit 一起判成
        #   `P-CAMERA-CONFLICT` 硬伤 —— 那是**禁止语**，不是第二个运镜。
        #   契约层已经保证单一主运动，所以这里只影响审计口径，不改提示词。
        audit_text = (_film.neutralize_negated_camera(prompt)
                      if _film is not None else prompt)
        audit = promptaudit.audit_video_prompt(
            audit_text, negative, duration=float(duration) if duration else None,
            dialogue=_dlg,
            # 契约化之后"多源运镜只保留第一个"这个旧警告已不成立
            # （提示词按契约写，不再"取第一个"）；冲突改由 P-CAMERA-CONTRACT 表达。
            extra={"camera_conflict": False,
                   "camera_moves_seen": seen_moves})
        # 契约层的冲突/适配信息**如实**并进 issues：
        #   硬冲突 → warning（记分），其余（含焦距被渲染器改写）→ info（不记分）。
        if _film is not None:
            audit = _film.inject_issues(
                audit,
                _film.contract_issues(contract, contract_text,
                                      raw_incompatible=raw_incompatible),
                prompt, negative)
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
