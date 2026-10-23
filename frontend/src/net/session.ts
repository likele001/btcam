/**
 * 会话装配：把 WebSocket 的帧流翻译成 store 的状态变化。
 *
 * 这是前端唯一"知道协议语义"的地方，UI 组件只读 store、只调 api，
 * 想换协议或加业务帧，改这一个文件即可。
 */

import { useAppStore } from '@/state/app-store';
import { api, WS_URL, type LogEntry } from './rest';
import { decodePolylines, MsgType, type Frame } from './protocol';
import { WsClient } from './ws-client';

let client: WsClient | null = null;

export function getWsClient(): WsClient | null {
  return client;
}

/** 请求后端重新下发全量图形（返回原始图纸视图） */
export function requestGeometrySync(): void {
  client?.sendFrame(MsgType.GEOMETRY_SYNC);
}

/** 请求后端重发排料状态（写操作后广播丢失/断开重连时用） */
export function requestLayoutSync(): void {
  client?.sendFrame(MsgType.LAYOUT_SYNC);
}

/** 上报选中集，便于将来做多窗口联动 / 服务端缓存 */
export function notifySelection(ids: number[]): void {
  if (ids.length > 0) {
    client?.sendFrame(MsgType.SELECTION_CHANGED, { ids });
  }
}

function asRecord(meta: Record<string, unknown>): Record<string, unknown> {
  return meta ?? {};
}

/** ALERT 帧的 meta -> 日志条目（字段名与后端 log_store 一致） */
const LOG_LEVELS = new Set(['debug', 'info', 'warning', 'error', 'critical']);

function pickLogEntry(meta: Record<string, unknown>): LogEntry {
  const raw = String(meta.level ?? 'warning');
  return {
    id: Number(meta.id ?? 0),
    time: Number(meta.time ?? Date.now() / 1000),
    level: (LOG_LEVELS.has(raw) ? raw : 'warning') as LogEntry['level'],
    source: String(meta.source ?? 'app'),
    message: String(meta.message ?? ''),
    taskId: meta.taskId ? String(meta.taskId) : null,
    kind: meta.kind ? String(meta.kind) : null,
    detail: meta.detail ? String(meta.detail) : null,
  };
}

/**
 * 前端异常上报：直送后端日志（既入环也写文件）。
 *
 * 同一文案 5 秒内只上报一次 —— 渲染循环报错时若不加限制，
 * 一秒就能把 1000 条缓冲刷满，真正有用的日志反而被挤掉。
 */
const reportedAt = new Map<string, number>();

export function reportClientError(message: string, detail = '', kind = 'frontend'): void {
  const text = message.trim().slice(0, 500) || '未知前端异常';
  const now = Date.now();
  const last = reportedAt.get(text);
  if (last !== undefined && now - last < 5000) return;
  reportedAt.set(text, now);
  if (reportedAt.size > 100) {
    reportedAt.forEach((stamp, key) => {
      if (now - stamp > 60_000) reportedAt.delete(key);
    });
  }
  // 后端不可用时上报本身也会失败：不能抛回 window.error，否则无限递归
  api.logs
    .report({ level: 'error', message: text, detail: detail.slice(0, 2000), kind })
    .catch(() => undefined);
}

/**
 * 建连并注册所有帧处理。返回销毁函数（React effect 清理）。
 */
export function bootstrapSession(): () => void {
  const store = useAppStore;

  const ws = new WsClient(WS_URL, {
    onStateChange: (state) => {
      store.getState().setConnection(state);
      store.getState().setMessage(state === 'online' ? '后端已连接' : '与后端连接断开，正在重连…');
    },
    onOpen: () => {
      // 连上后拉一次引擎信息，状态栏展示 OCCT / DeepNest 是否就绪
      api.health()
        .then((health) => store.getState().setHealth(health))
        .catch(() => undefined);
    },
  });

  // --- 全量图形 ---
  ws.on(MsgType.GEOMETRY_DOC, (frame) => {
    const polylines = decodePolylines(frame.payload);
    store.getState().applyDocument(asRecord(frame.meta), polylines);
  });

  // --- 排料视图（模块 3）：payload 是按摆放变换后的轮廓 ---
  ws.on(MsgType.LAYOUT_STATE, (frame) => {
    const polylines = decodePolylines(frame.payload);
    store.getState().applyLayout(asRecord(frame.meta), polylines);
  });

  // --- 任务生命周期 ---
  ws.on(MsgType.TASK_SUBMITTED, (frame) => {
    const meta = asRecord(frame.meta);
    store.getState().upsertTask({
      taskId: String(meta.taskId),
      kind: String(meta.kind ?? 'unknown'),
      status: 'queued',
      progress: 0,
      message: '排队中',
    });
  });

  ws.on(MsgType.TASK_PROGRESS, (frame) => {
    const meta = asRecord(frame.meta);
    const taskId = String(meta.taskId);
    store.getState().upsertTask({
      taskId,
      status: (meta.status as 'running') ?? 'running',
      progress: Number(meta.progress ?? 0),
      message: String(meta.message ?? ''),
    });
  });

  ws.on(MsgType.TASK_RESULT, (frame) => {
    const meta = asRecord(frame.meta);
    const kind = String(meta.kind ?? '');
    store.getState().upsertTask({
      taskId: String(meta.taskId),
      status: 'done',
      progress: 1,
      message: `完成（${Number(meta.elapsedMs ?? 0)} ms）`,
      finishedAt: Date.now(),
    });
    if (frame.payload.byteLength > 0) {
      const polylines = decodePolylines(frame.payload);
      // 几何运算（偏置/求交/合并）只是换一份轮廓，不是刀路预览：
      // 走 applyPreview 会把视图切成 preview、左面板变成刀路参数。
      if (kind === 'geometry') {
        store.getState().applyGeometryResult(meta, polylines);
      } else {
        store.getState().applyPreview(meta, polylines);
      }
    }
    if (meta.stats && typeof meta.stats === 'object') {
      store.getState().setStats(meta.stats as Record<string, number>);
    }
    // 自动排料：任务层紧接着才补发 LAYOUT_STATE，而 applyLayout 在排料态
    // 会保留当前 message，所以这里给的完成提示不会被覆盖。
    if (kind === 'layout_nest') {
      const stats = (meta.stats ?? {}) as Record<string, unknown>;
      const placed = Number(stats.placedCount ?? 0);
      const unplaced = Number(stats.unplacedCount ?? 0);
      store.getState().setMessage(unplaced > 0
        ? `自动排料完成：${placed} 个落位，${unplaced} 个未排下`
        : `自动排料完成：${placed} 个零件已落位`);
    }
    store.getState().setBusy(false);
  });

  // --- 业务结果：刀路预览 / 排料结果 ---
  const onPreviewFrame = (frame: Frame): void => {
    const meta = asRecord(frame.meta);
    store.getState().upsertTask({
      taskId: String(meta.taskId ?? 'unknown'),
      status: 'done',
      progress: 1,
      message: '计算完成',
      finishedAt: Date.now(),
    });
    if (frame.payload.byteLength > 0) {
      store.getState().applyPreview(meta, decodePolylines(frame.payload));
    }
    if (meta.stats && typeof meta.stats === 'object') {
      store.getState().setStats(meta.stats as Record<string, number>);
    }
    store.getState().setBusy(false);
  };

  ws.on(MsgType.TOOLPATH_PREVIEW, onPreviewFrame);
  ws.on(MsgType.NEST_RESULT, onPreviewFrame);

  ws.on(MsgType.TASK_FAILED, (frame) => {
    const meta = asRecord(frame.meta);
    store.getState().upsertTask({
      taskId: String(meta.taskId),
      status: (meta.status as 'failed') ?? 'failed',
      message: String(meta.message ?? '未知错误'),
      finishedAt: Date.now(),
    });
    store.getState().setMessage(`任务失败：${String(meta.message ?? '')}`);
    store.getState().setBusy(false);
  });

  // --- 图形校验告警（异常线段标红；弹窗由随后的 ALERT 帧统一负责） ---
  ws.on(MsgType.GEOMETRY_ISSUES, (frame) => {
    const meta = asRecord(frame.meta);
    const segments = frame.payload.byteLength > 0 ? decodePolylines(frame.payload) : [];
    store.getState().applyIssues(meta, segments);
    const count = Number(meta.count ?? segments.length);
    if (count > 0) {
      const first = Array.isArray(meta.items) && meta.items.length > 0
        ? String((meta.items[0] as Record<string, unknown>).message ?? '')
        : '';
      store.getState().setMessage(`图形校验：${count} 处异常${first ? `（${first}）` : ''}`);
    }
  });

  ws.on(MsgType.ERROR, (frame) => {
    store.getState().setMessage(`后端错误：${String(asRecord(frame.meta).message ?? '')}`);
  });

  // --- 运行告警（模块 9）：后端 ERROR 自动推送 + 业务显式告警 ---
  ws.on(MsgType.ALERT, (frame) => {
    const entry = pickLogEntry(asRecord(frame.meta));
    store.getState().appendLog(entry);
    store.getState().pushAlert(entry);
    store.getState().setMessage(
      `${entry.level === 'error' ? '错误' : '告警'}：${entry.message}`,
    );
  });

  // --- 前端异常全局兜底：渲染报错/未捕获的 Promise 都落到后端日志 ---
  const onError = (event: ErrorEvent): void => {
    const stack = event.error instanceof Error ? event.error.stack ?? '' : '';
    reportClientError(
      event.message || '脚本错误',
      `${event.filename || 'unknown'}:${event.lineno}:${event.colno}\n${stack}`,
    );
  };
  const onRejection = (event: PromiseRejectionEvent): void => {
    const reason: unknown = event.reason;
    const message = reason instanceof Error ? reason.message : String(reason);
    const stack = reason instanceof Error ? reason.stack ?? '' : '';
    reportClientError(`未处理的异步异常：${message}`, stack, 'unhandledrejection');
  };
  window.addEventListener('error', onError);
  window.addEventListener('unhandledrejection', onRejection);

  ws.connect();
  client = ws;

  return () => {
    window.removeEventListener('error', onError);
    window.removeEventListener('unhandledrejection', onRejection);
    ws.disconnect();
    client = null;
  };
}
