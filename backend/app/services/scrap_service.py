"""余料服务（模块 10）。

业务闭环：

    排完一块板  ->  remaining_regions()：当前板材减掉已摆零件，算出还能用的碎块
    挑几块留用  ->  save_regions()：落库成余料（轮廓 + 尺寸 + 物料/板厚）
    下次开工单  ->  apply_to_sheet()：把某块余料直接当板材，排料接口不用改调用方式
    余料用掉了  ->  delete()：删除后若正被当板材，自动解绑退回规则整板

职责边界（与项目总约束一致）：
    - 轮廓归一化、面积、包围盒、求差全部在后端（pyclipper），前端只拿点集画图；
    - 余料轮廓坐标相对**自身包围盒左下角**保存，与板材坐标同构，
      所以"把余料当板材"时不需要任何平移换算，直接用作边界多边形。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.models import db
from app.services.layout_service import LayoutState, layout_service

logger = logging.getLogger(__name__)

# pyclipper 定点比例：1 mm = 1000 单位（与 LayoutService / ClipperEngine 一致）
SCALE = 1000.0
# 剩余区域的默认最小面积（mm²）：太小的碎渣不列出来，避免列表噪音
DEFAULT_MIN_AREA = 10000.0
# 单次最多返回几块剩余区域（按面积从大到小）
MAX_REGIONS = 30
# 轮廓点数上限：求差后的碎块可能带大量圆角离散点，超过这个量才做抽稀
MAX_POINTS = 2000


class ScrapError(ValueError):
    """余料业务错误：路由层统一翻译成 HTTP 400/404。"""


# ---------------------------------------------------------------- clipper 助手
def _clipper() -> Any:
    """pyclipper 可选依赖：缺失时剩余区域计算会明确报错，而不是静默返回空。"""
    try:
        import pyclipper
        return pyclipper
    except ImportError as exc:                 # pragma: no cover - 依赖缺失兜底
        raise ScrapError("环境缺少 pyclipper，无法计算余料轮廓") from exc


def _to_path(pts: Sequence[Sequence[float]]) -> List[Tuple[int, int]]:
    return [(int(round(float(x) * SCALE)), int(round(float(y) * SCALE))) for x, y in pts]


def _from_path(path: Sequence[Tuple[int, int]]) -> List[List[float]]:
    return [[round(x / SCALE, 3), round(y / SCALE, 3)] for x, y in path]


def _signed_area(pts: Sequence[Sequence[float]]) -> float:
    """鞋带公式（正负表示点序方向，取绝对值就是面积）。"""
    total = 0.0
    count = len(pts)
    if count < 3:
        return 0.0
    for index in range(count):
        x1, y1 = float(pts[index][0]), float(pts[index][1])
        x2, y2 = float(pts[(index + 1) % count][0]), float(pts[(index + 1) % count][1])
        total += x1 * y2 - x2 * y1
    return 0.5 * total


def _bbox(pts: Sequence[Sequence[float]]) -> Tuple[float, float, float, float]:
    xs = [float(p[0]) for p in pts]
    ys = [float(p[1]) for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _simplify(pc: Any, path: List[Tuple[int, int]]) -> List[List[float]]:
    """去掉求差产生的共线点/重复点，返回 mm 坐标。"""
    try:
        cleaned = pc.SimplifyPolygon(path, pc.PFT_NONZERO)
    except Exception:                          # 退化多边形：原样使用
        cleaned = [path]
    best = max(cleaned, key=lambda item: abs(pc.Area(item)), default=path)
    points = _from_path(best)
    # 去掉与首点重合的收尾点（闭合多边形不需要重复点）
    if len(points) > 1 and points[0] == points[-1]:
        points.pop()
    if len(points) <= MAX_POINTS:
        return points
    # 点太多时按步长抽稀（余料边界是给排料用的，不是加工路径，抽稀可接受）
    step = max(1, -(-len(points) // MAX_POINTS))
    thinned = points[::step]
    return thinned if len(thinned) >= 3 else points


# ---------------------------------------------------------------- 轮廓归一化
def normalize_outline(raw_outline: Any, width: Optional[float] = None,
                      height: Optional[float] = None) -> Dict[str, Any]:
    """把前端/脚本传来的轮廓整理成落库格式。

    支持三种输入：
        [[x,y], ...]        点数组（推荐）
        [{"x":..,"y":..}]   对象数组（兼容旧写法）
        空 + width/height   生成矩形轮廓（手工录入“一块方料”）
    返回值里的 outline 已经把包围盒左下角平移到 (0,0)，与板材坐标同构。
    """
    points: List[List[float]] = []
    if isinstance(raw_outline, (list, tuple)):
        for item in raw_outline:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                points.append([float(item[0]), float(item[1])])
            elif isinstance(item, dict) and "x" in item and "y" in item:
                points.append([float(item["x"]), float(item["y"])])
    if len(points) < 3:
        if width and height and float(width) > 0 and float(height) > 0:
            points = [[0.0, 0.0], [float(width), 0.0],
                      [float(width), float(height)], [0.0, float(height)]]
        else:
            raise ScrapError("余料轮廓至少需要 3 个点，或同时给出宽和高")
    # 去掉末尾重复点
    if points[0] == points[-1]:
        points.pop()
    if len(points) < 3:
        raise ScrapError("余料轮廓至少需要 3 个有效点")

    min_x, min_y, max_x, max_y = _bbox(points)
    w = max_x - min_x
    h = max_y - min_y
    if w <= 0 or h <= 0:
        raise ScrapError("余料轮廓的包围盒尺寸必须大于 0")
    outline = [[round(x - min_x, 3), round(y - min_y, 3)] for x, y in points]
    area = abs(_signed_area(outline))
    if area <= 0:
        raise ScrapError("余料轮廓面积为 0（点共线或自相交）")
    return {"outline": outline, "width": round(w, 3), "height": round(h, 3),
            "area": round(area, 1), "points": len(outline)}


def _as_text(value: Any, field: str, max_length: int) -> str:
    text = "" if value is None else str(value).strip()
    if len(text) > max_length:
        raise ScrapError("%s 过长（最多 %d 字）" % (field, max_length))
    return text


def _row_to_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    """数据库行（snake_case）-> 接口输出（camelCase）。"""
    return {
        "id": int(row["id"]),
        "projectId": int(row["project_id"]),
        "name": str(row["name"]),
        "code": str(row["code"]),
        "material": str(row["material"]),
        "thickness": float(row["thickness"]),
        "width": float(row["width"]),
        "height": float(row["height"]),
        "area": float(row["area"]),
        "outline": row["outline"],
        "points": len(row["outline"]),
        "source": str(row["source"]),
        "sourceOrderId": int(row["source_order_id"] or 0),
        "note": str(row["note"]),
        "createdAt": float(row["created_at"]),
        "updatedAt": float(row["updated_at"]),
    }


# ---------------------------------------------------------------- CRUD
def list_scraps(conn: Any, project_id: int, keyword: str = "",
                limit: int = 200) -> Dict[str, Any]:
    """余料库列表 + 当前板材绑定的余料 id（前端据此显示“使用中”）。"""
    rows = db.list_scraps(conn, project_id, keyword, max(1, min(int(limit), 500)))
    sheet = db.load_sheet(conn, project_id)
    items = [_row_to_dict(row) for row in rows]
    return {
        "projectId": project_id,
        "items": items,
        "count": len(items),
        "totalArea": round(sum(item["area"] for item in items), 1),
        "currentScrapId": int(sheet.get("scrapId") or 0),
    }


def get_scrap(conn: Any, project_id: int, scrap_id: int) -> Dict[str, Any]:
    row = db.load_scrap(conn, project_id, scrap_id)
    if row is None:
        raise ScrapError("余料 #%d 不存在" % int(scrap_id))
    return _row_to_dict(row)


def create_scrap(conn: Any, project_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    """手工/接口直建一块余料（不经过排料板）。"""
    shape = normalize_outline(payload.get("outline"), payload.get("width"),
                              payload.get("height"))
    name = _as_text(payload.get("name"), "名称", 64) or "余料 %s" % time.strftime("%m-%d %H:%M")
    data = {
        "name": name,
        "code": _as_text(payload.get("code"), "编号", 64),
        "material": _as_text(payload.get("material"), "物料", 64),
        "thickness": max(0.0, float(payload.get("thickness") or 0.0)),
        "width": shape["width"],
        "height": shape["height"],
        "area": shape["area"],
        "outline": _dumps(shape["outline"]),
        "source": _as_text(payload.get("source"), "来源", 16) or "manual",
        "source_order_id": int(payload.get("sourceOrderId") or 0),
        "note": _as_text(payload.get("note"), "备注", 500),
    }
    scrap_id = db.insert_scrap(conn, project_id, data)
    logger.info("余料已入库: #%d %s (%.0f×%.0f)", scrap_id, name,
                shape["width"], shape["height"])
    return get_scrap(conn, project_id, scrap_id)


def update_scrap(conn: Any, project_id: int, scrap_id: int,
                 payload: Dict[str, Any]) -> Dict[str, Any]:
    """改名称/编号/物料/备注；给了轮廓就重新算尺寸与面积。"""
    row = db.load_scrap(conn, project_id, scrap_id)
    if row is None:
        raise ScrapError("余料 #%d 不存在" % int(scrap_id))
    outline = payload.get("outline") or row["outline"]
    shape = normalize_outline(outline, payload.get("width"), payload.get("height"))
    data = {
        "name": _as_text(payload.get("name"), "名称", 64) or row["name"],
        "code": _as_text(payload.get("code", row["code"]), "编号", 64),
        "material": _as_text(payload.get("material", row["material"]), "物料", 64),
        "thickness": max(0.0, float(payload.get("thickness", row["thickness"]) or 0.0)),
        "width": shape["width"],
        "height": shape["height"],
        "area": shape["area"],
        "outline": _dumps(shape["outline"]),
        "source": row["source"],
        "source_order_id": int(row["source_order_id"] or 0),
        "note": _as_text(payload.get("note", row["note"]), "备注", 500),
    }
    db.update_scrap(conn, project_id, scrap_id, data)
    return get_scrap(conn, project_id, scrap_id)


def remove_scrap(conn: Any, project_id: int, scrap_id: int) -> Dict[str, Any]:
    """删除余料；如果它正被当板材用，顺带解绑（排料板退回矩形板）。"""
    row = db.load_scrap(conn, project_id, scrap_id)
    if row is None:
        raise ScrapError("余料 #%d 不存在" % int(scrap_id))
    if not db.delete_scrap(conn, project_id, scrap_id):
        raise ScrapError("余料 #%d 删除失败" % int(scrap_id))
    sheet = db.load_sheet(conn, project_id)
    unbound = int(sheet.get("scrapId") or 0) == int(scrap_id)
    if unbound:
        db.set_sheet_scrap(conn, project_id, 0)
    logger.info("余料已删除: #%d %s（解绑=%s）", scrap_id, row["name"], unbound)
    return {"deleted": True, "id": int(scrap_id), "unbound": unbound}


def _dumps(points: Any) -> str:
    """轮廓落库：紧凑 JSON（一块板上可能有好几块碎料，不想撑大体积）。"""
    return json.dumps(points, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------- 剩余区域
def _boundary_of(state: LayoutState) -> List[List[float]]:
    """当前板材的可用边界：余料轮廓，或矩形板内缩 margin 后的矩形。"""
    if state.boundary is not None:
        return [[float(x), float(y)] for x, y in state.boundary]
    width = float(state.sheet["width"])
    height = float(state.sheet["height"])
    margin = float(state.sheet["margin"])
    return [[margin, margin], [width - margin, margin],
            [width - margin, height - margin], [margin, height - margin]]


def _obstacles(state: LayoutState, gap: float) -> List[List[List[float]]]:
    """已摆零件的碰撞轮廓（按 gap/2 外扩，保证余料留出安全边）。"""
    pc = _clipper()
    out: List[List[List[float]]] = []
    for part in state.parts:
        placement = state.placements.get(part.id)
        if placement is None:
            continue
        outline = part.rotated_outline(placement.rot, placement.tx, placement.ty)
        path = _to_path(outline)
        if gap > 0:
            offset = pc.PyclipperOffset()
            offset.AddPath(path, pc.JT_ROUND, pc.ET_CLOSEDPOLYGON)
            grown = offset.Execute(gap / 2.0 * SCALE)
            path = grown[0] if grown else path
        out.append(_from_path(path))
    return out


def remaining_regions(conn: Any, project_id: int, min_area: float = DEFAULT_MIN_AREA,
                      gap: Optional[float] = None) -> Dict[str, Any]:
    """算出当前板材上还能用的整块余料（板材 - 已摆零件）。

    返回值里 outline 是**板材坐标**（前端直接画在画布上即可）；
    保存时后端会重新计算一遍再按 index 取，不信任前端回传的几何。
    """
    state = layout_service.load(conn, project_id)
    if not state.parts:
        raise ScrapError("当前工程没有零件，先排料再保存余料")
    threshold = max(0.0, float(min_area))
    gap_value = float(state.sheet["gap"] if gap is None else gap)

    pc = _clipper()
    boundary = _boundary_of(state)
    clipper = pc.Pyclipper()
    clipper.AddPath(_to_path(boundary), pc.PT_SUBJECT, True)
    for obstacle in _obstacles(state, gap_value):
        clipper.AddPath(_to_path(obstacle), pc.PT_CLIP, True)
    try:
        solution = clipper.Execute(pc.CT_DIFFERENCE, pc.PFT_NONZERO, pc.PFT_NONZERO)
    except Exception as exc:                   # 自相交等退化数据
        raise ScrapError("剩余区域计算失败：%s" % exc) from exc

    regions: List[Dict[str, Any]] = []
    sheet_area = max(1.0, float(state.sheet["width"]) * float(state.sheet["height"]))
    for path in solution:
        area = abs(pc.Area(path)) / (SCALE * SCALE)
        if area < threshold:
            continue
        points = _simplify(pc, path)
        if len(points) < 3:
            continue
        min_x, min_y, max_x, max_y = _bbox(points)
        regions.append({
            "area": round(area, 1),
            "width": round(max_x - min_x, 3),
            "height": round(max_y - min_y, 3),
            "outline": points,                  # 板材坐标（绝对）
            "points": len(points),
        })
    regions.sort(key=lambda item: item["area"], reverse=True)
    regions = regions[:MAX_REGIONS]
    for index, region in enumerate(regions):
        region["index"] = index

    leftover = sum(region["area"] for region in regions)
    used = sum(part.area for part in state.parts if part.id in state.placements)
    logger.info("剩余区域计算: %d 块 · 可用 %.0f mm² · 零件面积 %.0f mm²",
                len(regions), leftover, used)
    return {
        "projectId": project_id,
        "regions": regions,
        "count": len(regions),
        "stats": {
            "sheetWidth": round(float(state.sheet["width"]), 3),
            "sheetHeight": round(float(state.sheet["height"]), 3),
            "sheetArea": round(sheet_area, 1),
            "usedArea": round(used, 1),
            "leftoverArea": round(leftover, 1),
            # 余料率：可回收面积 / 板材面积（含零件占用的部分，直观够用）
            "leftoverRatio": round(leftover / sheet_area, 4),
            "partCount": len(state.placements),
            "gap": round(gap_value, 3),
            "scrapId": int(state.sheet.get("scrapId") or 0),
        },
    }


def save_regions(conn: Any, project_id: int, indices: Sequence[int],
                 min_area: float = DEFAULT_MIN_AREA, gap: Optional[float] = None,
                 name_prefix: str = "", material: str = "",
                 thickness: float = 0.0, note: str = "") -> Dict[str, Any]:
    """把选中的剩余区域落库成余料。

    只接受 index：几何以后端重算结果为准，避免前端传回被改过的轮廓。
    """
    wanted = {int(index) for index in indices}
    if not wanted:
        raise ScrapError("请先勾选要保存的余料区域")
    result = remaining_regions(conn, project_id, min_area=min_area, gap=gap)
    picked = [region for region in result["regions"] if int(region["index"]) in wanted]
    if not picked:
        raise ScrapError("勾选的区域已不存在（排料状态可能变了），请重新计算")

    prefix = _as_text(name_prefix, "名称前缀", 40) or "余料"
    stamp = time.strftime("%m-%d %H:%M")
    saved: List[Dict[str, Any]] = []
    for order, region in enumerate(picked, start=1):
        shape = normalize_outline(region["outline"])
        data = {
            "name": "%s %s-%d" % (prefix, stamp, order),
            "code": "",
            "material": _as_text(material, "物料", 64),
            "thickness": max(0.0, float(thickness or 0.0)),
            "width": shape["width"],
            "height": shape["height"],
            "area": shape["area"],
            "outline": _dumps(shape["outline"]),
            "source": "layout",
            "source_order_id": 0,
            "note": _as_text(note, "备注", 500),
        }
        scrap_id = db.insert_scrap(conn, project_id, data)
        saved.append(get_scrap(conn, project_id, scrap_id))
    logger.info("剩余区域已入库: %d 块", len(saved))
    return {"ok": True, "saved": saved, "count": len(saved), "stats": result["stats"]}


# ---------------------------------------------------------------- 应用为板材
def apply_to_sheet(conn: Any, project_id: int, scrap_id: int,
                   gap: Optional[float] = None,
                   reset: bool = True) -> Dict[str, Any]:
    """把余料设为当前排料板：尺寸取余料包围盒、margin 归零、绑定 scrap_id。

    reset=True（默认）会清掉旧摆放再按新板材做一次初始行排，
    换板后零件位置本来就失效，留着反而出现"零件飘在板外"的假象。
    """
    row = db.load_scrap(conn, project_id, scrap_id)
    if row is None:
        raise ScrapError("余料 #%d 不存在" % int(scrap_id))
    sheet = db.load_sheet(conn, project_id)
    db.save_sheet(conn, project_id, {
        "width": float(row["width"]),
        "height": float(row["height"]),
        # 余料轮廓本身就是可用边界，再内缩一圈余量会把料浪费掉
        "margin": 0.0,
        "gap": float(sheet["gap"] if gap is None else gap),
    })
    db.set_sheet_scrap(conn, project_id, int(scrap_id))
    if reset:
        db.clear_placements(conn, project_id)
        state = layout_service.open(conn, project_id)
        parked = _park_outside(conn, project_id, state)
        if parked:
            state = layout_service.load(conn, project_id)
    else:
        state = layout_service.load(conn, project_id)
    logger.info("余料 #%d「%s」已设为当前板材（%.0f×%.0f）",
                scrap_id, row["name"], row["width"], row["height"])
    return {"scrap": _row_to_dict(row), "stats": layout_service.build_meta(state, {})["stats"]}


def clear_sheet(conn: Any, project_id: int, width: Optional[float] = None,
                height: Optional[float] = None, margin: Optional[float] = None,
                gap: Optional[float] = None) -> Dict[str, Any]:
    """解绑余料，退回规则矩形板（可选同时套用面板里的新尺寸）。"""
    sheet = db.load_sheet(conn, project_id)
    new_width = float(width) if width else float(sheet["width"])
    new_height = float(height) if height else float(sheet["height"])
    new_margin = float(margin) if margin is not None else float(sheet["margin"])
    new_gap = float(gap) if gap is not None else float(sheet["gap"])
    if new_width <= 0 or new_height <= 0:
        raise ScrapError("板材宽高必须大于 0")
    db.set_sheet_scrap(conn, project_id, 0)
    db.save_sheet(conn, project_id, {"width": new_width, "height": new_height,
                                     "margin": new_margin, "gap": new_gap})
    state = layout_service.load(conn, project_id)
    logger.info("已解绑余料，板材退回矩形 %.0f×%.0f", new_width, new_height)
    return {"ok": True, "stats": layout_service.build_meta(state, {})["stats"]}


def _park_outside(conn: Any, project_id: int, state: LayoutState) -> int:
    """把落在余料轮廓外的零件停靠到板材上方一行。

    停靠算法统一放在 layout_service（模块 3/10 共用）：换余料板与点"复位"走同一条逻辑，
    这里保留薄封装只是为了历史调用点不动，行为仍是"越界/间距不足都挪到板外"。
    """
    return layout_service.park_outside(conn, project_id, state, check_gap=True)

