/**
 * 排料写操作集合（模块 3）。
 *
 * 画布拖拽、左侧面板按钮、工具栏入口都走这里，保证三条交互路径只有一份收口逻辑：
 *   - 逐个提交：后端每写一次就广播一帧，串行可避免多零件操作的广播互相覆盖；
 *   - 失败回弹：越界/间距不足时后端不落库，onReject 由画布用来撤掉拖拽偏移；
 *   - 补齐同步：无论成败都拉一次 LAYOUT_SYNC，防止丢帧让画布停在"移动了但没落库"的假象上。
 *
 * 这里不判断几何合法性 —— 判定全在后端 LayoutService。
 */

import { useCallback } from 'react';

import { api, type LayoutActionResult, type LayoutNestParams, type SheetParams } from '@/net/rest';
import { requestLayoutSync } from '@/net/session';
import { selectedLayoutParts, useAppStore } from '@/state/app-store';
import type { LayoutPartInfo } from '@/types/geometry';

/** 当前工程 id；后端不传时用它自己的活动工程兜底 */
const projectId = (): number | undefined => useAppStore.getState().projectId ?? undefined;

export interface LayoutActions {
  /** 进入排料模式（首次会做一次初始行排） */
  open: () => Promise<void>;
  /** 保存板材参数 */
  applySheet: (sheet: SheetParams) => Promise<void>;
  /** 拖动结束后提交新位置 */
  movePart: (partId: number, tx: number, ty: number) => Promise<void>;
  /** 选中零件各复制若干份（排料板增件） */
  duplicateSelected: (count: number) => Promise<void>;
  /** 选中零件整体旋转（±角度） */
  rotateSelected: (delta: number) => Promise<void>;
  /** 选中零件整体锁定/解锁 */
  lockSelected: () => Promise<void>;
  /** 选中零件整体锁定/解除木纹方向 */
  fixedRotSelected: () => Promise<void>;
  /** 提交自动排料任务（异步，结果走 LAYOUT_STATE 广播） */
  nest: (engine: LayoutNestParams['engine'], rotation: boolean) => Promise<void>;
  /** 取消进行中的自动排料任务 */
  cancelNest: () => Promise<void>;
  /** 清空摆放重新行排 */
  reset: () => Promise<void>;
}

/**
 * @param onReject 后端拒绝或请求异常时的回调（画布传 cancelDrag 做回弹，面板可不传）
 */
export function useLayoutActions(onReject?: () => void): LayoutActions {
  const run = useCallback(
    async (action: () => Promise<LayoutActionResult>, okMessage: string) => {
      const store = useAppStore.getState();
      try {
        const result = await action();
        if (result.ok) {
          store.setMessage(okMessage);
        } else {
          onReject?.();
          store.setMessage(`已还原：${result.reason}`);
        }
      } catch (error) {
        onReject?.();
        store.setMessage(`排料操作失败：${(error as Error).message}`);
      } finally {
        requestLayoutSync();
      }
    },
    [onReject],
  );

  /** 对选中零件逐个执行同一动作，遇到拒绝就停（后面的零件位置依赖前一个的结果） */
  const eachSelected = useCallback(
    async (label: string, action: (part: LayoutPartInfo) => Promise<LayoutActionResult>) => {
      const store = useAppStore.getState();
      const parts = selectedLayoutParts(store);
      if (parts.length === 0) {
        store.setMessage(`先选中零件再${label}`);
        return;
      }
      await run(async () => {
        let last: LayoutActionResult = { ok: true, reason: '' };
        for (const part of parts) {
          last = await action(part);
          if (!last.ok) break;
        }
        return last;
      }, `${label}完成（${parts.length} 个零件）`);
    },
    [run],
  );

  const open = useCallback(
    () => run(() => api.layout.open(projectId()), '已进入排料模式'),
    [run],
  );

  const applySheet = useCallback(
    (sheet: SheetParams) => run(
      () => api.layout.sheet({ ...sheet, projectId: projectId() }),
      '板材参数已保存',
    ),
    [run],
  );

  const movePart = useCallback(
    (partId: number, tx: number, ty: number) => run(
      () => api.layout.place({ projectId: projectId(), partId, tx, ty }),
      '摆放已更新',
    ),
    [run],
  );

  /**
   * 复制选中零件：逐个提交（每个复制件的位置取决于前一个已落库的结果）。
   * 放不下的复制件由后端停靠到板外上方，这里把件数与停靠数一并告诉用户。
   */
  const duplicateSelected = useCallback(async (count: number) => {
    const store = useAppStore.getState();
    const parts = selectedLayoutParts(store);
    if (parts.length === 0) {
      store.setMessage('先选中零件再复制');
      return;
    }
    const copies = Math.min(99, Math.max(1, Math.floor(count)));
    let copied = 0;
    let parked = 0;
    try {
      for (const part of parts) {
        const result = await api.layout.duplicate({
          projectId: projectId(), partId: part.id, count: copies,
        });
        if (!result.ok) {
          store.setMessage(`复制失败：${result.reason}`);
          return;
        }
        copied += result.copied;
        parked += result.parked;
      }
      store.setMessage(parked > 0
        ? `已复制 ${copied} 件，${parked} 件放不下已停靠在板外上方，拖进来即可`
        : `已复制 ${copied} 件`);
    } catch (error) {
      store.setMessage(`复制失败：${(error as Error).message}`);
    } finally {
      requestLayoutSync();
    }
  }, []);

  const rotateSelected = useCallback(
    (delta: number) => eachSelected('旋转', (part) =>
      api.layout.rotate({ projectId: projectId(), partId: part.id, rot: part.rot + delta })),
    [eachSelected],
  );

  const lockSelected = useCallback(() => {
    const parts = selectedLayoutParts(useAppStore.getState());
    if (parts.length === 0) {
      useAppStore.getState().setMessage('先选中零件再锁定');
      return Promise.resolve();
    }
    // 只要还有未锁的零件就整体锁上，全部已锁则整体解锁
    const locked = !parts.every((part) => part.locked);
    return eachSelected(locked ? '锁定' : '解锁', (part) =>
      api.layout.lock({ projectId: projectId(), partId: part.id, locked }));
  }, [eachSelected]);

  const reset = useCallback(
    () => run(() => api.layout.reset(projectId()), '已重新排板'),
    [run],
  );

  const fixedRotSelected = useCallback(() => {
    const parts = selectedLayoutParts(useAppStore.getState());
    if (parts.length === 0) {
      useAppStore.getState().setMessage('先选中零件再锁定木纹方向');
      return Promise.resolve();
    }
    // 只要还有未锁的零件就整体锁上，全部已锁则整体解除
    const fixedRot = !parts.every((part) => part.fixedRot);
    return eachSelected(fixedRot ? '锁定木纹方向' : '解除木纹锁定', (part) =>
      api.layout.fixedRot({ projectId: projectId(), partId: part.id, fixedRot }));
  }, [eachSelected]);

  const nest = useCallback(async (
    engine: LayoutNestParams['engine'],
    rotation: boolean,
  ) => {
    const store = useAppStore.getState();
    // 同一个排料板只允许一个自动排料在跑，否则两次任务会互相覆写摆放
    const running = store.tasks.some((task) => task.kind === 'layout_nest'
      && (task.status === 'queued' || task.status === 'running'));
    if (running) {
      store.setMessage('自动排料正在进行，请等当前任务完成');
      return;
    }
    try {
      const result = await api.layout.nest({ projectId: projectId(), engine, rotation });
      store.setMessage(`自动排料已提交：${result.taskId}`);
    } catch (error) {
      store.setMessage(`自动排料提交失败：${(error as Error).message}`);
    }
  }, []);

  const cancelNest = useCallback(async () => {
    const store = useAppStore.getState();
    const task = store.tasks.find((item) => item.kind === 'layout_nest'
      && (item.status === 'queued' || item.status === 'running'));
    if (!task) {
      store.setMessage('当前没有进行中的自动排料任务');
      return;
    }
    try {
      await api.cancelTask(task.taskId);
      store.setMessage('已请求取消自动排料，等当前步骤结束即停');
    } catch (error) {
      store.setMessage(`取消失败：${(error as Error).message}`);
    }
  }, []);

  return {
    open, applySheet, movePart, duplicateSelected, rotateSelected, lockSelected, fixedRotSelected,
    nest, cancelNest, reset,
  };
}
