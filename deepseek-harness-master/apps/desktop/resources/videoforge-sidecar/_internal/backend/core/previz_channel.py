# -*- coding: utf-8 -*-
"""白膜工作台通道「自动就绪」—— openVideoForge 桥 / 页面 / 场景
================================================================

⚠ 命名避坑：本模块管的是 **openVideoForge 工作台通道**（桥 8787 + 页面 8802 +
场景编辑器），跟 `core/publish.py` 里那个「白膜公网通道」（命名隧道，给平台拉
参考视频用）**不是一回事**。那边的入口是 `check_previz_channel`。

为什么需要这个模块
------------------
2026-10-25 的 `videoforge.log` 里有这条：

    openVideoForge 白膜失败，回退 Blender：工作台页面停在「editor」视图，
    没有打开场景；白膜出片需要页面在编辑器里。

`previz_mcp.probe()` 的就绪判据是「桥在 + 页面在 + view=='editor' + scene_id 非空」。
实际断的常常不是「人没干活」，而是这几个运行时前提没人去**建立**：

    · 8787 桥根本没起（要 node 起一个进程，没人起就是「连不上桥」）；
    · 页面没开 / 开了没连桥（页面靠 SSE 连 /events，桥不在就永远不连）；
    · 页面在 editor 视图，但 `scene_id` 是空的 —— 页面侧的 `studioSceneId`
      只在「导出场景」或「载入场景」时才被写入，光点「新建场景」不会写。
      而导出/载入都要 VideoForge 服务（8765）在线。

这个模块把这四层按顺序**尝试自动建立**，失败时明确回答「卡在哪一层、下一步
怎么修」，供 `previz_mcp.render_project_previz_mcp()` 在渲染前调用：

    ① bridge  找到 openvideoforge 包根 → 用 node 起 server/agent-bridge.mjs → 等 /health
    ② page    页面服务不在就 node bin/cli.mjs serve；页面没连桥就打开浏览器
    ③ view    页面不在 editor 就 wb_scene_new（scene.new → openEditor）
    ④ scene   scene_id 为空就 wb_scene_export（需要 8765；没有 8765 如实报 service）
    ⑤ service 渲染服务（8765）不在/环境不全就如实说 —— 否则提交时才报一句
              「Blender服务不可用」，用户根本不知道卡在哪

返回值（失败路径示例，调用方/日志直接用）：

    { ok: False, step: 'scene',
      reason: '页面在编辑器里，但场景还没导出（scene_id 为空）',
      next_action: '先启动 VideoForge 服务（8765）…；或手动在工作台点「导出场景」' }

配置（都有默认值，环境变量优先级最高）
--------------------------------------
    VIDEOFORGE_MCP_BRIDGE           桥地址，默认 http://127.0.0.1:8787
    VIDEOFORGE_OPENVIDEOFORGE_URL   页面地址，默认 http://127.0.0.1:8802/index.html
    VIDEOFORGE_OPENVIDEOFORGE_DIR   openvideoforge 包根目录（含 server/agent-bridge.mjs）
    VIDEOFORGE_OPENVIDEOFORGE_OUT   桥的产物目录（wb_render_save/wb_handoff 落盘处）
    VIDEOFORGE_NODE                 node.exe 路径（默认 PATH / 常见安装位置）
    VIDEOFORGE_PREVIZ_AUTOSTART     0 = 只诊断不拉进程（默认 1）
    VIDEOFORGE_PREVIZ_OPEN_BROWSER  0 = 不自动开浏览器（默认 1）
    VIDEOFORGE_PREVIZ_BRIDGE_WAIT   起桥等 /health 的秒数（默认 12）
    VIDEOFORGE_PREVIZ_PAGE_WAIT     开页面后等它连桥的秒数（默认 18）

自测（只诊断，尽量不改状态）：
    python -m core.previz_channel --check
    python -m core.previz_channel --ensure      # 真去把通道拉起来（会起进程/开页面）
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

_LOG = logging.getLogger("videoforge.previz_channel")

DEFAULT_BRIDGE = "http://127.0.0.1:8787"
DEFAULT_PAGE = "http://127.0.0.1:8802/index.html"
#: 本机这份 openvideoforge 0.9.0 的默认位置（Task T3 已核）
DEFAULT_PRODUCT_DIR = r"D:\KnowledgeVault\projects\cinemap-teardown\product"

BRIDGE_START_TIMEOUT = 12.0     # 起桥等 /health 的上限（秒）
PAGE_START_TIMEOUT = 12.0       # 起静态页等 HTTP 200 的上限
PAGE_CONNECT_TIMEOUT = 18.0     # 打开浏览器后等页面连上桥的上限
EDITOR_WAIT_TIMEOUT = 10.0      # 切 editor / 建场景后等状态刷新的上限
SCENE_WAIT_TIMEOUT = 8.0


# ───────────────────────────── 配置 ─────────────────────────────

def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _float_env(name: str, default: float) -> float:
    """数值型环境变量（测试/排障时可把等待时间调小，避免卡流水线）。"""
    try:
        v = float(_env(name, str(default)))
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def bridge_url() -> str:
    return (_env("VIDEOFORGE_MCP_BRIDGE", DEFAULT_BRIDGE) or DEFAULT_BRIDGE).rstrip("/")


def page_url() -> str:
    return _env("VIDEOFORGE_OPENVIDEOFORGE_URL", DEFAULT_PAGE) or DEFAULT_PAGE


def out_dir() -> str:
    configured = _env("VIDEOFORGE_OPENVIDEOFORGE_OUT")
    if configured:
        return configured
    if sys.platform == "win32":
        return r"D:\下载\白膜工作台"
    return os.path.join(os.path.expanduser("~"), "Downloads", "白膜工作台")


def autostart_enabled() -> bool:
    return _env("VIDEOFORGE_PREVIZ_AUTOSTART", "1") not in ("0", "false", "False", "no")


def open_browser_enabled() -> bool:
    return _env("VIDEOFORGE_PREVIZ_OPEN_BROWSER", "1") not in ("0", "false", "False", "no")


def _data_dir() -> str:
    base = _env("VIDEOFORGE_DATA_DIR")
    if base:
        return base
    la = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(la, "VideoForge", "data")


def log_dir() -> str:
    p = os.path.join(_data_dir(), "previz_channel")
    try:
        os.makedirs(p, exist_ok=True)
    except OSError:
        return _data_dir()
    return p


# ─────────────────────── 可替换的底层原语（测试要 patch）───────────────────────

def _http(url: str, payload: Optional[Dict[str, Any]] = None,
          timeout: float = 5.0) -> Any:
    """本机回环 HTTP（GET / POST JSON）。失败抛异常，由调用方转成人话。"""
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 本机回环
        body = resp.read()
    if not body:
        return {}
    txt = body.decode("utf-8", "replace").strip()
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        return {"ok": False, "error": "响应不是 JSON：%s" % txt[:200]}


def _sleep(seconds: float) -> None:
    time.sleep(max(0.0, float(seconds)))


def _port_of(url: str, default: int) -> int:
    try:
        return int(urllib.parse.urlsplit(url).port or default)
    except (TypeError, ValueError):
        return default


def _spawn(cmd: List[str], cwd: str, log_path: str) -> Dict[str, Any]:
    """后台起一个常驻进程（桥 / 静态页），stdout+stderr 落日志。

    故意用 DETACHED_PROCESS：VideoForge 被关掉时桥不该跟着死，
    下次开 app 还能直接复用（桥自己处理端口占用）。
    """
    try:
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        flags = 0
        if sys.platform == "win32":
            flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            flags |= 0x08000000              # CREATE_NO_WINDOW
        with open(log_path, "ab") as fh:
            fh.write(("\n==== %s  %s  (cwd=%s)\n"
                      % (time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(cmd), cwd))
                     .encode("utf-8", "replace"))
            fh.flush()
            proc = subprocess.Popen(cmd, cwd=cwd or None, stdout=fh,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    creationflags=flags)  # type: ignore[arg-type]
        return {"ok": True, "pid": getattr(proc, "pid", None), "log": log_path}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e), "log": log_path}


def _open_browser(url: str) -> Dict[str, Any]:
    """用系统默认浏览器打开页面（Windows: cmd /c start；其它: webbrowser）。"""
    try:
        if sys.platform == "win32":
            flags = 0x08000000  # CREATE_NO_WINDOW
            subprocess.Popen(["cmd", "/c", "start", "", url],
                             creationflags=flags, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"ok": True, "how": "cmd start"}
        import webbrowser  # noqa: PLC0415 平台分支内延迟导入
        return {"ok": bool(webbrowser.open(url)), "how": "webbrowser"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


# ───────────────────────────── 定位 ─────────────────────────────

def find_product_dir() -> Dict[str, Any]:
    """找 openvideoforge 包根（含 server/agent-bridge.mjs）。

    优先级：环境变量 → 本机已知默认位置 → npm 全局安装目录 → PATH 上的 ovf 包。
    """
    tried: List[str] = []
    candidates: List[tuple[str, str]] = []
    env = _env("VIDEOFORGE_OPENVIDEOFORGE_DIR")
    if env:
        candidates.append((env, "env:VIDEOFORGE_OPENVIDEOFORGE_DIR"))
    candidates.append((DEFAULT_PRODUCT_DIR, "default"))
    for base, label in ((os.environ.get("APPDATA"), "npm-global"),
                        (os.environ.get("LOCALAPPDATA"), "npm-global")):
        if base:
            candidates.append((os.path.join(base, "npm", "node_modules", "openvideoforge"), label))
    # 本机 npm 全局（ProgramFiles）
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            candidates.append((os.path.join(base, "nodejs", "node_modules", "openvideoforge"), "npm-global"))

    for raw, source in candidates:
        if not raw:
            continue
        path = os.path.abspath(os.path.expanduser(raw))
        if path in tried:
            continue
        tried.append(path)
        if os.path.isfile(os.path.join(path, "server", "agent-bridge.mjs")):
            return {"ok": True, "root": path, "source": source, "tried": tried}
    return {
        "ok": False,
        "tried": tried,
        "reason": "找不到 openvideoforge 包根（需要目录里有 server/agent-bridge.mjs）",
        "next_action": (
            "设环境变量 VIDEOFORGE_OPENVIDEOFORGE_DIR 指向 openvideoforge 包根目录"
            "（例如 D:\\KnowledgeVault\\projects\\cinemap-teardown\\product），再重试"
        ),
    }


def node_exe() -> Dict[str, Any]:
    """找 node.exe：环境变量 → PATH → 常见安装位置。"""
    env = _env("VIDEOFORGE_NODE")
    cands: List[str] = []
    if env:
        cands.append(env)
    which = shutil.which("node")
    if which:
        cands.append(which)
    if sys.platform == "win32":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                     os.environ.get("LOCALAPPDATA")):
            if base:
                cands.append(os.path.join(base, "nodejs", "node.exe"))
                cands.append(os.path.join(base, "Programs", "nodejs", "node.exe"))
    tried: List[str] = []
    for c in cands:
        if not c:
            continue
        p = os.path.abspath(os.path.expanduser(c))
        if p in tried:
            continue
        tried.append(p)
        if os.path.isfile(p):
            return {"ok": True, "exe": p, "tried": tried}
    return {
        "ok": False,
        "tried": tried,
        "reason": "找不到 node.exe（起 openvideoforge 桥/页面服务要用）",
        "next_action": "安装 Node.js 18+，或用环境变量 VIDEOFORGE_NODE 指向 node.exe",
    }


def _page_open_url(page: str) -> str:
    """带 client 参数的页面地址（页面据此给桥一个稳定身份，也绕过浏览器缓存）。"""
    sep = "&" if "?" in page else "?"
    return "%s%sclient=videoforge&wbopen=%d" % (page, sep, int(time.time()))


# ─────────────────────────── 单步：桥 ───────────────────────────

def bridge_health(bridge: Optional[str] = None, timeout: float = 3.0) -> Dict[str, Any]:
    """GET /health（只读）。"""
    url = (bridge or bridge_url()).rstrip("/")
    try:
        out = _http(url + "/health", None, timeout)
        if not isinstance(out, dict):
            return {"ok": False, "error": "health 响应不是对象：%r" % (out,)}
        return out
    except urllib.error.URLError as e:
        return {"ok": False, "error": "连不上桥 %s（%s）"
                                      % (url, getattr(e, "reason", e))}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def tool_call(name: str, args: Optional[Dict[str, Any]] = None,
              bridge: Optional[str] = None, timeout: float = 60.0) -> Dict[str, Any]:
    """POST /tool {name,args} —— 与 MCP `tools/call` 等价（桥的原文错误原样带回）。"""
    url = (bridge or bridge_url()).rstrip("/")
    try:
        out = _http(url + "/tool", {"name": name, "args": args or {}}, timeout)
    except urllib.error.URLError as e:
        return {"ok": False, "error": "连不上白膜工作台桥 %s（%s）"
                                      % (url, getattr(e, "reason", e))}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    if not isinstance(out, dict):
        return {"ok": True, "data": out}
    return out


def _page_connected(health: Dict[str, Any]) -> bool:
    return bool(health.get("page") or (health.get("clients") or []))


def ensure_bridge(bridge: Optional[str] = None, *, timeout: Optional[float] = None,
                  allow_start: Optional[bool] = None) -> Dict[str, Any]:
    """桥没起就自动拉起（node server/agent-bridge.mjs），并等到 /health ok。"""
    url = (bridge or bridge_url()).rstrip("/")
    out: Dict[str, Any] = {"step": "bridge", "ok": False, "bridge": url,
                           "started": False, "log": "", "reason": "", "next_action": ""}
    h = bridge_health(url)
    if h.get("ok"):
        out["ok"] = True
        out["step"] = None
        out["health"] = h
        return out
    out["health"] = h

    if not (autostart_enabled() if allow_start is None else allow_start):
        out["reason"] = "桥没在跑（%s）" % (h.get("error") or "连不上")
        out["next_action"] = ("手动启动：node server/agent-bridge.mjs --port %d；"
                              "或允许自动拉起（去掉 VIDEOFORGE_PREVIZ_AUTOSTART=0）"
                              % _port_of(url, 8787))
        return out

    pkg = find_product_dir()
    if not pkg.get("ok"):
        out.update(reason=pkg.get("reason"), next_action=pkg.get("next_action"),
                   tried_roots=pkg.get("tried"))
        out["step"] = "root"
        return out
    node = node_exe()
    if not node.get("ok"):
        out.update(reason=node.get("reason"), next_action=node.get("next_action"))
        out["step"] = "root"
        return out

    port = _port_of(url, 8787)
    log = os.path.join(log_dir(), "bridge-%d.log" % port)
    cmd = [node["exe"], os.path.join(pkg["root"], "server", "agent-bridge.mjs"),
           "--port", str(port), "--out", out_dir()]
    sp = _spawn(cmd, pkg["root"], log)
    out["log"] = log
    out["product_dir"] = pkg["root"]
    out["node"] = node["exe"]
    if not sp.get("ok"):
        out.update(reason="起桥进程失败：%s" % sp.get("error"),
                   next_action="手动在 %s 下运行：%s" % (pkg["root"], " ".join(cmd)))
        return out
    out["pid"] = sp.get("pid")
    out["started"] = True

    deadline = time.time() + (timeout or _float_env("VIDEOFORGE_PREVIZ_BRIDGE_WAIT",
                                                   BRIDGE_START_TIMEOUT))
    while time.time() < deadline:
        _sleep(0.25)
        h = bridge_health(url, timeout=1.5)
        if h.get("ok"):
            out.update(ok=True, step=None, health=h)
            _LOG.info("白膜工作台桥已自动拉起：%s（pid=%s，日志 %s）", url, out.get("pid"), log)
            return out
    wait_s = timeout or _float_env("VIDEOFORGE_PREVIZ_BRIDGE_WAIT", BRIDGE_START_TIMEOUT)
    out["reason"] = ("桥进程已拉起（pid=%s），但 %.0f 秒内 /health 没响应"
                     % (out.get("pid"), wait_s))
    out["next_action"] = ("看日志 %s（常见：node 版本过低 / 端口被别的程序占用）；"
                          "或手动在 %s 下运行 node server/agent-bridge.mjs --port %d"
                          % (log, pkg["root"], port))
    return out


# ─────────────────────────── 单步：页面 ───────────────────────────

def _page_reachable(page: str, timeout: float = 3.0) -> Dict[str, Any]:
    try:
        req = urllib.request.Request(page, headers={"Accept": "text/html"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 本机回环
            head = resp.read(2048)
        if not head.strip():
            return {"ok": False, "error": "页面响应为空"}
        return {"ok": True, "status": getattr(resp, "status", 200)}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": "HTTP %s" % e.code}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def _start_page_server(page: str, *, allow_start: bool = True) -> Dict[str, Any]:
    pkg = find_product_dir()
    if not pkg.get("ok"):
        return {"ok": False, "reason": pkg.get("reason"), "next_action": pkg.get("next_action")}
    node = node_exe()
    if not node.get("ok"):
        return {"ok": False, "reason": node.get("reason"), "next_action": node.get("next_action")}
    cli = os.path.join(pkg["root"], "bin", "cli.mjs")
    if not os.path.isfile(cli):
        return {"ok": False,
                "reason": "openvideoforge 包根里没有 bin/cli.mjs：%s" % pkg["root"],
                "next_action": "确认 VIDEOFORGE_OPENVIDEOFORGE_DIR 指向的是完整包（含 bin/ 与 server/）"}
    if not allow_start:
        return {"ok": False, "reason": "页面服务 %s 没在跑" % page,
                "next_action": "手动启动：node bin/cli.mjs serve（在 %s 下）" % pkg["root"]}
    cmd = [node["exe"], cli, "serve",
           "--port", str(_port_of(bridge_url(), 8787)),
           "--static-port", str(_port_of(page, 8802)),
           "--out", out_dir()]
    log = os.path.join(log_dir(), "page-%d.log" % _port_of(page, 8802))
    sp = _spawn(cmd, pkg["root"], log)
    return {"ok": bool(sp.get("ok")), "log": log, "pid": sp.get("pid"),
            "reason": "" if sp.get("ok") else "起页面服务失败：%s" % sp.get("error"),
            "next_action": "" if sp.get("ok") else "手动在 %s 下运行：%s" % (pkg["root"], " ".join(cmd))}


def ensure_page(bridge: Optional[str] = None, *, page: Optional[str] = None,
                open_browser: Optional[bool] = None,
                timeout: Optional[float] = None,
                allow_start: Optional[bool] = None) -> Dict[str, Any]:
    """页面没连桥 → 必要时先起静态页服务，再打开浏览器，等页面自己连上桥。"""
    url = (bridge or bridge_url()).rstrip("/")
    page_url_ = page or page_url()
    out: Dict[str, Any] = {"step": "page", "ok": False, "bridge": url, "page_url": page_url_,
                           "started_static": False, "opened_browser": False,
                           "open_url": "", "log": "", "reason": "", "next_action": ""}
    allow = autostart_enabled() if allow_start is None else allow_start
    want_browser = open_browser_enabled() if open_browser is None else open_browser

    h = bridge_health(url)
    if not h.get("ok"):
        out["reason"] = "桥没就绪：%s" % (h.get("error") or "连不上")
        out["next_action"] = "先确保桥能起（见 bridge 步骤）"
        return out
    if _page_connected(h):
        out.update(ok=True, step=None, health=h)
        return out

    reach = _page_reachable(page_url_)
    if not reach.get("ok"):
        started = _start_page_server(page_url_, allow_start=allow)
        out["started_static"] = bool(started.get("ok"))
        out["log"] = started.get("log") or ""
        if not started.get("ok"):
            out["reason"] = ("页面服务 %s 打不开（%s），自动起静态服务也失败：%s"
                             % (page_url_, reach.get("error"), started.get("reason") or ""))
            out["next_action"] = started.get("next_action") or (
                "手动启动 OpenVideoForge 页面：node bin/cli.mjs serve")
            return out
        deadline = time.time() + PAGE_START_TIMEOUT
        while time.time() < deadline:
            _sleep(0.3)
            if _page_reachable(page_url_, timeout=1.5).get("ok"):
                break
        else:
            out["reason"] = "页面服务进程已拉起（pid=%s），但 %.0f 秒内 %s 打不开" % (
                started.get("pid"), PAGE_START_TIMEOUT, page_url_)
            out["next_action"] = "看日志 %s；或手动在 openvideoforge 包根下运行 node bin/cli.mjs serve" % out["log"]
            return out
        _LOG.info("白膜工作台页面服务已自动拉起：%s（日志 %s）", page_url_, out["log"])

    if want_browser:
        open_url = _page_open_url(page_url_)
        op = _open_browser(open_url)
        out["opened_browser"] = bool(op.get("ok"))
        out["open_url"] = open_url
        if op.get("ok"):
            _LOG.info("已打开白膜工作台页面：%s", open_url)

    wait_s = timeout or _float_env("VIDEOFORGE_PREVIZ_PAGE_WAIT", PAGE_CONNECT_TIMEOUT)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        _sleep(0.35)
        h = bridge_health(url, timeout=1.5)
        if h.get("ok") and _page_connected(h):
            out.update(ok=True, step=None, health=h)
            return out

    out["reason"] = ("桥在，但 %.0f 秒内没有任何页面连上（%s 可能打不开，"
                     "或页面 JS 没跑起来 / 被浏览器拦了 127.0.0.1）"
                     % (wait_s, page_url_))
    out["next_action"] = (
        "在本机浏览器打开 %s?client=videoforge（页面会自动连桥并每 2s 自报视图）；"
        "若本机是无头服务器，请在有浏览器的机器上打开同一地址（需能访问 127.0.0.1:%d）"
        % (page_url_, _port_of(url, 8787)))
    return out


# ─────────────────────── 单步：编辑器视图 / 场景 ───────────────────────

def _state(bridge: str, timeout: float = 15.0) -> Dict[str, Any]:
    r = tool_call("wb_state", {}, bridge, timeout)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or "wb_state 失败", "raw": r}
    data = r.get("data")
    if not isinstance(data, dict):
        return {"ok": False, "error": "wb_state 返回的不是对象：%r" % (data,)}
    return {"ok": True, "data": data}


def ensure_editor(bridge: Optional[str] = None, *, env: str = "room",
                  wait: Optional[float] = None) -> Dict[str, Any]:
    """页面不在编辑器 → wb_scene_new；等到 view=='editor'。"""
    url = (bridge or bridge_url()).rstrip("/")
    out: Dict[str, Any] = {"step": "view", "ok": False, "view": None,
                           "scene_id": None, "created": False,
                           "reason": "", "next_action": ""}
    st = _state(url)
    if not st.get("ok"):
        out["step"] = "page"
        out["reason"] = "桥在，但读不到页面状态：%s" % st.get("error")
        out["next_action"] = ("在工作台页面里打开一次编辑器（或让通道自动开页面："
                              "去掉 VIDEOFORGE_PREVIZ_OPEN_BROWSER=0）")
        return out
    data = st["data"]
    out["view"] = data.get("view")
    out["scene_id"] = data.get("scene_id")
    if data.get("view") == "editor":
        out["ok"] = True
        out["step"] = None
        return out

    r = tool_call("wb_scene_new", {"env": env}, url, 30)
    out["created"] = bool(r.get("ok"))
    if not r.get("ok"):
        out["reason"] = "页面停在「%s」视图，且自动新建场景失败：%s" % (
            data.get("view") or "?", r.get("error"))
        out["next_action"] = "在工作台页面点「新建场景」进入编辑器，再重试白膜"
        return out

    deadline = time.time() + (wait or EDITOR_WAIT_TIMEOUT)
    while time.time() < deadline:
        _sleep(0.4)
        st = _state(url)
        if st.get("ok") and st["data"].get("view") == "editor":
            out["view"] = "editor"
            out["scene_id"] = st["data"].get("scene_id")
            out.update(ok=True, step=None)
            _LOG.info("白膜工作台已自动进入编辑器视图")
            return out
    out["reason"] = "已下发「新建场景」，但 %.0f 秒内页面没切到编辑器视图" % (wait or EDITOR_WAIT_TIMEOUT)
    out["next_action"] = "在工作台页面手动点「新建场景」后重试；或刷新页面（桥会自动重连）"
    return out


def vf_service_health(vf: Optional[str] = None, timeout: float = 3.0) -> Dict[str, Any]:
    """VideoForge 服务（默认 8765）的 /api/studio/health（只读）。"""
    base = (vf or _env("VIDEOFORGE_MCP_VF", "http://127.0.0.1:8765")).rstrip("/")
    try:
        body = _http(base + "/api/studio/health", None, timeout)
    except urllib.error.URLError as e:
        return {"ok": False, "url": base,
                "error": "连不上 VideoForge 服务 %s（%s）" % (base, getattr(e, "reason", e))}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "url": base, "error": "%s: %s" % (type(e).__name__, e)}
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return {"ok": False, "url": base, "error": "服务响应异常：%s" % str(body)[:200]}
    return {"ok": True, "url": base, "data": data,
            "ready": bool(data.get("ready"))}


def ensure_scene(bridge: Optional[str] = None, *, vf: Optional[str] = None,
                 wait: Optional[float] = None,
                 timeout: int = 120) -> Dict[str, Any]:
    """页面在编辑器里但 scene_id 为空 → 导出一次场景（需要 8765 在线）。

    页面侧的 `studioSceneId` 只有「导出场景 / 载入场景」才会被写入；
    光「新建场景」不会。所以这一层不通时，probe 的判据永远过不了。
    """
    url = (bridge or bridge_url()).rstrip("/")
    render = {"seconds": 3, "fps": 24, "width": 640, "height": 360}
    out: Dict[str, Any] = {"step": "scene", "ok": False, "scene_id": None,
                           "exported": False, "service": None,
                           "reason": "", "next_action": ""}
    st = _state(url)
    if not st.get("ok"):
        out.update(step="page", reason="读不到页面状态：%s" % st.get("error"),
                   next_action="先让页面连上桥（见 page 步骤）")
        return out
    data = st["data"]
    if data.get("scene_id"):
        out.update(ok=True, step=None, scene_id=data.get("scene_id"))
        return out

    svc = vf_service_health(vf)
    out["service"] = {"ok": bool(svc.get("ok")), "ready": bool(svc.get("ready")),
                      "url": svc.get("url"), "error": svc.get("error")}
    if not svc.get("ok"):
        out["cross"] = True
        out["reason"] = ("页面在编辑器里，但场景还没导出（scene_id 为空）；"
                         "导出场景要写 VideoForge 服务（8765），而这个服务没在跑")
        out["next_action"] = ("先启动 VideoForge（服务模式后端会监听 8765），再重试白膜；"
                              "或设 VIDEOFORGE_MCP_VF 指向服务实际地址")
        return out
    if not svc.get("ready"):
        out["reason"] = ("VideoForge 服务在，但渲染环境没就绪（%s）；场景导出会失败"
                         % (svc.get("data") or {}))
        out["next_action"] = "按 /api/studio/health 的提示装好 Blender/ffmpeg 后再试白膜"
        return out

    def _export() -> Dict[str, Any]:
        return tool_call("wb_scene_export", dict(render), url, timeout)

    r = _export()
    if not r.get("ok") and "可渲染的模型" in str(r.get("error") or ""):
        # 新建的空场景里没有实例 → 先载入默认环境（有几何体）再导出
        tool_call("wb_env_load", {"env": "room"}, url, 30)
        _sleep(0.8)
        r = _export()
    out["exported"] = bool(r.get("ok"))
    if not r.get("ok"):
        out["reason"] = "页面在编辑器里，但自动导出场景失败：%s" % (r.get("error") or "")
        out["next_action"] = ("在工作台点「导出场景」（需 VideoForge 服务 8765 在线，"
                              "且场景里至少有一个模型/环境），然后重试白膜")
        return out

    deadline = time.time() + (wait or SCENE_WAIT_TIMEOUT)
    while time.time() < deadline:
        _sleep(0.35)
        st = _state(url)
        if st.get("ok") and st["data"].get("scene_id"):
            out.update(ok=True, step=None, scene_id=st["data"].get("scene_id"))
            _LOG.info("白膜工作台场景已自动导出：scene_id=%s", out["scene_id"])
            return out
    out["reason"] = "场景导出接口返回成功，但页面 state 里仍然没有 scene_id"
    out["next_action"] = "刷新工作台页面后重试；若仍为空，把页面 console 的报错发出来"
    return out


# ───────────────────────────── 总入口 ─────────────────────────────

def _fail(out: Dict[str, Any], started_at: float, step: str,
          reason: str, next_action: str) -> Dict[str, Any]:
    out.update(ok=False, step=step, reason=reason, next_action=next_action,
               elapsed_ms=int((time.time() - started_at) * 1000))
    _LOG.warning("白膜工作台通道没就绪（卡在「%s」）：%s；下一步：%s",
                 step, reason, next_action)
    return out


def ensure_ready(bridge: Optional[str] = None, *, page: Optional[str] = None,
                 vf: Optional[str] = None, open_browser: Optional[bool] = None,
                 allow_start: Optional[bool] = None) -> Dict[str, Any]:
    """把「桥 → 页面 → 编辑器 → 场景 → 渲染服务」五层按顺序拉到可用。

    Returns:
        {ok, step, reason, next_action, view, scene_id, actions:[...], steps:[...]}
        ok=False 时 step 是最先卡住的那一层（root/bridge/page/view/scene/service/probe），
        reason 是人能看懂的原因，next_action 是**可执行**的下一步。
    """
    started_at = time.time()
    url = (bridge or bridge_url()).rstrip("/")
    out: Dict[str, Any] = {
        "ok": False, "step": None, "reason": "", "next_action": "",
        "bridge": url, "page_url": page or page_url(), "view": None, "scene_id": None,
        "started_bridge": False, "started_static": False, "opened_browser": False,
        "actions": [], "steps": [], "elapsed_ms": 0,
    }

    rb = ensure_bridge(url, allow_start=allow_start)
    out["steps"].append({"step": "bridge", "ok": bool(rb.get("ok")),
                         "detail": rb.get("log") or rb.get("reason") or ""})
    out["started_bridge"] = bool(rb.get("started"))
    if rb.get("started"):
        out["actions"].append("已自动拉起 openVideoForge 桥 %s（pid=%s）"
                              % (url, rb.get("pid")))
    if not rb.get("ok"):
        return _fail(out, started_at, rb.get("step") or "bridge",
                     rb.get("reason") or "桥没就绪", rb.get("next_action") or "")

    rp = ensure_page(url, page=page, open_browser=open_browser, allow_start=allow_start)
    out["steps"].append({"step": "page", "ok": bool(rp.get("ok")),
                         "detail": rp.get("reason") or ("页面已连桥" if rp.get("ok") else "")})
    out["started_static"] = bool(rp.get("started_static"))
    out["opened_browser"] = bool(rp.get("opened_browser"))
    if rp.get("started_static"):
        out["actions"].append("已自动拉起工作台页面服务 %s（pid=%s）"
                              % (rp.get("page_url"), rp.get("pid")))
    if rp.get("opened_browser"):
        out["actions"].append("已自动打开工作台页面（%s）" % rp.get("open_url"))
    if not rp.get("ok"):
        return _fail(out, started_at, "page",
                     rp.get("reason") or "页面没连上桥",
                     rp.get("next_action") or "")

    re_ = ensure_editor(url)
    out["steps"].append({"step": "view", "ok": bool(re_.get("ok")),
                         "detail": ("view=%s" % re_.get("view")) if re_.get("ok") else re_.get("reason")})
    out["view"] = re_.get("view")
    out["scene_id"] = re_.get("scene_id")
    if re_.get("created") and re_.get("ok"):
        out["actions"].append("已自动新建场景并进入编辑器（env=room）")
    if not re_.get("ok"):
        return _fail(out, started_at, re_.get("step") or "view",
                     re_.get("reason") or "页面不在编辑器",
                     re_.get("next_action") or "")

    rs = ensure_scene(url, vf=vf)
    out["steps"].append({"step": "scene", "ok": bool(rs.get("ok")),
                         "detail": ("scene_id=%s" % rs.get("scene_id")) if rs.get("ok")
                                   else (rs.get("reason") or "")})
    out["scene_id"] = rs.get("scene_id") or out.get("scene_id")
    if rs.get("exported") and rs.get("ok"):
        out["actions"].append("已自动导出场景（scene_id=%s）" % rs.get("scene_id"))
    if not rs.get("ok"):
        step = "service" if rs.get("cross") else (rs.get("step") or "scene")
        return _fail(out, started_at, step,
                     rs.get("reason") or "场景没就绪",
                     rs.get("next_action") or "")

    # ⑤ 渲染服务：桥/页面/场景都好了，但 VideoForge 服务（8765）没起的话，
    #    wb_render_submit 会失败 —— 这一步提前说清，别等提交时才报一句
    #    「Blender服务不可用」。
    svc = vf_service_health(vf)
    svc_ok = bool(svc.get("ok") and svc.get("ready"))
    out["steps"].append({"step": "service", "ok": svc_ok,
                         "detail": (svc.get("url") if svc_ok
                                    else (svc.get("error")
                                          or "渲染环境未就绪：%s" % (svc.get("data") or {})))})
    out["service"] = {"ok": bool(svc.get("ok")), "ready": bool(svc.get("ready")),
                      "url": svc.get("url")}
    if not svc_ok:
        return _fail(out, started_at, "service",
                     "桥/页面/场景都已就绪，但 VideoForge 渲染服务没在跑或环境不全（%s）"
                     % (svc.get("error") or (svc.get("data") or {})),
                     "启动 VideoForge（服务模式后端会监听 8765），再重试白膜；"
                     "或设 VIDEOFORGE_MCP_VF 指向服务实际地址")

    # 最后一公里：按 probe 的同一套判据复核（view == editor 且 scene_id 非空）
    st = _state(url)
    if st.get("ok"):
        out["view"] = st["data"].get("view")
        out["scene_id"] = st["data"].get("scene_id") or out.get("scene_id")
    if out["view"] != "editor" or not out["scene_id"]:
        return _fail(out, started_at, "probe",
                     "通道自检没通过（view=%s, scene_id=%s）"
                     % (out["view"], out["scene_id"]),
                     "刷新工作台页面后重试白膜")

    out.update(ok=True, step=None, reason="", next_action="",
               elapsed_ms=int((time.time() - started_at) * 1000))
    _LOG.info("白膜工作台通道已就绪（view=editor, scene_id=%s，用时 %dms）%s",
              out["scene_id"], out["elapsed_ms"],
              ("；自动动作：" + "；".join(out["actions"])) if out["actions"] else "")
    return out


def describe(ready: Dict[str, Any]) -> str:
    """把 ensure_ready/probe 的结果压成一句给人看的话（日志/工具返回都用）。"""
    if ready.get("ok"):
        return ("白膜工作台就绪：桥 %s · view=%s · scene_id=%s"
                % (ready.get("bridge"), ready.get("view"), ready.get("scene_id")))
    step = ready.get("step") or "?"
    return ("白膜工作台没就绪（卡在「%s」）：%s；下一步：%s"
            % (step, ready.get("reason") or "未知原因",
               ready.get("next_action") or "重试一次"))


# ───────────────────────────── 自测 ─────────────────────────────

if __name__ == "__main__":  # pragma: no cover - 手工排障用
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    argv = [a for a in sys.argv[1:] if a in ("--check", "--ensure")]
    if "--ensure" in argv:
        print(json.dumps(ensure_ready(), ensure_ascii=False, indent=2))
    elif "--check" in argv:
        probe = {"bridge": bridge_url(), "page_url": page_url(),
                 "health": bridge_health(), "product_dir": find_product_dir(),
                 "node": node_exe(), "vf_service": vf_service_health()}
        try:
            probe["state"] = _state(bridge_url())
        except Exception as e:  # noqa: BLE001
            probe["state"] = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        print(json.dumps(probe, ensure_ascii=False, indent=2))
    else:
        print("用法：python -m core.previz_channel --check | --ensure")
