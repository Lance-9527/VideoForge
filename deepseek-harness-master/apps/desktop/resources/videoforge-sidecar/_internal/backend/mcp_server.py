# -*- coding: utf-8 -*-
"""VideoForge · MCP stdio server —— 把业务能力以**标准 MCP 工具**暴露出去。

★★ 为什么要有它（2026-10-25，客户刚需）
    客户的痛点是「自蒸馏 skill 换个平台就要降级、要适配」。根因往往不是环境，
    而是 **能力入口是私有 Python 函数名**（`agent_tools_*.py` 里的 `t_xxx`）——
    别人写的 skill 不认识这些名字，就得改代码。

    MCP 是跨 agent 的标准：只要 VideoForge 把能力暴露成 MCP 工具，任何会调 MCP 的
    桌面 agent / 外部 skill 都能直接接进来，**不需要懂我们的 Python 内部结构**。
    这也正是「环境依赖与桌面 agent 保持一致」的第一步：先把边界标准化。

    启动方式（两种都行）：
        VideoForgeService.exe --mcp        # 安装版（冻结包）
        python backend/mcp_server.py       # 开发版

    接进桌面 agent：
        mcp_add_server(name="videoforge", transport="stdio",
                       command="<安装目录>\\resources\\videoforge-sidecar\\VideoForgeService.exe",
                       args=["--mcp"])

★ 纪律
    * **stdout 只允许输出协议报文**（每行一条 JSON）；所有日志走 stderr。
    * 不自动花钱：本 server 不调用任何付费生成接口，只做查询/校验/环境体检。
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "videoforge"
SERVER_VERSION = "0.2.1"

#: 协议专用的文件描述符。**必须是 fd 1 的副本**，见 `_open_protocol()`。
_PROTO_FD: Optional[int] = None

log = logging.getLogger("videoforge.mcp")


# ──────────────────────────── 基础 IO ────────────────────────────

def _open_protocol() -> None:
    """把 stdout 让给库、把协议钉在自己的 fd 上。

    ★★ 为什么必须这么干（2026-10-25 本机实测）：
      第三方向库**会往 stdout 打提示**。实测 `import cv2` 在缺依赖时往 stdout 写：

          OpenCV bindings requires "numpy" package.
          Install it via command:
              pip install numpy

      而 MCP 的 stdout 是 **JSON-RPC 逐行帧** —— 混进一行普通文本，客户端整条流就废了。
      客户自蒸馏的 skill 里 print / 库的 banner 是常态，所以这里从根上隔离：
        1) 先把 fd 1 复制一份给协议用（`_PROTO_FD`）；
        2) 再把 fd 1 指到 stderr、`sys.stdout` 也换成 stderr；
        3) 之后任何库 print 都进 stderr，协议写入只走 `_PROTO_FD`。
    """
    global _PROTO_FD
    try:
        _PROTO_FD = os.dup(1)
    except Exception:  # pragma: no cover
        _PROTO_FD = None
    try:
        os.dup2(2, 1)
    except Exception:  # pragma: no cover
        pass
    try:
        sys.stdout = sys.stderr
    except Exception:  # pragma: no cover
        pass


def _stdout():
    """回退路径：拿一个可写、逐行 flush 的 stdout（仅当 `_PROTO_FD` 不可用时）。"""
    stream = sys.stdout
    if stream is not None and hasattr(stream, "write"):
        return stream
    try:
        return open(os.dup(1), "w", encoding="utf-8", buffering=1, newline="\n")
    except Exception:  # pragma: no cover
        return open(1, "w", encoding="utf-8", buffering=1, closefd=False, newline="\n")


def _emit(payload: Dict[str, Any]) -> None:
    data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    if _PROTO_FD is not None:
        try:
            os.write(_PROTO_FD, data)
            return
        except OSError:  # pragma: no cover
            pass
    out = _stdout()
    out.write(data.decode("utf-8"))
    out.flush()


def _result(msg_id: Any, result: Dict[str, Any]) -> None:
    _emit({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _error(msg_id: Any, code: int, message: str, data: Any = None) -> None:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    _emit({"jsonrpc": "2.0", "id": msg_id, "error": err})


# ──────────────────────────── 数据层 ────────────────────────────

def _data_root() -> Path:
    from core.data_root import resolve_data_root
    return Path(resolve_data_root())


def _db():
    from core.db import Database
    return Database(str(_data_root() / "videoforge.db"))


def _jsonable(value: Any) -> Any:
    """把 sqlite/ORM 返回值压成可 JSON 化的结构（去掉超大字段）。"""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# ──────────────────────────── 工具实现 ────────────────────────────

def tool_env_health(_args: Dict[str, Any]) -> Dict[str, Any]:
    """环境体检：解释器 / 可写扩展目录 / 缺哪些包。"""
    from core.env_ext import activate, diagnose
    root = _data_root()
    activate(root)
    return diagnose(root)


def tool_env_probe_import(args: Dict[str, Any]) -> Dict[str, Any]:
    """真 import 一个模块，确认「装上」之后「能用」（ABI 不匹配只能这样暴露）。"""
    from core.env_ext import activate, probe_import
    activate(_data_root())
    module = str(args.get("module") or "").strip()
    if not module:
        raise ValueError("必须给 module（import 名，例如 cv2）")
    return probe_import(module)


def tool_list_projects(_args: Dict[str, Any]) -> Dict[str, Any]:
    db = _db()
    limit = int(_args.get("limit") or 50)
    projects = db.list_projects(limit) or []
    return {"count": len(projects), "projects": [
        {"id": p.get("id"), "title": p.get("title"),
         "created_at": p.get("created_at"), "updated_at": p.get("updated_at")}
        for p in projects
    ]}


def tool_list_shots(args: Dict[str, Any]) -> Dict[str, Any]:
    pid = str(args.get("project_id") or "").strip()
    if not pid:
        raise ValueError("必须给 project_id（先用 vf_list_projects 查）")
    db = _db()
    if not db.get_project(pid):
        raise ValueError(f"没有这个项目：{pid}")
    shots = db.list_shots(pid) or []
    out = []
    for s in shots:
        candidates = s.get("candidates") or []
        if isinstance(candidates, str):
            try:
                candidates = json.loads(candidates)
            except Exception:
                candidates = []
        out.append({
            "shot_id": s.get("id"),
            "index": s.get("shot_index") or s.get("index"),
            "duration_seconds": s.get("duration_seconds"),
            "aspect_ratio": s.get("aspect_ratio"),
            "selected_candidate_id": s.get("selected_candidate_id"),
            "candidate_count": len(candidates),
        })
    return {"project_id": pid, "count": len(out), "shots": out}


def tool_reference_audit(args: Dict[str, Any]) -> Dict[str, Any]:
    """逐帧严格验收：给定白膜 vs 候选成片。**不生成、不选片、不花钱**。"""
    reference = str(args.get("reference") or "").strip()
    candidate = str(args.get("candidate") or "").strip()
    if not reference or not candidate:
        raise ValueError("必须给 reference（给定白膜视频）和 candidate（待验成片）")
    for label, value in (("reference", reference), ("candidate", candidate)):
        if not Path(value).is_file():
            raise FileNotFoundError(f"{label} 文件不存在：{value}")

    from core.strict_reference import evaluate_reference
    contract: Dict[str, Any] = {}
    if args.get("duration") is not None:
        contract["duration"] = float(args["duration"])
    if args.get("fps") is not None:
        contract["fps"] = args["fps"]
    if args.get("aspect_ratio"):
        contract["aspect_ratio"] = str(args["aspect_ratio"])
    if args.get("silent") is True:
        contract["silent"] = True

    report = evaluate_reference(reference, candidate, contract=contract or None)
    return {
        "ok": bool(report.get("ok")),
        "status": report.get("status"),
        "failures": report.get("failures"),
        "metrics": report.get("metrics"),
        "provenance": {k: v for k, v in (report.get("provenance") or {}).items()
                       if k in ("verifier_sha256", "contract", "reference", "candidate")},
    }


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "vf_env_health",
        "description": ("VideoForge 运行环境体检：Python 版本/是否冻结包/可写扩展目录是否就绪/"
                        "客户 skill 常用包（cv2、pandas、numba…）哪些可用、缺了怎么补。"
                        "任何『skill 导入失败』先查这个。"),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "_fn": tool_env_health,
    },
    {
        "name": "vf_env_probe_import",
        "description": ("真 import 一个模块并回报版本/报错：装完 wheel 之后用它确认「能用」，"
                        "ABI 不匹配（cp3xx 对不上）只有真 import 才会暴露。"),
        "inputSchema": {"type": "object", "properties": {
            "module": {"type": "string", "description": "import 名，例如 cv2 / pandas / numba"}},
            "required": ["module"], "additionalProperties": False},
        "_fn": tool_env_probe_import,
    },
    {
        "name": "vf_list_projects",
        "description": "列出 VideoForge 项目（id/标题/时间）。只读。",
        "inputSchema": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "最多返回多少个（默认 50）"}},
            "additionalProperties": False},
        "_fn": tool_list_projects,
    },
    {
        "name": "vf_list_shots",
        "description": "列出某项目的分镜、时长、画幅、已登记候选数、当前选中的候选。只读。",
        "inputSchema": {"type": "object", "properties": {
            "project_id": {"type": "string", "description": "项目 id（先用 vf_list_projects 查）"}},
            "required": ["project_id"], "additionalProperties": False},
        "_fn": tool_list_shots,
    },
    {
        "name": "vf_reference_audit",
        "description": ("逐帧严格验收：把候选成片与给定白膜按**同帧号、全片**比对，"
                        "报 ok/status/failures（帧数、时长、画幅、可见结构）。不生成、不选片、不花钱。"),
        "inputSchema": {"type": "object", "properties": {
            "reference": {"type": "string", "description": "给定白膜视频的绝对路径"},
            "candidate": {"type": "string", "description": "待验成片的绝对路径"},
            "duration": {"type": "number", "description": "期望时长（秒），可选"},
            "fps": {"type": ["string", "number"], "description": "期望帧率，可选"},
            "aspect_ratio": {"type": "string", "description": "期望画幅，如 16:9，可选"},
            "silent": {"type": "boolean", "description": "要求无音轨，可选"}},
            "required": ["reference", "candidate"], "additionalProperties": False},
        "_fn": tool_reference_audit,
    },
]

_BY_NAME = {t["name"]: t for t in TOOLS}


def _public_tools() -> List[Dict[str, Any]]:
    return [{"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
            for t in TOOLS]


# ──────────────────────────── 协议循环 ────────────────────────────

def handle(message: Dict[str, Any]) -> bool:
    """处理一条 JSON-RPC 消息；返回 False 表示应当退出。"""
    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params") or {}

    # 通知类（没有 id）不需要回包
    if msg_id is None and method is not None and method.startswith("notifications/"):
        return True

    if method == "initialize":
        client_version = params.get("protocolVersion") or PROTOCOL_VERSION
        _result(msg_id, {
            "protocolVersion": client_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
        return True

    if method == "ping":
        _result(msg_id, {})
        return True

    if method == "shutdown":
        _result(msg_id, None)
        return False

    if method == "tools/list":
        _result(msg_id, {"tools": _public_tools()})
        return True

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = _BY_NAME.get(name)
        if tool is None:
            _error(msg_id, -32602, f"未知工具：{name}",
                   {"available": [t["name"] for t in TOOLS]})
            return True
        try:
            payload = tool["_fn"](args if isinstance(args, dict) else {})
            _result(msg_id, {
                "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
                "isError": False,
            })
        except Exception as exc:  # noqa: BLE001
            log.exception("tool %s failed", name)
            _result(msg_id, {
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True,
            })
        return True

    _error(msg_id, -32601, f"不支持的方法：{method}")
    return True


def serve(stdin=None) -> int:
    """从 stdin 逐行读 JSON-RPC，写 stdout。返回进程退出码。

    ★ 必须用 `readline()` 而不是 `for line in stream`：后者是**块缓冲**的迭代器，
      在「父进程保持管道打开、一次只发一条」的正常 MCP 场景里会一直等缓冲区满，
      表现就是客户端发出 initialize 后**永远收不到回包**（本机实测踩过）。
    """
    stream = stdin or sys.stdin
    while True:
        raw = stream.readline()
        if raw == "":
            break
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            _error(None, -32700, f"解析失败：{exc}")
            continue
        if not isinstance(message, dict):
            _error(None, -32600, "请求必须是 JSON 对象")
            continue
        if not handle(message):
            return 0
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    # ★★ 必须把 stdio 明确钉成 UTF-8：Windows 子进程默认用**系统 locale**（中文机上
    #    是 GBK）编码 stdout，于是含中文的 JSON 报文发出去就不是 UTF-8，客户端
    #    （桌面 agent / MCP SDK）按 UTF-8 解码直接报错。本机实测踩到过。
    for _name, _newline in (("stdin", None), ("stdout", "\n")):
        try:
            getattr(sys, _name).reconfigure(encoding="utf-8", newline=_newline)
        except Exception:  # noqa: BLE001
            pass
    # ★ 把 stdout 让给库、协议改走独立 fd（见 _open_protocol 的说明）
    _open_protocol()
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    # 冻结包入口会从 backend/ 起跑；开发时也要保证 `from core import ...` 能找到
    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    # 顺手把可写扩展环境接上（体检工具要用到，也保证 skill 包可见）
    try:
        from core.env_ext import activate
        activate(_data_root())
    except Exception as exc:  # noqa: BLE001
        log.warning("skillenv 注入失败（不影响只读工具）：%s", exc)
    log.warning("VideoForge MCP server 已启动（stdio）")
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
