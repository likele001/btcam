"""几何服务：文档装载 / 示例图形 / 偏置与布尔任务。

本模块是"图纸数据"的唯一入口，UI 拿到的图形全部经过这里编码成二进制下发。
"""

from __future__ import annotations

import logging
import math
from typing import List

import numpy as np

from app.models import db
from app.models.geometry import GeometryDoc, Layer, Polyline

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- 文档装载
def load_document(conn, project_id: int) -> GeometryDoc:
    layers = [
        Layer(
            id=row["id"], name=row["name"], color=row["color"],
            operation=row["operation"], visible=bool(row["visible"]), locked=bool(row["locked"]),
        )
        for row in db.load_layers(conn, project_id)
    ]
    polylines = [
        Polyline(row["id"], row["layer_id"], row["pts"], row["closed"],
                 row["is_hole"], row["part_id"] or 0)
        for row in db.load_polylines(conn, project_id)
    ]
    return GeometryDoc(layers=layers, polylines=polylines)


# ---------------------------------------------------------------- 示例图形
def build_demo_document() -> GeometryDoc:
    """生成一组典型板式家具轮廓，用于验证渲染/选中/排料/刀路全链路。"""
    polylines: List[Polyline] = []
    pid = 1

    def rect(x: float, y: float, w: float, h: float, layer: int) -> None:
        nonlocal pid
        pts = np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.float32)
        polylines.append(Polyline(pid, layer, pts, True))
        pid += 1

    def circle(cx: float, cy: float, r: float, layer: int, seg: int = 64) -> None:
        nonlocal pid
        angles = np.linspace(0, 2 * math.pi, seg, endpoint=False)
        pts = np.stack([cx + r * np.cos(angles), cy + r * np.sin(angles)], axis=1)
        polylines.append(Polyline(pid, layer, pts.astype(np.float32), True))
        pid += 1

    def star(cx: float, cy: float, r_out: float, r_in: float, points: int, layer: int) -> None:
        nonlocal pid
        angles = np.linspace(0, 2 * math.pi, points * 2, endpoint=False)
        radii = np.where(np.arange(points * 2) % 2 == 0, r_out, r_in)
        pts = np.stack([cx + radii * np.cos(angles), cy + radii * np.sin(angles)], axis=1)
        polylines.append(Polyline(pid, layer, pts.astype(np.float32), True))
        pid += 1

    # 外框 + 内孔（工艺孔）
    rect(0, 0, 600, 400, 1)
    circle(60, 60, 15, 3)
    circle(540, 60, 15, 3)
    circle(60, 340, 15, 3)
    circle(540, 340, 15, 3)

    # 待排料的三个零件
    rect(700, 0, 300, 200, 1)
    rect(700, 240, 180, 180, 1)
    star(1080, 120, 90, 40, 5, 2)
    circle(880, 330, 60, 1)

    return GeometryDoc(
        layers=[
            Layer(1, "切割", 0x4FA3FF, "cut"),
            Layer(2, "划槽", 0xFFB74F, "engrave"),
            Layer(3, "定位孔", 0x8BE38B, "drill"),
        ],
        polylines=polylines,
    )


def persist_document(project_id: int, doc: GeometryDoc) -> int:
    """把文档写回 SQLite（清空旧轮廓）。返回写入条数。

    注意：这是"整份替换"语义，会连同零件一起清空。
    示例图形与 DXF 导入共用同一套零件语义：排料/锁定/旋转都以零件为单位，
    所以这里也按包含关系拆一次零件，否则进排料模式会没有可拖拽的对象。
    """
    from app.geometry.repair import group_parts

    groups = group_parts(doc.polylines, "示例零件")
    with db.get_conn() as conn:
        db.clear_document(conn, project_id)
        count = 0
        for group in groups:
            part_id = db.create_part(conn, project_id, group.name, "demo",
                                     list(group.bbox()))
            items = [(p.layer_id, p.closed, p.is_hole, p.pts) for p in group.polylines]
            count += len(db.save_part_polylines(conn, project_id, part_id, items))
        db.touch_project(conn, project_id)
    return count


# ---------------------------------------------------------------- 任务
# 轮廓偏移已统一到 app.services.geometry_ops（模块 2 的几何基础服务），
# 这里不再保留第二套偏置实现，避免两条代码路径行为不一致。
