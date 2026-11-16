"""SuperSplat 命令队列与产物落盘（2026-10-31）

## 为什么需要这个模块
SuperSplat 跑在**客户端的 iframe** 里（浏览器里），sidecar 摸不到它；
但 agent（DSH）要能指挥它。于是做一条**三段式回路**：

    agent ──POST /api/studio/splat/commands──▶ sidecar 队列
                                                 │
    客户端轮询 GET /api/studio/splat/commands ◀───┘
        └─ postMessage 给 iframe → SuperSplat 执行 → 回传结果
             └─ POST /api/studio/splat/result ▶ sidecar（agent 轮询/取回）

## 产物落盘
SuperSplat 导出的图片/视频由**浏览器**回传给 sidecar，sidecar 写进
**openVideoForge 的输出目录**（默认 ``D:\\下载\\OpenVideoForge``，可用环境变量
``OPENVIDEOFORGE_OUT_DIR`` 覆盖）⇒ 两条线（商品/场景）的产物在同一个地方 ✓。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, File, HTTPException, UploadFile
from fastapi.responses import FileResponse


def _out_dir() -> Path:
    """openVideoForge 的输出目录（两条线共用）。"""
    raw = (os.environ.get("OPENVIDEOFORGE_OUT_DIR") or "").strip()
    base = Path(raw) if raw else Path(r"D:\下载\OpenVideoForge")
    base = base.expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    return base


def _safe_name(name: str, fallback: str = "shot") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", (name or "").strip()).strip(". ")
    return (cleaned[:80] or fallback)


class SplatBridge:
    """命令队列 + 产物登记（进程内状态，sidecar 单进程足够）。"""

    def __init__(self) -> None:
        self._pending: list[dict[str, Any]] = []
        self._results: dict[str, dict[str, Any]] = {}
        self._exports: list[dict[str, Any]] = []

    # ── 命令队列 ──────────────────────────────────────────────
    def enqueue(self, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        if not tool or not isinstance(tool, str):
            raise HTTPException(status_code=422, detail="tool 不能为空")
        cmd = {"id": uuid.uuid4().hex[:16], "tool": tool, "args": args or {}, "created": _now()}
        self._pending.append(cmd)
        # 队列太长就丢最老的（防止客户端不在线时无限堆积）
        if len(self._pending) > 64:
            self._pending = self._pending[-64:]
        return cmd

    def take(self) -> dict[str, Any]:
        """客户端来取命令（取出即出队）。"""
        if not self._pending:
            return {"commands": []}
        cmds = list(self._pending)
        self._pending.clear()
        return {"commands": cmds}

    def complete(self, cmd_id: str, ok: bool, result: Any = None, error: Any = None) -> dict[str, Any]:
        self._results[cmd_id] = {
            "id": cmd_id, "ok": bool(ok), "result": result, "error": error,
            "finished": _now(),
        }
        if len(self._results) > 128:
            self._results = dict(list(self._results.items())[-128:])
        return self._results[cmd_id]

    def result(self, cmd_id: str) -> dict[str, Any] | None:
        return self._results.get(cmd_id)

    def results(self) -> list[dict[str, Any]]:
        return list(self._results.values())

    # ── 产物 ─────────────────────────────────────────────────
    def save_export(self, filename: str, data_b64: str, media_type: str = "application/octet-stream") -> dict[str, Any]:
        """浏览器回传二进制（base64）⇒ 写到 openVideoForge 输出目录。"""
        import base64

        if not data_b64:
            raise HTTPException(status_code=422, detail="缺少文件内容")
        name = _safe_name(filename, "splat_export")
        target = _out_dir() / name
        try:
            raw = base64.b64decode(data_b64, validate=False)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=422, detail=f"base64 解码失败：{exc}") from exc
        target.write_bytes(raw)
        item = {
            "name": name,
            "path": str(target),
            "bytes": len(raw),
            "media_type": media_type,
            "saved": _now(),
        }
        self._exports.append(item)
        if len(self._exports) > 64:
            self._exports = self._exports[-64:]
        return item

    def exports(self) -> list[dict[str, Any]]:
        return list(self._exports)

    def import_sources(self) -> list[dict[str, Any]]:
        """可供 SuperSplat 导入的本地场景文件（splat/ply 优先，其次 GLB）。"""
        out: list[dict[str, Any]] = []
        roots = [_out_dir()]
        for root in roots:
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if path.is_file() and path.suffix.lower() in {".ply", ".splat", ".ksplat", ".spz", ".sog", ".glb"}:
                    out.append({"name": path.name, "path": str(path), "bytes": path.stat().st_size})
        return out[-200:]


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_splat_router() -> APIRouter:
    router = APIRouter(prefix="/api/studio/splat", tags=["studio-splat"])
    bridge = SplatBridge()

    @router.get("/commands")
    def get_commands() -> dict[str, Any]:
        """客户端轮询：取走待执行的命令。"""
        return bridge.take()

    @router.post("/commands")
    def post_command(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """agent 投递一条 SuperSplat 工具调用（tool + args）。"""
        tool = str(payload.get("tool") or "").strip()
        args = payload.get("args")
        return {"ok": True, "command": bridge.enqueue(tool, args if isinstance(args, dict) else {})}

    @router.post("/result")
    def post_result(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """客户端回传执行结果。"""
        cmd_id = str(payload.get("id") or "").strip()
        if not cmd_id:
            raise HTTPException(status_code=422, detail="缺少命令 id")
        return {"ok": True, "result": bridge.complete(cmd_id, bool(payload.get("ok")), payload.get("result"), payload.get("error"))}

    @router.get("/results")
    def get_results() -> dict[str, Any]:
        """agent 读取执行结果。"""
        return {"results": bridge.results()}

    @router.post("/exports")
    def post_export(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """浏览器回传渲染产物 ⇒ 落盘到 openVideoForge 输出目录。"""
        return {"ok": True, "export": bridge.save_export(
            str(payload.get("filename") or ""),
            str(payload.get("data") or ""),
            str(payload.get("media_type") or "application/octet-stream"),
        )}

    @router.get("/exports")
    def get_exports() -> dict[str, Any]:
        return {"exports": bridge.exports(), "out_dir": str(_out_dir())}

    @router.get("/sources")
    def get_sources() -> dict[str, Any]:
        """可导入的本地场景文件。"""
        return {"sources": bridge.import_sources()}

    @router.get("/file")
    def get_file(name: str) -> FileResponse:
        """按名字取已落盘的产物（给 <img>/<video> 用）。"""
        target = (_out_dir() / _safe_name(name, "")).resolve()
        if not target.is_file() or not str(target).startswith(str(_out_dir())):
            raise HTTPException(status_code=404, detail="文件不存在")
        return FileResponse(str(target))

    @router.get("/files/{name}")
    def get_file_by_path(name: str) -> FileResponse:
        """按**路径**取文件 —— SuperSplat 加载 splat 用这个。

        ★ 为什么要有这个（实测踩过）：SuperSplat 的 `?load=<url>` 用
          `url.split('/').pop()` 当文件名 ⇒ 如果 URL 带查询串
          （`/file?name=x.ply`），拿到的文件名是 `file?name=x.ply`，
          **不以 .ply 结尾 ⇒ 格式识别失败 ⇒ 场景加载了但视口里什么都没有**。
          这里给不带查询串的路径式路由，文件名就干净了。
        """
        target = (_out_dir() / _safe_name(name, "")).resolve()
        if not target.is_file() or not str(target).startswith(str(_out_dir())):
            raise HTTPException(status_code=404, detail="文件不存在")
        return FileResponse(str(target))

    @router.get("/metadata")
    def get_metadata(name: str) -> dict[str, Any]:
        """重建模型的**坐标与尺度证据**：点数、包围盒、竖轴、尺度换算建议。

        ★ 为什么要有：摄影测量重建出来的点云坐标系是相机系、尺度是任意的。
          光看渲染图不算"对"，得能说出点数多少、竖轴是谁、一米对应多少单位。
        """
        from .splat_normalize import SplatFormatError, probe

        target = (_out_dir() / _safe_name(name, "")).resolve()
        if not target.is_file() or not str(target).startswith(str(_out_dir())):
            raise HTTPException(status_code=404, detail="文件不存在")
        try:
            return {"ok": True, "metadata": probe(target)}
        except SplatFormatError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/normalize")
    def post_normalize(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """**摆正 + 锚定尺度 + 落位**，产物另存为 `<name>.aligned.ply`。

        ★ 尺度必须由人给（"桌子宽 1.2m"），机器猜不出来 —— 不给 anchor
          就只摆正不缩放，并把尺度证据原样返回，绝不假装知道。
        ★ 原文件不动：重建结果和它的归一化版本是两件事。
        """
        from .splat_normalize import SplatFormatError, normalize_named

        name = str(payload.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="缺少 name")
        anchor = payload.get("anchor_meters")
        axis = str(payload.get("anchor_axis") or "width").strip() or "width"
        try:
            info = normalize_named(
                _out_dir(), name,
                anchor_meters=(float(anchor) if anchor is not None else None),
                anchor_axis=axis,
            )
        except SplatFormatError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"参数不对：{exc}") from exc
        info["name"] = Path(info["target"]).name
        return {"ok": True, "normalized": info}

    @router.post("/scene-from-image")
    async def scene_from_image(file: UploadFile = File(...)) -> dict[str, Any]:
        """**一张照片 → 可拆解的 3D 场景**（LDI 分层深度图，纯本地、零付费）。

        产物落进 openVideoForge 输出目录，返回 `name` / `camera` / `points`；
        客户端拿 `name` 调 SuperSplat 的 `import_url`（走 `/files/{name}` 路径式 URL），
        再按返回的 camera 摆回「还原原图」的机位。
        """
        from .scene_from_image import write_scene_from_image

        raw = await file.read()
        if not raw:
            raise HTTPException(status_code=422, detail="图片是空的")
        if len(raw) > 40 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="图片上限 40MB")
        stem = _safe_name(Path(file.filename or "scene").stem, "scene")
        target = _out_dir() / f"{stem}_ldi_{time.strftime('%Y%m%d_%H%M%S')}.ply"
        try:
            info = write_scene_from_image(raw, target)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"生成场景失败：{exc}") from exc
        return {"ok": True, "scene": info}

    return router
