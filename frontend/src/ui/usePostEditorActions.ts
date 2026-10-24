/**
 * 后处理可视化编辑器的动作层（模块 11）。
 *
 * 校验、预览、落盘全在后端：
 *   - validate 只读，返回逐字段错误 + 工艺告警 + 变量使用情况；
 *   - preview 用示例方框走真实渲染器，和导出同一套代码；
 *   - save/remove 写 %LOCALAPPDATA%/BtCAM/post/*.json（registry 负责）。
 *
 * 变量字典是纯静态数据，进程内缓存一次即可，不必每次开窗都拉。
 */

import { useCallback } from 'react';

import {
  api,
  type PostPreviewOptions,
  type PostPreviewResult,
  type PostProcessorRecord,
  type PostValidateResult,
  type PostVariableDictionary,
} from '@/net/rest';
import { useAppStore } from '@/state/app-store';
import { toProcessorInput } from './usePostActions';

/** 变量字典缓存：内置变量不会随工程变化，进程内一份足够 */
let variableCache: PostVariableDictionary | null = null;

/** 后端错误文本形如 `400 Bad Request {"detail":"..."}`：抠出 detail 更像人话 */
function humanError(error: unknown): string {
  const text = (error as Error).message ?? String(error);
  const match = text.match(/\{"detail":"([^"]*)"/);
  return match ? match[1] : text;
}

export function usePostEditorActions() {
  const open = useCallback((target = '') => {
    const store = useAppStore.getState();
    // 编辑器从导出弹窗打开：先收起旧弹窗，避免两层遮罩叠在一起
    store.setPostOpen(false);
    store.setPostEditorOpen(true, target);
  }, []);

  const close = useCallback(() => {
    useAppStore.getState().setPostEditorOpen(false);
  }, []);

  /** 变量字典：占位符说明 + 各后处理类别的指令槽位 */
  const loadVariables = useCallback(async (): Promise<PostVariableDictionary | null> => {
    if (variableCache) return variableCache;
    try {
      variableCache = await api.postEditor.variables();
      return variableCache;
    } catch (error) {
      useAppStore.getState().setMessage(`变量字典加载失败：${humanError(error)}`);
      return null;
    }
  }, []);

  /** 校验草稿：不落盘，失败只回错误明细，不抛异常给界面 */
  const validateDraft = useCallback(async (
    draft: PostProcessorRecord,
    processorId = '',
  ): Promise<PostValidateResult | null> => {
    try {
      return await api.postEditor.validate(draft, processorId || undefined);
    } catch (error) {
      useAppStore.getState().setMessage(`模板校验失败：${humanError(error)}`);
      return null;
    }
  }, []);

  /** 预览：校验不通过时后端返回空文本，这里原样交给界面展示原因 */
  const previewDraft = useCallback(async (
    draft: PostProcessorRecord,
    options: PostPreviewOptions,
  ): Promise<PostPreviewResult | null> => {
    try {
      return await api.postEditor.preview(draft, options);
    } catch (error) {
      useAppStore.getState().setMessage(`模板预览失败：${humanError(error)}`);
      return null;
    }
  }, []);

  /** 保存：isNew=true 走新建，否则覆盖同 id 的自定义模板 */
  const save = useCallback(async (
    draft: PostProcessorRecord,
    isNew: boolean,
  ): Promise<PostProcessorRecord | null> => {
    const store = useAppStore.getState();
    try {
      const payload = toProcessorInput(draft);
      const saved = isNew
        ? await api.posts.create(payload)
        : await api.posts.update(draft.id, payload);
      // 重新拉一次列表：内置/自定义排序、版本号都以磁盘为准
      const list = await api.posts.list();
      store.setPostProcessors(list.processors);
      store.setMessage(`自定义机床模板已保存：${saved.name}（${saved.id}）`);
      return saved;
    } catch (error) {
      store.setMessage(`模板保存失败：${humanError(error)}`);
      return null;
    }
  }, []);

  const remove = useCallback(async (id: string): Promise<boolean> => {
    const store = useAppStore.getState();
    try {
      await api.posts.remove(id);
      const list = await api.posts.list();
      store.setPostProcessors(list.processors);
      store.setMessage('自定义机床模板已删除');
      return true;
    } catch (error) {
      store.setMessage(`模板删除失败：${humanError(error)}`);
      return false;
    }
  }, []);

  return { open, close, loadVariables, validateDraft, previewDraft, save, remove };
}
