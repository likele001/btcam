"""BtCAM 后端入口。

启动顺序（lifespan）：
    建目录 -> 初始化日志 -> 建库 -> 绑定事件循环 -> 启动任务队列 -> 就绪

生产环境由 Tauri 作为 sidecar 拉起（`cam-backend.exe`），端口通过环境变量注入。
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    routes_cam,
    routes_geometry,
    routes_import,
    routes_layout,
    routes_logs,
    routes_mes,
    routes_post,
    routes_project,
    routes_scrap,
    routes_tools,
    routes_workorders,
    ws,
)
from app.config import settings
from app.core.hub import hub
from app.core.logging_setup import setup_logging
from app.core.procguard import watch_parent
from app.core.tasks import task_queue
from app.geometry.base import get_engine
from app.models import db
from app.post import registry as post_registry
from app.version import EDITION, __version__

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    setup_logging(settings.log_level, settings.data_dir / "logs", settings.log_buffer)
    logger.info("%s %s（%s）启动", "BtCAM", __version__, EDITION)
    logger.info("数据目录: %s", settings.data_dir)

    db.init_db()
    # 后处理模板落盘：一份 JSON 一台机床，旧版本按 version 升级
    post_registry.ensure_builtin()

    loop = asyncio.get_running_loop()
    hub.bind_loop(loop)
    await task_queue.start()

    engine = get_engine(settings.geometry_engine)
    logger.info("几何引擎: %s", getattr(engine, "name", "unknown"))
    logger.info("服务地址: http://%s:%d  (WS: /ws)", settings.host, settings.port)

    yield

    await task_queue.stop()
    logger.info("后端已退出")


def create_app() -> FastAPI:
    app = FastAPI(
        title="BtCAM Backend",
        version=__version__,
        description="2D 切割 CAM 计算服务：OCCT 几何 + 排料 + 刀路",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(routes_project.router)
    app.include_router(routes_cam.router)
    app.include_router(routes_import.router)
    app.include_router(routes_geometry.router)
    app.include_router(routes_layout.router)
    app.include_router(routes_tools.router)
    app.include_router(routes_post.router)
    app.include_router(routes_workorders.router)
    app.include_router(routes_scrap.router)
    # 模块 12：MES 对接是可选件，没配置时所有 /api/mes/* 调用只会回“未启用”
    app.include_router(routes_mes.router)
    app.include_router(routes_logs.router)
    app.include_router(ws.router)

    @app.get("/")
    async def root():
        return {"name": "BtCAM", "docs": "/docs", "ws": "/ws"}

    return app


app = create_app()

# 父进程守护放在模块级：`python -m app.main`、`uvicorn app.main:app`
# 以及 `--reload` 拉起的子进程都会 import 本模块，三种入口都能生效。
# 未设置 BTCAM_PARENT_PID 时它什么都不做（返回 False）。
_watching_parent = watch_parent()


def main() -> None:
    """直接 `python -m app.main` 或打包为 exe 后调用。

    两种启动方都会设 BTCAM_PARENT_PID：
      - Release: Tauri 主程序（内嵌 sidecar 自解压后拉起）
      - Dev    : `cargo tauri dev` 由 lib.rs 拉起本机 uvicorn
    父进程一旦消失，procguard 立即 os._exit，保证不留孤儿进程。
    """
    import uvicorn

    if _watching_parent:
        logger.warning("父进程守护已启动（PID=%s）", os.environ.get("BTCAM_PARENT_PID"))

    # 打包/侧车场景固定单进程：reload 需要再起子进程，与"退出即回收"目标冲突
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
