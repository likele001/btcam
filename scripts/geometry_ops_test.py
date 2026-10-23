"""模块 2（OCCT 几何基础封装服务）控制台验收测试。

两种跑法：

    # 1) 只测服务层（不需要后端，秒级）
    backend\\.venv\\Scripts\\python.exe scripts\\geometry_ops_test.py

    # 2) 服务层 + 接口全链路（后端需先启动在 8975）
    backend\\.venv\\Scripts\\python.exe scripts\\geometry_ops_test.py --url http://127.0.0.1:8975

覆盖的验收项（对应模块 2 的第 6 条要求 + 资源管控）：

    A1 刀具补偿偏移    100x60 矩形外偏 5mm -> 包围盒 (-5,-5,105,65)，面积≈7678.5
    A2 偏移后无自交    同一条偏移结果跑自相交检测 -> 0 条告警
    A3 内外方向        side=auto：外框外扩、内孔内缩（孔变小）
    A4 轮廓求交        两个重叠矩形 -> 交集 50x50 / 面积 2500
    A5 包围盒          多轮廓合并 bbox
    A6 自相交检测      蝴蝶结 -> 1 条告警，交点 (50,50)
    A7 闭合校验        0.3mm 缺口 -> ok=False gap≈0.3；真闭合 -> ok=True
    A8 断线合并        4 条首尾相接的直线 -> 1 条闭合轮廓
    A9 内存不泄漏      预热 30 次后连跑 200 次偏移，RSS 增长 < 10MB
    B1 接口信息        GET  /api/geometry/info
    B2 包围盒接口      POST /api/geometry/bbox（同步）
    B3 闭合校验接口    POST /api/geometry/closure（同步）
    B4 自相交接口      POST /api/geometry/self-intersections（同步）
    B5 偏移任务        POST /api/geometry/offset -> TASK_RESULT + BGC1
    B6 求交任务        POST /api/geometry/intersect -> TASK_RESULT + BGC1
    B7 合并任务        POST /api/geometry/merge -> TASK_RESULT + BGC1

退出码：0 全部通过；1 存在失败项。
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import math
import sys
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 允许直接以 scripts/geometry_ops_test.py 运行：把 backend 加入 import 路径
BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR / ".venv" / "Lib" / "site-packages"))

import numpy as np  # noqa: E402

CHECKS: List[Tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, ok, detail))
    print("  %s %s%s" % ("[OK]  " if ok else "[FAIL]", name, ("  " + detail) if detail else ""))
    return ok


# ---------------------------------------------------------------- 内存
class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def rss_mb() -> float:
    """当前进程常驻内存（MB）。OCCT 的泄露最终一定体现在 WorkingSet 上。"""
    counters = _PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(counters)
    ok = _PSAPI.GetProcessMemoryInfo(
        _KERNEL32.GetCurrentProcess(),
        ctypes.byref(counters), counters.cb,
    )
    if not ok:
        raise OSError("GetProcessMemoryInfo 失败，无法监控内存")
    return counters.WorkingSetSize / (1024.0 * 1024.0)


_KERNEL32 = ctypes.WinDLL("kernel32")
_PSAPI = ctypes.WinDLL("psapi")
_KERNEL32.GetCurrentProcess.restype = wintypes.HANDLE
_PSAPI.GetProcessMemoryInfo.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(_PROCESS_MEMORY_COUNTERS), wintypes.DWORD,
]
_PSAPI.GetProcessMemoryInfo.restype = wintypes.BOOL


# ---------------------------------------------------------------- 造图形
def rect(x: float, y: float, w: float, h: float, pid: int = 1,
         layer: int = 1, is_hole: bool = False):
    from app.models.geometry import Polyline
    pts = np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.float32)
    return Polyline(pid, layer, pts, True, is_hole)


def circle(cx: float, cy: float, r: float, pid: int = 1, seg: int = 128,
           is_hole: bool = False):
    from app.models.geometry import Polyline
    angles = np.linspace(0, 2 * math.pi, seg, endpoint=False)
    pts = np.stack([cx + r * np.cos(angles), cy + r * np.sin(angles)], axis=1)
    return Polyline(pid, 1, pts.astype(np.float32), True, is_hole)


def bowtie(pid: int = 1):
    from app.models.geometry import Polyline
    return Polyline(pid, 1, np.array([[0, 0], [100, 100], [100, 0], [0, 100]],
                                     dtype=np.float32), True)


def near(value: float, expect: float, tol: float) -> bool:
    return abs(value - expect) <= tol


# ---------------------------------------------------------------- A. 服务层
def run_service_checks() -> None:
    from app.services.geometry_ops import get_ops

    ops = get_ops()
    print("== 几何后端：%s（OCCT=%s） ==" % (ops.backend(), ops.info().get("occt")))

    # --- A1 刀具补偿偏移
    print("== A1 刀具补偿偏移（100x60 矩形，外偏 5mm） ==")
    out = ops.offset([rect(0, 0, 100, 60)], 5.0, join="round", side="outside")
    box = ops.bbox(out)
    area = float(np.sum([p.area() for p in out]))
    expect_area = 100 * 60 + 2 * (100 + 60) * 5 + math.pi * 25
    ok = (len(out) == 1 and near(box.xmin, -5, 1e-3) and near(box.ymin, -5, 1e-3)
          and near(box.xmax, 105, 1e-3) and near(box.ymax, 65, 1e-3)
          and near(area, expect_area, expect_area * 0.01))
    check("A1 刀具补偿偏移", ok,
          "轮廓=%d bbox=(%.2f,%.2f,%.2f,%.2f) 面积=%.2f(期望 %.2f)" % (
              len(out), box.xmin, box.ymin, box.xmax, box.ymax, area, expect_area))

    # --- A2 偏移后无自相交
    print("== A2 偏移后无自相交 ==")
    issues = ops.self_intersections(out)
    check("A2 偏移后无自交", len(issues) == 0, "告警=%d" % len(issues))

    # --- A3 内外方向
    print("== A3 内外方向（side=auto：外框外扩、内孔内缩） ==")
    outer = rect(0, 0, 200, 120, 1, is_hole=False)
    hole = circle(100, 60, 20, 2, is_hole=True)
    out3 = ops.offset([outer, hole], 5.0, side="auto")
    boxes = []
    for poly in out3:
        b = ops.bbox([poly])
        boxes.append((poly.is_hole, b, poly.area()))
    outer_box = next((b for is_hole, b, _a in boxes if not is_hole), None)
    hole_box = next((b for is_hole, b, _a in boxes if is_hole), None)
    hole_area = next((a for is_hole, _b, a in boxes if is_hole), 0.0)
    ok3 = (outer_box is not None and hole_box is not None
           and near(outer_box.xmin, -5, 1e-3) and near(outer_box.xmax, 205, 1e-3)
           and near(hole_box.width, 30, 0.3)                     # 直径 40 -> 30
           and near(hole_area, -math.pi * 225, math.pi * 225 * 0.03))
    check("A3 内外方向", ok3,
          "外框宽=%.2f 内孔直径=%.2f 内孔面积=%.2f(期望 -706.86)" % (
              outer_box.width if outer_box else 0.0,
              hole_box.width if hole_box else 0.0, hole_area))

    # --- A4 轮廓求交
    print("== A4 轮廓求交（两个重叠矩形） ==")
    a = rect(0, 0, 100, 100, 1)
    b = rect(50, 50, 100, 100, 2)
    inter = ops.intersect([a], [b])
    ibox = ops.bbox(inter) if inter else None
    iarea = float(np.sum([p.area() for p in inter])) if inter else 0.0
    ok4 = (len(inter) == 1 and ibox is not None
           and near(ibox.xmin, 50, 1e-3) and near(ibox.ymin, 50, 1e-3)
           and near(ibox.xmax, 100, 1e-3) and near(ibox.ymax, 100, 1e-3)
           and near(iarea, 2500, 1.0))
    check("A4 轮廓求交", ok4,
          "轮廓=%d bbox=%s 面积=%.2f" % (
              len(inter), "(%.1f,%.1f,%.1f,%.1f)" % (
                  ibox.xmin, ibox.ymin, ibox.xmax, ibox.ymax) if ibox else "-", iarea))

    # --- A5 包围盒
    print("== A5 包围盒（多轮廓） ==")
    box5 = ops.bbox([rect(0, 0, 50, 30), rect(200, 100, 10, 10)])
    ok5 = (near(box5.xmin, 0, 1e-6) and near(box5.ymin, 0, 1e-6)
           and near(box5.xmax, 210, 1e-6) and near(box5.ymax, 110, 1e-6)
           and near(box5.width, 210, 1e-6) and box5.contourCount == 2)
    check("A5 包围盒", ok5,
          "%.1f x %.1f 中心=(%.1f,%.1f) 轮廓=%d" % (
              box5.width, box5.height, box5.xmin + box5.width / 2,
              box5.ymin + box5.height / 2, box5.contourCount))

    # --- A6 自相交检测
    print("== A6 自相交检测（蝴蝶结） ==")
    issues6 = ops.self_intersections([bowtie()])
    point = issues6[0].point if issues6 else (0.0, 0.0)
    ok6 = len(issues6) == 1 and near(point[0], 50.0, 1e-3) and near(point[1], 50.0, 1e-3)
    check("A6 自相交检测", ok6,
          "告警=%d 交点=(%.2f,%.2f)" % (len(issues6), point[0], point[1]))

    # --- A7 闭合校验
    print("== A7 闭合校验 ==")
    from app.models.geometry import Polyline
    gap_line = Polyline(9, 1, np.array([[0, 0], [100, 0], [100, 60], [0, 0.3]],
                                       dtype=np.float32), False)
    infos = ops.closure_report([rect(0, 0, 100, 60, 1), gap_line], tol=0.05)
    closed_info = next(i for i in infos if i.polylineId == 1)
    gap_info = next(i for i in infos if i.polylineId == 9)
    ok7 = (closed_info.ok and near(closed_info.gap, 0.0, 1e-6)
           and not gap_info.ok and near(gap_info.gap, 0.3, 1e-3))
    check("A7 闭合校验", ok7,
          "闭合 gap=%.4f ok=%s / 缺口 gap=%.4f ok=%s" % (
              closed_info.gap, closed_info.ok, gap_info.gap, gap_info.ok))

    # --- A8 断线合并
    print("== A8 断线合并（4 条首尾相接直线） ==")
    segs = [
        Polyline(1, 1, np.array([[0, 0], [100, 0]], np.float32)),
        Polyline(2, 1, np.array([[100, 0], [100, 60]], np.float32)),
        Polyline(3, 1, np.array([[100, 60], [0, 60]], np.float32)),
        Polyline(4, 1, np.array([[0, 60], [0, 0]], np.float32)),
    ]
    merged = ops.merge_gaps(segs, tol=0.2)
    mbox = ops.bbox(merged) if merged else None
    ok8 = (len(merged) == 1 and merged[0].closed and merged[0].point_count == 4
           and mbox is not None and near(abs(merged[0].area()), 6000, 1e-3))
    check("A8 断线合并", ok8,
          "轮廓=%d 闭合=%s 顶点=%d 面积=%.1f" % (
              len(merged), merged[0].closed if merged else "-",
              merged[0].point_count if merged else 0,
              abs(merged[0].area()) if merged else 0.0))

    # --- A9 内存不泄漏
    print("== A9 长时间反复调用（预热 30 次 + 连跑 200 次） ==")
    samples = [circle(0, 0, 30, 1, seg=64), rect(-50, -40, 100, 80, 2)]
    for _ in range(30):                     # 预热：把绑定加载/字典缓存等一次性开销摊掉
        ops.offset(samples, 3.0, side="auto")
    base = rss_mb()
    for _ in range(200):
        ops.offset(samples, 3.0, side="auto")
    after = rss_mb()
    growth = after - base
    resource = ops.info().get("resource") or {}
    check("A9 内存不泄漏", growth < 10.0,
          "RSS %.1f -> %.1f MB（增长 %.2f MB）/ 释放对象 %s / gc %s" % (
              base, after, growth, resource.get("released", "-"),
              resource.get("gcRuns", "-")))


# ---------------------------------------------------------------- HTTP 工具
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


def contour_payload(*polylines) -> List[Dict[str, Any]]:
    """轮廓 -> 接口 JSON；pts 用嵌套列表，前端也是这个格式。"""
    return [
        {
            "id": p.id, "layerId": p.layer_id, "closed": p.closed, "isHole": p.is_hole,
            "pts": np.asarray(p.pts, dtype=np.float64).round(4).tolist(),
        }
        for p in polylines
    ]


async def wait_logical(sock, assembler, timeout: float):
    raw = await asyncio.wait_for(sock.recv(), timeout=timeout)
    if isinstance(raw, str):
        raise RuntimeError("服务端返回文本帧，违反'只走二进制'约定")
    msg_type, seq, meta_len, pay_len, total = protocol.decode_header(raw)
    if len(raw) < total:
        raise RuntimeError("帧被截断")
    meta = protocol.decode_meta(raw[protocol.HEADER_SIZE: protocol.HEADER_SIZE + meta_len])
    payload = raw[protocol.HEADER_SIZE + meta_len: total]
    return assembler.feed(msg_type, meta, payload, seq)


async def wait_result(sock, assembler, timeout: float = 120.0) -> Tuple[Dict[str, Any], bytes]:
    """等 TASK_RESULT；期间打印进度，任务失败直接抛错。"""
    deadline = time.monotonic() + timeout
    while True:
        remain = deadline - time.monotonic()
        if remain <= 0:
            raise TimeoutError("等待 TASK_RESULT 超时")
        logical = await wait_logical(sock, assembler, remain)
        if logical is None:
            continue
        msg_type, meta, payload, _seq = logical
        if msg_type == MsgType.TASK_FAILED:
            raise RuntimeError("任务失败：%s" % meta.get("message", meta))
        if msg_type == MsgType.TASK_PROGRESS:
            if isinstance(meta.get("progress"), (int, float)) and meta.get("message"):
                print("         进度 %5.1f%%  %s" % (
                    float(meta["progress"]) * 100, meta["message"]))
            continue
        if msg_type == MsgType.TASK_RESULT:
            return meta, payload


# ---------------------------------------------------------------- B. 接口
async def run_interface_checks(base: str) -> None:
    import websockets

    print("== B1 接口信息 ==")
    info = http_json("GET", base + "/api/geometry/info")
    check("B1 接口信息", bool(info.get("backend")),
          "backend=%s occt=%s" % (info.get("backend"), info.get("occt")))

    print("== B2 包围盒接口（同步） ==")
    box = http_json("POST", base + "/api/geometry/bbox",
                    {"contours": contour_payload(rect(0, 0, 100, 60))})
    check("B2 包围盒接口", near(box.get("width", 0), 100, 1e-6)
          and near(box.get("height", 0), 60, 1e-6),
          "%.1f x %.1f 中心=(%.1f,%.1f)" % (
              box.get("width", 0), box.get("height", 0),
              box.get("centerX", 0), box.get("centerY", 0)))

    print("== B3 闭合校验接口（同步） ==")
    from app.models.geometry import Polyline
    open_poly = Polyline(9, 1, np.array([[0, 0], [100, 0], [100, 60], [0, 0.4]],
                                        dtype=np.float32), False)
    closure = http_json("POST", base + "/api/geometry/closure",
                        {"contours": contour_payload(rect(0, 0, 100, 60), open_poly)})
    check("B3 闭合校验接口", closure.get("count") == 2 and closure.get("openCount") == 1,
          "总计=%s 未闭合=%s" % (closure.get("count"), closure.get("openCount")))

    print("== B4 自相交接口（同步） ==")
    selfx = http_json("POST", base + "/api/geometry/self-intersections",
                      {"contours": contour_payload(bowtie())})
    first = (selfx.get("items") or [{}])[0]
    check("B4 自相交接口", selfx.get("count") == 1
          and near(float(first.get("point", [0, 0])[0]), 50, 1e-3),
          "告警=%s 交点=%s" % (selfx.get("count"), first.get("point")))

    ws_url = base.replace("https://", "wss://").replace("http://", "ws://").rstrip("/") + "/ws"
    async with websockets.connect(ws_url, max_size=256 * 1024 * 1024) as sock:
        assembler = protocol.ChunkAssembler()
        await sock.send(protocol.encode_frame(MsgType.HELLO, {"clientVersion": "geom-test"}))
        # 吃掉握手回执与连接建立时下发的图形，避免误判
        deadline = time.monotonic() + 20.0
        seen = set()
        while time.monotonic() < deadline and seen != {MsgType.HELLO_ACK, MsgType.GEOMETRY_DOC}:
            logical = await wait_logical(sock, assembler, max(0.1, deadline - time.monotonic()))
            if logical is not None:
                seen.add(logical[0])

        print("== B5 偏移任务（异步） ==")
        accepted = http_json("POST", base + "/api/geometry/offset",
                             {"contours": contour_payload(rect(0, 0, 100, 60)),
                              "distance": 5.0, "join": "round", "side": "outside"})
        meta, payload = await wait_result(sock, assembler)
        polys = protocol.decode_polylines(payload)
        pts = np.vstack([p.pts for p in polys]) if polys else np.zeros((1, 2))
        ok5 = (accepted.get("kind") == "geometry" and len(polys) == 1
               and near(float(pts[:, 0].min()), -5, 1e-2)
               and near(float(pts[:, 0].max()), 105, 1e-2))
        check("B5 偏移任务", ok5,
              "taskId=%s 轮廓=%d bbox_x=(%.2f,%.2f) %s" % (
                  accepted.get("taskId"), len(polys),
                  float(pts[:, 0].min()), float(pts[:, 0].max()),
                  meta.get("stats", {})))

        print("== B6 求交任务（异步） ==")
        accepted = http_json("POST", base + "/api/geometry/intersect",
                             {"contours": contour_payload(rect(0, 0, 100, 100)),
                              "clip": contour_payload(rect(50, 50, 100, 100))})
        _meta, payload = await wait_result(sock, assembler)
        polys = protocol.decode_polylines(payload)
        area = float(np.sum([p.area() for p in polys])) if polys else 0.0
        check("B6 求交任务", accepted.get("kind") == "geometry" and len(polys) == 1
              and near(area, 2500, 1.0),
              "taskId=%s 轮廓=%d 面积=%.2f" % (accepted.get("taskId"), len(polys), area))

        print("== B7 断线合并任务（异步） ==")
        segs = [
            Polyline(1, 1, np.array([[0, 0], [100, 0]], np.float32)),
            Polyline(2, 1, np.array([[100, 0], [100, 60]], np.float32)),
            Polyline(3, 1, np.array([[100, 60], [0, 60]], np.float32)),
            Polyline(4, 1, np.array([[0, 60], [0, 0]], np.float32)),
        ]
        accepted = http_json("POST", base + "/api/geometry/merge",
                             {"contours": contour_payload(*segs), "tol": 0.2})
        _meta, payload = await wait_result(sock, assembler)
        polys = protocol.decode_polylines(payload)
        area = abs(float(np.sum([p.area() for p in polys]))) if polys else 0.0
        check("B7 断线合并任务", accepted.get("kind") == "geometry" and len(polys) == 1
              and polys[0].closed and near(area, 6000, 1.0),
              "taskId=%s 轮廓=%d 闭合=%s 面积=%.2f" % (
                  accepted.get("taskId"), len(polys),
                  polys[0].closed if polys else "-", area))


# ---------------------------------------------------------------- 入口
def main() -> int:
    parser = argparse.ArgumentParser(description="BtCAM 模块2（OCCT 几何封装）验收测试")
    parser.add_argument("--url", default="", help="后端地址；不填则只跑服务层")
    args = parser.parse_args()

    print("########## A. 几何服务层 ##########")
    run_service_checks()

    if args.url:
        global protocol, MsgType
        from app.core import protocol
        from app.core.protocol import MsgType
        print("\n########## B. 接口全链路 ##########")
        asyncio.run(run_interface_checks(args.url.rstrip("/")))
    else:
        print("\n（未指定 --url，跳过接口测试）")

    failed = [c for c in CHECKS if not c[1]]
    print("\n== 结果：%d 项通过，%d 项失败 ==" % (len(CHECKS) - len(failed), len(failed)))
    if failed:
        for name, _ok, detail in failed:
            print("  失败：%s %s" % (name, detail))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
