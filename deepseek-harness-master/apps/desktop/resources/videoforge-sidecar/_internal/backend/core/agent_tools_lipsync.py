# -*- coding: utf-8 -*-
"""
VideoForge · 唇形同步（云端 kling-lip-sync）的**工具入口**
==========================================================

用户要求（2026-10-23）：
    「再就是要跟视频里面人物的嘴型、唇语同步！」
    「本地太慢了！」    ← 明确否掉本地 Wav2Lip

所以走**中转站的 kling-lip-sync**（可灵官方唇形同步，云端 GPU）。

## 一个必须说清的前提（决定了这个功能什么时候有意义）

唇形同步是给**画面里真的有嘴在动**的镜头用的 —— 也就是
**视频模型生成的"人物开口说话"镜头**。
如果这一镜是用**静图 + 旁白/配音**做出来的（Ken Burns 推拉），
画面里**根本没有嘴**，谈不上唇语同步；这时只有两条路：
  ① 换成"人物说话"的动态镜头（视频模型生成）再对口型；
  ② 接受"声画分离"（画面不表演说话，声音在讲）—— 这是短剧/宣传片常见做法。

本工具在两种情况下都会**如实告诉用户**，不假装同步成功。

## 计费

按次计费（中转站侧）。所以：
  · 工具本身是**手动触发**的，不会在合成时偷偷跑；
  · 合成链路里只有设置 `lipsync_mode=cloud` 才会自动对**有台词的动态镜头**跑。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from .agent_tools import REGISTRY, err, ok

logger = logging.getLogger(__name__)


def _services():
    from .main import db  # 运行时拿，避免循环依赖
    return db


@REGISTRY.register(
    "lipsync_shot",
    "给某一镜做**唇形同步**（云端 kling-lip-sync，把配音对到画面人物的嘴上）。\n"
    "★ 前提：这一镜的画面得是**人物开口说话的动态镜头**；"
    "静图+旁白做出来的片子画面里没有嘴，对口型没有意义。\n"
    "★ 计费：按次扣费（走中转站）。只在用户明确要求时调用。\n"
    "★ 用法：先出这一镜的视频与配音，再调用本工具；工具会自动找"
    "「该镜已选定的成片视频」和「该镜的配音音轨」。",
    {"project_id": {"type": "string", "desc": "项目 id", "required": True},
     "shot_id": {"type": "string", "desc": "分镜 id", "required": True}},
)
async def t_lipsync_shot(args: Dict[str, Any]) -> Any:
    from core import lipsync as _ls
    db = _services()
    pid = str(args.get("project_id") or "").strip()
    sid = str(args.get("shot_id") or "").strip()
    if not pid or not sid:
        return err("缺少 project_id 或 shot_id")
    settings = db.get_all_settings() or {}
    if not _ls._key(settings):  # noqa: SLF001
        return err("没有中转站 Key（设置里 agentai/openai 那把），发不出公网 URL、"
                   "也调不了 kling-lip-sync")
    shot = None
    try:
        shot = db.get_shot(sid)
    except Exception:  # noqa: BLE001
        shot = None
    if not shot:
        for x in (db.list_shots(pid) or []):
            if str(x.get("id")) == sid:
                shot = x
                break
    if not shot:
        return err("找不到这个分镜")
    out_dir = os.path.join(str(settings.get("output_dir") or ""), pid, "lipsync")
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError:
        pass
    r = await _ls.sync_shot(db, shot, settings, out_dir)
    if not r.get("ok"):
        return err(r.get("reason") or "唇形同步失败")
    try:
        db.update_shot(sid, lipsync_path=r.get("path") or "")
    except Exception:  # noqa: BLE001
        logger.info(" lipsync_path 写库失败（结果文件仍在）：%s", r.get("path"))
    return ok(path=r.get("path"), bytes=r.get("bytes"),
              note=("这一镜已做唇形同步（云端 kling-lip-sync）。"
                    "把新文件当该镜的成片用；原文件保留未删。"))
