/**
 * 拾取与框选（纯几何计算，不依赖 Pixi，方便单测）。
 *
 * 为什么在前端做：
 * 鼠标拖动框选要求 16ms 内出结果，一次网络往返就卡顿了。
 * 这些判断只是浮点比较，10 万段轮廓也能在几毫秒内完成。
 */

import type { BBox, Polyline } from '@/types/geometry';

export interface WorldRect {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
}

export interface Point2 {
  x: number;
  y: number;
}

/** 从屏幕上的两个对角点构造规范化矩形 */
export function rectFromPoints(a: Point2, b: Point2): WorldRect {
  return {
    minX: Math.min(a.x, b.x),
    minY: Math.min(a.y, b.y),
    maxX: Math.max(a.x, b.x),
    maxY: Math.max(a.y, b.y),
  };
}

export function rectContains(rect: WorldRect, x: number, y: number): boolean {
  return x >= rect.minX && x <= rect.maxX && y >= rect.minY && y <= rect.maxY;
}

export function rectIntersects(a: WorldRect, b: WorldRect): boolean {
  return !(a.maxX < b.minX || a.minX > b.maxX || a.maxY < b.minY || a.minY > b.maxY);
}

/** 线段与矩形是否相交（Liang-Barsky 裁剪） */
export function segmentIntersectsRect(
  x1: number, y1: number, x2: number, y2: number, rect: WorldRect,
): boolean {
  const dx = x2 - x1;
  const dy = y2 - y1;
  let t0 = 0;
  let t1 = 1;

  const clip = (p: number, q: number): boolean => {
    if (p === 0) return q >= 0; // 平行于该边界
    const r = q / p;
    if (p < 0) {
      if (r > t1) return false;
      if (r > t0) t0 = r;
    } else {
      if (r < t0) return false;
      if (r < t1) t1 = r;
    }
    return true;
  };

  return (
    clip(-dx, x1 - rect.minX)
    && clip(dx, rect.maxX - x1)
    && clip(-dy, y1 - rect.minY)
    && clip(dy, rect.maxY - y1)
  );
}

/** 点到线段的最短距离平方 */
function pointSegmentDistanceSq(px: number, py: number,
  x1: number, y1: number, x2: number, y2: number): number {
  const dx = x2 - x1;
  const dy = y2 - y1;
  const lenSq = dx * dx + dy * dy;
  if (lenSq < 1e-12) {
    return (px - x1) ** 2 + (py - y1) ** 2;
  }
  let t = ((px - x1) * dx + (py - y1) * dy) / lenSq;
  t = Math.max(0, Math.min(1, t));
  const cx = x1 + t * dx;
  const cy = y1 + t * dy;
  return (px - cx) ** 2 + (py - cy) ** 2;
}

/** 轮廓包围盒（不缓存，轮廓点规模大时由调用方自行缓存） */
export function polylineBBox(poly: Polyline): BBox {
  const p = poly.pts;
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (let i = 0; i < p.length; i += 2) {
    const x = p[i];
    const y = p[i + 1];
    if (x < minX) minX = x;
    if (x > maxX) maxX = x;
    if (y < minY) minY = y;
    if (y > maxY) maxY = y;
  }
  return { minX, minY, maxX, maxY };
}

/**
 * 框选：返回与选框相交的轮廓 id。
 * 判据：包围盒相交 -> 任一顶点落在框内，或任一段与框边相交。
 */
export function rectSelect(polylines: Polyline[], rect: WorldRect): number[] {
  const selected: number[] = [];

  for (const poly of polylines) {
    const bbox = polylineBBox(poly);
    if (!rectIntersects(rect, bbox)) continue;

    // bbox 完全被框住：直接命中，省掉逐点判断
    if (bbox.minX >= rect.minX && bbox.maxX <= rect.maxX
      && bbox.minY >= rect.minY && bbox.maxY <= rect.maxY) {
      selected.push(poly.id);
      continue;
    }

    const pts = poly.pts;
    let hit = false;
    for (let i = 0; i < pts.length && !hit; i += 2) {
      if (rectContains(rect, pts[i], pts[i + 1])) hit = true;
    }

    if (!hit) {
      const count = pts.length / 2;
      const segCount = poly.closed ? count : count - 1;
      for (let i = 0; i < segCount && !hit; i += 1) {
        const j = (i + 1) % count;
        hit = segmentIntersectsRect(
          pts[i * 2], pts[i * 2 + 1], pts[j * 2], pts[j * 2 + 1], rect,
        );
      }
    }

    if (hit) selected.push(poly.id);
  }
  return selected;
}

/** 点选：返回距离鼠标最近且在容差内的轮廓 id */
export function pickAt(polylines: Polyline[], point: Point2, tolerance: number): number | null {
  const tolSq = tolerance * tolerance;
  let bestId: number | null = null;
  let bestDist = Infinity;

  for (const poly of polylines) {
    const pts = poly.pts;
    const count = pts.length / 2;
    if (count < 2) continue;
    const segCount = poly.closed ? count : count - 1;

    for (let i = 0; i < segCount; i += 1) {
      const j = (i + 1) % count;
      const dist = pointSegmentDistanceSq(
        point.x, point.y,
        pts[i * 2], pts[i * 2 + 1], pts[j * 2], pts[j * 2 + 1],
      );
      if (dist < bestDist && dist <= tolSq) {
        bestDist = dist;
        bestId = poly.id;
      }
    }
  }
  return bestId;
}

/**
 * 点是否落在轮廓围成的区域里（射线法 + 奇偶规则）。
 *
 * 一个零件通常是「外轮廓 + 若干内孔」，奇偶规则天然把孔洞判成外，
 * 不需要先分辨哪条是外轮廓 —— 排料模式下“点零件任意位置就能拖”靠它。
 */
export function pointInPolylines(polylines: Polyline[], point: Point2): boolean {
  let inside = false;
  for (const poly of polylines) {
    const pts = poly.pts;
    const count = pts.length / 2;
    if (count < 3) continue;
    for (let i = 0, j = count - 1; i < count; j = i, i += 1) {
      const xi = pts[i * 2];
      const yi = pts[i * 2 + 1];
      const xj = pts[j * 2];
      const yj = pts[j * 2 + 1];
      // 只统计射线在 y 方向上穿过的边，避免水平边与顶点重复计数
      if ((yi > point.y) !== (yj > point.y)) {
        const crossX = ((xj - xi) * (point.y - yi)) / (yj - yi) + xi;
        if (point.x < crossX) inside = !inside;
      }
    }
  }
  return inside;
}

/** 合并多个轮廓的包围盒 */
export function unionBBox(polylines: Polyline[]): BBox | null {
  if (polylines.length === 0) return null;
  let box: BBox | null = null;
  for (const poly of polylines) {
    const b = polylineBBox(poly);
    if (!box) {
      box = { ...b };
    } else {
      box.minX = Math.min(box.minX, b.minX);
      box.minY = Math.min(box.minY, b.minY);
      box.maxX = Math.max(box.maxX, b.maxX);
      box.maxY = Math.max(box.maxY, b.maxY);
    }
  }
  return box;
}
