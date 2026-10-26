"""修调用点：compile_project_prompts 变成 async 之后，调用处要 await + 传设置与输出目录。"""
import shutil
import time
from pathlib import Path

STAMP = time.strftime('%Y%m%d-%H%M%S')
p = Path(r'D:\VideoForge-dev\backend\core\agent_tools.py')
text = p.read_text(encoding='utf-8')
shutil.copyfile(p, p.with_suffix(p.suffix + '.bak-' + STAMP))

old = """        try:
            from core.shot_prompts import compile_project_prompts as _cpp
            _n = _cpp(db, args.get("project_id") or "")
            if _n:
                logger.info("分镜提示词已编译：%d 镜", _n)
        except Exception as _e:  # noqa: BLE001
            logger.warning("分镜提示词编译失败（不影响分镜本身）：%s", _e)"""
new = """        try:
            from core.shot_prompts import compile_project_prompts as _cpp
            _settings = {}
            try:
                _settings = _settings_of(db) or {}
            except Exception:  # noqa: BLE001
                _settings = {}
            _out = str(_settings.get("output_dir") or "")
            _n = await _cpp(db, args.get("project_id") or "", settings=_settings, output_dir=_out)
            if _n:
                logger.info("分镜提示词已编译：%d 镜", _n)
        except Exception as _e:  # noqa: BLE001
            logger.warning("分镜提示词编译失败（不影响分镜本身）：%s", _e)"""
if old in text:
    p.write_text(text.replace(old, new, 1), encoding='utf-8')
    print('调用点已改 await')
else:
    print('未匹配调用点')

import py_compile
py_compile.compile(str(p), doraise=True)
print('语法 OK')
