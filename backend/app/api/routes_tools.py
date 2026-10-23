"""刀具库 / 工艺模板接口（模块 6）。

接口只做三件事：取工程 id、调服务层、把 ToolError 翻译成 400。
刀路与导出通过 toolId/templateId 引用这里的记录，参数解析在服务层完成。

    GET    /api/tools              刀具列表
    POST   /api/tools              新增刀具
    PUT    /api/tools/{id}         修改刀具
    DELETE /api/tools/{id}         删除（被模板引用时拒绝）
    GET    /api/templates          模板列表（内嵌 tool 详情）
    POST   /api/templates          新增模板
    PUT    /api/templates/{id}     修改模板
    DELETE /api/templates/{id}     删除模板
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException

from app.models import db
from app.models.schemas import TemplateRequest, ToolRequest
from app.services import tool_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["tools"])


def _project_id(requested: Optional[int]) -> int:
    if requested:
        return int(requested)
    with db.get_conn() as conn:
        return db.get_active_project_id(conn)


# ---------------------------------------------------------------- 刀具
@router.get("/tools")
def list_tools(projectId: Optional[int] = None) -> Dict[str, Any]:
    project_id = _project_id(projectId)
    with db.get_conn() as conn:
        tools = tool_service.list_tools(conn, project_id)
    return {"projectId": project_id, "tools": tools}


@router.get("/tools/{tool_id}")
def get_tool(tool_id: int, projectId: Optional[int] = None) -> Dict[str, Any]:
    project_id = _project_id(projectId)
    with db.get_conn() as conn:
        tool = tool_service.get_tool(conn, project_id, tool_id)
    if tool is None:
        raise HTTPException(404, "刀具 #%d 不存在" % tool_id)
    return tool


@router.post("/tools")
def create_tool(req: ToolRequest) -> Dict[str, Any]:
    project_id = _project_id(req.projectId)
    try:
        with db.get_conn() as conn:
            return tool_service.create_tool(conn, project_id, req.model_dump())
    except tool_service.ToolError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.put("/tools/{tool_id}")
def update_tool(tool_id: int, req: ToolRequest) -> Dict[str, Any]:
    project_id = _project_id(req.projectId)
    try:
        with db.get_conn() as conn:
            return tool_service.edit_tool(conn, project_id, tool_id, req.model_dump())
    except tool_service.ToolError as exc:
        message = str(exc)
        raise HTTPException(404 if "不存在" in message else 400, message) from exc


@router.delete("/tools/{tool_id}")
def delete_tool(tool_id: int, projectId: Optional[int] = None) -> Dict[str, Any]:
    project_id = _project_id(projectId)
    try:
        with db.get_conn() as conn:
            tool_service.remove_tool(conn, project_id, tool_id)
    except tool_service.ToolError as exc:
        message = str(exc)
        raise HTTPException(404 if "不存在" in message else 400, message) from exc
    return {"deleted": True, "id": tool_id, "projectId": project_id}


# ---------------------------------------------------------------- 工艺模板
@router.get("/templates")
def list_templates(projectId: Optional[int] = None) -> Dict[str, Any]:
    project_id = _project_id(projectId)
    with db.get_conn() as conn:
        templates = tool_service.list_templates(conn, project_id)
    return {"projectId": project_id, "templates": templates}


@router.post("/templates")
def create_template(req: TemplateRequest) -> Dict[str, Any]:
    project_id = _project_id(req.projectId)
    try:
        with db.get_conn() as conn:
            return tool_service.create_template(conn, project_id, req.model_dump())
    except tool_service.ToolError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.put("/templates/{template_id}")
def update_template(template_id: int, req: TemplateRequest) -> Dict[str, Any]:
    project_id = _project_id(req.projectId)
    try:
        with db.get_conn() as conn:
            return tool_service.edit_template(conn, project_id, template_id, req.model_dump())
    except tool_service.ToolError as exc:
        message = str(exc)
        raise HTTPException(404 if "不存在" in message else 400, message) from exc


@router.delete("/templates/{template_id}")
def delete_template(template_id: int,
                    projectId: Optional[int] = None) -> Dict[str, Any]:
    project_id = _project_id(projectId)
    try:
        with db.get_conn() as conn:
            tool_service.remove_template(conn, project_id, template_id)
    except tool_service.ToolError as exc:
        message = str(exc)
        raise HTTPException(404 if "不存在" in message else 400, message) from exc
    return {"deleted": True, "id": template_id, "projectId": project_id}
