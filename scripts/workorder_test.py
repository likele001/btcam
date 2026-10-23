"""模块 8 验收：工单存档（快照 / 检索 / 打开 / 另存 / 文件归档）。

两种跑法：

    # 1) 只测服务层（不需要后端，秒级）
    backend\\.venv\\Scripts\\python.exe scripts\\workorder_test.py

    # 2) 服务层 + 接口全链路（后端需先启动在 8975）
    backend\\.venv\\Scripts\\python.exe scripts\\workorder_test.py --url http://127.0.0.1:8975

覆盖的验收项：

    A1 快照采集      零件/轮廓/板材/摆放/参数齐全，顶点 float32 往返一致
    A2 保存与检索    按名称、编号、日期区间过滤
    A3 重复打开      清空工程后按快照恢复零件、轮廓、板材与摆放
    A4 另存          副本独立，不影响原工单
    A5 文件归档      加工文件路径登记 + 列表 fileCount
    A6 参数清洗      选中集/临时轮廓不落库；名称校验；改名不丢快照
    A7 删除          工单与文件登记一并删除

    B1 保存接口      POST /api/workorders 返回统计
    B2 列表检索      关键词 + 起止日期
    B3 详情与改名    GET / PUT
    B4 打开接口      快照写回并回填刀路/后处理参数
    B5 导出登记      /api/export 带 workOrderId -> files 有记录
    B6 另存接口      duplicate 生成独立工单
    B7 参数校验      空名称 422 / 未知工单打开 404
    B8 删除接口      DELETE 后 GET 404

退出码：0 全部通过；1 存在失败项。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

CHECKS: List[Tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, ok, detail))
    print("  %s %s%s" % ("[OK]  " if ok else "[FAIL]", name, ("  " + detail) if detail else ""))
    return ok


# ---------------------------------------------------------------- 服务层
def service_tests() -> None:
    import numpy as np

    from app.models import db
    from app.services import geometry_service, work_order_service
    from app.services.work_order_service import WorkOrderError

    print("== A. 服务层：快照 / 检索 / 打开 ==")

    now = time.time()
    with db.get_conn() as conn:
        pid = int(conn.execute(
            "INSERT INTO project(name, note, created_at, updated_at) VALUES (?,?,?,?)",
            ("WO_SERVICE_TEST", "", now, now)).lastrowid)

    created_orders: List[int] = []
    try:
        # 临时工程：图层 + 示例图形 + 板材 + 摆放
        with db.get_conn() as conn:
            for name, color, operation in (
                    ("切割", 0x4FA3FF, "cut"),
                    ("划槽", 0xFFB74F, "engrave"),
                    ("定位孔", 0x8BE38B, "drill")):
                db.ensure_layer(conn, pid, name, color, operation)
        geometry_service.persist_document(pid, geometry_service.build_demo_document())
        with db.get_conn() as conn:
            db.save_sheet(conn, pid, {"width": 1220.0, "height": 2440.0,
                                      "margin": 10.0, "gap": 5.0})
            part_rows = db.load_parts(conn, pid)
            for index, part in enumerate(part_rows):
                db.save_placement(conn, pid, part["id"], 0, 20.0 + index * 30.0, 20.0,
                                  0.0, index == 0, index, False)

            # --- A1 快照采集（含参数清洗与浮点往返）
            raw_rows = db.load_polylines(conn, pid)
            decoded = work_order_service.decode_points(
                work_order_service.encode_points(raw_rows[0]["pts"]))
            snapshot = work_order_service.capture(conn, pid, {
                "cutType": "outer", "depth": 9.0, "ids": [1, 2, 3],
                "contours": [{"pts": [[0, 0], [1, 1]]}],
                "startPoints": [{"x": 12.5, "y": 30.0}],
            }, {"processorId": "weihong_nc", "format": "nc", "speed": 28})
            parts = snapshot["parts"]
            check("A1 快照采集",
                  len(parts) > 0 and len(snapshot["placements"]) == len(parts)
                  and snapshot["sheet"]["width"] == 1220.0 and len(snapshot["layers"]) >= 3
                  and "ids" not in snapshot["toolpath"]
                  and "contours" not in snapshot["toolpath"]
                  and snapshot["toolpath"]["depth"] == 9.0
                  and snapshot["toolpath"]["startPoints"][0]["x"] == 12.5
                  and snapshot["post"]["processorId"] == "weihong_nc"
                  and np.allclose(decoded, raw_rows[0]["pts"]),
                  "%d 个零件 / %d 条摆放 / %d 条轮廓" % (
                      len(parts), len(snapshot["placements"]),
                      snapshot["stats"]["polylineCount"]))

            save_params = {
                "name": "验收工单 A", "code": "WO-001", "note": "服务层验收",
                "toolpath": {"cutType": "outer", "depth": 9.0, "toolDiameter": 6.0,
                             "ids": [1, 2], "contours": [{"pts": [[0, 0]]}]},
                "post": {"processorId": "weihong_nc", "format": "nc",
                         "flipY": False, "speed": 28, "force": 20},
            }
            order = work_order_service.create_order(conn, pid, save_params)
            created_orders.append(int(order["id"]))
            stats = order["stats"]

            # --- A2 保存与检索
            hit = work_order_service.list_orders(conn, pid, "验收工单")
            by_code = work_order_service.list_orders(conn, pid, "WO-001")
            missing = work_order_service.list_orders(conn, pid, "不存在的工单")
            today = datetime.now().strftime("%Y-%m-%d")
            day_start = datetime.strptime(today, "%Y-%m-%d").timestamp()
            in_day = work_order_service.list_orders(
                conn, pid, "", day_start, day_start + 86400)
            tomorrow = day_start + 86400 * 2
            out_day = work_order_service.list_orders(
                conn, pid, "", tomorrow, tomorrow + 86400)
            check("A2 保存与检索",
                  len(hit["orders"]) == 1 and len(by_code["orders"]) == 1
                  and len(missing["orders"]) == 0 and len(in_day["orders"]) == 1
                  and len(out_day["orders"]) == 0
                  and order["code"] == "WO-001",
                  "关键词 %d / 编号 %d / 当日 %d / 区间外 %d" % (
                      len(hit["orders"]), len(by_code["orders"]),
                      len(in_day["orders"]), len(out_day["orders"])))

            # --- A3 重复打开：破坏当前工程后按快照恢复
            db.clear_document(conn, pid)
            db.save_sheet(conn, pid, {"width": 600.0, "height": 600.0,
                                      "margin": 5.0, "gap": 2.0})
            opened = work_order_service.open_order(conn, pid, int(order["id"]))
            restored_parts = db.load_parts(conn, pid)
            restored_rows = db.load_polylines(conn, pid)
            restored_sheet = db.load_sheet(conn, pid)
            restored_placements = db.load_placements(conn, pid)
            check("A3 重复打开",
                  len(restored_parts) == stats["partCount"]
                  and len(restored_rows) == stats["polylineCount"]
                  and len(restored_placements) == stats["placementCount"]
                  and restored_sheet["width"] == 1220.0
                  and opened["toolpath"]["depth"] == 9.0
                  and opened["post"]["processorId"] == "weihong_nc"
                  and opened["missing"] == [],
                  "恢复 %d 零件 / %d 轮廓 / %d 摆放 / 板宽 %.0f" % (
                      len(restored_parts), len(restored_rows),
                      len(restored_placements), restored_sheet["width"]))

            # --- A4 另存
            copy = work_order_service.duplicate_order(conn, pid, int(order["id"]))
            created_orders.append(int(copy["id"]))
            check("A4 另存",
                  copy["id"] != order["id"] and copy["name"].endswith("副本")
                  and copy["stats"]["partCount"] == stats["partCount"]
                  and copy["fileCount"] == 0,
                  "#%d -> #%d「%s」" % (order["id"], copy["id"], copy["name"]))

            # --- A5 文件归档
            added = work_order_service.add_export_files(conn, pid, int(order["id"]), [
                {"path": "C:/tmp/wo_a_s1.nc", "format": "nc",
                 "processor": "weihong_nc", "bytes": 1200, "sheetIndex": 0},
                {"path": "C:/tmp/wo_a_s2.nc", "format": "nc",
                 "processor": "weihong_nc", "bytes": 1300, "sheetIndex": 1},
            ])
            files = work_order_service.list_files(conn, pid, int(order["id"]))
            listing = work_order_service.list_orders(conn, pid, "验收工单 A")
            listed = next((item for item in listing["orders"]
                           if item["id"] == order["id"]), {})
            check("A5 文件归档",
                  added == 2 and files["total"] == 2
                  and listed.get("fileCount") == 2
                  and files["files"][0]["sheetIndex"] in (0, 1),
                  "登记 %d 个文件 / 列表 fileCount=%s" % (added, listed.get("fileCount")))

            # --- A6 参数清洗、名称校验、改名不丢快照
            renamed = work_order_service.update_order(conn, pid, int(order["id"]), {
                "name": "验收工单 A-改", "code": "WO-001", "note": "改备注"})
            rejected = False
            try:
                work_order_service.create_order(conn, pid, {"name": "   "})
            except WorkOrderError:
                rejected = True
            missing_open = False
            try:
                work_order_service.open_order(conn, pid, 999999)
            except WorkOrderError:
                missing_open = True
            check("A6 参数清洗与校验",
                  renamed["name"].endswith("-改")
                  and renamed["stats"]["partCount"] == stats["partCount"]
                  and "ids" not in renamed["toolpath"]
                  and rejected and missing_open,
                  "改名保留快照 / 空名称拒绝 / 未知工单拒绝")

            # --- A7 删除
            removed_a = work_order_service.remove_order(conn, pid, int(copy["id"]))
            removed_b = work_order_service.remove_order(conn, pid, int(order["id"]))
            gone = False
            try:
                work_order_service.get_order(conn, pid, int(order["id"]))
            except WorkOrderError:
                gone = True
            left = work_order_service.list_orders(conn, pid, "验收工单")
            check("A7 删除",
                  removed_a and removed_b and gone and len(left["orders"]) == 0,
                  "副本/原单删除 / 重新查询 %d 条" % len(left["orders"]))
    finally:
        with db.get_conn() as conn:
            for order_id in created_orders:
                db.delete_work_order(conn, pid, order_id)
            conn.execute("DELETE FROM work_order_file WHERE order_id IN"
                         " (SELECT id FROM work_order WHERE project_id = ?)", (pid,))
            conn.execute("DELETE FROM work_order WHERE project_id = ?", (pid,))
            conn.execute("DELETE FROM layout_placement WHERE project_id = ?", (pid,))
            conn.execute("DELETE FROM polyline WHERE project_id = ?", (pid,))
            conn.execute("DELETE FROM part WHERE project_id = ?", (pid,))
            conn.execute("DELETE FROM layout_sheet WHERE project_id = ?", (pid,))
            conn.execute("DELETE FROM layer WHERE project_id = ?", (pid,))
            conn.execute("DELETE FROM material WHERE project_id = ?", (pid,))
            conn.execute("DELETE FROM project WHERE id = ?", (pid,))


# ---------------------------------------------------------------- HTTP
def http_json(method: str, url: str, body: Optional[Dict[str, Any]] = None,
              timeout: float = 60.0) -> Any:
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


def http_error_code(method: str, url: str, body: Optional[Dict[str, Any]] = None) -> int:
    try:
        http_json(method, url, body)
        return 200
    except RuntimeError as exc:
        text = str(exc)
        for code in ("400", "404", "409", "422", "500"):
            if "HTTP %s" % code in text:
                return int(code)
        raise


def square(x: float, y: float, w: float, h: float) -> List[List[float]]:
    return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]


def rest_tests(base: str) -> None:
    from app.models import db

    print("== B. 接口：工单 CRUD / 打开 / 文件归档 ==")
    http_json("POST", base + "/api/demo", {})
    created_orders: List[int] = []
    created_files: List[Path] = []
    try:
        # --- B1 保存
        detail = http_json("POST", base + "/api/workorders", {
            "name": "API验收工单",
            "code": "API-001",
            "note": "接口层验收",
            "toolpath": {"cutType": "outer", "depth": 9.0, "toolDiameter": 6.0,
                         "ids": [1, 2, 3], "contours": [{"pts": [[0, 0]]}]},
            "post": {"processorId": "weihong_nc", "format": "nc",
                     "flipY": False, "speed": 25, "force": 20},
        })
        order_id = int(detail["id"])
        created_orders.append(order_id)
        check("B1 保存接口",
              order_id > 0 and detail["stats"]["partCount"] > 0
              and detail["stats"]["polylineCount"] > 0
              and "ids" not in detail["toolpath"]
              and detail["post"]["processorId"] == "weihong_nc",
              "#%d %d 零件 / %d 轮廓" % (
                  order_id, detail["stats"]["partCount"],
                  detail["stats"]["polylineCount"]))

        # --- B2 列表检索
        today = datetime.now().strftime("%Y-%m-%d")
        listing = http_json("GET", base + "/api/workorders?" + urlencode({"keyword": "API验收"}))
        by_code = http_json("GET", base + "/api/workorders?" + urlencode({"keyword": "API-001"}))
        in_day = http_json("GET", base + "/api/workorders?" + urlencode(
            {"dateFrom": today, "dateTo": today}))
        check("B2 列表检索",
              any(item["id"] == order_id for item in listing["orders"])
              and any(item["id"] == order_id for item in by_code["orders"])
              and any(item["id"] == order_id for item in in_day["orders"]),
              "关键词 %d / 编号 %d / 当日 %d" % (
                  listing["total"], by_code["total"], in_day["total"]))

        # --- B3 详情与改名
        single = http_json("GET", base + "/api/workorders/%d" % order_id)
        updated = http_json("PUT", base + "/api/workorders/%d" % order_id, {
            "name": "API验收工单-改", "code": "API-001", "note": "已改名",
            "recapture": False,
        })
        check("B3 详情与改名",
              single["id"] == order_id and single["files"] == []
              and updated["name"].endswith("-改")
              and updated["stats"]["partCount"] == single["stats"]["partCount"],
              "详情文件 %d / 改名后零件 %d" % (
                  len(single["files"]), updated["stats"]["partCount"]))

        # --- B4 打开接口
        opened = http_json("POST", base + "/api/workorders/%d/open" % order_id, {})
        with db.get_conn() as conn:
            pid = db.get_active_project_id(conn)
            restored_parts = len(db.load_parts(conn, pid))
        check("B4 打开接口",
              opened["toolpath"]["depth"] == 9.0
              and opened["post"]["processorId"] == "weihong_nc"
              and opened["missing"] == []
              and restored_parts == opened["stats"]["partCount"],
              "恢复 %d 个零件 / 刀路深度 %s" % (restored_parts, opened["toolpath"]["depth"]))

        # --- B5 导出登记
        export = http_json("POST", base + "/api/export", {
            "contours": [{"id": 1, "layerId": 1, "closed": True, "isHole": False,
                          "partId": 0, "pts": square(0, 0, 100, 60)}],
            "processorId": "weihong_nc", "name": "WO_API",
            "depth": 3.0, "passDepth": 3.0, "toolDiameter": 6.0,
            "workOrderId": order_id,
        })
        path = Path(export["path"])
        created_files.append(path)
        files = http_json("GET", base + "/api/workorders/%d/files" % order_id)
        listing = http_json("GET", base + "/api/workorders?" + urlencode({"keyword": "API验收"}))
        listed = next((item for item in listing["orders"] if item["id"] == order_id), {})
        check("B5 导出登记",
              files["total"] == 1 and files["files"][0]["path"] == str(path)
              and listed.get("fileCount") == 1,
              "%s（%d 字节）" % (path.name, path.stat().st_size))

        # --- B6 另存接口
        copy = http_json("POST", base + "/api/workorders/%d/duplicate" % order_id, {
            "name": "API验收工单-副本"})
        created_orders.append(int(copy["id"]))
        check("B6 另存接口",
              copy["id"] != order_id and copy["name"].endswith("副本")
              and copy["stats"]["partCount"] == opened["stats"]["partCount"],
              "#%d -> #%d" % (order_id, copy["id"]))

        # --- B7 参数校验
        empty_name = http_error_code("POST", base + "/api/workorders", {
            "name": "", "toolpath": {}, "post": {}})
        missing_open = http_error_code(
            "POST", base + "/api/workorders/999999/open", {})
        bad_date = http_error_code(
            "GET", base + "/api/workorders?dateFrom=2026-13-99")
        check("B7 参数校验",
              empty_name == 422 and missing_open == 404 and bad_date == 400,
              "空名称 %d / 未知工单 %d / 非法日期 %d" % (
                  empty_name, missing_open, bad_date))

        # --- B8 删除接口
        http_json("DELETE", base + "/api/workorders/%d" % copy["id"])
        http_json("DELETE", base + "/api/workorders/%d" % order_id)
        gone = http_error_code("GET", base + "/api/workorders/%d" % order_id)
        check("B8 删除接口", gone == 404, "删除后 GET -> %d" % gone)
    finally:
        for order_id in created_orders:
            try:
                http_json("DELETE", base + "/api/workorders/%d" % order_id)
            except Exception:
                pass
        with db.get_conn() as conn:
            pid = db.get_active_project_id(conn)
            for order_id in created_orders:
                db.delete_work_order(conn, pid, order_id)
        for path in created_files:
            try:
                path.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------- 入口
def main() -> int:
    parser = argparse.ArgumentParser(description="BtCAM 模块 8 工单存档验收测试")
    parser.add_argument("--url", default="", help="后端地址；给定时追加接口测试")
    args = parser.parse_args()

    from app.models import db
    db.init_db()

    try:
        service_tests()
    except Exception as exc:
        import traceback
        traceback.print_exc()
        check("服务层异常", False, str(exc))

    if args.url:
        try:
            rest_tests(args.url.rstrip("/"))
        except Exception as exc:
            import traceback
            traceback.print_exc()
            check("接口层异常", False, str(exc))

    passed = sum(1 for _, ok, _ in CHECKS if ok)
    total = len(CHECKS)
    print("\n== 结果：%d/%d 通过 ==" % (passed, total))
    failed = [name for name, ok, _ in CHECKS if not ok]
    if failed:
        print("失败项：%s" % "，".join(failed))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
