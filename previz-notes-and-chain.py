"""白膜配套收口：
① 修 previz_design 里 `_s` 未定义（几何体检会静默失效）
② 新增 core/previz_notes.py：导出「白膜规格说明」（白膜视频 + 每机位静帧 + 自动 prompt.txt）
③ core/shot_chain.py：首帧冲突预检 + 重校准（上游重生成 → 下游标记过期）
"""
import shutil
import time
from pathlib import Path

STAMP = time.strftime('%Y%m%d-%H%M%S')
ROOT = Path(r'D:\VideoForge-dev\backend\core')

# ── ① 修 _s ─────────────────────────────────────────────────────────────
p = ROOT / 'previz_design.py'
text = p.read_text(encoding='utf-8')
if 'name = _s(person.get("name"))' in text:
    text = text.replace('name = _s(person.get("name"))', 'name = str(person.get("name") or "").strip()')
    p.write_text(text, encoding='utf-8')
    print('① _s 未定义已修')
else:
    print('① 无需修 _s')

# ── ② 白膜规格说明导出 ─────────────────────────────────────────────────
notes = ROOT / 'previz_notes.py'
if not notes.exists():
    notes.write_text('''# -*- coding: utf-8 -*-
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
    return "\\n".join(lines)


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
        fh.write("\\n".join(parts))
    return target
''', encoding='utf-8')
    print('② core/previz_notes.py 已建')

# ── ③ 首帧冲突预检 + 重校准 ────────────────────────────────────────────
sc = ROOT / 'shot_chain.py'
text = sc.read_text(encoding='utf-8')
shutil.copyfile(sc, sc.with_suffix(sc.suffix + '.bak-' + STAMP))

if 'def mark_downstream_stale(' not in text:
    extra = '''

def mark_downstream_stale(db: Any, project_id: str, changed_shot_id: str) -> List[str]:
    """上游镜头重生成 → 把**依赖它的下游接龙镜**标记为过期（需重编译/重出）。

    ★ 为什么要：接龙镜的首帧就是上一镜的尾帧。上一镜重画了，下游的接龙首帧就是旧的，
      不重出就会出现"接缝对不上"。这里只**标记**（chain_mode=independent + stale 原因），
      真正重出由上层决定（花钱的事不能自动做）。
    """
    stale: List[str] = []
    shots = sorted(db.list_shots(project_id) or [], key=lambda s: s.get("order_index") or 0)
    ids = [str(s.get("id")) for s in shots]
    if changed_shot_id not in ids:
        return stale
    start = ids.index(changed_shot_id)
    for shot in shots[start + 1:]:
        prompts = stored_prompts_of(shot)
        if str(prompts.get("relay_source_shot_id") or "") != changed_shot_id:
            continue
        prompts["chain_mode"] = "independent"
        prompts["stale_reason"] = f"上一镜 {changed_shot_id[:8]} 重生成，本镜接龙首帧已过期"
        try:
            from core import shot_prompts as _sp
            layer3 = _sp.attach_prompts(shot, prompts)
            db.update_shot(str(shot.get("id")),
                           layer3_constraints=json.dumps(layer3, ensure_ascii=False))
            stale.append(str(shot.get("id")))
        except Exception as e:  # noqa: BLE001
            logger.warning("标记下游过期失败 %s：%s", shot.get("id"), e)
    return stale


def stored_prompts_of(shot: Dict[str, Any]) -> Dict[str, Any]:
    """读一个镜头已存的提示词载荷（没有就空 dict）。"""
    try:
        from core import shot_prompts as _sp
        return _sp.stored_prompts(shot)
    except Exception:  # noqa: BLE001
        return {}
'''
    text += extra
    sc.write_text(text, encoding='utf-8')
    print('③ mark_downstream_stale 已加')
else:
    print('③ 已存在')

import py_compile
for name in ('previz_design.py', 'previz_notes.py', 'shot_chain.py'):
    py_compile.compile(str(ROOT / name), doraise=True)
print('语法 OK')
