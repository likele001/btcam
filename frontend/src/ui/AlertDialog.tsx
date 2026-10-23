/**
 * 全局告警弹窗（模块 9）。
 *
 * 后端把 ERROR 及以上自动推成 ALERT 帧，业务侧（如导入校验收到的
 * 自相交告警）显式 alert() 的 warning 也走同一通道。前端排队逐条确认：
 * 突发报错时一次只弹一条，其余显示"还有 N 条"，避免弹窗风暴。
 */

import { useEffect } from 'react';

import { useAppStore } from '@/state/app-store';

const LEVEL_TEXT: Record<string, string> = {
  debug: '提示',
  info: '提示',
  warning: '告警',
  error: '错误',
  critical: '严重错误',
};

function pad(value: number): string {
  return String(value).padStart(2, '0');
}

function formatTime(stamp: number): string {
  const date = new Date(stamp * 1000);
  if (Number.isNaN(date.getTime())) return '--:--:--';
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

export function AlertDialog(): JSX.Element | null {
  const alerts = useAppStore((s) => s.alerts);
  const dismiss = useAppStore((s) => s.dismissAlert);
  const setLogOpen = useAppStore((s) => s.setLogOpen);

  const entry = alerts[0] ?? null;

  useEffect(() => {
    if (!entry) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') dismiss();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [entry, dismiss]);

  if (!entry) return null;

  const viewLogs = () => {
    setLogOpen(true);
    dismiss();
  };

  return (
    <div
      className="modal-mask alert-mask"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) dismiss();
      }}
    >
      <div
        className={`modal alert-dialog alert-${entry.level}`}
        role="alertdialog"
        aria-modal="true"
      >
        <header className="alert-head">
          <span className={`alert-badge alert-level-${entry.level}`}>
            {LEVEL_TEXT[entry.level] ?? entry.level}
          </span>
          <span className="alert-title">{entry.message}</span>
        </header>

        <div className="alert-body">
          <div className="alert-meta">
            <span>来源：{entry.source}</span>
            {entry.taskId && <span>任务：{entry.taskId}</span>}
            <span>{formatTime(entry.time)}</span>
          </div>
          {entry.detail && <pre className="alert-detail">{entry.detail}</pre>}
        </div>

        <footer className="alert-actions">
          {alerts.length > 1 && (
            <span className="alert-more">还有 {alerts.length - 1} 条待确认</span>
          )}
          <button type="button" className="btn btn-ghost" onClick={viewLogs}>
            查看日志
          </button>
          <button type="button" className="btn btn-accent" onClick={dismiss}>
            知道了
          </button>
        </footer>
      </div>
    </div>
  );
}
