"""工程 / 健康检查 / 任务查询等轻量接口。"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from app.api import ws as ws_module
from app.config import settings
from app.core.hub import hub
from app.core.tasks import task_queue
from app.geometry.base import get_engine
from app.models import db
from app.models.schemas import HealthResponse
from app.services import geometry_service, tool_service
from app.services.nesting_service import get_nester

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["project"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """前端启动时先探活，确认后端与计算引擎就绪。"""
    geometry = get_engine(settings.geometry_engine)
    nester = get_nester(settings.nesting_engine)
    return HealthResponse(
        status="ok",
        version="0.1.0",
        geometryEngine=getattr(geometry, "name", "unknown"),
        nestingEngine=getattr(nester, "name", "unknown"),
        clients=hub.client_count,
        workers=settings.worker_processes,
        dataDir=str(settings.data_dir),
    )


@router.get("/project")
async def current_project() -> Dict[str, Any]:
    """当前工程概要：面板显示用的轻量信息。"""
    with db.get_conn() as conn:
        project_id = db.get_active_project_id(conn)
        row = conn.execute("SELECT * FROM project WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "工程不存在")
        materials = [dict(r) for r in conn.execute(
            "SELECT * FROM material WHERE project_id = ?", (project_id,))]
        tools = tool_service.list_tools(conn, project_id)
    return {"project": dict(row), "materials": materials, "tools": tools}


@router.post("/demo")
async def load_demo() -> Dict[str, Any]:
    """载入内置示例图形（覆盖当前工程轮廓），用于联调渲染与刀路。"""
    doc = geometry_service.build_demo_document()
    with db.get_conn() as conn:
        project_id = db.get_active_project_id(conn)
    count = geometry_service.persist_document(project_id, doc)
    await ws_module.push_document()
    logger.info("已载入示例图形 %d 条轮廓", count)
    return {"polylines": count, "projectId": project_id, "bbox": list(doc.bbox())}


@router.get("/tasks")
async def list_tasks() -> Dict[str, Any]:
    """任务队列快照（前端任务面板用）。"""
    items = [
        {
            "taskId": t.id, "kind": t.kind, "status": t.status,
            "progress": t.progress, "message": t.message,
            "createdAt": t.created_at, "finishedAt": t.finished_at,
        }
        for t in task_queue.all_tasks().values()
    ]
    items.sort(key=lambda x: x["createdAt"], reverse=True)
    return {"tasks": items[:50]}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str) -> Dict[str, Any]:
    """取消排队/运行中的任务（模块 4 起用于"取消自动排料"）。

    取消是协作式的：这里只置标志位，计算线程在下一次 `report()`
    （内部调 `check_cancelled`）时抛 TaskCancelled 并结束；
    DeepNest 子进程会在轮询里感知标志并 kill，不会等满 180s 超时。
    """
    info = task_queue.get(task_id)
    if info is None:
        raise HTTPException(404, "任务不存在")
    if info.status in ("done", "failed", "cancelled"):
        raise HTTPException(409, "任务已结束（%s），无法取消" % info.status)
    if not task_queue.cancel(task_id):
        raise HTTPException(409, "任务已结束，无法取消")
    logger.info("已请求取消任务 id=%s kind=%s", task_id, info.kind)
    return {"ok": True, "taskId": task_id, "kind": info.kind, "status": info.status}
