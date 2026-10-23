/**
 * 日志面板（模块 9）。
 *
 * 数据来源有两条，最终都指向同一个环形缓冲：
 *   1) 打开面板后每 2 秒拉一次 /api/logs（过滤条件交给后端）；
 *   2) ALERT 帧即时插一条，弹窗关掉后仍能在列表里找到上下文。
 *
 * 这里只做展示与过滤，不做任何日志解析 —— 条目是后端结构化的
 * { level, source, message, detail }，文件格式变了也不影响面板。
 */

import { useEffect, useRef, useState } from 'react';

import { api, type LogEntry } from '@/net/rest';
import { useAppStore } from '@/state/app-store';

const LEVEL_TEXT: Record<string, string> = {
  debug: '调试',
  info: '信息',
  warning: '警告',
  error: '错误',
  critical: '严重',
};

/** 过滤下拉：值直接作为后端 level 参数（空串=全部） */
const LEVEL_OPTIONS: Array<{ value: string; label: string }> = [
  { value: '', label: '全部级别' },
  { value: 'info', label: '信息及以上' },
  { value: 'warning', label: '警告及以上' },
  { value: 'error', label: '错误及以上' },
  { value: 'debug', label: '含调试' },
];

/** 常用来源：后端 logger 名以这些前缀开头 */
const SOURCE_OPTIONS: Array<{ value: string; label: string }> = [
  { value: '', label: '全部来源' },
  { value: 'task', label: '任务' },
  { value: 'import', label: '导入' },
  { value: 'frontend', label: '前端' },
  { value: 'app', label: '服务' },
];

const POLL_MS = 2000;
const PAGE_LIMIT = 300;

function pad(value: number): string {
  return String(value).padStart(2, '0');
}

function formatTime(stamp: number): string {
  const date = new Date(stamp * 1000);
  if (Number.isNaN(date.getTime())) return '--:--:--';
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

export function LogPanel(): JSX.Element | null {
  const open = useAppStore((s) => s.logOpen);
  const logs = useAppStore((s) => s.logs);
  const capacity = useAppStore((s) => s.logCapacity);
  const file = useAppStore((s) => s.logFile);
  const setLogs = useAppStore((s) => s.setLogs);
  const setLogOpen = useAppStore((s) => s.setLogOpen);
  const setMessage = useAppStore((s) => s.setMessage);

  const [level, setLevel] = useState('');
  const [source, setSource] = useState('');
  const [keyword, setKeyword] = useState('');
  const [expanded, setExpanded] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [armed, setArmed] = useState(false);
  // 用户往上翻看时不强行拉到底部
  const stickRef = useRef(true);
  const listRef = useRef<HTMLDivElement | null>(null);

  const close = () => setLogOpen(false);

  /** 是否跟随最新日志（用户往上翻时置 false） */
  const setStick = (value: boolean) => {
    stickRef.current = value;
    const node = listRef.current;
    if (value && node) node.scrollTop = node.scrollHeight;
  };

  const load = async (): Promise<void> => {
    try {
      const res = await api.logs.list({
        level: level as '' | 'debug' | 'info' | 'warning' | 'error',
        source,
        keyword: keyword.trim(),
        limit: PAGE_LIMIT,
      });
      // 面板数据以服务端过滤结果为准：本地缓存里不匹配的条目会被覆盖
      setLogs(res.logs, { capacity: res.capacity, file: res.file });
    } catch (error) {
      setMessage(`日志读取失败：${(error as Error).message}`);
    }
  };

  // 打开时立即拉一次，之后定时轮询（弹窗关掉就停，避免空转）
  useEffect(() => {
    if (!open) return;
    void load();
    const timer = window.setInterval(() => void load(), POLL_MS);
    return () => window.clearInterval(timer);
    // load 依赖三个过滤条件，条件变化时重建定时器即可
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, level, source, keyword]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // 新条目到达：贴在底部时才自动滚动，避免打断阅读
  useEffect(() => {
    const node = listRef.current;
    if (!node || !stickRef.current) return;
    node.scrollTop = node.scrollHeight;
  }, [logs]);

  if (!open) return null;

  const onScroll = () => {
    const node = listRef.current;
    if (!node) return;
    stickRef.current = node.scrollHeight - node.scrollTop - node.clientHeight < 48;
  };

  const onClear = async () => {
    if (!armed) {
      setArmed(true);
      return;
    }
    setArmed(false);
    setBusy(true);
    try {
      const res = await api.logs.clear();
      setLogs([]);
      setExpanded(null);
      setMessage(`日志缓冲已清空（${res.cleared} 条，容量 ${res.capacity}）`);
    } catch (error) {
      setMessage(`清空失败：${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const onReport = async () => {
    // 手工上报一条，用于确认"前端 -> 后端日志"链路通畅
    setBusy(true);
    try {
      await api.logs.report({
        level: 'info',
        message: '前端日志面板手工写入',
        kind: 'frontend',
        source: 'frontend',
      });
      await load();
      setStick(true);
      setMessage('已写入一条前端日志');
    } catch (error) {
      setMessage(`写入失败：${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="modal-mask"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) close();
      }}
    >
      <div className="modal log-panel" role="dialog" aria-modal="true">
        <header className="modal-head">
          <span>运行日志</span>
          <span className="modal-sub">模块 9 · 图形报错 / 排料进度 / 刀路计算日志</span>
          <button type="button" className="modal-close" title="关闭" onClick={close}>×</button>
        </header>

        <div className="log-toolbar">
          <select value={level} onChange={(event) => setLevel(event.target.value)}>
            {LEVEL_OPTIONS.map((item) => (
              <option key={item.value} value={item.value}>{item.label}</option>
            ))}
          </select>
          <select value={source} onChange={(event) => setSource(event.target.value)}>
            {SOURCE_OPTIONS.map((item) => (
              <option key={item.value} value={item.value}>{item.label}</option>
            ))}
          </select>
          <input
            className="log-keyword"
            type="text"
            placeholder="搜索文本 / 来源（回车刷新）"
            value={keyword}
            onChange={(event) => setKeyword(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') void load();
            }}
          />
          <button type="button" className="btn btn-ghost" onClick={() => void load()} disabled={busy}>
            刷新
          </button>
          <button type="button" className="btn btn-ghost" onClick={() => void onReport()} disabled={busy}>
            测试写入
          </button>
          <button
            type="button"
            className={`btn${armed ? ' btn-danger' : ' btn-ghost'}`}
            onClick={() => void onClear()}
            disabled={busy}
          >
            {armed ? '再点一次确认清空' : '清空'}
          </button>
          <span className="log-count">{logs.length} 条</span>
        </div>

        <div className="log-list" ref={listRef} onScroll={onScroll}>
          {logs.length === 0 && <div className="empty">暂无日志</div>}
          {logs.map((entry: LogEntry) => (
            <div
              key={entry.id}
              className={`log-row log-${entry.level}${expanded === entry.id ? ' log-open' : ''}`}
              onClick={() => setExpanded(expanded === entry.id ? null : entry.id)}
              title="点击展开/收起明细"
            >
              <span className="log-time">{formatTime(entry.time)}</span>
              <span className={`log-level log-level-${entry.level}`}>
                {LEVEL_TEXT[entry.level] ?? entry.level}
              </span>
              <span className="log-source">{entry.source}</span>
              <span className="log-message">{entry.message}</span>
              {entry.detail && expanded === entry.id && (
                <pre className="log-detail">{entry.detail}</pre>
              )}
            </div>
          ))}
        </div>

        <footer className="log-foot">
          <span>
            最多保留最近 {capacity || '—'} 条（本次运行）
            {file ? ` · 文件：${file}` : ''}
          </span>
          <span className="log-hint">warning 及以上会弹窗提示，error 自动弹窗</span>
        </footer>
      </div>
    </div>
  );
}
