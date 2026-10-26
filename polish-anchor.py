"""锚定句小修：
· 角色卡里服装为空 → 不要拼"穿无服装"（蛇这种角色会出现"穿无服装。，全程保持同一张脸…"这种脏话）
· 没有服装也没有外貌特征的角色（蛇/物件）→ 不加"全程保持同一张脸、年龄、体型与服装"这句（它不适用）
"""
import shutil
import time
from pathlib import Path

STAMP = time.strftime('%Y%m%d-%H%M%S')
sp = Path(r'D:\VideoForge-dev\backend\core\shot_prompts.py')
text = sp.read_text(encoding='utf-8')
shutil.copyfile(sp, sp.with_suffix(sp.suffix + '.bak-' + STAMP))

old = '''    costume_text = _s(_cn(costume))
    if costume_text:
        parts.append(f"穿{costume_text}")
    parts.append("全程保持同一张脸、年龄、体型与服装")
    anchor = "，".join(parts)'''
new = '''    costume_text = _s(_cn(costume))
    # ★ 服装为空就别拼"穿无服装"（蛇/道具这类角色会出现脏话）。
    if costume_text and costume_text not in ("无服装", "无", "不适用"):
        parts.append(f"穿{costume_text}")
    # ★ "保持同一张脸"只对**有外貌描述**的角色成立（蛇/物件不适用）。
    if feature or costume_text:
        parts.append("全程保持同一张脸、年龄、体型与服装")
    anchor = "，".join(parts)'''
if old in text:
    sp.write_text(text.replace(old, new, 1), encoding='utf-8')
    print('锚定句小修已应用')
else:
    print('未匹配')

import py_compile
py_compile.compile(str(sp), doraise=True)
print('语法 OK')
