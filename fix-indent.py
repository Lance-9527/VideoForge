"""修复 agent_tools.py 里 try 块缩进（按 `return ok(` 所在行缩进对齐）。"""
import re
import shutil
import time
from pathlib import Path

p = Path(r'D:\VideoForge-dev\backend\core\agent_tools.py')
text = p.read_text(encoding='utf-8')
stamp = time.strftime('%Y%m%d-%H%M%S')
shutil.copyfile(p, p.with_suffix(p.suffix + f'.broken-{stamp}'))

lines = text.split('\n')
# 找我们插入的 try 块起点
start = next((i for i, l in enumerate(lines) if 'compile_project_prompts as _cpp' in l), -1)
if start < 0:
    print('没找到插入块')
    raise SystemExit(1)

try_line = start - 1
indent = len(lines[try_line]) - len(lines[try_line].lstrip())
print('try 行 =', try_line + 1, '缩进 =', indent, '内容 =', lines[try_line].strip()[:40])

# 找到该块的结束（插入的最后一行是 return ok( 那行之后的空行）
end = start
while end < len(lines) and not lines[end].strip().startswith('return ok('):
    end += 1
# end 指向 return ok( 行；把中间 6 行（try 之后到 return 之前）按 indent+4 重排
block_end = end  # exclusive 不含 return 行
inner = lines[try_line + 1:block_end]
inner = [' ' * (indent + 4) + l.strip() for l in inner if l.strip() != '']
lines[try_line + 1:block_end] = inner
p.write_text('\n'.join(lines), encoding='utf-8')
print('已重排，内层行数 =', len(inner))

import py_compile
py_compile.compile(str(p), doraise=True)
print('语法 OK')
