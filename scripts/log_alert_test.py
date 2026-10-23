"""模块 9 验收：运行日志与告警（环形缓冲 / 接口 / ALERT 帧）。

两种跑法：

    # 1) 只测服务层（不需要后端，秒级）
    backend\\.venv\\Scripts\\python.exe scripts\\log_alert_test.py

    # 2) 服务层 + 接口全链路（后端需先启动在 8975）
    backend\\.venv\\Scripts\\python.exe scripts\\log_alert_test.py --url http://127.0.0.1:8975

覆盖的验收项：

    A1 环形缓冲      满容量淘汰最旧、seq 自增、容量可调
    A2 过滤查询      最低级别 / 关键词 / 来源前缀 / 增量游标
    A3 告警广播      warning 显式推送、error 自动推送、3 秒同文案去重
    A4 logging 接入  LogRecord 结构化入环，异常堆栈进 detail，可跳过入环
    A5 进度日志      阶段文案变化才记录，任务中文名 + 百分比
    A6 校验告警      自相交/未闭合汇总成一条 warning（弹窗文案）

    B1 查询接口      GET /api/logs 结构、升序、limit
    B2 前端上报      POST /api/logs/client 入环（error 级）
    B3 过滤与增量    level / after 参数
    B4 参数校验      非法级别 400 / 空消息 422
    B5 清空缓冲      DELETE 后同关键词查不到
    B6 ALERT 帧      WS 收到 0x50，meta 即日志条目（error 自动推送）

退出码：0 全部通过；1 存在失败项。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

CHECKS: List[Tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, ok, detail))
    print("  %s %s%s" % ("[OK]  " if ok else "[FAIL]", name, ("  " + detail) if detail else ""))
    return ok


# ---------------------------------------------------------------- 服务层
def store_tests() -> None:
    import logging

    from app.core.hub import hub
    from app.core.log_store import (
        LogRingHandler,
        LogStore,
        log_store,
        normalize_level,
        task_label,
    )
    from app.core.protocol import MsgType

    print("== A. 服务层：缓冲 / 过滤 / 广播 ==")

    # --- A1 环形缓冲：容量 50 写 55 条，只留最近 50 条
    store = LogStore(capacity=50)
    for index in range(54):
        store.append("info", "第 %d 条" % index, source="task", task_id="t%d" % index)
    store.append("error", "刀路计算失败", source="task", broadcast=False)
    snap = store.query(limit=100)
    ids = [row["id"] for row in snap["logs"]]
    check("A1 环形缓冲",
          len(ids) == 50 and ids[0] == 6 and ids[-1] == 55 and snap["seq"] == 55
          and snap["capacity"] == 50 and snap["logs"][-1]["level"] == "error"
          and snap["logs"][0]["message"] == "第 5 条",
          "%d 条 / seq=%d / 容量=%d" % (len(ids), snap["seq"], snap["capacity"]))

    # --- A2 过滤查询：级别 / 关键词 / 来源前缀 / 增量
    only_error = store.query(level="error")["logs"]
    by_word = store.query(keyword="第 53 条")["logs"]
    by_source = store.query(source="tas")["logs"]
    by_after = store.query(after=54)["logs"]
    check("A2 过滤查询",
          len(only_error) == 1 and only_error[0]["level"] == "error"
          and len(by_word) == 1 and by_word[0]["id"] == 54
          and len(by_source) == 50 and all(r["source"].startswith("tas") for r in by_source)
          and [r["id"] for r in by_after] == [55],
          "error %d / 关键词 %d / 来源 %d / 增量 %d" % (
              len(only_error), len(by_word), len(by_source), len(by_after)))

    # --- A3 广播：warning 需显式 alert，error 自动推，同文案 3 秒去重
    calls: List[Tuple[int, Dict[str, Any]]] = []
    original = hub.broadcast_threadsafe

    def fake_broadcast(msg_type: Any, meta: Optional[Dict[str, Any]] = None,
                       payload: bytes = b"") -> None:
        calls.append((int(msg_type), dict(meta or {})))

    hub.broadcast_threadsafe = fake_broadcast  # type: ignore[assignment]
    try:
        store.alert("warning", "图形校验发现 1 处异常", source="import")
        store.alert("warning", "图形校验发现 1 处异常", source="import")   # 去重
        store.alert("error", "任务失败 刀路生成 id=abc", source="task")
        store.append("error", "自动广播的错误")
        store.append("info", "普通信息安静入环")
    finally:
        hub.broadcast_threadsafe = original  # type: ignore[assignment]
    check("A3 告警广播",
          len(calls) == 3 and calls[0][0] == MsgType.ALERT == 0x50
          and calls[0][1]["level"] == "warning" and calls[1][1]["level"] == "error"
          and calls[2][1]["message"] == "自动广播的错误",
          "%d 次推送（warning 去重 1 次，info 不推）" % len(calls))

    # --- A4 logging 接入：结构化字段 + 异常堆栈 + 跳过标记
    ring = LogStore(capacity=50)
    handler = LogRingHandler(ring)
    test_logger = logging.getLogger("btcam.test.log")
    test_logger.setLevel(logging.DEBUG)
    # 不往上抛：否则 root 的 lastResort 会把模拟异常打到 stderr，干扰输出
    test_logger.propagate = False
    test_logger.addHandler(handler)
    try:
        test_logger.info("普通记录 %s", "参数")
        try:
            raise RuntimeError("模拟计算失败")
        except RuntimeError:
            test_logger.exception("带堆栈的记录")
        test_logger.warning("跳过入环", extra={"_btcam_skip_ring": True})
    finally:
        test_logger.removeHandler(handler)
    rows = ring.query(limit=10)["logs"]
    check("A4 logging 接入",
          len(rows) == 2 and rows[0]["message"] == "普通记录 参数"
          and rows[1]["source"] == "btcam.test.log" and rows[1]["level"] == "error"
          and "Traceback" in (rows[1]["detail"] or "")
          and all(row["message"] != "跳过入环" for row in rows),
          "2 条入环，异常堆栈 %d 字符" % len(rows[1]["detail"] or ""))

    # --- A5 进度日志：同一文案只记一条，带任务中文名与百分比
    from app.core.tasks import ProgressReporter

    log_store.clear()
    reporter = ProgressReporter("task-9", None, None, "toolpath")  # type: ignore[arg-type]
    reporter(0.05, "粗切第一层")
    reporter(0.08, "粗切第一层")   # 文案未变：不重复记
    reporter(0.50, "精切")
    progress_rows = log_store.query(source="task")["logs"]
    check("A5 进度日志",
          len(progress_rows) == 2
          and progress_rows[0]["message"] == "刀路生成 5% · 粗切第一层"
          and progress_rows[1]["message"] == "刀路生成 50% · 精切"
          and progress_rows[0]["taskId"] == "task-9"
          and task_label("layout_nest") == "自动排料"
          and normalize_level("WARN") == "warning",
          "%d 条（文案变化才记）" % len(progress_rows))

    # --- A6 校验告警：多处异常汇总成一条 warning
    from app.geometry.repair import ContourIssue
    from app.services.import_service import _alert_issues

    log_store.clear()
    _alert_issues([
        ContourIssue("self_intersection", "线段在 (10.0, 5.0) 处自相交", 7, "warning", (10.0, 5.0)),
        ContourIssue("open_contour", "轮廓未闭合，缺口 0.8mm", 8, "warning", (1.0, 2.0)),
    ])
    alert_rows = log_store.query(limit=5)["logs"]
    entry = alert_rows[0] if alert_rows else {}
    check("A6 校验告警",
          entry.get("level") == "warning" and entry.get("source") == "import"
          and "图形校验发现 2 处异常" in entry.get("message", "")
          and "自相交 1 处、未闭合 1 处" in entry.get("message", "")
          and "轮廓 #7" in (entry.get("detail") or ""),
          entry.get("message", ""))


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
                     timeout: float = 60.0) -> Tuple[Dict[str, Any], bytes]:
    import asyncio

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


def rest_tests(base: str) -> None:
    import asyncio

    import websockets

    from app.core import protocol
    from app.core.protocol import MsgType

    print("== B. 接口 / ALERT 帧 ==")
    marker = "mod9-%d" % int(time.time() * 1000)

    # --- B1 查询接口：结构齐全、按 id 升序、limit 生效
    listing = http_json("GET", base + "/api/logs?limit=5")
    ids = [row["id"] for row in listing["logs"]]
    check("B1 查询接口",
          {"logs", "total", "seq", "capacity", "file"} <= set(listing)
          and ids == sorted(ids) and len(ids) <= 5
          and listing["capacity"] >= 100,
          "total=%d seq=%d capacity=%d" % (
              listing["total"], listing["seq"], listing["capacity"]))

    # --- B2 前端上报：error 级入环并带回 detail
    report = http_json("POST", base + "/api/logs/client", {
        "level": "error",
        "message": "%s 前端渲染失败" % marker,
        "detail": "TypeError: x is undefined\n    at Viewport",
        "kind": "frontend",
        "source": "frontend",
    })
    found = http_json("GET", base + "/api/logs?keyword=" + quote(marker) + "&limit=50")
    entry = next((row for row in found["logs"] if marker in row["message"]), None)
    check("B2 前端上报",
          bool(report.get("ok")) and report.get("level") == "error"
          and entry is not None and entry["kind"] == "frontend"
          and entry["source"] == "frontend"
          and (entry["detail"] or "").startswith("TypeError")
          and entry["id"] == report["id"],
          "#%d %s" % (report.get("id", 0), entry["message"] if entry else "未入库"))

    # --- B3 过滤与增量：level 最低级别、after 游标
    errors = http_json("GET", base + "/api/logs?level=error&limit=100")
    only_high = all(row["level"] in ("error", "critical") for row in errors["logs"])
    newer = http_json("GET", base + "/api/logs?after=%d&limit=100" % entry["id"])
    check("B3 过滤与增量",
          only_high and all(row["id"] > entry["id"] for row in newer["logs"]),
          "error+ %d 条 / after 之后 %d 条" % (len(errors["logs"]), len(newer["logs"])))

    # --- B4 参数校验
    bad_level = http_error_code("GET", base + "/api/logs?level=loud")
    empty_message = http_error_code("POST", base + "/api/logs/client",
                                    {"level": "error", "message": ""})
    check("B4 参数校验", bad_level == 400 and empty_message == 422,
          "非法级别 %d / 空消息 %d" % (bad_level, empty_message))

    # --- B5 清空缓冲：只清内存，关键词查不到即可（磁盘日志保留）
    pending = "%s 待清空" % marker
    http_json("POST", base + "/api/logs/client",
              {"level": "warning", "message": pending, "source": "frontend"})
    before = http_json("GET", base + "/api/logs?keyword=" + quote(pending))
    cleared = http_json("DELETE", base + "/api/logs")
    after = http_json("GET", base + "/api/logs?keyword=" + quote(pending))
    check("B5 清空缓冲",
          before["total"] >= 1 and cleared["cleared"] >= 1 and after["total"] == 0,
          "清空 %d 条，容量 %d" % (cleared["cleared"], cleared["capacity"]))

    # --- B6 ALERT 帧：error 级前端上报自动推给所有连接
    ws_url = base.replace("https://", "wss://").replace("http://", "ws://").rstrip("/") + "/ws"

    async def alert_roundtrip() -> Tuple[Dict[str, Any], Dict[str, Any]]:
        async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as sock:
            assembler = protocol.ChunkAssembler()
            await sock.send(protocol.encode_frame(
                MsgType.HELLO, {"clientVersion": "log-alert-test"}))
            await wait_frame(sock, assembler, MsgType.HELLO_ACK)

            posted = http_json("POST", base + "/api/logs/client", {
                "level": "error",
                "message": "%s ALERT 帧校验" % marker,
                "kind": "geometry_check",
                "source": "import",
            })
            meta, _payload = await wait_frame(sock, assembler, MsgType.ALERT)
            return posted, meta

    posted, alert_meta = asyncio.run(alert_roundtrip())
    check("B6 ALERT 帧",
          alert_meta.get("id") == posted["id"]
          and alert_meta.get("level") == "error"
          and alert_meta.get("source") == "import"
          and marker in alert_meta.get("message", ""),
          "0x%02X #%s %s" % (MsgType.ALERT, alert_meta.get("id"),
                             alert_meta.get("message", "")))

    # 收尾：把测试写入的条目清掉，避免污染后续人工查看
    try:
        http_json("DELETE", base + "/api/logs")
    except RuntimeError:
        pass


# ---------------------------------------------------------------- 入口
def main() -> int:
    parser = argparse.ArgumentParser(description="BtCAM 模块 9 日志与告警验收测试")
    parser.add_argument("--url", default="", help="后端地址；给定时追加接口测试")
    args = parser.parse_args()

    try:
        store_tests()
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
