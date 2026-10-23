"""2D 轮廓刀路生成服务（模块 5，纯 2D，不含任何 3D 浮雕逻辑）。

工艺流程：

    原始轮廓 --分类(外轮廓/内孔)--> 刀具半径补偿 --> 方向/起割点 --> 微连接拆分
             --> 路径排序 --> 每个 Z 层生成运动段 --> 预览 / 导出

关键约束：
- 原始零件轮廓只读，补偿结果全部是新数组，绝不回写数据库；
- 外轮廓默认向外补偿，内孔默认向内补偿（side=auto），也可显式覆盖；
- 全部几何计算在后端完成，前端只拿 BGC1 二进制折线渲染。

坐标约定：单位 mm，板左下角为原点，Y 向上；刀路 Z 向下为负。
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from app.config import settings
from app.geometry.base import chain_polylines, get_engine
from app.models.geometry import Polyline
from app.services import tool_service

logger = logging.getLogger(__name__)

# 预览用的辅助图层号（与业务图层区分开）
LAYER_CUT = 9001
LAYER_RAPID = 9002
LAYER_START = 9003

# 预览折线 id 段：切削沿用原始轮廓 id（便于前端点选起割点），
# 快移/起割点用独立号段，避免与图纸（1..N）和排料（200000+）冲突。
RAPID_POLY_BASE = 10_000_000
START_POLY_BASE = 20_000_000
EPS = 1e-9


# ---------------------------------------------------------------- 基本结构
@dataclass
class Move:
    """一段刀位运动。kind: rapid(快移) | plunge(下刀) | cut(切削) | retract(抬刀)"""

    x: float
    y: float
    z: float
    kind: str = "cut"
    feed: float = 0.0
    # 所属原始轮廓 id：预览时切削折线用它当 id，前端点选起割点可直接带回
    contour_id: int = 0


@dataclass
class _PathItem:
    """一条待加工轮廓（补偿后的中间态）。"""

    poly_id: int
    layer_id: int
    part_id: int
    is_hole: bool
    closed: bool
    side: str
    points: np.ndarray                 # 闭合轮廓首尾重合
    # 以下字段在生成阶段回填，用于 meta / 统计
    start_xy: Tuple[float, float] = (0.0, 0.0)
    segment_count: int = 0
    bridge_count: int = 0
    cut_length: float = 0.0

    @property
    def unique(self) -> np.ndarray:
        """去掉闭合重复点，便于找最近点/旋转起点。"""
        if self.closed and len(self.points) > 1:
            return self.points[:-1]
        return self.points


@dataclass
class ToolpathPlan:
    """一次刀路计算的全部结果。"""

    moves: List[Move] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)
    contours: List[Dict[str, Any]] = field(default_factory=list)
    bridges: List[Dict[str, Any]] = field(default_factory=list)
    start_points: List[Dict[str, float]] = field(default_factory=list)
    marker_radius: float = 1.6

    def polyline_preview(self) -> List[Polyline]:
        """把刀路投影成 2D 折线，供 PixiJS 直接渲染。

        切削层：一条连续切入→循边→抬刀记为一条折线，id 沿用原始轮廓 id；
        快移层：抬刀/空走路径（前端用虚线画）；
        起割点层：每个起割点画一个小菱形标记。
        """
        out: List[Polyline] = []
        cut_run: List[Tuple[float, float]] = []
        rapid_run: List[Tuple[float, float]] = []
        cut_id = 1
        sequence = 1

        def flush_cut() -> None:
            nonlocal cut_run
            if len(cut_run) >= 2:
                out.append(Polyline(cut_id, LAYER_CUT,
                                    np.asarray(cut_run, dtype=np.float32), False))
            cut_run = []

        def flush_rapid() -> None:
            nonlocal rapid_run, sequence
            if len(rapid_run) >= 2:
                out.append(Polyline(RAPID_POLY_BASE + sequence, LAYER_RAPID,
                                    np.asarray(rapid_run, dtype=np.float32), False))
                sequence += 1
            rapid_run = []

        for mv in self.moves:
            if mv.kind == "cut":
                if rapid_run:
                    flush_rapid()
                if cut_run and mv.contour_id != cut_id:
                    # 理论上不会跨轮廓连续切削，保险起见断开
                    flush_cut()
                cut_id = mv.contour_id or cut_id
                cut_run.append((mv.x, mv.y))
            elif mv.kind in ("rapid", "retract"):
                if cut_run:
                    # 抬刀点与最后切削点同 XY，补进切削段便于看到完整收尾
                    cut_run.append((mv.x, mv.y))
                    flush_cut()
                rapid_run.append((mv.x, mv.y))
        if cut_run:
            flush_cut()
        if rapid_run:
            flush_rapid()

        # 起割点：小菱形（闭合折线），颜色由前端图层控制
        radius = max(0.8, float(self.marker_radius))
        for index, start in enumerate(self.start_points):
            x, y = float(start["x"]), float(start["y"])
            diamond = np.asarray([[x, y + radius], [x + radius, y],
                                  [x, y - radius], [x - radius, y]],
                                 dtype=np.float32)
            out.append(Polyline(START_POLY_BASE + index, LAYER_START, diamond, True))
        return out


# ---------------------------------------------------------------- 轮廓预处理
def _normalize_contours(raw: List[Dict[str, Any]]) -> List[_PathItem]:
    """把请求里的轮廓字典转成中间结构；开放线先串接，闭合轮廓原样保留。"""
    polylines: List[Polyline] = []
    for i, item in enumerate(raw):
        pts = np.asarray(item.get("pts", []), dtype=np.float32).reshape(-1, 2)
        if len(pts) < 2:
            continue
        polylines.append(Polyline(
            int(item.get("id", i + 1)),
            int(item.get("layerId", 1)),
            pts,
            bool(item.get("closed", False)),
            bool(item.get("isHole", False)),
            int(item.get("partId", 0) or 0),
        ))
    if not polylines:
        return []

    merged = chain_polylines(polylines, tol=settings.geom_tolerance * 5)
    items: List[_PathItem] = []
    for poly in merged:
        if poly.point_count < 2:
            continue
        items.append(_PathItem(
            poly_id=poly.id,
            layer_id=poly.layer_id,
            part_id=poly.part_id,
            is_hole=poly.is_hole,
            closed=poly.closed,
            side="on",
            points=np.asarray(poly.pts, dtype=np.float64).copy(),
        ))
    return items


def _signed_area(pts: np.ndarray) -> float:
    """鞋带公式，逆时针为正。"""
    if len(pts) < 3:
        return 0.0
    x, y = pts[:, 0], pts[:, 1]
    return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def _point_in_polygon(point: np.ndarray, polygon: np.ndarray) -> bool:
    """射线法判定点是否在多边形内部（边界上按内部处理）。"""
    if len(polygon) < 3:
        return False
    x, y = float(point[0]), float(point[1])
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi, yi = float(polygon[i][0]), float(polygon[i][1])
        xj, yj = float(polygon[j][0]), float(polygon[j][1])
        # 点恰好落在顶点/边上：视为内部，避免嵌套孔被漏判
        if (abs(xi - x) < 1e-6 and abs(yi - y) < 1e-6):
            return True
        if ((yi > y) != (yj > y)):
            cross = (xj - xi) * (y - yi) / (yj - yi + 1e-30) + xi
            if x < cross:
                inside = not inside
        j = i
    return inside


def _infer_holes(items: List[_PathItem]) -> None:
    """补齐内孔标记：按“被更大闭合轮廓包住”判定。

    导入修复阶段本应写入 isHole；这里再兜一次底，兼容旧库/三方数据，
    保证 side=auto 时补偿方向不会整体反掉。
    """
    closed = [item for item in items if item.closed and len(item.unique) >= 3]
    if not closed:
        return
    grouped: Dict[int, List[_PathItem]] = {}
    for item in closed:
        grouped.setdefault(item.part_id, []).append(item)
    for group in grouped.values():
        ordered = sorted(group, key=lambda it: abs(_signed_area(it.unique)), reverse=True)
        for index, item in enumerate(ordered):
            if item.is_hole:
                continue
            point = item.unique[0]
            for outer in ordered[:index]:
                if _point_in_polygon(point, outer.unique):
                    item.is_hole = True
                    break


def _select_contours(items: List[_PathItem], cut_type: str) -> List[_PathItem]:
    """按刀路类型筛选轮廓。"""
    if cut_type == "outer":
        return [item for item in items if item.closed and not item.is_hole]
    if cut_type == "inner":
        return [item for item in items if item.closed and item.is_hole]
    return items


def _compensate(items: List[_PathItem], tool_diameter: float,
                side: str) -> Tuple[List[_PathItem], List[Dict[str, Any]]]:
    """逐条做刀具半径补偿，并保留 is_hole / part_id 关联。

    逐条调用引擎（而不是整批）是为了让偏置结果能准确回挂到原轮廓；
    轮廓数量在单板场景只有几十条，这点开销可以接受。
    """
    radius = max(0.0, float(tool_diameter)) / 2.0
    engine = get_engine(settings.geometry_engine)
    result: List[_PathItem] = []
    skipped: List[Dict[str, Any]] = []

    for item in items:
        if not item.closed:
            # 开放轮廓只能走中心线，不做半径补偿（避免出现“带状区域”）
            effective = "on"
        elif side == "auto":
            effective = "inside" if item.is_hole else "outside"
        else:
            effective = side

        distance = 0.0
        if item.closed and effective != "on" and radius > 0:
            distance = radius if effective == "outside" else -radius

        if abs(distance) < EPS:
            item.side = effective
            if item.closed:
                item.points = np.vstack([item.points, item.points[:1]])
            result.append(item)
            continue

        source = Polyline(item.poly_id, item.layer_id, item.points,
                          item.closed, item.is_hole, item.part_id)
        try:
            pieces = engine.offset([source], distance)
        except Exception as exc:              # 单条失败不拖垮整张板
            logger.warning("轮廓 %s 补偿失败：%s", item.poly_id, exc)
            skipped.append({"id": item.poly_id, "reason": str(exc)})
            continue

        if not pieces:
            skipped.append({"id": item.poly_id, "reason": "补偿后轮廓为空（刀具过大或轮廓过小）"})
            continue

        for piece in pieces:
            pts = np.asarray(piece.pts, dtype=np.float64)
            if len(pts) < 2:
                continue
            if item.closed:
                if len(pts) < 3:
                    continue
                # 统一成首尾重合，后续切片/微连接都按闭合路径处理
                pts = np.vstack([pts, pts[:1]])
            result.append(_PathItem(
                poly_id=item.poly_id,
                layer_id=item.layer_id,
                part_id=item.part_id,
                is_hole=item.is_hole,
                closed=item.closed,
                side=effective,
                points=pts,
            ))
    return result, skipped


def _enforce_direction(item: _PathItem, direction: str) -> None:
    """按用户选择重排点序（只对闭合轮廓有效）。

    ccw/cw 是强制点序；climb/conventional 按顺铣/逆铣语义映射到内外轮廓，
    映射关系集中在 tool_service.direction_for，刀路服务只负责执行。
    """
    if not item.closed:
        return
    want_ccw = tool_service.direction_for(direction, item.is_hole)
    if want_ccw is None:
        return
    area = _signed_area(item.unique)
    if (want_ccw and area < 0) or ((not want_ccw) and area > 0):
        unique = item.unique[::-1].copy()
        item.points = np.vstack([unique, unique[:1]])


def _actual_direction(item: _PathItem) -> str:
    """补偿与方向调整完成后的真实点序（climb 解析后只会是 ccw/cw）。"""
    if not item.closed:
        return "open"
    return "ccw" if _signed_area(item.unique) > 0 else "cw"


def _nearest_on_path(unique: np.ndarray, point: Tuple[float, float],
                     closed: bool) -> Tuple[np.ndarray, int, float, float]:
    """返回 (投影点, 线段下标, 线段参数 t, 距离)。"""
    target = np.asarray(point, dtype=np.float64)
    count = len(unique)
    if count == 0:
        return np.zeros(2), 0, 0.0, float("inf")
    if count == 1:
        return unique[0].copy(), 0, 0.0, float(np.linalg.norm(unique[0] - target))

    best = (None, 0, 0.0, float("inf"))
    segment_count = count if closed else count - 1
    for i in range(segment_count):
        a = unique[i]
        b = unique[(i + 1) % count]
        vec = b - a
        length_sq = float(np.dot(vec, vec))
        if length_sq < EPS:
            proj = a
            t = 0.0
        else:
            t = float(np.dot(target - a, vec) / length_sq)
            t = min(1.0, max(0.0, t))
            proj = a + vec * t
        distance = float(np.linalg.norm(proj - target))
        if distance < best[3]:
            best = (proj, i, t, distance)
    return best[0], best[1], best[2], best[3]


def _rotate_to_start(item: _PathItem, target: Tuple[float, float]) -> None:
    """把闭合轮廓旋转到离 target 最近的起割点；开放轮廓不处理。"""
    if not item.closed:
        return
    unique = item.unique
    proj, index, t, _ = _nearest_on_path(unique, target, closed=True)
    count = len(unique)
    if t < 1e-6:
        seq = [unique[(index + k) % count] for k in range(count)]
    else:
        seq = [proj]
        seq.extend(unique[(index + 1 + k) % count] for k in range(count))
    arr = np.asarray(seq, dtype=np.float64)
    item.points = np.vstack([arr, arr[:1]])


def _order_contours(items: List[_PathItem], pins: List[Dict[str, Any]],
                    optimize: bool) -> Tuple[List[Tuple[_PathItem, Tuple[float, float]]],
                                             float, float]:
    """贪心排序：每次选“离当前刀位最近”的轮廓，缩短抬刀空程。

    返回 (顺序列表[(轮廓, 起割点)], 输入顺序空程, 优化后空程)。
    手工起割点会先吸附到对应轮廓，排序时作为该轮廓的固定入口。
    """
    if not items:
        return [], 0.0, 0.0

    pinned: Dict[int, Tuple[float, float]] = {}
    for pin in pins:
        point = (float(pin["x"]), float(pin["y"]))
        contour_id = pin.get("contourId")
        candidates = items
        if contour_id is not None:
            matched = [it for it in items if it.poly_id == int(contour_id)]
            if matched:
                candidates = matched
        best_item = None
        best_distance = float("inf")
        for item in candidates:
            if id(item) in pinned:
                continue
            _, _, _, distance = _nearest_on_path(item.unique, point, item.closed)
            if distance < best_distance:
                best_distance = distance
                best_item = item
        if best_item is not None:
            pinned[id(best_item)] = point

    def entry_for(item: _PathItem, cursor: Tuple[float, float]) -> Tuple[float, float]:
        pin = pinned.get(id(item))
        if pin is not None:
            proj, _, _, _ = _nearest_on_path(item.unique, pin, item.closed)
            return (float(proj[0]), float(proj[1]))
        proj, _, _, _ = _nearest_on_path(item.unique, cursor, item.closed)
        return (float(proj[0]), float(proj[1]))

    # 输入顺序的空程（用于对比排序收益）
    naive = 0.0
    cursor = (0.0, 0.0)
    for item in items:
        entry = entry_for(item, cursor)
        naive += math.hypot(entry[0] - cursor[0], entry[1] - cursor[1])
        cursor = entry

    remaining = list(items)
    ordered: List[Tuple[_PathItem, Tuple[float, float]]] = []
    cursor = (0.0, 0.0)
    while remaining:
        if optimize:
            pick_index = 0
            pick_entry = entry_for(remaining[0], cursor)
            pick_distance = math.hypot(pick_entry[0] - cursor[0], pick_entry[1] - cursor[1])
            for index in range(1, len(remaining)):
                entry = entry_for(remaining[index], cursor)
                distance = math.hypot(entry[0] - cursor[0], entry[1] - cursor[1])
                if distance < pick_distance - 1e-9:
                    pick_index, pick_entry, pick_distance = index, entry, distance
            item = remaining.pop(pick_index)
            entry = pick_entry
        else:
            item = remaining.pop(0)
            entry = entry_for(item, cursor)
        ordered.append((item, entry))
        cursor = entry

    optimized = 0.0
    cursor = (0.0, 0.0)
    for _, entry in ordered:
        optimized += math.hypot(entry[0] - cursor[0], entry[1] - cursor[1])
        cursor = entry
    return ordered, naive, optimized


# ---------------------------------------------------------------- 微连接
def _arc_lengths(path: np.ndarray) -> np.ndarray:
    """累计弧长；path 首尾重合时最后一项等于周长。"""
    diffs = np.diff(path, axis=0)
    lengths = np.sqrt(np.sum(diffs * diffs, axis=1))
    return np.concatenate([[0.0], np.cumsum(lengths)])


def _point_at(path: np.ndarray, cumulative: np.ndarray, value: float) -> np.ndarray:
    """按弧长取路径上的点。"""
    total = float(cumulative[-1])
    value = min(max(0.0, value), total)
    index = int(np.searchsorted(cumulative, value, side="right") - 1)
    index = min(max(index, 0), len(path) - 2)
    span = float(cumulative[index + 1] - cumulative[index])
    t = 0.0 if span < EPS else (value - float(cumulative[index])) / span
    return path[index] + (path[index + 1] - path[index]) * t


def _slice_closed(path: np.ndarray, cumulative: np.ndarray,
                  start_s: float, end_s: float) -> Optional[np.ndarray]:
    """截取闭合路径 [start_s, end_s] 的一段，保留原始顶点。"""
    if end_s - start_s < 1e-6:
        return None
    points: List[np.ndarray] = [_point_at(path, cumulative, start_s)]
    for index in range(1, len(path) - 1):
        value = float(cumulative[index])
        if start_s < value < end_s:
            points.append(path[index])
    points.append(_point_at(path, cumulative, end_s))

    cleaned: List[np.ndarray] = []
    for point in points:
        if not cleaned or float(np.linalg.norm(point - cleaned[-1])) > 1e-9:
            cleaned.append(point)
    if len(cleaned) < 2:
        return None
    return np.asarray(cleaned, dtype=np.float64)


def _split_micro_joints(unique: np.ndarray, width: float,
                        count: int) -> Tuple[List[np.ndarray], List[Dict[str, float]]]:
    """把闭合轮廓按微连接切成多段；微连接处就是抬刀跨过的“留料桥”。

    微连接中心均匀分布，且避开起割点（从半间距开始），
    避免桥正好压在和上一刀的接刀痕上。
    """
    closed_path = np.vstack([unique, unique[:1]])
    if width <= EPS or count <= 0 or len(unique) < 3:
        return [closed_path], []

    cumulative = _arc_lengths(closed_path)
    perimeter = float(cumulative[-1])
    if perimeter < EPS:
        return [closed_path], []

    # 桥总长不超过周长的一半，数量自动收敛，防止把零件切碎
    max_count = max(1, int(perimeter * 0.5 / max(width, 1e-6)))
    effective_count = min(int(count), max_count)
    if effective_count <= 0 or width >= perimeter * 0.5:
        return [closed_path], []

    centers = [(i + 0.5) * perimeter / effective_count for i in range(effective_count)]
    segments: List[np.ndarray] = []
    bridges: List[Dict[str, float]] = []
    cursor_s = 0.0
    for center in centers:
        start_s = center - width / 2.0
        end_s = center + width / 2.0
        piece = _slice_closed(closed_path, cumulative, cursor_s, start_s)
        if piece is not None:
            segments.append(piece)
        point = _point_at(closed_path, cumulative, center)
        bridges.append({"x": float(point[0]), "y": float(point[1]),
                        "width": float(width), "length": float(width)})
        cursor_s = end_s
    tail = _slice_closed(closed_path, cumulative, cursor_s, perimeter)
    if tail is not None:
        segments.append(tail)

    # 段数不足说明桥之间没有有效切削段，退回不拆（避免空刀路）
    if len(segments) < 2:
        return [closed_path], []
    return segments, bridges


# ---------------------------------------------------------------- 切入圆弧
def _void_sign(item: _PathItem) -> int:
    """判断“空区”在路径左侧还是右侧，用于决定圆弧切入方向。

    外轮廓 CCW：零件在左、空区在右 => -1；内孔 CCW：孔内空区在左 => +1。
    CW 时整体取反。
    """
    area = _signed_area(item.unique) if item.closed else 0.0
    outside_void = -1 if not item.is_hole else 1
    if area < 0:
        outside_void = -outside_void
    return outside_void


def _lead_in_arc(start: np.ndarray, next_point: np.ndarray, radius: float,
                 void_sign: int, z: float, feed: float,
                 contour_id: int) -> Optional[Tuple[np.ndarray, List[Move]]]:
    """生成半圆切入：从空区一侧下刀，切到轮廓起点时与走刀方向相切。"""
    tangent = next_point - start
    length = float(np.linalg.norm(tangent))
    if length < 1e-9 or radius <= 0:
        return None
    tangent = tangent / length
    normal = np.array([-tangent[1], tangent[0]])       # 左法向
    void_normal = normal * (1 if void_sign > 0 else -1)
    center = start + void_normal * radius
    entry = start + void_normal * (2.0 * radius)
    start_angle = math.atan2(entry[1] - center[1], entry[0] - center[0])
    sweep = math.pi if void_sign > 0 else -math.pi

    samples = 8
    points: List[np.ndarray] = []
    for i in range(samples + 1):
        angle = start_angle + sweep * i / samples
        points.append(center + radius * np.array([math.cos(angle), math.sin(angle)]))
    points[-1] = start.copy()                          # 数值上严格落到起点
    moves = [Move(float(p[0]), float(p[1]), z, "cut", feed, contour_id)
             for p in points[1:]]
    return points[0], moves


# ---------------------------------------------------------------- 主算法
def build_toolpath(params: Dict[str, Any], report=None) -> ToolpathPlan:
    """同步构建刀路（在工作线程中调用）。"""
    t0 = time.perf_counter()
    check_cancelled = getattr(report, "check_cancelled", None)
    if check_cancelled is not None:
        check_cancelled()

    contours = _normalize_contours(params.get("contours", []))
    if not contours:
        raise ValueError("没有可用轮廓，无法生成刀路")
    _infer_holes(contours)

    cut_type = str(params.get("cutType", "auto"))
    targets = _select_contours(contours, cut_type)
    if not targets:
        if cut_type == "inner":
            raise ValueError("没有内孔轮廓，无法只生成内孔刀路")
        if cut_type == "outer":
            raise ValueError("没有外轮廓，无法只生成外轮廓刀路")
        raise ValueError("筛选后没有可加工轮廓")

    tool_diameter = float(params.get("toolDiameter", 6.0))
    side = str(params.get("side", "auto"))
    direction = str(params.get("direction", "ccw"))
    total_depth = float(params.get("depth", 18.0))
    pass_depth = max(0.05, float(params.get("passDepth", 3.0)))
    feed = float(params.get("feed", 3000.0))
    plunge_feed = float(params.get("plungeFeed", 800.0))
    safe_z = float(params.get("safeZ", 5.0))
    # 抬刀高度：退刀时收刀到该 Z，不与快速移动平面混用
    lift_z = float(params.get("liftZ", safe_z))
    lead_in = bool(params.get("leadIn", True))
    lead_radius = max(0.0, float(params.get("leadRadius", 2.0)))
    micro_joint = bool(params.get("microJoint", False))
    micro_width = max(0.0, float(params.get("microJointWidth", 2.0)))
    micro_count = max(0, int(params.get("microJointCount", 2)))
    optimize = bool(params.get("optimize", True))
    pins = list(params.get("startPoints") or [])

    if report:
        report(0.03, "轮廓分类完成，外轮廓 %d 条 / 内孔 %d 条" % (
            sum(1 for item in targets if not item.is_hole),
            sum(1 for item in targets if item.is_hole)))

    compensated, skipped = _compensate(targets, tool_diameter, side)
    if not compensated:
        raise ValueError("刀具补偿后没有可加工轮廓，请检查刀具直径")
    if report:
        report(0.12, "刀具补偿完成，共 %d 条刀路" % len(compensated))

    for item in compensated:
        _enforce_direction(item, direction)
    if check_cancelled is not None:
        check_cancelled()

    ordered, naive_rapid, optimized_rapid = _order_contours(compensated, pins, optimize)
    if report:
        report(0.2, "路径排序完成，空程 %.1fmm -> %.1fmm" % (naive_rapid, optimized_rapid))

    # 起割点：手工点选优先，否则自动取离上一刀最近的点（排序时已经算好）
    for item, entry in ordered:
        _rotate_to_start(item, entry)

    plan = ToolpathPlan()
    layers = max(1, int(math.ceil(total_depth / pass_depth)))
    moves = plan.moves
    marker_radius = max(1.2, tool_diameter * 0.6)
    plan.marker_radius = marker_radius

    total_cut = 0.0
    progress_total = max(1, len(ordered) * layers)
    progress_done = 0
    bridge_contours: set = set()

    for layer_index in range(layers):
        z = -min(total_depth, pass_depth * (layer_index + 1))
        for item, _entry in ordered:
            segments, bridges = _split_micro_joints(item.unique, micro_width, micro_count) \
                if (micro_joint and item.closed) else ([item.points], [])
            item.segment_count = len(segments)
            item.bridge_count = len(bridges)
            item.start_xy = (float(segments[0][0][0]), float(segments[0][0][1]))

            for segment_index, segment in enumerate(segments):
                points = np.asarray(segment, dtype=np.float64)
                if len(points) < 2:
                    continue
                start = points[0]
                entry_point = start
                lead_moves: List[Move] = []

                # 只在第一条切削段加圆弧切入；跨微连接后的重新下刀直接扎下
                if (segment_index == 0 and lead_in and item.closed
                        and lead_radius > 0 and len(points) >= 2):
                    bbox_w = float(item.unique[:, 0].max() - item.unique[:, 0].min())
                    bbox_h = float(item.unique[:, 1].max() - item.unique[:, 1].min())
                    radius = min(lead_radius, max(0.0, min(bbox_w, bbox_h) * 0.25))
                    lead = _lead_in_arc(start, points[1], radius, _void_sign(item),
                                        z, feed, item.poly_id)
                    if lead is not None:
                        entry_point, lead_moves = lead

                moves.append(Move(float(entry_point[0]), float(entry_point[1]),
                                  safe_z, "rapid", 0.0, item.poly_id))
                moves.append(Move(float(entry_point[0]), float(entry_point[1]),
                                  z, "plunge", plunge_feed, item.poly_id))
                if lead_moves:
                    moves.extend(lead_moves)
                    # 圆弧结束点就是轮廓起点，下面从第二个点继续
                # 补一个“起点到起点”的切削运动：让预览折线/导出文件都严格从起割点开始
                moves.append(Move(float(start[0]), float(start[1]),
                                  z, "cut", feed, item.poly_id))
                for point in points[1:]:
                    moves.append(Move(float(point[0]), float(point[1]),
                                      z, "cut", feed, item.poly_id))
                moves.append(Move(float(points[-1][0]), float(points[-1][1]),
                                  lift_z, "retract", 0.0, item.poly_id))

                # 逐轮廓累计切削长度（跨层累加，统计的是实际加工长度）
                for a, b in zip(points[:-1], points[1:]):
                    total_cut += float(np.linalg.norm(b - a))
                item.cut_length += float(np.sum(np.linalg.norm(np.diff(points, axis=0), axis=1)))

            if item.bridge_count and item.poly_id not in bridge_contours:
                bridge_contours.add(item.poly_id)
                for bridge in bridges:
                    plan.bridges.append({
                        "contourId": item.poly_id,
                        "x": round(float(bridge["x"]), 3),
                        "y": round(float(bridge["y"]), 3),
                        "width": round(float(bridge["width"]), 3),
                    })

            progress_done += 1
            if report:
                report(0.2 + 0.72 * progress_done / progress_total,
                       "第 %d/%d 层 · 第 %d/%d 条轮廓" % (
                           layer_index + 1, layers, progress_done, progress_total))
            if check_cancelled is not None:
                check_cancelled()

    # 空程与运动统计：快移距离只统计 XY（Z 抬刀在真实机床上也应尽量快）
    rapid_length = 0.0
    prev: Optional[Move] = Move(0.0, 0.0, safe_z, "rapid")
    for mv in moves:
        if mv.kind == "rapid":
            rapid_length += math.hypot(mv.x - prev.x, mv.y - prev.y)
        prev = mv

    plan.start_points = [
        {"x": round(float(item.start_xy[0]), 3), "y": round(float(item.start_xy[1]), 3)}
        for item, _entry in ordered
    ]
    plan.contours = [{
        "id": item.poly_id,
        "isHole": item.is_hole,
        "closed": item.closed,
        "side": item.side,
        "direction": _actual_direction(item),
        "segments": item.segment_count or 1,
        "bridges": item.bridge_count,
        "start": [round(item.start_xy[0], 3), round(item.start_xy[1], 3)],
        "cutLengthMm": round(item.cut_length, 2),
    } for item, _entry in ordered]

    order_gain = max(0.0, naive_rapid - optimized_rapid)
    plan.stats = {
        "moveCount": len(moves),
        "layerCount": layers,
        "contourCount": len(ordered),
        "outerCount": sum(1 for item, _ in ordered if not item.is_hole),
        "innerCount": sum(1 for item, _ in ordered if item.is_hole),
        "openCount": sum(1 for item, _ in ordered if not item.closed),
        "cutLengthMm": round(total_cut, 2),
        "rapidLengthMm": round(rapid_length, 2),
        "bridgeCount": sum(item.bridge_count for item, _ in ordered),
        "startPointCount": len(plan.start_points),
        "skippedCount": len(skipped),
        "orderGainMm": round(order_gain, 2),
        # 估算工时：切削 + 空程（按 5m/min 估算） + 每次下刀固定开销
        "estimatedMin": round(total_cut / max(1.0, feed)
                              + rapid_length / 5000.0
                              + len(moves) * 0.002, 2),
        "calcMs": round((time.perf_counter() - t0) * 1000, 1),
    }
    if skipped:
        plan.stats["skippedNote"] = "；".join(
            "#%s %s" % (row["id"], row["reason"]) for row in skipped[:3])
    return plan


# ---------------------------------------------------------------- 任务入口
def toolpath_job(params: Dict[str, Any], report) -> Dict[str, Any]:
    """任务队列调用的任务函数：必须模块级、参数可 pickle。"""
    report(0.01, "开始生成刀路")
    plan = build_toolpath(params, report)

    # 缓存最近一次刀路，供导出接口直接复用
    from app.services import state
    state.LAST_PLAN = plan

    report(0.97, "生成预览数据")
    return {
        "polylines": plan.polyline_preview(),
        "stats": plan.stats,
        "contours": plan.contours,
        "startPoints": plan.start_points,
        "bridges": plan.bridges,
        "tool": params.get("tool"),
        "template": params.get("template"),
        "source": str(params.get("source", "doc")),
        "sheet": params.get("sheet"),
        "layers": [
            {"id": LAYER_CUT, "name": "切削", "color": 0x37D67A,
             "operation": "cut", "dashed": False},
            {"id": LAYER_RAPID, "name": "抬刀空走", "color": 0x8A94A6,
             "operation": "rapid", "dashed": True},
            {"id": LAYER_START, "name": "起割点", "color": 0xFFB020,
             "operation": "marker", "dashed": False},
        ],
    }
