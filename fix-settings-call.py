"""把 `_settings_of(db)` 换成真实存在的 `db.get_all_settings()`。"""
from pathlib import Path

p = Path(r'D:\VideoForge-dev\backend\core\agent_tools.py')
text = p.read_text(encoding='utf-8')
old = """            _settings = {}
            try:
                _settings = _settings_of(db) or {}
            except Exception:  # noqa: BLE001
                _settings = {}"""
new = """            _settings = {}
            try:
                _settings = db.get_all_settings() or {}
            except Exception:  # noqa: BLE001
                _settings = {}"""
if old in text:
    p.write_text(text.replace(old, new, 1), encoding='utf-8')
    print('已改用 db.get_all_settings()')
else:
    print('未匹配')

import py_compile
py_compile.compile(str(p), doraise=True)
print('语法 OK')
