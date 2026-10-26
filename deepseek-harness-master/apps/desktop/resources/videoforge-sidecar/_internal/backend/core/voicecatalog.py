# -*- coding: utf-8 -*-
r"""按**角色卡 + 剧本情景**选音色 —— 而不是"8 个固定声音按名字发牌"。

用户原话：「不是你那几个固定的 NPC 发音。配音没这么简单的」。

查出来的事实（`tests/probe_voice_catalog.py`）：
  · 工具其实能列到 **52 个音色**（edge 22 / minimax 12 / dashscope 10 /
    siliconflow 8），中文 **44 个（男 17 / 女 27）**；
  · 而 `voicecast` 里我硬编码的池子只有 **8 个**（男 4 / 女 4）——
    选角靠"角色名哈希取模"，所以同一个性别+年龄段的角色拿到什么是听天由命；
  · 角色卡其实**信息很全**：`age`（"40岁上下"）、`role`（protagonist/antagonist/
    minor）、`personality`（很多卡直接把说话方式写进去了：
    "说话语气温和但坚定，略带沧桑" / "说话语气强硬且带煽动性" /
    "语气低沉而带有威胁性" / "说话语气缓慢且带着犹豫"）、还有外形描述。

这个模块做三件事（全部是**纯函数**，可单测）：
  ① `parse_voice_attrs(label)`：把音色的中文名解析成**年龄段 + 气质标签**
     （"晓梦 · 儿童女声" → child + [儿童]；"龙老铁 · 东北男声" → adult + [东北]）；
  ② `role_profile(char)`：从角色卡算出**这个角色该怎么说话**
     （性别/年龄段/角色定位/气质标签/语速倾向/音高倾向）；
  ③ `cast_from_catalog(...)`：把两者配上，并给出**可读的理由**（哪几个标签命中了）。

设计原则（沿用这个项目反复吃过的教训）：
  · **绝不自己编音色 id** —— 候选全部来自各厂商真实的 `list_voices()`；
  · 属性解析只依据**音色自己的名字/标签**，解析不出就留空、不硬猜；
  · 命中不了就退到"同性别+同年龄段"，再不行退到"同性别"，全程可解释。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ── 音色名 → 年龄段 ────────────────────────────────────────────
#   只放**字面就写了年龄**的词（儿童/少年/青年/成熟/老年）。
#   像"阳光/文艺/甜美"这种是**气质**，不是年龄 —— 它们走下面的
#   `_TAG_TO_BAND` 推断路径，并标明"推断"。分成两条路是为了让
#   "音色自己写的"和"我们推的"不混在一起（否则理由里会说不清）。
_VOICE_AGE_PATTERNS: List[Tuple[str, str]] = [
    ("child", r"(儿童|童趣|童声|幼|宝宝)"),
    ("teen", r"(少年|少女|学生)"),
    ("young", r"(青年|青春)"),
    ("mature", r"(成熟|中年|知性)"),
    ("senior", r"(老年|苍老|沧桑)"),
]

# ── 音色名 → 气质标签（我这边统一的标签词表）────────────────────
_VOICE_TAGS: List[Tuple[str, str]] = [
    ("温柔", r"(温柔|亲切|亲和)"),
    ("甜美", r"(甜美|可爱|萌)"),
    ("沉稳", r"(沉稳|成熟|知性|睿智)"),
    ("有力", r"(有力|浑厚|磁性|雄浑)"),
    ("播报", r"(新闻|播音|专业|纪录)"),
    ("叙述", r"(有声书|评书|旁白|叙事)"),
    ("阳光", r"(阳光|活力|青春|少年)"),
    ("文艺", r"(文艺|抒情)"),
    ("方言", r"(东北|陕西|四川|粤语|方言|河南|湖南)"),
    ("情感", r"(情感|多情感|激情)"),
    ("客服", r"(客服|助手|智能)"),
    ("阴冷", r"(阴冷|冷酷|低沉|沙哑|烟嗓)"),
    ("苍老", r"(苍老|老年|沧桑)"),
    ("童趣", r"(儿童|童趣|萌娃)"),
]

# ── 角色卡的 personality/description → 说话方式与气质倾向 ────────
#   注意：这里**只认卡里真的写了的话**。很多卡把"说话语气…"直接写进了
#   personality，所以这些词命中率很高（见 §26 的真实样本）。
_TRAIT_RULES: List[Tuple[str, str]] = [
    ("沉稳", r"(沉着|冷静|沉稳|老练|沉默寡言|内敛|谨慎)"),
    ("强硬", r"(强硬|凶狠|残暴|傲慢|自大|固执|威胁|压迫|有威胁)"),
    ("温和", r"(温和|善良|同情|温和但坚定|亲切)"),
    ("沧桑", r"(沧桑|缓慢|犹豫|无奈|饱经)"),
    ("有力", r"(简洁有力|果断|激励|煽动|号召|坚定)"),
    ("紧张", r"(紧张|稚嫩|服从|缺乏主动)"),
    ("直爽", r"(直爽|直来直去|精明|务实)"),
    ("阴冷", r"(阴冷|冷酷|狡猾|善于伪装|危险|刺客)"),
    ("热血", r"(热血|勇敢|冲锋|战斗经验|无畏)"),
]

# ── 角色定位 → 音色气质偏好 ────────────────────────────────────
_ROLE_PREF: Dict[str, List[str]] = {
    "protagonist": ["有力", "沉稳", "温柔", "播报", "阳光"],
    "antagonist": ["阴冷", "强硬", "沉稳", "有力"],
    "supporting": ["沉稳", "温柔", "叙述", "阳光"],
    "minor": ["温柔", "沉稳", "阳光", "叙述"],
}

# ── 气质标签 → 年龄段（**保守推断**，只在名字没写年龄时用）────────
#   依据是标签本身的语义。标出来是为了让理由里能写"年龄段（推断）"。
_TAG_TO_BAND: List[Tuple[str, str]] = [
    ("童趣", "child"),
    ("阳光", "young"),
    ("文艺", "young"),
    ("情感", "young"),
    ("沉稳", "mature"),
    ("播报", "mature"),
    ("叙述", "mature"),
    ("苍老", "senior"),
]


def parse_voice_attrs(label: str, gender: str = "") -> Dict[str, Any]:
    """从音色的**中文名/标签**解析出年龄段与气质标签（纯函数）。

    ★ 名字里没写年龄时，用**气质标签保守推断**年龄段 —— 这一步很关键：
      minimax / siliconflow / dashscope 的很多音色名不带"青年/成熟"字样
      （"低沉男声 alex"、"知性女声"），不推断的话它们永远拿不到年龄分，
      于是"已验证的 edge 音色"会仅凭加分就通吃 —— 实测就是这样把
      「50 岁沉默寡言的中年大叔」配成了「云健 · 体育男声」。
      推断依据是标签本身的语义（沉稳→中年、阳光→青年…），并标明是推断。
    """
    t = str(label or "")
    band = ""
    for b, pat in _VOICE_AGE_PATTERNS:
        if re.search(pat, t):
            band = b
            break
    tags = [name for name, pat in _VOICE_TAGS if re.search(pat, t)]
    inferred = ""
    if not band:
        for tag, b in _TAG_TO_BAND:
            if tag in tags:
                inferred = b
                break
    return {"age_band": band or inferred, "age_inferred": bool(inferred and not band),
            "tags": tags, "gender": (gender or "").lower()}


def age_band_from_text(*texts: Any) -> str:
    """从角色卡的年龄描述里读年龄段（支持"40岁上下"/"20-30岁"/"无明确年龄"）。

    读不出返回 ""（表示"不确定"，而不是硬猜一个）。

    ★ 2026-09-14 补：**光认数字是不够的**。真实角色卡里写的是
      `age = "少年"`（布兰）这种**年龄词**，而这里以前只 `re.findall(r"\\d{1,2}")`
      —— 于是「布兰（少年）」的年龄段是空的，选角时年龄分完全不起作用：
      实测把它配成了「成熟男声」，而「无面者刺客（成熟）」拿到了「少年男声」，
      两个人的嗓子正好**对调**。现在先看数字，没有再认年龄词。
    """
    blob = " ".join(str(x) for x in texts if x)
    # 明确说没有年龄 → 不确定
    if re.search(r"(无明确年龄|不明确|各年龄段|未知)", blob):
        return ""
    nums = [int(x) for x in re.findall(r"(\d{1,2})", blob)]
    if nums:
        n = sum(nums) / len(nums)          # "20-30岁" 取中值
        if n <= 12:
            return "child"
        if n <= 17:
            return "teen"
        if n <= 29:
            return "young"
        if n <= 49:
            return "adult"
        if n <= 59:
            return "mature"
        return "senior"
    # 没有数字 → 认年龄词（长者优先匹配，避免"老年"被"年"截胡）
    for pat, band in _AGE_WORD_BANDS:
        if re.search(pat, blob):
            return band
    return ""


# 年龄词 → 年龄段（顺序即优先级：更具体/更老的在前）
_AGE_WORD_BANDS: Tuple[Tuple[str, str], ...] = (
    (r"(儿童|孩童|幼童|小孩|孩子)", "child"),
    (r"(少年|少女|青春期|十几岁)", "teen"),
    (r"(青年|年轻人|小伙子|姑娘|少女感)", "young"),
    (r"(中年|中青年|壮年|不惑)", "adult"),
    (r"(老年|苍老|花甲|古稀|老者|老太太|老汉)", "senior"),
)


def role_profile(char: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """从角色卡算出"这个角色该怎么说话"。纯函数，可单测。

    取值顺序：`reference_features` 槽位（personality/role/gender）→ 顶层字段
    （age/body_type/description/name）→ 都读不到就留空（不硬猜）。
    """
    c = char or {}
    slots: Dict[str, Any] = {}
    rf = c.get("reference_features")
    if isinstance(rf, str):
        import json
        try:
            slots = json.loads(rf) or {}
        except Exception:
            slots = {}
    elif isinstance(rf, dict):
        slots = rf

    gender = str(slots.get("gender") or c.get("gender") or "").strip().lower()
    if gender in ("男", "男性"):
        gender = "male"
    if gender in ("女", "女性"):
        gender = "female"
    is_group = gender in ("mixed", "群体", "group") or bool(
        re.search(r"(们|群|众|队伍|战士們|战士们)$", str(c.get("name") or "").strip()))
    # ★ 群体角色（"村民们""战士们"）**不能**当普通个体处理：
    #   实测把「村民们」配成了一把具体女声（"龙婧 · 知性女声"），
    #   而剧本里那是一群人。这里标记出来，理由里如实写明是"群体代表音色"。
    if gender in ("mixed", "群体", "group"):
        gender = ""
    if gender not in ("male", "female"):
        gender = ""
    # ★★ 角色卡没写性别时，从**角色名里的身份词**兜一层（2026-09-14）。
    #   实测：角色卡「水手」没写性别 → 性别是空的 → 打分时性别过滤失效，
    #   结果把水手配成了**女声**（晓伊）。而"水手/士兵/刺客/国王/领主"这些
    #   词本身就带性别信息，用它兜底比"随便挑一把"准确得多。
    #   这只是**兜底**：卡片里写了性别永远优先，而且理由里会写明是推断的。
    gender_inferred = ""
    if not gender:
        _n = str(c.get("name") or "") + " " + str(slots.get("role") or "")
        for pat, g in _NAME_GENDER_HINTS:
            if re.search(pat, _n):
                gender = g
                gender_inferred = g
                break

    # ★★ 2026-09-27 第 71 轮：名字里没有线索时，**从角色卡正文**继续推。
    #   为什么必须补这一层（用户实测原话：「配音…声音跟人物形象对不上」）：
    #   实测《执棋者》里 60 岁的「老者」（描述写着"**胡须剃得干净**"）
    #   名字不带性别线索 ⇒ 性别判空 ⇒ 打分时的性别硬过滤**整个失效** ⇒
    #   最后按"气质"挑到了 **minimax 成熟女声**。同一批里「李队长」
    #   （"**胡茬明显**…肌肉结实、肩背宽阔"）也没被认出来。
    #   而正文里的外貌/服装描述其实**非常明确**（胡须/喉结/齐肩发/旗袍…），
    #   用它兜底比"按气质猜"准得多，而且理由里会写明是**推断**的。
    gender_evidence = ""
    if not gender:
        _body = " ".join(str(x) for x in (
            c.get("description"), c.get("body_type"), c.get("costume_main"),
            c.get("props"), " ".join(str(a) for a in (c.get("aliases") or [])
                                     if not isinstance(a, (dict, list))) if c.get("aliases") else "",
        ) if x)
        for pat, g in _BODY_GENDER_HINTS:
            mm = re.search(pat, _body)
            if mm:
                gender = g
                gender_inferred = g
                gender_evidence = mm.group(0)
                break

    blob = " ".join(str(x) for x in (
        slots.get("personality"), slots.get("role"),
        c.get("personality"), c.get("description"), c.get("body_type"),
        c.get("age"), c.get("name"), c.get("props"), c.get("costume_main")) if x)

    band = age_band_from_text(c.get("age"), slots.get("age"), c.get("description"))
    traits = [name for name, pat in _TRAIT_RULES if re.search(pat, blob)]
    role = str(slots.get("role") or c.get("role") or "").strip().lower()
    if role not in _ROLE_PREF:
        role = ""
    # 从 personality 里读**显式**的语速/语气要求（很多卡真的写了）
    want_slow = bool(re.search(r"(缓慢|低沉|犹豫|拖长|稳重)", blob))
    want_fast = bool(re.search(r"(急促|快速|连珠|激动|煽动)", blob))
    return {"gender": gender, "age_band": band, "role": role, "traits": traits,
            "want_slow": want_slow, "want_fast": want_fast, "is_group": is_group,
            "gender_inferred": gender_inferred,
            "gender_evidence": gender_evidence,
            "evidence": blob[:120]}


# 角色名/身份词里的性别线索（**只在角色卡没写性别时**兜底用，顺序即优先级）
_NAME_GENDER_HINTS: Tuple[Tuple[str, str], ...] = (
    (r"(国王|王子|老爷|领主|首领|族长|将军|统领|大臣|长者|父|伯|叔|爷|先生|"
     r"士兵|战士|侍卫|卫兵|猎人|水手|船夫|农夫|铁匠|车夫|刺客|剑客|和尚|道士|"
     r"少年|男孩|小伙子|哥|弟|叔|"
     # ★ 2026-09-27 第 71 轮补：**兵卒类**称谓。
     #   实测《执棋者》的「兵丁甲」既没被名字线索认出来、模型也判了"不确定"
     #   ⇒ 落到"按气质挑" ⇒ 配成了 **minimax 成熟女声**（一个兵丁是女声）。
     #   这类词在古装/战争戏里高频出现，必须覆盖。
     r"兵丁|兵卒|小兵|亲兵|家丁|家兵|衙役|捕快|差役|更夫|船工|镖师|武师)", "male"),
    (r"(女王|王后|公主|夫人|太太|母亲|婆婆|老妇|少女|女孩|姑娘|小姐|侍女|婢女|"
     r"护士|女官|姐|妹|娘|嫂|嬷嬷)", "female"),
)

#: **从角色卡正文（外貌/体型/服装/道具）推性别**的线索词。
#: 2026-09-27 第 71 轮加：名字不带性别线索时靠它兜底。
#: ★ 只放**指向性很强**的词，不放"短发"这种男女都可能的（宁可判不出来，
#:   也不要判错 —— 判错就是把男角色配成女声，那正是用户报的问题）。
#: 第一层：明确到几乎不会错的；顺序即优先级。
_BODY_GENDER_HINTS: Tuple[Tuple[str, str], ...] = (
    # ── 男性：胡须 / 喉结 / 男性专属服饰与体格 ──
    (r"(胡须|胡茬|胡子|络腮|八字须|山羊胡|喉结|剃得干净|军装|迷彩|肩背宽阔|"
     r"肌肉结实|壮实|矮壮|魁梧|精瘦的汉子|汉子|老头|老者|老丈|大爷|大叔|"
     r"小伙|少年|男孩|兄弟|父亲|爷爷|外公|叔叔|舅舅|哥哥|弟弟|儿子|"
     r"兵丁|兵卒|小兵|亲兵|家丁|衙役|捕快|差役)", "male"),
    # ── 女性：发型长度 / 妆容 / 女性专属服饰与亲属称谓 ──
    (r"(齐肩发|及腰长发|长发|披肩发|发尾|马尾|辫子|辫|刘海儿|妆容|口红|睫毛|"
     r"裙|旗袍|连衣裙|高跟|丝袜|胸针|耳环|发簪|少女|姑娘|女子|女孩|妇人|"
     r"少妇|女士|母亲|妈妈|奶奶|外婆|阿姨|姐姐|妹妹|女儿|妻子|夫人|太后|"
     r"公主|王后|皇后|贵妃|娘子|庶女|嫡女|养女|义女|孤女|幼女|闺女|"
     r"儿媳|媳妇|弟妹|嫂子|寡妇|道姑|尼姑)", "female"),
)


def score_voice(profile: Dict[str, Any], voice: Dict[str, Any]) -> Tuple[float, List[str]]:
    """给一个候选音色打分，返回 (分数, 命中理由)。纯函数。

    打分是**可解释**的：理由列表里会写清是哪些标签/年龄段命中的，
    用户看到的是"为什么给这个角色选了这把嗓子"，而不是一个黑盒结果。
    """
    why: List[str] = []
    score = 0.0
    # 性别：硬条件（写明了就不接受相反的）
    if profile.get("gender"):
        if voice.get("gender") and voice["gender"] != profile["gender"]:
            return -1e9, ["性别不符"]
        if voice.get("gender") == profile["gender"]:
            score += 40
            why.append("性别相符")
    # 年龄段
    pa, va = profile.get("age_band") or "", voice.get("age_band") or ""
    if pa and va:
        if pa == va:
            score += 26
            why.append(f"年龄段相符（{pa}）")
        else:
            order = ("child", "teen", "young", "adult", "mature", "senior")
            d = abs(order.index(pa) - order.index(va)) if pa in order and va in order else 9
            score += max(0.0, 14.0 - 7.0 * d)
            if d <= 1:
                why.append(f"年龄段相邻（要 {pa}，得 {va}）")
    elif not pa:
        score += 4                      # 年龄不确定 → 不奖不罚
    # 气质标签
    tags = set(voice.get("tags") or [])
    for t in profile.get("traits") or []:
        if t in tags:
            score += 12
            why.append(f"气质相符（{t}）")
    for t in _ROLE_PREF.get(profile.get("role") or "", []):
        if t in tags:
            score += 5
            why.append(f"适合{profile.get('role')}（{t}）")
    if profile.get("want_slow") and "苍老" in tags:
        score += 4
    if profile.get("want_fast") and "阳光" in tags:
        score += 3
    # ── 引擎偏好（2026-09-27 第 71 轮**改了方向**）────────────────────
    # 原来这里是「**免费引擎**轻微优先（edge +2.5）」—— 结果整片里
    # 大量角色落到 Edge（`edge:zh-CN-YunxiNeural` 之类），
    # 而 Edge **不支持 emotion 参数**（见 core/voice/edge_tts.py），
    # 于是剧本写着情绪也是平铺直叙 —— 用户的原话就是
    # 「发声太AI了」「AI感太重」，并且明说「你可以换渠道比如硅基流动
    # 或者 minimax 模型花钱都可以」。
    # ⇒ 改成**按质量排序**：有付费高质引擎就优先用它。
    #   权重给 8：足够打破"适配度相同"时的平手，但**压不过**性别(40)/年龄(26)
    #   —— 适配度永远第一，音质只是同一档里更好的选择。
    _pref = list(profile.get("prefer_providers") or ())
    _prov = str(voice.get("provider") or "")
    if _prov and _pref and _prov in _pref:
        # 越靠前分越高：第 1 位 +8、第 2 位 +6 …
        score += max(0.0, 8.0 - 2.0 * _pref.index(_prov))
        if _prov != "edge":
            why.append(f"高保真引擎（{_prov}）")
    elif _prov == "edge" and profile.get("allow_free_engine", True) is False:
        score -= 6.0        # 明确"不要免费引擎"时才扣分（默认不扣，保底可用）
    if voice.get("verified"):
        score += 2
    return score, why


def cast_from_catalog(characters: List[Dict[str, Any]],
                      catalog: List[Dict[str, Any]],
                      used: Optional[set] = None,
                      prefer_providers: Optional[Sequence[str]] = None,
                      ) -> Dict[str, Any]:
    """整片选角：每个角色一把嗓子，**不撞音**，并给出理由。纯函数，可单测。

    `catalog` 每项：`{"voice_id", "gender", "label", "provider", "verified"}`。
    `used`：已经占用的音色（例如角色卡里显式指定的那些）—— 传进来可以让
    新配的角色避开它们。调用方也可以拿返回结果里的 `used` 继续配下一批
    （分镜里**卡外角色**就是这么配的：让"村民A""村民B"各拿一把不同的嗓子，
    而不是全部落到默认那一把女声）。
    返回 `{"voices", "reasons", "collisions", "profiles", "unmatched", "used"}`。
    """
    from collections import Counter
    plan: Dict[str, str] = {}
    reasons: Dict[str, str] = {}
    profiles: Dict[str, Any] = {}
    used: set = set(used or set())
    chars = [c for c in (characters or []) if str(c.get("name") or "").strip()]

    # ① 角色卡里**显式指定**的音色优先（用户自己选的必须尊重）
    for c in chars:
        n = str(c["name"]).strip()
        v = ""
        rf = c.get("reference_features")
        if isinstance(rf, str):
            import json
            try:
                rf = json.loads(rf) or {}
            except Exception:
                rf = {}
        if isinstance(rf, dict):
            vv = rf.get("voice_id") or rf.get("voice")
            if isinstance(vv, dict):
                vv = vv.get("voice_id") or vv.get("id") or ""
            v = str(vv or "").strip()
        if v:
            plan[n] = v
            reasons[n] = "角色卡里已指定音色（保持不变）"
            used.add(v)

    # ② 其余按"角色卡 + 情景"打分挑（撞音优先于分数：两个角色同一把嗓子最出戏）
    for c in chars:
        n = str(c["name"]).strip()
        if n in plan:
            continue
        prof = role_profile(c)
        # ★ 引擎质量偏好（2026-09-27 第 71 轮）：有付费高保真引擎就优先用它，
        #   因为只有 MiniMax / CosyVoice 这类引擎**支持 emotion 参数**，
        #   而 Edge 不支持 —— 落在 Edge 上就是"平铺直叙、AI 感重"。
        if prefer_providers:
            prof = {**prof, "prefer_providers": list(prefer_providers)}
        profiles[n] = prof
        scored = []
        for v in (catalog or []):
            if v.get("voice_id") in used:
                continue
            s, why = score_voice(prof, v)
            if s <= -1e8:
                continue
            scored.append((s, v, why))
        if not scored:                       # 都撞了 → 允许复用，但如实说明
            for v in (catalog or []):
                s, why = score_voice(prof, v)
                if s > -1e8:
                    scored.append((s, v, why + ["（同性别音色已用完，只能复用）"]))
        if not scored:
            continue
        scored.sort(key=lambda x: (-x[0], str(x[1].get("voice_id"))))
        s, v, why = scored[0]
        plan[n] = v["voice_id"]
        used.add(v["voice_id"])
        _why = "；".join(why) or "同性别兜底"
        if prof.get("is_group"):
            _why = "群体角色 → 取一把代表音色；" + _why
        if not prof.get("gender"):
            # ★ 角色卡没写性别时，音色是按气质挑的 —— 必须说出来，
            #   否则"水手"被配成女声这种事会变成一个没人解释的谜。
            _why += "；⚠ 角色卡没写性别（也没能从外貌描述里推断），按气质挑的（可在「角色」页补上）"
        elif prof.get("gender_inferred"):
            # ★ 性别是**推断**出来的（角色名里的身份词，或外貌描述里的强线索）——
            #   也说明白，别让人以为卡片里写了。推断依据一并给出，便于核对。
            _src = "由外貌/服装描述推断" if prof.get("gender_evidence") else "由角色名推断"
            _why += (f"；性别{_src}（{prof['gender_inferred']}"
                     + (f"，依据「{prof['gender_evidence']}」" if prof.get("gender_evidence") else "")
                     + "；角色卡没写性别）")
        if prof.get("age_band") and v.get("age_inferred"):
            _why += "（年龄段由音色标签推断）"
        reasons[n] = _why + f" → {v.get('label') or v['voice_id']}"
    cnt = Counter(plan.values())
    collisions = [{"voice": v, "characters": [k for k, x in plan.items() if x == v]}
                  for v, c2 in cnt.items() if c2 > 1]
    unmatched = [n for n in (str(c.get("name") or "").strip() for c in chars)
                 if n and n not in plan]
    # ★ 如实报"谁落到了免费引擎"（2026-09-27 第 71 轮）。
    #   原因通常是**高保真音色不够分**（撞音优先于音质：两个角色同一把嗓子
    #   比音质更出戏）。但用户得知道，否则"我配了 minimax 怎么还是 AI 音"
    #   就成了一个没人解释的谜。可行的解法写在提示里。
    on_free = sorted({n for n, v in plan.items()
                      if str(v).split(":")[0] == "edge"
                      and any(str(x).split(":")[0] != "edge" for x in plan.values())})
    return {"voices": plan, "reasons": reasons, "collisions": collisions,
            "profiles": profiles, "unmatched": unmatched,
            "on_free_engine": on_free,
            "used": sorted(used), "catalog_size": len(catalog or [])}
