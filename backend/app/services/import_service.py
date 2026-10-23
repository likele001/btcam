"""DXF 导入服务（模块 1 的调度层）。

一条完整的导入链路（顺序不可调换）：

    读取(DXF -> 折线)  ->  修复(碎线/重叠/串接/闭合/内外/自相交)
      ->  分组(按包含关系拆零件)  ->  入库(part + polyline)  ->  推送(GEOMETRY_DOC / GEOMETRY_ISSUES)

线程模型：
- 整个任务跑在 TaskQueue 的工作线程里，全程同步（ezdxf / numpy 都是同步库）；
- 进度用 report(0~1, 文案) 推送，UI 直接驱动进度条；
- 大块几何用 hub.broadcast_threadsafe() 推送（会自动切回事件循环）；
- 修复后的轮廓先落库，再按库里的真实 id 下发 —— 保证前端拿到的 id 与后续
  刀路/排料/存档引用的是同一套 id。

前端只负责"选文件 + 上传二进制 + 画结果"，本模块不含任何前端逻辑。
"""

from __future__ import annotations

import logging
import os
import shutil
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from app.api import ws as ws_module
from app.core import protocol
from app.core.hub import hub
from app.core.log_store import log_store
from app.core.protocol import MsgType
from app.geometry.dxf_reader import DxfError, DxfReadOptions, read_dxf
from app.geometry.repair import (
    MAX_PARTS_PER_FILE,
    ContourIssue,
    PartGroup,
    RepairOptions,
    RepairStats,
    TopologyRepair,
    group_parts,
)
from app.models import db
from app.models.geometry import Layer, Polyline

logger = logging.getLogger(__name__)

# 任务类型标识：与 TASK_SUBMITTED / TASK_RESULT 的 kind 字段对应
TASK_KIND = "import"

# 校验异常类型 -> 中文名（日志与弹窗文案）
ISSUE_KIND_LABELS = {
    "self_intersection": "自相交",
    "open_contour": "未闭合",
}

# 每个文件的轮廓 id 偏移量：修复后轮廓 id 是 1..N（文件内唯一），
# 批量导入时必须加上文件序号前缀才能全局唯一，否则问题清单里的 id 会串台。
FILE_ID_STRIDE = 1_000_000


# ---------------------------------------------------------------- 小工具
def _check_cancel(report: Any) -> None:
    """任务取消检查：TaskQueue 的 reporter 有 check_cancelled，测试时可能没有。"""
    checker = getattr(report, "check_cancelled", None)
    if checker is not None:
        checker()


def _layer_interner() -> Tuple[Any, List[str], Dict[str, Layer]]:
    """返回一个 layer 名 -> 1..N 序号 的归一化函数。

    为什么要归一化：不同 DXF 的 layer_id 是各自文件内的编号，
    两个文件都有名为 "CUT" 的图层时必须映射到同一个项目图层，
    否则入库后会出现一堆同名图层。
    """
    names: List[str] = []
    meta: Dict[str, Layer] = {}

    def intern(layer: Layer) -> int:
        if layer.name not in meta:
            meta[layer.name] = layer
            names.append(layer.name)
        return names.index(layer.name) + 1

    return intern, names, meta


def encode_issue_segments(issues: List[ContourIssue]) -> bytes:
    """把异常线段编码成 BGC1 几何块（复用现有协议，前端可直接 decodePolylines 画红）。

    每条异常线段编码成一条 2 点开放折线，layer_id 固定 0（前端标红时不看图层）。
    """
    segments: List[Polyline] = []
    index = 1
    for issue in issues:
        if issue.segment is None:
            continue
        pts = np.asarray(issue.segment, dtype=np.float32).reshape(-1, 2)
        if len(pts) < 2:
            continue
        segments.append(Polyline(index, 0, pts, False))
        index += 1
    if not segments:
        return b""
    return protocol.encode_polylines(segments)


def _issue_meta(issues: List[ContourIssue], project_id: int) -> Dict[str, Any]:
    """异常明细（小 JSON）。坐标随 meta 一起走，前端弹窗里可直接显示位置。"""
    items = []
    for issue in issues:
        items.append({
            "kind": issue.kind,
            "message": issue.message,
            "polylineId": issue.polyline_id,
            "severity": issue.severity,
            "point": [round(float(issue.point[0]), 4), round(float(issue.point[1]), 4)],
        })
    return {"projectId": project_id, "count": len(items), "items": items}


# ---------------------------------------------------------------- 任务入口
def import_dxf_job(params: Dict[str, Any], report) -> Dict[str, Any]:
    """导入任务入口（TaskQueue 调用）。

    这里只包一层"无论如何都清理临时文件"，真正的流程在 _run_import。
    上传落盘的文件不清理会在 %LOCALAPPDATA% 里越积越多。
    """
    try:
        return _run_import(params, report)
    finally:
        if params.get("cleanup", True):
            cleanup_import_files([str(f.get("path") or "") for f in params.get("files") or []])


def _run_import(params: Dict[str, Any], report) -> Dict[str, Any]:
    """导入任务主体（在计算线程里执行）。

    入参 params：
        projectId  int      目标工程；不传取当前活跃工程
        files      list     待导入文件 [{"name": "面板.dxf", "path": "C:/tmp/x.dxf"}]
        read       dict     读取参数（见 DxfReadOptions.from_dict，camelCase）
        repair     dict     修复参数（见 RepairOptions.from_dict，camelCase）
        replace    bool     是否先清空当前工程图形（默认 True；False = 追加）
        splitParts bool     是否按包含关系拆成多个零件（默认 True）

    出参（随 TASK_RESULT 帧的 meta 下发）：
        projectId / fileCount / partCount / polylineCount / stats / files / warnings / failed
    """
    files = params.get("files") or []
    if not files:
        raise ValueError("没有待导入的文件")

    project_id = int(params.get("projectId") or 0)
    read_options = DxfReadOptions.from_dict(params.get("read"))
    repair_options = RepairOptions.from_dict(params.get("repair"))
    replace = bool(params.get("replace", True))
    split_parts = bool(params.get("splitParts", True))

    if not project_id:
        with db.get_conn() as conn:
            project_id = db.get_active_project_id(conn)

    intern_layer, layer_names, layer_meta = _layer_interner()
    collected: List[Tuple[str, str, List[Polyline]]] = []   # (零件名, 来源文件名, 轮廓)
    issues: List[ContourIssue] = []
    warnings: List[str] = []
    failed: List[Dict[str, str]] = []
    file_summaries: List[Dict[str, Any]] = []
    totals = RepairStats()

    total_files = len(files)
    report(0.02, "开始导入 %d 个文件" % total_files)

    for file_index, item in enumerate(files):
        _check_cancel(report)
        name = str(item.get("name") or os.path.basename(str(item.get("path") or "")))
        path = str(item.get("path") or "")
        stem = os.path.splitext(name)[0] or ("零件%d" % (file_index + 1))

        # 每个文件占一段进度区间：读取 60%，修复 30%，分组 10%
        base = 0.05 + 0.70 * file_index / total_files
        span = 0.70 / total_files

        def sub(ratio: float, message: str, _base: float = base, _span: float = span,
                _name: str = name) -> None:
            report(_base + _span * max(0.0, min(1.0, ratio)), "[%s] %s" % (_name, message))

        # ---- 1) 读取
        try:
            read = read_dxf(path, read_options, sub)
        except DxfError as exc:
            logger.warning("导入失败 %s: %s", name, exc)
            failed.append({"name": name, "error": str(exc)})
            warnings.append("%s：%s" % (name, exc))
            continue
        except Exception as exc:                     # 兜底：单个文件坏掉不拖垮整批
            logger.exception("导入异常 %s", name)
            failed.append({"name": name, "error": str(exc)})
            warnings.append("%s 读取异常：%s" % (name, exc))
            continue

        if not read.polylines:
            failed.append({"name": name, "error": "没有可用的 2D 轮廓"})
            warnings.extend("%s：%s" % (name, w) for w in read.warnings)
            continue

        # ---- 2) 修复
        _check_cancel(report)
        sub(0.62, "修复轮廓（输入 %d 条）" % len(read.polylines))
        repairer = TopologyRepair(repair_options)
        polylines, file_issues, stats = repairer.apply(read.polylines)

        # 轮廓 id 加文件前缀，保证跨文件唯一（问题清单里的 id 才对得上）
        offset = (file_index + 1) * FILE_ID_STRIDE
        for poly in polylines:
            poly.id += offset
        for issue in file_issues:
            issue.polyline_id += offset
        issues.extend(file_issues)

        # ---- 3) 图层归一 + 分组
        sub(0.9, "归组零件")
        layer_by_id = {l.id: l for l in read.layers}
        for poly in polylines:
            layer = layer_by_id.get(poly.layer_id)
            if layer is not None:
                poly.layer_id = intern_layer(layer)

        groups: List[PartGroup]
        if split_parts:
            groups = group_parts(polylines, stem)
            if len(groups) > MAX_PARTS_PER_FILE:
                warnings.append("%s 被拆出 %d 个零件，超过上限 %d，已合并为一个零件"
                                % (name, len(groups), MAX_PARTS_PER_FILE))
                groups = [PartGroup(name=stem, polylines=polylines)]
        else:
            groups = [PartGroup(name=stem, polylines=polylines)]

        for group in groups:
            collected.append((group.name, name, group.polylines))

        # 累加统计
        for field_name in ("inputContours", "inputSegments", "droppedTiny", "mergedContours",
                           "removedSegments", "closedContours", "openContours",
                           "outerContours", "holeContours", "issues"):
            setattr(totals, field_name, getattr(totals, field_name) + getattr(stats, field_name))

        warnings.extend("%s：%s" % (name, w) for w in read.warnings)
        file_summaries.append({
            "name": name,
            "parts": len(groups),
            "polylines": sum(len(g.polylines) for g in groups),
            "points": sum(p.point_count for g in groups for p in g.polylines),
            "stats": stats.as_dict(),
            "read": read.stats.as_dict(),
            "units": read.units,
            "unitScale": read.unitScale,
        })
        sub(1.0, "完成：%d 个零件 / %d 条轮廓" % (len(groups), len(polylines)))

    if not collected:
        detail = failed[0]["error"] if failed else "没有可用轮廓"
        raise ValueError("DXF 导入失败：%s" % detail)

    # ---- 4) 入库
    _check_cancel(report)
    report(0.80, "写入数据库（%d 个零件）" % len(collected))
    persist_info = _persist_parts(project_id, collected, layer_names, layer_meta, replace)

    # 问题清单里的 polyline_id 要换成库里的真实 id
    _remap_issue_ids(issues, persist_info["polylineIdMap"])

    # ---- 5) 推送
    report(0.92, "推送图形与告警")
    total_polylines = sum(len(polys) for _n, _s, polys in collected)
    _push_results(project_id, issues, {
        "importSummary": {
            "fileCount": total_files,
            "partCount": len(collected),
            "polylineCount": total_polylines,
        }
    })

    result: Dict[str, Any] = {
        "projectId": project_id,
        "fileCount": total_files,
        "partCount": len(collected),
        "polylineCount": total_polylines,
        "stats": totals.as_dict(),
        "files": file_summaries,
        "warnings": warnings,
        "failed": failed,
    }
    report(1.0, "导入完成：%d 个零件 / %d 条轮廓 / %d 条告警"
           % (len(collected), total_polylines, len(issues)))
    logger.info("DXF 导入完成 project=%s 文件=%d 零件=%d 轮廓=%d 告警=%d",
                project_id, total_files, len(collected), total_polylines, len(issues))
    return result


# ---------------------------------------------------------------- 入库
def _persist_parts(project_id: int,
                   collected: List[Tuple[str, str, List[Polyline]]],
                   layer_names: List[str],
                   layer_meta: Dict[str, Layer],
                   replace: bool) -> Dict[str, Any]:
    """把零件与轮廓写进 SQLite，返回 id 映射（供告警 id 校正）。

    一个事务写完：要么全部成功，要么全部回滚，不会留下半个工程。
    """
    polyline_id_map: Dict[int, int] = {}

    with db.get_conn() as conn:
        # 图层先落库（按名称复用已有图层，避免重复导入产生同名图层）
        layer_id_map: Dict[int, int] = {}
        for index, layer_name in enumerate(layer_names, start=1):
            meta = layer_meta[layer_name]
            layer_id_map[index] = db.ensure_layer(
                conn, project_id, layer_name, meta.color, meta.operation
            )

        if replace:
            db.clear_document(conn, project_id)

        created_parts: List[Dict[str, Any]] = []
        for part_name, source, polylines in collected:
            bbox = _bbox_of(polylines)
            part_id = db.create_part(conn, project_id, part_name, source, list(bbox))
            items = []
            for poly in polylines:
                items.append((
                    layer_id_map.get(poly.layer_id, 1),
                    poly.closed,
                    poly.is_hole,
                    poly.pts,
                ))
            ids = db.save_part_polylines(conn, project_id, part_id, items)
            for poly, db_id in zip(polylines, ids):
                polyline_id_map[poly.id] = db_id
            created_parts.append({"id": part_id, "name": part_name, "bbox": list(bbox)})

        db.touch_project(conn, project_id)

    return {"polylineIdMap": polyline_id_map, "parts": created_parts}


def _bbox_of(polylines: List[Polyline]) -> Tuple[float, float, float, float]:
    if not polylines:
        return (0.0, 0.0, 0.0, 0.0)
    boxes = np.array([p.bbox() for p in polylines], dtype=np.float64)
    return (float(boxes[:, 0].min()), float(boxes[:, 1].min()),
            float(boxes[:, 2].max()), float(boxes[:, 3].max()))


def _remap_issue_ids(issues: List[ContourIssue], id_map: Dict[int, int]) -> None:
    """把问题清单里的临时 id 换成数据库主键；找不到就保留原值（不致命）。"""
    for issue in issues:
        issue.polyline_id = int(id_map.get(issue.polyline_id, issue.polyline_id))


# ---------------------------------------------------------------- 推送
def _push_results(project_id: int, issues: List[ContourIssue],
                  extra_meta: Optional[Dict[str, Any]] = None) -> None:
    """推送全量图形 + 异常线段。

    顺序很重要：先推图形，再推告警 —— 前端收到告警时图形已经就位，
    标红层可以直接叠上去，不会出现"画布空白但有红色线段"的瞬态。
    """
    _pid, meta, payload = ws_module.build_document_frame(project_id, extra_meta)
    hub.broadcast_threadsafe(MsgType.GEOMETRY_DOC, meta, payload)

    if not issues:
        # 没有告警也要发一帧空清单：前端据此清掉上一次导入的红色标记
        hub.broadcast_threadsafe(MsgType.GEOMETRY_ISSUES,
                                 _issue_meta([], project_id), b"")
        return

    segment_payload = encode_issue_segments(issues)
    hub.broadcast_threadsafe(MsgType.GEOMETRY_ISSUES,
                             _issue_meta(issues, project_id), segment_payload)
    _alert_issues(issues)


def _alert_issues(issues: List[ContourIssue]) -> None:
    """校验告警写日志并推 ALERT 帧（前端弹窗）。

    画布标红只在用户看着画布时才有用；弹窗则保证“导入了带自相交的图”
    这件事不会被漏掉。warning 级别需要显式 alert 才推送，不会刷屏。
    """
    counts: Dict[str, int] = {}
    for issue in issues:
        counts[issue.kind] = counts.get(issue.kind, 0) + 1
    summary = "、".join(
        "%s %d 处" % (ISSUE_KIND_LABELS.get(kind, kind), count)
        for kind, count in counts.items()
    )
    detail = "\n".join(
        "轮廓 #%d · %s · (%.2f, %.2f) %s" % (
            issue.polyline_id,
            ISSUE_KIND_LABELS.get(issue.kind, issue.kind),
            float(issue.point[0]), float(issue.point[1]), issue.message,
        )
        for issue in issues[:20]
    )
    if len(issues) > 20:
        detail += "\n… 另有 %d 处未列出" % (len(issues) - 20)
    log_store.alert(
        "warning",
        "图形校验发现 %d 处异常（%s），已在画布标红" % (len(issues), summary),
        source="import", detail=detail[:2000], kind="geometry_check",
    )


def cleanup_import_files(paths: List[str]) -> None:
    """清理上传落地的临时文件（导入成功后调用，失败也只记日志）。"""
    for path in paths:
        try:
            folder = os.path.dirname(path)
            if folder and os.path.isdir(folder) and os.path.basename(folder).startswith("imp_"):
                shutil.rmtree(folder, ignore_errors=True)
            elif os.path.isfile(path):
                os.remove(path)
        except Exception as exc:              # 清理失败不能影响导入结果
            logger.warning("清理临时文件失败 %s: %s", path, exc)
