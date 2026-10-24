/**
 * 余料库动作层（模块 10）。
 *
 * 这一层只做"搬运 + 提示"，所有几何判断都在后端：
 *   - 剩余区域怎么切、面积多大、轮廓长什么样，全部由 pyclipper 算；
 *   - 哪块区域能排得下零件，由 LayoutService._check 判定；
 *   - 前端只记住"用户勾了哪几个 index"，保存时把 index 交回去。
 *
 * 换板（应用/解绑余料）成功后后端已经广播了 LAYOUT_STATE，
 * 这里再补一次 requestLayoutSync，防止瞬时丢帧导致画布还在用旧边界。
 */

import { useCallback } from 'react';

import {
  api,
  type ScrapInput,
  type ScrapListResult,
  type ScrapRemainingResult,
  type ScrapSaveResult,
} from '@/net/rest';
import { requestLayoutSync } from '@/net/session';
import { useAppStore } from '@/state/app-store';

/** 当前工程 id；后端不传时用它自己的活动工程兜底 */
const projectId = (): number | undefined => useAppStore.getState().projectId ?? undefined;

/** 后端错误文本形如 `400 Bad Request {"detail":"..."}`：抠出 detail 更像人话 */
function humanError(error: unknown): string {
  const text = (error as Error).message ?? String(error);
  const match = text.match(/\{"detail":"([^"]*)"/);
  return match ? match[1] : text;
}

/** 剩余区域默认最小面积（mm²）：与后端 DEFAULT_MIN_AREA 对齐 */
export const DEFAULT_MIN_AREA = 10000;

export function useScrapActions() {
  const open = useCallback(() => {
    useAppStore.getState().setScrapOpen(true);
  }, []);

  const close = useCallback(() => {
    useAppStore.getState().setScrapOpen(false);
  }, []);

  /** 余料库列表（含当前板材正在用的那块） */
  const loadList = useCallback(async (keyword = ''): Promise<ScrapListResult | null> => {
    const store = useAppStore.getState();
    try {
      return await api.scrap.list({ keyword, limit: 300, projectId: projectId() });
    } catch (error) {
      store.setMessage(`余料库读取失败：${humanError(error)}`);
      return null;
    }
  }, []);

  /** 算当前板材的剩余区域（预览，不落库） */
  const loadRemaining = useCallback(async (
    minArea = DEFAULT_MIN_AREA,
  ): Promise<ScrapRemainingResult | null> => {
    const store = useAppStore.getState();
    try {
      return await api.scrap.remaining({ projectId: projectId(), minArea });
    } catch (error) {
      store.setMessage(`剩余区域计算失败：${humanError(error)}`);
      return null;
    }
  }, []);

  /** 保存勾选的剩余区域：只传 index，几何以后端重算结果为准 */
  const saveRegions = useCallback(async (
    indices: number[],
    options: {
      minArea?: number;
      namePrefix?: string;
      material?: string;
      thickness?: number;
      note?: string;
    } = {},
  ): Promise<ScrapSaveResult | null> => {
    const store = useAppStore.getState();
    if (indices.length === 0) {
      store.setMessage('请先勾选要保存的余料区域');
      return null;
    }
    try {
      const result = await api.scrap.save({
        projectId: projectId(),
        indices,
        minArea: options.minArea ?? DEFAULT_MIN_AREA,
        namePrefix: options.namePrefix ?? '',
        material: options.material ?? '',
        thickness: options.thickness ?? 0,
        note: options.note ?? '',
      });
      store.setMessage(`已保存 ${result.count} 块余料到余料库`);
      return result;
    } catch (error) {
      store.setMessage(`余料保存失败：${humanError(error)}`);
      return null;
    }
  }, []);

  /** 把余料设为当前板材：轮廓即边界，之后照旧拖拽/自动排料 */
  const applyToSheet = useCallback(async (id: number): Promise<boolean> => {
    const store = useAppStore.getState();
    try {
      const result = await api.scrap.apply(id, { projectId: projectId() });
      store.setMessage(
        `已换成余料「${result.scrap.name}」`
        + `（${result.scrap.width.toFixed(0)}×${result.scrap.height.toFixed(0)}mm，`
        + '放不下的零件停靠在板外）',
      );
      return true;
    } catch (error) {
      store.setMessage(`余料换板失败：${humanError(error)}`);
      return false;
    } finally {
      requestLayoutSync();
    }
  }, []);

  /** 解绑余料，退回规则矩形板（尺寸沿用面板当前值） */
  const clearSheet = useCallback(async (): Promise<boolean> => {
    const store = useAppStore.getState();
    try {
      const result = await api.scrap.clear({ projectId: projectId() });
      store.setMessage(
        `已退回整板：${result.sheet.width.toFixed(0)}×${result.sheet.height.toFixed(0)}mm`,
      );
      return true;
    } catch (error) {
      store.setMessage(`解绑余料失败：${humanError(error)}`);
      return false;
    } finally {
      requestLayoutSync();
    }
  }, []);

  /** 手工建一块余料（只给宽高即可，后端生成矩形轮廓） */
  const create = useCallback(async (payload: ScrapInput): Promise<boolean> => {
    const store = useAppStore.getState();
    try {
      const scrap = await api.scrap.create({ ...payload, projectId: projectId() });
      store.setMessage(`余料已入库：#${scrap.id} ${scrap.name}`);
      return true;
    } catch (error) {
      store.setMessage(`新建余料失败：${humanError(error)}`);
      return false;
    }
  }, []);

  /** 改名称/编号/物料/备注 */
  const rename = useCallback(async (id: number, payload: ScrapInput): Promise<boolean> => {
    const store = useAppStore.getState();
    try {
      const scrap = await api.scrap.update(id, { ...payload, projectId: projectId() });
      store.setMessage(`余料已更新：#${scrap.id} ${scrap.name}`);
      return true;
    } catch (error) {
      store.setMessage(`余料更新失败：${humanError(error)}`);
      return false;
    }
  }, []);

  /** 删除余料；如果它正被当板材用，后端会自动解绑 */
  const remove = useCallback(async (id: number): Promise<boolean> => {
    const store = useAppStore.getState();
    try {
      const result = await api.scrap.remove(id, projectId());
      store.setMessage(result.unbound
        ? '余料已删除，排料板已退回整板'
        : '余料已删除');
      return true;
    } catch (error) {
      store.setMessage(`删除余料失败：${humanError(error)}`);
      return false;
    } finally {
      requestLayoutSync();
    }
  }, []);

  return { open, close, loadList, loadRemaining, saveRegions, applyToSheet, clearSheet, create, rename, remove };
}
