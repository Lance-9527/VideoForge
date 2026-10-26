# -*- coding: utf-8 -*-
"""VideoForge · MCP 相关的 Agent 工具

暴露 6 个元工具（而不是给每个 MCP 工具注册一条 —— 那会把工具目录撑爆，
见 `core/mcp_manager.py` 的说明）：

    mcp_servers()                       有哪些服务器、连不连得上
    mcp_tools(server)                   某服务器有哪些工具（含参数 schema）
    mcp_call(server, tool, arguments)   真正调用
    mcp_add_server(...)                 ★ 写操作，需确认
    mcp_remove_server(name)             ★ 写操作，需确认
    mcp_set_enabled(name, enabled)      ★ 写操作，需确认

★ 安全：stdio 传输就是「启动本机子进程并通过管道通信」，
  所以 `mcp_add_server` **等同于允许在本机执行任意命令**。
  这里不做"按命令名黑名单"那种假过滤 —— 拦不住真正的恶意，
  只会让正常用法莫名失败；真正的边界是"只有用户能加，且会被明确告知"。

★★ 2026-09-26 更正一句**原来写错的话**：

  本文件原来写的是「三个写操作都标了 `writes=True`，前端据此弹确认」。
  **这句是假的。** `ToolSpec.writes` 目前没有任何代码读它：

    · `agent_tools.py` 里三条给模型/前端的序列化路径
      （`signature_text` / `doc_text` / `to_json_schema`）都不含 `writes`；
    · `agent.py` 的 `AgentStep`（前端展示/SSE 用的那一步记录）也不带它；
    · 代码强制的闸门是 `agent._gate_check()`，它只看 `stages.GATED_TOOLS`；
      而 `GATED_TOOLS` = 9 个流水线工具 + `EXTRA_GATED_TOOLS`
      （kling_generate / make_character_look / refine_look …）——
      **MCP 的三个写操作不在里面**（`backend/core/stages.py:178-187`）。
    · 更麻烦的是：`_gate_check` 对**没有 `project_id`** 的工具**直接放行**
      （`core/agent.py:201-204`），而现有闸门协议是"项目 + 阶段"形状的
      （`propose_step(stage=...)`）—— **覆盖不到** MCP 这类全局工具。

  ⇒ 所以**目前唯一的防线是工具描述里那句「添加前请先跟用户确认」**，
    也就是**提示词级**的。而本仓自己已经量过这件事：`core/agent.py:182-185`
    ——「为什么必须在**代码里**拦，而不是在 system prompt 里写'请先确认'：
    实测过，模型会无视。提示词是建议，`if` 才是规则。」

  这是一处**已知的安全缺口**，不是"已经防住了"。现状由
  `tests/test_safety_posture.py` 钉住（哪些 `writes` 工具进了闸门、哪些没有、
  以及"没有"的那些凭什么）。

  修法方向（本轮**未做**：改行为必须能跑测试，而机器起不了控制台进程）：
  给 MCP 写操作加一个**与项目无关的确认协议** —— 例如要求显式
  `confirm=true`，或者把闸门的 pending 状态从"每项目一份"扩展成"全局一份"。
  ★ **不要**只在 `EXTRA_GATED_TOOLS` 里加个名字就算完：那会因为取不到
    `project_id` 而被放行，变成**看起来上了闸门、其实没有** ——
    比现在明说"没闸门"更危险。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from .agent_tools import REGISTRY, ToolResult, err, ok

logger = logging.getLogger("videoforge.agent.tools.mcp")


def _mgr():
    """取 MCP 管理器（按设置里的 data_dir 定位配置文件）。"""
    from .mcp_manager import manager_from_settings
    from .agent_tools import _services
    _db, settings, _q = _services()
    return manager_from_settings(settings)


def _brief_tools(tools: List[Dict[str, Any]], limit: int = 40) -> List[Dict[str, Any]]:
    """把工具清单压成给模型看的形状（保留参数名与必填，去掉冗长描述）。

    ★ 保留 MCP 协议自带的 `annotations.readOnlyHint` / `destructiveHint`。
      这是判断"这个工具会不会改东西"的**唯一可靠依据** ——
      静态地给 `mcp_call` 打一个 writes=True 是错的：同一次调用
      可能只是读文件，也可能删文件，取决于具体工具。
    """
    out = []
    for t in tools[:limit]:
        schema = t.get("inputSchema") or t.get("input_schema") or {}
        props = (schema.get("properties") or {}) if isinstance(schema, dict) else {}
        required = set(schema.get("required") or []) if isinstance(schema, dict) else set()
        params = []
        for pname, meta in list(props.items())[:20]:
            meta = meta if isinstance(meta, dict) else {}
            params.append({
                "name": pname,
                "type": meta.get("type") or "any",
                "required": pname in required,
                "desc": str(meta.get("description") or "")[:120],
                **({"enum": meta["enum"][:20]} if isinstance(meta.get("enum"), list) else {}),
            })
        desc = " ".join(str(t.get("description") or "").split())
        ann = t.get("annotations") if isinstance(t.get("annotations"), dict) else {}
        rec: Dict[str, Any] = {
            "name": t.get("name"),
            "qualified_name": t.get("qualified_name"),
            "description": desc[:400],
            "params": params,
        }
        if ann:
            rec["annotations"] = {
                k: ann.get(k) for k in
                ("title", "readOnlyHint", "destructiveHint",
                 "idempotentHint", "openWorldHint")
                if ann.get(k) is not None
            }
            if ann.get("destructiveHint"):
                rec["risk"] = "★ 服务器标注为**有破坏性**：调用前先跟用户确认"
        out.append(rec)
    return out


@REGISTRY.register(
    "mcp_servers",
    "列出已配置的 MCP 服务器及其连接状态。`probe=true` 会真的去连一次（慢，"
    "只在需要确认某个服务器能不能用时才传）。要看某个服务器有哪些工具，"
    "接着调 `mcp_tools`。",
    {"probe": {"type": "boolean", "desc": "是否真的连一次来确认可用（默认 false，用缓存状态）"}},
)
async def t_mcp_servers(args: Dict[str, Any]) -> ToolResult:
    try:
        mgr = _mgr()
        servers = mgr.status(probe=bool(args.get("probe")))
        cfg_path = str(mgr.config_path)
        if not servers:
            return ok(
                servers=[], count=0, config_path=cfg_path,
                hint="还没有配置任何 MCP 服务器。用 `mcp_add_server` 添加："
                     "stdio 类型给 `command`（如 npx / uvx / python），"
                     "http 类型给 `url`（Streamable HTTP 端点）。"
                     "★ 添加 stdio 服务器会在本机启动一个子进程，"
                     "请确认用户知道要运行什么。")
        return ok(servers=servers, count=len(servers), config_path=cfg_path)
    except Exception as e:
        logger.exception("mcp_servers failed")
        return err("列出 MCP 服务器失败：%s: %s" % (type(e).__name__, e))


@REGISTRY.register(
    "mcp_tools",
    "列出某个 MCP 服务器提供的工具（名字、说明、参数名与是否必填）。"
    "**调用前必须先看** —— 不要凭记忆猜工具名或参数名。"
    "`refresh=true` 强制重新拉取（默认有 5 分钟缓存）。",
    {"server": {"type": "string", "desc": "服务器名（用 mcp_servers 查）", "required": True},
     "refresh": {"type": "boolean", "desc": "强制刷新缓存"}},
)
async def t_mcp_tools(args: Dict[str, Any]) -> ToolResult:
    name = str(args.get("server") or "").strip()
    if not name:
        return err("必须给 `server`。用 `mcp_servers` 查有哪些服务器。")
    try:
        mgr = _mgr()
        r = mgr.list_tools(name, refresh=bool(args.get("refresh")))
        if not r.get("ok") and not r.get("tools"):
            # ★ 报错要说清是"没配这个服务器"还是"配了但连不上"
            if not r.get("configured"):
                return err("没有名为 %r 的 MCP 服务器。现有：%s"
                           % (name, ", ".join(s["name"] for s in mgr.status()) or "（无）"))
            return err("MCP 服务器 %r 连不上：%s" % (name, r.get("error") or "未知原因"))
        tools = _brief_tools(r.get("tools") or [])
        out: Dict[str, Any] = {"server": name, "count": len(tools), "tools": tools}
        if r.get("error"):
            out["warning"] = "上次刷新时报错（可能是缓存的旧清单）：%s" % r["error"]
        out["hint"] = ("用 `mcp_call(server=\"%s\", tool=\"工具名\", "
                       "arguments={...})` 调用；arguments 是对象，不是 JSON 字符串。"
                       "带 `destructiveHint` 的工具调用前**先跟用户确认**。" % name)
        return ok(**out)
    except Exception as e:
        logger.exception("mcp_tools failed")
        return err("列出 MCP 工具失败：%s: %s" % (type(e).__name__, e))


@REGISTRY.register(
    "mcp_call",
    "调用某个 MCP 服务器上的工具。`arguments` 是**对象**（不是 JSON 字符串）。"
    "工具自己报错时会返回 ok=false + 错误文本，那不是崩溃 —— 看清原因再决定"
    "要不要换个参数重试，不要盲目重试同样的调用。"
    "★ 如果 `mcp_tools` 里该工具带 `destructiveHint`（或返回里带 risk 提示），"
    "说明它会改东西/删东西 —— **先跟用户确认再调**。",
    {"server": {"type": "string", "desc": "服务器名", "required": True},
     "tool": {"type": "string", "desc": "工具名（先用 mcp_tools 确认）", "required": True},
     "arguments": {"type": "object", "desc": "调用参数，对象形式", "required": False},
     "timeout": {"type": "number", "desc": "超时秒数（默认用服务器配置值）"}},
    # ★ 这里**故意不标** writes=True：一次 mcp_call 是读还是写，
    #   取决于被调用的那个工具，静态布尔量表达不了。
    #   可靠的依据是 MCP 协议自带的 annotations（见 `_brief_tools`），
    #   以及工具描述里"破坏性调用前先跟用户确认"那条指令。
    #   顺带记录一个现状：`ToolSpec.writes` 目前**没有被任何代码读取**
    #   （只在 agent_tools*.py 里被赋值），所以标它也不会有运行时效果。
)
async def t_mcp_call(args: Dict[str, Any]) -> ToolResult:
    server = str(args.get("server") or "").strip()
    tool = str(args.get("tool") or "").strip()
    if not server or not tool:
        return err("必须同时给 `server` 和 `tool`。先用 `mcp_tools(server)` 查工具名。")
    a = args.get("arguments")
    if a is None:
        a = {}
    if isinstance(a, str):
        # 模型很爱把对象写成 JSON 字符串。这里**容错解析**，解析不了就明确报错
        # （而不是把字符串当参数传过去，那会得到一个莫名其妙的服务器端错误）。
        try:
            a = json.loads(a) if a.strip() else {}
        except Exception as e:
            return err("`arguments` 传的是字符串但解析不了 JSON：%s。"
                       "请直接传对象，例如 {\"path\": \"D:/a.txt\"}，不要加引号包一层。" % e)
    if not isinstance(a, dict):
        return err("`arguments` 必须是对象，收到 %s" % type(a).__name__)
    to = args.get("timeout")
    try:
        r = _mgr().call(server, tool, a,
                        timeout=float(to) if to not in (None, "") else None)
    except Exception as e:
        logger.exception("mcp_call failed")
        return err("调用 MCP 工具失败：%s: %s" % (type(e).__name__, e))
    if r.get("ok"):
        return ok(**r)
    return err(str(r.get("text") or "MCP 工具返回失败但没给文本"),
               **{k: v for k, v in r.items() if k != "text"})


@REGISTRY.register(
    "mcp_add_server",
    "添加一个 MCP 服务器。★ 这会在本机启动子进程（stdio）或连接一个远端端点（http），"
    "**等同于允许执行该命令** —— 添加前请先跟用户确认要加什么。"
    "stdio 例子：name=filesystem, transport=stdio, command=npx, "
    "args=[\"-y\",\"@modelcontextprotocol/server-filesystem\",\"D:/docs\"]；"
    "http 例子：name=remote, transport=http, url=http://127.0.0.1:8000/mcp。"
    "加完会自动连一次并列出工具，连不上会如实报错但配置仍然保存（便于改）。",
    {"name": {"type": "string", "desc": "服务器名（唯一）", "required": True},
     "transport": {"type": "string", "desc": "stdio 或 http", "required": True,
                   "enum": ["stdio", "http"]},
     "command": {"type": "string", "desc": "stdio：要启动的可执行文件"},
     "args": {"type": "array", "desc": "stdio：命令行参数数组"},
     "env": {"type": "object", "desc": "stdio：额外环境变量"},
     "cwd": {"type": "string", "desc": "stdio：工作目录"},
     "url": {"type": "string", "desc": "http：Streamable HTTP 端点"},
     "headers": {"type": "object", "desc": "http：额外请求头（如鉴权）"},
     "timeout": {"type": "number", "desc": "超时秒数，默认 30"},
     "overwrite": {"type": "boolean", "desc": "同名时是否替换"}},
    writes=True,
)
async def t_mcp_add_server(args: Dict[str, Any]) -> ToolResult:
    try:
        mgr = _mgr()
        cfg = mgr.add_server(args, overwrite=bool(args.get("overwrite")))
    except Exception as e:
        # 配置错误是**可修正的用户错误**，要把原因原样说清楚
        from .mcp_manager import MCPConfigError
        if isinstance(e, MCPConfigError):
            return err(str(e))
        logger.exception("mcp_add_server failed")
        return err("添加 MCP 服务器失败：%s: %s" % (type(e).__name__, e))

    name = str(cfg.get("name"))
    probe: Dict[str, Any] = {}
    try:
        r = mgr.list_tools(name, refresh=True)
        probe = {"connected": bool(r.get("ok")),
                 "tool_count": len(r.get("tools") or []),
                 "tools": [t.get("name") for t in (r.get("tools") or [])][:20],
                 "error": r.get("error") or ""}
    except Exception as e:
        probe = {"connected": False, "error": "%s: %s" % (type(e).__name__, e)}
    if not probe.get("connected"):
        return ok(added=True, server=name, config=cfg, probe=probe,
                  note="配置已保存，但**当前连不上**：%s。"
                       "配置不会因此丢，修好之后直接 mcp_tools 重试即可。"
                       % (probe.get("error") or "未知原因"))
    return ok(added=True, server=name, config=cfg, probe=probe)


@REGISTRY.register(
    "mcp_remove_server",
    "删除一个 MCP 服务器配置（会同时断开它的连接）。",
    {"name": {"type": "string", "desc": "服务器名", "required": True}},
    writes=True,
)
async def t_mcp_remove_server(args: Dict[str, Any]) -> ToolResult:
    name = str(args.get("name") or "").strip()
    if not name:
        return err("必须给 `name`")
    try:
        from .mcp_manager import MCPConfigError
        try:
            r = _mgr().remove_server(name)
        except MCPConfigError as e:
            return err(str(e))
        return ok(**r)
    except Exception as e:
        logger.exception("mcp_remove_server failed")
        return err("删除 MCP 服务器失败：%s: %s" % (type(e).__name__, e))


@REGISTRY.register(
    "mcp_set_enabled",
    "启用或禁用一个 MCP 服务器。禁用会断开连接、且它的工具不再注入提示词。",
    {"name": {"type": "string", "desc": "服务器名", "required": True},
     "enabled": {"type": "boolean", "desc": "true 启用 / false 禁用", "required": True}},
    writes=True,
)
async def t_mcp_set_enabled(args: Dict[str, Any]) -> ToolResult:
    name = str(args.get("name") or "").strip()
    if not name:
        return err("必须给 `name`")
    if "enabled" not in args:
        return err("必须给 `enabled`（true / false）")
    try:
        from .mcp_manager import MCPConfigError
        try:
            r = _mgr().set_enabled(name, bool(args.get("enabled")))
        except MCPConfigError as e:
            return err(str(e))
        return ok(**r)
    except Exception as e:
        logger.exception("mcp_set_enabled failed")
        return err("切换 MCP 服务器状态失败：%s: %s" % (type(e).__name__, e))
