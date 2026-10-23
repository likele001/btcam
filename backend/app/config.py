"""全局运行时配置。

所有可调参数集中在此，避免散落在业务代码里。
优先读环境变量，方便打包后的 exe 侧车（sidecar）被 Tauri 指定端口启动。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _default_data_dir() -> Path:
    """Windows 下默认落到 %LOCALAPPDATA%\\BtCAM，避免写 Program Files。"""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return Path(base) / "BtCAM"
    return Path.home() / ".btcam"


@dataclass
class Settings:
    # ---- 网络 ----
    host: str = os.environ.get("BTCAM_HOST", "127.0.0.1")
    port: int = _env_int("BTCAM_PORT", 8975)
    # 允许前端来源（Tauri 生产环境为 tauri://localhost，开发为 http://localhost:5173）
    cors_origins: list = field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "tauri://localhost",
            "http://tauri.localhost",
            "https://tauri.localhost",
        ]
    )

    # ---- 存储 ----
    data_dir: Path = field(default_factory=_default_data_dir)
    db_name: str = "btcam.db"

    # ---- 计算 ----
    # 计算池并发度：排料/刀路是 CPU 密集型，默认取物理核数的一半
    worker_processes: int = _env_int("BTCAM_WORKERS", max(1, (os.cpu_count() or 4) // 2))
    # 默认线程池：OCCT / pyclipper 的 C++ 计算会释放 GIL，线程池足以跑满 CPU，
    # 且进度回调、取消、调试都更简单；需要硬隔离（防 C++ 崩溃拖垮服务）时
    # 设 BTCAM_PROCESS_POOL=1 切到 spawn 进程池（任务参数必须可 pickle）。
    use_process_pool: bool = _env_bool("BTCAM_PROCESS_POOL", False)

    # ---- 几何引擎 ----
    # occt = OpenCASCADE（推荐）; clipper = pyclipper 兜底
    geometry_engine: str = os.environ.get("BTCAM_GEOM_ENGINE", "auto")
    geom_tolerance: float = 0.01        # mm，轮廓离散/合并容差
    arc_tolerance: float = 0.02         # mm，圆弧离散弦高容差

    # ---- 排料引擎 ----
    # deepnest = DeepNest 适配器; shelf = 内置简单行排（无外部依赖兜底）
    nesting_engine: str = os.environ.get("BTCAM_NEST_ENGINE", "auto")

    # ---- 日志 ----
    log_level: str = os.environ.get("BTCAM_LOG_LEVEL", "INFO")
    # 面板环形缓冲容量；调大可回溯更久，但只在内存中保留本次运行
    log_buffer: int = _env_int("BTCAM_LOG_BUFFER", 1000)

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_name

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
