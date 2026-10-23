"""模块 5（刀路生成）控制台验收测试。

两种跑法：

    # 1) 只测服务层（不需要后端，秒级）
    backend\\.venv\\Scripts\\python.exe scripts\\toolpath_test.py

    # 2) 服务层 + 接口/WS 全链路（后端需先启动在 8975）
    backend\\.venv\\Scripts\\python.exe scripts\\toolpath_test.py --url http://127.0.0.1:8975

覆盖的验收项：

    A1  外轮廓补偿      tool=10 的外轮廓向外扩 5mm，包围盒与方向正确
    A2  内孔补偿        isHole 轮廓向内缩 5mm，不会反向吃掉零件
    A3  切割方向        cw / ccw 与刀路点序的有向面积一致
    A4  手工起割点      画布点选坐标吸附到最近轮廓，首刀从该点开始
    A5  微连接          2 个桥把闭合轮廓切成 3 段，桥宽 10mm 处抬刀跨过
    A6  路径排序        乱序输入经贪心排序后空程明显缩短
    A7  刀路类型        只切内孔 / 只切外轮廓；没有内孔时明确报错
    A8  取消刀路        进度回调抛 TaskCancelled 时立即中止
    A9  整板刀路        排料板全部零件按摆放变换后合并成一套刀路

    B1  接口提交        POST /api/toolpath -> TOOLPATH_PREVIEW（含二进制块）
    B2  参数校验        负微连接宽度、非法方向返回 422
    B3  取消接口        未知任务 404，已结束任务 409
    B4  整板接口        source=layout 读取排料板，TASK_RESULT 回带 sheet/contours

退出码：0 全部通过；1 存在失败项。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

CHECKS: List[Tuple[str, bool, str]] = []
EPS = 1e-3
DEFAULT_SHEET = {"width": 1220.0, "height": 2440.0, "margin": 10.0, "gap": 5.0}


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, ok, detail))
    print("  %s %s%s" % ("[OK]  " if ok else "[FAIL]", name, ("  " + detail) if detail else ""))
    return ok


# ---------------------------------------------------------------- 图形工具
def square(x: float, y: float, w: float, h: float) -> List[List[float]]:
    """逆时针矩形（Y 向上）。"""
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
        "optimize": True,
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
        "leadIn": False,
        "leadRadius": 2.0,
    }
    params.update(overrides)
    return params


def cut_points(plan: Any) -> List[Tuple[float, float]]:
    return [(mv.x, mv.y) for mv in plan.moves if mv.kind == "cut"]


def bbox(points: List[Tuple[float, float]]) -> Tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def signed_area(points: List[Tuple[float, float]]) -> float:
    total = 0.0
    for i, (x1, y1) in enumerate(points):
        x2, y2 = points[(i + 1) % len(points)]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def near(value: float, target: float, tol: float = 0.15) -> bool:
    return abs(float(value) - float(target)) <= tol


# ---------------------------------------------------------------- 服务层
def service_tests() -> int:
    from app.core.tasks import TaskCancelled
    from app.services.toolpath_service import build_toolpath

    print("== A. 服务层：刀路生成 ==")

    # --- A1 外轮廓补偿
    plan = build_toolpath(base_params([contour(1, square(0, 0, 100, 60))],
                                     toolDiameter=10.0))
    min_x, min_y, max_x, max_y = bbox(cut_points(plan))
    meta = plan.contours[0]
    check("A1 外轮廓补偿",
          near(min_x, -5.0) and near(min_y, -5.0)
          and near(max_x, 105.0) and near(max_y, 65.0)
          and meta["side"] == "outside" and plan.stats["outerCount"] == 1,
          "包围盒 (%.2f, %.2f) - (%.2f, %.2f) / %s" % (
              min_x, min_y, max_x, max_y, meta["side"]))

    # --- A2 内孔补偿
    hole = contour(2, square(30, 10, 40, 40), is_hole=True, part_id=2)
    plan = build_toolpath(base_params([hole], toolDiameter=10.0, cutType="inner"))
    min_x, min_y, max_x, max_y = bbox(cut_points(plan))
    check("A2 内孔补偿",
          near(min_x, 35.0) and near(min_y, 15.0)
          and near(max_x, 65.0) and near(max_y, 45.0)
          and plan.contours[0]["side"] == "inside" and plan.stats["innerCount"] == 1,
          "内孔刀路包围盒 (%.2f, %.2f) - (%.2f, %.2f)" % (
              min_x, min_y, max_x, max_y))

    # --- A3 切割方向
    ccw = build_toolpath(base_params([contour(3, square(0, 0, 40, 40))],
                                     direction="ccw"))
    cw = build_toolpath(base_params([contour(3, square(0, 0, 40, 40))],
                                    direction="cw"))
    ccw_pts = cut_points(ccw)[:-1]
    cw_pts = cut_points(cw)[:-1]
    check("A3 切割方向",
          signed_area(ccw_pts) > 0 and signed_area(cw_pts) < 0
          and ccw.contours[0]["direction"] == "ccw"
          and cw.contours[0]["direction"] == "cw",
          "CCW 面积 %.1f / CW 面积 %.1f" % (
              signed_area(ccw_pts), signed_area(cw_pts)))

    # --- A4 手工起割点
    plan = build_toolpath(base_params(
        [contour(4, square(0, 0, 100, 60))],
        startPoints=[{"x": 100.0, "y": 30.0}]))
    points = cut_points(plan)
    check("A4 手工起割点",
          near(points[0][0], 100.0, 1e-6) and near(points[0][1], 30.0, 1e-6)
          and near(points[-1][0], points[0][0], 1e-6)
          and near(points[-1][1], points[0][1], 1e-6)
          and near(plan.start_points[0]["x"], 100.0, 1e-6)
          and near(plan.start_points[0]["y"], 30.0, 1e-6),
          "首刀 (%.3f, %.3f)，末刀回到同一点" % points[0])

    # --- A5 微连接：2 个 10mm 桥 -> 3 段切削，桥处抬刀跨过
    plan = build_toolpath(base_params(
        [contour(5, square(0, 0, 100, 60))],
        microJoint=True, microJointWidth=10.0, microJointCount=2))
    gaps: List[float] = []
    last_cut: Optional[Tuple[float, float]] = None
    for mv in plan.moves:
        if mv.kind == "cut":
            last_cut = (mv.x, mv.y)
        elif mv.kind == "rapid" and last_cut is not None:
            gaps.append(math.hypot(mv.x - last_cut[0], mv.y - last_cut[1]))
            last_cut = None
    check("A5 微连接",
          plan.stats["bridgeCount"] == 2
          and plan.contours[0]["segments"] == 3
          and near(plan.stats["cutLengthMm"], 300.0, 0.5)
          and len(gaps) == 2 and all(near(gap, 10.0, 0.01) for gap in gaps),
          "切 %d 段 / 桥 %d 个 / 空走 %.1fmm" % (
              plan.contours[0]["segments"], plan.stats["bridgeCount"],
              plan.stats["rapidLengthMm"]))

    # --- A6 路径排序：故意按远-近-中顺序输入
    ordered_input = [
        contour(10, square(1000, 0, 40, 40), part_id=10),
        contour(11, square(0, 0, 40, 40), part_id=11),
        contour(12, square(500, 0, 40, 40), part_id=12),
    ]
    optimized = build_toolpath(base_params(ordered_input, optimize=True))
    naive = build_toolpath(base_params(ordered_input, optimize=False))
    check("A6 路径排序",
          optimized.stats["rapidLengthMm"] < naive.stats["rapidLengthMm"] - 100.0
          and optimized.stats["orderGainMm"] > 100.0,
          "空程 %.1f -> %.1fmm（节省 %.1fmm）" % (
              naive.stats["rapidLengthMm"], optimized.stats["rapidLengthMm"],
              optimized.stats["orderGainMm"]))

    # --- A7 刀路类型
    both = [contour(20, square(0, 0, 100, 60), part_id=20),
            contour(21, square(30, 10, 30, 30), is_hole=True, part_id=20)]
    inner = build_toolpath(base_params(both, cutType="inner"))
    outer = build_toolpath(base_params(both, cutType="outer"))
    failed = False
    try:
        build_toolpath(base_params([contour(22, square(0, 0, 40, 40))],
                                   cutType="inner"))
    except ValueError as exc:
        failed = "内孔" in str(exc)
    check("A7 刀路类型",
          inner.stats["contourCount"] == 1 and inner.stats["innerCount"] == 1
          and outer.stats["contourCount"] == 1 and outer.stats["outerCount"] == 1
          and failed,
          "内孔 %d 条 / 外轮廓 %d 条 / 无孔报错 %s" % (
              inner.stats["contourCount"], outer.stats["contourCount"], failed))

    # --- A8 取消刀路：第 5 次进度回调抛 TaskCancelled
    seen = {"n": 0}

    def cancelling_report(value: float, message: str = "") -> None:
        seen["n"] += 1
        if seen["n"] >= 5:
            raise TaskCancelled("test-cancel")

    aborted = False
    try:
        build_toolpath(base_params(
            [contour(30 + i, square(i * 60, 0, 40, 40), part_id=30 + i)
             for i in range(5)],
            depth=15.0, passDepth=3.0), cancelling_report)
    except TaskCancelled:
        aborted = True
    check("A8 取消刀路", aborted and seen["n"] == 5,
          "第 %d 次进度回调中止" % seen["n"])

    # --- A9 整板刀路：内置示例图形排料后整板合并
    from app.models import db
    from app.services import geometry_service
    from app.services.layout_service import layout_service as layout_engine

    db.init_db()
    with db.get_conn() as conn:
        project_id = db.get_active_project_id(conn)
        layout_engine.set_sheet(conn, project_id, **DEFAULT_SHEET)
    geometry_service.persist_document(project_id, geometry_service.build_demo_document())
    with db.get_conn() as conn:
        state = layout_engine.open(conn, project_id)
        polylines, _ = layout_engine.build_polylines(state)

    payload = [{"id": p.id, "layerId": p.layer_id, "closed": p.closed,
                "isHole": p.is_hole, "partId": p.part_id, "pts": p.pts}
               for p in polylines]
    plan = build_toolpath(base_params(payload, depth=18.0, passDepth=6.0,
                                      leadIn=True, optimize=True))
    starts_inside = all(-1.0 <= row["x"] <= DEFAULT_SHEET["width"] + 1.0
                        and -1.0 <= row["y"] <= DEFAULT_SHEET["height"] + 1.0
                        for row in plan.start_points)
    check("A9 整板刀路",
          plan.stats["contourCount"] == len(polylines)
          and plan.stats["outerCount"] >= 1 and starts_inside
          and plan.stats["cutLengthMm"] > 0,
          "%d 条轮廓 / 外 %d / 内 %d / 切削 %.0fmm" % (
              plan.stats["contourCount"], plan.stats["outerCount"],
              plan.stats["innerCount"], plan.stats["cutLengthMm"]))
    return project_id


# ---------------------------------------------------------------- 接口层
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
    http_json("POST", base + "/api/demo", {})

    async with websockets.connect(ws_url, max_size=256 * 1024 * 1024) as sock:
        assembler = protocol.ChunkAssembler()
        await sock.send(protocol.encode_frame(MsgType.HELLO, {"clientVersion": "toolpath-test"}))
        await wait_frame(sock, assembler, MsgType.HELLO_ACK)

        # --- B1 接口提交：接口层回带三层预览（切削/快移/起割点）
        accepted = http_json("POST", base + "/api/toolpath", {
            "depth": 6.0, "passDepth": 3.0, "toolDiameter": 6.0,
            "microJoint": True, "microJointWidth": 3.0, "microJointCount": 1,
        })
        meta, payload = await wait_frame(sock, assembler, MsgType.TOOLPATH_PREVIEW)
        preview = protocol.decode_polylines(payload)
        layers = {int(row["id"]): row for row in meta.get("layers", [])}
        stats = meta.get("stats") or {}
        check("B1 接口提交",
              meta.get("kind") == "toolpath" and len(preview) > 0
              and stats.get("bridgeCount", 0) > 0
              and layers.get(9002, {}).get("dashed") is True
              and 9003 in layers and len(meta.get("contours", [])) > 0,
              "%s · %d 段预览 / 桥 %d / 切削 %.0fmm" % (
                  accepted["taskId"], len(preview), stats.get("bridgeCount", 0),
                  float(stats.get("cutLengthMm", 0))))

        # --- B2 参数校验
        rejected_width = rejected_direction = False
        try:
            http_json("POST", base + "/api/toolpath",
                      {"microJointWidth": -1.0})
        except RuntimeError as exc:
            rejected_width = "422" in str(exc)
        try:
            http_json("POST", base + "/api/toolpath", {"direction": "diagonal"})
        except RuntimeError as exc:
            rejected_direction = "422" in str(exc)
        check("B2 参数校验", rejected_width and rejected_direction,
              "负微连接宽度 / 非法方向均被拒绝")

        # --- B3 取消接口：未知 404，已结束 409
        not_found = conflict = False
        try:
            http_json("POST", base + "/api/tasks/no-such-task/cancel", {})
        except RuntimeError as exc:
            not_found = "404" in str(exc)
        try:
            http_json("POST", base + "/api/tasks/%s/cancel" % accepted["taskId"], {})
        except RuntimeError as exc:
            conflict = "409" in str(exc)
        check("B3 取消接口", not_found and conflict,
              "未知任务 404 / 已结束任务 409")

        # --- B4 整板接口：source=layout 直接读排料板
        http_json("POST", base + "/api/layout/open", {})
        accepted = http_json("POST", base + "/api/toolpath", {
            "source": "layout", "depth": 6.0, "passDepth": 3.0,
            "toolDiameter": 6.0,
        })
        meta, payload = await wait_frame(sock, assembler, MsgType.TOOLPATH_PREVIEW)
        stats = meta.get("stats") or {}
        check("B4 整板接口",
              meta.get("source") == "layout" and stats.get("contourCount", 0) >= 5
              and isinstance(meta.get("sheet"), dict)
              and float(meta["sheet"].get("width", 0)) > 0
              and len(protocol.decode_polylines(payload)) > 0,
              "%s · %d 条轮廓 / 板材 %.0fx%.0f" % (
                  accepted["taskId"], stats.get("contourCount", 0),
                  float((meta.get("sheet") or {}).get("width", 0)),
                  float((meta.get("sheet") or {}).get("height", 0))))


# ---------------------------------------------------------------- 入口
def main() -> int:
    parser = argparse.ArgumentParser(description="BtCAM 模块 5 刀路生成验收测试")
    parser.add_argument("--url", default="", help="后端地址；给定时追加接口/WS 测试")
    args = parser.parse_args()

    try:
        service_tests()
    except Exception as exc:              # 服务层异常直接判整轮失败
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
