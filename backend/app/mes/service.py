"""MES 业务编排（模块 12）：拉单入库 / 回传归档 / 自动推送旁路。

三层分工，与 settings / client 明确切开，便于单测：
    settings.py  存什么、怎么校验
    client.py    怎么发请求、怎么重试
    service.py   **发什么内容**：MES 字段归一化、用料统计组装、日志落库

字段归一化为什么在这里：CenkorMes 用 `orderNo`，LightMes 用 `orderId`，
别家可能用 `code`；不同实现连零件数组都叫法不一（parts / items /
components / details）。客户端只负责"把 JSON 拿回来"，认字段这种脏活集中
在 `_normalize_order`。

离线约束：`auto_push` 是导出成功后的旁路钩子，跑在 daemon 线程里并整体
try/except，网络失败只落一条日志，导出接口的返回值与耗时都不受 MES 影响。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from app.mes import settings as mes_settings
from app.mes.client import MesClient, MesError
from app.mes.settings import MesConfig
from app.models import db

logger = logging.getLogger(__name__)

# 单次回传的文件上限：超过就只传前 N 个并在 notes 里说明，避免把 MES 打爆
MAX_FILES = 20
MAX_FILE_BYTES = 32 * 1024 * 1024

ORDER_ID_KEYS = ("orderId", "order_id", "orderNo", "order_no", "code")
PART_LIST_KEYS = ("parts", "items", "components", "details", "partList")
PART_NAME_KEYS = ("name", "partName", "part_name", "partNo", "no")


class MesDisabledError(RuntimeError):
    """对接未启用；路由层翻译成 HTTP 400，前端提示"到设置页打开开关"。"""


class MesDataError(RuntimeError):
    """MES 返回的数据无法识别 / 本地缺少可回传的工单；路由层翻译成 400。"""


# ---------------------------------------------------------------- 小工具
def _first(raw: Dict[str, Any], keys: Tuple[str, ...], default: Any = "") -> Any:
    """按别名顺序取第一个非空字段；空字符串也算没有。"""
    for key in keys:
        value = raw.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _iso(stamp: float) -> str:
    """时间戳 -> 本地 ISO 文案（MES 侧基本都是人看的，不做时区转换）。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(float(stamp)))


def _log(action: str, ok: bool, message: str = "", attempts: int = 0,
         status_code: int = 0, order_id: int = 0) -> None:
    """写交互日志；日志本身失败不能再抛，否则会盖掉真正的错误。"""
    try:
        with db.get_conn() as conn:
            db.insert_mes_log(conn, action=action, ok=ok, message=message,
                              attempts=attempts, status_code=status_code,
                              order_id=order_id)
    except Exception:
        logger.warning("MES 日志写入失败（action=%s）", action, exc_info=True)


def _require_enabled() -> MesConfig:
    """业务动作的统一前置条件：没启用就一个包都别发出去。"""
    config = mes_settings.load()
    if not config.enabled:
        raise MesDisabledError("MES 对接未启用，请先到「MES 对接」打开开关")
    if not config.base_url:
        raise MesDisabledError("MES 服务地址为空，请先到设置页填写")
    return config


# ---------------------------------------------------------------- 字段归一化
def _normalize_part(item: Any, index: int) -> Dict[str, Any]:
    """单个零件；字符串也认（有些 MES 的零件清单就是名字数组）。"""
    fallback = "零件%d" % (index + 1)
    if isinstance(item, str):
        return {"name": item.strip() or fallback, "qty": 1, "material": "",
                "thickness": 0.0, "width": 0.0, "height": 0.0, "remoteId": ""}
    if not isinstance(item, dict):
        return {"name": fallback, "qty": 1, "material": "",
                "thickness": 0.0, "width": 0.0, "height": 0.0, "remoteId": ""}
    qty = _to_int(_first(item, ("qty", "quantity", "count", "num"), 1), 1) or 1
    return {
        "name": str(_first(item, PART_NAME_KEYS, fallback)).strip() or fallback,
        "qty": qty,
        "material": str(_first(item, ("material", "mat", "材质"), "") or "").strip(),
        "thickness": round(_to_float(_first(item, ("thickness", "thick", "厚度"), 0.0)), 3),
        "width": round(_to_float(item.get("width"), 0.0), 3),
        "height": round(_to_float(item.get("height"), 0.0), 3),
        "remoteId": str(_first(item, ("partId", "part_id", "id", "uuid", "no"), "") or ""),
    }


def _normalize_order(item: Any) -> Dict[str, Any]:
    """MES 工单 -> mes_order 行 payload；认不出单号就算失败（宁可少拉不可乱拉）。"""
    if not isinstance(item, dict):
        raise MesDataError("MES 返回的工单不是对象：%s" % type(item).__name__)
    remote_id = str(_first(item, ORDER_ID_KEYS, "") or "").strip()
    if not remote_id:
        raise MesDataError("MES 工单缺少单号（id / orderNo / code 都没有）")

    raw_parts = _first(item, PART_LIST_KEYS, [])
    if isinstance(raw_parts, dict):
        raw_parts = [raw_parts]
    if not isinstance(raw_parts, list):
        raw_parts = []
    parts = [_normalize_part(part, index) for index, part in enumerate(raw_parts)]
    qty_total = sum(part["qty"] for part in parts) or len(parts)

    first_part = parts[0] if parts else {}
    material = str(_first(item, ("material", "mat", "材质"), "") or "").strip()
    thickness = _to_float(_first(item, ("thickness", "thick", "厚度"), 0.0), 0.0)
    if not material:
        material = first_part.get("material") or ""
    if thickness <= 0:
        thickness = first_part.get("thickness") or 0.0

    part_count = _to_int(_first(item, ("partCount", "part_count"), 0), 0) or qty_total
    return {
        "remote_id": remote_id,
        "name": str(_first(item, ("name", "title", "orderName"), remote_id) or remote_id).strip(),
        "customer": str(_first(item, ("customer", "customerName", "client"), "") or "").strip(),
        "material": material,
        "thickness": round(float(thickness), 3),
        "due_date": str(_first(item, ("dueDate", "due_date", "deliveryDate",
                                      "planDate", "deadline"), "") or "").strip(),
        "part_count": int(part_count),
        "parts": parts,
        "payload": item,
    }


def _extract_orders(response: Any) -> List[Dict[str, Any]]:
    """从响应里捞出工单数组：裸数组 / {items|orders|data|list} / 单条对象。"""
    if isinstance(response, list):
        return [item for item in response if isinstance(item, dict)]
    if not isinstance(response, dict):
        return []
    for key in ("orders", "items", "data", "list", "results", "records"):
        value = response.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            for inner in ("orders", "items", "list", "data"):
                rows = value.get(inner)
                if isinstance(rows, list):
                    return [item for item in rows if isinstance(item, dict)]
    # 单条工单对象：有单号或零件清单就当它是一条
    if any(key in response for key in ("orderNo", "orderId", "code", "parts", "items")):
        return [response]
    return []


# ---------------------------------------------------------------- 测试连接
def test_connection(draft: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """设置页"测试连接"：允许用未保存的草稿试，也允许在未启用时先试通。

    密钥留空 = 沿用已保存的那一把，与保存逻辑一致（前端拿不到明文）。
    """
    started = time.time()
    current = mes_settings.load()
    config = mes_settings.build(draft, current) if draft else current
    if not config.base_url:
        raise MesDataError("请先填写 MES 服务地址（如 http://192.168.1.20:8080）")

    client = MesClient(config)
    try:
        response, status, attempts = client.ping()
    except MesError as exc:
        _log("test", False, str(exc), exc.attempts, exc.status_code)
        raise

    elapsed = int((time.time() - started) * 1000)
    message = "连接正常（HTTP %s）" % status
    _log("test", True, message, attempts, status)
    return {
        "ok": True,
        "vendor": config.vendor,
        "baseUrl": config.base_url,
        "statusCode": status,
        "attempts": attempts,
        "elapsedMs": elapsed,
        "message": message,
        "response": response if isinstance(response, dict) else {"raw": str(response)[:500]},
    }


# ---------------------------------------------------------------- 拉单
def pull_orders(project_id: int = 0, limit: int = 50) -> Dict[str, Any]:
    """拉取 MES 待加工工单，按 remote_id upsert 进本地 `mes_order`。

    同一张单反复拉只有一条记录，`pulled_at` 刷新；本地状态"已归档"保持不变。
    """
    started = time.time()
    config = _require_enabled()
    client = MesClient(config)
    try:
        response, status, attempts = client.list_orders(limit)
    except MesError as exc:
        _log("pull", False, str(exc), exc.attempts, exc.status_code)
        raise

    raw_orders = _extract_orders(response)
    created = updated = skipped = 0
    problems: List[str] = []
    saved: List[Dict[str, Any]] = []
    with db.get_conn() as conn:
        for item in raw_orders:
            try:
                data = _normalize_order(item)
                order_id, is_new = db.upsert_mes_order(conn, int(project_id or 0), data)
            except (MesDataError, ValueError) as exc:
                skipped += 1
                problems.append(str(exc))
                continue
            if is_new:
                created += 1
            else:
                updated += 1
            row = db.load_mes_order(conn, order_id)
            if row is not None:
                saved.append(row)

    elapsed = int((time.time() - started) * 1000)
    message = "拉取 %d 张工单：新增 %d / 更新 %d / 跳过 %d" % (
        len(raw_orders), created, updated, skipped)
    _log("pull", True, message, attempts, status)
    logger.info("MES 拉单完成：%s", message)
    return {
        "ok": True,
        "pulled": len(saved),
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "problems": problems,
        "attempts": attempts,
        "statusCode": status,
        "elapsedMs": elapsed,
        "orders": saved,
    }


# ---------------------------------------------------------------- 回传
def _read_files(conn, order_id: int) -> Tuple[List[Dict[str, Any]], List[Tuple[str, str, bytes]],
                                              List[str]]:
    """读加工文件：返回 (给 MES 看的清单, 真正要上传的二进制, 跳过说明)。

    文件被用户手工删掉、或单文件超大时只跳过该文件，不阻断整次回传，
    跳过原因会进 `notes`，前端在推送结果里能看到。
    """
    rows = db.load_work_order_files(conn, int(order_id))
    meta: List[Dict[str, Any]] = []
    uploads: List[Tuple[str, str, bytes]] = []
    notes: List[str] = []
    for index, row in enumerate(rows[:MAX_FILES]):
        path = row.get("path") or ""
        name = os.path.basename(path) or ("file_%s" % row.get("id"))
        try:
            if not path or not os.path.isfile(path):
                notes.append("跳过缺失文件：%s" % name)
                continue
            size = os.path.getsize(path)
            if size <= 0:
                notes.append("跳过空文件：%s" % name)
                continue
            if size > MAX_FILE_BYTES:
                notes.append("跳过超大文件：%s（%d 字节）" % (name, size))
                continue
            with open(path, "rb") as handle:
                content = handle.read()
        except OSError:
            notes.append("跳过无法读取的文件：%s" % name)
            continue
        meta.append({
            "name": name,
            "bytes": len(content),
            "format": row.get("format") or "",
            "sheetIndex": int(row.get("sheet_index") or 0),
        })
        uploads.append(("file_%s" % index, name, content))
    if len(rows) > MAX_FILES:
        notes.append("文件过多，只上传前 %d 个" % MAX_FILES)
    return meta, uploads, notes


def _usage_of(order: Dict[str, Any], stats: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """用料统计：优先用本地工单快照里的 stats（排料算出来的真数据），
    没有工单时退回 MES 单上的零件数量，保证 MES 侧字段始终齐全。
    """
    data = stats if isinstance(stats, dict) else {}
    return {
        "partCount": int(data.get("partCount") or order.get("partCount") or 0),
        "polylineCount": int(data.get("polylineCount") or 0),
        "placementCount": int(data.get("placementCount") or 0),
        "utilization": round(float(data.get("utilization") or 0.0), 4),
        "sheetWidth": round(float(data.get("sheetWidth") or 0.0), 3),
        "sheetHeight": round(float(data.get("sheetHeight") or 0.0), 3),
        "fileCount": int(data.get("fileCount") or 0),
        "fileBytes": int(data.get("fileBytes") or 0),
    }


def _find_local_order(conn, project_id: int, remote_id: str) -> Optional[Dict[str, Any]]:
    """本地工单通过 `code` 关联 MES 单号（模块 8 的工单本来就带"工件编号"）。"""
    row = conn.execute(
        "SELECT * FROM work_order WHERE project_id = ? AND code = ? ORDER BY id DESC LIMIT 1",
        (int(project_id), str(remote_id)),
    ).fetchone()
    return dict(row) if row is not None else None


def _local_work_order(conn, order: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """MES 单 ↔ 本地工单的关联视图（work_order.code == remoteId）。

    界面靠它回答两个问题：这张 MES 单有没有存成本地工单、回传时会带上哪张单的用料和文件。
    没有关联时返回 None，前端提示"先按单号建工单"，避免用户白推一次空数据。
    """
    remote_id = str(order.get("remoteId") or "").strip()
    if not remote_id:
        return None
    # projectId 为 0 的老单回退到当前工程，与 push_order 的口径保持一致
    project = int(order.get("projectId") or 0) or db.get_active_project_id(conn)
    row = _find_local_order(conn, project, remote_id)
    if row is None:
        return None
    try:
        stats = json.loads(row.get("stats") or "{}")
    except (TypeError, ValueError):
        stats = {}
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "code": row["code"] or "",
        "note": row["note"] or "",
        "createdAt": float(row["created_at"]),
        "updatedAt": float(row["updated_at"]),
        "partCount": int((stats or {}).get("partCount") or 0),
    }


def push_order(project_id: int = 0, order_id: int = 0, work_order_id: Optional[int] = None,
               include_files: Optional[bool] = None) -> Dict[str, Any]:
    """把工单信息 + 用料统计 + 加工文件推回 MES 归档。

    `workOrderId` 不传时按 MES 单号自动找本地同编号工单（code == remoteId），
    这样"拉单 → 存工单 → 推送"可以完全不手填 id。
    """
    started = time.time()
    config = _require_enabled()

    with db.get_conn() as conn:
        order = db.load_mes_order(conn, int(order_id))
        if order is None:
            raise MesDataError("MES 工单不存在 #%s" % order_id)
        remote_id = order["remoteId"]

        project = int(project_id or order.get("projectId") or 0)
        if not project:
            project = db.get_active_project_id(conn)

        row = None
        if work_order_id:
            row = db.load_work_order(conn, project, int(work_order_id))
            if row is None:
                raise MesDataError("本地工单不存在 #%s" % work_order_id)
        else:
            row = _find_local_order(conn, project, remote_id)

        stats: Dict[str, Any] = {}
        if row is not None:
            try:
                parsed = json.loads(row.get("stats") or "{}")
                stats = parsed if isinstance(parsed, dict) else {}
            except (TypeError, ValueError):
                stats = {}

        want_files = config.include_files if include_files is None else bool(include_files)
        if want_files and row is not None:
            meta, uploads, notes = _read_files(conn, int(row["id"]))
        else:
            meta, uploads, notes = [], [], []

        usage = _usage_of(order, stats)
        usage["fileCount"] = len(meta)
        usage["fileBytes"] = sum(int(item["bytes"]) for item in meta)

        work_order = None
        if row is not None:
            work_order = {
                "id": int(row["id"]),
                "name": row["name"],
                "code": row["code"] or "",
                "note": row["note"] or "",
                "createdAt": float(row["created_at"]),
                "updatedAt": float(row["updated_at"]),
                "fileCount": len(meta),
                "stats": stats,
            }

    stamp = time.time()
    payload: Dict[str, Any] = {
        "remoteId": remote_id,
        "source": "BtCAM",
        "status": "archived",
        # 幂等键：同一张单同一秒内重复提交可被 MES 去重
        "idempotencyKey": "%s-%s" % (remote_id, time.strftime("%Y%m%d%H%M%S",
                                                             time.localtime(stamp))),
        "pushedAt": _iso(stamp),
        "usage": usage,
        "workOrder": work_order,
    }
    if notes:
        payload["notes"] = notes

    client = MesClient(config)
    try:
        response, status, attempts = client.archive(remote_id, payload, uploads or None)
    except MesError as exc:
        _log("push", False, str(exc), exc.attempts, exc.status_code, order_id=int(order["id"]))
        raise

    elapsed = int((time.time() - started) * 1000)
    message = "MES 回传成功（HTTP %s，%d 个文件）" % (status, len(meta))
    with db.get_conn() as conn:
        db.mark_mes_order_archived(conn, int(order["id"]), stamp)
    _log("push", True, message, attempts, status, order_id=int(order["id"]))
    logger.info("MES 回传成功：单号 %s，%d 个文件", remote_id, len(meta))

    return {
        "ok": True,
        "orderId": int(order["id"]),
        "remoteId": remote_id,
        "workOrderId": int(row["id"]) if row is not None else None,
        "statusCode": status,
        "attempts": attempts,
        "elapsedMs": elapsed,
        "fileCount": len(meta),
        "uploadedBytes": usage["fileBytes"],
        "message": message,
        "notes": notes,
        "response": response if isinstance(response, dict) else {"raw": str(response)[:500]},
    }


# ---------------------------------------------------------------- 自动推送旁路
def auto_push(project_id: int, work_order_id: Optional[int]) -> bool:
    """导出成功后调用：已启用 + 打开自动推送 + 本地工单编号能对上 MES 单，才在后台线程回传。

    返回是否真的起了线程，仅用于日志，不参与业务判断。
    """
    try:
        config = mes_settings.load()
        if not config.enabled or not config.auto_push:
            return False
        if not work_order_id:
            return False
        with db.get_conn() as conn:
            row = db.load_work_order(conn, int(project_id), int(work_order_id))
            if row is None:
                return False
            code = str(row.get("code") or "").strip()
            if not code:
                return False
            order = db.find_mes_order_by_remote(conn, code)
        if order is None:
            logger.debug("工单编号 %s 没有对应的 MES 单，跳过自动推送", code)
            return False

        def _run() -> None:
            try:
                push_order(project_id, int(order["id"]), int(work_order_id))
            except Exception:
                logger.warning("MES 自动推送失败（工单 #%s）", work_order_id, exc_info=True)

        threading.Thread(target=_run, name="mes-auto-push", daemon=True).start()
        return True
    except Exception:
        logger.warning("MES 自动推送预检失败", exc_info=True)
        return False


# ---------------------------------------------------------------- 查询
def list_orders(conn, status: str = "", limit: int = 100) -> Dict[str, Any]:
    items = db.list_mes_orders(conn, status=status, limit=max(1, min(500, int(limit))))
    # 每行带上本地工单关联状态：前端一眼能看出哪些单还没开工单（直接推会没有文件与用料）
    for item in items:
        item["localWorkOrder"] = _local_work_order(conn, item)
    counts = db.count_mes_orders(conn)
    return {
        "items": items,
        "count": len(items),
        "archivedCount": counts["archived"],
        "linkedCount": sum(1 for item in items if item["localWorkOrder"]),
    }


def get_order(conn, order_id: int) -> Dict[str, Any]:
    order = db.load_mes_order(conn, int(order_id))
    if order is None:
        raise MesDataError("MES 工单不存在 #%s" % order_id)
    order["localWorkOrder"] = _local_work_order(conn, order)
    return order


def list_logs(conn, order_id: int = 0, limit: int = 100) -> Dict[str, Any]:
    items = db.list_mes_logs(conn, order_id=int(order_id or 0),
                             limit=max(1, min(500, int(limit))))
    return {"items": items, "count": len(items)}
