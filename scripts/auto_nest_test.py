"""模块 4（自动排料）控制台验收测试。

两种跑法：

    # 1) 只测服务层（不需要后端，秒级）
    backend\\.venv\\Scripts\\python.exe scripts\\auto_nest_test.py

    # 2) 服务层 + 接口/WS 全链路（后端需先启动在 8975）
    backend\\.venv\\Scripts\\python.exe scripts\\auto_nest_test.py --url http://127.0.0.1:8975

注意：脚本会用内置示例图形重建当前工程，并改动板材参数/摆放/锁定；
结束时恢复 1220x2440 / 余量 10 / 间距 5，清空锁定与木纹约束并复位。

覆盖的验收项：

    A1  全件落位        5 个零件全部排上，逐个再过一遍 _check（无越界、无间距不足）
    A2  紧凑度          最低一排以上的高度占比，利用率 = 净面积 / 板材面积
    A3  锁定件不动      已锁定零件坐标不变，其余零件绕开它
    A4  木纹锁定        自动排料保持角度；手动旋转被拒（提示“木纹”）
    A5  关闭旋转        rotation=false 时所有零件角度保持原样
    A6  未排下          板材 500x500 时大件排不下：不抛错，件停靠在板材上方
    A7  幂等            连跑两次结果完全一致（内置扫描是确定性的）
    A8  引擎回退        未配置 DeepNest 时 engine=deepnest 自动回退 scan 并给出说明
    A9  取消排料        进度回调抛 TaskCancelled 时中止，且不落库
    A10 入参出参        板材宽高/边距/间距/partIds 生效，出参回带 sheet + placements
    A11 DeepNest 取消  桥接子进程在轮询里被 kill，不等满超时

    B1  接口提交         POST /api/layout/nest -> TASK_RESULT(stats) + LAYOUT_STATE
    B2  落库一致         GET /api/layout 与广播帧的摆放/利用率一致
    B3  木纹接口         POST /api/layout/fixed-rot -> 广播 fixedRot，自动排料后仍成立
    B4  参数校验         非法 engine 返回 422
    B5  空工程           不存在的 projectId 返回 400
    B6  取消接口         未知任务 404，已结束任务 409
    B7  入参出参         接口级板材参数/partIds 生效，TASK_RESULT 回带 placements
    B8  partIds 校验     不存在的零件返回 400

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
def run_nest(project_id: int, engine: str = "scan", rotation: bool = True) -> Dict[str, Any]:
    from app.services.nest_service import auto_nest_job

    progress: List[Tuple[float, str]] = []

    def report(value: float, message: str = "") -> None:
        progress.append((value, message))

    result = auto_nest_job({"projectId": project_id, "engine": engine,
                            "rotation": rotation}, report)
    return result["stats"]


def in_board(state: Any, part: Any) -> bool:
    placement = state.placements.get(part.id)
    if placement is None:
        return False
    width, height = part.size_at(placement.rot)
    margin = float(state.sheet["margin"])
    return (placement.tx >= margin - EPS
            and placement.ty >= margin - EPS
            and placement.tx + width <= float(state.sheet["width"]) - margin + EPS
            and placement.ty + height <= float(state.sheet["height"]) - margin + EPS)


def all_valid(state: Any) -> bool:
    """对每个零件反查一次 _check：等价于"两两不重叠 + 全部在板内"。"""
    from app.services.layout_service import layout_service

    for part in state.parts:
        placement = state.placements.get(part.id)
        if placement is None:
            return False
        if layout_service._check(state, part, placement.rot,
                                 placement.tx, placement.ty, exclude=part.id):
            return False
    return True


def service_tests() -> None:
    from app.models import db
    from app.services import geometry_service
    from app.services.layout_service import layout_service

    db.init_db()
    with db.get_conn() as conn:
        project_id = db.get_active_project_id(conn)
        layout_service.set_sheet(conn, project_id, **DEFAULT_SHEET)

    geometry_service.persist_document(project_id, geometry_service.build_demo_document())

    print("== A. 服务层：自动排料 ==")
    with db.get_conn() as conn:
        layout_service.open(conn, project_id)

    # --- A1 全件落位
    stats = run_nest(project_id, "scan", True)
    with db.get_conn() as conn:
        state = layout_service.load(conn, project_id)
    check("A1 全件落位",
          stats["placedCount"] == 5 and stats["unplacedCount"] == 0 and all_valid(state),
          "%s 引擎 · %d/%d 落位 · 最长耗时 %.1fms" % (
              stats["engine"], stats["placedCount"], len(state.parts), stats["calcMs"]))

    # --- A2 紧凑度：5 个零件应压在板子下部（底线扫描的特征）
    tops = []
    for part in state.parts:
        placement = state.placements[part.id]
        tops.append(placement.ty + part.size_at(placement.rot)[1])
    sheet_area = float(state.sheet["width"]) * float(state.sheet["height"])
    net_area = sum(part.area for part in state.parts)
    util = net_area / sheet_area
    check("A2 紧凑度",
          max(tops) <= float(state.sheet["height"]) * 0.4
          and abs(stats["utilization"] - util) < 0.001,
          "最高点 %.1fmm / 板高 %gmm · 利用率 %.2f%%" % (
              max(tops), state.sheet["height"], util * 100))

    # --- A3 锁定件不动
    big = max(state.parts, key=lambda p: p.area)
    with db.get_conn() as conn:
        moved = layout_service.move(conn, project_id, big.id, 10.0, 2000.0)
        locked = layout_service.set_locked(conn, project_id, big.id, True)
    before = (moved.state.placements[big.id].tx, moved.state.placements[big.id].ty)
    stats = run_nest(project_id, "scan", True)
    with db.get_conn() as conn:
        state = layout_service.load(conn, project_id)
    after = state.placements[big.id]
    check("A3 锁定件不动",
          locked.ok and abs(after.tx - before[0]) < EPS and abs(after.ty - before[1]) < EPS
          and after.locked and all_valid(state),
          "锁定件停在 (%.1f, %.1f) · %d 个落位" % (after.tx, after.ty, stats["placedCount"]))

    with db.get_conn() as conn:
        layout_service.set_locked(conn, project_id, big.id, False)

    # --- A4 木纹锁定：自动不转、手动旋转被拒
    small = min(state.parts, key=lambda p: p.area)
    with db.get_conn() as conn:
        fixed = layout_service.set_fixed_rot(conn, project_id, small.id, True)
    stats = run_nest(project_id, "scan", True)
    with db.get_conn() as conn:
        state = layout_service.load(conn, project_id)
        spin = layout_service.rotate(conn, project_id, small.id,
                                     state.placements[small.id].rot + 90.0)
    check("A4 木纹锁定",
          fixed.ok and state.placements[small.id].fixed_rot
          and abs(state.placements[small.id].rot) < EPS
          and spin.ok is False and "木纹" in spin.reason,
          "自动后角度 %.0f° · 手动旋转: %s" % (
              state.placements[small.id].rot, spin.reason))

    # --- A5 关闭旋转：角度保持
    before_rot = {p.id: state.placements[p.id].rot for p in state.parts}
    stats = run_nest(project_id, "scan", False)
    with db.get_conn() as conn:
        state = layout_service.load(conn, project_id)
    rotated = [p.id for p in state.parts
               if abs(state.placements[p.id].rot - before_rot[p.id]) > EPS]
    check("A5 关闭旋转", not rotated and all_valid(state),
          "角度变化零件数 %d" % len(rotated))

    # --- A6 未排下：500x500 板放不下 600x400
    with db.get_conn() as conn:
        layout_service.set_sheet(conn, project_id, 500.0, 500.0, 10.0, 5.0)
    stats = run_nest(project_id, "scan", True)
    with db.get_conn() as conn:
        state = layout_service.load(conn, project_id)
    parked = [p for p in state.parts
              if state.placements[p.id].ty >= 500.0 - EPS]
    on_board = [p for p in state.parts if in_board(state, p)]
    check("A6 未排下",
          stats["unplacedCount"] >= 1 and len(parked) == stats["unplacedCount"]
          and len(on_board) == stats["placedCount"],
          "%d 个落位 / %d 个停靠板外" % (stats["placedCount"], stats["unplacedCount"]))

    # 恢复默认板
    with db.get_conn() as conn:
        layout_service.set_sheet(conn, project_id, **DEFAULT_SHEET)
        layout_service.set_fixed_rot(conn, project_id, small.id, False)

    # --- A7 幂等
    first = run_nest(project_id, "scan", True)
    with db.get_conn() as conn:
        snap_state = layout_service.load(conn, project_id)
    snap1 = {p.id: (round(snap_state.placements[p.id].tx, 3),
                    round(snap_state.placements[p.id].ty, 3),
                    snap_state.placements[p.id].rot)
             for p in snap_state.parts}
    second = run_nest(project_id, "scan", True)
    with db.get_conn() as conn:
        state = layout_service.load(conn, project_id)
    snap2 = {p.id: (round(state.placements[p.id].tx, 3),
                    round(state.placements[p.id].ty, 3), state.placements[p.id].rot)
             for p in state.parts}
    check("A7 幂等",
          snap1 == snap2 and first["utilization"] == second["utilization"],
          "两次利用率均 %.2f%%" % (second["utilization"] * 100))

    # --- A8 引擎回退
    stats = run_nest(project_id, "deepnest", True)
    check("A8 引擎回退",
          stats["engine"] == "scan" and "DeepNest" in stats["engineNote"],
          stats["engineNote"] or "无说明")

    # --- A9 取消：进度回调抛 TaskCancelled，任务立即中止且不落库
    from app.core.tasks import TaskCancelled
    from app.services.nest_service import auto_nest_job

    with db.get_conn() as conn:
        snap = layout_service.load(conn, project_id)
    before = {part_id: (round(p.tx, 3), round(p.ty, 3), round(p.rot, 3))
              for part_id, p in snap.placements.items()}

    seen = {"n": 0}

    def cancelling_report(value: float, message: str = "") -> None:
        seen["n"] += 1
        if seen["n"] >= 3:
            raise TaskCancelled("test-cancel")

    aborted = False
    try:
        auto_nest_job({"projectId": project_id, "engine": "scan", "rotation": True},
                      cancelling_report)
    except TaskCancelled:
        aborted = True
    with db.get_conn() as conn:
        after_state = layout_service.load(conn, project_id)
    after = {part_id: (round(p.tx, 3), round(p.ty, 3), round(p.rot, 3))
             for part_id, p in after_state.placements.items()}
    check("A9 取消排料", aborted and before == after,
          "第 %d 次进度回调取消 · 摆放未变（%d 个）" % (seen["n"], len(after)))

    # --- A10 入参：板材参数 + partIds 子集；出参带 sheet/placements
    keep = max(snap.parts, key=lambda p: p.area)
    target_part = min(snap.parts, key=lambda p: p.area)
    keep_before = snap.placements[keep.id]
    keep_w, keep_h = keep.size_at(keep_before.rot)
    new_sheet = {"width": 1000.0, "height": 1200.0, "margin": 12.0, "gap": 6.0}

    result = auto_nest_job({
        "projectId": project_id, "engine": "scan", "rotation": True,
        "width": new_sheet["width"], "height": new_sheet["height"],
        "margin": new_sheet["margin"], "gap": new_sheet["gap"],
        "partIds": [target_part.id],
    }, lambda value, message="": None)

    with db.get_conn() as conn:
        state = layout_service.load(conn, project_id)
    kept = state.placements[keep.id]
    # 非参与件不动，只被“改板材”夹回可用区
    exp_tx = min(max(keep_before.tx, new_sheet["margin"]),
                 max(new_sheet["margin"], new_sheet["width"] - new_sheet["margin"] - keep_w))
    exp_ty = min(max(keep_before.ty, new_sheet["margin"]),
                 max(new_sheet["margin"], new_sheet["height"] - new_sheet["margin"] - keep_h))
    out_sheet = result.get("sheet") or {}
    out = result.get("placements") or []
    out_target = next((item for item in out if item["partId"] == target_part.id), None)
    stats = result["stats"]
    check("A10 入参出参",
          abs(out_sheet.get("width", 0) - new_sheet["width"]) < EPS
          and abs(out_sheet.get("height", 0) - new_sheet["height"]) < EPS
          and abs(out_sheet.get("margin", 0) - new_sheet["margin"]) < EPS
          and abs(out_sheet.get("gap", 0) - new_sheet["gap"]) < EPS
          and stats["placedCount"] == 1 and stats["unplacedCount"] == 0
          and abs(kept.tx - exp_tx) < EPS and abs(kept.ty - exp_ty) < EPS
          and out_target is not None and "tx" in out_target and "rot" in out_target
          and in_board(state, target_part),
          "板材改为 %gx%g · 只排 1 件 · 其余 %d 件原位当障碍 · 回带 %d 个摆放" % (
              new_sheet["width"], new_sheet["height"],
              len(state.parts) - 1, len(out)))

    # --- A11 DeepNest 可取消：轮询里感知取消并 kill 子进程，不等 60s 超时
    import os
    import tempfile

    from app.services.nesting_service import DeepNestBridge, Part as NestPart, normalize_part

    fd, script = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    with open(script, "w", encoding="utf-8") as handle:
        handle.write("import time\ntime.sleep(30)\n")
    os.environ["BTCAM_DEEPNEST_CMD"] = "%s %s" % (sys.executable, script)

    class _CancelSignal:
        """假 reporter：只实现 check_cancelled，第 2 次检查就取消。"""

        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, value: float, message: str = "") -> None:
            pass

        def check_cancelled(self) -> None:
            self.calls += 1
            if self.calls >= 2:
                raise TaskCancelled("test-cancel")

    bridge = DeepNestBridge(timeout=60.0)
    started = time.monotonic()
    killed = False
    try:
        bridge.pack([NestPart(keep.id, normalize_part(keep.outline), True, False)],
                    1220.0, 2440.0, _CancelSignal())
    except TaskCancelled:
        killed = True
    finally:
        os.environ.pop("BTCAM_DEEPNEST_CMD", None)
        try:
            os.remove(script)
        except OSError:
            pass
    elapsed = time.monotonic() - started
    check("A11 DeepNest 取消", killed and elapsed < 5.0,
          "%.2fs 内中止（超时设 60s）" % elapsed)

    # 收尾：清空约束并复位
    with db.get_conn() as conn:
        layout_service.reset(conn, project_id)


# ---------------------------------------------------------------- 接口层
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
                     timeout: float = 60.0) -> Tuple[Dict[str, Any], bytes]:
    from app.core.protocol import MsgType

    deadline = time.monotonic() + timeout
    while True:
        remain = deadline - time.monotonic()
        if remain <= 0:
            raise TimeoutError("等待帧 0x%02X 超时（%.0fs）" % (want, timeout))
        logical = await recv_logical(sock, assembler, remain)
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
    http_json("POST", base + "/api/layout/sheet", DEFAULT_SHEET)

    async with websockets.connect(ws_url, max_size=256 * 1024 * 1024) as sock:
        assembler = protocol.ChunkAssembler()
        await sock.send(protocol.encode_frame(MsgType.HELLO, {"clientVersion": "nest-test"}))
        await wait_frame(sock, assembler, MsgType.HELLO_ACK)

        http_json("POST", base + "/api/layout/open", {})
        meta, _ = await wait_frame(sock, assembler, MsgType.LAYOUT_STATE)
        parts = meta["parts"]

        # --- B1 提交自动排料：TASK_RESULT 只带 stats（无 polylines），随后补一帧 LAYOUT_STATE
        accepted = http_json("POST", base + "/api/layout/nest",
                             {"engine": "scan", "rotation": True})
        result_meta, result_payload = await wait_frame(sock, assembler, MsgType.TASK_RESULT)
        state_meta, state_payload = await wait_frame(sock, assembler, MsgType.LAYOUT_STATE)
        stats = result_meta.get("stats") or {}
        check("B1 接口提交",
              result_meta.get("kind") == "layout_nest" and len(result_payload) == 0
              and stats.get("placedCount") == 5
              and len(protocol.decode_polylines(state_payload)) > 0,
              "%s · %s · 利用率 %.2f%%" % (accepted["taskId"], stats.get("engine"),
                                          float(stats.get("utilization", 0)) * 100))

        # --- B2 落库一致
        live = http_json("GET", base + "/api/layout", None)
        check("B2 落库一致",
              live["stats"]["placementCount"] == state_meta["stats"]["placementCount"]
              and abs(live["stats"]["utilization"] - state_meta["stats"]["utilization"]) < EPS,
              "%d 个摆放 / 利用率 %.2f%%" % (
                  live["stats"]["placementCount"], live["stats"]["utilization"] * 100))

        # --- B3 木纹接口 → 广播 → 自动排料后仍成立
        target = parts[0]
        fixed = http_json("POST", base + "/api/layout/fixed-rot",
                          {"partId": target["id"], "fixedRot": True})
        meta, _ = await wait_frame(sock, assembler, MsgType.LAYOUT_STATE)
        marked = next(p for p in meta["parts"] if p["id"] == target["id"])

        http_json("POST", base + "/api/layout/nest", {"engine": "scan", "rotation": True})
        await wait_frame(sock, assembler, MsgType.TASK_RESULT)
        meta, _ = await wait_frame(sock, assembler, MsgType.LAYOUT_STATE)
        kept = next(p for p in meta["parts"] if p["id"] == target["id"])
        check("B3 木纹接口",
              fixed["ok"] is True and marked["fixedRot"] is True
              and kept["fixedRot"] is True and abs(kept["rot"]) < EPS,
              "零件 #%d 角度 %.0f°" % (kept["id"], kept["rot"]))

        # --- B4 非法引擎参数
        rejected = False
        try:
            http_json("POST", base + "/api/layout/nest", {"engine": "magic"})
        except RuntimeError as exc:
            rejected = "422" in str(exc)
        check("B4 参数校验", rejected, "engine=magic 被拒绝" if rejected else "未被拒绝")

        # --- B5 不存在的工程
        empty = False
        try:
            http_json("POST", base + "/api/layout/nest", {"projectId": 999999})
        except RuntimeError as exc:
            empty = "400" in str(exc)
        check("B5 空工程", empty, "projectId=999999 返回 400" if empty else "未被拒绝")

        # --- B6 取消接口：未知任务 404，已结束任务 409
        unknown_ok = False
        try:
            http_json("POST", base + "/api/tasks/not-a-task/cancel")
        except RuntimeError as exc:
            unknown_ok = "404" in str(exc)
        finished_ok = False
        try:
            http_json("POST", base + "/api/tasks/%s/cancel" % accepted["taskId"])
        except RuntimeError as exc:
            finished_ok = "409" in str(exc)
        check("B6 取消接口", unknown_ok and finished_ok,
              "未知任务 404 · 已结束任务 409" if unknown_ok and finished_ok
              else "unknown=%s finished=%s" % (unknown_ok, finished_ok))

        # --- B7 入参出参：接口级板材参数 + partIds，TASK_RESULT 回带 placements
        target = min(parts, key=lambda p: p["width"] * p["height"])
        http_json("POST", base + "/api/layout/nest",
                  {"engine": "scan", "rotation": True, "width": 1000.0,
                   "height": 1200.0, "margin": 12.0, "gap": 6.0,
                   "partIds": [target["id"]]})
        result_meta, _ = await wait_frame(sock, assembler, MsgType.TASK_RESULT)
        state_meta, _ = await wait_frame(sock, assembler, MsgType.LAYOUT_STATE)
        stats = result_meta.get("stats") or {}
        sheet = result_meta.get("sheet") or {}
        returned = result_meta.get("placements") or []
        picked = next((item for item in returned if item["partId"] == target["id"]), None)
        check("B7 入参出参",
              abs(float(sheet.get("width", 0)) - 1000.0) < EPS
              and abs(float(sheet.get("height", 0)) - 1200.0) < EPS
              and abs(float(state_meta["sheet"]["height"]) - 1200.0) < EPS
              and stats.get("placedCount") == 1
              and picked is not None and all(k in picked for k in ("tx", "ty", "rot")),
              "板 %.0fx%.0f · 参与 1 件 · 回带 %d 个摆放" % (
                  float(sheet.get("width", 0)), float(sheet.get("height", 0)),
                  len(returned)))

        # --- B8 partIds 校验：不存在的零件直接 400
        rejected_ids = False
        try:
            http_json("POST", base + "/api/layout/nest", {"partIds": [999999]})
        except RuntimeError as exc:
            rejected_ids = "400" in str(exc)
        check("B8 partIds 校验", rejected_ids,
              "partIds=[999999] 返回 400" if rejected_ids else "未被拒绝")

        # 收尾：清约束 + 恢复板材 + 复位
        for part in meta["parts"]:
            http_json("POST", base + "/api/layout/fixed-rot",
                      {"partId": part["id"], "fixedRot": False})
            http_json("POST", base + "/api/layout/lock",
                      {"partId": part["id"], "locked": False})
        http_json("POST", base + "/api/layout/sheet", DEFAULT_SHEET)
        http_json("POST", base + "/api/layout/reset", {})


# ---------------------------------------------------------------- 入口
def main() -> int:
    parser = argparse.ArgumentParser(description="BtCAM 模块 4 自动排料验收测试")
    parser.add_argument("--url", default="", help="后端地址；给定时追加接口/WS 测试")
    args = parser.parse_args()

    try:
        service_tests()
        if args.url:
            asyncio.run(rest_tests(args.url.rstrip("/")))
        else:
            print("== B. 接口 / WS 广播（跳过，未指定 --url） ==")
    except Exception as exc:
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
