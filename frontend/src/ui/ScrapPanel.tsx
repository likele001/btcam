/**
 * 余料库弹窗（模块 10）。
 *
 * 两个页签：
 *   余料库    排完板存下来的剩余料：检索、设为当前板材、删除、手工录一块方料
 *   剩余区域  当前板材减掉已摆零件后还能用的碎块：勾选后存进余料库
 *
 * 分工：这里只做列表渲染、勾选与表单；
 * 轮廓归一化、求差、面积、越界判定全部由后端（scrap_service / LayoutService）算，
 * 保存时也只回传勾选区域的 index，避免前端伪造几何。
 */

import { useCallback, useEffect, useState } from 'react';

import type { ScrapListResult, ScrapRemainingResult } from '@/net/rest';
import { useAppStore } from '@/state/app-store';
import { DEFAULT_MIN_AREA, useScrapActions } from './useScrapActions';

type Tab = 'library' | 'regions';

/** 面积统一用 dm² 展示（1 dm² = 10000 mm²），与排料面板一致 */
function dm2(area: number): string {
  return `${(area / 1e4).toFixed(2)} dm²`;
}

export function ScrapPanel(): JSX.Element | null {
  const open = useAppStore((s) => s.scrapOpen);
  const layout = useAppStore((s) => s.layout);
  const setMessage = useAppStore((s) => s.setMessage);

  const {
    close, loadList, loadRemaining, saveRegions, applyToSheet, clearSheet, create, remove,
  } = useScrapActions();

  const [tab, setTab] = useState<Tab>('library');
  const [busy, setBusy] = useState(false);

  // ---- 余料库
  const [keyword, setKeyword] = useState('');
  const [list, setList] = useState<ScrapListResult | null>(null);

  // ---- 剩余区域
  const [minArea, setMinArea] = useState(DEFAULT_MIN_AREA);
  const [remaining, setRemaining] = useState<ScrapRemainingResult | null>(null);
  const [checked, setChecked] = useState<number[]>([]);
  const [prefix, setPrefix] = useState('');
  const [material, setMaterial] = useState('');
  const [thickness, setThickness] = useState(0);

  // ---- 手工录入一块方料
  const [manualWidth, setManualWidth] = useState(800);
  const [manualHeight, setManualHeight] = useState(600);

  const refresh = useCallback(async (text: string) => {
    const data = await loadList(text);
    setList(data);
    return data;
  }, [loadList]);

  // 每次打开都从库里拉最新一份；关闭时不做任何清理（下次打开重新拉）
  useEffect(() => {
    if (!open) return;
    setTab('library');
    setRemaining(null);
    setChecked([]);
    setKeyword('');
    void (async () => {
      setBusy(true);
      await refresh('');
      setBusy(false);
    })();
  }, [open, refresh]);

  if (!open) return null;

  const currentScrapId = list?.currentScrapId ?? layout?.sheet.scrapId ?? 0;
  const boundScrap = layout?.scrap ?? null;
  const items = list?.items ?? [];

  const doSearch = async () => {
    setBusy(true);
    await refresh(keyword.trim());
    setBusy(false);
  };

  const doApply = async (id: number) => {
    setBusy(true);
    const ok = await applyToSheet(id);
    setBusy(false);
    if (ok) {
      setTab('library');
      await refresh(keyword.trim());
    }
  };

  const doClear = async () => {
    setBusy(true);
    const ok = await clearSheet();
    setBusy(false);
    if (ok) await refresh(keyword.trim());
  };

  const doRemove = async (id: number) => {
    setBusy(true);
    const ok = await remove(id);
    setBusy(false);
    if (ok) await refresh(keyword.trim());
  };

  const doCreate = async () => {
    if (manualWidth <= 0 || manualHeight <= 0) {
      setMessage('手工余料的宽高必须大于 0');
      return;
    }
    setBusy(true);
    const ok = await create({
      name: '',
      material: material.trim(),
      thickness,
      width: manualWidth,
      height: manualHeight,
    });
    setBusy(false);
    if (ok) await refresh(keyword.trim());
  };

  const doCalc = async () => {
    setBusy(true);
    const data = await loadRemaining(minArea);
    setBusy(false);
    setRemaining(data);
    // 默认勾选面积最大的那一块，用户通常只想留最大碎料
    setChecked(data && data.regions.length > 0 ? [data.regions[0].index] : []);
  };

  const toggleRegion = (index: number) => {
    setChecked((prev) => (prev.includes(index)
      ? prev.filter((item) => item !== index)
      : [...prev, index].sort((a, b) => a - b)));
  };

  const doSave = async () => {
    if (checked.length === 0) {
      setMessage('请先勾选要保存的余料区域');
      return;
    }
    setBusy(true);
    const result = await saveRegions(checked, {
      minArea, namePrefix: prefix.trim(), material: material.trim(), thickness,
    });
    setBusy(false);
    if (result) {
      setRemaining(null);
      setChecked([]);
      setTab('library');
      await refresh(keyword.trim());
    }
  };

  const stats = remaining?.stats;

  return (
    <div
      className="modal-mask"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) close();
      }}
    >
      <div className="modal scrap-panel" role="dialog" aria-modal="true">
        <header className="modal-head">
          <span>余料库</span>
          <span className="modal-sub">
            模块 10 · 切割剩下的料存下来，下次当板材继续排零件
          </span>
          <button type="button" className="modal-close" title="关闭" onClick={close}>×</button>
        </header>

        <nav className="modal-tabs">
          <button
            type="button"
            className={`modal-tab${tab === 'library' ? ' active' : ''}`}
            onClick={() => setTab('library')}
          >
            余料库 {items.length > 0 ? `(${items.length})` : ''}
          </button>
          <button
            type="button"
            className={`modal-tab${tab === 'regions' ? ' active' : ''}`}
            onClick={() => setTab('regions')}
          >
            剩余区域
          </button>
        </nav>

        {/* ---------------------------------------------------- 余料库 */}
        {tab === 'library' && (
          <div className="scrap-body">
            <div className="scrap-bar">
              <input
                className="scrap-search"
                placeholder="按名称 / 编号 / 物料检索"
                value={keyword}
                onChange={(event) => setKeyword(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') void doSearch();
                }}
              />
              <button type="button" className="btn" onClick={() => void doSearch()} disabled={busy}>
                检索
              </button>
              <span className="scrap-hint">
                当前板材：
                {boundScrap
                  ? `${boundScrap.name}（${boundScrap.width.toFixed(0)}×${boundScrap.height.toFixed(0)}mm）`
                  : '规则整板'}
              </span>
              {currentScrapId > 0 && (
                <button type="button" className="btn" onClick={() => void doClear()} disabled={busy}>
                  退回整板
                </button>
              )}
            </div>

            <div className="scrap-list">
              {items.length === 0 && (
                <div className="empty">
                  余料库还是空的：先排一次板，到「剩余区域」页勾选碎料保存
                </div>
              )}
              {items.map((item) => (
                <div
                  key={item.id}
                  className={`scrap-row${item.id === currentScrapId ? ' scrap-row-active' : ''}`}
                >
                  <div className="scrap-row-main">
                    <span className="scrap-name" title={item.note || item.name}>
                      {item.name}
                      {item.id === currentScrapId && <span className="scrap-tag">使用中</span>}
                      {item.source === 'layout' && <span className="scrap-tag scrap-tag-dim">排板</span>}
                    </span>
                    <span className="scrap-meta">
                      {item.material || '未填物料'}
                      {item.thickness > 0 ? ` · ${item.thickness}mm` : ''}
                      {' · '}
                      {item.width.toFixed(1)}×{item.height.toFixed(1)}mm
                      {' · '}
                      {dm2(item.area)}
                      {item.points > 0 ? ` · ${item.points} 点轮廓` : ''}
                    </span>
                  </div>
                  <div className="scrap-row-actions">
                    <button
                      type="button"
                      className="btn btn-accent"
                      onClick={() => void doApply(item.id)}
                      disabled={busy || item.id === currentScrapId}
                      title="把这块余料设为当前排料板，零件只能在轮廓内摆放"
                    >
                      {item.id === currentScrapId ? '已在使用' : '设为板材'}
                    </button>
                    <button
                      type="button"
                      className="btn btn-ghost"
                      onClick={() => void doRemove(item.id)}
                      disabled={busy}
                      title="删除余料（正在使用时排料板会自动退回整板）"
                    >
                      删除
                    </button>
                  </div>
                </div>
              ))}
            </div>

            <div className="panel-title">手工录入余料</div>
            <div className="form scrap-manual">
              <label className="form-row">
                <span>宽 (mm)</span>
                <input
                  type="number"
                  min={1}
                  step={10}
                  value={manualWidth}
                  onChange={(event) => setManualWidth(Number(event.target.value))}
                />
              </label>
              <label className="form-row">
                <span>高 (mm)</span>
                <input
                  type="number"
                  min={1}
                  step={10}
                  value={manualHeight}
                  onChange={(event) => setManualHeight(Number(event.target.value))}
                />
              </label>
              <label className="form-row">
                <span>板厚 (mm)</span>
                <input
                  type="number"
                  min={0}
                  step={0.5}
                  value={thickness}
                  onChange={(event) => setThickness(Number(event.target.value))}
                />
              </label>
              <label className="form-row">
                <span>物料</span>
                <input
                  placeholder="如 18mm 颗粒板"
                  value={material}
                  onChange={(event) => setMaterial(event.target.value)}
                />
              </label>
              <div className="form-actions">
                <button type="button" className="btn" onClick={() => void doCreate()} disabled={busy}>
                  存为矩形余料
                </button>
              </div>
            </div>
          </div>
        )}

        {/* ---------------------------------------------------- 剩余区域 */}
        {tab === 'regions' && (
          <div className="scrap-body">
            <div className="scrap-bar">
              <label className="scrap-inline">
                最小面积 (mm²)
                <input
                  type="number"
                  min={0}
                  step={1000}
                  value={minArea}
                  onChange={(event) => setMinArea(Number(event.target.value))}
                />
              </label>
              <button type="button" className="btn btn-accent" onClick={() => void doCalc()} disabled={busy}>
                {busy ? '计算中…' : '计算剩余区域'}
              </button>
              <span className="scrap-hint">
                板材 - 已摆零件（按最小间距外扩）后还能用的整块料
              </span>
            </div>

            {stats && (
              <dl className="kv scrap-kv">
                <div className="kv-row">
                  <dt>板材 / 使用中</dt>
                  <dd>
                    {stats.sheetWidth.toFixed(0)}×{stats.sheetHeight.toFixed(0)}mm
                    {' · '}
                    {stats.scrapId > 0 ? `余料 #${stats.scrapId}` : '整板'}
                  </dd>
                </div>
                <div className="kv-row">
                  <dt>可回收 / 板材</dt>
                  <dd>{dm2(stats.leftoverArea)} / {dm2(stats.sheetArea)}</dd>
                </div>
                <div className="kv-row">
                  <dt>可回收率</dt>
                  <dd>{(stats.leftoverRatio * 100).toFixed(1)}%</dd>
                </div>
                <div className="kv-row">
                  <dt>已摆零件</dt>
                  <dd>{stats.partCount} 件 · 间距 {stats.gap}mm</dd>
                </div>
              </dl>
            )}

            <div className="scrap-list">
              {!remaining && <div className="empty">点「计算剩余区域」列出可回收的碎料</div>}
              {remaining && remaining.count === 0 && (
                <div className="empty">没有大于最小面积的碎料（把最小面积调小试试）</div>
              )}
              {remaining?.regions.map((region) => (
                <label key={region.index} className="scrap-region">
                  <input
                    type="checkbox"
                    checked={checked.includes(region.index)}
                    onChange={() => toggleRegion(region.index)}
                  />
                  <span className="scrap-region-index">#{region.index + 1}</span>
                  <span className="scrap-region-size">
                    {region.width.toFixed(1)}×{region.height.toFixed(1)}mm
                  </span>
                  <span className="scrap-region-area">{dm2(region.area)}</span>
                  <span className="scrap-region-points">{region.points} 点</span>
                </label>
              ))}
            </div>

            <div className="form scrap-save">
              <label className="form-row">
                <span>名称前缀</span>
                <input
                  placeholder="余料"
                  value={prefix}
                  onChange={(event) => setPrefix(event.target.value)}
                />
              </label>
              <label className="form-row">
                <span>物料</span>
                <input
                  placeholder="如 18mm 颗粒板"
                  value={material}
                  onChange={(event) => setMaterial(event.target.value)}
                />
              </label>
              <label className="form-row">
                <span>板厚 (mm)</span>
                <input
                  type="number"
                  min={0}
                  step={0.5}
                  value={thickness}
                  onChange={(event) => setThickness(Number(event.target.value))}
                />
              </label>
              <div className="form-actions">
                <button
                  type="button"
                  className="btn btn-accent"
                  onClick={() => void doSave()}
                  disabled={busy || checked.length === 0}
                >
                  保存勾选的 {checked.length} 块
                </button>
              </div>
            </div>
          </div>
        )}

        <div className="modal-foot scrap-foot">
          余料轮廓由后端算好并落库；设为板材后轮廓就是边界，
          超出余料范围的零件会被拒绝摆放（自动排料也会自动避让）。
          新建工单时选中这块余料再保存，工单快照里会带上余料绑定。
        </div>
      </div>
    </div>
  );
}
