# -*- coding: utf-8 -*-
"""
白膜**场地尺度**（地图大小）—— 单一真相源
==========================================

★ 为什么要有这个模块（2026-10-26 用户原话）：
    「openVideoForge 的地图大小可以再拓一下，可以大一些，装得下一个好莱坞；
      如果需要小场景，设置就行」

  以前白膜的场地尺寸是**写死的常量**：
    · `previz_design.normalize_spec` 把 width/depth 钳在 1.5~24 / 1.5~40
    · 兜底规格写死 {"width":6,"depth":9,"height":2.8}
    · 场景卡写死 space_scale="small"
  ⇒ 想拍大场面（小场景反而是"设置"里的一个选项）只能改代码。

  现在尺寸是**设置**：
    设置键 `previz_scene_scale` ∈ small / medium / large（默认 small = 今天的行为）
    · small  = 今天的尺寸（6×8×2.8，钳制上限 24×40×8）
    · medium = 中型场景（默认 24×40×6 —— 今天的上限）
    · large  = 大场景（默认 120×160×12，够"装下一个好莱坞"）
  工作台侧（openVideoForge 的 js/scenes.js）有**同名档位**，语义对齐。

读设置的路径与 `core/asr_siliconflow.py` 同一套（resolve_data_root → videoforge.db），
这样没有注入 db 的地方（agent 工具、通道层）也能读到同一个设置。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger("videoforge.previz_scale")

#: 设置键（在 VideoForge 的「设置」里加一条即可）
SCENE_SCALE_SETTING = "previz_scene_scale"

#: 缺省档位 = small（= 今天的行为，不改设置也不变味）
DEFAULT_SCENE_SCALE = "small"

#: 档位 → 场地尺寸上限/默认值
#:   width/depth/height = (下限, 上限, 默认值)，单位米
SCENE_SCALES: Dict[str, Dict[str, Any]] = {
    "small": {
        "width": (1.5, 24.0, 6.0),
        "depth": (1.5, 40.0, 8.0),
        "height": (1.8, 8.0, 2.8),
        # 兜底规格（模型不可用时用）：保持今天的 6×9×2.8 不变
        "fallback": (6.0, 9.0, 2.8),
    },
    "medium": {
        "width": (1.5, 24.0, 24.0),
        "depth": (1.5, 40.0, 40.0),
        "height": (1.8, 8.0, 6.0),
        "fallback": (24.0, 40.0, 6.0),
    },
    "large": {
        "width": (6.0, 120.0, 120.0),
        "depth": (6.0, 160.0, 160.0),
        "height": (1.8, 12.0, 12.0),
        "fallback": (120.0, 160.0, 12.0),
    },
}


def settings_of(db: Optional[Any] = None) -> Dict[str, Any]:
    """读设置（有 db 用 db；没有就按 data_root 打开同一个 videoforge.db）。"""
    if db is not None:
        try:
            return db.get_all_settings() or {}
        except Exception as e:  # noqa: BLE001
            logger.warning("读设置失败（白膜尺度用默认档）：%s", e)
            return {}
    try:
        from .data_root import resolve_data_root
        from .db import Database
        root = resolve_data_root()
        return Database(os.path.join(root, "videoforge.db")).get_all_settings() or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("读设置失败（白膜尺度用默认档）：%s", e)
        return {}


def scene_scale(db: Optional[Any] = None,
                scale: Optional[str] = None) -> str:
    """当前场地尺度档位（small/medium/large；脏值一律退回 small）。"""
    if scale:
        name = str(scale).strip().lower()
        if name in SCENE_SCALES:
            return name
    raw = str(settings_of(db).get(SCENE_SCALE_SETTING) or "").strip().lower()
    return raw if raw in SCENE_SCALES else DEFAULT_SCENE_SCALE


def limits(db: Optional[Any] = None,
           scale: Optional[str] = None) -> Dict[str, Any]:
    """当前档位的尺寸限制表（width/depth/height + fallback）。"""
    return SCENE_SCALES[scene_scale(db, scale)]


def fallback_size(db: Optional[Any] = None,
                  scale: Optional[str] = None):
    """兜底规格的场地尺寸 (宽, 深, 高)。"""
    return limits(db, scale)["fallback"]
