/**
 * MES 对接（模块 12）的前端动作层。
 *
 * 这一层只做"搬运 + 提示"：
 *   - 配置读写（密钥永远是掩码，输入框留空 = 不改）；
 *   - 测试连接 / 拉单 / 回传归档三个动作的调用与结果提示；
 *   - 列表与日志的缓存更新。
 *
 * 重试、鉴权、字段归一化、幂等全部在后端，前端不判断 MES 协议细节。
 * 离线时这里的所有调用都会失败，但 CAM 基础功能完全不依赖它们。
 */

import { useCallback } from 'react';

import {
  api,
  type MesConfig,
  type MesConfigInput,
  type MesLog,
  type MesOrder,
  type MesPushResult,
  type MesTestResult,
  type WorkOrderDetail,
} from '@/net/rest';
import { useAppStore } from '@/state/app-store';

/** 后端抛出的错误文本形如 `502 Bad Gateway {"detail":"..."}`：抠出 detail 更像人话 */
function humanError(error: unknown): string {
  const text = (error as Error).message ?? String(error);
  const match = text.match(/\{"detail":"([^"]*)"/);
  return match ? match[1] : text;
}

export function useMesActions() {
  const open = useCallback(() => {
    useAppStore.getState().setMesOpen(true);
  }, []);

  const close = useCallback(() => {
    useAppStore.getState().setMesOpen(false);
  }, []);

  /** 读配置（含掩码、厂商清单、工单计数） */
  const loadSettings = useCallback(async (): Promise<MesConfig | null> => {
    const store = useAppStore.getState();
    try {
      return await api.mes.settings();
    } catch (error) {
      store.setMessage(`MES 配置读取失败：${humanError(error)}`);
      return null;
    }
  }, []);

  /** 保存配置：返回最新配置（含新掩码） */
  const saveSettings = useCallback(async (payload: MesConfigInput): Promise<MesConfig | null> => {
    const store = useAppStore.getState();
    try {
      const config = await api.mes.saveSettings(payload);
      store.setMessage(config.enabled
        ? `MES 已启用：${config.baseUrl}`
        : 'MES 配置已保存（当前为关闭状态，CAM 功能不受影响）');
      return config;
    } catch (error) {
      store.setMessage(`MES 配置保存失败：${humanError(error)}`);
      return null;
    }
  }, []);

  /** 测试连接：可传草稿，便于"先测后存" */
  const testConnection = useCallback(async (
    draft?: MesConfigInput,
  ): Promise<MesTestResult | null> => {
    const store = useAppStore.getState();
    try {
      const result = await api.mes.test(draft);
      store.setMessage(`MES 连接正常：${result.message}（${result.elapsedMs} ms）`);
      return result;
    } catch (error) {
      store.setMessage(`MES 连接失败：${humanError(error)}`);
      return null;
    }
  }, []);

  /** 工单列表（本地缓存） */
  const refreshOrders = useCallback(async (status = ''): Promise<MesOrder[]> => {
    const store = useAppStore.getState();
    try {
      const result = await api.mes.orders({ status, limit: 200 });
      return result.items;
    } catch (error) {
      store.setMessage(`MES 工单读取失败：${humanError(error)}`);
      return [];
    }
  }, []);

  /** 从 MES 拉单：返回本次新增/更新统计，并刷新列表 */
  const pull = useCallback(async (): Promise<MesOrder[]> => {
    const store = useAppStore.getState();
    try {
      const result = await api.mes.pull({
        projectId: store.projectId ?? undefined,
        limit: 100,
      });
      store.setMessage(
        `MES 拉单完成：新增 ${result.created} / 更新 ${result.updated}`
        + (result.skipped > 0 ? ` / 跳过 ${result.skipped}` : '')
        + `（${result.elapsedMs} ms，HTTP ${result.statusCode}）`,
      );
      return result.orders;
    } catch (error) {
      store.setMessage(`MES 拉单失败：${humanError(error)}`);
      return [];
    }
  }, []);

  /** 回传归档：工单信息 + 用料统计 + 加工文件 */
  const push = useCallback(async (
    orderId: number,
    workOrderId: number | null,
  ): Promise<MesPushResult | null> => {
    const store = useAppStore.getState();
    try {
      const result = await api.mes.push({ orderId, workOrderId });
      store.setMessage(
        `已回传 MES：${result.remoteId}（${result.fileCount} 个文件，`
        + `尝试 ${result.attempts} 次，${result.elapsedMs} ms）`,
      );
      return result;
    } catch (error) {
      store.setMessage(`MES 回传失败：${humanError(error)}`);
      return null;
    }
  }, []);

  /** 按 MES 单号建本地工单：code = remoteId，回传时后端就能自动对上 */
  const createLocalOrder = useCallback(async (order: MesOrder): Promise<WorkOrderDetail | null> => {
    const store = useAppStore.getState();
    try {
      const detail = await api.workorders.save({
        projectId: store.projectId ?? undefined,
        name: order.name || order.remoteId,
        code: order.remoteId,
        note: `由 MES 单 ${order.remoteId} 创建`,
      });
      store.setMessage(
        `已建本地工单 #${detail.id}（编号 ${order.remoteId}），回传时会自动带上用料与文件`,
      );
      // 工单列表多个面板共用，建完同步刷新，下拉里马上能选到
      const list = await api.workorders.list({});
      store.setWorkOrders(list.orders);
      return detail;
    } catch (error) {
      store.setMessage(`本地工单创建失败：${humanError(error)}`);
      return null;
    }
  }, []);

  /** 交互日志 */
  const refreshLogs = useCallback(async (orderId?: number): Promise<MesLog[]> => {
    const store = useAppStore.getState();
    try {
      const result = await api.mes.logs({ orderId, limit: 100 });
      return result.items;
    } catch (error) {
      store.setMessage(`MES 日志读取失败：${humanError(error)}`);
      return [];
    }
  }, []);

  return {
    open, close, loadSettings, saveSettings, testConnection,
    refreshOrders, pull, push, createLocalOrder, refreshLogs,
  };
}
