"""CAM 业务接口：偏置 / 排料 / 刀路 / 导出。

统一的处理套路：
    REST 只做参数校验 + 组装参数 + 投递任务，立刻返回 taskId；
    真正的计算在 TaskQueue 的工作线程里跑，进度与结果通过 WS 推送。
只有导出接口是同步的（文件写入很快，且需要立刻拿到路径）。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.core.tasks import task_queue
from app.models import db
from app.models.geometry import Polyline
from app.models.schemas import (
    ExportRequest,
    NestRequest,
    OffsetRequest,
    TaskAccepted,
    ToolpathRequest,
)
from app.post import registry as post_registry
from app.post import service as post_service
from app.post.renderer import RenderContext
from app.services import geometry_service, state, tool_service, work_order_service
from app.services.export_service import FMT_TO_PROCESSOR
from app.services.geometry_ops import geometry_job, polylines_to_payload
from app.services.layout_service import layout_service as layout_engine
from app.services.nesting_service import nest_job
from app.services.toolpath_service import build_toolpath, toolpath_job

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["cam"])


# ---------------------------------------------------------------- 工具函数
def _project_id(requested: Optional[int]) -> int:
    if requested:
        return requested
    with db.get_conn() as conn:
        return db.get_active_project_id(conn)


def _load_selected(project_id: int, ids: List[int],
                   layer_ids: List[int]) -> List[Polyline]:
    """按 id / 图层筛选轮廓；两者都为空时返回全部。"""
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


def _to_payload(polylines: List[Polyline]) -> List[Dict[str, Any]]:
    """转成任务参数格式；numpy 数组可直接 pickle，无需 tolist 拷贝。"""
    return [
        {"id": p.id, "layerId": p.layer_id, "closed": p.closed,
         "isHole": p.is_hole, "partId": p.part_id, "pts": p.pts}
        for p in polylines
    ]


def _load_layout_selected(project_id: int, sheet_index: int,
                          ids: List[int]) -> tuple:
    """读排料板上的轮廓（模块 5 整板刀路）。

    返回 (轮廓列表, 板材参数)。ids 为排料轮廓 id（200000+），
    为空表示当前板材上的全部零件。
    """
    with db.get_conn() as conn:
        layout_state = layout_engine.load(conn, project_id)
    if not layout_state.placements:
        raise HTTPException(400, "当前工程还没有排料结果，请先进入排料模式并摆放零件")

    sheet_parts = {pid for pid, placement in layout_state.placements.items()
                   if placement.sheet == sheet_index}
    if not sheet_parts:
        raise HTTPException(400, "板材 #%d 上没有已摆放的零件" % sheet_index)

    polylines, _ = layout_engine.build_polylines(layout_state)
    selected = [p for p in polylines if p.part_id in sheet_parts]
    if ids:
        wanted = {int(item) for item in ids}
        existing = {p.id for p in selected}
        missing = sorted(wanted - existing)
        if missing:
            raise HTTPException(400, "排料轮廓不存在：%s" % missing[:5])
        selected = [p for p in selected if p.id in wanted]

    sheet = {key: float(value) for key, value in layout_state.sheet.items()}
    return selected, sheet


# ---------------------------------------------------------------- 偏置
@router.post("/offset", response_model=TaskAccepted)
async def offset(req: OffsetRequest) -> TaskAccepted:
    """轮廓偏置（兼容旧路径）。

    实际计算已统一到模块 2 的几何服务：这里只把旧参数（可负数表示内缩）
    翻译成 side=outside/inside，再投递同一个 geometry_job。
    """
    project_id = _project_id(req.projectId)
    contours = _load_selected(project_id, req.ids, [])
    if not contours:
        raise HTTPException(400, "选中集为空，无法偏置")

    params = {
        "op": "offset",
        "projectId": project_id,
        "contours": polylines_to_payload(contours),
        "distance": abs(float(req.distance)),
        "join": req.join,
        "side": "inside" if req.distance < 0 else "outside",
    }
    task_id = await task_queue.submit("geometry", geometry_job, params, project_id)
    return TaskAccepted(taskId=task_id, kind="geometry")


# ---------------------------------------------------------------- 排料
@router.post("/nest", response_model=TaskAccepted)
async def nest(req: NestRequest) -> TaskAccepted:
    project_id = _project_id(req.projectId)
    contours = [p for p in _load_selected(project_id, req.partIds, req.layerIds) if p.closed]
    if not contours:
        raise HTTPException(400, "没有闭合轮廓，无法排料")

    params: Dict[str, Any] = {
        "projectId": project_id,
        "sheetWidth": req.sheetWidth,
        "sheetHeight": req.sheetHeight,
        "gap": req.gap,
        "rotation": req.rotation,
        "engine": req.engine,
        "parts": _to_payload(contours),
    }
    task_id = await task_queue.submit("nest", nest_job, params, project_id)
    return TaskAccepted(taskId=task_id, kind="nest")


# ---------------------------------------------------------------- 刀路
def _toolpath_params(req: ToolpathRequest) -> Dict[str, Any]:
    project_id = _project_id(req.projectId)
    # 刀具/模板在提交时解析一次，把“刀具事实”固化进任务参数：
    # 任务执行期间即使用户改刀，已排队的刀路仍然按提交时的参数算。
    try:
        with db.get_conn() as conn:
            resolution = tool_service.resolve(
                conn, project_id, req.toolId, req.templateId)
    except tool_service.ToolError as exc:
        raise HTTPException(400, str(exc)) from exc

    sheet = None
    if req.contours:
        contours = req.contours
    elif req.source == "layout":
        # 整板合并：所有零件按当前摆放变换后，统一生成一套连续刀路
        selected, sheet = _load_layout_selected(project_id, req.sheetIndex, req.ids)
        contours = _to_payload(selected)
    else:
        contours = _to_payload(_load_selected(project_id, req.ids, req.layerIds))
    if not contours:
        raise HTTPException(400, "选中集为空，无法生成刀路")

    start_points = [
        point.model_dump() if hasattr(point, "model_dump") else point.dict()
        for point in req.startPoints
    ]
    return tool_service.apply_resolution({
        "projectId": project_id,
        "source": req.source,
        "sheetIndex": req.sheetIndex,
        "sheet": sheet,
        "contours": contours,
        "cutType": req.cutType,
        "direction": req.direction,
        "optimize": req.optimize,
        "startPoints": start_points,
        "microJoint": req.microJoint,
        "microJointWidth": req.microJointWidth,
        "microJointCount": req.microJointCount,
        "toolDiameter": req.toolDiameter,
        "side": req.side,
        "depth": req.depth,
        "passDepth": req.passDepth,
        "feed": req.feed,
        "plungeFeed": req.plungeFeed,
        "safeZ": req.safeZ,
        "liftZ": req.liftZ,
        "spindle": req.spindle,
        "leadIn": req.leadIn,
        "leadRadius": req.leadRadius,
    }, resolution)


@router.post("/toolpath", response_model=TaskAccepted)
async def toolpath(req: ToolpathRequest) -> TaskAccepted:
    params = _toolpath_params(req)
    task_id = await task_queue.submit("toolpath", toolpath_job, params, params["projectId"])
    return TaskAccepted(taskId=task_id, kind="toolpath")


# ---------------------------------------------------------------- 导出（模块 7）
def _pick_processor(req: ExportRequest) -> Dict[str, Any]:
    """processorId 优先；只给 format 时走内置映射（nc/plt/cut）。"""
    processor_id = req.processorId or FMT_TO_PROCESSOR.get(req.format)
    if not processor_id:
        raise HTTPException(400, "不支持的后处理格式：%s" % req.format)
    try:
        return post_registry.get_processor(processor_id)
    except post_registry.PostError as exc:
        raise HTTPException(400, str(exc)) from exc


def _render_context(params: Dict[str, Any], req: ExportRequest,
                    processor: Dict[str, Any]) -> RenderContext:
    """导出上下文：安全高度/进给/主轴/刀具号均以解析后的参数为准。"""
    tool = params.get("tool") or {}
    program_no = (processor.get("gcode") or {}).get("programNo") or "0001"
    return RenderContext(
        name=req.name,
        safe_z=float(params.get("safeZ", req.safeZ)),
        spindle=float(params.get("spindle", req.spindle)),
        feed=float(params.get("feed", req.feed)),
        tool=int(tool["id"]) if tool else None,
        flip_y=req.flipY,
        speed=req.speed,
        force=req.force,
        program_no=program_no,
    )


def _layout_sheets(project_id: int) -> List[int]:
    with db.get_conn() as conn:
        layout_state = layout_engine.load(conn, project_id)
    return sorted({int(p.sheet) for p in layout_state.placements.values()})


def _sum_stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """多板批量时汇总统计，字段与单板保持一致。"""
    if len(rows) == 1:
        return dict(rows[0].get("stats") or {})
    total: Dict[str, Any] = {"fileCount": len(rows)}
    for key in ("contourCount", "innerCount", "outerCount", "bridgeCount",
                "moveCount", "cutLengthMm", "rapidLengthMm", "estimatedMinutes"):
        total[key] = sum(float((row.get("stats") or {}).get(key, 0) or 0)
                         for row in rows)
    for key in ("contourCount", "innerCount", "outerCount", "bridgeCount", "moveCount"):
        total[key] = int(total[key])
    return total


@router.post("/export")
async def export(req: ExportRequest) -> Dict[str, Any]:
    """同步导出：刀路文本或 DXF 图纸；`allSheets` 时每块板一个文件。"""
    project_id = _project_id(req.projectId)
    processor = _pick_processor(req)

    def _record(result: Dict[str, Any]) -> None:
        """带工单导出时登记文件路径（失败不影响导出本身）。"""
        if not req.workOrderId:
            return
        try:
            with db.get_conn() as conn:
                count = work_order_service.add_export_files(
                    conn, project_id, int(req.workOrderId), result.get("files") or [])
            logger.info("工单 #%s 登记加工文件 %d 个", req.workOrderId, count)
        except Exception:
            logger.warning("工单加工文件登记失败（#%s）", req.workOrderId, exc_info=True)

    # DXF 导出的是工程图形，与刀路参数无关，也不按板材拆分
    if str(processor.get("kind")) == "dxf":
        def _drawing() -> Dict[str, Any]:
            with db.get_conn() as conn:
                doc = geometry_service.load_document(conn, project_id)
            if not doc.polylines:
                raise HTTPException(400, "当前工程没有图形，无法导出 DXF")
            result = post_service.export_drawing(doc, processor, req.name)
            payload = {
                "path": result["path"],
                "format": result["format"],
                "processor": processor["id"],
                "files": [result],
                "stats": {"contourCount": result["contours"]},
                "tool": None,
                "template": None,
            }
            _record(payload)
            return payload
        try:
            return await run_in_threadpool(_drawing)
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("DXF 导出失败")
            raise HTTPException(500, "DXF 导出失败: %s" % exc) from exc

    def _do() -> Dict[str, Any]:
        requests = [req]
        if req.source == "layout" and req.allSheets:
            sheets = _layout_sheets(project_id)
            if not sheets:
                raise HTTPException(400, "当前工程还没有排料结果，无法批量导出")
            requests = [req.model_copy(update={"sheetIndex": sheet, "allSheets": False})
                        for sheet in sheets]

        stamp = time.strftime("%Y%m%d_%H%M%S")
        batch = len(requests) > 1
        files: List[Dict[str, Any]] = []
        last_params: Dict[str, Any] = {}
        for sheet_req in requests:
            params = _toolpath_params(sheet_req)
            plan = build_toolpath(params)
            state.LAST_PLAN = plan
            tag = "S%d" % (sheet_req.sheetIndex + 1) if batch else None
            result = post_service.export_toolpath(
                plan, processor, _render_context(params, req, processor),
                tag=tag, stamp=stamp)
            result.update({"sheetIndex": sheet_req.sheetIndex, "stats": plan.stats})
            files.append(result)
            last_params = params

        return {
            "path": files[0]["path"],
            "format": files[0]["format"],
            "processor": processor["id"],
            "files": files,
            "stats": _sum_stats(files),
            "tool": last_params.get("tool"),
            "template": last_params.get("template"),
        }

    try:
        result = await run_in_threadpool(_do)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("导出失败")
        raise HTTPException(500, f"导出失败: {exc}") from exc

    def _register() -> None:
        _record(result)

    await run_in_threadpool(_register)
    return result


@router.get("/export/dir")
async def export_dir() -> Dict[str, str]:
    """导出目录（前端"打开文件夹"用）。"""
    path = settings.data_dir / "exports"
    path.mkdir(parents=True, exist_ok=True)
    return {"dir": str(path.resolve())}
