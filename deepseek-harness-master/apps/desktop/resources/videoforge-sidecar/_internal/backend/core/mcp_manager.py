# -*- coding: utf-8 -*-
"""VideoForge · MCP 服务管理（注册 / 连接池 / 工具暴露给 Agent）

═══════════════════════════════════════════════════════════════
这个模块解决什么
═══════════════════════════════════════════════════════════════
`core/mcp_client.py` 是**协议层**（一次连接、发请求、收结果）。
本模块是**平台层**：把「用户配了哪些 MCP 服务器」变成一个可管理、
可持久化、能被 Agent 真正调用的东西。

之前项目里只有一个写死的可灵 MCP（`core/kling_mcp.py`，OAuth + 固定服务器）。
本模块是**通用**的那一层：任何 MCP 服务器都能加进来。

═══════════════════════════════════════════════════════════════
工具怎么暴露给模型：为什么用「元工具」而不是每个工具注册一个
═══════════════════════════════════════════════════════════════
一个 MCP 服务器常有 10~30 个工具，多个服务器就是上百个。如果把每个都注册成
一条 agent 工具，工具目录会**直接撑爆提示词预算**（本仓 agent 的目录是
全量塞进系统提示词的），而且大部分和当前任务无关。

所以暴露 4 个元工具：
    mcp_servers()                     —— 有哪些服务器、连不连得上
    mcp_tools(server)                 —— 某个服务器有哪些工具（含参数 schema）
    mcp_call(server, tool, arguments) —— 真正调用
    mcp_add_server / mcp_remove_server / mcp_set_enabled

★ 但只有元工具还不够：模型不知道该去 `mcp_tools` 查什么。
  所以 `prompt_summary()` 会把**已连接服务器 + 它们的工具名和一句话说明**
  注入 agent 系统提示词（只注入名字和首句，不注入完整 schema）。
  两层合起来 = 模型知道有什么（提示词），需要细节时再查（元工具）。

═══════════════════════════════════════════════════════════════
安全：添加服务器等于允许执行本机程序
═══════════════════════════════════════════════════════════════
MCP 的 stdio 传输就是「启动一个子进程，通过 stdin/stdout 跟它说话」。
`mcp_add_server` 因此**等同于允许在本机执行任意命令**。

本模块的处理：
  · 所有会改配置的工具都标 `writes=True` —— 前端的确认机制据此弹确认框；
  · 名字做白名单校验（会进工具名和文件名，禁止路径穿越）；
  · **不做**任何"看起来安全"的假过滤（比如按命令名黑名单）——
    那种过滤既拦不住真正的恶意，又会让正常用法莫名失败。
    真正的边界是：只有用户能加服务器，且加的时候会被明确告知。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("videoforge.mcp")

CONFIG_FILE = "mcp_servers.json"
# 工具名前缀，避免和内置工具撞名（也让人一眼看出来源）
TOOL_PREFIX = "mcp__"

_NAME_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff.-]{0,47}$")

# 连接空闲多久后主动断开（stdio 服务器是常驻子进程，不能一直占着）
IDLE_CLOSE_SECONDS = 900.0


class MCPConfigError(Exception):
    """配置问题（名字非法、重复、找不到等），是可修正的用户错误。"""


# ──────────────────────── 配置持久化 ────────────────────────


def _default_config_path(data_dir: Optional[str] = None) -> Path:
    d = data_dir or os.environ.get("VIDEOFORGE_DATA_DIR") or os.getcwd()
    return Path(d) / CONFIG_FILE


def load_configs(path: Optional[Path] = None,
                 data_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """读服务器配置。文件不存在/坏了都返回空列表 —— 但不静默：
    坏文件会被改名留档，并记 error 日志，避免"配置莫名全没了"。"""
    p = path or _default_config_path(data_dir)
    if not p.is_file():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception as e:
        broken = p.with_suffix(p.suffix + ".broken-%s" % time.strftime("%Y%m%d-%H%M%S"))
        try:
            p.replace(broken)
            logger.error("MCP 配置解析失败（原文件已留档到 %s）：%s", broken, e)
        except Exception:
            logger.error("MCP 配置解析失败且留档也失败：%s", e)
        return []
    if isinstance(raw, dict):
        raw = raw.get("servers") or []
    if not isinstance(raw, list):
        logger.error("MCP 配置顶层不是数组（实际 %s），按空处理", type(raw).__name__)
        return []
    return [c for c in raw if isinstance(c, dict)]


def save_configs(configs: List[Dict[str, Any]], path: Optional[Path] = None,
                 data_dir: Optional[str] = None) -> Path:
    p = path or _default_config_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(configs, ensure_ascii=False, indent=2),
                 encoding="utf-8", newline="\n")
    return p


def validate_name(name: str) -> str:
    n = str(name or "").strip()
    if not _NAME_RE.match(n):
        raise MCPConfigError(
            "服务器名不合法：%r。只允许中文/字母/数字/下划线/连字符/点，"
            "1-48 个字符，且不能含路径分隔符。" % name)
    return n


#: 判断"两份配置实质相同"时要逐项比的键。
#: ★ 特意**不含 `enabled`** —— 启用/停用有专门的工具（`mcp_set_enabled`），
#:   重新 add 一份一样的配置不该因为开关状态不同就被判成"冲突"。
_CFG_CMP_KEYS = ("transport", "command", "args", "env", "cwd",
                 "url", "headers", "timeout")


def _same_config(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """两份配置是不是**实质相同**（忽略 enabled，见上）。"""
    if str(a.get("name")) != str(b.get("name")):
        return False
    for k in _CFG_CMP_KEYS:
        if (json.dumps(a.get(k), sort_keys=True, ensure_ascii=False)
                != json.dumps(b.get(k), sort_keys=True, ensure_ascii=False)):
            return False
    return True


def _brief_config(c: Dict[str, Any]) -> str:
    """一句话概括一份配置 —— 报错时给用户看，别让他去翻 json 文件。"""
    if str(c.get("transport")) == "http":
        return "http %s" % c.get("url")
    argv = " ".join([str(c.get("command") or "")]
                    + [str(x) for x in (c.get("args") or [])])
    return "stdio %s" % argv.strip()


def make_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    """把用户输入规范化成一份配置。**缺什么就明确报错，不猜默认值。**"""
    name = validate_name(payload.get("name"))
    transport = str(payload.get("transport") or "stdio").strip().lower()
    if transport not in ("stdio", "http"):
        raise MCPConfigError(
            "transport 只支持 stdio 或 http，收到 %r。"
            "（stdio＝启动本机子进程；http＝连一个 Streamable HTTP 端点）" % transport)

    cfg: Dict[str, Any] = {"name": name, "transport": transport,
                           "enabled": bool(payload.get("enabled", True)),
                           "timeout": float(payload.get("timeout") or 30.0)}
    if transport == "stdio":
        cmd = str(payload.get("command") or "").strip()
        if not cmd:
            raise MCPConfigError(
                "stdio 传输必须给 `command`（要启动的可执行文件）。"
                "例如 npx / uvx / python / node，或一个绝对路径。")
        args = payload.get("args") or []
        if isinstance(args, str):
            args = [a for a in re.split(r"\s+", args.strip()) if a]
        cfg["command"] = cmd
        cfg["args"] = [str(a) for a in args]
        cfg["env"] = {str(k): str(v) for k, v in (payload.get("env") or {}).items()}
        cfg["cwd"] = str(payload.get("cwd") or "")
    else:
        url = str(payload.get("url") or "").strip()
        if not url:
            raise MCPConfigError("http 传输必须给 `url`（Streamable HTTP 端点，如 http://127.0.0.1:8000/mcp）")
        if not re.match(r"^https?://", url):
            raise MCPConfigError("url 必须以 http:// 或 https:// 开头，收到 %r" % url)
        cfg["url"] = url
        cfg["headers"] = {str(k): str(v) for k, v in (payload.get("headers") or {}).items()}
    return cfg


# ──────────────────────── 连接池 ────────────────────────


class _Pooled:
    __slots__ = ("client", "last_used", "tools", "tools_at", "error")

    def __init__(self, client: Any) -> None:
        self.client = client
        self.last_used = time.time()
        self.tools: List[Dict[str, Any]] = []
        self.tools_at = 0.0
        self.error = ""


def _close_victim(name: str, victim: Optional["_Pooled"]) -> None:
    """关闭一个已经从连接池里**摘出来**的连接。

    ★ 单独抽成模块级函数，是为了让"关闭必须在锁外"这件事在**调用点**上
      一眼可见：`_close_victim(...)` 永远出现在 `with self._lock:` 块**之后**。
      写在类里做成方法的话，很容易又被顺手放进锁里
      （本轮修的就是这个：`add_server` / `remove_server` / `set_enabled`
        原来都在锁内 `close()`，而 close 要 terminate 子进程 + join 线程）。
    """
    if victim is None:
        return
    try:
        victim.client.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("关闭 MCP 服务器 %r 失败（忽略）：%s", name, e)


class MCPManager:
    """管理 MCP 服务器的配置、连接与工具调用。

    ══════════════════════════════════════════════════════════════════
    ★★ 锁的纪律（2026-09-26 修）：**绝不在持锁期间做阻塞 I/O**
    ══════════════════════════════════════════════════════════════════
    原来 `list_tools()` / `call()` 都是整个方法包在 `with self._lock` 里，
    于是这些**跨进程**操作全在锁内发生：

      · `_ensure()` 启动子进程 + 握手（超时默认 30 秒）
      · `client.list_tools()` 的一次管道往返
      · `client.call_tool()` —— **整个工具执行**，可能是几分钟
        （浏览器自动化类的 MCP 工具就是这种）

    后果：一个慢工具会让**所有** MCP 操作排队等它 ——
    包括界面上"刷新服务器列表"这种本该毫秒返回的请求。
    FastAPI 的同步端点跑在线程池里，几个并发 MCP 调用就能把线程池占满，
    表现成"整个界面卡住"。

    现在的分工：
      · 锁只保护**共享状态的读写**（`_pool` / `_configs` / `_connecting`）；
      · 启动、握手、列工具、调工具**全部在锁外**；
      · 用 `_connecting[name]` 这个 Event 保证"同一个服务器同一时刻
        只有一个线程在连"，其余线程在**锁外**等它 ——
        否则并发请求会各自拉一个子进程，留下多个孤儿。

    配置改动仍然立即落盘（不做"退出时保存"那种容易丢数据的做法）。
    """

    def __init__(self, data_dir: Optional[str] = None,
                 config_path: Optional[Path] = None) -> None:
        self.data_dir = data_dir or os.environ.get("VIDEOFORGE_DATA_DIR") or os.getcwd()
        self.config_path = config_path or _default_config_path(self.data_dir)
        self._lock = threading.RLock()
        self._pool: Dict[str, _Pooled] = {}
        self._configs: List[Dict[str, Any]] = []
        self._loaded_at = 0.0
        # ★ 同一服务器同一时刻只允许一个线程去连（其余在锁外等 Event）
        self._connecting: Dict[str, threading.Event] = {}
        # ★ 记录最近一次连接失败原因，供等待者如实回报（而不是笼统说"连不上"）
        self._connect_error: Dict[str, str] = {}

    # ── 配置 ──

    def configs(self, *, refresh: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            if refresh or not self._loaded_at:
                self._configs = load_configs(self.config_path)
                self._loaded_at = time.time()
            return [dict(c) for c in self._configs]

    def find(self, name: str) -> Optional[Dict[str, Any]]:
        n = str(name or "").strip()
        for c in self.configs():
            if str(c.get("name")) == n:
                return c
        return None

    def add_server(self, payload: Dict[str, Any], *, overwrite: bool = False) -> Dict[str, Any]:
        cfg = make_config(payload)
        victim = None
        with self._lock:
            self.configs(refresh=True)
            exist = next((c for c in self._configs
                          if str(c.get("name")) == cfg["name"]), None)
            if exist is not None and not overwrite:
                # ★★ 2026-09-26 第 41 轮：**同名 + 同配置 = 幂等成功**，不再报错。
                #
                # 用户实测（截图）：让 agent 接一个 filesystem MCP，它把同一条
                # add 调了两次（模型自己重试/重复表达很常见），第二次弹回
                #   「已经有一个叫 'filesystem' 的 MCP 服务器了。要替换请传 overwrite=true。」
                # 并把决定权踢回用户 —— 而实际上**什么都没变、也没坏**。
                # 用户看到的是"接个 MCP 还要我做决策"。
                #
                # 现在：配置逐字相同 → 返回 ok + `already=True`，当成功，不打扰用户；
                #       配置**不同** → 仍然报错要求 overwrite=true（那才是真冲突，
                #       必须让人来判断，别让模型悄悄换掉一个能连的配置）。
                if _same_config(exist, cfg):
                    logger.info("MCP 服务器 %r 已存在且配置相同 —— 幂等，不做改动",
                                cfg["name"])
                    _en = "启用" if exist.get("enabled", True) else "停用"
                    return {**exist, "already": True,
                            "note": ("已经有一个同名的 MCP 服务器，配置和这次**完全一样**"
                                     "—— 没有做任何改动，直接用它就行。"
                                     "（它现在是%s状态；要改状态用 mcp_set_enabled）" % _en)}
                raise MCPConfigError(
                    "已经有一个叫 %r 的 MCP 服务器了，而且**配置不一样**。"
                    "要换成新的就传 overwrite=true；想保留旧的就别再添加。"
                    "（现有的是：%s）" % (cfg["name"], _brief_config(exist)))
            if exist is not None:
                # ★ 摘出旧的连接，**关闭放到锁外**（close 要 terminate 子进程）
                victim = self._pool.pop(str(cfg["name"]), None)
                self._configs = [c for c in self._configs
                                 if str(c.get("name")) != cfg["name"]]
            self._configs.append(cfg)
            save_configs(self._configs, self.config_path)
        _close_victim(cfg["name"], victim)
        logger.info("加入 MCP 服务器 %r（%s）", cfg["name"], cfg["transport"])
        return cfg

    def remove_server(self, name: str) -> Dict[str, Any]:
        n = str(name or "").strip()
        victim = None
        with self._lock:
            self.configs(refresh=True)
            before = len(self._configs)
            self._configs = [c for c in self._configs if str(c.get("name")) != n]
            if len(self._configs) == before:
                raise MCPConfigError("没有名为 %r 的 MCP 服务器" % n)
            victim = self._pool.pop(n, None)
            save_configs(self._configs, self.config_path)
            remaining = len(self._configs)
        _close_victim(n, victim)
        return {"name": n, "removed": True, "remaining": remaining}

    def set_enabled(self, name: str, enabled: bool) -> Dict[str, Any]:
        n = str(name or "").strip()
        victim = None
        with self._lock:
            self.configs(refresh=True)
            hit = None
            for c in self._configs:
                if str(c.get("name")) == n:
                    c["enabled"] = bool(enabled)
                    hit = c
                    break
            if hit is None:
                raise MCPConfigError("没有名为 %r 的 MCP 服务器" % n)
            if not enabled:
                victim = self._pool.pop(n, None)
            save_configs(self._configs, self.config_path)
        _close_victim(n, victim)
        return {"name": n, "enabled": bool(enabled)}

    # ── 连接 ──

    def _client_module(self):
        from . import mcp_client
        return mcp_client

    def _build_client(self, cfg: Dict[str, Any]):
        mc = self._client_module()
        conf = mc.MCPServerConfig(
            name=str(cfg.get("name")),
            transport=str(cfg.get("transport") or "stdio"),
            command=str(cfg.get("command") or ""),
            args=[str(a) for a in (cfg.get("args") or [])],
            env={str(k): str(v) for k, v in (cfg.get("env") or {}).items()},
            cwd=str(cfg.get("cwd") or ""),
            url=str(cfg.get("url") or ""),
            headers={str(k): str(v) for k, v in (cfg.get("headers") or {}).items()},
            enabled=True,
            timeout=float(cfg.get("timeout") or 30.0),
        )
        return mc.MCPClient(conf)

    def _ensure(self, name: str, *, autostart: bool = True) -> Tuple[Optional[_Pooled], str]:
        """拿到（必要时建立）某个服务器的连接。返回 (pooled, 失败原因)。

        ★ 启动与握手是**阻塞**的（起子进程 + 走 MCP 生命周期，默认超时 30 秒），
          所以它们全部发生在 `self._lock` **外面**。
          锁只用来：读配置、查/写连接池、登记 `_connecting`。
        """
        mc = self._client_module()
        name = str(name)

        # ── ① 锁内：只做状态判断与"认领连接权" ──
        with self._lock:
            cfg = self.find(name)
            if cfg is None:
                return None, "没有名为 %r 的 MCP 服务器（用 mcp_add_server 先加）" % name
            if not cfg.get("enabled", True):
                return None, "MCP 服务器 %r 已被禁用（用 mcp_set_enabled 打开）" % name
            p = self._pool.get(name)
            if p is not None:
                p.last_used = time.time()
                return p, ""
            if not autostart:
                return None, "MCP 服务器 %r 尚未连接" % name
            wait_budget = float(cfg.get("timeout") or 30.0) + 15.0
            ev = self._connecting.get(name)
            if ev is None:
                ev = threading.Event()
                self._connecting[name] = ev
                i_am_connector = True
            else:
                i_am_connector = False

        # ── ② 锁外：别人正在连 → 等他，不重复拉子进程 ──
        if not i_am_connector:
            if not ev.wait(timeout=wait_budget):
                return None, ("另一个请求正在连接 MCP 服务器 %r，等了 %ds 还没好。"
                              "该服务器可能启动很慢或卡住了。"
                              % (name, int(wait_budget)))
            with self._lock:
                p = self._pool.get(name)
                if p is not None:
                    p.last_used = time.time()
                    return p, ""
                err = self._connect_error.get(name, "")
            return None, err or ("连接 MCP 服务器 %r 失败（另一个请求刚试过，未留下原因）"
                                 % name)

        # ── ③ 锁外：我是连接者，做真正阻塞的事 ──
        cli = None
        err = ""
        try:
            cli = self._build_client(cfg)
            cli.start()
        except mc.MCPError as e:
            err = "连接 MCP 服务器 %r 失败：%s" % (name, e)
        except Exception as e:  # noqa: BLE001
            err = "连接 MCP 服务器 %r 时出现意外错误：%s: %s" % (
                name, type(e).__name__, e)

        # ── ④ 锁内：登记结果；无论成败都必须 set() 唤醒等待者 ──
        pooled: Optional[_Pooled] = None
        try:
            with self._lock:
                if cli is not None and not err:
                    pooled = _Pooled(cli)
                    self._pool[name] = pooled
                    self._connect_error.pop(name, None)
                else:
                    self._connect_error[name] = err or "未知原因"
        except Exception as e:  # noqa: BLE001
            # 登记本身失败（理论上不该发生）也要如实报，不能返回 (None, "")
            err = err or ("登记 MCP 服务器 %r 的连接状态失败：%s: %s"
                          % (name, type(e).__name__, e))
        finally:
            # ★ 必须在 finally 里清登记 + set()：
            #   否则异常路径下等待者会一直等到超时，而且 `_connecting` 会永久残留，
            #   让这台服务器**再也连不上**（后续线程只会去等一个永不 set 的 Event）。
            with self._lock:
                self._connecting.pop(name, None)
            ev.set()

        if err or pooled is None:
            err = err or ("MCP 服务器 %r 连接后没有拿到可用连接（未知原因）" % name)
            # ★ 起了一半的 client 要关掉，否则留下孤儿子进程
            #   （本仓刚因为孤儿 cloudflared 吃过一次亏）
            if cli is not None:
                try:
                    cli.close()
                except Exception as e:  # noqa: BLE001
                    logger.warning("关闭连接失败的 MCP 客户端 %r 时出错（忽略）：%s", name, e)
            logger.warning("MCP 服务器 %r 连接失败：%s", name, err)
            return None, err
        logger.info("MCP 服务器 %r 已连接", name)
        return pooled, ""

    # ★ 这里原本有一个 `_close_one(name)`：它自己取锁、然后在**锁内** close()。
    #   本轮把它删了 —— 理由是它就是"锁内做阻塞 I/O"这个坑的载体：
    #     · 它内部取的是 RLock，从已持锁的地方调它**不会报错**，
    #       于是 close()（terminate 子进程 + join 线程）就在锁内跑了；
    #     · 关连接现在**只有一种写法**：锁内 `pop`、锁外 `_close_victim`。
    #   如果还有别处在调 `_close_one`，会立刻 AttributeError —— 响亮地坏掉，
    #   比留着一个会把锁占住的"兼容方法"安全。

    def close_all(self) -> int:
        """关掉所有连接。返回关掉的数量。

        ★ 先在锁内**摘出**要关的条目，再在锁外真正 close()
          （close 要 terminate 子进程 + join 线程，不该占着锁）。
        """
        with self._lock:
            victims = list(self._pool.items())
            self._pool.clear()
        for name, p in victims:
            try:
                p.client.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("关闭 MCP 服务器 %r 失败（忽略）：%s", name, e)
        return len(victims)

    def reap_idle(self, *, max_idle: float = IDLE_CLOSE_SECONDS) -> int:
        """关掉空闲太久的连接。stdio 服务器是常驻子进程，不该永久占着。

        ★ 同样：锁内只挑人，锁外再关。
        """
        now = time.time()
        with self._lock:
            dead = [k for k, v in self._pool.items() if now - v.last_used > max_idle]
            victims = [(k, self._pool.pop(k)) for k in dead]
        for k, p in victims:
            logger.info("MCP 服务器 %r 空闲超过 %ds，主动断开", k, int(max_idle))
            try:
                p.client.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("关闭空闲 MCP 服务器 %r 失败（忽略）：%s", k, e)
        return len(victims)

    # ── 工具 ──

    def list_tools(self, name: str, *, refresh: bool = False,
                   ttl: float = 300.0) -> Dict[str, Any]:
        """列出某个服务器的工具。带缓存（TTL 默认 5 分钟）。

        ★ 拉取（一次管道/HTTP 往返）在**锁外** —— 见类 docstring 的"锁的纪律"。
        """
        p, why = self._ensure(name)          # 内部自己管锁，不在锁内启动
        if p is None:
            cfg = self.find(name) or {}
            return {"ok": False, "server": name, "tools": [], "error": why,
                    "enabled": bool(cfg.get("enabled", False)),
                    "configured": cfg != {}}
        now = time.time()
        with self._lock:
            need_fetch = bool(refresh or not p.tools or (now - p.tools_at) > ttl)
        if need_fetch:
            try:
                tools = p.client.list_tools()        # ← 阻塞，锁外
                with self._lock:
                    p.tools = list(tools or [])
                    p.tools_at = now
                    p.error = ""
            except Exception as e:  # noqa: BLE001
                with self._lock:
                    p.error = "%s: %s" % (type(e).__name__, e)
                logger.warning("列出 MCP 服务器 %r 的工具失败：%s", name, e)
        with self._lock:
            out = [dict(t) for t in p.tools]
            err = p.error
            cached_at = p.tools_at
        for t in out:
            t["qualified_name"] = TOOL_PREFIX + str(name) + "__" + str(t.get("name"))
        return {"ok": not err, "server": name, "tools": out,
                "error": err, "cached_at": cached_at, "refreshed": refresh}

    def all_tools(self, *, refresh: bool = False) -> Dict[str, Any]:
        """所有**已启用**服务器的工具汇总（给提示词注入用）。

        连接失败的服务器会被列进 `unavailable` 并带原因 ——
        不是静默跳过，否则用户会以为"服务器配好了但工具没出现"是 bug。
        """
        out: List[Dict[str, Any]] = []
        unavailable: List[Dict[str, str]] = []
        for cfg in self.configs():
            if not cfg.get("enabled", True):
                continue
            r = self.list_tools(str(cfg.get("name")), refresh=refresh)
            if r.get("ok"):
                for t in r["tools"]:
                    out.append({"server": str(cfg.get("name")), **t})
            else:
                unavailable.append({"server": str(cfg.get("name")),
                                    "error": str(r.get("error") or "未知原因")})
        return {"ok": not unavailable, "tools": out, "unavailable": unavailable,
                "servers": len(self.configs())}

    def call(self, name: str, tool: str, arguments: Optional[Dict[str, Any]] = None,
             *, timeout: Optional[float] = None) -> Dict[str, Any]:
        """调用某个服务器上的工具。

        ★ 工具自身报错（`isError`）**不是**异常：返回 ok=False + 错误文本，
          让模型看到并自己决定下一步。只有连不上/协议错才是 error。

        ★★ 整个工具执行在**锁外**。这一点很关键：MCP 工具里有一类是慢的
          （浏览器自动化、跑构建、查大库），可能跑几分钟。原来它在锁内执行，
          于是那个期间**所有** MCP 操作（包括界面刷新服务器列表）全部排队。
        """
        t0 = time.time()
        p, why = self._ensure(name)
        if p is None:
            return {"ok": False, "server": name, "tool": tool, "text": why,
                    "is_error": True, "connection_error": True,
                    "elapsed_ms": int((time.time() - t0) * 1000)}
        try:
            r = p.client.call_tool(str(tool), dict(arguments or {}),
                                   **({"timeout": timeout} if timeout else {}))
        except Exception as e:  # noqa: BLE001
            # 通道坏了就把连接丢掉，下次自动重连（免得一直用一个死连接）。
            # ★ 锁内只摘人，关闭放锁外 —— close() 会 terminate 子进程。
            with self._lock:
                victim = self._pool.pop(str(name), None)
            _close_victim(str(name), victim)
            return {"ok": False, "server": name, "tool": tool,
                    "text": "%s: %s" % (type(e).__name__, e),
                    "is_error": True, "connection_error": True,
                    "elapsed_ms": int((time.time() - t0) * 1000)}
        with self._lock:
            p.last_used = time.time()
        r = dict(r)
        r.update({"server": name, "tool": tool})
        return r

    # ── 状态 / 提示词 ──

    def status(self, *, probe: bool = False) -> List[Dict[str, Any]]:
        """每个服务器的状态。`probe=True` 会真的去连一次（慢，只给显式请求用）。

        ★ 读连接池时**在锁内只做一次快照**，然后锁外再拼装。
          `probe=True` 会走 `list_tools`（连接 + 拉清单），那部分本来就在锁外。
        """
        out = []
        for cfg in self.configs():
            name = str(cfg.get("name"))
            with self._lock:
                p = self._pool.get(name)
                snap = None
                if p is not None:
                    snap = (len(p.tools), p.tools_at, p.error)
            rec: Dict[str, Any] = {
                "name": name, "transport": cfg.get("transport"),
                "enabled": bool(cfg.get("enabled", True)),
                "connected": snap is not None,
            }
            if cfg.get("transport") == "stdio":
                rec["command"] = "%s %s" % (cfg.get("command") or "",
                                            " ".join(cfg.get("args") or []))
            else:
                rec["url"] = cfg.get("url") or ""
            if snap is not None:
                n_tools, tools_at, perr = snap
                rec["tools"] = n_tools
                rec["tools_cached_at"] = tools_at
                if perr:
                    rec["error"] = perr
            else:
                # 还没连过：把"上次连失败的原因"也带上 ——
                # 否则界面上只显示"未连接"，用户不知道是命令写错了还是别的
                with self._lock:
                    last_err = self._connect_error.get(name, "")
                if last_err:
                    rec["error"] = last_err
            if probe and rec["enabled"]:
                r = self.list_tools(name, refresh=True)
                rec["connected"] = bool(r.get("ok"))
                rec["tools"] = len(r.get("tools") or [])
                if not r.get("ok"):
                    rec["error"] = r.get("error") or ""
            out.append(rec)
        return out

    def prompt_summary(self, *, max_tools_per_server: int = 12,
                       max_desc: int = 80) -> str:
        """拼给 agent 系统提示词看的一小段。

        ★ 只放**工具名 + 说明的第一句**，不放完整 JSON schema ——
          完整 schema 可能有几千字符，塞进系统提示词会把预算吃光。
          需要细节时模型应该去调 `mcp_tools(server)`。

        ★★ 这个方法在**每一轮对话**都会跑（`build_agent_prompt` 里），
          所以它**绝不能触发连接** —— 只读已有的连接池快照。
          否则每轮对话都可能卡在一个启动慢的 MCP 服务器上。
        """
        cfgs = self.configs()
        if not cfgs:
            return ""
        # 锁内一次性快照：{名字: (工具数, 工具列表)}，之后不再碰共享状态
        with self._lock:
            pool_snap = {n: (len(p.tools or []), list(p.tools or []))
                         for n, p in self._pool.items()}
        lines: List[str] = []
        connected = 0
        for cfg in cfgs:
            name = str(cfg.get("name"))
            if not cfg.get("enabled", True):
                lines.append("- `%s`（%s）：已禁用" % (name, cfg.get("transport")))
                continue
            snap = pool_snap.get(name)
            if snap is None:
                lines.append("- `%s`（%s）：未连接（调 mcp_tools 会自动连）"
                             % (name, cfg.get("transport")))
                continue
            connected += 1
            n_tools, tools = snap
            names = []
            for t in tools[:max_tools_per_server]:
                d = " ".join(str(t.get("description") or "").split())
                d = d.split("。")[0].split(". ")[0][:max_desc]
                names.append("`%s`%s" % (t.get("name"), ("（%s）" % d) if d else ""))
            more = ("" if n_tools <= max_tools_per_server else "…等 %d 个" % n_tools)
            lines.append("- `%s`：%s%s" % (name, "、".join(names), more))
        if not lines:
            return ""
        head = ("MCP 服务器（%d 个已连接）：" % connected if connected
                else "MCP 服务器：")
        return (head + "\n" + "\n".join(lines) +
                "\n  调用方式：先 `mcp_tools(server)` 看某个服务器的工具，"
                "再用 `mcp_call(server, tool, arguments)` 调用。"
                "**不要凭记忆猜工具名或参数名。**")


# ──────────────────────── 单例 ────────────────────────

_MANAGER: Optional[MCPManager] = None
_MANAGER_KEY: Optional[str] = None


def get_manager(data_dir: Optional[str] = None, *, force: bool = False) -> MCPManager:
    global _MANAGER, _MANAGER_KEY
    d = data_dir or os.environ.get("VIDEOFORGE_DATA_DIR") or os.getcwd()
    if force or _MANAGER is None or _MANAGER_KEY != str(d):
        if _MANAGER is not None:
            try:
                _MANAGER.close_all()
            except Exception:
                pass
        _MANAGER = MCPManager(d)
        _MANAGER_KEY = str(d)
    return _MANAGER


def manager_from_settings(settings: Optional[Dict[str, Any]] = None) -> MCPManager:
    """按设置里的 data_dir 取管理器（agent 工具统一用这个入口）。"""
    d = ""
    try:
        d = str((settings or {}).get("data_dir") or "")
    except Exception:
        d = ""
    if not d:
        d = os.environ.get("VIDEOFORGE_DATA_DIR") or os.getcwd()
    return get_manager(d)
