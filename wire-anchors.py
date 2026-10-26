"""把场景锚点表接进提示词编译与一致性检查。

改动（core/shot_prompts.py）：
  1. compile_shot_prompts 增加 anchor_sentence / forbidden_note 两个入参
     → 锚点句进 video_prompt，禁止句进 negative_prompt
  2. compile_project_prompts 变成 async：先按场景生成锚点表（一次 LLM 调用，有缓存），
     再逐镜编译
  3. check_project_prompts 增加 C10（必须含锚点句）与 C11（不得出现与锚点相反的方位）
"""
import shutil
import time
from pathlib import Path

STAMP = time.strftime('%Y%m%d-%H%M%S')
sp = Path(r'D:\VideoForge-dev\backend\core\shot_prompts.py')
text = sp.read_text(encoding='utf-8')
shutil.copyfile(sp, sp.with_suffix(sp.suffix + '.bak-' + STAMP))

# ① compile_shot_prompts 签名与注入
old_sig = """def compile_shot_prompts(
    shot: Dict[str, Any],
    characters: Dict[str, Dict[str, Any]],
    scenes: Dict[str, Dict[str, Any]],
    *,
    style_base: str = STYLE_BASE,
) -> Dict[str, Any]:"""
new_sig = """def compile_shot_prompts(
    shot: Dict[str, Any],
    characters: Dict[str, Dict[str, Any]],
    scenes: Dict[str, Dict[str, Any]],
    *,
    style_base: str = STYLE_BASE,
    anchor_sentence: str = "",
    forbidden_note: str = "",
) -> Dict[str, Any]:"""
if old_sig in text:
    text = text.replace(old_sig, new_sig, 1)
    print('① 签名已加锚点入参')
else:
    print('① 未匹配签名')

old_inject = """    parts.append("画面无任何文字、字幕、水印。")
    video_prompt = "".join(parts)"""
new_inject = """    parts.append("画面无任何文字、字幕、水印。")
    # ★ 场景锚点句（跨镜连贯）：同一场景的每一镜都原样注入同一段空间关系。
    if anchor_sentence:
        parts.append(anchor_sentence)
    video_prompt = "".join(parts)"""
if old_inject in text:
    text = text.replace(old_inject, new_inject, 1)
    print('① 锚点句已注入 video_prompt')
else:
    print('① 未匹配注入点')

old_neg = """    return {
        "video_prompt": video_prompt,
        "negative_prompt": NEGATIVE_BASE,"""
new_neg = """    negative_prompt = NEGATIVE_BASE + (forbidden_note or "")
    return {
        "video_prompt": video_prompt,
        "negative_prompt": negative_prompt,"""
if old_neg in text:
    text = text.replace(old_neg, new_neg, 1)
    print('① 禁止句已并入 negative_prompt')
else:
    print('① 未匹配 negative')

# ② compile_project_prompts → async + 先建锚点表
old_cpp = '''def compile_project_prompts(db: Any, project_id: str) -> int:
    """把一个项目的所有镜头重新编译三段提示词并写回。返回编译成功的镜数。"""
    if not project_id:
        return 0
    characters = {str(c.get("id")): c for c in (db.list_characters(project_id) or [])}
    scenes = {str(s.get("id")): s for s in (db.list_scenes(project_id) or [])}
    shots = db.list_shots(project_id) or []
    done = 0
    for shot in shots:
        try:
            compiled = compile_shot_prompts(shot, characters, scenes)
            db.update_shot(shot["id"], layer3_constraints=json.dumps(
                attach_prompts(shot, compiled), ensure_ascii=False))
            done += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("镜头 %s 提示词编译失败：%s", shot.get("id"), e)
    return done'''
new_cpp = '''async def compile_project_prompts(
    db: Any,
    project_id: str,
    settings: Optional[Dict[str, Any]] = None,
    output_dir: str = "",
) -> int:
    """把一个项目的所有镜头重新编译三段提示词并写回。返回编译成功的镜数。

    ★ 2026-10-22：先按场景生成**空间锚点表**（一次 LLM 调用，缓存复用），
      再逐镜编译 —— 同一场景的每一镜注入**同一段**锚点句 ⇒ 跨镜不穿帮。
    """
    if not project_id:
        return 0
    characters = {str(c.get("id")): c for c in (db.list_characters(project_id) or [])}
    scenes = {str(s.get("id")): s for s in (db.list_scenes(project_id) or [])}
    shots = db.list_shots(project_id) or []
    settings = settings or {}
    output_dir = output_dir or str(settings.get("output_dir")
                                   or os.path.join(os.getcwd(), "data", "outputs"))

    # ① 每个场景一张锚点表
    anchors_by_scene: Dict[str, Dict[str, Any]] = {}
    try:
        from core import scene_anchors as _sa
        by_scene: Dict[str, List[Dict[str, Any]]] = {}
        for s in shots:
            by_scene.setdefault(str(s.get("scene_id") or ""), []).append(s)
        for scene_id, scene_shots in by_scene.items():
            if not scene_id or scene_id not in scenes:
                continue
            try:
                anchors_by_scene[scene_id] = await _sa.build_scene_anchors(
                    db, scenes[scene_id], scene_shots, settings, output_dir=output_dir)
            except Exception as e:  # noqa: BLE001
                logger.warning("场景 %s 锚点表失败（该场景不注入锚点）：%s", scene_id, e)
    except Exception as e:  # noqa: BLE001
        logger.warning("锚点表环节整体失败（不影响编译）：%s", e)

    # ② 逐镜编译（带锚点句）
    done = 0
    for shot in shots:
        try:
            scene_id = str(shot.get("scene_id") or "")
            anchors = anchors_by_scene.get(scene_id) or {}
            anchor_sentence = ""
            forbidden_note = ""
            if anchors:
                try:
                    from core import scene_anchors as _sa
                    anchor_sentence = _sa.render_anchor_sentence(anchors)
                    forbidden_note = _sa.render_forbidden_note(anchors)
                except Exception:  # noqa: BLE001
                    pass
            compiled = compile_shot_prompts(
                shot, characters, scenes,
                anchor_sentence=anchor_sentence,
                forbidden_note=forbidden_note)
            db.update_shot(shot["id"], layer3_constraints=json.dumps(
                attach_prompts(shot, compiled), ensure_ascii=False))
            done += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("镜头 %s 提示词编译失败：%s", shot.get("id"), e)
    return done'''
if old_cpp in text:
    text = text.replace(old_cpp, new_cpp, 1)
    print('② compile_project_prompts 已改 async + 锚点表')
else:
    print('② 未匹配 compile_project_prompts')

# 需要 os 导入
if '\nimport os\n' not in text:
    text = text.replace('import json\nimport logging', 'import json\nimport logging\nimport os', 1)
    print('② 补 os 导入')

# ③ 检查器：C10 / C11
old_check = """    return {
        "project_id": project_id,
        "shots": len(shots),
        "issues": issues,
        "ok": len(issues) == 0,
    }"""
new_check = """    # C10 / C11：跨镜连贯 —— 必须含锚点句，且不得出现与锚点相反的方位
    try:
        from core import scene_anchors as _sa
        _out = str((db.get_all_settings() or {}).get("output_dir")
                   or os.path.join(os.getcwd(), "data", "outputs"))
        for shot in shots:
            _scene_id = str(shot.get("scene_id") or "")
            if not _scene_id:
                continue
            _anchors = _sa.load_anchors(_out, project_id, _scene_id)
            if not _anchors:
                continue
            _video = _s(stored_prompts(shot).get("video_prompt"))
            if _video == "":
                continue
            _sentence = _sa.render_anchor_sentence(_anchors)
            if _sentence and _sentence[:24] not in _video:
                add(shot, "C10", "这一镜的 video_prompt 没有该场景的空间锚点句（跨镜会穿帮）")
            for _c in _sa.find_conflicts(_anchors, _video):
                add(shot, "C11", _c)
    except Exception as e:  # noqa: BLE001
        logger.warning("锚点检查跳过：%s", e)

    return {
        "project_id": project_id,
        "shots": len(shots),
        "issues": issues,
        "ok": len(issues) == 0,
    }"""
if old_check in text:
    text = text.replace(old_check, new_check, 1)
    print('③ 检查器已加 C10/C11')
else:
    print('③ 未匹配检查器返回')

sp.write_text(text, encoding='utf-8')
import py_compile
py_compile.compile(str(sp), doraise=True)
print('语法 OK')
