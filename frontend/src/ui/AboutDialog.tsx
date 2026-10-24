/**
 * 关于弹窗（版本固化信息，模块 9 日志面板的邻居）。
 *
 * 生产包关掉了开发者工具（WebView 右键"检查"），所以版本号、数据目录、
 * 引擎状态这些排障入口统一放这里：用户只要会说"我的版本是 V0.4 完整版"，
 * 技术支持就能判断是哪一版，不必让用户去翻注册表。
 *
 * 数据全部来自已有来源，不新增接口：
 *   前端版本   -> src/version.ts
 *   后端版本   -> /api/health（health.version）
 *   数据目录   -> /api/health（health.dataDir）
 *   引擎状态   -> /api/health（geometryEngine / nestingEngine）
 */

import { useEffect } from 'react';

import { useAppStore } from '@/state/app-store';
import { APP_EDITION, APP_MODULES, APP_NAME, APP_VERSION } from '@/version';

interface AboutDialogProps {
  onClose: () => void;
}

export function AboutDialog({ onClose }: AboutDialogProps): JSX.Element {
  const health = useAppStore((s) => s.health);
  const setLogOpen = useAppStore((s) => s.setLogOpen);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const openLogs = () => {
    setLogOpen(true);
    onClose();
  };

  return (
    <div
      className="modal-mask"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="modal about-dialog" role="dialog" aria-modal="true">
        <header className="modal-head">
          <span>关于 {APP_NAME}</span>
          <span className="modal-sub">{APP_EDITION}</span>
          <button type="button" className="modal-close" onClick={onClose} title="关闭">
            ×
          </button>
        </header>

        <div className="about-body">
          <div className="about-hero">
            <span className="brand-dot" />
            <div className="about-hero-text">
              <div className="about-title">
                {APP_NAME}
                <span className="about-badge">{APP_EDITION}</span>
              </div>
              <div className="about-tagline">
                2D 轮廓切割 CAM — 板材排料、轮廓刀路、NC / PLT / CUT 输出
              </div>
            </div>
          </div>

          <dl className="about-facts">
            <dt>版本号</dt>
            <dd>{APP_VERSION}</dd>
            <dt>后端版本</dt>
            <dd>{health?.version ?? '未连接'}</dd>
            <dt>几何引擎</dt>
            <dd>{health?.geometryEngine ?? '-'}</dd>
            <dt>排料引擎</dt>
            <dd>{health?.nestingEngine ?? '-'}</dd>
            <dt>数据目录</dt>
            <dd className="about-path" title={health?.dataDir}>
              {health?.dataDir ?? '-'}
            </dd>
          </dl>

          <div className="about-section-title">已交付模块（12）</div>
          <div className="about-modules">
            {APP_MODULES.map((name, index) => (
              <span key={name} className="about-module">
                <span className="about-module-no">{index + 1}</span>
                {name}
              </span>
            ))}
          </div>

          <p className="about-note">
            生产版已关闭开发者工具。排错请用工具栏「日志…」打开软件内日志面板，
            面板里的行可直接复制给技术支持。
          </p>
        </div>

        <footer className="about-actions">
          <span className="about-copyright">© 2026 BtSoft · 内部发行</span>
          <button type="button" className="btn btn-ghost" onClick={openLogs}>
            打开日志
          </button>
          <button type="button" className="btn btn-accent" onClick={onClose}>
            关闭
          </button>
        </footer>
      </div>
    </div>
  );
}
