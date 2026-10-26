# -*- coding: utf-8 -*-
r"""试用版（分享版）判定。

为什么需要它（用户 2026-09-14）：
    「对了，假如我要分享怎么证明版权是我的。。。我不能做大好人」
    → 选定的做法是 **署名 + 许可协议 + 分发留痕**，并且**分享出去的试用版，
      成片右下角带"VideoForge 试用版"水印**；自己用的正式版不带。

怎么区分"哪一份是试用版"：
    打包后的程序旁边放一个 `trial.txt`（内容随便写一句说明）。
    启动器看到它就设置 `VIDEOFORGE_TRIAL=1`，后端据此给成片打水印。

★ 诚实的定位（必须写清楚，别自欺）：
    这是**礼貌门槛**，不是防破解。PyInstaller 打包的 Python 程序可以被解包，
    删掉 trial.txt、改一行代码、或者重新打包都能去掉水印。
    它真正的作用是：
      ① 发出去的片子自带出处（别人看到就知道是谁做的）；
      ② 想商用/想去水印 → 只能来找你要正式版 —— 把"白拿"变成一个要开口的动作；
      ③ 配合软著登记与许可协议，形成"技术标记 + 法律声明"的组合。
    真正的保护在**法律（软著登记/许可协议）与商业设计（服务、更新、模板）**，
    不在这一层代码里。
"""
from __future__ import annotations

import os
from pathlib import Path

TRIAL_MARKER = "trial.txt"
WATERMARK_TEXT = "VideoForge 试用版"


def _app_dir() -> Path:
    """程序所在目录（打包后 = exe 同级；开发时 = 仓库根）。"""
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parents[2]


def trial_marker_path() -> Path:
    return _app_dir() / TRIAL_MARKER


def is_trial() -> bool:
    """是否"分享用的试用版"。环境变量优先（启动器会设），其次看标记文件。"""
    env = (os.environ.get("VIDEOFORGE_TRIAL") or "").strip()
    if env in ("1", "true", "True", "yes"):
        return True
    if env in ("0", "false", "False", "no"):
        return False
    try:
        return trial_marker_path().exists()
    except Exception:
        return False


def trial_note() -> str:
    return (f"这是**试用版**：成片右下角会带「{WATERMARK_TEXT}」标识。"
            f"（正式版不带。该标识是版权署名的技术标记，不是恶意限制。）")


def summary() -> dict:
    return {
        "is_trial": is_trial(),
        "watermark_text": WATERMARK_TEXT,
        "marker": str(trial_marker_path()),
        "note": trial_note() if is_trial() else "",
        "strength": "礼貌门槛：trial.txt 或代码可被改动，真正的保护在许可协议与软著登记",
    }
