"""收紧 C11：方位冲突检查必须**对象绑定**，否则会误报。

原规则：从锚点里取对象名的第一个方位词，再在提示词里找「对象名 + 反向方位」，
于是「便利店…偏右」会被「路灯…偏左」（另一个对象）误伤。

改法：
  ① 只检查**去掉锚点句之后**的提示词正文（锚点句本身是"事实来源"，不该被自己判违规）；
  ② 方位词必须**紧跟对象名**（间隔 ≤ 6 个字）才算该对象的方位；
  ③ 锚点里该对象的方位词也必须在名字后 ≤ 14 字内，否则视为"描述太散"，不做判定。
"""
import shutil
import time
from pathlib import Path

STAMP = time.strftime('%Y%m%d-%H%M%S')
p = Path(r'D:\VideoForge-dev\backend\core\scene_anchors.py')
text = p.read_text(encoding='utf-8')
shutil.copyfile(p, p.with_suffix(p.suffix + '.bak-' + STAMP))

old = '''def find_conflicts(anchors: Dict[str, Any], video_prompt: str) -> List[str]:
    """C11：提示词里是否出现了与锚点相反的方位。"""
    conflicts: List[str] = []
    if not anchors:
        return conflicts
    props = anchors.get("props") or {}
    if not isinstance(props, dict):
        return conflicts
    for name, position in props.items():
        pos = str(position or "")
        m = re.search(r"(左|右|前|后|上|下|内|外|里|近|远)", pos)
        if not m:
            continue
        word = m.group(1)
        opposite = _OPPOSITES.get(word)
        if not opposite:
            continue
        # 在提示词里找"名字 + 反向方位"的组合
        pattern = re.escape(str(name)) + r"[^。；\\n]{0,12}" + re.escape(opposite)
        if re.search(pattern, video_prompt):
            conflicts.append(f"「{name}」在锚点里是「{word}」，提示词里却出现「{opposite}」")
    return conflicts'''

new = '''def _direction_of(text: str, name: str, window: int) -> str:
    """取「对象名后面 window 字以内」的方位词（取不到就空串）。"""
    pattern = re.escape(str(name)) + r"[^。；\\n]{0,%d}" % window
    match = re.search(pattern, text or "")
    if not match:
        return ""
    m = re.search(r"(左|右|前|后|上|下|内|外|里|近|远)", match.group(0))
    return m.group(1) if m else ""


def find_conflicts(anchors: Dict[str, Any], video_prompt: str) -> List[str]:
    """C11：提示词里是否出现了与锚点相反的方位（**对象绑定**，不误报别人）。"""
    conflicts: List[str] = []
    if not anchors:
        return conflicts
    props = anchors.get("props") or {}
    chars = anchors.get("characters") or {}
    if not isinstance(props, dict):
        props = {}
    if not isinstance(chars, dict):
        chars = {}
    if not props and not chars:
        return conflicts

    # ① 去掉锚点句本身（它是事实来源，不能被自己判违规）
    body = re.sub(r"【空间锚点】[^。]*。", "", video_prompt or "")

    for table in (props, chars):
        for name, position in table.items():
            # ② 锚点里该对象的方位（必须紧跟名字，否则不做判定）
            word = _direction_of(str(position or ""), str(name), 14)
            if not word:
                continue
            opposite = _OPPOSITES.get(word)
            if not opposite:
                continue
            # ③ 提示词正文里，该对象名附近出现了反向方位
            if _direction_of(body, str(name), 6) == opposite:
                conflicts.append(
                    f"「{name}」在锚点里是「{word}」，提示词里却写成「{opposite}」")
    return conflicts'''

if old in text:
    p.write_text(text.replace(old, new, 1), encoding='utf-8')
    print('C11 已收紧（对象绑定 + 去掉锚点句）')
else:
    print('未匹配 find_conflicts')

import py_compile
py_compile.compile(str(p), doraise=True)
print('语法 OK')
