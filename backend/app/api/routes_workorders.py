"""工单存档接口（模块 8）。

    GET    /api/workorders                列表（关键词 + 日期区间）
    POST   /api/workorders                保存当前工程为工单（快照）
    GET    /api/workorders/{id}           详情（参数、刀具引用、文件列表）
    PUT    /api/workorders/{id}           改名/备注；recapture 时重新拍快照
    DELETE /api/workorders/{id}           删除（连同文件登记）
    POST   /api/workorders/{id}/duplicate 另存为
    POST   /api/workorders/{id}/open      打开：快照写回当前工程并广播
    GET    /api/workorders/{id}/files     加工文件路径列表

导出成功后由 `POST /api/export` 的 `workOrderId` 自动登记文件，
这里不重复提供写文件的接口。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from app.api import ws as ws_module
from app.models import db
from app.models.schemas import (
    WorkOrderDuplicateRequest,
    WorkOrderSaveRequest,
)
from app.services import work_order_service
from app.services.work_order_service import WorkOrderError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workorders", tags=["workorder"])


# ---------------------------------------------------------------- 工具
def _project_id(requested: Optional[int]) -> int:
    if requested:
        return int(requested)
    with db.get_conn() as conn:
        return db.get_active_project_id(conn)


def _http_error(exc: WorkOrderError) -> HTTPException:
    message = str(exc)
    return HTTPException(404 if "不存在" in message else 400, message)


def _day_ts(value: str, end: bool = False) -> float:
    """YYYY-MM-DD -> 当天 00:00（end=True 时取次日 00:00，右开区间）。"""
    try:
        stamp = datetime.strptime(value.strip(), "%Y-%m-%d").timestamp()
    except ValueError as exc:
        raise HTTPException(400, "日期格式应为 YYYY-MM-DD") from exc
    return stamp + 86400.0 if end else stamp


# ---------------------------------------------------------------- 读
@router.get("")
async def list_orders(keyword: str = "", dateFrom: str = "", dateTo: str = "",
                      limit: int = 100, projectId: Optional[int] = None) -> Dict[str, Any]:
    """按名称/编号/备注模糊检索，日期按创建时间过滤。"""
    pid = _project_id(projectId)
    start = _day_ts(dateFrom) if dateFrom else None
    end = _day_ts(dateTo, end=True) if dateTo else None
    size = max(1, min(int(limit), 500))

    def _load() -> Dict[str, Any]:
        with db.get_conn() as conn:
            return work_order_service.list_orders(conn, pid, keyword, start, end, size)

    return await run_in_threadpool(_load)


@router.get("/{order_id}")
async def get_order(order_id: int, projectId: Optional[int] = None) -> Dict[str, Any]:
    pid = _project_id(projectId)

    def _load() -> Dict[str, Any]:
        with db.get_conn() as conn:
            try:
                return work_order_service.get_order(conn, pid, order_id)
            except WorkOrderError as exc:
                raise _http_error(exc) from exc

    return await run_in_threadpool(_load)


@router.get("/{order_id}/files")
async def list_files(order_id: int, projectId: Optional[int] = None) -> Dict[str, Any]:
    pid = _project_id(projectId)

    def _load() -> Dict[str, Any]:
        with db.get_conn() as conn:
            try:
                return work_order_service.list_files(conn, pid, order_id)
            except WorkOrderError as exc:
                raise _http_error(exc) from exc

    return await run_in_threadpool(_load)


# ---------------------------------------------------------------- 写
@router.post("")
async def create_order(req: WorkOrderSaveRequest) -> Dict[str, Any]:
    """保存当前工程快照；名称必填，编号/备注可选。"""
    pid = _project_id(req.projectId)
    payload = req.model_dump()

    def _save() -> Dict[str, Any]:
        with db.get_conn() as conn:
            try:
                return work_order_service.create_order(conn, pid, payload)
            except WorkOrderError as exc:
                raise _http_error(exc) from exc

    return await run_in_threadpool(_save)


@router.put("/{order_id}")
async def update_order(order_id: int, req: WorkOrderSaveRequest) -> Dict[str, Any]:
    pid = _project_id(req.projectId)
    payload = req.model_dump()

    def _save() -> Dict[str, Any]:
        with db.get_conn() as conn:
            try:
                return work_order_service.update_order(conn, pid, order_id, payload)
            except WorkOrderError as exc:
                raise _http_error(exc) from exc

    return await run_in_threadpool(_save)


@router.post("/{order_id}/duplicate")
async def duplicate_order(order_id: int, req: WorkOrderDuplicateRequest,
                          projectId: Optional[int] = None) -> Dict[str, Any]:
    pid = _project_id(projectId)

    def _copy() -> Dict[str, Any]:
        with db.get_conn() as conn:
            try:
                return work_order_service.duplicate_order(conn, pid, order_id, req.name)
            except WorkOrderError as exc:
                raise _http_error(exc) from exc

    return await run_in_threadpool(_copy)


@router.delete("/{order_id}")
async def delete_order(order_id: int, projectId: Optional[int] = None) -> Dict[str, Any]:
    pid = _project_id(projectId)

    def _remove() -> Dict[str, Any]:
        with db.get_conn() as conn:
            try:
                work_order_service.remove_order(conn, pid, order_id)
            except WorkOrderError as exc:
                raise _http_error(exc) from exc
            return {"deleted": True, "id": order_id}

    return await run_in_threadpool(_remove)


@router.post("/{order_id}/open")
async def open_order(order_id: int, projectId: Optional[int] = None) -> Dict[str, Any]:
    """打开历史工单：整份写回当前工程，并广播全量图形刷新画布。"""
    pid = _project_id(projectId)

    def _open() -> Dict[str, Any]:
        with db.get_conn() as conn:
            try:
                return work_order_service.open_order(conn, pid, order_id)
            except WorkOrderError as exc:
                raise _http_error(exc) from exc

    detail = await run_in_threadpool(_open)
    await ws_module.push_document()
    logger.info("工单 #%d 已打开并广播图形", order_id)
    return detail
