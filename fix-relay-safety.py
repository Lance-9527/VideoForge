"""接龙体系的安全带修三处：
① 尾帧持久化：平台给的 last_frame_url **不落地**（只有平台没给才本地抽帧），
   所以上一镜的尾帧在本地找不到 ⇒ 接龙拿不到首帧。改成把尾帧（URL 或本地路径）
   写进该镜的提示词载荷（prompts.last_frame），接龙时直接取。
② 首帧冲突预检判据太粗：把"从当前姿势继续"（蹲→捧、坐→站）误判成冲突。
   改成只判**物理上不可能**的情况（首帧里没有的道具/方向/位置被要求出现等）。
③ 接龙镜的动作提示词前加「从当前姿势开始」（规格给的缓解"首帧绑架"的做法）。
"""
import shutil
import time
from pathlib import Path

STAMP = time.strftime('%Y%m%d-%H%M%S')
ROOT = Path(r'D:\VideoForge-dev\backend\core')

# ── ① longvideo：生成成功后持久化尾帧 ──────────────────────────────────
lv = ROOT / 'longvideo.py'
text = lv.read_text(encoding='utf-8')
shutil.copyfile(lv, lv.with_suffix(lv.suffix + '.bak-' + STAMP))

old = '''    ok_count = [s for s in segments if s.get("status") == "ok"]'''
new = '''    # ★ 2026-10-22：把**最后一段的尾帧**写进本镜的提示词载荷（prompts.last_frame）。
    #   为什么必须有：平台的 last_frame_url **不落地**（只有平台没给时才本地抽帧），
    #   于是"上一镜尾帧"在本地根本找不到 ⇒ 跨镜接龙拿不到首帧，接龙永远触发不了。
    #   写 URL 或本地路径都行（平台吃 http(s) URL 与 base64 data URI）。
    try:
        _last = ""
        for _s in reversed(segments):
            if _s.get("status") == "ok" and _s.get("last_frame"):
                _last = str(_s["last_frame"])
                break
        if _last:
            from core import shot_prompts as _sp
            _prompts = _sp.stored_prompts(shot)
            if not isinstance(_prompts, dict):
                _prompts = {}
            _prompts["last_frame"] = _last
            db.update_shot(str(shot.get("id") or ""), layer3_constraints=json.dumps(
                _sp.attach_prompts(shot, _prompts), ensure_ascii=False))
    except Exception as _e:  # noqa: BLE001
        logger.warning("持久化尾帧失败（不影响本镜）：%s", _e)

    ok_count = [s for s in segments if s.get("status") == "ok"]'''
if old in text:
    text = text.replace(old, new, 1)
    print('① longvideo 已加尾帧持久化')
else:
    print('① 未匹配 ok_count 落点')

if '\nimport json\n' not in text:
    text = text.replace('import logging', 'import json\nimport logging', 1)
    print('① 补 json 导入')

lv.write_text(text, encoding='utf-8')

# ── ② shot_chain：找尾帧（优先 prompts.last_frame）─────────────────────
sc = ROOT / 'shot_chain.py'
text = sc.read_text(encoding='utf-8')
if 'def find_last_frame(' not in text:
    helper = '''

def find_last_frame(db: Any, project_id: str, shot_id: str, output_dir: str = "") -> str:
    """找某一镜的尾帧（跨镜接龙的第一帧来源）。

    优先级：
      ① 本镜提示词载荷里的 `last_frame`（可能是平台 URL，也可能是本地路径）
      ② 本地落盘的 `seg_*_last.jpg`（平台没给尾帧时我们自己抽的）
    都没有就返回空串 ⇒ 该镜退回"独立镜"。
    """
    if not shot_id:
        return ""
    try:
        from core import shot_prompts as _sp
        for shot in db.list_shots(project_id) or []:
            if str(shot.get("id")) != str(shot_id):
                continue
            stored = _sp.stored_prompts(shot)
            value = str(stored.get("last_frame") or "").strip()
            if value:
                return value
            break
    except Exception:  # noqa: BLE001
        pass
    if output_dir:
        return last_frame_path(output_dir, project_id, shot_id)
    return ""
'''
    text += helper
    print('② shot_chain.find_last_frame 已加')
else:
    print('② 已存在 find_last_frame')

# ── ③ 首帧冲突预检：判据改精确 ────────────────────────────────────────
old_prompt = '''        prompt = (
            "判断一段视频的首帧状态与后续动作是否冲突。\\n"
            f"首帧状态：{brief}\\n"
            f"要求动作：{action}\\n"
            "只回答一个词：自然 或 冲突。冲突的定义：动作无法从首帧状态自然开始"
            "（例如首帧是正面却要求背对镜头、首帧是坐姿却要求已经在跑）。"
        )'''
new_prompt = '''        prompt = (
            "判断一段视频的**首帧状态**与**后续动作**是否**物理上不可能**。\\n"
            f"首帧状态：{brief}\\n"
            f"要求动作：{action}\\n"
            "\\n"
            "规则（**只判物理矛盾，不判姿势不同**）：\\n"
            "· 允许：从当前姿势继续变化（蹲→捧起、坐→站、走→停、伸手→握住）、"
            "允许换机位与景别、允许时间微跳。\\n"
            "· 判为冲突：首帧里根本没有的东西却被要求出现（如首帧手里空着却要'
            '拿起杯子）、方向互斥（首帧背对镜头却要正对镜头说话）、"
            "位置互斥（首帧在门外却要求已在屋内完成动作）、"
            "首帧是倒地的却要求站起来跑。\\n"
            "\\n"
            "只回答一个词：自然 或 冲突。"
        )'''
if old_prompt in text:
    text = text.replace(old_prompt, new_prompt, 1)
    print('③ 预检判据已改精确')
else:
    print('③ 未匹配预检 prompt')

sc.write_text(text, encoding='utf-8')

# ── ④ 编译期：接龙用 find_last_frame + 动作加「从当前姿势开始」──────────
sp = ROOT / 'shot_prompts.py'
text = sp.read_text(encoding='utf-8')
old_find = """                _relay_first = ""
                try:
                    from core import shot_chain as _sc
                    _relay_first = _sc.last_frame_path(
                        output_dir, str(shot.get("project_id") or ""),
                        str(_plan.get("source_shot_id") or ""))"""
new_find = """                _relay_first = ""
                try:
                    from core import shot_chain as _sc
                    _relay_first = _sc.find_last_frame(
                        db, str(shot.get("project_id") or ""),
                        str(_plan.get("source_shot_id") or ""), output_dir)"""
if old_find in text:
    text = text.replace(old_find, new_find, 1)
    print('④ 编译期改用 find_last_frame')
else:
    print('④ 未匹配编译期取帧')

old_brief = """                compiled["continuity_brief"] = _plan.get("brief", "")"""
new_brief = """                compiled["continuity_brief"] = _plan.get("brief", "")
                # ★ 缓解"首帧绑架"（强模型规格）：动作从**当前姿势**开始写清楚，
                #   模型就不会原地扭动。
                compiled["relay_action_hint"] = "从当前姿势继续，动作幅度小而连贯，不要重置人物位置" """
if old_brief in text:
    text = text.replace(old_brief, new_brief, 1)
    print('④ 接龙镜动作提示已加')
else:
    print('④ 未匹配 brief 写入')

sp.write_text(text, encoding='utf-8')

import py_compile
for name in ('longvideo.py', 'shot_chain.py', 'shot_prompts.py'):
    py_compile.compile(str(ROOT / name), doraise=True)
print('语法 OK')
