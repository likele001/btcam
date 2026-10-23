/**
 * 工单存档弹窗（模块 8）。
 *
 * 左侧：保存当前工程 + 历史工单列表（关键词 / 日期检索）；
 * 右侧：选中工单的统计、加工文件与操作（打开 / 另存 / 删除）。
 *
 * 所有快照采集与恢复都在后端完成：这里只提交名称与刀路/后处理参数，
 * 打开工单后由 WS 全量图形帧刷新画布，前端不做任何几何搬运。
 */

import { useEffect, useState } from 'react';

import { api, type WorkOrderDetail, type WorkOrderSummary } from '@/net/rest';
import { useAppStore } from '@/state/app-store';
import { useWorkOrderActions } from './useWorkOrderActions';

function pad(value: number): string {
  return String(value).padStart(2, '0');
}

function formatTime(stamp: number): string {
  const date = new Date(stamp * 1000);
  if (Number.isNaN(date.getTime())) return '-';
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
    + ` ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / 1024).toFixed(1)} KB`;
}

function baseName(path: string): string {
  const parts = path.split(/[\\/]/);
  return parts[parts.length - 1] || path;
}

export function WorkOrderPanel(): JSX.Element | null {
  const open = useAppStore((s) => s.workOrderOpen);
  const orders = useAppStore((s) => s.workOrders);
  const active = useAppStore((s) => s.activeWorkOrder);
  const setMessage = useAppStore((s) => s.setMessage);

  const {
    close, refresh, saveNew, recapture, openOrder, duplicate, remove,
  } = useWorkOrderActions();

  const [keyword, setKeyword] = useState('');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [name, setName] = useState('');
  const [code, setCode] = useState('');
  const [note, setNote] = useState('');
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [detail, setDetail] = useState<WorkOrderDetail | null>(null);
  const [busy, setBusy] = useState(false);
  const [armed, setArmed] = useState(false);

  const loadDetail = async (id: number) => {
    try {
      const record = await api.workorders.get(id);
      setDetail(record);
      setName(record.name);
      setCode(record.code);
      setNote(record.note);
    } catch (error) {
      setMessage(`工单详情读取失败：${(error as Error).message}`);
    }
  };

  // 打开瞬间：复位检索条件，若已有“当前工单”则直接定位到它
  useEffect(() => {
    if (!open) return;
    setKeyword('');
    setDateFrom('');
    setDateTo('');
    setArmed(false);
    setSelectedId(active?.id ?? null);
    setDetail(null);
    if (active) {
      void loadDetail(active.id);
    } else {
      setName('');
      setCode('');
      setNote('');
    }
    void refresh();
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

  const onSearch = () => {
    setArmed(false);
    void refresh({
      keyword: keyword.trim(),
      dateFrom,
      dateTo,
    });
  };

  const onResetSearch = () => {
    setKeyword('');
    setDateFrom('');
    setDateTo('');
    void refresh();
  };

  const onSelect = (order: WorkOrderSummary) => {
    setSelectedId(order.id);
    setArmed(false);
    setDetail(null);
    void loadDetail(order.id);
  };

  const requireName = (): string | null => {
    const value = name.trim();
    if (!value) {
      setMessage('请先填写工单名称');
      return null;
    }
    return value;
  };

  const onSaveNew = async () => {
    const value = requireName();
    if (!value) return;
    setBusy(true);
    const record = await saveNew(value, code.trim(), note.trim());
    setBusy(false);
    if (!record) return;
    setSelectedId(record.id);
    setDetail(record);
  };

  const onUpdateSnapshot = async () => {
    if (!detail) return;
    const value = requireName();
    if (!value) return;
    setBusy(true);
    const record = await recapture(detail.id, value, code.trim(), note.trim());
    setBusy(false);
    if (record) setDetail(record);
  };

  const onOpen = async () => {
    if (!detail) return;
    setBusy(true);
    const record = await openOrder(detail.id);
    setBusy(false);
    if (record) close();
  };

  const onDuplicate = async () => {
    if (!detail) return;
    setBusy(true);
    const record = await duplicate(detail.id);
    setBusy(false);
    if (!record) return;
    setSelectedId(record.id);
    setDetail(record);
    setArmed(false);
  };

  const onDelete = async () => {
    if (!detail) return;
    if (!armed) {
      setArmed(true);
      return;
    }
    setBusy(true);
    const removed = await remove(detail.id);
    setBusy(false);
    setArmed(false);
    if (!removed) return;
    setSelectedId(null);
    setDetail(null);
    setName('');
    setCode('');
    setNote('');
  };

  const stats = detail?.stats;

  return (
    <div
      className="modal-mask"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) close();
      }}
    >
      <div className="modal workorder-panel" role="dialog" aria-modal="true">
        <header className="modal-head">
          <span>工单存档</span>
          <span className="modal-sub">模块 8 · 本地 SQLite 快照，可检索、重复打开</span>
          <button type="button" className="modal-close" title="关闭" onClick={close}>×</button>
        </header>

        <div className="workorder-toolbar">
          <input
            className="workorder-keyword"
            type="text"
            placeholder="按名称 / 编号 / 备注检索"
            value={keyword}
            onChange={(event) => setKeyword(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') onSearch();
            }}
          />
          <label className="workorder-date">
            <span>从</span>
            <input type="date" value={dateFrom} onChange={(event) => setDateFrom(event.target.value)} />
          </label>
          <label className="workorder-date">
            <span>到</span>
            <input type="date" value={dateTo} onChange={(event) => setDateTo(event.target.value)} />
          </label>
          <button type="button" className="btn btn-accent" onClick={onSearch}>查询</button>
          <button type="button" className="btn btn-ghost" onClick={onResetSearch}>重置</button>
          <span className="workorder-count">{orders.length} 份工单</span>
        </div>

        <div className="workorder-body">
          <section className="workorder-left">
            <div className="workorder-save">
              <div className="workorder-save-title">
                保存当前工程
                <span>图形 / 板材 / 摆放 / 刀具 / 刀路参数 / 后处理</span>
              </div>
              <div className="form tool-form-grid">
                <label className="form-row form-row-wide">
                  <span>工单名称</span>
                  <input
                    type="text"
                    maxLength={64}
                    placeholder="如：柜体 A 批量开料"
                    value={name}
                    onChange={(event) => setName(event.target.value)}
                  />
                </label>
                <label className="form-row">
                  <span>工件编号</span>
                  <input
                    type="text"
                    maxLength={64}
                    placeholder="可选，如 WO-2026-001"
                    value={code}
                    onChange={(event) => setCode(event.target.value)}
                  />
                </label>
                <label className="form-row form-row-wide">
                  <span>备注</span>
                  <input
                    type="text"
                    maxLength={500}
                    placeholder="可选：物料、客户、工艺要求"
                    value={note}
                    onChange={(event) => setNote(event.target.value)}
                  />
                </label>
              </div>
              <div className="workorder-actions">
                <button
                  type="button"
                  className="btn btn-accent"
                  onClick={() => void onSaveNew()}
                  disabled={busy || !name.trim()}
                >
                  保存为新工单
                </button>
                <button
                  type="button"
                  className="btn"
                  onClick={() => void onUpdateSnapshot()}
                  disabled={busy || !detail}
                  title="把当前工程重新拍成快照写回选中的工单"
                >
                  更新选中工单快照
                </button>
              </div>
            </div>

            <ul className="workorder-list">
              {orders.map((order) => (
                <li
                  key={order.id}
                  className={`workorder-item${selectedId === order.id ? ' active' : ''}`}
                  onClick={() => onSelect(order)}
                >
                  <div className="workorder-item-head">
                    <strong>{order.name}</strong>
                    <span>{formatTime(order.createdAt)}</span>
                  </div>
                  <div className="workorder-item-meta">
                    {order.code ? `#${order.code}` : '无编号'}
                    {' · '}
                    {order.stats.partCount} 零件
                    {' · '}
                    利用率 {(order.stats.utilization * 100).toFixed(1)}%
                    {' · '}
                    {order.fileCount} 文件
                  </div>
                  {active?.id === order.id && <span className="workorder-badge">当前</span>}
                </li>
              ))}
              {orders.length === 0 && <li className="workorder-empty">没有匹配的工单</li>}
            </ul>
          </section>

          <section className="workorder-detail">
            {detail && stats ? (
              <>
                <div className="workorder-detail-head">
                  <strong>#{detail.id} {detail.name}</strong>
                  <span>创建 {formatTime(detail.createdAt)} · 更新 {formatTime(detail.updatedAt)}</span>
                </div>

                <div className="workorder-detail-stats">
                  <div><span>零件</span><b>{stats.partCount}</b></div>
                  <div><span>轮廓</span><b>{stats.polylineCount}</b></div>
                  <div><span>摆放</span><b>{stats.placementCount}</b></div>
                  <div><span>利用率</span><b>{(stats.utilization * 100).toFixed(1)}%</b></div>
                  <div className="wide">
                    <span>板材</span>
                    <b>{stats.sheetWidth} × {stats.sheetHeight} mm</b>
                  </div>
                </div>

                <div className="workorder-note">{detail.note || '无备注'}</div>

                {detail.missing.length > 0 && (
                  <div className="workorder-warn">{detail.missing.join('；')}</div>
                )}

                <div className="workorder-files">
                  <div className="workorder-files-head">
                    加工文件 <span>{detail.files.length}</span>
                  </div>
                  {detail.files.length === 0 && (
                    <div className="workorder-empty">
                      还没有导出记录；打开工单后在导出面板出文件会自动登记路径。
                    </div>
                  )}
                  {detail.files.map((file) => (
                    <div className="workorder-file" key={file.id} title={file.path}>
                      <span className="workorder-file-name">{baseName(file.path)}</span>
                      <span className="workorder-file-meta">
                        {file.format.toUpperCase()} · {formatBytes(file.bytes)} · 板 {file.sheetIndex + 1}
                      </span>
                    </div>
                  ))}
                </div>

                <div className="workorder-actions">
                  <button
                    type="button"
                    className="btn btn-accent"
                    onClick={() => void onOpen()}
                    disabled={busy}
                  >
                    打开工单（进入编辑）
                  </button>
                  <button
                    type="button"
                    className="btn"
                    onClick={() => void onDuplicate()}
                    disabled={busy}
                  >
                    另存为副本
                  </button>
                  <button
                    type="button"
                    className={`btn${armed ? ' btn-danger' : ' btn-ghost'}`}
                    onClick={() => void onDelete()}
                    disabled={busy}
                  >
                    {armed ? '再点一次确认删除' : '删除'}
                  </button>
                </div>
              </>
            ) : (
              <div className="workorder-empty workorder-detail-empty">
                选择左侧工单查看统计与加工文件，或先保存当前工程。
              </div>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}
