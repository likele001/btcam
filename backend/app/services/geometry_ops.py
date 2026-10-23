"""OCCT 2D 几何基础封装服务（模块 2）。

定位：把底层几何能力收成一个类，业务层（刀路/排料/前端）只跟它打交道。
对外只吃/吐项目统一的 `Polyline`，**不泄漏任何 OCCT 类型**，
前端拿到的永远是 BGC1 二进制点阵或小 JSON。

本层承担的职责（不是简单转发引擎）：
1. 刀具补偿语义换算：side=outside/inside/auto + is_hole → 引擎的偏移符号；
2. 参数校验与逐条容错：单条轮廓失败不影响整批；
3. 结果 id 分段重写，避免与图纸原有主键冲突；
4. 引擎降级（OCCT → pyclipper → numpy）与资源统计；
5. OCCT Handle 释放：每次运算走 `ReleaseScope`，结束即释放并 gc。

只做 2D：不使用任何 BRep 实体/曲面/网格建模能力。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from app.config import settings
from app.geometry.base import (
    JOIN_ROUND,
    GeometryEngine,
    chain_polylines,
    ensure_ccw,
    ensure_cw,
    get_engine,
)
from app.geometry.repair import ContourIssue, find_self_intersections
from app.models.geometry import Polyline

logger = logging.getLogger(__name__)

# 输出轮廓的 id 分段：不占用图纸原有主键，前端一看就知道是"计算产物"
OFFSET_ID_BASE = 5_000_000
INTERSECT_ID_BASE = 6_000_000
MERGE_ID_BASE = 7_000_000

# 刀具补偿方向
SIDE_OUTSIDE = "outside"
SIDE_INSIDE = "inside"
SIDE_AUTO = "auto"


# ---------------------------------------------------------------- 结果结构
@dataclass
class BBox:
    """二维包围盒（mm）。"""

    xmin: float
    ymin: float
    xmax: float
    ymax: float
    contourCount: int = 0
    pointCount: int = 0

    @property
    def width(self) -> float:
        return self.xmax - self.xmin

    @property
    def height(self) -> float:
        return self.ymax - self.ymin

    def as_dict(self) -> Dict[str, float]:
        return {
            "xmin": round(self.xmin, 4),
            "ymin": round(self.ymin, 4),
            "xmax": round(self.xmax, 4),
            "ymax": round(self.ymax, 4),
            "width": round(self.width, 4),
            "height": round(self.height, 4),
            "centerX": round(self.xmin + self.width * 0.5, 4),
            "centerY": round(self.ymin + self.height * 0.5, 4),
            "contourCount": self.contourCount,
            "pointCount": self.pointCount,
        }


@dataclass
class ClosureInfo:
    """单条轮廓的闭合校验结果。"""

    polylineId: int
    closed: bool           # 数据上的闭合标志
    ok: bool               # 实际首尾是否接上（closed 或 gap <= tol）
    gap: float             # 首尾距离 mm
    isHole: bool
    pointCount: int
    area: float            # 有向面积：外轮廓正、内孔负
    perimeter: float

    def as_dict(self) -> Dict[str, Any]:
        return {
            "polylineId": self.polylineId,
            "closed": self.closed,
            "ok": self.ok,
            "gap": round(self.gap, 4),
            "isHole": self.isHole,
            "pointCount": self.pointCount,
            "area": round(self.area, 4),
            "perimeter": round(self.perimeter, 4),
        }


# ---------------------------------------------------------------- 转换工具
def polylines_from_payload(items: Sequence[Dict[str, Any]]) -> List[Polyline]:
    """把接口传入的轮廓 dict 转成 Polyline。

    同时容忍 camelCase 与 snake_case：前端统一用 camelCase，
    但任务参数在内部流转时可能沿用数据层命名。
    非法项（点太少 / 结构不对）直接跳过，不抛异常打断整批。
    """
    result: List[Polyline] = []
    for raw in items or []:
        try:
            pts = np.asarray(raw.get("pts"), dtype=np.float32)
        except (TypeError, ValueError):
            continue
        if pts.ndim != 2 or pts.shape[0] < 2 or pts.shape[1] != 2:
            continue
        result.append(Polyline(
            int(raw.get("id", 0) or 0),
            int(raw.get("layerId", raw.get("layer_id", 1)) or 1),
            pts,
            bool(raw.get("closed", False)),
            bool(raw.get("isHole", raw.get("is_hole", False))),
            int(raw.get("partId", raw.get("part_id", 0)) or 0),
        ))
    return result


def polylines_to_payload(polylines: Sequence[Polyline]) -> List[Dict[str, Any]]:
    """轮廓转成任务参数格式（numpy 可直接进线程池，无需 tolist 拷贝）。"""
    return [
        {
            "id": p.id,
            "layerId": p.layer_id,
            "closed": p.closed,
            "isHole": p.is_hole,
            "partId": p.part_id,
            "pts": p.pts,
        }
        for p in polylines
    ]


def _perimeter(pts: np.ndarray, closed: bool) -> float:
    """周长：闭合轮廓补上首尾段。"""
    if len(pts) < 2:
        return 0.0
    total = float(np.linalg.norm(np.diff(pts, axis=0), axis=1).sum())
    if closed and len(pts) > 2:
        total += float(np.linalg.norm(pts[-1] - pts[0]))
    return total


# ---------------------------------------------------------------- 服务类
class GeometryOps:
    """2D 几何基础服务（轮廓偏移 / 求交 / 包围盒 / 自相交 / 闭合校验 / 断线合并）。

    线程安全：内部只持有无状态的引擎对象与计数，可在计算线程池里并发调用；
    引擎本身不做缓存，每次运算都是独立对象，天然无共享状态。
    """

    def __init__(self, preferred: Optional[str] = None, deflection: float = 0.02) -> None:
        self._preferred = preferred or settings.geometry_engine
        self._engine: Optional[GeometryEngine] = None
        self._deflection = deflection
        self._lock = threading.Lock()
        # 业务侧计数（引擎自己的 OCCT 计数通过 resource_stats 取）
        self._counters: Dict[str, int] = {
            "offset": 0, "intersect": 0, "bbox": 0,
            "selfIntersections": 0, "closure": 0, "merge": 0, "failures": 0,
        }

    # ------------------------------------------------------------ 引擎
    @property
    def engine(self) -> GeometryEngine:
        """延迟创建引擎：导入 app 时不触碰 OCCT，避免拖慢启动。"""
        if self._engine is None:
            with self._lock:
                if self._engine is None:
                    self._engine = get_engine(self._preferred)
        return self._engine

    def backend(self) -> str:
        return self.engine.name

    def available(self) -> bool:
        return self.engine.available()

    def info(self) -> Dict[str, Any]:
        """引擎/资源快照，给 /api/geometry/info 与测试脚本用。"""
        engine = self.engine
        resource: Dict[str, Any] = {}
        stats_fn = getattr(engine, "resource_stats", None)
        if callable(stats_fn):
            resource = stats_fn()
        return {
            "backend": engine.name,
            "available": engine.available(),
            "occt": engine.name == "occt",
            "deflection": self._deflection,
            "counters": dict(self._counters),
            "resource": resource,
        }

    def _bump(self, name: str, value: int = 1) -> None:
        self._counters[name] = self._counters.get(name, 0) + value

    # ------------------------------------------------------------ 1. 偏移
    @staticmethod
    def _offset_sign(poly: Polyline, side: str) -> int:
        """把刀具补偿方向换算成引擎的偏移符号。

        引擎约定：正距离 = 沿逆时针外法向偏移（闭合轮廓即"向外"）。
        - outside：外轮廓向外（+），内孔也向外（孔变小）
        - inside ：反方向（零件变小 / 孔变大）
        - auto   ：刀具始终走在材料外侧 → 外轮廓 +，内孔 -
        """
        if side == SIDE_OUTSIDE:
            return 1
        if side == SIDE_INSIDE:
            return -1
        if not poly.closed:
            return 1                    # 开放轮廓（划线/雕刻）没有内外之分
        return -1 if poly.is_hole else 1

    def offset(self, polylines: Sequence[Polyline], distance: float,
               join: str = JOIN_ROUND, side: str = SIDE_AUTO) -> List[Polyline]:
        """轮廓偏移（刀具补偿）。

        入参：
            polylines -- 输入轮廓，单位 mm
            distance  -- 补偿量，必须 > 0
            join      -- 拐角方式：round / miter / square
            side      -- outside / inside / auto（见 _offset_sign）

        出参：
            偏移后的轮廓列表（id 从 OFFSET_ID_BASE 起，layer_id 沿用输入）

        异常：
            ValueError -- 距离非法或没有可偏移的轮廓（由接口层转 400）
        """
        items = [p for p in polylines if p.point_count >= 2]
        if not items:
            raise ValueError("没有可偏移的轮廓")
        delta = float(distance)
        if not (delta > 0):
            raise ValueError("偏移距离必须大于 0（方向由 side 决定）")

        # 按（图层, 符号）分组，一组一次调用引擎：
        # - 引擎只回吐点串，不保留来源语义，混在一起算就只能给全部结果套同一个
        #   图层，偏移后画布会整片变色；按图层分组才能把颜色/工艺属性带过去；
        # - 又不能逐条调用 —— OCCT 每次都要建作用域，1000 条会产生 1000 次 gc。
        # 同时按项目约定归一走向：外 CCW（面积为正）/ 内 CW（面积为负），
        # 否则前端按面积正负判断内外会得到相反结论。
        groups: Dict[int, List[Polyline]] = {}
        for poly in items:
            groups.setdefault(poly.layer_id, []).append(poly)

        result: List[Polyline] = []
        for layer_id, group in groups.items():
            for sign in (1, -1):
                subset = [p for p in group if self._offset_sign(p, side) == sign]
                if not subset:
                    continue
                # 负向组必然来自内孔，正向组必然是外轮廓/开放线
                normalize = ensure_ccw if sign > 0 else ensure_cw
                for poly in self.engine.offset(subset, delta * sign, join):
                    poly.pts = normalize(np.asarray(poly.pts, dtype=np.float32))
                    poly.is_hole = sign < 0
                    poly.layer_id = layer_id
                    result.append(poly)

        for index, poly in enumerate(result):
            poly.id = OFFSET_ID_BASE + index
        self._bump("offset")
        return result

    # ------------------------------------------------------------ 2. 求交
    def intersect(self, subject: Sequence[Polyline],
                  clip: Sequence[Polyline]) -> List[Polyline]:
        """轮廓求交：返回两组轮廓公共区域的边界。

        入参：
            subject -- 被求交的轮廓组（只取闭合轮廓）
            clip    -- 用来切的轮廓组（只取闭合轮廓）
        出参：
            交集轮廓（id 从 INTERSECT_ID_BASE 起）；没有交集返回空列表
        """
        result = self.engine.intersect(subject, clip)
        for index, poly in enumerate(result):
            poly.id = INTERSECT_ID_BASE + index
        self._bump("intersect")
        return result

    # ------------------------------------------------------------ 3. 包围盒
    def bbox(self, polylines: Sequence[Polyline]) -> BBox:
        """轮廓包围盒（纯 numpy，不需要 OCCT）。

        异常：ValueError -- 没有有效轮廓
        """
        arrays = [np.asarray(p.pts, dtype=np.float64)
                  for p in polylines if p.point_count >= 1]
        if not arrays:
            raise ValueError("没有可计算包围盒的轮廓")
        all_pts = np.vstack(arrays)
        self._bump("bbox")
        return BBox(
            xmin=float(all_pts[:, 0].min()),
            ymin=float(all_pts[:, 1].min()),
            xmax=float(all_pts[:, 0].max()),
            ymax=float(all_pts[:, 1].max()),
            contourCount=len(arrays),
            pointCount=int(all_pts.shape[0]),
        )

    # ------------------------------------------------------------ 4. 自相交
    def self_intersections(self, polylines: Sequence[Polyline]) -> List[ContourIssue]:
        """逐条检查自相交。

        复用模块 1 的网格分桶线段求交（纯 numpy，不依赖 OCCT），
        返回的 issue 里带交点与异常线段，前端可直接标红。
        """
        issues: List[ContourIssue] = []
        for poly in polylines:
            for point, segment in find_self_intersections(poly):
                issues.append(ContourIssue(
                    kind="self_intersection",
                    message="轮廓 %s 自相交于 (%.2f, %.2f)" % (
                        poly.id, float(point[0]), float(point[1])),
                    polyline_id=poly.id,
                    severity="error",
                    point=(float(point[0]), float(point[1])),
                    segment=segment,
                ))
        self._bump("selfIntersections")
        return issues

    # ------------------------------------------------------------ 5. 闭合校验
    def closure_report(self, polylines: Sequence[Polyline],
                       tol: float = 0.05) -> List[ClosureInfo]:
        """闭合校验：给出每条轮廓的首尾缺口与是否真正闭合。

        项目约定：closed=True 表示逻辑闭合，顶点序列不重复首点，
        首尾之间由一条隐含边连接，因此缺口恒为 0；
        只有 closed=False 的开放线才需要按实际距离判断能不能当闭合用。
        """
        tolerance = max(1e-6, float(tol))
        infos: List[ClosureInfo] = []
        for poly in polylines:
            pts = np.asarray(poly.pts, dtype=np.float64)
            if poly.closed or len(pts) < 2:
                gap = 0.0
            else:
                gap = float(np.linalg.norm(pts[0] - pts[-1]))
            infos.append(ClosureInfo(
                polylineId=poly.id,
                closed=bool(poly.closed),
                ok=bool(poly.closed) or gap <= tolerance,
                gap=gap,
                isHole=bool(poly.is_hole),
                pointCount=poly.point_count,
                area=float(poly.area()),
                perimeter=_perimeter(pts, bool(poly.closed)),
            ))
        self._bump("closure")
        return infos

    # ------------------------------------------------------------ 6. 断线合并
    def merge_gaps(self, polylines: Sequence[Polyline],
                   tol: float = 0.2) -> List[Polyline]:
        """断线合并：把端点相接的碎片串成连续轮廓。

        与导入时的拓扑修复不同，这里只做"串接"，不做去重/去碎/内外分类 ——
        它是刀路计算前的一道轻量预处理，输入已经是可用轮廓。
        """
        tolerance = max(1e-6, float(tol))
        merged = chain_polylines(polylines, tol=tolerance)
        for index, poly in enumerate(merged):
            poly.id = MERGE_ID_BASE + index
        self._bump("merge")
        return merged


# ---------------------------------------------------------------- 单例
_ops: Optional[GeometryOps] = None
_ops_lock = threading.Lock()


def get_ops() -> GeometryOps:
    """进程内单例：OCCT 绑定加载一次就够，反复 new 会重复构造引擎。"""
    global _ops
    with _ops_lock:
        if _ops is None:
            _ops = GeometryOps()
        return _ops


# ---------------------------------------------------------------- 异步任务
def geometry_job(params: Dict[str, Any], report) -> Dict[str, Any]:
    """几何运算任务（offset / intersect / merge）。

    入参 params：
        op        -- offset | intersect | merge
        contours  -- 主轮廓列表 [{id, layerId, closed, isHole, pts}]
        clip      -- 求交时的第二组轮廓（仅 intersect 用）
        distance  -- 偏移量 mm（仅 offset 用）
        join      -- round | miter | square（仅 offset 用）
        side      -- outside | inside | auto（仅 offset 用）
        tol       -- 串接容差 mm（仅 merge 用）

    出参（经 TASK_RESULT 下发，polylines 走 BGC1 二进制块）：
        {"polylines": [...], "stats": {...}}

    异常：
        参数缺失 / 距离非法 → ValueError，任务队列会转成 TASK_FAILED 帧
    """
    op = str(params.get("op") or "").strip().lower()
    ops = get_ops()
    contours = polylines_from_payload(params.get("contours") or [])
    if not contours:
        raise ValueError("没有可用轮廓")

    report(0.1, "准备 %d 条轮廓（引擎 %s）" % (len(contours), ops.backend()))

    if op == "offset":
        result = ops.offset(
            contours,
            float(params.get("distance", 1.0)),
            str(params.get("join") or JOIN_ROUND),
            str(params.get("side") or SIDE_AUTO),
        )
        report(0.9, "偏移完成：%d 条 → %d 条" % (len(contours), len(result)))
        return {
            "polylines": result,
            "stats": {
                "op": op, "engine": ops.backend(),
                "inCount": len(contours), "outCount": len(result),
                "distance": float(params.get("distance", 1.0)),
                "side": str(params.get("side") or SIDE_AUTO),
            },
        }

    if op == "intersect":
        clip = polylines_from_payload(params.get("clip") or [])
        if not clip:
            raise ValueError("求交缺少第二组轮廓 clip")
        result = ops.intersect(contours, clip)
        report(0.9, "求交完成：%d 条" % len(result))
        return {
            "polylines": result,
            "stats": {
                "op": op, "engine": ops.backend(),
                "subjectCount": len(contours), "clipCount": len(clip),
                "outCount": len(result),
            },
        }

    if op == "merge":
        result = ops.merge_gaps(contours, float(params.get("tol", 0.2)))
        report(0.9, "串接完成：%d 条 → %d 条" % (len(contours), len(result)))
        return {
            "polylines": result,
            "stats": {
                "op": op, "engine": ops.backend(),
                "inCount": len(contours), "outCount": len(result),
                "tol": float(params.get("tol", 0.2)),
            },
        }

    raise ValueError("不支持的几何运算：%s" % (op or "(空)"))


def issue_to_payload(issue: ContourIssue) -> Dict[str, Any]:
    """把告警转成接口 JSON（与 GEOMETRY_ISSUES 的 meta 同构）。"""
    return {
        "kind": issue.kind,
        "message": issue.message,
        "polylineId": issue.polyline_id,
        "severity": issue.severity,
        "point": [round(issue.point[0], 4), round(issue.point[1], 4)],
    }
