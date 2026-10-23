/**
 * 左侧图层面板：控制显示/隐藏，展示工艺类型与轮廓数量。
 * 图层数据由后端随 GEOMETRY_DOC 的 meta 一起下发。
 */

import { useMemo } from 'react';

import { useAppStore } from '@/state/app-store';

const OPERATION_LABEL: Record<string, string> = {
  cut: '切割',
  engrave: '划槽',
  drill: '钻孔',
};

export function LayerPanel(): JSX.Element {
  const layers = useAppStore((s) => s.layers);
  const polylines = useAppStore((s) => s.polylines);
  const selection = useAppStore((s) => s.selection);
  const viewMode = useAppStore((s) => s.viewMode);
  const toggleLayer = useAppStore((s) => s.toggleLayer);
  const showAllLayers = useAppStore((s) => s.showAllLayers);

  const counts = useMemo(() => {
    const map = new Map<number, number>();
    for (const poly of polylines) {
      map.set(poly.layerId, (map.get(poly.layerId) ?? 0) + 1);
    }
    return map;
  }, [polylines]);

  return (
    <aside className="panel panel-left">
      <div className="panel-title">
        图层
        <button type="button" className="link-btn" onClick={showAllLayers}>全部显示</button>
      </div>

      <div className="layer-list">
        {layers.length === 0 && <div className="empty">暂无图层</div>}
        {layers.map((layer) => (
          <label key={layer.id} className="layer-row" title={OPERATION_LABEL[layer.operation] ?? layer.operation}>
            <input
              type="checkbox"
              checked={layer.visible}
              onChange={() => toggleLayer(layer.id)}
            />
            <span
              className="layer-swatch"
              style={{ background: `#${layer.color.toString(16).padStart(6, '0')}` }}
            />
            <span className="layer-name">{layer.name}</span>
            <span className="layer-count">{counts.get(layer.id) ?? 0}</span>
          </label>
        ))}
      </div>

      <div className="panel-title">概览</div>
      <dl className="kv">
        <dt>视图</dt>
        <dd>{viewMode === 'doc' ? '图纸' : '计算结果'}</dd>
        <dt>轮廓</dt>
        <dd>{polylines.length}</dd>
        <dt>选中</dt>
        <dd>{selection.length}</dd>
      </dl>

      <div className="panel-footer">
        提示：左键拖拽框选，Ctrl 追加选择；F 键适配视图。
      </div>
    </aside>
  );
}
