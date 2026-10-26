# -*- coding: utf-8 -*-
"""
白膜预演 · openVideoForge MCP 通道
==================================

把「白膜视频」从 Blender 内置渲染换成 **openVideoForge 工作台** 出片。
2026-10-25 起，白膜这一段的默认通道是 openVideoForge（走 wb_* 工具，等价 MCP）。

为什么是这个形状
----------------
openVideoForge 的协作契约（`docs/collaboration-contract.md`）写明了分工：

    Agent 造 → 工作台排 → VideoForge 渲 → HUD 让你看见

所以本模块**不自己渲染**：它按镜头把「机位 / 时长 / 画幅」交给工作台，
由工作台把规范场景交给 VideoForge 服务（8765）的 Blender 渲，
产物（mp4 + 首帧 png + manifest/sha256）落盘后，再绑到分镜上。

协议
----
桥同时提供两种等价入口：

    · MCP（stdio）：`node server/agent-bridge.mjs --mcp` → `tools/call`
    · HTTP      ：`POST http://127.0.0.1:8787/tool {name, args}`

两者**同一套 schema**（契约文档 §四：「等价 MCP tools/call」）。
这里走 HTTP：sidecar 是常驻进程，不需要为每次调用起子进程、也不会把
stdio 的 JSON-RPC 帧和日志搅在一起。要换成 stdio 只改 `call()` 一个函数。

失败策略
--------
白膜是**花钱之前**的检查环节，不能因为它挂了就卡住整条流水线：

    · 通道没就绪（桥没起 / 页面没连 / 不在编辑器 / 没场景）→ 渲染前先调
      `core.previz_channel.ensure_ready()` **自动把它拉起来**（起桥、开页面、
      新建场景、导出 scene_id）；拉不起来才返回 ok=False + 卡在哪一层 +
      next_action，由调用方回退 Blender（`agent_tools_production.t_render_previz`）；
    · 渲染单镜失败 → 只跳这一镜，其余照跑，失败原因逐镜带回；
    · 任何异常都不吞：`steps` 里逐步记 ok/error，用户看得到卡在哪。

自动就绪（2026-10-25 起）
------------------------
之前日志里常见「工作台页面停在 editor 视图，没有打开场景」——根因不是人没干活，
而是**没人去建立运行时前提**：桥要 node 起进程；页面要浏览器连 SSE；页面在
editor 视图时 `scene_id` 仍为空（页面的 studioSceneId 只在导出/载入场景时才写入，
而导出又要 VideoForge 服务 8765 在线）。`previz_channel.ensure_ready()` 按
「桥 → 页面 → 编辑器 → 场景」四层依次尝试，失败时给 `step/reason/next_action`。

配置（环境变量，都有默认值）
---------------------------
    VIDEOFORGE_PREVIZ_CHANNEL   mcp | blender | auto   （默认 mcp）
    VIDEOFORGE_MCP_BRIDGE       桥地址                 （默认 http://127.0.0.1:8787）
    VIDEOFORGE_MCP_VF           VideoForge 服务地址    （默认 http://127.0.0.1:8765）
    VIDEOFORGE_MCP_W/H/FPS      白膜画幅与帧率         （默认 640/360/24）
    VIDEOFORGE_OPENVIDEOFORGE_DIR    openvideoforge 包根（自动就绪用）
    VIDEOFORGE_OPENVIDEOFORGE_URL    工作台页面地址（默认 http://127.0.0.1:8802/index.html）
    VIDEOFORGE_PREVIZ_AUTOSTART      0 = 只诊断不自动拉进程/页面（默认 1）
    VIDEOFORGE_PREVIZ_OPEN_BROWSER   0 = 不自动开浏览器（默认 1）

自测（不渲染、不碰页面状态）：
    python -m core.previz_mcp --probe
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

_LOG = logging.getLogger("videoforge.previz_mcp")

DEFAULT_BRIDGE = "http://127.0.0.1:8787"
DEFAULT_VF = "http://127.0.0.1:8765"
DEFAULT_RENDER = {"width": 640, "height": 360, "fps": 24}
POLL_SECONDS = 0.9
_TERMINAL = ("succeeded", "failed", "cancelled")


# ───────────────────────────── 配置 ─────────────────────────────

def bridge_url() -> str:
    return (os.environ.get("VIDEOFORGE_MCP_BRIDGE") or DEFAULT_BRIDGE).rstrip("/")


def vf_url() -> str:
    return (os.environ.get("VIDEOFORGE_MCP_VF") or DEFAULT_VF).rstrip("/")


def channel() -> str:
    """当前白膜通道：mcp / blender / auto（默认 mcp）。"""
    raw = (os.environ.get("VIDEOFORGE_PREVIZ_CHANNEL") or "mcp").strip().lower()
    return raw if raw in ("mcp", "blender", "auto") else "mcp"


def render_spec() -> Dict[str, int]:
    def _num(key: str, default: int) -> int:
        try:
            v = int(os.environ.get(key) or default)
            return v if v > 0 else default
        except (TypeError, ValueError):
            return default

    return {"width": _num("VIDEOFORGE_MCP_W", DEFAULT_RENDER["width"]),
            "height": _num("VIDEOFORGE_MCP_H", DEFAULT_RENDER["height"]),
            "fps": _num("VIDEOFORGE_MCP_FPS", DEFAULT_RENDER["fps"])}


# ───────────────────────── HTTP（桥 / 服务）─────────────────────────

def _request(url: str, payload: Optional[Dict[str, Any]] = None,
             timeout: float = 30.0) -> Any:
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


def call(name: str, args: Optional[Dict[str, Any]] = None,
         timeout: float = 120.0) -> Dict[str, Any]:
    """等价 MCP `tools/call`：调一个 wb_* 工具。

    Returns:
        {"ok": bool, "data": Any} 或 {"ok": False, "error": str}
        （桥的原始错误文本**原样**带回，不加工 —— 失败时要给用户看原文）
    """
    try:
        out = _request(bridge_url() + "/tool",
                       {"name": name, "args": args or {}}, timeout)
    except urllib.error.URLError as e:  # 桥没起
        return {"ok": False, "error": "连不上白膜工作台桥 %s（%s）。"
                                     "先启动 OpenVideoForge 的桥再试。"
                                     % (bridge_url(), getattr(e, "reason", e))}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}

    if not isinstance(out, dict):
        return {"ok": True, "data": out}
    if "ok" in out:
        return out
    return {"ok": True, "data": out}


def health(timeout: float = 5.0) -> Dict[str, Any]:
    """桥健康 + 页面连接情况（只读）。"""
    try:
        out = _request(bridge_url() + "/health", None, timeout)
        return out if isinstance(out, dict) else {"ok": False, "error": "health 响应异常"}
    except urllib.error.URLError as e:
        return {"ok": False, "error": "连不上桥 %s（%s）"
                                     % (bridge_url(), getattr(e, "reason", e))}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def state(timeout: float = 15.0) -> Dict[str, Any]:
    """读工作台状态（wb_state，只读）。data 里带 view / scene_id / shots。"""
    return call("wb_state", {}, timeout)


def probe() -> Dict[str, Any]:
    """体检：桥在不在、页面连没连、页面在哪个视图、有没有打开场景。

    只读，不改页面状态、不渲染 —— 用来在真正出白膜前给用户一句实话。
    （要把它**拉起来**用 `core.previz_channel.ensure_ready()`，本函数只诊断。）
    """
    h = health()
    out: Dict[str, Any] = {
        "ok": False,
        "bridge": bridge_url(),
        "channel": channel(),
        "health": h,
    }
    if not h.get("ok"):
        out["reason"] = h.get("error") or "桥没有响应"
        out["next_action"] = ("先启动 openVideoForge 的桥；白膜(mcp) 渲染前会自动尝试拉起"
                              "（node server/agent-bridge.mjs --port %s）"
                              % _bridge_port())
        return out

    clients = h.get("clients") or []
    views = h.get("views") or {}
    out["clients"] = clients
    out["views"] = views
    out["page"] = bool(h.get("page") or clients)
    if not out["page"]:
        out["reason"] = "桥在（%s），但没有任何工作台页面连上" % bridge_url()
        out["next_action"] = ("打开工作台页面（默认 http://127.0.0.1:8802/index.html），"
                              "页面会自动连桥；白膜渲染前也会自动尝试打开")
        return out

    st = state()
    if st.get("ok") and isinstance(st.get("data"), dict):
        s = st["data"]
        out["view"] = s.get("view")
        out["scene_id"] = s.get("scene_id")
        out["shots"] = len(s.get("shots") or [])
        out["objects"] = s.get("objects")
        out["ok"] = bool(out["view"] == "editor" and s.get("scene_id"))
        if not out["ok"]:
            if out["view"] != "editor":
                out["reason"] = ("工作台页面停在「%s」视图，不在编辑器里；"
                                 "白膜出片需要页面在编辑器里。" % (out["view"] or "?"))
                out["next_action"] = "让页面进编辑器：wb_scene_new {env:'room'}（渲染前会自动尝试）"
            else:
                out["reason"] = ("工作台页面在编辑器视图，但场景还没导出（scene_id 为空）；"
                                 "白膜出片要先导出一次场景。")
                out["next_action"] = ("wb_scene_export（需 VideoForge 服务 8765 在线）"
                                      "（渲染前会自动尝试）")
    else:
        out["reason"] = st.get("error") or "读不到工作台状态"
        out["next_action"] = ("确认工作台页面已打开且没被浏览器拦；"
                              "白膜渲染前会自动尝试打开页面")
    return out


def _bridge_port() -> int:
    try:
        from urllib.parse import urlsplit
        return int(urlsplit(bridge_url()).port or 8787)
    except (TypeError, ValueError):
        return 8787


# ───────────────────────── 出片 ─────────────────────────

def _download(url: str, dst: str, timeout: float = 120.0) -> Dict[str, Any]:
    """把一个产物 URL 落到 dst（相对 URL 按 VideoForge 服务地址补全）。"""
    full = url if url.startswith("http") else vf_url() + "/" + url.lstrip("/")
    try:
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        with urllib.request.urlopen(full, timeout=timeout) as resp:  # noqa: S310 本机回环
            blob = resp.read()
        with open(dst, "wb") as fh:
            fh.write(blob)
        return {"ok": True, "path": dst, "bytes": len(blob), "url": full}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e), "url": full}


def _job_state(job_id: str) -> Dict[str, Any]:
    r = call("wb_render_status", {"job_id": job_id}, 30)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error")}
    d = r.get("data")
    if not isinstance(d, dict):
        return {"ok": True, "raw": d}
    # 页面/服务两层都可能包一层 data：剥到有 status 的那一层为止（最多两层）
    for _ in range(2):
        if "status" in d:
            break
        inner = d.get("data")
        if not isinstance(inner, dict):
            break
        d = inner
    return d


def _wait(job_id: str, deadline: float) -> Dict[str, Any]:
    """轮询渲染任务直到终态。返回最后一次状态（带终态判定）。"""
    last: Dict[str, Any] = {"status": "unknown"}
    while time.time() < deadline:
        st = _job_state(job_id)
        if st.get("error"):
            last = {"status": "unknown", "error": st["error"]}
            # 查不到状态不算失败：再等一轮，桥可能正忙
        else:
            last = st
            status = str(st.get("status") or "").lower()
            if status in _TERMINAL:
                return last
        time.sleep(POLL_SECONDS)
    last["error"] = last.get("error") or "等待渲染超时（%s）" % job_id
    return last


def _cameras_file() -> str:
    """每镜机位表（shot_id → {r,phi,theta,tx,ty,tz,fov}）。

    ★ 为什么不用 `shot.previz_camera`：那个字段是**受控枚举**
      （static / manual_track …），orchestrator 和 shot_prompts 都按枚举读它，
      往里面塞 JSON 会把下游提示词写坏。机位是"渲染参数"，另存一份。
    """
    base = os.environ.get("VIDEOFORGE_DATA_DIR") or os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
        "VideoForge", "data")
    return os.path.join(base, "previz_cameras.json")


def _camera_for(shot: Dict[str, Any]) -> Dict[str, Any]:
    """取这一镜的机位：优先机位表（按 shot_id），其次 shot 上的 dict 字段。"""
    sid = str(shot.get("id") or "")
    if sid:
        try:
            fp = _cameras_file()
            if os.path.exists(fp):
                with open(fp, "r", encoding="utf-8") as fh:
                    table = json.load(fh) or {}
                cam = table.get(sid) if isinstance(table, dict) else None
                if isinstance(cam, dict) and cam:
                    return cam
        except Exception as e:  # noqa: BLE001
            _LOG.warning("读机位表失败（回退轨道机位）：%s", e)
    v = shot.get("previz_camera")
    return v if isinstance(v, dict) else {}


def _set_static_camera(cam: Dict[str, Any], seconds: float,
                       render: Dict[str, int]) -> Dict[str, Any]:
    """「围绕目标点的球坐标机位」→ 显式静态关键帧（Z-up 规范场景）。

    cam 用的是**页面坐标系**（Three.js，Y-up、米）：
        {r, phi, theta, tx, ty, tz, fov}
    phi 是从 +Y 起的极角（90° = 水平），theta 是水平方位角。
    契约要求 Z-up，转换公式固定：`(x, y, z)_web → (x, -z, y)`。

    静态机位就是把首帧和末帧写成同一位置 —— 这样渲出来的是一段
    "机位不动"的镜头；两镜之间的"闪切"由各渲各的自然形成。
    """
    def _f(key: str, default: float) -> float:
        try:
            return float(cam.get(key) if cam.get(key) is not None else default)
        except (TypeError, ValueError):
            return default

    r = _f("r", 6.0)
    phi = math.radians(_f("phi", 88.0))
    theta = math.radians(_f("theta", 0.0))
    tx, ty, tz = _f("tx", 0.0), _f("ty", 1.0), _f("tz", 0.0)

    px = tx + r * math.sin(phi) * math.cos(theta)
    py = ty + r * math.cos(phi)
    pz = tz + r * math.sin(phi) * math.sin(theta)
    pos = [round(px, 4), round(-pz, 4), round(py, 4)]      # web → Z-up
    tgt = [round(tx, 4), round(-tz, 4), round(ty, 4)]

    fps = int(render.get("fps") or 24)
    frames = max(2, int(round(float(seconds) * fps)))
    keys = [{"frame": 0, "position": pos, "target": tgt},
            {"frame": frames - 1, "position": pos, "target": tgt}]
    return call("wb_track_set",
                {"camera": {"keys": keys, "vertical_fov": _f("fov", 45.0)},
                 "render": {"seconds": float(seconds), **render}}, 90)


def render_shot(shot: Dict[str, Any], out_dir: str, *,
                render: Optional[Dict[str, int]] = None,
                timeout: float = 600.0) -> Dict[str, Any]:
    """把**一个镜头**交给工作台出白膜。

    走契约里的单步工具（不是 wb_handoff —— 那条是"一个 GLB 转一圈"，
    这里要的是"按当前场景 + 这镜的时长/画幅出一段"）：

        wb_camera → wb_track_orbit → wb_render_submit
        → wb_render_status(轮询) → 下载产物 → wb_render_bind

    Returns:
        {ok, index, video?, frames?, job_id?, status?, steps:[...], error?}
    """
    render = dict(render or render_spec())
    index = int(shot.get("order_index") or 0) + 1
    seconds = float(shot.get("duration_seconds") or 3.0)
    steps: List[Dict[str, Any]] = []
    out: Dict[str, Any] = {"ok": False, "index": index, "steps": steps}

    # ① 机位 / 轨迹
    #
    # ★ 2026-10-25 实测：`wb_track_orbit` **不认 target** —— 传 tx/ty/tz/r/phi
    #   进去，它照样绕场景默认中心转，结果是"两镜同一个视角"（白膜零分的真因）。
    #   所以只要分镜带了自己的机位（`shot.previz_camera`），就走
    #   `wb_track_set` 显式关键帧：静态机位 = 首尾两帧同一位置，
    #   多镜之间靠"各渲各的"实现闪切（用户要的"从一个点闪到另一个点"）。
    cam = _camera_for(shot)
    if isinstance(cam, dict) and cam:
        trk = _set_static_camera(cam, seconds, render)
    else:
        trk = call("wb_track_orbit", {"seconds": seconds, **render, "turns": 1}, 90)
    steps.append({"step": "track", "ok": bool(trk.get("ok")),
                  "mode": "track_set" if (isinstance(cam, dict) and cam) else "orbit",
                  "detail": trk.get("data") if trk.get("ok") else trk.get("error")})
    if not trk.get("ok"):
        out["error"] = "铺相机轨迹失败：%s" % trk.get("error")
        return out

    # ② 提交给 VideoForge 服务（Blender 后台渲染）
    sub = call("wb_render_submit", {"seconds": seconds, **render}, 120)
    steps.append({"step": "submit", "ok": bool(sub.get("ok")),
                  "job_id": (sub.get("data") or {}).get("job_id") if isinstance(sub.get("data"), dict) else None,
                  "detail": None if sub.get("ok") else sub.get("error")})
    if not sub.get("ok"):
        out["error"] = "提交渲染失败：%s" % sub.get("error")
        return out
    job_id = ""
    if isinstance(sub.get("data"), dict):
        job_id = str(sub["data"].get("job_id") or "")
    if not job_id:
        out["error"] = "提交渲染没拿到 job_id：%s" % json.dumps(sub.get("data"), ensure_ascii=False)[:300]
        return out
    out["job_id"] = job_id

    # ③ 盯进度（页面 HUD 也会同时接管，这里只负责等终态）
    final = _wait(job_id, time.time() + timeout)
    status = str(final.get("status") or "").lower()
    out["status"] = status or "unknown"
    steps.append({"step": "poll", "ok": status == "succeeded",
                  "status": out["status"], "progress": final.get("progress"),
                  "error": final.get("error")})
    if status != "succeeded":
        out["error"] = ("渲染未成功：%s%s" % (out["status"],
                        ("；" + str(final.get("error"))) if final.get("error") else ""))
        return out

    # ④ 产物落盘：mp4 → 镜NN.mp4，首帧 → 镜NN/f0001.png
    manifest = final.get("manifest") or {}
    outs = manifest.get("outputs") or final.get("outputs") or []
    shdir = os.path.join(out_dir, "镜%02d" % index)
    got_video = ""
    got_frames: List[str] = []
    for f in outs:
        if not isinstance(f, dict):
            continue
        kind = str(f.get("kind") or "")
        url = str(f.get("url") or "")
        if not url:
            continue
        if kind == "video":
            dst = os.path.join(out_dir, "镜%02d.mp4" % index)
        elif kind == "first_frame":
            dst = os.path.join(shdir, "f0001.png")
        else:
            continue
        saved = _download(url, dst)
        steps.append({"step": "download", "kind": kind, "ok": saved.get("ok"),
                      "path": saved.get("path"), "bytes": saved.get("bytes"),
                      "error": saved.get("error")})
        if saved.get("ok"):
            if kind == "video":
                got_video = dst
            else:
                got_frames.append(dst)

    out["video"] = got_video
    out["frames"] = got_frames
    if not got_video:
        out["error"] = "渲染成功但没有下载到视频产物（manifest.outputs=%s）" % (
            json.dumps(outs, ensure_ascii=False)[:300])
        return out

    # ⑤ 绑到分镜（工作台 → VideoForge 的官方绑定口）
    pid = str(shot.get("project_id") or "")
    sid = str(shot.get("id") or "")
    if pid and sid:
        bind = call("wb_render_bind",
                    {"project_id": pid, "shot_id": sid, "job_id": job_id}, 60)
        steps.append({"step": "bind", "ok": bool(bind.get("ok")),
                      "detail": bind.get("data") if bind.get("ok") else bind.get("error")})

    out["ok"] = True
    return out


def _ready_summary(rdy: Dict[str, Any]) -> Dict[str, Any]:
    """把 ensure_ready 的结果压成返回体里要带的字段（别把整段 probe 塞回去）。"""
    if not isinstance(rdy, dict):
        return {}
    return {"ok": bool(rdy.get("ok")), "step": rdy.get("step"),
            "reason": rdy.get("reason"), "next_action": rdy.get("next_action"),
            "actions": rdy.get("actions") or [],
            "view": rdy.get("view"), "scene_id": rdy.get("scene_id"),
            "elapsed_ms": rdy.get("elapsed_ms")}


def _not_ready(pr: Dict[str, Any], rdy: Dict[str, Any], out_dir: str) -> Dict[str, Any]:
    """通道没就绪的统一返回（reason / step / next_action 三段都要有）。

    ★ 为什么 reason 优先取就绪器的：就绪器说的是「卡在哪一层、为什么自动修复失败」
      （比如 8765 没起），比 probe 的「scene_id 为空」更靠近根因。只有就绪器自己
      说 ok、却复核不通过（页面状态漂移）时，才用 probe 的话。
    """
    ready_ok = bool(rdy.get("ok"))
    reason = (pr.get("reason") if ready_ok else rdy.get("reason")) or pr.get("reason") or "白膜工作台不可用"
    next_action = ((pr.get("next_action") if ready_ok else rdy.get("next_action"))
                   or pr.get("next_action") or rdy.get("next_action")
                   or "检查 openVideoForge 工作台后重试")
    return {"ok": False, "source": "mcp", "out_dir": out_dir,
            "reason": reason,
            "step": rdy.get("step") or pr.get("step"),
            "next_action": next_action,
            "ready": _ready_summary(rdy),
            "probe": {k: v for k, v in pr.items() if k != "health"}}


def render_project_previz_mcp(db, project_id: str, *,
                              only: Optional[List[int]] = None,
                              out_dir: str = "",
                              timeout: int = 900) -> Dict[str, Any]:
    """整个项目的白膜（openVideoForge 通道）。

    返回结构与 `previz_blender.render_project_previz` 对齐，
    调用方（agent_tools_production.t_render_previz）不用改下游：
        {ok, source, out_dir, shots:[{index, frames, video}], steps, note?, reason?,
         step?, next_action?, ready?}

    ★ 渲染前先 `previz_channel.ensure_ready()`：桥没起就起、页面没开就开、
      不在编辑器就新建场景、scene_id 为空就导出一次。失败时 step/reason/
      next_action 直接可执行，调用方照旧回退 Blender。
    """
    from core.previz_blender import _outputs_root  # 沿用同一套目录约定
    from core import previz_channel as _ch

    out_dir = out_dir or os.path.join(_outputs_root(db), project_id, "previz")
    os.makedirs(out_dir, exist_ok=True)

    # ① 通道自动就绪（桥 → 页面 → 编辑器 → 场景）
    try:
        rdy = _ch.ensure_ready(bridge=bridge_url())
    except Exception as _e:  # noqa: BLE001 就绪器自己炸了也要如实报，不能吞
        rdy = {"ok": False, "step": "ready", "reason": "%s: %s" % (type(e).__name__, _e),
               "next_action": "看日志里 previz_channel 的异常栈；或用 --check 诊断"}
    # ② 就绪器跑完再按同一套判据复核（就绪器也可能自认为成功而页面已变）
    #
    # ★ 注意：probe() 只看「桥+页面+编辑器+scene_id」，它**看不到 8765**；
    #   而就绪器还多查了渲染服务这一层。所以这里必须 `not rdy.ok` 也算没就绪，
    #   否则 8765 没起时 probe 通过、一路走到提交才报一句「Failed to fetch」。
    pr = probe()
    if not rdy.get("ok") or not pr.get("ok"):
        _LOG.warning("白膜工作台通道没就绪，不回退前先说清：%s", _ch.describe(rdy))
        return _not_ready(pr, rdy, out_dir)

    shots = [s for s in (db.list_shots(project_id) or [])
             if only is None or (int(s.get("order_index") or 0) + 1) in only]
    if not shots:
        return {"ok": False, "source": "mcp", "out_dir": out_dir,
                "reason": "项目里没有可渲的分镜"}

    render = render_spec()
    done: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    deadline = time.time() + max(60, int(timeout))
    for shot in shots:
        idx = int(shot.get("order_index") or 0) + 1
        left = max(30.0, deadline - time.time())
        r = render_shot(shot, out_dir, render=render, timeout=left)
        if r.get("ok"):
            done.append({"index": idx,
                         "frames": r.get("frames") or [],
                         "video": r.get("video") or "",
                         "job_id": r.get("job_id") or ""})
            # ★ 与 Blender 通道**同一套落库口径**（previz_blender.py:415）：
            #   previz_path 存**首帧 PNG**，白膜视频由 previz_ref.white_model_video
            #   按「镜NN.mp4 与 镜NN/f0001.png 同目录」的约定自动找到。
            #   不写这一步，阶段会卡在"没渲过"——文件在盘上也没用。
            try:
                _frames = r.get("frames") or []
                db.update_shot(str(shot.get("id")),
                               previz_status="rendered",
                               previz_path=_frames[0] if _frames else None)
            except Exception as _e:  # noqa: BLE001
                _LOG.warning("白膜(openVideoForge) 写回分镜失败（镜%s）：%s", idx, _e)
            _LOG.info("白膜(openVideoForge) 镜%s ok：%s", idx, r.get("video"))
        else:
            failed.append({"index": idx, "reason": r.get("error") or "未知原因",
                           "steps": r.get("steps")})
            _LOG.warning("白膜(openVideoForge) 镜%s 失败：%s", idx, r.get("error"))

    if not done:
        return {"ok": False, "source": "mcp", "out_dir": out_dir,
                "reason": (failed[0]["reason"] if failed else "一镜都没渲出来"),
                "step": "render",
                "next_action": ("看 failed[].steps 里卡在哪一步：track/submit/poll/download；"
                                "Blender 侧报错看 %LOCALAPPDATA%\\VideoForge\\data\\studio_bridge\\jobs\\<id>\\blender.log"),
                "failed": failed, "ready": _ready_summary(rdy),
                "probe": {k: v for k, v in pr.items() if k != "health"}}

    note = ""
    if failed:
        note = ("%d 镜没渲出来：%s" % (len(failed),
                "；".join("镜%s %s" % (f["index"], f["reason"]) for f in failed[:3])))
    return {"ok": True, "source": "mcp", "out_dir": out_dir,
            "shots": done,
            "videos": [d["video"] for d in done if d.get("video")],
            "failed": failed,
            "note": note or None,
            "ready": _ready_summary(rdy),
            "bridge": bridge_url()}


# ───────────────────────── 自测 ─────────────────────────

if __name__ == "__main__":  # pragma: no cover - 手工排障用
    import sys

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    if "--probe" in sys.argv:
        print(json.dumps(probe(), ensure_ascii=False, indent=2))
    else:
        print("用法：python -m core.previz_mcp --probe")
