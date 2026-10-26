"""
VideoForge · 白膜预演（Previz）  —— 【已降级为回退方案】

★★★★★ 2026-09-29：白膜默认引擎已换成 **Blender 3D 预演** ★★★★★
  新位置：`core/previz_blender.py`（渲染脚本在 `data/previz_blender/`）。

  为什么换掉本模块（用户实测，三个硬伤）：
    ① 体块只有矩形，**画不出「伏地的人」** —— 剧本写「陈砚俯卧泥水中」，
       渲出来是站着的方块；
    ② 没有厚度、**没有遮挡关系**（谁挡谁），也没真透视；
    ③ `y` 是方块**顶部**坐标，调用方按「中心」给值就整体埋进地面。

  Blender 的优势：世界单位就是米（人物 1.72 就是 1.72，无二义性），
  相机/透视/遮挡/影子全由它自己解；实测 12 镜 5.7 秒渲完，零费用。

  用户原话：「再把 Blender 这个环节加入进去（原先出白膜视频的工具可以去掉了）」。

  本模块**仍然保留**的原因：它是旧白膜三档（A/B/C）的实现，
  被 `previz_ref` / `longvideo` 等内部流程引用着，且充当
  「机器上没装 Blender」时的回退。**新代码不要再用它来出白膜**，
  请用 `core.previz_blender`。

════════════════════════════════════════════════════════════════════════
一句话结论
════════════════════════════════════════════════════════════════════════
**白膜不是一种画风，是一种控制信号。**

它只提供三样东西：**空间关系、运镜路径、时间节奏**。
材质、光线、色彩、造型——全部交给参考图。

所以"让模型生成白模风格的视频"是次优解（不稳定、抽卡率没保证），
正确做法是**真的渲染一个白膜**：本地做，免费，可以无限重来。
这也意味着白膜阶段的成本是 **0** —— 你不需要为了"先出个草稿"去买便宜模型。

════════════════════════════════════════════════════════════════════════
三级闸门（G1/G2/G3）
════════════════════════════════════════════════════════════════════════
    阶段 0  shotlist 分镜表          （免费）
      ↓
    G1  白膜视频（本地渲染，免费，无限重试）   ← 确认运镜/构图/节奏
      ↓  通过
    G2  480P 样片（便宜，≤3 次）                ← 确认画风/表演
      ↓  通过
    G3  1080P 成片（贵，≤1 次）
      ↓
      逐镜通过后 → 拼片

**驳回必须带理由码**，因为理由码决定"回退到哪一级重跑"，这就是省钱开关：

    理由码                     回退到        白膜要不要重渲
    ─────────────────────────────────────────────────────
    CAMERA / BLOCKING / PACING  →  G1          **要**（运镜/站位/节奏问题，
                                               改参考图救不回来）
    LOOK / IDENTITY             →  G2          **不要**（画风/脸的问题，
                                               白膜是对的，只换参考图）

这个区分是整套流程里最值钱的一条：如果 LOOK 问题也回 G1 重渲，
你会为了一个"颜色不对"把已经确认过的运镜又推翻一遍。

════════════════════════════════════════════════════════════════════════
实测换来的坑（写死在体检里，别删）
════════════════════════════════════════════════════════════════════════
· **白膜颜色会渗进成片**：蓝色人群白模 → 成片出现"蓝面罩人群"。
  → 必须纯灰，不要用颜色编码不同角色。
· **主体占画高 < 10% 且位移 < 0.5 个身高** → 模型**直接把它删掉**，
  加提示词也救不回来。→ 体检要报"主体太小"。
· **人形没有脸，正反面肉眼验不出** → 实测三条片子全员倒着走、
  三轮人工审查都没发现。→ 人形必须有鼻子和胸标（朝向标记）。
· **带纹理的白模是反效果**：模型会把墙当平面、脖子翻转 180°。
  → 必须纯灰 blockout，不能有贴图/网格/线框。
· **渲染前必须做纯几何体检**：lint 全绿、时长也全对的五个白模，
  抽帧一看运镜五个全坏。→ 光有 lint 不够，还要抽帧看。
· **prompt 与白膜必须同源**：分头演化会导致 prompt 承诺一个白膜里
  根本没有的运镜。→ 这个模块把"白膜参数"和"prompt"绑在一份数据里生成。
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("videoforge.previz")

# ── 白膜状态机 ────────────────────────────────────────────────────────
# 未开始 → 白膜生成中 → 白膜就绪 → (通过/驳回) → 样片 → 成片
S_NOT_STARTED = "not_started"
S_DRAFT_RUNNING = "white_model_running"
S_DRAFT_READY = "draft_ready"
S_DRAFT_APPROVED = "draft_approved"
S_FINAL_RUNNING = "final_running"
S_FINAL_READY = "final_ready"

VALID_STATES = [S_NOT_STARTED, S_DRAFT_RUNNING, S_DRAFT_READY,
                S_DRAFT_APPROVED, S_FINAL_RUNNING, S_FINAL_READY]

# 面向用户的状态名。
#
# ★ `white_model_running` 这个名字**有歧义**：它既要表示"正在渲白膜(G1)"，
#   又要表示"白膜已通过、正在出样片(G2)"—— 因为 LOOK/IDENTITY 驳回后
#   会回到这个状态。实测就出现了这个尴尬：
#     白膜确认过、用户驳回画风 → 状态显示"白膜生成中"
#   而实际上**白膜根本没在重渲**（那正是省钱的地方，却被文案说反了）。
#   状态名为了兼容旧数据不动，但给人看的文案必须分得清。
STATE_LABELS = {
    S_NOT_STARTED:     "还没开始",
    S_DRAFT_RUNNING:   "白膜生成中（G1）",
    S_DRAFT_READY:     "白膜已出，等你确认（G1）",
    S_DRAFT_APPROVED:  "白膜已确认，可以出样片/成片（G2）",
    S_FINAL_RUNNING:   "成片生成中（G3）",
    S_FINAL_READY:     "成片已就绪",
}


def state_label(state: str, *, after_reject: bool = False) -> str:
    """给状态一个人能看懂的说明。

    `after_reject=True` 且状态是 `white_model_running` 时，说明这是
    "驳回后回到出样片这一级"（G2），不是"在重渲白膜"（G1）——
    对用户来说这两件事差别很大（后者要多等一轮、前者白膜不动）。
    """
    if state == S_DRAFT_RUNNING and after_reject:
        return "白膜保持不变，正在重出样片（G2）"
    return STATE_LABELS.get(state, state)

# 合法迁移。写成白名单而不是 if-else 串，是为了让"跳步"必然报错：
# 没确认白膜就出终版 = 烧钱，必须挡住。
TRANSITIONS = {
    S_NOT_STARTED:    {S_DRAFT_RUNNING},
    S_DRAFT_RUNNING:  {S_DRAFT_READY, S_NOT_STARTED},
    S_DRAFT_READY:    {S_DRAFT_APPROVED, S_DRAFT_RUNNING, S_NOT_STARTED},
    S_DRAFT_APPROVED: {S_FINAL_RUNNING, S_DRAFT_RUNNING},
    S_FINAL_RUNNING:  {S_FINAL_READY, S_DRAFT_APPROVED},
    S_FINAL_READY:    {S_DRAFT_APPROVED, S_FINAL_RUNNING},
}

# ── 驳回理由码 ────────────────────────────────────────────────────────
# 分两类：动画问题（回 G1 重渲白膜）vs 画面问题（停 G2 只换参考图）
REASON_CODES = {
    "CAMERA":    ("运镜不对", "draft"),
    "BLOCKING":  ("站位/走位不对", "draft"),
    "PACING":    ("节奏不对", "draft"),
    "LOOK":      ("画风/色调不对", "sample"),
    "IDENTITY":  ("角色长相不对", "sample"),
    "OTHER":     ("其他", "sample"),
}

# 各闸门允许重试几次。G1 免费所以给很多；G3 最贵，一次。
GATE_RETRIES = {"G1": 99, "G2": 3, "G3": 1}


def classify_reason(code: str) -> Dict[str, Any]:
    """把驳回理由码翻译成"回退到哪一级、白膜要不要重渲"。

    这是整套流程的省钱开关，也是这个模块存在的理由。
    """
    code = (code or "OTHER").strip().upper()
    label, scope = REASON_CODES.get(code, REASON_CODES["OTHER"])
    return {
        "code": code if code in REASON_CODES else "OTHER",
        "label": label,
        "scope": scope,
        "regenerate_draft": scope == "draft",
        "back_to": "G1" if scope == "draft" else "G2",
        "advice": (
            "这是运镜/走位/节奏问题，改参考图救不回来 —— 重渲白膜。"
            if scope == "draft" else
            "白膜是对的，不用重渲 —— 只换参考图/调 prompt，停在样片这一级。"
        ),
    }


def can_transition(frm: str, to: str) -> bool:
    return to in TRANSITIONS.get(frm, set())


def assert_transition(frm: str, to: str) -> None:
    if not can_transition(frm, to):
        raise ValueError(
            f"白膜流程不允许从 {frm} 直接到 {to}。"
            f"（必须先确认白膜再出成片，否则就是在烧钱）"
        )


# ── 几何体检 ──────────────────────────────────────────────────────────

# 主体占画高下限。低于这个值模型会直接把主体删掉。
MIN_SUBJECT_HEIGHT_RATIO = 0.10
# 主体位移下限（以身高为单位）。低于这个值 = 画面里"什么都没发生"。
MIN_SUBJECT_MOVE_RATIO = 0.5


def infer_camera(text: str) -> tuple:
    """从**分镜的文字**里读出运镜，返回 (camera_key, why)。

    ★★ 2026-09-27 第 72 轮（用户原话：「白膜视频生成有问题」）。

    查出来的事实：`render_previz` 在调用方没给 `camera` 时**一律用 static**，
    于是实测《执棋者》5 个镜头里有 4 个渲出来是**一张完全静止的灰块**
    （25 KB / 12 秒，三个时间点抽帧的统计量一模一样）——
    白膜的全部价值（看运镜/节奏）等于零，用户当然说"有问题"。

    这里从分镜自己的文字（三层的动作描述 + 提示词）里读运镜，
    读不到才退回 static，并且**把依据一起返回**（结果里如实说明是推断的）。
    """
    t = str(text or "")
    # 顺序即优先级：更具体的说法在前（"推近"优先于"跟"）
    for pat, key, why in (
        (r"(推近|推镜|推进|推上去|dolly\s*in|zoom\s*in)", "dolly_in", "推近"),
        (r"(拉远|拉镜|拉开|退成全景|dolly\s*out|zoom\s*out)", "dolly_out", "拉远"),
        (r"(向左平移|左移|左横移|pan\s*left)", "pan_left", "向左平移"),
        (r"(向右平移|右移|右横移|pan\s*right)", "pan_right", "向右平移"),
        (r"(上摇|向上摇|仰摇|tilt\s*up)", "tilt_up", "上摇"),
        (r"(下摇|向下摇|俯摇|tilt\s*down)", "tilt_down", "下摇"),
        (r"(跟拍|跟随|跟镜|追随|跟住|track)", "track", "跟拍"),
        (r"(固定机位|机位固定|镜头不动|静止机位|固定镜头)", "static", "固定机位"),
    ):
        if re.search(pat, t, re.I):
            return key, f"从分镜文字里读到「{why}」"
    return "static", "分镜文字里没写运镜 → 按固定机位渲（这一版只能验构图与时长）"


#: 动作词 → **相对位移**（dx, dy，归一化画幅比例）。
#: ★★ 2026-09-28 第 74 轮改成"相对自己"：
#:   原来所有"走近/靠近"都指向**同一个绝对点** (0.5, 0.9) —— 实测两个兵丁
#:   于是**走到一起、名字标签叠在一起**（截图里就是"稽查兵丁甲/兵丁乙"糊成一团）。
#:   既然 2D 白膜只能表达"往哪边挪"，那就按各自的位置挪，不再挤到同一点。
_MOTION_HINTS: tuple = (
    (r"(冲进|冲入|闯入|走进|走入|进入|出现|走来|入场|上场|上台|进来)",
     (-0.26, 0.0), "进入画面"),
    (r"(离开|走出|退出|下场|退下|消失|跑掉|溜走)",
     (0.26, 0.0), "离开画面"),
    (r"(走近|靠近|上前|逼|扑向|奔向|跑向|扑过来|凑近|逼近)",
     (0.0, 0.14), "向镜头靠近"),
    (r"(退后|后退|退开|后撤)", (0.0, -0.12), "向后退"),
    (r"(起身|站起|站起来|直起身)", (0.0, -0.13), "由坐到站"),
    (r"(坐下|跪|倒下|伏地|倒地)", (0.0, 0.14), "由站到坐/倒"),
    (r"(转身|回头|转向)", (0.10, 0.0), "转身"),
    (r"(走向|走到|移向|挪向|踱向|赶到)", (0.30, 0.0), "横向走位"),
)


def infer_subject_moves(subjects: List[Dict[str, Any]], text: str,
                        frame_w: int = 1920, frame_h: int = 1080) -> tuple:
    """按分镜文字给每个主体补一条**走位轨迹**，返回 (新 subjects, 说明列表)。

    轨迹是**归一化坐标**（0~1，y 向下），起点取该主体**自己的站位**，
    终点 = 起点 + 相对位移 —— 这样多人同时"走近"不会挤到同一个点上。

    只在主体**还没有 move** 时补；补的依据写进说明里（结果里如实带出）。
    """
    t = str(text or "")
    out = [dict(s) for s in (subjects or [])]
    notes: List[str] = []
    for s in out:
        if s.get("move"):
            continue
        nm = str(s.get("name") or "")
        # 先看"提到这个主体的那句话"里有没有位移词；没有就看整段文字
        scope = t
        m = re.search(re.escape(nm) + r"[^。；\n]{0,40}", t) if nm else None
        if m:
            scope = m.group(0)
        hit = None
        for pat, (dx, dy), why in _MOTION_HINTS:
            if re.search(pat, scope):
                hit = ((dx, dy), why)
                break
        if not hit:
            continue
        # 起点：主体自己的位置（x/y 是左上角像素；没给就按画幅 45%/30% 估）
        try:
            x0 = (float(s.get("x")) if s.get("x") is not None else frame_w * 0.45) / frame_w
        except Exception:  # noqa: BLE001
            x0 = 0.45
        try:
            y0 = (float(s.get("y")) if s.get("y") is not None else frame_h * 0.30) / frame_h
        except Exception:  # noqa: BLE001
            y0 = 0.30
        bw = float(s.get("width") or frame_w * 0.14) / frame_w
        bh = float(s.get("height") or frame_h * 0.5) / frame_h
        cx, cy = x0 + bw / 2, y0 + bh / 2          # 归一化中心点
        dx, dy = hit[0]
        p0 = [round(max(0.05, min(0.95, cx)), 3), round(max(0.05, min(0.95, cy)), 3)]
        p1 = [round(max(0.05, min(0.95, cx + dx)), 3), round(max(0.05, min(0.95, cy + dy)), 3)]
        if abs(p1[0] - p0[0]) < 0.01 and abs(p1[1] - p0[1]) < 0.01:
            continue
        s["move"] = [p0, p1]
        notes.append("%s：%s（从 %.2f,%.2f 到 %.2f,%.2f）" % (
            nm, hit[1], p0[0], p0[1], p1[0], p1[1]))
    return out, notes


def script_shot_text(shot: Dict[str, Any]) -> str:
    """把一镜能拿到的**剧本文字**拼起来。

    ★ 只取"人话"字段（概览 / 每个时间段的动作·表情·运镜 / 台词）。
    为什么不去 json.dumps 整个 `layer3_constraints`：实测那样会把
    `{"字段": …, "16": …}` 这种 **JSON 键名当成说话人**，
    白膜上于是出现"字段""16""9"三个角色（真发生过）。
    """
    parts: List[str] = []
    for k in ("layer1_overview", "previz_notes"):
        if shot.get(k):
            parts.append(str(shot[k]))
    tl = shot.get("layer2_timeline")
    if isinstance(tl, list):
        for seg in tl:
            if not isinstance(seg, dict):
                parts.append(str(seg))
                continue
            for k in ("action", "expression", "camera", "speaker"):
                if seg.get(k):
                    parts.append(str(seg[k]))
            d = seg.get("dialogue")
            if isinstance(d, dict):
                for k in ("speaker", "name", "text", "line"):
                    if d.get(k):
                        parts.append(str(d[k]))
            elif isinstance(d, str) and d.strip():
                parts.append(d)
    elif tl:
        parts.append(tl if isinstance(tl, str) else json.dumps(tl, ensure_ascii=False))
    return " ".join(p for p in parts if p)


#: 这些词出现在冒号前**不是说话人**（JSON 键名 / 分镜字段名 / 通用词）
_NOT_SPEAKERS = {
    "字段", "动作", "表情", "镜头", "台词", "旁白", "字幕", "场景", "时间", "地点",
    "人物", "角色", "情绪", "语气", "道具", "运镜", "画面", "音乐", "音效", "备注",
    "start", "end", "action", "dialogue", "camera", "expression", "props_used",
    "must", "must_not", "must_keep", "must_appear", "must_happen",
}


def speakers_in_text(text: str) -> List[str]:
    r"""从台词里抽出**说话人**（`名字：台词`）。

    判据刻意收紧（实测教训）：只认 **2~4 个汉字**、**前面不能是引号/字母数字**
    （否则 JSON 的 `"key": value` 会被当成台词）、并且不在停用词表里。
    放宽的代价很实在：白膜上会出现叫「字段」「16」「9」的角色。
    """
    out: List[str] = []
    for m in re.finditer(r'(?<!["\'\w])([\u4e00-\u9fff]{2,4})\s*[：:]', str(text or "")):
        nm = m.group(1).strip()
        if not nm or nm in _NOT_SPEAKERS or nm in out:
            continue
        # 「第一层情绪面具：」这种"名词短语"长度上像名字，但里面有层级词
        if any(w in nm for w in ("第", "层", "个", "这", "那")):
            continue
        out.append(nm)
    return out


def subjects_from_script(shot: Dict[str, Any], characters: List[Dict[str, Any]],
                         scene_characters: Optional[List[str]] = None,
                         limit: int = 4) -> tuple:
    r"""**按剧本**决定这一镜有谁、站哪、怎么走 —— 白膜的"人从哪来"。

    ★★ 2026-09-28 第 74 轮（用户原话）：
        「你的白膜视频生成没有按照用户的要求来，得跟完全符合剧本指示做」
        「白膜视频有，但是只会挂机，就一直是这个画面」

    原来只读 `shot.character_ids`（用户的镜头是空的）⇒ 白膜里**永远只有
    一块没有名字的灰板**，既看不出是谁、也看不出剧本写了什么。

    现在的取人顺序（**从可靠到不可靠**）：

      ① **剧本自己写的这一场有谁**（`script.scenes[i].characters`）—— 最可靠，
         它本来就是剧本的演员表；
      ② 分镜时间线里**结构化的台词说话人**（`dialogue.speaker`）；
      ③ 剧本文字里出现的**角色卡名字/别名**；
      ④ 最后才用"文字里 `名字：` 形式"兜底（判据很紧，见 `speakers_in_text`）。

    返回 `(subjects, who)`。`who["from"]` 说明名单是哪来的 —— 结果里会如实带出。
    """
    text = script_shot_text(shot)
    names: List[str] = []
    src = ""

    def _add(nm: Any) -> None:
        nm = str(nm or "").strip()
        if nm and nm not in names and nm not in _NOT_SPEAKERS:
            names.append(nm)

    for nm in (scene_characters or []):
        _add(nm)
    if names:
        src = "剧本这一场的演员表"

    if not names and isinstance(shot.get("layer2_timeline"), list):
        for seg in shot["layer2_timeline"]:
            if isinstance(seg, dict):
                d = seg.get("dialogue")
                if isinstance(d, dict):
                    _add(d.get("speaker") or d.get("name"))
                _add(seg.get("speaker"))
        if names:
            src = "分镜台词里的说话人"

    if not names:
        for c in (characters or []):
            nm = str(c.get("name") or "").strip()
            if nm and nm in text:
                _add(nm)
        if names:
            src = "剧本文字里出现的角色卡"

    if not names:
        for nm in speakers_in_text(text):
            _add(nm)
        if names:
            src = "剧本文字里的台词形式"

    names = names[:max(1, int(limit))]

    subs: List[Dict[str, Any]] = []
    n = len(names)
    for i, nm in enumerate(names):
        x = 0.45 if n == 1 else 0.18 + (0.64 * i / (n - 1))
        subs.append(subject_template(
            name=nm, height=700, humanoid=True,
            x=int(x * 1920), y=int(1080 * 0.30),
        ))
    if not subs:
        subs.append(subject_template(
            name=(str(shot.get("layer1_overview") or "主体")[:8] or "主体"),
            height=700, humanoid=True, x=int(1920 * 0.45), y=int(1080 * 0.30)))
    subs, moves = infer_subject_moves(subs, text)
    return subs, {"names": [s.get("name") for s in subs],
                  "from": src or "（这一镜没认出人物，只放了一个主体）",
                  "moves": moves}


def lint_shot(spec: Dict[str, Any]) -> Dict[str, Any]:
    """**渲染前**的纯几何体检。免费、秒级，能挡掉大部分废镜头。

    先声明它的局限：**lint 全绿不等于运镜对**。
    实测有五个白模 lint 全过、时长全对，抽帧一看运镜五个全坏。
    所以这道关卡只负责挡"确定是废的"，不负责"确认是对的"——
    后者必须靠人看（或抽帧让模型看）。

    Args:
        spec: 镜头描述，字段见 `shot_template()`
    Returns:
        {ok, errors:[], warnings:[], stats:{}}
    """
    errors: List[str] = []
    warnings: List[str] = []
    subjects = spec.get("subjects") or []
    duration = float(spec.get("duration") or 0)
    frame_h = float((spec.get("frame") or {}).get("height") or 1080)

    if duration <= 0:
        errors.append("时长为 0：白膜与成片必须同时长，切点才对得上")
    if not subjects:
        warnings.append("镜头里没有主体：空景可以是有意的，但也可能是漏填了")

    for s in subjects:
        name = s.get("name") or "未命名主体"
        h = float(s.get("height") or 0)
        ratio = h / frame_h if frame_h else 0

        # ★ 实测：主体占画高 <10% 且位移 <0.5 身高 → 模型直接删掉它
        move = s.get("move") or []
        if len(move) >= 2:
            try:
                dx = float(move[-1][0]) - float(move[0][0])
                dy = float(move[-1][1]) - float(move[0][1])
                dist = (dx * dx + dy * dy) ** 0.5
            except Exception:
                dist = 0.0
            move_ratio = dist / h if h else 0
        else:
            move_ratio = 0.0

        if ratio and ratio < MIN_SUBJECT_HEIGHT_RATIO and move_ratio < MIN_SUBJECT_MOVE_RATIO:
            errors.append(
                f"{name} 占画高只有 {ratio:.1%}（下限 {MIN_SUBJECT_HEIGHT_RATIO:.0%}）"
                f"且几乎不动 —— 模型会直接把它删掉，加强提示词也救不回来。"
                f"要么把它推近，要么让它动起来。"
            )
        elif ratio and ratio < MIN_SUBJECT_HEIGHT_RATIO:
            warnings.append(f"{name} 占画高仅 {ratio:.1%}，容易被模型忽略（建议 ≥15%）")

        if s.get("color") and str(s["color"]).lower() not in ("grey", "gray", "灰", "#808080"):
            errors.append(
                f"{name} 用了颜色（{s['color']}）。★ 白膜的颜色会**渗进成片**"
                f"（实测蓝色人群白模 → 成片出现蓝面罩人群）。必须纯灰。"
            )
        if s.get("textured"):
            errors.append(
                f"{name} 带贴图/纹理。★ 带纹理的白模是**反效果**："
                f"模型会把墙当平面、脖子翻转 180°。必须纯灰 blockout。"
            )
        if s.get("humanoid") and not s.get("facing_marker"):
            warnings.append(
                f"{name} 是人形但没有朝向标记。★ 人形没有脸，正反面肉眼验不出——"
                f"实测三条片子全员倒着走、三轮审查都没发现。给它加鼻子+胸标。"
            )

    if not spec.get("ground") and subjects:
        warnings.append("没有地面/接触点：容易出现浮空脚")

    # ★ 每个镜头的首帧必须包含该镜头**所有关键元素**。少一个，模型就会
    #   自己编一个出来，而且编出来的必然与后面漂移。
    #   修法是**回 3D 挪机位**（把东西挪进画），不是加强提示词 ——
    #   提示词救不了"画面里根本没有它"。
    frame = spec.get("frame") or {}
    fw, fh = float(frame.get("width") or 0), float(frame.get("height") or 0)
    if fw and fh:
        for s in subjects:
            if not s.get("must_see"):
                continue
            # ★ 没给 x/y/width/height 的主体**不能算"看不见"** —— 那只是没量过
            #   几何，不是它真的在画外。之前没排除这种情况，于是所有"只写了
            #   名字和身高"的主体都被误报成"首帧里没有它"，把真问题淹掉了。
            if s.get("x") is None and s.get("width") is None:
                warnings.append(
                    f"{s.get('name') or '关键元素'} 标了 must_see，但没有给几何位置 —— "
                    f"无法确认它是否真的在首帧里。建议补上 x/y/width/height 再体检。"
                )
                continue
            sx, sy = float(s.get("x") or 0), float(s.get("y") or 0)
            sw = float(s.get("width") or 0)
            sh = float(s.get("height") or 0)
            # 判定用**中心点**：主体一半在画外是常见且有意的构图，
            # 中心点出画才是真的看不见。
            cx, cy = sx + sw / 2, sy + sh / 2
            if cx < 0 or cy < 0 or cx > fw or cy > fh:
                errors.append(
                    f"{s.get('name') or '关键元素'} 标了 must_see，但中心点在画外"
                    f"（{cx:g},{cy:g} vs 画幅 {fw:g}x{fh:g}）。"
                    f"模型会自己编一个出来，然后必然漂移。"
                    f"★ 正确修法是回 3D 挪机位把它挪进画，不是加强提示词 —— "
                    f"提示词救不了'画面里根本没有它'。"
                )

    # ★ 参考图必须裁成影片的**精确画幅**。实测：16:9 的参考图会静默覆盖
    #   2.39:1 白模的画幅 —— 白膜设计的构图被参考图带跑，还不报错。
    for s in subjects:
        ref_ar = s.get("reference_aspect") or ""
        if ref_ar and fw and fh:
            try:
                a, b = [float(x) for x in str(ref_ar).replace("：", ":").split(":")]
                if b and abs((a / b) - (fw / fh)) > 0.02:
                    warnings.append(
                        f"{s.get('name') or '主体'} 的参考图是 {ref_ar}，"
                        f"而画幅是 {fw:g}x{fh:g}（{fw/fh:.2f}:1）—— 不一致。"
                        f"★ 实测参考图会**静默覆盖**白模画幅，把构图带跑且不报错。"
                        f"先把参考图裁成精确画幅。"
                    )
            except Exception:
                pass

    # lint 全绿 ≠ 运镜对，这句话要跟着结果一起返回，免得被当保证
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "stats": {"subjects": len(subjects), "duration": duration},
        "caveat": "lint 只挡'确定是废的'；运镜对不对必须看画面（抽帧或人眼），别把全绿当通过。",
    }


# ── 分镜表模板 ────────────────────────────────────────────────────────


def shot_template(*, shot_id: str = "", duration: float = 5.0,                  width: int = 1920, height: int = 1080,
                  camera: str = "static", fps: int = 24) -> Dict[str, Any]:
    """一份白膜镜头描述。白膜与 prompt **由这一份数据同时生成**（同源）。

    subjects[].move 是归一化坐标的正数序列 [x, y]（0~1，y 向下）。
    """
    return {
        "shot_id": shot_id,
        "duration": duration,
        "fps": fps,
        "frame": {"width": width, "height": height},
        "ground": True,
        "camera": camera,       # static / dolly_in / dolly_out / pan_left / pan_right / tilt_up / tilt_down / track
        "subjects": [],
        "notes": "",
    }


def subject_template(*, name: str, height: float, x: Optional[float] = None,
                     y: Optional[float] = None, width: Optional[float] = None,
                     **kw: Any) -> Dict[str, Any]:
    """一个主体的标准描述，**把该填的都填上**，免得体检时才发现少字段。

    x/y/width 默认**不填**（而不是填 0）。区别很重要：
      · 不填 → 体检时知道"没量过几何"，只给 warning
      · 填 0 → 那是真的在左上角，出画判定会据此工作
    之前默认成 0，"没量过"和"在原点"分不开，导致出画检查静默失效。

    Args:
        name: 主体名（会出现在 prompt 里，用角色真名）
        height: 像素高。height/frame.height < 10% 且几乎不动
                → 模型会直接把它删掉
        x / y: 左上角像素坐标（y 向下）
        width: 像素宽
        kw: 其余可选字段，常用的有
            * `move`: [[x,y], ...] 轨迹，至少两点；位移 < 0.5 身高
                      且占比 < 10% 会被拦
            * `must_see`: True 表示"这个镜头必须看见它"，体检会检查它是否出画
            * `humanoid`: True 会检查有没有 `facing_marker`（没脸的人形
                          正反面肉眼验不出，实测全员倒着走）
            * `facing_marker`: 朝向标记（鼻子 / 胸标）
            * `reference_aspect`: 参考图比例，如 "16:9"；与画幅不一致会警告
            * `z` / `depth`: 给了就说明需要真三维遮挡 → 升到 B 档白模
            * `color`: **只允许灰**；非灰会被拦（颜色会渗进成片）
            * `textured`: 留空/False；带纹理是反效果
    """
    out: Dict[str, Any] = {"name": name, "height": height}
    if x is not None:
        out["x"] = x
    if y is not None:
        out["y"] = y
    if width is not None:
        out["width"] = width
    out.update(kw)
    return out


# ── A 档：FFmpeg 纯几何白膜（秒级，免依赖）──────────────────────────────


def _ffmpeg() -> str:
    from core.ffmpeg_manager import get_ffmpeg_for_postprocess
    return get_ffmpeg_for_postprocess()


def _run(cmd: List[str], timeout: float = 600.0):
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def _font_file() -> str:
    """挑一个能写中文的字体（白膜上要**写角色名和台词动作**）。"""
    for p in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhbd.ttc",
              r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\simsun.ttc",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if os.path.exists(p):
            return p
    return ""


def _esc_filter(p: str) -> str:
    """把路径转成能塞进 `filter_complex` 的写法（冒号和反斜杠要转义）。"""
    return str(p).replace("\\", "/").replace(":", r"\:")



# ★★ 2026-09-29：**缓入缓出**（修用户说的「抽搐」）。
#   线性插值的起步/停止瞬间速度是跳变的，看起来像抖了一下。
#   smoothstep  S(t)=3t²-2t³  在两端速度为 0、中间最快，起停都平滑。
#   ffmpeg 表达式里写成 t=clip(n/N,0,1); S=(3t²-2t³)·(v1-v0)+v0。
#   注意：仍然用 `n`（帧号）而不是 `t`（时间戳）—— 见下面 `_move_expr` 记的坑。
def _ease_piece(v0: float, v1: float, f0: int, f1: int) -> str:
    """在 [f0,f1) 帧区间内做 smoothstep 插值，返回 ffmpeg 表达式片段。"""
    span = max(1, f1 - f0)
    v0 = float(v0); v1 = float(v1)
    if abs(v1 - v0) < 0.5:
        return "%.0f" % v0
    # tt = (n-f0)/span，用 min/max 夹到 [0,1]；再 3tt^2-2tt^3 映射到 [v0,v1]
    tt = "clip((n-%d)/%d,0,1)" % (f0, span)
    s = "(3*%s*%s-2*%s*%s*%s)" % (tt, tt, tt, tt, tt)
    return "(%.0f+(%.0f)*%s)" % (v0, v1 - v0, s)


def _move_expr(v0: float, v1: float, dur: float, fps: int = 24) -> str:
    r"""线性位移表达式。

    ★★ 2026-09-28 第 74 轮：这里必须用 **`n`（帧号）**，不能用 `t`（时间戳）。

    实测踩到：把主体叠到"已经做过一次 overlay"的链上之后，
    主输入的**时间戳不再推进** ⇒ `t` 永远停在 0 ⇒ 表达式每帧都算成起点
    ⇒ 方块**一动不动**（用户看到的就是「只会挂机，一直是这个画面」）。
    而同一串滤镜把 `t` 换成 `n` 立刻就动起来了（实测质心 365 → 919 → 1483）。

    剂量对照（同一个 chain 形状）：
        overlay=x='154+1248*t/6'  → 质心恒定（不动）✗
        overlay=x='154+1248*n/144' → 质心 365 / 919 / 1483（真的在走）✓
    """
    frames = max(1, int(round(float(dur) * max(1, int(fps)))))
    return _ease_piece(v0, v1, 0, frames)


def _move_expr_multi(points, dur: float, fps: int = 24) -> str:
    r"""**多点多段线性**位移表达式（≥2 个点，可任意回头）。

    ★ 2026-09-29 新增。为什么需要：
      `_move_expr` 只能表达「A → B」一条直线。而剧本里的动作大量是往复的：
        「撑起 → 又栽回水里」（镜1）
        「俯身压向 → 直起身 → 抬铁尺」（镜7）
        「快步上前 → 单膝半蹲 → 转身往巷外走」（镜7 兵丁乙）
      两点插值表达不了回头，只能砍掉一半动作 —— 白膜就不"跟着剧情"了。

    实现：把轨迹按时间**均分**成 N-1 段，用嵌套 `if(lt(n, 帧号), 段i, 段i+1)`
    拼成分段线性。`n` 是 ffmpeg 的**帧号**（不是 `t`）—— 见 `_move_expr`
    里记的那个坑：叠过 overlay 之后时间戳不再推进，用 `t` 会一动不动。
    """
    pts = [float(x) for x in (points or [])]
    if not pts:
        return "0"
    if len(pts) == 1 or abs(pts[-1] - pts[0]) < 0.5 and len(pts) == 2:
        return "%.0f" % pts[0]

    frames = max(1, int(round(float(dur) * max(1, int(fps)))))
    segs = len(pts) - 1
    bounds = [int(round(frames * i / segs)) for i in range(len(pts))]

    def seg_expr(i: int) -> str:
        f0, f1 = bounds[i], bounds[i + 1]
        v0, v1 = pts[i], pts[i + 1]
        span = max(1, f1 - f0)
        return _ease_piece(v0, v1, f0, f1)

    expr = seg_expr(segs - 1)
    for i in range(segs - 2, -1, -1):
        expr = "if(lt(n,%d),%s,%s)" % (bounds[i + 1], seg_expr(i), expr)
    return expr


def _camera_caption(camera: str) -> str:
    return {"dolly_in": "运镜：推近 ↘", "dolly_out": "运镜：拉远 ↗",
            "pan_left": "运镜：向左平移 ←", "pan_right": "运镜：向右平移 →",
            "tilt_up": "运镜：上摇 ↑", "tilt_down": "运镜：下摇 ↓",
            "track": "运镜：跟拍 →", "static": "运镜：固定机位（不动）"
            }.get(camera, f"运镜：{camera}")


def _camera_filter(camera: str, duration: float, w: int, h: int) -> str:
    r"""把运镜名翻成 zoompan 表达式。

    ★★ 2026-09-28 第 74 轮：`d` **必须是 1**（不是总帧数）。

    这是"只会挂机"的**真正元凶**，实测抓到的：

        zoompan 的 `d` 是"**每个输入帧**输出多少帧"。
        写 `d=144`（总帧数）时，它会拿**输入的第 0 帧**复制 144 帧出来；
        而 `-t 6` 只取前 144 帧 ⇒ 整条片子**只有第 0 帧** ⇒
        主体哪怕在链路上真的走了，画面上也**永远是第一帧的样子**。
        （实测：同一串滤镜里 overlay 已经写成 `154+1248*n/144`，
         质心在固定机位下仍然恒定 295.2 —— 就是被这里吃掉的。）

        `d=1` 才是"一帧进一帧出"，运镜表达式用 `on`（输出帧号）照样随时间推进。

    zoompan 的 `fps` 必须显式给，否则默认 25 会改时长 —— 这是 FFmpeg 里
    最容易把时长搞错的地方（本仓栽过）。
    """
    frames = max(1, int(duration * 24))
    z_end = {
        # ★ 2026-09-28 第 74 轮：原来是 1.25 —— 那会裁掉四周 12.5%，
        #   把画面上的**角色名和剧本说明一起裁掉**（实测 t≈10s 时文字没了）。
        #   1.12 既能看出在推近，又保得住文字安全区。
        "dolly_in": 1.12, "dolly_out": 1.0, "static": 1.0,
        "pan_left": 1.06, "pan_right": 1.06, "track": 1.06,
        "tilt_up": 1.06, "tilt_down": 1.06,
    }.get(camera, 1.0)
    z_start = 1.0 if camera != "dolly_out" else 1.12

    x_expr, y_expr = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if camera == "pan_left":
        x_expr = f"(iw-iw/zoom)*(1-on/{frames})"
    elif camera == "pan_right":
        x_expr = f"(iw-iw/zoom)*(on/{frames})"
    elif camera == "tilt_up":
        y_expr = f"(ih-ih/zoom)*(1-on/{frames})"
    elif camera == "tilt_down":
        y_expr = f"(ih-ih/zoom)*(on/{frames})"

    return (f"zoompan=z='{z_start}+({z_end}-{z_start})*on/{frames}'"
            f":x='{x_expr}':y='{y_expr}':d=1"
            f":s={w}x{h}:fps=24")


def render_greybox(spec: Dict[str, Any], out_path: str, *, work_dir: str = "") -> Dict[str, Any]:
    """A 档白膜：纯几何灰模。**秒级、零依赖、免费。**

    构成（每一样都是实测换来的，别随手删）：
      · 中灰底 + 更暗的**地面带** —— 有地面才有接触点，否则模型容易画出浮空脚
      · 纯灰立方块当主体/道具，**明度分三档**（0x9a/0x6e/0xb0）区分层次，
        但**绝不上色** —— 白膜颜色会渗进成片（蓝色人群白模 → 成片蓝面罩人群）
      · 主体脚下一块**接触阴影**（比地面略暗的窄条）—— 让模型知道它站在地上
      · 人形加**朝向标记**（鼻子 + 胸标）—— 人形没有脸时正反面肉眼验不出，
        实测三条片子全员倒着走、三轮审查没发现
      · 三分构图参考线 —— 便于人工确认构图
      · 按 `camera` 施加运动（zoompan）

    Args:
        spec: 见 `shot_template()`
        out_path: 输出 mp4
        work_dir: 预留（当前不用）

    Returns:
        {ok, path, lint, duration, target_duration, duration_ok, note, error?}
    """
    # ★ 2026-09-29：`spec.get("skip_lint")` —— 白膜渲染**跳过 lint**。
    #   lint 那条「小道具占画高<10% 且几乎不动就删掉」是为**成片提示词**写的
    #   （模型确实会忽略画面里的小物件）。但白膜恰恰要**如实画出剧本里的所有道具**
    #   —— 油灯、积水、铁尺、钱袋往往就是这一镜的戏眼，不画就成了"遗漏"。
    #   成片生成仍然要 lint，所以做成开关而不是删掉。
    if spec.get("skip_lint"):
        lint = {"ok": True, "skipped": True, "errors": [], "warnings": []}
    else:
        lint = lint_shot(spec)
    if not lint["ok"]:
        return {"ok": False, "error": "几何体检未过，先修再渲",
                "lint": lint, "path": ""}

    dur = float(spec.get("duration") or 5.0)
    w = int((spec.get("frame") or {}).get("width") or 1920)
    h = int((spec.get("frame") or {}).get("height") or 1080)
    camera = spec.get("camera") or "static"
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    filters = [f"color=c=0x808080:s={w}x{h}:d={dur:.3f}:r=24[bg]"]
    cur = "bg"
    n = 0

    def overlay(x: int, y: int, label: str) -> None:
        """把一个色块叠上去。x/y 会被夹进画幅（负数会让 ffmpeg 报错）。"""
        nonlocal cur, n
        xx = max(0, min(int(x), w - 1))
        yy = max(0, min(int(y), h - 1))
        nxt = f"o{n}"
        filters.append(f"[{cur}][{label}]overlay=x={xx}:y={yy}:shortest=0[{nxt}]")
        cur = nxt
        n += 1

    def overlay_expr(x_expr: str, y_expr: str, label: str) -> None:
        """★ 2026-09-28 第 74 轮：**会动的**色块。

        为什么必须动（用户原话）：
            「白膜视频有，但是只会挂机，就一直是这个画面」
            「你的白膜视频生成没有按照用户的要求来，得跟完全符合剧本指示做」

        查出来的事实：`render_greybox` **从头到尾没用过 `subjects[].move`** ——
        主体永远是钉死在 (x, y) 的一个方块。于是就算白膜里写了走位，
        画面上也是**12 秒一动不动**（实测帧间差异 0，用户看到的就是"挂机"）。
        这里把 `move` 的两个端点插值成 ffmpeg 表达式（`t` 是秒），让方块真的走。
        """
        nonlocal cur, n
        nxt = f"o{n}"
        filters.append(f"[{cur}][{label}]overlay=x='{x_expr}':y='{y_expr}'"
                       f":shortest=0[{nxt}]")
        cur = nxt
        n += 1

    # 要写的文字（角色名 / 运镜 / 剧本动作）—— 用 textfile 传，避免中文转义踩坑
    texts: List[tuple] = []          # (text, x_expr, y_expr, size, color)
    tmp_texts: List[str] = []

    # ── 地面：占底部 18%，比背景略暗。没有它容易出浮空脚 ──
    if spec.get("ground", True):
        gh = max(8, int(h * 0.18))
        filters.append(
            f"color=c=0x6a6a6a:s={w}x{gh}:d={dur:.3f}:r=24,format=rgba[gnd]")
        overlay(0, h - gh, "gnd")

    # ── 主体 ──
    ground_top = h - max(8, int(h * 0.18)) if spec.get("ground", True) else h
    shades = ["0x9a9a9a", "0x6e6e6e", "0xb0b0b0"]
    for i, s in enumerate(spec.get("subjects") or []):
        bw = max(2, int(float(s.get("width") or w * 0.14)))
        bh = max(2, int(float(s.get("height") or h * 0.5)))
        bx = int(float(s.get("x") if s.get("x") is not None else w * 0.4))
        # 没给 y 就让它**站在地面上**（脚贴地），而不是浮在中间
        # ★★ 2026-09-29：**脚底锚点**（修「右肩埋进土里」）。
        #   原来 `y` 是方块**顶部**坐标，调用方若按"中心"给值就会整体下移，
        #   人就埋进地面带里。现在支持 `anchor:"foot"`：把 y 当**脚底地平线**，
        #   内部反推顶边 `by = y - bh`，"站在地上"变成数据保证。
        raw_y = s.get("y")
        _anchor = str(s.get("anchor") or "").strip().lower()
        if raw_y is not None:
            by = int(float(raw_y))
            if _anchor == "foot":
                by -= bh                      # y 是脚底 → 顶边要减掉自身高度
        else:
            by = ground_top - bh              # 默认：脚踩在地面带上沿
        by = max(0, min(by, h - bh))

        # ★★ 分型明度：人 / 道具 / 墙 / 案 / 灯 各自不同灰度，
        #   否则一堆方块分不清谁是人、谁是墙（用户：「不要莫名其妙」）。
        _kind = str(s.get("kind") or ("person" if s.get("humanoid") else "prop")).lower()
        _shade = {"person": ["0x9a9a9a", "0x6e6e6e", "0xb0b0b0"],
                  "prop":   ["0x7c7c7c", "0x5c5c5c", "0x8c8c8c"],
                  "wall":   ["0x585858"],
                  "table":  ["0x6a5a4a"],
                  "light":  ["0xd8d0b0"],
                  }.get(_kind, shades)
        _col = _shade[i % len(_shade)]

        lbl = f"s{i}"
        filters.append(
            f"color=c={_col}:s={bw}x{bh}:d={dur:.3f}:r=24,format=rgba[{lbl}]")
        # ★★ 2026-09-28 第 74 轮：**按 `move` 真的走起来**（原来这里是死坐标）。
        #   用户原话：「白膜视频有，但是只会挂机，就一直是这个画面」。
        #   查出来的事实：这个函数**从头到尾没用过 `subjects[].move`** ——
        #   主体永远钉死在 (x, y)。就算白膜里写了走位，画面上也是 12 秒一动不动。
        mv = s.get("move")
        moved = False
        mx_expr = "%d" % bx
        my_expr = "%d" % by
        if isinstance(mv, list) and len(mv) >= 2:
            try:
                # `move` 是**归一化 0~1**（见 subject_template）；兼容已是像素的旧数据。
                allv = [abs(float(p[0])) for p in mv] + [abs(float(p[1])) for p in mv]
                isnorm = (max(allv) <= 1.5) if allv else True
                xs = [(float(p[0]) * w if isnorm else float(p[0])) - bw / 2 for p in mv]
                ys = [(float(p[1]) * h if isnorm else float(p[1])) - bh / 2 for p in mv]
                # ★ 多点用分段线性（能回头），两点用原来的线性（行为不变）
                if len(mv) >= 3:
                    mx_expr = _move_expr_multi(xs, dur)
                    my_expr = _move_expr_multi(ys, dur)
                else:
                    mx_expr = _move_expr(xs[0], xs[-1], dur)
                    my_expr = _move_expr(ys[0], ys[-1], dur)
                overlay_expr(mx_expr, my_expr, lbl)
                # 角色名跟着人走
                texts.append((str(s.get("name") or "主体%d" % (i + 1)),
                              mx_expr,
                              "(%s)-%d" % (my_expr, max(2, int(bh * 0.16))),
                              max(18, int(h * 0.03)), "0xf2f2f2"))
                moved = True
            except Exception as e:  # noqa: BLE001
                logger.warning("走位表达式生成失败（退回静止）：%s", e)
        if not moved:
            overlay(bx, by, lbl)
            texts.append((str(s.get("name") or "主体%d" % (i + 1)),
                          "%d" % bx, "%d" % max(2, by - int(bh * 0.16)),
                          max(18, int(h * 0.03)), "0xf2f2f2"))

        # ── 接触阴影：主体正下方一条更暗的窄带（暗示它踩在地上）──
        #   ★ 必须**跟着主体走**：原来用的是静态 (bx, by)，
        #     主体一动阴影就掉在原地（画面上是"方块走了、影子没走"）。
        if spec.get("ground", True):
            sh_h = max(3, int(bh * 0.05))
            sh_w = max(4, int(bw * 1.15))
            filters.append(
                f"color=c=0x4a4a4a:s={sh_w}x{sh_h}:d={dur:.3f}:r=24,format=rgba[sh{i}]")
            sh_off = max(0, (sh_w - bw) // 2)
            sh_y = "(%s)+%d" % (my_expr, bh - sh_h // 2)
            if moved:
                overlay_expr("(%s)-%d" % (mx_expr, sh_off),
                             "min(%d,%s)" % (h - sh_h, sh_y), f"sh{i}")
            else:
                overlay(bx - sh_off, min(h - sh_h, by + bh - sh_h // 2), f"sh{i}")

        # ── 人形：朝向标记（鼻尖 + 胸标）──
        #    ★ 人形没有脸时，正面和背面在画面里**完全一样**。
        #      实测三条片子全员倒着走、三轮人工审查都没发现。
        #      加一个凸起就能一眼看出朝向 —— 这是最便宜的对策。
        #    ★ 同样要跟着主体走（否则脸会掉在原地）。
        if s.get("humanoid"):
            nose_off = (bw if (s.get("facing") or "right") == "right" else -bw // 3)
            nose_w = max(3, bw // 4)
            nose_h = max(3, bh // 12)
            filters.append(
                f"color=c=0xd8d8d8:s={nose_w}x{nose_h}:d={dur:.3f}:r=24,format=rgba[nose{i}]")
            chest_w = max(4, int(bw * 0.6))
            chest_h = max(3, bh // 14)
            filters.append(
                f"color=c=0xdedede:s={chest_w}x{chest_h}:d={dur:.3f}:r=24,format=rgba[chest{i}]")
            if moved:
                overlay_expr("(%s)+%d" % (mx_expr, nose_off),
                             "(%s)+%d" % (my_expr, int(bh * 0.12)), f"nose{i}")
                overlay_expr("(%s)+%d" % (mx_expr, max(0, bw // 5)),
                             "(%s)+%d" % (my_expr, int(bh * 0.28)), f"chest{i}")
            else:
                overlay(bx + nose_off, by + int(bh * 0.12), f"nose{i}")
                overlay(bx + max(0, bw // 5), by + int(bh * 0.28), f"chest{i}")

    # ── 三分构图参考线 ──
    filters.append(f"[{cur}]drawgrid=w=iw/3:h=ih/3:t=2:c=0x606060@0.55[grid]")
    cur = "grid"

    # ── ★ 文字：角色名（跟着人走）+ 运镜 + 剧本动作 ──────────────────
    #   用户原话：「白膜视频有，但是只会挂机」「得跟完全符合剧本指示做」。
    #   一块没有名字的灰方块，用户根本看不出"这是谁、在干什么" ——
    #   把名字、运镜、剧本原句写在画面上，白膜才真的是"用来看动向的"。
    font = _font_file()
    if font and texts:
        work = Path(out_path).parent / ("_vtxt_%s" % os.getpid())
        work.mkdir(parents=True, exist_ok=True)
        for k, (txt, xe, ye, size, color) in enumerate(texts):
            tf = work / ("t%d.txt" % k)
            try:
                tf.write_text(str(txt), encoding="utf-8")
            except Exception:  # noqa: BLE001
                continue
            tmp_texts.append(str(tf))
            nxt = f"o{n}"
            filters.append(
                f"[{cur}]drawtext=fontfile='{_esc_filter(font)}'"
                f":textfile='{_esc_filter(str(tf))}':x='{xe}':y='{ye}'"
                f":fontsize={size}:fontcolor={color}:box=1:boxcolor=0x000000@0.45"
                f":boxborderw=6[{nxt}]")
            cur = nxt
            n += 1
        # 底部说明：运镜 + 剧本里的那一句（让用户能对着剧本核对）
        _cap = _camera_caption(camera)
        if spec.get("notes"):
            # 只留 40 字：太长会在右边被裁掉（实测过），宁可短也要完整可读
            _t = str(spec["notes"]).strip().replace("\n", " ")
            _cap += "　|　" + (_t[:40] + "…" if len(_t) > 40 else _t)
        cf = work / "caption.txt"
        try:
            cf.write_text(_cap, encoding="utf-8")
            tmp_texts.append(str(cf))
            nxt = f"o{n}"
            # ★ 说明条要放在**安全区**内：运镜是 zoompan，放大时会裁掉四周，
            #   贴在最底部（h-th-24）实测在 t≈10s（zoom≈1.25）时**被裁没了**。
            #   0.86 这个位置在 1.25 倍放大下仍然完整可见。
            filters.append(
                f"[{cur}]drawtext=fontfile='{_esc_filter(font)}'"
                f":textfile='{_esc_filter(str(cf))}'"
                f":x={int(w * 0.08)}:y={int(h * 0.86)}:fontsize={max(20, int(h * 0.034))}"
                f":fontcolor=0xf7e08a:box=1:boxcolor=0x000000@0.55:boxborderw=10[{nxt}]")
            cur = nxt
            n += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("白膜底部说明写不上去（不影响画面）：%s", e)

    # ── 运镜放最后，避免参考线跟着抖 ──
    filters.append(f"[{cur}]{_camera_filter(camera, dur, w, h)}[out]")

    cmd = [
        _ffmpeg(), "-y", "-v", "error",
        "-filter_complex", ";".join(filters),
        "-map", "[out]",
        "-t", f"{dur:.3f}",
        "-r", "24", "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        out_path,
    ]
    try:
        p = _run(cmd)
        if p.returncode != 0 or not os.path.exists(out_path):
            return {"ok": False, "path": "",
                    "error": f"ffmpeg 失败 rc={p.returncode}: {(p.stderr or '')[:400]}",
                    "lint": lint, "cmd": " ".join(cmd)}
        actual = probe_duration(out_path)
        # ★ 「这一版到底动没动」如实带出去（用户报的就是"只会挂机"）
        moving = [str(s.get("name") or "主体%d" % (i + 1))
                  for i, s in enumerate(spec.get("subjects") or [])
                  if isinstance(s.get("move"), list) and len(s.get("move") or []) >= 2]
        return {"ok": True, "path": out_path, "lint": lint,
                "duration": actual, "target_duration": dur,
                "duration_ok": abs(actual - dur) < 0.2,
                "moving_subjects": moving,
                "text_overlay": bool(texts and font),
                "note": ("A 档纯几何白膜：含地面/接触阴影/人形朝向标记/三分线，"
                         "画面上写了**角色名 / 运镜 / 剧本那一句** —— "
                         "但它是平面的，**不验三维走位与遮挡**"
                         + ("；有 %d 个主体按走位在动" % len(moving) if moving
                            else "；**这一镜没有走位**（主体静止，只有运镜在动）"))}
    except Exception as e:
        return {"ok": False, "path": "", "error": f"{type(e).__name__}: {e}", "lint": lint}
    finally:
        # 临时文本文件不留给用户（它们只是 drawtext 的输入）
        for t in tmp_texts:
            try:
                os.remove(t)
            except Exception:  # noqa: BLE001
                pass
        if tmp_texts:
            try:
                os.rmdir(os.path.dirname(tmp_texts[0]))
            except Exception:  # noqa: BLE001
                pass


def probe_duration(path: str) -> float:
    """用 ffmpeg -i 读时长（不依赖 ffprobe，项目里其它模块也是这么做的）。"""
    import re
    try:
        p = _run([_ffmpeg(), "-hide_banner", "-i", path], timeout=60)
        m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", p.stderr or "")
        if not m:
            return 0.0
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        return 0.0


def extract_frames(video_path: str, out_dir: str, *, count: int = 3) -> List[str]:
    """抽帧供人工/模型审查。

    ★ 为什么非做不可：实测五个白模 lint 全绿、时长全对，
      抽帧一看运镜五个全坏。**不看画面的质检等于没质检。**
    """
    dur = probe_duration(video_path)
    if dur <= 0 or count <= 0:
        return []
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    out: List[str] = []
    for i in range(count):
        t = dur * (i + 1) / (count + 1)
        p = os.path.join(out_dir, f"frame_{i + 1}_{t:.2f}s.jpg")
        r = _run([_ffmpeg(), "-y", "-v", "error", "-ss", f"{t:.3f}",
                  "-i", video_path, "-frames:v", "1", "-q:v", "2", p], timeout=120)
        if r.returncode == 0 and os.path.exists(p) and os.path.getsize(p) > 0:
            out.append(p)
    return out


# ── 白膜 → prompt 同源生成 ────────────────────────────────────────────

# 运镜名 → 中文。提到模块级是因为**有两处要用**：
#   `spec_to_prompt()` 生成 prompt，以及 `previz_ref` 在"平台不支持视频参考"
#   时要把运镜降级写进 prompt。两份翻译表分头维护一定会漂。
LABELS_CAMERA: Dict[str, str] = {
    "static": "固定机位，画面不动",
    "dolly_in": "镜头缓慢推近",
    "dolly_out": "镜头缓慢拉远",
    "pan_left": "镜头向左平移",
    "pan_right": "镜头向右平移",
    "tilt_up": "镜头向上摇",
    "tilt_down": "镜头向下摇",
    "track": "镜头跟随主体平移",
}


def spec_to_prompt(spec: Dict[str, Any], *, style_prompt: str = "") -> str:
    """从白膜 spec 生成**与之同源**的成片 prompt。

    ★ 为什么要从同一份 spec 生成：研究结论说得很直白 ——
      "prompt 与白膜必须同源，分头演化会导致 prompt 承诺一个白膜里
      根本没有的运镜。"（实测中招）
      所以不要让 LLM 再自由发挥一遍运镜描述，直接从几何数据翻译。
    """
    camera_zh = LABELS_CAMERA
    parts: List[str] = []
    if style_prompt:
        parts.append(style_prompt.strip())

    parts.append(f"时长约 {float(spec.get('duration') or 5):g} 秒。")
    parts.append(camera_zh.get(spec.get("camera") or "static", "固定机位"))

    subs = spec.get("subjects") or []
    if subs:
        desc = []
        for s in subs:
            bits = [s.get("name") or "主体"]
            if s.get("action"):
                bits.append(f"（{s['action']}）")
            desc.append("".join(bits))
        parts.append("画面主体：" + "、".join(desc) + "。")

        # 位移方向也要说出来，否则模型自己编一套运动
        moves = []
        for s in subs:
            mv = s.get("move") or []
            if len(mv) >= 2:
                try:
                    dx = float(mv[-1][0]) - float(mv[0][0])
                    dy = float(mv[-1][1]) - float(mv[0][1])
                except Exception:
                    continue
                if abs(dx) < 1e-6 and abs(dy) < 1e-6:
                    continue
                horiz = "右" if dx > 0.02 else ("左" if dx < -0.02 else "")
                vert = "下" if dy > 0.02 else ("上" if dy < -0.02 else "")
                moves.append(f"{s.get('name') or '主体'}向{horiz}{vert}移动")
        if moves:
            parts.append("运动：" + "；".join(moves) + "。")

    if spec.get("notes"):
        parts.append(str(spec["notes"]).strip())

    return " ".join(p for p in parts if p)


def reference_note(spec: Dict[str, Any], *, keep: str = "") -> str:
    """给"白膜当参考素材"时的用法说明。

    ★ 官方语法是 `@Clay Render 1`（Seedance 2.5 把 clay render 列为一等
      参考类型）。这里只负责**告诉用户/调用方怎么引用**，不负责拼接 ——
      拼接交给 core/platform_refs.py（各平台方言不同）。
    """
    return (
        "白膜参考用法：把白膜视频作为参考素材上传，prompt 里用 `@Clay Render 1` 指代。\n"
        "白膜只提供空间关系 / 运镜路径 / 时间节奏，"
        "材质光线色彩造型由参考图决定。\n"
        "★ 只给首帧图当 i2v 输入是**不充分**的 —— 模型对'接下来怎么动'没有约束，"
        "会自己编运镜。白膜正是补上这一块的。\n"
        "★ 带纹理的白模是反效果，务必纯灰 blockout。\n"
        + (f"本镜头要保留的：{keep}" if keep else "")
    )


# ── 流程状态（存 db）────────────────────────────────────────────────


def get_state(shot: Dict[str, Any]) -> Dict[str, Any]:
    raw = shot.get("previz_status") or S_NOT_STARTED
    if raw not in VALID_STATES:
        raw = S_NOT_STARTED
    notes = shot.get("previz_notes") or ""
    history: List[Dict[str, Any]] = []
    if notes:
        try:
            history = json.loads(notes)
            if not isinstance(history, list):
                history = []
        except Exception:
            history = []
    return {
        "status": raw,
        "path": shot.get("previz_path") or "",
        "final_approved": bool(shot.get("final_approved")),
        "history": history,
        "retries": {
            "G1": sum(1 for h in history if h.get("gate") == "G1" and h.get("action") == "reject"),
            "G2": sum(1 for h in history if h.get("gate") == "G2" and h.get("action") == "reject"),
            "G3": sum(1 for h in history if h.get("gate") == "G3" and h.get("action") == "reject"),
        },
    }


def reject(reason_code: str, *, gate: str = "G1",
           comment: str = "") -> Dict[str, Any]:
    """生成一条驳回记录。返回值可以直接落进 history，也可以直接回给用户看。"""
    cls = classify_reason(reason_code)
    gate = gate if gate in GATE_RETRIES else "G1"
    return {
        "action": "reject",
        "gate": gate,
        "reason": cls["code"],
        "label": cls["label"],
        "scope": cls["scope"],
        "regenerate_draft": cls["regenerate_draft"],
        "back_to": cls["back_to"],
        "comment": comment,
        "advice": cls["advice"],
        "retry_limit": GATE_RETRIES[gate],
    }


def approve(*, gate: str = "G1", comment: str = "") -> Dict[str, Any]:
    return {"action": "approve", "gate": gate, "comment": comment}


def next_status_for(action: Dict[str, Any], current: str) -> str:
    """按"批准/驳回 + 理由码"推出下一个状态。纯函数，便于单测。"""
    if action.get("action") == "approve":
        return {
            S_DRAFT_READY: S_DRAFT_APPROVED,
            S_FINAL_READY: S_FINAL_READY,
            S_DRAFT_APPROVED: S_DRAFT_APPROVED,
        }.get(current, current)
    # 驳回
    if action.get("regenerate_draft"):
        return S_NOT_STARTED        # 回 G1：白膜要重渲
    if current in (S_DRAFT_READY, S_FINAL_READY):
        return S_DRAFT_RUNNING      # 回 G2：白膜不动，只重出样片
    return current


def render_plan(spec: Dict[str, Any]) -> Dict[str, Any]:
    """告诉调用方这个镜头该走哪一档白膜。

    ★ **A / B / C 三档现在全部内置**（纯 Python + ffmpeg，零第三方依赖）：
        A 档：只要验节奏/构图   → 本地平面灰模视频，秒级        ← 默认
        B 档：要验三维走位/遮挡 → 多机位**静态**联络表（俯视/正面/侧面）
        C 档：要选机位/看运动中遮挡 → 多机位**视频** + 覆盖片（真透视）

    历史备注：C 档以前写的是"用导演台或 Blender（外部工具）"，
    这等于没有 —— 要用户先装环境的功能就是不存在的功能。现在判定与渲染
    都收在 `previz_multi.py` 里（判定是纯函数 `render_plan_for`，有单测）。
    """
    try:
        from core import previz_multi as _pm
        return _pm.render_plan_for(spec)
    except Exception as e:      # 兜底：绝不让排档失败挡住白膜
        logger.warning("多机位判档不可用（%s），按 A 档走", e)

    subs = spec.get("subjects") or []
    need_3d = any(s.get("z") is not None or s.get("depth") for s in subs)
    if need_3d:
        tier, why = "B", "需要三维走位/遮挡关系 → 多机位静态联络表"
    else:
        tier, why = "A", "只需验节奏与构图 → 平面灰模视频，最快"
    return {"tier": tier, "why": why, "builtin": True,
            "free": True, "cost_note": "白膜阶段成本为 0，可无限重试"}
