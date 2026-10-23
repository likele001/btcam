"""WebSocket 端点：图形同步与增量推送的唯一长连接入口。

为什么用 WS 而不是 HTTP：
- 排料/刀路任务的进度需要主动推送，轮询太浪费；
- 图形二进制帧可以连续下发，不必反复建连、省一次 JSON 解析。

注意：接收只认二进制帧（binary），文本帧一律拒绝 —— 强制前后端走协议，
避免团队里有人图省事直接发 JSON。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.api.routes_layout import push_layout
from app.config import settings
from app.core import protocol
from app.core.hub import hub
from app.core.protocol import MsgType
from app.core.tasks import task_queue
from app.models import db
from app.services.geometry_service import load_document

logger = logging.getLogger(__name__)

router = APIRouter()

# 单个 WS 消息上限（排料结果最大也就几十 MB）
MAX_FRAME_BYTES = 256 * 1024 * 1024


def build_document_frame(project_id: Optional[int] = None,
                         extra: Optional[dict] = None) -> tuple:
    """同步组装一份全量图形帧，返回 (projectId, meta, payload)。

    为什么单独抽出来：导入任务在计算线程里跑，不能 await，
    但它必须把修复后的最新文档推给前端，所以要和 push_document
    共用同一套 meta 组装逻辑，避免两边字段对不上。
    """
    with db.get_conn() as conn:
        if project_id is None:
            project_id = db.get_active_project_id(conn)
        doc = load_document(conn, project_id)
        part_rows = db.load_parts(conn, project_id)

    # 轮廓按零件归组：二进制块里不存 part_id（避免改协议），
    # 改用 meta 里的 parts 告诉前端"哪些轮廓属于哪个零件"。
    part_polyline_ids: dict = {}
    for poly in doc.polylines:
        part_polyline_ids.setdefault(poly.part_id, []).append(poly.id)

    meta = {
        "projectId": project_id,
        "bbox": list(doc.bbox()),
        "layers": [
            {"id": l.id, "name": l.name, "color": l.color,
             "operation": l.operation, "visible": l.visible, "locked": l.locked}
            for l in doc.layers
        ],
        "parts": [
            {"id": r["id"], "name": r["name"], "source": r["source"],
             "bbox": r["bbox"], "polylineIds": part_polyline_ids.get(r["id"], [])}
            for r in part_rows
        ],
        "polylineCount": len(doc.polylines),
        "pointCount": sum(p.point_count for p in doc.polylines),
    }
    if extra:
        meta.update(extra)
    return project_id, meta, protocol.encode_polylines(doc.polylines)


async def push_document(ws: Optional[WebSocket] = None, seq: int = 0) -> None:
    """把当前工程全量图形以二进制帧下发；ws=None 时广播给所有客户端。"""
    _project_id, meta, payload = build_document_frame()
    if ws is None:
        await hub.broadcast(MsgType.GEOMETRY_DOC, meta, payload, seq)
    else:
        await hub.send_frame(ws, MsgType.GEOMETRY_DOC, meta, payload, seq)


async def _handle_frame(ws: WebSocket, msg_type: int, seq: int,
                        meta: dict, payload: bytes) -> None:
    """按帧类型分发。所有耗时操作都必须走 task_queue，绝不在这里同步计算。"""
    if msg_type == MsgType.HELLO:
        await hub.send_frame(ws, MsgType.HELLO_ACK, {
            "server": "BtCAM",
            "protocol": protocol.PROTOCOL_VERSION,
            "clientVersion": meta.get("clientVersion", "unknown"),
            "projectId": None,
        }, seq=seq)
        await push_document(ws, seq)

    elif msg_type == MsgType.PING:
        await hub.send_frame(ws, MsgType.PONG, {"t": meta.get("t")}, seq=seq)

    elif msg_type == MsgType.GEOMETRY_SYNC:
        await push_document(ws, seq)

    elif msg_type == MsgType.LAYOUT_SYNC:
        await push_layout(ws, seq=seq)

    elif msg_type == MsgType.SELECTION_CHANGED:
        # 选中集目前只在前端维护，服务端仅记录日志，后续可做多窗口联动
        logger.debug("选中集变化: %s", meta.get("ids"))

    else:
        logger.warning("未知帧类型: 0x%02X", msg_type)
        await hub.send_frame(ws, MsgType.ERROR,
                             {"message": f"未知帧类型 0x{msg_type:02X}"}, seq=seq)


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    hub.bind_loop(asyncio.get_running_loop())
    await hub.register(websocket)
    # 每个连接独立的重组器：大 payload 被拆成 CHUNK 帧后在这里拼回逻辑帧
    assembler = protocol.ChunkAssembler()

    try:
        while True:
            data = await websocket.receive_bytes()
            if len(data) > MAX_FRAME_BYTES:
                await hub.send_frame(websocket, MsgType.ERROR, {"message": "帧过大"})
                continue

            # 一条 WS 消息里可能塞了多帧，循环解析
            offset = 0
            while offset < len(data):
                msg_type, seq, meta_len, pay_len, total = protocol.decode_header(data[offset:])
                if len(data) - offset < total:
                    raise ValueError("帧被截断")
                meta = protocol.decode_meta(data[offset + protocol.HEADER_SIZE:
                                                 offset + protocol.HEADER_SIZE + meta_len])
                payload = data[offset + protocol.HEADER_SIZE + meta_len: offset + total]
                offset += total

                logical = assembler.feed(msg_type, meta, payload, seq)
                if logical is None:       # 分片未到齐，继续等下一片
                    continue
                frame_type, frame_meta, frame_payload, frame_seq = logical
                try:
                    await _handle_frame(websocket, frame_type, frame_seq, frame_meta, frame_payload)
                except Exception as exc:      # 单帧业务异常不打断连接
                    logger.exception("处理帧 0x%02X 失败", frame_type)
                    await hub.send_frame(websocket, MsgType.ERROR,
                                         {"message": str(exc)}, seq=frame_seq)

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.info("WS 连接异常关闭: %s", exc)
    finally:
        assembler.discard()
        await hub.unregister(websocket)
