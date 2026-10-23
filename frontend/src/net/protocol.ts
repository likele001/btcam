/**
 * 二进制通信协议（前端实现）。
 *
 * ⚠️ 本文件与 backend/app/core/protocol.py 必须严格一致，改动务必双端同步。
 *
 * 帧结构（小端）：
 *   magic uint16 | version uint8 | type uint8 | seq uint32 | metaLen uint32 | payLen uint32
 *   之后依次是 meta(UTF-8 JSON) 与 payload(二进制几何块)
 *
 * 为什么要自己手写协议：
 *   图形动辄几十万点，JSON 序列化/解析会吃掉大量 CPU 和内存，
 *   这里点坐标始终是 Float32 内存视图，解析后零拷贝交给 PixiJS 绘制。
 */

import type { Polyline } from '@/types/geometry';

export const MAGIC = 0xca0f;
export const PROTOCOL_VERSION = 1;
export const HEADER_SIZE = 16;

const GEOM_BLOB_MAGIC = 0x31434742; // 'BGC1'
const GEOM_BLOB_HEADER_SIZE = 12;
const GEOM_POLY_HEADER_SIZE = 16;

/** 帧类型枚举，与后端 MsgType 一一对应 */
export const MsgType = {
  HELLO: 0x01,
  HELLO_ACK: 0x02,
  PING: 0x03,
  PONG: 0x04,

  GEOMETRY_SYNC: 0x10,
  GEOMETRY_DOC: 0x11,
  SELECTION_CHANGED: 0x12,
  /** 图形校验告警：payload = 异常线段（BGC1），meta = 告警明细 */
  GEOMETRY_ISSUES: 0x13,
  /** 排料视图全量下发：payload = 按摆放变换后的轮廓（BGC1），meta = 板材/零件/统计 */
  LAYOUT_STATE: 0x14,
  /** 请求重发排料状态（C->S） */
  LAYOUT_SYNC: 0x15,

  TASK_SUBMITTED: 0x20,
  TASK_PROGRESS: 0x21,
  TASK_RESULT: 0x22,
  TASK_FAILED: 0x23,

  TOOLPATH_PREVIEW: 0x30,
  NEST_RESULT: 0x31,

  /** 大数据分片（与后端 MsgType.CHUNK 对应） */
  CHUNK: 0x40,

  /** 运行告警：meta 即日志条目 { id, time, level, source, message, taskId, kind, detail } */
  ALERT: 0x50,

  ERROR: 0x7f,
} as const;

export type MsgTypeValue = (typeof MsgType)[keyof typeof MsgType];

/** 单个物理帧的 payload 上限；超过则拆成 CHUNK 帧（与后端 DEFAULT_CHUNK_BYTES 一致） */
export const CHUNK_BYTES = 1024 * 1024;
/** 单个分片流的存活时间（毫秒），超时未凑齐就丢弃 */
const CHUNK_TTL_MS = 30_000;

export interface Frame {
  type: number;
  seq: number;
  meta: Record<string, unknown>;
  payload: ArrayBuffer;
}

const encoder = new TextEncoder();
const decoder = new TextDecoder('utf-8');

/** 打包一帧 */
export function encodeFrame(
  type: number,
  meta?: Record<string, unknown>,
  payload?: ArrayBuffer,
  seq = 0,
): ArrayBuffer {
  const metaBytes = meta ? encoder.encode(JSON.stringify(meta)) : new Uint8Array(0);
  const payloadLen = payload ? payload.byteLength : 0;
  const buffer = new ArrayBuffer(HEADER_SIZE + metaBytes.byteLength + payloadLen);
  const view = new DataView(buffer);

  view.setUint16(0, MAGIC, true);
  view.setUint8(2, PROTOCOL_VERSION);
  view.setUint8(3, type);
  view.setUint32(4, seq >>> 0, true);
  view.setUint32(8, metaBytes.byteLength, true);
  view.setUint32(12, payloadLen, true);

  if (metaBytes.byteLength > 0) {
    new Uint8Array(buffer, HEADER_SIZE, metaBytes.byteLength).set(metaBytes);
  }
  if (payload && payloadLen > 0) {
    new Uint8Array(buffer, HEADER_SIZE + metaBytes.byteLength, payloadLen).set(
      new Uint8Array(payload),
    );
  }
  return buffer;
}

/**
 * 把一个逻辑帧编码成 1..N 个物理帧。
 *
 * payload 不超过阈值时返回单帧；超过则切成 CHUNK 帧：
 *   首片 meta = { sid, i:0, n, t:<原类型>, meta:<原 meta> }
 *   后续片   meta = { sid, i, n }
 * 接收端用 ChunkAssembler 拼回。
 */
export function encodeFrames(
  type: number,
  meta?: Record<string, unknown>,
  payload?: ArrayBuffer,
  seq = 0,
  limit = CHUNK_BYTES,
): ArrayBuffer[] {
  const total = payload ? payload.byteLength : 0;
  if (total <= limit) {
    return [encodeFrame(type, meta, payload, seq)];
  }

  const count = Math.ceil(total / limit);
  const sid = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
  const frames: ArrayBuffer[] = [];
  for (let i = 0; i < count; i += 1) {
    const piece = payload!.slice(i * limit, Math.min((i + 1) * limit, total));
    const head: Record<string, unknown> = { sid, i, n: count, t: type };
    if (i === 0) head.meta = meta ?? {};
    frames.push(encodeFrame(MsgType.CHUNK, head, piece, seq));
  }
  return frames;
}

interface ChunkStream {
  total: number;
  parts: Array<ArrayBuffer | null>;
  size: number;
  meta: Record<string, unknown>;
  type: number;
  seq: number;
  started: number;
}

/**
 * 接收端分片重组器。
 *
 * feed() 对普通帧原样返回；对 CHUNK 帧累积到齐后返回重组帧，未到齐返回 null。
 * 超时或异常的分片流会被丢弃，不会无限占内存。
 */
export class ChunkAssembler {
  private streams = new Map<string, ChunkStream>();

  discard(): void {
    this.streams.clear();
  }

  feed(frame: Frame): Frame | null {
    if (frame.type !== MsgType.CHUNK) return frame;

    const now = Date.now();
    this.expire(now);

    const sid = String(frame.meta.sid ?? '');
    const index = Number(frame.meta.i ?? 0);
    const total = Number(frame.meta.n ?? 0);
    if (!sid || total <= 0 || index < 0 || index >= total) {
      throw new Error('分片头非法');
    }

    let stream = this.streams.get(sid);
    if (!stream) {
      stream = {
        total,
        parts: new Array<ArrayBuffer | null>(total).fill(null),
        size: 0,
        meta: (frame.meta.meta as Record<string, unknown>) ?? {},
        type: Number(frame.meta.t ?? MsgType.ERROR),
        seq: frame.seq,
        started: now,
      };
      this.streams.set(sid, stream);
    } else if (stream.total !== total) {
      this.streams.delete(sid);
      throw new Error('分片总数不一致');
    }

    if (stream.parts[index] === null) {
      stream.parts[index] = frame.payload;
      stream.size += frame.payload.byteLength;
    }

    if (stream.parts.some((p) => p === null)) return null;

    this.streams.delete(sid);
    const merged = new Uint8Array(stream.size);
    let offset = 0;
    for (const part of stream.parts) {
      const bytes = new Uint8Array(part as ArrayBuffer);
      merged.set(bytes, offset);
      offset += bytes.byteLength;
    }
    return { type: stream.type, seq: stream.seq, meta: stream.meta, payload: merged.buffer };
  }

  private expire(now: number): void {
    if (this.streams.size === 0) return;
    for (const [sid, stream] of this.streams) {
      if (now - stream.started > CHUNK_TTL_MS) this.streams.delete(sid);
    }
  }
}

/**
 * 解析一个 WS 消息中的全部帧。
 * WS 保证消息边界，但允许一条消息里塞多帧（批量下发时更省系统调用）。
 */
export function decodeFrames(buffer: ArrayBuffer): Frame[] {
  const view = new DataView(buffer);
  const frames: Frame[] = [];
  let offset = 0;

  while (offset + HEADER_SIZE <= buffer.byteLength) {
    const magic = view.getUint16(offset, true);
    if (magic !== MAGIC) {
      throw new Error(`帧魔数错误: 0x${magic.toString(16)}`);
    }
    const type = view.getUint8(offset + 3);
    const seq = view.getUint32(offset + 4, true);
    const metaLen = view.getUint32(offset + 8, true);
    const payLen = view.getUint32(offset + 12, true);
    const total = HEADER_SIZE + metaLen + payLen;
    if (offset + total > buffer.byteLength) {
      throw new Error('帧被截断');
    }

    const metaStart = offset + HEADER_SIZE;
    const metaText = metaLen > 0 ? decoder.decode(new Uint8Array(buffer, metaStart, metaLen)) : '';
    const meta = metaText ? (JSON.parse(metaText) as Record<string, unknown>) : {};

    // slice 而不是 subarray：保证 payload 起始地址 4 字节对齐，才能直接建 Float32Array
    const payload = payLen > 0
      ? buffer.slice(metaStart + metaLen, metaStart + metaLen + payLen)
      : new ArrayBuffer(0);

    frames.push({ type, seq, meta, payload });
    offset += total;
  }
  return frames;
}

/** 解析 BGC1 几何块 */
export function decodePolylines(payload: ArrayBuffer): Polyline[] {
  if (payload.byteLength < GEOM_BLOB_HEADER_SIZE) return [];
  const view = new DataView(payload);
  const magic = view.getUint32(0, true);
  if (magic !== GEOM_BLOB_MAGIC) {
    throw new Error('几何块魔数错误');
  }
  const count = view.getUint32(8, true);
  const result: Polyline[] = [];
  let offset = GEOM_BLOB_HEADER_SIZE;

  for (let i = 0; i < count; i += 1) {
    const id = view.getUint32(offset, true);
    const layerId = view.getUint32(offset + 4, true);
    const flags = view.getUint8(offset + 8);
    const pointCount = view.getUint32(offset + 12, true);
    offset += GEOM_POLY_HEADER_SIZE;

    // 直接复用底层内存，不做逐点拷贝
    const pts = new Float32Array(payload, offset, pointCount * 2);
    offset += pointCount * 8;

    result.push({
      id,
      layerId,
      closed: (flags & 1) === 1,
      isHole: (flags & 2) === 2,
      pts,
    });
  }
  return result;
}

/** 0xRRGGBB 转 Pixi 需要的数值色（保留以便后续做图层调色） */
export function colorToHex(color: number): string {
  return `#${color.toString(16).padStart(6, '0')}`;
}
