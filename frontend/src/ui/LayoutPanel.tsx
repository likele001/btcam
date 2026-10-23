/**
 * 左侧排料面板（模块 3 / 4）：板材参数 / 零件列表 / 旋转与锁定 / 自动排料 / 利用率。
 *
 * 与画布的分工：面板负责"精确输入与批量选择"，画布负责"鼠标摆放"。
 * 两者都只调后端接口 —— 越界、最小间距、锁定判定全在 LayoutService，
 * 面板不复制任何校验逻辑，失败时直接展示后端给的 reason。
 */

import { useEffect, useState, type ChangeEvent } from 'react';

import type { LayoutNestParams } from '@/net/rest';
import { useAppStore } from '@/state/app-store';
import type { LayoutPartInfo, SheetSpec } from '@/types/geometry';
import { useLayoutActions } from './useLayoutActions';

/** 与后端 db.py 的 layout_sheet 默认值保持一致 */
const DEFAULT_SHEET: SheetSpec = { width: 1220, height: 2440, margin: 10, gap: 5 };

const SHEET_FIELDS: Array<{ key: keyof SheetSpec; label: string; min: number; step: number }> = [
  { key: 'width', label: '板宽 (mm)', min: 10, step: 10 },
  { key: 'height', label: '板高 (mm)', min: 10, step: 10 },
  { key: 'margin', label: '四边余量 (mm)', min: 0, step: 1 },
  { key: 'gap', label: '最小间距 (mm)', min: 0, step: 1 },
];

export function LayoutPanel(): JSX.Element {
  const layout = useAppStore((s) => s.layout);
  const selection = useAppStore((s) => s.selection);
  const busy = useAppStore((s) => s.busy);
  const tasks = useAppStore((s) => s.tasks);
  const setSelection = useAppStore((s) => s.setSelection);
  const setBusy = useAppStore((s) => s.setBusy);

  const { applySheet, rotateSelected, lockSelected, fixedRotSelected, nest, cancelNest, reset } =
    useLayoutActions();

  // 自动排料参数：引擎与是否允许 90° 旋转（木纹锁定件始终不转）
  const [engine, setEngine] = useState<LayoutNestParams['engine']>('auto');
  const [rotation, setRotation] = useState(true);
  const nesting = tasks.some((task) => task.kind === 'layout_nest'
    && (task.status === 'queued' || task.status === 'running'));

  // 表单值本地维护：用户输入过程中不能被广播帧打断，点"应用"才回写后端
  const [sheet, setSheet] = useState<SheetSpec>(DEFAULT_SHEET);
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    if (layout && !dirty) setSheet(layout.sheet);
  }, [layout, dirty]);

  const onField = (key: keyof SheetSpec) => (event: ChangeEvent<HTMLInputElement>) => {
    const value = Number(event.target.value);
    setSheet((prev) => ({ ...prev, [key]: Number.isFinite(value) ? value : prev[key] }));
    setDirty(true);
  };

  const onApply = async () => {
    setBusy(true);
    try {
      await applySheet({ ...sheet });
      setDirty(false);
    } finally {
      setBusy(false);
    }
  };

  const picked = new Set(selection);
  const parts = layout?.parts ?? [];
  const selected = parts.filter((part) => part.polylineIds.some((id) => picked.has(id)));
  const allLocked = selected.length > 0 && selected.every((part) => part.locked);
  const allFixedRot = selected.length > 0 && selected.every((part) => part.fixedRot);
  const stats = layout?.stats;

  return (
    <aside className="panel panel-left">
      <div className="panel-title">排料板</div>

      <div className="form">
        {SHEET_FIELDS.map((field) => (
          <label key={field.key} className="form-row">
            <span>{field.label}</span>
            <input
              type="number"
              value={Number(sheet[field.key].toFixed(3))}
              min={field.min}
              step={field.step}
              onChange={onField(field.key)}
            />
          </label>
        ))}
        <div className="form-actions">
          <button
            type="button"
            className="btn btn-accent"
            onClick={() => void onApply()}
            disabled={busy || !dirty}
          >
            应用板材
          </button>
          <button type="button" className="btn" onClick={() => void reset()} disabled={busy}>
            重新排板
          </button>
        </div>
      </div>

      <div className="panel-title">
        零件
        <span className="panel-count">{parts.length}</span>
      </div>

      <div className="part-list">
        {parts.length === 0 && <div className="empty">暂无排料零件</div>}
        {parts.map((part) => (
          <PartRow
            key={part.id}
            part={part}
            selected={part.polylineIds.some((id) => picked.has(id))}
            onSelect={() => setSelection(part.polylineIds)}
          />
        ))}
      </div>

      <div className="form-actions panel-actions">
        <button
          type="button"
          className="btn"
          onClick={() => void rotateSelected(-90)}
          disabled={busy || selected.length === 0}
        >
          -90°
        </button>
        <button
          type="button"
          className="btn"
          onClick={() => void rotateSelected(90)}
          disabled={busy || selected.length === 0}
        >
          +90°
        </button>
        <button
          type="button"
          className="btn"
          onClick={() => void lockSelected()}
          disabled={busy || selected.length === 0}
        >
          {allLocked ? '解锁' : '锁定'}
        </button>
        <button
          type="button"
          className="btn"
          onClick={() => void fixedRotSelected()}
          disabled={busy || selected.length === 0}
          title="木纹方向锁定：自动排料与手动旋转都不改变角度"
        >
          {allFixedRot ? '解纹' : '木纹'}
        </button>
      </div>

      <div className="panel-title">自动排料</div>
      <div className="form">
        <label className="form-row">
          <span>引擎</span>
          <select
            value={engine}
            onChange={(event) => setEngine(event.target.value as LayoutNestParams['engine'])}
            disabled={busy || nesting}
          >
            <option value="auto">自动（优先 DeepNest）</option>
            <option value="deepnest">DeepNest 桥接</option>
            <option value="scan">内置扫描</option>
          </select>
        </label>
        <label className="form-row form-check">
          <span>允许 90° 旋转</span>
          <input
            type="checkbox"
            checked={rotation}
            onChange={(event) => setRotation(event.target.checked)}
            disabled={busy || nesting}
          />
        </label>
        <div className="form-actions">
          <button
            type="button"
            className="btn btn-accent"
            onClick={() => void nest(engine, rotation)}
            disabled={busy || nesting}
          >
            {nesting ? '排料中…' : '自动排料'}
          </button>
          {nesting && (
            <button type="button" className="btn" onClick={() => void cancelNest()}>
              取消排料
            </button>
          )}
        </div>
      </div>

      <div className="panel-title">排板结果</div>
      <dl className="kv">
        <div className="kv-row">
          <dt>零件 / 已摆放</dt>
          <dd>{stats?.partCount ?? 0} / {stats?.placementCount ?? 0}</dd>
        </div>
        <div className="kv-row">
          <dt>已锁定</dt>
          <dd>{stats?.lockedCount ?? 0}</dd>
        </div>
        <div className="kv-row">
          <dt>木纹锁定</dt>
          <dd>{stats?.fixedRotCount ?? 0}</dd>
        </div>
        <div className="kv-row">
          <dt>超出可用区</dt>
          <dd className={(stats?.overflowCount ?? 0) > 0 ? 'kv-danger' : undefined}>
            {stats?.overflowCount ?? 0}
          </dd>
        </div>
        <div className="kv-row">
          <dt>净用面积</dt>
          <dd>{((stats?.usedArea ?? 0) / 1e4).toFixed(2)} dm²</dd>
        </div>
        <div className="kv-row">
          <dt>板材面积</dt>
          <dd>{((stats?.sheetArea ?? 0) / 1e4).toFixed(2)} dm²</dd>
        </div>
        <div className="kv-row">
          <dt>利用率</dt>
          <dd>{((stats?.utilization ?? 0) * 100).toFixed(1)}%</dd>
        </div>
      </dl>

      <div className="panel-footer">
        拖拽零件摆放，R / Shift+R 旋转，L 锁定；锁定后不可移动。
        自动排料只重排未锁定零件，木纹锁定件不旋转，排料中可随时取消。
        松手即由后端校验越界与最小间距，不通过自动回弹。
      </div>
    </aside>
  );
}

interface PartRowProps {
  part: LayoutPartInfo;
  selected: boolean;
  onSelect: () => void;
}

/** 零件行：点击选中整件（含内孔的多个轮廓一起高亮） */
function PartRow({ part, selected, onSelect }: PartRowProps): JSX.Element {
  return (
    <button
      type="button"
      className={`part-row${selected ? ' part-row-selected' : ''}`}
      onClick={onSelect}
      title={`位置 (${part.tx.toFixed(1)}, ${part.ty.toFixed(1)}) · 角度 ${Math.round(part.rot)}°`}
    >
      <span className={`part-lock${part.locked ? ' part-lock-on' : ''}`}>
        {part.locked ? '锁' : '·'}
      </span>
      <span
        className={`part-grain${part.fixedRot ? ' part-grain-on' : ''}`}
        title="木纹方向锁定（自动排料不旋转）"
      >
        {part.fixedRot ? '纹' : ''}
      </span>
      <span className="part-name">{part.name}</span>
      <span className="part-size">{part.width.toFixed(1)}×{part.height.toFixed(1)}</span>
    </button>
  );
}
