# -*- coding: utf-8 -*-
"""VideoForge · 运行环境自检（"换一台新电脑能不能跑"）

★★ 用户的要求（2026-09-14）：
    「别的新电脑用不了这个软件，因为没有装环境什么的还有插件等，你做一个适配，
      就当别的电脑都是新电脑」

PyInstaller `onedir` 已经把 **Python 本体、依赖库、FFmpeg** 都打进去了，
所以"新电脑"真正可能缺的东西其实只有这几样 —— 本模块把它们**逐条查清并说人话**：

| 缺什么 | 后果 | 我们的应对 |
|---|---|---|
| **WebView2 运行库** | 桌面窗口起不来（白屏/闪退） | 自动降级到**默认浏览器**模式（功能一致）；给出官方下载页 |
| **VC++ 运行库（msvcp140 等）** | 子进程 0xc0000142，FFmpeg 起不来 | 检出并给出微软官方安装包链接；FFmpeg 会退回 imageio 自带副本 |
| **数据目录不可写** | 什么都存不下 | 自动换到 `%TEMP%\\VideoForge-data` 并说明 |
| **磁盘空间不足** | 生成中途失败 | 提前报"还剩多少 / 至少需要多少" |
| **长路径未启用** | 素材多的项目路径超 260 字符 | 检出并说明（我们内部已尽量用短路径） |
| **中文字体缺失** | 烧字幕变方块 | 检出（雅黑是 Win7+ 自带，正常不会缺） |

纯只读检查，不改系统设置、不下载任何东西。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional


logger = logging.getLogger("videoforge.envcheck")


def _reg_read(root: int, path: str, name: str) -> str:
    try:
        import winreg
        with winreg.OpenKey(root, path) as k:
            v, _ = winreg.QueryValueEx(k, name)
            return str(v or "")
    except Exception:
        return ""


def check_webview2() -> Dict[str, Any]:
    """WebView2 运行库（桌面窗口用；缺了会自动改用浏览器模式）。"""
    guid = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    try:
        import winreg
        for root, sub in (
            (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{guid}"),
            (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}"),
            (winreg.HKEY_CURRENT_USER, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}"),
        ):
            ver = _reg_read(root, sub, "pv")
            if ver:
                return {"ok": True, "version": ver, "path": sub}
    except Exception:
        pass
    return {"ok": False, "version": "", "path": "",
            "fix": "装 WebView2 Runtime（微软官方，免费）："
                   "https://developer.microsoft.com/microsoft-edge/webview2/  —— "
                   "不装也能用：程序会自动改用你的默认浏览器打开界面。"}


def check_vcruntime() -> Dict[str, Any]:
    """VC++ 运行库（FFmpeg 等子进程依赖；缺了会 0xc0000142）。"""
    sysdir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    need = ["msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"]
    missing = [n for n in need if not os.path.exists(os.path.join(sysdir, n))]
    # 也看看 PyInstaller 目录里有没有随包带的副本
    bundled = []
    try:
        base = os.path.dirname(os.path.abspath(sys.executable))
        for sub in ("_internal", "."):
            d = os.path.join(base, sub)
            if os.path.isdir(d):
                for n in need:
                    if os.path.exists(os.path.join(d, n)):
                        bundled.append(n)
    except Exception:
        pass
    ok = (not missing) or len(bundled) >= 2
    return {"ok": ok, "missing": missing, "bundled": sorted(set(bundled)),
            "fix": ("缺 VC++ 运行库（%s）。装一次「Microsoft Visual C++ 2015-2022 "
                    "Redistributable (x64)」即可：https://aka.ms/vs/17/release/vc_redist.x64.exe"
                    % "、".join(missing)) if not ok else ""}


def check_dotnet() -> Dict[str, Any]:
    """pywebview 的 WinForms 后端需要 .NET Framework 4.7.2+（缺了就用浏览器模式）。"""
    rel = 0
    try:
        import winreg
        key = r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full"
        for root in (winreg.HKEY_LOCAL_MACHINE,):
            v = _reg_read(root, key, "Release")
            if v:
                rel = int(v)
                break
    except Exception:
        rel = 0
    ok = rel == 0 or rel >= 461808
    return {"ok": ok, "release": rel,
            "fix": "" if ok else "建议装 .NET Framework 4.7.2+（Windows 10 一般自带）。"
                                 "缺了也不影响使用：会自动改用浏览器模式。"}


def check_data_dir(settings_dir: str = "") -> Dict[str, Any]:
    """数据目录可写性 —— 不可写就换到 %TEMP%（新电脑上装在 Program Files 时会遇到）。

    ★ 2026-10-21：候选顺序改为调用 `core.data_root.resolve_data_root`，
      免得"自检说的目录"和"后端实际用的目录"变成两个（launcher / 后端 / 自检
      曾各写一份，必然漂）。
    """
    from .data_root import resolve_data_root
    cand = resolve_data_root(settings_dir)
    used, writable, note = cand, False, ""
    try:
        os.makedirs(cand, exist_ok=True)
        probe = os.path.join(cand, ".write_test")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        writable = True
    except Exception as e:
        note = f"不可写（{type(e).__name__}）"
        alt = os.path.join(os.environ.get("TEMP", os.path.expanduser("~")), "VideoForge-data")
        try:
            os.makedirs(alt, exist_ok=True)
            probe = os.path.join(alt, ".write_test")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(probe)
            used, writable = alt, True
            note += f" → 已改用 {alt}"
        except Exception as e2:
            note += f"；备用目录也不可写（{type(e2).__name__}）"
    return {"ok": writable, "dir": used, "original": cand, "note": note,
            "fix": "" if writable else "数据目录不可写：把程序放到有写权限的目录，"
                                       "或以管理员身份运行一次。"}


def check_disk(path: str = "") -> Dict[str, Any]:
    """剩余空间：生成视频很吃磁盘（一段 1080p 就要几十 MB）。"""
    p = path or os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    try:
        total, used, free = shutil.disk_usage(p)
    except Exception:
        return {"ok": True, "free_gb": -1.0, "need_gb": 5.0, "fix": ""}
    free_gb = free / (1024 ** 3)
    need = 5.0
    return {"ok": free_gb >= need, "free_gb": round(free_gb, 1), "need_gb": need,
            "fix": "" if free_gb >= need else
                   f"磁盘只剩 {free_gb:.1f}GB，建议腾出至少 {need:.0f}GB 再用（生成很吃空间）。"}


def check_long_paths() -> Dict[str, Any]:
    """长路径支持（素材多的项目路径很容易超 260 字符）。"""
    enabled = 0
    try:
        import winreg
        v = _reg_read(winreg.HKEY_LOCAL_MACHINE,
                      r"SYSTEM\CurrentControlSet\Control\FileSystem",
                      "LongPathsEnabled")
        enabled = int(v or 0)
    except Exception:
        enabled = 0
    return {"ok": bool(enabled), "enabled": enabled,
            "fix": "" if enabled else
                   "未开启「长路径支持」（可选）：素材很多时项目路径可能超过 260 字符。"
                   "开启方式：组策略/注册表 LongPathsEnabled=1。不开启也能用。"}


def check_fonts() -> Dict[str, Any]:
    """烧字幕要中文字体（雅黑是 Win7+ 自带，正常不会缺）。"""
    cands = [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhbd.ttc",
             r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\simsun.ttc"]
    found = [p for p in cands if os.path.exists(p)]
    return {"ok": bool(found), "found": [os.path.basename(p) for p in found],
            "fix": "" if found else "没有找到中文字体，烧字幕可能显示方块。"}


def check_ffmpeg() -> Dict[str, Any]:
    """FFmpeg 是否可用（正常情况用打包自带的，不依赖系统装没装）。"""
    path, ver = "", ""
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
        path = get_ffmpeg_exe() or ""
    except Exception:
        path = ""
    if path and os.path.exists(path):
        try:
            r = subprocess.run([path, "-version"], capture_output=True, text=True,
                               timeout=25, encoding="utf-8", errors="replace")
            ver = (r.stdout or "").splitlines()[0] if r.stdout else ""
        except Exception as e:
            ver = f"跑不起来：{type(e).__name__}"
    ok = bool(path and os.path.exists(path) and "ffmpeg version" in ver.lower())
    return {"ok": ok, "path": path, "version": ver[:80],
            "fix": "" if ok else "内置 FFmpeg 不可用 —— 程序会退回内置渲染路径；"
                                 "如仍失败请反馈日志。"}


def report(data_dir: str = "") -> Dict[str, Any]:
    """一次跑完全部检查，返回给界面/接口用的报告。"""
    checks = {
        "webview2": check_webview2(),
        "vcruntime": check_vcruntime(),
        "dotnet": check_dotnet(),
        "data_dir": check_data_dir(data_dir),
        "disk": check_disk(),
        "long_paths": check_long_paths(),
        "fonts": check_fonts(),
        "ffmpeg": check_ffmpeg(),
    }
    # 硬问题（会真的挡住使用）与建议分开：aarch64/新机常见的是 webview2/vc++
    blocking = [k for k, v in checks.items()
                if not v.get("ok") and k in ("vcruntime", "data_dir", "ffmpeg")]
    hints = [f"[{k}] {v.get('fix')}" for k, v in checks.items()
             if not v.get("ok") and v.get("fix")]
    return {"ok": not blocking,
            "blocking": blocking,
            "frozen": bool(getattr(sys, "frozen", False)),
            "python": sys.version.split()[0],
            "checks": checks,
            "hints": hints,
            "summary": ("环境自检通过：这台电脑可以直接用。"
                        if not hints else
                        f"环境自检发现 {len(hints)} 项需要注意（其中 {len(blocking)} 项会挡使用）")}
