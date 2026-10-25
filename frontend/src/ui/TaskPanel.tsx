/**
 * 右侧任务/结果面板：展示异步任务进度与最近一次计算的统计。
 * 所有数据来自 WebSocket 推送，不做轮询。
 */

import { api } from '@/net/rest';
import { useAppStore } from '@/state/app-store';
import type { TaskStats } from '@/types/geometry';

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
  // 排料统计（LayoutStats）
  placementCount: '已摆放',
  lockedCount: '锁定件数',
  fixedRotCount: '木纹锁定',
  overflowCount: '超出板外',
  usedArea: '零件面积',
  sheetArea: '板材面积',
  boundaryArea: '可用面积',
  gap: '最小间距',
  margin: '四边余量',
  scrapId: '余料编号',
  requestedEngine: '请求引擎',
  engineNote: '引擎说明',
  rotation: '允许旋转',
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

/** 按百分比展示（后端存 0~1 的小数） */
const PERCENT_KEYS = new Set(['utilization']);
/** 面积类：加千分位与 mm² */
const AREA_KEYS = new Set(['usedArea', 'sheetArea', 'boundaryArea']);
/** 长度类：补 mm */
const MM_KEYS = new Set(['sheetWidth', 'sheetHeight', 'gap', 'margin']);
/** 布尔类：是 / 否 */
const BOOL_KEYS = new Set(['rotation']);
/** 长文本：独占一行，否则会把左侧字号小的标签挤到换行 */
const WIDE_KEYS = new Set(['engineNote', 'skippedNote', 'note']);
/** 引擎值翻译：后端给的是内部代号，直接显示“scan”没人看得懂 */
const ENGINE_TEXT: Record<string, string> = {
  auto: '自动选择',
  deepnest: 'DeepNest',
  shelf: '内置行排',
  scan: '内置扫描',
};
/** 计数类：为 0 时完全没有信息量（正常排料这三项就该是 0） */
const ZERO_HIDDEN_KEYS = new Set(['lockedCount', 'fixedRotCount', 'overflowCount']);

function formatStat(key: string, value: unknown): string {
  if (value === null || value === undefined) return '-';
  // 未绑定余料时后端给 0，直接写“整板”比“0”好读
  if (key === 'scrapId') return Number(value) > 0 ? `#${value}` : '整板';
  if (BOOL_KEYS.has(key)) return value ? '是' : '否';
  if (key === 'engine' || key === 'requestedEngine') {
    return ENGINE_TEXT[String(value)] ?? String(value);
  }
  if (typeof value === 'number') {
    if (PERCENT_KEYS.has(key)) return `${(value * 100).toFixed(1)}%`;
    if (AREA_KEYS.has(key)) return `${Math.round(value).toLocaleString('zh-CN')} mm²`;
    if (MM_KEYS.has(key)) return `${Number.isInteger(value) ? value : value.toFixed(1)} mm`;
    return Number.isInteger(value) ? String(value) : value.toFixed(3);
  }
  return String(value);
}

/**
 * 收纳规则：把“看与不看没区别”的行收掉，只留能判断结果好坏的。
 * 只看 stats 自身，不收任何外部状态，所以刷新/切工程行为一致：
 *   - 锁定/木纹/超出板外为 0 → 隐藏（这三项非 0 才是异常信号）；
 *   - 已摆放 == 零件数 → 只留零件数（多板或停靠板外时两数不等，才分开看）；
 *   - 余料编号为 0（整板）、可用面积 == 板材面积 → 隐藏；
 *   - 请求引擎为 auto 或与实际算法相同 → 隐藏（没发生回退就不必提）；
 *   - 旋转为允许（默认值）→ 隐藏，只有被禁时才提示。
 */
function visibleStats(stats: TaskStats): Array<[string, unknown]> {
  return Object.entries(stats).filter(([key, value]) => {
    if (ZERO_HIDDEN_KEYS.has(key) && Number(value) === 0) return false;
    if (key === 'placementCount' && Number(value) === Number(stats.partCount)) return false;
    if (key === 'scrapId' && !(Number(value) > 0)) return false;
    if (key === 'boundaryArea' && Number(value) === Number(stats.sheetArea)) return false;
    if (key === 'requestedEngine' && (value === 'auto' || value === stats.engine)) return false;
    if (key === 'rotation' && value === true) return false;
    return true;
  });
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
          {visibleStats(stats).map(([key, value]) => (
            <div key={key} className={`kv-row${WIDE_KEYS.has(key) ? ' kv-row-wide' : ''}`}>
              <dt>{STAT_LABEL[key] ?? key}</dt>
              <dd title={typeof value === 'string' ? value : undefined}>
                {formatStat(key, value)}
              </dd>
            </div>
          ))}
        </dl>
      ) : (
        <div className="empty">尚未执行计算</div>
      )}
    </aside>
  );
}
