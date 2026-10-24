"""模块 10 余料管理 —— 后端控制台验收脚本。

两种跑法：

    # 1) 只测服务层（不需要后端，秒级）
    backend\\.venv\\Scripts\\python.exe scripts\\scrap_test.py

    # 2) 服务层 + 接口全链路（后端需先启动在 8975）
    backend\\.venv\\Scripts\\python.exe scripts\\scrap_test.py --url http://127.0.0.1:8975

验收主线（对应需求"保存余料 -> 新建工单选余料当板材 -> 在余料范围内继续排零件"）：

    A 系列  余料入库：矩形/凹多边形轮廓归一化、检索、修改、非法轮廓拒绝
    B 系列  剩余区域：排完板算碎块、面积降序、勾选落库
    C 系列  余料当板材：尺寸取包围盒、余量归零、绑定 id、meta 下发轮廓
    D 系列  L 形余料边界：缺口内拒绝、跨缺口拒绝、轮廓内通过、板框外拒绝
    E 系列  复用排料接口：自动排料落位件全部在余料轮廓内，放不下的停靠板外
    F 系列  解绑 / 删除自动解绑 / 工单快照带余料绑定并能随工单回开恢复
    G 系列  HTTP 接口全链路（--url 时才跑）

注意：脚本会用内置示例图形重建当前工程并改动板材参数与摆放；
结束时把板材恢复为 1220x2440 / 余量 10 / 间距 5，复位成初始行排，并删掉测试余料。

退出码：0 全部通过；1 存在失败项。
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 允许直接以 scripts/scrap_test.py 运行：把 backend 与 venv 依赖加入 import 路径
BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR / ".venv" / "Lib" / "site-packages"))

CHECKS: List[Tuple[str, bool, str]] = []

EPS = 1e-3
DEFAULT_SHEET = {"width": 1220.0, "height": 2440.0, "margin": 10.0, "gap": 5.0}
# L 形余料：底横 1000x400 + 左竖 400x600，缺口在右上（x>400 且 y>400 不可用）
L_OUTLINE = [[0, 0], [1000, 0], [1000, 400], [400, 400], [400, 1000], [0, 1000]]


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, ok, detail))
    print("  %s %s%s" % ("[OK]  " if ok else "[FAIL]", name, ("  " + detail) if detail else ""))
    return ok


# ---------------------------------------------------------------- HTTP
def http_json(method: str, url: str, body: Optional[Dict[str, Any]] = None,
              timeout: float = 30.0) -> Any:
    """极简 JSON 请求：不引入 requests/httpx，保持后端依赖干净。"""
    import json

    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError("%s %s -> HTTP %s: %s" % (method, url, exc.code, detail))


# ---------------------------------------------------------------- 独立几何校验
# 说明：下面这几个函数刻意不复用 LayoutService._inside，测试要能独立发现问题，
# 而不是"用被测代码验证被测代码"。
def _on_edge(x: float, y: float, poly: Sequence[Sequence[float]]) -> bool:
    """点是否落在多边形某条边上（1e-6 容差）。"""
    count = len(poly)
    for index in range(count):
        x1, y1 = float(poly[index][0]), float(poly[index][1])
        x2, y2 = float(poly[(index + 1) % count][0]), float(poly[(index + 1) % count][1])
        cross = (x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)
        if abs(cross) > 1e-6:
            continue
        if (min(x1, x2) - 1e-6 <= x <= max(x1, x2) + 1e-6
                and min(y1, y2) - 1e-6 <= y <= max(y1, y2) + 1e-6):
            return True
    return False


def _ray_inside(x: float, y: float, poly: Sequence[Sequence[float]]) -> bool:
    """射线法（不做边界特判，由调用方结合 _on_edge 使用）。"""
    inside = False
    count = len(poly)
    for index in range(count):
        x1, y1 = float(poly[index][0]), float(poly[index][1])
        x2, y2 = float(poly[(index + 1) % count][0]), float(poly[(index + 1) % count][1])
        if (y1 > y) != (y2 > y):
            xin = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if xin >= x:
                inside = not inside
    return inside


def point_in_poly(x: float, y: float, poly: Sequence[Sequence[float]]) -> bool:
    return _on_edge(x, y, poly) or _ray_inside(x, y, poly)


def strictly_inside(x: float, y: float, poly: Sequence[Sequence[float]]) -> bool:
    """严格内部（边界上不算）：用来查"边界的凹点是否落进零件"。"""
    return (not _on_edge(x, y, poly)) and _ray_inside(x, y, poly)


def outline_inside(points: Sequence[Sequence[float]],
                   boundary: Sequence[Sequence[float]]) -> bool:
    """零件轮廓是否整块落在余料轮廓内。

    两个方向都查：
        1) 零件的每个顶点都在边界内（挡住"整体外移/跨出直边"）；
        2) 边界的每个顶点都不在零件内部（挡住"零件横跨 L 形缺口"，
           这种情况下零件顶点可以全在边界内，但缺口被零件盖住了）。
    """
    poly = [[float(p[0]), float(p[1])] for p in boundary]
    part = [[float(p[0]), float(p[1])] for p in points]
    if not all(point_in_poly(x, y, poly) for x, y in part):
        return False
    if any(strictly_inside(x, y, part) for x, y in poly):
        return False
    return True


def placement_inside(state: Any, part: Any) -> bool:
    """按当前摆放算零件真实轮廓，再判是否在余料轮廓内。"""
    placement = state.placements[part.id]
    outline = part.rotated_outline(placement.rot, placement.tx, placement.ty)
    return outline_inside(outline, state.boundary)


# ---------------------------------------------------------------- 状态准备
def setup_sheet(conn: Any, project_id: int, sheet: Optional[Dict[str, float]] = None,
                scrap_id: int = 0) -> None:
    """把板材参数与余料绑定复位到已知状态。"""
    from app.models import db

    data = dict(DEFAULT_SHEET)
    data.update(sheet or {})
    db.save_sheet(conn, project_id, data)
    db.set_sheet_scrap(conn, project_id, int(scrap_id))


def cleanup(conn: Any, project_id: int, scrap_ids: Sequence[int],
            order_ids: Sequence[int] = ()) -> None:
    """删掉本次脚本造的余料/工单，并把排料板复位成默认整板。"""
    from app.models import db
    from app.services.layout_service import layout_service

    for order_id in order_ids:
        try:
            db.delete_work_order(conn, project_id, int(order_id))
        except Exception:                       # 收尾失败不影响验收结论
            pass
    setup_sheet(conn, project_id)
    for scrap_id in scrap_ids:
        try:
            db.delete_scrap(conn, project_id, int(scrap_id))
        except Exception:
            pass
    db.clear_placements(conn, project_id)
    layout_service.open(conn, project_id)


# ---------------------------------------------------------------- 服务层
def service_tests() -> None:
    from app.models import db
    from app.services import geometry_service, scrap_service, work_order_service
    from app.services.layout_service import layout_service
    from app.services.nest_service import auto_nest_job
    from app.services.scrap_service import ScrapError, normalize_outline

    db.init_db()
    with db.get_conn() as conn:
        project_id = db.get_active_project_id(conn)

    # persist_document 会连同零件/摆放一起清空，排料从零开始
    geometry_service.persist_document(project_id, geometry_service.build_demo_document())

    created: List[int] = []
    orders: List[int] = []

    # ============================================================ A 入库
    print("== A. 余料入库 / 检索 ==")
    with db.get_conn() as conn:
        setup_sheet(conn, project_id)
        rect = scrap_service.create_scrap(conn, project_id, {
            "name": "手工方块", "material": "18mm 颗粒板", "thickness": 18.0,
            "width": 800.0, "height": 600.0,
        })
        created.append(rect["id"])
    check("A1 手工矩形余料",
          abs(rect["width"] - 800) < EPS and abs(rect["height"] - 600) < EPS
          and rect["points"] == 4 and abs(rect["area"] - 480000) < 1.0,
          "%s · %.0f×%.0f · 面积 %.0f · %d 点" % (
              rect["name"], rect["width"], rect["height"], rect["area"], rect["points"]))

    with db.get_conn() as conn:
        # 输入轮廓故意不贴原点：入库应把包围盒左下角平移到 (0,0)
        moved = scrap_service.create_scrap(conn, project_id, {
            "name": "偏移轮廓", "outline": [[100, 100], [900, 100], [900, 500], [100, 500]],
        })
        created.append(moved["id"])
    normalized = moved["outline"][0] == [0.0, 0.0] or (
        abs(moved["outline"][0][0]) < EPS and abs(moved["outline"][0][1]) < EPS)
    check("A2 轮廓归一化", normalized and abs(moved["width"] - 800) < EPS
          and abs(moved["height"] - 400) < EPS,
          "首点 %s · %.0f×%.0f" % (moved["outline"][0], moved["width"], moved["height"]))

    with db.get_conn() as conn:
        listing = scrap_service.list_scraps(conn, project_id, "手工")
        all_items = scrap_service.list_scraps(conn, project_id)
    check("A3 检索与计数", listing["count"] >= 1 and all_items["count"] >= 2
          and all_items["currentScrapId"] == 0 and all_items["totalArea"] > 0,
          "关键字命中 %d 块 · 库内 %d 块 · 总可用 %.0f mm²" % (
              listing["count"], all_items["count"], all_items["totalArea"]))

    bad_outline = False
    try:
        normalize_outline([[0, 0], [100, 100]])     # 两点且没有宽高 -> 无法构成面
    except ScrapError:
        bad_outline = True
    degenerate = False
    try:
        normalize_outline([[0, 0], [100, 0], [200, 0]])   # 共线 -> 面积为 0
    except ScrapError:
        degenerate = True
    check("A4 非法轮廓拒绝", bad_outline and degenerate,
          "点不足/面积为零都抛 ScrapError")

    with db.get_conn() as conn:
        renamed = scrap_service.update_scrap(conn, project_id, rect["id"], {
            "name": "手工方块-改", "material": "25mm 多层板",
        })
    check("A5 修改不改几何", renamed["name"] == "手工方块-改"
          and renamed["material"] == "25mm 多层板"
          and abs(renamed["width"] - 800) < EPS,
          "%s · %s · 仍 %.0f×%.0f" % (renamed["name"], renamed["material"],
                                      renamed["width"], renamed["height"]))

    with db.get_conn() as conn:
        removed = scrap_service.remove_scrap(conn, project_id, moved["id"])
    created.remove(moved["id"])
    check("A6 删除未使用余料", removed["deleted"] is True and removed["unbound"] is False)

    # ============================================================ B 剩余区域
    print("== B. 排板后的剩余区域 ==")
    with db.get_conn() as conn:
        setup_sheet(conn, project_id)
        db.clear_placements(conn, project_id)
        state = layout_service.open(conn, project_id)
        remaining = scrap_service.remaining_regions(conn, project_id)
    sheet_area = float(state.sheet["width"]) * float(state.sheet["height"])
    check("B1 剩余区域非空", remaining["count"] >= 1,
          "%d 块 · 可用 %.0f mm² · 板 %.0f mm²" % (
              remaining["count"], remaining["stats"]["leftoverArea"], sheet_area))

    areas = [region["area"] for region in remaining["regions"]]
    check("B2 面积降序", all(a >= b - EPS for a, b in zip(areas, areas[1:])),
          " · ".join("%.0f" % value for value in areas[:4]))

    inside_board = True
    for region in remaining["regions"]:
        for x, y in region["outline"]:
            if not (state.sheet["margin"] - EPS <= x <= state.sheet["width"] - state.sheet["margin"] + EPS
                    and state.sheet["margin"] - EPS <= y <= state.sheet["height"] - state.sheet["margin"] + EPS):
                inside_board = False
    check("B3 碎块都在板内", inside_board, "已按四边余量内缩")

    with db.get_conn() as conn:
        saved = scrap_service.save_regions(conn, project_id, [0], name_prefix="测试余料",
                                          material="18mm 颗粒板", thickness=18.0)
    first = saved["saved"][0]
    created.extend(item["id"] for item in saved["saved"])
    check("B4 勾选落库", saved["count"] == 1 and first["source"] == "layout"
          and first["points"] >= 3 and first["area"] > 0
          and first["name"].startswith("测试余料"),
          "%s · %.0f×%.0f · %d 点" % (first["name"], first["width"],
                                      first["height"], first["points"]))

    empty_rejected = False
    try:
        with db.get_conn() as conn:
            scrap_service.save_regions(conn, project_id, [])
    except ScrapError:
        empty_rejected = True
    stale_rejected = False
    try:
        with db.get_conn() as conn:
            scrap_service.save_regions(conn, project_id, [999])
    except ScrapError:
        stale_rejected = True
    check("B5 非法勾选拒绝", empty_rejected and stale_rejected,
          "空选 / 过期 index 都抛 ScrapError")

    # ============================================================ C 当板材
    print("== C. 余料当板材 ==")
    with db.get_conn() as conn:
        scrap_service.apply_to_sheet(conn, project_id, first["id"], gap=5.0)
        state = layout_service.load(conn, project_id)
        sheet = db.load_sheet(conn, project_id)
        meta = layout_service.build_meta(state, {})
    check("C1 尺寸取余料包围盒",
          abs(float(sheet["width"]) - first["width"]) < EPS
          and abs(float(sheet["height"]) - first["height"]) < EPS,
          "板材 %.0f×%.0f · 余料 %.0f×%.0f" % (
              sheet["width"], sheet["height"], first["width"], first["height"]))
    check("C2 余量归零并绑定 id",
          abs(float(sheet["margin"])) < EPS and int(sheet["scrapId"]) == first["id"],
          "margin=%.1f · scrapId=%d" % (sheet["margin"], sheet["scrapId"]))
    check("C3 排料上下文带轮廓", state.boundary is not None
          and (state.scrap or {}).get("id") == first["id"],
          "边界 %d 点" % (0 if state.boundary is None else len(state.boundary)))
    check("C4 meta 下发余料", (meta.get("scrap") or {}).get("id") == first["id"]
          and meta["stats"]["scrapId"] == first["id"]
          and meta["stats"]["boundaryArea"] > 0,
          "boundaryArea=%.0f mm²" % meta["stats"]["boundaryArea"])

    # ============================================================ D L 形边界
    print("== D. L 形余料边界判定 ==")
    with db.get_conn() as conn:
        l_shape = scrap_service.create_scrap(conn, project_id, {
            "name": "L 形余料", "outline": L_OUTLINE,
            "material": "18mm 颗粒板", "thickness": 18.0,
        })
        created.append(l_shape["id"])
        scrap_service.apply_to_sheet(conn, project_id, l_shape["id"], gap=5.0)
        state = layout_service.load(conn, project_id)
        target = next((part for part in state.parts
                       if abs(part.width - 300) < 1 and abs(part.height - 200) < 1), None)
    check("D1 L 形余料当板材",
          abs(l_shape["width"] - 1000) < EPS and abs(l_shape["height"] - 1000) < EPS
          and abs(l_shape["area"] - 640000) < 1.0
          and state.boundary is not None and abs(state.sheet["width"] - 1000) < EPS,
          "包围盒 %.0f×%.0f · 面积 %.0f mm²（缺口 600×600 不可用）" % (
              l_shape["width"], l_shape["height"], l_shape["area"]))
    if not check("D2 找到 300x200 测试件", target is not None):
        return

    with db.get_conn() as conn:
        notch = layout_service.move(conn, project_id, target.id, 600.0, 600.0)
    check("D3 缺口内拒绝", (not notch.ok) and "余料轮廓" in notch.reason, notch.reason)

    with db.get_conn() as conn:
        cross = layout_service.move(conn, project_id, target.id, 350.0, 450.0)
    check("D4 跨缺口拒绝", (not cross.ok) and "余料轮廓" in cross.reason, cross.reason)

    with db.get_conn() as conn:
        good = layout_service.move(conn, project_id, target.id, 20.0, 700.0)  # 左竖段内
    check("D5 轮廓内通过", good.ok, good.reason or "落位 (20,700)")

    with db.get_conn() as conn:
        outside = layout_service.move(conn, project_id, target.id, 1500.0, 20.0)
    check("D6 板框外拒绝", (not outside.ok) and "右边界" in outside.reason, outside.reason)

    # ============================================================ E 自动排料
    print("== E. 复用排料接口：在余料内自动排料 ==")
    with db.get_conn() as conn:
        scrap_service.apply_to_sheet(conn, project_id, l_shape["id"], gap=5.0)
    result = auto_nest_job({"projectId": project_id, "engine": "scan",
                            "rotation": True}, lambda *args: None)
    stats = result["stats"]
    with db.get_conn() as conn:
        state = layout_service.load(conn, project_id)
    height = float(state.sheet["height"])
    on_board = [part for part in state.parts
                if part.id in state.placements and state.placements[part.id].ty <= height + EPS]
    outside_sheet = [part for part in state.parts
                     if part.id in state.placements and state.placements[part.id].ty > height + EPS]
    check("E1 确实排上了零件", stats["placedCount"] >= 3,
          "%d 落位 / %d 未排下 · %d ms" % (stats["placedCount"],
                                          stats["unplacedCount"], stats["calcMs"]))
    offenders = [part.name for part in on_board if not placement_inside(state, part)]
    check("E2 落位件全在余料轮廓内", not offenders,
          ("越界件: " + "、".join(offenders)) if offenders
          else "%d 件全部在轮廓内（含缺口避让）" % len(on_board))
    check("E3 放不下的停靠板外",
          len(outside_sheet) == stats["unplacedCount"] and len(on_board) == stats["placedCount"],
          "板外停靠 %d 件（ty > %.0f）" % (len(outside_sheet), height))

    # ============================================================ F 解绑/工单
    print("== F. 解绑 / 删除 / 工单快照 ==")
    with db.get_conn() as conn:
        snapshot = work_order_service.capture(conn, project_id)
        bound = int((snapshot.get("sheet") or {}).get("scrapId") or 0)
        order = work_order_service.create_order(conn, project_id, {
            "name": "余料排料验收单", "note": "模块 10 控制台测试自动生成",
        })
        orders.append(order["id"])
    check("F1 工单快照带余料绑定", bound == l_shape["id"],
          "snapshot.sheet.scrapId=%d" % bound)

    with db.get_conn() as conn:
        scrap_service.clear_sheet(conn, project_id, **DEFAULT_SHEET)
        state = layout_service.load(conn, project_id)
    check("F2 解绑退回整板",
          int(state.sheet.get("scrapId") or 0) == 0 and state.boundary is None
          and abs(float(state.sheet["margin"]) - 10.0) < EPS,
          "scrapId=0 · margin=%.0f · 边界=None" % state.sheet["margin"])

    with db.get_conn() as conn:
        work_order_service.open_order(conn, project_id, order["id"])
        state = layout_service.load(conn, project_id)
    check("F3 打开工单恢复余料绑定",
          int(state.sheet.get("scrapId") or 0) == l_shape["id"] and state.boundary is not None,
          "回开后 scrapId=%d" % int(state.sheet.get("scrapId") or 0))

    with db.get_conn() as conn:
        dropped = scrap_service.remove_scrap(conn, project_id, l_shape["id"])
        state = layout_service.load(conn, project_id)
    if l_shape["id"] in created:
        created.remove(l_shape["id"])
    check("F4 删除在用余料自动解绑",
          dropped["unbound"] is True and int(state.sheet.get("scrapId") or 0) == 0
          and state.boundary is None,
          "unbound=%s · 业务不会挂着幽灵轮廓" % dropped["unbound"])

    # 收尾：清掉本次造的余料与工单，板材/摆放复位
    with db.get_conn() as conn:
        cleanup(conn, project_id, created, orders)
    print("  [OK]   收尾：测试余料/工单已删除，板材复位 %.0fx%.0f" % (
        DEFAULT_SHEET["width"], DEFAULT_SHEET["height"]))


# ---------------------------------------------------------------- 接口层
def rest_tests(base: str) -> None:
    from app.models import db

    url = base.rstrip("/")
    db.init_db()
    with db.get_conn() as conn:
        project_id = db.get_active_project_id(conn)

    print("== G. HTTP 接口全链路 ==")
    # 复位：解绑余料 + 默认板材 + 重新行排，保证剩余区域可复现
    http_json("POST", url + "/api/scrap/clear", dict(projectId=project_id, **DEFAULT_SHEET))
    http_json("POST", url + "/api/layout/reset?projectId=%d" % project_id)

    remaining = http_json("POST", url + "/api/scrap/remaining",
                          {"projectId": project_id, "minArea": 10000.0})
    check("G1 剩余区域接口", int(remaining.get("count") or 0) >= 1
          and remaining["regions"][0]["index"] == 0,
          "%d 块 · 最大 %.0f mm²" % (remaining.get("count", 0),
                                    remaining["regions"][0]["area"] if remaining.get("regions") else 0))

    saved = http_json("POST", url + "/api/scrap/save", {
        "projectId": project_id, "indices": [0], "namePrefix": "HTTP 余料",
        "material": "18mm 颗粒板", "thickness": 18.0,
    })
    scrap = saved["saved"][0]
    check("G2 保存余料接口", saved["count"] == 1 and scrap["points"] >= 3
          and scrap["source"] == "layout",
          "余料 #%d %s" % (scrap["id"], scrap["name"]))

    applied = http_json("POST", url + "/api/scrap/%d/apply" % scrap["id"],
                        {"projectId": project_id, "gap": 5.0})
    check("G3 应用为板材",
          abs(float(applied["sheet"]["width"]) - scrap["width"]) < EPS
          and int(applied["stats"]["scrapId"]) == scrap["id"],
          "板材 %.0f×%.0f · margin=%.0f" % (
              applied["sheet"]["width"], applied["sheet"]["height"],
              applied["sheet"]["margin"]))

    meta = http_json("GET", url + "/api/layout?projectId=%d" % project_id)
    outline = (meta.get("scrap") or {}).get("outline") or []
    check("G4 排料 meta 带余料轮廓",
          (meta.get("scrap") or {}).get("id") == scrap["id"] and len(outline) >= 3,
          "轮廓 %d 点 · boundaryArea=%.0f mm²" % (len(outline),
                                                  meta["stats"]["boundaryArea"]))

    target = next((item for item in meta["parts"]
                   if abs(item["width"] - 300) < 1 and abs(item["height"] - 200) < 1), None)
    if target is None and meta.get("parts"):
        target = meta["parts"][0]
    if check("G5 找到可拖拽零件", target is not None):
        rejected = http_json("POST", url + "/api/layout/place", {
            "projectId": project_id, "partId": target["id"], "tx": 100000.0, "ty": 0.0,
        })
        check("G6 越界拖拽被拒", rejected["ok"] is False and bool(rejected["reason"]),
              rejected["reason"])

    listing = http_json("GET", url + "/api/scrap?projectId=%d" % project_id)
    check("G7 列表带 currentScrapId",
          int(listing["currentScrapId"]) == scrap["id"] and listing["count"] >= 1,
          "库内 %d 块 · 使用中 #%d" % (listing["count"], listing["currentScrapId"]))

    created = http_json("POST", url + "/api/scrap", {
        "projectId": project_id, "name": "HTTP 手工块", "width": 500.0, "height": 400.0,
    })
    updated = http_json("PUT", url + "/api/scrap/%d" % created["id"], {
        "projectId": project_id, "name": "HTTP 手工块-改", "material": "15mm 多层板",
        "outline": created["outline"],
    })
    check("G8 新建/修改余料",
          updated["name"] == "HTTP 手工块-改" and updated["material"] == "15mm 多层板"
          and abs(updated["width"] - 500) < EPS,
          "余料 #%d %.0f×%.0f" % (updated["id"], updated["width"], updated["height"]))

    deleted = http_json("DELETE", url + "/api/scrap/%d?projectId=%d" % (created["id"], project_id))
    check("G9 删除余料", deleted.get("deleted") is True and deleted.get("unbound") is False)

    cleared = http_json("POST", url + "/api/scrap/clear",
                        dict(projectId=project_id, **DEFAULT_SHEET))
    check("G10 解绑退回整板", int(cleared["stats"]["scrapId"]) == 0
          and abs(float(cleared["sheet"]["margin"]) - 10.0) < EPS,
          "scrapId=0 · margin=%.0f" % cleared["sheet"]["margin"])

    # 收尾：删掉 HTTP 流程造的余料，恢复初始行排
    http_json("DELETE", url + "/api/scrap/%d?projectId=%d" % (scrap["id"], project_id))
    http_json("POST", url + "/api/layout/reset?projectId=%d" % project_id)


# ---------------------------------------------------------------- 入口
def main() -> int:
    parser = argparse.ArgumentParser(description="模块 10 余料管理后端验收")
    parser.add_argument("--url", default="",
                        help="后端地址（给则追加 HTTP 接口全链路测试，如 http://127.0.0.1:8975）")
    args = parser.parse_args()

    started = time.time()
    print("模块 10 余料管理 —— 后端验收")
    print("")

    try:
        service_tests()
    except Exception as exc:                    # 单个用例炸掉也要把结论打全
        traceback.print_exc()
        check("服务层用例异常中止", False, str(exc))

    if args.url:
        try:
            rest_tests(args.url)
        except Exception as exc:
            traceback.print_exc()
            check("接口用例异常中止", False, str(exc))
    else:
        print("")
        print("（未给 --url，跳过 HTTP 接口用例）")

    failed = [item for item in CHECKS if not item[1]]
    print("")
    print("结果：%d 项通过 / %d 项失败 · 用时 %.1fs" % (
        len(CHECKS) - len(failed), len(failed), time.time() - started))
    if failed:
        for name, _ok, detail in failed:
            print("  失败: %s%s" % (name, ("  " + detail) if detail else ""))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
