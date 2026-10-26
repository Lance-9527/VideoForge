# -*- coding: utf-8 -*-
"""
VideoForge · 台词回填（剧本有、分镜没有 → 写回分镜）
=====================================================

用户反馈（2026-10-23）：
    「音色太烂了…人物感情不对」—— 查出来的**真正病根**是：
    这个项目 4 个分镜的 `layer2_timeline[*].dialogue` **全是 null**，
    剧本里明明写着每一场有对白。配音阶段没句子可念 → 只能静音，
    或者用默认音色念画面概述 → 听起来就是"一个调、没感情"。

`core/dubbing.py:script_sources_for()` 已经在**配音那一刻**做了临时回填
（所以声音能出来），但**分镜表里仍然是空的** —— 用户在分镜页看不到台词，
下一轮重新生成分镜又会丢，体检也永远报"没有台词"。

所以这里做**持久化回填**：把剧本里对应场次的台词写进分镜的
`layer2_timeline`（落到 DB），让
  · 分镜页看得见台词；
  · 体检/字幕/配音都读到同一份；
  · 重跑分镜后还能再补回来（可重复执行、幂等）。

只**填空**不**改写**：分镜已经写了台词的镜头**一个字都不动**。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("videoforge.dialogue_backfill")


def _as_list(v: Any) -> List[Dict[str, Any]]:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:  # noqa: BLE001
            return []
    return [x for x in (v or []) if isinstance(x, dict)]


def script_scene_lines(script_row: Optional[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """从剧本行里取出"每一场的台词"。返回 [[{character,text,emotion}, ...], ...]"""
    if not isinstance(script_row, dict):
        return []
    scenes = _as_list(script_row.get("scenes"))
    out: List[List[Dict[str, Any]]] = []
    for sc in scenes:
        lines: List[Dict[str, Any]] = []
        for d in (sc.get("dialogues") or []):
            if not isinstance(d, dict):
                continue
            txt = str(d.get("text") or "").strip()
            if not txt:
                continue
            lines.append({"character": str(d.get("character") or ""),
                          "text": txt,
                          "emotion": str(d.get("emotion") or "")})
        out.append(lines)
    if out:
        return out
    # 剧本可能只存了 outline（JSON）没存 scenes
    oj = script_row.get("outline_json")
    if isinstance(oj, str) and oj.strip():
        try:
            oj = json.loads(oj)
        except Exception:  # noqa: BLE001
            oj = None
    if isinstance(oj, dict):
        for sc in (oj.get("scenes") or []):
            lines = []
            for d in (sc.get("dialogues") or []):
                if isinstance(d, dict) and str(d.get("text") or "").strip():
                    lines.append({"character": str(d.get("character") or ""),
                                  "text": str(d.get("text")).strip(),
                                  "emotion": str(d.get("emotion") or "")})
            out.append(lines)
    return out


def _shot_has_dialogue(shot: Dict[str, Any]) -> bool:
    for it in _as_list(shot.get("layer2_timeline")):
        d = it.get("dialogue")
        if isinstance(d, str) and d.strip():
            return True
        if isinstance(d, dict) and str(d.get("text") or "").strip():
            return True
        if isinstance(d, list) and any(
                isinstance(x, dict) and str(x.get("text") or "").strip() for x in d):
            return True
    return False


def backfill_project(db, project_id: str) -> Dict[str, Any]:
    """把剧本台词回填进**没有台词**的分镜。返回 {ok, filled, shots, lines, detail}。"""
    out: Dict[str, Any] = {"ok": False, "filled": 0, "shots": 0,
                            "lines": 0, "detail": []}
    try:
        script_row = db.get_script(project_id) or {}
    except Exception as e:  # noqa: BLE001
        out["detail"].append("读剧本失败：%s" % e)
        return out
    per_scene = script_scene_lines(script_row)
    if not per_scene:
        out["detail"].append("这个项目没有剧本台词（先出剧本）")
        return out
    try:
        shots = db.list_shots(project_id) or []
    except Exception as e:  # noqa: BLE001
        out["detail"].append("读分镜失败：%s" % e)
        return out
    shots = sorted(shots, key=lambda s: s.get("order_index") or 0)
    out["shots"] = len(shots)
    for i, sh in enumerate(shots):
        if _shot_has_dialogue(sh):
            continue
        lines = per_scene[i] if i < len(per_scene) else []
        if not lines:
            continue
        tl = _as_list(sh.get("layer2_timeline"))
        if not tl:
            continue
        # 只填空位：把台词挂到时间轴的**说话点**上（第一项或第一个没有台词的项）
        placed = 0
        for it in tl:
            d = it.get("dialogue")
            has = (isinstance(d, str) and d.strip()) or (
                isinstance(d, dict) and str(d.get("text") or "").strip()) or (
                isinstance(d, list) and any(isinstance(x, dict) and str(x.get("text") or "").strip()
                                            for x in d))
            if has:
                continue
            ln = lines[placed] if placed < len(lines) else None
            if ln is None:
                break
            it["dialogue"] = {"character": ln["character"], "text": ln["text"],
                              "emotion": ln.get("emotion") or ""}
            placed += 1
        if placed <= 0:
            continue
        try:
            db.update_shot(str(sh.get("id")),
                           layer2_timeline=json.dumps(tl, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            out["detail"].append("第%d镜写回失败：%s" % (i + 1, e))
            continue
        out["filled"] += 1
        out["lines"] += placed
        out["detail"].append("第%d镜补了 %d 句台词" % (i + 1, placed))
    out["ok"] = True
    return out
