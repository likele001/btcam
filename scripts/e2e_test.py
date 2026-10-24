"""V0.1 全链路联调验收（模块 1 → 12 串起来跑一遍）。

跑法（后端需先启动在 8975）：

    # 1) 主流程：导入 → 排料 → 刀路 → 余料复用 → 自定义后处理 → 工单 → MES 闭环
    backend\\.venv\\Scripts\\python.exe scripts\\e2e_test.py --url http://127.0.0.1:8975

    # 2) 重启软件后复核数据持久化（把上一步留下的探针对象逐项比对后删除）
    backend\\.venv\\Scripts\\python.exe scripts\\e2e_test.py --url http://127.0.0.1:8975 --verify

验收主线：

    P 系列  底座：健康检查、示例图形
    A 系列  DXF 导入：合法图纸入库、坏图纸让任务失败但不动已有图形
    B 系列  排料：手动落位/越界拒绝、自动排料、取消、复位
    C 系列  刀路与后处理：刀路任务、内置导出、自定义模板保存、改模板后导出实时生效
    D 系列  余料复用：剩余区域入库、换板、复位后仍受余料轮廓约束、余料内自动排料、解绑
    E 系列  工单与持久化：存单、导出登记文件、按单回开、探针数据落库（重启后用 --verify 复核）
    F 系列  MES 闭环：拉单、本地工单关联可见、回传归档、离线时 CAM 不受影响
    G 系列  日志：跨模块错误能按来源分类查出来

注意：脚本会重建当前工程的示例图形、改动板材/摆放/MES 配置，结束时恢复板材与 MES 配置，
并删除本次造的余料/工单/模板；只有"持久化探针"（名字带 E2E- 前缀）会留到 --verify 复核后再删。

退出码：0 全部通过；1 存在失败项。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlsplit

# 允许直接以 scripts/e2e_test.py 运行：把 backend 与 venv 依赖加入 import 路径
BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR / ".venv" / "Lib" / "site-packages"))

from app.mes import settings as mes_settings                 # noqa: E402
from app.models import db                                    # noqa: E402

CHECKS: List[Tuple[str, bool, str]] = []

EPS = 1e-3
SHEET = {"width": 1220.0, "height": 2440.0, "margin": 10.0, "gap": 5.0}
# L 形余料：底横 1000x400 + 左竖 400x600，右上 600x600 是缺口（矩形包围盒里有、轮廓里没有）
L_OUTLINE = [[0, 0], [1000, 0], [1000, 400], [400, 400], [400, 1000], [0, 1000]]
MES_KEY = "sk-e2e-0123456789abcdef"
STATE_FILE = Path(tempfile.gettempdir()) / "btcam_e2e_state.json"


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, ok, detail))
    print("  %s %s%s" % ("[OK]  " if ok else "[FAIL]", name, ("  " + detail) if detail else ""))
    return ok


# ---------------------------------------------------------------- HTTP
def call(method: str, url: str, body: Optional[Dict[str, Any]] = None,
         timeout: float = 180.0) -> Tuple[int, Any]:
    """发一个 JSON 请求，返回 (状态码, 解析后的响应)；4xx/5xx 不抛异常。"""
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return int(resp.status), (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return int(exc.code), json.loads(raw)
        except ValueError:
            return int(exc.code), {"detail": raw[:300]}
    except urllib.error.URLError as exc:
        raise RuntimeError("请求失败 %s %s：%s" % (method, url, exc)) from exc


def ok_call(method: str, url: str, body: Optional[Dict[str, Any]] = None,
            timeout: float = 180.0) -> Any:
    """要求 2xx，否则带上后端 detail 抛错（失败项会显示人话）。"""
    status, payload = call(method, url, body, timeout)
    if status >= 400:
        raise RuntimeError("%s %s -> HTTP %s：%s" % (
            method, url, status, (payload or {}).get("detail") if isinstance(payload, dict) else payload))
    return payload


def http_multipart(url: str, files: List[Tuple[str, str, bytes]],
                   fields: Dict[str, str], timeout: float = 180.0) -> Any:
    """极简 multipart 上传：保持后端依赖干净，不引 requests。"""
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


def wait_task(base: str, task_id: str, timeout: float = 180.0) -> Dict[str, Any]:
    """轮询任务终态：done / failed / cancelled。"""
    deadline = time.time() + timeout
    last: Dict[str, Any] = {}
    while time.time() < deadline:
        payload = ok_call("GET", base + "/api/tasks")
        for item in payload.get("tasks", []):
            if item.get("taskId") == task_id:
                last = item
                if item.get("status") in ("done", "failed", "cancelled"):
                    return item
        time.sleep(0.15)
    last.setdefault("status", "timeout")
    return last


# ---------------------------------------------------------------- 客户端侧几何判定
def point_in_poly(x: float, y: float, poly: Sequence[Sequence[float]]) -> bool:
    """射线法 + 边上特判；测试脚本自带，不复用后端代码（避免用被测代码验证被测代码）。"""
    inside = False
    count = len(poly)
    for index in range(count):
        x1, y1 = float(poly[index][0]), float(poly[index][1])
        x2, y2 = float(poly[(index + 1) % count][0]), float(poly[(index + 1) % count][1])
        cross = (x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)
        if abs(cross) <= 1e-6 and (min(x1, x2) - 1e-6 <= x <= max(x1, x2) + 1e-6) \
                and (min(y1, y2) - 1e-6 <= y <= max(y1, y2) + 1e-6):
            return True
        if (y1 > y) != (y2 > y):
            xin = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if xin >= x:
                inside = not inside
    return inside


def rect_inside(poly: Sequence[Sequence[float]], x: float, y: float,
                w: float, h: float) -> bool:
    """轴对齐矩形是否整块落在多边形内：四角在内 + 边界顶点不在矩形内部（挡跨缺口）。"""
    for cx, cy in ((x, y), (x + w, y), (x + w, y + h), (x, y + h)):
        if not point_in_poly(cx, cy, poly):
            return False
    for bx, by in poly:
        if x + EPS < float(bx) < x + w - EPS and y + EPS < float(by) < y + h - EPS:
            return False
    return True


def _size_of(part: Dict[str, Any]) -> Tuple[float, float]:
    """meta 里的 width/height 是零件原始尺寸，rot=90 时要交换。"""
    w, h = float(part["width"]), float(part["height"])
    if abs(float(part.get("rot") or 0.0) % 180.0 - 90.0) < 1e-6:
        return h, w
    return w, h


def classify_placements(meta: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """把零件分成"在板材边界内"和"越界（含停靠板外）"，返回 (界内, 界外) 的零件名。"""
    boundary = (meta.get("scrap") or {}).get("outline") or []
    width = float(meta["sheet"]["width"])
    height = float(meta["sheet"]["height"])
    margin = float(meta["sheet"]["margin"])
    inside: List[str] = []
    outside: List[str] = []
    for part in meta.get("parts") or []:
        tx, ty = float(part["tx"]), float(part["ty"])
        w, h = _size_of(part)
        ok = (tx >= margin - EPS and ty >= margin - EPS
              and tx + w <= width - margin + EPS and ty + h <= height - margin + EPS)
        if ok and boundary:
            ok = rect_inside(boundary, tx, ty, w, h)
        (inside if ok else outside).append(part["name"])
    return inside, outside


# ---------------------------------------------------------------- 假 MES
class FakeMes:
    """最小可用假 MES：ping / 拉单 / 回传归档，记录收到的每个请求。"""

    def __init__(self, orders: List[Dict[str, Any]]) -> None:
        self.orders = orders
        self.archives: List[Dict[str, Any]] = []
        self.requests: List[str] = []
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:      # 静音：测试输出要干净
                return

            def _send(self, status: int, payload: Any) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:                        # noqa: N802
                parsed = urlsplit(self.path)
                state.requests.append("GET " + parsed.path)
                if parsed.path == "/api/v1/ping":
                    self._send(200, {"ok": True, "server": "fake-mes"})
                    return
                if parsed.path == "/api/v1/workorders":
                    limit = int((parse_qs(parsed.query).get("limit") or ["0"])[0] or 0)
                    self._send(200, {"ok": True,
                                     "orders": state.orders[:limit] if limit else state.orders})
                    return
                self._send(404, {"detail": "接口不存在：%s" % parsed.path})

            def do_POST(self) -> None:                       # noqa: N802
                parsed = urlsplit(self.path)
                state.requests.append("POST " + parsed.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                if parsed.path.endswith("/archive"):
                    state.archives.append({
                        "remoteId": parsed.path.split("/")[-2],
                        "payload": _payload_of(body),
                        "fileCount": body.count(b'filename="'),
                        "auth": self.headers.get("Authorization", ""),
                        "at": time.time(),
                    })
                    self._send(200, {"ok": True, "archived": True})
                    return
                self._send(404, {"detail": "接口不存在：%s" % parsed.path})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
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


def _payload_of(body: bytes) -> Dict[str, Any]:
    """从 multipart 里抠出 payload 字段（回传带文件时走这条路）。"""
    text = body.decode("utf-8", "replace")
    index = text.find('name="payload"')
    if index < 0:
        try:
            parsed = json.loads(text or "{}")
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    start = text.find("\r\n\r\n", index)
    if start < 0:
        return {}
    start += 4
    end = text.find("\r\n--", start)
    try:
        parsed = json.loads(text[start:end if end > 0 else len(text)])
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def build_test_dxf(path: Path) -> None:
    """造一张最小可用的 DXF：一个带内孔的外框 + 一个独立矩形。"""
    import ezdxf

    doc = ezdxf.new("R2010")
    doc.units = 4                       # mm
    doc.layers.add("CUT", color=1)
    doc.layers.add("HOLES", color=3)
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (600, 0), (600, 400), (0, 400)],
                       close=True, dxfattribs={"layer": "CUT"})
    msp.add_lwpolyline([(200, 150), (400, 150), (400, 250), (200, 250)],
                       close=True, dxfattribs={"layer": "HOLES"})
    msp.add_lwpolyline([(700, 0), (1000, 0), (1000, 200), (700, 200)],
                       close=True, dxfattribs={"layer": "CUT"})
    doc.saveas(str(path))


# ---------------------------------------------------------------- P 底座
def section_env(base: str, project_id: int) -> None:
    print("== P. 底座：健康检查 / 示例图形 ==")
    health = ok_call("GET", base + "/api/health")
    check("P1 后端在线", health.get("status") == "ok" and int(health.get("workers") or 0) >= 1,
          "几何 %s · 排料 %s · 连接 %s" % (health.get("geometryEngine"),
                                     health.get("nestingEngine"), health.get("clients")))
    demo = ok_call("POST", base + "/api/demo")
    check("P2 载入示例图形", int(demo.get("polylines") or 0) > 0,
          "%d 条轮廓" % int(demo.get("polylines") or 0))


# ---------------------------------------------------------------- A 导入
def section_import(base: str, project_id: int) -> None:
    print("== A. DXF 导入：好图纸入库 / 坏图纸不拖垮进程 ==")
    temp_dir = Path(tempfile.mkdtemp(prefix="btcam_e2e_"))
    dxf_path = temp_dir / "E2E图纸.dxf"
    build_test_dxf(dxf_path)

    accepted = http_multipart(base + "/api/import/dxf",
                              files=[("files", dxf_path.name, dxf_path.read_bytes())],
                              fields={"options": json.dumps({"replace": True, "splitParts": True})})
    done = wait_task(base, str(accepted.get("taskId") or ""))
    meta = ok_call("GET", base + "/api/layout?projectId=%d" % project_id)
    # 统计取 stats.partCount：刚导入还没做过初始摆放，meta.parts 只含“有摆放”的件
    imported = int(meta["stats"]["partCount"])
    check("A1 合法 DXF 导入完成", done.get("status") == "done" and imported >= 1,
          "任务 %s · %d 个零件" % (done.get("status"), imported))

    before = imported
    bad = http_multipart(base + "/api/import/dxf",
                         files=[("files", "坏图纸.dxf", b"this is not a dxf at all\r\n\x00\x01")],
                         fields={"options": json.dumps({"replace": True})})
    failed = wait_task(base, str(bad.get("taskId") or ""))
    check("A2 坏 DXF 让任务失败而不是崩溃", failed.get("status") == "failed",
          str(failed.get("message") or "")[:80])

    health = ok_call("GET", base + "/api/health")
    after = ok_call("GET", base + "/api/layout?projectId=%d" % project_id)
    kept = int(after["stats"]["partCount"])
    check("A3 坏导入后图形原样保留", health.get("status") == "ok" and kept == before,
          "导入前 %d / 导入后 %d 个零件" % (before, kept))


# ---------------------------------------------------------------- B 排料
def section_layout(base: str, project_id: int) -> None:
    print("== B. 排料：手动落位 / 越界拒绝 / 自动排料 / 取消 / 复位 ==")
    ok_call("POST", base + "/api/scrap/clear", dict(projectId=project_id, **SHEET))
    meta = ok_call("POST", base + "/api/layout/reset?projectId=%d" % project_id)
    meta = ok_call("GET", base + "/api/layout?projectId=%d" % project_id)
    check("B1 板材复位成整板", abs(float(meta["sheet"]["width"]) - SHEET["width"]) < EPS
          and abs(float(meta["sheet"]["margin"]) - SHEET["margin"]) < EPS
          and len(meta["parts"]) == len(meta["placements"]),
          "%.0f×%.0f 余量 %.0f · %d 个摆放" % (
              meta["sheet"]["width"], meta["sheet"]["height"], meta["sheet"]["margin"],
              len(meta["placements"])))

    # 挑最高最宽的件：初始行排会把它摆在 (margin, margin)，往下错开一行就撞不到旁边的小件
    target = max(meta["parts"], key=lambda p: (float(p["height"]), float(p["width"])))
    moved = ok_call("POST", base + "/api/layout/place", {
        "projectId": project_id, "partId": target["id"], "tx": 30.0, "ty": 600.0})
    after_move = ok_call("GET", base + "/api/layout?projectId=%d" % project_id)
    placed = next((p for p in after_move["parts"] if p["id"] == target["id"]), {})
    check("B2 手动落位生效", moved["ok"] is True
          and abs(float(placed.get("tx", 0.0)) - 30.0) < EPS
          and abs(float(placed.get("ty", 0.0)) - 600.0) < EPS,
          "零件「%s」(%.0f×%.0f) → (30,600) %s" % (
              target["name"], float(target["width"]), float(target["height"]),
              moved.get("reason") or ""))

    rejected = ok_call("POST", base + "/api/layout/place", {
        "projectId": project_id, "partId": target["id"], "tx": 100000.0, "ty": 30.0})
    check("B3 越界落位被拒", rejected["ok"] is False and bool(rejected["reason"]),
          rejected["reason"])

    accepted = ok_call("POST", base + "/api/layout/nest", {
        "projectId": project_id, "engine": "scan", "rotation": True})
    nested = wait_task(base, str(accepted.get("taskId") or ""))
    meta = ok_call("GET", base + "/api/layout?projectId=%d" % project_id)
    inside, outside = classify_placements(meta)
    check("B4 自动排料有结果", nested.get("status") == "done" and len(inside) >= 1,
          "%d 落位 / %d 越界 · 状态 %s" % (len(inside), len(outside), nested.get("status")))

    # 取消：任务很快，可能已经结束（409）——只要最终是终态、没有 running 残留即可
    accepted = ok_call("POST", base + "/api/layout/nest", {
        "projectId": project_id, "engine": "scan", "rotation": True})
    task_id = str(accepted.get("taskId") or "")
    status, cancel_payload = call("POST", base + "/api/tasks/%s/cancel" % task_id)
    final = wait_task(base, task_id, timeout=60.0)
    tasks = ok_call("GET", base + "/api/tasks")["tasks"]
    running = [t for t in tasks if t.get("taskId") == task_id
               and t.get("status") in ("queued", "running")]
    check("B5 取消后任务进入终态", final.get("status") in ("cancelled", "done") and not running,
          "取消接口 HTTP %s · 最终 %s" % (status, final.get("status")))

    ok_call("POST", base + "/api/layout/reset?projectId=%d" % project_id)
    meta = ok_call("GET", base + "/api/layout?projectId=%d" % project_id)
    check("B6 复位回到初始行排", len(meta["placements"]) == len(meta["parts"]),
          "%d 个摆放" % len(meta["placements"]))


# ---------------------------------------------------------------- C 刀路 / 后处理
def section_post(base: str, project_id: int) -> Optional[int]:
    print("== C. 刀路与后处理：导出 / 自定义模板 / 改模板实时生效 ==")
    accepted = ok_call("POST", base + "/api/toolpath", {
        "projectId": project_id, "source": "layout", "sheetIndex": 0})
    task = wait_task(base, str(accepted.get("taskId") or ""))
    check("C1 刀路任务完成", task.get("status") == "done",
          "状态 %s · %s" % (task.get("status"), task.get("message") or ""))

    exported = ok_call("POST", base + "/api/export", {
        "projectId": project_id, "source": "layout", "format": "nc", "name": "E2E内置"})
    text = Path(exported["path"]).read_text(encoding="utf-8", errors="replace")
    stats = exported.get("stats") or {}
    check("C2 内置后处理导出", len(text) > 0 and int(stats.get("contourCount") or 0) >= 1,
          "%d 条轮廓 · 切长 %.0fmm · %d 字符" % (
              int(stats.get("contourCount") or 0), float(stats.get("cutLengthMm") or 0), len(text)))

    # 复制内置模板成自定义模板（只保留请求模型认识的字段）
    builtin = ok_call("GET", base + "/api/post/shanlong_nc")
    pid = "e2e_%s" % time.strftime("%H%M%S")
    draft = {
        "id": pid, "name": "E2E 联调模板", "kind": "gcode", "ext": ".nc",
        "description": "全链路验收脚本创建",
        "gcode": dict(builtin.get("gcode") or {}),
    }
    draft["gcode"]["header"] = list(draft["gcode"].get("header") or []) + ["( E2E-MARK-1 )"]
    saved = ok_call("POST", base + "/api/post", draft)
    check("C3 自定义后处理保存", saved.get("id") == pid
          and "( E2E-MARK-1 )" in (saved.get("gcode") or {}).get("header", []),
          "id=%s" % pid)

    custom = ok_call("POST", base + "/api/export", {
        "projectId": project_id, "source": "layout", "processorId": pid, "name": "E2E自定义"})
    first = Path(custom["path"]).read_text(encoding="utf-8", errors="replace")
    check("C4 用自定义模板导出", "E2E-MARK-1" in first,
          "%s · %d 字符" % (Path(custom["path"]).name, len(first)))

    # 编辑器改模板 → 立刻再导出：导出的文件必须跟着变（registry 每次都读盘）
    draft["gcode"]["header"] = [line for line in draft["gcode"]["header"]
                                if line != "( E2E-MARK-1 )"] + ["( E2E-MARK-2 )"]
    ok_call("PUT", base + "/api/post/%s" % pid, draft)
    custom = ok_call("POST", base + "/api/export", {
        "projectId": project_id, "source": "layout", "processorId": pid, "name": "E2E自定义2"})
    second = Path(custom["path"]).read_text(encoding="utf-8", errors="replace")
    check("C5 改模板后导出实时生效", "E2E-MARK-2" in second and "E2E-MARK-1" not in second,
          "MARK-2 在 / MARK-1 已消失")

    broken = json.loads(json.dumps(draft))
    # 故意把占位符写错：{nope} 不在变量字典里，必须在校验阶段拦住，而不是导出时才炸
    broken["gcode"]["cutXY"] = "G01 X{x} Y{y} {nope}"
    validated = ok_call("POST", base + "/api/post/validate", {"processor": broken})
    fields = [str(item.get("field") or "") for item in validated.get("errors") or []]
    check("C6 非法模板被校验拦住", bool(validated.get("errors")) and any(fields),
          "错误字段：%s" % "、".join(f for f in fields if f))

    ok_call("DELETE", base + "/api/post/%s" % pid)
    status, _ = call("GET", base + "/api/post/%s" % pid)
    check("C7 删除自定义模板", status == 404, "HTTP %d" % status)
    return None


# ---------------------------------------------------------------- D 余料复用
def section_scrap(base: str, project_id: int) -> Dict[str, Any]:
    print("== D. 余料复用：剩余区域入库 / 换板 / 复位后仍受轮廓约束 ==")
    ok_call("POST", base + "/api/scrap/clear", dict(projectId=project_id, **SHEET))
    ok_call("POST", base + "/api/layout/reset?projectId=%d" % project_id)

    remaining = ok_call("POST", base + "/api/scrap/remaining", {
        "projectId": project_id, "minArea": 10000.0})
    check("D1 算出剩余区域", int(remaining.get("count") or 0) >= 1,
          "%d 块 · 最大 %.0f mm²" % (int(remaining.get("count") or 0),
                                 float(remaining["regions"][0]["area"]) if remaining.get("regions") else 0))

    saved = ok_call("POST", base + "/api/scrap/save", {
        "projectId": project_id, "indices": [0], "namePrefix": "E2E 余料",
        "material": "18mm 颗粒板", "thickness": 18.0,
    })
    scrap = saved["saved"][0]
    check("D2 剩余区域存进余料库", scrap["points"] >= 3 and scrap["area"] > 0
          and scrap["source"] == "layout",
          "余料 #%d %.0f×%.0f · %.0f mm²" % (
              scrap["id"], scrap["width"], scrap["height"], scrap["area"]))

    applied = ok_call("POST", base + "/api/scrap/%d/apply" % scrap["id"],
                      {"projectId": project_id, "gap": 5.0})
    check("D3 余料设为当前板材", abs(float(applied["sheet"]["width"]) - float(scrap["width"])) < EPS
          and abs(float(applied["sheet"]["margin"])) < EPS
          and int(applied["stats"]["scrapId"]) == int(scrap["id"]),
          "%.0f×%.0f · margin=0 · scrapId=%d" % (
              applied["sheet"]["width"], applied["sheet"]["height"], applied["stats"]["scrapId"]))

    # 关键回归：绑定余料后点「复位」，放不进轮廓的件必须停靠板外，统计也要如实报越界
    ok_call("POST", base + "/api/layout/reset?projectId=%d" % project_id)
    meta = ok_call("GET", base + "/api/layout?projectId=%d" % project_id)
    inside, outside = classify_placements(meta)
    overflow = int(meta["stats"]["overflowCount"])
    check("D4 复位后仍受余料轮廓约束",
          len(outside) == overflow and len(inside) + len(outside) == len(meta["parts"]),
          "轮廓内 %d / 越界 %d · overflowCount=%d" % (len(inside), len(outside), overflow))

    accepted = ok_call("POST", base + "/api/layout/nest", {
        "projectId": project_id, "engine": "scan", "rotation": True})
    wait_task(base, str(accepted.get("taskId") or ""))
    meta = ok_call("GET", base + "/api/layout?projectId=%d" % project_id)
    inside, outside = classify_placements(meta)
    parked = [p for p in meta["parts"]
              if float(p["ty"]) >= float(meta["sheet"]["height"]) - EPS]
    overflow = int(meta["stats"]["overflowCount"])
    if os.environ.get("E2E_DEBUG"):
        for p in meta["parts"]:
            print("      %s tx=%.2f ty=%.2f w=%.1f h=%.1f rot=%.0f" % (
                p["name"], p["tx"], p["ty"], p["width"], p["height"], p["rot"]))
        print("      inside=%s outside=%s" % (inside, outside))
    # 安全底线：自动排料结果要么整块在轮廓内，要么整块停靠板外，不能停在余料缺口的矩形里；
    # 同时要求至少有一件真排进余料（P-1 修好后，凹形余料不该再是 0 落位）
    check("D5 余料内自动排料不越界",
          len(outside) == overflow and len(parked) == len(outside)
          and len(inside) + len(outside) == len(meta["parts"])
          and len(inside) >= 1,
          "轮廓内 %d / 停靠板外 %d · overflowCount=%d" % (
              len(inside), len(outside), overflow))

    cleared = ok_call("POST", base + "/api/scrap/clear", dict(projectId=project_id, **SHEET))
    check("D6 解绑退回整板", int(cleared["stats"]["scrapId"]) == 0
          and abs(float(cleared["sheet"]["margin"]) - SHEET["margin"]) < EPS,
          "scrapId=0 · margin=%.0f" % cleared["sheet"]["margin"])
    return scrap


# ---------------------------------------------------------------- E 工单 / 持久化
def section_workorder(base: str, project_id: int, scrap: Dict[str, Any],
                      order_code: str) -> Dict[str, Any]:
    print("== E. 工单与持久化：存单 / 导出登记 / 回开 / 落库探针 ==")
    # 让工单快照带上余料绑定：apply 之后建单
    ok_call("POST", base + "/api/scrap/%d/apply" % scrap["id"],
            {"projectId": project_id, "gap": 5.0})

    # 工单编号故意不填 MES 单号：F2 要验证“没建本地工单时 localWorkOrder 为空”
    order = ok_call("POST", base + "/api/workorders", {
        "projectId": project_id,
        "name": "E2E 联调工单",
        "code": order_code,
        "note": "全链路验收脚本创建",
        "post": {"processorId": "shanlong_nc", "format": "nc", "name": "山龙",
                 "flipY": False, "speed": 30, "force": 20},
    })
    snapshot = order.get("stats") or {}
    # 工单详情不带 sheet（快照只在“打开工单”时回灌），绑定关系看当前排料板即可
    bound = int(ok_call("GET", base + "/api/layout?projectId=%d" % project_id)["stats"]["scrapId"])
    check("E1 工单保存带快照", int(snapshot.get("partCount") or 0) > 0 and bound == int(scrap["id"]),
          "工单 #%d · %d 个零件 · 余料 #%d" % (order["id"], int(snapshot.get("partCount") or 0), bound))

    export = ok_call("POST", base + "/api/export", {
        "projectId": project_id, "source": "layout", "processorId": "shanlong_nc",
        "name": "E2E工单导出", "workOrderId": order["id"]})
    listing = ok_call("GET", base + "/api/workorders/%d" % order["id"])
    files = listing.get("files") or []
    check("E2 导出登记到工单", len(files) >= 1
          and files[0].get("format") == export.get("format"),
          "%d 个文件 · 首个 %s" % (len(files), files[0].get("format") if files else "-"))

    ok_call("POST", base + "/api/scrap/clear", dict(projectId=project_id, **SHEET))
    reopened = ok_call("POST", base + "/api/workorders/%d/open" % order["id"])
    meta = ok_call("GET", base + "/api/layout?projectId=%d" % project_id)
    check("E3 按工单回开恢复余料绑定",
          int(meta["stats"]["scrapId"]) == int(scrap["id"]) and meta.get("scrap") is not None,
          "scrapId=%d" % int(meta["stats"]["scrapId"]))
    check("E4 回开后工单参数可比对", int(reopened.get("id") or 0) == int(order["id"])
          and (reopened.get("post") or {}).get("processorId") == "shanlong_nc",
          "后处理 %s" % (reopened.get("post") or {}).get("processorId"))

    # 刀具模板：存一个再按 id 回读，字段要一字不差（重启后 --verify 再比一次）
    template = ok_call("POST", base + "/api/templates", {
        "projectId": project_id, "name": "E2E-刀具模板", "material": "18mm 颗粒板",
        "params": {"cutType": "outer", "side": "outside", "optimize": True,
                   "microJoint": True, "microJointWidth": 3.0, "leadIn": True}})
    fetched = ok_call("GET", base + "/api/templates?projectId=%d" % project_id)
    hit = next((item for item in fetched.get("templates") or []
                if int(item["id"]) == int(template["id"])), None)
    check("E5 刀具模板保存加载完整", hit is not None
          and hit["name"] == "E2E-刀具模板"
          and float((hit.get("params") or {}).get("microJointWidth") or 0) == 3.0,
          "模板 #%d" % int(template["id"]))

    state = {
        "orderId": int(order["id"]),
        "orderName": order["name"],
        "orderCode": order_code,
        "scrapId": int(scrap["id"]),
        "scrapName": scrap["name"],
        "templateId": int(template["id"]),
        "templateName": "E2E-刀具模板",
    }
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return state


# ---------------------------------------------------------------- F MES 闭环
def section_mes(base: str, project_id: int, mes_remote: str) -> Optional[int]:
    print("== F. MES 闭环：拉单 / 本地工单关联 / 回传 / 离线 ==")
    local_id: Optional[int] = None
    fake = FakeMes([{
        "orderNo": mes_remote,
        "name": "E2E MES 工单",
        "customer": "联调客户",
        "material": "多层板",
        "thickness": 18.0,
        "dueDate": "2026-12-31",
        "parts": [{"name": "侧板", "qty": 2, "width": 600.0, "height": 400.0}],
    }])
    fake.start()
    try:
        ok_call("PUT", base + "/api/mes/settings", {
            "enabled": True, "vendor": "cenkor", "baseUrl": fake.base_url,
            "apiKey": MES_KEY, "timeout": 5, "retries": 1,
            "autoPush": False, "includeFiles": True, "paths": {},
        })
        pulled = ok_call("POST", base + "/api/mes/pull", {"projectId": project_id, "limit": 50})
        check("F1 从 MES 拉单", int(pulled.get("created") or 0) >= 1,
              "新增 %d / 更新 %d" % (int(pulled.get("created") or 0),
                                 int(pulled.get("updated") or 0)))

        listing = ok_call("GET", base + "/api/mes/orders?limit=50")
        remote = next((item for item in listing["items"] if item["remoteId"] == mes_remote), None)
        check("F2 未建工单时关联状态为 null", remote is not None
              and remote.get("localWorkOrder") is None,
              "localWorkOrder=%s" % (remote or {}).get("localWorkOrder"))

        # 建单时工单编号就填 MES 单号：这就是"拉单 → 存单"的双向关联契约
        local = ok_call("POST", base + "/api/workorders", {
            "projectId": project_id, "name": "E2E MES 本地工单",
            "code": mes_remote, "note": "由 MES 单创建"})
        local_id = int(local["id"])
        listing = ok_call("GET", base + "/api/mes/orders?limit=50")
        remote = next((item for item in listing["items"] if item["remoteId"] == mes_remote), None)
        linked = (remote or {}).get("localWorkOrder") or {}
        check("F3 本地工单关联可见", int(linked.get("id") or 0) == int(local["id"])
              and int(listing.get("linkedCount") or 0) >= 1,
              "工单 #%s · linkedCount=%s" % (linked.get("id"), listing.get("linkedCount")))

        ok_call("POST", base + "/api/export", {
            "projectId": project_id, "source": "layout", "format": "nc",
            "name": "E2E回传", "workOrderId": local["id"]})
        pushed = ok_call("POST", base + "/api/mes/push", {
            "orderId": remote["id"], "workOrderId": local["id"]})
        archived = fake.archives[-1] if fake.archives else {}
        payload = archived.get("payload") or {}
        usage = payload.get("usage") or {}
        check("F4 回传归档闭环", pushed.get("ok") is True
              and int((payload.get("workOrder") or {}).get("id") or 0) == int(local["id"])
              and int(usage.get("partCount") or 0) > 0
              and int(archived.get("fileCount") or 0) >= 1,
              "HTTP %s · %d 个文件 · 零件 %s · 鉴权 %s" % (
                  pushed.get("statusCode"), int(archived.get("fileCount") or 0),
                  usage.get("partCount"), "有" if archived.get("auth") else "无"))

        before = len(fake.requests)
        ok_call("PUT", base + "/api/mes/settings", {
            "enabled": False, "vendor": "cenkor", "baseUrl": fake.base_url,
            "apiKey": MES_KEY, "timeout": 5, "retries": 0,
            "autoPush": True, "includeFiles": True, "paths": {},
        })
        status, detail = call("POST", base + "/api/mes/push", {"orderId": remote["id"]})
        check("F5 关闭后回传被拒且零请求", status == 400 and len(fake.requests) == before,
              "HTTP %d · %s" % (status, str((detail or {}).get("detail"))[:40]))

        reset = ok_call("POST", base + "/api/layout/reset?projectId=%d" % project_id)
        exported = ok_call("POST", base + "/api/export", {
            "projectId": project_id, "source": "layout", "format": "nc", "name": "E2E离线"})
        check("F6 离线时 CAM 功能不受影响", bool(reset.get("ok", True))
              and Path(exported["path"]).exists(),
              "复位 + 导出均正常（%s）" % Path(exported["path"]).name)
    finally:
        fake.stop()
    return local_id


# ---------------------------------------------------------------- G 日志
def section_logs(base: str) -> None:
    print("== G. 日志：跨模块错误按来源可查 ==")
    mes_logs = ok_call("GET", base + "/api/logs?source=app.mes&limit=200")
    task_logs = ok_call("GET", base + "/api/logs?source=app.core.tasks&limit=200")
    entries = mes_logs.get("logs") or []
    check("G1 MES 日志单独归类", len(entries) >= 1
          and all("mes" in str(item.get("source", "")).lower() for item in entries),
          "%d 条 · 最新：%s" % (len(entries), (entries[0].get("message") or "")[:40] if entries else "-"))
    check("G2 任务日志与 MES 日志分开", len(task_logs.get("logs") or []) >= 1,
          "%d 条任务日志" % len(task_logs.get("logs") or []))

    # 级别过滤：只要返回的都是 warning 及以上，说明面板能按严重程度筛
    failed = ok_call("GET", base + "/api/logs?level=warning&limit=200")
    items = failed.get("logs") or []
    levels = {str(item.get("level")) for item in items}
    check("G3 告警级别可过滤", isinstance(items, list)
          and levels <= {"warning", "error", "critical"},
          "%d 条 warning 及以上（%s）" % (len(items), "、".join(sorted(levels)) or "-"))


# ---------------------------------------------------------------- 收尾
def cleanup(base: str, project_id: int, scrap_id: Optional[int],
            order_ids: Sequence[int]) -> None:
    """删掉本次造的余料/工单，板材与摆放复位（探针对象留给 --verify）。"""
    for order_id in order_ids:
        try:
            call("DELETE", base + "/api/workorders/%d?projectId=%d" % (order_id, project_id))
        except Exception:
            pass
    for pid in ("e2e_",):
        for item in ok_call("GET", base + "/api/post").get("processors") or []:
            if str(item.get("id", "")).startswith(pid) and not item.get("builtin"):
                call("DELETE", base + "/api/post/%s" % item["id"])
    try:
        call("POST", base + "/api/scrap/clear", dict(projectId=project_id, **SHEET))
        if scrap_id:
            call("DELETE", base + "/api/scrap/%d?projectId=%d" % (scrap_id, project_id))
        call("POST", base + "/api/layout/reset?projectId=%d" % project_id)
    except Exception:
        pass


def cleanup_stale(base: str, project_id: int) -> None:
    """清掉上次没复核就重跑留下的 E2E 探针（只认名字前缀，不碰用户数据）。"""
    try:
        orders = ok_call("GET", base + "/api/workorders?projectId=%d&limit=200" % project_id)
        for item in orders.get("orders") or []:
            if str(item.get("name") or "").startswith("E2E"):
                call("DELETE", base + "/api/workorders/%d?projectId=%d"
                     % (int(item["id"]), project_id))
    except Exception:
        pass
    try:
        listing = ok_call("GET", base + "/api/templates?projectId=%d" % project_id)
        for item in listing.get("templates") or []:
            if str(item.get("name") or "").startswith("E2E"):
                call("DELETE", base + "/api/templates/%d?projectId=%d"
                     % (int(item["id"]), project_id))
    except Exception:
        pass
    try:
        scraps = ok_call("GET", base + "/api/scrap?projectId=%d&limit=500" % project_id)
        for item in scraps.get("items") or []:
            if str(item.get("name") or "").startswith("E2E"):
                call("DELETE", base + "/api/scrap/%d?projectId=%d"
                     % (int(item["id"]), project_id))
    except Exception:
        pass


def restore_mes_config(prev_raw: Optional[str]) -> None:
    """把 MES 配置还原成脚本开始前的样子（不碰用户的真实密钥）。"""
    try:
        with db.get_conn() as conn:
            if prev_raw is None:
                conn.execute("DELETE FROM app_setting WHERE key = ?", (mes_settings.SETTING_KEY,))
            else:
                db.set_setting(conn, mes_settings.SETTING_KEY, prev_raw)
    except Exception:
        traceback.print_exc()


def cleanup_mes_rows(prefix: str) -> None:
    """删掉本次造的 MES 缓存单与日志（直接改库，脚本和后端同机）。"""
    try:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM mes_push_log WHERE order_id IN"
                         " (SELECT id FROM mes_order WHERE remote_id LIKE ?)", (prefix + "%",))
            conn.execute("DELETE FROM mes_order WHERE remote_id LIKE ?", (prefix + "%",))
    except Exception:
        traceback.print_exc()


# ---------------------------------------------------------------- 重启复核
def verify_after_restart(base: str, project_id: int) -> None:
    """重启后端后复核：上次留下的探针数据是否一字不差地读回来。"""
    print("== V. 重启复核：工单 / 余料 / 刀具模板 ==")
    if not STATE_FILE.exists():
        check("V0 找到上次的探针清单", False, "缺 %s，先跑一遍主流程" % STATE_FILE)
        return
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    ok = True

    status, order = call("GET", base + "/api/workorders/%d" % state["orderId"])
    ok = check("V1 工单重启后仍在", status == 200
               and order.get("code") == state["orderCode"]
               and int((order.get("stats") or {}).get("partCount") or 0) > 0,
               "工单 #%s · 编号 %s" % (state["orderId"], (order or {}).get("code"))) and ok

    status, scrap = call("GET", base + "/api/scrap/%d?projectId=%d" % (state["scrapId"], project_id))
    ok = check("V2 余料重启后仍在", status == 200
               and scrap.get("name") == state["scrapName"]
               and len(scrap.get("outline") or []) >= 3,
               "余料 #%s · %d 点" % (state["scrapId"], len((scrap or {}).get("outline") or []))) and ok

    listing = ok_call("GET", base + "/api/templates?projectId=%d" % project_id)
    hit = next((item for item in listing.get("templates") or []
                if int(item["id"]) == int(state["templateId"])), None)
    ok = check("V3 刀具模板重启后仍在", hit is not None
               and hit.get("name") == state["templateName"],
               "模板 #%s" % state["templateId"]) and ok

    # 复核完清掉探针，别在用户库里留垃圾
    call("DELETE", base + "/api/workorders/%d?projectId=%d" % (state["orderId"], project_id))
    call("DELETE", base + "/api/templates/%d?projectId=%d" % (state["templateId"], project_id))
    call("DELETE", base + "/api/scrap/%d?projectId=%d" % (state["scrapId"], project_id))
    if ok:
        STATE_FILE.unlink(missing_ok=True)
    print("  探针已清理：工单 #%s / 余料 #%s / 模板 #%s" % (
        state["orderId"], state["scrapId"], state["templateId"]))


# ---------------------------------------------------------------- 入口
def main() -> int:
    parser = argparse.ArgumentParser(description="V0.1 全链路联调验收")
    parser.add_argument("--url", default="http://127.0.0.1:8975", help="后端地址")
    parser.add_argument("--verify", action="store_true",
                        help="重启后端后复核上次留下的持久化探针（复核完自动删除）")
    args = parser.parse_args()
    base = args.url.rstrip("/")

    db.init_db()
    with db.get_conn() as conn:
        project_id = db.get_active_project_id(conn)
        prev_raw = db.get_setting(conn, mes_settings.SETTING_KEY)

    started = time.time()
    mes_remote = "E2E-MES-%s" % time.strftime("%H%M%S")
    # 本地工单编号与 MES 单号错开：先验证“未建本地工单时关联为空”，再验证“建单后能对上”
    local_code = "E2E-LOCAL-%s" % time.strftime("%H%M%S")
    order_ids: List[int] = []
    try:
        if args.verify:
            verify_after_restart(base, project_id)
        else:
            cleanup_mes_rows("E2E-MES-")
            cleanup_stale(base, project_id)
            section_env(base, project_id)
            section_import(base, project_id)
            flush_stdout()
            section_layout(base, project_id)
            section_post(base, project_id)
            scrap = section_scrap(base, project_id)
            # 工单/余料/刀具模板探针不删：重启后跑 --verify 复核
            state = section_workorder(base, project_id, scrap, local_code)
            mes_local = section_mes(base, project_id, mes_remote)
            if mes_local:
                order_ids.append(mes_local)
            section_logs(base)
            cleanup(base, project_id, None, order_ids)
            cleanup_mes_rows("E2E-MES-")
    except Exception:
        traceback.print_exc()
        check("脚本执行中断", False, "见上方堆栈")
    finally:
        restore_mes_config(prev_raw)

    passed = sum(1 for _, ok, _ in CHECKS if ok)
    total = len(CHECKS)
    print("\n%s  %d/%d 通过 · 用时 %.1fs" % (
        "全部通过" if passed == total else "存在失败", passed, total, time.time() - started))
    for name, ok, detail in CHECKS:
        if not ok:
            print("  [FAIL] %s  %s" % (name, detail))
    if not args.verify and STATE_FILE.exists():
        print("\n持久化复核：重启后端后执行")
        print("  backend\\.venv\\Scripts\\python.exe scripts\\e2e_test.py --url %s --verify" % base)
    return 0 if passed == total else 1


def flush_stdout() -> None:
    sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
