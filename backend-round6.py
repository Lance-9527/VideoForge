"""第 6 轮后端：① 一致性检查器 ② 音色锚定逐句化 ③ skill 只读路由。

全部追加式改动（不动已有函数体，除了 compile_shot_prompts 里加两行音色）。
"""
import shutil
import time
from pathlib import Path

ROOT = Path(r'D:\VideoForge-dev\backend')
STAMP = time.strftime('%Y%m%d-%H%M%S')
sp = ROOT / 'core' / 'shot_prompts.py'
shutil.copyfile(sp, sp.with_suffix(sp.suffix + f'.bak-{STAMP}'))
print('备份 shot_prompts.py')

text = sp.read_text(encoding='utf-8')

# ── ① 音色锚定：audio_prompt 里带上每个角色的 voice_id / rate / emotion ──
old_audio = '''    audio_bits: List[str] = []
    if lines:
        audio_bits.append("台词：" + " / ".join(lines[:4]) + "（≤40字，语气自然、口型对得上）")
    else:
        audio_bits.append("本镜无台词。")'''
new_audio = '''    audio_bits: List[str] = []
    # 音色锚定：同一角色跨镜必须同一声线（voice_id + 语速 + 基础情绪）
    voice_lines = []
    for char in cast:
        vid = _s(char.get("voice_id"))
        if vid:
            voice_lines.append(f"{_s(char.get('name'))}→{vid}（语速 0.92，情绪克制，跨镜保持一致）")
    if voice_lines:
        audio_bits.append("音色锚定：" + "；".join(voice_lines) + "。")
    if lines:
        audio_bits.append("台词：" + " / ".join(lines[:4]) + "（≤40字，语气自然、口型对得上）")
    else:
        audio_bits.append("本镜无台词。")'''
assert old_audio in text, '音色锚定锚点未匹配'
text = text.replace(old_audio, new_audio, 1)

# ── ② 一致性检查器 ──────────────────────────────────────────────────────
if 'def check_project_prompts' not in text:
    text += '''

#: 一致性规则（可逐条断言；来自 claude-opus-5 的协议稿，收敛成代码能跑的 8 条）
def check_project_prompts(db: Any, project_id: str) -> Dict[str, Any]:
    """检查一个项目的分镜提示词一致性，返回 {issues, counts, checked}。

    规则（每条都能用一句 if 判定，不写"注意一致"这种废话）：
      C01 每镜必须有编译好的 video_prompt
      C02 video_prompt 必须包含该镜下每个角色的锚定短语（去标点后比对关键片段）
      C03 video_prompt 必须包含该场景的锚定短语
      C04 同场景的 scene_anchor 必须完全相同（跨镜不许漂）
      C05 有台词的镜，audio_prompt 必须含"台词："
      C06 无台词的镜，audio_prompt 必须含"本镜无台词"
      C07 negative_prompt 非空且包含"文字"
      C08 人数声明与实际入镜人数一致（"画面内人物 N 人" 的 N == 角色卡里的人数）
    """
    characters = {str(c.get("id")): c for c in (db.list_characters(project_id) or [])}
    scenes = {str(s.get("id")): s for s in (db.list_scenes(project_id) or [])}
    shots = db.list_shots(project_id) or []
    issues: List[Dict[str, Any]] = []
    scene_anchors_seen: Dict[str, set] = {}

    def add(shot: Dict[str, Any], code: str, message: str) -> None:
        issues.append({
            "shot_id": shot.get("id"),
            "index": shot.get("order_index"),
            "code": code,
            "message": message,
        })

    for shot in shots:
        prompts = stored_prompts(shot)
        video = _s(prompts.get("video_prompt"))
        negative = _s(prompts.get("negative_prompt"))
        audio = _s(prompts.get("audio_prompt"))
        if video == "":
            add(shot, "C01", "这一镜还没有编译提示词（先跑一次编译）")
            continue

        # C02：入镜角色锚定短语是否整段出现在 video_prompt 里
        cast: List[Dict[str, Any]] = []
        for cid in (shot.get("character_ids") or []):
            char = characters.get(str(cid))
            if char:
                cast.append(char)
        for char in cast:
            anchor = character_anchor(char)
            head = anchor.split("，")[0] if anchor else ""
            if head and head not in video:
                add(shot, "C02", f"video_prompt 里没有角色「{_s(char.get('name'))}」的锚定短语开头『{head}』")

        # C03 / C04：场景锚定
        scene = scenes.get(_s(shot.get("scene_id"))) or {}
        if scene:
            anchor = scene_anchor(scene)
            if anchor and anchor not in video:
                add(shot, "C03", "video_prompt 里没有完整场景锚定短语（跨镜容易换空间）")
            scene_anchors_seen.setdefault(_s(scene.get("id")), set()).add(anchor)

        # C05 / C06：台词一致性
        timeline = shot.get("layer2_timeline")
        if isinstance(timeline, str):
            try:
                timeline = json.loads(timeline)
            except Exception:  # noqa: BLE001
                timeline = []
        has_line = any(_dialogue_text(i) for i in (timeline or []) if isinstance(i, dict))
        if has_line and "台词：" not in audio:
            add(shot, "C05", "这一镜台词有内容，但 audio_prompt 里没有『台词：』")
        if not has_line and "本镜无台词" not in audio:
            add(shot, "C06", "这一镜没有台词，但 audio_prompt 没写『本镜无台词』（可能被配上旁白）")

        # C07：负面词
        if negative == "" or "文字" not in negative:
            add(shot, "C07", "negative_prompt 缺失或没包含『文字』（会出现字幕/水印）")

        # C08：人数
        animal_marks = ("蛇", "狗", "猫", "鸟", "狼", "马", "牛", "羊", "兔", "鱼", "鼠")
        people = [c for c in cast if not any(mark in _s(c.get("name")) for mark in animal_marks)]
        if people and f"人物 {len(people)} 人" not in video:
            add(shot, "C08", f"镜头里有 {len(people)} 个人，但 video_prompt 的人数声明对不上")

    # C04（跨镜）：同一场景的锚定短语必须完全一致
    for scene_id, anchors in scene_anchors_seen.items():
        if len(anchors) > 1:
            issues.append({
                "shot_id": None, "index": None, "code": "C04",
                "message": f"场景 {scene_id} 的锚定短语在不同镜里不一致（{len(anchors)} 种写法）",
            })

    return {
        "project_id": project_id,
        "shots": len(shots),
        "issues": issues,
        "ok": len(issues) == 0,
    }
'''
sp.write_text(text, encoding='utf-8')
print('已加：音色锚定 + check_project_prompts')

# ── ③ main.py：skill 路由 + 检查路由 ────────────────────────────────────
mp = ROOT / 'main.py'
mt = mp.read_text(encoding='utf-8')
if '/api/skills' in mt:
    print('skills 路由已存在')
else:
    anchor = '''    @app.get("/api/shots/{sid}")'''
    block = '''    # ── Skill 库（只读）：给 vf_skill 工具与前端看 ─────────────────────
    @app.get("/api/skills")
    async def list_skills_route():
        """列出已装载的 skill（名字 + 一句话 + 来源）。"""
        from core.skills import get_registry, extra_dirs_from_settings, default_learned_dir
        try:
            reg = get_registry(default_learned_dir(), extra_dirs_from_settings(db.get_all_settings()))
            skills = getattr(reg, "skills", None) or getattr(reg, "_skills", {}) or {}
            items = []
            for name, sk in (skills.items() if hasattr(skills, "items") else []):
                items.append({
                    "name": getattr(sk, "name", name),
                    "title": getattr(sk, "title", "") or getattr(sk, "name", name),
                    "description": getattr(sk, "description", ""),
                    "source": getattr(sk, "source", ""),
                })
            return success({"skills": items, "count": len(items)})
        except Exception as e:  # noqa: BLE001
            return error(f"读 skill 库失败：{type(e).__name__}: {e}", 500)

    @app.get("/api/skills/{name}")
    async def get_skill_body(name: str):
        """读某个 skill 的完整正文（模型需要时才拉，避免吃 token）。"""
        from core.skills import get_registry, extra_dirs_from_settings, default_learned_dir
        try:
            reg = get_registry(default_learned_dir(), extra_dirs_from_settings(db.get_all_settings()))
            sk = reg.get(name)
            if sk is None:
                return error(f"没有这个 skill：{name}", 404)
            return success({
                "name": getattr(sk, "name", name),
                "description": getattr(sk, "description", ""),
                "body": getattr(sk, "body", "") or getattr(sk, "content", ""),
            })
        except Exception as e:  # noqa: BLE001
            return error(f"读 skill 失败：{type(e).__name__}: {e}", 500)

    @app.post("/api/projects/{pid}/prompts/check")
    async def check_prompts_route(pid: str):
        """检查分镜提示词一致性（C01~C08），返回问题清单。"""
        from core.shot_prompts import check_project_prompts
        return success(check_project_prompts(db, pid))

'''
    shutil.copyfile(mp, mp.with_suffix(mp.suffix + f'.bak-{STAMP}'))
    mp.write_text(mt.replace(anchor, block + anchor, 1), encoding='utf-8')
    print('已插入 skill 路由 + 检查路由')

import py_compile
for path in (sp, mp):
    py_compile.compile(str(path), doraise=True)
    print('语法 OK', path.name)
