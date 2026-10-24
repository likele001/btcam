"""自动排料服务（模块 4）。

在模块 3 的排料板之上做"一键重排"：

    输入  -> layout_sheet / layout_placement / part 轮廓（全部经 LayoutService.load 读取）
    搜索  -> ScanPacker（内置底线扫描）或 DeepNestBridge（外部进程）
    输出  -> 直接写回 layout_placement（沿用 tx/ty/rot 契约），由任务层广播 LAYOUT_STATE

三条硬规则：
1. 已锁定的零件原地不动，作为障碍物；未锁定零件全部参与重排；
2. 木纹锁定的零件不旋转（手动与自动口径一致，见 LayoutService.rotate）；
3. 外部引擎的结果必须再过一遍 LayoutService._check 才落库 —— 自动/手动共用同一套
   越界与最小间距判定，避免"两个入口两种结果"。

为什么不在前端算：排料搜索与碰撞判定属于重型几何计算，前端只负责渲染。
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.models import db
from app.services.layout_service import (
    EPS,
    PARK_OFFSET,
    LayoutPart,
    LayoutService,
    LayoutState,
    Placement,
    layout_service,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- 待排任务
@dataclass
class PartTask:
    """一个待排零件：形状 + 起始角度 + 是否允许旋转。"""

    part: LayoutPart
    rot: float = 0.0
    fixed_rot: bool = False


# ---------------------------------------------------------------- 内置扫描
class ScanPacker:
    """底线扫描排料（Bottom-Left）。

    候选位置来自"已放零件包围盒的右上角"与"余料轮廓顶点"：左边贴齐 + 下边贴齐
    是排料解的两个常见极值点，逐个用 LayoutService._check 验证即可保证间距与边界
    正确。相比模块 2 的 ShelfNester（只按行游标摆放），它天然支持障碍物与定角零件。
    """

    name = "scan"

    # 余料轮廓顶点候选的上限：布尔差会吐出大量共线点，超过就按更粗的网格去重，
    # 保证候选集规模可控（每个候选都要过一次 clipper 校验）
    MAX_BOUNDARY_CANDIDATES = 600

    def __init__(self, service: LayoutService, state: LayoutState,
                 rotation: bool, report: Any = None) -> None:
        self.service = service
        self.state = state
        self.rotation = rotation
        self.report = report

    def pack(self, tasks: Sequence[PartTask]) -> Tuple[List[Placement], List[LayoutPart]]:
        # 大件优先，减少碎片；同面积时高件优先，压住行高
        ordered = sorted(tasks, key=lambda t: (t.part.area, t.part.height), reverse=True)
        placed: List[Placement] = []
        unplaced: List[LayoutPart] = []

        for index, task in enumerate(ordered):
            placement = self._place(task)
            if placement is None:
                unplaced.append(task.part)
            else:
                placed.append(placement)
            self._tick(index, len(ordered))
        return placed, unplaced

    # ------------------------------------------------------------ 单件
    def _place(self, task: PartTask) -> Optional[Placement]:
        part = task.part
        margin, gap = float(self.state.sheet["margin"]), float(self.state.sheet["gap"])
        right = float(self.state.sheet["width"]) - margin
        top = float(self.state.sheet["height"]) - margin

        candidates = self._candidates(margin, gap)
        for rot in self._rotations(task):
            width, height = part.size_at(rot)
            for x, y in candidates:
                if x + width > right + EPS or y + height > top + EPS:
                    continue
                probe = Placement(part.id, 0, x, y, rot, fixed_rot=task.fixed_rot)
                self.state.placements[part.id] = probe
                error = self.service._check(self.state, part, rot, x, y, exclude=part.id)
                if not error:
                    return probe
                del self.state.placements[part.id]
        return None

    def _candidates(self, margin: float, gap: float) -> List[Tuple[float, float]]:
        """候选点集：板内左下角 + 每个已放零件右侧/上侧的贴边点 + 余料轮廓顶点。

        排序按 (y, x)：先往下压，再往左贴，得到经典的底线填充效果。
        """
        points = {(margin, margin)}
        for placement in self.state.placements.values():
            part = next((p for p in self.state.parts if p.id == placement.part_id), None)
            if part is None:
                continue
            width, height = part.size_at(placement.rot)
            points.add((placement.tx + width + gap, placement.ty))
            points.add((placement.tx, placement.ty + height + gap))
            points.add((margin, placement.ty + height + gap))
            points.add((placement.tx + width + gap, margin))
        points.update(self._boundary_candidates(margin))
        # 候选点最多几百个（零件数量级 + 轮廓顶点），排序后再按轮廓校验，避免无谓的 clipper 调用
        return sorted(points, key=lambda item: (round(item[1], 3), round(item[0], 3)))

    def _boundary_candidates(self, margin: float) -> List[Tuple[float, float]]:
        """余料轮廓上的落脚点（模块 10 凹形余料）。

        规则板（boundary is None）不产生额外候选，行为与改造前完全一致。
        之所以需要它：余料板 margin=0，左下角 (0,0) 有可能正好落在缺口里，此时
        "已放件右上角"一个点都还没有，搜索就只剩一个无效候选、整批件排不上。
        轮廓顶点天然是可行的左下角（例如缺口右侧、上方竖边的底端），再补上
        "顶到左边"与"压到底边"两条投影。多给的候选一律由 _check 复验，不存在误放。
        """
        boundary = self.state.boundary
        if boundary is None:
            return []
        rows = boundary.tolist() if hasattr(boundary, "tolist") else list(boundary)
        # 1mm 网格去重：布尔差的圆弧近似会吐出上百个近邻点，它们对落脚没区别。
        # 关键：网格只当"去重键"，坐标一律保留原值 —— 把点四舍五入易位，
        # 圆弧上的点就可能被挪进缺口里，得到一个看着贴边、实际越界 1mm² 的候选
        picked: Dict[Tuple[float, float], Tuple[float, float]] = {}
        for grid in (1.0, 4.0, 10.0):
            picked = {}
            for item in rows:
                bx, by = float(item[0]), float(item[1])
                picked.setdefault((round(bx / grid), round(by / grid)), (bx, by))
            if len(picked) * 3 <= self.MAX_BOUNDARY_CANDIDATES:
                break
        points = set()
        for bx, by in picked.values():
            points.add((bx, by))
            points.add((margin, by))
            points.add((bx, margin))
        return sorted(points, key=lambda item: (item[1], item[0]))

    def _rotations(self, task: PartTask) -> List[float]:
        """可用角度：木纹锁定只保留当前角；关闭旋转则保持现有姿方向。"""
        if task.fixed_rot:
            return [task.rot]
        if not self.rotation:
            return [task.rot]
        width, height = task.part.width, task.part.height
        if abs(width - height) < 1e-6:
            return [0.0]
        return [0.0, 90.0]

    def _tick(self, index: int, total: int) -> None:
        if self.report is None:
            return
        if index % 5 == 0 or index == total - 1:
            cancel = getattr(self.report, "check_cancelled", None)
            if cancel is not None:
                cancel()
            self.report(0.05 + 0.8 * (index + 1) / max(1, total),
                        "排料 %d/%d" % (index + 1, total))


# ---------------------------------------------------------------- DeepNest 桥接
def _pack_deepnest(service: LayoutService, state: LayoutState, tasks: Sequence[PartTask],
                   rotation: bool, report: Any) -> List[Placement]:
    """把待排零件交给外部 DeepNest，结果换算成排料板坐标后复验。

    桥接返回的 (tx,ty,rot) 是"绕零件原点旋转再平移"，而排料板契约定的是
    "旋转后包围盒左下角"，所以这里统一用变换后的顶点最小值回填，
    避免两套坐标混用导致零件整体偏移一个尺寸。
    """
    from app.services.nesting_service import (
        DeepNestBridge,
        Part as NestPart,
        normalize_part,
        transform_points,
    )

    bridge = DeepNestBridge()
    if not bridge.available():
        raise RuntimeError("未配置 BTCAM_DEEPNEST_CMD")
    bridge.gap = float(state.sheet["gap"])
    bridge.rotation = rotation

    nest_parts = [NestPart(t.part.id, normalize_part(t.part.outline), True, t.fixed_rot)
                  for t in tasks]
    result = bridge.pack(nest_parts, float(state.sheet["width"]),
                         float(state.sheet["height"]), report)

    by_task = {t.part.id: t for t in tasks}
    by_part = {p.id: p for p in nest_parts}
    converted: List[Placement] = []
    for raw in result.placements:
        task = by_task.get(raw.part_id)
        nest_part = by_part.get(raw.part_id)
        if task is None or nest_part is None:
            continue
        if task.fixed_rot and abs(math.remainder(raw.rot, 360.0)) > 1e-6:
            raise RuntimeError("DeepNest 旋转了木纹锁定的零件")
        pts = transform_points(nest_part.pts, raw.tx, raw.ty, raw.rot)
        converted.append(Placement(
            part_id=raw.part_id, sheet=0,
            tx=float(pts[:, 0].min()), ty=float(pts[:, 1].min()),
            rot=float(raw.rot), fixed_rot=task.fixed_rot,
        ))
    if not converted:
        raise RuntimeError("DeepNest 没有返回任何摆放")

    # 外部引擎不可信：用与手动摆料完全相同的规则复验，任何一件不合格就整体回退
    original = state.placements
    state.placements = dict(original)
    try:
        for placement in converted:
            state.placements[placement.part_id] = placement
        for placement in converted:
            task = by_task[placement.part_id]
            error = service._check(state, task.part, placement.rot,
                                   placement.tx, placement.ty, exclude=placement.part_id)
            if error:
                raise RuntimeError("DeepNest 结果未通过校验：%s" % error)
    finally:
        state.placements = original
    return converted


# ---------------------------------------------------------------- 任务入口
def auto_nest_job(params: Dict[str, Any], report: Any) -> Dict[str, Any]:
    """任务队列调用的自动排料任务。

    入参（camelCase，来自 /api/layout/nest）：
        projectId / engine / rotation / width / height / margin / gap / partIds
    出参：stats + sheet + placements（每件 tx/ty/rot/fixedRot）；
    结果已写库，任务层随后广播 LAYOUT_STATE。
    """
    started = time.perf_counter()
    engine_req = str(params.get("engine", "auto"))
    rotation = bool(params.get("rotation", True))
    subset = {int(part_id) for part_id in params.get("partIds") or []}
    # 排队期间就被取消的任务：直接停在入口，不去改板材/摆放
    check_cancelled = getattr(report, "check_cancelled", None)
    if check_cancelled is not None:
        check_cancelled()

    with db.get_conn() as conn:
        project_id = int(params.get("projectId") or 0) or db.get_active_project_id(conn)
        state = layout_service.load(conn, project_id)
        if not state.parts:
            raise ValueError("当前工程没有可排料的零件，请先导入 DXF")

        # 0) 板材参数入参：走与手动“应用板材”同一个入口，越界零件会被夹回
        sheet_input = {key: params.get(key)
                       for key in ("width", "height", "margin", "gap")}
        if any(value is not None for value in sheet_input.values()):
            merged = dict(state.sheet)
            merged.update({key: float(value) for key, value in sheet_input.items()
                           if value is not None})
            applied = layout_service.set_sheet(conn, project_id, merged["width"],
                                               merged["height"], merged["margin"],
                                               merged["gap"])
            if not applied.ok:
                raise ValueError(applied.reason)
            state = applied.state

        sheet = state.sheet
        gap = float(sheet["gap"])

        # 1) 参与重排的零件 = 未锁定 且（可选）落在 partIds 子集内；
        #    其余零件原地不动，作为障碍物保留在 state.placements 里
        locked_ids = {part_id for part_id, placement in state.placements.items()
                      if placement.locked}
        available = [part for part in state.parts if part.id not in locked_ids]
        targets = ([part for part in available if part.id in subset]
                   if subset else available)
        if not targets:
            raise ValueError("指定零件都已锁定，没有可自动排料的零件" if subset
                             else "所有零件都已锁定，没有可自动排料的零件")

        tasks: List[PartTask] = []
        for part in targets:
            placement = state.placements.get(part.id)
            tasks.append(PartTask(
                part=part,
                rot=float(placement.rot) if placement else 0.0,
                fixed_rot=bool(placement.fixed_rot) if placement else False,
            ))
        target_ids = {task.part.id for task in tasks}
        # 板上的“非参与件”：位置锁定件 + partIds 之外的件
        blockers = [part_id for part_id in state.placements if part_id not in target_ids]

        report(0.03, "读入 %d 个待排零件 · 板材 %.0f×%.0f" % (
            len(tasks), float(sheet["width"]), float(sheet["height"])))
        for task in tasks:
            state.placements.pop(task.part.id, None)

        # 2) 选引擎：DeepNest 需要桥接进程，且无法表达“障碍物保持原位”
        placed: List[Placement] = []
        note = ""
        engine_used = "scan"
        if engine_req in ("auto", "deepnest"):
            if blockers:
                note = "存在锁定/未参与重排的零件，DeepNest 无法保留其位置，改用内置扫描"
            else:
                try:
                    placed = _pack_deepnest(layout_service, state, tasks, rotation, report)
                    engine_used = "deepnest"
                except Exception as exc:          # 桥接缺失/超时/取消不准吞
                    from app.core.tasks import TaskCancelled
                    if isinstance(exc, TaskCancelled):
                        raise
                    note = "DeepNest 不可用，已回退内置扫描：%s" % exc
                    logger.warning("DeepNest 排料失败，回退 scan：%s", exc)

        if not placed:
            placed, unplaced_parts = ScanPacker(layout_service, state, rotation, report).pack(tasks)
            engine_used = "scan"
            unplaced_ids = {p.id for p in unplaced_parts}
        else:
            unplaced_ids = {t.part.id for t in tasks} - {p.part_id for p in placed}

        # 3) 落库：排上的写新位置；没排下的停靠板材上方（可见的"未排下"），
        #    而不是原地保留 —— 旧位置可能已被新零件占用，留着会形成视觉重叠
        for index, placement in enumerate(placed):
            db.save_placement(conn, project_id, placement.part_id, 0,
                              placement.tx, placement.ty, placement.rot,
                              locked=False, zorder=index,
                              fixed_rot=placement.fixed_rot)

        # 停靠口径与 LayoutService.park_outside 完全一致：左端贴 0，上边留 PARK_OFFSET，
        # 同一批"未排下"的件不管是自动排料挪的还是复位挪的，位置都一样
        park_x, park_y, park_row_h = 0.0, float(sheet["height"]) + PARK_OFFSET, 0.0
        park_index = 0
        for task in tasks:
            if task.part.id not in unplaced_ids:
                continue
            width, height = task.part.size_at(task.rot)
            if park_row_h > 0 and park_x + width > max(float(sheet["width"]), width):
                park_x = 0.0
                park_y += park_row_h + gap
                park_row_h = 0.0
            db.save_placement(conn, project_id, task.part.id, 0,
                              round(park_x, 3), round(park_y, 3), task.rot,
                              locked=False, zorder=len(placed) + park_index,
                              fixed_rot=task.fixed_rot)
            park_x += width + gap
            park_row_h = max(park_row_h, height)
            park_index += 1

        # 4) 统计：用重新读出的状态算，保证与 GET /api/layout 完全一致
        fresh = layout_service.load(conn, project_id)
        meta = layout_service.build_meta(fresh, {})
        stats = dict(meta["stats"])

    on_board = [part for part in fresh.parts
                if part.id in fresh.placements and part.id not in unplaced_ids]
    sheet_area = max(1.0, float(sheet["width"]) * float(sheet["height"]))
    stats.update({
        "engine": engine_used,
        "requestedEngine": engine_req,
        "engineNote": note,
        "rotation": rotation,
        "placedCount": len(placed),
        "unplacedCount": len(unplaced_ids),
        "usedArea": round(sum(part.area for part in on_board), 1),
        "utilization": round(sum(part.area for part in on_board) / sheet_area, 4),
        "calcMs": round((time.perf_counter() - started) * 1000, 1),
    })

    report(0.99, "排料完成：%d 个落位，%d 个未排下" % (len(placed), len(unplaced_ids)))
    # 出参：stats（含利用率）+ 板材 + 逐件摆放，前端与脚本不用再发 GET 就能拿到结果
    return {
        "stats": stats,
        "sheet": dict(fresh.sheet),
        "placements": [placement.as_dict() for placement in fresh.placements.values()],
    }
