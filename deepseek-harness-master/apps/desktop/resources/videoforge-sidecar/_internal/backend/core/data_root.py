# -*- coding: utf-8 -*-
"""数据根解析（**单一真相源**）。

为什么要有这个模块（2026-10-21，v3 / DSH 化时补上）：
    同一个"数据目录在哪"的规则，本仓过去写在**三个地方**——
      · `launcher.py:resolve_data_dir`（打包 exe 自己的启动器）
      · `backend/main.py`（后端自己兜底）
      · `backend/core/envcheck.py:check_data_dir`（环境自检）
    三处各写一份必然漂。v3 又多了一个启动者（DSH 桌面端 spawn python），
    于是"sidecar 连的是哪个库"这件事，谁也说不清了。

**这条规则为什么关键**：
    用户装的那份库在 `%LOCALAPPDATA%\\VideoForge\\data`（含 40 个老项目）；
    开发时用的是仓库里的 `backend/data`（测试项目）。
    两者混用 = 用户打开 app 看不到自己的项目 —— 这正是 v3 要消灭的故障。

**优先级（与 `launcher.py:resolve_data_dir` 同源）**：
    1. `VIDEOFORGE_DATA_DIR` 环境变量 —— 显式指定永远最大；**只认绝对路径**
       （相对路径曾让"当前工作目录"混进候选并胜出，见 launcher.py 第二道防线）
    2. `%LOCALAPPDATA%\\VideoForge\\data` —— 用户装的那份库
    3. `<cwd>\\data` —— 开发模式（`python main.py` 时的仓库库）

    ★ 第 2 条只在**该目录里已经有一份真的 `videoforge.db`** 时才选中。
      理由：绝不在打包库里凭空建一个空库 —— 那会把"用户项目没显示"
      变成"我们以为在读用户的库，其实在读自己新建的空库"，更难查。

**给 v3 启动者的约定**：
    DSH 桌面端 spawn sidecar 时**必须**显式设 `VIDEOFORGE_DATA_DIR`，
    指向 `%LOCALAPPDATA%\\VideoForge\\data`；不设就退回 cwd/data（开发库）。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

#: 产品名（与 `launcher.py:APP_NAME` 保持一致）。
APP_NAME = "VideoForge"

#: 数据库文件名（`main.py` 与 `launcher.py` 共用）。
DB_FILENAME = "videoforge.db"

#: 数据根环境变量名（launcher 注入 / v3 启动者注入）。
DATA_DIR_ENV = "VIDEOFORGE_DATA_DIR"


def packaged_data_dir() -> Path:
    """用户装的那份数据目录：`%LOCALAPPDATA%\\VideoForge\\data`。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / APP_NAME / "data"


def dev_data_dir(cwd: Optional[str] = None) -> Path:
    """开发模式的数据目录：`<cwd>\\data`。"""
    return Path(cwd or os.getcwd()) / "data"


def resolve_data_root(
    explicit: str = "",
    cwd: Optional[str] = None,
    require_existing_db: bool = True,
) -> str:
    """解析最终的数据根目录（返回绝对路径字符串）。

    :param explicit: 显式指定的数据目录；为空则读 `VIDEOFORGE_DATA_DIR` 环境变量。
    :param cwd: 开发模式基准目录（默认当前工作目录）。
    :param require_existing_db: 第 2 条（打包库）是否要求该目录里已有真的数据库。
    :returns: 数据根目录的绝对路径。
    """
    candidate = (explicit or os.environ.get(DATA_DIR_ENV) or "").strip()
    if candidate:
        path = Path(candidate)
        if path.is_absolute():
            return str(path)
        # 非绝对路径一律不认：跟随工作目录是这个 bug 的根源。
    packaged = packaged_data_dir()
    if not require_existing_db or (packaged / DB_FILENAME).exists():
        return str(packaged)
    return str(dev_data_dir(cwd))


def describe_data_root(data_root: str) -> str:
    """给日志/自检用的一句话说明：用的是哪个库、里面有没有项目库。"""
    path = Path(data_root)
    db = path / DB_FILENAME
    state = "有库" if db.exists() else "无库"
    try:
        size_kb = round(db.stat().st_size / 1024, 1) if db.exists() else 0.0
    except OSError:
        size_kb = 0.0
    return f"数据目录 {path}（{state}，{DB_FILENAME} {size_kb} KB）"
