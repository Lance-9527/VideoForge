# -*- coding: utf-8 -*-
r"""VideoForge · 提示词审计（Prompt Audit）

═══════════════════════════════════════════════════════════════════
这个模块解决用户的一句话
═══════════════════════════════════════════════════════════════════
用户原话（2026-09-13）：

  "视频分镜里面有的视频太扯了……有个主角还从冰雪里面爬出来。。。什么鬼"

"太扯"有一大半**不是模型的问题，是提示词本身不合格**：模型没被告知
"一个镜头只准有一个主运镜""画面里恰好几个人""这是几秒""对白只作为声音"，
于是它自己编 —— 编出来的就是不可控的画面。

本模块把参考方法论（`Hell-Grind-AIGC-Skill`，本地克隆在
`D:\VideoForge-dev\reference\Hell-Grind-AIGC-Skill`）里**可代码化**的那部分
落成检查项。规则的来源逐条标注：

  · `references/video-prompt-contract.md` 的 12 段镜头契约
  · `references/prompt-architecture.md` 的七层结构与"一个镜头只有一个主运动"
  · `references/negative-constraints.md` 的同义负面词合并
  · `references/action-physics-vfx.md` 的"准备→发力→接触→反作用→落定"
  · `scripts/audit_prompt.py` 的 13 个 `P-*` 错误码与打分公式

═══════════════════════════════════════════════════════════════════
打分口径（照抄 `scripts/audit_prompt.py`，不自己发明）
═══════════════════════════════════════════════════════════════════
    score = 0 if 空 else max(0, min(100, 100 - errors*15 - warnings*5))
    valid_for_review = (errors == 0)

**"平均分不能抵消硬伤"**：只要有一个 error，`valid_for_review` 就是 False ——
这条来自 `project-qa-gates.md`（身份、人数、逐字台词、时长这些是 hard gate）。

═══════════════════════════════════════════════════════════════════
它能做什么、不能做什么（照抄参考文档的自述，免得被误用）
═══════════════════════════════════════════════════════════════════
    参考 `project-qa-gates.md`：审计器"检查提示词结构和高置信冲突。
    它们**不观看媒体**，也不判断表演美感、叙事效果、真实口型或权利事实真伪"。

所以：**审计通过 ≠ 生成结果一定好**；但审计不过，几乎一定出废片。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("videoforge.promptaudit")

# ── 严重级别（与参考一致：error 必须先修；warning 可人工判断后继续）──
ERROR, WARNING, INFO = "error", "warning", "info"

# ── 主运镜词表（逐条对应参考 `scripts/audit_prompt.py` 的枚举）──
CAMERA_MOVES = {
    "orbit": r"环绕|orbit",
    "push": r"推进|推近|推镜|push\s?in|dolly\s?in",
    "pull": r"拉远|拉出|拉镜|pull\s?out|dolly\s?out",
    "track": r"跟拍|跟随|track|follow",
    "pan": r"横摇|摇摄|\bpan\b",
    "tilt": r"俯仰摇|上摇|下摇|\btilt\b",
    "crane": r"升降|摇臂|\bcrane\b|\bboom\b",
    "zoom": r"变焦|zoom",
    "handheld": r"手持",
}
LOCKED_CAMERA = r"锁定机位|固定机位|摄影机固定|镜头固定|locked[\s-]?off|static\s+camera"

# ── 时长与结束构图（参考 `video-prompt-contract.md` #6 / #12）──
DURATION_RE = re.compile(r"(?:总时长|时长|duration)?\s*(\d+(?:\.\d+)?)\s*(?:秒|s\b|sec(?:ond)?s?\b)")
RANGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[-–—~至到]\s*(\d+(?:\.\d+)?)\s*(?:秒|s\b|sec(?:ond)?s?\b)")
CAMERA_END_RE = re.compile(
    r"结束构图|最终(?:停|落|定格)|尾帧|结尾画面|停在|落到|结束画面|"
    r"end\s?frame|final\s?frame|ends?\s(?:on|with|at)|settles?\s(?:on|at)", re.I)
AUDIO_RE = re.compile(
    r"无配乐|无音乐|不要音乐|环境(?:底床|声|音)|现场声|同期声|静音|"
    r"对白|台词|人声|音效|无旁白|no\s+score|no\s+music|ambien", re.I)
DIALOGUE_VERBATIM_RE = re.compile(r"逐字|台词[:：]|对白[:：]|说[：:]|“|「")
DIALOGUE_NOVIS_RE = re.compile(
    r"对白不视觉化|台词不视觉化|只作为声音|仅作为声音|不触发闪回|没有闪回|"
    r"audio\s?only|not\s+visualized|no\s+flashback", re.I)
REFERENCE_RE = re.compile(r"参考|reference|image_\d|ref_\d", re.I)
REFERENCE_SCOPE_RE = re.compile(r"inherit|exclude|继承|排除|只参考|不要带入", re.I)
PLATFORM_PARAM_RE = re.compile(r"\bseed\b|\bsteps?\b|\bcfg\b|\bsampler\b|采样器|采样步数", re.I)
ADAPTER_SECTION_RE = re.compile(r"平台适配层|provider\s?adapter|adapter\s?layer", re.I)
ABSTRACT_WORDS = re.compile(
    r"高级|震撼|电影感|史诗感|唯美|梦幻|大片感|质感|氛围感|premium|cinematic|epic|stunning", re.I)
CONCRETE_WORDS = re.compile(
    r"画面中|主体|人物|角色|场景|道具|动作|视线|构图|机位|光源|材质|颜色|镜头|环境|"
    r"subject|character|scene|prop|action|camera|light|material|color", re.I)
SUBJECT_RE = re.compile(r"人物|角色|主体|他|她|人|character|subject|影|兽|蛇|怪")

# 同义负面词组（参考 `negative-constraints.md` 的"去重 4 条规则"）
NEGATIVE_SYNONYM_GROUPS = [
    (r"无配乐|不要音乐|无音乐|no\s+score|no\s+music", "无配乐"),
    (r"无额外人物|不要加人|不加人|no\s+extra\s+(?:people|characters)|没有额外", "无额外人物"),
    (r"无字幕|不要字幕|no\s+subtitles?", "无字幕"),
    (r"无抖动|不要抖动|no\s+jitter|no\s+shake", "无抖动"),
]
# 互斥组合（参考 `negative-constraints.md` 的"冲突压缩 6 行"）
MOTION_STILL_RE = re.compile(r"全程.{0,12}(?:完全)?静止|完全静止|保持不动|身体位置.{0,12}不变|静止不动")
MOTION_MOVE_RE = re.compile(r"持续.{0,10}(?:奔跑|跑动|移动|行走)|奔跑|跑向|快步走|不断移动|快速移动")


def _hits(pat: str, text: str) -> List[str]:
    return re.findall(pat, text, re.I)


def camera_context(text: str) -> str:
    """抽出提示词里**真正在讲摄影机**的那部分，用于判"几个主运镜"。

    ★ 为什么不能直接在整段提示词上正则匹配运镜词：实测**假阳性**很明显 ——
      场景描述里写着"城堡建筑**环绕**广场"（这是地点关系，不是运镜），
      旧写法把它算成一个 `orbit`，于是提示词被判"两个主运镜"。
      参考仓库的 `audit_prompt.py` 就是在全文上匹配的，属于那套实现的一个已知弱点；
      这里收紧成"必须以摄影机词开头"的片段才算。
    """
    parts = []
    # ① 显式小节：`镜头运动：…` / `运镜：…` / `摄影机：…`
    for m in re.finditer(r"(?:镜头运动|运镜|摄影机|camera\s?(?:move|path)|机位)[:：]([^。；\n]{0,80})",
                         text, re.I):
        parts.append(m.group(1))
    # ② 行文里的"镜头/摄影机 + 动作"从句（限制在镜头词之后 24 字内）
    for m in re.finditer(r"(?:镜头|摄影机|camera|运镜|机位)([^。；\n]{0,24})", text, re.I):
        parts.append(m.group(1))
    ctx = " ｜ ".join(parts)
    # ★ 复合运镜名先归一：`变焦推近`/`变焦拉远`/`zoom in` 是**一个**运动（变焦），
    #   但词表里"变焦"和"推近/拉远"各算一条 → 会被数成两个主运镜，
    #   把本来合格的提示词判成硬伤（结构不变量测试 D1 抓到的就是这两条）。
    #   注意：只归一"变焦+推拉"这种**同一个动作**的写法；
    #   "变焦，同时环绕" 仍然是两个，照常判冲突。
    ctx = re.sub(r"变焦\s*(?:推近|推进|拉远|拉出|推|拉)|(?:推近|推进|拉远|拉出)\s*变焦",
                 "变焦", ctx)
    return ctx


def audit_video_prompt(
    prompt: str,
    negative: str = "",
    *,
    duration: Optional[float] = None,
    dialogue: Optional[List[str]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """审计一段**视频**提示词。返回 issues / 分数 / 是否可进入生成。

    `duration` 给定时会额外检查"时间范围不能超出镜头时长"（`P-TIMELINE-OVERFLOW`）；
    `dialogue` 是这一镜的**逐字台词**列表 —— 有台词时必须声明"对白不视觉化"，
    否则模型很容易把"台词里提到的过去/别处"画成闪回或额外人物
    （这正是"主角从冰雪里爬出来"那一类画面的机制之一）。
    """
    p = prompt or ""
    issues: List[Dict[str, str]] = []

    def add(code: str, severity: str, message: str, fix: str = ""):
        issues.append({"code": code, "severity": severity,
                       "message": message, "fix": fix})

    if not p.strip():
        add("P-EMPTY", ERROR, "提示词是空的。", "先给这一镜写出'演什么'。")
        return _result(issues, p, negative)

    # ── 1. 主体（error）：`P-MISSING-SUBJECT` ──
    if not SUBJECT_RE.search(p):
        add("P-MISSING-SUBJECT", ERROR,
            "提示词里没有可识别的主体（人物/角色/主体）。",
            "写清画面里是谁或是什么，并给出精确数量。")

    # ── 2. 时长（error）：`P-MISSING-DURATION` ──
    dur_in_text = None
    m = DURATION_RE.search(p)
    if m:
        try:
            dur_in_text = float(m.group(1))
        except Exception:
            dur_in_text = None
    if dur_in_text is None and not duration:
        add("P-MISSING-DURATION", ERROR,
            "提示词没有声明时长，调用方也没有给 duration。",
            "写明'总时长 N 秒'，或让上层传入 duration。")

    # ── 3. 结束构图（error）：`P-MISSING-CAMERA-END` ──
    if not CAMERA_END_RE.search(p):
        add("P-MISSING-CAMERA-END", ERROR,
            "没有写**结束构图 / 尾帧落到哪**。只写'怎么动'不写'停在哪'，"
            "模型会把镜头甩到任意地方收尾。",
            "补一句'结束构图：…（景别、主体关系、对焦）'。")

    # ── 4. 音频边界（error）：`P-MISSING-AUDIO` ──
    if not AUDIO_RE.search(p) and not AUDIO_RE.search(negative):
        add("P-MISSING-AUDIO", ERROR,
            "没有声明音频边界（对白 / 环境声 / 音效 / 音乐 / 静音，至少写一个）。"
            "不写的话平台会自动加音乐或旁白 —— 方法论把它列为"
            "`F-AUDIO-POLLUTION`（自动音乐、旁白、字幕或无关声）。",
            "补一句：'无配乐；环境底床为…；无旁白'。")

    # ── 5. 主运镜冲突（error）：`P-CAMERA-CONFLICT` ──
    #    只在**摄影机语境**里统计（见 `camera_context` 的说明：全文匹配会假阳性）
    ctx = camera_context(p)
    found = [k for k, pat in CAMERA_MOVES.items() if re.search(pat, ctx, re.I)]
    locked = bool(re.search(LOCKED_CAMERA, ctx, re.I))
    if len(found) > 1:
        add("P-CAMERA-CONFLICT", ERROR,
            f"提示词里同时出现了多个主运镜：{'、'.join(found)}。"
            "方法论明确：**一个镜头只有一个主摄影机运动**。",
            "只保留一个，其余改成很小的构图修正或拆成两个镜头。")
    elif locked and any(k not in ("handheld",) for k in found):
        add("P-CAMERA-CONFLICT", ERROR,
            f"同时写了'固定机位'和运镜（{'、'.join(found)}），互相矛盾。",
            "二选一。")

    # ── 6. 静止 vs 移动（error）：`P-CONFLICT-MOTION` ──
    if MOTION_STILL_RE.search(p) and MOTION_MOVE_RE.search(p):
        add("P-CONFLICT-MOTION", ERROR,
            "同时要求'保持不动'和'持续移动'，模型只能二选一或糊成一团。",
            "删掉其中一个；若确实要先静后动，写清时间点。")

    # ── 7. 时间范围越界（error）：`P-TIMELINE-OVERFLOW` ──
    if duration:
        for a, b in RANGE_RE.findall(p):
            try:
                if float(b) > float(duration) + 1e-6:
                    add("P-TIMELINE-OVERFLOW", ERROR,
                        f"提示词里写了 {a}–{b} 秒，但这一镜只有 {duration:g} 秒。",
                        "把时间点压回镜头长度内，或把镜头拆开。")
                    break
            except Exception:
                continue

    # ── 8. 动作超载（warning；本模块的扩展，依据参考的时长预算表）──
    #    参考 `video-prompt-contract.md`：一次完整身体动作 1–3 秒；
    #    超载时的降级顺序是"删装饰动作 → 缩短台词 → 降低运镜 → 拆镜头"。
    #
    #    ★ 第一版这里按**整段提示词**数逗号，数出"73 个动作"这种荒唐数字 ——
    #      因为人物外形签名里本来就有一堆逗号。必须只数**画面内容那一段**，
    #      而且只在"随后/然后/接着/最后"与句号分号处切（逗号是句子内部停顿，不是动作边界）。
    if duration:
        seg = ""
        # 只取"画面内容"那一段，遇到任何一个小节标题就停
        m = re.search(r"画面内容[:：](.*?)(?=(?:^|[。；;\s])"
                      r"(?:音频|风格|镜头|人物|环境|结束构图|总时长|起始构图)[:：（]|$)", p, re.S)
        seg = m.group(1) if m else p
        acts = [x.strip() for x in re.split(r"随后|然后|接着|最后|同时|[。；;]", seg)
                if len(x.strip()) >= 2]      # ★ 2 字也算一个动作（"转身""蹲下"都是）
        n_act = max(1, len(acts))
        per = float(duration) / n_act
        # ★ 门限按方法论的经验值「一次完整身体动作 1~3 秒」取 1.0 秒：
        #   低于 1 秒就是"动作被压缩"，这正是 `F-ACTION-OVERLOAD`
        #   （动作被压缩、遗漏或顺序错）。注意 4 个动作塞进 5 秒（每个 1.25s）
        #   **不算超载** —— 那还在 1~3 秒区间内，不该误报。
        if n_act >= 3 and per < 1.0:
            add("P-ACTION-OVERLOAD", WARNING,
                f"这一镜 {duration:g} 秒里安排了约 {n_act} 个动作，平均每个只有 {per:.2f} 秒 —— "
                f"方法论的经验值是「一次完整身体动作需要 1~3 秒」。",
                "按顺序降级：删装饰动作 → 缩短台词 → 降低运镜 → 拆成两镜。")

    # ── 9. 逐字台词但没写"不视觉化"（warning）：`P-DIALOGUE-VISUALIZATION` ──
    if dialogue and any(str(d).strip() for d in dialogue):
        if not DIALOGUE_NOVIS_RE.search(p):
            add("P-DIALOGUE-VISUALIZATION", WARNING,
                "这一镜有逐字台词，但没写「对白不视觉化」。模型可能把台词里提到的"
                "过去事件/别处/想象画成**闪回或额外人物** —— 这正是"
                "「画面跟剧本对不上」的常见机制。",
                "补一句：'台词只作为声音，不触发闪回，不出现额外人物'。")

    # ── 10. 参考图没有声明继承范围（warning）：`P-REFERENCE-SCOPE` ──
    if REFERENCE_RE.search(p) and not REFERENCE_SCOPE_RE.search(p):
        add("P-REFERENCE-SCOPE", WARNING,
            "提到了参考图，但没说**继承什么、排除什么**。"
            "方法论：'参考这张图'不是可控指令，构图/机位/背景会被一起带进来。",
            "写成：'只继承<身份/服装>，排除<构图/机位/背景/光线>'。")

    # ── 11. 私有参数混进主提示词（warning）：`P-PLATFORM-MIXED` ──
    if PLATFORM_PARAM_RE.search(p) and not ADAPTER_SECTION_RE.search(p):
        add("P-PLATFORM-MIXED", WARNING,
            "主提示词里混进了平台私有参数（seed/steps/cfg/sampler），"
            "且没有单独的适配层。",
            "把私有参数挪到平台适配层，主提示词保持模型无关。")

    # ── 12. 同义负面词重复（warning）：`P-NEGATIVE-DUPLICATE` ──
    for pat, label in NEGATIVE_SYNONYM_GROUPS:
        if len(_hits(pat, negative)) >= 2:
            add("P-NEGATIVE-DUPLICATE", WARNING,
                f"负面提示里「{label}」用不同的说法写了多次。"
                "方法论：同义句重复不会增加权重，只是噪声。",
                "合并成一条。")

    # ── 13. 只有抽象词、没有可执行信息（warning）：`P-ABSTRACT-ONLY` ──
    if len(_hits(ABSTRACT_WORDS.pattern, p)) >= 2 and not CONCRETE_WORDS.search(p):
        add("P-ABSTRACT-ONLY", WARNING,
            "提示词堆了多个抽象质量词（电影感/震撼…），却没有任何具体信息"
            "（主体、动作、构图、光源）。",
            "把抽象词翻译成可观察的结果。")

    # ── 13b. 「画面内容」里混进**导演语/展示语**（warning）：`P-META-VERB` ──
    #    来源：实测 63 个真实分镜里 8 个中招，全是这一类：
    #      「展示他沉静的姿态」「展示四层楼整体结构」「展示其精致的纹理和工艺」
    #      「展示行人穿梭的繁忙景象」「展示两人互动」。
    #    "展示 X" 不是**看得见**的动作，是编剧/导演对意图的说明。
    #    模型拿到它只能猜，常见结果是拍成一个**静态展示镜头**（人站着不动、
    #    或者镜头对着东西慢慢扫），那一段就废了。
    #    注意只列**强导演语**：像"表现出担忧"这类其实是可观察的表情，不算。
    _body = ""
    _m = re.search(r"画面内容[:：]([^。]*)", p)
    if _m:
        _body = _m.group(1)
    _mv = _hits(r"(展示|凸显|营造|象征|传达|体现)", _body)
    if len(_mv) >= 1:
        add("P-META-VERB", WARNING,
            f"「画面内容」里有 {len(_mv)} 处导演语（{'、'.join(sorted(set(_mv)))}）—— "
            f"它们是**意图说明**，不是看得见的动作，模型容易拍成静态展示镜头。",
            "把它们翻译成可观察的动作与表情，例如「展示他沉静的姿态」→"
            "「他双手交叠放在膝上，目光平稳地看向前方」。")

    # ── 14. 剧本本身给了多个运镜，我们只留了一个（warning；本模块的扩展）──
    #    来源：`camera-editing-language.md` 的"若需要'先推近再环绕'，
    #    通常已经是两个主运动。应删除其一、拆成多镜头"。
    #    我们做了"删除其一"，但**必须告诉用户**：他想要的那两种运镜，
    #    要么拆镜、要么接受只保留第一个 —— 不能指望一个镜头里同时发生。
    if extra and extra.get("camera_conflict"):
        _seen = extra.get("camera_moves_seen") or []
        add("P-CAMERA-MULTI-SOURCE", WARNING,
            f"这一镜的剧本里出现了不止一个运镜（{'、'.join(map(str, _seen))}），"
            f"提示词里**只保留了第一个**。方法论：这通常意味着它该拆成两个镜头。",
            "想在成片里看到两种运镜，就把这一镜拆开；否则接受只保留第一个。")

    return _result(issues, p, negative)


def _result(issues: List[Dict[str, str]], prompt: str, negative: str) -> Dict[str, Any]:
    errs = [i for i in issues if i["severity"] == ERROR]
    warns = [i for i in issues if i["severity"] == WARNING]
    if not (prompt or "").strip():
        score = 0
    else:
        score = max(0, min(100, 100 - len(errs) * 15 - len(warns) * 5))
    return {
        "issues": issues,
        "errors": errs,
        "warnings": warns,
        "error_count": len(errs),
        "warning_count": len(warns),
        "score": score,
        # ★ 硬闸门：只要有 error 就不许进入生成（参考 `project-qa-gates.md`
        #   的"平均分不能抵消 hard gate"）
        "valid_for_generation": not errs,
        "valid_for_review": not errs,
        "summary": _summary(errs, warns),
    }


def _summary(errs: List[Dict[str, str]], warns: List[Dict[str, str]]) -> str:
    if not errs and not warns:
        return "结构完整：主体、时长、结束构图、音频边界、单一主运镜都写清了。"
    parts = []
    if errs:
        parts.append(f"{len(errs)} 处硬伤（必须先修）：" + "；".join(i["message"][:40] for i in errs[:3]))
    if warns:
        parts.append(f"{len(warns)} 处提醒：" + "；".join(i["message"][:30] for i in warns[:3]))
    return " ｜ ".join(parts)
