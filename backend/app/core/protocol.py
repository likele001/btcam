"""前后端统一二进制通信协议（服务端实现）。

帧结构（小端 little-endian）：

    +---------+--------+--------+--------+--------+--------+
    | magic   | ver    | type   | seq    | metaLen| payLen |
    | uint16  | uint8  | uint8  | uint32 | uint32 | uint32 |
    +---------+--------+--------+--------+--------+--------+
    | meta (UTF-8 JSON, 小体积，<= 64KB) | payload (二进制图解块) |

设计要点：
1. 图形点数据永远走 payload 二进制块，meta 只放 taskId / 进度 / 状态等小字段，
   避免大 JSON 造成的序列化与 GC 压力；
2. 帧头定长 16 字节，解析时一次 read 即可确定整帧长度；
3. 本文件必须与 frontend/src/net/protocol.ts 完全对齐，改动需双端同步。

几何二进制块（payload）格式 "BGC1"：

    magic     uint32 = 0x31434742 ('BGC1')
    version   uint16 = 1
    reserved  uint16 = 0
    polyCount uint32
    ------------------------------------------------ 重复 polyCount 次
    id        uint32
    layerId   uint32
    flags     uint8   (bit0 = closed)
    pad[3]    uint8
    ptCount   uint32
    pts       float32[ptCount * 2]  (x0,y0,x1,y1,...)

大数据分片：
    单个逻辑帧的 payload 超过 CHUNK_BYTES 时，发送方自动切成 N 个物理帧，
    帧类型统一为 CHUNK，首片 meta 携带原帧类型与原 meta，后续片只带流信息。
    接收端用 ChunkAssembler 拼回逻辑帧后再交给业务处理。
    这样超大图形/千块板排料结果不会造成单条 WS 消息过大（浏览器/代理常有限制），
    也便于边收边重组、避免一次性分配巨大内存。
"""

from __future__ import annotations

import json
import os
import struct
import time
import uuid
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from app.models.geometry import Polyline

# ---------------------------------------------------------------- 常量
MAGIC = 0xCA0F
PROTOCOL_VERSION = 1
HEADER = struct.Struct("<HBBI II".replace(" ", ""))  # 16 bytes
HEADER_SIZE = HEADER.size
MAX_META_BYTES = 64 * 1024           # meta 上限，防止误传大 JSON
MAX_PAYLOAD_BYTES = 256 * 1024 * 1024

GEOM_BLOB_MAGIC = 0x31434742
GEOM_BLOB_HEADER = struct.Struct("<IHHI")   # magic, version, reserved, polyCount
GEOM_POLY_HEADER = struct.Struct("<IIB3xI")  # id, layerId, flags, ptCount

# ---------------------------------------------------------------- 分片
# 单个物理帧的 payload 上限（字节）。超过则切成 CHUNK 帧；
# 1MB 是经验值：既不会把消息切得太碎，又能避开浏览器/代理的单条消息上限。
DEFAULT_CHUNK_BYTES = 1024 * 1024
# 重组缓冲上限，防止异常数据把内存吃光
MAX_REASSEMBLED_BYTES = 512 * 1024 * 1024
# 单个分片流的存活时间（秒），超时未凑齐则丢弃
CHUNK_STREAM_TTL = 30.0


def chunk_bytes() -> int:
    """当前分片阈值；BTCAM_CHUNK_BYTES 可覆盖（测试时调小可覆盖分片路径）。"""
    try:
        value = int(os.environ.get("BTCAM_CHUNK_BYTES", DEFAULT_CHUNK_BYTES))
    except (TypeError, ValueError):
        return DEFAULT_CHUNK_BYTES
    return max(1024, value)


class MsgType(IntEnum):
    """帧类型。高 4 bit 区分方向域，便于日志过滤。"""

    # --- 连接控制 0x0x
    HELLO = 0x01            # C->S 握手，meta 携带 clientVersion
    HELLO_ACK = 0x02        # S->C 握手回执
    PING = 0x03
    PONG = 0x04

    # --- 图形同步 0x1x
    GEOMETRY_SYNC = 0x10    # C->S 请求全量同步（可带 camera 信息）
    GEOMETRY_DOC = 0x11     # S->C 全量图形下发（二进制块）
    SELECTION_CHANGED = 0x12  # C->S 选中集变化（meta only）
    GEOMETRY_ISSUES = 0x13  # S->C 图形校验告警（payload=异常线段，meta=明细）
    LAYOUT_STATE = 0x14     # S->C 排料视图全量下发（payload=按摆放变换后的轮廓）
    LAYOUT_SYNC = 0x15      # C->S 请求重发排料状态

    # --- 异步任务 0x2x
    TASK_SUBMITTED = 0x20   # S->C 任务已入队
    TASK_PROGRESS = 0x21    # S->C 进度（meta only）
    TASK_RESULT = 0x22      # S->C 结果（可能含二进制块）
    TASK_FAILED = 0x23      # S->C 失败

    # --- 业务结果 0x3x
    TOOLPATH_PREVIEW = 0x30  # 刀路预览（刀路几何 + 颜色分层）
    NEST_RESULT = 0x31       # 排料结果（实例变换矩阵 + 轮廓）

    # --- 传输控制 0x4x
    CHUNK = 0x40             # 大数据分片（见模块顶部说明）

    # --- 日志告警 0x5x
    ALERT = 0x50             # S->C 运行告警（meta=日志条目，前端弹窗提示）

    # --- 异常 0x7x
    ERROR = 0x7F


@dataclass
class Frame:
    """解析后的完整帧。"""

    type: MsgType
    seq: int
    meta: Dict[str, Any]
    payload: bytes

    @property
    def payload_view(self) -> memoryview:
        return memoryview(self.payload)


# ---------------------------------------------------------------- 编码
def encode_frame(
    msg_type: MsgType,
    meta: Dict[str, Any] | None = None,
    payload: bytes | bytearray | memoryview = b"",
    seq: int = 0,
) -> bytes:
    """把业务数据打包成一帧字节流。"""
    meta_bytes = b""
    if meta:
        meta_bytes = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(meta_bytes) > MAX_META_BYTES:
            raise ValueError(f"meta 过大({len(meta_bytes)}B)，图形数据必须走二进制 payload")

    payload_bytes = bytes(payload)
    header = HEADER.pack(
        MAGIC, PROTOCOL_VERSION, int(msg_type), seq & 0xFFFFFFFF,
        len(meta_bytes), len(payload_bytes),
    )
    return b"".join((header, meta_bytes, payload_bytes))


def encode_polylines(polylines: Iterable[Polyline]) -> bytes:
    """把轮廓集合编码成 BGC1 二进制块。

    点数据使用 numpy 连续内存一次性 tobytes()，不经过 Python 循环，
    10 万点级别的下发在毫秒量级。
    """
    items: List[Polyline] = [p for p in polylines if p.point_count > 0]
    chunks: List[bytes] = [GEOM_BLOB_HEADER.pack(GEOM_BLOB_MAGIC, 1, 0, len(items))]

    for poly in items:
        # bit0 = closed，bit1 = 内孔（导入修复阶段判定）
        flags = (1 if poly.closed else 0) | (2 if poly.is_hole else 0)
        pts = np.ascontiguousarray(poly.pts, dtype="<f4")
        chunks.append(GEOM_POLY_HEADER.pack(poly.id, poly.layer_id, flags, poly.point_count))
        chunks.append(pts.tobytes())
    return b"".join(chunks)


def encode_transforms(transforms: Sequence[Tuple[float, float, float]]) -> bytes:
    """排料实例变换矩阵块：每条 (tx, ty, rot) 三个 float32。"""
    return np.asarray(transforms, dtype="<f4").reshape(-1, 3).tobytes()


def iter_frames(
    msg_type: MsgType,
    meta: Optional[Dict[str, Any]] = None,
    payload: bytes = b"",
    seq: int = 0,
    limit: Optional[int] = None,
) -> Iterable[bytes]:
    """把一个逻辑帧编码成 1..N 个物理帧。

    payload 不超过阈值时直接产出单帧；超过则切成 CHUNK 帧：
        首片 meta = {sid, i:0, n, t:<原类型>, meta:<原meta>}
        后续片 meta = {sid, i, n}
    接收端用 ChunkAssembler 拼回。
    """
    size = limit or chunk_bytes()
    total_payload = len(payload)
    if total_payload <= size:
        yield encode_frame(msg_type, meta, payload, seq)
        return

    stream_id = uuid.uuid4().hex[:12]
    total = (total_payload + size - 1) // size
    for index in range(total):
        piece = payload[index * size:(index + 1) * size]
        head: Dict[str, Any] = {"sid": stream_id, "i": index, "n": total, "t": int(msg_type)}
        if index == 0:
            head["meta"] = meta or {}
        yield encode_frame(MsgType.CHUNK, head, piece, seq)


class ChunkAssembler:
    """接收端分片重组器（每个连接一个实例，非线程安全）。

    feed() 对普通帧原样返回；对 CHUNK 帧累积到齐后返回重组结果，
    未到齐返回 None。超过 TTL 或总大小上限的流会被丢弃。
    """

    def __init__(self, ttl: float = CHUNK_STREAM_TTL,
                 max_bytes: int = MAX_REASSEMBLED_BYTES) -> None:
        self._ttl = ttl
        self._max_bytes = max_bytes
        # sid -> {total, received, size, parts, meta, type, seq, started}
        self._streams: Dict[str, Dict[str, Any]] = {}

    def discard(self) -> None:
        """连接断开时清掉未完成的流，避免占内存。"""
        self._streams.clear()

    def feed(self, msg_type: int, meta: Dict[str, Any], payload: bytes,
             seq: int = 0) -> Optional[Tuple[int, Dict[str, Any], bytes, int]]:
        if msg_type != MsgType.CHUNK:
            return int(msg_type), meta, payload, seq

        now = time.monotonic()
        self._expire(now)

        sid = str(meta.get("sid", ""))
        index = int(meta.get("i", 0))
        total = int(meta.get("n", 0))
        if not sid or total <= 0 or not (0 <= index < total):
            raise ValueError("分片头非法: %r" % (meta,))

        stream = self._streams.get(sid)
        if stream is None:
            stream = {
                "total": total,
                "parts": [None] * total,   # type: List[Optional[bytes]]
                "size": 0,
                "meta": meta.get("meta") or {},
                "type": int(meta.get("t", MsgType.ERROR)),
                "seq": seq,
                "started": now,
            }
            self._streams[sid] = stream
        elif stream["total"] != total:
            self._streams.pop(sid, None)
            raise ValueError("分片总数不一致: sid=%s" % sid)

        if stream["parts"][index] is None:
            stream["parts"][index] = payload
            stream["size"] += len(payload)

        if stream["size"] > self._max_bytes:
            self._streams.pop(sid, None)
            raise ValueError("分片重组超过上限 %d 字节" % self._max_bytes)

        if any(p is None for p in stream["parts"]):
            return None

        self._streams.pop(sid, None)
        return (stream["type"], stream["meta"], b"".join(stream["parts"]), stream["seq"])

    def _expire(self, now: float) -> None:
        if not self._streams:
            return
        dead = [sid for sid, s in self._streams.items() if now - s["started"] > self._ttl]
        for sid in dead:
            self._streams.pop(sid, None)


# ---------------------------------------------------------------- 解码
def decode_header(buf: bytes) -> Tuple[int, int, int, int, int]:
    """返回 (msgType, seq, metaLen, payloadLen, totalLen)。"""
    if len(buf) < HEADER_SIZE:
        raise ValueError("帧头不完整")
    magic, _ver, msg_type, seq, meta_len, pay_len = HEADER.unpack_from(buf, 0)
    if magic != MAGIC:
        raise ValueError(f"帧魔数错误: 0x{magic:04X}")
    return msg_type, seq, meta_len, pay_len, HEADER_SIZE + meta_len + pay_len


def decode_meta(raw: bytes) -> Dict[str, Any]:
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def decode_polylines(payload: bytes) -> List[Polyline]:
    """解析 BGC1 二进制块，用于接收前端上传的图形（导入 DXF 之外的补充通道）。"""
    if len(payload) < GEOM_BLOB_HEADER.size:
        return []
    magic, _ver, _rsv, count = GEOM_BLOB_HEADER.unpack_from(payload, 0)
    if magic != GEOM_BLOB_MAGIC:
        raise ValueError("几何块魔数错误")

    offset = GEOM_BLOB_HEADER.size
    result: List[Polyline] = []
    for _ in range(count):
        pid, layer_id, flags, pt_count = GEOM_POLY_HEADER.unpack_from(payload, offset)
        offset += GEOM_POLY_HEADER.size
        nbytes = pt_count * 2 * 4
        arr = np.frombuffer(payload, dtype="<f4", count=pt_count * 2, offset=offset)
        offset += nbytes
        result.append(
            Polyline(pid, layer_id, arr.reshape(-1, 2).copy(), bool(flags & 1), bool(flags & 2))
        )
    return result
