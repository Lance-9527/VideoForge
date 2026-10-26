# -*- coding: utf-8 -*-
"""VideoForge · character-casting-studio 融合进「角色生成」

═══════════════════════════════════════════════════════════════
这个模块解决什么
═══════════════════════════════════════════════════════════════
在没有它之前，`assets.build_portrait_prompt()` 把人物卡拼成一段中文描述，
没有质感约束：出图经常是「蜡像脸 / 网红脸 / 油腻高光 / 脸被环境染成橘色」，
而且多个角色之间会长得越来越像（同一套"鹅蛋脸+黑长直+大眼"）。

`backend/skills_builtin/character-casting-studio/` 这个 skill 正好治这个病，
但它是一篇给人看的长文档。本模块把它变成**可执行的拼装逻辑**：

  · 优先级 `character accuracy > beauty`、`realism > perfection` 落到取舍上
  · NO WAX SKIN / 中性白平衡 / v1.1.1 干净电影感人像 三条硬规则变成
    **正向串 + 负向串**，由 `rules.json` 提供，不是硬编码
  · 11 步构建顺序决定字段的先后（人脸模型对靠前的词更敏感）
  · 质量门 `audit()` 用**具体证据**报告合规与缺口，而不是只说"看起来还行"

═══════════════════════════════════════════════════════════════
「跟剧本设定要契合」是怎么做到的
═══════════════════════════════════════════════════════════════
`ScriptContext` 从**剧本自己的文字**里抽出年代/题材/世界，并**带上证据**：

    era="民国/抗战", evidence=["剧本出现『抗日』（场景 1）", "剧本出现『伏击』（场景 2）"]

判不出来就如实写「未判定」+ 空证据，**绝不猜**。理由和 `scriptlines.py` 一样：
给画面配错设定比不给设定更糟，而且用户没法发现。

有了 era 之后 `audit()` 才能查真正的契合问题，例如：
  · 剧本是抗战，角色却穿着「西装」→ 报 mismatch，附证据
  · 剧本说这个人是「40 岁上下」，提示词里却写「30 多岁」→ 报年龄漂移
  · 角色只在「废墟/铁王座」场次出现，却被写成现代都市造型 → 报世界不一致

═══════════════════════════════════════════════════════════════
设计约束
═══════════════════════════════════════════════════════════════
· **纯函数**：不碰数据库、不碰网络。数据由 `main.py` / agent 工具取好传进来。
  这样才测得动 —— 参见 `scriptlines.py` 的同一约定。
· **只填空白，不改用户写的东西**（skill 原文：
  "Follow the user's explicit details exactly. Fill only unspecified fields."）。
  `brief()` 里每个字段都记 `source`（card / slots / description / script / derived），
  审计时能看出哪一项是补的。
· 审计失败**不阻断出图**，只如实报告 —— 一个会误判的检查不该有否决权。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger("videoforge.casting")


# ──────────────────────── 规则加载 ────────────────────────

# ★ 规则来自 skill 目录，不硬编码在这里。
#   这样"skill 学习"才有意义：用户更新 skill 的 rules.json，行为跟着变。
#   读不到时的兜底**只有安全的那几条**，并且会记 warning + 在结果里标出来，
#   绝不假装拿到了完整规则。
_FALLBACK_RULES: Dict[str, Any] = {
    "_fallback": True,
    "no_wax_negative": (
        "no waxy skin, no oily forehead, no glossy cheeks, no shiny nose bridge, "
        "no porcelain skin, no beauty-filter effect, no over-retouching, no mannequin face"),
    "positive_face_block": (
        "real human facial anatomy, natural semi-matte skin, subtle pores, fine skin texture, "
        "restrained professional retouching, slight tonal variation, believable under-eye texture, "
        "natural eyebrow hair, realistic lip texture, subtle facial asymmetry, individual nose geometry, "
        "distinctive but believable facial features, clean commercial photography, authentic human presence, "
        "neutral white balance on skin, very low facial specular reflection"),
    "neutral_skin_constraints": (
        "neutral white balance on skin, accurate skin tone, no yellow cast, no orange cast, "
        "no amber skin, no golden skin, no warm color grade on face, no tungsten spill on face, "
        "very low facial specular reflection"),
    "modes": {
        "cinematic": {"zh": "电影感",
                      "positive": "richer facial texture, lived-in expression, restrained wear, "
                                  "story-bearing wardrobe"},
        "commercial": {"zh": "商业",
                       "positive": "clean light, controlled color, clear wardrobe, "
                                   "polished but believable skin"},
    },
    "mode_default_blend": ["commercial", "cinematic"],
    "construction_order": [
        "original fictional character identity",
        "explicit user-specified features",
        "face structure and distinctive details",
        "hair, makeup, body, and pose",
        "complete wardrobe and accessories",
        "photographic treatment and lighting",
        "requested framing",
        "identity-consistency requirements",
        "negative constraints",
    ],
    "identity_safety": {
        "must_not_reproduce": [
            "a named celebrity, actor, model, public figure, or influencer",
            "a recognizable film or television character",
            "a specific supermodel's face or a single real person's complete feature set",
        ],
    },
    "general_negative": (
        "no celebrity likeness, no recognizable existing character, no duplicate person, "
        "no plastic skin, no beauty-filter face, no influencer look, no anime, no game concept art, "
        "no text labels"),
    "camera_split": {
        "face_closeup": "85-105mm portrait lens feeling, f/1.8-f/2.4, precise eye focus, "
                        "shallow depth of field, soft natural bokeh",
        "body_row": "50-70mm editorial lens feeling, f/4-f/5.6",
    },
    "clean_cinematic": {"positive": "clean cinematic exposure, face clearly readable, "
                                    "controlled contrast, readable facial planes",
                        "negative": "no underexposed face, no crushed facial shadows, "
                                    "no blacked-out eye sockets, no muddy contrast, "
                                    "no dirty-looking skin, no low-detail cinematic darkness"},
    "face_realism": {"min_feature_fields_replaced": 3, "min_identity_anchors_replaced": 1,
                     "memorable_traits_min": 2, "memorable_traits_max": 3,
                     "feature_fields": ["face shape/width", "eye shape/spacing", "eyelid type",
                                        "brow structure", "nose bridge/tip", "lip ratio",
                                        "chin length", "cheekbones", "jaw ratio"],
                     "identity_anchors": ["distinctive mark position", "eye signature",
                                          "mouth-corner movement", "hair silhouette",
                                          "signature accessory"]},
    "anti_gloss_banned_terms": [
        "glowing skin", "dewy skin", "luminous complexion", "radiant skin",
        "flawless skin", "glass skin", "polished skin", "wet look skin", "beauty lighting"],
    "batch_diversity": {
        "diversity_statement": "distinct unrelated individuals, not sisters, not twins, "
                               "not triplets, not the same face",
        "min_dimensions_changed_between_adjacent": 5,
        "banned_repeat_combo": "oval face, straight black hair, almond eyes, "
                               "high cheekbones, and neutral expression"},
}

_RULES_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def load_rules(*, force: bool = False) -> Dict[str, Any]:
    """从内置 skill 目录读 casting 规则。读不到就用兜底并标记。"""
    global _RULES_CACHE
    try:
        from . import skills as SK
        import os
        data_dir = os.environ.get("VIDEOFORGE_DATA_DIR") or os.getcwd()
        reg = SK.get_registry(data_dir)
        sk = reg.get("character-casting-studio")
        if sk is None:
            raise SK.SkillError("注册表里没有 character-casting-studio")
        mt = sk.rules_path.stat().st_mtime if sk.rules_path.is_file() else 0.0
        key = str(sk.rules_path)
        hit = _RULES_CACHE.get(key)
        if hit and not force and hit[0] == mt:
            return hit[1]
        r = reg.rules("character-casting-studio")
        if "_missing" in r or "_error" in r:
            raise SK.SkillError(str(r.get("_missing") or r.get("_error")))
        r = dict(r)
        r["_source"] = str(sk.rules_path)
        _RULES_CACHE[key] = (mt, r)
        return r
    except Exception as e:
        logger.warning(
            "读不到 character-casting-studio 的 rules.json（用内置安全兜底，"
            "质量门的部分检查会降级）：%s", e)
        fb = dict(_FALLBACK_RULES)
        fb["_fallback_reason"] = "%s: %s" % (type(e).__name__, e)
        return fb


# ──────────────────────── 剧本上下文（契合的依据）────────────────────────


@dataclass
class ScriptContext:
    """从剧本/项目里抽出来、用于「让角色契合设定」的依据。

    `evidence` 里的每一条都是**剧本原文里的具体字符串 + 出处**，
    这样用户能核对，也能一眼看出判定错在哪。
    """
    project_title: str = ""
    project_desc: str = ""
    style: str = "cinematic"
    aspect_ratio: str = "16:9"
    era: str = "未判定"
    genre: str = "未判定"
    world: str = ""
    era_evidence: List[str] = field(default_factory=list)
    genre_evidence: List[str] = field(default_factory=list)
    scenes: List[Dict[str, Any]] = field(default_factory=list)
    scene_details: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    characters: List[Dict[str, Any]] = field(default_factory=list)
    _notes: List[str] = field(default_factory=list)

    def is_grounded(self) -> bool:
        return self.era != "未判定" or self.genre != "未判定"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "project_title": self.project_title, "era": self.era, "genre": self.genre,
            "world": self.world, "style": self.style, "aspect_ratio": self.aspect_ratio,
            "era_evidence": self.era_evidence, "genre_evidence": self.genre_evidence,
            "scene_count": len(self.scenes), "character_count": len(self.characters),
            "grounded": self.is_grounded(), "notes": self._notes,
        }


# 年代/题材判据表。★ 每条的词都要求**足够specific**，并且记下命中词作证据。
_ERA_RULES: List[Tuple[str, Tuple[str, ...]]] = [
    ("民国/抗战", ("抗日", "抗战", "鬼子", "日寇", "伪军", "八路军", "新四军", "军阀",
                  "租界", "民国", "连长", "伏击", "战壕", "阵地", "游击队")),
    ("古代/王朝", ("皇帝", "皇上", "圣上", "朝廷", "王朝", "宰相", "将军", "大臣",
                  "江湖", "侠客", "门派", "宫中", "后宫", "御", "科举", "状元",
                  "王爷", "格格", "衙门", "县令", "书生", "古装")),
    ("近代/民国都市", ("旗袍", "黄包车", "洋行", "留声机", "老爷车", "电报", "报馆")),
    ("现代都市", ("手机", "电脑", "屏幕", "界面", "网络", "程序员", "公司", "办公室",
                  "地铁", "咖啡", "直播", "短视频", "APP", "软件", "硬盘", "服务器",
                  "代码", "AI", "人工智能", "外卖", "加班", "创业")),
    ("科幻/未来", ("星际", "飞船", "宇宙", "机器人", "人工智能觉醒", "赛博", "芯片",
                  "全息", "基因", "克隆", "太空", "外星", "机甲")),
    ("西式奇幻", ("国王", "王后", "王冠", "加冕", "城堡", "骑士", "巫师", "魔法",
                  "龙", "精灵", "矮人", "守夜人", "长城", "野人", "领主", "铁王座",
                  "君临", "王国", "公主", "王子", "议会")),
]

_GENRE_RULES: List[Tuple[str, Tuple[str, ...]]] = [
    ("抗战/战争", ("抗日", "抗战", "伏击", "阵地", "战壕", "鬼子", "连长", "战士", "战斗")),
    ("武侠/古装", ("江湖", "侠客", "门派", "武功", "剑", "内力", "掌门", "师父", "朝廷")),
    ("奇幻/史诗", ("王冠", "加冕", "铁王座", "守夜人", "龙", "魔法", "城堡", "领主", "野人")),
    ("科幻", ("星际", "飞船", "机器人", "赛博", "全息", "外星", "机甲", "人工智能")),
    ("都市/职场", ("公司", "办公室", "加班", "创业", "程序员", "地铁", "咖啡", "开会")),
    ("家庭/生活", ("母亲", "父亲", "孩子", "家里", "厨房", "饭桌", "邻居", "老家")),
    ("悬疑/犯罪", ("凶手", "案件", "警察", "证据", "尸体", "绑架", "审讯", "逃犯")),
]


def _scan_rules(text: str, rules: Sequence[Tuple[str, Tuple[str, ...]]],
                where: str, limit: int = 4) -> Tuple[str, List[str]]:
    """在文本里找判据。返回 (标签, 证据列表)。证据格式：出现『词』（出处）。"""
    for label, words in rules:
        hits = [w for w in words if w in text]
        if hits:
            ev = ["%s出现『%s』" % (where, w) for w in hits[:limit]]
            if len(hits) > limit:
                ev.append("…共命中 %d 个判据" % len(hits))
            return label, ev
    return "", []


def build_script_context(
    *,
    project: Optional[Dict[str, Any]] = None,
    script: Optional[Dict[str, Any]] = None,
    scene_details: Optional[Iterable[Dict[str, Any]]] = None,
    characters: Optional[Iterable[Dict[str, Any]]] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> ScriptContext:
    """把 db 里取来的原始行拼成 ScriptContext（纯函数）。

    年代/题材只从**剧本自己的文字**推断：outline + 每场的 title/hook/location/
    actions/dialogues/mood + 角色名与描述。项目描述也看一眼，但证据里标清楚出处。
    """
    project = project or {}
    script = script or {}
    settings = settings or {}

    ctx = ScriptContext(
        project_title=str(project.get("title") or ""),
        project_desc=str(project.get("description") or ""),
        style=str(settings.get("style") or project.get("style") or "cinematic"),
        aspect_ratio=str(settings.get("default_aspect_ratio") or "16:9"),
        characters=list(characters or []),
    )

    scenes = script.get("scenes") or []
    if isinstance(scenes, str):
        try:
            scenes = json.loads(scenes)
        except Exception:
            scenes = []
    if not isinstance(scenes, list):
        scenes = []
    ctx.scenes = [s for s in scenes if isinstance(s, dict)]

    for d in (scene_details or []):
        if not isinstance(d, dict):
            continue
        num = d.get("scene_number")
        detail = d.get("detail") if "detail" in d else d
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except Exception:
                continue
        if isinstance(detail, dict) and num is not None:
            try:
                ctx.scene_details[int(num)] = detail
            except (TypeError, ValueError):
                pass

    # ── 拼一份「剧本全文」，并按出处切块，好给证据标来源 ──
    chunks: List[Tuple[str, str]] = []
    outline = str(script.get("outline") or "").strip()
    if outline:
        chunks.append(("剧本大纲", outline))
    if ctx.project_desc.strip():
        chunks.append(("项目描述", ctx.project_desc))
    for i, s in enumerate(ctx.scenes, 1):
        num = s.get("scene_number") or i
        parts = [str(s.get(k) or "") for k in ("title", "hook", "location", "subtitle")]
        for a in (s.get("actions") or []):
            parts.append(str(a))
        for d in (s.get("dialogues") or []):
            if isinstance(d, dict):
                parts.append(str(d.get("text") or ""))
                parts.append(str(d.get("emotion") or ""))
        parts.append(str(s.get("mood") or ""))
        chunks.append(("场景 %s《%s》" % (num, s.get("title") or ""), " ".join(parts)))
    for num, d in sorted(ctx.scene_details.items()):
        parts = [str(d.get(k) or "") for k in ("title", "location", "mood", "lighting")]
        for a in (d.get("actions") or []):
            parts.append(str(a))
        for a in (d.get("key_props") or []):
            parts.append(str(a))
        chunks.append(("场次细纲 %s" % num, " ".join(parts)))
    for c in ctx.characters:
        if isinstance(c, dict):
            chunks.append(("角色《%s》" % (c.get("name") or ""),
                           " ".join(str(c.get(k) or "") for k in
                                    ("name", "description", "costume_main", "costume_alternate"))))

    # ── 逐块判定，取第一处命中并保留全部证据 ──
    era_votes: Dict[str, List[str]] = {}
    genre_votes: Dict[str, List[str]] = {}
    for where, text in chunks:
        if not text.strip():
            continue
        e, ev = _scan_rules(text, _ERA_RULES, where)
        if e:
            era_votes.setdefault(e, []).extend(ev)
        g, gv = _scan_rules(text, _GENRE_RULES, where)
        if g:
            genre_votes.setdefault(g, []).extend(gv)

    if era_votes:
        # 命中证据最多的胜出；平票时按 _ERA_RULES 的先后（越靠前越specific）
        order = {lab: i for i, (lab, _) in enumerate(_ERA_RULES)}
        best = sorted(era_votes.items(), key=lambda kv: (-len(kv[1]), order.get(kv[0], 99)))[0]
        ctx.era, ctx.era_evidence = best[0], best[1][:6]
        if len(era_votes) > 1:
            others = [k for k in era_votes if k != ctx.era]
            ctx._notes.append(
                "年代判定有分歧：主判『%s』（%d 条证据），另命中 %s。"
                "如果判错了请直接改角色服装/在剧本里写明年代。"
                % (ctx.era, len(best[1]), "、".join(others)))
    else:
        ctx._notes.append("年代未判定：剧本里没有出现任何可依据的年代词（不猜）。")

    if genre_votes:
        order = {lab: i for i, (lab, _) in enumerate(_GENRE_RULES)}
        best = sorted(genre_votes.items(), key=lambda kv: (-len(kv[1]), order.get(kv[0], 99)))[0]
        ctx.genre, ctx.genre_evidence = best[0], best[1][:6]
    else:
        ctx._notes.append("题材未判定：剧本里没有出现任何可依据的题材词（不猜）。")

    # ── 世界：用场景地点拼一句，作为视觉世界约束 ──
    locs: List[str] = []
    for s in ctx.scenes:
        loc = str(s.get("location") or "").strip()
        if loc and loc not in locs:
            locs.append(loc)
    for _n, d in sorted(ctx.scene_details.items()):
        loc = str(d.get("location") or "").strip()
        if loc and loc not in locs:
            locs.append(loc)
    ctx.world = "；".join(locs[:4])
    return ctx


# ──────────────────────── 角色 brief ────────────────────────

_GENDER_ZH = {"male": "男性", "m": "男性", "man": "男性", "男": "男性",
              "female": "女性", "f": "女性", "woman": "女性", "女": "女性"}
_ROLE_ZH = {"protagonist": "主角", "lead": "主角", "supporting": "配角",
            "support": "配角", "extra": "群演", "antagonist": "反派",
            "villain": "反派", "narrator": "旁白"}

# 年代 → 该年代**不该出现**的服装/物件词（用于查"跟剧本设定不契合"）
_ANACHRONISM: Dict[str, Tuple[str, ...]] = {
    "民国/抗战": ("西装", "晚礼服", "高跟鞋", "手机", "电脑", "西装革履", "牛仔裤", "T恤",
                  "球鞋", "墨镜", "手表", "机车", "皮夹克"),
    "古代/王朝": ("手机", "电脑", "西装", "牛仔裤", "T恤", "球鞋", "领带", "手表",
                  "眼镜", "皮夹克", "连衣裙"),
    "西式奇幻": ("手机", "电脑", "西装", "牛仔裤", "T恤", "球鞋", "领带", "眼镜",
                  "机车", "西装革履"),
    "现代都市": ("盔甲", "长袍", "佩剑", "蓑衣", "马褂", "官服", "龙袍", "铠甲"),
    "科幻/未来": ("马褂", "官服", "龙袍", "蓑衣", "长矛", "铠甲"),
}

# 服装里的"年代锚点"词（正向）：出现这些说明服装**符合**某个年代
_ERA_WARDROBE: Dict[str, Tuple[str, ...]] = {
    "民国/抗战": ("军装", "军服", "粗布", "绑腿", "布鞋", "布腰带", "补丁", "长衫",
                  "旗袍", "中山装", "学生装", "草鞋", "斗笠"),
    "古代/王朝": ("长袍", "长衫", "襦裙", "马褂", "官服", "龙袍", "铠甲", "盔甲",
                  "佩剑", "束发", "发冠", "靴", "披风", "蓑衣", "罗裙"),
    "西式奇幻": ("斗篷", "皮甲", "锁子甲", "长袍", "皮靴", "披风", "束腰", "铠甲",
                  "盔甲", "佩剑", "毛皮"),
    "现代都市": ("西装", "衬衫", "T恤", "牛仔裤", "连衣裙", "卫衣", "球鞋", "风衣",
                  "针织", "外套", "职业装"),
    "科幻/未来": ("紧身", "功能服", "护甲", "金属", "连体", "作战服", "制服"),
}


@dataclass
class Field:
    """带来源标注的字段：审计时能看出哪一项是用户写的、哪一项是我补的。"""
    value: str = ""
    source: str = ""          # card | slots | description | script | derived | ""
    evidence: str = ""

    def __bool__(self) -> bool:
        return bool(str(self.value or "").strip())

    def as_tuple(self) -> Tuple[str, str, str]:
        return (str(self.value or "").strip(), self.source, self.evidence)


# casting 只认 `reference_features["slots"]` 这个**嵌套**形状
# （`assets._character_slots()` 就是按它写的）。如果卡被**平铺**存
# （face_shape 放在顶层），槽位会静默全丢 —— 用下面的告警把这种情况喊出来。
_SLOT_KEYS = ("face_shape", "skin", "eyes", "hair", "distinctive", "signature",
              "height_build", "costume_top", "costume_bottom", "costume_shoes")


def _rf_of(char: Dict[str, Any]) -> Dict[str, Any]:
    """取角色卡（reference_features），并保证返回 dict。

    ★★ 解析失败以前是**静默**返回 `{}`（2026-09-26 改成告警）。
    静默的后果很难查：角色卡的固定槽位、性格、人物关系**全部消失**，
    提示词悄悄退化成"只有 description"，而**没有任何地方说为什么** ——
    用户看到的只是"角色怎么越出越不像"。

    实测这个字段真的会被污染：`main.py` 的角色出图接口曾把
    `generate_image()` 返回的 prompt 写进这里（`core/imagegen.py:555`
    的 `"prompt"` 键），于是**点一次出图，slots 就没了**，
    而且从此每次重新生成都在漂。源头已经修掉，这里留一句告警：
    再有别的写法污染它，日志里能直接看到是什么、前 120 字长什么样。
    """
    rf = char.get("reference_features")
    if isinstance(rf, str):
        s = rf.strip()
        if not s:
            return {}
        try:
            rf = json.loads(s)
        except Exception as e:
            logger.warning(
                "角色卡的 reference_features 不是合法 JSON（%s: %s）—— "
                "固定槽位/性格/人物关系全部读不到，提示词会退化成只有 description。"
                "这个字段必须是 JSON 对象。前 120 字：%s",
                type(e).__name__, e, s[:120])
            return {}
    return rf if isinstance(rf, dict) else {}


def _slots_of(char: Dict[str, Any]) -> Dict[str, Any]:
    """取固定槽位（即 `reference_features["slots"]`）。"""
    rf = _rf_of(char)
    sl = rf.get("slots")
    if isinstance(sl, dict) and sl:
        return sl
    # 形状不对、但看得出是"平铺的角色卡" → 明确喊出来，
    # 别让它静默变成"这个角色没有槽位"
    flat = [k for k in _SLOT_KEYS if str(rf.get(k) or "").strip()]
    if not isinstance(sl, dict) and len(flat) >= 3:
        logger.warning(
            "角色卡是**平铺**的（顶层有 %s… 共 %d 个槽位键），但 casting 只读 "
            "reference_features['slots'] —— 这些槽位会被当成不存在。"
            "存卡时请用 {'slots': {...}} 的嵌套形状（见 assets._character_slots）。",
            "、".join(flat[:4]), len(flat))
    return sl if isinstance(sl, dict) else {}


def _norm(s: Any) -> str:
    return re.sub(r"[\s·・\.\-—_]+", "", str(s or ""))


def _scenes_with(ctx: ScriptContext, char_name: str) -> List[Dict[str, Any]]:
    """这个角色出现在哪些场次（按名字匹配，双向包含，长度≥2 才认）。"""
    out = []
    n = _norm(char_name)
    if len(n) < 2:
        return out
    for i, s in enumerate(ctx.scenes, 1):
        names = [str(x) for x in (s.get("characters") or [])]
        blob = " ".join(names + [str(s.get("hook") or ""), str(s.get("title") or ""),
                                 str(s.get("location") or "")])
        if any(_norm(x) and (_norm(x) in n or n in _norm(x)) for x in names) or n in _norm(blob):
            out.append({"scene_number": s.get("scene_number") or i, **s})
    return out


def brief(char: Dict[str, Any], ctx: Optional[ScriptContext] = None) -> Dict[str, Any]:
    """把人物卡 + 剧本上下文整理成一个**带来源**的选角 brief。

    这是 skill 里 "Input Handling" 那节的落地：把自由文本解析成
    identity / face / distinctive / hair_makeup / body_pose / wardrobe / treatment。
    """
    ctx = ctx or ScriptContext()
    char = char or {}
    slots = _slots_of(char)
    rf = _rf_of(char)
    name = str(char.get("name") or "角色")
    desc = str(char.get("description") or "").strip()

    def F(card_key: str = "", slot_key: str = "", script_val: str = "",
          script_ev: str = "", derived: str = "") -> Field:
        if card_key:
            v = str(char.get(card_key) or "").strip()
            if v:
                return Field(v, "card", card_key)
        if slot_key:
            v = str(slots.get(slot_key) or "").strip()
            if v:
                return Field(v, "slots", slot_key)
        if script_val:
            return Field(script_val, "script", script_ev)
        if derived:
            return Field(derived, "derived", "")
        return Field("", "", "")

    gender_raw = str(char.get("gender") or rf.get("gender") or "").strip()
    gender = _GENDER_ZH.get(gender_raw.lower(), gender_raw)
    age = F("age")
    role_raw = str(char.get("role") or rf.get("role") or "").strip()
    role = _ROLE_ZH.get(role_raw.lower(), role_raw)

    # 出场场次 —— 这是"跟剧本契合"最硬的依据
    in_scenes = _scenes_with(ctx, name)
    scene_desc = ""
    if in_scenes:
        bits = []
        for s in in_scenes[:3]:
            t = str(s.get("title") or "").strip()
            h = str(s.get("hook") or "").strip()
            bits.append("场景%s《%s》%s" % (s.get("scene_number"), t, h))
        scene_desc = "；".join(b for b in bits if b.strip("《》 "))
    role_in_story = F(script_val=scene_desc,
                      script_ev="剧本 %d 场里出现" % len(in_scenes))

    personality = F("personality")
    if not personality and rf.get("personality"):
        personality = Field(str(rf["personality"]), "slots", "reference_features.personality")
    relationships = Field(str(rf.get("relationships") or ""), "slots",
                          "reference_features.relationships")

    # ★★ 体型：**优先固定槽位**，退化到卡片自带的 body_type（2026-09-26 第一次真跑抓到的）
    #
    # 为什么不能直接用 `F("body_type", "height_build")`：`F()` 的优先级是写死的
    # "卡片字段优先"，于是只要有 `body_type`，提示词里的「体型：」就会写
    # `结实健壮`，而槽位里那份更具体的 `身材结实健壮，中等身高` **被顶掉了**。
    # 两个后果：
    #   ① 提示词少了一截（身高没了）——而出图一致性本来就该靠槽位；
    #   ② `recover_card_from_prompt()` 从提示词回读时只能拿回 `结实健壮`，
    #      与槽位原文**对不上**（tests/test_casting.py §6d 的往返断言因此红）。
    # ⇒ 按本文件 `personality` 那段的同一写法手工兜底。
    height_build_f = F(slot_key="height_build")
    if not height_build_f:
        height_build_f = F("body_type")

    return {
        "name": name,
        "gender": Field(gender, "card" if char.get("gender") else ("slots" if rf.get("gender") else ""),
                        "gender"),
        "age": age,
        "role": Field(role, "card" if char.get("role") else ("slots" if rf.get("role") else ""),
                      "role"),
        "role_raw": role_raw,
        # face：优先固定槽位（跨镜头一致性的基础），退化到 description 自由文本
        "face_shape": F(slot_key="face_shape"),
        "skin": F(slot_key="skin"),
        "eyes": F(slot_key="eyes"),
        "hair": F(slot_key="hair"),
        "distinctive": F(slot_key="distinctive"),
        "description": Field(desc, "card" if desc else "", "description"),
        "height_build": height_build_f,
        "costume_top": F(slot_key="costume_top"),
        "costume_bottom": F(slot_key="costume_bottom"),
        "costume_shoes": F(slot_key="costume_shoes"),
        "costume_main": F("costume_main"),
        "costume_alternate": F("costume_alternate"),
        "costume_accessories": Field(
            "、".join(map(str, slots.get("costume_accessories") or []))
            if isinstance(slots.get("costume_accessories"), list)
            else str(slots.get("costume_accessories") or ""),
            "slots", "costume_accessories"),
        "costume_palette": Field(
            "、".join(map(str, slots.get("costume_palette") or []))
            if isinstance(slots.get("costume_palette"), list)
            else str(slots.get("costume_palette") or ""),
            "slots", "costume_palette"),
        "signature": F(slot_key="signature"),
        "props": char.get("props") or [],
        "personality": personality,
        "relationships": relationships,
        "role_in_story": role_in_story,
        "in_scene_numbers": [s.get("scene_number") for s in in_scenes],
        # 上下文
        "era": ctx.era,
        "genre": ctx.genre,
        "world": ctx.world,
        "style": ctx.style,
        "has_reference_image": bool(str(char.get("reference_image_path") or "").strip()),
        "slots_present": bool(slots),
    }


# ──────────────────────── 模式选择 ────────────────────────


def pick_mode(brief_d: Dict[str, Any], ctx: Optional[ScriptContext] = None,
              rules: Optional[Dict[str, Any]] = None) -> str:
    """按题材/风格选主基调。skill 原文：没指定时按
    commercial → cinematic → advertising → casting 的顺序混合。"""
    rules = rules or load_rules()
    modes = rules.get("modes") or {}
    genre = str((ctx.genre if ctx else "") or brief_d.get("genre") or "")
    era = str((ctx.era if ctx else "") or brief_d.get("era") or "")
    style = str(brief_d.get("style") or "cinematic").lower()

    if genre in ("抗战/战争", "奇幻/史诗", "武侠/古装", "悬疑/犯罪") or \
            era in ("民国/抗战", "古代/王朝", "西式奇幻"):
        return "cinematic" if "cinematic" in modes else "commercial"
    if genre in ("都市/职场", "家庭/生活") or era in ("现代都市", "近代/民国都市"):
        return "commercial" if "commercial" in modes else "cinematic"
    if genre == "科幻":
        return "cinematic" if "cinematic" in modes else "commercial"
    if style in modes:
        return style
    blend = rules.get("mode_default_blend") or ["commercial", "cinematic"]
    for m in blend:
        if m in modes:
            return m
    return next(iter(modes), "commercial")


# ──────────────────────── 提示词构建（11 步顺序）────────────────────────


def _join(*xs: Any, sep: str = "，") -> str:
    return sep.join(str(x).strip() for x in xs if str(x or "").strip())


# 拼接前要去掉的句末标点。
# ★ 为什么必须去：角色卡的 `description` / `costume_main` 字段**自带句号**
#   （"…左脸颊有一条细长的旧伤疤。"），再和下一段用 "。" 拼起来就出现
#   "旧伤疤。。服装：" 这种双标点。提示词的标点质量会直接影响出图
#   （`videoprompt.py` 里有同样的教训与同样的处理：`_trim_end`）。
_PUNCT_TAIL = "。．.；;，,、！!？? \t\r\n"


def _trim_punct(s: Any) -> str:
    return str(s or "").strip().rstrip(_PUNCT_TAIL)


def _join_sentences(parts: Sequence[str]) -> str:
    """用句号拼句子，并在拼接前剪掉每段末尾已有的标点（避免 "。。"）。"""
    return "。".join(p for p in (_trim_punct(x) for x in parts) if p)


def _join_clean(sep: str, items: Sequence[Any]) -> str:
    """用 sep 拼接，**每段先剪掉末尾标点**。

    ★ 为什么每处都要剪：角色卡里的槽位值经常自带句号
      （"脸型：国字脸。"），用 "；" 拼起来就变成 "国字脸。；肤色肤质：…"。
      出图提示词里的标点质量会直接影响生成质量，而且这种"每段都带一个多余句号"
      在长提示词里会累积成几十处噪声。
    """
    return sep.join(p for p in (_trim_punct(x) for x in items) if p)


def quality_rules_zh(
    char: Optional[Dict[str, Any]] = None,
    *,
    ctx: Optional[ScriptContext] = None,
    mode: Optional[str] = None,
    rules: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """skill 里**面向中文出图模型**的那几条硬规则（rules.json 的 `*_zh` 字段）。

    ══════════════════════════════════════════════════════════════════
    为什么需要这个函数（2026-09-26）
    ══════════════════════════════════════════════════════════════════
    `rules.json` 里有三个中文规则字段：

        anti_gloss_rule_zh · neutral_skin_rule_zh · clean_cinematic.total_rule_zh

    而**没有任何代码读它们** —— 本模块 1137 行逐行核对过，
    拼提示词只用了英文串（`no_wax_negative` / `neutral_skin_constraints` / …）。
    `use_skill` 会把整份 rules.json 倒给**模型**，所以它们对模型可见，
    但**出图时完全不起作用**。

    ★ 这对「skill 学习」是**实打实的误导**：
      用户改 `neutral_skin_rule_zh`（以为这是"让脸不黄"的那个旋钮），
      出图行为一个字都不会变，然后合理地认为 skill 学习是坏的。
      一个没人读的"规则"比没有这条规则更糟。

    ★ 为什么中文版值得进提示词：
      出图模型是中文的（本地 qwen-image）。英文质量词它认，
      但 `neutral_skin_rule_zh` 那句「额头/鼻梁/颧骨/上唇不得出现亮白
      高光块」是**具体到部位**的约束 —— 比 `very low facial specular
      reflection` 更容易被准确执行，而"油腻高光/脸被染黄"正是实测里
      反复出现的退化。

    ★ 门控必须与英文串**一致**（不能出现"英文那条不适用、中文这条却生效"）：
      · 反光泽 / 中性肤色 —— 和 `no_wax_negative`、`neutral_skin_constraints`
        一样**始终生效**；
      · `clean_cinematic.total_rule_zh` —— 和同级的 `positive` / `negative`
        一样，**只在 mode 属于 `applies_to` 时生效**。

    兜底规则（`_FALLBACK_RULES`）里没有这些字段 → 返回空列表，
    调用方什么也不加（读不到 rules.json 也不会多出半句话）。
    """
    rules = rules or load_rules()
    if not mode:
        # 惰性：调用方（build_casting_prompt）已经算过 mode 就不重复算
        mode = pick_mode(brief(char or {}, ctx), ctx, rules)
    out: List[str] = []
    for key in ("anti_gloss_rule_zh", "neutral_skin_rule_zh"):
        v = str(rules.get(key) or "").strip()
        if v:
            out.append(v)
    cc = rules.get("clean_cinematic") or {}
    applies = cc.get("applies_to") or ["cinematic", "advertising", "commercial"]
    if mode in applies:
        v = str(cc.get("total_rule_zh") or "").strip()
        if v:
            out.append(v)
    return out


def build_casting_prompt(
    char: Dict[str, Any],
    *,
    ctx: Optional[ScriptContext] = None,
    style: str = "cinematic",
    view: str = "bust",
    mode: Optional[str] = None,
    rules: Optional[Dict[str, Any]] = None,
    include_negatives: bool = True,
) -> str:
    """按 skill 的 11 步顺序拼一条**选角级**角色图提示词。

    与旧的 `assets.build_portrait_prompt` 的区别：
      · 每条硬规则（NO WAX / 中性白平衡 / 干净电影感 / 原创性）都落到字面上
      · 带**剧本依据**：出场场次、题材年代、世界
      · 结尾的负向串是 skill 的原文串，不是临时编的
    """
    rules = rules or load_rules()
    b = brief(char, ctx)
    mode = mode or pick_mode(b, ctx, rules)
    modes = rules.get("modes") or {}
    mode_info = modes.get(mode) or {}
    parts: List[str] = []

    # 1) 原创虚构身份
    ident_bits = [f"《{b['name']}》"]
    head = _join(b["age"].value, b["gender"].value, b["role"].value, sep=" ")
    if head:
        ident_bits.append(head)
    if b["era"] and b["era"] != "未判定":
        ident_bits.append(f"{b['era']}背景")
    if b["genre"] and b["genre"] != "未判定":
        ident_bits.append(b["genre"] + "题材")
    parts.append("原创虚构人物的角色设定图：" + "，".join(ident_bits))

    # 2) 用户/剧本明确写了的特征（skill：只填空白，不改用户写的）
    explicit = []
    if b["description"]:
        explicit.append("外貌：" + b["description"].value)
    if b["costume_main"]:
        explicit.append("服装：" + b["costume_main"].value)
    if explicit:
        parts.append(_join_sentences(explicit))

    # 3) 面部结构与辨识细节（固定槽位优先 —— 跨镜头一致性的基础）
    face_bits = []
    slot_map = (("face_shape", "脸型"), ("skin", "肤色肤质"), ("eyes", "眼睛"),
                ("hair", "发型"), ("distinctive", "辨识特征"))
    for k, lab in slot_map:
        f: Field = b[k]
        if f:
            face_bits.append(f"{lab}：{f.value}")
    if face_bits:
        parts.append("面部与辨识细节（各镜头必须逐字保持不变）：" + _join_clean("；", face_bits))

    # 4) 体型与姿态
    pose_bits = []
    if b["height_build"]:
        pose_bits.append("体型：" + b["height_build"].value)
    pose_bits.append("姿态自然、受控，符合角色身份与年龄")
    parts.append(_join_clean("；", pose_bits))

    # 5) 完整服装与配饰
    cos = []
    for k, lab in (("costume_top", "上装"), ("costume_bottom", "下装"),
                   ("costume_shoes", "鞋")):
        f: Field = b[k]
        if f:
            cos.append(f"{lab}：{f.value}")
    if b["costume_accessories"]:
        cos.append("配饰：" + b["costume_accessories"].value)
    if b["costume_palette"]:
        cos.append("主色：" + b["costume_palette"].value)
    if cos:
        parts.append("完整可穿的服装：" + _join_clean("；", cos))
    alt = b["costume_alternate"]
    if alt:
        parts.append("备用服装：" + _trim_punct(alt.value))

    # 6) 摄影处理与光线 —— ★ 三条硬规则就在这里落地
    treatment = []
    if mode_info.get("positive"):
        treatment.append(mode_info["positive"])
    treatment.append(str(rules.get("positive_face_block") or ""))
    cc = rules.get("clean_cinematic") or {}
    applies = cc.get("applies_to") or ["cinematic", "advertising", "commercial"]
    if mode in applies and cc.get("positive"):
        treatment.append(str(cc["positive"]))
    treatment.append(str(rules.get("neutral_skin_constraints") or ""))
    parts.append("摄影与肤质要求（硬性）：" + _join_clean("，", treatment))

    # 6b) skill 的**中文**硬规则（rules.json 的 *_zh 字段）
    #     2026-09-26 补：这三个字段原来只对模型可见（`use_skill` 会把
    #     rules.json 倒给模型），但**从来没进过出图提示词** —— 见
    #     `quality_rules_zh()` 的说明。这里让它们真正生效。
    zh = quality_rules_zh(char, ctx=ctx, mode=mode, rules=rules)
    if zh:
        parts.append("肤质与色彩硬规则：" + _join_sentences(zh))

    # 7) 构图 / 视图
    view_map = {
        "bust": "正面半身像，单人，居中构图，简洁纯色背景",
        "full": "正面全身像，单人，站姿，简洁纯色背景",
        "fourview": "角色四视图设定稿（正面 / 侧面 / 背面 / 面部特写并排），"
                    "同一人物同一服装，比例一致，白色背景",
    }
    parts.append(view_map.get(view, view_map["bust"]))
    cam = rules.get("camera_split") or {}
    if view == "fourview":
        if cam.get("body_row"):
            parts.append("上排全身：%s" % cam["body_row"])
        if cam.get("face_closeup"):
            parts.append("下排面部特写：%s。%s"
                         % (cam["face_closeup"], cam.get("closeup_background") or ""))

    # 8) 身份一致性
    ident = ["同一人物在不同镜头间必须保持脸型、五官比例、发型、妆容、服装完全一致"]
    if b["signature"]:
        ident.append("识别锚点（各镜头必须保持不变）：" + b["signature"].value)
    parts.append(_join_clean("；", ident))

    # 9) 剧本依据（"跟剧本设定契合"的可核对部分）
    ground = []
    if b["role_in_story"]:
        ground.append("剧本中的戏份：" + b["role_in_story"].value)
    if b["world"]:
        ground.append("故事世界：" + b["world"])
    if b["personality"]:
        ground.append("气质（由剧本设定而来）：" + b["personality"].value)
    if b["relationships"]:
        ground.append("人物关系：" + b["relationships"].value)
    if ground:
        parts.append("剧本设定依据：" + _join_clean("；", ground))

    # 10) 批量差异（单个角色也要声明，避免每次出图都长成同一张 AI 脸）
    bd = rules.get("batch_diversity") or {}
    parts.append("辨识度要求：%s；面部由结构与比例产生辨识度，而不是靠夸张的美化"
                 % (bd.get("diversity_statement") or
                    "distinct unrelated individuals, not the same face"))

    # 11) 负向约束
    if include_negatives:
        neg = build_casting_negatives(char, ctx=ctx, mode=mode, rules=rules)
        if neg:
            parts.append("严格避免：" + neg)

    parts.append("画面中不要出现任何文字、水印、logo")
    return _join_sentences(parts)


def build_casting_negatives(
    char: Optional[Dict[str, Any]] = None,
    *,
    ctx: Optional[ScriptContext] = None,
    mode: Optional[str] = None,
    rules: Optional[Dict[str, Any]] = None,
) -> str:
    """按 skill 组装负向串：NO WAX 必带，时尚/广告再加抗光泽，最后是通用负向。"""
    rules = rules or load_rules()
    b = brief(char or {}, ctx)
    mode = mode or pick_mode(b, ctx, rules)
    neg: List[str] = []
    if rules.get("no_wax_negative"):
        neg.append(str(rules["no_wax_negative"]))
    if mode in ("advertising", "commercial") and rules.get("fashion_negative_extra"):
        neg.append(str(rules["fashion_negative_extra"]))
    if rules.get("negative_face_block"):
        neg.append(str(rules["negative_face_block"]))
    cc = rules.get("clean_cinematic") or {}
    if mode in (cc.get("applies_to") or []) and cc.get("negative"):
        neg.append(str(cc["negative"]))
    if rules.get("general_negative"):
        neg.append(str(rules["general_negative"]))
    return "，".join(dict.fromkeys(x.strip() for x in neg if str(x or "").strip()))


def build_refine_guard(
    char: Optional[Dict[str, Any]] = None,
    *,
    ctx: Optional[ScriptContext] = None,
    rules: Optional[Dict[str, Any]] = None,
) -> str:
    """给 **img2img 精修**用的「质感硬规则」串（正向 + 负向）。

    ══════════════════════════════════════════════════════════════════
    为什么需要单独一个函数（2026-09-26）
    ══════════════════════════════════════════════════════════════════
    `refine_look`（img2img 精修）原来**一条质感约束都没有**，只有
    「只修改这一处：X。必须保持不变：Y。不要重画整张图。」

    而精修是**重绘**：皮肤会被重新合成。没有约束时，
    蜡像感、油腻高光、脸被环境染黄都会被**带回来** ——
    那正是 character-casting-studio 要治的病。
    更糟的是精修结果会**覆盖** `reference_image_path`，
    于是退化版本成了角色的正式定妆照，再传到每一个镜头。

    ★ 两条最要紧的，缺一不可：
      · **正向**：`neutral_skin_constraints`（中性肤色白平衡 /
        极低镜面反射）—— 只给负向的话，"该是什么样"没有说，
        模型仍可能选一个偏黄偏亮的肤色。
      · **负向**：`build_casting_negatives()`（NO WAX SKIN 等）。

    ★ 为什么不直接把整条 `build_casting_prompt` 塞进去：
      精修的指令是「**只改这一处**」，塞进几百字的完整提示词会稀释这条
      核心指令，反而更容易被"重画整张图"。这里只取"别把质感改坏"那部分。
    """
    rules = rules or load_rules()
    parts: List[str] = []
    nsc = str(rules.get("neutral_skin_constraints") or "").strip()
    if nsc:
        parts.append(nsc)
    neg = build_casting_negatives(char, ctx=ctx, rules=rules)
    if neg:
        parts.append("严格避免：" + neg)
    return " ".join(parts)


# ──────────────── 被污染的卡：从提示词**回读**角色卡 ────────────────
#
# ★ 背景（docs 第 40 节「补 A」）：
#   出图路径曾经把**整条出图提示词**写进 `reference_features`（那本该是
#   角色卡的 JSON）。于是角色的 `slots` / `personality` / `relationships`
#   **全丢了** —— 而各消费方在 `json.loads` 失败后只会降级、不报错，
#   用户看到的是"角色越出越不像"。
#
#   源头已经修掉（不再写那两处），但**已经丢掉的卡不会自己回来**。
#   这个函数负责"尽量把它们读回来"：被写进去的那段提示词里
#   **恰好含这些字段的原文**。

#: 提示词里的中文标签 → 卡槽位键。与 `build_casting_prompt` 的第 3/4/5 步一一对应。
_RECOVER_LABELS: Dict[str, str] = {
    "脸型": "face_shape", "肤色肤质": "skin", "眼睛": "eyes", "发型": "hair",
    "辨识特征": "distinctive", "体型": "height_build",
    "上装": "costume_top", "下装": "costume_bottom", "鞋": "costume_shoes",
    "配饰": "costume_accessories", "主色": "costume_palette",
}
#: 这两个槽位在卡里是**列表**，提示词里用 `、` 拼
_RECOVER_LIST_SLOTS = ("costume_accessories", "costume_palette")


def recover_card_from_prompt(text: str) -> Dict[str, Any]:
    """从**被污染的** `reference_features`（其实是一整条出图提示词）里回读角色卡。

    提示词是 `build_casting_prompt()` 用 `。` 拼起来的，里面这些句子带原文：

        面部与辨识细节（各镜头必须逐字保持不变）：脸型：…；肤色肤质：…；…
        体型：…；姿态自然、受控，符合角色身份与年龄
        完整可穿的服装：上装：…；下装：…；鞋：…；配饰：…；主色：…
        识别锚点（各镜头必须保持不变）：…
        剧本设定依据：剧本中的戏份：…；故事世界：…；
                    气质（由剧本设定而来）：…；人物关系：…

    所以做法是：按 `。`/换行切句 → 每句按 `；` 切键值对 → 用 `_RECOVER_LABELS`
    认标签。**顺带**能处理"嵌套"那种（`剧本设定依据` 那句里还套着 `气质：…`）。

    返回::

        {"slots": {...}, "personality": "", "relationships": "",
         "found": ["脸型", …], "missing": ["下装", …], "ok": bool}

    ★ 这是**回读**，不是"重建"：读到什么就是什么，读不到就**如实留空**
      （绝不编一个默认值 —— 那会让体检把坏卡当成好卡）。
    ★ **回不来的字段**：`role` / `gender` —— 提示词里只有**译过的**中文
      （"主角" / "男性"），反推不可靠，所以不猜，列在 `missing` 里。
    """
    raw = str(text or "")
    slots: Dict[str, Any] = {}
    found: List[str] = []
    personality = ""
    relationships = ""

    for sentence in re.split(r"[。\n]", raw):
        # ★ 一层切分：每句按 `；` 拆键值对。
        #   这样既能处理"一整句就是键值对列表"（面部/服装那两句），
        #   也能处理"嵌套"（`剧本设定依据：…；气质（…）：…`）——
        #   后者的第 3 段起会自然变成 `气质（…）：…` 这种独立的键值对。
        for part in re.split(r"[；;]", sentence):
            if "：" not in part:
                continue
            k, _, v = part.partition("：")
            k = k.strip()
            v = _trim_punct(v)
            # ★★ 前缀节名要先剥掉（往返测试抓出来的真 bug）：
            #   `面部与辨识细节（各镜头必须逐字保持不变）：脸型：国字脸…`
            #   这种形状里**第一个 `：`** 切出来的是**节名**、不是槽位名。
            #   不剥的话，`脸型`（各句的**首个**键值对）会被整条丢掉 ——
            #   而 `face_shape` / `costume_top` 正好都在首位。
            #   剥法是：认不出 k、而 v 里还有 `：` 时，对后半段再切一次。
            for _ in range(3):
                if k in _RECOVER_LABELS or k.startswith(("识别锚点", "气质", "人物关系")):
                    break
                if "：" not in v:
                    break
                k, _, v = v.partition("：")
                k, v = k.strip(), _trim_punct(v)
            if not k or not v:
                continue
            key = _RECOVER_LABELS.get(k)
            if key:
                if key in _RECOVER_LIST_SLOTS:
                    items = [x.strip() for x in re.split(r"[、,]", v) if x.strip()]
                    if items:
                        slots[key] = items
                        found.append(k)
                else:
                    slots[key] = v
                    found.append(k)
            elif k.startswith("识别锚点"):
                slots["signature"] = v
                found.append("识别锚点")
            elif k.startswith("气质"):
                personality = v
                found.append("气质")
            elif k.startswith("人物关系"):
                relationships = v
                found.append("人物关系")

    missing = [lab for lab, key in _RECOVER_LABELS.items() if key not in slots]
    if "signature" not in slots:
        missing.append("识别锚点")
    return {
        "slots": slots,
        "personality": personality,
        "relationships": relationships,
        "found": found,
        "missing": missing,
        # `ok` = 至少回读到**一个**槽位（够不够用由调用方自己判断，
        # 这里不替它下结论 —— 只报"读没读到东西"）
        "ok": bool(slots),
    }


# ──────────────────────── 质量门 / 契合审计 ────────────────────────


def audit(char: Dict[str, Any], prompt: str = "",
          *, ctx: Optional[ScriptContext] = None,
          view: str = "bust",
          rules: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """对着 skill 的 Quality Gate 出一条**带证据**的体检报告。

    返回：
      conformance  逐项 {ok, detail}      —— 合规项
      mismatches   与剧本设定冲突的项（"契合"失败）
      gaps         数据缺口（该有但没有的字段）—— 不是错误，是"还差什么"
      score        合规项通过率（只用于排序，不是结论）

    ★ 这个函数**不阻断**出图。原因见模块 docstring：会误判的检查不该有否决权。
      调用方可以据此提醒用户，但不能因此拒绝生成。
    """
    rules = rules or load_rules()
    b = brief(char, ctx)
    p = str(prompt or "")
    conformance: Dict[str, Dict[str, Any]] = {}
    mismatches: List[Dict[str, str]] = []
    gaps: List[str] = []
    notes: List[str] = []

    if rules.get("_fallback"):
        notes.append("★ 规则用的是内置兜底，不是 skill 的 rules.json（原因：%s）"
                     % rules.get("_fallback_reason", "未知"))

    # ── 合规项（必须逐条给出可核对的证据片段）──
    def need(key: str, label: str, needle: str) -> None:
        has = bool(needle) and needle.lower() in p.lower()
        conformance[label] = {
            "ok": has,
            "detail": ("提示词里含『%s…』" % needle[:48]) if has
                      else "提示词里**找不到**『%s…』" % needle[:48],
        }

    need("no_wax", "NO WAX SKIN 负向约束在提示词里",
         (rules.get("no_wax_negative") or "").split(",")[0].strip())
    need("neutral", "中性白平衡 / 极低镜面反射约束在提示词里",
         (rules.get("neutral_skin_constraints") or "").split(",")[0].strip())
    face_block = (rules.get("positive_face_block") or "").split(",")[0].strip()
    need("face_block", "真实人脸质感正向块在提示词里", face_block)

    # 反光泽：被禁的词**不能**出现在正向描述里
    banned = rules.get("anti_gloss_banned_terms") or []
    hit = [t for t in banned if t and t.lower() in p.lower()]
    conformance["anti_gloss"] = {
        "ok": not hit,
        "detail": "没有出现被禁的光泽词" if not hit
                  else "★ 出现了被 skill 明令禁止的光泽词：%s" % "、".join(hit),
    }

    # 原创性
    idsafe = rules.get("identity_safety") or {}
    must_not = idsafe.get("must_not_reproduce") or []
    conformance["identity_safety"] = {
        "ok": "celebrity likeness" in p.lower() or "no celebrity likeness" in p.lower()
              or not p,
        "detail": ("提示词含 no celebrity likeness 一类的原创性约束" if p else "未提供提示词，跳过"),
    }

    # ── 数据完整性（gaps，不是错误）──
    face_fields_present = [k for k in ("face_shape", "skin", "eyes", "hair")
                           if b[k]]
    fr = rules.get("face_realism") or {}
    need_n = int(fr.get("min_feature_fields_replaced") or 3)
    conformance["face_fields"] = {
        "ok": len(face_fields_present) >= need_n,
        "detail": "有 %d/%d 个五官字段（%s）" % (
            len(face_fields_present), need_n, "、".join(face_fields_present) or "无"),
    }
    if len(face_fields_present) < need_n:
        gaps.append(
            "五官字段只有 %d 个（skill 要求 ≥%d 个才有跨镜头一致性和辨识度）：%s。"
            "建议在角色卡的 slots 里补全 face_shape/skin/eyes/hair。"
            % (len(face_fields_present), need_n,
               "、".join(sorted(set(fr.get("feature_fields") or []))[:4])))

    anchors = [k for k in ("distinctive", "signature") if b[k]]
    need_a = int(fr.get("min_identity_anchors_replaced") or 1)
    conformance["identity_anchor"] = {
        "ok": len(anchors) >= need_a,
        "detail": "有 %d 个身份锚点（%s）" % (len(anchors), "、".join(anchors) or "无"),
    }
    if len(anchors) < need_a:
        gaps.append("缺少身份锚点（skill 要求 ≥%d 个）：建议补 distinctive（痣/疤/皱纹/不对称）"
                    "或 signature（标志性配饰/发型）。" % need_a)

    if not b["costume_main"] and not b["costume_top"]:
        gaps.append("没有写服装 —— 出图会自由发挥，跨镜头一致性必然崩。")
    if not b["age"]:
        gaps.append("没有写年龄 —— skill 要求保留与年龄相符的比例"
                    "（成年/老年/少年/儿童），缺了会漂。")

    # ── 与剧本设定的契合（这才是"契合"检查的正题）──
    era = str(b.get("era") or "未判定")
    cos_text = " ".join(str(b[k].value) for k in
                        ("costume_main", "costume_top", "costume_bottom",
                         "costume_shoes", "costume_accessories", "costume_alternate"))
    desc_text = str(b["description"].value)

    if era in _ANACHRONISM:
        bad = [w for w in _ANACHRONISM[era] if w in cos_text]
        if bad:
            mismatches.append({
                "kind": "服装与年代冲突",
                "detail": "剧本判定为『%s』，但服装/配饰里出现：%s" % (era, "、".join(bad)),
                "evidence": "；".join((ctx.era_evidence if ctx else [])[:3]) or "（无证据）",
                "fix": "改服装，或如果年代判定错了，请在剧本里写明年代",
            })
        good = [w for w in _ERA_WARDROBE.get(era, ()) if w in cos_text]
        if cos_text.strip() and not good:
            notes.append("服装里没有出现任何『%s』的年代锚点词（%s），"
                         "请人工确认造型是否符合剧本。"
                         % (era, "、".join(_ERA_WARDROBE.get(era, ())[:5])))

    if era == "未判定":
        notes.append("★ 年代未判定，无法检查服装是否与时代冲突。"
                     "要拿到这项检查，请在剧本里写出可判定的年代线索。")

    # 年龄漂移：剧本/卡里写 40，提示词里却写 30
    age_m = re.search(r"(\d{1,3})", str(char.get("age") or ""))
    if age_m and p:
        stated = int(age_m.group(1))
        in_prompt = [int(x) for x in re.findall(r"(\d{1,3})\s*岁", p)]
        wrong = [x for x in in_prompt if abs(x - stated) >= 5]
        conformance["age_consistent"] = {
            "ok": not wrong or stated in in_prompt,
            "detail": ("角色卡写 %d 岁，提示词里出现 %s" % (stated, in_prompt))
                      if in_prompt else "提示词里没有写具体年龄",
        }
        if wrong and stated not in in_prompt:
            mismatches.append({
                "kind": "年龄漂移",
                "detail": "角色卡写 %d 岁，提示词里写成 %s 岁" % (stated, wrong),
                "evidence": "角色卡 age=%s" % char.get("age"),
                "fix": "提示词必须逐字使用角色卡的年龄",
            })

    # 只在某些场次出现，却完全没有该场次的造型线索
    if ctx and ctx.scenes and not b["in_scene_numbers"]:
        notes.append("这个角色在剧本的 %d 个场次里都没有被提到（按名字匹配）。"
                     "可能是剧本里用了别名，或这个角色是新增的 —— 请确认。"
                     % len(ctx.scenes))

    total = len(conformance)
    passed = sum(1 for v in conformance.values() if v.get("ok"))
    return {
        "score": round(passed / total, 3) if total else 0.0,
        "passed": passed, "total": total,
        "conformance": conformance,
        "mismatches": mismatches,
        "gaps": gaps,
        "notes": notes,
        "era": era, "genre": b.get("genre"), "mode": pick_mode(b, ctx, rules) if p else "",
        "grounded": bool(ctx and ctx.is_grounded()),
        "ok": (not mismatches) and passed == total,
        "rules_source": rules.get("_source") or ("内置兜底" if rules.get("_fallback") else "?"),
    }


# ──────────────────────── 批量差异（多角色别长成一个样）────────────────────────


def batch_report(characters: Sequence[Dict[str, Any]],
                 *, ctx: Optional[ScriptContext] = None,
                 rules: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """检查一批角色的辨识度是否真的分开了。

    skill 原文要求相邻角色至少改 **5 个维度**，禁止整套复用
    「oval face + straight black hair + almond eyes + high cheekbones + neutral」。
    这里就按那套维度给每个角色做指纹，然后两两比。
    """
    rules = rules or load_rules()
    bd = rules.get("batch_diversity") or {}
    min_diff = int(bd.get("min_dimensions_changed_between_adjacent") or 5)

    DIMS = ("face_shape", "eyes", "hair", "skin", "distinctive",
            "height_build", "costume_top")

    fps = []
    for c in characters:
        b = brief(c, ctx)
        vals = {d: _norm(b[d].value) for d in DIMS}
        fps.append({"id": c.get("id"), "name": b["name"],
                    "age": b["age"].value, "role": b["role"].value,
                    "fingerprint": vals,
                    "filled": sum(1 for v in vals.values() if v)})

    pairs = []
    for i in range(len(fps)):
        for j in range(i + 1, len(fps)):
            a, b2 = fps[i], fps[j]
            same = [d for d in DIMS
                    if a["fingerprint"][d] and a["fingerprint"][d] == b2["fingerprint"][d]]
            # 两边都空不算"相同"，那是"都没填"
            diff = len([d for d in DIMS
                        if a["fingerprint"][d] and b2["fingerprint"][d]
                        and a["fingerprint"][d] != b2["fingerprint"][d]])
            only_one = [d for d in DIMS if bool(a["fingerprint"][d]) != bool(b2["fingerprint"][d])]
            pairs.append({
                "a": a["name"], "b": b2["name"],
                "same_fields": same, "diff_fields": diff,
                "undecidable": len(only_one),
                "risky": bool(same) and diff < min_diff,
            })

    risky = [p for p in pairs if p["risky"]]
    thin = [f["name"] for f in fps if f["filled"] < 3]
    out: Dict[str, Any] = {
        "characters": len(fps), "pairs": len(pairs),
        "min_dimensions_required": min_diff,
        "risky_pairs": risky,
        # ★ 明细要留着：只给一个"有 3 对有风险"的数字，
        #   调用方没法知道是哪几对、撞在哪 —— 那就成了一句正确的废话。
        "pair_details": pairs,
        "thin_fingerprints": thin,
        "fingerprints": fps,
        "ok": not risky and not thin,
    }
    if risky:
        out["advice"] = (
            "有 %d 对角色在「%s」上撞了且差异不足 %d 个维度，会出成兄弟姐妹脸。"
            "建议给其中辨识度更低的那个换脸型/眼型/发型/辨识特征。"
            % (len(risky), "、".join(sorted({d for p in risky for d in p["same_fields"]})),
               min_diff))
    if thin:
        out["advice"] = (out.get("advice", "") +
                         (" 另有 %d 个角色槽位太少（<3 个维度有值），无法判断差异，"
                          "请先在角色卡里补全。" % len(thin))).strip()
    return out


def context_from_db(db: Any, project_id: str) -> ScriptContext:
    """从数据库取齐数据并拼成 ScriptContext（**唯一**碰 db 的地方）。

    ★ 这是本模块里唯一的非纯函数，单独放在末尾，方便审查：
      其余全部逻辑都是纯函数，可以在没有数据库的情况下测试。
      任何一步取数失败都只让对应部分为空，并记进 `_notes` ——
      **不会**因为"读不到剧本细节"就让整个角色生成失败。
    """
    project = script = settings = None
    scene_details: List[Dict[str, Any]] = []
    characters: List[Dict[str, Any]] = []
    notes: List[str] = []
    try:
        project = db.get_project(project_id)
    except Exception as e:
        notes.append("读项目失败：%s" % e)
    try:
        script = db.get_script(project_id)
    except Exception as e:
        notes.append("读剧本失败：%s" % e)
    try:
        settings = db.get_all_settings()
    except Exception as e:
        notes.append("读设置失败：%s" % e)
    try:
        scene_details = db.list_scene_details(project_id) or []
    except Exception as e:
        notes.append("读场次细纲失败：%s" % e)
    try:
        characters = db.list_characters(project_id) or []
    except Exception as e:
        notes.append("读角色失败：%s" % e)

    ctx = build_script_context(project=project, script=script,
                               scene_details=scene_details, characters=characters,
                               settings=settings)
    ctx._notes = list(ctx._notes) + notes
    return ctx


# ──────────────────────── 给 agent 看的一行摘要 ────────────────────────


def summarize(audit_result: Dict[str, Any]) -> str:
    """把审计结果压成一句人话，避免把整个 JSON 倒给模型。"""
    r = audit_result
    bits = ["合规 %d/%d" % (r.get("passed", 0), r.get("total", 0))]
    if r.get("mismatches"):
        bits.append("与剧本冲突 %d 处：%s" % (
            len(r["mismatches"]), "；".join(m["detail"] for m in r["mismatches"][:2])))
    if r.get("gaps"):
        bits.append("数据缺口 %d 项" % len(r["gaps"]))
    if r.get("era") and r["era"] != "未判定":
        bits.append("年代=%s" % r["era"])
    return "；".join(bits)
