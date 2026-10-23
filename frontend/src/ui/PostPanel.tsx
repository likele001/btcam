/**
 * 导出与后处理弹窗（模块 7）。
 *
 * 两个页签：
 *   导出：选一份后处理（一台机床），填文件名/批量/速度压力后直接出文件；
 *   后处理配置：左侧机床列表 + 右侧模板编辑器，可改程序头尾、换刀指令、行号、精度。
 *
 * 配置是 JSON（后端落盘到 %LOCALAPPDATA%/BtCAM/post），内置只读；
 * 想改内置模板必须“另存为”自定义后处理。模板占位符由后端校验与替换，
 * 前端只负责把文本搬到请求里，不做任何渲染。
 */

import { useEffect, useState } from 'react';

import type {
  GcodePostConfig,
  HpglPostConfig,
  PostKind,
  PostProcessorRecord,
} from '@/net/rest';
import { useAppStore } from '@/state/app-store';
import { formatForKind, toProcessorInput, usePostActions } from './usePostActions';

type Tab = 'export' | 'config';

const KIND_LABEL: Record<PostKind, string> = {
  gcode: 'G 代码',
  hpgl: 'HPGL 笔式',
  dxf: 'DXF 图形',
};

const KIND_EXT: Record<PostKind, string> = {
  gcode: 'nc',
  hpgl: 'plt',
  dxf: 'dxf',
};

/** 新建 gcode 配置时的最小可用模板：字段必须齐全，后端要求 5 条指令模板不为空 */
const BLANK_GCODE: GcodePostConfig = {
  precision: 3,
  lineNumbers: false,
  lineNumberStart: 10,
  lineNumberStep: 10,
  programNo: '0001',
  header: [
    '%',
    '( BTCAM - {name} )',
    '( Generated {date} )',
    'G21',
    'G90',
    'G17',
    'G40',
    'G0 Z{safeZ}',
    'M3 S{spindle}',
  ],
  footer: ['G0 Z{safeZ}', 'M5', 'M30', '%'],
  toolChange: ['T{tool} M6', 'G43 H{tool}'],
  rapidZ: 'G0 Z{z}',
  rapidXY: 'G0 X{x} Y{y}',
  plungeZ: 'G1 Z{z} F{feed}',
  feed: 'F{feed}',
  cutXY: 'G1 X{x} Y{y}',
};

const BLANK_HPGL: HpglPostConfig = {
  unitsPerMm: 40,
  header: ['IN;', 'SP1;'],
  footer: ['SP0;'],
  penUp: 'PU{x},{y};',
  penDown: 'PD{x},{y};',
  penLift: 'PU;',
};

function cloneProcessor(record: PostProcessorRecord): PostProcessorRecord {
  return {
    ...record,
    gcode: record.gcode
      ? {
        ...record.gcode,
        header: [...record.gcode.header],
        footer: [...record.gcode.footer],
        toolChange: [...record.gcode.toolChange],
      }
      : undefined,
    hpgl: record.hpgl
      ? { ...record.hpgl, header: [...record.hpgl.header], footer: [...record.hpgl.footer] }
      : undefined,
    dxf: record.dxf ? { ...record.dxf } : undefined,
  };
}

/** 另存为时生成一个不冲突的 id（后端限制 2~40 位小写字母/数字/下划线） */
function suggestId(base: string, processors: PostProcessorRecord[]): string {
  const taken = new Set(processors.map((item) => item.id));
  const stem = base.replace(/[^a-z0-9_]/g, '_').slice(0, 34) || 'custom_post';
  let candidate = stem;
  let index = 2;
  while (taken.has(candidate)) {
    candidate = `${stem}_${index}`;
    index += 1;
  }
  return candidate;
}

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / 1024).toFixed(1)} KB`;
}

function baseName(path: string): string {
  const parts = path.split(/[\\/]/);
  return parts[parts.length - 1] || path;
}

/** 模板数组编辑器：一行一条指令，空行原样保留 */
function TemplateArea({ label, hint, value, onChange }: {
  label: string;
  hint?: string;
  value: string[];
  onChange: (rows: string[]) => void;
}): JSX.Element {
  return (
    <label className="post-template">
      <span>{label}{hint ? ` · ${hint}` : ''}</span>
      <textarea
        rows={4}
        spellCheck={false}
        value={value.join('\n')}
        onChange={(event) => onChange(event.target.value.split('\n'))}
      />
    </label>
  );
}

export function PostPanel(): JSX.Element | null {
  const open = useAppStore((s) => s.postOpen);
  const processors = useAppStore((s) => s.postProcessors);
  const viewMode = useAppStore((s) => s.viewMode);
  const toolpathPreview = useAppStore((s) => s.toolpathPreview);
  const lastExport = useAppStore((s) => s.lastExport);
  const activeWorkOrder = useAppStore((s) => s.activeWorkOrder);
  const setExportPreset = useAppStore((s) => s.setExportPreset);

  const { close, save, remove, exportNow, showExportDir } = usePostActions();

  const [tab, setTab] = useState<Tab>('export');
  const [exportId, setExportId] = useState('');
  const [name, setName] = useState('BT_CAM');
  const [flipY, setFlipY] = useState(false);
  const [allSheets, setAllSheets] = useState(false);
  const [speed, setSpeed] = useState(30);
  const [force, setForce] = useState(20);
  const [exporting, setExporting] = useState(false);

  const [draft, setDraft] = useState<PostProcessorRecord | null>(null);
  const [creating, setCreating] = useState(false);
  const [armed, setArmed] = useState(false);
  const [saving, setSaving] = useState(false);

  // 打开瞬间复位导出表单；后处理列表可能还没返回，先落空值等下面的兜底
  useEffect(() => {
    if (!open) return;
    setTab('export');
    setArmed(false);
    setSaving(false);
    setExporting(false);
    setCreating(false);
    // 从工单打开时，优先用存档里的后处理与速度/压力
    const savedPost = activeWorkOrder?.post ?? null;
    setFlipY(savedPost?.flipY ?? false);
    setSpeed(savedPost?.speed ?? 30);
    setForce(savedPost?.force ?? 20);
    setAllSheets(false);
    const preferred = processors.find((item) => item.id === savedPost?.processorId)
      ?? processors.find((item) => item.id === 'weihong_nc') ?? processors[0] ?? null;
    setExportId(preferred ? preferred.id : '');
    setDraft(preferred ? cloneProcessor(preferred) : null);
    // 只在弹窗打开瞬间初始化；之后由列表点击/保存结果控制
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // 列表异步返回 / 保存后刷新时兜底：保留当前可用的选中项，新建态不覆盖
  useEffect(() => {
    if (!open || processors.length === 0) return;
    setExportId((prev) => (processors.some((item) => item.id === prev) ? prev : processors[0].id));
    setDraft((prev) => {
      if (creating) return prev;
      if (prev && processors.some((item) => item.id === prev.id)) return prev;
      return cloneProcessor(processors[0]);
    });
  }, [open, processors, creating]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, close]);

  if (!open) return null;

  const exportProcessor = processors.find((item) => item.id === exportId) ?? processors[0] ?? null;
  const exportKind: PostKind | null = exportProcessor ? exportProcessor.kind : null;
  const layoutSource = viewMode === 'layout'
    || (viewMode === 'preview' && toolpathPreview?.source === 'layout');
  const isNew = draft !== null && !processors.some((item) => item.id === draft.id);

  const pickProcessor = (record: PostProcessorRecord) => {
    setDraft(cloneProcessor(record));
    setCreating(false);
    setArmed(false);
  };

  const newProcessor = () => {
    const base = draft ?? processors[0];
    if (!base) return;
    setDraft({
      ...cloneProcessor(base),
      id: suggestId('custom_post', processors),
      name: '自定义后处理',
      builtin: false,
      version: 1,
    });
    setCreating(true);
    setArmed(false);
  };

  const copyProcessor = () => {
    if (!draft) return;
    setDraft({
      ...cloneProcessor(draft),
      id: suggestId(`${draft.id}_copy`, processors),
      name: `${draft.name} 副本`,
      builtin: false,
    });
    setCreating(true);
    setArmed(false);
  };

  const onSave = async () => {
    if (!draft) return;
    setSaving(true);
    setArmed(false);
    const saved = await save(isNew ? null : draft.id, toProcessorInput(draft));
    setSaving(false);
    if (saved) {
      setDraft(cloneProcessor(saved));
      setCreating(false);
      setExportId(saved.id);
    }
  };

  const onDelete = async () => {
    if (!draft || draft.builtin) return;
    if (!armed) {
      setArmed(true);
      return;
    }
    const removed = await remove(draft.id);
    setArmed(false);
    if (!removed) return;
    const rest = useAppStore.getState().postProcessors;
    const next = rest[0] ?? null;
    setDraft(next ? cloneProcessor(next) : null);
    setCreating(false);
    setExportId(next ? next.id : '');
  };

  const onExport = async () => {
    if (!exportProcessor) return;
    setExporting(true);
    setExportPreset({ flipY, speed, force });
    await exportNow({
      processorId: exportProcessor.id,
      format: formatForKind(exportProcessor.kind),
      name: name.trim() || 'BT_CAM',
      flipY: exportProcessor.kind === 'hpgl' ? flipY : false,
      speed,
      force,
      allSheets: layoutSource ? allSheets : false,
    });
    setExporting(false);
  };

  const openEditor = (record: PostProcessorRecord) => {
    setDraft(cloneProcessor(record));
    setCreating(false);
    setArmed(false);
    setTab('config');
  };

  const patchDraft = (patch: Partial<PostProcessorRecord>) =>
    setDraft((prev) => (prev ? { ...prev, ...patch } : prev));

  const patchGcode = (patch: Partial<GcodePostConfig>) =>
    setDraft((prev) => (prev
      ? { ...prev, gcode: { ...(prev.gcode ?? BLANK_GCODE), ...patch } }
      : prev));

  const patchHpgl = (patch: Partial<HpglPostConfig>) =>
    setDraft((prev) => (prev
      ? { ...prev, hpgl: { ...(prev.hpgl ?? BLANK_HPGL), ...patch } }
      : prev));

  /** 切换类别时补一份该类别的最小配置，并同步默认扩展名 */
  const changeKind = (kind: PostKind) => setDraft((prev) => {
    if (!prev) return prev;
    return {
      ...prev,
      kind,
      ext: prev.kind === kind ? prev.ext : KIND_EXT[kind],
      gcode: kind === 'gcode'
        ? (prev.gcode ?? { ...BLANK_GCODE, header: [...BLANK_GCODE.header], footer: [...BLANK_GCODE.footer], toolChange: [...BLANK_GCODE.toolChange] })
        : prev.gcode,
      hpgl: kind === 'hpgl'
        ? (prev.hpgl ?? { ...BLANK_HPGL, header: [...BLANK_HPGL.header], footer: [...BLANK_HPGL.footer] })
        : prev.hpgl,
      dxf: kind === 'dxf' ? (prev.dxf ?? { version: 'R2010' }) : prev.dxf,
    };
  });

  const gcode = draft?.gcode ?? BLANK_GCODE;
  const hpgl = draft?.hpgl ?? BLANK_HPGL;

  return (
    <div
      className="modal-mask"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) close();
      }}
    >
      <div className="modal post-panel" role="dialog" aria-modal="true">
        <header className="modal-head">
          <span>导出与后处理</span>
          <span className="modal-sub">模块 7 · 配置驱动，新增机床只加 JSON</span>
          <button type="button" className="modal-close" title="关闭" onClick={close}>×</button>
        </header>

        <div className="modal-tabs">
          <button
            type="button"
            className={`modal-tab${tab === 'export' ? ' active' : ''}`}
            onClick={() => setTab('export')}
          >
            导出
          </button>
          <button
            type="button"
            className={`modal-tab${tab === 'config' ? ' active' : ''}`}
            onClick={() => setTab('config')}
          >
            后处理配置 <span className="modal-tab-count">{processors.length}</span>
          </button>
        </div>

        {tab === 'export' && (
          <div className="post-export-pane">
            <div className="form tool-form-grid post-export-grid">
              <label className="form-row form-row-wide">
                <span>后处理</span>
                <select
                  value={exportId}
                  onChange={(event) => setExportId(event.target.value)}
                  disabled={processors.length === 0}
                >
                  {processors.length === 0 && <option value="">暂无配置</option>}
                  {processors.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name}（.{item.ext} · {KIND_LABEL[item.kind]}
                      {item.builtin ? ' · 内置' : ''}）
                    </option>
                  ))}
                </select>
              </label>

              <label className="form-row">
                <span>文件名前缀</span>
                <input
                  type="text"
                  value={name}
                  placeholder="BT_CAM"
                  onChange={(event) => setName(event.target.value)}
                />
              </label>

              {layoutSource && (
                <label className="form-row form-check">
                  <span>全部板材各出一个文件</span>
                  <input
                    type="checkbox"
                    checked={allSheets}
                    onChange={(event) => setAllSheets(event.target.checked)}
                  />
                </label>
              )}

              {exportKind === 'hpgl' && (
                <>
                  <label className="form-row">
                    <span>速度 (0~100)</span>
                    <input
                      type="number"
                      min={0}
                      max={100}
                      step={1}
                      value={speed}
                      onChange={(event) => setSpeed(Number(event.target.value))}
                    />
                  </label>
                  <label className="form-row">
                    <span>压力 (0~100)</span>
                    <input
                      type="number"
                      min={0}
                      max={100}
                      step={1}
                      value={force}
                      onChange={(event) => setForce(Number(event.target.value))}
                    />
                  </label>
                  <label className="form-row form-check">
                    <span>Y 轴翻转（绘图仪原点在左下）</span>
                    <input
                      type="checkbox"
                      checked={flipY}
                      onChange={(event) => setFlipY(event.target.checked)}
                    />
                  </label>
                </>
              )}

              {exportKind === 'dxf' && (
                <div className="post-note">
                  DXF 导出的是图纸原始轮廓（按图层分色、闭合状态保留），
                  与刀路参数、板材拆分无关。
                </div>
              )}

              {activeWorkOrder && (
                <div className="post-note">
                  当前工单：#{activeWorkOrder.id}「{activeWorkOrder.name}」，
                  本次导出的文件会自动登记到该工单。
                </div>
              )}
            </div>

            <div className="post-actions">
              <button
                type="button"
                className="btn btn-accent"
                onClick={() => void onExport()}
                disabled={exporting || !exportProcessor}
              >
                {exporting ? '导出中…' : '导出'}
              </button>
              <button
                type="button"
                className="btn"
                onClick={() => (exportProcessor ? openEditor(exportProcessor) : undefined)}
                disabled={!exportProcessor}
              >
                编辑配置…
              </button>
              <button type="button" className="btn" onClick={() => void showExportDir()}>
                导出目录
              </button>
            </div>

            {lastExport && (
              <div className="post-result">
                <div className="post-result-head">
                  最近导出 · {lastExport.files.length} 个文件 · {lastExport.processor}
                </div>
                <div className="post-result-list">
                  {lastExport.files.map((file) => (
                    <div key={file.path} className="post-result-row">
                      <span className="post-result-name">{baseName(file.path)}</span>
                      <span className="post-result-meta">
                        {file.format.toUpperCase()} · {formatBytes(file.bytes)}
                      </span>
                      <code className="post-result-path" title={file.path}>{file.path}</code>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {tab === 'config' && (
          <div className="tool-library-body">
            <div className="tool-list-pane">
              <div className="tool-list-head">
                <span>机床</span>
                <button type="button" className="btn" onClick={newProcessor}>新建</button>
              </div>
              <div className="tool-list">
                {processors.length === 0 && (
                  <div className="empty">还没有后处理配置，点“新建”添加</div>
                )}
                {processors.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    className={`tool-list-row${draft?.id === item.id ? ' active' : ''}`}
                    onClick={() => pickProcessor(item)}
                  >
                    <span className="tool-row-name">{item.name}</span>
                    <span className="tool-row-meta">
                      {KIND_LABEL[item.kind]} · .{item.ext}
                      {item.builtin ? ' · 内置' : ''}
                    </span>
                  </button>
                ))}
              </div>
            </div>

            <div className="tool-form-pane">
              {draft ? (
                <>
                  <div className="tool-form-head">
                    {draft.builtin
                      ? `内置后处理 · ${draft.id}（只读）`
                      : (isNew ? '新建后处理' : `编辑后处理 · ${draft.id}`)}
                  </div>
                  <div className="form tool-form-grid">
                    <label className="form-row form-row-wide">
                      <span>名称</span>
                      <input
                        type="text"
                        value={draft.name}
                        placeholder="如 T1 维宏雕刻机"
                        onChange={(event) => patchDraft({ name: event.target.value })}
                      />
                    </label>
                    <label className="form-row">
                      <span>标识 id</span>
                      <input
                        type="text"
                        value={draft.id}
                        readOnly={!isNew}
                        title="2~40 位小写字母/数字/下划线"
                        onChange={(event) => patchDraft({ id: event.target.value })}
                      />
                    </label>
                    <label className="form-row">
                      <span>类别</span>
                      <select
                        value={draft.kind}
                        onChange={(event) => changeKind(event.target.value as PostKind)}
                      >
                        <option value="gcode">G 代码</option>
                        <option value="hpgl">HPGL 笔式</option>
                        <option value="dxf">DXF 图形</option>
                      </select>
                    </label>
                    <label className="form-row">
                      <span>扩展名</span>
                      <input
                        type="text"
                        value={draft.ext}
                        onChange={(event) => patchDraft({ ext: event.target.value })}
                      />
                    </label>
                    <label className="form-row form-row-wide">
                      <span>说明</span>
                      <input
                        type="text"
                        value={draft.description}
                        placeholder="适用机型/注意事项"
                        onChange={(event) => patchDraft({ description: event.target.value })}
                      />
                    </label>

                    {draft.kind === 'gcode' && (
                      <>
                        <label className="form-row">
                          <span>小数精度</span>
                          <input
                            type="number"
                            min={0}
                            max={6}
                            step={1}
                            value={gcode.precision}
                            onChange={(event) => patchGcode({ precision: Number(event.target.value) })}
                          />
                        </label>
                        <label className="form-row form-check">
                          <span>输出行号</span>
                          <input
                            type="checkbox"
                            checked={gcode.lineNumbers}
                            onChange={(event) => patchGcode({ lineNumbers: event.target.checked })}
                          />
                        </label>
                        <label className="form-row">
                          <span>行号起始</span>
                          <input
                            type="number"
                            min={0}
                            step={10}
                            value={gcode.lineNumberStart}
                            onChange={(event) => patchGcode({ lineNumberStart: Number(event.target.value) })}
                          />
                        </label>
                        <label className="form-row">
                          <span>行号步长</span>
                          <input
                            type="number"
                            min={1}
                            step={10}
                            value={gcode.lineNumberStep}
                            onChange={(event) => patchGcode({ lineNumberStep: Number(event.target.value) })}
                          />
                        </label>
                        <label className="form-row">
                          <span>程序号</span>
                          <input
                            type="text"
                            value={gcode.programNo}
                            onChange={(event) => patchGcode({ programNo: event.target.value })}
                          />
                        </label>

                        <TemplateArea
                          label="程序头"
                          hint="一行一条，{name} {date} {safeZ} {spindle} {programNo}"
                          value={gcode.header}
                          onChange={(rows) => patchGcode({ header: rows })}
                        />
                        <TemplateArea
                          label="程序尾"
                          hint="回到安全高度再关主轴"
                          value={gcode.footer}
                          onChange={(rows) => patchGcode({ footer: rows })}
                        />
                        <TemplateArea
                          label="换刀指令"
                          hint="有刀具号时才插入，{tool} 为刀具 id"
                          value={gcode.toolChange}
                          onChange={(rows) => patchGcode({ toolChange: rows })}
                        />

                        <label className="form-row form-row-wide">
                          <span>快速抬刀</span>
                          <input
                            type="text"
                            value={gcode.rapidZ}
                            onChange={(event) => patchGcode({ rapidZ: event.target.value })}
                          />
                        </label>
                        <label className="form-row form-row-wide">
                          <span>快速定位</span>
                          <input
                            type="text"
                            value={gcode.rapidXY}
                            onChange={(event) => patchGcode({ rapidXY: event.target.value })}
                          />
                        </label>
                        <label className="form-row form-row-wide">
                          <span>下刀</span>
                          <input
                            type="text"
                            value={gcode.plungeZ}
                            onChange={(event) => patchGcode({ plungeZ: event.target.value })}
                          />
                        </label>
                        <label className="form-row form-row-wide">
                          <span>进给</span>
                          <input
                            type="text"
                            value={gcode.feed}
                            onChange={(event) => patchGcode({ feed: event.target.value })}
                          />
                        </label>
                        <label className="form-row form-row-wide">
                          <span>切削</span>
                          <input
                            type="text"
                            value={gcode.cutXY}
                            onChange={(event) => patchGcode({ cutXY: event.target.value })}
                          />
                        </label>
                      </>
                    )}

                    {draft.kind === 'hpgl' && (
                      <>
                        <label className="form-row">
                          <span>单位/毫米</span>
                          <input
                            type="number"
                            min={0.1}
                            step={1}
                            value={hpgl.unitsPerMm}
                            onChange={(event) => patchHpgl({ unitsPerMm: Number(event.target.value) })}
                          />
                        </label>
                        <label className="form-row form-row-wide">
                          <span>抬笔</span>
                          <input
                            type="text"
                            value={hpgl.penUp}
                            onChange={(event) => patchHpgl({ penUp: event.target.value })}
                          />
                        </label>
                        <label className="form-row form-row-wide">
                          <span>落笔</span>
                          <input
                            type="text"
                            value={hpgl.penDown}
                            onChange={(event) => patchHpgl({ penDown: event.target.value })}
                          />
                        </label>
                        <label className="form-row form-row-wide">
                          <span>断笔</span>
                          <input
                            type="text"
                            value={hpgl.penLift}
                            onChange={(event) => patchHpgl({ penLift: event.target.value })}
                          />
                        </label>

                        <TemplateArea
                          label="程序头"
                          hint="如 IN; SP1; VS{speed}; FS{force};"
                          value={hpgl.header}
                          onChange={(rows) => patchHpgl({ header: rows })}
                        />
                        <TemplateArea
                          label="程序尾"
                          hint="如 SP0;"
                          value={hpgl.footer}
                          onChange={(rows) => patchHpgl({ footer: rows })}
                        />
                      </>
                    )}

                    {draft.kind === 'dxf' && (
                      <label className="form-row form-row-wide">
                        <span>DXF 版本</span>
                        <input
                          type="text"
                          value={draft.dxf?.version ?? 'R2010'}
                          placeholder="R2000 / R2010 / R2013"
                          onChange={(event) => patchDraft({
                            dxf: { version: event.target.value },
                          })}
                        />
                      </label>
                    )}
                  </div>

                  <div className="tool-summary">
                    模板占位符：{'{name} {date} {programNo} {tool} {spindle} {safeZ} {x} {y} {z} {feed} {speed} {force}'}；
                    未知占位符会保存失败。内置配置只读，修改请先“另存为”。
                  </div>

                  <div className="form-actions tool-form-actions">
                    <button
                      type="button"
                      className="btn btn-accent"
                      onClick={() => void onSave()}
                      disabled={saving || draft.builtin || draft.name.trim() === ''}
                    >
                      {saving ? '保存中…' : '保存'}
                    </button>
                    <button type="button" className="btn" onClick={copyProcessor} disabled={saving}>
                      另存为
                    </button>
                    <button
                      type="button"
                      className="btn"
                      onClick={() => void onDelete()}
                      disabled={saving || draft.builtin || isNew}
                    >
                      {armed ? '确认删除？' : '删除'}
                    </button>
                  </div>
                </>
              ) : (
                <div className="empty">后处理列表还没加载完成</div>
              )}
            </div>
          </div>
        )}

        <footer className="modal-foot">
          <span className="modal-hint">
            一份 JSON 配置就是一台机床：配置文件在数据目录 post 下，新增设备无需改代码。
          </span>
          <button type="button" className="btn" onClick={close}>关闭</button>
        </footer>
      </div>
    </div>
  );
}
