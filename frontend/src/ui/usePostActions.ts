/**
 * 后处理与导出（模块 7）的前端动作层。
 *
 * 三件事：
 *   1. 后处理列表缓存：一份 JSON 配置一台机床，内置只读、自定义可增删改；
 *   2. 导出：复用最近一次刀路请求（保证导出与预览一致），只追加后处理与批量选项；
 *   3. 结果回写：文件列表进 store，消息栏提示首个路径。
 *
 * 后端负责模板校验、占位符替换与 G/HPGL/DXF 渲染，前端只搬参数。
 */

import { useCallback } from 'react';

import {
  api,
  type ExportParams,
  type ExportResult,
  type PostKind,
  type PostProcessorInput,
  type PostProcessorRecord,
} from '@/net/rest';
import { useAppStore } from '@/state/app-store';
import { buildToolpathRequest } from './useToolpathActions';

/** 导出选项：format 不传时按后处理 kind 推导 */
export interface ExportOptions {
  format?: ExportParams['format'];
  processorId?: string;
  name?: string;
  flipY?: boolean;
  speed?: number;
  force?: number;
  allSheets?: boolean;
}

/** 后处理 kind -> 导出 format（format 只用于未指定 processorId 时回退内置模板） */
export function formatForKind(kind: PostKind): ExportParams['format'] {
  if (kind === 'dxf') return 'dxf';
  if (kind === 'hpgl') return 'plt';
  return 'nc';
}

/** 弹窗里保存用：去掉后端维护的 builtin/version */
export function toProcessorInput(record: PostProcessorRecord): PostProcessorInput {
  return {
    id: record.id,
    name: record.name,
    kind: record.kind,
    ext: record.ext,
    description: record.description,
    gcode: record.gcode,
    hpgl: record.hpgl,
    dxf: record.dxf,
  };
}

export function usePostActions() {
  /** 拉取全部后处理；坏配置由后端跳过，前端不做二次校验 */
  const refresh = useCallback(async () => {
    const store = useAppStore.getState();
    try {
      const list = await api.posts.list();
      store.setPostProcessors(list.processors);
    } catch (error) {
      store.setMessage(`后处理加载失败：${(error as Error).message}`);
    }
  }, []);

  const open = useCallback(() => {
    useAppStore.getState().setPostOpen(true);
    void refresh();
  }, [refresh]);

  const close = useCallback(() => {
    useAppStore.getState().setPostOpen(false);
  }, []);

  /** id 为 null 表示新建；内置配置由后端拒绝修改，前端只做禁用提示 */
  const save = useCallback(async (id: string | null,
                                    payload: PostProcessorInput): Promise<PostProcessorRecord | null> => {
    const store = useAppStore.getState();
    try {
      const saved = id === null
        ? await api.posts.create(payload)
        : await api.posts.update(id, payload);
      await refresh();
      store.setMessage(`后处理已保存：${saved.name}`);
      return saved;
    } catch (error) {
      store.setMessage(`后处理保存失败：${(error as Error).message}`);
      return null;
    }
  }, [refresh]);

  const remove = useCallback(async (id: string): Promise<boolean> => {
    const store = useAppStore.getState();
    try {
      await api.posts.remove(id);
      await refresh();
      store.setMessage('后处理已删除');
      return true;
    } catch (error) {
      store.setMessage(`后处理删除失败：${(error as Error).message}`);
      return false;
    }
  }, [refresh]);

  /**
   * 导出：有最近一次刀路请求就复用，否则按当前视图组装。
   * 批量多板时清空 ids —— 否则其它板材会因“排料轮廓不存在”被拒。
   */
  const exportNow = useCallback(async (options: ExportOptions): Promise<ExportResult | null> => {
    const store = useAppStore.getState();
    const request = store.toolpathRequest ?? buildToolpathRequest();
    const processor = store.postProcessors.find((item) => item.id === options.processorId);
    const payload: ExportParams = {
      ...request,
      ...(options.allSheets ? { ids: [] } : {}),
      format: options.format ?? (processor ? formatForKind(processor.kind) : 'nc'),
      processorId: options.processorId,
      name: options.name,
      flipY: options.flipY,
      speed: options.speed,
      force: options.force,
      allSheets: options.allSheets,
      // 打开工单后导出的文件自动归档到该工单名下
      workOrderId: store.activeWorkOrder?.id,
    };
    try {
      const result = await api.exportFile(payload);
      store.setLastExport(result);
      store.setStats(result.stats);
      store.setExportPreset({
        flipY: Boolean(options.flipY),
        speed: options.speed ?? 30,
        force: options.force ?? 20,
      });
      const count = result.files.length;
      store.setMessage(count > 1
        ? `已导出 ${count} 个文件：${result.files[0].path} 等`
        : `已导出：${result.path}`);
      return result;
    } catch (error) {
      store.setMessage(`导出失败：${(error as Error).message}`);
      return null;
    }
  }, []);

  /** 导出目录（Tauri 内无文件管理器插件，只回显路径供复制） */
  const showExportDir = useCallback(async () => {
    const store = useAppStore.getState();
    try {
      const result = await api.exportDir();
      store.setMessage(`导出目录：${result.dir}`);
    } catch (error) {
      store.setMessage(`导出目录读取失败：${(error as Error).message}`);
    }
  }, []);

  return { refresh, open, close, save, remove, exportNow, showExportDir };
}
