"""一致性检查器要与编译逻辑同一套「声音角色」分流，否则会误报。

C02（画面必须含角色锚定）与 C08（人数声明）都只对**出镜角色**生效；
声音角色（旁白/画外音）改查：audio_prompt 里必须有它的音色锚定。
"""
import shutil
import time
from pathlib import Path

STAMP = time.strftime('%Y%m%d-%H%M%S')
sp = Path(r'D:\VideoForge-dev\backend\core\shot_prompts.py')
text = sp.read_text(encoding='utf-8')

old = """        # C02：入镜角色锚定短语是否整段出现在 video_prompt 里
        cast: List[Dict[str, Any]] = []
        for cid in (shot.get("character_ids") or []):
            char = characters.get(str(cid))
            if char:
                cast.append(char)
        for char in cast:"""
new = """        # C02：入镜角色锚定短语是否整段出现在 video_prompt 里
        # ★ 只对**出镜角色**生效：旁白/画外音是"只出声不出镜"，
        #   它的锚定短语**不该**出现在画面提示词里（否则模型会画出一个人）。
        cast: List[Dict[str, Any]] = []
        voice_only_chars: List[Dict[str, Any]] = []
        for cid in (shot.get("character_ids") or []):
            char = characters.get(str(cid))
            if not char:
                continue
            if is_voice_only(char):
                voice_only_chars.append(char)
            else:
                cast.append(char)
        for char in voice_only_chars:
            # 声音角色：音色锚定必须出现在 audio_prompt 里（跨镜同一声线）
            vid = _s(char.get("voice_id"))
            if vid and vid not in audio:
                add(shot, "C09", f"声音角色「{_s(char.get('name'))}」的音色 {vid} 没写进 audio_prompt")
        for char in cast:"""
if old in text:
    shutil.copyfile(sp, sp.with_suffix(sp.suffix + '.bak-' + STAMP))
    text = text.replace(old, new, 1)

    # C08：人数只算出镜角色（声音角色不算人）
    old_c08 = """        people = [c for c in cast if not any(mark in _s(c.get("name")) for mark in animal_marks)]
        if people and f"人物 {len(people)} 人" not in video:
            add(shot, "C08", f"镜头里有 {len(people)} 个人，但 video_prompt 的人数声明对不上")"""
    new_c08 = """        people = [c for c in cast if not any(mark in _s(c.get("name")) for mark in animal_marks)]
        if people and f"人物 {len(people)} 人" not in video:
            add(shot, "C08", f"镜头里有 {len(people)} 个人，但 video_prompt 的人数声明对不上")
        if not people and "没有出镜人物" not in video and "画面内人物" not in video:
            add(shot, "C08", "这一镜没有出镜人物，但 video_prompt 没写明（模型可能自己加人）")"""
    if old_c08 in text:
        text = text.replace(old_c08, new_c08, 1)
        print('C08 已按出镜角色重算')
    else:
        print('C08 未匹配')

    sp.write_text(text, encoding='utf-8')
    print('检查器已与编译逻辑对齐')
else:
    print('未匹配 C02 段')

import py_compile
py_compile.compile(str(sp), doraise=True)
print('语法 OK')

# 同步载荷
import hashlib, shutil as sh
payload = Path(r'D:\VideoForge-dsh\deepseek-harness-master\apps\desktop\resources\videoforge-sidecar\_internal\backend\core\shot_prompts.py')
sh.copyfile(sp, payload)
print('载荷已同步 ->', hashlib.md5(payload.read_bytes()).hexdigest()[:8])
