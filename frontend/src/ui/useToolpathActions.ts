/**
 * 刀路生成/取消（模块 5）。
 *
 * 收口三件事：
 *   1. 把 store 里的表单参数 + 当前选中集 + 起割点组装成后端请求；
 *   2. 记录最近一次请求，导出接口直接复用，保证"导出与预览一致"；
 *   3. 取消只发请求，状态由 TASK_FAILED(cancelled) 帧回写。
 *
 * 注意：工具栏生成、面板重新生成、导出都走 buildToolpathRequest，
 * 不允许各自拼参数 —— 否则切换数据源（图纸/排料板）后容易导错对象。
 */

import { useCallback } from 'react';

import { api, type ToolpathParams } from '@/net/rest';
import { useAppStore } from '@/state/app-store';

/** 当前视图对应的刀路数据源：排料板整板 or 图纸轮廓 */
function currentSource(): 'doc' | 'layout' {
  const state = useAppStore.getState();
  if (state.viewMode === 'layout') return 'layout';
  // 预览态重新生成时沿用上次来源，避免把排料板刀路误当成图纸刀路
  if (state.viewMode === 'preview' && state.toolpathPreview) {
    return state.toolpathPreview.source;
  }
  return 'doc';
}

/** 用当前视图 + 表单参数组装完整请求（生成与导出共用） */
export function buildToolpathRequest(): ToolpathParams {
  const state = useAppStore.getState();
  // 预览态下选中集可能含抬刀/起割点图层（辅助线），这些 id 不是真实轮廓，
  // 直接传会让后端报“轮廓不存在”，所以按本次刀路明细里的轮廓 id 过滤一遍。
  const known = state.viewMode === 'preview' && state.toolpathPreview
    ? new Set(state.toolpathPreview.contours.map((contour) => contour.id))
    : null;
  const ids = known ? state.selection.filter((id) => known.has(id)) : state.selection;
  return {
    ...state.toolpathParams,
    projectId: state.projectId ?? undefined,
    source: currentSource(),
    sheetIndex: 0,
    // 为空表示整板/全部轮廓；后端按数据源自行展开
    ids,
    startPoints: state.startPoints.map((point) => ({ x: point.x, y: point.y })),
  };
}

/** 是否有进行中的刀路任务（同一时间只允许一个，避免结果帧互相覆盖） */
function runningTask() {
  return useAppStore.getState().tasks.find((task) => task.kind === 'toolpath'
    && (task.status === 'queued' || task.status === 'running'));
}

export function useToolpathActions() {
  const generate = useCallback(async () => {
    const store = useAppStore.getState();
    if (runningTask()) {
      store.setMessage('刀路计算进行中，请等待完成或先取消');
      return;
    }
    const request = buildToolpathRequest();
    // 先记请求再提交：失败时清掉，避免导出拿到未生效的参数
    store.setToolpathRequest(request);
    try {
      const result = await api.toolpath(request);
      store.setMessage(`刀路任务已提交：${result.taskId}`);
    } catch (error) {
      store.setToolpathRequest(null);
      store.setMessage(`刀路提交失败：${(error as Error).message}`);
    }
  }, []);

  const cancel = useCallback(async () => {
    const store = useAppStore.getState();
    const task = runningTask();
    if (!task) {
      store.setMessage('当前没有进行中的刀路任务');
      return;
    }
    try {
      await api.cancelTask(task.taskId);
      store.setMessage('已请求取消刀路，等当前步骤结束即停');
    } catch (error) {
      store.setMessage(`取消失败：${(error as Error).message}`);
    }
  }, []);

  return { generate, cancel };
}
