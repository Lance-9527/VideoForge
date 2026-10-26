# -*- coding: utf-8 -*-
"""
VideoForge · 通用 MCP（Model Context Protocol）客户端 —— **同步版，不用 asyncio**

════════════════════════════════════════════════════════════════════════
一、为什么不用官方 `mcp` SDK 的客户端
════════════════════════════════════════════════════════════════════════
本机确实装了官方 SDK（mcp 1.27.2，`mcp.client.stdio` / `sse` / `streamable_http`
都能 import）。但**它的客户端全是 async 的**，套进 VideoForge 会踩三个真实的坑：

1. **同步调用链**：VideoForge 的 agent 工具（`backend/core/agent_tools*.py`）是
   同步函数，跑在 FastAPI 的线程池里。要用官方 SDK 就得在每个工具里
   `asyncio.run(...)` 或者维护一条常驻事件循环 —— 前者在「已经身处事件循环」
   时会直接抛 `RuntimeError: asyncio.run() cannot be called from a running event loop`，
   后者要把整个生命周期挂到一个后台线程上，复杂度和故障面都翻倍。

2. **Windows 事件循环策略**：asyncio 的子进程（`asyncio.create_subprocess_exec`）
   在 Windows 上**只有 ProactorEventLoop 支持**；而 uvicorn 在某些配置下会把策略
   设成 `WindowsSelectorEventLoopPolicy`（Selector 不支持子进程）。也就是说
   stdio 传输能不能用，取决于**别的模块有没有先动过全局策略**——这种隐式全局状态
   在打包后的 exe 里极难排查。同步的 `subprocess.Popen` 没有这个问题。

3. **PyInstaller 体积**：官方 SDK 拖进来 anyio / sse-starlette / starlette /
   uvicorn / jsonschema / pyjwt / pywin32 等一串包，单文件 exe 会明显变大，
   还要为每个包写 hiddenimports。本模块只用 **标准库 + httpx**（httpx 项目本来就有），
   打包零新增依赖。

因此：**手写 JSON-RPC 2.0**，`threading` + `queue`，全同步。协议本身很简单
（请求/响应/通知三种报文），代码量比 adapter 少，行为完全可控。

════════════════════════════════════════════════════════════════════════
二、stdio 的帧格式：**NDJSON，不是 Content-Length**
════════════════════════════════════════════════════════════════════════
这是最容易写错的地方，明确写死：

    MCP 的 stdio 传输 = **换行分隔的 JSON（newline-delimited JSON）**
    每一行是**一个完整的 JSON 对象**，行尾是 `\n`。
    报文内部**不允许**出现裸换行（JSON 字符串里的换行必须转义成 `\\n`）。

    ✗ 不要用 LSP 那套 `Content-Length: 123\\r\\n\\r\\n{...}` 头（那是 LSP/DAP）。
    ✗ 不要按字节长度读固定长度。
    ✓ 就是「一行一 JSON」。

本模块的读写都用 `text=True, encoding="utf-8"`，读线程 `for line in stdout`，
写的时候 `json.dumps(..., ensure_ascii=False) + "\\n"` 并立刻 flush。
`ensure_ascii=False` 是为了让中文原样出去（UTF-8 字节），避免某些服务器
对 `\\uXXXX` 转义处理不一致。

另外：**stderr 必须单独开线程读掉**。MCP 服务器（尤其是官方的 FastMCP）
会把日志写到 stderr；如果没人读，管道缓冲区（Windows 上约 4KB）写满后
**子进程会阻塞在写日志上，整个会话死锁**。本模块把 stderr 收进环形缓冲，
既当日志又当报错时的证据。

════════════════════════════════════════════════════════════════════════
三、call_tool 的错误约定（**重要，上层别写错**）
════════════════════════════════════════════════════════════════════════
· **工具自己报错不是异常，是正常业务结果。** MCP 规定工具失败时返回
  `{"result": {"content": [...], "isError": true}}` —— 这是一次**成功的 RPC**。
  所以 `call_tool()` 遇到 `isError: true` 时 **绝不抛异常**，而是返回
  `{"ok": False, "is_error": True, "text": "<错误文本>"}`，让上层把错误文本
  喂回给 LLM 或展示给用户。
· 同理，**工具不存在**、**参数校验失败**（服务器返回 JSON-RPC error 或
  `isError`）也走 `ok=False`，不抛。
· **只有传输层/协议层的问题才抛**：
    - `MCPTransportError` —— 子进程起不来 / 中途死掉 / 读写断线 / 超时 / HTTP 4xx-5xx;
    - `MCPProtocolError`  —— 回包不是合法 JSON-RPC（比如缺 result、content 类型不对、
      initialize 被拒）。
  上层只需要 `except MCPTransportError` 决定「这个服务器挂了，标记不可用」，
  不必为工具的业务错误写 try/except。

════════════════════════════════════════════════════════════════════════
四、已实现 / 未实现（诚实边界）
════════════════════════════════════════════════════════════════════════
已实现：
    · stdio 传输（`subprocess.Popen` + 读线程 + 超时 + 优雅关闭 + 僵尸回收）
    · Streamable HTTP 传输（`httpx.Client`，`application/json` 与 `text/event-stream`
      两种响应都解析，回传 `Mcp-Session-Id` 与 `MCP-Protocol-Version` 头）
    · 完整生命周期：initialize → notifications/initialized → tools/list（带
      nextCursor 分页）→ tools/call
    · 服务器返回不同 protocolVersion 时**降级继续**（协议允许，只记日志不报错）
    · **HTTP 代理决策**（见 `_proxy_decision`）：本机地址强制直连、只认显式的
      HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/NO_PROXY，**不继承 Windows 注册表里的
      IE/WinINET 代理** —— 本机实测那会让每次本机请求从 3ms 变成 2050ms
未实现（本模块**不打算**做）：
    · resources / prompts / sampling / roots / elicitation 这些能力
      （只做了 tools —— VideoForge 只需要工具）
    · 旧版 HTTP+SSE 双端点传输（`mcp.client.sse` 那种 `GET /sse` + `POST /messages`）；
      `transport="http"` 指的是 2025-03-26 起的 **Streamable HTTP**（单端点）
    · OAuth —— 可灵那条专用通道请继续用 `backend/core/kling_mcp.py`，本模块不碰它
    · 断线自动重连（断了就抛 `MCPTransportError`，由上层决定要不要重建客户端）
    · **写 stdio 管道没有超时**：`proc.stdin.write()` 本身不带超时，如果子进程完全
      不读 stdin（已经卡死），一次超大参数的请求理论上会阻塞在写上。读那侧是有
      超时的（实测有效）。真遇到卡死的服务器，`close()` 会关掉 stdin 把它解开。
    · 不带 `Mcp-Session-Id` 过期后的自动重握手（HTTP 404 时抛 `MCPTransportError`
      并在错误信息里说明，由上层重建客户端）

实测证据：`tests/test_mcp_client.py`（用官方 SDK 的 server 端做真协议对打）。
"""

from __future__ import annotations

import collections
import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger("videoforge.mcp_client")

#: 我们支持并默认使用的 MCP 协议版本（2025-06-18 是当前稳定版）
PROTOCOL_VERSION = "2025-06-18"

#: 客户端自我介绍（服务器会在 serverInfo/日志里看到）
CLIENT_NAME = "VideoForge"
CLIENT_VERSION = "2.0.0"

#: 单次 tools/list 最多翻多少页（防御服务器给死循环 nextCursor）
MAX_TOOL_PAGES = 50

#: stderr 环形缓冲保留多少行（报错时当作证据打印出来）
STDERR_TAIL_LINES = 100


# ══════════════════════════════════════════════════════════════════════
# 异常
# ══════════════════════════════════════════════════════════════════════
class MCPError(Exception):
    """MCP 相关错误基类。上层 `except MCPError` 可以兜住全部。"""


class MCPTransportError(MCPError):
    """传输层错误：起不来 / 断线 / 超时 / 进程退出 / HTTP 非 2xx。

    出现这个异常意味着**这条通道不可用了**，不是「工具执行失败」。
    """


class MCPProtocolError(MCPError):
    """协议层错误：回包不符合 JSON-RPC / MCP 规范（缺 result、类型不对、被拒绝）。"""


# ══════════════════════════════════════════════════════════════════════
# 配置
# ══════════════════════════════════════════════════════════════════════
@dataclass
class MCPServerConfig:
    """一个 MCP 服务器的连接配置。

    `transport="stdio"` 用 command/args/env/cwd；`transport="http"` 用 url/headers。
    """

    name: str
    transport: str = "stdio"          # "stdio" | "http"
    command: str = ""                 # stdio: 可执行文件
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    url: str = ""                     # http
    headers: Dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    timeout: float = 30.0

    def describe(self) -> str:
        """给日志/错误信息用的一行描述（不含敏感 header 值）。"""
        if self.transport == "http":
            return f"MCP[{self.name}] http {self.url}"
        return "MCP[%s] stdio %s %s" % (self.name, self.command, " ".join(self.args))


# ══════════════════════════════════════════════════════════════════════
# 内容拍平
# ══════════════════════════════════════════════════════════════════════
def flatten_content(content: Any) -> str:
    """把 tools/call 返回的 `content` 数组拍平成**人类可读文本**。

    MCP 的 content 元素形态（2025-06-18）：
        {"type": "text",     "text": "..."}
        {"type": "image",    "data": "<base64>", "mimeType": "image/png"}
        {"type": "audio",    "data": "<base64>", "mimeType": "audio/wav"}
        {"type": "resource", "resource": {"uri": "...", "text"/"blob": ..., "mimeType": ...}}
        {"type": "resource_link", "uri": "...", "name": ...}

    规则（刻意保守，避免把几 MB 的 base64 灌进 LLM 上下文）：
        · text     → 原文
        · image    → `[image: image/png]`（**不输出 base64**）
        · audio    → `[audio: audio/wav]`
        · resource → `[resource: <uri>]`，若带 text 再附上文本
        · 其它     → 紧凑 JSON（截断到 2000 字）
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        items: List[Any] = [content]
    elif isinstance(content, (list, tuple)):
        items = list(content)
    else:
        return str(content)

    parts: List[str] = []
    for el in items:
        if isinstance(el, str):
            parts.append(el)
            continue
        if not isinstance(el, dict):
            parts.append(str(el))
            continue

        etype = str(el.get("type") or "")
        if etype == "text":
            parts.append(str(el.get("text") or ""))
        elif etype == "image":
            parts.append("[image: %s]" % (el.get("mimeType") or "unknown"))
        elif etype == "audio":
            parts.append("[audio: %s]" % (el.get("mimeType") or "unknown"))
        elif etype == "resource":
            res = el.get("resource") if isinstance(el.get("resource"), dict) else {}
            uri = res.get("uri") or el.get("uri") or ""
            mime = res.get("mimeType") or ""
            head = "[resource: %s%s]" % (uri, (" (%s)" % mime) if mime else "")
            inner = res.get("text")
            parts.append(head + (("\n" + str(inner)) if inner else ""))
        elif etype == "resource_link":
            parts.append("[resource_link: %s]" % (el.get("uri") or el.get("name") or ""))
        else:
            # 未知类型：原样给 JSON，但别把上下文撑爆
            try:
                blob = json.dumps(el, ensure_ascii=False)
            except Exception:  # pragma: no cover - 理论上不会发生
                blob = str(el)
            parts.append(blob[:2000])

    return "\n".join(p for p in parts if p != "")


# ══════════════════════════════════════════════════════════════════════
# HTTP 代理决策（本机实测踩出来的坑，别删）
# ══════════════════════════════════════════════════════════════════════
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]")


def _is_local_host(host: str) -> bool:
    h = (host or "").lower()
    return h in _LOCAL_HOSTS or h.startswith("127.") or h.endswith(".localhost")


def _no_proxy_match(host: str, no_proxy: str) -> bool:
    """够用的 NO_PROXY 匹配：`*` / 精确主机 / 后缀（.example.com）。"""
    host = (host or "").lower()
    for raw in (no_proxy or "").split(","):
        pat = raw.strip().lower()
        if not pat:
            continue
        if pat == "*":
            return True
        pat = pat.split(":")[0].lstrip(".")
        if host == pat or host.endswith("." + pat):
            return True
    return False


def _proxy_decision(url: str) -> Tuple[Optional[str], bool, str]:
    """决定这次 HTTP 请求要不要走代理，返回 `(proxy, trust_env, 原因说明)`。

    **为什么不让 httpx 自己决定（trust_env 默认 True）—— 这是实测的坑：**

    `httpx` 在 `trust_env=True` 时会调用 `urllib.request.getproxies()`，而在 Windows 上
    那会去读 **IE/WinINET 的注册表设置**：

        HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings
            ProxyEnable = 1
            ProxyServer = localhost:15236        ← 本机真实存在（隧道工具留下的）

    后果（本机实测，同一台 FastMCP streamable-http 服务器、同一个报文）：

        httpx 默认(trust_env=True)  → 每次请求 **2.05 秒**，偶发 WinError 10054 连接被重置
        httpx trust_env=False       → 每次请求 **0.003 秒**（标准库 http.client 也是 3ms）

    更坑的是 httpx **不认** WinINET 的 `ProxyOverride` 绕过列表（里面明明写了
    `localhost;127.*;<local>`），它只认 `NO_PROXY` 环境变量 —— 所以连
    `http://127.0.0.1:xxxx/mcp` 都会被送去代理绕一圈。

    规则（按优先级）：
        ① 目标是本机（localhost / 127.* / ::1）→ **永不代理**（除非显式 NO_PROXY 之外
           你还非要用代理，那也轮不到本机请求）；
        ② `NO_PROXY` 命中 → 不代理；
        ③ 环境变量里有 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY（按 URL scheme 取）→ 用它，
           并且 trust_env=True（这样 netrc / SSL_CERT_FILE 这些也照旧生效）；
        ④ 都没有 → `trust_env=False`，**不继承注册表里的 IE 代理**。
    """
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    scheme = (parts.scheme or "http").lower()

    if _is_local_host(host):
        return None, False, "目标是本机 %s → 强制不走代理（含不继承 Windows 注册表代理）" % host

    no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    if _no_proxy_match(host, no_proxy):
        return None, False, "命中 NO_PROXY=%r → 不走代理" % no_proxy

    if scheme == "https":
        names = ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")
    else:
        names = ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
    for name in names:
        val = os.environ.get(name)
        if val:
            return val, True, "使用显式环境变量 %s=%s → 走代理（注册表代理被忽略）" % (name, val)

    return None, False, "没有显式代理环境变量 → 不继承 Windows 注册表里的 IE/WinINET 代理（实测能省 2 秒/次）"


# ══════════════════════════════════════════════════════════════════════
# 客户端
# ══════════════════════════════════════════════════════════════════════
class MCPClient:
    """**同步** MCP 客户端。典型用法：

        cfg = MCPServerConfig(name="files", command="npx",
                              args=["-y", "@modelcontextprotocol/server-filesystem", "D:\\\\"])
        with MCPClient(cfg) as cli:                 # with 进入自动 start()，退出一定 close()
            for t in cli.list_tools():
                print(t["name"], t["description"])
            r = cli.call_tool("read_file", {"path": "D:\\\\a.txt"})
            print(r["ok"], r["text"])

    线程安全：一个实例内部的「请求 id 分配 / 待收响应表 / 写管道」都有锁。
    但是**同一实例被多个线程并发 call_tool 并不推荐**（MCP 服务器大多是单线程
    语义，并发调用容易乱序超时）。要并发就每个线程一个 MCPClient 实例。
    """

    def __init__(self, config: MCPServerConfig):
        self.config = config

        # ---- 通用状态 ----
        self._lock = threading.RLock()          # 保护 id 分配 / pending / 状态位
        self._next_id = 1
        self._pending: Dict[Any, "queue.Queue[Any]"] = {}
        self._notifications: List[Dict[str, Any]] = []
        self._closed = False
        self._started = False
        self._server_info: Dict[str, Any] = {}
        self._init_result: Dict[str, Any] = {}
        self._protocol_version = ""             # 服务器实际用的版本（可能与我们发不同）
        self._capabilities: Dict[str, Any] = {}

        # ---- stdio ----
        self._proc: Optional[subprocess.Popen] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._write_lock = threading.Lock()
        self._stderr_tail: "collections.deque[str]" = collections.deque(maxlen=STDERR_TAIL_LINES)
        self._stderr_seen = 0

        # ---- http ----
        self._http: Optional[httpx.Client] = None
        self._session_id = ""

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> Dict[str, Any]:
        """建立传输 → `initialize` → 发 `notifications/initialized` 通知。

        返回服务器信息字典（serverInfo 的字段 + `protocolVersion` / `capabilities`
        / `instructions`）。**幂等**：重复调用直接返回缓存，不会重复握手。

        抛 `MCPTransportError`：连不上/起不来/握手超时。
        抛 `MCPProtocolError`：服务器返回的东西不是合法 MCP（例如 initialize 被拒）。
        """
        with self._lock:
            if self._started:
                return dict(self._server_info)
            if self._closed:
                raise MCPError("MCPClient 已 close()，不能重新 start()；请新建实例")

        transport = (self.config.transport or "stdio").strip().lower()
        if transport == "stdio":
            self._start_stdio()
        elif transport in ("http", "streamable_http", "streamable-http"):
            self._start_http()
        else:
            raise MCPTransportError(
                "不支持的 transport=%r（只支持 'stdio' 与 'http'=Streamable HTTP）" % self.config.transport
            )

        try:
            result = self._initialize()
        except BaseException:
            # 握手失败别把子进程/HTTP 连接漏在那
            try:
                self.close()
            except Exception:  # noqa: BLE001
                logger.debug("[%s] 握手失败后清理时又出错（忽略）", self.config.name, exc_info=True)
            raise

        with self._lock:
            self._started = True
        return dict(self._server_info)

    def close(self) -> None:
        """关闭传输。幂等；任何情况下都不抛异常（清理失败只记 error 日志）。

        stdio 的顺序：关 stdin（让服务器看到 EOF 优雅退出）→ terminate →
        wait(3s) → kill → wait(3s) → 还活着就 Windows 上 taskkill /T /F 兜底
        （有的 MCP 服务器自己还会 fork 子进程，杀进程树才干净）。
        最后 join 读线程，**不留僵尸进程、不留悬挂线程**。
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True

        # 唤醒所有还在等的调用方，别让它们等满超时
        self._fail_all_pending("客户端已关闭（close()）")

        proc = self._proc
        if proc is not None:
            try:
                if proc.stdin is not None:
                    try:
                        proc.stdin.close()
                    except Exception:  # noqa: BLE001
                        logger.debug("[%s] 关 stdin 失败（忽略）", self.config.name, exc_info=True)
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        logger.warning("[%s] terminate 后 3s 没退出，改用 kill", self.config.name)
                        proc.kill()
                        try:
                            proc.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            logger.error(
                                "[%s] kill 后仍未退出 pid=%s，尝试 taskkill /T /F", self.config.name, proc.pid
                            )
                            self._kill_tree_hard(proc.pid)
                rc = proc.poll()
                logger.info("[%s] stdio 子进程已结束 pid=%s returncode=%s", self.config.name, proc.pid, rc)
            except Exception:  # noqa: BLE001
                logger.error("[%s] 关闭 stdio 子进程时出错", self.config.name, exc_info=True)

        for th in (self._stdout_thread, self._stderr_thread):
            if th is not None and th.is_alive() and th is not threading.current_thread():
                th.join(timeout=3.0)
                if th.is_alive():
                    logger.warning("[%s] 读线程 %s 3s 内没结束（通常是子进程还握着管道）", self.config.name, th.name)

        http = self._http
        if http is not None:
            try:
                http.close()
            except Exception:  # noqa: BLE001
                logger.debug("[%s] 关闭 httpx.Client 失败（忽略）", self.config.name, exc_info=True)
            self._http = None

    def __enter__(self) -> "MCPClient":
        """`with` 进入时自动 `start()`。"""
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        """`with` 退出时**一定** `close()`（即使块内抛了异常）。"""
        self.close()
        return False

    @property
    def server_info(self) -> Dict[str, Any]:
        """initialize 拿到的服务器信息（未 start 时是空 dict）。"""
        with self._lock:
            return dict(self._server_info)

    @property
    def protocol_version(self) -> str:
        """服务器实际使用的协议版本（可能低于我们请求的版本 —— 协议允许降级）。"""
        return self._protocol_version

    @property
    def capabilities(self) -> Dict[str, Any]:
        """服务器声明的 capabilities。"""
        return dict(self._capabilities)

    def take_notifications(self) -> List[Dict[str, Any]]:
        """取走并清空已收到的服务器通知（额外便利 API，主流程用不到）。"""
        with self._lock:
            out = list(self._notifications)
            self._notifications.clear()
            return out

    # ------------------------------------------------------------------
    # 公开 API：工具
    # ------------------------------------------------------------------
    def list_tools(self) -> List[Dict[str, Any]]:
        """列出服务器全部工具，**自动翻页**（nextCursor）。

        返回 `[{"name","description","inputSchema", ...}]`：
            · `name` **不加服务器前缀**（前缀由上层拼，例如 `mcp__<server>__<tool>`）；
            · 缺 description/inputSchema 时补 `""` / `{"type":"object","properties":{}}`，
              保证上层拿到的形状稳定；
            · 服务器多给的字段（annotations / outputSchema / title ...）原样保留。
        """
        self._ensure_started("list_tools")

        tools: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        for page in range(MAX_TOOL_PAGES):
            params = {"cursor": cursor} if cursor else {}
            resp = self._request("tools/list", params)
            result = self._unwrap(resp, "tools/list")
            if not isinstance(result, dict):
                raise MCPProtocolError(
                    "tools/list 的 result 应当是对象，实际是 %s：%s" % (type(result).__name__, _short(result))
                )
            raw_tools = result.get("tools")
            if raw_tools is None:
                raw_tools = []
            if not isinstance(raw_tools, list):
                raise MCPProtocolError("tools/list 的 result.tools 应当是数组，实际是 %s" % type(raw_tools).__name__)
            for t in raw_tools:
                if not isinstance(t, dict):
                    logger.warning("[%s] tools/list 第 %d 页有非对象元素，已跳过：%s", self.config.name, page + 1, _short(t))
                    continue
                item = dict(t)
                item["name"] = str(t.get("name") or "")
                item["description"] = str(t.get("description") or "")
                schema = t.get("inputSchema")
                if not isinstance(schema, dict):
                    schema = {"type": "object", "properties": {}}
                item["inputSchema"] = schema
                tools.append(item)

            cursor = result.get("nextCursor") or None
            if not cursor:
                break
            logger.debug("[%s] tools/list 续页 cursor=%s", self.config.name, cursor)
        else:
            logger.warning("[%s] tools/list 翻了 %d 页还有 nextCursor，强制停止", self.config.name, MAX_TOOL_PAGES)

        logger.info("[%s] tools/list 共 %d 个工具", self.config.name, len(tools))
        return tools

    def call_tool(
        self,
        name: str,
        arguments: Dict[str, Any],
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """调用一个工具，返回：

            {"ok": bool, "text": str, "raw": Any, "is_error": bool, "elapsed_ms": int}

        **错误约定（务必按这个写上层）：**

        · **工具自己失败不抛异常。** MCP 里工具报错是正常业务结果，服务器返回
          `isError: true`；本方法如实返回 `ok=False, is_error=True`，错误文本在
          `text` 里（`raw` 是服务器原始 result）。工具不存在、参数校验失败同理。
        · **只有传输/协议层的问题才抛**：`MCPTransportError`（起不来/断线/超时）
          与 `MCPProtocolError`（回包不是合法 MCP）。这两类才代表「这台服务器不可用」。

        参数：
            name      工具名（**不加服务器前缀**）
            arguments 参数对象，直接塞进 `params.arguments`
            timeout   覆盖 `config.timeout`（秒）；用于「这个工具就是很慢」的场景
        """
        self._ensure_started("call_tool")

        t0 = time.perf_counter()
        resp = self._request("tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)

        # JSON-RPC 层错误：服务器明确拒绝（例如 -32601 Method not found / -32602 参数错）。
        # 这仍然是「一次成功的 RPC」，属于工具调用失败而不是通道故障 → ok=False，不抛。
        if "error" in resp and resp.get("error") is not None:
            err = resp.get("error")
            code = err.get("code") if isinstance(err, dict) else None
            msg = err.get("message") if isinstance(err, dict) else str(err)
            text = "MCP 服务器返回 JSON-RPC 错误 code=%s: %s" % (code, msg)
            logger.warning("[%s] call_tool(%s) 被服务器拒绝：%s", self.config.name, name, text)
            return {"ok": False, "text": text, "raw": err, "is_error": True, "elapsed_ms": elapsed_ms}

        result = resp.get("result")
        if not isinstance(result, dict):
            raise MCPProtocolError(
                "tools/call(%s) 的 result 应当是对象，实际是 %s：%s"
                % (name, type(result).__name__, _short(result))
            )

        text = flatten_content(result.get("content"))
        if not text:
            # 有些服务器只给 structuredContent（FastMCP 1.27 对同步工具两者都给）
            sc = result.get("structuredContent")
            if sc is not None:
                try:
                    text = json.dumps(sc, ensure_ascii=False)
                except Exception:  # noqa: BLE001
                    text = str(sc)

        is_error = bool(result.get("isError"))
        if is_error:
            logger.info("[%s] call_tool(%s) 工具报错（正常业务结果，不抛）：%s", self.config.name, name, text[:200])
        else:
            logger.debug("[%s] call_tool(%s) 成功 %dms：%s", self.config.name, name, elapsed_ms, text[:200])

        return {"ok": not is_error, "text": text, "raw": result, "is_error": is_error, "elapsed_ms": elapsed_ms}

    # ------------------------------------------------------------------
    # 内部：启动 stdio
    # ------------------------------------------------------------------
    def _start_stdio(self) -> None:
        cfg = self.config
        if not cfg.command:
            raise MCPTransportError(
                "stdio 传输缺少 command（MCP 配置错误）：服务器 %r 没有指定可执行文件" % cfg.name
            )

        argv, how = self._resolve_command(cfg.command, list(cfg.args or []))

        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in (cfg.env or {}).items()})
        # 只做「没设才设」的兜底：让 python 系服务器说 UTF-8、不缓冲（中文不乱码、不卡）
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.setdefault("PYTHONUTF8", "1")

        kwargs: Dict[str, Any] = dict(
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",     # 服务器吐了非 UTF-8 字节也不要崩，替换掉继续
            bufsize=1,            # 行缓冲：写一行就出去，别攒着
            cwd=cfg.cwd or None,
            env=env,
        )
        # Windows：不弹黑窗（PyInstaller 的 windowed 模式尤其需要）
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if creationflags:
            kwargs["creationflags"] = creationflags

        logger.info("[%s] 启动 stdio 服务器：%s（%s）", cfg.name, " ".join(argv), how)
        try:
            self._proc = subprocess.Popen(argv, **kwargs)
        except FileNotFoundError as e:
            raise MCPTransportError(
                "stdio 子进程起不来：找不到可执行文件 %r（%s）。"
                "请检查 MCPServerConfig.command 是否写对、是否在 PATH 里、"
                "或在 env 里给出绝对路径。原始错误：%s" % (cfg.command, how, e)
            ) from e
        except OSError as e:
            raise MCPTransportError(
                "stdio 子进程起不来：%s（command=%r，%s）。原始错误：%s: %s"
                % (type(e).__name__, cfg.command, how, type(e).__name__, e)
            ) from e

        logger.info("[%s] stdio 子进程已启动 pid=%s", cfg.name, self._proc.pid)

        self._stdout_thread = threading.Thread(
            target=self._stdout_loop, name="mcp-stdout-%s" % cfg.name, daemon=True
        )
        self._stderr_thread = threading.Thread(
            target=self._stderr_loop, name="mcp-stderr-%s" % cfg.name, daemon=True
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    @staticmethod
    def _resolve_command(command: str, args: List[str]) -> Tuple[List[str], str]:
        """把 command 解析成真正能喂给 Popen 的 argv。

        为什么需要：Windows 上 `npx` / `npm` 是 `.cmd` 批处理，CreateProcess
        **不能**直接执行它们（会报 WinError 193）。这里用 `shutil.which` 找到真实
        路径；如果落在 .cmd/.bat，就套一层 `cmd.exe /c`。
        返回 (argv, 说明文字)。
        """
        if os.path.isabs(command) or os.sep in command or (os.altsep and os.altsep in command):
            return [command] + args, "绝对/相对路径，直接用"

        found = shutil.which(command)
        if not found:
            # 交给 Popen 去抛 FileNotFoundError，我们负责把错误翻译成人话
            return [command] + args, "PATH 里没找到（shutil.which 返回 None）"

        if os.name == "nt" and found.lower().endswith((".cmd", ".bat")):
            comspec = os.environ.get("COMSPEC") or "cmd.exe"
            return [comspec, "/c", found] + args, "PATH 命中 %s（批处理，套 cmd /c）" % found
        return [found] + args, "PATH 命中 %s" % found

    # ------------------------------------------------------------------
    # 内部：启动 http
    # ------------------------------------------------------------------
    def _start_http(self) -> None:
        cfg = self.config
        if not cfg.url:
            raise MCPTransportError("http 传输缺少 url（MCP 配置错误）：服务器 %r 没有指定 URL" % cfg.name)

        headers = {
            # Streamable HTTP 要求客户端同时接受这两种（服务器二选一返回）
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": "%s/%s (MCP sync client)" % (CLIENT_NAME, CLIENT_VERSION),
        }
        headers.update({str(k): str(v) for k, v in (cfg.headers or {}).items()})

        proxy, trust_env, why = _proxy_decision(cfg.url)
        logger.info("[%s] HTTP 代理决策：%s", cfg.name, why)

        timeout = float(cfg.timeout or 30.0)
        kwargs: Dict[str, Any] = dict(
            headers=headers,
            timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
            follow_redirects=True,
            # trust_env 见 _proxy_decision 的说明：**不能**让它默认 True 去继承
            # Windows 注册表里的 IE/WinINET 代理，否则连本机服务器都会被代理拖慢。
            trust_env=trust_env,
        )
        if proxy:
            kwargs["proxy"] = proxy
        try:
            self._http = httpx.Client(**kwargs)
        except Exception as e:  # noqa: BLE001
            raise MCPTransportError("创建 httpx.Client 失败：%s: %s" % (type(e).__name__, e)) from e

        logger.info("[%s] 使用 Streamable HTTP：%s", cfg.name, cfg.url)

    def _http_post(self, payload: Dict[str, Any], method: str, timeout: float) -> Optional[Dict[str, Any]]:
        """POST 一条 JSON-RPC 报文。通知（无 id）返回 None。"""
        if self._http is None:
            raise MCPTransportError("HTTP 客户端还没建好就被调用了（内部状态错误）")

        headers: Dict[str, str] = {}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._protocol_version:
            # 2025-06-18 规范：初始化之后，后续 HTTP 请求 MUST 带这个头
            headers["MCP-Protocol-Version"] = self._protocol_version

        try:
            resp = self._http.post(self.config.url, json=payload, headers=headers, timeout=timeout)
        except httpx.TimeoutException as e:
            raise MCPTransportError(
                "HTTP %s 超时（%.1fs，URL=%s，%s）。服务器可能卡住、或在等一个迟迟不回的 SSE 流。"
                % (method, timeout, self.config.url, type(e).__name__)
            ) from e
        except httpx.HTTPError as e:
            raise MCPTransportError(
                "HTTP %s 失败（URL=%s）：%s: %s" % (method, self.config.url, type(e).__name__, e)
            ) from e

        # initialize 的响应里可能给会话 id，后续必须回传
        sid = resp.headers.get("mcp-session-id")
        if sid:
            if sid != self._session_id:
                logger.debug("[%s] 记录 Mcp-Session-Id=%s", self.config.name, sid)
            self._session_id = sid

        if resp.status_code == 404 and self._session_id:
            raise MCPTransportError(
                "HTTP %s 返回 404：Mcp-Session-Id=%s 可能已过期（服务器重启/会话超时），"
                "需要重新 initialize。URL=%s" % (method, self._session_id, self.config.url)
            )
        if resp.status_code == 202 and "id" not in payload:
            return None  # 通知被接受，规范允许 202 空体
        if resp.status_code >= 400:
            body = (resp.text or "")[:500]
            raise MCPTransportError(
                "HTTP %s 返回 %d %s（URL=%s）。响应体前 500 字：%s"
                % (method, resp.status_code, resp.reason_phrase, self.config.url, body or "<空>")
            )

        want_id = payload.get("id")
        ctype = (resp.headers.get("content-type") or "").lower()
        text = resp.text or ""
        if not text.strip():
            if want_id is None:
                return None
            raise MCPProtocolError(
                "HTTP %s 返回了 %d 但响应体是空的（content-type=%s），无法解析 JSON-RPC 响应"
                % (method, resp.status_code, ctype or "<无>")
            )

        if "text/event-stream" in ctype:
            return self._parse_sse(text, want_id, method)

        try:
            msg = json.loads(text)
        except json.JSONDecodeError as e:
            raise MCPProtocolError(
                "HTTP %s 的响应不是合法 JSON（content-type=%s）：%s；响应体前 300 字：%s"
                % (method, ctype or "<无>", e, text[:300])
            ) from e
        if not isinstance(msg, dict):
            raise MCPProtocolError("HTTP %s 的响应不是 JSON 对象：%s" % (method, _short(msg)))
        return msg

    def _parse_sse(self, text: str, want_id: Any, method: str) -> Dict[str, Any]:
        """解析 SSE 文本，挑出 id 匹配的那条 JSON-RPC 响应。

        SSE 帧格式：`data: {json}`，事件之间用空行分隔；`:comment` 忽略。
        一个流里可能先来几条 notification（无 id）再来响应，所以不能只看第一条。
        """
        events: List[Dict[str, Any]] = []
        buf: List[str] = []

        def _flush() -> None:
            if not buf:
                return
            payload = "\n".join(buf)
            buf.clear()
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                logger.warning("[%s] SSE 里有解析不了的 data 段，已跳过：%s", self.config.name, payload[:200])
                return
            if isinstance(obj, dict):
                events.append(obj)

        for raw in text.splitlines():
            line = raw.rstrip("\r")
            if line == "":
                _flush()
            elif line.startswith(":"):
                continue
            elif line.startswith("data:"):
                buf.append(line[5:].lstrip())
            # event: / id: / retry: 对 JSON-RPC 无意义，忽略
        _flush()

        for ev in events:
            if "id" in ev and ev.get("id") == want_id and ("result" in ev or "error" in ev):
                return ev
        # id 对不上：把通知收下，然后报协议错（附上看到的东西，方便定位）
        for ev in events:
            if "method" in ev:
                with self._lock:
                    self._notifications.append(ev)
        if events:
            for ev in events:
                if "result" in ev or "error" in ev:
                    logger.warning(
                        "[%s] SSE 响应的 id=%r 与请求 id=%r 不一致，仍然采用它", self.config.name, ev.get("id"), want_id
                    )
                    return ev
        raise MCPProtocolError(
            "HTTP %s 返回的是 text/event-stream，但 %d 个事件里没有 id=%r 的 JSON-RPC 响应。原始 SSE 前 400 字：%s"
            % (method, len(events), want_id, text[:400])
        )

    # ------------------------------------------------------------------
    # 内部：stdio 读写线程
    # ------------------------------------------------------------------
    def _stdout_loop(self) -> None:
        """读线程：逐行读 stdout，解析 NDJSON，按 id 投递到等待队列。

        （stdio 帧格式是「一行一个 JSON」，**不是** LSP 的 Content-Length 头。）
        """
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        name = self.config.name
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    # 不规范但很常见的服务器：把日志混进了 stdout。记下来，别崩。
                    logger.warning("[%s] stdout 收到非 JSON 的行（已忽略）：%s", name, line[:300])
                    continue
                if not isinstance(msg, dict):
                    logger.warning("[%s] stdout 收到非对象的 JSON（已忽略）：%s", name, line[:300])
                    continue

                mid = msg.get("id")
                if "method" in msg:
                    # 服务器通知（notifications/*），也可能是服务器发起的请求
                    # （sampling/createMessage 之类）—— 本客户端没实现那些能力，
                    # 先原样收着，不假装处理。
                    logger.debug("[%s] 收到服务器通知/请求：%s", name, msg.get("method"))
                    with self._lock:
                        self._notifications.append(msg)
                elif mid is not None:
                    # 有 id 且没有 method = 响应。**注意这里不检查有没有 result/error**：
                    # 有些服务器会回一条既无 result 也无 error 的报文，那属于协议层故障，
                    # 必须原样抛给等待方，让它立刻抛 MCPProtocolError ——
                    # 不能在这儿丢掉，否则调用方只能干等到超时，把「协议错」误报成「超时」。
                    with self._lock:
                        q = self._pending.get(mid)
                    if q is not None:
                        q.put(msg)
                    else:
                        logger.debug("[%s] 收到迟到/无人认领的响应 id=%r（多半是超时后才回来）", name, mid)
                else:
                    logger.warning("[%s] stdout 收到既不是响应也不是通知的报文（已忽略）：%s", name, line[:300])
        except Exception:  # noqa: BLE001
            logger.debug("[%s] stdout 读线程异常退出", name, exc_info=True)
        finally:
            # stdout 关闭 = 服务器要走了：立刻唤醒所有等待者，别让他们干等到超时
            self._fail_all_pending("stdio 服务器的 stdout 已关闭（子进程退出/崩溃）")

    def _stderr_loop(self) -> None:
        """stderr 必须有人读，否则子进程写日志写满管道缓冲区会**死锁**。"""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        name = self.config.name
        try:
            for line in proc.stderr:
                line = line.rstrip("\n")
                if not line:
                    continue
                self._stderr_tail.append(line)
                self._stderr_seen += 1
                logger.debug("[%s][stderr] %s", name, line)
        except Exception:  # noqa: BLE001
            logger.debug("[%s] stderr 读线程异常退出", name, exc_info=True)

    def _fail_all_pending(self, reason: str) -> None:
        with self._lock:
            pending = list(self._pending.items())
            self._pending.clear()
        for mid, q in pending:
            try:
                q.put(_EOF)
            except Exception:  # noqa: BLE001
                pass
        if pending:
            logger.debug("[%s] 唤醒 %d 个等待中的请求：%s", self.config.name, len(pending), reason)

    def _diag(self) -> str:
        """出错时给出**真实原因**：进程状态 + stderr 尾巴。"""
        proc = self._proc
        bits: List[str] = []
        if proc is not None:
            rc = proc.poll()
            if rc is None:
                bits.append("子进程仍在运行 pid=%s" % proc.pid)
            else:
                bits.append("子进程已退出 pid=%s returncode=%s" % (proc.pid, rc))
        tail = list(self._stderr_tail)
        if tail:
            bits.append(
                "stderr 共 %d 行，末 %d 行：\n    %s"
                % (self._stderr_seen, len(tail), "\n    ".join(tail[-20:]))
            )
        elif self._stderr_seen == 0:
            bits.append("stderr 没有任何输出")
        if self._session_id:
            bits.append("Mcp-Session-Id=%s" % self._session_id)
        return "；".join(bits)

    def _kill_tree_hard(self, pid: int) -> None:
        """最后手段：Windows 上连子孙进程一起强杀。"""
        if os.name != "nt":
            return
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            logger.warning("[%s] 已对 pid=%s 执行 taskkill /T /F", self.config.name, pid)
        except Exception:  # noqa: BLE001
            logger.error("[%s] taskkill pid=%s 失败", self.config.name, pid, exc_info=True)

    # ------------------------------------------------------------------
    # 内部：JSON-RPC 请求 / 通知
    # ------------------------------------------------------------------
    def _alloc_id(self) -> int:
        with self._lock:
            mid = self._next_id
            self._next_id += 1
            return mid

    def _ensure_started(self, who: str) -> None:
        if self._started:
            return
        if self._closed:
            raise MCPError("MCPClient 已 close()，不能再 %s()；请新建实例" % who)
        # 图省事：没 start 就直接用，自动补一次握手
        logger.debug("[%s] 还没 start() 就调用了 %s()，自动握手", self.config.name, who)
        self.start()

    def _request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """发一条有 id 的请求并等响应（stdio 走队列，http 走 POST）。"""
        if self._closed:
            raise MCPTransportError("MCPClient 已 close()，无法发送 %s" % method)
        tmo = float(timeout if timeout is not None else (self.config.timeout or 30.0))
        mid = self._alloc_id()
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            payload["params"] = params

        if self._http is not None:
            t0 = time.perf_counter()
            resp = self._http_post(payload, method, tmo)
            if resp is None:
                raise MCPProtocolError("HTTP %s 没有返回响应体" % method)
            if resp.get("id") != mid:
                logger.debug("[%s] %s 响应 id=%r 与请求 id=%r 不一致", self.config.name, method, resp.get("id"), mid)
            logger.debug("[%s] %s 完成 %dms", self.config.name, method, int((time.perf_counter() - t0) * 1000))
            return resp

        # ------- stdio -------
        q: "queue.Queue[Any]" = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[mid] = q
        try:
            self._write_stdio(payload, method)
            try:
                item = q.get(timeout=tmo)
            except queue.Empty:
                raise MCPTransportError(
                    "stdio 调用 %s 超时：%.1fs 内没收到 id=%d 的响应（%s）"
                    % (method, tmo, mid, self._diag())
                ) from None
            if item is _EOF:
                raise MCPTransportError(
                    "stdio 调用 %s 时连接断了：%s" % (method, self._diag())
                )
            return item
        finally:
            with self._lock:
                self._pending.pop(mid, None)

    def _notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        """发通知（无 id，不等响应）。失败要吵，因为多半意味着通道坏了。"""
        if self._closed:
            return
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params

        if self._http is not None:
            self._http_post(payload, method, float(self.config.timeout or 30.0))
            return
        self._write_stdio(payload, method)

    def _write_stdio(self, payload: Dict[str, Any], method: str) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise MCPTransportError("stdio 还没启动就发报文（内部状态错误）：method=%s" % method)
        if proc.poll() is not None:
            raise MCPTransportError(
                "stdio 子进程已经退出，无法发送 %s：%s" % (method, self._diag())
            )
        raw = json.dumps(payload, ensure_ascii=False)  # NDJSON：一行一个 JSON，中文不转义
        try:
            with self._write_lock:
                proc.stdin.write(raw + "\n")
                proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as e:
            raise MCPTransportError(
                "stdio 写管道失败（%s: %s），发送 %s 失败：%s" % (type(e).__name__, e, method, self._diag())
            ) from e
        logger.debug("[%s] >>> %s", self.config.name, raw[:400])

    # ------------------------------------------------------------------
    # 内部：协议
    # ------------------------------------------------------------------
    def _initialize(self) -> Dict[str, Any]:
        """initialize → 记录版本/能力 → 发 initialized 通知。"""
        name = self.config.name
        resp = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
        )
        result = self._unwrap(resp, "initialize")
        if not isinstance(result, dict):
            raise MCPProtocolError(
                "initialize 的 result 应当是对象，实际是 %s：%s" % (type(result).__name__, _short(result))
            )

        server_proto = str(result.get("protocolVersion") or "")
        if server_proto and server_proto != PROTOCOL_VERSION:
            # 协议允许降级：服务器给别的版本**不是错误**，按它的版本继续，只记一笔
            logger.warning(
                "[%s] 服务器返回 protocolVersion=%s，我们请求的是 %s；"
                "按服务器的版本继续（协议允许降级，不报错）",
                name, server_proto, PROTOCOL_VERSION,
            )
        self._protocol_version = server_proto or PROTOCOL_VERSION

        caps = result.get("capabilities")
        self._capabilities = caps if isinstance(caps, dict) else {}

        server_info = result.get("serverInfo")
        server_info = server_info if isinstance(server_info, dict) else {}
        if not server_info:
            logger.warning("[%s] initialize 没有返回 serverInfo（服务器不完全合规），用配置名兜底", name)
            server_info = {"name": self.config.name, "version": ""}

        info: Dict[str, Any] = dict(server_info)
        info.setdefault("protocolVersion", self._protocol_version)
        info.setdefault("capabilities", self._capabilities)
        if result.get("instructions"):
            info.setdefault("instructions", result["instructions"])
        self._init_result = result
        self._server_info = info

        # 通知：无 id，服务器不回。收到错也无所谓 —— 但发了才算握手完成。
        self._notify("notifications/initialized")

        if "tools" not in self._capabilities:
            logger.warning(
                "[%s] 服务器 capabilities 里没有 tools（%s），tools/list 可能失败",
                name, list(self._capabilities.keys()) or "空",
            )

        logger.info(
            "[%s] 握手完成：server=%s version=%s protocol=%s",
            name, info.get("name"), info.get("version"), self._protocol_version,
        )
        return result

    def _unwrap(self, resp: Dict[str, Any], method: str) -> Any:
        """从 JSON-RPC 响应里取 result；有 error 就抛协议错（带服务器原话）。"""
        if "error" in resp and resp.get("error") is not None:
            err = resp.get("error")
            if isinstance(err, dict):
                raise MCPProtocolError(
                    "MCP 服务器拒绝了 %s：code=%s message=%s data=%s"
                    % (method, err.get("code"), err.get("message"), _short(err.get("data")))
                )
            raise MCPProtocolError("MCP 服务器拒绝了 %s，返回的 error 不是对象：%s" % (method, _short(err)))
        if "result" not in resp:
            raise MCPProtocolError(
                "%s 的响应里既没有 result 也没有 error（不是合法 JSON-RPC 响应）：%s" % (method, _short(resp))
            )
        return resp.get("result")


# ══════════════════════════════════════════════════════════════════════
# 小工具
# ══════════════════════════════════════════════════════════════════════
class _Eof:
    """哨兵：告诉等待中的调用方「管道断了」，不用等满超时。"""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover
        return "<MCP-EOF>"


_EOF = _Eof()


def _short(obj: Any, limit: int = 300) -> str:
    """把任意对象压成一行短字符串，用于错误信息。"""
    if isinstance(obj, str):
        s = obj
    else:
        try:
            s = json.dumps(obj, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            s = repr(obj)
    return s if len(s) <= limit else s[:limit] + "...(截断)"


__all__ = [
    "MCPError",
    "MCPTransportError",
    "MCPProtocolError",
    "MCPServerConfig",
    "MCPClient",
    "flatten_content",
    "PROTOCOL_VERSION",
    "CLIENT_NAME",
    "CLIENT_VERSION",
]