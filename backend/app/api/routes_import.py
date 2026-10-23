"""DXF 导入接口（模块 1）。

前端只做两件事：弹出文件选择框、把文件二进制 POST 上来。
解析 / 修复 / 入库全部在 TaskQueue 的工作线程里完成，进度与结果走 WebSocket。

为什么用 multipart 而不是让前端读路径：
Tauri 的文件对话框给的是本地绝对路径，后端直接读盘会绕过前端权限；
上传二进制则由前端决定"给哪个文件"，后端只认落地的临时文件，
将来换成远程后端也不用改协议。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.config import settings
from app.core.tasks import task_queue
from app.models import db
from app.models.schemas import TaskAccepted
from app.services.import_service import TASK_KIND, import_dxf_job

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/import", tags=["import"])

# 单文件上限 200MB：正常板式家具 DXF 在 1~20MB，超限基本是误选了 3D 模型
MAX_FILE_BYTES = 200 * 1024 * 1024
# 单次批量上限：再多就不适合"一次导入一个工单"的用法了
MAX_FILES = 50

# Windows 文件名非法字符（顺带挡掉路径穿越）
_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _resolve_project_id(requested: Optional[int]) -> int:
    if requested:
        return int(requested)
    with db.get_conn() as conn:
        return db.get_active_project_id(conn)


def _safe_name(raw: str) -> str:
    """保留中文，只清理非法字符；同时去掉任何目录成分。"""
    name = os.path.basename(raw.replace("\\", "/"))
    name = _UNSAFE_NAME.sub("_", name).strip(" .")
    return name or "unnamed.dxf"


@router.post("/dxf", response_model=TaskAccepted)
async def import_dxf_upload(
    files: List[UploadFile] = File(..., description="一个或多个 .dxf 文件（每个文件 = 一个或多个零件）"),
    options: str = Form(
        "{}",
        description='JSON：{"read":{...}, "repair":{...}, "replace":true, "splitParts":true}',
    ),
    projectId: Optional[int] = Form(None, description="目标工程 id，不传则用当前活跃工程"),
) -> TaskAccepted:
    """上传 DXF 并异步解析。

    入参：
        files     -- multipart 文件列表，只接受 .dxf
        options   -- JSON 字符串，可选：
                     read  : {flattenDistance, unitScale, skipOffLayers, explodeBlocks, includeHatch}
                     repair: {mergeTol, minLength, minArea, closeGap, removeOverlap, checkIssues}
                     replace   : true = 先清空当前工程图形（默认），false = 追加
                     splitParts: true = 按包含关系拆成多个零件（默认）
        projectId -- 目标工程 id

    出参：
        TaskAccepted {taskId, kind, status}
        后续进度走 TASK_PROGRESS，结果走 TASK_RESULT + GEOMETRY_DOC + GEOMETRY_ISSUES

    异常：
        400 -- 没有文件 / 扩展名不对 / options 不是合法 JSON
        413 -- 单文件超过 200MB
    """
    if not files:
        raise HTTPException(400, "没有收到文件")
    if len(files) > MAX_FILES:
        raise HTTPException(400, "单次最多导入 %d 个文件" % MAX_FILES)

    try:
        raw_options: Dict[str, Any] = json.loads(options or "{}")
        if not isinstance(raw_options, dict):
            raise ValueError("options 必须是 JSON 对象")
    except Exception as exc:
        raise HTTPException(400, "options 解析失败：%s" % exc) from exc

    project_id = _resolve_project_id(projectId)

    # 临时目录名以 imp_ 开头：import_service 清理时按这个前缀识别
    upload_dir = settings.data_dir / "imports" / ("imp_" + uuid.uuid4().hex[:12])
    upload_dir.mkdir(parents=True, exist_ok=True)

    saved: List[Dict[str, str]] = []
    try:
        total_bytes = 0
        for upload in files:
            name = _safe_name(upload.filename or "unnamed.dxf")
            if not name.lower().endswith(".dxf"):
                raise HTTPException(400, "只支持 .dxf 文件：%s" % name)
            target = upload_dir / name
            size = 0
            with open(target, "wb") as handle:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    total_bytes += len(chunk)
                    if size > MAX_FILE_BYTES or total_bytes > MAX_FILE_BYTES * 4:
                        raise HTTPException(413, "文件过大：%s" % name)
                    handle.write(chunk)
            if size == 0:
                raise HTTPException(400, "文件为空：%s" % name)
            saved.append({"name": name, "path": str(target)})
    except HTTPException:
        shutil.rmtree(upload_dir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(upload_dir, ignore_errors=True)
        logger.exception("保存上传文件失败")
        raise HTTPException(500, "保存上传文件失败：%s" % exc) from exc

    params: Dict[str, Any] = {
        "projectId": project_id,
        "files": saved,
        "read": raw_options.get("read") or {},
        "repair": raw_options.get("repair") or {},
        "replace": bool(raw_options.get("replace", True)),
        "splitParts": bool(raw_options.get("splitParts", True)),
        "cleanup": True,
    }
    task_id = await task_queue.submit(TASK_KIND, import_dxf_job, params, project_id)
    logger.info("DXF 导入任务已提交 task=%s project=%s 文件=%s",
                task_id, project_id, [s["name"] for s in saved])
    return TaskAccepted(taskId=task_id, kind=TASK_KIND)


@router.get("/defaults")
async def import_defaults() -> Dict[str, Any]:
    """导入参数的默认值（前端"高级选项"面板初始化用）。"""
    return {
        "read": {
            "flattenDistance": 0.05,
            "unitScale": None,
            "skipOffLayers": True,
            "explodeBlocks": True,
            "includeHatch": True,
            "maxBlockDepth": 8,
        },
        "repair": {
            "mergeTol": 0.2,
            "minLength": 0.5,
            "minArea": 0.5,
            "closeGap": 1.0,
            "removeOverlap": True,
            "checkIssues": True,
        },
        "replace": True,
        "splitParts": True,
    }
