"""MES 对接接口（模块 12）。

    GET  /api/mes/settings        读配置（密钥掩码）
    PUT  /api/mes/settings        保存配置（校验 + 密钥留空不改）
    POST /api/mes/test            测试连接（可用未保存的草稿）
    POST /api/mes/pull            从 MES 拉取工单（按单号 upsert）
    GET  /api/mes/orders          MES 工单列表
    GET  /api/mes/orders/{id}     单张 MES 工单（含零件清单与原始响应）
    POST /api/mes/push            回传归档（工单信息 + 用料统计 + 加工文件）
    GET  /api/mes/logs            交互日志（测试 / 拉单 / 推送）

错误口径（前端按这些码区分提示）：
    400  未启用 / 配置不合法 / 数据认不出
    404  MES 工单或本地工单不存在
    502  MES 侧不可达或返回错误，detail 里带"已尝试 N 次"

整个模块是"可选件"：没有任何 CAM 基础接口依赖这里，关掉开关或干脆不配，
`/api/health`、排料、刀路、导出全都照常工作。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query
from starlette.concurrency import run_in_threadpool

from app.mes import service as mes_service
from app.mes import settings as mes_settings
from app.mes.client import MesError
from app.mes.service import MesDataError, MesDisabledError
from app.mes.settings import MesConfig, MesConfigError
from app.models import db
from app.models.schemas import MesPullRequest, MesPushRequest, MesSettingsRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mes", tags=["mes"])


def _translate(exc: Exception) -> HTTPException:
    """把内部异常翻成 HTTP；未识别的交给全局 500。"""
    if isinstance(exc, MesDataError):
        # "不存在"→404，其余算数据认不出→400
        return HTTPException(404 if "不存在" in str(exc) else 400, str(exc))
    if isinstance(exc, (MesDisabledError, MesConfigError)):
        return HTTPException(400, str(exc))
    if isinstance(exc, MesError):
        detail = str(exc)
        if exc.attempts:
            detail = "%s（已尝试 %d 次）" % (detail, exc.attempts)
        return HTTPException(502, detail)
    logger.exception("MES 对接内部错误")
    return HTTPException(500, "MES 对接内部错误：%s" % exc)


def _with_stats(config: MesConfig) -> Dict[str, Any]:
    """设置页需要的形状：掩码配置 + 工单计数 + 最后一条日志。"""
    data = mes_settings.public_payload(config)
    with db.get_conn() as conn:
        counts = db.count_mes_orders(conn)
        logs = db.list_mes_logs(conn, limit=1)
    data["orderCount"] = counts["total"]
    data["archivedCount"] = counts["archived"]
    data["lastLog"] = logs[0] if logs else None
    return data


def _active_project_id(requested: Optional[int]) -> int:
    if requested:
        return int(requested)
    with db.get_conn() as conn:
        return db.get_active_project_id(conn)


# ---------------------------------------------------------------- 配置
@router.get("/settings")
async def mes_settings_get() -> Dict[str, Any]:
    """读配置；apiKey 永不回明文，只回掩码与"是否已设置"。"""
    def _load() -> Dict[str, Any]:
        return _with_stats(mes_settings.load())

    try:
        return await run_in_threadpool(_load)
    except Exception as exc:
        raise _translate(exc) from exc


@router.put("/settings")
async def mes_settings_put(req: MesSettingsRequest) -> Dict[str, Any]:
    """保存配置；`apiKey` 留空沿用旧密钥，避免掩码被当成新密钥写回。"""
    def _save() -> Dict[str, Any]:
        return _with_stats(mes_settings.save(req.model_dump()))

    try:
        return await run_in_threadpool(_save)
    except Exception as exc:
        raise _translate(exc) from exc


# ---------------------------------------------------------------- 测试连接
@router.post("/test")
async def mes_test(req: Optional[MesSettingsRequest] = None) -> Dict[str, Any]:
    """测试连接：带草稿参数就测草稿（设置页"先测后存"），不带就测已保存的。"""
    draft = req.model_dump() if req is not None else None

    def _test() -> Dict[str, Any]:
        return mes_service.test_connection(draft)

    try:
        return await run_in_threadpool(_test)
    except Exception as exc:
        raise _translate(exc) from exc


# ---------------------------------------------------------------- 拉单
@router.post("/pull")
async def mes_pull(req: MesPullRequest) -> Dict[str, Any]:
    """拉取工单，写入本地 mes_order，返回本次新增/更新数量。"""
    def _pull() -> Dict[str, Any]:
        return mes_service.pull_orders(_active_project_id(req.projectId), req.limit)

    try:
        return await run_in_threadpool(_pull)
    except Exception as exc:
        raise _translate(exc) from exc


# ---------------------------------------------------------------- 工单
@router.get("/orders")
async def mes_orders(status: str = "", limit: int = 100) -> Dict[str, Any]:
    """本地缓存的 MES 工单列表；`status` 支持 pulled / archived。"""
    def _load() -> Dict[str, Any]:
        with db.get_conn() as conn:
            return mes_service.list_orders(conn, status=status, limit=limit)

    try:
        return await run_in_threadpool(_load)
    except Exception as exc:
        raise _translate(exc) from exc


@router.get("/orders/{order_id}")
async def mes_order_detail(order_id: int) -> Dict[str, Any]:
    """单张 MES 工单：零件清单 + 原始响应，排障与"下一步排料"都用得上。"""
    def _load() -> Dict[str, Any]:
        with db.get_conn() as conn:
            return mes_service.get_order(conn, order_id)

    try:
        return await run_in_threadpool(_load)
    except Exception as exc:
        raise _translate(exc) from exc


# ---------------------------------------------------------------- 回传
@router.post("/push")
async def mes_push(req: MesPushRequest) -> Dict[str, Any]:
    """回传归档：组 payload（工单 / 用料 / 文件）→ 推送 → 标记 archived → 写日志。"""
    def _push() -> Dict[str, Any]:
        # projectId 传 0：服务层会先看工单上是哪个工程，最后才退回当前工程
        return mes_service.push_order(0, req.orderId, req.workOrderId, req.includeFiles)

    try:
        return await run_in_threadpool(_push)
    except Exception as exc:
        raise _translate(exc) from exc


# ---------------------------------------------------------------- 日志
@router.get("/logs")
async def mes_logs(order_id: int = Query(0, alias="orderId"),
                   limit: int = 100) -> Dict[str, Any]:
    """交互日志，最新在前；前端设置页底部直接铺开。"""
    def _load() -> Dict[str, Any]:
        with db.get_conn() as conn:
            return mes_service.list_logs(conn, order_id=order_id, limit=limit)

    try:
        return await run_in_threadpool(_load)
    except Exception as exc:
        raise _translate(exc) from exc
