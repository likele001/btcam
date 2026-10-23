/**
 * 左侧刀路面板（模块 5）。
 *
 * 负责三块：
 *   1. 刀路参数表单（类型/方向/补偿/微连接/分层/进给），直接写 store；
 *   2. 起割点：进入画布点选模式，列出现有起割点，可逐个删除；
 *   3. 结果側：图例（实线切削 / 虚线抬刀 / 菱形起割点）+ 统计 + 重新生成/取消。
 *
 * 几何计算全在后端：这里只调接口，不推算任何轮廓、补偿或排序。
 */

import type { ChangeEvent } from 'react';

import type { ToolpathParams } from '@/net/rest';
import { requestGeometrySync } from '@/net/session';
import { useAppStore } from '@/state/app-store';
import { useLayoutActions } from './useLayoutActions';
import { useToolpathActions } from './useToolpathActions';
import { TOOL_OWNED_KEYS, TEMPLATE_OWNED_KEYS, useToolLibraryActions } from './useToolLibraryActions';

/** 数值型参数键：表单统一渲染，避免十几行重复 JSX */
type NumberKey =
  | 'toolDiameter'
  | 'depth'
  | 'passDepth'
  | 'feed'
  | 'plungeFeed'
  | 'safeZ'
  | 'liftZ'
  | 'spindle'
  | 'leadRadius'
  | 'microJointWidth'
  | 'microJointCount';

/**
 * 数字输入字段。
 *
 * 注意 min 一律给 0：HTML 的 step 以 min 为基准做整除校验，
 * min=0.1 + step=1 会把 18 误判成 stepMismatch（整张表单标红 invalid）。
 * 真正的业务下限放在 floor，由 onNumber 在输入时夹紧。
 */
const NUMBER_FIELDS: Array<{
  key: NumberKey;
  label: string;
  /** 业务下限（前端夹紧用） */
  floor: number;
  step: number;
}> = [
  { key: 'toolDiameter', label: '刀具直径 (mm)', floor: 0, step: 0.5 },
  { key: 'depth', label: '切割深度 (mm)', floor: 0.1, step: 1 },
  { key: 'passDepth', label: '每层切深 (mm)', floor: 0.1, step: 0.5 },
  { key: 'feed', label: '进给 (mm/min)', floor: 1, step: 100 },
  { key: 'plungeFeed', label: '下刀 (mm/min)', floor: 1, step: 100 },
  { key: 'safeZ', label: '安全高度 (mm)', floor: 0, step: 1 },
  { key: 'liftZ', label: '抬刀高度 (mm)', floor: 0, step: 1 },
  { key: 'spindle', label: '主轴 (rpm)', floor: 0, step: 1000 },
];

const hexColor = (color: number): string => `#${color.toString(16).padStart(6, '0')}`;

export function ToolpathPanel(): JSX.Element {
  const params = useAppStore((s) => s.toolpathParams);
  const preview = useAppStore((s) => s.toolpathPreview);
  const startPoints = useAppStore((s) => s.startPoints);
  const picking = useAppStore((s) => s.pickingStart);
  const layers = useAppStore((s) => s.layers);
  const viewMode = useAppStore((s) => s.viewMode);
  const tasks = useAppStore((s) => s.tasks);
  const busy = useAppStore((s) => s.busy);
  const tools = useAppStore((s) => s.tools);
  const templates = useAppStore((s) => s.templates);
  const setParams = useAppStore((s) => s.setToolpathParams);
  const setPicking = useAppStore((s) => s.setPickingStart);
  const removeStartPoint = useAppStore((s) => s.removeStartPoint);
  const clearStartPoints = useAppStore((s) => s.clearStartPoints);
  const toggleLayer = useAppStore((s) => s.toggleLayer);

  const { generate, cancel } = useToolpathActions();
  const { open: openLayout } = useLayoutActions();
  const { open: openToolLibrary, selectTool, selectTemplate } = useToolLibraryActions();

  const boundTool = tools.find((tool) => tool.id === params.toolId) ?? null;
  const boundTemplate = templates.find((tpl) => tpl.id === params.templateId) ?? null;
  const toolOwned = params.toolId != null;
  const templateOwned = params.templateId != null;
  /** 字段被刀具/模板接管：控件置灰，避免“改了不起作用” */
  const locked = (key: keyof ToolpathParams): boolean =>
    (toolOwned && TOOL_OWNED_KEYS.includes(key))
    || (templateOwned && TEMPLATE_OWNED_KEYS.includes(key));

  const running = tasks.some((task) => task.kind === 'toolpath'
    && (task.status === 'queued' || task.status === 'running'));

  const stats = preview?.stats ?? {};
  const stat = (key: string): number | null =>
    (typeof stats[key] === 'number' ? (stats[key] as number) : null);
  const fmt = (value: number | null, digits = 1): string =>
    (value === null ? '—' : value.toFixed(digits));
  const note = typeof stats.skippedNote === 'string' ? stats.skippedNote : '';

  const sourceLabel = preview
    ? preview.source === 'layout'
      ? `排料板${preview.sheet ? ` ${Math.round(preview.sheet.width)}×${Math.round(preview.sheet.height)}` : ''}`
      : '图纸轮廓'
    : viewMode === 'layout'
      ? '排料板（未生成）'
      : '图纸轮廓（未生成）';

  const onNumber = (key: NumberKey) => (event: ChangeEvent<HTMLInputElement>) => {
    const value = Number(event.target.value);
    if (!Number.isFinite(value)) return;
    const field = NUMBER_FIELDS.find((item) => item.key === key);
    // 小于业务下限的输入直接夹住：HTML 的 min 只用来给 step 做基准
    setParams({ [key]: field ? Math.max(field.floor, value) : value } as Partial<ToolpathParams>);
  };

  const togglePicking = () => setPicking(!picking);

  return (
    <aside className="panel panel-left">
      <div className="panel-title">刀具与工艺</div>
      <div className="form toolpath-form">
        <label className="form-row">
          <span>刀具</span>
          <select
            value={params.toolId ?? ''}
            onChange={(event) => selectTool(event.target.value ? Number(event.target.value) : null)}
          >
            <option value="">不绑定（手动参数）</option>
            {tools.map((tool) => (
              <option key={tool.id} value={tool.id}>
                {tool.name}{tool.category === 'wire' ? '（钼丝）' : ''}
              </option>
            ))}
          </select>
        </label>
        {boundTool && (
          <label className="form-row">
            <span>补偿量</span>
            <span className="form-value">
              ⌀{boundTool.effectiveDiameter.toFixed(3)} · R{boundTool.compensationRadius.toFixed(3)}
            </span>
          </label>
        )}
        <label className="form-row">
          <span>工艺模板</span>
          <select
            value={params.templateId ?? ''}
            onChange={(event) => selectTemplate(event.target.value ? Number(event.target.value) : null)}
          >
            <option value="">不使用模板</option>
            {templates.map((tpl) => (
              <option key={tpl.id} value={tpl.id}>{tpl.name}</option>
            ))}
          </select>
        </label>
        {boundTemplate && (
          <label className="form-row">
            <span>物料</span>
            <span className="form-value">{boundTemplate.material || '未分类'}</span>
          </label>
        )}
        <div className="form-actions">
          <button type="button" className="btn" onClick={openToolLibrary}>管理刀具库</button>
        </div>
      </div>
      {(toolOwned || templateOwned) && (
        <div className="form-hint">
          {toolOwned ? '刀具参数（直径/进给/分层/抬刀/方向）由刀具库接管；' : ''}
          {templateOwned ? '补偿与微连接由模板接管；' : ''}
          选择“不绑定/不使用”即可手动编辑。
        </div>
      )}

      <div className="panel-title">
        刀路参数
        <span className="panel-count">
          {preview ? `${preview.contours.length} 条` : '未生成'}
        </span>
      </div>
      <div className="form toolpath-form">
        <label className="form-row">
          <span>数据源</span>
          <span className="form-value">{sourceLabel}</span>
        </label>
        <label className="form-row">
          <span>刀路类型</span>
          <select
            value={params.cutType ?? 'auto'}
            disabled={locked('cutType')}
            onChange={(event) => setParams({ cutType: event.target.value as ToolpathParams['cutType'] })}
          >
            <option value="auto">外轮廓 + 内孔</option>
            <option value="outer">仅外轮廓</option>
            <option value="inner">仅内孔</option>
          </select>
        </label>
        <label className="form-row">
          <span>切割方向</span>
          <select
            value={params.direction ?? 'ccw'}
            disabled={toolOwned}
            onChange={(event) => setParams({ direction: event.target.value as ToolpathParams['direction'] })}
          >
            <option value="climb">顺铣（外顺内逆）</option>
            <option value="conventional">逆铣（外逆内顺）</option>
            <option value="ccw">强制逆时针 CCW</option>
            <option value="cw">强制顺时针 CW</option>
          </select>
        </label>
        <label className="form-row">
          <span>刀具补偿</span>
          <select
            value={params.side}
            disabled={locked('side')}
            onChange={(event) => setParams({ side: event.target.value as ToolpathParams['side'] })}
          >
            <option value="auto">按内外自动</option>
            <option value="outside">外补偿 +R</option>
            <option value="inside">内补偿 -R</option>
            <option value="on">不补偿</option>
          </select>
        </label>
        {NUMBER_FIELDS.map((field) => (
          <label key={field.key} className="form-row">
            <span>{field.label}</span>
            <input
              type="number"
              value={params[field.key] as number}
              min={0}
              step={field.step}
              disabled={locked(field.key)}
              onChange={onNumber(field.key)}
            />
          </label>
        ))}
        <label className="form-row form-check">
          <span>路径排序（少抬刀）</span>
          <input
            type="checkbox"
            checked={params.optimize !== false}
            disabled={locked('optimize')}
            onChange={(event) => setParams({ optimize: event.target.checked })}
          />
        </label>
        <label className="form-row form-check">
          <span>圆弧切入</span>
          <input
            type="checkbox"
            checked={params.leadIn !== false}
            disabled={locked('leadIn')}
            onChange={(event) => setParams({ leadIn: event.target.checked })}
          />
        </label>
        {params.leadIn !== false && (
          <label className="form-row">
            <span>切入半径 (mm)</span>
            <input
              type="number"
              value={params.leadRadius}
              min={0}
              step={0.5}
              disabled={locked('leadRadius')}
              onChange={onNumber('leadRadius')}
            />
          </label>
        )}
      </div>

      <div className="panel-title">微连接（桥接）</div>
      <div className="form toolpath-form">
        <label className="form-row form-check">
          <span>启用微连接</span>
          <input
            type="checkbox"
            checked={params.microJoint === true}
            disabled={locked('microJoint')}
            onChange={(event) => setParams({ microJoint: event.target.checked })}
          />
        </label>
        {params.microJoint === true && (
          <>
            <label className="form-row">
              <span>桥宽 (mm)</span>
              <input
                type="number"
                value={params.microJointWidth ?? 2}
                min={0}
                step={0.5}
                disabled={locked('microJointWidth')}
                onChange={onNumber('microJointWidth')}
              />
            </label>
            <label className="form-row">
              <span>桥数量</span>
              <input
                type="number"
                value={params.microJointCount ?? 2}
                min={0}
                max={20}
                step={1}
                disabled={locked('microJointCount')}
                onChange={onNumber('microJointCount')}
              />
            </label>
          </>
        )}
      </div>

      <div className="panel-title">
        起割点
        <span className="panel-count">{startPoints.length}</span>
      </div>
      <div className="form-actions-row">
        <button
          type="button"
          className={`btn${picking ? ' btn-active' : ''}`}
          onClick={togglePicking}
        >
          {picking ? '点选中…（Esc 退出）' : '点选起割点'}
        </button>
        <button
          type="button"
          className="btn"
          onClick={clearStartPoints}
          disabled={startPoints.length === 0}
        >
          清空
        </button>
      </div>
      {picking && (
        <div className="form-hint">在画布上点击目标位置，后端会自动吸附到最近轮廓并旋转点序。</div>
      )}
      <div className="point-list">
        {startPoints.length === 0 && (
          <div className="empty">未指定：每条轮廓从默认起点下刀</div>
        )}
        {startPoints.map((point, index) => (
          <div className="point-row" key={`${point.x}-${point.y}-${index}`}>
            <span className="point-index">{index + 1}</span>
            <span className="point-xy">
              X {point.x.toFixed(1)} · Y {point.y.toFixed(1)}
            </span>
            <button
              type="button"
              className="point-del"
              title="移除该起割点"
              onClick={() => removeStartPoint(index)}
            >
              ×
            </button>
          </div>
        ))}
      </div>

      <div className="panel-title">图例</div>
      <div className="legend">
        {layers.length === 0 && <div className="empty">暂无刀路图层</div>}
        {layers.map((layer) => (
          <label key={layer.id} className="layer-row">
            <input
              type="checkbox"
              checked={layer.visible}
              onChange={() => toggleLayer(layer.id)}
            />
            <span
              className={`layer-swatch${layer.dashed ? ' layer-swatch-dashed' : ''}`}
              style={{ backgroundColor: hexColor(layer.color) }}
            />
            <span className="layer-name">{layer.name}</span>
            <span className="layer-count">
              {layer.operation === 'rapid' ? '虚线' : layer.operation === 'marker' ? '标记' : '实线'}
            </span>
          </label>
        ))}
      </div>

      <div className="panel-title">刀路统计</div>
      <dl className="kv">
        <div className="kv-row">
          <dt>轮廓</dt>
          <dd>{fmt(stat('contourCount'), 0)}（外 {fmt(stat('outerCount'), 0)} / 内 {fmt(stat('innerCount'), 0)}）</dd>
        </div>
        <div className="kv-row">
          <dt>切削长度</dt>
          <dd>{fmt(stat('cutLengthMm'))} mm</dd>
        </div>
        <div className="kv-row">
          <dt>空程</dt>
          <dd>{fmt(stat('rapidLengthMm'))} mm</dd>
        </div>
        <div className="kv-row">
          <dt>微连接</dt>
          <dd>{fmt(stat('bridgeCount'), 0)}</dd>
        </div>
        <div className="kv-row">
          <dt>起割点</dt>
          <dd>{fmt(stat('startPointCount'), 0)}</dd>
        </div>
        <div className="kv-row">
          <dt>排序节省</dt>
          <dd>{fmt(stat('orderGainMm'))} mm</dd>
        </div>
        <div className="kv-row">
          <dt>层数 / 刀位点</dt>
          <dd>{fmt(stat('layerCount'), 0)} / {fmt(stat('moveCount'), 0)}</dd>
        </div>
        <div className="kv-row">
          <dt>预计工时</dt>
          <dd>{fmt(stat('estimatedMin'))} min</dd>
        </div>
        <div className="kv-row">
          <dt>计算耗时</dt>
          <dd>{fmt(stat('calcMs'), 0)} ms</dd>
        </div>
        <div className="kv-row">
          <dt>跳过轮廓</dt>
          <dd className={(stat('skippedCount') ?? 0) > 0 ? 'kv-danger' : undefined}>
            {fmt(stat('skippedCount'), 0)}
          </dd>
        </div>
      </dl>
      {note && <div className="form-hint">{note}</div>}

      <div className="form-actions-row">
        <button
          type="button"
          className="btn btn-accent"
          onClick={() => void generate()}
          disabled={busy || running}
        >
          {running ? '计算中…' : preview ? '重新生成' : '生成刀路'}
        </button>
        {running && (
          <button type="button" className="btn" onClick={() => void cancel()}>
            取消
          </button>
        )}
      </div>
      <div className="form-actions-row">
        <button type="button" className="btn" onClick={() => void openLayout()} disabled={busy}>
          返回排料
        </button>
        <button type="button" className="btn" onClick={() => requestGeometrySync()}>
          返回图纸
        </button>
      </div>

      <div className="panel-footer">
        选中轮廓只做选中部分，未选中时按数据源生成全部（排料板为整板）。
        绑定刀具后补偿量由刀具库提供（钼丝 = 丝径/2 + 放电间隙），绑定模板后补偿与微连接按模板；
        内孔默认向内补偿、外轮廓向外补偿；起割点坐标为世界坐标，后端吸附后以画布上的菱形为准。
      </div>
    </aside>
  );
}
