# -*- coding: utf-8 -*-
"""
依据**本项目的剧本/分镜**生成白膜 3D 规格（JSON），供 `render_spec.py` 渲染。

★★★ 2026-10-20 新增。要解决的问题（用户原话：「那就做大工程」）：

  之前白膜渲染用的是 `data/previz_blender/design.py` —— 一份**为一个项目手写的**
  设计表（《人贩子亲爸》的楼顶/村口 + 8 个写死道具）。于是：
    · 任何新项目点「白膜预演」，渲出来都是**别人戏里的画面**；
    · 用户想"先看机位/走位再花钱"这件事**根本没发生**（他看的是无关画面）；
    · 想支持新项目只能人手再写一份 design.py。

  现在：从**分镜表**生成一份 JSON 规格（场景类型/尺寸/道具/人物站位与移动/
  相机关键帧），交给通用渲染器 `render_spec.py` 搭 3D。

  为什么产出 **JSON 而不是 Python**：
    · 生成的代码要在 Blender 里 exec，格式一歪就整段炸，且不好校验；
    · JSON 可以逐字段钳制（尺寸范围、道具数、人数、帧号），坏数据不会进渲染器；
    · 同一份 spec 还能给别的用途（出片时的机位/走位提示）。

  两级兜底（保证"永远有东西可渲"）：
    ① 模型按剧本生成 → ② 模型不可用/格式坏 → **确定性推断**（从镜头文字猜
       室内/室外、时长、人数），至少让白膜和这个项目**有关**。
"""

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("videoforge.previz_design")

SPEC_VERSION = 1
SPEC_COMPILER_REVISION = 2

#: 允许的场景类型（渲染器实现了这几种）
SET_KINDS = ("room", "corridor", "street", "roof", "field")

#: 允许的姿态（make_human 支持这些；其余按 stand 处理）
POSES = ("stand", "hold", "sit", "squat", "prone", "run", "walk", "pull", "dragged")


# ──────────────────────── 小工具 ────────────────────────

def _outputs_root(db) -> str:
    try:
        s = db.get_all_settings() or {}
        base = (s.get("output_dir") or "").strip()
        if base:
            return os.path.abspath(os.path.expanduser(base))
    except Exception:  # noqa: BLE001
        pass
    return os.path.join(os.environ.get("VIDEOFORGE_DATA_DIR") or os.getcwd(), "outputs")


def spec_path(db, project_id: str) -> str:
    return os.path.join(_outputs_root(db), project_id, "previz", "spec.json")


def _f(v, d: float = 0.0) -> float:
    try:
        out = float(v)
        return out if out == out and abs(out) != float("inf") else d   # 去掉 NaN/inf
    except (TypeError, ValueError):
        return d


def _clamp(v, lo, hi, d=0.0):
    return max(lo, min(hi, _f(v, d)))


# ──────────────────────── 规格校验 ────────────────────────

#: 一步的最大位移（Proma 实测：>0.34m 腿会被拉直，走起来像木偶）
MAX_STEP_M = 0.34


def techviz_check(spec: Dict[str, Any]) -> List[str]:
    """几何体检：主体出画 / 穿墙 / 超框 / 步幅过大 / 人物与相机重合。

    ★ 为什么要查：白膜是**花钱前唯一的机位检查机会**，这些问题在静态帧上最容易看出来，
      到成片才发现就是白花钱（调研报告里的 Techviz 定位）。
    """
    issues: List[str] = []
    for idx, shot in enumerate(spec.get("shots") or [], 1):
        tag = f"镜{idx}"
        st = shot.get("set") or {}
        width = float(st.get("width") or 0) or 0.0
        depth = float(st.get("depth") or 0) or 0.0
        height = float(st.get("height") or 0) or 0.0
        cam = shot.get("camera") or {}
        cam_pos = cam.get("position") or [0, -4, 1.5]
        for person in (shot.get("cast") or []):
            name = str(person.get("name") or "").strip() or "人物"
            x = float(person.get("x") or 0)
            y = float(person.get("y") or 0)
            # 超框：人物站在场景之外
            if width and (abs(x) > width / 2 + 0.5):
                issues.append(f"{tag}「{name}」横向超出场景范围（x={x:.2f} > 宽 {width:.2f}）")
            if depth and (y > depth + 0.5 or y < -1.0):
                issues.append(f"{tag}「{name}」纵深超出场景范围（y={y:.2f}）")
            # 穿墙/重合：人物与相机在同一位置
            if abs(y - float(cam_pos[1] if len(cam_pos) > 1 else 0)) < 0.6:
                issues.append(f"{tag}「{name}」与相机几乎重合（会穿帮/看不见人）")
            # 步幅：beats 之间位移
            path = person.get("path") or []
            for a, b in zip(path, path[1:]):
                try:
                    d = ((float(b[1]) - float(a[1])) ** 2 + (float(b[2]) - float(a[2])) ** 2) ** 0.5
                except Exception:  # noqa: BLE001
                    continue
                frames = abs(int(b[0]) - int(a[0])) or 1
                step = d / max(1.0, frames / 24.0)
                if step > MAX_STEP_M:
                    issues.append(
                        f"{tag}「{name}」步幅过大（{step:.2f}m/步 > {MAX_STEP_M}m，会像木偶）")
        # 接地：人物高度必须合理（不在地上也不会在天上）
        for person in (shot.get("cast") or []):
            h = float(person.get("height") or 0)
            if h and not (1.2 <= h <= 2.1):
                issues.append(f"{tag} 人物身高异常（{h:.2f}m），白膜比例会失真")
        # 相机是否在场景外
        if width and depth:
            cx = float(cam_pos[0] if cam_pos else 0)
            cy = float(cam_pos[1] if len(cam_pos) > 1 else 0)
            if abs(cx) > width / 2 + 2.0 or cy > depth + 2.0 or cy < -depth - 2.0:
                issues.append(f"{tag} 相机在场景之外（{cx:.1f},{cy:.1f}），画面会空")
        if height and cam_pos and len(cam_pos) > 2 and float(cam_pos[2]) > height + 1.5:
            issues.append(f"{tag} 相机高于场景天花板（z={float(cam_pos[2]):.1f}）")
    return issues


def normalize_spec(spec: Any, shots: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """把模型给的规格**钳制成渲染器一定接得住**的形状。

    任何一处不合法都就地修（不整份丢弃）：钳尺寸、限数量、补默认值。
    返回 None 表示连镜头列表都对不上（调用方会走确定性兜底）。
    """
    if not isinstance(spec, dict):
        return None
    raw = spec.get("shots")
    if not isinstance(raw, list) or not raw:
        return None

    out_shots: List[Dict[str, Any]] = []
    for i, sh in enumerate(raw[:12]):
        if not isinstance(sh, dict):
            continue
        dur = _clamp(sh.get("duration"), 2.0, 20.0, 5.0)
        # 与库里的分镜时长对齐（分镜说了几秒就是几秒）
        if i < len(shots):
            dur = _clamp(shots[i].get("duration_seconds"), 2.0, 20.0, dur)
        frames = int(round(dur * 24))

        st = sh.get("set") if isinstance(sh.get("set"), dict) else {}
        kind = str(st.get("kind") or "room").lower()
        if kind not in SET_KINDS:
            kind = "room"
        props = []
        for p in (st.get("props") or [])[:12]:
            if isinstance(p, dict) and p.get("type"):
                props.append({"type": str(p["type"])[:16],
                              **{key:p[key] for key in ('id','anchor','on_surface','role') if key in p},
                              "x": _clamp(p.get("x"), -20, 20),
                              "y": _clamp(p.get("y"), -20, 20),
                              "z": _clamp(p.get("z"), 0, 4),
                              "rot": _clamp(p.get("rot"), -180, 180),
                              "w": _clamp(p.get("w"), 0, 6),
                              "d": _clamp(p.get("d"), 0, 6),
                              "h": _clamp(p.get("h"), 0, 4)})

        cast = []
        for c in (sh.get("cast") or [])[:5]:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            pose = str(c.get("pose") or "stand").lower()
            if pose not in POSES:
                pose = "stand"
            path = []
            for p in (c.get("path") or [])[:4]:
                if isinstance(p, (list, tuple)) and len(p) >= 3:
                    # ★ 2026-10-20：path 的第 4 位是**竖直偏移**（不是"走了几米"）。
                    #   实测模型经常写成 3（它以为那是距离/速度）⇒ 人被抬到天花板
                    #   上面 3 米，画面里根本看不到人（"人物1 对象24"却空无一人）。
                    #   人的竖直偏移不可能超出 ±0.6m（上台阶/下坡而已）。
                    path.append([int(_clamp(p[0], 1, frames, 1)),
                                 _clamp(p[1], -20, 20), _clamp(p[2], -20, 20),
                                 _clamp(p[3] if len(p) > 3 else 0, -0.3, 0.6)])
            cast.append({"name": str(c["name"])[:20],
                         "x": _clamp(c.get("x"), -20, 20),
                         "y": _clamp(c.get("y"), -20, 20),
                         "rot": _clamp(c.get("rot"), -360, 360, 180),
                         "pose": pose,
                         "height": _clamp(c.get("height"), 0.9, 2.1, 1.72),
                         "path": path})

        cam = sh.get("camera") if isinstance(sh.get("camera"), dict) else {}
        from core.previz_match import explicit_camera_path,_positive
        _explicit_path=explicit_camera_path(cam)
        # ★★ 2026-10-20：**按场景尺寸钳制相机**（实测踩到的画面事故）。
        #   模型把相机放在 (x=-1.5, y=-0.5, z=0.6) 而走廊只有 3.2m 宽（墙在 ±1.6）
        #   ⇒ 镜头离左墙仅 **10 公分**、高度只有膝盖高 —— 渲出来一块大灰板
        #   糊住 40% 画面，这一镜的白膜等于废了（用户看它没有任何意义）。
        #   规则：离侧墙 ≥0.8m、高度 0.7~2.4m（人眼高度带）、
        #        且不能穿进后墙里（y ≤ depth/2 - 0.8）。
        _sw = _clamp(st.get("width"), 1.5, 24, 6)
        _sd = _clamp(st.get("depth"), 1.5, 40, 8)
        _xlim = max(0.0, _sw / 2 - 0.8)
        _ylim = _sd / 2 - 0.8

        def _fix_cam(px, py, pz):
            if _explicit_path:return _f(px),_f(py),_f(pz,1.5)
            px = max(-_xlim, min(_xlim, _f(px)))
            py = min(_ylim, _f(py))                  # 在 -Y 侧或场内，不进后墙
            pz = max(0.7, min(2.4, _f(pz, 1.5)))     # 人眼高度带
            return px, py, pz

        moves = []
        for m in (cam.get("moves") or [])[:6]:
            if isinstance(m, (list, tuple)) and len(m) >= 7:
                _mx, _my, _mz = _fix_cam(m[1], m[2], m[3])
                _lx, _ly, _lz = _fix_cam(m[4], m[5], m[6])
                coord=(lambda value:value) if _explicit_path else (lambda value:round(value,2))
                moves.append([int(_clamp(m[0], 1, frames, 1)),
                              coord(_mx), coord(_my), coord(_mz),
                              coord(_lx), coord(_ly), coord(_lz),
                              _clamp(m[7] if len(m) > 7 else 35, 12, 120, 35)])
        if moves:
            moves.sort(key=lambda x: x[0])
            moves[0][0] = 1
            if len(moves) > 1:
                moves[-1][0] = max(moves[-2][0] + 1, min(frames, moves[-1][0]))
        _pos = [_clamp(v, -30, 30) for v in
                (cam.get("position") or [0, -4, 1.6])[:3]] or [0, -4, 1.6]
        _pos = list(_fix_cam(*_pos))
        _look = [_clamp(v, -30, 30) for v in
                 (cam.get("look_at") or [0, 0, 1.2])[:3]] or [0, 0, 1.2]
        # ══════════════════════════════════════════════════════════════
        # ★★ 2026-10-24（用户三连问：正对镜头 / 空桌 / 没运镜）
        #   白膜是"花钱前唯一的机位检查机会"，这三件事必须在规格里定死，
        #   否则白膜渲出来就是：人物对着镜头傻站、桌上空无一物、整镜一动不动。
        # ══════════════════════════════════════════════════════════════
        # ① 道具兜底：空桌 = 白膜没有信息量。规格没给就从镜头文本里抽（复用已有函数）。
        if not props:
            try:
                _raw_shot = shots[i] if 0 <= i < len(shots) else {}
                _txt = " ".join([str(_raw_shot.get("layer1_overview") or ""),
                                 str(_raw_shot.get("scene_name") or ""),
                                 str(_raw_shot.get("location") or "")])
                props = _props_from_text(_txt, _sw, _sd)
            except Exception as _e:  # noqa: BLE001
                logger.warning("白膜道具兜底失败（该镜可能渲成空场景）：%s", _e)

        # ② 朝向：人物面向**相机在看的东西**（桌子/屏幕/对手），不是面向镜头。
        #    原来 facing 固定 "right"，相机放右侧时就等于正对镜头。
        _look_x = float(_look[0]) if _look else 0.0
        _look_y = float(_look[1]) if len(_look) > 1 else 0.0
        _look_z = float(_look[2]) if len(_look) > 2 else 1.2
        for _p in cast:
            try:
                _px = _f(_p.get("x"))
            except Exception:  # noqa: BLE001
                _px = 0.0
            _face = "right" if _look_x > _px else "left"
            if abs(_look_x - _px) < 0.25:
                # 相机正对着人物 → 用道具位置定朝向（桌面在右就面向右）
                _prop_x = None
                for _q in props or []:
                    if isinstance(_q, dict) and _q.get("x") is not None:
                        try:
                            _prop_x = float(_q.get("x"))
                            break
                        except Exception:  # noqa: BLE001
                            continue
                _face = "right" if (_prop_x is None or _prop_x >= _px) else "left"
            _p["facing"] = _face
            _p["look_target"] = [round(_look_x, 2), round(_look_y, 2), round(_look_z, 2)]

        from core.previz_props import normalize_props
        props=normalize_props(props,(shot_text(shots[i],limit=6000) if i<len(shots) else '')
                              + ' '+str(sh.get('note') or ''))
        # A missing numeric path is a stationary camera, never an automatic push-in.
        if not moves:
            try:
                _end = frames
                _cx, _cy, _cz = _pos
                _lx, _ly, _lz = _look
                _k = 0.0
                _nx = _cx + (_lx - _cx) * _k
                _ny = _cy + (_ly - _cy) * _k
                _nz = _cz + (_lz - _cz) * _k * 0.5
                _lens = _clamp(cam.get("lens"), 12, 120, 35)
                moves = [
                    [1, round(_cx, 2), round(_cy, 2), round(_cz, 2),
                     round(_lx, 2), round(_ly, 2), round(_lz, 2), _lens],
                    [_end, round(_nx, 2), round(_ny, 2), round(_nz, 2),
                     round(_lx, 2), round(_ly, 2), round(_lz, 2), _lens],
                ]
            except Exception as _e:  # noqa: BLE001
                logger.warning("白膜运镜关键帧兜底失败（该镜会是固定机位）：%s", _e)

        out_shots.append({
            "duration": round(dur, 2),
            "movement": str(sh.get("movement") or "static")[:20],
            "set": {"kind": kind,
                    "width": _sw,
                    "depth": _sd,
                    "height": _clamp(st.get("height"), 1.8, 8, 2.8),
                    "enclosure":st.get('enclosure') or ('closed' if _positive(shot_text(shots[i],limit=6000) if i<len(shots) else '', '封闭') else 'cutaway'),
                    "props": props},
            "cast": cast,
            "camera": {"position": [round(v, 2) for v in _pos],
                       "look_at": [round(v, 2) for v in _look],
                       "lens": _clamp(cam.get("lens"), 12, 120, 35),
                       "moves": moves,"_explicit_path":_explicit_path,
                       "framing_policy":cam.get('framing_policy') or ('preserve' if re.search(r'\d+(?:\.\d+)?\s*mm',shot_text(shots[i],limit=6000) if i<len(shots) else '',re.I) else 'fit_lens')},
            "note": str(sh.get("note") or "")[:200],
        })

    if not out_shots:
        return None
    # 2026-10-25：用统一镜头契约强制机位。
    # 为什么必须强制：实测 LLM 把剧本里的「以主体为圆心弧线横移、距离恒定」
    # 写成了 movement=push_in，白膜与提示词因此同时跑偏。契约层按剧本意图
    # 重算关键帧（弧线用恒定半径重建），保证白膜与剧本同源。
    try:
        from core.shot_contract import enforce_shot_camera
        for _i, _shot_spec in enumerate(out_shots):
            _text = shot_text(shots[_i], limit=6000) if _i < len(shots) else ""
            out_shots[_i] = enforce_shot_camera(_shot_spec, _text)
    except Exception as _e:  # noqa: BLE001
        logger.warning("镜头契约强制机位失败（保留原 spec）：%s", _e)
    return {"version": SPEC_VERSION,"compiler_revision":SPEC_COMPILER_REVISION, "shots": out_shots}


# ──────────────────────── 确定性补全 ────────────────────────

# ──────────────────────── 镜头文本 / 空镜判定 ────────────────────────

#: 库里镜头的文本字段。
#: ★★ 实测（2026-10-20）：`shots` 表**没有** title/description —— 真正的文字在
#:   layer1_overview（整段描述）、layer2_timeline（逐段 action/camera）、
#:   layer3_constraints（禁项）里。只读 title/description 的话，
#:   **真实项目的镜头对模型来说几乎是空的** → 白膜自然跟剧本对不上。
_SHOT_TEXT_KEYS = ("layer1_overview", "layer2_timeline", "layer3_constraints",
                   "title", "description", "summary", "prompt")

#: "这一镜不该有人"的说法
_EMPTY_HINTS = ("无人出现", "无人脸", "空镜", "无人物", "没有人", "空无一人",
                "不允许出现人物", "不出现人物", "不得出现人物")


def shot_text(shot: Dict[str, Any], limit: int = 900) -> str:
    """把一个镜头里所有可读文字拼起来（给模型看 / 给关键词匹配用）。"""
    if not isinstance(shot, dict):
        return ""
    parts=[]
    for key in _SHOT_TEXT_KEYS:
        value=shot.get(key)
        if not value:continue
        if key in ('layer2_timeline','layer3_constraints'):
            if isinstance(value,str):
                try:value=json.loads(value)
                except (ValueError,TypeError):parts.append(value);continue
            if key=='layer2_timeline' and isinstance(value,list):
                for beat in value:
                    if isinstance(beat,dict):
                        for name in ('action','camera','props_used'):
                            if beat.get(name):parts.append(('摄影机：' if name=='camera' else '')+str(beat[name]))
                continue
            if key=='layer3_constraints' and isinstance(value,dict):
                for name in ('must_not_appear','negative_prompt'):
                    if value.get(name):parts.append('禁止：'+str(value[name]))
                continue
        parts.append(str(value))
    return " ".join(parts)[:limit]


def is_empty_scene(text: str) -> bool:
    """这一镜是不是"空镜 / 不许有人"。

    ★ 为什么必须判：《日常反转》两个镜头明写「一个无人物出现的安静空镜」，
      且 layer3 有 `must_not_appear: ["人物","人脸"]`。
      补全逻辑不看这个，就会往空镜里**硬塞一个人** —— 白膜立刻"不是这条片子"。
    """
    t = str(text or "")
    return any(h in t for h in _EMPTY_HINTS)


#: 从剧本文字里能认出来的道具（词 → 渲染器的道具类型）
_PROP_WORDS = (
    ("table", ("桌", "案", "几")),
    ("chair", ("椅", "凳")),
    ("bed", ("床", "炕")),
    ("sofa", ("沙发",)),
    ("door", ("门", "门口", "门口", "闸")),
    ("window", ("窗",)),
    ("lamp", ("灯", "路灯", "台灯", "吊灯")),
    ("car", ("车", "汽车", "出租", "面包车")),
    ("tree", ("树",)),
    ("pole", ("电线杆", "杆子", "柱子")),
    ("sign", ("招牌", "告示", "牌", "广告")),
    ("shelf", ("书架", "货架", "架子")),
)


def _props_from_text(text: str, width: float, depth: float) -> List[Dict[str, Any]]:
    """从镜头文字里认出该摆什么，按"靠墙/靠边"的规则给坐标。

    ★ 为什么要这层：实测模型给的规格**常常把 props 留空**（它怕乱加东西），
      于是走廊里空无一物 —— 白膜就退化成"一个空盒子"，用户看不出是哪儿。
      剧本明确写了告示/路灯的，就该摆出来。
    """
    t = str(text or "")
    out: List[Dict[str, Any]] = []
    seen = set()
    for ptype, words in _PROP_WORDS:
        if ptype in seen:
            continue
        if not any(w in t for w in words):
            continue
        seen.add(ptype)
        n = len(out)
        # 靠左墙 / 靠右墙 交替，纵向铺开，别挡人
        side = -1 if n % 2 == 0 else 1
        x = side * max(0.4, min(2.5, width / 2 - 0.55))
        y = depth * (0.12 + 0.22 * (n % 3))
        z = 0.0
        if ptype == "lamp":
            z = max(2.0, 2.4)          # 挂/立灯，抬到 2m 以上才看得见
        if ptype == "sign":
            z = 1.4                     # 招牌/告示贴墙中高处
        out.append({"type": ptype, "x": round(x, 2), "y": round(y, 2),
                    "z": round(z, 2), "rot": 0, "w": 0, "d": 0, "h": 0})
        if len(out) >= 5:
            break
    return out


def enrich_spec(spec: Dict[str, Any], shots: List[Dict[str, Any]],
                char_names: List[str]) -> Dict[str, Any]:
    """把"模型太保守"留下的空洞补上（**确定性**，不依赖模型）。

    两条硬规则：
      · 剧本里点到了角色名 → 这一镜必须有这个人（站中间，朝向相机侧）；
      · 文字里出现了桌/门/灯/招牌… → 就摆一个（靠边，不挡人）。
    """
    if not isinstance(spec, dict):
        return spec
    names = [n for n in (char_names or []) if n]
    for i, sh in enumerate(spec.get("shots") or []):
        text = shot_text(shots[i] if i < len(shots) else {})
        st = sh.get("set") or {}
        w = _f(st.get("width"), 6.0)
        d = _f(st.get("depth"), 9.0)

        # ① 道具：**合并**补齐，而不是"空了才填"。
        #   实测模型只给了 2 个门，而剧本明写"墙上贴着告示、尽头有一盏路灯" ——
        #   只补空的话这两样永远不会出现，白膜就和剧本对不上。
        got = _props_from_text(text, w, d)
        if got:
            have = [str(p.get("type") or "") for p in (st.get("props") or [])
                    if isinstance(p, dict)]
            extra = [p for p in got if p["type"] not in have]
            if extra:
                st["props"] = list(st.get("props") or []) + extra
                sh["set"] = st

        # ② 缺人物 → 按"文字里点名的角色"补；一个都没点名就用第一个角色。
        #   ★★ 空镜（"无人出现" / must_not_appear 人物）**绝不能塞人** ——
        #     《日常反转》两镜都是空镜，塞进一个人白膜就"不是这条片子"了。
        if not (sh.get("cast") or []) and not is_empty_scene(text):
            present = [n for n in names if n and n in text][:2]
            if not present:
                present = names[:1]
            cast = []
            for k, nm in enumerate(present):
                cast.append({"name": nm,
                             "x": round(-0.45 + k * 0.95, 2), "y": 0.0,
                             "rot": 180, "pose": "stand", "height": 1.72,
                             "path": []})
            if cast:
                sh["cast"] = cast
    return spec


# ──────────────────────── 确定性兜底 ────────────────────────

_KIND_HINTS = (
    ("corridor", ("走廊", "楼道", "过道", "通道", "巷", "隧道")),
    ("roof", ("楼顶", "天台", "屋顶", "阳台")),
    ("street", ("街", "马路", "路", "广场", "店", "门口", "车站")),
    ("field", ("田", "野外", "村", "山", "河边", "草地", "树林")),
    ("room", ("房", "屋", "室", "厅", "床", "桌", "厨房", "办公")),
)


def _guess_kind(text: str) -> str:
    t = str(text or "")
    for kind, words in _KIND_HINTS:
        if any(w in t for w in words):
            return kind
    return "room"


def build_fallback_spec(db, project_id: str) -> Dict[str, Any]:
    """模型不可用时：从分镜文字里抽取**至少和这个项目有关**的最小规格。

    不做花架子：场景类型按关键词猜、人物按资产库里的角色摆、相机给个能看全的
    中景。它比"别人的楼顶/村口"有意义得多 —— 至少是**这场戏的空间**。
    """
    shots = db.list_shots(project_id) or []
    chars = []
    try:
        chars = db.list_characters(project_id) or []
    except Exception:  # noqa: BLE001
        chars = []
    names = [str(c.get("name") or "") for c in chars if c.get("name")][:3]

    out = []
    for sh in shots[:12]:
        text = shot_text(sh)
        kind = _guess_kind(text)
        dur = _clamp(sh.get("duration_seconds"), 2.0, 20.0, 5.0)
        cast = []
        # ★ 空镜不摆人（同 enrich_spec 的理由）
        if not is_empty_scene(text):
            for i, nm in enumerate(names):
                cast.append({"name": nm,
                             "x": -0.55 + i * 1.1, "y": 0.0, "rot": 180,
                             "pose": "stand", "height": 1.72, "path": []})
        out.append({
            "duration": round(dur, 2),
            "movement": "static",
            "set": {"kind": kind, "width": 6, "depth": 9, "height": 2.8,
                    "props": _props_from_text(text, 6, 9)},
            "cast": cast,
            "camera": {"position": [0, -4.2, 1.55], "look_at": [0, 0, 1.15],
                       "lens": 35, "moves": []},
            "note": ("（兜底规格：模型不可用，场景类型/道具按镜头文字推断，"
                     "只保证空间与机位可看）"),
        })
    return {"version": SPEC_VERSION, "shots": out}


# ──────────────────────── 模型生成 ────────────────────────

_SYS = (
    "你是电影预演（previz）的**技术美术**。你的输出会被一个确定性的 Blender 脚本"
    "直接读成 3D 场景，所以只需要**摆放与机位**，不要写文学描写，不要写代码。\n"
    "坐标系：+X 右、+Y 远、+Z 上；地面 Z=0；单位=米；人物默认面向 -Y（rot=180）。\n"
    "相机默认在 -Y 侧（负 y）看向 +Y 方向，y 约为 -2 ~ -8，高度 1.2~1.8。\n"
    "\n"
    "★ 白膜是什么（Proma 训练沉淀，规范见 PREVIZ_SPEC.md）：\n"
    "  白膜 = **空间关系 + 运镜路径 + 时间节奏**的三维预演，给下游视频模型当运动参考。\n"
    "  它**不是**成片：没有材质、没有景深、没有布料动力学。成本恒为 0（本地 Blender 渲染）。\n"
    "\n"
    "★ 每镜先定**镜头任务**，再选拍法（顺序反了就会写出空话）：\n"
    "  建立空间=谁在哪、什么环境 → 大全景/全景/固定机位\n"
    "  对峙蓄力=双方关系、实力差距 → 中景/推近\n"
    "  出招动作=谁先动手、怎么动 → 中景跟拍/摇摄\n"
    "  撞击结果=碰撞结果 → 特写/移焦\n"
    "  表演情绪=情绪变化 → 特写/环绕\n"
    "\n"
    "★ 每镜按 **4 段式**写：起点状态 / 人物动作 / 摄影机运动 / 终点状态。\n"
    "  起点状态 = 机位(距离/高度/角度/焦距) + 人物空间关系 + 关键道具位置\n"
    "  人物动作 = 只写动作（具体动词 + 方向 + 重心变化），不写镜头\n"
    "  摄影机运动 = 只写镜头（起始位置 + 拍法/幅度 + 落点 + 起止焦距），不写人物\n"
    "  终点状态 = 机位 + 人物姿态 + 道具状态 + 环境反馈\n"
    "\n"
    "★ 时间打点：把动作拆成 2~3 段（例：0-3s approach / 3-5s turn / 5-8s reveal），"
    "每段给一个人物位置与相机位置，落点必须**衔接**（上一段落点 = 下一段起点）。\n"
    "  人物移动步幅 ≤ 0.34m/步（实测超过就会腿被拉直、像木偶）。\n"
    "  人物必须有**接触点**（脚踩在 Z=0 地面上），不许浮空。\n"
    "\n"
    "★ 只摆剧本里真的出现的东西与角色；角色名要**原样复述**剧本里的名字（下游按名字对参考图）。"
)

_SCHEMA_HINT = """返回**严格 JSON**（不要 markdown 围栏、不要解释），形状：
{"version":1,"shots":[
 {"duration":5,
  "movement":"static|push_in|push_out|pan|orbit|track|descend|handheld",
  "set":{"kind":"room|corridor|street|roof|field","width":6,"depth":9,"height":2.8,
         "props":[{"type":"door|window|table|desk|chair|bed|sofa|lamp|car|tree|pole|sign|shelf|box",
                   "id":"table_1","x":0,"y":3,"z":0,"anchor":"base","w":1.2,"d":0.7,"h":0.75,"rot":0}]},
  "cast":[{"name":"角色名","x":0,"y":0,"rot":180,
           "pose":"stand|hold|sit|squat|prone|run","height":1.72,
           "path":[[1,0,0,0],[96,0,2.0,0]]}],
  "camera":{"position":[0,-4.2,1.55],"look_at":[0,0,1.15],"lens":35,
            "moves":[[1,0,-4.2,1.55,0,0,1.15,35],[96,0,-3.0,1.5,0,0,1.15,38]]},
  "note":"一句话：这镜在演什么",
  "purpose":"建立空间|对峙蓄力|出招动作|撞击结果|表演情绪",
  "start_state":"起点状态：机位 + 人物空间关系 + 关键道具位置（一句话）",
  "beats":[{"start":0,"end":72,"action":"这段的动作（只写动作）",
            "cast_pos":[0,0],"camera_pos":[0,-4.2,1.55],"focus":[0,0,1.15]}],
  "end_state":"终点状态：机位 + 人物姿态 + 道具状态 + 环境反馈（一句话）"} ]}
硬要求：
· shot 数量与顺序**必须**和给定分镜一致，duration 用分镜给的秒数；
· 24fps，所以最后一帧 = duration*24；moves 的第一帧必须是 1；
· cast 的 y 与相机 y 不能重合（人物在 y≈0，相机在 y≈-4）；
· set 的尺寸要像真地方：窄巷宽 2~3.5m、普通房间 4~7m、街道宽 6~10m；
  **kind 的含义**：room=有墙有顶的室内；corridor=窄长通道；street=两侧有建筑的城市街道；
  roof=楼顶（四周无墙、有护栏）；field=**开阔野外**（田地/雪原/山野/河边 —— 地平线开阔、
  不要四面墙、不要天花板，远处可以有低矮起伏与稀疏树木剪影）；
· 只摆剧本里真的提到的东西（没提到就别加车、别加树）；
· props的z明确表示底面高度，anchor默认base；用center时需给h；立方体w=d=h；
  摆在桌上的物品必须给on_surface:"桌的id"，由系统将底面放到支撑面，不能写猜测的浮空z；
· camera.moves是明确的机位合同，缓慢运动和停稳应按真实时间给关键帧，不自动变成推近；
· beats 的段要**时间递增且首尾相接**（第 N 段的 end = 第 N+1 段的 start）；
  人物在 beats 里的位移 ≤ 0.34m/步；相机 moves 的第一帧必须是 1 且落点与 beats 对得上；
· purpose 必填（决定这一镜的任务）；start_state / end_state 各一句话，不超过 60 字。"""


def _make_llm(db):
    """按当前设置造一个同步可用的 LLM 客户端（含自定义 base_url）。"""
    from core.chat import resolve_llm_settings
    from core.llm import LLMClient

    s = db.get_all_settings() or {}
    r = resolve_llm_settings(s)
    kw = dict(provider=r["provider"], api_key=r["api_key"], model=r["model"])
    base = str(s.get("llm_base_url") or "").strip()
    if base:
        kw["base_url"] = base.rstrip("/")
    return LLMClient(**kw)


def _script_context(db, project_id: str) -> str:
    parts = []
    try:
        proj = db.get_project(project_id) or {}
        if proj.get("title"):
            parts.append("【片名】" + str(proj.get("title")))
        if proj.get("description"):
            parts.append("【一句话】" + str(proj.get("description"))[:200])
    except Exception:  # noqa: BLE001
        pass
    try:
        sc = db.get_script(project_id)
        if sc:
            for k in ("outline", "title", "logline"):
                if sc.get(k):
                    parts.append("【剧本 %s】%s" % (k, str(sc[k])[:600]))
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(parts)[:2000]


def _shots_context(shots: List[Dict[str, Any]]) -> str:
    lines = []
    for i, s in enumerate(shots[:12], start=1):
        bits = [f"镜{i}"]
        if s.get("duration_seconds"):
            bits.append("时长 %ss" % s.get("duration_seconds"))
        # ★ 用统一文本（layer1_overview + layer2_timeline 的 action/camera +
        #   layer3 的禁项）—— 只读 title/description 时，真实项目几乎给不出信息，
        #   模型只能瞎猜场景。
        _txt = shot_text(s, limit=520)
        if _txt:
            bits.append(_txt)
        lines.append(" | ".join(bits))
    return "\n".join(lines)


def generate_spec(db, project_id: str, *, force: bool = False) -> Dict[str, Any]:
    """生成并落盘本项目的白膜规格。

    Returns: {ok, spec, path, source: "llm"|"fallback"|"cache", reason?}
    """
    path = spec_path(db, project_id)
    shots = db.list_shots(project_id) or []
    if not shots:
        return {"ok": False, "reason": "这个项目还没有分镜 —— 先拆镜头再渲白膜。"}
    # ★★ 2026-10-23：**分镜/提示词变了，白膜 spec 必须重算**。
    #   否则缓存永远返回旧 spec —— 用户改了提示词，白膜还是上一版机位，
    #   这就是「白膜跟分镜提示词出入很大」的根之一。
    _fp = ""
    try:
        from core import previz_match as _pm0
        _fp = _pm0.prompt_fingerprint(shots)
    except Exception:  # noqa: BLE001
        _fp = ""
    if not force and os.path.exists(path):
        try:
            with open(path, encoding="utf-8-sig") as f:
                cached = json.load(f)
            if isinstance(cached, dict) and cached.get("shots") and cached.get('compiler_revision')==SPEC_COMPILER_REVISION:
                _old_fp = str(cached.get("source_fingerprint") or "")
                if _fp and _old_fp != _fp:
                    logger.info("白膜 spec 已过期（分镜/提示词变了 %s→%s）→ 重新生成",
                                _old_fp or "?", _fp)
                else:
                    return {"ok": True, "spec": cached, "path": path, "source": "cache"}
        except Exception:  # noqa: BLE001
            pass

    spec = None
    reason = ""
    try:
        llm = _make_llm(db)
        user = (_SCHEMA_HINT + "\n\n" + _script_context(db, project_id)
                + "\n\n【分镜表（按顺序）】\n" + _shots_context(shots))
        text = asyncio.run(llm.chat(
            [{"role": "system", "content": _SYS},
             {"role": "user", "content": user}],
            max_tokens=3000, temperature=0.2))
        from core.llm import extract_json
        spec = normalize_spec(extract_json(text), shots)
        if spec is not None:
            # ★ 模型常常把 props/cast 留空（怕乱加）→ 确定性补全，
            #   否则走廊里空无一物，白膜退化成"一个空盒子"。
            names = []
            try:
                names = [str(c.get("name") or "") for c in
                         (db.list_characters(project_id) or []) if c.get("name")]
            except Exception:  # noqa: BLE001
                names = []
            spec = enrich_spec(spec, shots, names)
        if spec is None:
            reason = "模型返回的规格对不上分镜，已用确定性推断"
    except Exception as e:  # noqa: BLE001
        reason = "模型生成失败（%s: %s），已用确定性推断" % (type(e).__name__, str(e)[:120])
        logger.warning("白膜规格生成失败：%s", e)

    source = "llm"
    if spec is None:
        spec = normalize_spec(build_fallback_spec(db, project_id), shots)
        source = "fallback"
    if spec is None:
        return {"ok": False, "reason": reason or "无法生成白膜规格"}

    # ★ 2026-10-23（用户：「白膜要匹配分镜提示词」）：
    #   写盘前做一次**确定性校正** —— 运镜/焦距/人物姿态按该镜的 video_prompt 对齐，
    #   免得 LLM 的 spec 与提示词各说一套（实测：坐着的镜头被写成"带速度进画"）。
    try:
        from core import previz_match as _pm
        _pairs = []
        for _i, _sh in enumerate(sorted(shots, key=lambda x: x.get("order_index") or 0)):
            _vp = _pm.video_prompt_of(_sh)
            _src = "video_prompt"
            if not _vp:
                # 提示词还没生成（或老项目）→ 用分镜文字兜底，并标明来源
                _vp = shot_text(_sh, limit=1200)
                _src = "shot_text"
            _pairs.append((_i, _vp, _src))
        spec = _pm.reconcile_spec(spec, _pairs)
    except Exception as _e:  # noqa: BLE001
        logger.warning("白膜 spec 校正失败（按原 spec 继续）：%s", _e)
    if _fp:
        spec["source_fingerprint"] = _fp
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(spec, f, ensure_ascii=False, indent=1)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": "规格写盘失败：%s: %s" % (type(e).__name__, e)}

    return {"ok": True, "spec": spec, "path": path, "source": source,
            "reason": reason}


def load_spec(db: Any, project_id: str) -> Optional[Dict[str, Any]]:
    """读回已生成的白膜规格（没有就 None）。"""
    path = spec_path(db, project_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None
