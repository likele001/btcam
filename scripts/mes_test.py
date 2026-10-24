"""模块 12 MES 对接 —— 后端控制台验收脚本。

两种跑法：

    # 1) 只测服务层（不需要 CAM 后端；脚本自带假 MES 服务，秒级）
    backend\\.venv\\Scripts\\python.exe scripts\\mes_test.py

    # 2) 服务层 + 接口全链路（CAM 后端需先启动在 8975）
    backend\\.venv\\Scripts\\python.exe scripts\\mes_test.py --url http://127.0.0.1:8975

验收主线（对应需求"模拟 MES 接口，拉取工单、回传结果"）：

    A 系列  配置：校验、密钥留空沿用、掩码、camelCase 往返、厂商默认路径
    B 系列  客户端：鉴权头、limit 参数、5xx 退避重试、401 不重试、错误详情
    C 系列  拉单：字段归一化（orderNo/orderId、字符串零件）、幂等 upsert、跳过无单号
    D 系列  回传：用料统计、加工文件 multipart、幂等键、归档状态、日志落库
    E 系列  离线：开关关闭时一个包都不发、自动推送预检直接返回
    F 系列  自动推送：导出后的 daemon 线程真把工单推给 MES
    G 系列  HTTP 接口全链路（--url 时才跑）

注意：脚本会临时改写本机的 MES 配置（指向脚本内的假 MES），结束时恢复原值，
并删掉所有 `MES-TEST-*` 测试工单与本次产生的日志。

退出码：0 全部通过；1 存在失败项。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

# 允许直接以 scripts/mes_test.py 运行：把 backend 与 venv 依赖加入 import 路径
BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR / ".venv" / "Lib" / "site-packages"))

from app.config import settings as app_settings           # noqa: E402
from app.mes import service as mes_service                # noqa: E402
from app.mes import settings as mes_settings              # noqa: E402
from app.mes.client import MesClient, MesError            # noqa: E402
from app.mes.settings import MesConfig, MesConfigError    # noqa: E402
from app.models import db                                 # noqa: E402

CHECKS: List[Tuple[str, bool, str]] = []

TEST_KEY = "sk-test-0123456789abcdef"
REMOTE_OK = "MES-TEST-0001"        # 有本地工单、会被回传
REMOTE_2 = "MES-TEST-0002"         # 只入缓存、不回传
REMOTE_BAD = "MES-TEST-BAD"        # 缺少单号，应被跳过


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, ok, detail))
    print("  %s %s%s" % ("[OK]  " if ok else "[FAIL]", name, ("  " + detail) if detail else ""))
    return ok


# ---------------------------------------------------------------- 假 MES 服务
class FakeMesState:
    """假 MES 的共享状态：鉴权密钥、工单数据、收到的请求与归档。"""

    def __init__(self) -> None:
        self.api_key = TEST_KEY
        self.orders: List[Dict[str, Any]] = [
            {
                "orderNo": REMOTE_OK,
                "name": "测试工单 A",
                "customer": "上海某家具厂",
                "material": "多层板",
                "thickness": 18.0,
                "dueDate": "2026-11-01",
                "parts": [
                    "侧板A",
                    {"name": "背板", "qty": 2, "material": "多层板",
                     "thickness": 18.0, "width": 600.0, "height": 400.0,
                     "partId": "P-02"},
                ],
            },
            {
                "orderId": REMOTE_2,
                "title": "测试工单 B",
                "items": [{"partName": "层板", "quantity": 4, "mat": "密度板"}],
            },
            {
                # 没有单号：应当被判为"认不出"并跳过，不能被乱拉进来
                "name": "缺单号的脏数据",
                "parts": [{"name": "未知零件"}],
            },
        ]
        self.requests: List[Dict[str, Any]] = []
        self.archives: List[Dict[str, Any]] = []
        self.fail_pull = 0          # >0 时下一次拉单直接 503
        self.fail_archive = 0       # >0 时下一次回传直接 503
        self.last_limit = 0
        self.auth_headers: List[Tuple[str, str]] = []


STATE = FakeMesState()


def _parse_multipart(headers: Any, body: bytes) -> Tuple[Optional[Any], List[Tuple[str, str, int]]]:
    """极简 multipart 解析：只要能取出 payload JSON 和文件清单就够验收用。"""
    ctype = headers.get("Content-Type", "") or ""
    if not ctype.startswith("multipart/form-data"):
        return None, []
    boundary = ctype.split("boundary=", 1)[-1].strip().strip('"')
    payload: Optional[Any] = None
    files: List[Tuple[str, str, int]] = []
    for chunk in body.split(("--" + boundary).encode("utf-8")):
        if not chunk.strip(b"\r\n-"):
            continue
        head, _, content = chunk.partition(b"\r\n\r\n")
        text = head.decode("utf-8", "replace")
        name = ""
        filename = ""
        for line in text.splitlines():
            if line.lower().startswith("content-disposition"):
                for part in line.split(";"):
                    part = part.strip()
                    if part.startswith("name="):
                        name = part[5:].strip('"')
                    elif part.startswith("filename="):
                        filename = part[9:].strip('"')
        content = content.rstrip(b"\r\n")
        if filename:
            files.append((name, filename, len(content)))
        elif name == "payload":
            try:
                payload = json.loads(content.decode("utf-8"))
            except ValueError:
                payload = None
    return payload, files


class FakeMesHandler(BaseHTTPRequestHandler):
    server_version = "FakeMES/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音：测试输出要干净
        return

    # ------------------------------------------------------------ 工具
    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self) -> bool:
        raw = self.headers.get("Authorization", "") or ""
        token = raw[7:].strip() if raw.lower().startswith("bearer ") else ""
        header_key = self.headers.get("X-Api-Key", "") or ""
        STATE.auth_headers.append((raw, header_key))
        return bool(token) and token == STATE.api_key and header_key == token

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    # ------------------------------------------------------------ GET
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        parsed = urlsplit(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        STATE.requests.append({"method": "GET", "path": path, "query": query})

        if path in ("/api/v1/ping", "/api/ping"):
            if not self._auth_ok():
                self._send(401, {"detail": "密钥无效"})
                return
            self._send(200, {"ok": True, "vendor": "cenkor", "server": "fake-mes"})
            return

        if path in ("/api/v1/workorders", "/api/workorders"):
            if not self._auth_ok():
                self._send(401, {"detail": "密钥无效"})
                return
            STATE.last_limit = int((query.get("limit") or ["0"])[0] or 0)
            if STATE.fail_pull > 0:
                STATE.fail_pull -= 1
                self._send(503, {"detail": "MES 服务暂时不可用"})
                return
            limit = STATE.last_limit or len(STATE.orders)
            self._send(200, {"ok": True, "orders": STATE.orders[:limit]})
            return

        self._send(404, {"detail": "接口不存在：%s" % path})

    # ------------------------------------------------------------ POST
    def do_POST(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = parsed.path
        STATE.requests.append({"method": "POST", "path": path})
        if not self._auth_ok():
            self._send(401, {"detail": "密钥无效"})
            return

        if path.startswith("/api/v1/workorders/") or path.startswith("/api/workorders/"):
            if not path.endswith("/archive"):
                self._send(404, {"detail": "接口不存在：%s" % path})
                return
            remote_id = path.split("/")[-2]
            body = self._body()
            ctype = self.headers.get("Content-Type", "") or ""
            if ctype.startswith("multipart/form-data"):
                payload, files = _parse_multipart(self.headers, body)
            else:
                try:
                    payload = json.loads(body.decode("utf-8") or "{}")
                except ValueError:
                    payload = None
                files = []
            if STATE.fail_archive > 0:
                STATE.fail_archive -= 1
                self._send(503, {"detail": "归档服务暂时不可用"})
                return
            STATE.archives.append({
                "remoteId": remote_id,
                "payload": payload,
                "files": files,
                "contentType": ctype,
                "at": time.time(),
            })
            self._send(200, {"ok": True, "archived": True,
                             "remoteId": remote_id, "files": len(files)})
            return

        self._send(404, {"detail": "接口不存在：%s" % path})


class FakeMes:
    """假 MES 的进程内实例：起/停 + 基地址。"""

    def __init__(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeMesHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       name="fake-mes", daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return "http://%s:%d" % (host, port)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# ---------------------------------------------------------------- 测试夹具
def _cleanup() -> None:
    """删掉本次测试造出来的 MES 工单与日志。"""
    try:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM mes_order WHERE remote_id LIKE ?", ("MES-TEST-%",))
            conn.execute("DELETE FROM mes_push_log WHERE message LIKE ?", ("%MES-TEST-%",))
            conn.execute("DELETE FROM work_order_file WHERE order_id IN"
                         " (SELECT id FROM work_order WHERE code LIKE ?)", ("MES-TEST-%",))
            conn.execute("DELETE FROM work_order WHERE code LIKE ?", ("MES-TEST-%",))
    except Exception:
        pass


def _save_config(base_url: str, enabled: bool = True, auto_push: bool = False,
                 retries: int = 2, include_files: bool = True) -> MesConfig:
    return mes_settings.save({
        "enabled": enabled,
        "vendor": "cenkor",
        "baseUrl": base_url,
        "apiKey": TEST_KEY,
        "timeout": 5.0,
        "retries": retries,
        "autoPush": auto_push,
        "includeFiles": include_files,
        "paths": {},
    })


def _make_work_order(project_id: int, code: str, file_count: int = 2) -> int:
    """造一张本地工单 + 若干加工文件，用于验证回传内容。"""
    stats = {
        "partCount": 12, "polylineCount": 34, "pointCount": 900,
        "placementCount": 12, "utilization": 0.7321,
        "sheetWidth": 1220.0, "sheetHeight": 2440.0,
    }
    export_dir = app_settings.data_dir / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    with db.get_conn() as conn:
        order_id = db.insert_work_order(
            conn, project_id, "MES 测试工单 %s" % code, code, "模块 12 验收用",
            "{}", json.dumps(stats, ensure_ascii=False))
        for index in range(file_count):
            path = export_dir / ("%s_%d.nc" % (code, index + 1))
            path.write_bytes(("G0 X0 Y0\nM30\n; %s\n" % code).encode("utf-8"))
            db.insert_work_order_file(
                conn, order_id, str(path), "nc", "weihong_nc",
                int(path.stat().st_size), index)
    return order_id


# ---------------------------------------------------------------- A 配置
def test_settings(prev_raw: Optional[str]) -> None:
    print("\n[A] 配置：校验 / 密钥 / 往返 / 厂商路径")
    default = MesConfig()
    check("A1 默认是关闭状态", default.enabled is False and default.vendor == "cenkor")
    public = mes_settings.public_payload(default)
    check("A2 默认公钥掩码为空", public["apiKey"] == "" and public["hasApiKey"] is False)
    check("A3 厂商清单三家 + 默认路径齐全",
          [item["vendor"] for item in public["vendors"]] == ["cenkor", "lightmes", "custom"]
          and public["defaults"]["cenkor"]["archive"].endswith("/archive"))

    for name, payload, expect in (
        ("A4 非法 vendor", {"vendor": "foo"}, "vendor"),
        ("A5 非法地址", {"vendor": "cenkor", "baseUrl": "ftp://x"}, "http(s)"),
        ("A6 启用但没地址", {"enabled": True, "baseUrl": ""}, "服务地址"),
        ("A7 超时越界", {"timeout": 120}, "超时"),
        ("A8 重试越界", {"retries": 9}, "重试"),
    ):
        try:
            mes_settings.build(payload, default)
        except MesConfigError as exc:
            check(name, expect in str(exc), str(exc))
        else:
            check(name, False, "没有抛出 MesConfigError")

    saved = _save_config("http://127.0.0.1:9/")
    check("A9 保存后启用并落掩码", saved.enabled and mes_settings.mask_key(TEST_KEY).startswith("sk****"),
          mes_settings.mask_key(TEST_KEY))
    # 留空 = 不修改密钥
    kept = mes_settings.save({"enabled": True, "vendor": "cenkor",
                              "baseUrl": "http://127.0.0.1:9", "apiKey": "",
                              "timeout": 5.0, "retries": 1, "autoPush": True,
                              "includeFiles": False, "paths": {}})
    check("A10 密钥留空沿用旧值", kept.api_key == TEST_KEY)
    check("A11 camelCase 往返（autoPush/includeFiles）",
          kept.auto_push is True and kept.include_files is False)
    loaded = mes_settings.load()
    check("A12 读回与保存一致", loaded.base_url == "http://127.0.0.1:9"
          and loaded.retries == 1 and loaded.api_key == TEST_KEY)
    check("A13 对外 payload 不含明文密钥",
          mes_settings.public_payload(loaded)["apiKey"] == ""
          and mes_settings.public_payload(loaded)["hasApiKey"] is True)
    if prev_raw is not None:
        # 还原成脚本开始前的配置（后面的用例会再次覆盖，这里只保证有值可读）
        with db.get_conn() as conn:
            db.set_setting(conn, mes_settings.SETTING_KEY, prev_raw)


# ---------------------------------------------------------------- B 客户端
def test_client(base_url: str) -> None:
    print("\n[B] 客户端：鉴权 / 参数 / 重试 / 错误详情")
    config = _save_config(base_url, retries=2)
    client = MesClient(config)

    response, status, attempts = client.ping()
    check("B1 ping 成功", status == 200 and bool(response.get("ok")) and attempts == 1)
    check("B2 同时带 Bearer 与 X-Api-Key",
          STATE.auth_headers[-1][0].startswith("Bearer ") and STATE.auth_headers[-1][1] == TEST_KEY)

    client.list_orders(7)
    check("B3 limit 拼进查询串", STATE.last_limit == 7, "limit=%d" % STATE.last_limit)

    STATE.fail_pull = 2
    response, status, attempts = client.list_orders(10)
    check("B4 503 指数退避重试后成功", status == 200 and attempts == 3,
          "attempts=%d" % attempts)

    bad = MesConfig.from_dict(config.to_dict(public=False))
    bad.api_key = "sk-wrong"
    try:
        MesClient(bad).ping()
    except MesError as exc:
        check("B5 401 不重试", exc.status_code == 401 and exc.attempts == 1, str(exc))
        check("B6 错误详情抠出人话", "密钥无效" in str(exc), str(exc))
    else:
        check("B5 401 不重试", False, "没有抛 MesError")

    empty = MesConfig.from_dict({**config.to_dict(public=False), "paths": {
        "ping": "", "orders": "", "archive": ""}, "vendor": "custom"})
    try:
        MesClient(empty).request("GET", "")
    except MesError as exc:
        check("B7 路径未配置时报错", "未配置" in str(exc), str(exc))
    else:
        check("B7 路径未配置时报错", False, "没有抛 MesError")


# ---------------------------------------------------------------- C 拉单
def test_pull(project_id: int, base_url: str) -> None:
    print("\n[C] 拉单：归一化 / 幂等 / 跳过脏数据")
    _save_config(base_url)
    first = mes_service.pull_orders(project_id, limit=20)
    check("C1 首次拉单新增 2 张", first["created"] == 2 and first["updated"] == 0,
          "created=%d updated=%d" % (first["created"], first["updated"]))
    check("C2 脏数据被跳过并留原因", first["skipped"] == 1 and bool(first["problems"]),
          first["problems"][0] if first["problems"] else "")

    with db.get_conn() as conn:
        order = db.find_mes_order_by_remote(conn, REMOTE_OK)
    check("C3 orderNo 归一化", order is not None and order["remoteId"] == REMOTE_OK)
    parts = (order or {}).get("parts") or []
    check("C4 字符串零件被接受", parts and parts[0]["name"] == "侧板A" and parts[0]["qty"] == 1)
    check("C5 字典零件字段齐全",
          len(parts) > 1 and parts[1]["qty"] == 2 and parts[1]["material"] == "多层板"
          and abs(parts[1]["width"] - 600.0) < 1e-6)
    check("C6 零件数按数量累加", (order or {}).get("partCount") == 3,
          str((order or {}).get("partCount")))

    second = mes_service.pull_orders(project_id, limit=20)
    check("C7 重复拉单只更新不新增", second["created"] == 0 and second["updated"] == 2,
          "created=%d updated=%d" % (second["created"], second["updated"]))
    with db.get_conn() as conn:
        rows = db.list_mes_orders(conn, limit=100)
        test_rows = [row for row in rows if row["remoteId"].startswith("MES-TEST-")]
    check("C8 库里仍然只有 2 条", len(test_rows) == 2, "rows=%d" % len(test_rows))

    with db.get_conn() as conn:
        listing = mes_service.list_orders(conn, limit=100)
    check("C9 列表带计数", listing["count"] >= 2 and "archivedCount" in listing)


# ---------------------------------------------------------------- D 回传
def test_push(project_id: int, base_url: str, work_order_id: int) -> None:
    print("\n[D] 回传：用料统计 / 文件 / 幂等键 / 归档状态 / 日志")
    _save_config(base_url)

    before = len(STATE.archives)
    with db.get_conn() as conn:
        order = db.find_mes_order_by_remote(conn, REMOTE_OK)
    result = mes_service.push_order(project_id, int(order["id"]), work_order_id)

    check("D1 回传成功并返回状态码", result["ok"] and result["statusCode"] == 200)
    check("D2 关联到本地工单", result["workOrderId"] == work_order_id)
    check("D3 文件数 / 字节数正确",
          result["fileCount"] == 2 and result["uploadedBytes"] > 0,
          "files=%d bytes=%d" % (result["fileCount"], result["uploadedBytes"]))
    check("D4 走了 multipart（带文件）", len(STATE.archives) == before + 1
          and "multipart/form-data" in STATE.archives[-1]["contentType"])

    payload = (STATE.archives[-1]["payload"] or {})
    usage = payload.get("usage") or {}
    check("D5 用料统计来自本地工单快照",
          usage.get("partCount") == 12 and usage.get("placementCount") == 12
          and abs(float(usage.get("utilization") or 0) - 0.7321) < 1e-6,
          json.dumps(usage, ensure_ascii=False))
    check("D6 用量里带文件统计", usage.get("fileCount") == 2 and usage.get("fileBytes") > 0)
    check("D7 幂等键与来源",
          str(payload.get("idempotencyKey") or "").startswith(REMOTE_OK + "-")
          and payload.get("source") == "BtCAM" and payload.get("status") == "archived")
    work_order = payload.get("workOrder") or {}
    check("D8 工单信息含编号与统计",
          work_order.get("code") == REMOTE_OK and (work_order.get("stats") or {}).get("partCount") == 12)
    check("D9 文件真的上传了",
          len(STATE.archives[-1]["files"]) == 2
          and STATE.archives[-1]["files"][0][1].endswith(".nc"))

    with db.get_conn() as conn:
        order = db.find_mes_order_by_remote(conn, REMOTE_OK)
        logs = db.list_mes_logs(conn, limit=10)
    check("D10 本地标记已归档",
          order["status"] == "archived" and order["archivedAt"] is not None)
    push_logs = [row for row in logs if row["action"] == "push"]
    check("D11 推送写日志（含尝试次数）",
          bool(push_logs) and push_logs[0]["ok"] is True and push_logs[0]["attempts"] >= 1)

    # 关闭文件上传
    _save_config(base_url, include_files=False)
    before = len(STATE.archives)
    result = mes_service.push_order(project_id, int(order["id"]), work_order_id)
    check("D12 关闭文件上传后走 JSON",
          result["fileCount"] == 0 and len(STATE.archives) == before + 1
          and "application/json" in STATE.archives[-1]["contentType"]
          and len(STATE.archives[-1]["files"]) == 0)

    # 工单不存在
    try:
        mes_service.push_order(project_id, int(order["id"]), 99999999)
    except mes_service.MesDataError as exc:
        check("D13 本地工单不存在报错", "不存在" in str(exc), str(exc))
    else:
        check("D13 本地工单不存在报错", False, "没有抛 MesDataError")

    try:
        mes_service.push_order(project_id, 99999999, work_order_id)
    except mes_service.MesDataError as exc:
        check("D14 MES 工单不存在报错", "不存在" in str(exc), str(exc))
    else:
        check("D14 MES 工单不存在报错", False, "没有抛 MesDataError")


# ---------------------------------------------------------------- E 离线
def test_offline(project_id: int, base_url: str, work_order_id: int) -> None:
    print("\n[E] 离线：开关关闭时一个包都不发")
    _save_config(base_url, enabled=False, auto_push=True)
    before = len(STATE.requests)
    try:
        mes_service.pull_orders(project_id, limit=5)
    except mes_service.MesDisabledError as exc:
        check("E1 未启用时拉单被拒", "未启用" in str(exc), str(exc))
    else:
        check("E1 未启用时拉单被拒", False, "没有抛 MesDisabledError")
    check("E2 一个请求都没发出去", len(STATE.requests) == before,
          "requests=%d" % (len(STATE.requests) - before))

    try:
        mes_service.push_order(project_id, 1, work_order_id)
    except mes_service.MesDisabledError:
        check("E3 未启用时回传被拒", True)
    else:
        check("E3 未启用时回传被拒", False, "没有抛 MesDisabledError")

    check("E4 自动推送预检直接返回 False",
          mes_service.auto_push(project_id, work_order_id) is False)


# ---------------------------------------------------------------- F 自动推送
def test_auto_push(project_id: int, base_url: str, work_order_id: int) -> None:
    print("\n[F] 自动推送：导出后的 daemon 线程")
    _save_config(base_url, auto_push=True)
    with db.get_conn() as conn:
        order = db.find_mes_order_by_remote(conn, REMOTE_OK)
        if order is not None:
            conn.execute("UPDATE mes_order SET status='pulled', archived_at=NULL WHERE id=?",
                         (int(order["id"]),))

    before = len(STATE.archives)
    started = mes_service.auto_push(project_id, work_order_id)
    check("F1 预检通过并起了线程", started is True)

    deadline = time.time() + 8.0
    while time.time() < deadline and len(STATE.archives) <= before:
        time.sleep(0.05)
    check("F2 线程真的把工单推了过去", len(STATE.archives) > before,
          "archives=%d" % (len(STATE.archives) - before))
    with db.get_conn() as conn:
        order = db.find_mes_order_by_remote(conn, REMOTE_OK)
    check("F3 推送后自动标记归档", (order or {}).get("status") == "archived")

    # 编号对不上：不应起线程
    with db.get_conn() as conn:
        conn.execute("UPDATE work_order SET code='' WHERE id=?", (int(work_order_id),))
    check("F4 工单编号为空时跳过", mes_service.auto_push(project_id, work_order_id) is False)
    with db.get_conn() as conn:
        conn.execute("UPDATE work_order SET code=? WHERE id=?", (REMOTE_OK, int(work_order_id)))


# ---------------------------------------------------------------- G HTTP
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
        raise RuntimeError("HTTP %s: %s" % (exc.code, detail))


def test_api(base: str, project_id: int, work_order_id: int, base_url: str) -> None:
    print("\n[G] HTTP 接口全链路")
    _save_config(base_url)

    settings_payload = http_json("GET", base + "/api/mes/settings")
    check("G1 读配置：掩码 + 厂商清单",
          settings_payload["apiKey"] == "" and settings_payload["hasApiKey"] is True
          and len(settings_payload["vendors"]) == 3)

    saved = http_json("PUT", base + "/api/mes/settings", {
        "enabled": True, "vendor": "cenkor", "baseUrl": base_url,
        "apiKey": "", "timeout": 5.0, "retries": 2, "autoPush": False,
        "includeFiles": True, "paths": {},
    })
    check("G2 保存配置：密钥留空不改", saved["hasApiKey"] is True and saved["enabled"] is True)

    tested = http_json("POST", base + "/api/mes/test", {
        "enabled": True, "vendor": "cenkor", "baseUrl": base_url,
        "apiKey": "", "timeout": 5.0, "retries": 1, "autoPush": False,
        "includeFiles": True, "paths": {},
    })
    check("G3 测试连接（草稿）", tested["ok"] is True and tested["statusCode"] == 200)

    pulled = http_json("POST", base + "/api/mes/pull", {"projectId": project_id, "limit": 10})
    check("G4 拉单返回统计", pulled["ok"] is True and pulled["pulled"] >= 2,
          "created=%d updated=%d" % (pulled["created"], pulled["updated"]))

    listing = http_json("GET", base + "/api/mes/orders?limit=50")
    check("G5 工单列表", listing["count"] >= 2 and bool(listing["items"]))

    target = next((item for item in listing["items"] if item["remoteId"] == REMOTE_OK), None)
    detail = http_json("GET", base + "/api/mes/orders/%d" % int(target["id"]))
    check("G6 工单详情带零件清单", len(detail["parts"]) == 2, "parts=%d" % len(detail["parts"]))

    pushed = http_json("POST", base + "/api/mes/push",
                       {"orderId": int(target["id"]), "workOrderId": work_order_id})
    check("G7 回传归档", pushed["ok"] is True and pushed["fileCount"] == 2,
          "attempts=%d" % pushed["attempts"])

    logs = http_json("GET", base + "/api/mes/logs?limit=20")
    check("G8 交互日志", logs["count"] >= 3
          and {row["action"] for row in logs["items"]} >= {"test", "pull", "push"})

    # 404：不存在工单
    try:
        http_json("GET", base + "/api/mes/orders/99999999")
    except RuntimeError as exc:
        check("G9 不存在工单回 404", "HTTP 404" in str(exc), str(exc)[:80])
    else:
        check("G9 不存在工单回 404", False, "没有报错")

    # 400：关掉开关后拉单
    http_json("PUT", base + "/api/mes/settings", {
        "enabled": False, "vendor": "cenkor", "baseUrl": base_url,
        "apiKey": "", "timeout": 5.0, "retries": 2, "autoPush": False,
        "includeFiles": True, "paths": {},
    })
    try:
        http_json("POST", base + "/api/mes/pull", {"projectId": project_id, "limit": 5})
    except RuntimeError as exc:
        check("G10 未启用时回 400", "HTTP 400" in str(exc) and "未启用" in str(exc), str(exc)[:80])
    else:
        check("G10 未启用时回 400", False, "没有报错")

    # 七条路由全部注册（导出钩子靠 app.mes.service 直调，不进 openapi）
    schema = http_json("GET", base + "/openapi.json")
    paths = schema.get("paths") or {}
    expected = {
        "/api/mes/settings", "/api/mes/test", "/api/mes/pull",
        "/api/mes/orders", "/api/mes/orders/{order_id}",
        "/api/mes/push", "/api/mes/logs",
    }
    check("G11 MES 路由全部注册", expected.issubset(set(paths)),
          "missing=%s" % sorted(expected - set(paths)))


# ---------------------------------------------------------------- main
def main() -> int:
    parser = argparse.ArgumentParser(description="模块 12 MES 对接验收")
    parser.add_argument("--url", default="", help="CAM 后端地址，如 http://127.0.0.1:8975")
    args = parser.parse_args()

    db.init_db()
    with db.get_conn() as conn:
        prev_raw = db.get_setting(conn, mes_settings.SETTING_KEY)
        project_id = db.get_active_project_id(conn)

    _cleanup()
    fake = FakeMes()
    fake.start()
    print("假 MES 已启动：%s" % fake.base_url)
    work_order_id = _make_work_order(project_id, REMOTE_OK, file_count=2)
    print("本地工单 #%d（编号 %s）已就绪" % (work_order_id, REMOTE_OK))

    try:
        test_settings(prev_raw)
        test_client(fake.base_url)
        test_pull(project_id, fake.base_url)
        test_push(project_id, fake.base_url, work_order_id)
        test_offline(project_id, fake.base_url, work_order_id)
        test_auto_push(project_id, fake.base_url, work_order_id)
        if args.url:
            test_api(args.url.rstrip("/"), project_id, work_order_id, fake.base_url)
        else:
            print("\n[G] 跳过接口层（未传 --url）")
    except Exception:
        traceback.print_exc()
        check("脚本未异常中断", False, "见上方堆栈")
    finally:
        # 恢复原配置 + 清理测试数据，避免把用户的 MES 配置改坏
        try:
            with db.get_conn() as conn:
                if prev_raw is None:
                    conn.execute("DELETE FROM app_setting WHERE key = ?",
                                 (mes_settings.SETTING_KEY,))
                else:
                    db.set_setting(conn, mes_settings.SETTING_KEY, prev_raw)
        except Exception:
            pass
        _cleanup()
        fake.stop()

    passed = sum(1 for _, ok, _ in CHECKS if ok)
    total = len(CHECKS)
    print("\n%s  %d/%d 通过" % ("全部通过" if passed == total else "存在失败", passed, total))
    for name, ok, detail in CHECKS:
        if not ok:
            print("  [FAIL] %s  %s" % (name, detail))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
