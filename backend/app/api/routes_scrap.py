"""余料库接口（模块 10）。

    GET    /api/scrap                    余料列表（含当前板材绑定的 id）
    POST   /api/scrap                    手工/接口直建一块余料
    GET    /api/scrap/{id}               单块余料详情（含轮廓）
    PUT    /api/scrap/{id}               改名称/物料/备注（给轮廓就重算尺寸）
    DELETE /api/scrap/{id}               删除（正在当板材则自动解绑）
    POST   /api/scrap/remaining          计算当前板材的剩余区域（不落库）
    POST   /api/scrap/save               把勾选的剩余区域存进余料库
    POST   /api/scrap/{id}/apply         把余料设为当前板材（轮廓即边界）
    POST   /api/scrap/clear              解绑余料，退回规则矩形板

"把余料当板材"不新增排料接口：只改 layout_sheet 的尺寸与 scrap_id，
之后的移动/旋转/自动排料仍然走 /api/layout/*，边界判定在 LayoutService。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from app.api.routes_layout import push_layout
from app.models import db
from app.models.schemas import (
    ScrapApplyRequest,
    ScrapClearRequest,
    ScrapRemainingRequest,
    ScrapRequest,
    ScrapSaveRequest,
)
from app.services import scrap_service
from app.services.scrap_service import ScrapError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/scrap", tags=["scrap"])


# ---------------------------------------------------------------- 工具
def _project_id(requested: Optional[int]) -> int:
    if requested:
        return int(requested)
    with db.get_conn() as conn:
        return db.get_active_project_id(conn)


def _http_error(exc: ScrapError) -> HTTPException:
    message = str(exc)
    return HTTPException(404 if "不存在" in message else 400, message)


# ---------------------------------------------------------------- 增删改查
@router.get("")
async def list_scraps(keyword: str = "", limit: int = 200,
                      projectId: Optional[int] = None) -> Dict[str, Any]:
    """余料库列表；`currentScrapId` 为当前排料板正在用的那块。"""
    pid = _project_id(projectId)

    def _load() -> Dict[str, Any]:
        with db.get_conn() as conn:
            return scrap_service.list_scraps(conn, pid, keyword, limit)

    return await run_in_threadpool(_load)


@router.post("")
async def create_scrap(req: ScrapRequest) -> Dict[str, Any]:
    """新建余料：outline 优先；只给宽高则按矩形生成轮廓。"""
    pid = _project_id(req.projectId)
    payload = req.model_dump()

    def _create() -> Dict[str, Any]:
        with db.get_conn() as conn:
            try:
                return scrap_service.create_scrap(conn, pid, payload)
            except ScrapError as exc:
                raise _http_error(exc) from exc

    return await run_in_threadpool(_create)


@router.get("/{scrap_id}")
async def get_scrap(scrap_id: int, projectId: Optional[int] = None) -> Dict[str, Any]:
    pid = _project_id(projectId)

    def _load() -> Dict[str, Any]:
        with db.get_conn() as conn:
            try:
                return scrap_service.get_scrap(conn, pid, scrap_id)
            except ScrapError as exc:
                raise _http_error(exc) from exc

    return await run_in_threadpool(_load)


@router.put("/{scrap_id}")
async def update_scrap(scrap_id: int, req: ScrapRequest) -> Dict[str, Any]:
    pid = _project_id(req.projectId)
    payload = req.model_dump()

    def _update() -> Dict[str, Any]:
        with db.get_conn() as conn:
            try:
                return scrap_service.update_scrap(conn, pid, scrap_id, payload)
            except ScrapError as exc:
                raise _http_error(exc) from exc

    return await run_in_threadpool(_update)


@router.delete("/{scrap_id}")
async def delete_scrap(scrap_id: int, projectId: Optional[int] = None) -> Dict[str, Any]:
    pid = _project_id(projectId)

    def _remove() -> Dict[str, Any]:
        with db.get_conn() as conn:
            try:
                return scrap_service.remove_scrap(conn, pid, scrap_id)
            except ScrapError as exc:
                raise _http_error(exc) from exc

    return await run_in_threadpool(_remove)


# ---------------------------------------------------------------- 剩余区域
@router.post("/remaining")
async def remaining_regions(req: ScrapRemainingRequest) -> Dict[str, Any]:
    """当前板材减掉已摆零件后的可用碎块（预览，不落库）。"""
    pid = _project_id(req.projectId)

    def _calc() -> Dict[str, Any]:
        with db.get_conn() as conn:
            try:
                return scrap_service.remaining_regions(conn, pid, req.minArea, req.gap)
            except ScrapError as exc:
                raise _http_error(exc) from exc

    return await run_in_threadpool(_calc)


@router.post("/save")
async def save_regions(req: ScrapSaveRequest) -> Dict[str, Any]:
    """把勾选的剩余区域存进余料库（几何以后端重算结果为准）。"""
    pid = _project_id(req.projectId)

    def _save() -> Dict[str, Any]:
        with db.get_conn() as conn:
            try:
                return scrap_service.save_regions(
                    conn, pid, req.indices, req.minArea, req.gap,
                    req.namePrefix, req.material, req.thickness, req.note)
            except ScrapError as exc:
                raise _http_error(exc) from exc

    return await run_in_threadpool(_save)


# ---------------------------------------------------------------- 应用为板材
@router.post("/clear")
async def clear_sheet(req: ScrapClearRequest) -> Dict[str, Any]:
    """解绑余料，退回规则矩形板；改完立即广播排料状态刷新画布。"""
    pid = _project_id(req.projectId)

    def _clear() -> Dict[str, Any]:
        with db.get_conn() as conn:
            try:
                return scrap_service.clear_sheet(conn, pid, req.width, req.height,
                                                 req.margin, req.gap)
            except ScrapError as exc:
                raise _http_error(exc) from exc

    result = await run_in_threadpool(_clear)
    meta = await push_layout(project_id=pid)
    result["stats"] = meta.get("stats", result.get("stats", {}))
    result["sheet"] = meta.get("sheet", {})
    return result


@router.post("/{scrap_id}/apply")
async def apply_scrap(scrap_id: int, req: ScrapApplyRequest) -> Dict[str, Any]:
    """把余料设为当前板材：尺寸取包围盒、margin 归零、绑定 scrap_id。

    之后前端照旧调 /api/layout/*，越界判定会自动用余料轮廓。
    """
    pid = _project_id(req.projectId)

    def _apply() -> Dict[str, Any]:
        with db.get_conn() as conn:
            try:
                return scrap_service.apply_to_sheet(conn, pid, scrap_id, req.gap,
                                                    req.resetPlacements)
            except ScrapError as exc:
                raise _http_error(exc) from exc

    result = await run_in_threadpool(_apply)
    # 换板会改变零件位置与边界：广播一帧，画布与面板一起刷新
    meta = await push_layout(project_id=pid)
    result["stats"] = meta.get("stats", result.get("stats", {}))
    result["sheet"] = meta.get("sheet", {})
    result["scrap"] = meta.get("scrap")
    return result
