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
