# -*- coding: utf-8 -*-
"""可写技能环境（skillenv）＋ 环境体检。

★★ 为什么有这个模块（2026-10-25，客户刚需）
    安装版后端是 PyInstaller **冻结包**：没有 pip，而且打包时为了体积**主动裁掉**
    了 cv2 / pandas / numba / cryptography / llvmlite 等依赖（见
    `apps/desktop/scripts/build-videoforge-sidecar.mjs` 的 CUT_DIRECTORIES）。
    后果：客户自己蒸馏出来的 skill，只要 `import cv2` 就 ImportError；
    而旧代码还在提示「pip install opencv-python-headless」——在冻结包里**根本装不了**。
    这正是客户说的「装依赖很麻烦 / skill 到别的平台要降级」。

    本模块给出一条**真的能走通**的路：
      1) 在用户数据目录下开一个可写扩展目录 `<data>/skillenv/`；
      2) 后端启动时把 `<data>/skillenv/python` 注入 `sys.path`、
         `<data>/skillenv/bin` 注入 `PATH`；
      3) 提供如实报告「缺哪个包、怎么补」的体检数据（`/api/env`）；
      4) 支持**离线安装 wheel**（`install_wheel`）：把 `.whl` 解压进
         `skillenv/python`，不需要联网、不需要 pip。

    目录约定（全部由本模块创建）::

        <data>/skillenv/
            python/   ← 纯 Python 包直接解压到这里（会被加进 sys.path）
            bin/      ← 放可执行文件（ffmpeg 等），会被加进 PATH
            wheels/   ← 把 .whl 放这里方便排查/离线安装

    ⚠ 边界：只解决「能 import、能调用」。带编译扩展的包必须与**当前解释器 ABI**
      匹配（本产品冻结版是 CPython 3.14）；ABI 不匹配的 wheel 解压后依然 import
      失败，体检接口会把它标成 missing，而不是假装成功。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

ENV_DIRNAME = "skillenv"
_SUBDIRS = ("python", "bin", "wheels")

#: 客户自蒸馏 skill 最常缺的一批库。
#: (import 名, pip 包名, 用途) —— 顺序按「踩坑概率」排，和打包裁剪清单对齐。
WATCH_MODULES: List[tuple] = [
    ("cv2", "opencv-python-headless", "图像/视频检测"),
    ("pandas", "pandas", "表格/CSV 处理"),
    ("numba", "numba", "数值加速（依赖 llvmlite）"),
    ("llvmlite", "llvmlite", "numba 的底层 LLVM 绑定"),
    ("cryptography", "cryptography", "加密/签名"),
    ("scipy", "scipy", "科学计算"),
    ("moviepy", "moviepy", "视频剪辑"),
    ("soundfile", "soundfile", "音频读写"),
    ("librosa", "librosa", "音频分析"),
    ("onnxruntime", "onnxruntime", "本地推理"),
    ("pyworld", "pyworld", "音色/变声"),
    ("insightface", "insightface", "人脸"),
    ("numpy", "numpy", "数值基础（随包自带）"),
    ("PIL", "Pillow", "图像基础（随包自带）"),
]

#: 由 `activate()` 记下的状态；`diagnose()` 用它判断「到底注入了没有」。
_ACTIVE: Dict[str, Any] = {}


def resolve_skillenv(data_dir: str | os.PathLike[str]) -> Path:
    """扩展目录的**唯一真相源**：`<data>/skillenv`。

    允许用环境变量覆盖，方便测试与多环境并排：
    `VIDEOFORGE_SKILLENV=/some/dir`。
    """
    override = (os.environ.get("VIDEOFORGE_SKILLENV") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (Path(data_dir).expanduser().resolve() / ENV_DIRNAME)


def _ensure_layout(root: Path) -> Dict[str, bool]:
    """建好 python/bin/wheels 三个子目录；逐个报告可写性。"""
    created: Dict[str, bool] = {}
    for name in _SUBDIRS:
        d = root / name
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            created[name] = True
        except Exception:
            created[name] = False
    return created


def _install_numpy_compat_aliases() -> None:
    """给第三方二进制扩展补上 numpy 2.x 的旧路径别名。

    背景（2026-10-25 本机实测）：numpy 2.x 的实现搬到 `numpy/_core`，只留
    `numpy/core` 兼容 shim。打包环境里 shim 可能没被带上，于是像 cv2 5.0.0 这类
    扩展会 `import numpy.core.multiarray` → ModuleNotFoundError。

    这里在主进程里把 `numpy._core.<sub>` 投影到 `numpy.core.<sub>`：
      * 不改磁盘、不改 numpy 本身；只补 `sys.modules` 与父包属性；
      * 只对已存在且能导入的模块生效，失败静默（体检/探测会如实报错）。
    幂等，可重复调用。
    """
    try:
        import importlib
        import numpy  # noqa: F401  —— 确保 numpy 已加载
    except Exception:
        return
    try:
        legacy = importlib.import_module("numpy.core")
    except Exception:
        legacy = None
    for sub in ("multiarray", "umath", "numeric", "numerictypes", "fromnumeric",
                "shape_base", "function_base", "_multiarray_umath", "_dtype",
                "_dtype_ctypes", "records", "einsumfunc", "getlimits", "arrayprint"):
        try:
            module = importlib.import_module(f"numpy._core.{sub}")
        except Exception:
            continue
        alias = f"numpy.core.{sub}"
        if alias not in sys.modules:
            sys.modules[alias] = module
        if legacy is not None:
            try:
                setattr(legacy, sub, module)
            except Exception:
                pass


def activate(data_dir: str | os.PathLike[str]) -> Dict[str, Any]:
    """把可写扩展目录注入运行环境。幂等，可重复调用。

    * `<root>/python` → `sys.path` 最前面（用户的包优先于随包依赖，便于覆盖版本）
    * `<root>/bin`    → `PATH`（ffmpeg、小工具等）
    * 同时设置 `VIDEOFORGE_SKILLENV`，子进程（mcp server / ffmpeg 包装）能继承

    返回一份可日志化的状态字典；**不抛异常**（环境扩展失败不该让后端起不来）。
    """
    info: Dict[str, Any] = {"ok": False, "path": "", "python_path": "", "bin_path": "",
                            "created": {}, "writable": {}, "error": ""}
    try:
        root = resolve_skillenv(data_dir)
        created = _ensure_layout(root)
        py_dir, bin_dir = root / "python", root / "bin"
        info["created"] = created
        info["writable"] = created

        py_str = str(py_dir)
        if py_str not in sys.path:
            sys.path.insert(0, py_str)
        bin_str = str(bin_dir)
        path_items = [p for p in (os.environ.get("PATH") or "").split(os.pathsep) if p]
        if bin_str not in path_items:
            os.environ["PATH"] = bin_str + os.pathsep + (os.environ.get("PATH") or "")
        os.environ["VIDEOFORGE_SKILLENV"] = str(root)

        importlib.invalidate_caches()
        # numpy 2.x 的旧路径别名（cv2 等二进制扩展会 import numpy.core.multiarray）
        _install_numpy_compat_aliases()
        info.update(ok=True, path=str(root), python_path=py_str, bin_path=bin_str)
        _ACTIVE.clear()
        _ACTIVE.update(info)
    except Exception as exc:  # noqa: BLE001
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def _probe(module: str) -> bool:
    """只查「找不找得到」，不执行包（避免 cv2 这类重包拖慢体检）。"""
    try:
        return importlib.util.find_spec(module) is not None
    except Exception:
        return False


def diagnose(data_dir: str | os.PathLike[str] | None = None) -> Dict[str, Any]:
    """环境体检：如实报告解释器、扩展目录、pip、以及每个常缺包的状态。"""
    root: Optional[Path] = None
    try:
        if data_dir is not None:
            root = resolve_skillenv(data_dir)
        elif _ACTIVE.get("ok"):
            root = Path(str(_ACTIVE["path"]))
    except Exception:
        root = None

    skillenv: Dict[str, Any] = {"path": "", "active": bool(_ACTIVE.get("ok")),
                                "writable": {}, "python_path_in_sys_path": False,
                                "bin_in_path": False}
    if root is not None:
        writable = _ensure_layout(root)
        skillenv.update(path=str(root), writable=writable,
                        python_path_in_sys_path=str(root / "python") in sys.path,
                        bin_in_path=str(root / "bin") in (os.environ.get("PATH") or "").split(os.pathsep))

    packages: List[Dict[str, Any]] = []
    missing: List[str] = []
    for module, pip_name, use in WATCH_MODULES:
        available = _probe(module)
        if not available:
            missing.append(pip_name)
        packages.append({
            "module": module,
            "pip": pip_name,
            "use": use,
            "available": available,
            "fix": "" if available else f"把 {pip_name} 的 wheel 放进 {skillenv['path'] or '<data>/skillenv'}/wheels，再调 /api/env/install-wheel",
        })

    hint = ""
    if missing:
        hint = ("缺 %d 个包：%s。冻结版没有 pip，请下载对应 wheel 放到 skillenv/wheels 后调用 "
                "POST /api/env/install-wheel（离线解压安装），或改用自带完整环境的开发版。"
                % (len(missing), "、".join(missing)))

    return {
        "python": {
            "version": sys.version.split()[0],
            "implementation": sys.implementation.name,
            "executable": sys.executable,
            "frozen": bool(getattr(sys, "frozen", False)),
            "pip_available": _probe("pip"),
        },
        "skillenv": skillenv,
        "packages": packages,
        "missing": missing,
        "install_hint": hint,
        "watch_modules": [m for (m, _p, _u) in WATCH_MODULES],
    }


def _safe_members(zf: zipfile.ZipFile) -> List[zipfile.ZipInfo]:
    """拒绝路径穿越 / 绝对路径的成员。"""
    out: List[zipfile.ZipInfo] = []
    for item in zf.infolist():
        name = item.filename.replace("\\", "/")
        if name.startswith("/") or ".." in name.split("/"):
            raise ValueError(f"wheel 内含不安全的路径：{item.filename}")
        out.append(item)
    return out


def install_wheel(wheel_path: str | os.PathLike[str],
                  data_dir: str | os.PathLike[str]) -> Dict[str, Any]:
    """离线安装一个 wheel：解压进 `<root>/python`，不联网、不需要 pip。

    返回：`{ok, installed:[顶层包名], files, path, error}`。
    失败时**不吞异常语义**：`ok=False` 且 `error` 写明原因。
    """
    p = Path(wheel_path).expanduser()
    if p.suffix.lower() != ".whl":
        raise ValueError("只接受 .whl 文件")
    if not p.is_file():
        raise FileNotFoundError(f"wheel 不存在：{p}")

    root = resolve_skillenv(data_dir)
    _ensure_layout(root)
    target = root / "python"
    installed: List[str] = []
    count = 0
    with zipfile.ZipFile(p) as zf:
        for item in _safe_members(zf):
            name = item.filename.replace("\\", "/")
            if name.endswith("/"):
                continue
            # dist-info/RECORD 等留在目录里便于排查；不执行任何代码
            out_path = target / name
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(item) as src, out_path.open("wb") as dst:
                dst.write(src.read())
            count += 1
            top = name.split("/")[0]
            if top and not top.endswith(".dist-info") and not top.endswith(".data") and top not in installed:
                installed.append(top)

    importlib.invalidate_caches()
    return {
        "ok": True,
        "installed": installed,
        "files": count,
        "path": str(p),
        "target": str(target),
        "note": "已解压到可写扩展目录；若 ABI 与当前解释器不符，体检仍会标 missing。",
    }


def install_wheels_dir(data_dir: str | os.PathLike[str]) -> Dict[str, Any]:
    """把 `<root>/wheels` 里的**所有** `.whl` 依次装进可写扩展目录。

    这是给「不会敲命令的用户」的一键路径：下载 wheel → 丢进 wheels 目录 → 点一下。
    返回每个 wheel 的结果；部分失败不清空已成功的包。
    """
    root = resolve_skillenv(data_dir)
    _ensure_layout(root)
    wheels_dir = root / "wheels"
    files = sorted(p for p in wheels_dir.glob("*.whl") if p.is_file())
    results: List[Dict[str, Any]] = []
    for wheel in files:
        try:
            one = install_wheel(wheel, data_dir)
            results.append({"wheel": wheel.name, **one})
        except Exception as exc:  # noqa: BLE001
            results.append({"wheel": wheel.name, "ok": False,
                            "error": f"{type(exc).__name__}: {exc}"})
    installed: List[str] = []
    for row in results:
        for name in (row.get("installed") or []):
            if name not in installed:
                installed.append(name)
    return {
        "ok": all(bool(row.get("ok")) for row in results) if results else True,
        "wheels": [p.name for p in files],
        "results": results,
        "installed": installed,
        "files": sum(int(row.get("files") or 0) for row in results if row.get("ok")),
        "wheels_dir": str(wheels_dir),
    }


def probe_import(module: str) -> Dict[str, Any]:
    """真 import 一次并报告结果 —— 「装上」不等于「能用」。

    客户场景：把第三方 wheel 解压进扩展目录后，ABI 不匹配（cp3xx 对不上）
    只有真 import 才会暴露。这里把结论做成结构化结果，前端/agent 都能直接看。
    """
    name = (module or "").strip()
    if not name:
        raise ValueError("必须给 module（import 名，例如 cv2）")
    import importlib
    import time
    started = time.perf_counter()
    importlib.invalidate_caches()
    try:
        mod = importlib.import_module(name)
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        return {"ok": True, "module": name,
                "version": str(getattr(mod, "__version__", "") or ""),
                "file": str(getattr(mod, "__file__", "") or ""),
                "elapsed_ms": elapsed}
    except Exception as exc:  # noqa: BLE001
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        return {"ok": False, "module": name,
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_ms": elapsed}


def reset() -> None:
    """测试用：清掉进程内的注入状态（不改磁盘）。"""
    py = _ACTIVE.get("python_path")
    if py and py in sys.path:
        sys.path.remove(py)
    _ACTIVE.clear()
