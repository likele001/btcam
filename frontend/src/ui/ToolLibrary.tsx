/**
 * 刀具库 / 工艺模板弹窗（模块 6）。
 *
 * 两个页签共用一条左侧列表 + 右侧表单的布局：
 *   刀具：铣刀（平底/锥刀/球刀）与线切割钼丝，参数含补偿直径/进给/分层/抬刀/方向；
 *   模板：按物料分类，一把刀 + 一组补偿/微连接参数，一键加载到刀路面板。
 *
 * 这里只编辑与搬运数据：校验、换算、补偿全部由后端完成。
 */

import { useEffect, useState } from 'react';

import type {
  TemplateInput,
  TemplateRecord,
  ToolInput,
  ToolRecord,
} from '@/net/rest';
import { useAppStore } from '@/state/app-store';
import { useToolLibraryActions } from './useToolLibraryActions';

type Tab = 'tools' | 'templates';

const BLANK_TOOL: ToolInput = {
  name: '',
  category: 'mill',
  toolType: 'flat',
  diameter: 6,
  tipRadius: 0,
  wireDiameter: 0.2,
  dischargeGap: 0.03,
  feed: 3000,
  plungeFeed: 800,
  spindle: 18000,
  passDepth: 3,
  safeZ: 5,
  liftZ: 5,
  direction: 'climb',
  note: '',
};

const BLANK_TEMPLATE: TemplateInput = {
  name: '',
  material: '',
  toolId: null,
  params: {
    cutType: 'auto',
    side: 'auto',
    optimize: true,
    microJoint: false,
    microJointWidth: 2,
    microJointCount: 2,
    leadIn: true,
    leadRadius: 2,
  },
  note: '',
};

function toolToInput(tool: ToolRecord): ToolInput {
  return {
    name: tool.name,
    category: tool.category,
    toolType: tool.toolType,
    diameter: tool.diameter,
    tipRadius: tool.tipRadius,
    wireDiameter: tool.wireDiameter,
    dischargeGap: tool.dischargeGap,
    feed: tool.feed,
    plungeFeed: tool.plungeFeed,
    spindle: tool.spindle,
    passDepth: tool.passDepth,
    safeZ: tool.safeZ,
    liftZ: tool.liftZ,
    direction: tool.direction,
    note: tool.note,
  };
}

function templateToInput(template: TemplateRecord): TemplateInput {
  return {
    name: template.name,
    material: template.material,
    toolId: template.toolId,
    params: { ...template.params },
    note: template.note,
  };
}

const toolTypeLabel: Record<ToolRecord['toolType'], string> = {
  flat: '平底刀',
  ball: '球刀',
  v: '锥刀',
};

export function ToolLibrary(): JSX.Element | null {
  const open = useAppStore((s) => s.toolLibraryOpen);
  const tools = useAppStore((s) => s.tools);
  const templates = useAppStore((s) => s.templates);

  const {
    close, saveTool, deleteTool, saveTemplate, deleteTemplate, selectTool, selectTemplate,
  } = useToolLibraryActions();

  const [tab, setTab] = useState<Tab>('tools');
  const [toolId, setToolId] = useState<number | null>(null);
  const [toolDraft, setToolDraft] = useState<ToolInput>(BLANK_TOOL);
  const [templateId, setTemplateId] = useState<number | null>(null);
  const [templateDraft, setTemplateDraft] = useState<TemplateInput>(BLANK_TEMPLATE);
  // 删除是破坏性操作：第一次点击变成“确认删除”，切换选中即复位
  const [armed, setArmed] = useState(false);
  const [saving, setSaving] = useState(false);
  // 新建态：列表异步返回时不要自动把第一条盖到空白草稿上
  const [creatingTool, setCreatingTool] = useState(false);
  const [creatingTemplate, setCreatingTemplate] = useState(false);

  // 打开时回到第一把刀 / 第一个模板，避免残留上次编辑状态
  useEffect(() => {
    if (!open) return;
    setArmed(false);
    setTab('tools');
    setCreatingTool(false);
    setCreatingTemplate(false);
    const firstTool = tools[0];
    setToolId(firstTool?.id ?? null);
    setToolDraft(firstTool ? toolToInput(firstTool) : BLANK_TOOL);
    const firstTemplate = templates[0];
    setTemplateId(firstTemplate?.id ?? null);
    setTemplateDraft(firstTemplate ? templateToInput(firstTemplate) : BLANK_TEMPLATE);
    // 只在弹窗打开瞬间同步一次，之后由列表点击控制
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // 列表异步返回时兜底：当前没选中且不是新建态 -> 自动选第一条
  useEffect(() => {
    if (!open || creatingTool || tools.length === 0 || toolId !== null) return;
    setToolId(tools[0].id);
    setToolDraft(toolToInput(tools[0]));
  }, [open, creatingTool, tools, toolId]);

  useEffect(() => {
    if (!open || creatingTemplate || templates.length === 0 || templateId !== null) return;
    setTemplateId(templates[0].id);
    setTemplateDraft(templateToInput(templates[0]));
  }, [open, creatingTemplate, templates, templateId]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, close]);

  if (!open) return null;

  const pickTool = (tool: ToolRecord) => {
    setToolId(tool.id);
    setToolDraft(toolToInput(tool));
    setCreatingTool(false);
    setArmed(false);
  };

  const newTool = () => {
    setToolId(null);
    setToolDraft({ ...BLANK_TOOL, note: '' });
    setCreatingTool(true);
    setArmed(false);
  };

  const pickTemplate = (template: TemplateRecord) => {
    setTemplateId(template.id);
    setTemplateDraft(templateToInput(template));
    setCreatingTemplate(false);
    setArmed(false);
  };

  const newTemplate = () => {
    setTemplateId(null);
    setTemplateDraft({
      ...BLANK_TEMPLATE,
      params: { ...BLANK_TEMPLATE.params },
      toolId: tools[0]?.id ?? null,
    });
    setCreatingTemplate(true);
    setArmed(false);
  };

  const onSaveTool = async () => {
    setSaving(true);
    setArmed(false);
    const saved = await saveTool(toolId, toolDraft);
    setSaving(false);
    if (saved) {
      setToolId(saved.id);
      setToolDraft(toolToInput(saved));
      setCreatingTool(false);
    }
  };

  const onDeleteTool = async () => {
    if (toolId === null) return;
    if (!armed) {
      setArmed(true);
      return;
    }
    const removed = await deleteTool(toolId);
    setArmed(false);
    if (!removed) return;
    const rest = useAppStore.getState().tools;
    if (rest.length > 0) {
      setToolId(rest[0].id);
      setToolDraft(toolToInput(rest[0]));
      setCreatingTool(false);
    } else {
      setToolId(null);
      setToolDraft({ ...BLANK_TOOL });
      setCreatingTool(false);
    }
  };

  const onSaveTemplate = async () => {
    setSaving(true);
    setArmed(false);
    const saved = await saveTemplate(templateId, templateDraft);
    setSaving(false);
    if (saved) {
      setTemplateId(saved.id);
      setTemplateDraft(templateToInput(saved));
      setCreatingTemplate(false);
    }
  };

  const onDeleteTemplate = async () => {
    if (templateId === null) return;
    if (!armed) {
      setArmed(true);
      return;
    }
    const removed = await deleteTemplate(templateId);
    setArmed(false);
    if (!removed) return;
    const rest = useAppStore.getState().templates;
    if (rest.length > 0) {
      setTemplateId(rest[0].id);
      setTemplateDraft(templateToInput(rest[0]));
      setCreatingTemplate(false);
    } else {
      setTemplateId(null);
      setTemplateDraft({ ...BLANK_TEMPLATE, params: { ...BLANK_TEMPLATE.params } });
      setCreatingTemplate(false);
    }
  };

  const applyCurrent = () => {
    if (tab === 'tools' && toolId !== null) selectTool(toolId);
    if (tab === 'templates' && templateId !== null) selectTemplate(templateId);
    close();
  };

  /** 仅用于表单即时预览，真正的补偿量以后端返回的 effectiveDiameter 为准 */
  const effective = toolDraft.category === 'wire'
    ? toolDraft.wireDiameter + 2 * toolDraft.dischargeGap
    : toolDraft.diameter;

  const patchTool = (patch: Partial<ToolInput>) => setToolDraft((prev) => ({ ...prev, ...patch }));
  const patchParams = (patch: Partial<TemplateInput['params']>) =>
    setTemplateDraft((prev) => ({ ...prev, params: { ...prev.params, ...patch } }));

  const canApply = tab === 'tools' ? toolId !== null : templateId !== null;

  return (
    <div
      className="modal-mask"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) close();
      }}
    >
      <div className="modal tool-library" role="dialog" aria-modal="true">
        <header className="modal-head">
          <span>刀具库与工艺模板</span>
          <span className="modal-sub">模块 6 · 参数由后端换算补偿量</span>
          <button type="button" className="modal-close" title="关闭" onClick={close}>×</button>
        </header>

        <div className="modal-tabs">
          <button
            type="button"
            className={`modal-tab${tab === 'tools' ? ' active' : ''}`}
            onClick={() => { setTab('tools'); setArmed(false); }}
          >
            刀具库 <span className="modal-tab-count">{tools.length}</span>
          </button>
          <button
            type="button"
            className={`modal-tab${tab === 'templates' ? ' active' : ''}`}
            onClick={() => { setTab('templates'); setArmed(false); }}
          >
            工艺模板 <span className="modal-tab-count">{templates.length}</span>
          </button>
        </div>

        <div className="tool-library-body">
          <div className="tool-list-pane">
            <div className="tool-list-head">
              <span>{tab === 'tools' ? '刀具' : '模板'}</span>
              <button
                type="button"
                className="btn"
                onClick={tab === 'tools' ? newTool : newTemplate}
              >
                新建
              </button>
            </div>
            <div className="tool-list">
              {tab === 'tools' && tools.length === 0 && (
                <div className="empty">还没有刀具，点“新建”添加</div>
              )}
              {tab === 'tools' && tools.map((tool) => (
                <button
                  key={tool.id}
                  type="button"
                  className={`tool-list-row${toolId === tool.id ? ' active' : ''}`}
                  onClick={() => pickTool(tool)}
                >
                  <span className="tool-row-name">{tool.name}</span>
                  <span className="tool-row-meta">
                    {tool.category === 'wire' ? '钼丝' : toolTypeLabel[tool.toolType]}
                    {' · '}
                    {tool.category === 'wire'
                      ? `丝径 ${tool.wireDiameter}mm`
                      : `⌀${tool.diameter}mm`}
                    {' · 补偿 ⌀'}
                    {tool.effectiveDiameter.toFixed(3)}
                  </span>
                </button>
              ))}

              {tab === 'templates' && templates.length === 0 && (
                <div className="empty">还没有工艺模板，点“新建”添加</div>
              )}
              {tab === 'templates' && templates.map((template) => (
                <button
                  key={template.id}
                  type="button"
                  className={`tool-list-row${templateId === template.id ? ' active' : ''}`}
                  onClick={() => pickTemplate(template)}
                >
                  <span className="tool-row-name">{template.name}</span>
                  <span className="tool-row-meta">
                    {template.material || '未分类'}
                    {' · '}
                    {template.tool ? template.tool.name : '未绑定刀具'}
                  </span>
                </button>
              ))}
            </div>
          </div>

          <div className="tool-form-pane">
            {tab === 'tools' && (
              <>
                <div className="tool-form-head">
                  {toolId === null ? '新建刀具' : `编辑刀具 #${toolId}`}
                </div>
                <div className="form tool-form-grid">
                  <label className="form-row form-row-wide">
                    <span>名称</span>
                    <input
                      type="text"
                      value={toolDraft.name}
                      placeholder="如 T1 平底 6mm"
                      onChange={(event) => patchTool({ name: event.target.value })}
                    />
                  </label>
                  <label className="form-row">
                    <span>分类</span>
                    <select
                      value={toolDraft.category}
                      onChange={(event) => patchTool({
                        category: event.target.value as ToolInput['category'],
                      })}
                    >
                      <option value="mill">铣刀</option>
                      <option value="wire">线切割钼丝</option>
                    </select>
                  </label>
                  {toolDraft.category === 'mill' ? (
                    <>
                      <label className="form-row">
                        <span>类型</span>
                        <select
                          value={toolDraft.toolType}
                          onChange={(event) => patchTool({
                            toolType: event.target.value as ToolInput['toolType'],
                          })}
                        >
                          <option value="flat">平底刀</option>
                          <option value="ball">球刀</option>
                          <option value="v">锥刀</option>
                        </select>
                      </label>
                      <label className="form-row">
                        <span>直径 (mm)</span>
                        <input
                          type="number"
                          min={0.01}
                          step={0.5}
                          value={toolDraft.diameter}
                          onChange={(event) => patchTool({ diameter: Number(event.target.value) })}
                        />
                      </label>
                      <label className="form-row">
                        <span>刀尖圆角 (mm)</span>
                        <input
                          type="number"
                          min={0}
                          step={0.1}
                          value={toolDraft.tipRadius}
                          onChange={(event) => patchTool({ tipRadius: Number(event.target.value) })}
                        />
                      </label>
                    </>
                  ) : (
                    <>
                      <label className="form-row">
                        <span>丝径 (mm)</span>
                        <input
                          type="number"
                          min={0.01}
                          step={0.05}
                          value={toolDraft.wireDiameter}
                          onChange={(event) => patchTool({ wireDiameter: Number(event.target.value) })}
                        />
                      </label>
                      <label className="form-row">
                        <span>放电间隙 (mm)</span>
                        <input
                          type="number"
                          min={0}
                          step={0.01}
                          value={toolDraft.dischargeGap}
                          onChange={(event) => patchTool({ dischargeGap: Number(event.target.value) })}
                        />
                      </label>
                    </>
                  )}
                  <label className="form-row">
                    <span>进给 (mm/min)</span>
                    <input
                      type="number"
                      min={1}
                      step={100}
                      value={toolDraft.feed}
                      onChange={(event) => patchTool({ feed: Number(event.target.value) })}
                    />
                  </label>
                  <label className="form-row">
                    <span>下刀 (mm/min)</span>
                    <input
                      type="number"
                      min={1}
                      step={50}
                      value={toolDraft.plungeFeed}
                      onChange={(event) => patchTool({ plungeFeed: Number(event.target.value) })}
                    />
                  </label>
                  {toolDraft.category === 'mill' && (
                    <label className="form-row">
                      <span>主轴 (rpm)</span>
                      <input
                        type="number"
                        min={0}
                        step={1000}
                        value={toolDraft.spindle}
                        onChange={(event) => patchTool({ spindle: Number(event.target.value) })}
                      />
                    </label>
                  )}
                  <label className="form-row">
                    <span>每层切深 (mm)</span>
                    <input
                      type="number"
                      min={0.01}
                      step={0.5}
                      value={toolDraft.passDepth}
                      onChange={(event) => patchTool({ passDepth: Number(event.target.value) })}
                    />
                  </label>
                  <label className="form-row">
                    <span>安全高度 (mm)</span>
                    <input
                      type="number"
                      min={0}
                      step={1}
                      value={toolDraft.safeZ}
                      onChange={(event) => patchTool({ safeZ: Number(event.target.value) })}
                    />
                  </label>
                  <label className="form-row">
                    <span>抬刀高度 (mm)</span>
                    <input
                      type="number"
                      min={0}
                      step={1}
                      value={toolDraft.liftZ}
                      onChange={(event) => patchTool({ liftZ: Number(event.target.value) })}
                    />
                  </label>
                  <label className="form-row">
                    <span>走刀方向</span>
                    <select
                      value={toolDraft.direction}
                      onChange={(event) => patchTool({
                        direction: event.target.value as ToolInput['direction'],
                      })}
                    >
                      <option value="climb">顺铣</option>
                      <option value="conventional">逆铣</option>
                    </select>
                  </label>
                  <label className="form-row form-row-wide">
                    <span>备注</span>
                    <input
                      type="text"
                      value={toolDraft.note}
                      placeholder="适用材料 / 机床限制"
                      onChange={(event) => patchTool({ note: event.target.value })}
                    />
                  </label>
                </div>

                <div className="tool-summary">
                  补偿量预览：等效直径 ⌀{effective.toFixed(3)} mm · 半径 R{(effective / 2).toFixed(3)} mm
                  {toolDraft.category === 'wire' ? '（丝径 + 2×放电间隙）' : ''}
                </div>

                <div className="form-actions tool-form-actions">
                  <button
                    type="button"
                    className="btn btn-accent"
                    onClick={() => void onSaveTool()}
                    disabled={saving || toolDraft.name.trim() === ''}
                  >
                    {saving ? '保存中…' : '保存'}
                  </button>
                  <button
                    type="button"
                    className="btn"
                    onClick={() => void onDeleteTool()}
                    disabled={toolId === null || saving}
                  >
                    {armed ? '确认删除？' : '删除'}
                  </button>
                  <button
                    type="button"
                    className="btn"
                    onClick={applyCurrent}
                    disabled={toolId === null || saving}
                  >
                    应用到刀路
                  </button>
                </div>
              </>
            )}

            {tab === 'templates' && (
              <>
                <div className="tool-form-head">
                  {templateId === null ? '新建工艺模板' : `编辑模板 #${templateId}`}
                </div>
                <div className="form tool-form-grid">
                  <label className="form-row form-row-wide">
                    <span>名称</span>
                    <input
                      type="text"
                      value={templateDraft.name}
                      placeholder="如 密度板 18mm 标准"
                      onChange={(event) => setTemplateDraft((prev) => ({
                        ...prev, name: event.target.value,
                      }))}
                    />
                  </label>
                  <label className="form-row form-row-wide">
                    <span>物料</span>
                    <input
                      type="text"
                      value={templateDraft.material}
                      placeholder="密度板 / 多层板 / 无纺布…"
                      onChange={(event) => setTemplateDraft((prev) => ({
                        ...prev, material: event.target.value,
                      }))}
                    />
                  </label>
                  <label className="form-row form-row-wide">
                    <span>绑定刀具</span>
                    <select
                      value={templateDraft.toolId ?? ''}
                      onChange={(event) => setTemplateDraft((prev) => ({
                        ...prev,
                        toolId: event.target.value ? Number(event.target.value) : null,
                      }))}
                    >
                      <option value="">不绑定</option>
                      {tools.map((tool) => (
                        <option key={tool.id} value={tool.id}>{tool.name}</option>
                      ))}
                    </select>
                  </label>
                  <label className="form-row">
                    <span>刀路类型</span>
                    <select
                      value={templateDraft.params.cutType}
                      onChange={(event) => patchParams({
                        cutType: event.target.value as TemplateInput['params']['cutType'],
                      })}
                    >
                      <option value="auto">外轮廓 + 内孔</option>
                      <option value="outer">仅外轮廓</option>
                      <option value="inner">仅内孔</option>
                    </select>
                  </label>
                  <label className="form-row">
                    <span>补偿方式</span>
                    <select
                      value={templateDraft.params.side}
                      onChange={(event) => patchParams({
                        side: event.target.value as TemplateInput['params']['side'],
                      })}
                    >
                      <option value="auto">按内外自动</option>
                      <option value="outside">外补偿 +R</option>
                      <option value="inside">内补偿 -R</option>
                      <option value="on">不补偿</option>
                    </select>
                  </label>
                  <label className="form-row form-check">
                    <span>路径排序</span>
                    <input
                      type="checkbox"
                      checked={templateDraft.params.optimize}
                      onChange={(event) => patchParams({ optimize: event.target.checked })}
                    />
                  </label>
                  <label className="form-row form-check">
                    <span>微连接</span>
                    <input
                      type="checkbox"
                      checked={templateDraft.params.microJoint}
                      onChange={(event) => patchParams({ microJoint: event.target.checked })}
                    />
                  </label>
                  {templateDraft.params.microJoint && (
                    <>
                      <label className="form-row">
                        <span>桥宽 (mm)</span>
                        <input
                          type="number"
                          min={0}
                          step={0.5}
                          value={templateDraft.params.microJointWidth}
                          onChange={(event) => patchParams({
                            microJointWidth: Number(event.target.value),
                          })}
                        />
                      </label>
                      <label className="form-row">
                        <span>桥数量</span>
                        <input
                          type="number"
                          min={0}
                          max={20}
                          step={1}
                          value={templateDraft.params.microJointCount}
                          onChange={(event) => patchParams({
                            microJointCount: Number(event.target.value),
                          })}
                        />
                      </label>
                    </>
                  )}
                  <label className="form-row form-check">
                    <span>圆弧切入</span>
                    <input
                      type="checkbox"
                      checked={templateDraft.params.leadIn}
                      onChange={(event) => patchParams({ leadIn: event.target.checked })}
                    />
                  </label>
                  {templateDraft.params.leadIn && (
                    <label className="form-row">
                      <span>切入半径 (mm)</span>
                      <input
                        type="number"
                        min={0}
                        step={0.5}
                        value={templateDraft.params.leadRadius}
                        onChange={(event) => patchParams({
                          leadRadius: Number(event.target.value),
                        })}
                      />
                    </label>
                  )}
                  <label className="form-row form-row-wide">
                    <span>备注</span>
                    <input
                      type="text"
                      value={templateDraft.note}
                      placeholder="工艺说明"
                      onChange={(event) => setTemplateDraft((prev) => ({
                        ...prev, note: event.target.value,
                      }))}
                    />
                  </label>
                </div>

                <div className="tool-summary">
                  一键加载会把补偿/微连接参数与绑定刀具一起写入刀路面板；
                  模板未绑定刀具时只接管补偿参数。
                </div>

                <div className="form-actions tool-form-actions">
                  <button
                    type="button"
                    className="btn btn-accent"
                    onClick={() => void onSaveTemplate()}
                    disabled={saving || templateDraft.name.trim() === ''}
                  >
                    {saving ? '保存中…' : '保存'}
                  </button>
                  <button
                    type="button"
                    className="btn"
                    onClick={() => void onDeleteTemplate()}
                    disabled={templateId === null || saving}
                  >
                    {armed ? '确认删除？' : '删除'}
                  </button>
                  <button
                    type="button"
                    className="btn"
                    onClick={applyCurrent}
                    disabled={templateId === null || saving}
                  >
                    应用到刀路
                  </button>
                </div>
              </>
            )}
          </div>
        </div>

        <footer className="modal-foot">
          <span className="modal-hint">
            补偿量由后端根据刀具自动换算：铣刀取直径，钼丝取“丝径 + 2×放电间隙”。
          </span>
          <button type="button" className="btn" onClick={close}>关闭</button>
          <button type="button" className="btn btn-accent" onClick={applyCurrent} disabled={!canApply}>
            一键加载并关闭
          </button>
        </footer>
      </div>
    </div>
  );
}
