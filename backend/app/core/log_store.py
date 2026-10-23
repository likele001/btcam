"""运行日志环形缓冲与告警广播（模块 9）。

为什么不用"让前端读日志文件"：
- 文件是给人排查用的，格式随 formatter 变化，前端解析等于把日志格式再耦合一次；
- 面板需要按级别/来源/关键词过滤，结构化记录才做得到。

三层职责：
    LogRingHandler  挂在 root logger 上，把 logging 记录转成结构化条目；
    LogStore        定长环形缓冲（默认 1000 条），线程安全，支持过滤查询；
    ALERT 帧        级别 >= warning 的条目通过 WS 即时推送，前端弹窗提示。

约定：
- 环形缓冲只保留本次运行；长期留存仍由 logs/backend.log 负责；
- ERROR 及以上自动广播；warning 默认只入面板，由业务显式调用 `alert()`
  推送（例如图形校验告警），避免把普通提示刷成弹窗；
- 相同 (来源, 级别, 文本) 在 3 秒内只广播一次，防止异常循环刷屏。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from app.core.hub import hub
from app.core.protocol import MsgType

LEVEL_ORDER: Dict[str, int] = {
    "debug": 10,
    "info": 20,
    "warning": 30,
    "error": 40,
    "critical": 50,
}

# 任务类型 -> 面板里的中文名（进度日志用）
TASK_LABELS: Dict[str, str] = {
    "import": "DXF 导入",
    "offset": "轮廓偏置",
    "intersect": "轮廓求交",
    "merge": "断线合并",
    "nest": "排料",
    "layout_nest": "自动排料",
    "toolpath": "刀路生成",
    "toolpath_preview": "刀路预览",
}

ALERT_DEDUPE_SECONDS = 3.0
MAX_MESSAGE_CHARS = 2000


def normalize_level(value: Any) -> str:
    """统一级别写法：WARN/WARNING/30 都落到标准小写名。"""
    text = str(value or "").strip().lower()
    if text in ("warn", "w"):
        return "warning"
    if text in ("err", "e"):
        return "error"
    if text in ("fatal", "crit", "c"):
        return "critical"
    if text in ("dbg", "d"):
        return "debug"
    return text if text in LEVEL_ORDER else "info"


def task_label(kind: str) -> str:
    return TASK_LABELS.get(str(kind or ""), str(kind or "任务"))


class LogStore:
    """定长日志缓冲。所有公开方法都可从计算线程调用。"""

    def __init__(self, capacity: int = 1000) -> None:
        self._lock = threading.Lock()
        self._capacity = max(50, int(capacity))
        self._entries: Deque[Dict[str, Any]] = deque(maxlen=self._capacity)
        self._seq = 0
        self._dedupe: Dict[str, float] = {}
        # 日志文件绝对路径：面板只展示，不负责读取
        self.file_path = ""

    # ------------------------------------------------------------ 配置
    @property
    def capacity(self) -> int:
        return self._capacity

    def set_capacity(self, capacity: int) -> None:
        """调整容量；保留最近的条目，旧的不迁移。"""
        size = max(50, int(capacity))
        with self._lock:
            if size == self._capacity:
                return
            self._capacity = size
            self._entries = deque(self._entries, maxlen=size)

    def clear(self) -> int:
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
            self._dedupe.clear()
            return count

    # ------------------------------------------------------------ 写入
    def append(self, level: Any, message: str, source: str = "app",
               timestamp: Optional[float] = None, task_id: Optional[str] = None,
               kind: Optional[str] = None, detail: Optional[str] = None,
               broadcast: Optional[bool] = None) -> Dict[str, Any]:
        """追加一条日志。`broadcast=None` 时 ERROR 及以上自动推送 ALERT。"""
        name = normalize_level(level)
        text = str(message or "").strip() or "(空消息)"
        entry = {
            "id": 0,
            "time": float(timestamp if timestamp is not None else time.time()),
            "level": name,
            "source": str(source or "app"),
            "message": text[:MAX_MESSAGE_CHARS],
            "taskId": task_id,
            "kind": kind,
            "detail": (detail or None),
        }
        with self._lock:
            self._seq += 1
            entry["id"] = self._seq
            self._entries.append(entry)

        should_push = broadcast if broadcast is not None else LEVEL_ORDER[name] >= LEVEL_ORDER["error"]
        if should_push:
            self._broadcast(entry)
        return dict(entry)

    def append_record(self, record: logging.LogRecord) -> Dict[str, Any]:
        """logging.LogRecord -> 条目（带异常堆栈）。"""
        detail: Optional[str] = None
        if record.exc_info:
            try:
                detail = logging.Formatter().formatException(record.exc_info)
            except Exception:
                detail = None
        return self.append(
            record.levelname,
            record.getMessage(),
            source=record.name,
            timestamp=record.created,
            task_id=getattr(record, "taskId", None),
            kind=getattr(record, "kind", None),
            detail=(detail[:MAX_MESSAGE_CHARS] if detail else None),
        )

    def alert(self, level: Any, message: str, source: str = "app",
              detail: Optional[str] = None, task_id: Optional[str] = None,
              kind: Optional[str] = None) -> Dict[str, Any]:
        """业务显式告警：warning 及以上都会推 ALERT 帧。"""
        return self.append(level, message, source=source, detail=detail,
                           task_id=task_id, kind=kind, broadcast=True)

    def task_progress(self, task_id: str, kind: str, progress: float,
                      message: str) -> Optional[Dict[str, Any]]:
        """阶段性进度写入日志（只在一个阶段文案变化时调用，天然不刷屏）。"""
        text = str(message or "").strip()
        if not text:
            return None
        percent = int(round(max(0.0, min(1.0, float(progress))) * 100))
        return self.append(
            "info", "%s %d%% · %s" % (task_label(kind), percent, text),
            source="task", task_id=task_id, kind=kind,
        )

    # ------------------------------------------------------------ 查询
    def query(self, level: str = "", keyword: str = "", source: str = "",
              limit: int = 200, after: int = 0) -> Dict[str, Any]:
        """按最低级别 / 关键词 / 来源前缀过滤，返回按时间升序的最近 limit 条。"""
        min_rank = LEVEL_ORDER[normalize_level(level)] if str(level or "").strip() else 0
        text = str(keyword or "").strip().lower()
        prefix = str(source or "").strip().lower()
        offset = max(0, int(after or 0))
        size = max(1, min(int(limit or 200), 1000))

        with self._lock:
            snapshot = list(self._entries)
            seq = self._seq

        items: List[Dict[str, Any]] = []
        for entry in snapshot:
            if entry["id"] <= offset:
                continue
            if LEVEL_ORDER[entry["level"]] < min_rank:
                continue
            if text and text not in entry["message"].lower() \
                    and text not in entry["source"].lower():
                continue
            if prefix and not entry["source"].lower().startswith(prefix):
                continue
            items.append(dict(entry))

        total = len(items)
        return {
            "logs": items[-size:],
            "total": total,
            "seq": seq,
            "capacity": self._capacity,
            "file": self.file_path,
        }

    # ------------------------------------------------------------ 广播
    def _broadcast(self, entry: Dict[str, Any]) -> bool:
        """推送 ALERT；同一来源+文本在窗口期内只推一次。"""
        if LEVEL_ORDER[entry["level"]] < LEVEL_ORDER["warning"]:
            return False
        key = "%s|%s|%s" % (entry["source"], entry["level"], entry["message"])
        now = time.time()
        with self._lock:
            last = self._dedupe.get(key)
            if last is not None and now - last < ALERT_DEDUPE_SECONDS:
                return False
            self._dedupe[key] = now
            if len(self._dedupe) > 300:
                # 顺手清理过期键，避免长时间运行时无限增长
                stale = [k for k, stamp in self._dedupe.items()
                         if now - stamp > ALERT_DEDUPE_SECONDS * 10]
                for item in stale:
                    self._dedupe.pop(item, None)

        meta = {
            "id": entry["id"],
            "time": entry["time"],
            "level": entry["level"],
            "source": entry["source"],
            "message": entry["message"],
            "taskId": entry["taskId"],
            "kind": entry["kind"],
            "detail": entry["detail"],
        }
        try:
            hub.broadcast_threadsafe(MsgType.ALERT, meta)
        except Exception:
            # 日志链路自身绝不能抛异常（否则 emit 递归）
            return False
        return True


class LogRingHandler(logging.Handler):
    """把 logging 记录灌进 LogStore；不参与格式化，也不吞掉控制台/文件输出。"""

    def __init__(self, store: Optional[LogStore] = None) -> None:
        super().__init__(logging.DEBUG)
        self.store = store

    def emit(self, record: logging.LogRecord) -> None:
        # 前端上报等场景会显式写环，再用同一 logger 落盘，避免重复记录
        if getattr(record, "_btcam_skip_ring", False):
            return
        store = self.store
        if store is None:
            return
        try:
            store.append_record(record)
        except Exception:
            self.handleError(record)


# 全局单例：整个后端进程一份
log_store = LogStore()
