"""运行日志接口（模块 9）。

    GET    /api/logs         查询环形缓冲（级别/关键词/来源/增量游标）
    DELETE /api/logs         清空缓冲（不影响 logs/backend.log）
    POST   /api/logs/client  前端异常上报（渲染报错、未捕获 Promise）

设计取舍：面板读内存缓冲而不是读日志文件 —— 文件是给人排查用的，
格式随 formatter 变化，前端解析等于把日志格式再耦合一次；
结构化条目才能支持按级别/来源过滤，也才能直接作为 ALERT 帧的 meta 复用。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query

from app.core.log_store import LEVEL_ORDER, log_store
from app.models.schemas import ClientLogRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("")
async def list_logs(
    level: str = Query("", description="最低级别：debug/info/warning/error"),
    keyword: str = Query("", max_length=100, description="关键词，匹配文本与来源"),
    source: str = Query("", max_length=60, description="来源前缀，如 task / import"),
    limit: int = Query(200, ge=1, le=1000),
    after: int = Query(0, ge=0, description="只返回 id 大于该值的条目（增量拉取）"),
) -> Dict[str, Any]:
    """返回按时间升序的最近 N 条，附 seq（最新 id）便于前端做增量轮询。"""
    if level and level.strip().lower() not in LEVEL_ORDER and \
            level.strip().lower() not in ("warn", "err", "fatal", "dbg"):
        raise HTTPException(400, "非法级别: %s" % level)
    return log_store.query(level=level, keyword=keyword, source=source,
                           limit=limit, after=after)


@router.delete("")
async def clear_logs() -> Dict[str, Any]:
    """清空内存缓冲；磁盘日志由滚动策略自己管理，这里不动。"""
    cleared = log_store.clear()
    logger.info("日志缓冲已清空（%d 条）", cleared)
    return {"cleared": cleared, "capacity": log_store.capacity}


@router.post("/client")
async def report_client_log(req: ClientLogRequest) -> Dict[str, Any]:
    """前端异常上报。

    条目先经 log_store 入环（error 自动推 ALERT，前端弹窗；
    info 只入面板，避免普通提示刷成弹窗），再用同一个 logger 写文件，
    通过 `_btcam_skip_ring` 标记跳过环形缓冲，防止同一条记两遍。
    """
    entry = log_store.append(
        req.level, req.message, source=req.source,
        kind=req.kind, detail=req.detail or None,
        broadcast=req.level != "info",
    )
    text = "前端异常 [%s] %s" % (req.source, req.message)
    if req.detail:
        # 堆栈接在正文后，backend.log 里一眼能看全
        text = "%s\n%s" % (text, req.detail)
    extra = {"_btcam_skip_ring": True, "kind": req.kind}
    if req.level == "error":
        logger.error(text, extra=extra)
    elif req.level == "warning":
        logger.warning(text, extra=extra)
    else:
        logger.info(text, extra=extra)
    return {"ok": True, "id": entry["id"], "time": entry["time"],
            "level": entry["level"], "seq": log_store.query(limit=1)["seq"]}
