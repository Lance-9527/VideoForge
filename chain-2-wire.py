"""接龙第 2 步（修正版）：编译期判定 + 出片优先用接龙首帧。

要点：
  · 上一镜的尾帧本来就会落盘（<output_root>/<pid>/<shot_id>/seg_XX_last.jpg），
    所以编译期直接 glob 找上一镜目录里序号最大的那张即可，**不需要新增保存逻辑**。
  · 接龙时：first_frame = 上一镜尾帧，且**清空参考图**（首帧与参考图互斥）。
  · 关键帧仍然用于"独立镜"（换机位/跨场景/时间跳跃/连续接龙满 2 镜后重校准）。
"""
import shutil
import time
from pathlib import Path

STAMP = time.strftime('%Y%m%d-%H%M%S')
ROOT = Path(r'D:\VideoForge-dev\backend\core')

# ── 0) shot_chain：把"找上一镜尾帧"改成 glob ──────────────────────────
sc = ROOT / 'shot_chain.py'
text = sc.read_text(encoding='utf-8')
old = '''def last_frame_path(output_dir: str, project_id: str, shot_id: str) -> str:
    """上一镜尾帧的固定存放位置。"""
    return str(Path(output_dir) / str(project_id) / "lastframe" / f"{shot_id}.jpg")'''
new = '''def last_frame_path(output_dir: str, project_id: str, shot_id: str) -> str:
    """找某一镜的**最后一段尾帧**（生成本来就会存 seg_XX_last.jpg）。

    ★ 不用新增保存逻辑：分段生成时每段都会落一张 `seg_XX_last.jpg`，
      这里按序号取最大的那张即可（没有就返回空串 → 该镜退回独立镜）。
    """
    if not shot_id:
        return ""
    folder = Path(output_dir) / str(project_id) / str(shot_id)
    if not folder.is_dir():
        return ""
    frames = sorted(folder.glob("seg_*_last.jpg"))
    if not frames:
        return ""
    def _idx(p: Path) -> int:
        m = re.search(r"seg_(\\d+)_last", p.name)
        return int(m.group(1)) if m else 0
    return str(sorted(frames, key=_idx)[-1])'''
if old in text:
    sc.write_text(text.replace(old, new, 1), encoding='utf-8')
    print('0) shot_chain.last_frame_path 改为 glob 找尾帧')
else:
    print('0) 未匹配 last_frame_path')

# ── 1) shot_prompts：编译期接龙判定 ───────────────────────────────────
sp = ROOT / 'shot_prompts.py'
text = sp.read_text(encoding='utf-8')
shutil.copyfile(sp, sp.with_suffix(sp.suffix + '.bak-' + STAMP))

old_loop = '    # ② 逐镜编译（带锚点句）\n    done = 0\n    for shot in shots:'
new_loop = '''    # ② 逐镜编译（带锚点句 + 跨镜接龙判定）
    #
    # ★ 2026-10-22（按强模型规格）：接龙只在「同场景 + 同景别 + 无时间跳跃」时用；
    #   连续接龙满 2 镜后**强制回到独立镜**（重校准），防角色漂移。
    relay_plan: Dict[str, Dict[str, Any]] = {}
    try:
        from core import shot_chain as _sc
        _ordered = sorted(shots, key=lambda s: (s.get("order_index") or 0))
        _chain_len = 0
        for _i, _s in enumerate(_ordered):
            _prev = _ordered[_i - 1] if _i > 0 else None
            _sid = str(_s.get("id") or "")
            _decision = _sc.can_relay(_prev, _s)
            if not _decision.get("relay"):
                _chain_len = 0
                relay_plan[_sid] = {"relay": False, "reason": _decision.get("reason", "")}
                continue
            if _chain_len >= 2:
                _chain_len = 0
                relay_plan[_sid] = {"relay": False, "reason": "已连续接龙 2 镜，本镜强制重校准（回自己的关键帧 + 参考图）"}
                continue
            _chain_len += 1
            relay_plan[_sid] = {
                "relay": True,
                "reason": _decision.get("reason", ""),
                "brief": _sc.continuity_brief(_prev),
                "source_shot_id": str(_prev.get("id") or ""),
            }
    except Exception as e:  # noqa: BLE001
        logger.warning("接龙判定失败（全部按独立镜）：%s", e)
        relay_plan = {}

    done = 0
    for shot in shots:'''
if old_loop in text:
    text = text.replace(old_loop, new_loop, 1)
    print('1) 编译期接龙判定已插入')
else:
    print('1) 未匹配编译循环')

old_call = '''            compiled = compile_shot_prompts(
                shot, characters, scenes,
                anchor_sentence=anchor_sentence,
                forbidden_note=forbidden_note)'''
new_call = '''            compiled = compile_shot_prompts(
                shot, characters, scenes,
                anchor_sentence=anchor_sentence,
                forbidden_note=forbidden_note)
            # ★ 接龙信息写进提示词载荷：出片时据此取上一镜尾帧当首帧。
            _plan = relay_plan.get(str(shot.get("id") or "")) or {}
            compiled["chain_mode"] = "relay" if _plan.get("relay") else "independent"
            if _plan.get("relay"):
                compiled["relay_source_shot_id"] = _plan.get("source_shot_id", "")
                compiled["continuity_brief"] = _plan.get("brief", "")
                _relay_first = ""
                try:
                    from core import shot_chain as _sc
                    _relay_first = _sc.last_frame_path(
                        output_dir, str(shot.get("project_id") or ""),
                        str(_plan.get("source_shot_id") or ""))
                except Exception:  # noqa: BLE001
                    _relay_first = ""
                if _relay_first:
                    compiled["relay_first_frame"] = _relay_first
                else:
                    compiled["chain_mode"] = "independent"
                    compiled["chain_note"] = "上一镜尾帧还没生成，本镜先独立出片"'''
if old_call in text:
    text = text.replace(old_call, new_call, 1)
    print('1) 编译写回 chain_mode / relay_first_frame / continuity_brief')
else:
    print('1) 未匹配编译调用')

sp.write_text(text, encoding='utf-8')
import py_compile
py_compile.compile(str(sp), doraise=True)
py_compile.compile(str(sc), doraise=True)
print('1) 语法 OK')

# ── 2) longvideo：出片优先用接龙首帧 ───────────────────────────────────
lv = ROOT / 'longvideo.py'
text = lv.read_text(encoding='utf-8')
shutil.copyfile(lv, lv.with_suffix(lv.suffix + '.bak-' + STAMP))

old_kw = '        if keyframe_file:'
new_kw = '''        _relay_first = ""
        try:
            from core import shot_prompts as _sp
            _pr = _sp.stored_prompts(shot)
            if str(_pr.get("chain_mode") or "") == "relay" and _pr.get("relay_first_frame"):
                _relay_first = str(_pr["relay_first_frame"])
        except Exception:  # noqa: BLE001
            _relay_first = ""
        if _relay_first:
            # ★ 跨镜接龙：上一镜尾帧当这一镜首帧 ⇒ 像素级连续（位置不会跳）。
            #   代价：首帧与参考图互斥，所以这一镜**不能**带角色/场景参考图，
            #   身份靠提示词里的角色锚定句 + 负面词锁。
            try:
                kw["first_frame"] = to_image_ref(_relay_first)
                kw["character_reference"] = []
                previz_info.setdefault("applied", {})["relay_first_frame"] = True
                previz_info.setdefault("warnings", []).append(
                    "本镜与上一镜**像素级接龙**（上一镜尾帧当首帧，位置不会跳）。")
            except Exception as _e:  # noqa: BLE001
                logger.warning("接龙首帧转 URL 失败（按独立镜继续）：%s", _e)
        elif keyframe_file:'''
if old_kw in text:
    text = text.replace(old_kw, new_kw, 1)
    print('2) 出片优先用接龙首帧已插入')
else:
    print('2) 未匹配 keyframe 分支')

lv.write_text(text, encoding='utf-8')
py_compile.compile(str(lv), doraise=True)
print('2) 语法 OK')
