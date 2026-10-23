"""OpenCASCADE(OCCT) 2D 几何引擎。

同时兼容两种 Python 绑定：
- pythonocc-core  -> 模块根 `OCC.Core`（conda install -c conda-forge pythonocc-core）
- cadquery-ocp    -> 模块根 `OCP`     （pip install cadquery-ocp）

只使用 2D 平面相关的类，不碰任何 BRep 实体/曲面/网格建模，保持轻量。
"""

from __future__ import annotations

import gc
import importlib
import logging
from typing import Any, Callable, Dict, List, Optional, Sequence

import numpy as np

from app.geometry.base import (
    JOIN_MITER,
    JOIN_ROUND,
    JOIN_SQUARE,
    GeometryEngine,
    dedup_points,
    ensure_ccw,
)
from app.models.geometry import Polyline

logger = logging.getLogger(__name__)

# 需要的子模块清单（两种绑定下路径一致，只是根不同）
_SUBMODULES = (
    "gp", "TopoDS", "TopAbs", "TopExp", "TopTools", "BRep", "BRepTools",
    "BRepBuilderAPI", "BRepOffsetAPI", "BRepAlgoAPI",
    "BRepAdaptor", "GCPnts", "GeomAbs",
)

# 批量运算时每处理这么多条轮廓主动回收一次 OCCT 对象
_GC_INTERVAL = 200

# 累计 gc 次数：产给 /api/geometry/info，测试脚本用它验证"确实在做释放"
_GC_RUNS = 0


def _collect() -> None:
    """主动触发垃圾回收。

    OCCT 的 Python 绑定对象在 C++ 堆上持有几何数据，且引用关系经常成环，
    单靠引用计数无法及时释放。排料/偏置连续处理上千条轮廓时，
    不 collect 会让峰值内存持续抬高（典型表现是几十万点图纸跑到几个 GB）。
    """
    global _GC_RUNS
    _GC_RUNS += 1
    gc.collect()


def _load_bindings() -> Optional[Dict[str, Any]]:
    """尝试加载 OCCT 绑定，返回 {子模块名: module}，加载失败返回 None。"""
    for root in ("OCC.Core", "OCP"):
        try:
            return {name: importlib.import_module(f"{root}.{name}") for name in _SUBMODULES}
        except ImportError:
            continue
    return None


class ReleaseScope:
    """OCCT 对象释放作用域（模块 2 的资源管控核心）。

    为什么不能用"局部变量出作用域"了事：
    - OCP/pythonocc 的 Shape、算法对象本体都在 C++ 堆上，Python 侧只是薄壳；
    - BRepAlgoAPI_* / BRepOffsetAPI_* 会与结果 Shape 互相引用，形成引用环，
      引用计数归零不了，只能等 gc；
    - 异常路径会跳过普通的 del 语句。

    所以这里把每个 OCCT 对象显式登记，退出时统一置 None + gc.collect()，
    保证"一次运算结束就把 Handle 还回去"，且计数可被外部观测。
    """

    __slots__ = ("_objects", "_on_release")

    def __init__(self, on_release: Optional[Callable[[int], None]] = None) -> None:
        self._objects: List[Any] = []
        self._on_release = on_release

    def hold(self, obj: Any) -> Any:
        """登记一个 OCCT 对象并原样返回，方便写成 scope.hold(self._wire(...))。"""
        if obj is not None:
            self._objects.append(obj)
        return obj

    def release(self) -> int:
        """显式掉所有登记对象并触发一次回收，返回释放个数。"""
        count = len(self._objects)
        for index in range(count):
            self._objects[index] = None      # 逐个置空，先切断 Python 侧引用
        self._objects.clear()
        if count:
            _collect()
            if self._on_release is not None:
                self._on_release(count)
        return count

    def __enter__(self) -> "ReleaseScope":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False        # 不吞异常


class OcctEngine(GeometryEngine):
    name = "occt"

    def __init__(self, deflection: float = 0.02) -> None:
        self._m = _load_bindings()
        self._deflection = deflection          # 曲线离散弦高容差 mm
        self._released = 0                     # 累计释放的 OCCT 对象个数
        self._calls = 0                        # OCCT 运算次数

    # ------------------------------------------------------------ 资源
    def _scope(self) -> ReleaseScope:
        return ReleaseScope(self._on_release)

    def _on_release(self, count: int) -> None:
        self._released += count

    def resource_stats(self) -> Dict[str, int]:
        """资源统计：接口 /api/geometry/info 与内存泄漏测试都看这个。"""
        return {"calls": self._calls, "released": self._released, "gcRuns": _GC_RUNS}

    # ------------------------------------------------------------ 基础
    def available(self) -> bool:
        return self._m is not None

    def _wire(self, pts: np.ndarray, closed: bool):
        m = self._m
        assert m is not None
        pts = dedup_points(np.asarray(pts, dtype=np.float64))
        if len(pts) < 2:
            return None
        builder = m["BRepBuilderAPI"].BRepBuilderAPI_MakePolygon()
        gp_Pnt = m["gp"].gp_Pnt
        step = 1
        if pts.shape[0] > 20000:      # 超大轮廓抽稀建线，避免建线爆炸
            step = max(1, pts.shape[0] // 20000)
        for x, y in pts[::step]:
            builder.Add(gp_Pnt(float(x), float(y), 0.0))
        if closed:
            builder.Close()
        if not builder.IsDone():
            return None
        return builder.Wire()

    def _face(self, pts: np.ndarray, scope: Optional[ReleaseScope] = None):
        """闭合轮廓 -> 平面 Face，用于布尔运算。

        传入 scope 时，建线过程中的 Wire / Maker / Face 都会被登记，
        由调用方统一释放；否则靠局部变量出作用域回收。
        """
        m = self._m
        assert m is not None
        wire = self._wire(pts, True)
        if wire is None:
            return None
        maker = m["BRepBuilderAPI"].BRepBuilderAPI_MakeFace(wire, True)
        if not maker.IsDone():
            return None
        face = maker.Face()
        if scope is not None:
            scope.hold(wire)
            scope.hold(maker)
            scope.hold(face)
        return face

    def _shape_list(self, items):
        """Python list -> TopTools_ListOfShape。

        OCP/pythonocc 的 SetArguments/SetTools 只接受 TopTools_ListOfShape，
        直接传 list 会报 "incompatible function arguments"。
        """
        result = self._m["TopTools"].TopTools_ListOfShape()
        for item in items:
            result.Append(item)
        return result

    def _to_polylines(self, shape, out_layer: int = 0, start_id: int = 1,
                      include_edges: bool = False) -> List[Polyline]:
        """把 Shape 里的所有 Wire 离散成折线。

        偏置/并集的结果都是 Wire；但布尔求交在"只碰到边"时结果只有 EDGE，
        所以给 include_edges=True 时退化按边输出，避免返回空集。

        注意：BRepAdaptor_Curve 只读底层曲线参数，不考虑 Edge 在 Wire 里的
        方向（TopAbs_REVERSED），不处理的话圆弧会被反向遍历，
        拼出来的轮廓缺边并自交。所以每条边都按 orientation 反转一次。
        """
        if shape is None or shape.IsNull():
            return []
        m = self._m
        assert m is not None
        top_exp = m["TopExp"].TopExp_Explorer(shape, m["TopAbs"].TopAbs_WIRE)
        wire_exp_cls = m["BRepTools"].BRepTools_WireExplorer
        adaptor_cls = m["BRepAdaptor"].BRepAdaptor_Curve
        disc_cls = m["GCPnts"].GCPnts_QuasiUniformDeflection
        reversed_flag = m["TopAbs"].TopAbs_REVERSED

        result: List[Polyline] = []
        pid = start_id
        wire_count = 0
        while top_exp.More():
            wire = m["TopoDS"].TopoDS.Wire_s(top_exp.Current())
            pts: List[List[float]] = []
            wexp = wire_exp_cls(wire)
            while wexp.More():
                edge = wexp.Current()
                curve = adaptor_cls(edge)
                disc = disc_cls(curve, self._deflection)
                if disc.IsDone():
                    segment = [[disc.Value(i).X(), disc.Value(i).Y()]
                               for i in range(1, disc.NbPoints() + 1)]
                    if edge.Orientation() == reversed_flag:
                        segment.reverse()
                    # 相邻边共享端点，去重；不用固定跳过第 1 个点，
                    # 因为弧边反向时重复端点在尾部
                    if pts and segment and \
                            np.linalg.norm(np.subtract(segment[0], pts[-1])) < 1e-6:
                        segment = segment[1:]
                    pts.extend(segment)
                # curve / disc 持有 OCCT 句柄，逐边释放避免堆积
                del disc, curve, edge
                wexp.Next()
            if len(pts) >= 2:
                arr = np.asarray(pts, dtype=np.float32)
                closed = bool(np.linalg.norm(arr[0] - arr[-1]) < 1e-3)
                if closed and len(arr) > 3:
                    arr = arr[:-1]
                result.append(Polyline(pid, out_layer, arr, closed))
                pid += 1
            del wexp, wire
            wire_count += 1
            if wire_count % _GC_INTERVAL == 0:
                _collect()
            top_exp.Next()
        del top_exp

        if not result and include_edges:
            return self._edges_to_polylines(shape, out_layer, start_id)
        return result

    def _edges_to_polylines(self, shape, out_layer: int, start_id: int) -> List[Polyline]:
        """退化路径：直接按 Edge 离散（求交结果只剩边时用）。"""
        m = self._m
        assert m is not None
        edge_exp = m["TopExp"].TopExp_Explorer(shape, m["TopAbs"].TopAbs_EDGE)
        adaptor_cls = m["BRepAdaptor"].BRepAdaptor_Curve
        disc_cls = m["GCPnts"].GCPnts_QuasiUniformDeflection

        result: List[Polyline] = []
        pid = start_id
        while edge_exp.More():
            edge = m["TopoDS"].TopoDS.Edge_s(edge_exp.Current())
            curve = adaptor_cls(edge)
            disc = disc_cls(curve, self._deflection)
            if disc.IsDone():
                pts = [[disc.Value(i).X(), disc.Value(i).Y()]
                       for i in range(1, disc.NbPoints() + 1)]
                if len(pts) >= 2:
                    result.append(Polyline(pid, out_layer,
                                           np.asarray(pts, dtype=np.float32), False))
                    pid += 1
            del disc, curve, edge
            edge_exp.Next()
        del edge_exp
        return result

    # ------------------------------------------------------------ 偏置
    def offset(self, polylines: Sequence[Polyline], distance: float,
               join: str = JOIN_ROUND) -> List[Polyline]:
        """轮廓偏置（刀具补偿）；正距离恒为外扩。"""
        m = self._m
        if m is None or abs(distance) < 1e-9:
            return list(polylines)

        join_type = {
            JOIN_ROUND: m["GeomAbs"].GeomAbs_Arc,
            JOIN_MITER: m["GeomAbs"].GeomAbs_Intersection,
            JOIN_SQUARE: m["GeomAbs"].GeomAbs_Intersection,
        }.get(join, m["GeomAbs"].GeomAbs_Arc)

        result: List[Polyline] = []
        self._calls += 1
        for index, poly in enumerate(polylines):
            pts = np.asarray(poly.pts, dtype=np.float64)
            # 归一化为逆时针：OCCT 的偏移方向与 Wire 走向绑定，
            # 不归一化的话同一个 +5mm 对顺时针轮廓会变成内缩
            if poly.closed:
                pts = ensure_ccw(pts)
            # 一次偏置一个 ReleaseScope：退出即释放 wire/maker/shape 并回收
            with self._scope() as scope:
                wire = scope.hold(self._wire(pts, poly.closed))
                if wire is None:
                    continue
                maker = scope.hold(m["BRepOffsetAPI"].BRepOffsetAPI_MakeOffset())
                try:
                    maker.Init(join_type, not poly.closed)   # 开放轮廓 => openResult
                    maker.AddWire(wire)
                    maker.Perform(float(distance))
                    if not maker.IsDone():
                        logger.debug("OCCT 偏置未完成 id=%s", poly.id)
                        continue
                    shape = scope.hold(maker.Shape())
                    result.extend(self._to_polylines(shape, poly.layer_id, poly.id * 1000))
                except Exception as exc:      # 单条轮廓失败不影响整体
                    logger.debug("OCCT 偏置失败 id=%s: %s", poly.id, exc)
            if (index + 1) % _GC_INTERVAL == 0:
                _collect()
        return result

    # ------------------------------------------------------------ 布尔并集
    def union(self, polylines: Sequence[Polyline]) -> List[Polyline]:
        m = self._m
        if m is None or len(polylines) < 2:
            return list(polylines)

        self._calls += 1
        with self._scope() as scope:
            faces = []
            for poly in polylines:
                if not poly.closed or poly.point_count < 3:
                    continue
                face = self._face(ensure_ccw(np.asarray(poly.pts, dtype=np.float64)), scope)
                if face is not None:
                    faces.append(face)
            if len(faces) < 2:
                return list(polylines)
            try:
                fuse = scope.hold(m["BRepAlgoAPI"].BRepAlgoAPI_Fuse())
                fuse.SetArguments(self._shape_list(faces))
                fuse.SetNonDestructive(True)
                fuse.Build()
                if not fuse.IsDone():
                    return list(polylines)
                shape = scope.hold(fuse.Shape())
                return self._to_polylines(shape, polylines[0].layer_id)
            except Exception as exc:
                logger.warning("OCCT 并集失败，返回原始轮廓: %s", exc)
                return list(polylines)

    # ------------------------------------------------------------ 布尔交集
    def intersect(self, subject: Sequence[Polyline],
                  clip: Sequence[Polyline]) -> List[Polyline]:
        """轮廓求交：返回两组轮廓公共区域的边界（模块 2）。

        只对闭合轮廓做面积求交；开放轮廓不参与。
        入参 subject / clip 各自可以有多条轮廓（当成一个整体）。
        """
        m = self._m
        if m is None:
            return []
        subject_items = [p for p in subject if p.closed and p.point_count >= 3]
        clip_items = [p for p in clip if p.closed and p.point_count >= 3]
        if not subject_items or not clip_items:
            return []

        self._calls += 1
        with self._scope() as scope:
            args = []
            for poly in subject_items:
                face = self._face(ensure_ccw(np.asarray(poly.pts, dtype=np.float64)), scope)
                if face is not None:
                    args.append(face)
            tools = []
            for poly in clip_items:
                face = self._face(ensure_ccw(np.asarray(poly.pts, dtype=np.float64)), scope)
                if face is not None:
                    tools.append(face)
            if not args or not tools:
                return []
            try:
                common = scope.hold(m["BRepAlgoAPI"].BRepAlgoAPI_Common())
                common.SetArguments(self._shape_list(args))
                common.SetTools(self._shape_list(tools))
                common.SetNonDestructive(True)
                common.Build()
                if not common.IsDone():
                    logger.warning("OCCT 求交未完成")
                    return []
                shape = scope.hold(common.Shape())
                return self._to_polylines(shape, subject_items[0].layer_id,
                                          include_edges=True)
            except Exception as exc:
                logger.warning("OCCT 求交失败: %s", exc)
                return []
