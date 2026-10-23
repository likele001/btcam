/**
 * 图形渲染层：把 Polyline 画到 PixiJS 的 Graphics 上。
 *
 * 性能要点：
 * 1. 只画视口内的轮廓（包围盒裁剪），大图纸也不掉帧；
 * 2. 线宽按 1/scale 折算成"屏幕 1px"，缩放时不会变粗变细；
 * 3. 选中的轮廓单独一层高亮，避免每次重建整张图。
 */

import { Container, Graphics } from 'pixi.js';

import type { BBox, LayerInfo, Polyline } from '@/types/geometry';

const DEFAULT_COLOR = 0x9fb3c8;
const SELECT_COLOR = 0xff6b3d;
/** 图形校验异常线段颜色（红）：与选中色分开，避免误读 */
const ISSUE_COLOR = 0xff4d4f;
/** 虚线节拍（屏幕像素）：按缩放折算成世界长度，保证任何缩放下节拍一致 */
const DASH_PX = 7;
const GAP_PX = 5;

/** 排料拖拽的视觉偏移：只影响绘制，不修改点数据（几何仍以后端为准） */
export interface DragVisual {
  partId: number;
  dx: number;
  dy: number;
  /** polylineId -> partId 映射，排料模式下才有 */
  partOf: Map<number, number>;
}

export class GeometryLayer {
  readonly container = new Container();

  private readonly base = new Graphics();
  private readonly highlight = new Graphics();
  /** 异常线段层：单次导入不会太多，每次重画成本可忽略 */
  private readonly issues = new Graphics();

  constructor() {
    this.container.addChild(this.base);
    this.container.addChild(this.highlight);
    this.container.addChild(this.issues);
  }

  /** 清空画布内容 */
  clear(): void {
    this.base.clear();
    this.highlight.clear();
    this.issues.clear();
  }

  /**
   * 重绘全部图形。
   * @param visibleBBox 视口世界包围盒，用于裁剪
   * @param scale 当前缩放（px/mm），用于把线宽折算成屏幕像素
   * @param issues 图形校验异常线段（红色叠加在最上层）
   */
  render(
    polylines: Polyline[],
    layers: Map<number, LayerInfo>,
    selection: Set<number>,
    visibleBBox: BBox,
    scale: number,
    issues: Polyline[] = [],
    drag: DragVisual | null = null,
  ): void {
    this.clear();
    const lineWidth = Math.max(1 / scale, 1e-4);

    const base = this.base;
    const highlight = this.highlight;

    for (const poly of polylines) {
      const pts = poly.pts;
      if (pts.length < 4) continue;

      const layer = layers.get(poly.layerId);
      if (layer && !layer.visible) continue;

      // 正在拖拽的零件：加视觉偏移，且不做视口裁剪（拖出去也要看得见）
      const dragging = drag !== null && drag.partOf.get(poly.id) === drag.partId;
      const ox = dragging ? drag.dx : 0;
      const oy = dragging ? drag.dy : 0;
      if (!dragging && !this.intersectsView(poly, visibleBBox)) continue;

      const selected = selection.has(poly.id);
      const target = selected ? highlight : base;
      const color = selected ? SELECT_COLOR : (layer?.color ?? DEFAULT_COLOR);
      const width = selected ? lineWidth * 2.4 : lineWidth;

      // 抬刀空走层：后端下发 dashed=true，这里手绘虚线段
      // （PixiJS v8 没有原生虚线样式，只能按节拍拆线段）
      if (layer?.dashed === true) {
        this.strokeDashed(target, pts, ox, oy, poly.closed, width, color, scale);
        continue;
      }

      target.moveTo(pts[0] + ox, pts[1] + oy);
      for (let i = 2; i < pts.length; i += 2) {
        target.lineTo(pts[i] + ox, pts[i + 1] + oy);
      }
      if (poly.closed) target.closePath();
      // 起割点层：小菱形填充，比纯描边更容易在深色背景上看到
      if (layer?.operation === 'marker' && poly.closed) {
        target.fill({ color, alpha: 1 });
      }

      target.stroke({
        width,
        color,
        alpha: 1,
        cap: 'round',
        join: 'round',
      });
    }

    // --- 异常线段：红色粗线，画在图形之上 ---
    // 不用虚线：异常线段往往很短，虚线在缩放后会看不到。
    for (const issue of issues) {
      const pts = issue.pts;
      if (pts.length < 4) continue;
      if (!this.intersectsView(issue, visibleBBox)) continue;
      this.issues.moveTo(pts[0], pts[1]);
      for (let i = 2; i < pts.length; i += 2) {
        this.issues.lineTo(pts[i], pts[i + 1]);
      }
      this.issues.stroke({ width: lineWidth * 3, color: ISSUE_COLOR, alpha: 1, cap: 'round' });
    }
  }

  /**
   * 按屏幕像素节拍画虚线。
   *
   * 世界坐标下 dash/gap 会随缩放变化，所以每段都重新折算：
   * 缩小时 dash 变长（线段数变少），放大时变短，视觉上恒定。
   */
  private strokeDashed(
    g: Graphics,
    pts: Float32Array,
    ox: number,
    oy: number,
    closed: boolean,
    width: number,
    color: number,
    scale: number,
  ): void {
    const safeScale = Math.max(scale, 1e-6);
    const dash = DASH_PX / safeScale;
    const gap = GAP_PX / safeScale;
    const count = Math.floor(pts.length / 2);
    if (count < 2) return;

    let drawing = true;
    let remain = dash;
    let x = pts[0] + ox;
    let y = pts[1] + oy;
    g.moveTo(x, y);

    const segments = closed ? count : count - 1;
    for (let i = 0; i < segments; i += 1) {
      const j = (i + 1) % count;
      const nx = pts[j * 2] + ox;
      const ny = pts[j * 2 + 1] + oy;
      const len = Math.hypot(nx - x, ny - y);
      if (len < 1e-9) continue;

      const ux = (nx - x) / len;
      const uy = (ny - y) / len;
      let cursor = 0;
      while (cursor < len) {
        const step = Math.min(remain, len - cursor);
        const px = x + ux * (cursor + step);
        const py = y + uy * (cursor + step);
        if (drawing) g.lineTo(px, py);
        else g.moveTo(px, py);
        cursor += step;
        remain -= step;
        if (remain <= 1e-9) {
          drawing = !drawing;
          remain = drawing ? dash : gap;
        }
      }
      x = nx;
      y = ny;
    }

    g.stroke({ width, color, alpha: 1, cap: 'butt' });
  }

  /** 只重绘缩放相关的线宽时调用（当前实现与 render 相同，保留接口以便后续优化） */
  setVisible(visible: boolean): void {
    this.container.visible = visible;
  }

  private intersectsView(poly: Polyline, box: BBox): boolean {
    const pts = poly.pts;
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (let i = 0; i < pts.length; i += 2) {
      const x = pts[i];
      const y = pts[i + 1];
      if (x < minX) minX = x;
      if (x > maxX) maxX = x;
      if (y < minY) minY = y;
      if (y > maxY) maxY = y;
    }
    return !(maxX < box.minX || minX > box.maxX || maxY < box.minY || minY > box.maxY);
  }

  destroy(): void {
    this.container.destroy({ children: true });
  }
}
