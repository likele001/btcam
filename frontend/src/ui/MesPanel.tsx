/**
 * MES 对接弹窗（模块 12）。
 *
 * 三个页签：
 *   连接设置  MES 地址 / 密钥 / 超时 / 重试 / 自动回传；可先「测试连接」再保存
 *   工单     从 MES 拉取工单列表，选中后回传归档（工单信息 + 用料统计 + 加工文件）
 *   日志     每次测试 / 拉单 / 推送的结果（含尝试次数与状态码），排障看这里
 *
 * 前端只做表单与展示：鉴权头、重试退避、字段归一化、幂等全部在后端完成。
 * 关闭开关时后端一个包都不会发，CAM 的排料/刀路/导出完全不受影响。
 */

import { useEffect, useState } from 'react';

import {
  api,
  type MesConfig,
  type MesConfigInput,
  type MesLog,
  type MesOrder,
  type MesPushResult,
  type MesTestResult,
} from '@/net/rest';
import { useAppStore } from '@/state/app-store';
import { useMesActions } from './useMesActions';

function pad(value: number): string {
  return String(value).padStart(2, '0');
}

function formatTime(stamp: number): string {
  const date = new Date(stamp * 1000);
  if (Number.isNaN(date.getTime())) return '-';
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
    + ` ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / 1024).toFixed(1)} KB`;
}

/** 日志动作的中文名；后端只存英文 action，翻译放前端 */
const ACTION_LABEL: Record<string, string> = {
  test: '测试连接',
  pull: '拉取工单',
  push: '回传归档',
};

type Tab = 'config' | 'orders' | 'logs';

export function MesPanel(): JSX.Element | null {
  const open = useAppStore((s) => s.mesOpen);
  const activeWorkOrder = useAppStore((s) => s.activeWorkOrder);
  const workOrders = useAppStore((s) => s.workOrders);
  const setMessage = useAppStore((s) => s.setMessage);

  const {
    close, loadSettings, saveSettings, testConnection, refreshOrders, pull, push,
    createLocalOrder, refreshLogs,
  } = useMesActions();

  const [tab, setTab] = useState<Tab>('config');
  const [config, setConfig] = useState<MesConfig | null>(null);

  // ---- 表单字段（保存时组装成 MesConfigInput）
  const [enabled, setEnabled] = useState(false);
  const [vendor, setVendor] = useState('cenkor');
  const [baseUrl, setBaseUrl] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [timeoutSec, setTimeoutSec] = useState(8);
  const [retries, setRetries] = useState(2);
  const [autoPush, setAutoPush] = useState(false);
  const [includeFiles, setIncludeFiles] = useState(true);
  const [paths, setPaths] = useState<Record<string, string>>({});

  // ---- 动作结果
  const [busy, setBusy] = useState(false);
  const [testResult, setTestResult] = useState<MesTestResult | null>(null);
  const [orders, setOrders] = useState<MesOrder[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [detail, setDetail] = useState<MesOrder | null>(null);
  const [logs, setLogs] = useState<MesLog[]>([]);
  const [pushResult, setPushResult] = useState<MesPushResult | null>(null);
  /** 回传时指定的本地工单：空串 = 按 MES 单号自动匹配 */
  const [workOrderId, setWorkOrderId] = useState('');

  /** 把后端配置灌进表单（密钥不回明文，输入框始终从空开始） */
  const hydrate = (data: MesConfig) => {
    setConfig(data);
    setEnabled(data.enabled);
    setVendor(data.vendor);
    setBaseUrl(data.baseUrl);
    setApiKey('');
    setTimeoutSec(data.timeout);
    setRetries(data.retries);
    setAutoPush(data.autoPush);
    setIncludeFiles(data.includeFiles);
    setPaths(data.paths ?? {});
  };

  useEffect(() => {
    if (!open) return;
    setTab('config');
    setTestResult(null);
    setPushResult(null);
    setSelectedId(null);
    setDetail(null);
    setWorkOrderId('');
    void (async () => {
      const data = await loadSettings();
      if (data) hydrate(data);
      setOrders(await refreshOrders());
      setLogs(await refreshLogs());
    })();
    // 只在弹窗打开瞬间初始化
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, close]);

  if (!open) return null;

  /** 组装草稿：测试连接与保存共用，保证"测的就是存的" */
  const draft = (): MesConfigInput => ({
    enabled,
    vendor,
    baseUrl: baseUrl.trim(),
    // 留空 = 不修改（后端沿用已保存的密钥）
    apiKey: apiKey.trim(),
    timeout: timeoutSec,
    retries,
    autoPush,
    includeFiles,
    paths,
  });

  const onTest = async () => {
    setBusy(true);
    setTestResult(await testConnection(draft()));
    setBusy(false);
    setLogs(await refreshLogs());
  };

  const onSave = async () => {
    setBusy(true);
    const saved = await saveSettings(draft());
    setBusy(false);
    if (!saved) return;
    hydrate(saved);
    setLogs(await refreshLogs());
  };

  /** 一键关掉对接（离线模式）：不影响任何 CAM 功能 */
  const onDisable = async () => {
    setBusy(true);
    const saved = await saveSettings({ ...draft(), enabled: false });
    setBusy(false);
    if (saved) hydrate(saved);
  };

  const onPull = async () => {
    setBusy(true);
    const items = await pull();
    setBusy(false);
    if (items.length > 0) setOrders(items);
    setLogs(await refreshLogs());
  };

  const onSelectOrder = async (order: MesOrder) => {
    setSelectedId(order.id);
    setPushResult(null);
    try {
      // 列表里的 parts 已经完整，但详情可能带更新的 pulled_at，统一再取一次
      setDetail(await api.mes.order(order.id));
    } catch (error) {
      setMessage(`MES 工单详情读取失败：${(error as Error).message}`);
    }
  };

  const onPush = async () => {
    if (!detail) return;
    setBusy(true);
    const result = await push(detail.id, workOrderId ? Number(workOrderId) : null);
    setBusy(false);
    if (result) {
      setPushResult(result);
      setOrders(await refreshOrders());
      setDetail(await api.mes.order(detail.id).catch(() => detail));
    }
    setLogs(await refreshLogs());
  };

  const onRefreshLogs = async () => {
    setBusy(true);
    setLogs(await refreshLogs());
    setBusy(false);
  };

  /** 按 MES 单号建本地工单：建完把关联状态、工单下拉一起刷新 */
  const onCreateLocalOrder = async () => {
    if (!detail) return;
    setBusy(true);
    const created = await createLocalOrder(detail);
    setBusy(false);
    if (!created) return;
    setOrders(await refreshOrders());
    setDetail(await api.mes.order(detail.id).catch(() => detail));
    setWorkOrderId(String(created.id));
  };

  const vendors = config?.vendors ?? [{ vendor: 'cenkor', label: 'CenkorMes' }];
  const defaults = config?.defaults?.[vendor];
  const archived = orders.filter((item) => item.status === 'archived').length;

  return (
    <div
      className="modal-mask"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) close();
      }}
    >
      <div className="modal mes-panel" role="dialog" aria-modal="true">
        <header className="modal-head">
          <span>MES 对接</span>
          <span className="modal-sub">
            模块 12 · 可选件：关闭时完全离线，CAM 功能不受影响
          </span>
          <button type="button" className="modal-close" title="关闭" onClick={close}>×</button>
        </header>

        <nav className="modal-tabs">
          <button
            type="button"
            className={`modal-tab${tab === 'config' ? ' active' : ''}`}
            onClick={() => setTab('config')}
          >
            连接设置
          </button>
          <button
            type="button"
            className={`modal-tab${tab === 'orders' ? ' active' : ''}`}
            onClick={() => setTab('orders')}
          >
            工单 {orders.length > 0 ? `(${orders.length})` : ''}
          </button>
          <button
            type="button"
            className={`modal-tab${tab === 'logs' ? ' active' : ''}`}
            onClick={() => setTab('logs')}
          >
            日志 {logs.length > 0 ? `(${logs.length})` : ''}
          </button>
        </nav>

        {tab === 'config' && (
          <div className="tool-library-body">
            <section className="tool-form-pane">
              <div className="tool-summary">
                <div>
                  <span>状态</span>
                  <b className={enabled ? 'mes-on' : 'mes-off'}>
                    {enabled ? '已启用（联网）' : '已关闭（离线）'}
                  </b>
                </div>
                <div>
                  <span>厂商</span>
                  <b>{vendors.find((item) => item.vendor === vendor)?.label ?? vendor}</b>
                </div>
                <div>
                  <span>密钥</span>
                  <b>{config?.hasApiKey ? config.apiKeyMask : '未设置'}</b>
                </div>
                <div>
                  <span>工单</span>
                  <b>{config?.orderCount ?? 0} 张 / 已归档 {config?.archivedCount ?? 0}</b>
                </div>
              </div>

              <div className="form">
                <label className="form-row form-check">
                  <span>启用 MES 对接（关闭后不发起任何网络请求）</span>
                  <input
                    type="checkbox"
                    checked={enabled}
                    onChange={(event) => setEnabled(event.target.checked)}
                  />
                </label>

                <label className="form-row">
                  <span>MES 厂商</span>
                  <select value={vendor} onChange={(event) => setVendor(event.target.value)}>
                    {vendors.map((item) => (
                      <option key={item.vendor} value={item.vendor}>{item.label}</option>
                    ))}
                  </select>
                </label>

                <label className="form-row form-row-wide">
                  <span>服务地址</span>
                  <input
                    type="text"
                    maxLength={300}
                    placeholder="http://192.168.1.20:8080"
                    value={baseUrl}
                    onChange={(event) => setBaseUrl(event.target.value)}
                  />
                </label>

                <label className="form-row form-row-wide">
                  <span>接口密钥</span>
                  <input
                    type="password"
                    maxLength={256}
                    placeholder={config?.hasApiKey ? '留空 = 不修改已保存的密钥' : 'API Key / Token'}
                    value={apiKey}
                    onChange={(event) => setApiKey(event.target.value)}
                  />
                </label>

                <label className="form-row">
                  <span>超时（秒）</span>
                  <input
                    type="number"
                    min={0.5}
                    max={60}
                    step={0.5}
                    value={timeoutSec}
                    onChange={(event) => setTimeoutSec(Number(event.target.value))}
                  />
                </label>

                <label className="form-row">
                  <span>重试次数</span>
                  <input
                    type="number"
                    min={0}
                    max={5}
                    step={1}
                    value={retries}
                    onChange={(event) => setRetries(Number(event.target.value))}
                  />
                </label>

                <label className="form-row form-check">
                  <span>导出成功后自动回传（本地工单编号需与 MES 单号一致）</span>
                  <input
                    type="checkbox"
                    checked={autoPush}
                    onChange={(event) => setAutoPush(event.target.checked)}
                  />
                </label>

                <label className="form-row form-check">
                  <span>回传时上传加工文件（NC / PLT / CUT）</span>
                  <input
                    type="checkbox"
                    checked={includeFiles}
                    onChange={(event) => setIncludeFiles(event.target.checked)}
                  />
                </label>

                {vendor === 'custom' && (
                  <div className="mes-paths">
                    <div className="mes-paths-title">自定义接口路径（留空用默认）</div>
                    {(['ping', 'orders', 'archive'] as const).map((name) => (
                      <label className="form-row form-row-wide" key={name}>
                        <span>{name}</span>
                        <input
                          type="text"
                          placeholder={defaults?.[name] ?? ''}
                          value={paths[name] ?? ''}
                          onChange={(event) => setPaths({ ...paths, [name]: event.target.value })}
                        />
                      </label>
                    ))}
                  </div>
                )}
              </div>

              <div className="mes-actions">
                <button type="button" className="btn btn-accent" onClick={() => void onSave()} disabled={busy}>
                  保存配置
                </button>
                <button type="button" className="btn" onClick={() => void onTest()} disabled={busy || !baseUrl.trim()}>
                  测试连接
                </button>
                <button type="button" className="btn btn-ghost" onClick={() => void onDisable()} disabled={busy || !enabled}>
                  关闭对接（离线）
                </button>
              </div>

              {testResult && (
                <div className={`mes-result ${testResult.ok ? 'ok' : 'bad'}`}>
                  <b>{testResult.ok ? '连接正常' : '连接失败'}</b>
                  <span>
                    HTTP {testResult.statusCode} · 尝试 {testResult.attempts} 次 · {testResult.elapsedMs} ms
                  </span>
                  <p>{testResult.message}</p>
                </div>
              )}

              <div className="post-note">
                密钥只在本机 SQLite 中以明文保存（单用户工具，便于排障），
                接口回包永远是掩码；日志里也不会打印密钥。
                离线/断网时这里的动作会失败，但排料、刀路、导出照常使用。
              </div>
            </section>
          </div>
        )}

        {tab === 'orders' && (
          <div className="tool-library-body">
            <section className="tool-list-pane mes-list-pane">
              <div className="mes-toolbar">
                <button type="button" className="btn btn-accent" onClick={() => void onPull()} disabled={busy || !enabled}>
                  从 MES 拉取
                </button>
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => void (async () => setOrders(await refreshOrders()))()}
                  disabled={busy}
                >
                  刷新本地
                </button>
                <span className="mes-count">{orders.length} 张 · 已归档 {archived}</span>
              </div>

              <ul className="mes-list">
                {orders.map((order) => (
                  <li
                    key={order.id}
                    className={`mes-item${selectedId === order.id ? ' active' : ''}`}
                    onClick={() => void onSelectOrder(order)}
                  >
                    <div className="mes-item-head">
                      <strong>{order.name}</strong>
                      <span className={`mes-badge ${order.status}`}>
                        {order.status === 'archived' ? '已归档' : '待推送'}
                      </span>
                    </div>
                    <div className="mes-item-meta">
                      {order.remoteId}
                      {' · '}
                      {order.partCount} 零件
                      {' · '}
                      {order.material || '未填物料'}
                      {order.thickness > 0 ? ` ${order.thickness}mm` : ''}
                    </div>
                    <div className="mes-item-meta">
                      <span className={`mes-badge ${order.localWorkOrder ? 'linked' : 'unlinked'}`}>
                        {order.localWorkOrder
                          ? `工单 #${order.localWorkOrder.id}`
                          : '未建本地工单'}
                      </span>
                    </div>
                    <div className="mes-item-meta mes-item-time">
                      拉取 {formatTime(order.pulledAt)}
                      {order.archivedAt ? ` · 归档 ${formatTime(order.archivedAt)}` : ''}
                    </div>
                  </li>
                ))}
                {orders.length === 0 && (
                  <li className="empty">还没有 MES 工单。启用对接后点「从 MES 拉取」。</li>
                )}
              </ul>
            </section>

            <section className="tool-form-pane mes-detail">
              {detail ? (
                <>
                  <div className="mes-detail-head">
                    <strong>{detail.name}</strong>
                    <span>{detail.remoteId}</span>
                  </div>
                  <div className="tool-summary">
                    <div><span>客户</span><b>{detail.customer || '-'}</b></div>
                    <div><span>物料</span><b>{detail.material || '-'}{detail.thickness > 0 ? ` ${detail.thickness}mm` : ''}</b></div>
                    <div><span>交期</span><b>{detail.dueDate || '-'}</b></div>
                    <div><span>零件</span><b>{detail.partCount}</b></div>
                  </div>

                  <div className="mes-actions">
                    {detail.localWorkOrder ? (
                      <>
                        <span className="mes-badge linked">已关联工单 #{detail.localWorkOrder.id}</span>
                        <span className="mes-hint">
                          {detail.localWorkOrder.name}
                          {` · 快照 ${detail.localWorkOrder.partCount} 个零件`}
                        </span>
                      </>
                    ) : (
                      <>
                        <span className="mes-badge unlinked">未建本地工单</span>
                        <button
                          type="button"
                          className="btn btn-ghost"
                          onClick={() => void onCreateLocalOrder()}
                          disabled={busy}
                        >
                          按单号 {detail.remoteId} 新建工单
                        </button>
                      </>
                    )}
                  </div>

                  <div className="mes-parts">
                    <div className="mes-parts-head">零件清单</div>
                    {detail.parts.map((part, index) => (
                      <div className="mes-part" key={`${part.remoteId}-${index}`}>
                        <span className="mes-part-name">{part.name}</span>
                        <span className="mes-part-meta">
                          ×{part.qty}
                          {part.width > 0 && part.height > 0 ? ` · ${part.width}×${part.height}` : ''}
                          {part.material ? ` · ${part.material}` : ''}
                        </span>
                      </div>
                    ))}
                    {detail.parts.length === 0 && <div className="empty">MES 未提供零件明细</div>}
                  </div>

                  <label className="form-row form-row-wide">
                    <span>本地工单</span>
                    <select value={workOrderId} onChange={(event) => setWorkOrderId(event.target.value)}>
                      <option value="">自动匹配（按单号 {detail.remoteId}）</option>
                      {activeWorkOrder && (
                        <option value={String(activeWorkOrder.id)}>
                          #{activeWorkOrder.id} {activeWorkOrder.name}（当前工单）
                        </option>
                      )}
                      {workOrders
                        .filter((item) => item.id !== activeWorkOrder?.id)
                        .map((item) => (
                          <option key={item.id} value={String(item.id)}>
                            #{item.id} {item.name}{item.code ? ` · ${item.code}` : ''}
                          </option>
                        ))}
                    </select>
                  </label>

                  <div className="mes-actions">
                    <button type="button" className="btn btn-accent" onClick={() => void onPush()} disabled={busy || !enabled}>
                      {detail.status === 'archived' ? '重新回传归档' : '回传归档'}
                    </button>
                    <span className="mes-hint">工单信息 + 用料统计 + 加工文件</span>
                  </div>

                  {pushResult && (
                    <div className="mes-result ok">
                      <b>回传成功</b>
                      <span>
                        HTTP {pushResult.statusCode} · 尝试 {pushResult.attempts} 次 ·{' '}
                        {pushResult.fileCount} 个文件 {formatBytes(pushResult.uploadedBytes)}
                      </span>
                      <p>
                        {pushResult.workOrderId
                          ? `已关联本地工单 #${pushResult.workOrderId}`
                          : '未找到同编号本地工单，只回传了 MES 单信息与零件清单'}
                      </p>
                    </div>
                  )}

                  <details className="mes-raw">
                    <summary>原始响应（排障用）</summary>
                    <pre>{JSON.stringify(detail.payload, null, 2).slice(0, 4000)}</pre>
                  </details>
                </>
              ) : (
                <div className="empty mes-detail-empty">
                  选择左侧 MES 工单查看零件清单并回传归档。
                </div>
              )}
            </section>
          </div>
        )}

        {tab === 'logs' && (
          <div className="mes-logs">
            <div className="mes-toolbar">
              <button type="button" className="btn btn-ghost" onClick={() => void onRefreshLogs()} disabled={busy}>
                刷新
              </button>
              <span className="mes-count">{logs.length} 条交互记录</span>
            </div>
            <div className="mes-log-list">
              {logs.map((log) => (
                <div className={`mes-log ${log.ok ? 'ok' : 'bad'}`} key={log.id}>
                  <span className="mes-log-time">{formatTime(log.createdAt)}</span>
                  <span className="mes-log-action">{ACTION_LABEL[log.action] ?? log.action}</span>
                  <span className="mes-log-status">
                    {log.ok ? '成功' : '失败'}
                    {log.statusCode > 0 ? ` · HTTP ${log.statusCode}` : ''}
                    {` · ${log.attempts} 次`}
                  </span>
                  <span className="mes-log-msg" title={log.message}>{log.message}</span>
                </div>
              ))}
              {logs.length === 0 && <div className="empty">还没有交互记录</div>}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
