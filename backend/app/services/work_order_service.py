"""工单存档服务（模块 8）。

一份工单 = 当前工程的一份只读快照：

    图形（零件 + 轮廓，顶点 base64 float32）、板材参数、摆料状态、
    刀具/工艺模板引用、刀路参数、后处理选择、加工文件路径。

为什么存"快照"而不是引用工程：
    工单是交付凭证，用户继续改当前工程（甚至重新导入图纸）不能改变历史记录；
    打开旧工单时再把快照写回当前工程（零件/轮廓重新分配 id），
    这样既能"重复打开编辑"，也不会反向污染存档。

坐标与摆放沿用模块 3 的契约：零部件轮廓按原坐标存，tx/ty 仍是
"旋转后包围盒左下角"，恢复时只做 part_id 重映射，不做几何换算。
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any, Dict, List, Optional

import numpy as np

from app.models import db
from app.services import geometry_service, tool_service
from app.services.layout_service import layout_service as layout_engine

logger = logging.getLogger(__name__)

SNAPSHOT_VERSION = 1
MAX_START_POINTS = 64

# 只保存"参数"，不保存选中集/临时轮廓：
# 恢复后轮廓 id 会变，旧 ids 没有意义，刀路按当前工程重新生成。
TOOLPATH_KEYS = (
    "source", "sheetIndex", "cutType", "direction", "optimize",
    "microJoint", "microJointWidth", "microJointCount",
    "toolDiameter", "toolId", "templateId", "side",
    "depth", "passDepth", "feed", "plungeFeed", "safeZ", "liftZ",
    "spindle", "leadIn", "leadRadius",
)
POST_KEYS = ("processorId", "format", "name", "flipY", "speed", "force")


class WorkOrderError(ValueError):
    """业务校验失败：路由层统一翻译成 HTTP 400 / 404。"""


# ---------------------------------------------------------------- 快照编解码
def encode_points(pts: Any) -> str:
    """顶点 -> base64(float32 LE)。

    上万条轮廓的图纸如果按 JSON 文本存点，快照会膨胀十倍；
    这里与二进制协议保持一致，存 float32 原始字节。
    """
    arr = np.ascontiguousarray(pts, dtype="<f4")
    return base64.b64encode(arr.tobytes()).decode("ascii")


def decode_points(text: str) -> np.ndarray:
    raw = base64.b64decode(str(text).encode("ascii"))
    return np.frombuffer(raw, dtype="<f4").reshape(-1, 2)


def clean_toolpath(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """白名单化刀路参数，顺带裁掉超量起割点。"""
    data = raw if isinstance(raw, dict) else {}
    out: Dict[str, Any] = {key: data[key] for key in TOOLPATH_KEYS
                           if key in data and data[key] is not None}
    points = data.get("startPoints")
    if isinstance(points, list) and points:
        cleaned: List[Dict[str, Any]] = []
        for point in points[:MAX_START_POINTS]:
            if not isinstance(point, dict):
                continue
            item = {"x": float(point.get("x", 0.0)), "y": float(point.get("y", 0.0))}
            if point.get("contourId") is not None:
                item["contourId"] = int(point["contourId"])
            cleaned.append(item)
        if cleaned:
            out["startPoints"] = cleaned
    return out


def clean_post(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    out: Dict[str, Any] = {key: data[key] for key in POST_KEYS
                           if key in data and data[key] is not None}
    out.setdefault("format", "nc")
    out.setdefault("name", "BT_CAM")
    return out


# ---------------------------------------------------------------- 采集
def capture(conn, project_id: int, toolpath: Optional[Dict[str, Any]] = None,
            post: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把当前工程采集成快照字典（不落库）。"""
    doc = geometry_service.load_document(conn, project_id)
    rows = db.load_polylines(conn, project_id)
    raw_parts = db.load_parts(conn, project_id)
    placements = db.load_placements(conn, project_id)
    sheet = db.load_sheet(conn, project_id)
    params = clean_toolpath(toolpath)

    by_part: Dict[int, List[Dict[str, Any]]] = {}
    loose: List[Dict[str, Any]] = []
    point_count = 0
    for row in rows:
        point_count += len(row["pts"])
        item = {
            "layerId": int(row["layer_id"]),
            "closed": bool(row["closed"]),
            "isHole": bool(row["is_hole"]),
            "points": encode_points(row["pts"]),
        }
        if row["part_id"]:
            by_part.setdefault(int(row["part_id"]), []).append(item)
        else:
            loose.append(item)

    parts: List[Dict[str, Any]] = []
    for raw in raw_parts:
        items = by_part.get(int(raw["id"]), [])
        if not items:
            continue
        parts.append({
            "id": int(raw["id"]),
            "name": raw["name"],
            "source": raw.get("source") or "",
            "bbox": list(raw.get("bbox") or [0, 0, 0, 0]),
            "polylines": items,
        })

    tool = tool_service.get_tool(conn, project_id, int(params["toolId"])) \
        if params.get("toolId") else None
    template = tool_service.get_template(conn, project_id, int(params["templateId"])) \
        if params.get("templateId") else None

    poly_count = sum(len(p["polylines"]) for p in parts) + len(loose)

    used_area = 0.0
    if placements:
        state = layout_engine.load(conn, project_id)
        used_area = sum(part.area for part in state.parts if part.id in state.placements)
    sheet_area = max(1.0, float(sheet["width"]) * float(sheet["height"]))

    stats = {
        "partCount": len(parts),
        "polylineCount": poly_count,
        "pointCount": point_count,
        "layerCount": len(doc.layers),
        "placementCount": len(placements),
        "utilization": round(used_area / sheet_area, 4) if placements else 0.0,
        "sheetWidth": round(float(sheet["width"]), 3),
        "sheetHeight": round(float(sheet["height"]), 3),
    }

    return {
        "version": SNAPSHOT_VERSION,
        "capturedAt": time.time(),
        "sheet": sheet,
        "layers": [
            {"id": l.id, "name": l.name, "color": l.color,
             "operation": l.operation, "visible": l.visible, "locked": l.locked}
            for l in doc.layers
        ],
        "parts": parts,
        "loosePolylines": loose,
        "placements": list(placements.values()),
        "tool": tool,
        "template": template,
        "toolpath": params,
        "post": clean_post(post),
        "stats": stats,
    }


# ---------------------------------------------------------------- 行 <-> 对象
def _file_api(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": int(row["id"]),
        "orderId": int(row["order_id"]),
        "path": row["path"],
        "format": row["format"],
        "processor": row["processor"],
        "bytes": int(row["bytes"]),
        "sheetIndex": int(row["sheet_index"]),
        "createdAt": float(row["created_at"]),
    }


def _stats_of(row: Dict[str, Any]) -> Dict[str, Any]:
    try:
        stats = json.loads(row.get("stats") or "{}")
    except (TypeError, ValueError):
        stats = {}
    return stats if isinstance(stats, dict) else {}


def _summary(row: Dict[str, Any], file_count: Optional[int] = None) -> Dict[str, Any]:
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "code": row["code"] or "",
        "note": row["note"] or "",
        "createdAt": float(row["created_at"]),
        "updatedAt": float(row["updated_at"]),
        "stats": _stats_of(row),
        "fileCount": int(row.get("file_count") or file_count or 0),
    }


def _load_row(conn, project_id: int, order_id: int) -> Dict[str, Any]:
    row = db.load_work_order(conn, project_id, int(order_id))
    if row is None:
        raise WorkOrderError("工单 #%d 不存在" % int(order_id))
    return row


def _detail(conn, row: Dict[str, Any]) -> Dict[str, Any]:
    try:
        snapshot = json.loads(row["snapshot"])
    except (TypeError, ValueError):
        snapshot = {}
    files = [_file_api(item) for item in db.load_work_order_files(conn, int(row["id"]))]
    detail = _summary(row, file_count=len(files))
    detail.update({
        "version": int(snapshot.get("version") or SNAPSHOT_VERSION),
        "toolpath": snapshot.get("toolpath") or {},
        "post": snapshot.get("post") or {},
        "tool": snapshot.get("tool"),
        "template": snapshot.get("template"),
        "files": files,
        "missing": [],
    })
    return detail


def _name_of(data: Dict[str, Any]) -> str:
    name = str(data.get("name") or "").strip()
    if not name:
        raise WorkOrderError("工单名称不能为空")
    if len(name) > 64:
        raise WorkOrderError("工单名称不能超过 64 个字符")
    return name


# ---------------------------------------------------------------- 保存 / 检索
def create_order(conn, project_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
    name = _name_of(data)
    snapshot = capture(conn, project_id, data.get("toolpath"), data.get("post"))
    stats = snapshot["stats"]
    order_id = db.insert_work_order(
        conn, project_id, name, str(data.get("code") or "").strip(),
        str(data.get("note") or ""), json.dumps(snapshot, ensure_ascii=False),
        json.dumps(stats, ensure_ascii=False),
    )
    db.touch_project(conn, project_id)
    row = _load_row(conn, project_id, order_id)
    logger.info("工单已保存 #%d「%s」：%d 个零件 / %d 条轮廓",
                order_id, name, stats["partCount"], stats["polylineCount"])
    return _detail(conn, row)


def update_order(conn, project_id: int, order_id: int,
                 data: Dict[str, Any]) -> Dict[str, Any]:
    row = _load_row(conn, project_id, order_id)
    name = _name_of(data)
    old_snapshot = json.loads(row["snapshot"])
    if bool(data.get("recapture", False)):
        # 未显式给出的参数沿用存档，避免“重新拍快照”顺手把后处理清空
        toolpath = data.get("toolpath") or old_snapshot.get("toolpath")
        post = data.get("post") if data.get("post") is not None \
            else old_snapshot.get("post")
        snapshot = capture(conn, project_id, toolpath, post)
    else:
        snapshot = old_snapshot
        # 只改元信息时，刀路/后处理选择也允许顺手更新
        if data.get("toolpath"):
            snapshot["toolpath"] = clean_toolpath(data["toolpath"])
        if data.get("post") is not None:
            snapshot["post"] = clean_post(data["post"])
    stats = snapshot.get("stats") or _stats_of(row)
    db.update_work_order(
        conn, project_id, order_id, name, str(data.get("code") or "").strip(),
        str(data.get("note") or ""), json.dumps(snapshot, ensure_ascii=False),
        json.dumps(stats, ensure_ascii=False),
    )
    return _detail(conn, _load_row(conn, project_id, order_id))


def list_orders(conn, project_id: int, keyword: str = "",
                start: Optional[float] = None, end: Optional[float] = None,
                limit: int = 100) -> Dict[str, Any]:
    rows = db.list_work_orders(conn, project_id, keyword.strip(), start, end, limit)
    return {"orders": [_summary(row) for row in rows], "total": len(rows)}


def get_order(conn, project_id: int, order_id: int) -> Dict[str, Any]:
    return _detail(conn, _load_row(conn, project_id, order_id))


def list_files(conn, project_id: int, order_id: int) -> Dict[str, Any]:
    _load_row(conn, project_id, order_id)
    files = [_file_api(item) for item in db.load_work_order_files(conn, int(order_id))]
    return {"files": files, "total": len(files)}


def remove_order(conn, project_id: int, order_id: int) -> bool:
    _load_row(conn, project_id, order_id)
    return db.delete_work_order(conn, project_id, int(order_id))


def duplicate_order(conn, project_id: int, order_id: int,
                    name: str = "") -> Dict[str, Any]:
    """另存：复制快照，与来源工单完全独立。"""
    row = _load_row(conn, project_id, order_id)
    snapshot = json.loads(row["snapshot"])
    new_name = str(name or "").strip() or (str(row["name"]) + " 副本")
    new_name = new_name[:64]
    stats = snapshot.get("stats") or _stats_of(row)
    new_id = db.insert_work_order(
        conn, project_id, new_name, str(row["code"] or ""),
        str(row["note"] or ""), json.dumps(snapshot, ensure_ascii=False),
        json.dumps(stats, ensure_ascii=False),
    )
    return _detail(conn, _load_row(conn, project_id, new_id))


def add_export_files(conn, project_id: int, order_id: int,
                     files: List[Dict[str, Any]]) -> int:
    """导出成功后登记加工文件路径；工单被删则静默跳过（不阻断导出）。"""
    row = db.load_work_order(conn, project_id, int(order_id))
    if row is None:
        logger.warning("工单 #%s 不存在，跳过加工文件登记", order_id)
        return 0
    count = 0
    for item in files or []:
        path = str(item.get("path") or "")
        if not path:
            continue
        db.insert_work_order_file(
            conn, int(order_id), path, str(item.get("format") or ""),
            str(item.get("processor") or ""), int(item.get("bytes") or 0),
            int(item.get("sheetIndex") or 0),
        )
        count += 1
    return count


# ---------------------------------------------------------------- 打开（恢复）
def open_order(conn, project_id: int, order_id: int) -> Dict[str, Any]:
    """把快照写回当前工程：图形、图层、板材、摆料全部按快照重建。

    恢复是"整份替换"语义（与导图一致），旧工程图形会被清空；
    历史工单本身不受影响，可反复打开。
    """
    row = _load_row(conn, project_id, order_id)
    snapshot = json.loads(row["snapshot"])

    # 图层按名字对齐：同名图层复用，颜色/工艺随快照写回，轮廓 id 重映射
    layer_map: Dict[int, int] = {}
    default_layer: Optional[int] = None
    for layer in snapshot.get("layers") or []:
        name = str(layer.get("name") or "切割")
        new_id = db.ensure_layer(conn, project_id, name,
                                 int(layer.get("color") or 0x4FA3FF),
                                 str(layer.get("operation") or "cut"))
        conn.execute(
            "UPDATE layer SET visible = ?, locked = ? WHERE id = ?",
            (int(bool(layer.get("visible", True))), int(bool(layer.get("locked", False))),
             new_id),
        )
        layer_map[int(layer.get("id") or 0)] = new_id
        default_layer = default_layer or new_id
    if default_layer is None:
        default_layer = db.ensure_layer(conn, project_id, "切割")

    db.clear_document(conn, project_id)

    part_map: Dict[int, int] = {}
    order_map: Dict[int, int] = {}
    for index, part in enumerate(snapshot.get("parts") or []):
        bbox = list(part.get("bbox") or [0, 0, 0, 0])
        new_part = db.create_part(conn, project_id, str(part.get("name") or "零件"),
                                  str(part.get("source") or ""), bbox)
        items = []
        for poly in part.get("polylines") or []:
            items.append((
                layer_map.get(int(poly.get("layerId") or 0), default_layer),
                bool(poly.get("closed", True)),
                bool(poly.get("isHole", False)),
                decode_points(poly["points"]),
            ))
        db.save_part_polylines(conn, project_id, new_part, items)
        part_map[int(part.get("id") or index)] = new_part
        order_map[index] = new_part

    loose = []
    for poly in snapshot.get("loosePolylines") or []:
        loose.append((
            layer_map.get(int(poly.get("layerId") or 0), default_layer),
            bool(poly.get("closed", False)),
            decode_points(poly["points"]),
        ))
    if loose:
        db.save_polylines(conn, project_id, loose)

    sheet = snapshot.get("sheet") or {}
    if float(sheet.get("width") or 0) > 0 and float(sheet.get("height") or 0) > 0:
        db.save_sheet(conn, project_id, {
            "width": float(sheet["width"]), "height": float(sheet["height"]),
            "margin": float(sheet.get("margin", 10.0)),
            "gap": float(sheet.get("gap", 5.0)),
        })

    # 摆放：part_id 重映射；旧版本快照没存 part_id 时按采集顺序兜底
    for index, placement in enumerate(snapshot.get("placements") or []):
        new_id = part_map.get(int(placement.get("partId") or -1)) or order_map.get(index)
        if not new_id:
            continue
        db.save_placement(
            conn, project_id, new_id, int(placement.get("sheet") or 0),
            float(placement.get("tx") or 0.0), float(placement.get("ty") or 0.0),
            float(placement.get("rot") or 0.0), bool(placement.get("locked", False)),
            int(placement.get("zorder") or 0), bool(placement.get("fixedRot", False)),
        )

    db.touch_project(conn, project_id)

    detail = _detail(conn, row)
    toolpath = dict(detail["toolpath"] or {})
    missing: List[str] = []

    tool = detail.get("tool") or {}
    if tool.get("id"):
        current_tool = tool_service.get_tool(conn, project_id, int(tool["id"]))
        if current_tool is None:
            missing.append("刀具「%s」已不在刀具库，请重新选刀" % tool.get("name"))
            toolpath["toolId"] = None
        else:
            detail["tool"] = current_tool
    template = detail.get("template") or {}
    if template.get("id"):
        current_template = tool_service.get_template(conn, project_id, int(template["id"]))
        if current_template is None:
            missing.append("工艺模板「%s」已不存在" % template.get("name"))
            toolpath["templateId"] = None
        else:
            detail["template"] = current_template

    detail["toolpath"] = toolpath
    detail["missing"] = missing
    detail["stats"] = snapshot.get("stats") or detail["stats"]
    logger.info("工单 #%d「%s」已打开：%d 个零件 / %d 条摆放",
                order_id, row["name"], len(snapshot.get("parts") or []),
                len(snapshot.get("placements") or []))
    return detail
