"""pyclipper 几何引擎（OCCT 不可用时的生产级兜底）。

pyclipper 是 Clipper 的 Python 绑定，专门做 2D 多边形偏置 / 布尔，
速度快、结果稳定，非常适合板式家具这类纯 2D 轮廓场景。
坐标以 1/1000 mm 定点数参与运算，避免浮点误差。
"""

from __future__ import annotations

import logging
from typing import List, Sequence

import numpy as np

from app.geometry.base import JOIN_ROUND, GeometryEngine, ensure_ccw
from app.models.geometry import Polyline

logger = logging.getLogger(__name__)

SCALE = 1000.0          # 定点比例：1 mm = 1000 单位


class ClipperEngine(GeometryEngine):
    name = "clipper"

    def __init__(self) -> None:
        try:
            import pyclipper  # noqa: F401
            self._pc = pyclipper
        except ImportError:
            self._pc = None

    def available(self) -> bool:
        return self._pc is not None

    # ------------------------------------------------------------ 工具
    def _to_path(self, pts: np.ndarray) -> List[tuple]:
        return [(int(round(x * SCALE)), int(round(y * SCALE))) for x, y in pts]

    def _from_path(self, path: Sequence[tuple], layer_id: int, pid: int) -> Polyline:
        arr = np.asarray(path, dtype=np.float64) / SCALE
        return Polyline(pid, layer_id, arr.astype(np.float32), closed=True)

    # ------------------------------------------------------------ 偏置
    def offset(self, polylines: Sequence[Polyline], distance: float,
               join: str = JOIN_ROUND) -> List[Polyline]:
        if self._pc is None:
            return list(polylines)
        pc = self._pc
        delta = float(distance) * SCALE
        out: List[Polyline] = []
        pid = 1_000_000

        for poly in polylines:
            if poly.point_count < 2:
                continue
            if not poly.closed:
                # 开放轮廓：pyclipper 只回"带宽度区域"，这里自己算法向偏置保留中心线
                shifted = offset_open_path(poly.pts, float(distance))
                if shifted is not None:
                    out.append(Polyline(pid, poly.layer_id, shifted, False))
                    pid += 1
                continue

            pts = ensure_ccw(np.asarray(poly.pts, dtype=np.float64))
            co = pc.PyclipperOffset()
            co.AddPath(self._to_path(pts), pc.JT_ROUND, pc.ET_CLOSEDPOLYGON)
            solution = co.Execute(delta)
            for path in solution:
                if len(path) >= 3:
                    out.append(self._from_path(path, poly.layer_id, pid))
                    pid += 1
        return out

    # ------------------------------------------------------------ 布尔并集
    def union(self, polylines: Sequence[Polyline]) -> List[Polyline]:
        if self._pc is None:
            return list(polylines)
        pc = self._pc
        clipper = pc.Pyclipper()
        added = 0
        for poly in polylines:
            if not poly.closed or poly.point_count < 3:
                continue
            pts = ensure_ccw(np.asarray(poly.pts, dtype=np.float64))
            clipper.AddPath(self._to_path(pts), pc.PT_SUBJECT, True)
            added += 1
        if added < 2:
            return list(polylines)

        solution = clipper.Execute(pc.CT_UNION, pc.PFT_NONZERO, pc.PFT_NONZERO)
        layer_id = polylines[0].layer_id
        return [self._from_path(path, layer_id, 2_000_000 + i)
                for i, path in enumerate(solution) if len(path) >= 3]

    # ------------------------------------------------------------ 布尔交集
    def intersect(self, subject: Sequence[Polyline],
                  clip: Sequence[Polyline]) -> List[Polyline]:
        """两个轮廓集的公共区域（模块2 轮廓求交）。

        subject / clip 各自可以有多条轮廓（多轮廓组会先当作一个整体求交）。
        开放轮廓不参与面积求交，直接忽略。
        """
        if self._pc is None:
            return []
        pc = self._pc
        clipper = pc.Pyclipper()
        added = 0
        for poly in subject:
            if not poly.closed or poly.point_count < 3:
                continue
            clipper.AddPath(self._to_path(ensure_ccw(np.asarray(poly.pts, dtype=np.float64))),
                            pc.PT_SUBJECT, True)
            added += 1
        if added == 0:
            return []
        added = 0
        for poly in clip:
            if not poly.closed or poly.point_count < 3:
                continue
            clipper.AddPath(self._to_path(ensure_ccw(np.asarray(poly.pts, dtype=np.float64))),
                            pc.PT_CLIP, True)
            added += 1
        if added == 0:
            return []

        # 注意：pyclipper 1.3 的 Execute 只接受 3 个位置参数
        # （clipType, subjFillType, clipFillType），多传一个会直接 TypeError
        solution = clipper.Execute(pc.CT_INTERSECTION, pc.PFT_NONZERO, pc.PFT_NONZERO)
        layer_id = subject[0].layer_id
        return [self._from_path(path, layer_id, 3_000_000 + i)
                for i, path in enumerate(solution) if len(path) >= 3]


# ---------------------------------------------------------------- 纯 numpy 工具
def offset_open_path(pts: np.ndarray, delta: float) -> np.ndarray | None:
    """开放折线的法向偏置（斜接 join），用于单线雕刻 / 划槽的中心线补偿。"""
    pts = np.asarray(pts, dtype=np.float64)
    if len(pts) < 2 or abs(delta) < 1e-9:
        return pts.astype(np.float32)

    seg_vec = np.diff(pts, axis=0)
    lengths = np.linalg.norm(seg_vec, axis=1)
    valid = lengths > 1e-9
    if not np.any(valid):
        return None
    seg_vec, lengths = seg_vec[valid], lengths[valid]
    # 左法向：(-dy, dx)/|d|
    normals = np.stack([-seg_vec[:, 1], seg_vec[:, 0]], axis=1) / lengths[:, None]

    out: List[np.ndarray] = [pts[0] + normals[0] * delta]
    for i in range(len(normals) - 1):
        p = pts[i + 1]
        n1, n2 = normals[i], normals[i + 1]
        miter = n1 + n2
        miter_len = np.linalg.norm(miter)
        if miter_len < 1e-6:                     # 180° 折返，直接退化
            out.append(p + n1 * delta)
        else:
            miter /= miter_len
            # 斜接长度补偿：1/cos(θ/2)
            denom = float(np.dot(miter, n1))
            scale = 1.0 / denom if abs(denom) > 0.2 else 1.0
            out.append(p + miter * delta * scale)
    out.append(pts[-1] + normals[-1] * delta)
    return np.asarray(out, dtype=np.float32)
