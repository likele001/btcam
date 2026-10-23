"""异步任务队列。

目标：排料 / 刀路 / 布尔运算这类长耗时计算绝不阻塞 HTTP 与 WS 协程。

结构：
    API 线程  --submit()-->  asyncio.Queue  --worker协程-->  run_in_executor(计算池)
                                                     |
                                                     +--> 通过 Hub 广播进度/结果

约定：
- 任务函数签名固定为 `fn(params: dict, report: ProgressReporter) -> dict`
  因此函数必须定义在模块顶层（可被进程池 pickle）；
- 任务函数内部应周期性调用 `report(0.0~1.0, '文案')`，既能驱动前端进度条，
  也是响应取消的唯一时机；
- 结果字典里的 `polylines` 字段（可选）会被编码成 BGC1 二进制块随帧下发，
  其余字段作为小 JSON meta。
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
import uuid
from concurrent.futures import Executor, ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from app.config import settings
from app.core.hub import hub
from app.core.log_store import log_store, task_label
from app.core.protocol import MsgType, encode_polylines
from app.models import db

logger = logging.getLogger(__name__)

# 进度日志的节流参数：刀路/排料是按轮廓/零件回调的，逐条记会把环形缓冲刷满。
# 取“时间间隔或进度跳跃”任一满足即记一条：既保留阶段文案，又不淹没报错。
LOG_MIN_INTERVAL = 1.5      # 秒
LOG_MIN_STEP = 0.08         # 进度 8%


# ---------------------------------------------------------------- 异常
class TaskCancelled(Exception):
    """任务被取消（客户端关闭 / 用户点取消）。"""


# ---------------------------------------------------------------- 进度上报
class ProgressReporter:
    """线程内进度上报器：所有方法都自动切回事件循环，可安全在计算线程里调用。"""

    def __init__(self, task_id: str, loop: asyncio.AbstractEventLoop,
                 cancel_flag: Optional[Dict[str, bool]] = None, kind: str = "") -> None:
        self.task_id = task_id
        self.kind = kind
        self._loop = loop
        self._cancel = cancel_flag if cancel_flag is not None else {}
        self._last = -1.0
        self._last_message = ""
        self._logged_at = 0.0
        self._logged_value = -1.0

    def __call__(self, progress: float, message: str = "", **extra: Any) -> None:
        value = max(0.0, min(1.0, float(progress)))
        # 阶段文案变化才写日志，并按时间/进度跳跃节流
        text = str(message or "").strip()
        if text and text != self._last_message:
            self._last_message = text
            now = time.monotonic()
            if (value >= 1.0 or now - self._logged_at >= LOG_MIN_INTERVAL
                    or value - self._logged_value >= LOG_MIN_STEP):
                self._logged_at = now
                self._logged_value = value
                log_store.task_progress(self.task_id, self.kind, value, text)
        # 进度变化小于 1% 时不再刷屏
        if value - self._last < 0.01 and value < 1.0:
            return
        self._last = value
        meta = {"taskId": self.task_id, "progress": round(value, 4), "message": message}
        meta.update(extra)
        hub.broadcast_threadsafe(MsgType.TASK_PROGRESS, meta)

    def check_cancelled(self) -> None:
        if self._cancel.get("cancelled"):
            raise TaskCancelled(self.task_id)


# ---------------------------------------------------------------- 任务描述
TaskFn = Callable[[Dict[str, Any], ProgressReporter], Dict[str, Any]]


@dataclass
class TaskInfo:
    id: str
    kind: str
    project_id: Optional[int]
    status: str = "queued"      # queued | running | done | failed | cancelled
    progress: float = 0.0
    message: str = ""
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None


# ---------------------------------------------------------------- 队列
class TaskQueue:
    def __init__(self, workers: Optional[int] = None,
                 use_process_pool: Optional[bool] = None) -> None:
        # 未显式指定时走全局配置（BTCAM_WORKERS / BTCAM_PROCESS_POOL 可覆盖）
        self._workers = max(1, workers or settings.worker_processes)
        self._use_process_pool = (
            settings.use_process_pool if use_process_pool is None else use_process_pool
        )
        # 队列必须延迟到 start() 里创建：Python 3.8/3.9 的 asyncio.Queue 在构造时
        # 绑定当前 loop，模块导入期建会绑到隐式创建的废 loop，于是 put/get 的 future
        # 挂到错误的 loop 上，表现为任务永远停在 queued、worker 协程却已就绪。
        self._queue: Optional["asyncio.Queue[tuple[TaskInfo, TaskFn, Dict[str, Any]]]"] = None
        self._executor: Optional[Executor] = None
        self._runners: list = []
        self._cancel_flags: Dict[str, Dict[str, bool]] = {}
        self._tasks: Dict[str, TaskInfo] = {}
        self._started = False

    # ------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        if self._started:
            return
        # 见 __init__ 注释：必须在事件循环内创建，才能绑定到正在运行的 loop
        self._queue = asyncio.Queue()
        if self._use_process_pool:
            self._executor = ProcessPoolExecutor(max_workers=self._workers)
            logger.info("计算池: ProcessPoolExecutor x%d", self._workers)
        else:
            self._executor = ThreadPoolExecutor(max_workers=self._workers,
                                                thread_name_prefix="btcam-worker")
            logger.info("计算池: ThreadPoolExecutor x%d", self._workers)
        self._runners = [asyncio.create_task(self._worker_loop(i), name=f"task-worker-{i}")
                         for i in range(self._workers)]
        self._started = True

    async def stop(self) -> None:
        for runner in self._runners:
            runner.cancel()
        self._runners.clear()
        if self._executor is not None:
            # cancel_futures 是 Python 3.9+ 参数，3.8 上退化为普通 shutdown
            try:
                self._executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                self._executor.shutdown(wait=False)
            self._executor = None
        self._started = False

    # ------------------------------------------------------------ 提交 / 取消
    async def submit(self, kind: str, fn: TaskFn, params: Dict[str, Any],
                     project_id: Optional[int] = None) -> str:
        task_id = uuid.uuid4().hex[:12]
        info = TaskInfo(id=task_id, kind=kind, project_id=project_id)
        self._tasks[task_id] = info
        self._cancel_flags[task_id] = {"cancelled": False}

        db.record_task(task_id, project_id, kind, "queued")
        await hub.broadcast(MsgType.TASK_SUBMITTED, {"taskId": task_id, "kind": kind, "status": "queued"})
        assert self._queue is not None, "任务队列未启动：先 await task_queue.start()"
        await self._queue.put((info, fn, params))
        logger.info("任务入队 %s id=%s", task_label(kind), task_id)
        return task_id

    def cancel(self, task_id: str) -> bool:
        flag = self._cancel_flags.get(task_id)
        if flag is None:
            return False
        flag["cancelled"] = True
        return True

    def get(self, task_id: str) -> Optional[TaskInfo]:
        return self._tasks.get(task_id)

    def all_tasks(self) -> Dict[str, TaskInfo]:
        return dict(self._tasks)

    # ------------------------------------------------------------ worker
    async def _worker_loop(self, index: int) -> None:
        loop = asyncio.get_running_loop()
        # 取本地引用，避免每轮属性查找；非 None 已由 start() 保证
        queue = self._queue
        assert queue is not None
        while True:
            info, fn, params = await queue.get()
            reporter = ProgressReporter(info.id, loop, self._cancel_flags.get(info.id), info.kind)
            info.status = "running"
            db.record_task(info.id, info.project_id, info.kind, "running")
            await hub.broadcast(MsgType.TASK_PROGRESS,
                                {"taskId": info.id, "progress": 0.0, "status": "running"})
            logger.info("任务开始 %s id=%s", task_label(info.kind), info.id)
            started = time.perf_counter()
            try:
                assert self._executor is not None
                result = await loop.run_in_executor(
                    self._executor, functools.partial(fn, params, reporter)
                )
                elapsed = time.perf_counter() - started
                info.status, info.progress, info.message = "done", 1.0, "完成"
                await self._emit_result(info, result, elapsed)
                logger.info("任务完成 %s id=%s 耗时 %.0fms",
                            task_label(info.kind), info.id, elapsed * 1000)
            except TaskCancelled:
                info.status, info.message = "cancelled", "已取消"
                await hub.broadcast(MsgType.TASK_FAILED,
                                    {"taskId": info.id, "status": "cancelled", "message": "任务已取消"})
                db.record_task(info.id, info.project_id, info.kind, "cancelled", info.progress,
                               "已取消", finished=True)
                logger.warning("任务取消 %s id=%s（进度 %d%%）",
                               task_label(info.kind), info.id, int(info.progress * 100))
                if info.kind == "layout_nest":
                    # 取消可能发生在已落库之后：补一帧真实状态，避免前端停在旧位置
                    from app.api.routes_layout import push_layout
                    await push_layout(project_id=info.project_id)
            except Exception as exc:  # 任何计算异常都转成前端可读的失败帧
                logger.exception("任务失败 %s id=%s", task_label(info.kind), info.id)
                info.status, info.message = "failed", str(exc)
                await hub.broadcast(MsgType.TASK_FAILED,
                                    {"taskId": info.id, "status": "failed", "message": str(exc)})
                db.record_task(info.id, info.project_id, info.kind, "failed", info.progress,
                               str(exc), finished=True)
            finally:
                info.finished_at = time.time()
                self._cancel_flags.pop(info.id, None)
                queue.task_done()

    async def _emit_result(self, info: TaskInfo, result: Dict[str, Any], elapsed: float) -> None:
        """结果帧：图形走二进制，其余小字段走 meta。"""
        polylines = result.pop("polylines", None) or []
        msg_type = MsgType.TASK_RESULT
        if info.kind == "nest":
            msg_type = MsgType.NEST_RESULT
        elif info.kind in ("toolpath", "toolpath_preview"):
            msg_type = MsgType.TOOLPATH_PREVIEW

        meta = {
            "taskId": info.id,
            "kind": info.kind,
            "status": "done",
            "elapsedMs": round(elapsed * 1000, 1),
        }
        meta.update({k: v for k, v in result.items() if k != "polylines"})
        payload = encode_polylines(polylines) if polylines else b""
        await hub.broadcast(msg_type, meta, payload)
        db.record_task(info.id, info.project_id, info.kind, "done", 1.0, "完成", finished=True)

        if info.kind == "layout_nest":
            # 自动排料的结果已写回 layout_placement：紧跟一帧 LAYOUT_STATE，
            # 前端无需再发同步请求就能重绘排料板。
            from app.api.routes_layout import push_layout
            await push_layout(project_id=info.project_id)


# 全局单例：main.py 里 start/stop；执行器参数来自 config.Settings
task_queue = TaskQueue()
