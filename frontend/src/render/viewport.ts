/**
 * 画布控制器：PixiJS 渲染 + 鼠标交互的唯一入口。
 *
 * 交互约定（与主流 CAD/CAM 一致）：
 *   - 滚轮          : 以光标为锚点缩放
 *   - 中键拖动 / 空格+左键拖动 : 平移
 *   - 左键拖动      : 框选（Ctrl/Shift 追加）；排料模式下按住零件内部任意位置拖动
 *   - 左键单击      : 点选轮廓（Ctrl 切换；排料模式下选中整个零件）
 *   - R / Shift+R   : 排料模式下旋转选中零件 ±90°
 *   - L 键          : 排料模式下锁定/解锁选中零件
 *   - F 键          : 缩放到全部图形
 *   - Esc           : 取消当前操作
 *
 * 职责边界：排料拖拽只做“像素级平移”用于实时反馈，
 * 落库与越界/重叠判定全部交后端（松手后发一次请求）。
 *
 * 与 React 解耦：本类不 import React，通过回调把状态抛给外层，
 * 便于单测、也便于将来换 Vue/Svelte 时复用。
 */

// PixiJS 默认用 new Function 生成 uniform/shader 的同步代码，而打包版的 CSP 是
// script-src 'self'（不允许 unsafe-eval，dev 环境才有豁免），会让 app.init() 直接抛错、
// 画布停在初始 300x150 的空白状态。这个子模块用等价的 eval-free 实现覆盖内部生成器，
// 是 Pixi 官方给受限 CSP 环境的方案；必须在 new Application() 之前执行，故放在首行 import。
import 'pixi.js/unsafe-eval';
import { Application, Container, Graphics } from 'pixi.js';

import type { BBox, LayerInfo, LayoutPartInfo, LayoutState, Polyline, SheetSpec } from '@/types/geometry';
import { Camera } from './camera';
import { GeometryLayer, type DragVisual } from './geometry-layer';
import {
  pickAt, pointInPolylines, polylineBBox, rectFromPoints, rectSelect, unionBBox,
  type Point2, type WorldRect,
} from './selection';

export interface CanvasCallbacks {
  /** 选中集变化（id 列表） */
  onSelectionChange?: (ids: number[]) => void;
  /** 视图变化：缩放比例与光标世界坐标，状态栏用 */
  onViewChange?: (info: { scale: number; cursorX: number; cursorY: number }) => void;
  /** 排料模式下拖拽结束：把新位置（零件包围盒左下角）交给后端校验 */
  onPartMoved?: (partId: number, tx: number, ty: number) => void;
  /** 排料模式下请求旋转选中零件（±角度） */
  onRotateRequest?: (delta: number) => void;
  /** 排料模式下请求锁定/解锁选中零件 */
  onLockToggle?: () => void;
  /** 点选起割点模式被 Esc 取消（面板据此同步按钮状态） */
  onPointPickEnd?: () => void;
}

const BG_COLOR = 0x14161a;
const GRID_COLOR = 0x22262d;
const AXIS_COLOR = 0x3a4250;
const BOX_COLOR = 0x4fa3ff;
/** 点选容差（屏幕像素） */
const PICK_TOLERANCE_PX = 6;
/** 小于该拖动距离视为点击而非框选 */
const CLICK_THRESHOLD_PX = 3;

export class CanvasController {
  readonly camera = new Camera();

  private app: Application | null = null;
  private canvas: HTMLCanvasElement | null = null;
  private readonly world = new Container();
  /** 板材底图：画在图形下面，避免遮住轮廓 */
  private readonly board = new Graphics();
  /** 待提交的起割点标记：单独一个 Graphics，避免与网格/选框混在一起 */
  private readonly picks = new Graphics();
  private readonly geometry = new GeometryLayer();
  private readonly overlay = new Graphics();

  private polylines: Polyline[] = [];
  private layers = new Map<number, LayerInfo>();
  private selection = new Set<number>();
  /** 图形校验异常线段（导入后由后端推送，标红显示） */
  private issues: Polyline[] = [];

  // --- 排料（模块 3）
  private layout: LayoutState | null = null;
  /** 预览底板（模块 5）：整板刀路时只画板框，不参与拖拽 */
  private boardSheet: SheetSpec | null = null;
  private partByPolyline = new Map<number, number>();
  private partById = new Map<number, LayoutPartInfo>();
  /** 零件 -> 包围盒 / 轮廓：排料模式下面内命中的索引（几何一变就重建） */
  private partBoxes = new Map<number, BBox>();
  private partShapes = new Map<number, Polyline[]>();
  private dragPartId: number | null = null;
  private dragOffset = { dx: 0, dy: 0 };
  private dragStartWorld = { x: 0, y: 0 };

  private mode: 'idle' | 'pan' | 'box' | 'drag-part' = 'idle';
  private additive = false;
  private spaceDown = false;
  private dragStart = { x: 0, y: 0 };
  private dragCurrent = { x: 0, y: 0 };

  private rafId = 0;
  private resizeObserver: ResizeObserver | null = null;

  // --- 点选起割点（模块 5）
  private pointPick: ((x: number, y: number) => void) | null = null;
  private pickPoints: Array<{ x: number; y: number }> = [];

  constructor(private readonly callbacks: CanvasCallbacks = {}) {}

  // ------------------------------------------------------------ 生命周期
  async init(canvas: HTMLCanvasElement): Promise<void> {
    this.canvas = canvas;

    const app = new Application();
    await app.init({
      canvas,
      // WebGL 优先，性能最好；驱动异常时 Pixi 会自动降级
      preference: 'webgl',
      powerPreference: 'high-performance',
      antialias: true,
      backgroundColor: BG_COLOR,
      backgroundAlpha: 1,
      resolution: Math.min(window.devicePixelRatio || 1, 2),
      autoDensity: true,
    });
    this.app = app;

    this.world.addChild(this.board);
    this.world.addChild(this.geometry.container);
    app.stage.addChild(this.world);
    app.stage.addChild(this.overlay);
    app.stage.addChild(this.picks);

    this.attachEvents();
    this.resize();
  }

  destroy(): void {
    this.detachEvents();
    if (this.rafId) cancelAnimationFrame(this.rafId);
    this.rafId = 0;
    this.pointPick = null;
    this.app?.destroy(true, { children: true });
    this.app = null;
    this.canvas = null;
  }

  // ------------------------------------------------------------ 数据
  setGeometry(polylines: Polyline[], layers: LayerInfo[]): void {
    this.polylines = polylines;
    this.layers = new Map(layers.map((layer) => [layer.id, layer]));
    this.rebuildPartIndex();
    this.requestRender();
  }

  setSelection(ids: number[]): void {
    const next = new Set(ids);
    if (next.size === this.selection.size && ids.every((id) => this.selection.has(id))) {
      return;
    }
    this.selection = next;
    this.requestRender();
  }

  /** 设置异常线段（传空数组即清空红色标记） */
  setIssues(issues: Polyline[]): void {
    this.issues = issues;
    this.requestRender();
  }

  /**
   * 设置排料上下文（传 null 退回普通模式）。
   *
   * 每次收到新的 LAYOUT_STATE 帧都会走这里，顺带清掉拖拽残移，
   * 所以“后端拒绝拖拽”时只要清偏移就能回弹到正确位置。
   */
  setLayout(layout: LayoutState | null): void {
    this.layout = layout;
    this.partByPolyline.clear();
    this.partById.clear();
    if (layout) {
      for (const part of layout.parts) {
        this.partById.set(part.id, part);
        for (const id of part.polylineIds) this.partByPolyline.set(id, part.id);
      }
    }
    this.dragPartId = null;
    this.dragOffset = { dx: 0, dy: 0 };
    this.rebuildPartIndex();
    this.requestRender();
  }

  /**
   * 重建「零件 -> 包围盒 / 轮廓」索引。
   * 排料帧的轮廓已按摆放变换过，所以这里量出来的就是屏幕上看到的位置。
   */
  private rebuildPartIndex(): void {
    this.partBoxes.clear();
    this.partShapes.clear();
    if (!this.layout) return;
    for (const poly of this.polylines) {
      const partId = this.partByPolyline.get(poly.id);
      if (partId === undefined) continue;
      const shapes = this.partShapes.get(partId);
      if (shapes) shapes.push(poly);
      else this.partShapes.set(partId, [poly]);

      const box = polylineBBox(poly);
      const prev = this.partBoxes.get(partId);
      if (!prev) {
        this.partBoxes.set(partId, box);
      } else {
        prev.minX = Math.min(prev.minX, box.minX);
        prev.minY = Math.min(prev.minY, box.minY);
        prev.maxX = Math.max(prev.maxX, box.maxX);
        prev.maxY = Math.max(prev.maxY, box.maxY);
      }
    }
  }

  /** 取消拖拽视觉偏移（后端拒绝或请求超时时由外层调用） */
  cancelDrag(): void {
    if (this.dragPartId === null && this.dragOffset.dx === 0 && this.dragOffset.dy === 0) return;
    this.dragPartId = null;
    this.dragOffset = { dx: 0, dy: 0 };
    this.requestRender();
  }

  /** 设置预览底板（整板刀路的板框）；传 null 清掉 */
  setBoard(sheet: SheetSpec | null): void {
    this.boardSheet = sheet;
    this.requestRender();
  }

  /**
   * 进入点选模式：左键单击回传世界坐标，可连续点选；Esc 退出。
   * 点选期间禁用框选/零件拖拽，滚轮缩放与平移仍然可用。
   */
  beginPointPick(onPick: (x: number, y: number) => void): void {
    this.pointPick = onPick;
    if (this.canvas) this.canvas.style.cursor = 'crosshair';
  }

  /** 退出点选模式（重复调用安全） */
  endPointPick(): void {
    if (this.pointPick === null) return;
    this.pointPick = null;
    if (this.canvas && this.mode !== 'pan') this.canvas.style.cursor = 'default';
  }

  /** 同步待提交的起割点标记（面板列表变化时调） */
  setPickPoints(points: Array<{ x: number; y: number }>): void {
    this.pickPoints = points;
    this.requestRender();
  }

  /** 缩放到全部图形；排料/预览模式下把板材边界也纳入，否则板边看不见 */
  fitToContent(): void {
    if (!this.app) return;
    let box: BBox | null = unionBBox(this.polylines);
    const sheet = this.layout?.sheet ?? this.boardSheet;
    if (sheet && sheet.width > 0 && sheet.height > 0) {
      box = box
        ? {
          minX: Math.min(box.minX, 0),
          minY: Math.min(box.minY, 0),
          maxX: Math.max(box.maxX, sheet.width),
          maxY: Math.max(box.maxY, sheet.height),
        }
        : { minX: 0, minY: 0, maxX: sheet.width, maxY: sheet.height };
    }
    if (!box) box = { minX: 0, minY: 0, maxX: 1000, maxY: 1000 };
    this.camera.fit(box, this.app.screen.width, this.app.screen.height);
    this.requestRender();
    // 适配后 scale 变了，主动刷新 HUD（用画布中心作为光标位置）
    this.emitView(this.app.screen.width / 2, this.app.screen.height / 2);
  }

  get scale(): number {
    return this.camera.scale;
  }

  // ------------------------------------------------------------ 渲染
  private resize(): void {
    if (!this.app || !this.canvas) return;
    const parent = this.canvas.parentElement;
    const width = Math.max(1, parent?.clientWidth ?? this.canvas.clientWidth ?? 800);
    const height = Math.max(1, parent?.clientHeight ?? this.canvas.clientHeight ?? 600);
    this.app.renderer.resize(width, height);
    this.requestRender();
  }

  /** 合并同一帧内的多次重绘请求，避免连续 wheel 事件把主线程打满 */
  private requestRender(): void {
    if (this.rafId) return;
    this.rafId = requestAnimationFrame(() => {
      this.rafId = 0;
      this.renderNow();
    });
  }

  private renderNow(): void {
    if (!this.app) return;
    const { width, height } = this.app.screen;

    // 世界坐标系 Y 向上：scale.y 取负即可完成翻转
    const { scale } = this.camera;
    this.world.scale.set(scale, -scale);
    this.world.position.set(width / 2 - this.camera.x * scale, height / 2 + this.camera.y * scale);

    const visible = this.camera.visibleBBox(width, height);
    this.drawBoard();
    this.geometry.render(this.polylines, this.layers, this.selection, visible, scale,
      this.issues, this.dragVisual());
    this.drawOverlay(width, height, visible);
    this.drawPicks(width, height);
  }

  /** 当前拖拽的视觉偏移（非排料模式或未拖拽时为 null） */
  private dragVisual(): DragVisual | null {
    if (this.dragPartId === null) return null;
    return {
      partId: this.dragPartId,
      dx: this.dragOffset.dx,
      dy: this.dragOffset.dy,
      partOf: this.partByPolyline,
    };
  }

  /** 板块底图：排料模式或整板刀路预览时绘制，坐标系与轮廓一致（左下角为原点） */
  private drawBoard(): void {
    const g = this.board;
    g.clear();
    const sheet = this.layout?.sheet ?? this.boardSheet;
    if (!sheet) return;

    const { width, height, margin } = sheet;
    if (width <= 0 || height <= 0) return;
    const line = 1 / Math.max(this.camera.scale, 1e-6);

    // 模块 10：板材是余料时，真实可用区是那块料的轮廓（可能带缺口），
    // 不能画成矩形——用户得一眼看出哪块区域不能用。
    const scrap = this.layout?.scrap ?? null;
    if (scrap && scrap.outline.length >= 3) {
      const points = scrap.outline.flatMap(([x, y]) => [x, y]);
      // 余料面（填充与描边分开两次提交，与下面的矩形写法一致）
      g.poly(points).fill({ color: 0x0f1319, alpha: 1 });
      g.poly(points).stroke({ width: line * 2, color: 0x4fa3ff, alpha: 0.9 });
      // 包围盒只做淡色参考线：方便看出余料是不是歪料/带缺角
      g.rect(0, 0, width, height).stroke({ width: line, color: 0x4fa3ff, alpha: 0.18 });
      return;
    }

    // 板面底色（比背景略亮，区分“板内/板外”），矩形整板路径
    g.rect(0, 0, width, height).fill({ color: 0x0f1319, alpha: 1 });
    // 板边
    g.rect(0, 0, width, height).stroke({ width: line * 2, color: 0x4fa3ff, alpha: 0.9 });
    // 四边预留余量（可用区域）
    if (margin > 0 && width > margin * 2 && height > margin * 2) {
      g.rect(margin, margin, width - margin * 2, height - margin * 2)
        .stroke({ width: line, color: 0x4fa3ff, alpha: 0.3 });
    }
  }

  /** 网格 / 坐标轴 / 选框，全部用屏幕坐标绘制，保证线宽恒定 */
  private drawOverlay(width: number, height: number, visible: WorldRect): void {
    const g = this.overlay;
    g.clear();

    // --- 自适应网格 ---
    const step = this.camera.gridStep();
    const startX = Math.floor(visible.minX / step) * step;
    for (let x = startX; x <= visible.maxX; x += step) {
      const sx = Math.round(this.camera.worldToScreen(x, 0, width, height).x) + 0.5;
      g.moveTo(sx, 0).lineTo(sx, height);
    }
    const startY = Math.floor(visible.minY / step) * step;
    for (let y = startY; y <= visible.maxY; y += step) {
      const sy = Math.round(this.camera.worldToScreen(0, y, width, height).y) + 0.5;
      g.moveTo(0, sy).lineTo(width, sy);
    }
    g.stroke({ width: 1, color: GRID_COLOR, alpha: 0.8 });

    // --- 坐标轴 ---
    const origin = this.camera.worldToScreen(0, 0, width, height);
    if (origin.y >= 0 && origin.y <= height) {
      g.moveTo(0, Math.round(origin.y) + 0.5).lineTo(width, Math.round(origin.y) + 0.5);
    }
    if (origin.x >= 0 && origin.x <= width) {
      g.moveTo(Math.round(origin.x) + 0.5, 0).lineTo(Math.round(origin.x) + 0.5, height);
    }
    g.stroke({ width: 1, color: AXIS_COLOR, alpha: 1 });

    // --- 框选矩形 ---
    if (this.mode === 'box') {
      const x = Math.min(this.dragStart.x, this.dragCurrent.x);
      const y = Math.min(this.dragStart.y, this.dragCurrent.y);
      const w = Math.abs(this.dragCurrent.x - this.dragStart.x);
      const h = Math.abs(this.dragCurrent.y - this.dragStart.y);
      if (w > 1 && h > 1) {
        g.rect(x, y, w, h)
          .fill({ color: BOX_COLOR, alpha: 0.08 })
          .stroke({ width: 1, color: BOX_COLOR, alpha: 0.9 });
      }
    }
  }

  /** 待提交的起割点：屏幕固定大小的菱形，缩放时不会看不见 */
  private drawPicks(width: number, height: number): void {
    const g = this.picks;
    g.clear();
    if (this.pickPoints.length === 0) return;

    const radius = 5;
    for (const point of this.pickPoints) {
      const s = this.camera.worldToScreen(point.x, point.y, width, height);
      g.moveTo(s.x, s.y - radius)
        .lineTo(s.x + radius, s.y)
        .lineTo(s.x, s.y + radius)
        .lineTo(s.x - radius, s.y)
        .closePath();
    }
    g.fill({ color: 0xffb020, alpha: 0.95 });
    g.stroke({ width: 1, color: 0x14161a, alpha: 0.9 });
  }

  // ------------------------------------------------------------ 交互
  private localPoint(event: { clientX: number; clientY: number }): { x: number; y: number } {
    const rect = this.canvas?.getBoundingClientRect();
    if (!rect) return { x: 0, y: 0 };
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  }

  private visiblePolylines(): Polyline[] {
    return this.polylines.filter((poly) => this.layers.get(poly.layerId)?.visible !== false);
  }

  private emitSelection(ids: number[]): void {
    this.selection = new Set(ids);
    this.callbacks.onSelectionChange?.(ids);
    this.requestRender();
  }

  private onWheel = (event: WheelEvent): void => {
    event.preventDefault();
    if (!this.app) return;
    const point = this.localPoint(event);
    const factor = Math.exp(-event.deltaY * 0.0012);
    this.camera.zoomAt(point.x, point.y, factor, this.app.screen.width, this.app.screen.height);
    this.emitView(point.x, point.y);
    this.requestRender();
  };

  private onPointerDown = (event: PointerEvent): void => {
    if (!this.canvas || !this.app) return;
    const point = this.localPoint(event);
    this.dragStart = point;
    this.dragCurrent = point;

    const wantPan = event.button === 1 || (event.button === 0 && this.spaceDown);
    if (wantPan) {
      this.mode = 'pan';
      this.canvas.style.cursor = 'grabbing';
    } else if (event.button === 0 && this.pointPick) {
      // 点选起割点：只回传世界坐标，不动选择集，也不进入框选
      const world = this.camera.screenToWorld(point.x, point.y,
        this.app.screen.width, this.app.screen.height);
      this.mode = 'idle';
      this.pointPick(world.x, world.y);
      event.preventDefault();
      return;
    } else if (event.button === 0 && this.layout) {
      // 排料模式：命中零件则选中整个零件，未锁定时开始拖拽
      if (!this.beginPartDrag(event, point)) return;
    } else if (event.button === 0) {
      this.mode = 'box';
      this.additive = event.ctrlKey || event.shiftKey;
    } else {
      this.mode = 'idle';
      return;
    }
    event.preventDefault();
    this.canvas.setPointerCapture(event.pointerId);
    this.requestRender();
  };

  /**
   * 排料模式的面内命中：返回光标所在零件 id。
   *
   * 先用包围盒筛，再用射线法确认（点在内孔里不算命中）；
   * 多个零件嵌套时取包围盒最小的那个，符合“按住最上面那个”的直觉。
   */
  private partAt(point: Point2): number | null {
    let best: number | null = null;
    let bestArea = Infinity;
    for (const [partId, box] of this.partBoxes) {
      if (point.x < box.minX || point.x > box.maxX
        || point.y < box.minY || point.y > box.maxY) {
        continue;
      }
      const area = (box.maxX - box.minX) * (box.maxY - box.minY);
      if (area >= bestArea) continue;
      const shapes = this.partShapes.get(partId);
      if (shapes && !pointInPolylines(shapes, point)) continue;
      bestArea = area;
      best = partId;
    }
    return best;
  }

  /**
   * 排料模式的按下处理。返回 true 表示已接管（拖拽或只选中），
   * 返回 false 表示点在空白处，交给外层继续走框选流程。
   *
   * 命中零件：选中该零件全部轮廓；未锁定则进入拖拽；锁定则只选中。
   */
  private beginPartDrag(event: PointerEvent, point: { x: number; y: number }): boolean {
    if (!this.app || !this.canvas) return true;
    const world = this.camera.screenToWorld(point.x, point.y,
      this.app.screen.width, this.app.screen.height);
    // 先按零件范围命中：点在零件内部任意位置都能拖；没命中再退回轮廓线拾取
    let partId = this.partAt(world) ?? undefined;
    if (partId === undefined) {
      const hit = pickAt(this.visiblePolylines(), world, PICK_TOLERANCE_PX / this.camera.scale);
      if (hit !== null) partId = this.partByPolyline.get(hit);
    }
    const part = partId !== undefined ? this.partById.get(partId) : undefined;

    if (part && partId !== undefined) {
      if (event.ctrlKey || event.shiftKey) {
        const next = new Set(this.selection);
        const allSelected = part.polylineIds.every((id) => next.has(id));
        for (const id of part.polylineIds) {
          if (allSelected) next.delete(id);
          else next.add(id);
        }
        this.emitSelection([...next]);
      } else {
        this.emitSelection(part.polylineIds);
      }

      if (part.locked) {
        this.mode = 'idle';
        this.canvas.style.cursor = 'not-allowed';
        event.preventDefault();
        return true;
      }

      this.mode = 'drag-part';
      this.dragPartId = partId;
      this.dragStartWorld = world;
      this.dragOffset = { dx: 0, dy: 0 };
      this.canvas.style.cursor = 'move';
      event.preventDefault();
      this.canvas.setPointerCapture(event.pointerId);
      this.requestRender();
      return true;
    }

    // 空白处：未按修饰键则先清空选择，再等拖拽框选
    if (!event.ctrlKey && !event.shiftKey) this.emitSelection([]);
    this.mode = 'box';
    this.additive = event.ctrlKey || event.shiftKey;
    return false;
  }

  private onPointerMove = (event: PointerEvent): void => {
    if (!this.app) return;
    const point = this.localPoint(event);
    const world = this.camera.screenToWorld(point.x, point.y,
      this.app.screen.width, this.app.screen.height);

    if (this.mode === 'pan') {
      this.camera.panBy(point.x - this.dragCurrent.x, point.y - this.dragCurrent.y);
      this.dragCurrent = point;
      this.requestRender();
    } else if (this.mode === 'box') {
      this.dragCurrent = point;
      this.requestRender();
    } else if (this.mode === 'drag-part') {
      // 只做像素级位移反馈，几何合法性交给后端
      this.dragOffset = {
        dx: world.x - this.dragStartWorld.x,
        dy: world.y - this.dragStartWorld.y,
      };
      this.requestRender();
    }

    this.callbacks.onViewChange?.({
      scale: this.camera.scale,
      cursorX: world.x,
      cursorY: world.y,
    });
  };

  private onPointerUp = (event: PointerEvent): void => {
    if (!this.app || !this.canvas) return;
    const point = this.localPoint(event);
    const width = this.app.screen.width;
    const height = this.app.screen.height;

    if (this.mode === 'drag-part') {
      const partId = this.dragPartId;
      const part = partId !== null ? this.partById.get(partId) : undefined;
      const { dx, dy } = this.dragOffset;
      const moved = Math.abs(dx) > 1e-3 || Math.abs(dy) > 1e-3;
      if (part && partId !== null && moved) {
        // 偏移保留到后端返回新状态；失败时由外层调 cancelDrag() 回弹
        this.callbacks.onPartMoved?.(partId, part.tx + dx, part.ty + dy);
      } else {
        this.dragPartId = null;
        this.dragOffset = { dx: 0, dy: 0 };
      }
    } else if (this.mode === 'box') {
      const moved = Math.hypot(point.x - this.dragStart.x, point.y - this.dragStart.y);
      if (moved < CLICK_THRESHOLD_PX) {
        // 单击：点选
        const worldPoint = this.camera.screenToWorld(point.x, point.y, width, height);
        const hit = pickAt(this.visiblePolylines(), worldPoint, PICK_TOLERANCE_PX / this.camera.scale);
        if (hit === null) {
          if (!this.additive) this.emitSelection([]);
        } else if (this.additive) {
          const next = new Set(this.selection);
          if (next.has(hit)) next.delete(hit);
          else next.add(hit);
          this.emitSelection([...next]);
        } else {
          this.emitSelection([hit]);
        }
      } else {
        // 拖拽：框选；排料模式下把命中轮廓展开成整个零件
        const a = this.camera.screenToWorld(this.dragStart.x, this.dragStart.y, width, height);
        const b = this.camera.screenToWorld(point.x, point.y, width, height);
        const hits = this.expandToParts(
          rectSelect(this.visiblePolylines(), rectFromPoints(a, b)));
        this.emitSelection(this.additive ? [...new Set([...this.selection, ...hits])] : hits);
      }
    }

    this.mode = 'idle';
    this.canvas.style.cursor = this.pointPick
      ? 'crosshair'
      : (this.spaceDown ? 'grab' : 'default');
    if (this.canvas.hasPointerCapture(event.pointerId)) {
      this.canvas.releasePointerCapture(event.pointerId);
    }
    this.requestRender();
  };

  /** 把命中的轮廓 id 展开为“所属零件的全部轮廓”（非排料模式原样返回） */
  private expandToParts(ids: number[]): number[] {
    if (!this.layout) return ids;
    const out = new Set<number>();
    for (const id of ids) {
      const partId = this.partByPolyline.get(id);
      const part = partId !== undefined ? this.partById.get(partId) : undefined;
      if (part) {
        for (const polyId of part.polylineIds) out.add(polyId);
      } else {
        out.add(id);
      }
    }
    return [...out];
  }

  private onKeyDown = (event: KeyboardEvent): void => {
    // 输入框里不抢快捷键
    const target = event.target as HTMLElement | null;
    if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return;

    if (event.code === 'Space') {
      this.spaceDown = true;
      if (this.canvas) this.canvas.style.cursor = 'grab';
      event.preventDefault();
    } else if (event.key === 'f' || event.key === 'F') {
      this.fitToContent();
    } else if (this.layout && (event.key === 'r' || event.key === 'R')) {
      // 排料模式：R 顺时针转 90°，Shift+R 逆时针
      this.callbacks.onRotateRequest?.(event.shiftKey ? -90 : 90);
      event.preventDefault();
    } else if (this.layout && (event.key === 'l' || event.key === 'L')) {
      this.callbacks.onLockToggle?.();
      event.preventDefault();
    } else if (event.key === 'Escape') {
      this.mode = 'idle';
      this.cancelDrag();
      if (this.pointPick) {
        this.endPointPick();
        this.callbacks.onPointPickEnd?.();
      }
    }
  };

  private onKeyUp = (event: KeyboardEvent): void => {
    if (event.code === 'Space') {
      this.spaceDown = false;
      if (this.canvas && this.mode !== 'pan') this.canvas.style.cursor = 'default';
    }
  };

  private emitView(sx: number, sy: number): void {
    if (!this.app) return;
    const world = this.camera.screenToWorld(sx, sy, this.app.screen.width, this.app.screen.height);
    this.callbacks.onViewChange?.({
      scale: this.camera.scale,
      cursorX: world.x,
      cursorY: world.y,
    });
  }

  private attachEvents(): void {
    const canvas = this.canvas;
    if (!canvas) return;

    canvas.addEventListener('wheel', this.onWheel, { passive: false });
    canvas.addEventListener('pointerdown', this.onPointerDown);
    canvas.addEventListener('pointermove', this.onPointerMove);
    canvas.addEventListener('pointerup', this.onPointerUp);
    canvas.addEventListener('pointercancel', this.onPointerUp);
    canvas.addEventListener('contextmenu', (e) => e.preventDefault());
    window.addEventListener('keydown', this.onKeyDown);
    window.addEventListener('keyup', this.onKeyUp);

    if (typeof ResizeObserver !== 'undefined' && canvas.parentElement) {
      this.resizeObserver = new ResizeObserver(() => this.resize());
      this.resizeObserver.observe(canvas.parentElement);
    }
  }

  private detachEvents(): void {
    const canvas = this.canvas;
    if (canvas) {
      canvas.removeEventListener('wheel', this.onWheel);
      canvas.removeEventListener('pointerdown', this.onPointerDown);
      canvas.removeEventListener('pointermove', this.onPointerMove);
      canvas.removeEventListener('pointerup', this.onPointerUp);
      canvas.removeEventListener('pointercancel', this.onPointerUp);
    }
    window.removeEventListener('keydown', this.onKeyDown);
    window.removeEventListener('keyup', this.onKeyUp);
    this.resizeObserver?.disconnect();
    this.resizeObserver = null;
  }
}
