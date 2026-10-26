"""镜头契约：把「剧本意图 → 白膜机位 → 视频提示词 → 一致性验收」压成同一套数字。

为什么必须有一个契约层（2026-10-25）：
  旧链路里三处各自理解剧本，互相不校对，实测出现三种硬冲突：
    1. 剧本写「以立方体为圆心弧线横移」，白膜 spec 里却是 movement=push_in；
    2. 视频提示词检测到两个运镜（固定机位、横移）后**只保留第一个**（固定机位），
       于是提示词明确写了「不要叠加第二个运镜」，和白膜、剧本全反；
    3. 一致性验收只有 SSIM，机制上不可能发现机位类型错了。
  所以这里定义唯一权威：movement / 弧角 / 半径 / 焦段 / 机位高度 / 看向点 /
  速度曲线 / 主体占画比 / 布光方向。白膜按它摆机位，提示词按它写电影语言，
  验收按它量。

设计约束：
  · 纯函数、无 IO、无网络；不读数据库。
  · 数字优先来自于白膜 spec（已经过几何校验），其次才是文本推断。
  · 文本推断只做「意图分类 + 少量显式数值」，不做艺术发挥。
"""

from __future__ import annotations

import math
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: 意图词表：先匹配更具体的，再匹配泛化的。顺序敏感。
_INTENTS: Sequence[Tuple[str, Tuple[str, ...]]] = (
    ("orbit", ("弧线", "弧形", "环绕", "绕行", "圆周", "以...为圆心", "为圆心", "orbit", "arc")),
    ("dolly_in", ("推近", "推进", "推镜", "dolly in", "push in", "推拉")),
    ("dolly_out", ("拉远", "拉出", "后拉", "dolly out", "pull out")),
    ("crane", ("升降", "俯冲", "摇臂", "升镜", "降镜", "crane")),
    ("pan_rotate", ("摇镜", "摇摄", "左右摇", "pan", "摇")),
    ("track", ("横移", "平移", "侧移", "跟移", "truck", "tracking", "dolly")),
    ("static", ("固定机位", "固定镜头", "静止机位", "机位不动", "static", "locked off")),
)

#: 允许出现在 spec.movement 里的标准值
MOVEMENTS = ("orbit", "dolly_in", "dolly_out", "crane", "pan_rotate", "track", "handheld", "static")

#: 白膜 spec / 旧数据里的运动词 → 契约标准词
_MOVEMENT_ALIASES = {
    "push_in": "dolly_in", "pushin": "dolly_in", "推近": "dolly_in",
    "push_out": "dolly_out", "pull_out": "dolly_out", "拉远": "dolly_out",
    "pan": "pan_rotate", "潘": "pan_rotate",
    "descend": "crane", "ascend": "crane", "crane_up": "crane", "crane_down": "crane",
    "handheld": "handheld", "orbit": "orbit", "track": "track",
    "static": "static", "fixed": "static", "locked_off": "static",
}

#: 目标帧率（白膜与交付片共用）
FPS = 24

#: 一致性阈值（写死在这里，验收脚本和测试共用同一套）
THRESHOLDS = {
    "radius_drift_ratio": 0.02,      # 弧线半径相对变化 ≤2%（实测正确 0.0007）
    "lens_drift_mm": 0.5,            # 焦距漂移 ≤0.5mm（不能变焦）
    "subject_ratio_drift": 0.05,     # 白膜↔成片 主体占画高差 ≤5%（实测正确 0.003 / 错误 0.083）
    "subject_ratio_span": 0.08,      # 段内占画比跨度 ≤8%（环绕本身会让 bbox 高度变化约 5%）
    "ssim_min": 0.80,                # 只作 smoke：跨风格不可当判据
    "edge_ncc_min": 0.30,            # 结构判据（正确 0.502 / 错配 0.046–0.081）
    "silhouette_iou_min": 0.45,      # 正确 0.581 / 同源 0.549 / 错配 ≤0.289
    "motion_coherence_min": 0.50,    # 相干度门控；低于它就只报告不判罚
    "motion_cosine_min": 0.75,       # 仅在相干度达标时使用
    "arc_angle_tolerance_deg": 12.0, # 弧角由 spec 几何（A 组）判定
}


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def parse_intent(script_text: str) -> Dict[str, Any]:
    """从剧本文字里读出运镜意图与显式数值。只做分类，不发明参数。"""
    text = _norm(script_text)
    hits: List[str] = []
    for kind, words in _INTENTS:
        for word in words:
            plain = _norm(word).replace("...", "")
            if plain and plain in text:
                if kind not in hits:
                    hits.append(kind)
                break
    # 「横移 + 恒定距离 + 圆心」在电影语言里就是 orbit，优先于 track
    if "orbit" in hits:
        movement = "orbit"
    elif "track" in hits and re.search(r"(恒定|不变|等距|距离基本不变)", text):
        movement = "orbit"
    elif hits:
        movement = hits[0]
    else:
        movement = "static"

    angle = None
    m = re.search(r"(\d{2,3}(?:\.\d+)?)\s*(?:°|度)", text)
    if m:
        angle = float(m.group(1))
    elif re.search(r"(四分之一|90度)", text):
        angle = 90.0
    elif re.search(r"(半圈|180度)", text):
        angle = 180.0
    if movement == "orbit" and not angle:
        angle = 90.0

    lens = None
    m = re.search(r"(\d{2,3})\s*mm", text, re.I)
    if m:
        lens = float(m.group(1))

    confirms_no_zoom = bool(re.search(r"(不推拉|不变焦|不缩放|无推拉|无变焦)", text))
    confirms_no_cut = bool(re.search(r"(不切镜|不转场|单一连续镜头|一镜到底)", text))
    return {"movement": movement, "intent_hits": hits, "arc_degrees": angle,
            "lens_mm": lens, "no_zoom": confirms_no_zoom, "one_take": confirms_no_cut}


def _vec3(value: Any, default: Sequence[float]) -> List[float]:
    try:
        seq = list(value)[:3]
        return [float(v) for v in seq] if len(seq) == 3 else list(default)
    except (TypeError, ValueError):
        return list(default)


def _cameras_of(shot_spec: Dict[str, Any]) -> Tuple[List[float], List[float], float, List[list]]:
    cam = (shot_spec or {}).get("camera") or {}
    look = _vec3(cam.get("look_at"), (0.0, 0.0, 1.0))
    pos = _vec3(cam.get("position"), (0.0, -3.0, 1.4))
    lens = float(cam.get("lens") or 35.0)
    moves = [list(m) for m in (cam.get("moves") or []) if isinstance(m, list) and len(m) == 8]
    return pos, look, lens, moves


def orbit_path(pos: Sequence[float], look: Sequence[float], lens: float,
               frames: int, arc_degrees: float, *, steps: int = 4) -> List[list]:
    """按「以 look_at 在水平面的投影为圆心、半径与高度恒定」重建关键帧。

    只绕垂直轴转，不改变俯仰（否则等于改构图）。方向沿用原机位相对看向点的
    方位角，并朝「弧线横移」的自然方向（顺时针）展开。
    """
    # 坐标约定（与 render_spec 关键帧一致）：[frame, x, y, z, lookx, looky, lookz, lens]
    # 水平面是 (x, y)，z 是高度；环绕只能在水平面内转，高度恒定。
    cx, cy = float(look[0]), float(look[1])
    px, py, pz = float(pos[0]), float(pos[1]), float(pos[2])
    r = math.hypot(px - cx, py - cy)
    if r < 1e-6:
        r = 3.0
    start = math.atan2(py - cy, px - cx)
    total = math.radians(max(5.0, min(360.0, float(arc_degrees))))
    frames = max(2, int(frames))
    out: List[list] = []
    for i in range(max(2, steps)):
        t = i / (max(2, steps) - 1)
        # 速度曲线：慢起 → 中段匀速 → 停稳（ease-in-out，与「减速停稳」一致）
        eased = t * t * (3 - 2 * t)
        a = start + total * eased
        out.append([1 if i == 0 else int(round(1 + (frames - 1) * t)),
                    round(cx + r * math.cos(a), 3),
                    round(cy + r * math.sin(a), 3),
                    round(pz, 3),
                    round(float(look[0]), 3), round(float(look[1]), 3), round(float(look[2]), 3),
                    round(float(lens), 2)])
    out[0][0] = 1
    out[-1][0] = frames
    return out


def enforce_shot_camera(shot_spec: Dict[str, Any], script_text: str) -> Dict[str, Any]:
    """把剧本意图强制写进白膜机位。返回新的 shot spec（不原地修改）。"""
    spec = dict(shot_spec or {})
    intent = parse_intent(script_text)
    pos, look, lens, moves = _cameras_of(spec)
    frames = int(round(float(spec.get("duration") or 4.0) * FPS)) or FPS * 4
    movement = intent["movement"]
    if movement == "static":
        path = [[1] + [round(v, 3) for v in pos] + [round(v, 3) for v in look] + [round(lens, 2)],
                [frames] + [round(v, 3) for v in pos] + [round(v, 3) for v in look] + [round(lens, 2)]]
    elif movement == "orbit":
        path = orbit_path(pos, look, lens, frames, intent.get("arc_degrees") or 90.0)
    else:
        # 其它意图：保留模型给出的路径，但强制 movement 标签与恒定焦距
        path = moves or [
            [1] + [round(v, 3) for v in pos] + [round(v, 3) for v in look] + [round(lens, 2)],
            [frames] + [round(v, 3) for v in pos] + [round(v, 3) for v in look] + [round(lens, 2)],
        ]
        for row in path:
            row[7] = round(lens, 2)
    cam = dict(spec.get("camera") or {})
    cam.update({"position": [round(v, 3) for v in pos], "look_at": [round(v, 3) for v in look],
                "lens": round(lens, 2), "moves": path, "_explicit_path": True,
                "intent": intent})
    spec["camera"] = cam
    spec["movement"] = movement
    spec["intent"] = intent
    return spec


def _radius_stats(moves: Sequence[list], look: Sequence[float]) -> Tuple[float, float]:
    cx, cy = float(look[0]), float(look[1])
    radii = [math.hypot(float(m[1]) - cx, float(m[2]) - cy) for m in moves]
    if not radii:
        return 0.0, 0.0
    mean = sum(radii) / len(radii)
    if mean <= 1e-9:
        return 0.0, 0.0
    return mean, (max(radii) - min(radii)) / mean


def _arc_degrees(moves: Sequence[list], look: Sequence[float]) -> float:
    if len(moves) < 2:
        return 0.0
    cx, cy = float(look[0]), float(look[1])
    a0 = math.atan2(float(moves[0][2]) - cy, float(moves[0][1]) - cx)
    a1 = math.atan2(float(moves[-1][2]) - cy, float(moves[-1][1]) - cx)
    deg = abs(math.degrees(a1 - a0))
    return 360.0 - deg if deg > 180 else deg


def _samples_camera(samples: Sequence[Dict[str, Any]], look: Sequence[float]) -> Dict[str, Any]:
    """从渲染后逐帧相机采样提取**实际**参数（渲染器会为取景改焦距）。"""
    rows = [s for s in (samples or []) if isinstance(s, dict) and s.get("position")]
    if len(rows) < 2:
        return {}
    cx, cy = float(look[0]), float(look[1])
    radii, angles, heights, lens_values = [], [], [], set()
    for row in rows:
        px, py, pz = (float(v) for v in list(row["position"])[:3])
        radii.append(math.hypot(px - cx, py - cy))
        angles.append(math.atan2(py - cy, px - cx))
        heights.append(pz)
        lens = row.get("lens")
        if lens:
            lens_values.add(round(float(lens), 2))
    mean_r = sum(radii) / len(radii)
    drift = (max(radii) - min(radii)) / mean_r if mean_r > 1e-9 else 0.0
    deg = abs(math.degrees(angles[-1] - angles[0]))
    if deg > 180:
        deg = 360 - deg
    return {"radius_m": round(mean_r, 3), "radius_drift_ratio": round(drift, 5),
            "arc_degrees": round(deg, 2), "lens_values": sorted(lens_values),
            "camera_height_m": round(sum(heights) / len(heights), 3)}


def build_contract(shot: Dict[str, Any], shot_spec: Optional[Dict[str, Any]] = None,
                   script_text: str = "", camera_samples: Optional[Sequence[Dict[str, Any]]] = None,
                   subject_height_ratio_measured: Optional[float] = None) -> Dict[str, Any]:
    """合成权威契约。几何优先取**渲染实测**，其次 spec；意图优先取剧本；冲突如实记录。"""
    spec = dict(shot_spec or {})
    text = script_text or " ".join(str(shot.get(k) or "") for k in
                                   ("layer1_overview", "layer2_timeline", "layer3_constraints"))
    intent = parse_intent(text)
    pos, look, lens, moves = _cameras_of(spec)
    movement = str(spec.get("movement") or intent["movement"] or "static")
    # 白膜 spec 的枚举与契约枚举不是一套词（实测真实项目里 movement="push_in"）。
    # 原先直接判 not in MOVEMENTS 就归 static —— 白膜真在推的镜头，提示词会写成
    # 「固定机位、全程不推不拉」，与参考片相反。这里先做映射再判。
    movement = _MOVEMENT_ALIASES.get(movement, movement)
    if movement not in MOVEMENTS:
        mapped_intent = _MOVEMENT_ALIASES.get(intent["movement"], intent["movement"])
        movement = mapped_intent if mapped_intent in MOVEMENTS else "static"
    radius_m, drift = _radius_stats(moves, look)
    arc = _arc_degrees(moves, look) if moves else 0.0
    lens_values = sorted({round(float(m[7]), 2) for m in moves}) if moves else [round(lens, 2)]
    conflicts: List[str] = []
    if intent["movement"] and intent["movement"] != movement:
        conflicts.append(f"剧本意图={intent['movement']} 与白膜 movement={movement} 不符")
    if intent.get("arc_degrees") and arc and abs(arc - float(intent["arc_degrees"])) > THRESHOLDS["arc_angle_tolerance_deg"]:
        conflicts.append(f"弧角偏差：剧本 {intent['arc_degrees']}° vs 白膜 {arc:.1f}°")
    if len(lens_values) > 1 and (max(lens_values) - min(lens_values)) > THRESHOLDS["lens_drift_mm"]:
        conflicts.append(f"焦距不恒定：{lens_values}")
    if intent.get("no_zoom") and movement in ("dolly_in", "dolly_out"):
        conflicts.append("剧本要求不推拉，但白膜 movement 是推拉")

    props = ((spec.get("set") or {}).get("props") or [])
    subject = next((p for p in props if str(p.get("type")) == "box"), None)
    support = subject.get("on_surface") if isinstance(subject, dict) else None
    height_ratio = _subject_height_ratio(subject, pos, look, lens)

    declared_lens = round(lens_values[0], 2) if lens_values else round(lens, 2)

    # 渲染实测优先：渲染器会为了「不切边」改写焦距（实测 85mm → 46.75mm），
    # 此时以渲染后的相机为准，否则提示词描述的焦段与参考片不是同一个镜头。
    measured = _samples_camera(camera_samples or [], look)
    if measured:
        radius_m = measured["radius_m"]
        drift = measured["radius_drift_ratio"]
        arc = measured["arc_degrees"]
        lens_values = measured["lens_values"] or lens_values
        camera_height = measured["camera_height_m"]
    else:
        camera_height = float(pos[2])
    if subject_height_ratio_measured is not None:
        height_ratio = float(subject_height_ratio_measured)
        ratio_source = "measured_frames"
    else:
        ratio_source = "analytic"
    rendered_lens = round(lens_values[0], 2) if lens_values else declared_lens
    if abs(declared_lens - rendered_lens) > THRESHOLDS["lens_drift_mm"]:
        conflicts.append(f"实际焦距 {rendered_lens}mm 与声明 {declared_lens}mm 不同（渲染取景适配）")

    return {
        "duration": float(spec.get("duration") or shot.get("duration_seconds") or 4.0),
        "fps": FPS,
        "movement": movement,
        "movement_zh": {"orbit": "弧形环绕横移", "track": "平行横移", "static": "固定机位",
                        "dolly_in": "推近", "dolly_out": "拉远", "crane": "升降",
                        "pan_rotate": "摇镜"}.get(movement, movement),
        "arc_degrees": round(arc, 2),
        "arc_degrees_intent": intent.get("arc_degrees"),
        "radius_m": round(radius_m, 3),
        "radius_drift_ratio": round(drift, 5),
                "lens_mm": rendered_lens,
        "lens_declared_mm": declared_lens,
        "lens_rewritten": abs(rendered_lens - declared_lens) > THRESHOLDS["lens_drift_mm"],
        "lens_values": lens_values,
        "camera_height_m": round(float(camera_height), 3),
        "look_at": [round(float(v), 3) for v in look],
        "subject_height_ratio": round(height_ratio, 4),
        "subject_ratio_source": ratio_source,
        "subject_on_surface": bool(support),
        "subject_contact_ok": bool(support),
        "props_count": len(props),
        "no_zoom_confirmed": bool(intent.get("no_zoom")),
        "one_take": bool(intent.get("one_take")),
        "intent_hits": intent["intent_hits"],
        "conflicts": conflicts,
        "thresholds": dict(THRESHOLDS),
    }


def _subject_height_ratio(subject: Optional[Dict[str, Any]], pos: Sequence[float],
                          look: Sequence[float], lens_mm: float) -> float:
    """主体在画面里的高度占比（近似，sensor 高度 24mm 折算）。"""
    if not isinstance(subject, dict):
        return 0.0
    try:
        h = float(subject.get("h") or 0.0)
        z = float(subject.get("z") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if h <= 0:
        return 0.0
    dist = math.dist([float(v) for v in pos], [float(look[0]), float(look[1]), z + h / 2.0])
    if dist <= 1e-6 or lens_mm <= 0:
        return 0.0
    frame_height_m = dist * 24.0 / float(lens_mm)
    return max(0.0, min(1.5, h / frame_height_m)) if frame_height_m > 1e-6 else 0.0


def camera_prompt_block(contract: Dict[str, Any]) -> str:
    """把契约写成专业电影语言的镜头块（提示词用）。"""
    c = contract
    parts = [
        f"镜头规格：{c['lens_mm']:g}mm 定焦（全片焦距不变、不推拉、不变焦），"
        f"机位高度 {c['camera_height_m']:g} 米。",
    ]
    if c["movement"] == "orbit":
        parts.append(
            f"运镜：以主体为圆心做{c['arc_degrees']:g}°弧形环绕横移，"
            f"半径恒定 {c['radius_m']:g} 米、机位高度不变，看向点全程锁定主体中心；"
            "起手缓起、中段匀速、收尾减速停稳，全程单一连续镜头、不切、不推不拉。")
    elif c["movement"] == "static":
        parts.append("运镜：固定机位，三脚架锁定，全程不推不拉不摇不移，无任何运镜叠加。")
    elif c["movement"] in ("dolly_in", "dolly_out"):
        direction = "沿光轴推进" if c["movement"] == "dolly_in" else "沿光轴后拉"
        parts.append(f"运镜：{direction}（{c['movement_zh']}），全程焦距不变、无变焦，速度均匀。")
    else:
        parts.append(f"运镜：{c['movement_zh']}，单一主运动、焦距不变、不叠加第二个运镜。")
    if c.get("subject_height_ratio"):
        pct = c["subject_height_ratio"] * 100
        parts.append(
            f"构图锁定：首帧构图必须与参考视频首帧一致，主体垂直占画面高度约 {pct:.0f}%"
            f"（允许 ±5%），上下留白比例与参考片相同；全程不得拉宽视野、不得把主体缩小、"
            "不得改变景别；主体始终完整在画内、不切边。")
    parts.append("场景完整性：画面里只出现主体与它的那一张支撑台面，不得新增第二张台面、"
                 "背景家具、陈设或装饰物；场景边界与参考片一致。")
    if c.get("subject_on_surface"):
        parts.append("接触关系：主体底面与台面贴合，投影与接触阴影连续，不得悬浮。")
    if c.get("no_zoom_confirmed"):
        parts.append("硬约束：不推拉、不变焦、不切镜。")
    return "".join(parts)

def _previz_dirs(shot: Dict[str, Any]) -> List[Path]:
    """白膜产物可能落在 <previz>/镜NN/ 或 <previz>/ 下，两处都找。

    ★ 2026-10-25 修：原来拿**文件名**判断是不是镜目录（previz_path 是
    `.../previz/镜01/f0001.png`，文件名是 f0001.png），于是永远判断失败、
    根目录取成 `镜01/`，读不到 spec.json 与 camera-samples.json，
    契约静默用了默认机位 —— 几何数字全是错的。改为看**父目录名**。
    """
    from pathlib import Path as _P
    raw = str((shot or {}).get("previz_path") or "").strip()
    if not raw:
        return []
    base = _P(raw)
    parent = base if base.is_dir() else base.parent
    root = parent.parent if parent.name.startswith("镜") else parent
    dirs: List[_P] = [parent, root]
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            if ".bak" in child.name or ".stack" in child.name:
                continue
            if child.name.startswith("镜"):
                dirs.append(child)
    out: List[_P] = []
    for d in dirs:
        if d.is_dir() and d not in out:
            out.append(d)
    return out


def load_camera_samples(shot: Dict[str, Any]) -> List[Dict[str, Any]]:
    """读出渲染器记录的逐帧相机（这是白膜真正的机位真相）。"""
    import json
    for folder in _previz_dirs(shot):
        path = folder / "camera-samples.json"
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, list) and len(data) >= 2:
                    return data
            except (OSError, ValueError):
                continue
    return []


def load_shot_spec(shot: Dict[str, Any]) -> Dict[str, Any]:
    """读白膜 spec，返回本镜那一份。"""
    import json
    for folder in _previz_dirs(shot):
        path = folder / "spec.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        shots = data.get("shots") or []
        index = int((shot or {}).get("order_index") or 0)
        if 0 <= index < len(shots):
            return shots[index]
        if shots:
            return shots[0]
    return {}



def contract_for_shot(shot: Dict[str, Any], spec: Optional[Dict[str, Any]] = None,
                      script_text: str = "") -> Dict[str, Any]:
    """一步拿到某镜的权威契约（自动读 spec + 渲染相机采样）。

    ★ 新鲜度保护（2026-10-25）：实测出现过「spec.json 已按新剧本重写、渲染产物
    还是旧帧」的错位。契约若盲信旧采样，就会把错机位当权威。采样比 spec 旧时
    弃用采样，并在 conflicts 里如实说明。
    """
    shot_spec = spec if spec is not None else load_shot_spec(shot)
    samples = load_camera_samples(shot)
    spec_mtime = 0.0
    samples_mtime = 0.0
    for folder in _previz_dirs(shot):
        candidate = folder / "spec.json"
        if candidate.is_file():
            spec_mtime = max(spec_mtime, candidate.stat().st_mtime)
        sample = folder / "camera-samples.json"
        if sample.is_file():
            samples_mtime = max(samples_mtime, sample.stat().st_mtime)
    if samples and spec_mtime and samples_mtime and samples_mtime + 1.0 < spec_mtime:
        contract = build_contract(shot, shot_spec, script_text, camera_samples=None)
        contract["conflicts"].append("渲染采样早于白膜规格：旧帧，已改用 spec 几何；建议重渲白膜")
        contract["camera_samples_stale"] = True
        return contract
    contract = build_contract(shot, shot_spec, script_text, camera_samples=samples)

    # 主体占画比：参考片实测值才是提示词该写的数字。
    # 实测偏差来源：解析估算 40% vs 白膜实际 58%，成片最终落在 47% —— 三方不一致。
    index = int((shot or {}).get("order_index") or 0) + 1
    for folder in _previz_dirs(shot):
        for name in (f"镜{index:02d}.mp4", "镜01.mp4"):
            candidate = folder / name
            if candidate.is_file():
                measured = measure_subject_height_ratio(str(candidate))
                if measured:
                    contract["subject_height_ratio"] = measured
                    contract["subject_ratio_source"] = "measured_frames"
                    contract["subject_ratio_video"] = str(candidate)
                break
        if contract.get("subject_ratio_source") == "measured_frames":
            break
    return contract


def measure_subject_height_ratio(video_path: str, frame_ratio: float = 0.5) -> Optional[float]:
    """从白膜视频里量「主体占画面高度」。参考片实测值才是提示词该写的数字。

    方法：抽 3 帧 → 亮度取高分位（主体在灰模里最亮）→ 取前景行跨度 / 画面高。
    低对比灰模上比"中位色做背景"稳，实测与独立验收工具（55.8%）相差 <3%。
    返回值限制在 (0.02, 0.98)，越界视为量失败（宁可不写数字，也不写错数字）。
    """
    if not video_path or not os.path.isfile(str(video_path)):
        return None
    # 首选独立验收工具的测量（同一套分割逻辑，已用「同源对 IoU=1.0」交叉验证）。
    # 自己实现的分位数法在低对比灰模上会偏高（实测 73.7% vs 真值 58.3%）。
    try:
        from core import shot_consistency as _sc
        result = _sc.evaluate_generated(str(video_path), str(video_path), None)
        value = (result.get("metrics") or {}).get("subject_height_whitebox")
        if value and 0.02 < float(value) < 0.98:
            return round(float(value), 4)
    except Exception:  # noqa: BLE001
        pass
    import subprocess
    import tempfile
    try:
        import numpy as np
    except ImportError:
        return None
    ratios: List[float] = []
    with tempfile.TemporaryDirectory() as tmp:
        for t in (0.0, frame_ratio, 0.95):
            out = os.path.join(tmp, "f-%.2f.png" % t)
            try:
                subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                                "-ss", "%.3f" % t, "-i", str(video_path), "-frames:v", "1", out],
                               check=True, timeout=60, capture_output=True)
            except (OSError, subprocess.SubprocessError):
                continue
            try:
                from PIL import Image
                with Image.open(out) as img:
                    arr = np.asarray(img.convert("L"), dtype=np.float32)
            except (OSError, ImportError):
                continue
            h, w = arr.shape
            if h < 32 or w < 32:
                continue
            # 主体是画面里最亮的一块：取 85 分位做阈值，再用行投影裁出跨度和列跨度
            threshold = float(np.percentile(arr, 85))
            mask = arr >= threshold
            rows = np.where(mask.sum(axis=1) >= max(2, int(w * 0.02)))[0]
            cols = np.where(mask.sum(axis=0) >= max(2, int(h * 0.02)))[0]
            if rows.size < 4 or cols.size < 4:
                continue
            span_h = float(rows.size) / float(h)
            span_w = float(cols.size) / float(w)
            # 全屏泛白（阈值失效）时跨度接近 1，丢弃
            if span_h > 0.97 or span_w > 0.97:
                continue
            ratios.append(span_h)
    if not ratios:
        return None
    ratios.sort()
    median = ratios[len(ratios) // 2]
    if not (0.02 < median < 0.98):
        return None
    return round(median, 4)
