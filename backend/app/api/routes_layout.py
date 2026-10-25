"""画布手动排料接口（模块 3）。

与其他计算接口的区别：
    这里的操作都是"交互级"的同步写操作（移动/旋转/锁定/改板材），
    耗时应控制在几十毫秒内，所以不走 task_queue，直接在线程池里算完返回；
    结果通过 WS 的 LAYOUT_STATE 帧广播给所有窗口，前端按帧重绘。

    拖拽的实时位移由前端做纯像素变换（不涉及几何判定），
    松手后才发到这里做越界/重叠校验并落库 —— 校验失败前端回弹。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from app.core import protocol
from app.core.hub import hub
from app.core.protocol import MsgType
from app.core.tasks import task_queue
from app.models import db
from app.models.schemas import (
    LayoutDuplicateRequest,
    LayoutFixedRotRequest,
    LayoutLockRequest,
    LayoutNestRequest,
    LayoutOutlineRequest,
    LayoutPlaceRequest,
    LayoutRotateRequest,
    LayoutShapeRequest,
    LayoutSheetRequest,
    TaskAccepted,
)
from app.services.layout_service import LayoutResult, LayoutState, layout_service
from app.services.nest_service import auto_nest_job

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/layout", tags=["layout"])


# ---------------------------------------------------------------- 工具
def _project_id(requested: Optional[int]) -> int:
    if requested:
        return requested
    with db.get_conn() as conn:
        return db.get_active_project_id(conn)


async def push_layout(ws: Any = None, project_id: Optional[int] = None,
                      seq: int = 0) -> Dict[str, Any]:
    """组装并下发排料状态；ws=None 时广播给所有客户端。"""
    pid = _project_id(project_id)

    def _load() -> LayoutState:
        with db.get_conn() as conn:
            return layout_service.load(conn, pid)

    state = await run_in_threadpool(_load)
    polylines, id_map = layout_service.build_polylines(state)
    meta = layout_service.build_meta(state, id_map)
    payload = protocol.encode_polylines(polylines)
    if ws is None:
        await hub.broadcast(MsgType.LAYOUT_STATE, meta, payload, seq)
    else:
        await hub.send_frame(ws, MsgType.LAYOUT_STATE, meta, payload, seq)
    return meta


async def _publish(state: LayoutState) -> Dict[str, Any]:
    """把最新排料状态编码成 BGC1 帧广播出去，同时返回 meta 给调用方。"""
    polylines, id_map = layout_service.build_polylines(state)
    meta = layout_service.build_meta(state, id_map)
    await hub.broadcast(MsgType.LAYOUT_STATE, meta, protocol.encode_polylines(polylines))
    return meta


def _respond(result: LayoutResult, published: Dict[str, Any]) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "ok": result.ok,
        "reason": result.reason,
        "stats": published.get("stats", {}),
        "sheet": published.get("sheet", {}),
    }
    # 写操作自带的额外信息（如复制件数/停靠件数）原样回给发起方
    body.update(result.extra)
    if not result.ok:
        # 失败时把"当前真实状态"一并带回，前端据此回弹到正确位置
        body["placements"] = [p.as_dict() for p in result.state.placements.values()]
    return body


# ---------------------------------------------------------------- 读
@router.get("")
async def layout_get(projectId: Optional[int] = None) -> Dict[str, Any]:
    """读当前排料状态（不广播，测试与面板初始化用）。"""
    pid = _project_id(projectId)

    def _load() -> LayoutState:
        with db.get_conn() as conn:
            return layout_service.load(conn, pid)

    state = await run_in_threadpool(_load)
    polylines, id_map = layout_service.build_polylines(state)
    meta = layout_service.build_meta(state, id_map)
    meta["polylineCount"] = len(polylines)
    return meta


# ---------------------------------------------------------------- 写
@router.post("/open")
async def layout_open(projectId: Optional[int] = None) -> Dict[str, Any]:
    """进入排料模式：首次调用会做一次初始行排。"""
    pid = _project_id(projectId)

    def _open() -> LayoutState:
        with db.get_conn() as conn:
            return layout_service.open(conn, pid)

    state = await run_in_threadpool(_open)
    if not state.parts:
        raise HTTPException(400, "当前工程没有可排料的零件，请先导入 DXF")
    meta = await _publish(state)
    return {"ok": True, "reason": "", "stats": meta["stats"], "sheet": meta["sheet"]}


@router.post("/sheet")
async def layout_sheet(req: LayoutSheetRequest) -> Dict[str, Any]:
    """保存板材参数（宽/高/四边余量/最小间距）。"""
    pid = _project_id(req.projectId)

    def _save() -> LayoutResult:
        with db.get_conn() as conn:
            return layout_service.set_sheet(conn, pid, req.width, req.height,
                                            req.margin, req.gap)

    result = await run_in_threadpool(_save)
    meta = await _publish(result.state)
    return _respond(result, meta)


@router.post("/place")
async def layout_place(req: LayoutPlaceRequest) -> Dict[str, Any]:
    """移动零件到 (tx,ty)；越界或与其他零件间距不足时拒绝。"""
    pid = _project_id(req.projectId)

    def _move() -> LayoutResult:
        with db.get_conn() as conn:
            return layout_service.move(conn, pid, req.partId, req.tx, req.ty, req.rot)

    result = await run_in_threadpool(_move)
    meta = await _publish(result.state)
    return _respond(result, meta)


@router.post("/duplicate")
async def layout_duplicate(req: LayoutDuplicateRequest) -> Dict[str, Any]:
    """复制零件 count 份（排料板手动增件）：就近找空位，放不下的停靠到板外上方。"""
    pid = _project_id(req.projectId)

    def _copy() -> LayoutResult:
        with db.get_conn() as conn:
            return layout_service.duplicate(conn, pid, req.partId, req.count)

    result = await run_in_threadpool(_copy)
    meta = await _publish(result.state)
    return _respond(result, meta)


@router.post("/add-shape")
async def layout_add_shape(req: LayoutShapeRequest) -> Dict[str, Any]:
    """新建零件（排料板作图）：矩形 / 圆 / 手绘多边形，直接当可排件。"""
    pid = _project_id(req.projectId)

    def _create() -> LayoutResult:
        with db.get_conn() as conn:
            return layout_service.add_shape(
                conn, pid, kind=req.kind, name=req.name, width=req.width,
                height=req.height, diameter=req.diameter, points=req.points,
                at=req.at)

    result = await run_in_threadpool(_create)
    meta = await _publish(result.state)
    return _respond(result, meta)


@router.post("/update-outline")
async def layout_update_outline(req: LayoutOutlineRequest) -> Dict[str, Any]:
    """改造型（顶点编辑）：用拖过顶点的轮廓替换零件几何，位置随之重算。"""
    pid = _project_id(req.projectId)

    def _update() -> LayoutResult:
        with db.get_conn() as conn:
            return layout_service.update_outline(conn, pid, req.partId, req.contours)

    result = await run_in_threadpool(_update)
    meta = await _publish(result.state)
    return _respond(result, meta)


@router.post("/rotate")
async def layout_rotate(req: LayoutRotateRequest) -> Dict[str, Any]:
    """把零件旋转到指定角度（绕包围盒中心，位置自动补偿）。"""
    pid = _project_id(req.projectId)

    def _rotate() -> LayoutResult:
        with db.get_conn() as conn:
            return layout_service.rotate(conn, pid, req.partId, req.rot)

    result = await run_in_threadpool(_rotate)
    meta = await _publish(result.state)
    return _respond(result, meta)


@router.post("/lock")
async def layout_lock(req: LayoutLockRequest) -> Dict[str, Any]:
    """锁定/解锁零件（锁定后不可移动、不可旋转）。"""
    pid = _project_id(req.projectId)

    def _lock() -> LayoutResult:
        with db.get_conn() as conn:
            return layout_service.set_locked(conn, pid, req.partId, req.locked)

    result = await run_in_threadpool(_lock)
    meta = await _publish(result.state)
    return _respond(result, meta)


@router.post("/fixed-rot")
async def layout_fixed_rot(req: LayoutFixedRotRequest) -> Dict[str, Any]:
    """锁定/解锁旋转角度（木纹方向）。"""
    pid = _project_id(req.projectId)

    def _fix() -> LayoutResult:
        with db.get_conn() as conn:
            return layout_service.set_fixed_rot(conn, pid, req.partId, req.fixedRot)

    result = await run_in_threadpool(_fix)
    meta = await _publish(result.state)
    return _respond(result, meta)


@router.post("/nest", response_model=TaskAccepted)
async def layout_nest(req: LayoutNestRequest) -> TaskAccepted:
    """一键自动排料（模块 4）：异步任务，完成后广播 LAYOUT_STATE。

    入参可选板材参数与 partIds：不传则重排当前排料板上的全部未锁定零件；
    出参（TASK_RESULT meta）带 stats / sheet / placements，任务层随后补发 LAYOUT_STATE。
    """
    pid = _project_id(req.projectId)

    def _validate() -> None:
        with db.get_conn() as conn:
            state = layout_service.load(conn, pid)
        if not state.parts:
            raise HTTPException(400, "当前工程没有可排料的零件，请先导入 DXF")
        if req.partIds:
            known = {part.id for part in state.parts}
            missing = [part_id for part_id in req.partIds if part_id not in known]
            if missing:
                raise HTTPException(400, "零件不存在：%s" % missing[:5])

    await run_in_threadpool(_validate)

    params: Dict[str, Any] = {
        "projectId": pid,
        "engine": req.engine,
        "rotation": req.rotation,
    }
    # 可选入参：只透传显式给出的字段，任务层才能区分“沿用当前板材”与“改成 0”
    for key in ("width", "height", "margin", "gap"):
        value = getattr(req, key)
        if value is not None:
            params[key] = float(value)
    if req.partIds:
        params["partIds"] = list(req.partIds)

    task_id = await task_queue.submit("layout_nest", auto_nest_job, params, pid)
    return TaskAccepted(taskId=task_id, kind="layout_nest")


@router.post("/reset")
async def layout_reset(projectId: Optional[int] = None) -> Dict[str, Any]:
    """清空摆放并重新初始行排。"""
    pid = _project_id(projectId)

    def _reset() -> LayoutResult:
        with db.get_conn() as conn:
            return layout_service.reset(conn, pid)

    result = await run_in_threadpool(_reset)
    meta = await _publish(result.state)
    return _respond(result, meta)
