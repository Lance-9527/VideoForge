"""把 main.py 里我插入路由用的 `error(...)` 改成项目里真实存在的 `error_response(...)`。

（诚实记录：我按 core 层习惯用了 `error(...)`，但 main.py 里只有 `error_response`；
  第一版 prompts 路由能跑是因为那三条路径里没触发 error 分支。）
"""
from pathlib import Path

p = Path(r'D:\VideoForge-dev\backend\main.py')
text = p.read_text(encoding='utf-8')
start = text.find('    # ── 分镜提示词（三段式')
end = text.find('    @app.get("/api/shots/{sid}")', start)
assert start > 0 and end > start, '定位失败'
segment = text[start:end]
fixed = segment.replace('return error(', 'return error_response(')
count = segment.count('return error(')
text = text[:start] + fixed + text[end:]
p.write_text(text, encoding='utf-8')
print(f'替换 {count} 处 error() → error_response()')

import py_compile
py_compile.compile(str(p), doraise=True)
print('语法 OK')
