# -*- coding: utf-8 -*-
"""VideoForge · Agent 工具：**运行环境体检 / 技能依赖**

★★ 为什么需要（2026-10-25 客户刚需）
    客户自蒸馏的 skill 拿到本产品上跑，最常见的失败是「import 不到某个库」。
    安装版后端是 PyInstaller 冻结包（没有 pip，且打包时裁掉了 cv2/pandas/numba 等），
    因此不能靠「pip install」解决。

    给 agent 一个**能自己查**的工具：缺什么、装在哪、怎么补（离线 wheel），
    免得它瞎猜，也免得用户只能看到一句 ImportError。

    配套：
      · `core/env_ext.py` —— 可写扩展目录 + 体检/离线安装实现
      · `backend/mcp_server.py` —— 把同一份体检暴露成 MCP 工具（跨 agent）
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from .agent_tools import REGISTRY, ToolResult, err, ok

logger = logging.getLogger("videoforge.agent.tools.env")


@REGISTRY.register(
    "env_health",
    "**运行环境体检**（只读、不花钱）：报告 Python 版本/是否冻结包/有没有 pip、"
    "可写技能扩展目录是否就绪、客户 skill 常用包（cv2、pandas、numba、cryptography…）"
    "哪些可用、缺的包怎么补（离线 wheel）。"
    "★ 遇到「某个库 import 失败 / skill 跑不起来 / 缺依赖」**先调这个**，不要猜。",
    {},
    writes=False,
)
async def t_env_health(args: Dict[str, Any]) -> ToolResult:
    try:
        from core.data_root import resolve_data_root
        from core.env_ext import activate, diagnose
        root = resolve_data_root()
        activate(root)          # 幂等：确保本次进程的 sys.path 已带上扩展目录
        report = diagnose(root)
        # 给模型一句可直接执行的结论，省一次推理
        missing = report.get("missing") or []
        summary = ("环境正常，技能常用包都在。" if not missing
                   else f"缺 {len(missing)} 个包：{ '、'.join(missing) }；按 install_hint 补。")
        return ok(**report, summary=summary)
    except Exception as exc:  # noqa: BLE001
        logger.exception("env_health failed")
        return err(f"环境体检失败：{type(exc).__name__}: {exc}")
