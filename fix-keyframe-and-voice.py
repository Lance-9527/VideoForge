"""修三处（真机暴露的）：

① `core/keyframe.py`：图像 provider 的 key 可能在 api_keys / llm_api_keys / tts_api_keys
   三个桶的任何一个（后端 main.py 就是三桶都查的），我们原来只查 api_keys → 误报"没配 Key"。
② `core/shot_prompts.py`：**声音角色**（旁白/画外音/不出镜）不该进画面主体，也不该算人数；
   它只进 audio_prompt 的音色锚定。
③ `core/shot_prompts.py`：场景锚定里 "夜晚光" + "夜晚" 重复，去重。
"""
import shutil
import time
from pathlib import Path

STAMP = time.strftime('%Y%m%d-%H%M%S')
ROOT = Path(r'D:\VideoForge-dev\backend\core')

# ── ① keyframe.py：三桶查 key ───────────────────────────────────────────
kf = ROOT / 'keyframe.py'
text = kf.read_text(encoding='utf-8')
old = """    keys = settings.get("api_keys") or {}
    api_key = str(keys.get(provider) or "")"""
new = """    # ★ key 可能在三个桶里的任何一个（后端 main.py 也是三桶都查的）：
    #   api_keys（视频/图像）· llm_api_keys（文本）· tts_api_keys（语音/图像兼用）
    api_key = ""
    for bucket in ("api_keys", "llm_api_keys", "tts_api_keys"):
        api_key = str((settings.get(bucket) or {}).get(provider) or "")
        if api_key:
            break"""
if old in text:
    shutil.copyfile(kf, kf.with_suffix(kf.suffix + '.bak-' + STAMP))
    kf.write_text(text.replace(old, new, 1), encoding='utf-8')
    print('① keyframe.py 三桶查 key 已修')
else:
    print('① 未匹配 keyframe.py')

# ── ②③ shot_prompts.py：声音角色 + 场景锚定去重 ─────────────────────────
sp = ROOT / 'shot_prompts.py'
text = sp.read_text(encoding='utf-8')

# ② 声音角色识别
if 'VOICE_ONLY_MARKS' not in text:
    text = text.replace(
        'def _s(value: Any, default: str = "") -> str:',
        '''#: 「只出声不出镜」的角色标记（旁白/画外音/解说…）——
#:   这类角色**不进画面主体、不算人数**，只进音频提示词的音色锚定。
VOICE_ONLY_MARKS = ("旁白", "画外", "解说", "旁述", "声音角色", "不出镜", "无出镜", "只闻其声")


def is_voice_only(char: Dict[str, Any]) -> bool:
    """这个角色是不是「只出声不出镜」？"""
    name = _s(char.get("name"))
    costume = _s(char.get("costume_main")) + _s(char.get("description"))[:80]
    for mark in VOICE_ONLY_MARKS:
        if mark in name or mark in costume:
            return True
    return False


def _s(value: Any, default: str = "") -> str:''',
        1,
    )
    print('② 加入声音角色识别')

# 编译时把声音角色分离
old_cast = """    cast = []
    for cid in (shot.get("character_ids") or []):
        char = characters.get(str(cid))
        if char:
            cast.append(char)
    if not cast and len(characters) == 1:
        cast = list(characters.values())"""
new_cast = """    cast = []
    voice_only = []
    for cid in (shot.get("character_ids") or []):
        char = characters.get(str(cid))
        if not char:
            continue
        # ★ 只出声不出镜的角色（旁白等）单独拎出来：它们不构成画面主体。
        if is_voice_only(char):
            voice_only.append(char)
        else:
            cast.append(char)
    if not cast and not voice_only and len(characters) == 1:
        cast = list(characters.values())"""
if old_cast in text:
    text = text.replace(old_cast, new_cast, 1)
    print('② 编译时分离声音角色')
else:
    print('② 未匹配 cast')

# 主体行：只有声音角色时明确写"本镜无出镜人物"
old_subject = """    if anchors:
        # ★ 人数只数"人"：蛇/狗/猫/鸟这些是**主体但不是人**，
        #   写成"共 2 人"会让视频模型凭空多生成一个人。
        animal_marks = ("蛇", "狗", "猫", "鸟", "狼", "马", "牛", "羊", "兔", "鱼", "鼠")
        people = [c for c in cast if not any(mark in _s(c.get("name")) for mark in animal_marks)]
        count_line = f"。画面内人物 {len(people)} 人" if people else ""
        parts.append("主体：" + "；".join(anchors) + count_line + "。")"""
new_subject = """    if anchors:
        # ★ 人数只数"人"：蛇/狗/猫/鸟这些是**主体但不是人**，
        #   写成"共 2 人"会让视频模型凭空多生成一个人。
        animal_marks = ("蛇", "狗", "猫", "鸟", "狼", "马", "牛", "羊", "兔", "鱼", "鼠")
        people = [c for c in cast if not any(mark in _s(c.get("name")) for mark in animal_marks)]
        count_line = f"。画面内人物 {len(people)} 人" if people else ""
        parts.append("主体：" + "；".join(anchors) + count_line + "。")
    elif voice_only:
        # ★ 只有旁白/画外音时：**明确说本镜没有出镜人物**，
        #   否则模型会把"旁白"当成一个要出现在画面里的人。
        parts.append("本镜没有出镜人物，画面只交代环境与空镜。")"""
if old_subject in text:
    text = text.replace(old_subject, new_subject, 1)
    print('② 只有声音角色时写明无出镜人物')
else:
    print('② 未匹配 subject')

# 音色锚定：声音角色也要进音色锚定（它就是声音！）
old_voice = """    voice_lines = []
    for char in cast:"""
new_voice = """    voice_lines = []
    for char in list(cast) + list(voice_only):"""
if old_voice in text:
    text = text.replace(old_voice, new_voice, 1)
    print('② 音色锚定包含声音角色')
else:
    print('② 未匹配 voice_lines')

# ③ 场景锚定去重（夜晚光 + 夜晚）
old_scene = """    lighting_cn = _cn(lighting)
    if lighting_cn:
        parts.append(f"{lighting_cn}光" if not lighting_cn.endswith("光") else lighting_cn)
    when_cn = _cn(when)
    if when_cn:
        parts.append(when_cn)
    weather_cn = _cn(weather)
    if weather_cn:
        parts.append(weather_cn)"""
new_scene = """    lighting_cn = _cn(lighting)
    when_cn = _cn(when)
    weather_cn = _cn(weather)
    if lighting_cn:
        # "夜晚"+"夜晚光" 这类重复要合并（lighting 字段常常就是 time_of_day）
        if lighting_cn == when_cn:
            parts.append(f"{lighting_cn}光")
        else:
            parts.append(f"{lighting_cn}光" if not lighting_cn.endswith("光") else lighting_cn)
    if when_cn and when_cn != lighting_cn:
        parts.append(when_cn)
    if weather_cn:
        parts.append(weather_cn)"""
if old_scene in text:
    text = text.replace(old_scene, new_scene, 1)
    print('③ 场景锚定去重')
else:
    print('③ 未匹配 scene')

shutil.copyfile(sp, sp.with_suffix(sp.suffix + '.bak-' + STAMP))
sp.write_text(text, encoding='utf-8')

import py_compile
for path in (kf, sp):
    py_compile.compile(str(path), doraise=True)
    print('语法 OK', path.name)
