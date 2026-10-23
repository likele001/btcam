/**
 * 右侧任务/结果面板：展示异步任务进度与最近一次计算的统计。
 * 所有数据来自 WebSocket 推送，不做轮询。
 */

import { api } from '@/net/rest';
import { useAppStore } from '@/state/app-store';

const STATUS_TEXT: Record<string, string> = {
  queued: '排队',
  running: '计算中',
  done: '完成',
  failed: '失败',
  cancelled: '已取消',
};

const KIND_TEXT: Record<string, string> = {
  import: 'DXF 导入',
  offset: '轮廓偏置',
  nest: '排料',
  layout_nest: '自动排料',
  toolpath: '刀路生成',
  toolpath_preview: '刀路预览',
};

/** 统计字段中文名，未列出的原样显示 */
const STAT_LABEL: Record<string, string> = {
  moveCount: '刀位点',
  layerCount: '层数',
  contourCount: '轮廓数',
  outerCount: '外轮廓',
  innerCount: '内孔',
  openCount: '开放线',
  cutLengthMm: '切削长度(mm)',
  rapidLengthMm: '空程(mm)',
  bridgeCount: '微连接',
  startPointCount: '起割点',
  skippedCount: '跳过轮廓',
  skippedNote: '跳过说明',
  orderGainMm: '排序节省(mm)',
  estimatedMin: '预计工时(min)',
  calcMs: '计算耗时(ms)',
  partCount: '零件数',
  sheetCount: '板材数',
  utilization: '利用率',
  engine: '算法',
  inCount: '输入轮廓',
  outCount: '输出轮廓',
  sheetWidth: '板宽',
  sheetHeight: '板高',
  // DXF 导入统计（RepairStats）
  inputContours: '输入轮廓',
  inputSegments: '输入线段',
  droppedTiny: '剔除碎线',
  mergedContours: '串接轮廓',
  removedSegments: '去除重线',
  closedContours: '闭合轮廓',
  openContours: '开放轮廓',
  outerContours: '外轮廓',
  holeContours: '内孔',
  issues: '异常数',
};

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return '-';
  if (typeof value === 'number') {
    return Number.isInteger(value) ? String(value) : value.toFixed(3);
  }
  return String(value);
}

export function TaskPanel(): JSX.Element {
  const tasks = useAppStore((s) => s.tasks);
  const stats = useAppStore((s) => s.lastStats);
  const setMessage = useAppStore((s) => s.setMessage);

  /** 取消中/排队中的任务：只发请求，状态由 TASK_FAILED(cancelled) 帧回写 */
  const onCancel = async (taskId: string) => {
    try {
      await api.cancelTask(taskId);
      setMessage('已请求取消任务');
    } catch (error) {
      setMessage(`取消失败：${(error as Error).message}`);
    }
  };

  return (
    <aside className="panel panel-right">
      <div className="panel-title">任务</div>
      <div className="task-list">
        {tasks.length === 0 && <div className="empty">暂无任务</div>}
        {tasks.map((task) => (
          <div key={task.taskId} className={`task-row task-${task.status}`}>
            <div className="task-head">
              <span className="task-kind">{KIND_TEXT[task.kind] ?? task.kind}</span>
              <span className="task-status">{STATUS_TEXT[task.status] ?? task.status}</span>
            </div>
            <div className="progress">
              <div className="progress-bar" style={{ width: `${Math.round(task.progress * 100)}%` }} />
            </div>
            <div className="task-msg" title={task.message}>{task.message || '—'}</div>
            {(task.status === 'queued' || task.status === 'running') && (
              <button type="button" className="btn task-cancel"
                onClick={() => void onCancel(task.taskId)}>
                取消
              </button>
            )}
          </div>
        ))}
      </div>

      <div className="panel-title">结果统计</div>
      {stats ? (
        <dl className="kv">
          {Object.entries(stats).map(([key, value]) => (
            <div key={key} className="kv-row">
              <dt>{STAT_LABEL[key] ?? key}</dt>
              <dd>{formatValue(value)}</dd>
            </div>
          ))}
        </dl>
      ) : (
        <div className="empty">尚未执行计算</div>
      )}
    </aside>
  );
}
