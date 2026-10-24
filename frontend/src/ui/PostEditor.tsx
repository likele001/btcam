/**
 * 后处理可视化编辑器（模块 11）。
 *
 * 三栏布局：
 *   左   机床列表（内置在前，自定义可删）
 *   中   模板编辑：元信息 + 指令槽位 + 程序头/尾/换刀多行模板
 *   右   变量提示 / 校验结果 / 渲染预览
 *
 * 边界：前端只负责输入与展示。语法校验、工艺告警、渲染预览、文件落盘
 * 全部由后端 `app/post/editor.py` 与 `registry.py` 完成；预览与导出共用
 * 同一个渲染器，所以这里看到什么，导出的文件就是什么。
 */

import { useCallback, useEffect, useMemo, useState } from 'react';

import type {
  GcodePostConfig,
  HpglPostConfig,
  PostKind,
  PostPreviewOptions,
  PostPreviewResult,
  PostProcessorRecord,
  PostValidateResult,
  PostVariable,
  PostVariableDictionary,
} from '@/net/rest';
import { useAppStore } from '@/state/app-store';
import {
  BLANK_GCODE, BLANK_HPGL, KIND_LABEL, cloneProcessor, suggestId, switchKind,
} from './postDrafts';
import { usePostEditorActions } from './usePostEditorActions';

/** 多行模板编辑区：一行一条指令，空行原样保留 */
function TemplateArea({ label, hint, value, bad, readOnly, onChange }: {
  label: string;
  hint?: string;
  value: string[];
  bad: boolean;
  readOnly: boolean;
  onChange: (rows: string[]) => void;
}): JSX.Element {
  return (
    <label className={`pe-area${bad ? ' pe-bad' : ''}`}>
      <span className="pe-area-head">
        {label}
        {hint ? <em>{hint}</em> : null}
      </span>
      <textarea
        rows={Math.min(16, Math.max(4, value.length + 1))}
        spellCheck={false}
        readOnly={readOnly}
        value={value.join('\n')}
        onChange={(event) => onChange(event.target.value.split('\n'))}
      />
    </label>
  );
}

export function PostEditor(): JSX.Element | null {
  const open = useAppStore((s) => s.postEditorOpen);
  const processors = useAppStore((s) => s.postProcessors);
  const target = useAppStore((s) => s.postEditorTarget);
  const setMessage = useAppStore((s) => s.setMessage);

  const { close, loadVariables, validateDraft, previewDraft, save, remove } = usePostEditorActions();

  const [dictionary, setDictionary] = useState<PostVariableDictionary | null>(null);
  const [draft, setDraft] = useState<PostProcessorRecord | null>(null);
  const [check, setCheck] = useState<PostValidateResult | null>(null);
  const [preview, setPreview] = useState<PostPreviewResult | null>(null);
  const [opts, setOpts] = useState<PostPreviewOptions>({
    sizeMm: 100, feed: 3000, spindle: 18000, safeZ: 5, flipY: false,
  });
  const [busy, setBusy] = useState(false);
  const [armed, setArmed] = useState(false);
  const [copied, setCopied] = useState('');

  // 打开瞬间：挑中要编辑的机床，并拉一次变量字典（进程内已缓存则秒回）
  useEffect(() => {
    if (!open) return;
    setArmed(false);
    setCopied('');
    setCheck(null);
    setPreview(null);
    setOpts({ sizeMm: 100, feed: 3000, spindle: 18000, safeZ: 5, flipY: false });
    const preferred = processors.find((item) => item.id === target)
      ?? processors.find((item) => !item.builtin)
      ?? processors[0]
      ?? null;
    setDraft(preferred ? cloneProcessor(preferred) : null);
    void loadVariables().then((data) => {
      if (data) setDictionary(data);
    });
    // 只在开窗瞬间初始化；之后由列表点击与保存结果控制
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // 列表是异步拉的：打开时为空也能在返回后落到第一台机床上
  useEffect(() => {
    if (!open || draft || processors.length === 0) return;
    setDraft(cloneProcessor(processors[0]));
  }, [open, draft, processors]);

  /** 校验 + 预览：后端一把算完，前端只负责展示 */
  const runCheck = useCallback(async (
    record: PostProcessorRecord,
    options: PostPreviewOptions,
  ) => {
    setBusy(true);
    const result = await validateDraft(record);
    setCheck(result);
    if (result?.ok) {
      setPreview(await previewDraft(record, options));
    } else {
      setPreview(null);
    }
    setBusy(false);
  }, [validateDraft, previewDraft]);

  // 改动后自动校验（防抖）：模板动一行就能看到错误与预览变化
  useEffect(() => {
    if (!open || !draft) return;
    const timer = window.setTimeout(() => { void runCheck(draft, opts); }, 350);
    return () => window.clearTimeout(timer);
  }, [open, draft, opts, runCheck]);

  const isNew = draft !== null && !processors.some((item) => item.id === draft.id);
  const readOnly = draft?.builtin === true;
  const gcode = draft?.gcode ?? BLANK_GCODE;
  const hpgl = draft?.hpgl ?? BLANK_HPGL;

  const errorFields = useMemo(
    () => new Set((check?.errors ?? []).map((item) => item.field)),
    [check],
  );
  const bad = (field: string): boolean => errorFields.has(field);
  const badArea = (prefix: string): boolean =>
    [...errorFields].some((field) => field.startsWith(prefix));

  /** 变量按 group 分组：任务 / 机床 / 坐标 / 刻绘 */
  const groups = useMemo(() => {
    const map = new Map<string, PostVariable[]>();
    (dictionary?.variables ?? []).forEach((item) => {
      const rows = map.get(item.group) ?? [];
      rows.push(item);
      map.set(item.group, rows);
    });
    return [...map.entries()];
  }, [dictionary]);

  if (!open) return null;

  const touch = () => {
    setArmed(false);
  };

  const patchDraft = (patch: Partial<PostProcessorRecord>) => {
    touch();
    setDraft((prev) => (prev ? { ...prev, ...patch } : prev));
  };

  const patchGcode = (patch: Partial<GcodePostConfig>) => {
    touch();
    setDraft((prev) => (prev
      ? { ...prev, gcode: { ...(prev.gcode ?? BLANK_GCODE), ...patch } }
      : prev));
  };

  const patchHpgl = (patch: Partial<HpglPostConfig>) => {
    touch();
    setDraft((prev) => (prev
      ? { ...prev, hpgl: { ...(prev.hpgl ?? BLANK_HPGL), ...patch } }
      : prev));
  };

  const pick = (record: PostProcessorRecord) => {
    setDraft(cloneProcessor(record));
    setArmed(false);
  };

  const newTemplate = () => {
    const base = draft ?? processors[0];
    if (!base) return;
    setDraft({
      ...cloneProcessor(base),
      id: suggestId('custom_post', processors),
      name: '自定义后处理',
      builtin: false,
      version: 1,
    });
    setArmed(false);
  };

  const onSave = async () => {
    if (!draft) return;
    const saved = await save(draft, isNew);
    if (saved) setDraft(cloneProcessor(saved));
  };

  /** 另存为：内置模板改 id 后成为可编辑的自定义模板 */
  const onSaveAs = async () => {
    if (!draft) return;
    const copy: PostProcessorRecord = {
      ...cloneProcessor(draft),
      id: suggestId(`${draft.id}_copy`, processors),
      name: `${draft.name} 副本`,
      builtin: false,
    };
    setDraft(copy);
    const saved = await save(copy, true);
    if (saved) {
      setDraft(cloneProcessor(saved));
      setMessage(`已另存为自定义模板：${saved.name}（${saved.id}）`);
    }
  };

  const onDelete = async () => {
    if (!draft || draft.builtin || isNew) return;
    if (!armed) {
      setArmed(true);
      return;
    }
    const done = await remove(draft.id);
    setArmed(false);
    if (!done) return;
    const rest = useAppStore.getState().postProcessors;
    setDraft(rest[0] ? cloneProcessor(rest[0]) : null);
  };

  /** 变量面板点击复制占位符：Tauri 里剪贴板偶尔不可用，退化成提示 */
  const copyVariable = async (name: string) => {
    const token = `{${name}}`;
    try {
      await navigator.clipboard.writeText(token);
      setCopied(name);
      window.setTimeout(() => setCopied(''), 1500);
    } catch {
      setMessage(`占位符 ${token}（复制失败，请手动输入）`);
    }
  };

  /** 指令槽位（不含 header/footer/toolChange，它们走多行编辑区） */
  const slotKeys = (dictionary?.instructions?.[draft?.kind ?? 'gcode'] ?? [])
    .filter((key) => !['header', 'footer', 'toolChange', 'version'].includes(key));
  const slotLabel = (key: string): string =>
    dictionary?.instructionLabels?.[draft?.kind ?? 'gcode']?.[key] ?? key;
  const slotValue = (key: string): string => {
    if (draft?.kind === 'gcode') {
      return String((draft.gcode as unknown as Record<string, unknown>)?.[key] ?? '');
    }
    if (draft?.kind === 'hpgl') {
      return String((draft.hpgl as unknown as Record<string, unknown>)?.[key] ?? '');
    }
    return '';
  };
  const onSlot = (key: string, text: string) => {
    if (draft?.kind === 'gcode') patchGcode({ [key]: text } as Partial<GcodePostConfig>);
    else if (draft?.kind === 'hpgl') patchHpgl({ [key]: text } as Partial<HpglPostConfig>);
  };

  return (
    <div
      className="modal-mask"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) close();
      }}
    >
      <div className="modal post-editor" role="dialog" aria-modal="true">
        <header className="modal-head">
          <span>后处理可视化编辑器</span>
          <span className="modal-sub">
            模块 11 · 改模板 → 后端校验 → 实时预览；预览与导出同一渲染器
          </span>
          <button type="button" className="modal-close" title="关闭" onClick={close}>×</button>
        </header>

        {processors.length === 0 ? (
          <div className="empty pe-empty">后处理列表还没加载完成，请稍后重开</div>
        ) : (
          <div className="pe-body">
            {/* -------------------------------------------------- 左：机床列表 */}
            <aside className="pe-side">
              <div className="pe-side-head">
                <span>机床</span>
                <button type="button" className="btn" onClick={newTemplate}>新建</button>
              </div>
              <div className="pe-list">
                {processors.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    className={`pe-item${draft?.id === item.id ? ' pe-item-active' : ''}`}
                    onClick={() => pick(item)}
                  >
                    <span className="pe-item-name">{item.name}</span>
                    <span className="pe-item-meta">
                      {KIND_LABEL[item.kind]} · .{item.ext}
                      {item.builtin ? ' · 内置' : ''}
                    </span>
                  </button>
                ))}
              </div>
            </aside>

            {/* -------------------------------------------------- 中：模板编辑 */}
            <section className="pe-edit">
              {!draft ? (
                <div className="empty">还没有可编辑的模板</div>
              ) : (
                <>
                  <div className="pe-edit-head">
                    {draft.builtin
                      ? `内置模板 · ${draft.id}（只读，改前先另存为）`
                      : (isNew ? `新建模板 · ${draft.id}` : `编辑模板 · ${draft.id}`)}
                  </div>

                  {draft.builtin && (
                    <div className="pe-banner">
                      内置模板受保护：直接保存会被后端拒绝。点下方「另存为」生成自定义副本后再改，
                      副本会出现在导出下拉里。
                    </div>
                  )}

                  <div className="form pe-meta">
                    <label className="pe-line">
                      <span>名称</span>
                      <input
                        type="text"
                        value={draft.name}
                        readOnly={readOnly}
                        onChange={(event) => patchDraft({ name: event.target.value })}
                      />
                    </label>
                    <label className={`pe-line${bad('id') ? ' pe-bad' : ''}`}>
                      <span>标识 id</span>
                      <input
                        type="text"
                        value={draft.id}
                        readOnly={readOnly || !isNew}
                        title="2~40 位小写字母/数字/下划线，保存后不可改"
                        onChange={(event) => patchDraft({ id: event.target.value })}
                      />
                    </label>
                    <label className={`pe-line${bad('kind') ? ' pe-bad' : ''}`}>
                      <span>类别</span>
                      <select
                        value={draft.kind}
                        disabled={readOnly}
                        onChange={(event) => {
                          touch();
                          setDraft((prev) => (prev
                            ? switchKind(prev, event.target.value as PostKind)
                            : prev));
                        }}
                      >
                        <option value="gcode">G 代码</option>
                        <option value="hpgl">HPGL 笔式</option>
                        <option value="dxf">DXF 图形</option>
                      </select>
                    </label>
                    <label className={`pe-line${bad('ext') ? ' pe-bad' : ''}`}>
                      <span>扩展名</span>
                      <input
                        type="text"
                        value={draft.ext}
                        readOnly={readOnly}
                        onChange={(event) => patchDraft({ ext: event.target.value })}
                      />
                    </label>
                    <label className="pe-line pe-line-wide">
                      <span>说明</span>
                      <input
                        type="text"
                        value={draft.description}
                        readOnly={readOnly}
                        placeholder="适用机型 / 注意事项"
                        onChange={(event) => patchDraft({ description: event.target.value })}
                      />
                    </label>
                  </div>

                  {draft.kind === 'gcode' && (
                    <>
                      <div className="pe-sec">数值与行号</div>
                      <div className="form pe-meta">
                        <label className={`pe-line${bad('gcode.precision') ? ' pe-bad' : ''}`}>
                          <span>小数精度</span>
                          <input
                            type="number"
                            min={0}
                            max={6}
                            step={1}
                            value={gcode.precision}
                            readOnly={readOnly}
                            onChange={(event) => patchGcode({ precision: Number(event.target.value) })}
                          />
                        </label>
                        <label className={`pe-line${bad('gcode.lineNumberStart') ? ' pe-bad' : ''}`}>
                          <span>行号起始</span>
                          <input
                            type="number"
                            min={0}
                            step={10}
                            value={gcode.lineNumberStart}
                            readOnly={readOnly}
                            onChange={(event) => patchGcode({ lineNumberStart: Number(event.target.value) })}
                          />
                        </label>
                        <label className={`pe-line${bad('gcode.lineNumberStep') ? ' pe-bad' : ''}`}>
                          <span>行号步长</span>
                          <input
                            type="number"
                            min={1}
                            step={10}
                            value={gcode.lineNumberStep}
                            readOnly={readOnly}
                            onChange={(event) => patchGcode({ lineNumberStep: Number(event.target.value) })}
                          />
                        </label>
                        <label className={`pe-line${bad('gcode.programNo') ? ' pe-bad' : ''}`}>
                          <span>程序号</span>
                          <input
                            type="text"
                            value={gcode.programNo}
                            readOnly={readOnly}
                            onChange={(event) => patchGcode({ programNo: event.target.value })}
                          />
                        </label>
                        <label className="pe-line pe-check">
                          <span>输出行号</span>
                          <input
                            type="checkbox"
                            checked={gcode.lineNumbers}
                            disabled={readOnly}
                            onChange={(event) => patchGcode({ lineNumbers: event.target.checked })}
                          />
                        </label>
                      </div>

                      <div className="pe-sec">程序头 / 尾 / 换刀</div>
                      <TemplateArea
                        label="程序头"
                        hint="一行一条；可用变量见右侧面板"
                        value={gcode.header}
                        bad={badArea('gcode.header')}
                        readOnly={readOnly}
                        onChange={(rows) => patchGcode({ header: rows })}
                      />
                      <TemplateArea
                        label="程序尾"
                        hint="建议回安全高度并关主轴"
                        value={gcode.footer}
                        bad={badArea('gcode.footer')}
                        readOnly={readOnly}
                        onChange={(rows) => patchGcode({ footer: rows })}
                      />
                      <TemplateArea
                        label="换刀指令"
                        hint="有刀具号时才插入"
                        value={gcode.toolChange}
                        bad={badArea('gcode.toolChange')}
                        readOnly={readOnly}
                        onChange={(rows) => patchGcode({ toolChange: rows })}
                      />
                    </>
                  )}

                  {draft.kind === 'hpgl' && (
                    <>
                      <div className="pe-sec">笔式参数</div>
                      <div className="form pe-meta">
                        <label className={`pe-line${bad('hpgl.unitsPerMm') ? ' pe-bad' : ''}`}>
                          <span>单位/毫米</span>
                          <input
                            type="number"
                            min={0.1}
                            step={1}
                            value={hpgl.unitsPerMm}
                            readOnly={readOnly}
                            onChange={(event) => patchHpgl({ unitsPerMm: Number(event.target.value) })}
                          />
                        </label>
                      </div>
                      <TemplateArea
                        label="初始化指令"
                        hint="如 IN; SP1; VS{speed}; FS{force};"
                        value={hpgl.header}
                        bad={badArea('hpgl.header')}
                        readOnly={readOnly}
                        onChange={(rows) => patchHpgl({ header: rows })}
                      />
                      <TemplateArea
                        label="收尾指令"
                        hint="如 SP0;"
                        value={hpgl.footer}
                        bad={badArea('hpgl.footer')}
                        readOnly={readOnly}
                        onChange={(rows) => patchHpgl({ footer: rows })}
                      />
                    </>
                  )}

                  {draft.kind !== 'dxf' && (
                    <>
                      <div className="pe-sec">指令槽位</div>
                      <div className="form pe-meta">
                        {slotKeys.map((key) => (
                          <label
                            key={key}
                            className={`pe-line pe-line-wide${bad(`${draft.kind}.${key}`) ? ' pe-bad' : ''}`}
                          >
                            <span>{slotLabel(key)}</span>
                            <input
                              type="text"
                              value={slotValue(key)}
                              readOnly={readOnly}
                              onChange={(event) => onSlot(key, event.target.value)}
                            />
                          </label>
                        ))}
                      </div>
                    </>
                  )}

                  {draft.kind === 'dxf' && (
                    <div className="form pe-meta">
                      <label className="pe-line pe-line-wide">
                        <span>DXF 版本</span>
                        <input
                          type="text"
                          value={draft.dxf?.version ?? 'R2010'}
                          readOnly={readOnly}
                          placeholder="R2000 / R2010 / R2013"
                          onChange={(event) => patchDraft({ dxf: { version: event.target.value } })}
                        />
                      </label>
                      <div className="pe-banner">
                        DXF 后处理没有模板文本：导出时由后端 ezdxf 按图层写回图形。
                      </div>
                    </div>
                  )}

                  <div className="pe-actions">
                    <button
                      type="button"
                      className="btn btn-accent"
                      onClick={() => void onSave()}
                      disabled={busy || readOnly || !check?.ok}
                      title={check?.ok ? '保存到数据目录 post 下' : '先按右侧提示修好模板'}
                    >
                      {isNew ? '保存为新模板' : '保存模板'}
                    </button>
                    <button type="button" className="btn" onClick={() => void onSaveAs()} disabled={busy}>
                      另存为
                    </button>
                    <button
                      type="button"
                      className="btn"
                      onClick={() => void onDelete()}
                      disabled={busy || draft.builtin || isNew}
                    >
                      {armed ? '确认删除？' : '删除'}
                    </button>
                  </div>
                </>
              )}
            </section>

            {/* -------------------------------------------------- 右：变量 + 校验 + 预览 */}
            <section className="pe-preview">
              <div className="pe-sec">可用变量（点击复制）</div>
              <div className="pe-vars">
                {groups.length === 0 && <div className="empty">变量字典加载中…</div>}
                {groups.map(([group, rows]) => (
                  <div key={group} className="pe-var-group">
                    <div className="pe-var-title">{group}</div>
                    <div className="pe-var-list">
                      {rows.map((item) => (
                        <button
                          key={item.name}
                          type="button"
                          className={`pe-var${copied === item.name ? ' pe-var-copied' : ''}`}
                          title={`${item.desc}；示例：${item.example}`}
                          onClick={() => void copyVariable(item.name)}
                        >
                          {`{${item.name}}`}
                        </button>
                      ))}
                    </div>
                  </div>
                ))}
              </div>

              <div className="pe-sec">
                校验
                {check && (
                  <span className={`pe-badge${check.ok ? ' pe-badge-ok' : ' pe-badge-bad'}`}>
                    {check.ok ? '通过' : `${check.errors.length} 个错误`}
                  </span>
                )}
                {busy && <span className="pe-dim">校验中…</span>}
              </div>
              {check && (
                <div className="pe-issues">
                  <div className="pe-used">
                    变量：已用 {check.usedVariables.length}
                    {' · '}未用 {check.unusedVariables.length}
                    {check.unknownVariables.length > 0
                      && ` · 未知 ${check.unknownVariables.join(', ')}`}
                  </div>
                  {check.errors.map((item) => (
                    <div key={`${item.field}-${item.message}`} className="pe-issue">
                      <b>{item.field}</b>
                      <span>{item.message}</span>
                      {item.hint && <em>{item.hint}</em>}
                    </div>
                  ))}
                  {check.warnings.map((item) => (
                    <div key={`${item.field}-${item.message}`} className="pe-warn">
                      <b>{item.field}</b>
                      <span>{item.message}</span>
                    </div>
                  ))}
                </div>
              )}

              <div className="pe-sec">
                预览
                {preview?.ok && (
                  <span className="pe-dim">
                    {preview.lineCount} 行 · {preview.bytes} 字节
                  </span>
                )}
              </div>
              <div className="pe-opts">
                <label>
                  尺寸
                  <input
                    type="number"
                    min={5}
                    step={10}
                    value={opts.sizeMm ?? 100}
                    onChange={(event) => setOpts((prev) => ({ ...prev, sizeMm: Number(event.target.value) }))}
                  />
                </label>
                <label>
                  进给
                  <input
                    type="number"
                    min={1}
                    step={100}
                    value={opts.feed ?? 3000}
                    onChange={(event) => setOpts((prev) => ({ ...prev, feed: Number(event.target.value) }))}
                  />
                </label>
                <label className="pe-opts-check">
                  <input
                    type="checkbox"
                    checked={Boolean(opts.flipY)}
                    onChange={(event) => setOpts((prev) => ({ ...prev, flipY: event.target.checked }))}
                  />
                  Y 翻转
                </label>
                <button
                  type="button"
                  className="btn"
                  onClick={() => (draft ? void runCheck(draft, opts) : undefined)}
                  disabled={!draft || busy}
                >
                  刷新
                </button>
              </div>

              {preview?.note && <div className="pe-note">{preview.note}</div>}
              {preview?.ok && preview.text ? (
                <div className="pe-code">
                  {preview.text.replace(/\n$/, '').split('\n').map((line, index) => (
                    <div className="pe-code-row" key={index}>
                      <span className="pe-ln">{index + 1}</span>
                      <span className="pe-code-text">{line || '\u00a0'}</span>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="empty pe-preview-empty">
                  {check && !check.ok ? '模板校验未通过，先改右侧列出的问题' : '校验通过后这里显示渲染结果'}
                </div>
              )}
            </section>
          </div>
        )}

        <footer className="modal-foot">
          <span className="modal-hint">
            模板落盘在数据目录 post 下（一份 JSON 一台机床）；保存后导出时选中它即可生效，
            内置模板不会被覆盖。
          </span>
          <button type="button" className="btn" onClick={close}>关闭</button>
        </footer>
      </div>
    </div>
  );
}
