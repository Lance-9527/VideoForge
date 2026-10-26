"""修正 shot_prompts 的四个真实缺陷（首次编译在真项目上暴露出来的）：

1. 年龄字段本身含"岁/上下/左右"时又补了一个"岁" → "50 岁上下岁"
2. 服装为空时拼出"穿无服装"
3. 把蛇算成"人" → "画面内共 2 人"
4. 场景锚定把内部枚举原样吐出来（outdoor / overcast / day / snowy）
"""
import shutil
import time
from pathlib import Path

p = Path(r'D:\VideoForge-dev\backend\core\shot_prompts.py')
text = p.read_text(encoding='utf-8')
shutil.copyfile(p, p.with_suffix(p.suffix + '.bak-' + time.strftime('%Y%m%d-%H%M%S')))

# ── ① 枚举 → 中文 映射表 ────────────────────────────────────────────────
enum_block = '''

#: 内部枚举 → 中文（这些值来自场景卡，直接吐给视频模型等于没写）
_ENUM = {
    "outdoor": "室外", "indoor": "室内", "large": "开阔", "medium": "中等", "small": "狭小",
    "day": "白天", "night": "夜晚", "dusk": "黄昏", "dawn": "清晨",
    "sunny": "晴", "rainy": "雨", "snowy": "雪", "overcast": "阴", "cloudy": "多云", "foggy": "雾",
    "natural": "自然光", "warm": "暖光", "cool": "冷光", "dim": "弱光", "bright": "强光",
    "无服装": "", "none": "", "null": "",
}


def _cn(value: Any) -> str:
    """把枚举/英文标签翻成中文；不认识就原样返回。"""
    text = _s(value)
    if text == "":
        return ""
    return _ENUM.get(text.strip().lower(), _ENUM.get(text.strip(), text))
'''

text = text.replace('\ndef _s(value: Any, default: str = "") -> str:', enum_block + '\ndef _s(value: Any, default: str = "") -> str:', 1)

# ── ② 年龄/服装/人数 修正 ──────────────────────────────────────────────
old_age = '''    if age:
        parts.append(f"{age}岁" if not age.endswith("岁") else age)
    if body:
        parts.append(body)
    if costume:
        parts.append(f"穿{costume}")'''
new_age = '''    if age:
        # 年龄字段可能已经是"50 岁上下 / 成年 / 中年" —— 只在纯数字时才补"岁"
        parts.append(age if any(mark in age for mark in ("岁", "上下", "左右", "成年", "中年", "老年", "少年", "青年")) else f"{age}岁")
    if body:
        parts.append(body)
    costume_text = _s(_cn(costume))
    if costume_text:
        parts.append(f"穿{costume_text}")'''
text = text.replace(old_age, new_age, 1)

old_scene = '''    if location:
        parts.append(location)
    if spatial:
        parts.append(spatial)
    if lighting:
        parts.append(f"{lighting}光")
    if when:
        parts.append(when)
    if weather:
        parts.append(weather)'''
new_scene = '''    location_cn = _cn(location)
    if location_cn:
        parts.append(location_cn)
    if spatial:
        parts.append(spatial)
    lighting_cn = _cn(lighting)
    if lighting_cn:
        parts.append(f"{lighting_cn}光" if not lighting_cn.endswith("光") else lighting_cn)
    when_cn = _cn(when)
    if when_cn:
        parts.append(when_cn)
    weather_cn = _cn(weather)
    if weather_cn:
        parts.append(weather_cn)'''
text = text.replace(old_scene, new_scene, 1)

# ── ③ 人数只算"人"（蛇/狗/猫等不算人）─────────────────────────────────
old_cast = '''    if anchors:
        parts.append("主体：" + "；".join(anchors) + f"。画面内共 {len(anchors)} 人。")'''
new_cast = '''    if anchors:
        # ★ 人数只数"人"：蛇/狗/猫/鸟这些是**主体但不是人**，
        #   写成"共 2 人"会让视频模型凭空多生成一个人。
        animal_marks = ("蛇", "狗", "猫", "鸟", "狼", "马", "牛", "羊", "兔", "鱼", "鼠")
        people = [c for c in cast if not any(mark in _s(c.get("name")) for mark in animal_marks)]
        count_line = f"。画面内人物 {len(people)} 人" if people else ""
        parts.append("主体：" + "；".join(anchors) + count_line + "。")'''
text = text.replace(old_cast, new_cast, 1)

p.write_text(text, encoding='utf-8')
import py_compile
py_compile.compile(str(p), doraise=True)
print('shot_prompts.py 已修正并通过语法检查', p.stat().st_size, 'B')
