# -*- coding: utf-8 -*-
"""
动作序列设计引擎
================

★ 为什么需要它（用户实测反馈的原话）
────────────────────────────────────
「出的视频，没人类爱看！——就是视频设计出的各种动作的顺序不符合人类的观赏习惯。」

根因不是画质，也不是模型不行，而是**镜头密度**：

  之前：剧本 → 12 个分镜 → **每镜就是一个 13~15 秒的连续长镜**。
        观众看到的是「一个人在水里慢慢动」「一伙人在巷子里站着」——
        没有任何节奏起伏，也没有信息增量。

  人类观赏习惯要的是**高密度短镜头切换**：
    · 动作模式 15 秒 → **至少 10 个镜头**，每镜约 1.2~2.4 秒
    · 对峙模式 15 秒 → **至少 5 个镜头**，每镜约 2~4 秒

  而且镜头数**不等于招式数**：一条动作因果链要拆成不同「观看重点」——
    全景交代空间 → 中近景看抓腕 → 脚部特写看重心 → 主观镜头看攻击逼近 → 反应近景看受力。
  这才是「顺序符合观赏习惯」。

★ 来源
  吸收 `script-to-shot-engine v2.3.2`（用户提供的 Skill 包）的核心规则：
    · SKILL.md 的「动作容量」「三层信息规则」
    · references/action-choreography-rules.md 的「动作语法」「摄影选择」
    · references/seedance-prompt-renderer.md 的「镜头必要成分」「光圈焦段对照」

★ 本模块的职责
  1. 给 LLM 一份**可执行的规则**（不是让它自由发挥）；
  2. 把它的输出**解析成结构化镜头表**；
  3. **机器校验**（时间轴连续、镜头数达标、每镜有参数+动作）——
     校验不过就打回重写。这一步是「符合观赏习惯」的保证：
     光靠提示词要求，模型会偷懒写成 5 个长镜。
  4. 输出**按时间顺序排列的提示信息**，并转成白膜的设计表。

★ 与出片的关系
  每个**短镜头**是一笔独立的视频生成调用。镜头变多 → 单镜变短。
  好处：单镜 1.2~2.4 秒**永远在付费模型的能力范围内**，
  不再需要分段串联，也就绕开了「段末帧被判真人而拒收」的老毛病。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# ══════════════════════════════════════════════════════════════
#  1. 容量规则（来自 SKILL.md「动作容量」）
# ══════════════════════════════════════════════════════════════
MODE_RULES: Dict[str, Dict[str, Any]] = {
    "action": {
        "zh": "动作模式",
        "min_per_15s": 10,          # 硬下限：完整 15 秒不得少于 10 个镜头
        "max_per_15s": 18,
        "shot_sec": (1.0, 2.6),     # 单镜典型时长
        "units_per_15s": (2, 3),    # 主要攻防单元
        "usage": "存在攻防、追逐、破坏或法术释放",
    },
    "dialogue": {
        "zh": "对峙模式",
        "min_per_15s": 5,
        "max_per_15s": 8,
        "shot_sec": (2.0, 4.0),
        "units_per_15s": (1, 2),    # 张力单元
        "usage": "动作由台词、视线和微动作（掐烟、合扇、按桌、离座）推进",
    },
}

#: 中文景别 → 焦段（mm）中值，用于把文字镜头转成白膜机位
_SHOT_SIZE_MM: List[Tuple[str, float]] = [
    ("大特写", 220.0), ("特写", 150.0), ("近景", 95.0),
    ("中近景", 68.0), ("中景", 45.0), ("中远景", 36.0),
    ("全景", 28.0), ("大全景", 22.0),
]

_APERTURE_DEPTH = [
    ((1.0, 2.0), "极浅景深，背景强烈虚化"),
    ((2.2, 2.8), "浅景深，背景明显虚化"),
    ((4.0, 5.6), "中等景深，背景轻微虚化"),
    ((8.0, 11.0), "深景深，前后景基本清晰"),
    ((16.0, 64.0), "全景深，远近皆清晰"),
]


def depth_for(aperture: float) -> str:
    for (lo, hi), txt in _APERTURE_DEPTH:
        if lo <= aperture <= hi:
            return txt
    return "浅景深，背景明显虚化"


def shot_size_for(mm: float) -> str:
    """焦段 → 中文景别（与渲染器对照表一致）。"""
    if mm <= 24:
        return "大全景"
    if mm <= 35:
        return "全景"
    if mm <= 55:
        return "中景"
    if mm <= 79:
        return "中近景"
    if mm <= 120:
        return "近景"
    if mm <= 200:
        return "特写"
    return "大特写"


# ══════════════════════════════════════════════════════════════
#  2. 规则提示词 —— 给 LLM 的执行约束（不是建议）
# ══════════════════════════════════════════════════════════════
ACTION_SEQUENCE_SYSTEM = """你是影视动作场面的分镜师。把用户给的一场戏，拆成**高密度短镜头**的动作序列。

## 你要解决的唯一问题
观众爱不爱看，取决于**镜头密度**。一个人物慢慢动 15 秒，观众会关掉。
15 秒里切 10~14 次镜头、每次只给出一个新的信息点，观众才会看下去。

## 硬性规则（不满足就是错的）
1. **镜头数下限**：动作模式每 15 秒 ≥ 10 个镜头；对峙模式每 15 秒 ≥ 5 个。按比例缩放，不足 15 秒时按比例取整（至少 3）。
2. **时间轴严格连续**：第一个镜头从 0 秒开始，相邻镜头首尾相接，最后一个镜头**准确结束在总时长**。不得重叠、不得留空。
3. **每个镜头必须同时有**：光圈 + 焦段 + 一个摄影描述（景别/机位/运镜）+ 一个可见动作或结果。
4. **镜头 ≠ 招式**。同一段连续动作可以用多个镜头覆盖，每个镜头换个「观看重点」：
   全景交代双方位置 → 中近景看抓腕/接触点 → 脚部特写看重心与脚步 →
   主观镜头看攻击逼近 → 反应近景看受力 → 广角看位移与结果。
   **不得让同一个动作在每次切镜后重新发生**；用「顺势、承接、同时、动作未停」保持连贯。
5. **动作因果链**：每个主要动作必须有
   `局势与距离 → 发起 → 防守/闪避/命中 → 受力与位移 → 恢复或新局势`。
   前一个动作的结果要成为下一个动作的条件（格挡打开的空当、撞墙造成的失衡、被封死的退路）。
6. **对峙模式**（无攻防，靠台词/视线/微动作推进）：张力因果链
   `施压 → 承受 → 泄露或反制 → 新平衡`。台词写**完整原句、不跨镜**；
   听方反应镜头**不得连续超过两个**；镜头功能配额（每 15 秒）：
   建立空间 ≤1、说话人 ≤3、听方反应 ≤2、微动作特写 ≤2、局势变化 1。
7. **轴线**：所有普通机位保持在动作轴同一侧。不得无因写「轴线另一侧」。
8. 不要写「激战」「连续进攻」「高速缠斗」这种没有结果的空话。

## 参数规范（格式错了要重写）
- 光圈写 `f/2.8`（不写 f2.8），值后紧跟中文景深描述：`光圈 f/2.8（浅景深，背景明显虚化）`
  · f/1.2~2 极浅景深，背景强烈虚化｜f/2.2~2.8 浅景深，背景明显虚化
  · f/4~5.6 中等景深，背景轻微虚化｜f/8~11 深景深，前后景基本清晰｜f/16+ 全景深，远近皆清晰
- 焦段写 `80mm`，值后紧跟中文景别：`焦段 80mm（近景）`
  · ≤24mm 大全景｜25~35mm 全景｜36~55mm 中景｜56~79mm 中近景｜80~120mm 近景｜121~200mm 特写｜200+ 大特写
- 参数与功能匹配：24~35mm 拍空间与位移；35~50mm 拍中景攻防；80~100mm 拍表情/抓腕/接触；135~200mm 拍手脚武器强压缩细节。
- 摄影术语首次出现时写 `中文（English）`，同一场里同一词只附一次英文。
  常用：全景 full shot｜中景 medium shot｜近景 close-up｜特写 close-up detail｜
  推进 push in｜后拉 pull out｜横摇 pan｜上摇/下摇 tilt up/tilt down｜横移 truck｜
  跟拍 tracking shot｜环绕 orbit｜升降 crane｜手持 handheld｜固定机位 static shot｜
  低角度 low angle｜高角度 high angle｜俯拍 overhead shot｜荷兰角 Dutch angle｜
  第一视角 POV｜过肩 over-the-shoulder｜升格 slow motion

## 输出格式（严格遵守，不要加任何解释文字）
第一行：
空间站位：<主要角色在画面左/右/前/后/高/低，彼此面向，初始距离与姿势，动作轴，关键道具在谁手上>

随后每个镜头一行：
镜头N（起始-结束秒）：光圈 f/x（景深描述），焦段 xxmm（景别），<景别/机位/运镜>，<本镜头新发生的动作、接触、受力或位移结果>

最后两行：
结尾状态：<人物位置、朝向、姿势、道具归属、环境结果>
约束：<3~6 项本场最容易失败的连续性问题，至少三项要引用本场具体事实（人名、持物、已熄的灯等）>

用中文数字编号（镜头一、镜头二…）。时间保留一位小数，用半角连字符。
总字数 15 秒目标 1000~2000 汉字。不要输出风格词、不要输出质量词、不要输出解释。"""


# ══════════════════════════════════════════════════════════════
#  3. 解析
# ══════════════════════════════════════════════════════════════
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
           "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12, "十三": 13,
           "十四": 14, "十五": 15, "十六": 16, "十七": 17, "十八": 18,
           "十九": 19, "二十": 20, "二十一": 21, "二十二": 22, "二十三": 23,
           "二十四": 24, "二十五": 25, "二十六": 26, "二十七": 27,
           "二十八": 28, "二十九": 29, "三十": 30}

# 镜头二（1.2-2.4秒）：  /  镜头2（1.2-2.4秒）：
_SHOT_RE = re.compile(
    r"^\s*镜头\s*([一二三四五六七八九十百0-9]+)\s*[（(]\s*"
    r"([0-9]+(?:\.[0-9]+)?)\s*[-–—~]\s*([0-9]+(?:\.[0-9]+)?)\s*秒?\s*[)）]\s*[:：]\s*(.*)$"
)
_APERTURE_RE = re.compile(r"光圈\s*f?/?\s*([0-9]+(?:\.[0-9]+)?)")
_FOCAL_RE = re.compile(r"焦段\s*([0-9]+(?:\.[0-9]+)?)\s*mm")
_MM_RE = re.compile(r"([0-9]+)\s*mm")


def _cn2int(tok: str) -> int:
    tok = tok.strip()
    if tok.isdigit():
        return int(tok)
    if tok in _CN_NUM:
        return _CN_NUM[tok]
    # 「二十三」这类组合
    if "十" in tok:
        a, _, b = tok.partition("十")
        n = (_CN_NUM.get(a, 1) if a else 1) * 10 + (_CN_NUM.get(b, 0) if b else 0)
        return n
    return 0


def parse_shots(text: str) -> Dict[str, Any]:
    """
    把渲染器格式的正文解析成结构化镜头表。

    Returns:
        {"space": str, "shots": [{index, start, end, sec, aperture, focal,
                                  size, body, raw}], "ending": str, "constraint": str}
    """
    space, ending, constraint = "", "", ""
    shots: List[Dict[str, Any]] = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("空间站位"):
            space = s.split("：", 1)[-1].strip()
            continue
        if s.startswith("结尾状态"):
            ending = s.split("：", 1)[-1].strip()
            continue
        if s.startswith("约束"):
            constraint = s.split("：", 1)[-1].strip()
            continue
        m = _SHOT_RE.match(s)
        if not m:
            continue
        idx_raw, a, b, body = m.group(1), float(m.group(2)), float(m.group(3)), m.group(4).strip()
        ap = _APERTURE_RE.search(body)
        fo = _FOCAL_RE.search(body)
        aperture = float(ap.group(1)) if ap else 0.0
        focal = float(fo.group(1)) if fo else 0.0
        shots.append({
            "index": _cn2int(idx_raw) or (len(shots) + 1),
            "cn": idx_raw,
            "start": a, "end": b,
            "sec": round(max(0.1, b - a), 2),
            "aperture": aperture,
            "focal": focal,
            "size": shot_size_for(focal) if focal else "",
            "body": body,
            "raw": s,
        })
    return {"space": space, "shots": shots, "ending": ending,
            "constraint": constraint}


def validate(parsed: Dict[str, Any], total: float, mode: str = "action",
             tol: float = 0.35) -> Dict[str, Any]:
    """
    **机器校验** —— 这一步才是「符合人类观赏习惯」的真正保证。

    提示词里写了「至少 10 个镜头」，模型仍然会偷懒写成 5 个。
    所以必须由代码数出来、对时间轴，不达标就打回重写。

    Returns: {"ok": bool, "errors": [...], "warnings": [...], "stats": {...}}
    """
    rules = MODE_RULES.get(mode) or MODE_RULES["action"]
    shots = parsed.get("shots") or []
    errors: List[str] = []
    warnings: List[str] = []

    n = len(shots)
    # 下限按 15 秒比例缩放；不足 15 秒时给一个不荒谬的地板
    if total >= 15:
        need = rules["min_per_15s"] * (total / 15.0)
    else:
        need = max(3, round(rules["min_per_15s"] * (total / 15.0)))
    need = int(need)
    if n < need:
        errors.append(
            f"镜头数不足：{rules['zh']} {total:g} 秒至少要 {need} 个镜头，只给了 {n} 个。"
            f"（每镜约 {rules['shot_sec'][0]:g}~{rules['shot_sec'][1]:g} 秒）")
    if n > int(rules["max_per_15s"] * (total / 15.0)) + 3:
        warnings.append(f"镜头偏多（{n} 个），节奏可能碎，建议不超过 "
                        f"{int(rules['max_per_15s'] * (total / 15.0))} 个")

    if not shots:
        return {"ok": False, "errors": errors or ["没有解析出任何镜头行"], "warnings": warnings,
                "stats": {"shots": 0, "need": need}}

    # 时间轴连续
    if abs(shots[0]["start"]) > 0.01:
        errors.append(f"第一个镜头应从 0 秒开始，实际从 {shots[0]['start']:g} 秒开始")
    for i in range(1, n):
        gap = shots[i]["start"] - shots[i - 1]["end"]
        if abs(gap) > tol:
            errors.append(
                f"镜头 {shots[i-1]['cn']} 结束在 {shots[i-1]['end']:g} 秒，"
                f"镜头 {shots[i]['cn']} 却从 {shots[i]['start']:g} 秒开始"
                f"（{'重叠' if gap < 0 else '留空'} {abs(gap):.1f} 秒）")
    tail = shots[-1]["end"] - total
    if abs(tail) > tol:
        errors.append(f"最后一个镜头结束在 {shots[-1]['end']:g} 秒，"
                      f"但本段总时长是 {total:g} 秒（差 {tail:+.1f} 秒）")

    # 每镜必须同时有参数和动作
    no_param, no_body, too_long = [], [], []
    for sh in shots:
        if not sh["aperture"] or not sh["focal"]:
            no_param.append(sh["cn"])
        if len(sh["body"]) < 12:
            no_body.append(sh["cn"])
        if sh["sec"] > rules["shot_sec"][1] + 1.6:
            too_long.append(f"{sh['cn']}({sh['sec']:g}s)")
    if no_param:
        errors.append(f"这些镜头缺光圈或焦段：{'、'.join(no_param)}")
    if no_body:
        warnings.append(f"这些镜头没写出可见动作：{'、'.join(no_body)}")
    if too_long:
        errors.append(f"这些镜头过长，节奏会拖：{'、'.join(too_long)}"
                      f"（单镜上限约 {rules['shot_sec'][1]:g} 秒）")

    secs = [s["sec"] for s in shots]
    stats = {"shots": n, "need": need, "mode": rules["zh"],
             "total": total, "avg_sec": round(sum(secs) / len(secs), 2),
             "min_sec": min(secs), "max_sec": max(secs),
             "chars": len(parsed.get("space") or "") + sum(len(s["body"]) for s in shots)}
    return {"ok": not errors, "errors": errors, "warnings": warnings, "stats": stats}


# ══════════════════════════════════════════════════════════════
#  4. 输出：按时间顺序排列的提示信息 + 转白膜设计表
# ══════════════════════════════════════════════════════════════
def render_timeline(parsed: Dict[str, Any], total: float, mode: str = "action") -> str:
    """
    输出「按时间顺序排列的提示信息」——这就是要应用到白膜与出片的那份表。

    含每镜的 起止时间 / 光圈 / 焦段 / 景别 / 动作，按时间正序。
    """
    lines = [f"# 动作序列（{MODE_RULES.get(mode, MODE_RULES['action'])['zh']}｜"
             f"总时长 {total:g}s｜{len(parsed.get('shots') or [])} 镜）", ""]
    if parsed.get("space"):
        lines += [f"**空间站位**：{parsed['space']}", ""]
    lines += ["| # | 时间 | 时长 | 光圈 | 焦段 | 景别 | 动作与结果 |",
              "|---|---|---|---|---|---|---|"]
    for sh in parsed.get("shots") or []:
        lines.append(
            f"| {sh['cn']} | {sh['start']:g}-{sh['end']:g}s | {sh['sec']:g}s | "
            f"f/{sh['aperture']:g} | {sh['focal']:g}mm | {sh['size']} | {sh['body']} |")
    if parsed.get("ending"):
        lines += ["", f"**结尾状态**：{parsed['ending']}"]
    if parsed.get("constraint"):
        lines += [f"**约束**：{parsed['constraint']}"]
    return "\n".join(lines)


#: 景别 → 白膜机位（距主体距离 m, 机位高 m, look 高 m）
_PREVIZ_CAM: Dict[str, Tuple[float, float, float]] = {
    "大全景": (7.5, 1.75, 0.95),
    "全景": (5.0, 1.65, 0.90),
    "中远景": (4.0, 1.60, 0.90),
    "中景": (3.1, 1.55, 0.90),
    "中近景": (2.3, 1.50, 0.95),
    "近景": (1.5, 1.45, 1.05),
    "特写": (1.0, 1.40, 1.10),
    "大特写": (0.7, 1.35, 1.10),
}


def to_previz_shots(parsed: Dict[str, Any], total: float) -> List[Dict[str, Any]]:
    """
    把镜头表转成**白膜渲染可以直接用**的逐镜参数。

    关键：一个 15 秒的段，在白膜里要渲成 **10+ 个独立机位**，
    而不是一个长镜。白膜正是用来验「这套镜头切得对不对」的：
    镜头数、时间轴、每镜的景别变化，在灰模里一眼就能看出来。
    """
    out: List[Dict[str, Any]] = []
    for sh in parsed.get("shots") or []:
        size = sh["size"] or "中景"
        dist, cam_h, look_h = _PREVIZ_CAM.get(size, _PREVIZ_CAM["中景"])
        out.append({
            "index": sh["index"],
            "cn": sh["cn"],
            "start": sh["start"],
            "end": sh["end"],
            "sec": sh["sec"],
            "aperture": sh["aperture"],
            "focal": sh["focal"],
            "size": size,
            "cam_dist": dist,
            "cam_z": cam_h,
            "look_z": look_h,
            "body": sh["body"],
        })
    return out


#: 动作模式的关键词（命中就按动作模式走）
_ACTION_HINTS = ("打", "斗", "杀", "剑", "刀", "拳", "踢", "追", "逃", "擒",
                  "斩", "撞", "押", "拔", "挥", "扑", "夺", "锤", "鞭", "踢翻",
                  "砍", "刺", "招", "攻", "防", "抡", "摁", "扯", "挣", "踹")
#: 对峙模式的关键词
_DIALOGUE_HINTS = ("说", "问", "道", "答", "沉声", "冷笑", "喝", "叱", "吼",
                    "吟", "念", "低声", "反唇", "揶揄", "逼问", "坦白", "认", "审")


def infer_mode(scene_text: str) -> str:
    """判断这一场是「动作」还是「对峙」。"""
    t = scene_text or ""
    na = sum(t.count(k) for k in _ACTION_HINTS)
    nd = sum(t.count(k) for k in _DIALOGUE_HINTS)
    return "action" if na >= nd else "dialogue"


# ══════════════════════════════════════════════════════════════
#  5. 设计（调 LLM + 校验回环）
# ══════════════════════════════════════════════════════════════
async def design_sequence(llm, scene_text: str, total_seconds: float = 15.0,
                          mode: Optional[str] = None, *, max_retry: int = 3,
                          asset_anchors: str = "") -> Dict[str, Any]:
    """
    把一场戏拆成高密度短镜头动作序列。

    ★ 为什么要有「校验 → 失败就带上错误重写」这个回环：
      提示词里写「至少 10 个镜头」，模型仍然经常只给 5 个长镜。
      光靠提示词是不够的 —— 必须由代码数出来、对时间轴，
      不达标就把**具体哪一条不合格**喂回去让它改。
      实测这是唯一能稳定拿到合格序列的办法。

    Returns:
        {ok, mode, text, parsed, validation, timeline, previz_shots, rounds, errors}
    """
    mode = mode or infer_mode(scene_text)
    rules = MODE_RULES.get(mode) or MODE_RULES["action"]
    total = float(total_seconds or 15.0)
    if total < 15:
        need = max(3, round(rules["min_per_15s"] * (total / 15.0)))
    else:
        need = int(rules["min_per_15s"] * (total / 15.0))

    user = (f"【本场内容】\n{scene_text.strip()}\n\n"
            f"【本段参数】\n"
            f"· 模式：{rules['zh']}（{rules['usage']}）\n"
            f"· 总时长：{total:g} 秒\n"
            f"· **镜头数不得少于 {need} 个**（每镜约 "
            f"{rules['shot_sec'][0]:g}~{rules['shot_sec'][1]:g} 秒）\n"
            f"· 最后一个镜头必须准确结束在 {total:g} 秒\n"
            f"· 主要{'攻防' if mode == 'action' else '张力'}单元 "
            f"{rules['units_per_15s'][0]}~{rules['units_per_15s'][1]} 个\n")
    if asset_anchors:
        user += (f"\n【本段可用资产（必须用这些名字，不得改名）】\n{asset_anchors}\n"
                 f"每个资产首次出现时写一次短锚点，后续只写资产名。\n")
    user += "\n直接输出规定格式的正文，不要任何解释或前言。"

    last: Dict[str, Any] = {}
    for rnd in range(1, max(1, max_retry) + 1):
        messages = [{"role": "system", "content": ACTION_SEQUENCE_SYSTEM},
                    {"role": "user", "content": user}]
        try:
            text = await llm.chat(messages, temperature=0.5, max_tokens=16000,
                                  reasoning_effort="low")
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "mode": mode, "rounds": rnd,
                    "errors": [f"调用模型失败：{type(e).__name__}: {e}"]}
        text = (text or "").strip()
        parsed = parse_shots(text)
        val = validate(parsed, total, mode)
        last = {"ok": val["ok"], "mode": mode, "text": text, "parsed": parsed,
                "validation": val, "rounds": rnd, "errors": val["errors"],
                "timeline": render_timeline(parsed, total, mode) if parsed["shots"] else "",
                "previz_shots": to_previz_shots(parsed, total)}
        if val["ok"]:
            return last
        # 把**具体哪条不合格**喂回去 —— 不要只说「重写」
        user += (f"\n\n【上一次不合格，必须修正】\n"
                 + "\n".join(f"- {e}" for e in val["errors"])
                 + f"\n（你上次只给了 {len(parsed['shots'])} 个镜头，要求至少 {need} 个。"
                   f"请把同一条动作因果链拆成更多镜头，每个镜头换一个观看重点："
                   f"全景交代位置、中近景看接触点、脚部特写看重心、主观镜头看逼近、"
                   f"反应近景看受力。）\n"
                   "请重新输出完整正文。")
    return last
