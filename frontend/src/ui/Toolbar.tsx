/**
 * 顶部工具栏：所有"发起计算/导出"的入口。
 *
 * 这里只做参数组装与调用，不含任何几何算法。
 * 计算类接口一律异步返回 taskId，结果通过 WebSocket 推送回来。
 */

import { useCallback, useRef, type ChangeEvent } from 'react';

import { api } from '@/net/rest';
import { requestGeometrySync } from '@/net/session';
import { useAppStore } from '@/state/app-store';
import { useLayoutActions } from './useLayoutActions';
import { useMesActions } from './useMesActions';
import { usePostActions } from './usePostActions';
import { useScrapActions } from './useScrapActions';
import { useToolpathActions } from './useToolpathActions';
import { useToolLibraryActions } from './useToolLibraryActions';
import { useWorkOrderActions } from './useWorkOrderActions';

interface ToolbarProps {
  onFit: () => void;
}

export function Toolbar({ onFit }: ToolbarProps): JSX.Element {
  const selection = useAppStore((s) => s.selection);
  const busy = useAppStore((s) => s.busy);
  const viewMode = useAppStore((s) => s.viewMode);
  const projectId = useAppStore((s) => s.projectId);
  const tasks = useAppStore((s) => s.tasks);
  const issues = useAppStore((s) => s.issues);
  const setBusy = useAppStore((s) => s.setBusy);
  const setMessage = useAppStore((s) => s.setMessage);
  const setLogOpen = useAppStore((s) => s.setLogOpen);
  const setAboutOpen = useAppStore((s) => s.setAboutOpen);

  // 隐藏的文件选择框：Tauri 窗口内弹出原生文件对话框
  const fileRef = useRef<HTMLInputElement | null>(null);

  // 排料模式入口（模块 3）：打开后左侧面板切成板材/零件，画布画板材
  const { open: openLayout, nest } = useLayoutActions();
  // 刀路生成（模块 5）：参数在左侧面板编辑，这里只负责发起
  const { generate: generateToolpath } = useToolpathActions();
  // 刀具库（模块 6）：弹窗增删改查，刀路面板通过 toolId 引用
  const { open: openToolLibrary } = useToolLibraryActions();
  // 导出与后处理（模块 7）：快捷按钮走内置模板，弹窗里可换自定义机床
  const { open: openPost, exportNow } = usePostActions();
  // 工单存档（模块 8）：保存/检索/重复打开
  const { open: openWorkOrders } = useWorkOrderActions();
  // 余料库（模块 10）：存余料 / 选余料当板材 / 解绑回整板
  const { open: openScrap } = useScrapActions();
  // MES 对接（模块 12）：可选件，离线时打开也不影响 CAM
  const { open: openMes } = useMesActions();

  /** 最近一次导入任务：它的进度就是导入进度条的数据源 */
  const importTask = tasks.find((task) => task.kind === 'import') ?? null;
  const importRunning = importTask !== null
    && (importTask.status === 'queued' || importTask.status === 'running');

  const guard = useCallback(
    async (label: string, action: () => Promise<void>) => {
      setBusy(true);
      setMessage(`${label}…`);
      try {
        await action();
      } catch (error) {
        setMessage(`${label}失败：${(error as Error).message}`);
      } finally {
        // 不论成败都解锁按钮；长耗时计算的进度由任务面板单独展示，
        // 不能把 busy 挂在“等任务结果”上，否则 WS 断开会永久卡住
        setBusy(false);
      }
    },
    [setBusy, setMessage],
  );

  const onDemo = () =>
    guard('载入示例图形', async () => {
      await api.loadDemo();
      setMessage('示例图形已载入');
    });

  /** 点“导入 DXF”按钮：只弹文件框，不做任何解析 */
  const onPickDxf = () => fileRef.current?.click();

  /** 选完文件：把二进制交给后端，解析/修复/校验全在后端完成 */
  const onDxfSelected = (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files ?? []);
    // 先清空 value：同一个文件连续导入两次也要能触发 change
    event.target.value = '';
    if (files.length === 0) return;
    // 导入新图纸 = 新任务，解除与历史工单的关联，避免导出归档到旧工单
    useAppStore.getState().setActiveWorkOrder(null);
    void guard('导入 DXF', async () => {
      const result = await api.importDxf(files, { replace: true }, projectId ?? undefined);
      setMessage(`已提交 ${files.length} 个文件，等待后端解析（${result.taskId}）`);
    });
  };

  const onOffset = () =>
    guard('轮廓偏置', async () => {
      const result = await api.offset({
        projectId: projectId ?? undefined,
        ids: selection,
        distance: 1.0,
        join: 'round',
      });
      setMessage(`偏置任务已提交：${result.taskId}`);
    });

  const onNest = () =>
    guard('自动排料', async () => {
      // 模块 4：结果写回当前排料板，完成后后端广播 LAYOUT_STATE
      await nest('auto', true);
    });

  const onToolpath = () => guard('刀路生成', generateToolpath);

  const onExport = (format: 'nc' | 'plt' | 'cut' | 'dxf') =>
    guard(`导出 ${format.toUpperCase()}`, async () => {
      // 导出复用最近一次刀路请求，保证与预览完全一致；
      // DXF 是图形导出，没有内置 format 映射，直接指定内置处理器
      await exportNow({
        format,
        processorId: format === 'dxf' ? 'generic_dxf' : undefined,
        name: 'BT_CAM',
      });
    });

  return (
    <header className="toolbar">
      <div className="toolbar-brand">
        <span className="brand-dot" />
        BtCAM
        <span className="brand-sub">2D 轮廓切割</span>
      </div>

      <div className="toolbar-group">
        <input
          ref={fileRef}
          type="file"
          accept=".dxf"
          multiple
          hidden
          onChange={onDxfSelected}
        />
        <button type="button" className="btn btn-accent" onClick={onPickDxf} disabled={busy}>
          导入 DXF
        </button>
        <button type="button" className="btn" onClick={onDemo} disabled={busy}>
          载入示例
        </button>
        <button type="button" className="btn" onClick={onFit}>
          适配视图
        </button>
        <button type="button" className="btn btn-accent" onClick={() => void openLayout()} disabled={busy}>
          排料模式
        </button>
        <button
          type="button"
          className="btn"
          onClick={() => { requestGeometrySync(); setMessage('已请求重新同步图纸'); }}
          disabled={viewMode === 'doc'}
        >
          返回图纸
        </button>
      </div>

      <div className="toolbar-group">
        <button type="button" className="btn" onClick={openToolLibrary} disabled={busy}>
          刀具库
        </button>
        <button type="button" className="btn btn-accent" onClick={onOffset} disabled={busy}>
          偏置 1mm
        </button>
        <button type="button" className="btn btn-accent" onClick={onNest} disabled={busy}>
          自动排料
        </button>
        <button type="button" className="btn btn-accent" onClick={onToolpath} disabled={busy}>
          生成刀路
        </button>
      </div>

      <div className="toolbar-group toolbar-right">
        {importRunning && importTask && (
          <div className="toolbar-progress" title={importTask.message}>
            <div className="progress">
              <div
                className="progress-bar"
                style={{ width: `${Math.round(importTask.progress * 100)}%` }}
              />
            </div>
            <span className="toolbar-progress-text">
              导入 {Math.round(importTask.progress * 100)}%
            </span>
          </div>
        )}
        {issues.length > 0 && (
          <span className="toolbar-issues" title="图形校验异常（画布已标红）">
            异常 {issues.length}
          </span>
        )}
        <button type="button" className="btn" onClick={() => setLogOpen(true)}>
          日志…
        </button>
        <button type="button" className="btn" onClick={() => void openWorkOrders()}>
          工单…
        </button>
        <button type="button" className="btn" onClick={openScrap}>
          余料…
        </button>
        <button type="button" className="btn" onClick={openMes}>
          MES…
        </button>
        <button type="button" className="btn" onClick={openPost}>
          后处理…
        </button>
        <button type="button" className="btn" onClick={() => setAboutOpen(true)} title="版本信息">
          关于…
        </button>
        <span className="toolbar-hint">导出</span>
        {(['nc', 'plt', 'cut', 'dxf'] as const).map((fmt) => (
          <button
            key={fmt}
            type="button"
            className="btn btn-ghost"
            onClick={() => onExport(fmt)}
            disabled={busy}
          >
            {fmt.toUpperCase()}
          </button>
        ))}
        <span className="toolbar-sel">选中 {selection.length}</span>
      </div>
    </header>
  );
}
