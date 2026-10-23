"""模块 1（DXF 解析与轮廓修复）控制台验收测试。

两种跑法：

    # 1) 只跑算法（不需要后端，秒级）
    backend\\.venv\\Scripts\\python.exe scripts\\dxf_import_test.py

    # 2) 算法 + 接口全链路（后端需先启动在 8975）
    backend\\.venv\\Scripts\\python.exe scripts\\dxf_import_test.py --url http://127.0.0.1:8975

覆盖的验收项（对应模块 1 的第 6 条要求）：
    A1 开放线闭合      4 条断开直线，首尾留 0.3mm 缺口 -> 应自动闭合为 1 条轮廓
    A2 重叠线清理      同一矩形画两遍 -> 重复线段应被消除（4 条边保留）
    A3 自相交识别      蝴蝶结轮廓 -> 应报 1 条 self_intersection，交点 (50,50)
    A4 内外轮廓区分    外框 + 内孔 -> 外轮廓 is_hole=False/逆时针，内孔 is_hole=True/顺时针
    A5 碎线剔除        0.2mm 的短线 -> 应被丢弃
    B1 接口导入        上传 DXF -> TASK_RESULT(partCount/polylineCount/stats)
    B2 图形下发        GEOMETRY_DOC 帧可解码，且 is_hole 标记正确
    B3 告警下发        GEOMETRY_ISSUES 帧可解码为异常线段，数量与 meta 一致

退出码：0 全部通过；1 存在失败项。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 允许直接以 scripts/dxf_import_test.py 运行：把 backend 加入 import 路径
BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))
# 让 ezdxf / numpy 从后端 venv 里找（脚本由 后端venv 的 python 执行）
sys.path.insert(0, str(BACKEND_DIR / ".venv" / "Lib" / "site-packages"))

import numpy as np  # noqa: E402

CHECKS: List[Tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, ok, detail))
    print("  %s %s%s" % ("[OK]  " if ok else "[FAIL]", name, ("  " + detail) if detail else ""))
    return ok


# ---------------------------------------------------------------- 算法级
def _rect(x: float, y: float, w: float, h: float, pid: int = 1,
          layer: int = 1, closed: bool = True):
    from app.models.geometry import Polyline
    pts = np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.float32)
    return Polyline(pid, layer, pts, closed)


def run_algorithm_checks() -> None:
    from app.geometry.repair import RepairOptions, TopologyRepair
    from app.models.geometry import Polyline

    repair = TopologyRepair(RepairOptions())

    # --- A1 开放线闭合
    print("== A1 开放线闭合（4 条直线，缺口 0.3mm） ==")
    segs = [
        Polyline(1, 1, np.array([[0, 0], [100, 0]], np.float32)),
        Polyline(2, 1, np.array([[100, 0], [100, 60]], np.float32)),
        Polyline(3, 1, np.array([[100, 60], [0, 60]], np.float32)),
        Polyline(4, 1, np.array([[0, 60.3], [0, 0]], np.float32)),
    ]
    polys, issues, stats = repair.apply(segs)
    ok = len(polys) == 1 and polys[0].closed and abs(polys[0].area() - 6000.0) < 200
    check("A1 开放线闭合", ok,
          "轮廓=%d 闭合=%s 面积=%.1f 告警=%d" % (
              len(polys), polys[0].closed if polys else "-",
              polys[0].area() if polys else 0.0, len(issues)))

    # --- A2 重叠线清理
    print("== A2 重叠线清理（同一矩形画两遍） ==")
    polys, issues, stats = repair.apply([_rect(0, 0, 100, 60, 1), _rect(0, 0, 100, 60, 2)])
    ok = (stats.removedSegments == 4 and len(polys) == 1
          and polys[0].point_count == 4 and abs(polys[0].area() - 6000.0) < 1e-3)
    check("A2 重叠线清理", ok,
          "去除重线=%d 剩余顶点=%d 面积=%.1f" % (
              stats.removedSegments, polys[0].point_count if polys else 0,
              polys[0].area() if polys else 0.0))

    # --- A3 自相交识别
    print("== A3 自相交识别（蝴蝶结） ==")
    bow = Polyline(1, 1, np.array([[0, 0], [100, 100], [100, 0], [0, 100]], np.float32), True)
    polys, issues, stats = repair.apply([bow])
    hits = [i for i in issues if i.kind == "self_intersection"]
    point = hits[0].point if hits else (0.0, 0.0)
    ok = len(hits) == 1 and abs(point[0] - 50.0) < 1e-3 and abs(point[1] - 50.0) < 1e-3
    check("A3 自相交识别", ok,
          "告警=%d 交点=(%.2f, %.2f) 异常线段=%s" % (
              len(hits), point[0], point[1],
              "有" if hits and hits[0].segment is not None else "无"))

    # --- A4 内外轮廓区分
    print("== A4 内外轮廓区分（外框 + 内孔） ==")
    angles = np.linspace(0, 2 * np.pi, 32, endpoint=False)
    hole = Polyline(2, 1, np.stack([50 + 20 * np.cos(angles),
                                    60 + 20 * np.sin(angles)], axis=1).astype(np.float32), True)
    polys, issues, stats = repair.apply([_rect(0, 0, 200, 120, 1), hole])
    outer = next((p for p in polys if not p.is_hole), None)
    inner = next((p for p in polys if p.is_hole), None)
    ok = (len(polys) == 2 and outer is not None and inner is not None
          and outer.area() > 0 and inner.area() < 0)
    check("A4 内外轮廓区分", ok,
          "外轮廓 area=%.1f(CCW) 内孔 area=%.1f(CW)" % (
              outer.area() if outer else 0.0, inner.area() if inner else 0.0))

    # --- A5 碎线剔除
    print("== A5 碎线剔除（0.2mm 短线） ==")
    tiny = [Polyline(1, 1, np.array([[0, 0], [0.2, 0]], np.float32)),
            _rect(0, 0, 50, 50, 2)]
    polys, issues, stats = repair.apply(tiny)
    check("A5 碎线剔除", stats.droppedTiny == 1 and len(polys) == 1,
          "剔除=%d 保留轮廓=%d" % (stats.droppedTiny, len(polys)))


# ---------------------------------------------------------------- 造测试 DXF
def build_test_dxf(path: str) -> None:
    """造一张覆盖全部验收点的图纸：带孔零件 + 断线零件 + 自相交零件 + 重复线。"""
    import ezdxf

    doc = ezdxf.new("R2010")
    doc.units = 4                       # mm
    doc.layers.add("CUT", color=1)
    doc.layers.add("HOLES", color=3)
    msp = doc.modelspace()

    # 零件 A：100x60 外框 + 20x20 内孔
    msp.add_lwpolyline([(0, 0), (100, 0), (100, 60), (0, 60)],
                       close=True, dxfattribs={"layer": "CUT"})
    msp.add_lwpolyline([(40, 20), (60, 20), (60, 40), (40, 40)],
                       close=True, dxfattribs={"layer": "HOLES"})

    # 零件 B：4 条断线（缺口 0.4mm）+ 一条完全重复的矩形
    msp.add_line((200, 0), (280, 0), dxfattribs={"layer": "CUT"})
    msp.add_line((280, 0), (280, 80), dxfattribs={"layer": "CUT"})
    msp.add_line((280, 80), (200, 80), dxfattribs={"layer": "CUT"})
    msp.add_line((200, 80.4), (200, 0), dxfattribs={"layer": "CUT"})
    msp.add_lwpolyline([(200, 0), (280, 0), (280, 80), (200, 80)],
                       close=True, dxfattribs={"layer": "CUT"})

    # 零件 C：自相交蝴蝶结
    msp.add_lwpolyline([(400, 0), (500, 100), (500, 0), (400, 100)],
                       close=True, dxfattribs={"layer": "CUT"})

    # 零件 D：外框 + 块引用（内含圆/圆弧）+ 椭圆
    # 目的：验证块递归展开、圆弧/椭圆离散这两条 DXF 读取分支
    # 注意：块内图元坐标是相对块原点(0,0)的，插入点会整体平移
    blk = doc.blocks.new(name="HOLE_BLK")
    blk.add_circle((20, 20), 8, dxfattribs={"layer": "HOLES"})
    blk.add_arc((20, 42), 6, 0, 180, dxfattribs={"layer": "HOLES"})
    msp.add_blockref("HOLE_BLK", (600, 0), dxfattribs={"layer": "HOLES"})
    msp.add_ellipse((680, 25), major_axis=(12, 0), ratio=0.5,
                    dxfattribs={"layer": "HOLES"})
    msp.add_lwpolyline([(590, -15), (700, -15), (700, 60), (590, 60)],
                       close=True, dxfattribs={"layer": "CUT"})

    # 零件 E：外框 + HATCH（三角形填充边界）+ 样条，验证另两条图元分支
    msp.add_lwpolyline([(710, -15), (800, -15), (800, 60), (710, 60)],
                       close=True, dxfattribs={"layer": "CUT"})
    hatch = msp.add_hatch(color=2, dxfattribs={"layer": "HOLES"})
    hatch.paths.add_polyline_path([(730, 5), (780, 5), (755, 35)], is_closed=True)
    msp.add_spline_control_frame([(715, 45), (730, 55), (745, 55), (760, 45)],
                                 dxfattribs={"layer": "CUT"})

    doc.saveas(path)


# ---------------------------------------------------------------- 接口级
def http_multipart(url: str, files: List[Tuple[str, str, bytes]],
                   fields: Dict[str, str], timeout: float = 120.0) -> Any:
    """极简 multipart 上传：不引 requests，保持后端依赖干净。"""
    boundary = "----BtCAM" + uuid.uuid4().hex
    body = bytearray()
    for name, value in fields.items():
        body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                 % (boundary, name, value)).encode("utf-8")
    for field, filename, data in files:
        body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                 "Content-Type: application/dxf\r\n\r\n" % (boundary, field, filename)).encode("utf-8")
        body += data + b"\r\n"
    body += ("--%s--\r\n" % boundary).encode("utf-8")

    req = urllib.request.Request(
        url, data=bytes(body), method="POST",
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary,
                 "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


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


async def run_interface_checks(base: str) -> None:
    import websockets

    ws_url = base.replace("https://", "wss://").replace("http://", "ws://").rstrip("/") + "/ws"

    # 造一张真实的 DXF 落到临时目录
    temp_dir = Path(tempfile.mkdtemp(prefix="btcam_dxf_test_"))
    dxf_path = temp_dir / "验收图纸.dxf"
    build_test_dxf(str(dxf_path))
    print("== 测试图纸：%s（%.1f KB） ==" % (dxf_path.name, dxf_path.stat().st_size / 1024))

    async with websockets.connect(ws_url, max_size=256 * 1024 * 1024) as sock:
        assembler = protocol.ChunkAssembler()
        # 先握手，吃掉连接建立时下发的旧图形
        await sock.send(protocol.encode_frame(MsgType.HELLO, {"clientVersion": "dxf-test"}))
        await _drain(sock, assembler, {MsgType.HELLO_ACK, MsgType.GEOMETRY_DOC}, timeout=30.0)

        print("== B1 上传 DXF（multipart） ==")
        accepted = http_multipart(
            base + "/api/import/dxf",
            files=[("files", dxf_path.name, dxf_path.read_bytes())],
            fields={"options": json.dumps({"replace": True, "splitParts": True})},
        )
        task_id = str(accepted.get("taskId") or "")
        check("B1 接口接受导入", bool(task_id) and accepted.get("kind") == "import",
              "taskId=%s" % task_id)

        # 依次收集：GEOMETRY_DOC(importSummary) -> GEOMETRY_ISSUES -> TASK_RESULT
        doc_meta: Optional[Dict[str, Any]] = None
        doc_payload = b""
        issues_meta: Optional[Dict[str, Any]] = None
        issues_payload = b""
        result_meta: Optional[Dict[str, Any]] = None

        deadline = time.monotonic() + 180.0
        while time.monotonic() < deadline:
            logical = await wait_logical(sock, assembler, deadline - time.monotonic())
            if logical is None:
                continue
            msg_type, meta, payload, _seq = logical
            if msg_type == MsgType.TASK_FAILED:
                raise RuntimeError("导入任务失败: %s" % meta.get("message", meta))
            if msg_type == MsgType.TASK_PROGRESS:
                if isinstance(meta.get("progress"), (int, float)):
                    print("         进度 %5.1f%%  %s" % (float(meta["progress"]) * 100,
                                                         meta.get("message") or ""))
                continue
            if msg_type == MsgType.GEOMETRY_DOC and "importSummary" in meta:
                doc_meta, doc_payload = meta, payload
            elif msg_type == MsgType.GEOMETRY_ISSUES:
                issues_meta, issues_payload = meta, payload
            elif msg_type == MsgType.TASK_RESULT and meta.get("kind") == "import":
                result_meta = meta
                break

        if result_meta is None:
            raise RuntimeError("等待导入结果超时")

        stats = result_meta.get("stats") or {}
        # 零件：A/B/C/D 四个闭合件；180°圆弧虽开放，但落在 D 内部，按设计归入 D
        check("B2 任务结果", int(result_meta.get("partCount", 0)) >= 4
              and int(result_meta.get("polylineCount", 0)) >= 8,
              "零件=%s 轮廓=%s 去除重线=%s 串接=%s 内孔=%s" % (
                  result_meta.get("partCount"), result_meta.get("polylineCount"),
                  stats.get("removedSegments"), stats.get("mergedContours"),
                  stats.get("holeContours")))

        polylines = protocol.decode_polylines(doc_payload) if doc_payload else []
        holes = [p for p in polylines if p.is_hole]
        declared = int(doc_meta.get("polylineCount", -1)) if doc_meta else -2
        check("B3 图形帧下发", len(polylines) == declared and len(polylines) >= 8 and len(holes) >= 2,
              "%d 条轮廓 / 内孔 %d 条 / %d 字节" % (len(polylines), len(holes), len(doc_payload)))

        segments = protocol.decode_polylines(issues_payload) if issues_payload else []
        issue_count = int((issues_meta or {}).get("count", -1))
        check("B4 告警帧下发", issue_count >= 1 and len(segments) == issue_count,
              "告警 %d 条 / 异常线段 %d 条" % (issue_count, len(segments)))
        if issues_meta:
            for item in (issues_meta.get("items") or [])[:3]:
                print("         - %s %s @ %s" % (item.get("kind"), item.get("message"),
                                                  item.get("point")))

    # 收尾：把接口当前工程恢复成内置示例，避免测试图纸留在库里
    try:
        urllib.request.urlopen(
            urllib.request.Request(base + "/api/demo", data=b"{}", method="POST",
                                   headers={"Content-Type": "application/json"}),
            timeout=30,
        ).read()
        print("== 已恢复内置示例图形 ==")
    except Exception as exc:                 # 恢复失败不影响测试结论
        print("== 恢复示例图形失败（可忽略）：%s ==" % exc)


async def _drain(sock, assembler, types: set, timeout: float) -> None:
    """把连接建立时服务端主动推送的帧读完（避免后面误判）。"""
    deadline = time.monotonic() + timeout
    seen = set()
    while time.monotonic() < deadline and seen != types:
        logical = await wait_logical(sock, assembler, max(0.1, deadline - time.monotonic()))
        if logical is None:
            continue
        msg_type = logical[0]
        if msg_type in types:
            seen.add(msg_type)


# ---------------------------------------------------------------- 入口
def main() -> int:
    parser = argparse.ArgumentParser(description="BtCAM 模块1（DXF 导入与修复）验收测试")
    parser.add_argument("--url", default="", help="后端地址；不填则只跑算法部分")
    args = parser.parse_args()

    print("########## A. 修复算法 ##########")
    run_algorithm_checks()

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
