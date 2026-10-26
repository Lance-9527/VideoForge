# -*- coding: utf-8 -*-
"""`backend/tests/test_prompt_film.py` —— 兼容入口（不做逻辑，只转发）。

用例本体在仓库根的 `tests/test_prompt_film.py`：本仓所有 `tests/test_*.py` 都在那里，
所以「仓库根跑 `python -X utf8 tests/test_prompt_film.py`」才是主路径。
这个文件存在的意义：任务书写的是 `backend/tests/`，在 backend 目录下跑同名命令
（`python -X utf8 tests/test_prompt_film.py`）时也能直接跑到同一套用例。

★ 新增用例请改仓库根的 `tests/test_prompt_film.py`，不要在这里加。
"""
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "tests" / "test_prompt_film.py"
if not TARGET.is_file():  # pragma: no cover - 只有目录被搬家时才会触发
    sys.stderr.write("找不到用例本体：%s\n" % TARGET)
    raise SystemExit(2)
runpy.run_path(str(TARGET), run_name="__main__")
