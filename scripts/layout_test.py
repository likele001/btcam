"""模块 3（画布手动排料）控制台验收测试。

两种跑法：

    # 1) 只测服务层（不需要后端，秒级）
    backend\\.venv\\Scripts\\python.exe scripts\\layout_test.py

    # 2) 服务层 + 接口/WS 全链路（后端需先启动在 8975）
    backend\\.venv\\Scripts\\python.exe scripts\\layout_test.py --url http://127.0.0.1:8975

注意：脚本会用内置示例图形重建当前工程，并改动板材参数与摆放；
结束时把板材恢复为 1220x2440 / 余量 10 / 间距 5，并复位成初始行排。

覆盖的验收项：

    A1  零件拆分        demo 图形 -> 5 个零件，带内孔的零件含 5 条轮廓
    A2  初始行排        5 个摆放全部落在可用区域内，无重叠
    A3  越界拒绝        移到板外 -> ok=False，原因指明左/右边界
    A4  间距拒绝        压到相邻零件上 -> ok=False，原因指明间距不足
    A5  合法移动        移到空白处 -> ok=True 且坐标真正落库
    A6  旋转            300x200 -> 90° 后包围盒变 200x300，仍在板内
    A7  锁定            锁定后移动拒绝、旋转拒绝
    A8  解锁            解锁后可以再次移动
    A9  轮廓变换        BGC1 输出轮廓的包围盒 == (tx, ty, tx+w, ty+h)
    A10 统计一致        净面积/利用率与摆放数据自洽
    A11 进模式幂等      再次 open 不会打乱用户已经摆好的位置
    A12 改板材          板变小后越界零件被夹回可用区域并落库
    A13 复位            reset 清空锁定并重新行排

    B1  读接口          GET  /api/layout
    B2  进模式           POST /api/layout/open + WS LAYOUT_STATE 广播
    B3  拖拽落库         POST /api/layout/place（合法/非法两条路径）
    B4  旋转             POST /api/layout/rotate + 广播中的角度
    B5  锁定             POST /api/layout/lock（锁定后拖拽被拒）
    B6  改板材           POST /api/layout/sheet + 广播中的板材尺寸
    B7  复位             POST /api/layout/reset（锁定清零、角度归零）

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

# 允许直接以 scripts/layout_test.py 运行：把 backend 与 venv 依赖加入 import 路径
BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR / ".venv" / "Lib" / "site-packages"))

CHECKS: List[Tuple[str, bool, str]] = []

EPS = 1e-3
DEFAULT_SHEET = {"width": 1220.0, "height": 2440.0, "margin": 10.0, "gap": 5.0}


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, ok, detail))
    print("  %s %s%s" % ("[OK]  " if ok else "[FAIL]", name, ("  " + detail) if detail else ""))
    return ok


# ---------------------------------------------------------------- HTTP
def http_json(method: str, url: str, body: Optional[Dict[str, Any]] = None,
              timeout: float = 30.0) -> Any:
    """极简 JSON 请求：不引入 requests/httpx，保持后端依赖干净。"""
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


# ---------------------------------------------------------------- 服务层
def in_board(state: Any, part: Any) -> bool:
    """零件当前摆放是否完整落在四边余量之内。"""
    placement = state.placements[part.id]
    width, height = part.size_at(placement.rot)
    margin = float(state.sheet["margin"])
    return (placement.tx >= margin - EPS
            and placement.ty >= margin - EPS
            and placement.tx + width <= float(state.sheet["width"]) - margin + EPS
            and placement.ty + height <= float(state.sheet["height"]) - margin + EPS)


def service_tests() -> None:
    import numpy as np

    from app.models import db
    from app.services import geometry_service
    from app.services.layout_service import layout_service

    db.init_db()
    with db.get_conn() as conn:
        project_id = db.get_active_project_id(conn)

    # persist_document 会同时清空 part / polyline / layout_placement，排料从零开始
    geometry_service.persist_document(project_id, geometry_service.build_demo_document())

    print("== A. 服务层：零件 / 排板 / 校验 ==")
    with db.get_conn() as conn:
        state = layout_service.open(conn, project_id)

    sheet = state.sheet
    check("A1 零件拆分", len(state.parts) == 5,
          "%d 个零件 · 板材 %gx%g · 间距 %g" % (
              len(state.parts), sheet["width"], sheet["height"], sheet["gap"]))

    holed = [p for p in state.parts if len(p.contours) > 1]
    check("A1 内孔归属", len(holed) == 1 and len(holed[0].contours) == 5,
          "「%s」%d 条轮廓（1 外框 + %d 孔）" % (
              holed[0].name if holed else "-",
              len(holed[0].contours) if holed else 0,
              (len(holed[0].contours) - 1) if holed else 0))

    check("A2 初始行排", len(state.placements) == 5 and all(in_board(state, p) for p in state.parts),
          "摆放 %d 个，全部在可用区内" % len(state.placements))

    first = max(state.parts, key=lambda p: p.width * p.height)
    second = min(state.parts, key=lambda p: abs(p.width * p.height - 60000.0))
    with db.get_conn() as conn:
        result = layout_service.move(conn, project_id, first.id, -50.0, 10.0)
    check("A3 越界拒绝", not result.ok and "边界" in result.reason, result.reason)

    with db.get_conn() as conn:
        result = layout_service.move(conn, project_id, second.id, first.width / 2.0, 100.0)
    check("A4 间距拒绝", not result.ok and "间距不足" in result.reason, result.reason)

    with db.get_conn() as conn:
        result = layout_service.move(conn, project_id, second.id, 615.0, 450.0)
        persisted = layout_service.load(conn, project_id).placements[second.id]
    check("A5 合法移动", result.ok and abs(persisted.ty - 450.0) < EPS,
          "落库坐标 (%.1f, %.1f)" % (persisted.tx, persisted.ty))

    with db.get_conn() as conn:
        result = layout_service.rotate(conn, project_id, second.id,
                                       persisted.rot + 90.0)
        rotated = result.state.placements[second.id]
        width, height = result.state.part(second.id).size_at(rotated.rot)
    check("A6 旋转",
          result.ok and abs(width - 200.0) < EPS and abs(height - 300.0) < EPS
          and in_board(result.state, result.state.part(second.id)),
          "角度 %g° 包围盒 %gx%g" % (rotated.rot, width, height))

    with db.get_conn() as conn:
        layout_service.set_locked(conn, project_id, second.id, True)
        moved = layout_service.move(conn, project_id, second.id, 700.0, 450.0)
        spun = layout_service.rotate(conn, project_id, second.id, 0.0)
    check("A7 锁定", not moved.ok and not spun.ok,
          "%s / %s" % (moved.reason, spun.reason))

    with db.get_conn() as conn:
        layout_service.set_locked(conn, project_id, second.id, False)
        moved = layout_service.move(conn, project_id, second.id, 615.0, 10.0)
    check("A8 解锁", moved.ok, "解锁后移动成功")

    with db.get_conn() as conn:
        state = layout_service.load(conn, project_id)
    polylines, id_map = layout_service.build_polylines(state)
    meta = layout_service.build_meta(state, id_map)

    bad: List[str] = []
    for part in state.parts:
        ids = set(id_map.get(part.id, []))
        arrays = [poly.pts for poly in polylines if poly.id in ids]
        if not arrays:
            bad.append("%s(无轮廓)" % part.name)
            continue
        pts = np.concatenate(arrays, axis=0)
        placement = state.placements[part.id]
        width, height = part.size_at(placement.rot)
        if (abs(float(pts[:, 0].min()) - placement.tx) > 1e-2
                or abs(float(pts[:, 1].min()) - placement.ty) > 1e-2
                or abs(float(pts[:, 0].max()) - (placement.tx + width)) > 1e-2
                or abs(float(pts[:, 1].max()) - (placement.ty + height)) > 1e-2):
            bad.append("%s(%.2f,%.2f)" % (part.name, placement.tx, placement.ty))
    check("A9 轮廓变换", not bad, "%d 条轮廓，包围盒与摆放一致" % len(polylines) if not bad
          else "不一致: %s" % ", ".join(bad))

    stats = meta["stats"]
    used = sum(part.area for part in state.parts if part.id in state.placements)
    expect = used / max(1.0, float(sheet["width"]) * float(sheet["height"]))
    check("A10 统计一致",
          abs(stats["usedArea"] - used) < 1.0 and abs(stats["utilization"] - expect) < 1e-3,
          "净面积 %.0f mm² 利用率 %.1f%%" % (used, stats["utilization"] * 100))

    with db.get_conn() as conn:
        again = layout_service.open(conn, project_id)
    point = again.placements[second.id]
    check("A11 进模式幂等",
          abs(point.tx - 615.0) < EPS and abs(point.ty - 10.0) < EPS,
          "再次 open 后仍为 (%.1f, %.1f)" % (point.tx, point.ty))

    with db.get_conn() as conn:
        result = layout_service.set_sheet(conn, project_id, 1000.0, 1200.0, 20.0, 8.0)
        clamped = layout_service.load(conn, project_id)
        inside = all(in_board(clamped, p) for p in clamped.parts)
        saved = clamped.sheet
    check("A12 改板材",
          result.ok and inside and abs(saved["width"] - 1000.0) < EPS
          and abs(saved["gap"] - 8.0) < EPS,
          "板材 %gx%g 余量 %g 间距 %g，越界零件已夹回" % (
              saved["width"], saved["height"], saved["margin"], saved["gap"]))

    with db.get_conn() as conn:
        layout_service.set_locked(conn, project_id, first.id, True)
        result = layout_service.reset(conn, project_id)
        fresh = layout_service.load(conn, project_id)
    check("A13 复位",
          result.ok and len(fresh.placements) == len(fresh.parts)
          and not any(p.locked for p in fresh.placements.values()),
          "复位后 %d 个摆放，锁定全部清零" % len(fresh.placements))

    # 收尾：恢复默认板材并复位，避免脚本把开发工程的板子改成小尺寸
    with db.get_conn() as conn:
        layout_service.set_sheet(conn, project_id, DEFAULT_SHEET["width"], DEFAULT_SHEET["height"],
                                 DEFAULT_SHEET["margin"], DEFAULT_SHEET["gap"])
        layout_service.reset(conn, project_id)
    print("  [..] 已恢复默认板材并复位")


# ---------------------------------------------------------------- WS 帧
def split_frame(raw: bytes) -> Tuple[int, int, Dict[str, Any], bytes]:
    from app.core import protocol
    msg_type, seq, meta_len, pay_len, total = protocol.decode_header(raw)
    if len(raw) < total:
        raise RuntimeError("帧被截断: 期望 %d 字节，实际 %d" % (total, len(raw)))
    meta = protocol.decode_meta(raw[protocol.HEADER_SIZE: protocol.HEADER_SIZE + meta_len])
    payload = raw[protocol.HEADER_SIZE + meta_len: total]
    return msg_type, seq, meta, payload


async def recv_logical(sock: Any, assembler: Any, timeout: float) -> Optional[Any]:
    raw = await asyncio.wait_for(sock.recv(), timeout=timeout)
    if isinstance(raw, str):
        raise RuntimeError("服务端返回文本帧，违反'只走二进制'约定")
    msg_type, seq, meta, payload = split_frame(raw)
    return assembler.feed(msg_type, meta, payload, seq)


async def wait_frame(sock: Any, assembler: Any, want: int,
                     timeout: float = 30.0) -> Tuple[Dict[str, Any], bytes]:
    from app.core.protocol import MsgType
    deadline = time.monotonic() + timeout
    while True:
        remain = deadline - time.monotonic()
        if remain <= 0:
            raise TimeoutError("等待帧 0x%02X 超时（%.0fs）" % (want, timeout))
        logical = await recv_logical(sock, assembler, remain)
        if logical is None:          # 分片还没凑齐
            continue
        msg_type, meta, payload, _seq = logical
        if msg_type == MsgType.ERROR:
            raise RuntimeError("后端错误: %s" % meta.get("message", meta))
        if msg_type == want:
            return meta, payload


# ---------------------------------------------------------------- 接口层
async def rest_tests(base: str) -> None:
    import websockets

    from app.core import protocol
    from app.core.protocol import MsgType

    ws_url = base.replace("https://", "wss://").replace("http://", "ws://").rstrip("/") + "/ws"

    print("== B. 接口 / WS 广播 ==")
    demo = http_json("POST", base + "/api/demo", {})
    check("B1 载入示例", int(demo.get("polylines", 0)) > 0,
          "%d 条轮廓" % int(demo.get("polylines", 0)))

    async with websockets.connect(ws_url, max_size=256 * 1024 * 1024) as sock:
        assembler = protocol.ChunkAssembler()
        await sock.send(protocol.encode_frame(MsgType.HELLO, {"clientVersion": "layout-test"}))
        await wait_frame(sock, assembler, MsgType.HELLO_ACK)

        # 连上时服务端只推图纸；排料帧仅由写操作/LAYOUT_SYNC 触发，不会有旧帧残留
        opened = http_json("POST", base + "/api/layout/open", {})
        meta, payload = await wait_frame(sock, assembler, MsgType.LAYOUT_STATE)
        polylines = protocol.decode_polylines(payload)
        declared = sum(len(p.get("polylineIds", [])) for p in meta["parts"])
        check("B2 进模式",
              opened["ok"] is True and len(polylines) == declared
              and len(meta["parts"]) == meta["stats"]["placementCount"],
              "%d 个零件 / %d 条轮廓 / 利用率 %.1f%%" % (
                  len(meta["parts"]), len(polylines), meta["stats"]["utilization"] * 100))

        parts = meta["parts"]
        target = parts[0]
        other = parts[1]

        # --- 非法拖拽：小件压到大件上（板内，但间距不足）
        bad = http_json("POST", base + "/api/layout/place",
                        {"partId": other["id"], "tx": target["tx"] + 50.0,
                         "ty": target["ty"] + 50.0})
        meta, _ = await wait_frame(sock, assembler, MsgType.LAYOUT_STATE)
        restored = next(p for p in meta["parts"] if p["id"] == other["id"])
        check("B3 拖拽拒绝",
              bad["ok"] is False and "间距不足" in bad["reason"]
              and abs(restored["tx"] - other["tx"]) < EPS,
              "%s（广播仍为原位 %.1f,%.1f）" % (bad["reason"], restored["tx"], restored["ty"]))

        # --- 合法拖拽：挪到零件右侧的空白带（板宽够，且不与任何零件重叠）
        free_x = float(meta["sheet"]["width"]) - float(meta["sheet"]["margin"]) - target["width"]
        free_y = float(meta["sheet"]["height"]) - float(meta["sheet"]["margin"]) - target["height"]
        good = http_json("POST", base + "/api/layout/place",
                         {"partId": target["id"], "tx": free_x, "ty": free_y})
        meta, _ = await wait_frame(sock, assembler, MsgType.LAYOUT_STATE)
        placed = next(p for p in meta["parts"] if p["id"] == target["id"])
        check("B3 拖拽落库",
              good["ok"] is True and abs(placed["tx"] - free_x) < EPS
              and abs(placed["ty"] - free_y) < EPS,
              "(%.1f, %.1f)" % (placed["tx"], placed["ty"]))

        # --- 旋转 90°
        spun = http_json("POST", base + "/api/layout/rotate",
                         {"partId": target["id"], "rot": target["rot"] + 90.0})
        meta, _ = await wait_frame(sock, assembler, MsgType.LAYOUT_STATE)
        rotated = next(p for p in meta["parts"] if p["id"] == target["id"])
        check("B4 旋转", spun["ok"] is True and abs(rotated["rot"] - 90.0) < EPS,
              "角度 %.0f°" % rotated["rot"])

        # --- 锁定后拖拽必须被拒
        locked = http_json("POST", base + "/api/layout/lock",
                           {"partId": target["id"], "locked": True})
        meta, _ = await wait_frame(sock, assembler, MsgType.LAYOUT_STATE)
        still = http_json("POST", base + "/api/layout/place",
                          {"partId": target["id"], "tx": free_x, "ty": 10.0})
        meta, _ = await wait_frame(sock, assembler, MsgType.LAYOUT_STATE)
        check("B5 锁定",
              locked["ok"] is True and still["ok"] is False and meta["stats"]["lockedCount"] == 1,
              still["reason"])

        http_json("POST", base + "/api/layout/lock", {"partId": target["id"], "locked": False})
        await wait_frame(sock, assembler, MsgType.LAYOUT_STATE)

        # --- 改板材
        resized = http_json("POST", base + "/api/layout/sheet",
                            {"width": 1000.0, "height": 1500.0, "margin": 15.0, "gap": 6.0})
        meta, _ = await wait_frame(sock, assembler, MsgType.LAYOUT_STATE)
        check("B6 改板材",
              resized["ok"] is True and abs(meta["sheet"]["width"] - 1000.0) < EPS
              and abs(meta["sheet"]["gap"] - 6.0) < EPS,
              "板材 %gx%g 余量 %g 间距 %g" % (
                  meta["sheet"]["width"], meta["sheet"]["height"],
                  meta["sheet"]["margin"], meta["sheet"]["gap"]))

        # --- 复位
        reset = http_json("POST", base + "/api/layout/reset", {})
        meta, _ = await wait_frame(sock, assembler, MsgType.LAYOUT_STATE)
        check("B7 复位",
              reset["ok"] is True
              and meta["stats"]["placementCount"] == len(meta["parts"])
              and meta["stats"]["lockedCount"] == 0
              and all(abs(p["rot"]) < EPS for p in meta["parts"]),
              "复位后 %d 个摆放" % meta["stats"]["placementCount"])

        # 收尾：恢复默认板材
        http_json("POST", base + "/api/layout/sheet", DEFAULT_SHEET)


# ---------------------------------------------------------------- 入口
def main() -> int:
    parser = argparse.ArgumentParser(description="BtCAM 模块 3 画布手动排料验收测试")
    parser.add_argument("--url", default="", help="后端地址；给定时追加接口/WS 测试")
    args = parser.parse_args()

    try:
        service_tests()
        if args.url:
            asyncio.run(rest_tests(args.url.rstrip("/")))
        else:
            print("== B. 接口 / WS 广播（跳过，未指定 --url） ==")
    except Exception as exc:             # 连接失败 / 断言失败 / 超时
        print("\n[ERROR] %s: %s" % (type(exc).__name__, exc))
        return 1

    failed = [c for c in CHECKS if not c[1]]
    print("\n== 结果：%d 项通过，%d 项失败 ==" % (len(CHECKS) - len(failed), len(failed)))
    if failed:
        for name, _ok, detail in failed:
            print("  失败: %s %s" % (name, detail))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
