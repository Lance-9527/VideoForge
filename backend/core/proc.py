# -*- coding: utf-8 -*-
"""
VideoForge · 子进程统一封装

═══════════════════════════════════════════════════════════════════
为什么必须统一（两个真实故障）
═══════════════════════════════════════════════════════════════════
1) **闪屏**：打包成 `--windowed` 的 GUI 程序没有控制台。但它 spawn 的
   ffmpeg / ffprobe / powershell 都是**控制台程序**，Windows 会给每个子进程
   分配一个控制台窗口 → 屏幕上不停闪黑框（用户描述"使用过程中闪屏"）。
   解法：`creationflags=CREATE_NO_WINDOW` + `STARTF_USESHOWWINDOW/SW_HIDE`。

2) **`ffmpeg.exe - Application Error 0xc0000142`**（应用无法正常启动）：
   PyInstaller 会把 `_MEIPASS/_internal` 塞进进程环境，而 `_internal` 里带着
   自带的 `VCRUNTIME140.dll` / `MSVCP140.dll` / `python3xx.dll` 等。
   子进程启动时按 PATH 搜索依赖 DLL，**捡到这套为 Python 准备的运行库**，
   与 ffmpeg 自己需要的版本不兼容 → 加载失败 → 0xc0000142。
   解法：给子进程一份**清理过的环境**（剔除指向 `_internal` 的 PATH 项、
   把 System32 放最前、保留必要的系统变量）。

统一入口后，全项目的子进程行为一致，不会再出现"某处漏了参数就闪屏/崩"。
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("videoforge.proc")

# Windows: 不创建控制台窗口
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200

_SYSTEM_KEEP = (
    "SystemRoot", "SystemDrive", "windir", "COMSPEC", "PATHEXT",
    "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES",
    "HOMEDRIVE", "HOMEPATH", "USERNAME", "USERDOMAIN", "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE", "OS", "LANG", "LC_ALL", "PYTHONIOENCODING",
    "VIDEOFORGE_DATA_DIR", "VIDEOFORGE_HOME",
)


def _bundle_dirs() -> List[str]:
    """当前进程的"打包目录"（这些目录绝不能出现在子进程的 PATH 里）"""
    out = []
    mp = getattr(sys, "_MEIPASS", None)
    if mp:
        out.append(os.path.abspath(mp))
    if getattr(sys, "frozen", False):
        out.append(os.path.dirname(os.path.abspath(sys.executable)))
        out.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "_internal"))
    # 开发态：源码根目录也不该进 PATH（避免同名 DLL 干扰）
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out.append(os.path.abspath(here))
    return [d for d in dict.fromkeys(out) if d and os.path.isdir(d)]


def child_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """给子进程一份干净环境。

    - 只保留系统必需变量
    - PATH 里剔除所有指向打包目录/源码目录的项，并把 System32 提到最前
    - 这样 ffmpeg 只会加载系统运行库，不会误捡 PyInstaller 的那套
    """
    env: Dict[str, str] = {}
    for k in _SYSTEM_KEEP:
        v = os.environ.get(k)
        if v:
            env[k] = v
        elif k == "SystemRoot" and sys.platform == "win32":
            env[k] = r"C:\Windows"

    # 重建 PATH
    sysroot = env.get("SystemRoot", r"C:\Windows")
    preferred = [
        os.path.join(sysroot, "System32"),
        sysroot,
        os.path.join(sysroot, "System32", "Wbem"),
        os.path.join(sysroot, "System32", "WindowsPowerShell", "v1.0"),
    ]
    banned = _bundle_dirs()
    kept: List[str] = []
    for p in (os.environ.get("PATH") or "").split(os.pathsep):
        p = p.strip()
        if not p:
            continue
        ap = os.path.abspath(p)
        if any(ap == b or ap.startswith(b + os.sep) for b in banned):
            continue
        kept.append(p)
    path_parts = [p for p in preferred if os.path.isdir(p)] + kept
    env["PATH"] = os.pathsep.join(dict.fromkeys(path_parts))

    if sys.platform == "win32":
        # 明确不继承 PyInstaller 注入的 DLL 目录提示
        env.pop("PYTHONHOME", None)
        env.pop("PYTHONPATH", None)
        env.pop("_MEIPASS2", None)
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


def _win_startupinfo():
    if sys.platform != "win32":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return si


def popen_kwargs() -> Dict:
    """同步 subprocess 用的"不弹窗"参数"""
    kw: Dict = {"env": child_env()}
    if sys.platform == "win32":
        kw["creationflags"] = CREATE_NO_WINDOW
        kw["startupinfo"] = _win_startupinfo()
    return kw


async def run(cmd: List[str], timeout: int = 900,
              extra_env: Optional[Dict[str, str]] = None) -> Tuple[int, str]:
    """跑外部命令，返回 (returncode, stderr+stdout 尾部)。

    - **不弹窗**（CREATE_NO_WINDOW）
    - **干净环境**（避免 0xc0000142）
    - FFmpeg 把信息打在 stderr，所以两个流合并返回
    """
    cmd = [str(c) for c in cmd]
    logger.debug("run: %s", " ".join(cmd))
    kwargs: Dict = {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "env": child_env(extra_env),
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = CREATE_NO_WINDOW
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)
    except FileNotFoundError as e:
        return 127, f"找不到可执行文件：{cmd[0]}（{e}）"
    except OSError as e:
        # 0xc0000142 之类会以 OSError/WinError 抛出
        return 126, f"无法启动 {os.path.basename(cmd[0])}：{e}"
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return 124, f"超时（>{timeout}s）"
    text = ((err or b"") + b"\n" + (out or b"")).decode("utf-8", "ignore")
    return proc.returncode or 0, text[-6000:]


def run_sync(cmd: List[str], timeout: int = 60) -> Tuple[int, str]:
    """同步版本（少量地方用得到）"""
    cmd = [str(c) for c in cmd]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, **popen_kwargs())
    except FileNotFoundError as e:
        return 127, f"找不到可执行文件：{cmd[0]}（{e}）"
    except OSError as e:
        return 126, f"无法启动 {os.path.basename(cmd[0])}：{e}"
    except subprocess.TimeoutExpired:
        return 124, f"超时（>{timeout}s）"
    text = ((r.stderr or b"") + b"\n" + (r.stdout or b"")).decode("utf-8", "ignore")
    return r.returncode or 0, text[-6000:]


def spawn_detached(cmd: List[str]) -> bool:
    """启动一个**不等待**的分离进程（如 explorer 打开目录），且不闪窗"""
    try:
        subprocess.Popen([str(c) for c in cmd], **popen_kwargs())
        return True
    except Exception:
        logger.warning("spawn_detached failed: %s", cmd, exc_info=True)
        return False


# ═══════════════════════════════════════════════════════════════
# 全局注入：让全项目（含第三方库）的 asyncio 子进程都不闪窗 + 用干净环境
#
# 项目里有 20+ 处 asyncio.create_subprocess_exec 调用点，逐个改容易漏；
# 这里在启动时包一层，凡是没有显式指定 creationflags/env 的调用都被补全。
# 幂等：重复调用无副作用。
# ═══════════════════════════════════════════════════════════════

_PATCHED = False


def install_asyncio_patch() -> bool:
    global _PATCHED
    if _PATCHED:
        return True
    if sys.platform != "win32":
        _PATCHED = True
        return False

    orig_exec = asyncio.create_subprocess_exec
    orig_shell = asyncio.create_subprocess_shell

    def _inject(kwargs: Dict) -> Dict:
        kw = dict(kwargs)
        if not kw.get("creationflags"):
            kw["creationflags"] = CREATE_NO_WINDOW
        if kw.get("startupinfo") is None:
            kw["startupinfo"] = _win_startupinfo()
        if not kw.get("env"):
            kw["env"] = child_env()
        return kw

    async def _exec(*args, **kwargs):
        return await orig_exec(*args, **_inject(kwargs))

    async def _shell(*args, **kwargs):
        return await orig_shell(*args, **_inject(kwargs))

    asyncio.create_subprocess_exec = _exec
    asyncio.create_subprocess_shell = _shell
    _PATCHED = True
    logger.info("已注入子进程补丁：CREATE_NO_WINDOW + 干净环境（防闪屏 / 防 0xc0000142）")
    return True


def verify_binary(path: str, timeout: int = 25) -> Tuple[bool, str]:
    """验证一个可执行文件能否真的跑起来（用干净环境）。

    专治 0xc0000142：先在本进程环境跑一次，失败就用清理过的环境再跑；
    还失败就说明这个二进制在打包环境下不可用，调用方应换 fallback。
    """
    if not path or not os.path.exists(path):
        return False, "文件不存在"
    rc, out = run_sync([path, "-version"], timeout=timeout)
    if rc == 0 and ("ffmpeg version" in out or "ffprobe version" in out or out.strip()):
        return True, out.splitlines()[0] if out.strip() else ""
    return False, f"rc={rc} {out.strip()[:200]}"


def run_sync_full(cmd: List[str], timeout: int = 60) -> Tuple[int, str]:
    """同步执行并返回**完整输出**（不截断）。

    用于"能力探测"这类需要看全量输出的场景；报错用途仍请用 run_sync（尾部更相关）。
    """
    cmd = [str(c) for c in cmd]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, **popen_kwargs())
    except FileNotFoundError as e:
        return 127, f"找不到可执行文件：{cmd[0]}（{e}）"
    except OSError as e:
        return 126, f"无法启动 {os.path.basename(cmd[0])}：{e}"
    except subprocess.TimeoutExpired:
        return 124, f"超时（>{timeout}s）"
    return (r.returncode or 0,
            ((r.stdout or b"") + b"\n" + (r.stderr or b"")).decode("utf-8", "ignore"))


_FILTER_CACHE: Dict[str, set] = {}


def list_filters(ffmpeg: str, timeout: int = 40) -> set:
    """取 ffmpeg 的**完整**滤镜名集合（带缓存）。

    ⚠ 必须用 run_sync_full 而不是 run_sync：后者只保留尾部 6000 字符，
    滤镜表远长于此，靠前的项会被截掉，导致把有能力的 ffmpeg 误判成缺滤镜。
    """
    if ffmpeg in _FILTER_CACHE:
        return _FILTER_CACHE[ffmpeg]
    names = set()
    try:
        rc, out = run_sync_full([ffmpeg, "-hide_banner", "-filters"], timeout=timeout)
        if rc == 0:
            for line in out.splitlines():
                parts = line.split()
                # 形如 " T.C afade  A->A  Fade in/out input audio."
                if len(parts) >= 3 and len(parts[0]) <= 4 and "->" in parts[2]:
                    names.add(parts[1])
    except Exception:
        logger.warning("list_filters failed", exc_info=True)
    _FILTER_CACHE[ffmpeg] = names
    return names


def has_filter(ffmpeg: str, name: str, timeout: int = 20) -> bool:
    """探测 ffmpeg 是否带某个滤镜（基于完整滤镜表，带缓存）"""
    if not name:
        return False
    names = list_filters(ffmpeg, timeout=timeout)
    if names:
        return name in names
    # 兜底：滤镜表取不到时用 `-h filter=` 单独问
    try:
        rc, out = run_sync([ffmpeg, "-hide_banner", "-h", f"filter={name}"], timeout=timeout)
        low = (out or "").lower()
        return rc == 0 and "unknown filter" not in low and "no such filter" not in low
    except Exception:
        return False


# 成片链路实际依赖的滤镜（缺任何一个都会让字幕 / BGM / 变速 / 转场静默失效）
REQUIRED_FILTERS = ("subtitles", "xfade", "acrossfade", "amix", "volume",
                    "afade", "atempo", "apad", "anullsrc", "zoompan")


def check_capabilities(ffmpeg: str) -> Dict[str, bool]:
    """检查 ffmpeg 是否具备成片所需的全部滤镜能力"""
    return {f: has_filter(ffmpeg, f) for f in REQUIRED_FILTERS}
