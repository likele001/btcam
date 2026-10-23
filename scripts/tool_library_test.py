"""模块 6（刀具库与工艺模板）控制台验收测试。

两种跑法：

    # 1) 只测服务层（不需要后端，秒级）
    backend\\.venv\\Scripts\\python.exe scripts\\tool_library_test.py

    # 2) 服务层 + 接口/WS 全链路（后端需先启动在 8975）
    backend\\.venv\\Scripts\\python.exe scripts\\tool_library_test.py --url http://127.0.0.1:8975

覆盖的验收项：

    A1  铣刀换算      直径即补偿直径；非法直径/刀尖圆角被拒绝
    A2  钼丝换算      等效直径 = 丝径 + 2×放电间隙；主轴归零
    A3  模板 CRUD     参数白名单（未知字段被丢弃）、绑定不存在的刀具报错
    A4  参数优先级    刀具 > 模板 > 请求默认值；显式 toolId 覆盖模板刀具
    A5  顺逆铣映射    顺铣外 CCW / 内 CW；逆铣相反（按真实点序回报）
    A6  刀具驱动补偿  刀路自动读取刀具等效直径，包围盒按半径扩大
    A7  线切割补偿    钼丝按“丝径+2×间隙”做偏置，逆铣外轮廓为 CW
    A8  抬刀高度      retract 落到 liftZ，快移平面仍用 safeZ
    A9  模板微连接    模板参数驱动桥接数量与分段数
    A10 删除保护      被模板引用的刀具拒绝删除
    A11 未知引用      不存在的 toolId/templateId 明确报错

    B1  刀具接口      GET/POST/PUT/DELETE + 驼峰字段 + effectiveDiameter
    B2  模板接口      模板列表内嵌刀具详情，增删改查闭环
    B3  刀路读刀具    POST /api/toolpath 带 toolId -> meta.tool + 补偿生效
    B4  模板一键加载  POST /api/toolpath 带 templateId -> 补偿/微连接按模板
    B5  导出读刀具    POST /api/export 的进给/安全高度来自刀具
    B6  参数校验      不存在的刀具 -> 400
    B7  删除保护      接口层删除被引用刀具 -> 400

退出码：0 全部通过；1 存在失败项。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

CHECKS: List[Tuple[str, bool, str]] = []
EPS = 0.02


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, ok, detail))
    print("  %s %s%s" % ("[OK]  " if ok else "[FAIL]", name, ("  " + detail) if detail else ""))
    return ok


# ---------------------------------------------------------------- 图形工具
def square(x: float, y: float, w: float, h: float) -> List[List[float]]:
    return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]


def contour(contour_id: int, pts: List[List[float]], closed: bool = True,
            is_hole: bool = False, part_id: int = 0) -> Dict[str, Any]:
    return {"id": contour_id, "layerId": 1, "closed": closed,
            "isHole": is_hole, "partId": part_id, "pts": pts}


def base_params(contours: List[Dict[str, Any]], **overrides: Any) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "contours": contours,
        "cutType": "auto",
        "direction": "ccw",
        "optimize": False,
        "startPoints": [],
        "microJoint": False,
        "microJointWidth": 2.0,
        "microJointCount": 2,
        "toolDiameter": 0.0,
        "side": "auto",
        "depth": 3.0,
        "passDepth": 3.0,
        "feed": 3000.0,
        "plungeFeed": 800.0,
        "safeZ": 5.0,
        "liftZ": 5.0,
        "spindle": 18000.0,
        "leadIn": False,
        "leadRadius": 2.0,
    }
    params.update(overrides)
    return params


def bbox(points: List[Tuple[float, float]]) -> Tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def near(value: float, target: float, tol: float = 0.02) -> bool:
    return abs(float(value) - float(target)) <= tol


def plan_cut_points(plan: Any) -> List[Tuple[float, float]]:
    return [(mv.x, mv.y) for mv in plan.moves if mv.kind == "cut"]


def tool_name(tag: str) -> str:
    return "TEST6-%s-%d" % (tag, int(time.time() * 1000) % 1000000)


# ---------------------------------------------------------------- 服务层
def service_tests() -> None:
    from app.models import db
    from app.services import tool_service as ts
    from app.services.toolpath_service import build_toolpath

    print("== A. 服务层：刀具库与工艺模板 ==")

    db.init_db()
    cleanup: List[Tuple[str, int]] = []

    def drop(kind: str, ident: int) -> None:
        try:
            with db.get_conn() as conn:
                if kind == "template":
                    ts.remove_template(conn, project_id, ident)
                else:
                    ts.remove_tool(conn, project_id, ident)
        except Exception:
            pass

    with db.get_conn() as conn:
        project_id = db.get_active_project_id(conn)

    try:
        # --- A1 铣刀换算与校验
        with db.get_conn() as conn:
            mill = ts.create_tool(conn, project_id, {
                "name": tool_name("mill"), "category": "mill", "toolType": "flat",
                "diameter": 10.0, "tipRadius": 1.0, "feed": 1234.0,
                "plungeFeed": 456.0, "spindle": 15000.0, "passDepth": 2.5,
                "safeZ": 7.0, "liftZ": 2.0, "direction": "climb", "note": "验收",
            })
        cleanup.append(("tool", int(mill["id"])))
        invalid_diameter = invalid_tip = False
        try:
            with db.get_conn() as conn:
                ts.create_tool(conn, project_id, {
                    "name": tool_name("bad"), "category": "mill", "diameter": 0.0,
                })
        except ts.ToolError:
            invalid_diameter = True
        try:
            with db.get_conn() as conn:
                ts.create_tool(conn, project_id, {
                    "name": tool_name("bad"), "category": "mill",
                    "diameter": 6.0, "tipRadius": 4.0,
                })
        except ts.ToolError:
            invalid_tip = True
        check("A1 铣刀换算",
              near(mill["effectiveDiameter"], 10.0) and near(mill["compensationRadius"], 5.0)
              and invalid_diameter and invalid_tip,
              "等效 ⌀%.2f / R%.2f；非法直径与刀尖圆角被拒绝" % (
                  mill["effectiveDiameter"], mill["compensationRadius"]))

        # --- A2 钼丝换算
        with db.get_conn() as conn:
            wire = ts.create_tool(conn, project_id, {
                "name": tool_name("wire"), "category": "wire", "toolType": "flat",
                "wireDiameter": 0.2, "dischargeGap": 0.03, "feed": 800.0,
                "plungeFeed": 200.0, "spindle": 18000.0, "passDepth": 18.0,
                "safeZ": 5.0, "liftZ": 5.0, "direction": "conventional",
            })
        cleanup.append(("tool", int(wire["id"])))
        check("A2 钼丝换算",
              near(wire["effectiveDiameter"], 0.26, 0.001)
              and near(wire["compensationRadius"], 0.13, 0.001)
              and wire["spindle"] == 0.0 and wire["diameter"] == 0.0,
              "丝径 0.2 + 间隙 0.03 -> 等效 ⌀%.3f / R%.3f / 主轴 %.0f" % (
                  wire["effectiveDiameter"], wire["compensationRadius"], wire["spindle"]))

        # --- A3 模板 CRUD / 白名单 / 非法刀具
        with db.get_conn() as conn:
            template = ts.create_template(conn, project_id, {
                "name": tool_name("tpl"), "material": "密度板",
                "toolId": mill["id"],
                "params": {
                    "cutType": "inner", "side": "inside", "optimize": False,
                    "microJoint": True, "microJointWidth": 3.0, "microJointCount": 2,
                    "leadIn": False, "leadRadius": 0.0,
                    "hacked": 123,          # 不在白名单，应被丢弃
                },
                "note": "验收模板",
            })
        cleanup.append(("template", int(template["id"])))
        with db.get_conn() as conn:
            roundtrip = ts.get_template(conn, project_id, template["id"])
        keys = set(roundtrip["params"].keys()) if roundtrip else set()
        bad_tool = False
        try:
            with db.get_conn() as conn:
                ts.create_template(conn, project_id, {
                    "name": tool_name("bad-tpl"), "toolId": 99999999,
                    "params": {},
                })
        except ts.ToolError:
            bad_tool = True
        check("A3 模板 CRUD",
              roundtrip is not None and "hacked" not in keys and len(keys) == 8
              and roundtrip["tool"]["id"] == mill["id"] and bad_tool,
              "参数白名单 %d 项；非法 toolId 被拒绝" % len(keys))

        with db.get_conn() as conn:
            edited = ts.edit_template(conn, project_id, template["id"], {
                "name": roundtrip["name"] + "-改", "material": "多层板",
                "toolId": mill["id"], "params": roundtrip["params"], "note": "已修改",
            })
        check("A3b 模板修改",
              edited["material"] == "多层板" and edited["name"].endswith("-改"),
              "名称/物料已更新")

        # --- A4 参数优先级：显式刀具覆盖模板刀具，模板覆盖请求
        with db.get_conn() as conn:
            mill_b = ts.create_tool(conn, project_id, {
                "name": tool_name("mill-b"), "category": "mill", "toolType": "ball",
                "diameter": 5.0, "tipRadius": 2.5, "feed": 2222.0,
                "plungeFeed": 333.0, "spindle": 12000.0, "passDepth": 1.5,
                "safeZ": 8.0, "liftZ": 3.0, "direction": "conventional",
            })
        cleanup.append(("tool", int(mill_b["id"])))
        with db.get_conn() as conn:
            resolved = ts.resolve(conn, project_id, mill_b["id"], template["id"])
        merged = ts.apply_resolution({
            "toolDiameter": 99.0, "feed": 1.0, "cutType": "outer",
            "side": "outside", "safeZ": 1.0, "direction": "ccw",
        }, resolved)
        check("A4 参数优先级",
              near(merged["toolDiameter"], 5.0) and near(merged["feed"], 2222.0)
              and merged["cutType"] == "inner" and merged["side"] == "inside"
              and merged["direction"] == "conventional"
              and merged["tool"]["id"] == mill_b["id"]
              and merged["template"]["id"] == template["id"],
              "刀具 ⌀%.1f / feed %.0f；模板 cutType=%s side=%s" % (
                  merged["toolDiameter"], merged["feed"],
                  merged["cutType"], merged["side"]))

        # --- A5 顺逆铣映射
        climb_outer = ts.direction_for("climb", False)
        climb_inner = ts.direction_for("climb", True)
        conv_outer = ts.direction_for("conventional", False)
        conv_inner = ts.direction_for("conventional", True)
        outline = contour(1, square(0, 0, 100, 60))
        hole = contour(2, square(30, 10, 40, 40), is_hole=True, part_id=1)
        plan_climb = build_toolpath(base_params(
            [outline, hole], direction="climb", toolDiameter=0.0))
        plan_conv = build_toolpath(base_params(
            [outline, hole], direction="conventional", toolDiameter=0.0))
        by_id = {row["id"]: row for row in plan_climb.contours}
        by_id_conv = {row["id"]: row for row in plan_conv.contours}
        check("A5 顺逆铣映射",
              climb_outer is True and climb_inner is False
              and conv_outer is False and conv_inner is True
              and by_id[1]["direction"] == "ccw" and by_id[2]["direction"] == "cw"
              and by_id_conv[1]["direction"] == "cw"
              and by_id_conv[2]["direction"] == "ccw",
              "顺铣 外%s/内%s；逆铣 外%s/内%s" % (
                  by_id[1]["direction"], by_id[2]["direction"],
                  by_id_conv[1]["direction"], by_id_conv[2]["direction"]))

        # --- A6 刀具驱动补偿：10mm 刀 -> 外轮廓外扩 5mm
        with db.get_conn() as conn:
            resolved = ts.resolve(conn, project_id, mill["id"], None)
        params = ts.apply_resolution(
            base_params([contour(1, square(0, 0, 100, 60))], toolDiameter=1.0),
            resolved)
        plan = build_toolpath(params)
        min_x, min_y, max_x, max_y = bbox(plan_cut_points(plan))
        check("A6 刀具驱动补偿",
              near(min_x, -5.0) and near(min_y, -5.0)
              and near(max_x, 105.0) and near(max_y, 65.0)
              and plan.contours[0]["side"] == "outside",
              "包围盒 (%.2f, %.2f) - (%.2f, %.2f) / %s" % (
                  min_x, min_y, max_x, max_y, plan.contours[0]["side"]))

        # --- A7 线切割补偿：0.26mm 等效直径 -> 外扩 0.13mm，逆铣为 CW
        with db.get_conn() as conn:
            resolved = ts.resolve(conn, project_id, wire["id"], None)
        plan = build_toolpath(ts.apply_resolution(
            base_params([contour(1, square(0, 0, 100, 60))]), resolved))
        min_x, min_y, max_x, max_y = bbox(plan_cut_points(plan))
        check("A7 线切割补偿",
              near(min_x, -0.13, 0.01) and near(max_x, 100.13, 0.01)
              and near(min_y, -0.13, 0.01) and near(max_y, 60.13, 0.01)
              and plan.contours[0]["direction"] == "cw",
              "包围盒 (%.3f, %.3f) - (%.3f, %.3f) / %s" % (
                  min_x, min_y, max_x, max_y, plan.contours[0]["direction"]))

        # --- A8 liftZ / safeZ 分离
        plan = build_toolpath(base_params(
            [contour(1, square(0, 0, 100, 60))], toolDiameter=6.0,
            safeZ=8.0, liftZ=2.0, depth=3.0, passDepth=3.0))
        retract_z = {round(mv.z, 3) for mv in plan.moves if mv.kind == "retract"}
        rapid_z = {round(mv.z, 3) for mv in plan.moves if mv.kind == "rapid"}
        cut_z = {round(mv.z, 3) for mv in plan.moves if mv.kind in ("cut", "plunge")}
        check("A8 抬刀高度",
              retract_z == {2.0} and rapid_z == {8.0} and cut_z == {-3.0},
              "retract Z=%s / rapid Z=%s / cut Z=%s" % (
                  sorted(retract_z), sorted(rapid_z), sorted(cut_z)))

        # --- A9 模板微连接驱动：模板里存 3 个桥，加载后无需再传微连接参数
        with db.get_conn() as conn:
            bridge_tpl = ts.create_template(conn, project_id, {
                "name": tool_name("bridge-tpl"), "material": "密度板",
                "toolId": mill["id"],
                "params": {
                    "cutType": "auto", "side": "auto", "optimize": True,
                    "microJoint": True, "microJointWidth": 2.0, "microJointCount": 3,
                    "leadIn": False, "leadRadius": 0.0,
                },
            })
        cleanup.append(("template", int(bridge_tpl["id"])))
        with db.get_conn() as conn:
            resolved = ts.resolve(conn, project_id, None, bridge_tpl["id"])
        params = ts.apply_resolution(
            base_params([contour(1, square(0, 0, 100, 60))], depth=3.0, passDepth=3.0),
            resolved)
        plan = build_toolpath(params)
        check("A9 模板微连接",
              plan.stats["bridgeCount"] == 3 and plan.contours[0]["segments"] == 4,
              "桥 %d / 分段 %d（模板 3 个桥）" % (
                  plan.stats["bridgeCount"], plan.contours[0]["segments"]))

        # --- A10 删除保护
        with db.get_conn() as conn:
            guarded_tool = ts.create_tool(conn, project_id, {
                "name": tool_name("guarded"), "category": "mill", "diameter": 4.0,
            })
        cleanup.append(("tool", int(guarded_tool["id"])))
        with db.get_conn() as conn:
            guarded_tpl = ts.create_template(conn, project_id, {
                "name": tool_name("guarded-tpl"), "toolId": guarded_tool["id"],
                "params": {},
            })
        cleanup.append(("template", int(guarded_tpl["id"])))
        refused = False
        try:
            with db.get_conn() as conn:
                ts.remove_tool(conn, project_id, guarded_tool["id"])
        except ts.ToolError as exc:
            refused = "模板" in str(exc)
        with db.get_conn() as conn:
            ts.remove_template(conn, project_id, guarded_tpl["id"])
        cleanup.remove(("template", int(guarded_tpl["id"])))
        with db.get_conn() as conn:
            ts.remove_tool(conn, project_id, guarded_tool["id"])
        cleanup.remove(("tool", int(guarded_tool["id"])))
        check("A10 删除保护", refused, "被模板引用的刀具删除被拒绝，解绑后可删")

        # --- A11 未知引用
        unknown_tool = unknown_tpl = False
        try:
            with db.get_conn() as conn:
                ts.resolve(conn, project_id, 99999999, None)
        except ts.ToolError:
            unknown_tool = True
        try:
            with db.get_conn() as conn:
                ts.resolve(conn, project_id, None, 99999999)
        except ts.ToolError:
            unknown_tpl = True
        check("A11 未知引用", unknown_tool and unknown_tpl,
              "不存在的刀具/模板都明确报错")

    finally:
        # 逆序清理：先模板后刀具
        for kind, ident in reversed(cleanup):
            drop(kind, ident)


# ---------------------------------------------------------------- HTTP / WS
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


def split_frame(raw: bytes) -> Tuple[int, int, Dict[str, Any], bytes]:
    from app.core import protocol

    msg_type, seq, meta_len, pay_len, total = protocol.decode_header(raw)
    if len(raw) < total:
        raise RuntimeError("帧被截断")
    meta = protocol.decode_meta(raw[protocol.HEADER_SIZE: protocol.HEADER_SIZE + meta_len])
    payload = raw[protocol.HEADER_SIZE + meta_len: total]
    return msg_type, seq, meta, payload


async def wait_frame(sock: Any, assembler: Any, want: int,
                     timeout: float = 90.0) -> Tuple[Dict[str, Any], bytes]:
    from app.core.protocol import MsgType

    deadline = time.monotonic() + timeout
    while True:
        remain = deadline - time.monotonic()
        if remain <= 0:
            raise TimeoutError("等待帧 0x%02X 超时" % want)
        raw = await asyncio.wait_for(sock.recv(), timeout=remain)
        if isinstance(raw, str):
            raise RuntimeError("服务端返回文本帧")
        msg_type, seq, meta, payload = split_frame(raw)
        logical = assembler.feed(msg_type, meta, payload, seq)
        if logical is None:
            continue
        msg_type, meta, payload, _seq = logical
        if msg_type == MsgType.ERROR:
            raise RuntimeError("后端错误: %s" % meta.get("message", meta))
        if msg_type == want:
            return meta, payload


async def rest_tests(base: str) -> None:
    import websockets

    from app.core import protocol
    from app.core.protocol import MsgType

    ws_url = base.replace("https://", "wss://").replace("http://", "ws://").rstrip("/") + "/ws"
    print("== B. 接口 / WS 广播 ==")

    tool = http_json("POST", base + "/api/tools", {
        "name": tool_name("api"), "category": "mill", "toolType": "flat",
        "diameter": 10.0, "tipRadius": 1.0, "feed": 1234.0, "plungeFeed": 456.0,
        "spindle": 15000.0, "passDepth": 2.5, "safeZ": 7.0, "liftZ": 2.0,
        "direction": "climb", "note": "接口验收",
    })
    template: Optional[Dict[str, Any]] = None
    exported: Optional[Path] = None
    try:
        # --- B1 刀具接口：更新 + 单条读取 + 列表驼峰字段
        updated = http_json("PUT", base + "/api/tools/%d" % tool["id"], {
            "name": tool["name"] + "-改", "category": "mill", "toolType": "flat",
            "diameter": 10.0, "tipRadius": 1.0, "feed": 1234.0, "plungeFeed": 456.0,
            "spindle": 15000.0, "passDepth": 2.5, "safeZ": 7.0, "liftZ": 2.0,
            "direction": "climb", "note": "接口验收-已改",
        })
        single = http_json("GET", base + "/api/tools/%d" % tool["id"])
        listing = http_json("GET", base + "/api/tools")
        listed = next((row for row in listing["tools"] if row["id"] == tool["id"]), None)
        check("B1 刀具接口",
              updated["name"].endswith("-改") and single["id"] == tool["id"]
              and listed is not None and near(listed["effectiveDiameter"], 10.0)
              and "toolType" in listed and "plungeFeed" in listed,
              "更新/读取/列表字段齐全，等效 ⌀%.1f" % listed["effectiveDiameter"])
        tool = updated

        # --- B2 模板接口：创建 + 列表内嵌刀具 + 修改
        template = http_json("POST", base + "/api/templates", {
            "name": tool_name("api-tpl"), "material": "密度板",
            "toolId": tool["id"],
            "params": {
                "cutType": "auto", "side": "auto", "optimize": False,
                "microJoint": True, "microJointWidth": 3.0, "microJointCount": 2,
                "leadIn": False, "leadRadius": 0.0,
            },
        })
        tpl_list = http_json("GET", base + "/api/templates")
        tpl_listed = next(
            (row for row in tpl_list["templates"] if row["id"] == template["id"]), None)
        template = http_json("PUT", base + "/api/templates/%d" % template["id"], {
            "name": template["name"] + "-改", "material": "多层板",
            "toolId": tool["id"], "params": template["params"], "note": "已改",
        })
        check("B2 模板接口",
              tpl_listed is not None and tpl_listed["tool"]["id"] == tool["id"]
              and template["material"] == "多层板"
              and template["params"]["microJoint"] is True,
              "模板内嵌刀具 %s；修改后物料 %s" % (
                  tpl_listed["tool"]["name"], template["material"]))

        # 显式轮廓：100x60 方框，后面用它验证补偿/微连接/导出
        square_contour = [contour(1, square(0, 0, 100, 60))]

        async with websockets.connect(ws_url, max_size=256 * 1024 * 1024) as sock:
            assembler = protocol.ChunkAssembler()
            await sock.send(protocol.encode_frame(
                MsgType.HELLO, {"clientVersion": "tool-library-test"}))
            await wait_frame(sock, assembler, MsgType.HELLO_ACK)

            # --- B3 刀路读刀具：10mm 刀 -> 周长 360，meta 回带 tool
            # depth 与刀具 passDepth(2.5) 一致，只切一层，切削长度即轮廓周长
            accepted = http_json("POST", base + "/api/toolpath", {
                "contours": square_contour, "toolId": tool["id"],
                "depth": 2.5, "passDepth": 2.5, "leadIn": False,
                "toolDiameter": 1.0, "feed": 1.0,
            })
            meta, payload = await wait_frame(sock, assembler, MsgType.TOOLPATH_PREVIEW)
            stats = meta.get("stats") or {}
            meta_tool = meta.get("tool") or {}
            # 外扩 5mm 后 110x70，四角圆弧过渡：2*(110+70) - 4*(10 - 5π/2) ≈ 351.4
            check("B3 刀路读刀具",
                  meta_tool.get("id") == tool["id"]
                  and near(float(stats.get("cutLengthMm", 0)), 351.4, 0.5)
                  and meta["contours"][0]["direction"] == "ccw"
                  and len(protocol.decode_polylines(payload)) > 0,
                  "%s · 切削 %.1fmm（10mm 刀外扩 5mm）· 工具 %s" % (
                      accepted["taskId"], float(stats.get("cutLengthMm", 0)),
                      meta_tool.get("name")))

            # --- B4 模板一键加载：微连接/方向按模板
            accepted = http_json("POST", base + "/api/toolpath", {
                "contours": square_contour, "templateId": template["id"],
                "depth": 3.0, "passDepth": 3.0,
            })
            meta, payload = await wait_frame(sock, assembler, MsgType.TOOLPATH_PREVIEW)
            stats = meta.get("stats") or {}
            meta_tpl = meta.get("template") or {}
            check("B4 模板一键加载",
                  meta_tpl.get("id") == template["id"]
                  and (meta.get("tool") or {}).get("id") == tool["id"]
                  and stats.get("bridgeCount") == 2
                  and meta["contours"][0]["segments"] == 3,
                  "%s · 桥 %d / 分段 %d" % (
                      accepted["taskId"], stats.get("bridgeCount", 0),
                      meta["contours"][0]["segments"]))

            # --- B5 导出读刀具：进给 F1234 / 安全高度 Z7 来自刀具
            result = http_json("POST", base + "/api/export", {
                "contours": square_contour, "toolId": tool["id"],
                "depth": 3.0, "passDepth": 3.0, "leadIn": False,
                "format": "nc", "name": "TEST6",
            })
            exported = Path(result["path"])
            content = exported.read_text(encoding="utf-8", errors="replace")
            check("B5 导出读刀具",
                  "F1234" in content and "G0 Z7" in content
                  and result.get("tool", {}).get("id") == tool["id"],
                  "文件 %s 使用刀具进给 1234 / 安全高度 7" % exported.name)

        # --- B6 不存在的刀具 -> 400
        code = http_error_code("POST", base + "/api/toolpath", {
            "contours": square_contour, "toolId": 99999999,
        })
        check("B6 参数校验", code == 400, "不存在的 toolId -> HTTP %d" % code)

        # --- B7 删除保护：模板仍引用刀具 -> 400；解绑后可删
        guarded = http_error_code("DELETE", base + "/api/tools/%d" % tool["id"])
        http_json("DELETE", base + "/api/templates/%d" % template["id"])
        template = None
        deleted = http_error_code("DELETE", base + "/api/tools/%d" % tool["id"])
        tool = None
        check("B7 删除保护", guarded == 400 and deleted == 200,
              "被引用 -> %d；解绑后 -> %d" % (guarded, deleted))

    finally:
        if exported is not None:
            try:
                exported.unlink()
            except OSError:
                pass
        if template is not None:
            http_error_code("DELETE", base + "/api/templates/%d" % template["id"])
        if tool is not None:
            http_error_code("DELETE", base + "/api/tools/%d" % tool["id"])


# ---------------------------------------------------------------- 入口
def main() -> int:
    parser = argparse.ArgumentParser(description="BtCAM 模块 6 刀具库验收测试")
    parser.add_argument("--url", default="", help="后端地址；给定时追加接口/WS 测试")
    args = parser.parse_args()

    try:
        service_tests()
    except Exception as exc:
        import traceback
        traceback.print_exc()
        check("服务层异常", False, str(exc))

    if args.url:
        try:
            asyncio.run(rest_tests(args.url.rstrip("/")))
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
