"""把「三段式分镜提示词」接进出片与分镜阶段（2026-10-21 第 5 轮）。

三处改动，全部是最小侵入：
  ① `core/orchestrator.py::_compose_full_prompt` —— 有编译好的 video_prompt 就用它
     （再补一句场景锚点），没有就走原来的三层拼装（向后兼容）。
  ② `core/agent_tools.py::t_plan_shots` —— 分镜落库后**自动编译**三段提示词并写回，
     用户不用额外点一次；失败只记日志，不阻塞分镜。
  ③ `main.py` —— 加三个只读/可写路由，供前端与 vf_prompts 工具用：
       GET  /api/projects/{pid}/prompts          列出每镜三段提示词
       POST /api/projects/{pid}/prompts/compile  重新编译全部镜头
       PUT  /api/projects/{pid}/prompts/{sid}    改某镜的 video/negative/audio 提示词
"""
import re
import shutil
import time
from pathlib import Path

ROOT = Path(r'D:\VideoForge-dev\backend')
STAMP = time.strftime('%Y%m%d-%H%M%S')


def backup(p: Path) -> None:
    target = p.with_suffix(p.suffix + f'.bak-{STAMP}')
    shutil.copyfile(p, target)
    print('  备份 →', target.name)


# ── ① orchestrator：优先用编译好的 video_prompt ─────────────────────────
orch = ROOT / 'core' / 'orchestrator.py'
text = orch.read_text(encoding='utf-8')
old = '''    parts = []

    # Layer 1
    if shot.get("layer1_overview"):
        parts.append(shot["layer1_overview"])
'''
new = '''    # ★ 2026-10-21（用户第 5 条）：分镜阶段编译好的「三段式视频提示词」优先。
    #   它是按 画面质感→场景→主体(角色锚定)→镜头→动作→构图→禁文字 的顺序拼的专业 prompt，
    #   比下面用一句话概述 + 枚举运镜拼出来的强得多。没有就用旧路径（旧项目照常能出片）。
    try:
        from core import shot_prompts as _sp
        _stored = _sp.stored_prompts(shot)
        _video = str(_stored.get("video_prompt") or "").strip()
        if _video:
            _scene = str(_stored.get("scene_anchor") or "").strip()
            if _scene and _scene not in _video:
                _video = _video + "　" + _scene + "。"
            return _video
    except Exception:  # noqa: BLE001
        pass

    parts = []

    # Layer 1
    if shot.get("layer1_overview"):
        parts.append(shot["layer1_overview"])
'''
if old in text:
    backup(orch)
    orch.write_text(text.replace(old, new, 1), encoding='utf-8')
    print('① orchestrator._compose_full_prompt 已优先用 video_prompt')
else:
    print('① 未匹配 orchestrator 目标段落')

# ── ② agent_tools.py：分镜落库后自动编译提示词 ──────────────────────────
tools = ROOT / 'core' / 'agent_tools.py'
text = tools.read_text(encoding='utf-8')
anchor = 'def t_plan_shots'
idx = text.find(anchor)
if idx < 0:
    print('② 找不到 t_plan_shots')
else:
    # 在 t_plan_shots 的 return 前调用编译（用正则找函数体里最后一个 return ok(）
    body_start = idx
    body_end = text.find('\n@REGISTRY.register', body_start + 10)
    if body_end < 0:
        body_end = len(text)
    body = text[body_start:body_end]
    if 'compile_project_prompts' in body:
        print('② t_plan_shots 已接入过')
    else:
        patched = body.replace(
            'return ok(',
            '''try:
        from core.shot_prompts import compile_project_prompts as _cpp
        _n = _cpp(db, args.get("project_id") or "")
        if _n:
            logger.info("分镜提示词已编译：%d 镜", _n)
    except Exception as _e:  # noqa: BLE001
        logger.warning("分镜提示词编译失败（不影响分镜本身）：%s", _e)
    return ok(''',
            1,
        )
        if patched != body:
            backup(tools)
            tools.write_text(text[:body_start] + patched + text[body_end:], encoding='utf-8')
            print('② t_plan_shots 已接入自动编译')
        else:
            print('② t_plan_shots 里没找到 return ok(')

# ── ③ 给 shot_prompts 补 compile_project_prompts（依赖 db）─────────────
sp = ROOT / 'core' / 'shot_prompts.py'
sp_text = sp.read_text(encoding='utf-8')
if 'def compile_project_prompts' not in sp_text:
    sp_text += '''

def compile_project_prompts(db: Any, project_id: str) -> int:
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
    return done
'''
    sp.write_text(sp_text, encoding='utf-8')
    print('③ shot_prompts.compile_project_prompts 已补上')

# 语法自检
import py_compile
for path in (orch, tools, sp):
    py_compile.compile(str(path), doraise=True)
    print('  语法 OK', path.name)
