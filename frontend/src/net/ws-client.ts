/**
 * WebSocket 长连接客户端。
 *
 * 职责：
 * 1. 建立/维持与 FastAPI 的连接（指数退避重连 + 心跳保活）；
 * 2. 二进制帧的收发与分发（按帧类型注册回调）；
 * 3. 连接未就绪时的发送排队，避免 UI 早期操作丢失。
 *
 * 不负责业务语义：谁关心哪种帧，自己 on(type, handler)。
 */

import type { ConnectionState } from '@/types/geometry';
import { ChunkAssembler, decodeFrames, encodeFrames, MsgType, type Frame } from './protocol';

export type FrameHandler = (frame: Frame) => void;

const MAX_BACKOFF_MS = 10_000;
const HEARTBEAT_MS = 15_000;

export class WsClient {
  private url: string;
  private socket: WebSocket | null = null;
  private handlers = new Map<number, Set<FrameHandler>>();
  private pending: ArrayBuffer[] = [];
  private retry = 0;
  private seq = 0;
  private heartbeatTimer: number | null = null;
  private reconnectTimer: number | null = null;
  private disposed = false;
  /** 大 payload 的分片重组器：整条连接共享 */
  private assembler = new ChunkAssembler();

  private onStateChange?: (state: ConnectionState) => void;
  private onOpen?: () => void;

  constructor(url: string, options?: {
    onStateChange?: (state: ConnectionState) => void;
    onOpen?: () => void;
  }) {
    this.url = url;
    this.onStateChange = options?.onStateChange;
    this.onOpen = options?.onOpen;
  }

  // ------------------------------------------------------------ 连接
  connect(): void {
    if (this.disposed || this.socket) return;
    this.onStateChange?.('connecting');

    const socket = new WebSocket(this.url);
    socket.binaryType = 'arraybuffer';
    this.socket = socket;

    socket.onopen = () => {
      this.retry = 0;
      this.onStateChange?.('online');
      this.startHeartbeat();
      this.sendFrame(MsgType.HELLO, { clientVersion: '0.1.0' });
      this.flushPending();
      this.onOpen?.();
    };

    socket.onmessage = (event: MessageEvent<ArrayBuffer>) => {
      if (!(event.data instanceof ArrayBuffer)) return;
      try {
        for (const frame of decodeFrames(event.data)) {
          // CHUNK 帧先累积，凑齐后才会拿到真正的业务帧
          const logical = this.assembler.feed(frame);
          if (logical) this.dispatch(logical);
        }
      } catch (error) {
        console.error('[ws] 帧解析失败', error);
      }
    };

    socket.onclose = () => {
      this.socket = null;
      this.stopHeartbeat();
      this.onStateChange?.('offline');
      this.scheduleReconnect();
    };

    socket.onerror = () => {
      // onclose 会紧随其后，这里只做日志
      console.warn('[ws] 连接异常');
    };
  }

  private scheduleReconnect(): void {
    if (this.disposed || this.reconnectTimer !== null) return;
    const delay = Math.min(MAX_BACKOFF_MS, 500 * 2 ** this.retry);
    this.retry += 1;
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.heartbeatTimer = window.setInterval(() => {
      this.sendFrame(MsgType.PING, { t: Date.now() });
    }, HEARTBEAT_MS);
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer !== null) {
      window.clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  disconnect(): void {
    this.disposed = true;
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.stopHeartbeat();
    this.assembler.discard();
    this.socket?.close();
    this.socket = null;
  }

  // ------------------------------------------------------------ 收发
  get online(): boolean {
    return this.socket !== null && this.socket.readyState === WebSocket.OPEN;
  }

  sendFrame(type: number, meta?: Record<string, unknown>, payload?: ArrayBuffer): void {
    // 超过阈值时 encodeFrames 会返回多个 CHUNK 帧，逐条发送
    const frames = encodeFrames(type, meta, payload, ++this.seq);
    if (!this.online || this.socket === null) {
      // 连接就绪前的请求缓存起来，重连后自动补发
      if (this.pending.length + frames.length <= 64) this.pending.push(...frames);
      return;
    }
    for (const buffer of frames) this.socket.send(buffer);
  }

  private flushPending(): void {
    if (!this.online || this.socket === null) return;
    const queue = this.pending;
    this.pending = [];
    for (const buffer of queue) {
      this.socket.send(buffer);
    }
  }

  /** 注册帧回调，返回取消函数（React useEffect 清理用）。 */
  on(type: number, handler: FrameHandler): () => void {
    let set = this.handlers.get(type);
    if (!set) {
      set = new Set();
      this.handlers.set(type, set);
    }
    set.add(handler);
    return () => set?.delete(handler);
  }

  private dispatch(frame: Frame): void {
    const set = this.handlers.get(frame.type);
    if (!set) return;
    for (const handler of set) {
      try {
        handler(frame);
      } catch (error) {
        console.error(`[ws] 处理帧 0x${frame.type.toString(16)} 失败`, error);
      }
    }
  }
}
