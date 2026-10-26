"""给 main.py 加三个提示词路由（在 `/api/projects/{pid}/shots` 之后插入）。

  GET  /api/projects/{pid}/prompts           每镜三段提示词 + 角色/场景锚定
  POST /api/projects/{pid}/prompts/compile   重新编译全部镜头（分镜改过之后用）
  PUT  /api/projects/{pid}/prompts/{sid}     改某镜的 video/negative/audio 提示词

缩进与邻居一致（app 内 4 空格，函数内 8 空格）。
"""
import shutil
import time
from pathlib import Path

p = Path(r'D:\VideoForge-dev\backend\main.py')
text = p.read_text(encoding='utf-8')
if '/prompts/compile' in text:
    print('已存在 prompts 路由，跳过')
    raise SystemExit(0)

anchor = '''    @app.get("/api/shots/{sid}")
    async def get_shot(sid: str):'''
block = '''    # ── 分镜提示词（三段式：video / negative / audio + 角色·场景锚定）──────
    # 设计稿来自 claude-opus-5 的协议（2026-10-22）；实现在 core/shot_prompts.py。
    @app.get("/api/projects/{pid}/prompts")
    async def list_shot_prompts(pid: str):
        """每镜三段提示词；没编译过的返回空，前端/工具可据此提示"先编译"。"""
        from core import shot_prompts as _sp
        shots = db.list_shots(pid) or []
        out = []
        for s in shots:
            prompts = _sp.stored_prompts(s)
            out.append({
                "shot_id": s.get("id"),
                "index": s.get("order_index"),
                "overview": s.get("layer1_overview") or "",
                "compiled": bool(prompts.get("video_prompt")),
                "video_prompt": prompts.get("video_prompt") or "",
                "negative_prompt": prompts.get("negative_prompt") or "",
                "audio_prompt": prompts.get("audio_prompt") or "",
                "character_anchor": prompts.get("character_anchor") or "",
                "scene_anchor": prompts.get("scene_anchor") or "",
            })
        return success({"shots": out, "count": len(out)})

    @app.post("/api/projects/{pid}/prompts/compile")
    async def compile_shot_prompts_route(pid: str):
        """重新编译整个项目的分镜提示词（改了剧本/人设/场景之后调用）。"""
        from core.shot_prompts import compile_project_prompts
        n = compile_project_prompts(db, pid)
        return success({"compiled": n})

    @app.put("/api/projects/{pid}/prompts/{sid}")
    async def update_shot_prompt(pid: str, sid: str, payload: dict = None):
        """改某一镜的提示词：只接受 video_prompt / negative_prompt / audio_prompt 三个键。

        ★ 允许用户直接改，是因为这几句直接决定出片效果；改完不用重编译，
          下一次生成就会用新的（`_compose_full_prompt` 优先读这里）。
        """
        import json as _json
        from core import shot_prompts as _sp
        shot = db.get_shot(sid)
        if not shot or str(shot.get("project_id")) != str(pid):
            return error("找不到这个镜头", 404)
        payload = payload or {}
        allowed = ("video_prompt", "negative_prompt", "audio_prompt")
        changed = {k: str(payload[k]).strip() for k in allowed if isinstance(payload.get(k), str) and str(payload[k]).strip()}
        if not changed:
            return error("没有可改的字段（只接受 video_prompt / negative_prompt / audio_prompt）", 400)
        layer3 = _sp.attach_prompts(shot, changed)
        db.update_shot(sid, layer3_constraints=_json.dumps(layer3, ensure_ascii=False))
        return success({"shot_id": sid, "updated": sorted(changed.keys()), "next": "下一次出片就会用这段提示词"})

'''

if anchor in text:
    shutil.copyfile(p, p.with_suffix(p.suffix + '.bak-' + time.strftime('%Y%m%d-%H%M%S')))
    p.write_text(text.replace(anchor, block + anchor, 1), encoding='utf-8')
    print('已插入 3 个 prompts 路由')
else:
    print('未找到锚点 /api/shots/{sid}')
    raise SystemExit(2)

import py_compile
py_compile.compile(str(p), doraise=True)
print('语法 OK', p.stat().st_size, 'B')
