"""2D 几何引擎抽象层。

为什么要抽象：
- OCCT 的 Python 绑定在 Windows 上安装方式不同（pythonocc-core / cadquery-ocp），
  打包后体积也大；
- 偏置 / 布尔这类操作在不同引擎上行为略有差异，业务层不应直接依赖具体实现。

业务层只拿 `get_engine()` 返回的对象，调用统一方法即可。
"""

from __future__ import annotations

import logging
from typing import List, Protocol, Sequence, runtime_checkable

import numpy as np

from app.models.geometry import Polyline

logger = logging.getLogger(__name__)

# 偏置拐角处理方式
JOIN_ROUND = "round"
JOIN_MITER = "miter"
JOIN_SQUARE = "square"


@runtime_checkable
class GeometryEngine(Protocol):
    """几何引擎契约。所有坐标单位 mm，返回轮廓的 layer_id 由调用方重写。"""

    name: str

    def available(self) -> bool: ...

    def offset(self, polylines: Sequence[Polyline], distance: float,
               join: str = JOIN_ROUND) -> List[Polyline]:
        """轮廓外扩/内缩。distance > 0 外扩，< 0 内缩。"""
        ...

    def union(self, polylines: Sequence[Polyline]) -> List[Polyline]:
        """并集：把重叠/相交的闭合轮廓融合成外轮廓 + 内孔。"""
        ...

    def intersect(self, subject: Sequence[Polyline],
                  clip: Sequence[Polyline]) -> List[Polyline]:
        """交集：返回 subject 与 clip 的公共区域轮廓（模块2 轮廓求交）。"""
        ...


def ensure_ccw(pts: np.ndarray) -> np.ndarray:
    """统一为逆时针，保证偏置时正距离一定是"外扩"。

    Clipper 与 OCCT 对偏置方向的约定都依赖轮廓走向，
    所以两个引擎入口都先过一遍这个函数，行为才对得上。
    """
    if len(pts) < 3:
        return pts
    area = _signed_area(pts)
    return pts if area >= 0 else pts[::-1]


def ensure_cw(pts: np.ndarray) -> np.ndarray:
    """统一为顺时针（内孔约定，有向面积为负）。

    与 ensure_ccw 配对使用：偏移/布尔的结果必须按外 CCW / 内 CW 归一，
    否则前端按面积正负判断内外会得到相反结论。
    """
    if len(pts) < 3:
        return pts
    return pts if _signed_area(pts) <= 0 else pts[::-1]


def _signed_area(pts: np.ndarray) -> float:
    """鞋带公式有向面积，逆时针为正。"""
    x, y = pts[:, 0], pts[:, 1]
    return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def dedup_points(pts: np.ndarray, tol: float = 1e-4) -> np.ndarray:
    """去掉相邻重复点，避免 OCCT 建线失败（CAM 导入 DXF 后极常见）。"""
    if len(pts) < 2:
        return pts
    keep = [0]
    for i in range(1, len(pts)):
        if np.linalg.norm(pts[i] - pts[keep[-1]]) > tol:
            keep.append(i)
    return pts[keep]


def chain_polylines(polylines: Sequence[Polyline], tol: float = 0.05) -> List[Polyline]:
    """把首尾相接的开放折线串成更长的链。

    导入的 DXF 常被炸成很多小段，直接做偏置会得到一堆断头轮廓，
    所以刀路计算前必须先串接。
    """
    open_items = [p for p in polylines if not p.closed and p.point_count >= 2]
    closed_items = [p for p in polylines if p.closed]
    used = [False] * len(open_items)
    result: List[Polyline] = []

    next_id = 1_000_000
    for i, head in enumerate(open_items):
        if used[i]:
            continue
        used[i] = True
        chain = [np.asarray(head.pts, dtype=np.float32)]
        layer_id = head.layer_id

        extended = True
        while extended:
            extended = False
            tail = chain[-1][-1]
            for j, cand in enumerate(open_items):
                if used[j]:
                    continue
                pts = np.asarray(cand.pts, dtype=np.float32)
                c0, c1 = pts[0], pts[-1]
                # 接在尾部：候选的起点贴 tail 就顺着走，终点贴 tail 就反转走。
                # 衔接点会重复，所以一律跳过首点，否则闭合判定会把重复点算进去。
                if np.linalg.norm(c0 - tail) <= tol:
                    chain.append(pts[1:])
                elif np.linalg.norm(c1 - tail) <= tol:
                    chain.append(pts[::-1][1:])
                else:
                    continue
                used[j] = True
                extended = True
                break

        merged = np.vstack(chain)
        # 首尾重合 => 视为闭合轮廓
        closed = np.linalg.norm(merged[0] - merged[-1]) <= tol and len(merged) > 3
        if closed:
            merged = merged[:-1]
        result.append(Polyline(next_id, layer_id, merged, closed))
        next_id += 1

    result.extend(closed_items)
    return result


def get_engine(preferred: str = "auto") -> GeometryEngine:
    """引擎工厂：occt 优先，失败自动降级到 clipper，最后是纯 Python 兜底。"""
    from app.geometry.occt_engine import OcctEngine
    from app.geometry.clipper_engine import ClipperEngine

    if preferred in ("auto", "occt"):
        engine = OcctEngine()
        if engine.available():
            return engine
        if preferred == "occt":
            logger.error("已指定 OCCT 但未安装绑定，回退到 clipper")
        else:
            logger.warning("未检测到 OCCT Python 绑定，回退到 pyclipper 引擎")

    engine = ClipperEngine()
    if engine.available():
        return engine

    logger.error("pyclipper 也未安装，使用退化引擎（仅支持凸多边形粗偏置）")
    from app.geometry.fallback_engine import FallbackEngine
    return FallbackEngine()
