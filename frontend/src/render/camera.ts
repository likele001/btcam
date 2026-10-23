/**
 * 2D 相机：负责"世界坐标(mm) <-> 屏幕坐标(px)"的换算。
 *
 * 约定：
 * - 世界坐标系 Y 轴向上，屏幕 Y 轴向下，换算时翻转；
 * - scale 表示 1mm 对应多少屏幕像素；
 * - 相机位置 (x, y) 是视口中心对应的世界坐标。
 *
 * 之所以不直接把相机塞进 Pixi 的 Container，是因为框选/拾取/标尺都需要
 * 频繁做反变换，独立实现比反复读矩阵更直观、也更好单测。
 */

import type { BBox } from '@/types/geometry';

export const MIN_SCALE = 0.01;
export const MAX_SCALE = 4000;

export interface Point2 {
  x: number;
  y: number;
}

export class Camera {
  x = 0;
  y = 0;
  scale = 1;

  screenToWorld(sx: number, sy: number, width: number, height: number): Point2 {
    return {
      x: (sx - width / 2) / this.scale + this.x,
      y: (height / 2 - sy) / this.scale + this.y,
    };
  }

  worldToScreen(wx: number, wy: number, width: number, height: number): Point2 {
    return {
      x: (wx - this.x) * this.scale + width / 2,
      y: height / 2 - (wy - this.y) * this.scale,
    };
  }

  /** 按屏幕像素平移视图 */
  panBy(dxPx: number, dyPx: number): void {
    this.x -= dxPx / this.scale;
    this.y += dyPx / this.scale;
  }

  /** 以屏幕上某点为锚点缩放（鼠标滚轮的标准手感） */
  zoomAt(sx: number, sy: number, factor: number, width: number, height: number): void {
    const before = this.screenToWorld(sx, sy, width, height);
    this.scale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, this.scale * factor));
    const after = this.screenToWorld(sx, sy, width, height);
    this.x += before.x - after.x;
    this.y += before.y - after.y;
  }

  /** 缩放到合适范围，让 bbox 完整可见 */
  fit(bbox: BBox, width: number, height: number, padding = 48): void {
    const bw = Math.max(1e-6, bbox.maxX - bbox.minX);
    const bh = Math.max(1e-6, bbox.maxY - bbox.minY);
    // padding 随视口自适应：小窗口下固定 48px 会把可用区域挤成负数，
    // 导致 scale 被 MIN_SCALE 钳住、图形缩成一个点
    const pad = Math.min(padding, width * 0.1, height * 0.1);
    const scale = Math.min((width - pad * 2) / bw, (height - pad * 2) / bh);
    this.scale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale));
    this.x = (bbox.minX + bbox.maxX) / 2;
    this.y = (bbox.minY + bbox.maxY) / 2;
  }

  /** 当前视口对应的世界矩形（用于视锥裁剪） */
  visibleBBox(width: number, height: number, marginRatio = 0.05): BBox {
    const halfW = (width / 2) / this.scale;
    const halfH = (height / 2) / this.scale;
    const mx = halfW * marginRatio;
    const my = halfH * marginRatio;
    return {
      minX: this.x - halfW - mx,
      maxX: this.x + halfW + mx,
      minY: this.y - halfH - my,
      maxY: this.y + halfH + my,
    };
  }

  /** 生成 1 / 2 / 5 序列的网格步长，让网格密度随缩放自适应 */
  gridStep(targetPx = 80): number {
    const raw = targetPx / this.scale;
    const magnitude = 10 ** Math.floor(Math.log10(Math.max(1e-6, raw)));
    const normalized = raw / magnitude;
    const step = normalized < 2 ? 1 : normalized < 5 ? 2 : 5;
    return step * magnitude;
  }
}
