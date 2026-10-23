"""DXF 轮廓修复算法（模块 1）。

处理流水线（顺序不可调换）：

    1. 去碎线      —— 长度 < min_length 的段、面积 < min_area 的闭合碎片
    2. 重叠清理    —— 同图层内按"直线分组 + 区间并集"消除重复/压线线段
    3. 断线串接    —— 端点聚类吸附 + 最直延续优先走线，重建连续轮廓
    4. 自动闭合    —— 首尾缺口 <= close_gap 时闭合（超过则保留开放并告警）
    5. 内外分类    —— 按包含关系深度奇偶判定外轮廓 / 内孔，并统一走向
    6. 自相交检测  —— 网格分桶后的线段求交，返回异常线段与交点

实现说明（与需求中"交给 OCCT"的关系）：
- 端点吸附、串接、包含判定属于离散拓扑问题，OCCT 没有直接 API，
  用 numpy + 并查集实现更快、结果可解释、且不依赖 OCCT 是否安装；
- 共线压线清理若用 pyclipper 只能处理闭合多边形，对开放线无效，
  所以这里用"直线分组 + 一维区间并集"，开/闭轮廓都适用；
- 模块 2 封装 OCCT 后，闭合轮廓的并集/偏移可直接换引擎，
  本模块只需替换 TopologyRepair 内部实现，对外接口与数据结构不变。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from app.geometry.base import dedup_points
from app.models.geometry import Polyline

logger = logging.getLogger(__name__)

# 自相交检测的规模上限：超过后跳过精细检查，避免一张病图卡死导入
MAX_INTERSECTION_SEGMENTS = 4000
MAX_INTERSECTION_CHECKS = 300_000


# ---------------------------------------------------------------- 配置 / 结果
@dataclass
class RepairOptions:
    """修复参数。单位全部为 mm，默认值针对板式家具图纸。"""

    merge_tol: float = 0.2      # 端点吸附距离：小于它的两个端点视为同一点
    min_length: float = 0.5     # 碎线阈值：短于此长度的线段直接丢弃
    min_area: float = 0.5       # 碎面阈值：闭合轮廓面积小于它直接丢弃
    close_gap: float = 1.0      # 自动闭合允许的最大首尾缺口
    remove_overlap: bool = True  # 是否清理共线重叠/重复线段
    check_issues: bool = True    # 是否做自相交检测

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, object]]) -> "RepairOptions":
        """从接口参数构造；非法值静默回落到默认，避免一次误传毁掉整份图纸。"""
        raw = raw or {}

        def num(key: str, default: float, low: float, high: float) -> float:
            try:
                value = float(raw.get(key, default))
            except (TypeError, ValueError):
                return default
            return max(low, min(high, value))

        options = cls(
            merge_tol=num("mergeTol", 0.2, 0.001, 5.0),
            min_length=num("minLength", 0.5, 0.0, 100.0),
            min_area=num("minArea", 0.5, 0.0, 10000.0),
            close_gap=num("closeGap", 1.0, 0.0, 20.0),
            remove_overlap=bool(raw.get("removeOverlap", True)),
            check_issues=bool(raw.get("checkIssues", True)),
        )
        # 闭合容差不能小于吸附容差，否则刚吸附好的端点又要被判为"没闭合"
        options.close_gap = max(options.close_gap, options.merge_tol)
        return options


@dataclass
class RepairStats:
    """修复统计：直接进导入结果 meta，UI 展示与验收都用它。"""

    inputContours: int = 0
    inputSegments: int = 0
    droppedTiny: int = 0
    mergedContours: int = 0
    removedSegments: int = 0
    closedContours: int = 0
    openContours: int = 0
    outerContours: int = 0
    holeContours: int = 0
    issues: int = 0

    def as_dict(self) -> Dict[str, int]:
        return {
            "inputContours": self.inputContours,
            "inputSegments": self.inputSegments,
            "droppedTiny": self.droppedTiny,
            "mergedContours": self.mergedContours,
            "removedSegments": self.removedSegments,
            "closedContours": self.closedContours,
            "openContours": self.openContours,
            "outerContours": self.outerContours,
            "holeContours": self.holeContours,
            "issues": self.issues,
        }


@dataclass
class ContourIssue:
    """一条图形校验告警。

    segment 用于前端标红：两个端点组成的短线段（交点处会画成十字）。
    """

    kind: str                  # self_intersection | open_contour
    message: str
    polyline_id: int
    severity: str = "warning"  # warning | error
    point: Tuple[float, float] = (0.0, 0.0)
    segment: Optional[np.ndarray] = None


# ---------------------------------------------------------------- 基础工具
def _as_array(poly: Polyline) -> np.ndarray:
    return np.asarray(poly.pts, dtype=np.float64)


def _bbox_of(pts: np.ndarray) -> Tuple[float, float, float, float]:
    return (float(pts[:, 0].min()), float(pts[:, 1].min()),
            float(pts[:, 0].max()), float(pts[:, 1].max()))


def _bbox_contains(box: Tuple[float, float, float, float], p: Tuple[float, float]) -> bool:
    return box[0] <= p[0] <= box[2] and box[1] <= p[1] <= box[3]


def point_in_polygon(point: Sequence[float], poly: np.ndarray) -> bool:
    """射线法判断点是否在多边形内部（支持凹多边形）。

    边界上的点结果不确定，调用方传的探测点已经做了向内偏移。
    """
    x, y = float(point[0]), float(point[1])
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            t = (y - y1) / (y2 - y1)
            if x < x1 + t * (x2 - x1):
                inside = not inside
    return inside


def _representative_point(poly: np.ndarray) -> Tuple[float, float]:
    """在轮廓内部找一个稳定的探测点，用于包含关系判断。"""
    center = poly.mean(axis=0)
    if point_in_polygon(center, poly):
        return float(center[0]), float(center[1])

    # 凹凸形状的形心可能落在外面：取最长边的中点向内/向外各试一次
    best_index = 0
    best_len = -1.0
    n = len(poly)
    for i in range(n):
        length = float(np.linalg.norm(poly[(i + 1) % n] - poly[i]))
        if length > best_len:
            best_len, best_index = length, i

    a = poly[best_index]
    b = poly[(best_index + 1) % n]
    direction = b - a
    normal = np.array([-direction[1], direction[0]], dtype=np.float64)
    norm = float(np.linalg.norm(normal))
    if norm < 1e-9:
        return float(center[0]), float(center[1])
    normal /= norm
    offset = max(0.05, best_len * 0.01)
    for sign in (1.0, -1.0):
        probe = (a + b) / 2.0 + normal * offset * sign
        if point_in_polygon(probe, poly):
            return float(probe[0]), float(probe[1])
    return float(center[0]), float(center[1])


def _segment_intersection(a: np.ndarray, b: np.ndarray,
                          c: np.ndarray, d: np.ndarray) -> Optional[np.ndarray]:
    """两条线段求交；平行/共线返回 None（重叠已在去重阶段处理）。"""
    r = b - a
    s = d - c
    denom = r[0] * s[1] - r[1] * s[0]
    if abs(denom) < 1e-12:
        return None
    t = ((c[0] - a[0]) * s[1] - (c[1] - a[1]) * s[0]) / denom
    u = ((c[0] - a[0]) * r[1] - (c[1] - a[1]) * r[0]) / denom
    if -1e-9 <= t <= 1.0 + 1e-9 and -1e-9 <= u <= 1.0 + 1e-9:
        return a + t * r
    return None


# ---------------------------------------------------------------- 步骤 1：碎线
def _gross_area(pts: np.ndarray) -> float:
    """计算"总面积"：三角形面积绝对值累加。

    为什么不用鞋带公式：蝴蝶结（自相交）轮廓的有向面积接近 0，
    用有向面积会把它当成碎面误删；总面积对自相交形状永不为 0，
    只对真正细小的碎片才小。
    """
    if len(pts) < 3:
        return 0.0
    origin = pts[0]
    d = pts[1:] - origin
    crosses = d[:-1, 0] * d[1:, 1] - d[:-1, 1] * d[1:, 0]
    return 0.5 * float(np.sum(np.abs(crosses)))


def drop_slivers(polylines: Sequence[Polyline], options: RepairOptions) -> Tuple[List[Polyline], int]:
    kept: List[Polyline] = []
    dropped = 0
    for poly in polylines:
        pts = dedup_points(_as_array(poly), tol=1e-4)
        if len(pts) < 2:
            dropped += 1
            continue
        if not poly.closed:
            length = float(np.sum(np.linalg.norm(np.diff(pts, axis=0), axis=1)))
            if length < options.min_length:
                dropped += 1
                continue
        else:
            # 闭合轮廓的面积必须按"总面积"算，否则自相交轮廓会被误删
            if _gross_area(pts) < options.min_area:
                dropped += 1
                continue
        kept.append(Polyline(poly.id, poly.layer_id, pts.astype(np.float32), poly.closed, poly.is_hole))
    return kept, dropped


# ---------------------------------------------------------------- 步骤 2：重叠
def _line_key(a: np.ndarray, b: np.ndarray, ang_eps: float = 1e-4,
              off_eps: float = 0.05) -> Optional[Tuple[Tuple[int, int], np.ndarray, float]]:
    """把线段归一化成 (角度, 法向截距) 分组键。

    角度归一到 [0, π)，保证同一直线正反方向落到同一组；
    量化后再回代验证共线性，避免仅靠四舍五入误并。
    """
    direction = b - a
    length = float(math.hypot(direction[0], direction[1]))
    if length <= 1e-9:
        return None
    ux, uy = direction[0] / length, direction[1] / length
    if ux < -1e-9 or (abs(ux) <= 1e-9 and uy < 0):
        ux, uy = -ux, -uy
    angle = math.atan2(uy, ux)          # [0, π)
    nx, ny = -uy, ux
    offset = float(a[0] * nx + a[1] * ny)
    key = (int(round(angle / ang_eps)), int(round(offset / off_eps)))
    return key, np.array([ux, uy]), offset


# ---------------------------------------------------------------- 步骤 3：串接
class _UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _cluster_endpoints(points: np.ndarray, tol: float) -> Tuple[np.ndarray, np.ndarray]:
    """端点聚类：网格分桶 + 邻域并查集，返回 (clusterIds, clusterCenters)。"""
    count = len(points)
    uf = _UnionFind(count)
    cell = max(tol, 1e-6)
    buckets: Dict[Tuple[int, int], List[int]] = {}
    keys = np.floor(points / cell).astype(np.int64)

    for idx, (cx, cy) in enumerate(keys):
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for other in buckets.get((int(cx) + dx, int(cy) + dy), ()):  # type: ignore[arg-type]
                    if float(np.linalg.norm(points[idx] - points[other])) <= tol:
                        uf.union(idx, other)
        buckets.setdefault((int(cx), int(cy)), []).append(idx)

    roots = np.array([uf.find(i) for i in range(count)], dtype=np.int64)
    unique_roots, inverse = np.unique(roots, return_inverse=True)
    centers = np.zeros((len(unique_roots), 2), dtype=np.float64)
    for idx in range(count):
        centers[inverse[idx]] += points[idx]
    counts = np.bincount(inverse, minlength=len(unique_roots)).astype(np.float64)
    centers /= counts[:, None]
    return inverse, centers


def chain_segments(
    segments: Sequence[Tuple[np.ndarray, np.ndarray, int]],
    tol: float,
) -> List[Polyline]:
    """把线段集合串成轮廓：端点吸附 + 最直延续优先，闭合时自动收尾。"""
    if not segments:
        return []

    endpoints = np.array(
        [p for a, b, _ in segments for p in (a, b)], dtype=np.float64
    ).reshape(-1, 2)
    cluster_ids, centers = _cluster_endpoints(endpoints, tol)

    # node -> [(edgeIndex, otherNode)]
    # edge_nodes 必须与 segments 下标对齐（预分配 + None 占位），
    # 否则退化边会导致后面按 start 取端点时整体错位。
    graph: Dict[int, List[Tuple[int, int]]] = {}
    edge_nodes: List[Optional[Tuple[int, int]]] = [None] * len(segments)
    for edge_index in range(len(segments)):
        ca = int(cluster_ids[2 * edge_index])
        cb = int(cluster_ids[2 * edge_index + 1])
        if ca == cb:                     # 两端吸附成同一点：退化边，丢弃
            continue
        edge_nodes[edge_index] = (ca, cb)
        graph.setdefault(ca, []).append((edge_index, cb))
        graph.setdefault(cb, []).append((edge_index, ca))

    used = [False] * len(segments)
    contours: List[Polyline] = []
    next_id = 1

    for start in range(len(segments)):
        if used[start]:
            continue
        nodes = edge_nodes[start]
        if nodes is None:
            used[start] = True
            continue
        ca, cb = nodes
        used[start] = True
        chain = [ca, cb]

        # 向后延伸
        while True:
            tail = chain[-1]
            direction = centers[tail] - centers[chain[-2]]
            candidates = [
                (edge, other) for edge, other in graph.get(tail, [])
                if not used[edge]
            ]
            if not candidates:
                break
            # 选"转向最小"（点积最大）的一条，保持轮廓走向自然
            best_edge, best_node = max(
                candidates,
                key=lambda item: _cosine(direction, centers[item[1]] - centers[tail]),
            )
            used[best_edge] = True
            if best_node == chain[0]:
                chain.append(best_node)
                break
            chain.append(best_node)

        # 向前延伸（从头部继续）
        if chain[0] != chain[-1]:
            while True:
                head = chain[0]
                direction = centers[head] - centers[chain[1]]
                candidates = [
                    (edge, other) for edge, other in graph.get(head, [])
                    if not used[edge]
                ]
                if not candidates:
                    break
                best_edge, best_node = max(
                    candidates,
                    key=lambda item: _cosine(direction, centers[item[1]] - centers[head]),
                )
                used[best_edge] = True
                if best_node == chain[-1]:
                    chain.insert(0, best_node)
                    break
                chain.insert(0, best_node)

        closed = chain[0] == chain[-1]
        pts = centers[chain]
        if closed:
            pts = pts[:-1]
        if len(pts) < 2:
            continue
        layer_id = _layer_of(segments, min(start, len(segments) - 1))
        contours.append(
            Polyline(next_id, layer_id, pts.astype(np.float32), closed and len(pts) >= 3)
        )
        next_id += 1

    return contours


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na < 1e-12 or nb < 1e-12:
        return -1.0
    return float(np.dot(a, b) / (na * nb))


def _layer_of(segments: Sequence[Tuple[np.ndarray, np.ndarray, int]], index: int) -> int:
    if not segments:
        return 1
    return segments[index][2]


# ---------------------------------------------------------------- 步骤 4：闭合
def close_open_contours(
    polylines: Sequence[Polyline], options: RepairOptions
) -> Tuple[List[Polyline], List[ContourIssue]]:
    result: List[Polyline] = []
    issues: List[ContourIssue] = []

    for poly in polylines:
        pts = _as_array(poly)
        if len(pts) < 3:
            result.append(poly)
            continue
        gap = float(np.linalg.norm(pts[-1] - pts[0]))
        if poly.closed or gap <= 1e-6:
            result.append(Polyline(poly.id, poly.layer_id, pts.astype(np.float32),
                                   len(pts) >= 3, poly.is_hole))
            continue
        if gap <= options.close_gap:
            # 缺口在容差内：把首尾吸附到缺口中点，再丢掉尾点。
            # 这一步很关键：只置闭合标志而不真正对齐端点，会出现
            # "上边端点恰好落在左边线上"的伪自相交（实测过）。
            pts = pts.copy()
            mid = (pts[0] + pts[-1]) * 0.5
            pts[0] = mid
            if len(pts) >= 4:
                pts = pts[:-1]        # 闭合语义自带"末点 -> 首点"这条边
            result.append(Polyline(poly.id, poly.layer_id, pts.astype(np.float32), True, poly.is_hole))
        else:
            result.append(Polyline(poly.id, poly.layer_id, pts.astype(np.float32), False))
            issues.append(ContourIssue(
                kind="open_contour",
                message="开放轮廓未闭合（首尾缺口 %.2f mm）" % gap,
                polyline_id=poly.id,
                severity="warning",
                point=(float(pts[0][0]), float(pts[0][1])),
                segment=np.stack([pts[0], pts[-1]]),
            ))
    return result, issues


# ---------------------------------------------------------------- 步骤 5：分类
def classify_outer_holes(polylines: Sequence[Polyline]) -> Tuple[int, int]:
    """按包含关系深度奇偶判定内外轮廓，并统一走向（外 CCW / 内 CW）。"""
    closed = [p for p in polylines if p.closed and p.point_count >= 3]
    boxes = [(p, _bbox_of(_as_array(p))) for p in closed]
    outer = 0
    holes = 0

    for index, (poly, box) in enumerate(boxes):
        pts = _as_array(poly)
        probe = _representative_point(pts)
        poly_area = abs(float(poly.area()))
        depth = 0
        for other_index, (other, other_box) in enumerate(boxes):
            if other_index == index:
                continue
            # 面积不大于自己的轮廓不可能包含自己。
            # 这个剪枝必须留着：当内孔的边恰好穿过外轮廓形心时，
            # 射线法会把边界上的点判成"在内部"，导致外轮廓被误判成内孔。
            if abs(float(other.area())) <= poly_area + 1e-9:
                continue
            # 包围盒不包含探测点，必然不包含整个轮廓，先做廉价排除
            if not _bbox_contains(other_box, probe):
                continue
            other_pts = _as_array(other)
            if point_in_polygon(probe, other_pts):
                depth += 1
        poly.is_hole = depth % 2 == 1
        # 统一走向：外轮廓逆时针（面积为正），内孔顺时针
        if poly.is_hole and poly.area() > 0:
            poly.pts = poly.pts[::-1].copy()
        elif not poly.is_hole and poly.area() < 0:
            poly.pts = poly.pts[::-1].copy()
        if poly.is_hole:
            holes += 1
        else:
            outer += 1
    return outer, holes


# ---------------------------------------------------------------- 步骤 6：自相交
def find_self_intersections(poly: Polyline) -> List[Tuple[np.ndarray, np.ndarray]]:
    """返回 [(交点, 出问题的线段)]；线段用于前端标红。"""
    pts = _as_array(poly)
    count = len(pts)
    if count < 4:
        return []
    edge_count = count if poly.closed else count - 1
    if edge_count > MAX_INTERSECTION_SEGMENTS:
        logger.warning("轮廓 %s 段数 %d 超过自相交检测上限，跳过", poly.id, edge_count)
        return []

    segments = [(pts[i], pts[(i + 1) % count]) for i in range(edge_count)]
    lengths = np.array([np.linalg.norm(b - a) for a, b in segments])
    cell = max(1.0, float(lengths.mean()) * 2.0 if len(lengths) else 1.0)

    buckets: Dict[Tuple[int, int], List[int]] = {}
    for index, (a, b) in enumerate(segments):
        minx, miny = float(min(a[0], b[0])), float(min(a[1], b[1]))
        maxx, maxy = float(max(a[0], b[0])), float(max(a[1], b[1]))
        for gx in range(int(minx // cell), int(maxx // cell) + 1):
            for gy in range(int(miny // cell), int(maxy // cell) + 1):
                buckets.setdefault((gx, gy), []).append(index)

    hits: List[Tuple[np.ndarray, np.ndarray]] = []
    checked = set()
    checks = 0

    for members in buckets.values():
        if len(members) < 2:
            continue
        for i_idx in range(len(members)):
            for j_idx in range(i_idx + 1, len(members)):
                i, j = members[i_idx], members[j_idx]
                if (i, j) in checked:
                    continue
                checked.add((i, j))
                # 相邻线段共享端点，交集不算自交
                if abs(i - j) == 1 or (poly.closed and {i, j} == {0, edge_count - 1}):
                    continue
                checks += 1
                if checks > MAX_INTERSECTION_CHECKS:
                    logger.warning("轮廓 %s 自相交检测达到检查上限，提前结束", poly.id)
                    return hits
                point = _segment_intersection(segments[i][0], segments[i][1],
                                              segments[j][0], segments[j][1])
                if point is not None:
                    hits.append((point, segments[i]))
    return hits


# ---------------------------------------------------------------- 流水线
class TopologyRepair:
    """轮廓修复器：无状态、可复用；每次 apply 返回全新对象。"""

    def __init__(self, options: Optional[RepairOptions] = None) -> None:
        self.options = options or RepairOptions()

    def apply(self, polylines: Iterable[Polyline]) -> Tuple[List[Polyline], List[ContourIssue], RepairStats]:
        options = self.options
        # 只消费一次：允许调用方直接传生成器
        items = list(polylines)
        stats = RepairStats(inputContours=len(items))

        # 1) 碎线
        current, dropped = drop_slivers(items, options)
        stats.droppedTiny = dropped

        # 2) 拆段 + 去重/去压线
        segments, removed = _split_and_dedupe(current, options)
        stats.inputSegments = len(segments) + removed
        stats.removedSegments = removed

        # 3) 串接
        chained = chain_segments(segments, options.merge_tol)
        stats.mergedContours = len(chained)

        # 4) 闭合
        closed, issues = close_open_contours(chained, options)

        # 5) 内外分类
        outer, holes = classify_outer_holes(closed)
        stats.outerContours = outer
        stats.holeContours = holes

        # 6) 自相交
        if options.check_issues:
            for poly in closed:
                for point, segment in find_self_intersections(poly):
                    issues.append(ContourIssue(
                        kind="self_intersection",
                        message="轮廓自相交于 (%.2f, %.2f)" % (point[0], point[1]),
                        polyline_id=poly.id,
                        severity="error",
                        point=(float(point[0]), float(point[1])),
                        segment=segment,
                    ))

        stats.closedContours = sum(1 for p in closed if p.closed)
        stats.openContours = sum(1 for p in closed if not p.closed)
        stats.issues = len(issues)
        return closed, issues, stats


def _split_and_dedupe(
    polylines: Sequence[Polyline], options: RepairOptions
) -> Tuple[List[Tuple[np.ndarray, np.ndarray, int]], int]:
    """拆成线段并按图层清理重叠；未开启去重时直接返回原始线段。"""
    if not options.remove_overlap:
        segments: List[Tuple[np.ndarray, np.ndarray, int]] = []
        for poly in polylines:
            pts = _as_array(poly)
            count = len(pts)
            edge_count = count if poly.closed else count - 1
            for i in range(max(0, edge_count)):
                segments.append((pts[i], pts[(i + 1) % count], poly.layer_id))
        return segments, 0
    return _dedupe_segments(polylines)


def _dedupe_segments(
    polylines: Sequence[Polyline],
) -> Tuple[List[Tuple[np.ndarray, np.ndarray, int]], int]:
    """按"直线分组 + 一维区间并集"清理重复与压线的线段。

    返回 (线段列表, 被消除的线段数)。
    """
    groups: Dict[Tuple[int, int, int], List[Tuple[float, float, np.ndarray, float]]] = {}
    originals = 0
    leftovers: List[Tuple[np.ndarray, np.ndarray, int]] = []

    for poly in polylines:
        pts = _as_array(poly)
        count = len(pts)
        if count < 2:
            continue
        edge_count = count if poly.closed else count - 1
        for i in range(edge_count):
            a, b = pts[i], pts[(i + 1) % count]
            info = _line_key(a, b)
            if info is None:
                continue
            (angle_key, offset_key), direction, offset = info
            originals += 1
            nx, ny = -direction[1], direction[0]
            # 量化误差校验：端点偏离分组直线超过 0.05mm 就不参与合并
            if max(abs(float(a[0] * nx + a[1] * ny) - offset),
                   abs(float(b[0] * nx + b[1] * ny) - offset)) > 0.05:
                leftovers.append((a, b, poly.layer_id))
                continue
            sa = float(a[0] * direction[0] + a[1] * direction[1])
            sb = float(b[0] * direction[0] + b[1] * direction[1])
            key = (angle_key, offset_key, int(poly.layer_id))
            groups.setdefault(key, []).append((min(sa, sb), max(sa, sb), direction, offset))

    result: List[Tuple[np.ndarray, np.ndarray, int]] = list(leftovers)
    for (angle_key, offset_key, layer_id), items in groups.items():
        direction = items[0][2]
        offset = items[0][3]
        normal = np.array([-direction[1], direction[0]])
        anchor = normal * offset

        spans = sorted((s, e) for s, e, _d, _o in items)
        merged: List[List[float]] = []
        for start, end in spans:
            if not merged or start > merged[-1][1] + 1e-6:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)

        for start, end in merged:
            if end - start <= 1e-9:
                continue
            result.append((anchor + direction * start, anchor + direction * end, layer_id))

    removed = max(0, originals - len(result))
    return result, removed


# ---------------------------------------------------------------- 零件分组
@dataclass
class PartGroup:
    """一个零件的轮廓集合（第一个通常是最外层外轮廓）。"""

    name: str
    polylines: List[Polyline] = field(default_factory=list)

    def bbox(self) -> Tuple[float, float, float, float]:
        if not self.polylines:
            return (0.0, 0.0, 0.0, 0.0)
        boxes = np.array([p.bbox() for p in self.polylines], dtype=np.float64)
        return (float(boxes[:, 0].min()), float(boxes[:, 1].min()),
                float(boxes[:, 2].max()), float(boxes[:, 3].max()))


# 零件数上限：一张图里如果被拆出成千上万个"零件"，说明图纸是碎的，
# 此时按散线处理更安全，避免后续排料把后端拖死。
MAX_PARTS_PER_FILE = 500


def group_parts(polylines: Sequence[Polyline], base_name: str = "零件") -> List[PartGroup]:
    """把一堆轮廓按包含关系拆成零件。

    规则：每条"最外层闭合轮廓"连同它内部的所有轮廓（内孔、内部的内部）算一个零件；
    没被任何闭合轮廓包住的开放轮廓归入一个名为 "<base_name>-散线" 的零件。

    为什么要拆零件：排料/锁定的最小单位是零件，不是单根轮廓；
    一个 DXF 里画了 20 个板件时，用户希望一次性拿到 20 个可拖拽的零件。
    """
    items = list(polylines)
    if not items:
        return []

    closed_index = [i for i, p in enumerate(items) if p.closed and p.point_count >= 3]
    boxes = {i: _bbox_of(_as_array(items[i])) for i in closed_index}
    areas = {i: abs(float(items[i].area())) for i in closed_index}
    probes = {i: _representative_point(_as_array(items[i])) for i in closed_index}

    # parent[i] = 包含 i 且面积最小的那条闭合轮廓
    parent: Dict[int, int] = {}
    for i in closed_index:
        best = -1
        best_area = float("inf")
        for j in closed_index:
            if i == j:
                continue
            if areas[j] <= areas[i]:          # 包含者面积一定更大
                continue
            if not _bbox_contains(boxes[j], probes[i]):
                continue
            if point_in_polygon(probes[i], _as_array(items[j])):
                if areas[j] < best_area:
                    best, best_area = j, areas[j]
        if best >= 0:
            parent[i] = best

    def root_of(index: int) -> int:
        seen = set()
        while index in parent and index not in seen:
            seen.add(index)
            index = parent[index]
        return index

    # 按最外层轮廓归组（保持原始顺序，便于前端稳定显示）
    groups_map: Dict[int, PartGroup] = {}
    loose: List[Polyline] = []

    for index, poly in enumerate(items):
        if index in closed_index:
            root = root_of(index)
            group = groups_map.get(root)
            if group is None:
                group = PartGroup(name="%s-%d" % (base_name, len(groups_map) + 1))
                groups_map[root] = group
            group.polylines.append(poly)
        else:
            # 开放轮廓：尽量归到包含它的零件里，否则进散线组
            probe = _representative_point(_as_array(poly)) if poly.point_count >= 3 else None
            owner = -1
            if probe is not None:
                owner_area = float("inf")
                for j in closed_index:
                    if not _bbox_contains(boxes[j], probe):
                        continue
                    if point_in_polygon(probe, _as_array(items[j])) and areas[j] < owner_area:
                        owner, owner_area = j, areas[j]
            if owner >= 0:
                groups_map.setdefault(
                    root_of(owner), PartGroup(name="%s-%d" % (base_name, len(groups_map) + 1))
                ).polylines.append(poly)
            else:
                loose.append(poly)

    groups = list(groups_map.values())
    if loose:
        groups.append(PartGroup(name="%s-散线" % base_name, polylines=loose))
    return groups
