"""2D 几何基础能力接口（模块 2）。

分两类，和项目既有约定一致：

- **同步查询**（包围盒 / 闭合校验 / 自相交）：结果是小 JSON，立刻返回；
  计算放到线程池，不阻塞事件循环。
- **异步任务**（角色偏移 / 轮廓求交 / 断线合并）：会产出新轮廓，
  立刻返回 taskId，结果走 TASK_RESULT 帧 + BGC1 二进制块。

前端只发参数、收结果渲染，不做任何偏移/布尔/拓扑计算。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from app.core.tasks import task_queue
from app.models import db
from app.models.geometry import Polyline
from app.models.schemas import (
    GeometryIntersectRequest,
    GeometryMergeRequest,
    GeometryOffsetRequest,
    GeometryQueryRequest,
    TaskAccepted,
)
from app.services import geometry_service
from app.services.geometry_ops import (
    geometry_job,
    get_ops,
    issue_to_payload,
    polylines_from_payload,
    polylines_to_payload,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/geometry", tags=["geometry"])

TASK_KIND = "geometry"
MAX_CONTOURS = 20000      # 单次请求的轮廓数上限，防止误传超大图纸撑爆内存


# ---------------------------------------------------------------- 工具
def _project_id(requested: Optional[int]) -> int:
    if requested:
        return requested
    with db.get_conn() as conn:
        return db.get_active_project_id(conn)


def _load_contours(project_id: int, ids: List[int], layer_ids: List[int],
                   inline: Optional[List[Dict[str, Any]]]) -> List[Polyline]:
    """取轮廓：优先用请求里内联的图形，否则按 id / 图层从工程库读。

    入参：
        project_id -- 工程 id（读库时用）
        ids        -- 轮廓主键，空表示不按 id 过滤
        layer_ids  -- 图层 id，空表示不按图层过滤
        inline     -- 前端直接传的轮廓（临时图形，未入库）
    出参：Polyline 列表；可能是空列表，由调用方决定报错还是放过
    """
    if inline:
        items = polylines_from_payload(inline)
        if items:
            return items

    with db.get_conn() as conn:
        doc = geometry_service.load_document(conn, project_id)

    result = doc.polylines
    if layer_ids:
        allowed = set(layer_ids)
        result = [p for p in result if p.layer_id in allowed]
    if ids:
        wanted = set(ids)
        result = [p for p in result if p.id in wanted]
    return result


def _guard_size(contours: List[Polyline]) -> None:
    if len(contours) > MAX_CONTOURS:
        raise HTTPException(400, "单次最多处理 %d 条轮廓（收到 %d 条）"
                            % (MAX_CONTOURS, len(contours)))


# ---------------------------------------------------------------- 信息
@router.get("/info")
async def geometry_info() -> Dict[str, Any]:
    """当前几何后端与资源快照。

    出参：
        backend    -- occt / clipper / fallback
        available  -- 引擎是否可用
        occt       -- 是否真的在用 OCCT
        counters   -- 业务调用计数（offset/intersect/bbox/...）
        resource   -- OCCT 侧计数（calls/released/gcRuns）
    """
    return get_ops().info()


# ---------------------------------------------------------------- 偏移（异步）
@router.post("/offset", response_model=TaskAccepted)
async def geometry_offset(req: GeometryOffsetRequest) -> TaskAccepted:
    """轮廓偏移（刀具补偿），异步任务。

    入参见 GeometryOffsetRequest：distance > 0，方向由 side 决定。
    出参：TaskAccepted{taskId, kind:"geometry"}；
          结果帧 TASK_RESULT，payload 为偏移后的轮廓（BGC1）。
    异常：400 -- 没有可偏移的轮廓。
    """
    project_id = _project_id(req.projectId)
    contours = _load_contours(project_id, req.ids, req.layerIds, req.contours)
    _guard_size(contours)
    if not contours:
        raise HTTPException(400, "选中集为空，无法偏移")

    params: Dict[str, Any] = {
        "op": "offset",
        "projectId": project_id,
        "contours": polylines_to_payload(contours),
        "distance": req.distance,
        "join": req.join,
        "side": req.side,
    }
    task_id = await task_queue.submit(TASK_KIND, geometry_job, params, project_id)
    logger.info("几何任务已提交 op=offset task=%s 轮廓=%d 距离=%.3f side=%s",
                task_id, len(contours), req.distance, req.side)
    return TaskAccepted(taskId=task_id, kind=TASK_KIND)


# ---------------------------------------------------------------- 求交（异步）
@router.post("/intersect", response_model=TaskAccepted)
async def geometry_intersect(req: GeometryIntersectRequest) -> TaskAccepted:
    """轮廓求交（布尔交集），异步任务。

    入参：ids/contours 为主体，clipIds/clip 为切割轮廓，两组都只取闭合轮廓。
    出参：TaskAccepted；结果帧 TASK_RESULT，payload 为交集轮廓。
    异常：400 -- 任一组为空。
    """
    project_id = _project_id(req.projectId)
    subject = _load_contours(project_id, req.ids, [], req.contours)
    clip = _load_contours(project_id, req.clipIds, [], req.clip)
    _guard_size(subject)
    _guard_size(clip)
    if not subject:
        raise HTTPException(400, "主体轮廓为空，无法求交")
    if not clip:
        raise HTTPException(400, "切割轮廓 clip 为空，无法求交")

    params: Dict[str, Any] = {
        "op": "intersect",
        "projectId": project_id,
        "contours": polylines_to_payload(subject),
        "clip": polylines_to_payload(clip),
    }
    task_id = await task_queue.submit(TASK_KIND, geometry_job, params, project_id)
    logger.info("几何任务已提交 op=intersect task=%s 主体=%d 切割=%d",
                task_id, len(subject), len(clip))
    return TaskAccepted(taskId=task_id, kind=TASK_KIND)


# ---------------------------------------------------------------- 断线合并（异步）
@router.post("/merge", response_model=TaskAccepted)
async def geometry_merge(req: GeometryMergeRequest) -> TaskAccepted:
    """断线合并：把端点相接的碎片串成连续轮廓，异步任务。

    入参：tol 为端点吸附容差（mm），默认 0.2。
    出参：TaskAccepted；结果帧 TASK_RESULT，payload 为串接后的轮廓。
    异常：400 -- 没有可合并的轮廓。
    """
    project_id = _project_id(req.projectId)
    contours = _load_contours(project_id, req.ids, req.layerIds, req.contours)
    _guard_size(contours)
    if not contours:
        raise HTTPException(400, "选中集为空，无法合并")

    params: Dict[str, Any] = {
        "op": "merge",
        "projectId": project_id,
        "contours": polylines_to_payload(contours),
        "tol": req.tol,
    }
    task_id = await task_queue.submit(TASK_KIND, geometry_job, params, project_id)
    logger.info("几何任务已提交 op=merge task=%s 轮廓=%d 容差=%.3f",
                task_id, len(contours), req.tol)
    return TaskAccepted(taskId=task_id, kind=TASK_KIND)


# ---------------------------------------------------------------- 包围盒（同步）
@router.post("/bbox")
async def geometry_bbox(req: GeometryQueryRequest) -> Dict[str, Any]:
    """计算包围盒，同步返回。

    出参：{xmin, ymin, xmax, ymax, width, height, centerX, centerY,
            contourCount, pointCount}
    异常：400 -- 没有可计算的轮廓。
    """
    project_id = _project_id(req.projectId)
    contours = _load_contours(project_id, req.ids, req.layerIds, req.contours)
    _guard_size(contours)
    if not contours:
        raise HTTPException(400, "选中集为空，无法计算包围盒")

    ops = get_ops()
    try:
        box = await run_in_threadpool(ops.bbox, contours)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return box.as_dict()


# ---------------------------------------------------------------- 闭合校验（同步）
@router.post("/closure")
async def geometry_closure(req: GeometryQueryRequest) -> Dict[str, Any]:
    """闭合校验，同步返回每条轮廓的首尾缺口。

    出参：{count, openCount, items:[{polylineId, closed, ok, gap, isHole,
            pointCount, area, perimeter}]}
    异常：400 -- 没有可校验的轮廓。
    """
    project_id = _project_id(req.projectId)
    contours = _load_contours(project_id, req.ids, req.layerIds, req.contours)
    _guard_size(contours)
    if not contours:
        raise HTTPException(400, "选中集为空，无法做闭合校验")

    ops = get_ops()
    infos = await run_in_threadpool(ops.closure_report, contours, req.tol)
    items = [info.as_dict() for info in infos]
    return {
        "count": len(items),
        "openCount": sum(1 for item in items if not item["ok"]),
        "tol": req.tol,
        "items": items,
    }


# ---------------------------------------------------------------- 自相交检查（同步）
@router.post("/self-intersections")
async def geometry_self_intersections(req: GeometryQueryRequest) -> Dict[str, Any]:
    """自相交检查，同步返回告警清单。

    出参：{count, items:[{kind, message, polylineId, severity, point}]}
          返回结构与 GEOMETRY_ISSUES 帧的 meta.items 一致，前端可复用渲染。
    异常：400 -- 没有可检查的轮廓。
    """
    project_id = _project_id(req.projectId)
    contours = _load_contours(project_id, req.ids, req.layerIds, req.contours)
    _guard_size(contours)
    if not contours:
        raise HTTPException(400, "选中集为空，无法做自相交检查")

    ops = get_ops()
    issues = await run_in_threadpool(ops.self_intersections, contours)
    items = [issue_to_payload(issue) for issue in issues]
    return {
        "count": len(items),
        "contourCount": len(contours),
        "items": items,
    }
