# -*- coding: utf-8 -*-
"""
VideoForge · 桌面启动器（重建版 v1.1）

面向「干净电脑」的健壮启动流程：
  0. 环境自检（Windows 版本 / WebView2 Runtime / .NET Framework / 数据目录可写性）
  1. 数据目录 = %LOCALAPPDATA%\\VideoForge\\data（可用 VIDEOFORGE_DATA_DIR 覆盖）
  2. 选空闲端口启动 FastAPI 后端，并等待就绪
  3. 打开界面：优先 pywebview（WebView2 桌面窗口）；
     缺 WebView2 或窗口创建失败 → 自动降级为「默认浏览器」模式（功能一致，不闪退）
  4. 全程日志：~/.videoforge/videoforge.log（windowed 模式下 stdout/stderr 也重定向到文件）

用法：
  VideoForge.exe                 # 桌面窗口（缺 WebView2 自动降级浏览器）
  VideoForge.exe --browser       # 强制浏览器模式
  VideoForge.exe --port 8899     # 固定端口
  VideoForge.exe --diagnose      # 只做环境自检并输出报告（不启动）
"""
import argparse
import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

# ── 依赖清单（显式 import，确保 PyInstaller 静态分析时把后端依赖一并打入）──
import fastapi          # noqa: F401
import uvicorn          # noqa: F401
import pydantic         # noqa: F401
import httpx            # noqa: F401
import yaml             # noqa: F401
import PIL              # noqa: F401  (Pillow)
import numpy            # noqa: F401
import multipart        # noqa: F401  (python-multipart)
import sse_starlette    # noqa: F401

APP_NAME = "VideoForge"
APP_VERSION = "1.2.0"

# 打包（PyInstaller）后资源解压在 sys._MEIPASS；开发模式用脚本所在目录
if getattr(sys, "frozen", False):
    HERE = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
else:
    HERE = Path(__file__).resolve().parent
BACKEND_DIR = HERE / "backend"
FRONTEND_DIR = HERE / "frontend"
LOG_DIR = Path.home() / ".videoforge"

# ── 环境要求 ──
MIN_WINDOWS_BUILD = 17763          # Windows 10 1809（Python 3.14 最低要求）
WEBVIEW2_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_DOWNLOAD = "https://developer.microsoft.com/microsoft-edge/webview2/"


# ──────────── 日志 ────────────
def _redirect_std_streams() -> None:
    """打包（--windowed）模式下 sys.stdout/stderr 为 None，
    会导致 uvicorn/日志/子进程写入失败甚至静默卡死 —— 重定向到日志文件。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if sys.stdout is None:
            sys.stdout = open(LOG_DIR / "stdout.log", "a", encoding="utf-8", buffering=1)
        if sys.stderr is None:
            sys.stderr = open(LOG_DIR / "stderr.log", "a", encoding="utf-8", buffering=1)
    except Exception:
        pass


def setup_logging(verbose: bool = False) -> logging.Logger:
    _redirect_std_streams()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handlers = [logging.FileHandler(LOG_DIR / "videoforge.log", encoding="utf-8")]
    try:
        if sys.stdout is not None:
            handlers.append(logging.StreamHandler(sys.stdout))
    except Exception:
        pass
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )
    log = logging.getLogger("videoforge.launcher")
    log.info("=" * 56)
    log.info("  %s Launcher v%s", APP_NAME, APP_VERSION)
    log.info("=" * 56)
    log.info("Python: %s | Platform: %s | Frozen: %s",
             sys.version.split()[0], sys.platform, getattr(sys, "frozen", False))
    log.info("Executable: %s", sys.executable)
    log.info("Log file: %s", LOG_DIR / "videoforge.log")
    return log


# ──────────── 环境自检 ────────────
def windows_build() -> int:
    try:
        return int(sys.getwindowsversion().build)
    except Exception:
        return 0


def check_webview2() -> bool:
    """检测 WebView2 Runtime（系统级 HKLM / 用户级 HKCU 两种安装位置）"""
    if sys.platform != "win32":
        return True
    try:
        import winreg
    except Exception:
        return True
    candidates = (
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}"),
        (winreg.HKEY_CURRENT_USER, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}"),
    )
    for root, path in candidates:
        try:
            with winreg.OpenKey(root, path) as key:
                ver, _ = winreg.QueryValueEx(key, "pv")
                if ver and str(ver) not in ("0.0.0.0", ""):
                    return True
        except OSError:
            continue
    return False


def check_dotnet_release() -> int:
    """返回 .NET Framework Release 号（0 = 未检测到；528040+ ≈ 4.8）"""
    if sys.platform != "win32":
        return 0
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full") as key:
            rel, _ = winreg.QueryValueEx(key, "Release")
            return int(rel)
    except Exception:
        return 0


def alert(title: str, text: str, icon: int = 0x40) -> None:
    """无控制台（--windowed）环境下用系统消息框提示用户（icon: 0x40 信息 / 0x30 警告 / 0x10 错误）"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, title, icon)
    except Exception:
        pass


def environment_report() -> dict:
    build = windows_build()
    wv2 = check_webview2()
    rel = check_dotnet_release()
    return {
        "windows_build": build,
        "windows_ok": (build == 0) or (build >= MIN_WINDOWS_BUILD),
        "webview2": wv2,
        "dotnet_release": rel,
        "dotnet_ok": rel == 0 or rel >= 461808,     # 461808 ≈ 4.7.2（pythonnet/WinForms 需要）
    }


# ──────────── 端口 / 数据目录 ────────────
def pick_port(preferred: int = 0) -> int:
    if preferred:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", preferred))
                return preferred
            except OSError:
                pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def resolve_data_dir(log: logging.Logger) -> Path:
    """确定数据目录：优先环境变量 → %LOCALAPPDATA% → 用户主目录 → 临时目录（保证可写）"""
    candidates = []
    env = os.environ.get("VIDEOFORGE_DATA_DIR")
    if env:
        candidates.append(Path(env))
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        candidates.append(Path(base) / APP_NAME / "data")
    candidates.append(Path.home() / f".{APP_NAME.lower()}" / "data")
    candidates.append(Path(os.environ.get("TEMP", ".")) / f"{APP_NAME}-data")

    for d in candidates:
        try:
            (d / "outputs").mkdir(parents=True, exist_ok=True)
            (d / "cache").mkdir(parents=True, exist_ok=True)
            probe = d / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return d
        except Exception as e:
            log.warning("数据目录不可用（%s）: %s", d, e)
            continue
    raise RuntimeError("找不到可写的数据目录")


def wait_for_backend(port: int, timeout: float = 90.0) -> bool:
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def keep_alive() -> int:
    """保持进程存活（浏览器模式下后端需持续运行）"""
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


def open_in_browser(url: str, log: logging.Logger, reason: str = "") -> int:
    if reason:
        log.warning("降级为浏览器模式：%s", reason)
    try:
        webbrowser.open(url)
    except Exception as e:
        log.error("无法自动打开浏览器（%s），请手动访问：%s", e, url)
        alert(f"{APP_NAME} 已启动",
              f"界面已就绪，请手动在浏览器打开：\n\n{url}", 0x40)
    log.info("浏览器模式运行中（关闭本进程即退出）")
    return keep_alive()


# ──────────── 主流程 ────────────
def main() -> int:
    ap = argparse.ArgumentParser(description=f"{APP_NAME} 桌面启动器 v{APP_VERSION}")
    ap.add_argument("--port", type=int, default=0, help="固定端口（0=自动选择）")
    ap.add_argument("--browser", action="store_true", help="强制用默认浏览器打开")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--diagnose", action="store_true", help="只输出环境自检报告")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    log = setup_logging(args.verbose)

    # ── 0. 环境自检 ──
    env = environment_report()
    log.info("环境自检：Windows build=%s（要求≥%s，%s）| WebView2=%s | .NET Release=%s",
             env["windows_build"], MIN_WINDOWS_BUILD,
             "OK" if env["windows_ok"] else "偏低",
             "已安装" if env["webview2"] else "缺失",
             env["dotnet_release"] or "未检测到")
    if args.diagnose:
        print(f"{APP_NAME} 环境自检报告")
        print(f"  程序版本      : {APP_VERSION}")
        print(f"  Python        : {sys.version.split()[0]}  (Frozen={getattr(sys, 'frozen', False)})")
        print(f"  Windows build : {env['windows_build']}  (最低要求 {MIN_WINDOWS_BUILD})  -> {'OK' if env['windows_ok'] else '版本偏低'}")
        print(f"  WebView2      : {'已安装' if env['webview2'] else '缺失（将使用浏览器模式）'}")
        print(f"  .NET Framework: Release={env['dotnet_release']}  -> {'OK' if env['dotnet_ok'] else '偏低（桌面窗口可能不可用）'}")
        print(f"  日志目录      : {LOG_DIR}")
        return 0

    if not env["windows_ok"]:
        log.warning("Windows 版本偏低（build %s < %s），可能无法运行", env["windows_build"], MIN_WINDOWS_BUILD)
        alert(f"{APP_NAME} 环境提示",
              f"检测到系统版本偏低（build {env['windows_build']}）。\n"
              f"本程序需要 Windows 10 1809（build {MIN_WINDOWS_BUILD}）或更高版本。\n\n"
              f"程序将继续尝试启动，可能出现异常。", 0x30)

    # ── 1. 数据目录 ──
    try:
        data_dir = resolve_data_dir(log)
    except Exception as e:
        log.exception("数据目录初始化失败: %s", e)
        alert(f"{APP_NAME} 启动失败", f"无法创建数据目录：\n{e}\n\n请检查磁盘空间与权限。", 0x10)
        return 1
    os.environ["VIDEOFORGE_DATA_DIR"] = str(data_dir)
    log.info("Data dir: %s", data_dir)

    frozen = getattr(sys, "frozen", False)
    if not frozen and not BACKEND_DIR.exists():
        log.error("找不到 backend 目录: %s", BACKEND_DIR)
        return 1

    port = pick_port(args.port)
    os.environ["VIDEOFORGE_PORT"] = str(port)
    os.environ["VIDEOFORGE_HOST"] = args.host
    log.info("Port: %s", port)

    # ── 2. 加载并启动后端 ──
    def import_backend_app():
        if not frozen:
            sys.path.insert(0, str(BACKEND_DIR))
            os.chdir(BACKEND_DIR)
        import main as backend_main  # noqa: E402
        return backend_main.app

    import uvicorn
    try:
        app = import_backend_app()
    except Exception as e:
        log.exception("后端加载失败: %s", e)
        alert(f"{APP_NAME} 启动失败",
              f"后端模块加载失败：\n{e}\n\n日志：{LOG_DIR / 'videoforge.log'}", 0x10)
        return 1

    def serve():
        try:
            uvicorn.run(app, host=args.host, port=port, log_level="info")
        except Exception as e:  # pragma: no cover
            log.exception("后端异常退出: %s", e)

    threading.Thread(target=serve, daemon=True, name="videoforge-backend").start()
    log.info("Starting backend... waiting for readiness (max 90s)")

    if not wait_for_backend(port):
        log.error("后端未在预期时间内就绪")
        alert(f"{APP_NAME} 启动失败",
              f"后端未能在 90 秒内就绪。\n\n请查看日志：\n{LOG_DIR / 'videoforge.log'}\n\n"
              f"（可能是端口被占用或安全软件拦截）", 0x10)
        return 1
    log.info("Backend ready on port %s", port)

    url = f"http://{args.host}:{port}/"
    # 开发/自检用：VIDEOFORGE_START_QUERY 可附加查询串（如 selftest=batch3&view=chars）
    _extra = (os.environ.get("VIDEOFORGE_START_QUERY") or "").strip().lstrip("?")
    if _extra:
        url += "?" + _extra
    log.info("Opening %s", url)

    # ── 3. 打开界面（窗口优先，失败自动降级）──
    if args.browser:
        return open_in_browser(url, log, "指定了 --browser")

    if not env["webview2"]:
        alert(f"{APP_NAME} 提示",
              "未检测到 WebView2 运行库，将改用【浏览器模式】打开界面"
              "（功能完全一致，只是窗口形式不同）。\n\n"
              "如需桌面窗口体验，可安装 WebView2 Runtime（免费，微软官方）：\n"
              f"{WEBVIEW2_DOWNLOAD}\n\n"
              "安装后重新启动本程序即可。", 0x40)
        return open_in_browser(url, log, "未检测到 WebView2 Runtime")

    try:
        import webview
    except Exception as e:
        return open_in_browser(url, log, f"pywebview 不可用：{e}")

    try:
        log.info("Opening pywebview window at %s", url)
        webview.create_window(
            f"{APP_NAME} · 私人 AI 短片 Agent",
            url,
            width=1440,
            height=900,
            min_size=(1100, 700),
            text_select=True,
        )
        webview.start()
        log.info("窗口已关闭，退出")
        return 0
    except Exception as e:
        # 缺 WebView2 / WebView2 初始化失败 / .NET 缺失 都会走到这里 —— 不能闪退，降级浏览器
        log.exception("桌面窗口启动失败: %s", e)
        alert(f"{APP_NAME} 提示",
              f"桌面窗口启动失败，已自动改用【浏览器模式】：\n\n{e}\n\n"
              f"如需桌面窗口，请安装 WebView2 Runtime：\n{WEBVIEW2_DOWNLOAD}", 0x30)
        return open_in_browser(url, log, f"窗口启动失败：{e}")


if __name__ == "__main__":
    raise SystemExit(main())
