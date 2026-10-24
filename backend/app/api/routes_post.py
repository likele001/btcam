"""后处理配置接口（模块 7）。

后处理不是工程数据，而是“机床库”：存 `%LOCALAPPDATA%/BtCAM/post/*.json`，
一份配置一台机床，内置模板首次启动自动落盘。接口只做搬运与错误翻译：

    GET    /api/post                   全部后处理（内置在前）
    GET    /api/post/variables         变量字典（模块 11：占位符 + 指令槽位）
    POST   /api/post/validate          校验草稿（不落盘，逐字段报错）
    POST   /api/post/preview           预览渲染（与导出同一套渲染器）
    GET    /api/post/{id}              单份配置
    POST   /api/post                   新建自定义后处理
    PUT    /api/post/{id}              修改自定义后处理（内置拒绝，请另存为）
    DELETE /api/post/{id}              删除自定义后处理（内置不可删）

注意路由顺序：/variables /validate /preview 必须定义在 /{processor_id} 之前，
否则会被通配路由当成一个 id 吞掉。

导出走 `POST /api/export` 的 `processorId`，不在这里触发。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from app.models.schemas import PostDraftRequest, PostPreviewRequest, PostProcessorRequest
from app.post import editor, registry
from app.post.registry import PostError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/post", tags=["postprocessor"])


def _http_error(exc: PostError) -> HTTPException:
    message = str(exc)
    return HTTPException(404 if "不存在" in message else 400, message)


@router.get("")
def list_processors() -> Dict[str, Any]:
    return {"processors": registry.list_processors()}


@router.get("/variables")
def post_variables() -> Dict[str, Any]:
    """变量字典（模块 11）：前端变量面板的唯一数据源。"""
    return editor.variable_dictionary()


@router.post("/validate")
def validate_draft(req: PostDraftRequest) -> Dict[str, Any]:
    """校验草稿：只读，不写文件；错误按字段返回，告警不拦保存。"""
    return editor.validate(req.processor, req.processorId)


@router.post("/preview")
def preview_draft(req: PostPreviewRequest) -> Dict[str, Any]:
    """预览渲染：校验不过就返回空文本，避免“半成品”误导用户。"""
    return editor.preview(req.processor, req.options)


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
