/**
 * 底部状态栏：连接状态 / 引擎信息 / 消息。
 * 引擎信息来自 /api/health，可以直观看到 OCCT、DeepNest 是否真的就绪。
 */

import { useAppStore } from '@/state/app-store';
import { APP_EDITION, APP_VERSION } from '@/version';

const CONNECTION_TEXT: Record<string, string> = {
  connecting: '连接中',
  online: '已连接',
  offline: '已断开',
};

export function StatusBar(): JSX.Element {
  const connection = useAppStore((s) => s.connection);
  const health = useAppStore((s) => s.health);
  const message = useAppStore((s) => s.message);

  return (
    <footer className="statusbar">
      <span className={`dot dot-${connection}`} />
      <span className="status-item">{CONNECTION_TEXT[connection] ?? connection}</span>
      <span className="status-sep" />
      <span className="status-item">几何 {health?.geometryEngine ?? '-'}</span>
      <span className="status-item">排料 {health?.nestingEngine ?? '-'}</span>
      <span className="status-item">计算进程 {health?.workers ?? '-'}</span>
      <span className="status-sep" />
      <span className="status-message" title={message}>{message}</span>
      <span className="status-sep" />
      {/* 版本角标：用户报障时报这一串，技术支持能直接对上是哪个包 */}
      <span className="status-version" title={`前端 ${APP_VERSION}`}>{APP_EDITION}</span>
    </footer>
  );
}
