"""后处理配置接口（模块 7）。

后处理不是工程数据，而是“机床库”：存 `%LOCALAPPDATA%/BtCAM/post/*.json`，
一份配置一台机床，内置模板首次启动自动落盘。接口只做搬运与错误翻译：

    GET    /api/post                   全部后处理（内置在前）
    GET    /api/post/{id}              单份配置
    POST   /api/post                   新建自定义后处理
    PUT    /api/post/{id}              修改自定义后处理（内置拒绝，请另存为）
    DELETE /api/post/{id}              删除自定义后处理（内置不可删）

导出走 `POST /api/export` 的 `processorId`，不在这里触发。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from app.models.schemas import PostProcessorRequest
from app.post import registry
from app.post.registry import PostError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/post", tags=["postprocessor"])


def _http_error(exc: PostError) -> HTTPException:
    message = str(exc)
    return HTTPException(404 if "不存在" in message else 400, message)


@router.get("")
def list_processors() -> Dict[str, Any]:
    return {"processors": registry.list_processors()}


@router.get("/{processor_id}")
def get_processor(processor_id: str) -> Dict[str, Any]:
    try:
        return registry.get_processor(processor_id)
    except PostError as exc:
        raise _http_error(exc) from exc


@router.post("")
def create_processor(req: PostProcessorRequest) -> Dict[str, Any]:
    try:
        return registry.save_processor(req.model_dump())
    except PostError as exc:
        raise _http_error(exc) from exc


@router.put("/{processor_id}")
def update_processor(processor_id: str, req: PostProcessorRequest) -> Dict[str, Any]:
    payload = req.model_dump()
    payload["id"] = processor_id
    try:
        return registry.save_processor(payload, processor_id=processor_id)
    except PostError as exc:
        raise _http_error(exc) from exc


@router.delete("/{processor_id}")
def delete_processor(processor_id: str) -> Dict[str, Any]:
    try:
        registry.remove_processor(processor_id)
    except PostError as exc:
        raise _http_error(exc) from exc
    return {"deleted": True, "id": processor_id}
