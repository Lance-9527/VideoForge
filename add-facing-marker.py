"""在人偶 make_human 末尾插入朝向标记（按函数体尾部插入，避免逐字匹配原文）。"""
import re
import shutil
import time
from pathlib import Path

STAMP = time.strftime('%Y%m%d-%H%M%S')
d = Path(r'D:\VideoForge-dev\backend\data\previz_blender\design.py')
text = d.read_text(encoding='utf-8')

if '胸口' in text:
    print('已存在朝向标记')
    raise SystemExit(0)

i = text.find('def make_human(')
assert i >= 0, '找不到 make_human'
# 函数结束 = 下一个顶层 def/class
m = re.search(r'\n(?=def |class |# ---)', text[i + 10:])
assert m, '找不到函数结尾'
end = i + 10 + m.start()

marker = '''    # ★ 2026-10-22（按 Proma 的白膜规范）：灰模人偶要**一眼看出朝向**，
    #   否则用户在白膜里判断不了"人朝哪走"（这正是"白膜看不出走位"的根因）。
    #   做法同 Proma 的 nose + chest marker：胸口一个小方块 + 头前方一个小楔子（鼻子）。
    #   人物默认面向 -Y，所以鼻子朝 -Y；rot 会一起转。
    cube(f"{name}_胸口", (x, y - 0.11, tall * 0.72), (0.10, 0.04, 0.10), key, r)
    _nd = -0.15
    cube(f"{name}_鼻", (x + _nd * math.sin(r), y + _nd * math.cos(r), tall + 0.10),
         (0.06, 0.13, 0.06), key, r)
'''

text = text[:end] + marker + text[end:]
shutil.copyfile(d, d.with_suffix(d.suffix + '.bak-' + STAMP))
d.write_text(text, encoding='utf-8')
print('已在 make_human 末尾插入朝向标记')

import py_compile
py_compile.compile(str(d), doraise=True)
print('语法 OK')
print(text[i:i + 200].split('\n')[0])
