"""日志初始化：控制台 + 滚动文件 + 内存环形缓冲，强制 UTF-8（Windows 控制台默认 GBK 会乱码）。

三个 handler 各管一件事，互不影响：
    StreamHandler       开发时看控制台；
    RotatingFileHandler 长期留存 logs/backend.log（给人排查）；
    LogRingHandler      结构化条目进 LogStore，供 /api/logs 与 ALERT 帧使用。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from app.core.log_store import LogRingHandler, log_store


class _AsyncDisconnectNoise(logging.Filter):
    """丢弃 asyncio 层「客户端已断连」这类无意义告警。

    Windows 上客户端刷新/关窗口用 RST 断开时，ProactorEventLoop 回收连接会抛
     ConnectionResetError / ConnectionAbortedError（WinError 10053/10054），
    asyncio 默认处理器把它记成 ERROR 并进日志面板（error 自动弹窗）。
    连接本就断了，无需告警，故丢弃；其它 asyncio 错误照常记录。

    main.lifespan 已在 loop 层做了同样的拦截，这里是兜底：
    uvicorn 重装 exception handler 或第三方代码直接 logger.error 时仍能生效。
    """

    _DISCONNECT_ERRORS = (ConnectionResetError, ConnectionAbortedError)

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "asyncio":
            return True
        # 断连噪音一定带 traceback，按异常类型判定最准；无异常信息的记录不去猜
        exc_type = record.exc_info[0] if record.exc_info else None
        if exc_type is None:
            return True
        return not issubclass(exc_type, self._DISCONNECT_ERRORS)


def setup_logging(level: str = "INFO", log_dir: Path | None = None,
                  capacity: int = 1000) -> None:
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
            sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s", datefmt="%H:%M:%S"
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    root.handlers.clear()

    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    if log_dir is not None:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "backend.log"
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=4 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
        # 面板底部展示路径，用户可自己打开文件看历史
        log_store.file_path = str(log_file)

    log_store.set_capacity(capacity)
    root.addHandler(LogRingHandler(log_store))

    # 降噪：uvicorn 的访问日志太啰嗦
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    # 降噪：WS 客户端断连时 Proactor 抛的 ConnectionResetError（见 _AsyncDisconnectNoise）
    logging.getLogger("asyncio").addFilter(_AsyncDisconnectNoise())
