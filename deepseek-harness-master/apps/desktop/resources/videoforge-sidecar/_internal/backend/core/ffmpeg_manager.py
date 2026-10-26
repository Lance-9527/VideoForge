"""
VideoForge · FFmpeg 智能获取

优先级：
1. 项目 bin/ffmpeg.exe（PyInstaller 打包后内置）
2. 系统 PATH 中的 ffmpeg
3. imageio-ffmpeg 包自带
4. 自动下载到 bin/
"""

import os
import sys
import shutil
import logging
import platform
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Optional


logger = logging.getLogger("videoforge.ffmpeg")


@lru_cache(maxsize=1)
def find_or_install_ffmpeg() -> Optional[str]:
    """智能获取 FFmpeg，返回可执行路径；找不到则返回 None

    ★ 单进程内只探测一次（lru_cache）。hot path 反复调用会导致：
      - shutil.which("ffmpeg") 在 Windows spawn `where.exe`（每次 ~20-30ms）
      - imageio_ffmpeg.get_ffmpeg_exe() 内部解压 + ffmpeg -version 验证
      实测 10:20:10 这 1 秒内 200Hz 触发 = 单核 CPU 跑满 + DWM 渲染抖动
      → 闪屏抽搐 + asyncio pipe ConnectionResetError (WinError 10054)
      → 显卡驱动 TDR → LOL 黑屏（2026-09-14 真机现象）。

    改了 ffmpeg_path 设置或探测结果失效，调 clear_ffmpeg_cache() 让缓存失效。
    """
    # 1. 项目 bin/ 目录（最优先）
    bundle_dir = _get_bundle_dir()
    if bundle_dir:
        local_ffmpeg = bundle_dir / "bin" / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
        if local_ffmpeg.exists() and local_ffmpeg.stat().st_size > 1024 * 1024:  # 至少 1MB
            logger.info(f"FFmpeg found (project bin): {local_ffmpeg}")
            return str(local_ffmpeg)

    # 2. 系统 PATH
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        logger.info(f"FFmpeg found (PATH): {system_ffmpeg}")
        return system_ffmpeg

    # 3. imageio-ffmpeg 包
    try:
        import imageio_ffmpeg
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        if os.path.exists(ffmpeg_exe):
            logger.info(f"FFmpeg found (imageio-ffmpeg): {ffmpeg_exe}")
            return ffmpeg_exe
    except ImportError:
        logger.warning("imageio-ffmpeg not installed")

    # 4. 自动下载到 bin/
    if bundle_dir:
        target = bundle_dir / "bin" / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
        try:
            return _download_ffmpeg(target)
        except Exception as e:
            logger.error(f"Auto-download FFmpeg failed: {e}")

    return None


def _get_bundle_dir() -> Optional[Path]:
    """获取应用根目录（开发/打包兼容）"""
    if getattr(sys, "frozen", False):
        # PyInstaller 打包后
        return Path(os.path.dirname(sys.executable)).resolve()
    else:
        # 开发模式
        return Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))).resolve()


def _download_ffmpeg(target: Path) -> Optional[str]:
    """下载 FFmpeg 到指定位置（仅 Windows 用 gyan.dev 版本）"""
    target.parent.mkdir(parents=True, exist_ok=True)

    # 先尝试用 imageio-ffmpeg（pip 包内置 80+ MB 的 binary）
    try:
        import imageio_ffmpeg
        src = imageio_ffmpeg.get_ffmpeg_exe()
        if os.path.exists(src):
            logger.info(f"Copying FFmpeg from imageio-ffmpeg: {src} → {target}")
            shutil.copy2(src, target)
            if target.exists():
                return str(target)
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"imageio-ffmpeg copy failed: {e}")

    # Windows: 从 gyan.dev 下载 essentials build
    if sys.platform == "win32":
        return _download_ffmpeg_windows(target)

    # macOS / Linux: 引导用户用 brew / apt
    logger.error("请安装 ffmpeg: macOS  brew install ffmpeg  |  Ubuntu  sudo apt install ffmpeg")
    return None


def _download_ffmpeg_windows(target: Path) -> Optional[str]:
    """从 gyan.dev 下载 Windows FFmpeg essentials build"""
    import urllib.request
    import zipfile
    import tempfile

    # essentials build (~80 MB)
    url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

    logger.info(f"Downloading FFmpeg from {url}...")
    logger.info("This may take 1-2 minutes (~80 MB)")

    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = Path(tmpdir) / "ffmpeg.zip"
        try:
            # 下载（带进度）
            urllib.request.urlretrieve(url, str(zip_path))
        except Exception as e:
            logger.error(f"Download failed: {e}")
            return None

        logger.info("Extracting FFmpeg...")
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                # 找 ffmpeg.exe
                for name in zf.namelist():
                    if name.endswith("ffmpeg.exe") and "bin/" in name:
                        with zf.open(name) as src, open(target, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        logger.info(f"FFmpeg extracted to {target}")
                        return str(target)
        except Exception as e:
            logger.error(f"Extract failed: {e}")
            return None

    return None


def check_ffmpeg_via_process(ffmpeg_path: str) -> bool:
    """用 ffmpeg -version 验证可执行。

    ⚠ 必须用**清理过的环境 + 不弹窗**来验证：打包后 PyInstaller 的 `_internal`
    会带一套 Python 用的 VC 运行库，子进程按 PATH 捡到就会 0xc0000142
    （用户实际看到过 "ffmpeg-win-x86_64-v7.1.exe - Application Error"）。
    """
    if not ffmpeg_path or not os.path.exists(ffmpeg_path):
        # 允许是 PATH 上的裸命令名
        if ffmpeg_path != "ffmpeg":
            return False
    try:
        from core.proc import run_sync
        rc, out = run_sync([ffmpeg_path, "-version"], timeout=15)
        return rc == 0 and ("ffmpeg version" in out or bool(out.strip()))
    except Exception:
        return False


# 记住"哪个 ffmpeg 能用"，避免每次都 spawn 一次
_FFMPEG_OK: dict = {}


def _verify_cached(path: str) -> bool:
    if path in _FFMPEG_OK:
        return _FFMPEG_OK[path]
    ok = check_ffmpeg_via_process(path)
    _FFMPEG_OK[path] = ok
    if not ok:
        logger.warning("FFmpeg 自检失败（该二进制在本机/打包环境下不可用）：%s", path)
    return ok


@lru_cache(maxsize=1)
def get_ffmpeg_for_postprocess() -> str:
    """获取**经自检可用**的 FFmpeg 路径。

    顺序：探测到的（程序内 / imageio-ffmpeg）→ PATH 上的 ffmpeg → 明确报错。
    只要有一个能跑起来就用它，避免把 0xc0000142 的坏二进制一路传下去。

    ★ 单进程内只探测一次（lru_cache）。hot path 上多次调用会反复触发
    find_or_install_ffmpeg() 的 4 步探测（spawn where.exe + 解压 imageio），
    与 find_or_install_ffmpeg() 同因导致闪屏/黑屏。改设置后调 clear_ffmpeg_cache()。
    """
    candidates = []
    try:
        found = find_or_install_ffmpeg()
        if found:
            candidates.append(found)
    except Exception as e:
        logger.warning("find_or_install_ffmpeg 失败：%s", e)

    # imageio-ffmpeg 直接取（不依赖前面的探测逻辑）
    try:
        import imageio_ffmpeg
        candidates.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        pass

    import shutil as _sh
    which = _sh.which("ffmpeg")
    if which:
        candidates.append(which)

    seen = set()
    for c in candidates:
        if not c or c in seen:
            continue
        seen.add(c)
        if _verify_cached(c):
            return c

    logger.error("没有找到任何可用的 FFmpeg（候选：%s）", list(seen))
    return "ffmpeg"  # 最后兜底：让调用方报错时给出可读提示


def download_ffmpeg_blocking(target_dir: Path, progress_callback=None) -> Path:
    """同步下载 FFmpeg（带进度回调，用于 UI 显示）"""
    import urllib.request

    target = target_dir / "ffmpeg.exe"
    url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

    if progress_callback:
        progress_callback(0.0, "开始下载 FFmpeg...")

    # 先用 imageio-ffmpeg（更快）
    try:
        import imageio_ffmpeg
        src = imageio_ffmpeg.get_ffmpeg_exe()
        if os.path.exists(src):
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            if target.exists():
                if progress_callback:
                    progress_callback(1.0, "完成")
                return target
    except ImportError:
        pass

    # 下载
    if progress_callback:
        progress_callback(0.1, f"下载中... (~80 MB)")

    import tempfile
    import zipfile

    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = Path(tmpdir) / "ffmpeg.zip"
        urllib.request.urlretrieve(url, str(zip_path))

        if progress_callback:
            progress_callback(0.85, "解压中...")

        with zipfile.ZipFile(zip_path, "r") as zf:
            for name in zf.namelist():
                if name.endswith("ffmpeg.exe") and "bin/" in name:
                    with zf.open(name) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    break

        if progress_callback:
            progress_callback(1.0, "完成")
        return target


def clear_ffmpeg_cache() -> None:
    """手动清缓存。场景：

    - 用户在设置里改了 ffmpeg_path，需要重新探测
    - main.py L191-196 health 兜底探测到 alt 路径后写回配置
    - 测试场景验证 lru_cache 行为

    必须同时清 find_or_install_ffmpeg() 和 get_ffmpeg_for_postprocess()
    两个缓存，以及 _FFMPEG_OK 自检缓存（find_or_install_ffmpeg()
    内部 imageio_ffmpeg.get_ffmpeg_exe() 也 spawn 过 ffmpeg -version 验证）。
    """
    find_or_install_ffmpeg.cache_clear()
    get_ffmpeg_for_postprocess.cache_clear()
    _FFMPEG_OK.clear()
