"""画布手动排料服务（模块 3）。

职责边界（与项目总约束一致：几何计算全在后端，前端只渲染）：
    板材参数  -> layout_sheet 表（宽 / 高 / 四边余量 / 零件最小间距）
    摆放状态  -> layout_placement 表（tx / ty / rot / locked）
    合法性    -> 越界判定 + 零件重叠判定（pyclipper 求交，含最小间距）
    输出      -> 按摆放状态变换后的轮廓，前端拿到即可绘制

坐标约定（前后端共用）：
    板材左下角为原点，X 向右、Y 向上，单位 mm；
    tx/ty 表示"零件旋转后包围盒左下角"在板材上的位置。
    这样前端拖动零件时只需 tx += dx / ty += dy，不需要在前端做旋转补偿，
    旋转则以零件包围盒中心为锚点，视觉上不会跳。

为什么不在前端做这几件事：
    - 越界/重叠判定要处理旋转后的多边形，属于几何计算，前端只做像素渲染；
    - 摆料结果是可持久化数据（工单回读），必须由后端落库后下发。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from app.models import db
from app.models.geometry import Polyline

logger = logging.getLogger(__name__)

# pyclipper 定点比例：1 mm = 1000 单位（与 ClipperEngine 保持一致）
SCALE = 1000.0
# 排料视图里轮廓 id 的起始值：与图纸轮廓（数据库自增 id）天然分开，
# 前端据此不会把"排料结果"误当成"原始图形"。
LAYOUT_POLY_BASE = 200_000
# 浮点比较容差（mm）
EPS = 1e-6
# 判定"真重叠"的最小面积（mm²），避免相切被误判
MIN_OVERLAP_AREA = 1e-3
# 停靠件与板材上边界的距离（mm）：留一条缝，前端画出来不会和板框粘在一起
PARK_OFFSET = 10.0
# 复制零件时沿原件右侧/下方外推的候选步数（再多就交给停靠区，避免拖慢响应）
COPY_SCAN_STEPS = 8
# 新建零件时板内扫描的网格上限（行 x 列）：新建是单件操作，扫太密只会让响应变慢
SCAN_MAX_ROWS = 40
SCAN_MAX_COLS = 40
# 圆用正多边形近似时的分段数：切割路径本来就是折线，段数够多看起来就是圆
CIRCLE_SEGMENTS = 64
# 作图新建的轮廓单独归一个图层：将来导回图纸时一眼能看出是手画的
SHAPE_LAYER_NAME = "作图"
SHAPE_LAYER_COLOR = 0xFFB020


# ---------------------------------------------------------------- 数据结构
@dataclass
class LayoutPart:
    """可摆料的零件：轮廓已平移到自身包围盒左下角（原点归一化）。"""

    id: int
    name: str
    contours: List[np.ndarray]          # 全部轮廓（外轮廓 + 内孔），已归一化
    width: float
    height: float
    outline: np.ndarray                 # 碰撞用外轮廓（面积最大的那条）
    outline_index: int                  # 外轮廓在 contours 中的下标
    area: float                         # 净面积（外轮廓 - 内孔）

    def rotated_contours(self, rot: float, tx: float, ty: float) -> List[np.ndarray]:
        """绕包围盒中心旋转，再平移；返回的包围盒左下角正好落在 (tx, ty)。"""
        rad = math.radians(rot)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        cx, cy = self.width / 2.0, self.height / 2.0

        rotated: List[np.ndarray] = []
        for pts in self.contours:
            dx = pts[:, 0].astype(np.float64) - cx
            dy = pts[:, 1].astype(np.float64) - cy
            rotated.append(np.stack([cx + dx * cos_a - dy * sin_a,
                                     cy + dx * sin_a + dy * cos_a], axis=1))

        all_pts = np.concatenate(rotated, axis=0) if rotated else np.zeros((1, 2))
        min_x, min_y = float(all_pts[:, 0].min()), float(all_pts[:, 1].min())
        shift = np.array([tx - min_x, ty - min_y], dtype=np.float64)
        return [(arr + shift).astype(np.float32) for arr in rotated]

    def rotated_outline(self, rot: float, tx: float, ty: float) -> np.ndarray:
        """只算碰撞用的外轮廓，避免每次都变换全部内孔。"""
        rad = math.radians(rot)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        cx, cy = self.width / 2.0, self.height / 2.0
        pts = self.outline.astype(np.float64)
        dx, dy = pts[:, 0] - cx, pts[:, 1] - cy
        arr = np.stack([cx + dx * cos_a - dy * sin_a,
                        cy + dx * sin_a + dy * cos_a], axis=1)
        min_x, min_y = float(arr[:, 0].min()), float(arr[:, 1].min())
        return arr - np.array([min_x - tx, min_y - ty], dtype=np.float64)

    def size_at(self, rot: float) -> Tuple[float, float]:
        """旋转后的包围盒尺寸（用于越界判定，不必变换全部顶点）。"""
        if abs(rot % 180.0) < 1e-9:
            return self.width, self.height
        if abs(rot % 180.0 - 90.0) < 1e-9:
            return self.height, self.width
        rad = math.radians(rot)
        cos_a, sin_a = abs(math.cos(rad)), abs(math.sin(rad))
        return self.width * cos_a + self.height * sin_a, \
            self.width * sin_a + self.height * cos_a


@dataclass
class Placement:
    """一个零件在板材上的摆放状态。"""

    part_id: int
    sheet: int = 0
    tx: float = 0.0
    ty: float = 0.0
    rot: float = 0.0
    locked: bool = False
    zorder: int = 0
    # 木纹方向锁定：自动排料不旋转它，手动旋转也被拒绝（需先解锁）
    fixed_rot: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "partId": self.part_id,
            "sheet": self.sheet,
            "tx": round(self.tx, 3),
            "ty": round(self.ty, 3),
            "rot": round(self.rot, 3),
            "locked": self.locked,
            "fixedRot": self.fixed_rot,
            "zorder": self.zorder,
        }


@dataclass
class LayoutState:
    """一次排料操作所需的全量上下文。"""

    project_id: int
    sheet: Dict[str, float]
    parts: List[LayoutPart] = field(default_factory=list)
    placements: Dict[int, Placement] = field(default_factory=dict)
    # 余料边界（模块 10）：None = 规则矩形板；否则为闭合多边形（板材坐标）。
    # 边界属于“几何约束”，所以留在后端校验，前端只拿轮廓来绘制
    boundary: Optional[np.ndarray] = None
    # 余料元信息（id / 名称 / 尺寸），仅用于下发与界面展示
    scrap: Optional[Dict[str, Any]] = None

    def part(self, part_id: int) -> Optional[LayoutPart]:
        return next((p for p in self.parts if p.id == part_id), None)


@dataclass
class LayoutResult:
    """一次写操作的结果：ok=False 时 reason 给前端弹提示，state 始终是最新状态。"""

    ok: bool
    reason: str
    state: LayoutState
    # 额外的响应字段（如复制件数/停靠件数），只回给发起方，不进 WS 广播
    extra: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------- 服务类
class LayoutService:
    """零件摆放与板材参数的服务类（无状态，所有数据来自 SQLite）。"""

    def __init__(self) -> None:
        try:
            import pyclipper
            self._pc = pyclipper
        except ImportError:                     # pragma: no cover - 依赖缺失时的兜底
            self._pc = None
            logger.warning("pyclipper 不可用：排料的零件重叠校验将退化为包围盒判定")

    # ------------------------------------------------------------ 载入
    def load(self, conn: Any, project_id: int) -> LayoutState:
        """从数据库组装排料上下文（轮廓归一化、摆放状态原样读出）。"""
        sheet = db.load_sheet(conn, project_id)
        # 模块 10：排料板绑定了余料时，真正的边界是那块料的轮廓。
        # 余料被删掉了就地解绑，避免拿着幽灵轮廓继续做越界判定
        boundary: Optional[np.ndarray] = None
        scrap: Optional[Dict[str, Any]] = None
        if int(sheet.get("scrapId") or 0):
            row = db.load_scrap(conn, project_id, int(sheet["scrapId"]))
            if row is None:
                db.set_sheet_scrap(conn, project_id, 0)
                sheet["scrapId"] = 0
            else:
                boundary = np.asarray(row["outline"], dtype=np.float64)
                scrap = {
                    "id": int(row["id"]),
                    "name": str(row["name"]),
                    "code": str(row["code"]),
                    "material": str(row["material"]),
                    "thickness": float(row["thickness"]),
                    "width": float(row["width"]),
                    "height": float(row["height"]),
                    "area": float(row["area"]),
                    "outline": row["outline"],
                }

        raw_parts = db.load_parts(conn, project_id)
        rows = db.load_polylines(conn, project_id)
        placements = {pid: Placement(
            part_id=pid, sheet=int(p["sheet"]), tx=float(p["tx"]), ty=float(p["ty"]),
            rot=float(p["rot"]), locked=bool(p["locked"]), zorder=int(p["zorder"]),
            fixed_rot=bool(p.get("fixedRot", False)),
        ) for pid, p in db.load_placements(conn, project_id).items()}

        by_part: Dict[int, List[Dict[str, Any]]] = {}
        for row in rows:
            if row["part_id"]:
                by_part.setdefault(int(row["part_id"]), []).append(row)

        parts: List[LayoutPart] = []
        for raw in raw_parts:
            items = by_part.get(int(raw["id"]), [])
            if not items:
                continue
            part = self._make_part(int(raw["id"]), str(raw["name"]), items)
            if part is not None:
                parts.append(part)

        # 防御：摆放记录里可能残留已删除零件的 part_id（旧库/异常中断），直接丢弃
        known = {p.id for p in parts}
        placements = {pid: p for pid, p in placements.items() if pid in known}

        return LayoutState(project_id=project_id, sheet=sheet, parts=parts,
                           placements=placements, boundary=boundary, scrap=scrap)

    @staticmethod
    def _make_part(part_id: int, name: str, items: Sequence[Dict[str, Any]]) -> Optional[LayoutPart]:
        """把零件下所有轮廓一起平移到包围盒左下角（共用同一个偏移，内孔不会错位）。"""
        arrays = [np.asarray(it["pts"], dtype=np.float64) for it in items if len(it["pts"]) > 0]
        if not arrays:
            return None

        all_pts = np.concatenate(arrays, axis=0)
        min_xy = all_pts.min(axis=0)
        normalized = [(arr - min_xy).astype(np.float32) for arr in arrays]
        width = float(all_pts[:, 0].max() - min_xy[0])
        height = float(all_pts[:, 1].max() - min_xy[1])

        # 外轮廓 = 面积绝对值最大的那条；净面积 = 外轮廓 - 其余（内孔）
        # 不依赖轮廓方向：示例图形/第三方 DXF 的内孔方向未必统一
        areas = [abs(LayoutService._signed_area(arr)) for arr in normalized]
        outline_idx = int(np.argmax(areas)) if areas else 0
        outline = normalized[outline_idx].astype(np.float64)
        if sum(areas) <= 0:
            return None
        net_area = max(0.0, areas[outline_idx] - (sum(areas) - areas[outline_idx]))

        return LayoutPart(id=part_id, name=name, contours=normalized,
                          width=max(width, EPS), height=max(height, EPS),
                          outline=outline, outline_index=outline_idx, area=net_area)

    @staticmethod
    def _signed_area(pts: np.ndarray) -> float:
        if len(pts) < 3:
            return 0.0
        x, y = pts[:, 0], pts[:, 1]
        return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))

    # ------------------------------------------------------------ 初始化
    def open(self, conn: Any, project_id: int) -> LayoutState:
        """进入排料模式：没有摆放记录时按行排做一次初始摆放。

        追加导入（replace=false）或手工新建零件后，会出现"有零件但没摆放记录"，
        这里一并补摆，否则它们只在数据库里、画布上永远看不见。
        """
        state = self.load(conn, project_id)
        if not state.parts:
            return state
        if not state.placements:
            self._initial_layout(conn, state)
            state.placements = {pid: Placement(
                part_id=pid, sheet=int(p["sheet"]), tx=float(p["tx"]), ty=float(p["ty"]),
                rot=float(p["rot"]), locked=bool(p["locked"]), zorder=int(p["zorder"]),
                fixed_rot=bool(p.get("fixedRot", False)),
            ) for pid, p in db.load_placements(conn, project_id).items()}
        else:
            self._place_new_parts(conn, state)
        return state

    def _place_new_parts(self, conn: Any, state: LayoutState) -> int:
        """给还没有摆放记录的零件找位置（追加导入 / 新建零件后补摆）。

        先按网格扫描找板内空位，整板都放不下就停靠到板外上方，
        与复制零件的口径一致：不静默叠在已有零件上。
        """
        pending = [part for part in state.parts if part.id not in state.placements]
        if not pending:
            return 0
        zorder = max((item.zorder for item in state.placements.values()), default=-1) + 1
        placed = 0
        for index, part in enumerate(pending):
            spot = self._scan_spot(state, part, 0.0)
            if spot is None:
                tx, ty = self._park_spot(state, part, 0.0)
            else:
                tx, ty = spot
            state.placements[part.id] = Placement(
                part_id=part.id, sheet=0, tx=tx, ty=ty, rot=0.0,
                locked=False, zorder=zorder + index)
            db.save_placement(conn, state.project_id, part.id, 0, tx, ty, 0.0,
                              locked=False, zorder=zorder + index)
            placed += 1
        if placed:
            logger.info("补摆 %d 个新零件（追加导入/新建）", placed)
        return placed

    def _initial_layout(self, conn: Any, state: LayoutState) -> None:
        """行排初始摆放：高件优先，自下而上逐行摆。

        这只是"进排料模式时的初始位置"，不追求利用率（自动排料是模块 4）；
        超出板材的零件继续往下摆并在 stats 里报 overflow，用户能看见也能拖回来。
        """
        margin = float(state.sheet["margin"])
        gap = float(state.sheet["gap"])
        sheet_w = float(state.sheet["width"])

        ordered = sorted(state.parts, key=lambda p: (p.height, p.width), reverse=True)
        x, y, row_h = margin, margin, 0.0
        for index, part in enumerate(ordered):
            if x + part.width + margin > sheet_w and row_h > 0:
                x = margin
                y += row_h + gap
                row_h = 0.0
            db.save_placement(conn, state.project_id, part.id, 0,
                              round(x, 3), round(y, 3), 0.0, False, index)
            x += part.width + gap
            row_h = max(row_h, part.height)

    # ------------------------------------------------------------ 写操作
    def set_sheet(self, conn: Any, project_id: int, width: float, height: float,
                  margin: float, gap: float) -> LayoutResult:
        """保存板材参数；越界零件夹回可用区域（不改变相对布局）。"""
        if width <= 0 or height <= 0:
            return LayoutResult(False, "板材尺寸必须大于 0", self.load(conn, project_id))
        if margin < 0 or gap < 0:
            return LayoutResult(False, "余量与间距不能为负数", self.load(conn, project_id))
        if width - 2 * margin <= 0 or height - 2 * margin <= 0:
            return LayoutResult(False, "四边余量之和超过板材尺寸", self.load(conn, project_id))

        # 模块 10：手改了宽高就不再是那块余料了（轮廓对不上），自动解绑退回矩形板
        before = db.load_sheet(conn, project_id)
        if int(before.get("scrapId") or 0) and (
                abs(float(before["width"]) - width) > 0.5
                or abs(float(before["height"]) - height) > 0.5):
            db.set_sheet_scrap(conn, project_id, 0)

        db.save_sheet(conn, project_id, {"width": width, "height": height,
                                         "margin": margin, "gap": gap})
        state = self.load(conn, project_id)

        # 板变小了：把越界零件夹回可用区域，避免出现"看不见的零件"
        for part in state.parts:
            placement = state.placements.get(part.id)
            if placement is None:
                continue
            w, h = part.size_at(placement.rot)
            tx = min(max(placement.tx, margin), max(margin, width - margin - w))
            ty = min(max(placement.ty, margin), max(margin, height - margin - h))
            if abs(tx - placement.tx) > EPS or abs(ty - placement.ty) > EPS:
                placement.tx, placement.ty = tx, ty
                db.save_placement(conn, project_id, part.id, placement.sheet,
                                  tx, ty, placement.rot, placement.locked, placement.zorder,
                                  fixed_rot=placement.fixed_rot)
        return LayoutResult(True, "", state)

    def move(self, conn: Any, project_id: int, part_id: int,
             tx: float, ty: float, rot: Optional[float] = None) -> LayoutResult:
        """移动（可同时改角度）一个零件；校验不通过时不落库。"""
        state = self.load(conn, project_id)
        part = state.part(part_id)
        placement = state.placements.get(part_id)
        if part is None or placement is None:
            return LayoutResult(False, f"零件 #{part_id} 不在排料板上", state)
        if placement.locked and rot is None:
            return LayoutResult(False, f"零件「{part.name}」已锁定，先解锁再移动", state)
        if placement.locked and rot is not None and abs(rot - placement.rot) > EPS:
            return LayoutResult(False, f"零件「{part.name}」已锁定，不能旋转", state)
        if (placement.fixed_rot and rot is not None
                and abs(rot - placement.rot) > EPS):
            return LayoutResult(False, f"零件「{part.name}」已锁定旋转角度（木纹），先解锁再转", state)

        target_rot = placement.rot if rot is None else float(rot)
        error = self._check(state, part, target_rot, float(tx), float(ty), exclude=part_id)
        if error:
            return LayoutResult(False, error, state)

        placement.tx, placement.ty, placement.rot = float(tx), float(ty), target_rot
        db.save_placement(conn, project_id, part_id, placement.sheet,
                          placement.tx, placement.ty, placement.rot,
                          placement.locked, placement.zorder,
                          fixed_rot=placement.fixed_rot)
        return LayoutResult(True, "", state)

    def rotate(self, conn: Any, project_id: int, part_id: int, rot: float) -> LayoutResult:
        """旋转到指定角度：以包围盒中心为锚点，位置自动补偿。"""
        state = self.load(conn, project_id)
        part = state.part(part_id)
        placement = state.placements.get(part_id)
        if part is None or placement is None:
            return LayoutResult(False, f"零件 #{part_id} 不在排料板上", state)
        if placement.locked:
            return LayoutResult(False, f"零件「{part.name}」已锁定，不能旋转", state)
        if placement.fixed_rot:
            return LayoutResult(False, f"零件「{part.name}」已锁定旋转角度（木纹），先解锁再转", state)

        old_w, old_h = part.size_at(placement.rot)
        center_x = placement.tx + old_w / 2.0
        center_y = placement.ty + old_h / 2.0
        new_w, new_h = part.size_at(rot)

        # 旋转后先保持中心不动；若因此越界，就把零件夹回可用区域再试，
        # 大件在板边旋转时这是最符合直觉的行为（不横向跑出板外）。
        margin = float(state.sheet["margin"])
        max_x = max(margin, float(state.sheet["width"]) - margin - new_w)
        max_y = max(margin, float(state.sheet["height"]) - margin - new_h)
        tx = min(max(center_x - new_w / 2.0, margin), max_x)
        ty = min(max(center_y - new_h / 2.0, margin), max_y)
        return self.move(conn, project_id, part_id, tx, ty, rot)

    def set_locked(self, conn: Any, project_id: int, part_id: int, locked: bool) -> LayoutResult:
        state = self.load(conn, project_id)
        placement = state.placements.get(part_id)
        if placement is None:
            return LayoutResult(False, f"零件 #{part_id} 不在排料板上", state)
        placement.locked = bool(locked)
        db.save_placement(conn, project_id, part_id, placement.sheet,
                          placement.tx, placement.ty, placement.rot,
                          placement.locked, placement.zorder,
                          fixed_rot=placement.fixed_rot)
        return LayoutResult(True, "", state)

    def set_fixed_rot(self, conn: Any, project_id: int, part_id: int,
                      fixed_rot: bool) -> LayoutResult:
        """锁定/解锁旋转角度（木纹方向）：自动排料不再旋转该零件。"""
        state = self.load(conn, project_id)
        placement = state.placements.get(part_id)
        if placement is None:
            return LayoutResult(False, f"零件 #{part_id} 不在排料板上", state)
        placement.fixed_rot = bool(fixed_rot)
        db.save_placement(conn, project_id, part_id, placement.sheet,
                          placement.tx, placement.ty, placement.rot,
                          placement.locked, placement.zorder,
                          fixed_rot=placement.fixed_rot)
        return LayoutResult(True, "", state)

    # ------------------------------------------------------------ 复制增件
    @staticmethod
    def _copy_name(state: LayoutState, name: str) -> str:
        """复制件命名：去掉已有的 " (n)" 后缀，再取同级序号 +1。"""
        base = name
        if base.endswith(")") and " (" in base:
            head, _, tail = base.rpartition(" (")
            if tail[:-1].isdigit():
                base = head
        used = 1
        for part in state.parts:
            other = part.name
            if other.startswith(base + " (") and other.endswith(")"):
                suffix = other[len(base) + 2:-1]
                if suffix.isdigit():
                    used = max(used, int(suffix))
        return f"{base} ({used + 1})"

    def duplicate(self, conn: Any, project_id: int, part_id: int,
                  count: int = 1) -> LayoutResult:
        """复制零件 count 份（模块 3 手动增件）：几何、角度、木纹锁定一并继承。

        摆放策略：先贴着原件右侧/上方、再贴着已有零件找空位；整块板都放不下时
        停靠到板外上方 —— 不静默叠在原件上，用户能一眼看见也能直接拖进来。
        """
        state = self.load(conn, project_id)
        source = state.part(part_id)
        placement = state.placements.get(part_id)
        if source is None or placement is None:
            return LayoutResult(False, f"零件 #{part_id} 不在排料板上", state)

        rows = db.load_part_polylines(conn, project_id, part_id)
        if not rows:
            return LayoutResult(False, f"零件「{source.name}」没有轮廓，无法复制", state)

        total = max(1, int(count))
        zorder = max((item.zorder for item in state.placements.values()), default=-1) + 1
        created: List[int] = []
        parked = 0
        for index in range(total):
            name = self._copy_name(state, source.name)
            bbox = [0.0, 0.0, round(source.width, 3), round(source.height, 3)]
            new_id = db.create_part(conn, project_id, name, "copy", bbox)
            db.save_part_polylines(conn, project_id, new_id, [
                (int(row["layer_id"]), bool(row["closed"]), bool(row["is_hole"]), row["pts"])
                for row in rows
            ])

            # 几何与原件同源：轮廓已归一化到包围盒左下角，直接复用即可
            part = LayoutPart(id=new_id, name=name, contours=source.contours,
                              width=source.width, height=source.height,
                              outline=source.outline, outline_index=source.outline_index,
                              area=source.area)
            state.parts.append(part)

            rot = placement.rot
            spot = self._free_spot(state, part, rot, placement, source)
            if spot is None:
                tx, ty = self._park_spot(state, part, rot)
                parked += 1
            else:
                tx, ty = spot
            state.placements[new_id] = Placement(
                part_id=new_id, sheet=0, tx=tx, ty=ty, rot=rot,
                locked=False, zorder=zorder + index, fixed_rot=placement.fixed_rot)
            db.save_placement(conn, project_id, new_id, 0, tx, ty, rot,
                              locked=False, zorder=zorder + index,
                              fixed_rot=placement.fixed_rot)
            created.append(new_id)

        logger.info("复制零件「%s」%d 份（停靠 %d 份）", source.name, total, parked)
        return LayoutResult(True, "", state,
                            extra={"copied": len(created), "parked": parked,
                                   "partIds": created})

    def _free_spot(self, state: LayoutState, part: LayoutPart, rot: float,
                   anchor: Placement, anchor_part: LayoutPart,
                   ) -> Optional[Tuple[float, float]]:
        """在已排零件边缘找第一个合法位置；整张板都放不下时返回 None。"""
        margin = float(state.sheet["margin"])
        gap = float(state.sheet["gap"])
        anchor_w, anchor_h = anchor_part.size_at(anchor.rot)

        candidates: List[Tuple[float, float]] = []
        # 先沿原件右侧外推，再沿下方外推：最贴近"再加一件同款"的直觉
        for step in range(1, COPY_SCAN_STEPS + 1):
            candidates.append((anchor.tx + (anchor_w + gap) * step, anchor.ty))
            candidates.append((anchor.tx, anchor.ty - (anchor_h + gap) * step))
        # 再贴着每个已排零件的右侧与上方试
        for other in state.parts:
            place = state.placements.get(other.id)
            if place is None or other.id == part.id:
                continue
            other_w, other_h = other.size_at(place.rot)
            candidates.append((place.tx + other_w + gap, place.ty))
            candidates.append((place.tx, place.ty + other_h + gap))
        candidates.append((margin, margin))

        for tx, ty in candidates:
            if not self._check(state, part, rot, tx, ty, exclude=part.id):
                return round(tx, 3), round(ty, 3)
        return None

    def _park_spot(self, state: LayoutState, part: LayoutPart,
                   rot: float) -> Tuple[float, float]:
        """板外上方停靠位：接在已有停靠件右侧，超出板宽就另起一行。"""
        sheet_w = float(state.sheet["width"])
        gap = float(state.sheet["gap"])
        width, height = part.size_at(rot)
        base_y = float(state.sheet["height"]) + PARK_OFFSET
        row_y, next_x, row_h = base_y, 0.0, 0.0
        for other in state.parts:
            place = state.placements.get(other.id)
            if place is None or place.ty < base_y - EPS:
                continue
            other_w, other_h = other.size_at(place.rot)
            row_y = max(row_y, place.ty)
            next_x = max(next_x, place.tx + other_w + gap)
            row_h = max(row_h, other_h)
        if next_x > 0 and next_x + width > max(sheet_w, width):
            return 0.0, round(row_y + row_h + gap, 3)
        return round(next_x, 3), round(row_y, 3)

    # ------------------------------------------------------------ 作图新建
    @staticmethod
    def shape_points(kind: str, width: float, height: float, diameter: float,
                     points: Sequence[Sequence[float]],
                     ) -> Tuple[np.ndarray, str]:
        """把作图参数转成轮廓点串（板坐标）与默认名称。

        圆用正多边形近似：切割路径本来就是折线，导出/预览/碰撞全按折线走，
        比存一个"真圆"再到处特判稳得多。
        """
        if kind == "rect":
            if width <= 0 or height <= 0:
                raise ValueError("矩形的宽和高必须大于 0")
            pts = np.asarray([[0.0, 0.0], [width, 0.0],
                              [width, height], [0.0, height]], dtype=np.float64)
            return pts, f"矩形 {width:g}×{height:g}"

        if kind == "circle":
            if diameter <= 0:
                raise ValueError("圆的直径必须大于 0")
            radius = diameter / 2.0
            angles = np.linspace(0.0, 2.0 * math.pi, CIRCLE_SEGMENTS, endpoint=False)
            pts = np.stack([radius + radius * np.cos(angles),
                            radius + radius * np.sin(angles)], axis=1)
            return pts, f"圆 Φ{diameter:g}"

        if kind == "polygon":
            cleaned: List[List[float]] = []
            for item in points:
                if len(item) < 2:
                    continue
                x, y = float(item[0]), float(item[1])
                if cleaned and abs(cleaned[-1][0] - x) < EPS and abs(cleaned[-1][1] - y) < EPS:
                    continue        # 双击闭合时最后一个点和起点重合，去掉
                cleaned.append([x, y])
            if len(cleaned) >= 3 and abs(cleaned[0][0] - cleaned[-1][0]) < EPS \
                    and abs(cleaned[0][1] - cleaned[-1][1]) < EPS:
                cleaned.pop()
            if len(cleaned) < 3:
                raise ValueError("多边形至少需要 3 个点")
            return np.asarray(cleaned, dtype=np.float64), f"多边形 {len(cleaned)}点"

        raise ValueError(f"不支持的作图类型：{kind}")

    def add_shape(self, conn: Any, project_id: int, kind: str = "rect", name: str = "",
                  width: float = 0.0, height: float = 0.0, diameter: float = 0.0,
                  points: Sequence[Sequence[float]] = (),
                  at: Optional[Sequence[float]] = None) -> LayoutResult:
        """在排料板上新建一个零件（手动作图）：矩形 / 圆 / 手绘多边形。

        几何按板坐标传进来，这里归一化到自身包围盒左下角（与 DXF 导入件同构）。
        落位优先级：用户画的位置（at）能用就用 -> 网格扫描找空位 -> 停靠板外上方。
        """
        state = self.load(conn, project_id)
        try:
            outline, default_name = self.shape_points(kind, width, height, diameter, points)
        except ValueError as exc:
            return LayoutResult(False, str(exc), state)

        part_name = (name or "").strip() or default_name
        min_xy = outline.min(axis=0)
        local = (outline - min_xy).astype(np.float32)
        made = self._make_part(0, part_name, [{"pts": local}])
        if made is None:
            return LayoutResult(False, "轮廓退化（面积为 0），没法建件", state)

        layer_id = db.ensure_layer(conn, project_id, SHAPE_LAYER_NAME, SHAPE_LAYER_COLOR)
        bbox = [0.0, 0.0, round(made.width, 3), round(made.height, 3)]
        new_id = db.create_part(conn, project_id, part_name, "draw", bbox)
        db.save_part_polylines(conn, project_id, new_id, [(layer_id, True, False, local)])

        part = LayoutPart(id=new_id, name=part_name, contours=made.contours,
                          width=made.width, height=made.height, outline=made.outline,
                          outline_index=made.outline_index, area=made.area)
        state.parts.append(part)

        zorder = max((item.zorder for item in state.placements.values()), default=-1) + 1
        # 手绘的件就放在用户画的位置：能放下就不挪，放不下才去找空位
        spot: Optional[Tuple[float, float]] = None
        if at is not None and len(at) >= 2:
            wanted = (round(float(at[0]), 3), round(float(at[1]), 3))
            if not self._check(state, part, 0.0, wanted[0], wanted[1], exclude=part.id):
                spot = wanted
        if spot is None:
            spot = self._scan_spot(state, part, 0.0)
        parked = spot is None
        tx, ty = self._park_spot(state, part, 0.0) if parked else spot
        state.placements[new_id] = Placement(
            part_id=new_id, sheet=0, tx=tx, ty=ty, rot=0.0, locked=False, zorder=zorder)
        db.save_placement(conn, project_id, new_id, 0, tx, ty, 0.0,
                          locked=False, zorder=zorder)

        logger.info("新建零件「%s」（%s，%.1f×%.1f，%s）", part_name, kind,
                    part.width, part.height, "停靠板外" if parked else "已上板")
        return LayoutResult(True, "", state, extra={
            "partId": new_id, "parked": parked, "partIds": [new_id],
            "width": round(part.width, 3), "height": round(part.height, 3),
        })

    def update_outline(self, conn: Any, project_id: int, part_id: int,
                       contours: Sequence[Sequence[Sequence[float]]]) -> LayoutResult:
        """顶点编辑：用新的轮廓点串替换零件几何。

        点串是"屏幕上看到的坐标"（板坐标，已含摆放与旋转），所以这里直接把它
        当成 0° 的新轮廓落库，并把摆放位置设为新包围盒左下角 —— 用户拖到哪、
        切出来就是哪，不会因为旧角度再被旋转一次。
        木纹锁定件不允许改型：改完角度语义就乱了，得先解纹。
        """
        state = self.load(conn, project_id)
        part = state.part(part_id)
        placement = state.placements.get(part_id)
        if part is None or placement is None:
            return LayoutResult(False, f"零件 #{part_id} 不在排料板上", state)
        if placement.locked:
            return LayoutResult(False, f"零件「{part.name}」已锁定，先解锁再改造型", state)
        if placement.fixed_rot:
            return LayoutResult(False, f"零件「{part.name}」锁定了木纹方向，先解纹再改造型", state)

        arrays: List[np.ndarray] = []
        for item in contours:
            arr = np.asarray(item, dtype=np.float64).reshape(-1, 2)
            if len(arr) >= 2 and np.allclose(arr[0], arr[-1]):
                arr = arr[:-1]
            if len(arr) < 3 or abs(self._signed_area(arr)) < MIN_OVERLAP_AREA:
                continue
            arrays.append(arr)
        if not arrays:
            return LayoutResult(False, "轮廓至少需要 3 个不共线的点", state)

        all_pts = np.concatenate(arrays, axis=0)
        min_xy = all_pts.min(axis=0)
        local = [(arr - min_xy).astype(np.float32) for arr in arrays]
        made = self._make_part(part_id, part.name, [{"pts": arr} for arr in local])
        if made is None:
            return LayoutResult(False, "新轮廓退化（面积为 0）", state)

        tx, ty = round(float(min_xy[0]), 3), round(float(min_xy[1]), 3)
        error = self._check(state, made, 0.0, tx, ty, exclude=part_id)
        if error:
            return LayoutResult(False, error, state)

        rows = db.load_part_polylines(conn, project_id, part_id)
        layer_id = int(rows[0]["layer_id"]) if rows else 1
        areas = [abs(self._signed_area(arr)) for arr in local]
        outline_idx = int(np.argmax(areas))
        db.replace_part_polylines(conn, project_id, part_id, [
            (layer_id, True, index != outline_idx, arr) for index, arr in enumerate(local)
        ])
        db.update_part_bbox(conn, project_id, part_id,
                            [0.0, 0.0, round(made.width, 3), round(made.height, 3)])

        placement.tx, placement.ty, placement.rot = tx, ty, 0.0
        db.save_placement(conn, project_id, part_id, placement.sheet, tx, ty, 0.0,
                          placement.locked, placement.zorder,
                          fixed_rot=placement.fixed_rot)
        state.parts = [made if item.id == part_id else item for item in state.parts]

        logger.info("改造型「%s」：%d 条轮廓，%.1f×%.1f",
                    part.name, len(local), made.width, made.height)
        return LayoutResult(True, "", state, extra={
            "partId": part_id, "width": round(made.width, 3),
            "height": round(made.height, 3),
        })

    def _scan_spot(self, state: LayoutState, part: LayoutPart,
                   rot: float) -> Optional[Tuple[float, float]]:
        """板内按行扫描第一个合法位置（新建零件用）；整板放不下时返回 None。"""
        margin = float(state.sheet["margin"])
        gap = float(state.sheet["gap"])
        sheet_w = float(state.sheet["width"])
        sheet_h = float(state.sheet["height"])
        width, height = part.size_at(rot)
        step_x = max(width, 1.0) + gap
        step_y = max(height, 1.0) + gap

        for row in range(SCAN_MAX_ROWS):
            y = margin + step_y * row
            if y + height > sheet_h - margin + EPS:
                break
            for col in range(SCAN_MAX_COLS):
                x = margin + step_x * col
                if x + width > sheet_w - margin + EPS:
                    break
                if not self._check(state, part, rot, x, y, exclude=part.id):
                    return round(x, 3), round(y, 3)
        return None

    def reset(self, conn: Any, project_id: int) -> LayoutResult:
        """清空摆放并重新做一次初始行排。

        模块 10：板材是余料时，行排只认矩形包围盒，结果会落进余料的缺口
        （矩形里有、轮廓里没有）。这里补一次停靠，保证"看得见的位置就是切得到的位置"。
        """
        db.clear_placements(conn, project_id)
        state = self.open(conn, project_id)
        if self.park_outside(conn, project_id, state, check_gap=False):
            # 停靠只写了库，内存里的摆放已经过期，重新读一次再下发
            state = self.load(conn, project_id)
        logger.info("排料复位：%d 个零件，板材 %.0f×%.0f",
                    len(state.parts), float(state.sheet["width"]),
                    float(state.sheet["height"]))
        return LayoutResult(True, "", state)

    def park_outside(self, conn: Any, project_id: int, state: LayoutState,
                     check_gap: bool = True) -> int:
        """把放不到当前板材边界内的零件停靠到板外上方，返回停靠件数。

        与自动排料的"未排下"口径一致：不删零件、不静默改位置，而是挪到板上方一行，
        用户一眼能看出这块料放不下它。只在绑定余料时生效（矩形板不搬动零件，
        越界件继续留在板上并在 stats 里报 overflow）。

        check_gap=True 时连带判最小间距（换余料板时旧摆放可能贴得过近）；
        复位重排是新做的行排，只判"矩形板框 + 余料轮廓"，避免把间距问题也当放不下。
        """
        if state.boundary is None:
            return 0
        sheet_w = float(state.sheet["width"])
        gap = float(state.sheet["gap"])
        park_x = 0.0
        park_y = float(state.sheet["height"]) + PARK_OFFSET
        row_h = 0.0
        parked = 0
        zorder = len(state.placements)
        for part in state.parts:
            placement = state.placements.get(part.id)
            if placement is None:
                continue
            if check_gap:
                error = self._check(state, part, placement.rot, placement.tx,
                                    placement.ty, exclude=part.id)
                if not error:
                    continue
            elif not self._outside_boundary(state, part, placement.rot,
                                            placement.tx, placement.ty):
                continue
            width, height = part.size_at(placement.rot)
            if row_h > 0 and park_x + width > max(sheet_w, width):
                park_x = 0.0
                park_y += row_h + gap
                row_h = 0.0
            db.save_placement(conn, project_id, part.id, 0,
                              round(park_x, 3), round(park_y, 3), placement.rot,
                              locked=False, zorder=zorder + parked,
                              fixed_rot=placement.fixed_rot)
            park_x += width + gap
            row_h = max(row_h, height)
            parked += 1
        if parked:
            logger.info("有 %d 个零件放不进当前余料轮廓，已停靠到板外上方", parked)
        return parked

    def _outside_boundary(self, state: LayoutState, part: LayoutPart, rot: float,
                          tx: float, ty: float) -> bool:
        """只判"矩形板框 + 余料轮廓"是否放不下，不考虑与其它零件的间距。"""
        margin = float(state.sheet["margin"])
        w, h = part.size_at(rot)
        if (tx < margin - EPS or ty < margin - EPS
                or tx + w > float(state.sheet["width"]) - margin + EPS
                or ty + h > float(state.sheet["height"]) - margin + EPS):
            return True
        if state.boundary is None:
            return False
        return not self._inside(state.boundary, part.rotated_outline(rot, tx, ty))

    # ------------------------------------------------------------ 校验
    def _check(self, state: LayoutState, part: LayoutPart, rot: float,
               tx: float, ty: float, exclude: int) -> str:
        """返回空串表示合法，否则返回中文原因。"""
        margin = float(state.sheet["margin"])
        right = float(state.sheet["width"]) - margin
        top = float(state.sheet["height"]) - margin
        w, h = part.size_at(rot)

        if tx < margin - EPS:
            return f"零件「{part.name}」超出板材左边界"
        if ty < margin - EPS:
            return f"零件「{part.name}」超出板材下边界"
        if tx + w > right + EPS:
            return f"零件「{part.name}」超出板材右边界（可用宽度 {right - margin:.1f}mm）"
        if ty + h > top + EPS:
            return f"零件「{part.name}」超出板材上边界（可用高度 {top - margin:.1f}mm）"

        outline = part.rotated_outline(rot, tx, ty)
        # 模块 10：当前板材是余料时，零件还必须整个落在余料轮廓内。
        # 矩形板框只是余料的包围盒，凹进去的部分不能用
        if state.boundary is not None and not self._inside(state.boundary, outline):
            return f"零件「{part.name}」超出余料轮廓（请放到余料范围内）"

        box = (min(float(outline[:, 0].min()), tx), min(float(outline[:, 1].min()), ty),
               max(float(outline[:, 0].max()), tx + w), max(float(outline[:, 1].max()), ty + h))

        gap = float(state.sheet["gap"])
        for other in state.parts:
            if other.id == exclude:
                continue
            other_place = state.placements.get(other.id)
            if other_place is None or other_place.sheet != 0:
                continue
            other_w, other_h = other.size_at(other_place.rot)
            if box[2] + gap <= other_place.tx or other_place.tx + other_w + gap <= box[0]:
                continue
            if box[3] + gap <= other_place.ty or other_place.ty + other_h + gap <= box[1]:
                continue

            other_outline = other.rotated_outline(other_place.rot, other_place.tx, other_place.ty)
            if self._overlaps(outline, other_outline, gap):
                return f"零件「{part.name}」与「{other.name}」间距不足 {gap:g}mm"
        return ""

    def _inside(self, boundary: np.ndarray, outline: np.ndarray) -> bool:
        """轮廓是否完整落在余料边界内（模块 10）。

        做法：两者求交，交面积等于轮廓自身面积才说明没有一丝跑到边界外。
        比“顶点逐个判断”稳：凹多边形里顶点全在内、但某条边跨出去的情况也能拦住。
        """
        pc = self._pc
        part_area = abs(self._signed_area(outline))
        if part_area <= 0:
            return False
        if pc is None:                      # 没有 clipper：退化为顶点包含性判定
            poly = boundary.tolist()
            return all(self._point_in_polygon(float(x), float(y), poly) for x, y in outline)
        try:
            clipper = pc.Pyclipper()
            clipper.AddPath(self._to_path(boundary), pc.PT_SUBJECT, True)
            clipper.AddPath(self._to_path(outline), pc.PT_CLIP, True)
            solution = clipper.Execute(pc.CT_INTERSECTION, pc.PFT_NONZERO, pc.PFT_NONZERO)
            inside = sum(abs(pc.Area(path)) for path in solution) / (SCALE * SCALE)
            return inside >= part_area - 1.0     # 1mm² 容差：浮点与圆角外扩的余量
        except Exception:                    # 退化轮廓不应让拖拽崩掉，按“超出”拒绝
            logger.warning("余料边界判定失败，按超出处理", exc_info=True)
            return False

    @staticmethod
    def _to_path(pts: np.ndarray) -> List[Tuple[int, int]]:
        """ndarray -> pyclipper 定点路径。"""
        return [(int(round(float(x) * SCALE)), int(round(float(y) * SCALE)))
                for x, y in pts]

    @staticmethod
    def _point_in_polygon(x: float, y: float, poly: List[Any]) -> bool:
        """射线法（clipper 缺失时的兜底）。"""
        inside = False
        count = len(poly)
        for index in range(count):
            x1, y1 = float(poly[index][0]), float(poly[index][1])
            x2, y2 = float(poly[(index + 1) % count][0]), float(poly[(index + 1) % count][1])
            if (y1 > y) != (y2 > y):
                cross = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
                if x < cross:
                    inside = not inside
        return inside

    def _overlaps(self, a: np.ndarray, b: np.ndarray, gap: float) -> bool:
        """两个多边形在留出 gap 间距后是否冲突。

        做法：双方各向外扩 gap/2 后求交，交面积 > 0 即视为间距不足。
        圆角外扩在对角方向略保守，工程上可接受（宁可留缝不可撞刀）。
        """
        if self._pc is None:
            return True          # 没有 clipper 时退化为保守判定（只要包围盒相交就拒绝）
        pc = self._pc
        try:
            clipper = pc.Pyclipper()
            clipper.AddPath(self._inflate(a, gap / 2.0, pc), pc.PT_SUBJECT, True)
            clipper.AddPath(self._inflate(b, gap / 2.0, pc), pc.PT_CLIP, True)
            solution = clipper.Execute(pc.CT_INTERSECTION, pc.PFT_NONZERO, pc.PFT_NONZERO)
            total = 0.0
            for path in solution:
                total += abs(pc.Area(path))
            return total / (SCALE * SCALE) > MIN_OVERLAP_AREA
        except Exception:        # 退化数据（自交等）不应让拖拽崩掉
            logger.warning("排料重叠校验失败，按冲突处理", exc_info=True)
            return True

    @staticmethod
    def _inflate(pts: np.ndarray, delta: float, pc: Any) -> List[Tuple[int, int]]:
        """多边形向外扩 delta（mm），用于把"最小间距"转成"求交"判定。"""
        path = [(int(round(x * SCALE)), int(round(y * SCALE))) for x, y in pts]
        offset = pc.PyclipperOffset()
        offset.AddPath(path, pc.JT_ROUND, pc.ET_CLOSEDPOLYGON)
        result = offset.Execute(delta * SCALE)
        return result[0] if result else path

    # ------------------------------------------------------------ 输出
    def build_polylines(self, state: LayoutState) -> Tuple[List[Polyline], Dict[int, List[int]]]:
        """按摆放状态变换零件轮廓，返回 (轮廓列表, part_id -> 轮廓 id 列表)。"""
        polylines: List[Polyline] = []
        id_map: Dict[int, List[int]] = {}
        next_id = LAYOUT_POLY_BASE

        for part in state.parts:
            placement = state.placements.get(part.id)
            if placement is None:
                continue
            ids: List[int] = []
            for index, pts in enumerate(part.rotated_contours(placement.rot,
                                                             placement.tx, placement.ty)):
                # 图层号用板材序号 + 1：多板排料时前端按图层上色
                poly = Polyline(next_id, placement.sheet + 1, pts, True,
                                is_hole=index != part.outline_index, part_id=part.id)
                polylines.append(poly)
                ids.append(next_id)
                next_id += 1
            id_map[part.id] = ids
        return polylines, id_map

    def build_meta(self, state: LayoutState, id_map: Dict[int, List[int]]) -> Dict[str, Any]:
        """组装摆放结果的 meta（板材参数 + 零件表 + 统计）。"""
        sheet_area = max(1.0, float(state.sheet["width"]) * float(state.sheet["height"]))
        used_area = sum(part.area for part in state.parts if part.id in state.placements)
        margin = float(state.sheet["margin"])

        # 越界 = 超出矩形板框 **或** 落在余料轮廓外（模块 10）
        overflow = sum(
            1 for part in state.parts
            if part.id in state.placements and self._outside_boundary(
                state, part, state.placements[part.id].rot,
                state.placements[part.id].tx, state.placements[part.id].ty)
        )

        return {
            "projectId": state.project_id,
            "sheet": {k: round(float(v), 3) for k, v in state.sheet.items()},
            # 模块 10：余料作为板材时下发轮廓，前端照此画边界（不再画矩形）
            "scrap": state.scrap,
            "parts": [
                {
                    "id": part.id,
                    "name": part.name,
                    "width": round(part.width, 3),
                    "height": round(part.height, 3),
                    "polylineIds": id_map.get(part.id, []),
                    "tx": round(state.placements[part.id].tx, 3),
                    "ty": round(state.placements[part.id].ty, 3),
                    "rot": round(state.placements[part.id].rot, 3),
                    "locked": state.placements[part.id].locked,
                    "fixedRot": state.placements[part.id].fixed_rot,
                }
                for part in state.parts if part.id in state.placements
            ],
            "placements": [p.as_dict() for p in state.placements.values()],
            "stats": {
                "partCount": len(state.parts),
                "placementCount": len(state.placements),
                "lockedCount": sum(1 for p in state.placements.values() if p.locked),
                "fixedRotCount": sum(1 for p in state.placements.values() if p.fixed_rot),
                "overflowCount": overflow,
                "usedArea": round(used_area, 1),
                "sheetArea": round(sheet_area, 1),
                "utilization": round(used_area / sheet_area, 4),
                "sheetWidth": round(float(state.sheet["width"]), 3),
                "sheetHeight": round(float(state.sheet["height"]), 3),
                "gap": round(float(state.sheet["gap"]), 3),
                "margin": round(margin, 3),
                "scrapId": int(state.sheet.get("scrapId") or 0),
                "boundaryArea": round(float(abs(self._signed_area(state.boundary))), 1)
                if state.boundary is not None else round(sheet_area, 1),
            },
        }


# 全局单例：无状态，可被多线程共用
layout_service = LayoutService()
