/**
 * 刀具库 / 工艺模板（模块 6）的前端动作层。
 *
 * 三件事：
 *   1. 列表缓存：进入应用拉一次，增删改后刷新，面板与弹窗共用 store；
 *   2. 一键加载：模板 -> 补偿参数 + 绑定的刀具；刀具 -> 补偿直径/进给/分层/抬刀/方向；
 *   3. 增删改查：只搬参数，校验与换算全部在后端，前端不推算补偿量。
 *
 * 绑定优先级与后端一致：刀具 > 模板 > 表单值。
 * 因此面板上“已被刀具/模板接管”的字段要禁用，避免改了却不生效。
 */

import { useCallback } from 'react';

import {
  api,
  type TemplateInput,
  type TemplateRecord,
  type ToolpathParams,
  type ToolInput,
  type ToolRecord,
} from '@/net/rest';
import { useAppStore } from '@/state/app-store';

/** 由刀具接管的刀路参数：绑定刀具后这些字段只读 */
export const TOOL_OWNED_KEYS: Array<keyof ToolpathParams> = [
  'toolDiameter', 'feed', 'plungeFeed', 'spindle', 'passDepth', 'safeZ', 'liftZ', 'direction',
];

/** 由工艺模板接管的补偿参数：绑定模板后这些字段只读 */
export const TEMPLATE_OWNED_KEYS: Array<keyof ToolpathParams> = [
  'cutType', 'side', 'optimize', 'microJoint', 'microJointWidth', 'microJointCount',
  'leadIn', 'leadRadius',
];

/** 刀具记录 -> 刀路参数（补偿量用后端算好的等效直径） */
export function toolToParams(tool: ToolRecord): Partial<ToolpathParams> {
  return {
    toolId: tool.id,
    toolDiameter: tool.effectiveDiameter,
    feed: tool.feed,
    plungeFeed: tool.plungeFeed,
    spindle: tool.spindle,
    passDepth: tool.passDepth,
    safeZ: tool.safeZ,
    liftZ: tool.liftZ,
    direction: tool.direction,
  };
}

/** 模板记录 -> 刀路参数：补偿参数 + 模板绑定的刀具 */
export function templateToParams(template: TemplateRecord): Partial<ToolpathParams> {
  const patch: Partial<ToolpathParams> = {
    templateId: template.id,
    ...template.params,
    toolId: template.toolId,
  };
  if (template.tool) Object.assign(patch, toolToParams(template.tool));
  return patch;
}

export function useToolLibraryActions() {
  /** 拉取刀具与模板；两个接口互不依赖，并行发 */
  const refresh = useCallback(async () => {
    const store = useAppStore.getState();
    try {
      const projectId = store.projectId ?? undefined;
      const [toolList, templateList] = await Promise.all([
        api.tools.list(projectId),
        api.templates.list(projectId),
      ]);
      store.setTools(toolList.tools);
      store.setTemplates(templateList.templates);
    } catch (error) {
      store.setMessage(`刀具库加载失败：${(error as Error).message}`);
    }
  }, []);

  const open = useCallback(() => {
    useAppStore.getState().setToolLibraryOpen(true);
    void refresh();
  }, [refresh]);

  const close = useCallback(() => {
    useAppStore.getState().setToolLibraryOpen(false);
  }, []);

  /** 选择刀具：null = 解除绑定，保留当前参数供手动编辑 */
  const selectTool = useCallback((toolId: number | null) => {
    const store = useAppStore.getState();
    if (toolId === null) {
      store.setToolpathParams({ toolId: null });
      store.setMessage('已解除刀具绑定，刀具参数可手动编辑');
      return;
    }
    const tool = store.tools.find((item) => item.id === toolId);
    if (!tool) {
      store.setMessage(`刀具 #${toolId} 不存在，请刷新刀具库`);
      return;
    }
    store.setToolpathParams(toolToParams(tool));
    store.setMessage(`已加载刀具：${tool.name}（补偿 ⌀${tool.effectiveDiameter}mm）`);
  }, []);

  /** 选择工艺模板：null = 解除模板，补偿参数转为手动 */
  const selectTemplate = useCallback((templateId: number | null) => {
    const store = useAppStore.getState();
    if (templateId === null) {
      store.setToolpathParams({ templateId: null });
      store.setMessage('已解除工艺模板，补偿参数可手动编辑');
      return;
    }
    const template = store.templates.find((item) => item.id === templateId);
    if (!template) {
      store.setMessage(`工艺模板 #${templateId} 不存在，请刷新刀具库`);
      return;
    }
    store.setToolpathParams(templateToParams(template));
    store.setMessage(`已加载模板：${template.name}`);
  }, []);

  const saveTool = useCallback(async (id: number | null,
                                    payload: ToolInput): Promise<ToolRecord | null> => {
    const store = useAppStore.getState();
    const body: ToolInput = { ...payload, projectId: store.projectId ?? undefined };
    try {
      const saved = id === null
        ? await api.tools.create(body)
        : await api.tools.update(id, body);
      await refresh();
      // 当前正在用这把刀：同步刷新刀路面板里的只读值
      if (store.toolpathParams.toolId === saved.id) {
        store.setToolpathParams(toolToParams(saved));
      }
      store.setMessage(`刀具已保存：${saved.name}`);
      return saved;
    } catch (error) {
      store.setMessage(`刀具保存失败：${(error as Error).message}`);
      return null;
    }
  }, [refresh]);

  const deleteTool = useCallback(async (id: number): Promise<boolean> => {
    const store = useAppStore.getState();
    try {
      await api.tools.remove(id, store.projectId ?? undefined);
      if (store.toolpathParams.toolId === id) {
        store.setToolpathParams({ toolId: null });
      }
      await refresh();
      store.setMessage('刀具已删除');
      return true;
    } catch (error) {
      // 被模板引用时后端返回 400 + 原因，原样提示
      store.setMessage(`刀具删除失败：${(error as Error).message}`);
      return false;
    }
  }, [refresh]);

  const saveTemplate = useCallback(async (id: number | null,
                                         payload: TemplateInput): Promise<TemplateRecord | null> => {
    const store = useAppStore.getState();
    const body: TemplateInput = { ...payload, projectId: store.projectId ?? undefined };
    try {
      const saved = id === null
        ? await api.templates.create(body)
        : await api.templates.update(id, body);
      await refresh();
      if (store.toolpathParams.templateId === saved.id) {
        store.setToolpathParams(templateToParams(saved));
      }
      store.setMessage(`工艺模板已保存：${saved.name}`);
      return saved;
    } catch (error) {
      store.setMessage(`模板保存失败：${(error as Error).message}`);
      return null;
    }
  }, [refresh]);

  const deleteTemplate = useCallback(async (id: number): Promise<boolean> => {
    const store = useAppStore.getState();
    try {
      await api.templates.remove(id, store.projectId ?? undefined);
      if (store.toolpathParams.templateId === id) {
        store.setToolpathParams({ templateId: null });
      }
      await refresh();
      store.setMessage('工艺模板已删除');
      return true;
    } catch (error) {
      store.setMessage(`模板删除失败：${(error as Error).message}`);
      return false;
    }
  }, [refresh]);

  return {
    refresh,
    open,
    close,
    selectTool,
    selectTemplate,
    saveTool,
    deleteTool,
    saveTemplate,
    deleteTemplate,
  };
}
