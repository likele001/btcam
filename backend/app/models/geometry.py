"""2D 几何基础数据模型。

设计约束：
- 只描述 2D 轮廓（Polyline），不引入任何 3D / 网格 / 浮雕概念；
- 点坐标统一使用 numpy float32 连续数组，便于零拷贝式二进制序列化；
- 单位统一为毫米（mm），坐标系为笛卡尔右手系（Y 轴向上）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import numpy as np

# 轮廓顶点数组类型：形状 (N, 2)，dtype = float32
PointsArray = np.ndarray


@dataclass
class Polyline:
    """一条 2D 折线 / 闭合轮廓。"""

    id: int
    layer_id: int
    pts: PointsArray
    closed: bool = False
    # 内孔标记：由导入修复阶段按包含关系判定（外轮廓=False，内孔=True）。
    # 刀路阶段据此决定补偿方向，渲染层可据此用虚线区分，避免重复计算包含关系。
    is_hole: bool = False
    # 所属零件 id（0 = 不属于任何零件，如工艺线/散线）。
    # 排料、锁定、旋转都以零件为操作单位，因此轮廓必须能反查到零件。
    part_id: int = 0

    # ---------- 构造 ----------
    @staticmethod
    def from_points(
        polyline_id: int,
        layer_id: int,
        points: Sequence[Sequence[float]],
        closed: bool = False,
    ) -> "Polyline":
        arr = np.asarray(points, dtype=np.float32).reshape(-1, 2)
        return Polyline(id=polyline_id, layer_id=layer_id, pts=arr, closed=closed)

    # ---------- 几何属性 ----------
    @property
    def point_count(self) -> int:
        return int(self.pts.shape[0])

    def bbox(self) -> Tuple[float, float, float, float]:
        """返回 (minx, miny, maxx, maxy)，空轮廓返回全 0。"""
        if self.point_count == 0:
            return (0.0, 0.0, 0.0, 0.0)
        return (
            float(self.pts[:, 0].min()),
            float(self.pts[:, 1].min()),
            float(self.pts[:, 0].max()),
            float(self.pts[:, 1].max()),
        )

    def area(self) -> float:
        """有向面积（鞋带公式）。闭合轮廓才有物理意义，逆时针为正。"""
        if self.point_count < 3:
            return 0.0
        x = self.pts[:, 0].astype(np.float64)
        y = self.pts[:, 1].astype(np.float64)
        return float(0.5 * np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))

    def perimeter(self) -> float:
        if self.point_count < 2:
            return 0.0
        if not self.closed:
            return float(np.sum(np.linalg.norm(np.diff(self.pts, axis=0), axis=1)))
        closed_pts = np.vstack([self.pts, self.pts[:1]])
        return float(np.sum(np.linalg.norm(np.diff(closed_pts, axis=0), axis=1)))

    def reversed_copy(self) -> "Polyline":
        """反转走向（用于统一轮廓方向）。"""
        return Polyline(self.id, self.layer_id, self.pts[::-1].copy(), self.closed,
                        self.is_hole, self.part_id)


@dataclass
class Part:
    """一个可独立摆料的零件（一次 DXF 文件导入 = 一个零件）。

    为什么把零件和轮廓分开：
    - 手动排料/自动排料操作的是零件整体（平移、旋转、锁定），不是单根轮廓；
    - 原始轮廓永远保存后端，前端只拿变换矩阵，避免几何数据前后端不一致。
    """

    id: int
    name: str
    source: str = ""                       # 来源文件名（历史工单回溯用）
    bbox: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    polyline_ids: List[int] = field(default_factory=list)


@dataclass
class Layer:
    """图层：CAM 中通常按加工工艺划分（切割层 / 划槽层 / 定位孔层）。"""

    id: int
    name: str
    color: int = 0x4FA3FF          # 0xRRGGBB
    operation: str = "cut"          # cut | engrave | drill
    visible: bool = True
    locked: bool = False


@dataclass
class GeometryDoc:
    """一整份图形文档（一次全量同步的最小单位）。"""

    layers: List[Layer] = field(default_factory=list)
    polylines: List[Polyline] = field(default_factory=list)

    def find_layer(self, layer_id: int) -> Optional[Layer]:
        return next((l for l in self.layers if l.id == layer_id), None)

    def bbox(self) -> Tuple[float, float, float, float]:
        """整份文档包围盒；空文档返回单位框。"""
        if not self.polylines:
            return (0.0, 0.0, 100.0, 100.0)
        boxes = np.array([p.bbox() for p in self.polylines], dtype=np.float64)
        return (
            float(boxes[:, 0].min()),
            float(boxes[:, 1].min()),
            float(boxes[:, 2].max()),
            float(boxes[:, 3].max()),
        )
