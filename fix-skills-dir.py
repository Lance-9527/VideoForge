"""修正 skill 路由：`get_registry` 的第一个参数是 **data_dir**（它自己会拼 /skills）。

原来传了 default_learned_dir(...) → 变成 .../data/skills/skills，所以只看到 3 个内置。
"""
from pathlib import Path

p = Path(r'D:\VideoForge-dev\backend\main.py')
text = p.read_text(encoding='utf-8')

old_import = 'from core.skills import get_registry, extra_dirs_from_settings, default_learned_dir'
new_import = 'from core.skills import get_registry, extra_dirs_from_settings'
old_call = 'reg = get_registry(default_learned_dir(), extra_dirs_from_settings(db.get_all_settings()))'
new_call = '''_dd = os.environ.get("VIDEOFORGE_DATA_DIR") or os.path.join(os.getcwd(), "data")
            reg = get_registry(_dd, extra_dirs_from_settings(db.get_all_settings()))'''

n_import = text.count(old_import)
n_call = text.count(old_call)
text = text.replace(old_import, new_import).replace(old_call, new_call)
p.write_text(text, encoding='utf-8')
print(f'替换 import {n_import} 处、调用 {n_call} 处')

import py_compile
py_compile.compile(str(p), doraise=True)
print('语法 OK')
