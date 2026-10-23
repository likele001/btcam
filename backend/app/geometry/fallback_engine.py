"""零依赖退化引擎：仅在 OCCT 与 pyclipper 都未安装时启用。

只做"按包围盒缩放"的粗偏置，仅用于保证软件能启动、界面能联调，
绝不可用于真实生产加工。
"""

from __future__ import annotations

import logging
from typing import List, Sequence

import numpy as np

from app.geometry.base import JOIN_ROUND, GeometryEngine
from app.models.geometry import Polyline

logger = logging.getLogger(__name__)


class FallbackEngine(GeometryEngine):
    name = "fallback"

    def available(self) -> bool:
        return True

    def offset(self, polylines: Sequence[Polyline], distance: float,
               join: str = JOIN_ROUND) -> List[Polyline]:
        out: List[Polyline] = []
        for poly in polylines:
            pts = np.asarray(poly.pts, dtype=np.float64)
            if len(pts) < 2:
                continue
            center = pts.mean(axis=0)
            vec = pts - center
            norm = np.linalg.norm(vec, axis=1, keepdims=True)
            norm[norm < 1e-9] = 1.0
            shifted = (center + vec + vec / norm * distance).astype(np.float32)
            out.append(Polyline(poly.id, poly.layer_id, shifted, poly.closed))
        return out

    def union(self, polylines: Sequence[Polyline]) -> List[Polyline]:
        return list(polylines)
