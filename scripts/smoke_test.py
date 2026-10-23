"""端到端冒烟测试：REST 接口 + WS 二进制帧 + 异步任务 + 加工文件导出。

先启动后端，再执行：
    backend\\.venv\\Scripts\\python.exe scripts\\smoke_test.py
    backend\\.venv\\Scripts\\python.exe scripts\\smoke_test.py --url http://127.0.0.1:8975

覆盖链路：
    1. GET  /api/health          后端与几何/排料引擎探活
    2. POST /api/demo            载入内置示例图形（写入 SQLite 并广播）
    3. WS   HELLO                -> HELLO_ACK + GEOMETRY_DOC（BGC1 二进制解析校验）
    4. POST /api/toolpath        投递刀路任务 -> 等 TOOLPATH_PREVIEW 帧（含二进制块）
    5. POST /api/nest            投递排料任务 -> 等 NEST_RESULT 帧
    6. POST /api/export          NC / PLT / CUT 三种格式落盘与文件头校验

退出码：0 全部通过；1 存在失败项（便于 CI / 打包脚本串联）。
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

# 允许直接以 scripts/smoke_test.py 运行：把 backend 加入 import 路径
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import websockets  # noqa: E402

from app.core import protocol  # noqa: E402
from app.core.protocol import MsgType  # noqa: E402

# 结果收集：(名称, 是否通过, 说明)
CHECKS: List[Tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, ok, detail))
    mark = "[OK]  " if ok else "[FAIL]"
    print("  %s %s%s" % (mark, name, ("  " + detail) if detail else ""))
    return ok


# ---------------------------------------------------------------- HTTP
def http_json(method: str, url: str, body: Optional[Dict[str, Any]] = None,
              timeout: float = 60.0) -> Any:
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


# ---------------------------------------------------------------- WS 帧
def split_frame(raw: bytes) -> Tuple[int, int, Dict[str, Any], bytes]:
    """把一条 WS 消息拆成 (msgType, seq, meta, payload)；协议头定长 16 字节。"""
    msg_type, seq, meta_len, pay_len, total = protocol.decode_header(raw)
    if len(raw) < total:
        raise RuntimeError("帧被截断: 期望 %d 字节，实际 %d" % (total, len(raw)))
    meta = protocol.decode_meta(raw[protocol.HEADER_SIZE: protocol.HEADER_SIZE + meta_len])
    payload = raw[protocol.HEADER_SIZE + meta_len: total]
    return msg_type, seq, meta, payload


async def recv_logical(sock, assembler, timeout: float):
    """收一条 WS 消息并返回逻辑帧；CHUNK 未凑齐时返回 None。"""
    raw = await asyncio.wait_for(sock.recv(), timeout=timeout)
    if isinstance(raw, str):
        raise RuntimeError("服务端返回文本帧，违反'只走二进制'约定")
    msg_type, seq, meta, payload = split_frame(raw)
    return assembler.feed(msg_type, meta, payload, seq)


async def wait_frame(sock, assembler, want: int, timeout: float = 180.0) -> Tuple[Dict[str, Any], bytes]:
    """持续收帧直到出现目标类型；期间打印进度帧，任务失败立即抛错。"""
    deadline = time.monotonic() + timeout
    while True:
        remain = deadline - time.monotonic()
        if remain <= 0:
            raise TimeoutError("等待帧 0x%02X 超时（%.0fs）" % (want, timeout))

        logical = await recv_logical(sock, assembler, remain)
        if logical is None:          # 分片还没凑齐，继续收
            continue
        msg_type, meta, payload, _seq = logical

        if msg_type == MsgType.TASK_FAILED:
            raise RuntimeError("任务失败: %s" % meta.get("message", meta))
        if msg_type == MsgType.TASK_PROGRESS:
            pct = float(meta.get("progress", 0.0)) * 100
            print("         进度 %5.1f%%  %s" % (pct, meta.get("message") or meta.get("status") or ""))
            continue
        if msg_type == want:
            return meta, payload
        # 其它类型的帧（例如别的任务广播）跳过


# ---------------------------------------------------------------- 主流程
async def run(base: str) -> None:
    ws_url = base.replace("https://", "wss://").replace("http://", "ws://").rstrip("/") + "/ws"

    print("== 1. REST 探活 ==")
    health = http_json("GET", base + "/api/health")
    check("health", health.get("status") == "ok",
          "geom=%s nest=%s workers=%s" % (health.get("geometryEngine"),
                                          health.get("nestingEngine"),
                                          health.get("workers")))

    print("== 2. 载入示例图形 ==")
    demo = http_json("POST", base + "/api/demo", {})
    total_polys = int(demo.get("polylines", 0))
    check("demo", total_polys > 0, "%d 条轮廓 bbox=%s" % (total_polys, demo.get("bbox")))

    async with websockets.connect(ws_url, max_size=256 * 1024 * 1024) as sock:
        # 与服务端分片保持对称：CHUNK 帧在这里重组回逻辑帧
        assembler = protocol.ChunkAssembler()
        print("== 3. WS 握手与图形同步 ==")
        await sock.send(protocol.encode_frame(MsgType.HELLO, {"clientVersion": "smoke-test"}))
        meta, _ = await wait_frame(sock, assembler, MsgType.HELLO_ACK)
        check("hello_ack", meta.get("protocol") == protocol.PROTOCOL_VERSION,
              "protocol=%s server=%s" % (meta.get("protocol"), meta.get("server")))

        meta, payload = await wait_frame(sock, assembler, MsgType.GEOMETRY_DOC)
        polylines = protocol.decode_polylines(payload)
        declared = int(meta.get("polylineCount", -1))
        pts = sum(p.point_count for p in polylines)
        check("geometry_doc", len(polylines) == declared and len(polylines) > 0,
              "%d 条 / %d 点 / %d 字节" % (len(polylines), pts, len(payload)))

        print("== 4. 异步任务：刀路 ==")
        accepted = http_json("POST", base + "/api/toolpath",
                             {"depth": 6.0, "passDepth": 2.0, "toolDiameter": 6.0})
        print("         taskId=%s kind=%s" % (accepted.get("taskId"), accepted.get("kind")))
        meta, payload = await wait_frame(sock, assembler, MsgType.TOOLPATH_PREVIEW)
        preview = protocol.decode_polylines(payload)
        stats = meta.get("stats", {})
        cut_len = float(stats.get("cutLengthMm", 0.0))
        check("toolpath", len(preview) > 0 or cut_len > 0,
              "%d 段预览 / %d 条运动 / 切削 %.1fmm / 预估 %.2f 分 / 用时 %sms" % (
                  len(preview), int(stats.get("moveCount", 0)), cut_len,
                  float(stats.get("estimatedMin", 0.0)), meta.get("elapsedMs")))

        print("== 5. 异步任务：智能排料 ==")
        accepted = http_json("POST", base + "/api/nest",
                             {"sheetWidth": 1220.0, "sheetHeight": 2440.0, "gap": 5.0})
        print("         taskId=%s kind=%s" % (accepted.get("taskId"), accepted.get("kind")))
        meta, payload = await wait_frame(sock, assembler, MsgType.NEST_RESULT)
        placed = protocol.decode_polylines(payload)
        nstats = meta.get("stats", {})
        check("nest", len(placed) > 0,
              "引擎=%s 零件=%s 板材=%s 利用率=%.1f%%" % (
                  nstats.get("engine"), nstats.get("partCount"),
                  nstats.get("sheetCount"), float(nstats.get("utilization", 0.0)) * 100))

    print("== 6. 导出加工文件 ==")
    expected = {"nc": ("%", "G21"), "plt": ("IN;", "PD"), "cut": ("VS", "FS")}
    for fmt, needles in expected.items():
        res = http_json("POST", base + "/api/export", {"format": fmt, "name": "SMOKE"})
        path = Path(res["path"])
        text = path.read_text(encoding="ascii", errors="replace") if path.exists() else ""
        ok = path.exists() and path.stat().st_size > 0 and all(n in text for n in needles)
        check("export_%s" % fmt, ok, "%s (%d 字节)" % (path.name, path.stat().st_size if path.exists() else 0))

    # ------------------------------------------------------------ 汇总
    failed = [c for c in CHECKS if not c[1]]
    print("\n== 结果：%d 项通过，%d 项失败 ==" % (len(CHECKS) - len(failed), len(failed)))
    if failed:
        for name, _ok, detail in failed:
            print("  失败: %s %s" % (name, detail))
        raise SystemExit(1)


def main() -> int:
    parser = argparse.ArgumentParser(description="BtCAM 后端端到端冒烟测试")
    parser.add_argument("--url", default="http://127.0.0.1:8975", help="后端地址")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.url.rstrip("/")))
    except SystemExit:
        raise
    except Exception as exc:  # 连接失败 / 断言失败 / 超时
        print("\n[ERROR] %s: %s" % (type(exc).__name__, exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
