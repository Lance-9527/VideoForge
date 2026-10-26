# -*- coding: utf-8 -*-
"""VideoForge · 分镜引用完整性体检（"我关联了角色，人却没出现"）

★★ 用户实测反馈（2026-09-14）：
    「我用了 seedance 2.5，关联了场景+角色，还是没出现相关的人物」

查真数据后确认，**不是模型不听话，是我们的关联断了而且没人说**：

    分镜 `39844f1a` 的 `character_ids` 是
      ["ffe56cb9…(用户)", "931af29e…(旧 AI 神)"]
    而项目里的角色卡现在是
      ["ffe56cb9…(用户)", "e97e0a9e…(新 AI 神)"]
    —— 旧 AI 神那张卡被删掉/重建了，**分镜上留着的是一个悬空 ID**。

后果是静默的、而且很能骗人（当天真实发生的）：
  · 我们丢掉悬空 ID → 提示词里只写「画面中恰好 **1** 个主体：用户」；
  · 「AI 神」只出现在动作描述里 → 模型**自己编了一个发光壮汉**；
  · 界面没有任何提示，用户看到的是"人物不对/没出现"，
    而参考图明明在项目里躺着。

这正是参考方法论 Hell-Grind 校验器里的 `MISSING_REFERENCE`
（外键指向不存在的目标 = **error**，`validate_project.py:149-163`）要拦的东西 ——
我们一条都没有。本模块把它补上，并且**按名字给出可修的提示**（悬空 ID 修不回来，
但"本镜提到了某角色却没关联"是能修的）。

纯函数，不碰数据库、不碰网络。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional


logger = logging.getLogger("videoforge.refcheck")


def _norm(s: Any) -> str:
    return re.sub(r"[\s·・\.\-—_]+", "", str(s or ""))


def _mentions(text: str, name: str) -> bool:
    t, n = _norm(text), _norm(name)
    if len(n) < 2 or not t:
        return False
    return n in t or (len(t) >= 2 and t in n)


def _shot_text(shot: Dict[str, Any]) -> str:
    parts: List[str] = [str(shot.get("layer1_overview") or "")]
    tl = shot.get("layer2_timeline")
    if isinstance(tl, str):
        try:
            tl = json.loads(tl)
        except Exception:
            tl = []
    for seg in (tl or []):
        if not isinstance(seg, dict):
            continue
        for k in ("action", "expression", "camera", "dialogue"):
            v = seg.get(k)
            if isinstance(v, dict):
                parts.append(str(v.get("text") or ""))
                parts.append(str(v.get("character") or ""))
            else:
                parts.append(str(v or ""))
    l3 = shot.get("layer3_constraints")
    if isinstance(l3, str):
        try:
            l3 = json.loads(l3)
        except Exception:
            l3 = {}
    if isinstance(l3, dict):
        for k in ("must_appear", "must_keep", "must_happen"):
            v = l3.get(k)
            if isinstance(v, (list, tuple)):
                parts.extend(str(x) for x in v)
            elif v:
                parts.append(str(v))
    return "\n".join(p for p in parts if p)


def check_shot_refs(shot: Optional[Dict[str, Any]],
                    characters: Optional[Iterable[Dict[str, Any]]] = None,
                    scenes: Optional[Iterable[Dict[str, Any]]] = None,
                    ) -> Dict[str, Any]:
    """体检一镜的资产关联。返回：

    `{"ok", "severity", "linked", "dangling_ids", "mentioned_not_linked",
      "scene", "missing_scene", "hint", "issues": [{level,code,message}]}`

    判定（对齐 `validate_project.py` 的 `MISSING_REFERENCE` = error）：
      · `character_ids` 里的 ID 在角色表里**不存在** → error（悬空引用）；
      · 分镜文字里**提到**了某个角色卡的名字，但这一镜没关联它 → error：
        模型会**自己编一个**该角色（当天真实发生的就是这条）；
      · `scene_id` 指向不存在的场景 → warn（场景缺失时仍能出片，只是环境全靠文字）。
    """
    shot = shot or {}
    chars = [c for c in (characters or []) if isinstance(c, dict)]
    by_id = {str(c.get("id")): c for c in chars if c.get("id")}
    by_name = {_norm(c.get("name")): c for c in chars if c.get("name")}

    ids = shot.get("character_ids")
    if isinstance(ids, str):
        try:
            ids = json.loads(ids)
        except Exception:
            ids = []
    ids = [str(i) for i in (ids or []) if i]

    linked_ids = [i for i in ids if i in by_id]
    dangling = [i for i in ids if i not in by_id]
    linked_names = [str(by_id[i].get("name") or "") for i in linked_ids]
    linked_norm = {_norm(n) for n in linked_names}

    text = _shot_text(shot)
    mentioned_not_linked: List[Dict[str, str]] = []
    for c in chars:
        n = str(c.get("name") or "")
        if not n or _norm(n) in linked_norm:
            continue
        if _mentions(text, n):
            mentioned_not_linked.append({
                "id": str(c.get("id") or ""), "name": n,
                "has_reference": bool(c.get("reference_image_path")),
            })

    scenes = [s for s in (scenes or []) if isinstance(s, dict)]
    scene_id = str(shot.get("scene_id") or "")
    scene = next((s for s in scenes if str(s.get("id")) == scene_id), None) if scene_id else None
    missing_scene = bool(scene_id) and scene is None

    issues: List[Dict[str, str]] = []
    if dangling:
        issues.append({
            "level": "error", "code": "MISSING_REFERENCE",
            "message": (f"这一镜关联了 {len(dangling)} 个**已经不存在的角色 ID**"
                        f"（{', '.join(i[:8] for i in dangling)}）—— 多半是角色卡被删除/"
                        f"重建过。这几个角色**不会出现在提示词里，也不会发出参考图**，"
                        f"模型会自己编一个。"),
        })
    for m in mentioned_not_linked:
        issues.append({
            "level": "error", "code": "MENTIONED_BUT_NOT_LINKED",
            "message": (f"这一镜的文字里提到了「{m['name']}」，但它**没有关联到这一镜**"
                        + ("（它的参考图是有的，白关联了）" if m["has_reference"] else "")
                        + "—— 生成时模型只会照着文字自己造一个。"
                          "到分镜页点「🔗 补关联」可以按剧本修好。"),
        })
    if missing_scene:
        issues.append({
            "level": "warn", "code": "MISSING_SCENE",
            "message": "这一镜关联的场景已不存在，环境只能靠文字描述。",
        })

    severity = "error" if any(i["level"] == "error" for i in issues) else (
        "warn" if issues else "ok")
    hint = ""
    if severity == "error":
        hint = ("**先别急着花钱生成**：这一镜的角色关联是不完整的，"
                "生成出来的人物多半不是你要的。到分镜页点「🔗 补关联」"
                "（按剧本重新关联角色/场景），或者手工把角色补上再生成。")
    elif severity == "warn":
        hint = "场景关联缺失，画面环境可能和你的场景图不一致。"

    return {"ok": severity != "error", "severity": severity,
            "linked": linked_names, "dangling_ids": dangling,
            "mentioned_not_linked": mentioned_not_linked,
            "scene": str((scene or {}).get("name") or ""),
            "missing_scene": missing_scene,
            "issues": issues, "hint": hint,
            "summary": (f"关联角色 {len(linked_names)} 个"
                        + (f"；悬空 ID {len(dangling)} 个" if dangling else "")
                        + (f"；文字提到但未关联 {len(mentioned_not_linked)} 个"
                           if mentioned_not_linked else "")
                        + (f"；场景「{scene.get('name')}」" if scene else ""))}


def check_project_shots(shots: Optional[Iterable[Dict[str, Any]]],
                        characters: Optional[Iterable[Dict[str, Any]]] = None,
                        scenes: Optional[Iterable[Dict[str, Any]]] = None,
                        ) -> Dict[str, Any]:
    """整片体检：哪些镜头有关联问题（供分镜页批量显示 / 生成前一览）。"""
    rows = []
    n_bad = 0
    for sh in (shots or []):
        r = check_shot_refs(sh, characters, scenes)
        if r["severity"] == "error":
            n_bad += 1
        rows.append({"shot_id": sh.get("id"),
                     "order_index": (sh.get("order_index") or 0) + 1,
                     "severity": r["severity"], "summary": r["summary"],
                     "issues": r["issues"]})
    return {"total": len(rows), "error_shots": n_bad, "shots": rows,
            "ok": n_bad == 0}
