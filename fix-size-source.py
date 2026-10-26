"""修两处：
① 景别判定改为**以 camera 字段为准**（概述里常写"从全景推到特写"，按位置取会取错）；
② 服装"无服装。"（带句号）要归一化成空，别拼出"穿无服装。"。
"""
import shutil
import time
from pathlib import Path

STAMP = time.strftime('%Y%m%d-%H%M%S')
ROOT = Path(r'D:\VideoForge-dev\backend\core')

# ① shot_chain：景别以 camera 为准
sc = ROOT / 'shot_chain.py'
text = sc.read_text(encoding='utf-8')
if 'def shot_size_of(' not in text:
    helper = '''

def _timeline_of(shot: Dict[str, Any]) -> List[Dict[str, Any]]:
    """镜头的 layer2_timeline（字符串则解 JSON）。"""
    timeline = shot.get("layer2_timeline")
    if isinstance(timeline, str):
        try:
            timeline = json.loads(timeline)
        except Exception:  # noqa: BLE001
            timeline = []
    return [i for i in (timeline or []) if isinstance(i, dict)]


def shot_size_of(shot: Dict[str, Any]) -> str:
    """这一镜的景别 —— **以 camera 字段为准**，没有才回退到概述。

    ★ 实测踩坑：概述常写成"从全景推到特写"，按全文出现位置取会取到"全景"，
      而真正的机位是 camera 里的"特写" ⇒ 同机位连续动作被误判成换机位，接龙不触发。
    """
    for item in _timeline_of(shot):
        camera = str(item.get("camera") or "")
        size = shot_size(camera)
        if size:
            return size
    return shot_size(str(shot.get("layer1_overview") or ""))
'''
    anchor = '\ndef has_time_gap(text: str) -> bool:'
    text = text.replace(anchor, helper + anchor, 1)
    # can_relay 改用 shot_size_of
    text = text.replace(
        '''    prev_size = shot_size(prev_text)
    this_size = shot_size(this_text)''',
        '''    prev_size = shot_size_of(prev_shot)
    this_size = shot_size_of(shot)''',
    )
    sc.write_text(text, encoding='utf-8')
    print('① 景别判定改为以 camera 字段为准')
else:
    print('① 已存在 shot_size_of')

# ② shot_prompts：服装归一化
sp = ROOT / 'shot_prompts.py'
text = sp.read_text(encoding='utf-8')
old = '''    costume_text = _s(_cn(costume))
    # ★ 服装为空就别拼"穿无服装"（蛇/道具这类角色会出现脏话）。
    if costume_text and costume_text not in ("无服装", "无", "不适用"):'''
new = '''    costume_text = _s(_cn(costume)).strip("。．.、 ")
    # ★ 服装为空就别拼"穿无服装"（蛇/道具这类角色会出现脏话；
    #   注意角色卡里常写成"无服装。"带句号，要先去掉标点再判断）。
    if costume_text and costume_text not in ("无服装", "无", "不适用", "无衣物"):'''
if old in text:
    sp.write_text(text.replace(old, new, 1), encoding='utf-8')
    print('② 服装归一化已修')
else:
    print('② 未匹配')

import py_compile
for p in (sc, sp):
    py_compile.compile(str(p), doraise=True)
print('语法 OK')
