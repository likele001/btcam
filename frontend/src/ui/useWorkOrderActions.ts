/**
 * 工单存档（模块 8）的前端动作层。
 *
 * 后端负责快照采集/恢复（图形、板材、摆放、刀具引用、刀路参数、后处理选择），
 * 前端只做三件事：
 *   1. 列表与详情的搬运；
 *   2. 保存时把当前刀路/后处理参数一并提交；
 *   3. 打开后把刀路参数与后处理回填到 store（图形由 WS 全量刷新）。
 *
 * 打开工单后 activeWorkOrder 会被置位，之后每次导出都会把文件路径
 * 登记到该工单名下（见 usePostActions.exportNow）。
 */

import { useCallback } from 'react';

import { api, type WorkOrderDetail, type WorkOrderListParams } from '@/net/rest';
import { DEFAULT_TOOLPATH_PARAMS, useAppStore } from '@/state/app-store';

/** 当前导出面板的后处理选择：优先沿用已打开工单的存档 */
function currentPost(): WorkOrderDetail['post'] {
  const store = useAppStore.getState();
  const preset = store.exportPreset;
  const last = store.lastExport;
  const saved = store.activeWorkOrder?.post ?? null;
  const processorId = saved?.processorId ?? last?.processor ?? null;
  const processor = store.postProcessors.find((item) => item.id === processorId);
  const format = processor
    ? (processor.kind === 'dxf' ? 'dxf' : processor.kind === 'hpgl' ? 'plt' : 'nc')
    : saved?.format ?? (last?.format as WorkOrderDetail['post']['format']) ?? 'nc';
  return {
    processorId,
    format,
    name: saved?.name ?? 'BT_CAM',
    flipY: preset.flipY,
    speed: preset.speed,
    force: preset.force,
  };
}

export function useWorkOrderActions() {
  const refresh = useCallback(async (params: WorkOrderListParams = {}) => {
    const store = useAppStore.getState();
    try {
      const result = await api.workorders.list(params);
      store.setWorkOrders(result.orders);
      return result.orders;
    } catch (error) {
      store.setMessage(`工单列表加载失败：${(error as Error).message}`);
      return [];
    }
  }, []);

  const open = useCallback(async () => {
    useAppStore.getState().setWorkOrderOpen(true);
    await refresh();
  }, [refresh]);

  const close = useCallback(() => {
    useAppStore.getState().setWorkOrderOpen(false);
  }, []);

  /** 保存当前工程为新工单；toolpath / post 一并固化 */
  const saveNew = useCallback(async (name: string, code: string,
                                     note: string): Promise<WorkOrderDetail | null> => {
    const store = useAppStore.getState();
    try {
      const detail = await api.workorders.save({
        projectId: store.projectId ?? undefined,
        name,
        code,
        note,
        toolpath: store.toolpathParams,
        post: currentPost(),
      });
      store.setActiveWorkOrder({ id: detail.id, name: detail.name, post: detail.post });
      await refresh();
      store.setMessage(`工单已保存：#${detail.id}「${detail.name}」`);
      return detail;
    } catch (error) {
      store.setMessage(`工单保存失败：${(error as Error).message}`);
      return null;
    }
  }, [refresh]);

  /** 只改名称/编号/备注，不动快照 */
  const updateMeta = useCallback(async (id: number, name: string, code: string,
                                        note: string): Promise<WorkOrderDetail | null> => {
    const store = useAppStore.getState();
    try {
      const detail = await api.workorders.update(id, {
        projectId: store.projectId ?? undefined, name, code, note,
      });
      if (store.activeWorkOrder?.id === id) {
        store.setActiveWorkOrder({ id: detail.id, name: detail.name, post: detail.post });
      }
      await refresh();
      store.setMessage(`工单信息已更新：#${detail.id}「${detail.name}」`);
      return detail;
    } catch (error) {
      store.setMessage(`工单修改失败：${(error as Error).message}`);
      return null;
    }
  }, [refresh]);

  /** 用当前工程重新拍快照写回已有工单（改完图/排料后的“更新工单”） */
  const recapture = useCallback(async (id: number, name: string, code: string,
                                       note: string): Promise<WorkOrderDetail | null> => {
    const store = useAppStore.getState();
    try {
      const detail = await api.workorders.update(id, {
        projectId: store.projectId ?? undefined,
        name, code, note,
        toolpath: store.toolpathParams,
        post: currentPost(),
        recapture: true,
      });
      store.setActiveWorkOrder({ id: detail.id, name: detail.name, post: detail.post });
      await refresh();
      store.setMessage(`工单快照已更新：#${detail.id}「${detail.name}」`);
      return detail;
    } catch (error) {
      store.setMessage(`工单快照更新失败：${(error as Error).message}`);
      return null;
    }
  }, [refresh]);

  /** 打开历史工单：图形由 WS 广播刷新，这里回填刀路/后处理参数 */
  const openOrder = useCallback(async (id: number): Promise<WorkOrderDetail | null> => {
    const store = useAppStore.getState();
    try {
      const detail = await api.workorders.open(id);
      store.setToolpathParams({
        ...DEFAULT_TOOLPATH_PARAMS,
        ...detail.toolpath,
        toolId: detail.toolpath.toolId ?? null,
        templateId: detail.toolpath.templateId ?? null,
      });
      store.setToolpathRequest(null);
      store.setActiveWorkOrder({ id: detail.id, name: detail.name, post: detail.post });
      store.setExportPreset({
        flipY: detail.post.flipY ?? false,
        speed: detail.post.speed ?? 30,
        force: detail.post.force ?? 20,
      });
      store.setMessage(detail.missing.length > 0
        ? `已打开工单「${detail.name}」；${detail.missing.join('；')}`
        : `已打开工单「${detail.name}」：${detail.stats.partCount} 个零件`);
      await refresh();
      return detail;
    } catch (error) {
      store.setMessage(`打开工单失败：${(error as Error).message}`);
      return null;
    }
  }, [refresh]);

  const duplicate = useCallback(async (id: number, name = ''): Promise<WorkOrderDetail | null> => {
    const store = useAppStore.getState();
    try {
      const detail = await api.workorders.duplicate(id, name);
      await refresh();
      store.setMessage(`已另存工单：#${detail.id}「${detail.name}」`);
      return detail;
    } catch (error) {
      store.setMessage(`工单另存失败：${(error as Error).message}`);
      return null;
    }
  }, [refresh]);

  const remove = useCallback(async (id: number): Promise<boolean> => {
    const store = useAppStore.getState();
    try {
      await api.workorders.remove(id);
      if (store.activeWorkOrder?.id === id) store.setActiveWorkOrder(null);
      await refresh();
      store.setMessage('工单已删除');
      return true;
    } catch (error) {
      store.setMessage(`工单删除失败：${(error as Error).message}`);
      return false;
    }
  }, [refresh]);

  return { refresh, open, close, saveNew, updateMeta, recapture, openOrder, duplicate, remove };
}
