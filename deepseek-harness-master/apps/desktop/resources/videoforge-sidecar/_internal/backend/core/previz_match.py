# -*- coding: utf-8 -*-
"""
VideoForge · 白膜 ↔ 分镜提示词 一致性检查（先让"出入"可见）
============================================================

用户实测（2026-10-23）：
    「白膜视频跟分镜提示词出入很大」
    「白膜视频要效果至少能匹配分镜提示词的描述吧！」

## 查到的根因（不是猜）

项目里**白膜和提示词是两套编译器**产出的：

  · 白膜 spec：`core/previz_design.py` 把分镜文字再交给 LLM **二次翻译**成
    3D 参数（camera.position / look_at / lens / moves、cast[].pose、path），
    写进 `outputs/<pid>/previz/spec.json`；
  · 视频提示词：`core/shot_prompts.py` 从**同一份分镜文字**独立编译。

两次理解必然漂移。实测这支片子里的三处硬矛盾：
  1. 白膜 `pose='sit'`、path 只有 2 点（人坐着不动），而提示词写了
     「出场动势：本镜以向前运动进入（人物带着速度进画）」—— 直接打架；
  2. 提示词要「全程手持轻微呼吸感」「缓慢向右推近」，白膜只有 `push_in`
     （沿视线推）＋固定机位渲染，**渲不出手持与横移**；
  3. 提示词要「右手食指在鼠标上横向滑一下」，白膜是**骨架级**，手指动不了。

## 这个模块做什么

**只做一件事：把差异算出来、说清楚。** 不做静默改写。
（先让用户看到「哪条白膜预演不到、哪条是两套编译器打架"，
  再决定是改白膜渲、还是降级提示词 —— 否则改一处又会和别处不一致。）

纯函数、零网络、零费用。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Dict, List, Optional, Tuple

#: 白膜渲染器**能表达**的运镜（超出这些的，白膜一定渲不出来）
RENDERER_MOVEMENTS = {
    "static": "固定机位",
    "push_in": "沿视线推近",
    "pull_out": "沿视线拉远",
    "pan_left": "水平摇左",
    "pan_right": "水平摇右",
    "track": "横移跟随",
    "track_left": "水平横移左",
    "track_right": "水平横移右",
    "orbit": "环绕",
    "crane_up": "上升",
    "crane_down": "下降",
}

#: 白膜能表达的**粗粒度**人物姿态（骨架/体块级）
RENDERER_POSES = {
    "stand": "站立", "idle": "站立(待机)", "walk": "走", "walking": "走",
    "run": "跑", "running": "跑", "sit": "坐", "squat": "蹲", "prone": "趴",
    "hold": "持物", "pull": "拉", "dragged": "被拖",
}

#: 提示词里常见、但白膜**表达不了**的东西（命中就报"预演不到"）
UNRENDERABLE = [
    (("手持", "呼吸感", "轻微晃动", "handheld"), "手持抖动/呼吸感", "白膜是固定机位渲染，没有手持抖动"),
    (("手指", "食指", "拇指", "指节", "敲键", "触控板", "鼠标滚"), "手指级动作", "白膜是骨架级，手指动不了"),
    (("眼神", "对视", "目光", "扫一眼", "瞟"), "眼神/视线细节", "白膜没有眼球与视线"),
    (("表情", "嘴角", "皱眉", "眼眶", "微表情"), "表情", "白膜没有面部表情"),
    (("虚化", "景深", "焦外", "光斑", "bokeh"), "景深/虚化", "白膜是实心渲染，没有光学虚化"),
    (("反光", "高光", "逆光", "轮廓光", "色温"), "光线质感", "白膜是灰模，不谈材质与光"),
    (("字幕", "字", "logo", "水印"), "画面文字", "白膜不渲文字"),
]

#: 提示词里表示"运动方向"的词 → 白膜 movement
_DIR_TO_MOVE: List[Tuple[Tuple[str, ...], str]] = [
    (("向右推", "右移", "右横移", "右平移"), "track_right"),
    (("向左推", "左移", "左横移", "左平移"), "track_left"),
    (("推近", "推进", "推镜", "镜头推", "相机推", "摄影机推"), "push_in"),
    (("拉远", "后拉", "拉开", "拉出"), "pull_out"),
    (("摇左", "左摇"), "pan_left"),
    (("摇右", "右摇"), "pan_right"),
    (("环绕", "绕拍", "弧线", "圆弧"), "orbit"),
    (("相机升起", "镜头升起", "机位升起", "相机上移", "镜头上移", "机位上移", "上摇", "升镜"), "crane_up"),
    (("相机下降", "镜头下降", "机位下降", "相机下移", "镜头下移", "机位下移", "下摇", "降镜"), "crane_down"),
    (("跟拍", "跟随", "横移", "平移"), "track"),
]


def infer_movement(prompt: str) -> List[str]:
    """从提示词里推断它要求的运镜（可能多条 = 组合运镜）。"""
    out: List[str] = []
    for words, mv in _DIR_TO_MOVE:
        if any(_positive(prompt,w) for w in words) and mv not in out:
            out.append(mv)
    if not out and any(_positive(prompt,w) for w in ("固定机位","相机静止","镜头固定","机位固定")):
        out.append("static")
    return out


def report(prompt: str, spec_shot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """算一次差异。返回 {ok, issues:[{kind, level, title, detail}], summary}。"""
    prompt = str(prompt or "")
    sp = spec_shot if isinstance(spec_shot, dict) else {}
    cam = sp.get("camera") if isinstance(sp.get("camera"), dict) else {}
    issues: List[Dict[str, str]] = []

    # ① 运镜：提示词要的 vs 白膜 spec 里写的
    want = infer_movement(prompt)
    have = str(sp.get("movement") or "static")
    if want:
        if have not in want and not (have == "static" and "static" in want):
            issues.append({
                "kind": "movement", "level": "error",
                "title": "运镜不一致",
                "detail": ("提示词要求「%s」，而白膜 spec 里 movement=%s（%s）。"
                           "成片会按提示词的运镜走，白膜预演的是另一个运镜 —— "
                           "观众看到的白膜和成片不是同一个机位。"
                           % ("、".join(RENDERER_MOVEMENTS.get(w, w) for w in want),
                              have, RENDERER_MOVEMENTS.get(have, have))),
            })
        if len(want) > 1:
            issues.append({
                "kind": "movement_combo", "level": "warn",
                "title": "提示词是组合运镜（白膜只能预演单条）",
                "detail": "提示词同时要求：%s；白膜 spec 只记录 movement=%s。"
                          "组合运镜（如「推+横移」）在白膜里只能近似其中一条。"
                          % ("、".join(RENDERER_MOVEMENTS.get(w, w) for w in want), have),
            })

    # ② 姿态：提示词说坐着/站着，白膜 spec 里 cast[].pose 必须一致
    statics = [("坐", "sit"), ("站", "stand"), ("蹲", "squat"), ("躺", "prone"), ("趴", "prone")]
    prompt_pose = ""
    for cn, pv in statics:
        if ("%s着" % cn) in prompt or ("%s在" % cn) in prompt or ("%s下" % cn) in prompt:
            prompt_pose = pv
            break
    if prompt_pose:
        spec_poses = [str(c.get("pose") or "") for c in (sp.get("cast") or []) if isinstance(c, dict)]
        if spec_poses and prompt_pose not in spec_poses:
            issues.append({
                "kind": "pose", "level": "error",
                "title": "人物姿态不一致",
                "detail": ("提示词里人物是「%s」，白膜 spec 里是 %s。"
                           "这两者一打架，白膜就没有参考价值了。"
                           % (RENDERER_POSES.get(prompt_pose, prompt_pose),
                              "、".join(RENDERER_POSES.get(p, p) or p for p in spec_poses))),
            })

    # ③ 白膜表达不了的细节（不算错，但必须告诉用户"这一层预演不到"）
    for words, name, why in UNRENDERABLE:
        hits = [w for w in words if w in prompt]
        if hits:
            issues.append({
                "kind": "unrenderable", "level": "info",
                "title": "白膜预演不到：%s" % name,
                "detail": "提示词里有「%s」—— %s。白膜只能大致表达走位/运镜/节奏，"
                          "这些细节靠成片阶段的模型去补。" % ("、".join(hits[:3]), why),
            })

    # ④ 空镜/有人：人数是否一致
    people_prompt = len(re.findall(r"[阿-龥]{2,4}(?=在|坐在|站着|走到)", prompt)) or 0
    cast_n = len(sp.get("cast") or [])
    if people_prompt and cast_n and people_prompt > cast_n + 1:
        issues.append({
            "kind": "cast", "level": "warn", "title": "人数可能不一致",
            "detail": "提示词里出现约 %d 个人物指称，白膜 spec 里只有 %d 个主体。" % (people_prompt, cast_n),
        })

    errs = [i for i in issues if i["level"] == "error"]
    return {
        "ok": not errs,
        "issues": issues,
        "summary": ("白膜与提示词一致（仅有的差异是白膜精度所限）" if not errs
                    else "白膜与提示词有 %d 处**硬矛盾**，必须修掉其中一边" % len(errs)),
    }


# ══════════════════════════════════════════════════════════════
# 校正：把 LLM 产出的 spec **按分镜提示词**改到一致
#   用户：「白膜视频要效果至少能匹配分镜提示词的描述」
#   不重写整条生成链，只在写盘前做一次**确定性校正**：
#     运镜 movement / 焦距 lens / 人物姿态 pose / 备注 note
# ══════════════════════════════════════════════════════════════
#: 是否让白膜按提示词改焦距。默认 **False** —— 白膜只做运动参考，
#: 改焦距会把主体裁掉（实测：50→85mm 人不见了；加距离补偿后头被切）。
ADJUST_LENS = False

_SHOT_SIZE_LENS = {"特写": 85.0, "近景": 85.0, "中近景": 50.0, "中景": 50.0,
                   "全景": 35.0, "远景": 24.0, "大全景": 24.0}


def lens_from_prompt(prompt: str, default: float = 50.0) -> float:
    for k in ("中近景", "中全景", "大全景", "特写", "近景", "中景", "全景", "远景"):
        if _positive(prompt,k):
            return _SHOT_SIZE_LENS[k]
    return default


def pose_from_prompt(prompt: str, default: str = "stand") -> str:
    if "坐在" in prompt or "坐着" in prompt or "坐下" in prompt:
        return "sit"
    if "蹲" in prompt:
        return "squat"
    if "趴" in prompt:
        return "prone"
    if "跑" in prompt:
        return "run"
    if "走" in prompt or "穿过" in prompt or "走向" in prompt:
        return "walk"
    if "站" in prompt:
        return "stand"
    return default


# ══════════════════════════════════════════════════════════════
# ★★ 2026-10-23（用户）：「得确认白膜视频以后都跟分镜提示词要求一致！」
#
#   上一版 reconcile 只改 `movement` 标签 —— 标签对了、相机路径还是 LLM 写的，
#   画面照样不对。现在把提示词里**可验证**的东西全部解析成 `prompt_req`：
#     景别/构图（特写？中景？上半身入画？）→ 焦距区间 + 主体占画面高度区间
#     运镜（推/拉/横移/摇/升降/跟）→ 相机关键帧
#     姿态（坐/站/走…）、人数、角度（平视/俯/仰）
#   然后 **确定性重建相机路径**（保留 spec 的方位与高度，只换掉错的运动），
#   渲染器再按 `prompt_req` 框构图，并把"实际做到的"逐项核对（一致性报告）。
# ══════════════════════════════════════════════════════════════
#: (关键词, 代码, 中文, 焦距区间, 构图目标, 主体占画面高度区间)
#: 顺序 = 优先级（先匹配到的优先；"中近景"必须排在"近景"前面）
SHOT_SIZE_RULES = [
    (("大特写", "极特写", "微距", "macro", "细节特写", "微距特写"),
     "extreme_close", "大特写", (85, 135), "detail", (0.55, 1.40)),
    (("中近景",), "medium_close", "中近景", (45, 85), "upper_body", (0.34, 0.90)),
    (("特写", "近景"), "close", "特写", (60, 110), "head", (0.40, 1.05)),
    (("中全景", "中全"), "medium_full", "中全景", (28, 45), "full_body", (0.24, 0.72)),
    (("中景",), "medium", "中景", (35, 65), "upper_body", (0.32, 0.88)),
    (("全景", "全身"), "full", "全景", (24, 40), "full_body", (0.20, 0.62)),
    (("远景", "大全景"), "wide", "远景", (18, 30), "environment", (0.10, 0.50)),
]
SIZE_ZH = {r[1]: r[2] for r in SHOT_SIZE_RULES}
COMPOSITION_ZH = {"detail": "局部细节", "head": "头部/近景", "upper_body": "上半身",
                  "full_body": "全身", "environment": "环境/远景"}


def _hits(text: str, rules) -> List[Tuple]:
    """按**在文本里出现的先后**返回命中的规则（同位置时按规则优先级）。"""
    candidates = []
    for order, r in enumerate(rules):
        for w in r[0]:
            for match in re.finditer(re.escape(w),text):
                if _positive_at(text,match.start()):
                    candidates.append((match.start(),match.end(),order,r))
    candidates.sort(key=lambda item:(item[0],-(item[1]-item[0]),item[2]))
    accepted=[];seen=set()
    for start,end,order,rule in candidates:
        if any(start>=a and end<=b for a,b,_,_ in accepted):continue
        accepted.append((start,end,order,rule))
    result=[]
    for start,end,order,rule in accepted:
        if rule[1] not in seen:result.append(rule);seen.add(rule[1])
    return result


def _positive_at(text: str, position: int) -> bool:
    # Negation belongs to a clause; a later affirmative clause must remain usable.
    lead=re.split(r"[，,。；;！!？?\n：:]|而是|但是|改为|但|而",text[:position])[-1][-30:]
    return not re.search(r"不|无|未|禁止|避免|杜绝|不得|不可|不要|无需|拒绝|\b(?:no|not|without)\b",lead,re.I)


def _positive(text: str, word: str) -> bool:
    """文本里是否**正面**出现 word（排除「避免空镜」「不要空镜」这类否定）。"""
    return any(_positive_at(text,m.start()) for m in re.finditer(re.escape(word),text))


def shot_size_from_prompt(prompt: str) -> Dict[str, Any]:
    """解析景别（优先看【镜头】段，再看全文），并给出构图/焦距/占屏高度。"""
    p = str(prompt or "")
    seg = ""
    m = re.search(r"【镜头】([^【]{0,300})", p)
    if m:
        seg = m.group(1)
    hits = _hits(seg, SHOT_SIZE_RULES) if seg else []
    all_hits = _hits(p, SHOT_SIZE_RULES)
    conflicts = []
    if len({h[1] for h in all_hits}) > 1:
        conflicts.append("提示词全文出现多个景别：%s（以「%s」为准）"
                         % ("、".join(h[2] for h in all_hits),
                            (hits[0][2] if hits else all_hits[0][2])))
    if not hits:
        hits = all_hits
    if not hits:
        return {"size": "medium", "zh": "中景", "lens": [35, 65],
                "composition": "upper_body", "target_h": [0.30, 0.90],
                "evidence": "", "conflicts": conflicts}
    kw, code, zh, lens, comp, hrange = hits[0]
    # 显式构图文字覆盖（优先级：全身/上半身 > 脸 > 空镜）
    if "全身" in p or "整个人" in p:
        comp = "full_body"
    elif "上半身" in p or "半身" in p:
        comp = "upper_body"
    elif ("侧脸" in p or "脸部" in p or "面部" in p) and code in ("close", "medium_close"):
        comp = "head"
    if _positive(p, "空镜"):
        comp = "environment"
    return {"size": code, "zh": zh, "lens": list(lens), "composition": comp,
            "target_h": list(hrange), "evidence": (seg[:80] or kw),
            "conflicts": conflicts}


def requirements_from_prompt(prompt: str, *, source: str = "video_prompt") -> Dict[str, Any]:
    """把提示词解析成白膜必须满足的硬指标（写进 spec.prompt_req）。"""
    p = str(prompt or "")
    sz = shot_size_from_prompt(p)
    pose = pose_from_prompt(p, "")
    cnt = 0
    m = (re.search(r"画面内人物\s*(\d+)\s*人", p)
         or re.search(r"画面(?:里|中)?有?\s*(\d+)\s*人", p))
    if m:
        try:
            cnt = int(m.group(1))
        except (TypeError, ValueError):
            cnt = 0
    angle = "eye"
    if any(w in p for w in ("俯拍", "俯视", "高角度", "俯瞰", "顶拍")):
        angle = "high"
    if any(w in p for w in ("仰拍", "仰视", "低角度")):
        angle = "low"
    return {
        "shot_size": sz["size"], "shot_size_zh": sz["zh"],
        "lens_range": sz["lens"], "composition": sz["composition"],
        "target_h": sz["target_h"], "target_h_range": sz["target_h"],
        "camera_move": infer_movement(p), "pose": pose,
        "subject_count": cnt, "angle": angle,
        "conflicts": list(sz["conflicts"]),
        "evidence": sz["evidence"],
        "source": source if p else "",
        "prompt_sha1": hashlib.sha1(p.encode("utf-8")).hexdigest()[:12] if p else "",
    }


def _p3(v, default=(0.0, 0.0, 0.0)) -> List[float]:
    try:
        return [float(x) for x in (v or [])][:3]
    except (TypeError, ValueError):
        return list(default)


def _dominant_move(moves: List[str]) -> str:
    for pref in ("orbit", "push_in", "pull_out", "track_right", "track_left", "track",
                 "crane_up", "crane_down", "pan_right", "pan_left", "orbit"):
        if pref in moves:
            return pref
    return "static"


def explicit_camera_path(camera: Dict[str,Any]) -> bool:
    if camera.get('_synth') or camera.get('_explicit_path') is False:return False
    keys=camera.get('moves')
    if not isinstance(keys,list) or not keys:return False
    previous=0
    for key in keys:
        if not isinstance(key,(list,tuple)) or len(key)<8:return False
        try:
            values=[float(v) for v in key[:8]]
            if not all(math.isfinite(v) for v in values) or values[0]<=previous:return False
        except (ValueError,TypeError):return False
        previous=values[0]
    return True


def orbit_camera_keys(keys):
    """Sample an explicit orbit in polar space, preserving every supplied waypoint.

    Independent Cartesian Bezier curves cut across the circle between valid waypoints.
    The focus, height, lens and radius may still change if the numeric keys ask for it.
    """
    if len(keys)<2:return keys
    result=[]
    for left,right in zip(keys,keys[1:]):
        a=math.atan2(left[2]-left[5],left[1]-left[4])
        b=math.atan2(right[2]-right[5],right[1]-right[4])
        delta=(b-a+math.pi)%(2*math.pi)-math.pi
        r0=math.hypot(left[1]-left[4],left[2]-left[5])
        r1=math.hypot(right[1]-right[4],right[2]-right[5])
        for frame in range(int(left[0]),int(right[0])):
            if frame==left[0]:result.append(tuple(left));continue
            t=(frame-left[0])/(right[0]-left[0])
            look=[left[index]+(right[index]-left[index])*t for index in (4,5,6)]
            angle=a+delta*t;radius=r0+(r1-r0)*t
            result.append((frame,look[0]+radius*math.cos(angle),look[1]+radius*math.sin(angle),
                           left[3]+(right[3]-left[3])*t,*look,left[7]+(right[7]-left[7])*t))
    result.append(tuple(keys[-1]))
    return result


def _synth_camera(sp: Dict[str, Any], req: Dict[str, Any]) -> Dict[str, Any]:
    """按 prompt_req 确定性重建相机路径（24fps 帧号；渲染器会换算到 12fps）。

    保留 spec 里的**方位（从哪边看）与高度**，只改运动方式/焦距/看向点；
    这样既跟提示词一致，又不会把 LLM 选的机位角度抹掉。
    """
    cam = dict(sp.get("camera") or {})
    pos = _p3(cam.get("position"), (0.0, -3.2, 1.5))
    cast = [c for c in (sp.get("cast") or []) if isinstance(c, dict)]
    if cast:
        cx = float(cast[0].get("x") or 0.0)
        cy = float(cast[0].get("y") or 0.0)
        h = float(cast[0].get("height") or 1.7)
        comp = req.get("composition") or "upper_body"
        fz = {"head": 0.86, "detail": 0.74, "upper_body": 0.62,
              "full_body": 0.50, "environment": 0.55}.get(comp, 0.55) * h
        subject = [cx, cy, fz]
    else:
        subject = _p3(cam.get("look_at"), (0.0, 0.0, 1.0))
    dx, dy = pos[0] - subject[0], pos[1] - subject[1]
    d0 = math.hypot(dx, dy)
    if d0 < 0.6:
        dx, dy, d0, pos = 0.0, -3.2, 3.2, [subject[0], subject[1] - 3.2, pos[2]]
    ux, uy = dx / d0, dy / d0          # 主体 → 相机
    rx, ry = -uy, ux                   # 相机右方向
    z = float(pos[2])
    z_end = z
    d_end = d0
    lat = 0.0
    pan = 0.0
    for mv in (req.get("camera_move") or []):
        if mv == "push_in":
            d_end = max(1.2, d_end * 0.72)
        elif mv == "pull_out":
            d_end = d_end * 1.35
        elif mv in ("track_right",):
            lat += 1.1
        elif mv in ("track_left",):
            lat -= 1.1
        elif mv == "track":
            lat += 0.9
        elif mv == "crane_up":
            z_end = min(2.6, z_end + 0.7)
        elif mv == "crane_down":
            z_end = max(0.65, z_end - 0.7)
        elif mv == "pan_right":
            pan = 0.13
        elif mv == "pan_left":
            pan = -0.13
        elif mv == "orbit":
            pan = 0.10
            lat += 0.45
    if req.get("angle") == "high":
        z = max(z, 2.2)
        z_end = max(z_end, z)
    if req.get("angle") == "low":
        z = min(z, 1.05)
        z_end = min(z_end, z)
    frames = max(24, int(round(float(sp.get("duration") or 5.0) * 24)))
    lo, hi = req.get("lens_range") or (35, 65)
    lens = round((float(lo) + float(hi)) / 2.0, 1)
    # pan：看向点横移一点点 → 主体在画面里从一侧滑到中间（仍在框内，不破坏取景断言）
    look_start = [subject[0] + rx * pan * d0, subject[1] + ry * pan * d0, subject[2]]
    look_end = list(subject)
    p0 = [subject[0] + ux * d0, subject[1] + uy * d0, z]
    p1 = [subject[0] + ux * d_end + rx * lat,
          subject[1] + uy * d_end + ry * lat, z_end]
    return {
        "position": [round(v, 2) for v in p0],
        "look_at": [round(v, 2) for v in look_end],
        "lens": lens,
        "moves": [
            [1] + [round(v, 2) for v in p0] + [round(v, 2) for v in look_start] + [lens],
            [frames] + [round(v, 2) for v in p1] + [round(v, 2) for v in look_end] + [lens],
        ],
        "_synth": True,
    }


def reconcile_spec(spec: Dict[str, Any], prompt_pairs: List[Tuple]) -> Dict[str, Any]:
    """按分镜提示词把 spec 校正到一致，并把要求写进 `prompt_req`。

    `prompt_pairs`：[(镜序号从0起, 该镜的提示词[, 来源]), ...]
      来源 = "video_prompt"（正式分镜提示词）或 "shot_text"（提示词还没生成时用分镜文字兜底）。
    """
    by_idx: Dict[int, Tuple[str, str]] = {}
    for p in (prompt_pairs or []):
        try:
            idx = int(p[0])
        except (TypeError, ValueError, IndexError):
            continue
        txt = str(p[1] or "")
        src = str(p[2]) if len(p) > 2 and p[2] else "video_prompt"
        by_idx[idx] = (txt, src)

    changes: List[str] = []
    for idx, sp in enumerate((spec or {}).get("shots") or []):
        if not isinstance(sp, dict):
            continue
        prompt, src = by_idx.get(idx, ("", ""))
        if not prompt:
            continue
        req = requirements_from_prompt(prompt, source=src)
        if not sp.get('cast') and (sp.get('set') or {}).get('props') and req['composition'] in ('upper_body','head'):
            req['composition']='full_object'
        sp["prompt_req"] = req
        _sp: List[str] = []           # 本镜的校正说明（note 只写本镜的）
        # ── ① 焦距：必须落在景别要求的区间 ──
        cam = dict(sp.get("camera") or {})
        old_lens = float(cam.get("lens") or 50.0)
        lo, hi = req["lens_range"]
        # ── ② 运镜：提示词里写了动，就**确定性重建路径**（不再只改标签） ──
        want_moves = req.get("camera_move") or []
        if explicit_camera_path(cam):
            # Numeric poses carry more information than keyword guesses. Report requirements,
            # then let the renderer validate the actual framing; never replace this trajectory.
            cam['_explicit_path']=True
            sp['camera']=cam
            if want_moves:sp['movement']=_dominant_move(want_moves)
            _sp.append('镜%d 保留显式相机关键帧，按实际路径验收' % (idx+1))
        elif want_moves:
            new_cam = _synth_camera(sp, req)
            sp["camera"] = new_cam
            new_mv = _dominant_move(want_moves)
            if str(sp.get("movement") or "") != new_mv:
                _sp.append("镜%d 运镜 %s → %s（按提示词重建相机路径）"
                           % (idx + 1, sp.get("movement") or "?", new_mv))
            else:
                _sp.append("镜%d 按提示词重建相机路径（%s）" % (idx + 1, new_mv))
            sp["movement"] = new_mv
        else:
            # 提示词没写运镜 → 保留 LLM 路径，只把焦距夹进区间
            new_lens = max(float(lo), min(float(hi), old_lens))
            if abs(new_lens - old_lens) > 0.5:
                _sp.append("镜%d 焦距 %.0f → %.0fmm（景别要求 %s）"
                           % (idx + 1, old_lens, new_lens, req["shot_size_zh"]))
                cam["lens"] = new_lens
                mv = cam.get("moves")
                if isinstance(mv, list):
                    for m in mv:
                        if isinstance(m, list) and len(m) >= 8:
                            m[7] = new_lens
            sp["camera"] = cam
        # ── ③ 姿态 ──
        want_pose = req.get("pose") or ""
        cast = [c for c in (sp.get("cast") or []) if isinstance(c, dict)]
        if want_pose and cast:
            for c in cast:
                old_p = str(c.get("pose") or "")
                if old_p != want_pose:
                    c["pose"] = want_pose
                    _sp.append("镜%d %s 姿态 %s → %s"
                               % (idx + 1, c.get("name") or "?", old_p or "?", want_pose))
        # ── ④ 人数：白膜不能凭空加人，但必须如实记差异 ──
        cnt = int(req.get("subject_count") or 0)
        if cnt and cast and cnt != len(cast):
            _sp.append("镜%d 提示词写 %d 人，白膜里有 %d 个主体（白膜无法凭空加人）"
                       % (idx + 1, cnt, len(cast)))
        # ── ⑤ 提示词自身矛盾（如【镜头】中景 + 【手法】微距特写） ──
        for cf in (req.get("conflicts") or []):
            _sp.append("镜%d %s" % (idx + 1, cf))
        changes.extend(_sp)
        sp["note"] = (str(sp.get("note") or "") + "｜按分镜提示词校正："
                      + ("；".join(_sp[-4:]) if _sp else "无差异"))
    (spec or {}).setdefault("reconciled", {})
    spec["reconciled"] = {"changes": changes, "count": len(changes)}
    return spec


def _json_norm(v):
    """把 DB 里的 JSON 字符串 / API 里的 dict 归一成同一种结构（指纹才能稳定）。"""
    if isinstance(v, str):
        s = v.strip()
        if s[:1] in ("{", "["):
            try:
                return json.loads(s)
            except Exception:  # noqa: BLE001
                return s
    return v


def prompt_fingerprint(shots: List[Dict[str, Any]]) -> str:
    """分镜/提示词的指纹：变了就要重新生成白膜 spec（否则白膜永远是旧的）。

    ★ 归一化很重要：`db.list_shots()` 给的是 JSON **字符串**，API 给的是 dict。
      同一份内容必须算出同一个指纹，否则重启后第一次渲白膜会白白重生成一次（花钱）。
    """
    key = []
    for s in sorted((sh or {} for sh in (shots or [])),
                    key=lambda x: (int(x.get("order_index") or 0), str(x.get("id") or ""))):
        key.append([s.get("id"), s.get("order_index"), s.get("duration_seconds"),
                    _json_norm(s.get("layer1_overview")),
                    _json_norm(s.get("layer2_timeline")),
                    _json_norm(s.get("layer3_constraints"))])
    return hashlib.sha1(json.dumps(key, ensure_ascii=False, sort_keys=True,
                                   default=str).encode("utf-8")).hexdigest()[:16]


def video_prompt_of(shot: Dict[str, Any]) -> str:
    """取这一镜的正式视频提示词（layer3.prompts.video_prompt）；没有就空串。"""
    l3 = (shot or {}).get("layer3_constraints")
    if isinstance(l3, str):
        try:
            l3 = json.loads(l3) if l3.strip() else {}
        except Exception:  # noqa: BLE001
            l3 = {}
    try:
        return str(((l3 or {}).get("prompts") or {}).get("video_prompt") or "")
    except Exception:  # noqa: BLE001
        return ""


# ══════════════════════════════════════════════════════════════
# 取景修正：保证白膜画面里**真的有人**
#   实测事故：spec 的 camera.position 落在桌面/柜体里、look_at 偏，
#   渲出来的白膜只有桌腿和几何体侧面，人物根本在视锥外 ——
#   这种白膜当"参考视频"喂给模型等于给垃圾。
# ══════════════════════════════════════════════════════════════
FRAMING_FIX = False   # 保持关闭：取景修正已改到**渲染器侧**
SAFE_DIST = 2.9          # 相机到人物头部的安全距离（米）
MIN_DIST = 1.6


def _fov_deg(lens: float, sensor: float = 24.0) -> float:
    lens = max(8.0, min(200.0, float(lens or 50.0)))
    return math.degrees(2.0 * math.atan(sensor / (2.0 * lens)))


def _p(v, d=0.0):
    try:
        return [float(x) for x in (v or [])][:3]
    except Exception:  # noqa: BLE001
        return [d, d, d]


def fix_framing(spec: Dict[str, Any]) -> List[str]:
    """把每个镜头里"人不在画面内"的机位拉回来。返回修正说明。

    ★ 2026-10-23 结论更正：白膜取景的根因**不是** spec 里的 camera 数值，
      而是渲染器侧的三处缺陷（都已在 `backend/data/previz_blender/render_spec.py`
      修掉，并加了 Blender 侧取景断言 + 自动重构图）：
        ① 焦距没打关键帧 → 首帧用的是最后一帧的焦距；
        ② pose='sit' 渲染器不认 → 按站立生成，头高出框；
        ③ 人物 path 的 dz 让人整体悬空。
      所以这个"在后端改 spec 数值"的兜底保持关闭（改了也只会让 spec 与
      渲染器反馈不一致）。要看真实取景结果，读渲染器落盘的
      `<out_dir>/取景报告.json`（previz_blender 会把它带回 `framing` 字段）。
    """
    if not FRAMING_FIX:
        return []
    notes: List[str] = []
    for idx, sp in enumerate((spec or {}).get("shots") or []):
        if not isinstance(sp, dict):
            continue
        cast = [c for c in (sp.get("cast") or []) if isinstance(c, dict)]
        cam = sp.get("camera") if isinstance(sp.get("camera"), dict) else {}
        if not cast or not cam:
            continue
        c0 = cast[0]
        h = float(c0.get("height") or 1.7)
        subj = [float(c0.get("x") or 0.0), float(c0.get("y") or 0.0), 0.85 * h]
        pos = _p(cam.get("position"))
        look = _p(cam.get("look_at"), 0.0)
        lens = float(cam.get("lens") or 50.0)
        # ① 视锥检查：观察方向 vs 指向人物的方向
        vx, vy, vz = (subj[0] - pos[0], subj[1] - pos[1], subj[2] - pos[2])
        dist = math.sqrt(vx * vx + vy * vy + vz * vz)
        lx, ly, lz = (look[0] - pos[0], look[1] - pos[1], look[2] - pos[2])
        ld = math.sqrt(lx * lx + ly * ly + lz * lz) or 1e-6
        cosang = (vx * lx + vy * ly + vz * lz) / max(1e-6, dist * ld)
        ang = math.degrees(math.acos(max(-1.0, min(1.0, cosang))))
        half = min(_fov_deg(lens, 24.0), _fov_deg(lens, 36.0)) / 2.0
        # ★ 实测结论：仅靠"视锥检查"不够 —— 原来的机位有时落在桌体内部，
        #   检查能过、渲出来却没有人物。所以有人物就**一律重设成标准中景机位**：
        #   正对人物、距离 3.2m、机位高 1.5m、看向胸/脸高度（1.1m），lens 50。
        bad = True
        # ② 修：看向人物头部，并沿原方向退到安全距离
        dx, dy = (pos[0] - look[0]), (pos[1] - look[1])
        n = math.hypot(dx, dy) or 1e-6
        ux, uy = dx / n, dy / n
        target_d = max(SAFE_DIST, dist * 1.2)
        # 用人物朝向的反方向做"正面"（cast.rot=180 表示面朝 -Y）
        rot = math.radians(float(c0.get("rot") or 0.0))
        fx, fy = math.sin(rot), math.cos(rot)
        back = 3.2
        new_pos = [subj[0] - fx * back, subj[1] - fy * back, 1.5]
        target_d = back
        cam["position"] = [round(v, 2) for v in new_pos]
        cam["look_at"] = [round(subj[0], 2), round(subj[1], 2), 1.1]
        cam["lens"] = 50.0
        # moves 同步平移到新机位（保持轨迹形状）
        mv = cam.get("moves")
        if isinstance(mv, list) and mv:
            dxs = new_pos[0] - pos[0]
            dys = new_pos[1] - pos[1]
            nmv = []
            for m in mv:
                if isinstance(m, list) and len(m) >= 7:
                    m2 = list(m)
                    m2[1] = round(float(m[1]) + dxs, 2)
                    m2[2] = round(float(m[2]) + dys, 2)
                    m2[4] = round(subj[0], 2)
                    m2[5] = round(subj[1], 2)
                    m2[6] = round(subj[2], 2)
                    nmv.append(m2)
                else:
                    nmv.append(m)
            try:
                cam["moves"] = [[1, round(subj[0] - fx * back, 2), round(subj[1] - fy * back, 2), 1.5,
                                 round(subj[0], 2), round(subj[1], 2), 1.1, 50.0],
                                [96, round(subj[0] - fx * (back - 0.3), 2), round(subj[1] - fy * (back - 0.3), 2), 1.5,
                                 round(subj[0], 2), round(subj[1], 2), 1.1, 50.0]]
            except Exception:  # noqa: BLE001
                cam["moves"] = nmv
        sp["camera"] = cam
        notes.append("镜%d 取景修正：原机位距人 %.2fm、偏角 %.0f°（视锥半角 %.0f°）→ 退到 %.2fm 并看向人物"
                     % (idx + 1, dist, ang, half, target_d))
    if notes:
        spec.setdefault("framing_fix", {})["changes"] = notes
    return notes
