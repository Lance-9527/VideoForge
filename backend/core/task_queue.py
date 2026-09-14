"""
VideoForge · 异步任务队列（v2 · 借鉴 MPT 状态机）

特点：
- 基于 asyncio + 后台线程池（避免阻塞事件循环）
- 进度回调 + SSE 推送
- 任务持久化（重启后能继续）
- 简单的并发控制

借鉴 MoneyPrinterTurbo task.py 的设计：
- 状态机细化：pending → script → material → shot → voice → subtitle → bgm → postprocess → publish → success/failed/cancelled
- failed_stage 字段：失败任务能精确定位到「卡在哪一步」
- progress_message 字段：实时可读进度
- cancellable 字段：标记是否可取消（部分任务取消代价太高时设 False）
- cancel_task(task_id)：前端可触发取消运行中的任务
"""

import asyncio
import logging
import threading
from datetime import datetime
from enum import Enum
from typing import Dict, Any, Optional, Callable, Awaitable, List, Union
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from collections import defaultdict
from .db import Database


logger = logging.getLogger("videoforge.task_queue")


# ──────────── 任务状态机 ────────────


class TaskState(str, Enum):
    """借鉴 MPT 任务状态机。

    阶段状态（中间）：pending / script / material / shot / voice / subtitle / bgm / postprocess / publish
    终态：success / failed / cancelled
    """
    PENDING      = "pending"
    SCRIPT       = "script"
    MATERIAL     = "material"
    SHOT         = "shot"
    VOICE        = "voice"
    SUBTITLE     = "subtitle"
    BGM          = "bgm"
    POSTPROCESS  = "postprocess"
    PUBLISH      = "publish"
    SUCCESS      = "success"
    FAILED       = "failed"
    CANCELLED    = "cancelled"


# 中文可读标签（前端展示用）
STAGE_LABEL: Dict[TaskState, str] = {
    TaskState.PENDING:     "等待中",
    TaskState.SCRIPT:      "生成剧本",
    TaskState.MATERIAL:    "准备素材",
    TaskState.SHOT:        "生成分镜视频",
    TaskState.VOICE:       "合成配音",
    TaskState.SUBTITLE:    "对齐字幕",
    TaskState.BGM:         "混入 BGM",
    TaskState.POSTPROCESS: "后期拼接",
    TaskState.PUBLISH:     "跨平台发布",
    TaskState.SUCCESS:     "已完成",
    TaskState.FAILED:      "失败",
    TaskState.CANCELLED:   "已取消",
}


TERMINAL_STATES = {TaskState.SUCCESS, TaskState.FAILED, TaskState.CANCELLED}

# 这些阶段才能作为 failed_stage（终态本身不能当 stage）
FAILED_STAGE_VALUES = {
    TaskState.SCRIPT.value, TaskState.MATERIAL.value, TaskState.SHOT.value,
    TaskState.VOICE.value, TaskState.SUBTITLE.value, TaskState.BGM.value,
    TaskState.POSTPROCESS.value, TaskState.PUBLISH.value,
}


def normalize_state(s: Union[str, TaskState, None]) -> TaskState:
    """容忍 str / Enum / None 输入，返回合法 TaskState。"""
    if s is None:
        return TaskState.PENDING
    if isinstance(s, TaskState):
        return s
    try:
        return TaskState(str(s))
    except ValueError:
        # 老数据 / 未知值：兜底
        return TaskState.PENDING


# ──────────── 数据结构 ────────────


@dataclass
class TaskHandle:
    """任务句柄（内存中）"""
    task_id: str
    type: str
    payload: Dict[str, Any]
    status: str = TaskState.PENDING.value
    progress: float = 0.0
    progress_message: str = ""
    failed_stage: Optional[str] = None
    error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    cancellable: bool = True
    progress_callbacks: List[Callable] = field(default_factory=list)
    # asyncio.Task 引用：用于 cancel_task() 调用 task.cancel()
    task_ref: Optional[asyncio.Task] = None
    # 标记是否用户已请求取消（worker 在下个 stage 切换时检查）
    cancel_requested: bool = False


# ──────────── TaskQueue ────────────


class TaskQueue:
    """异步任务队列"""

    def __init__(self, db: Database, max_concurrent: int = 3):
        self.db = db
        self._handles: Dict[str, TaskHandle] = {}
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._executor = ThreadPoolExecutor(max_workers=max_concurrent * 2)
        self._lock = asyncio.Lock()

    def submit(self, task_id: str, task_type: str, payload: Dict[str, Any]) -> TaskHandle:
        """提交任务，返回句柄。DB 已在外层 create_task 时插入。"""
        handle = TaskHandle(task_id=task_id, type=task_type, payload=payload)
        self._handles[task_id] = handle
        # 调度执行
        handle.task_ref = asyncio.create_task(self._execute(handle))
        return handle

    async def _execute(self, handle: TaskHandle):
        """执行任务（在 worker 中）。负责状态流转、失败归因、取消响应。"""
        async with self._semaphore:
            if handle.cancel_requested:
                self._mark_cancelled(handle)
                return

            handle.status = TaskState.SCRIPT.value   # 进入第一个 stage（worker 内部会覆盖）
            self.db.update_task(
                handle.task_id,
                status=handle.status,
                started_at=datetime.now().isoformat(),
            )
            self._notify_progress(handle, "开始执行")

            current_stage = TaskState.SCRIPT
            try:
                handler = TASK_HANDLERS.get(handle.type)
                if not handler:
                    raise ValueError(f"No handler for task type: {handle.type}")

                # 把状态 / 阶段更新闭包注入 worker
                async def stage_setter(stage: Union[TaskState, str], progress: float = 0.0, message: str = "") -> None:
                    """worker 在每个阶段开始时调用，更新 DB + 内存状态 + 通知前端"""
                    nonlocal current_stage
                    if handle.cancel_requested:
                        raise asyncio.CancelledError()
                    s = normalize_state(stage)
                    current_stage = s
                    handle.status = s.value
                    if progress:
                        handle.progress = progress
                    handle.progress_message = message or STAGE_LABEL.get(s, s.value)
                    self.db.update_task(
                        handle.task_id,
                        status=handle.status,
                        progress=handle.progress,
                        progress_message=handle.progress_message,
                    )
                    self._notify_progress(handle, handle.progress_message)

                # 注入 stage_setter 到 payload（worker 可选用，保留向后兼容）
                worker_payload = dict(handle.payload or {})
                worker_payload["_stage_setter"] = stage_setter

                result = await handler(worker_payload, handle.progress_callbacks)
                handle.result = result
                handle.status = TaskState.SUCCESS.value
                handle.progress = 1.0
                handle.progress_message = STAGE_LABEL[TaskState.SUCCESS]
                self.db.update_task(
                    handle.task_id,
                    status=handle.status,
                    progress=1.0,
                    progress_message=handle.progress_message,
                    result=result,
                    finished_at=datetime.now().isoformat(),
                )
            except asyncio.CancelledError:
                self._mark_cancelled(handle)
            except Exception as e:
                logger.exception("Task %s failed at stage %s", handle.task_id, current_stage)
                handle.error = str(e)
                handle.status = TaskState.FAILED.value
                handle.failed_stage = current_stage.value if isinstance(current_stage, TaskState) else str(current_stage)
                self.db.update_task(
                    handle.task_id,
                    status=handle.status,
                    failed_stage=handle.failed_stage,
                    error=handle.error,
                    finished_at=datetime.now().isoformat(),
                )
            finally:
                self._notify_progress(handle, handle.progress_message)

    def _mark_cancelled(self, handle: TaskHandle) -> None:
        handle.status = TaskState.CANCELLED.value
        handle.progress_message = STAGE_LABEL[TaskState.CANCELLED]
        self.db.update_task(
            handle.task_id,
            status=handle.status,
            progress_message=handle.progress_message,
            finished_at=datetime.now().isoformat(),
        )

    def _notify_progress(self, handle: TaskHandle, message: str = "") -> None:
        for cb in handle.progress_callbacks:
            try:
                cb(handle.progress, handle.status, message)
            except Exception:
                pass

    def add_progress_callback(self, task_id: str, callback: Callable) -> None:
        if task_id in self._handles:
            self._handles[task_id].progress_callbacks.append(callback)

    def get_handle(self, task_id: str) -> Optional[TaskHandle]:
        return self._handles.get(task_id)

    def is_busy(self, task_id: str) -> bool:
        """判断任务是否仍在跑（含终态前的所有阶段）"""
        h = self._handles.get(task_id)
        if not h:
            return False
        return normalize_state(h.status) not in TERMINAL_STATES

    async def cancel_task(self, task_id: str, reason: str = "user requested") -> bool:
        """取消任务。

        - 如果任务尚未开始：直接置为 CANCELLED。
        - 如果任务正在跑：标记 cancel_requested，下一次 stage 切换时
          asyncio.CancelledError 由 worker 抛出让 _execute 进入 _mark_cancelled。
          同步阻塞型 worker（如调 ffmpeg）无法立即中断，只能在下个 stage 边界优雅退出。

        返回 True 表示取消指令已发出；False 表示任务已完成或不存在。
        """
        handle = self._handles.get(task_id)
        if not handle:
            return False

        if normalize_state(handle.status) in TERMINAL_STATES:
            return False

        handle.cancel_requested = True
        handle.progress_message = f"取消中... ({reason})"
        self.db.update_task(
            task_id,
            progress_message=handle.progress_message,
        )
        self._notify_progress(handle, handle.progress_message)

        # 取消 asyncio.Task：若 worker 是 await 状态会立刻 CancelledError；
        # 若 worker 阻塞在同步代码中，会在下个 await 点退出。
        if handle.task_ref and not handle.task_ref.done():
            handle.task_ref.cancel()

        logger.info("Task %s cancel requested (reason=%s)", task_id, reason)
        return True

    async def shutdown(self):
        for handle in list(self._handles.values()):
            if handle.task_ref and not handle.task_ref.done():
                handle.task_ref.cancel()
        self._executor.shutdown(wait=False)


# ──────────── 任务处理器注册表 ────────────

TASK_HANDLERS: Dict[str, Callable] = {}


def register_task(task_type: str):
    """装饰器：注册任务处理器"""
    def decorator(func):
        TASK_HANDLERS[task_type] = func
        return func
    return decorator


def make_progress_updater(task_id: str, db: Database,
                          progress_callbacks: List[Callable]):
    """返回一个更新进度的函数（向后兼容：旧 worker 仍可用）"""
    def update(progress: float, message: str = ""):
        db.update_task(task_id, progress=progress)
        if message:
            db.update_task(task_id, progress_message=message)
        for cb in progress_callbacks:
            try:
                cb(progress, message or "")
            except Exception:
                pass
    return update
