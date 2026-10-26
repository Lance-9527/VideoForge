# -*- coding: utf-8 -*-
"""白膜规格说明导出（blockout 的做法：白膜视频 + 每机位静帧 + 自动 prompt.txt）。

★ 为什么要有这个文件：白膜的价值是**花钱前把机位/走位/节奏定死**，
  但"定死了什么"如果只存在于一堆 JSON 里，用户看不到、下游模型也拿不到。
  所以把每一镜锁定的内容写成一段**人能读、模型能吃**的规格说明，
  与白膜视频、静帧放在一起。

产出：`<output_root>/<pid>/previz/白膜规格.txt`
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


def notes_path(output_root: str, project_id: str) -> str:
    """白膜规格说明文件位置。"""
    return str(Path(output_root) / str(project_id) / "previz" / "白膜规格.txt")


def build_shot_notes(shot: Dict[str, Any], index: int) -> str:
    """一镜的白膜规格（人话）。"""
    lines: List[str] = []
    n = index + 1
    purpose = str(shot.get("purpose") or "").strip()
    lines.append(f"■ 镜{n}" + (f"　镜头任务：{purpose}" if purpose else ""))
    start = str(shot.get("start_state") or "").strip()
    end = str(shot.get("end_state") or "").strip()
    if start:
        lines.append(f"　起点状态：{start}")
    for beat in (shot.get("beats") or []):
        if not isinstance(beat, dict):
            continue
        lines.append(
            f"　打点 {beat.get('start')}–{beat.get('end')}s：{str(beat.get('action') or '').strip()}")
    cam = shot.get("camera") or {}
    pos = cam.get("position") or []
    look = cam.get("look_at") or []
    if pos:
        lines.append(f"　机位起点：{pos}　看向：{look}　焦距：{cam.get('lens')}")
    moves = cam.get("moves") or []
    if len(moves) > 1:
        lines.append(f"　运镜关键帧：{len(moves)} 个（首 {moves[0][:4]} → 末 {moves[-1][:4]}）")
    for person in (shot.get("cast") or []):
        lines.append(
            f"　人物：{str(person.get('name') or '').strip()}"
            f"　位置({person.get('x')},{person.get('y')})"
            f"　朝向{person.get('rot')}　姿态{person.get('pose')}")
    note = str(shot.get("note") or "").strip()
    if note:
        lines.append(f"　这镜在演：{note}")
    if end:
        lines.append(f"　终点状态：{end}")
    return "\n".join(lines)


def export_notes(spec: Dict[str, Any], output_root: str, project_id: str) -> str:
    """把整份白膜规格导出成说明文件，返回路径。"""
    target = notes_path(output_root, project_id)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    parts: List[str] = ["# 白膜规格说明（机位 / 走位 / 节奏 —— 花钱前定死的东西）", ""]
    techviz = spec.get("techviz") or []
    if techviz:
        parts.append("## 几何体检（Techviz）")
        parts.extend(f"- {item}" for item in techviz)
        parts.append("")
    for i, shot in enumerate(spec.get("shots") or []):
        parts.append(build_shot_notes(shot, i))
        parts.append("")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts))
    return target
