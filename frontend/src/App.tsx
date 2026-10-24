/**
 * 应用外壳：三段式布局（工具栏 / 工作区 / 状态栏）。
 * 这里只做组装，业务逻辑分布在各组件与 render、net 层。
 */

import { useCallback, useEffect, useRef } from 'react';

import { bootstrapSession } from '@/net/session';
import type { CanvasController } from '@/render/viewport';
import { useAppStore } from '@/state/app-store';
import { AboutDialog } from '@/ui/AboutDialog';
import { AlertDialog } from '@/ui/AlertDialog';
import { LayoutPanel } from '@/ui/LayoutPanel';
import { LayerPanel } from '@/ui/LayerPanel';
import { LogPanel } from '@/ui/LogPanel';
import { MesPanel } from '@/ui/MesPanel';
import { PostEditor } from '@/ui/PostEditor';
import { PostPanel } from '@/ui/PostPanel';
import { ScrapPanel } from '@/ui/ScrapPanel';
import { StatusBar } from '@/ui/StatusBar';
import { TaskPanel } from '@/ui/TaskPanel';
import { Toolbar } from '@/ui/Toolbar';
import { ToolLibrary } from '@/ui/ToolLibrary';
import { ToolpathPanel } from '@/ui/ToolpathPanel';
import { useToolLibraryActions } from '@/ui/useToolLibraryActions';
import { Viewport } from '@/ui/Viewport';
import { WorkOrderPanel } from '@/ui/WorkOrderPanel';

export default function App(): JSX.Element {
  const controllerRef = useRef<CanvasController | null>(null);
  // 左侧面板随模式切换：图纸看图层，排料看板材与零件，刀路预览看参数与统计
  const viewMode = useAppStore((s) => s.viewMode);
  // 刀具库/模板是全局配置：应用启动拉一次，弹窗与刀路面板共用
  const { refresh: refreshToolLibrary } = useToolLibraryActions();
  // 关于弹窗与其它浮层一样挂在应用外壳上，不占工具栏分支
  const aboutOpen = useAppStore((s) => s.aboutOpen);
  const setAboutOpen = useAppStore((s) => s.setAboutOpen);

  // 只建一次 WebSocket；销毁时自动断连，避免开发热更新留下僵尸连接
  useEffect(() => bootstrapSession(), []);

  useEffect(() => {
    void refreshToolLibrary();
  }, [refreshToolLibrary]);

  const handleReady = useCallback((controller: CanvasController) => {
    controllerRef.current = controller;
  }, []);

  const handleFit = useCallback(() => {
    controllerRef.current?.fitToContent();
  }, []);

  return (
    <div className="app">
      <Toolbar onFit={handleFit} />
      <main className="workspace">
        {viewMode === 'layout'
          ? <LayoutPanel />
          : viewMode === 'preview'
            ? <ToolpathPanel />
            : <LayerPanel />}
        <Viewport onReady={handleReady} />
        <TaskPanel />
      </main>
      <StatusBar />
      <ToolLibrary />
      <PostPanel />
      <PostEditor />
      <WorkOrderPanel />
      <ScrapPanel />
      <MesPanel />
      <LogPanel />
      <AlertDialog />
      {aboutOpen && <AboutDialog onClose={() => setAboutOpen(false)} />}
    </div>
  );
}
