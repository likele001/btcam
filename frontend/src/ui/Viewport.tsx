/**
 * 画布容器组件。
 *
 * 生命周期：useEffect 里创建 CanvasController -> init -> 组件卸载时 destroy。
 * 数据流：store 变化 -> 调 controller.setGeometry / setSelection / setLayout（命令式，不走 props diff）。
 *
 * 排料交互（模块 3）的收口点也在这里：
 *   拖拽结束 / 按 R 旋转 / 按 L 锁定 -> 调后端接口 -> 后端广播新状态 -> store 更新 -> 画布重绘。
 * 前端全程不判定几何合法性，失败时只负责把视觉偏移撤掉（回弹）。
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { notifySelection } from '@/net/session';
import { CanvasController } from '@/render/viewport';
import { useAppStore } from '@/state/app-store';
import type { Polyline } from '@/types/geometry';
import { useLayoutActions } from './useLayoutActions';

/** 提交给后端的坐标保留 3 位小数：float32 的尾巴不该写进数据库 */
const round3 = (value: number): number => Math.round(value * 1000) / 1000;

interface ViewportProps {
  onReady: (controller: CanvasController) => void;
}

export function Viewport({ onReady }: ViewportProps): JSX.Element {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const controllerRef = useRef<CanvasController | null>(null);
  const fittedRef = useRef<Polyline[] | null>(null);
  const modeRef = useRef<string>('');

  const polylines = useAppStore((s) => s.polylines);
  const layers = useAppStore((s) => s.layers);
  const selection = useAppStore((s) => s.selection);
  const viewMode = useAppStore((s) => s.viewMode);
  const layout = useAppStore((s) => s.layout);
  const issueSegments = useAppStore((s) => s.issueSegments);
  // 模块 5：整板刀路的板框 / 点选起割点模式 / 待提交的起割点标记
  const board = useAppStore((s) => s.board);
  const pickingStart = useAppStore((s) => s.pickingStart);
  const startPoints = useAppStore((s) => s.startPoints);
  // 作图（模块 3）：手绘多边形中 / 顶点编辑中的零件
  const drawingShape = useAppStore((s) => s.drawingShape);
  const editingPartId = useAppStore((s) => s.editingPartId);

  const [hud, setHud] = useState({ scale: 1, cx: 0, cy: 0 });
  // 画布初始化是异步的：就绪后置 true，让依赖画布的 effect 重跑一次
  const [ready, setReady] = useState(false);

  /** 后端拒绝时撤掉拖拽偏移，让零件回到真实位置 */
  const cancelDrag = useCallback(() => controllerRef.current?.cancelDrag(), []);
  const actions = useLayoutActions(cancelDrag);

  // 控制器只建一次，而回调里必须拿到最新的动作集合，所以用 ref 转接
  const actionsRef = useRef(actions);
  actionsRef.current = actions;

  // --- 创建 / 销毁控制器（只跑一次） ---
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return undefined;

    const controller = new CanvasController({
      onSelectionChange: (ids) => {
        useAppStore.getState().setSelection(ids);
        notifySelection(ids);
      },
      onViewChange: ({ scale, cursorX, cursorY }) => {
        const next = {
          scale: Math.round(scale * 1000) / 1000,
          cx: Math.round(cursorX * 100) / 100,
          cy: Math.round(cursorY * 100) / 100,
        };
        setHud((prev) => (prev.scale === next.scale && prev.cx === next.cx && prev.cy === next.cy
          ? prev
          : next));
      },
      onPartMoved: (partId, tx, ty) => {
        void actionsRef.current.movePart(partId, tx, ty);
      },
      onRotateRequest: (delta) => {
        void actionsRef.current.rotateSelected(delta);
      },
      onLockToggle: () => {
        void actionsRef.current.lockSelected();
      },
      onPointPickEnd: () => {
        // Esc 退出点选：同步面板按钮状态
        useAppStore.getState().setPickingStart(false);
      },
      onDrawEnd: () => {
        // 手绘完成或取消：面板按钮回到“开始绘制”
        useAppStore.getState().setDrawingShape(false);
      },
      onVertexEditEnd: () => {
        useAppStore.getState().setEditingPartId(null);
      },
    });
    controllerRef.current = controller;

    let disposed = false;
    void controller.init(canvas).then(() => {
      if (disposed) return;
      controller.fitToContent();
      setReady(true);
      onReady(controller);
    }).catch((err: unknown) => {
      // 初始化失败（WebGL 不可用 / 资源加载失败等）不能让界面静默白屏，状态栏给出原因
      const message = err instanceof Error ? err.message : String(err);
      useAppStore.getState().setMessage(`画布初始化失败：${message}`);
    });

    return () => {
      disposed = true;
      controller.destroy();
      controllerRef.current = null;
    };
    // onReady 必须由父组件用 useCallback 固定，否则这里会反复重建画布
  }, [onReady]);

  // --- 排料上下文要先于图形同步（fit 需要板材尺寸） ---
  useEffect(() => {
    controllerRef.current?.setLayout(layout);
  }, [layout]);

  // --- 模块 5：整板刀路的板框（与 layout 分开，避免误入拖拽逻辑） ---
  useEffect(() => {
    controllerRef.current?.setBoard(board);
  }, [board]);

  // --- 模块 5：点选起割点模式开关 ---
  useEffect(() => {
    const controller = controllerRef.current;
    if (!controller) return undefined;
    if (!pickingStart) {
      controller.endPointPick();
      return undefined;
    }
    controller.beginPointPick((x, y) => {
      useAppStore.getState().addStartPoint({ x, y });
    });
    return () => controller.endPointPick();
  }, [pickingStart]);

  // --- 模块 5：点选的起割点实时标在画布上（生成后换成后端吸附结果） ---
  useEffect(() => {
    controllerRef.current?.setPickPoints(startPoints);
  }, [startPoints]);

  // --- 作图：手绘多边形（点左键加顶点，双击/回车闭合） ---
  useEffect(() => {
    const controller = controllerRef.current;
    if (!controller || !ready) return undefined;
    if (!drawingShape) {
      controller.endPolygonDraw();
      return undefined;
    }
    controller.beginPolygonDraw((points) => {
      // 用户画在哪就摆在哪：把点集的包围盒左下角当作期望落位，后端放不下时再另找
      const xs = points.map((p) => p.x);
      const ys = points.map((p) => p.y);
      void actionsRef.current.addShape({
        kind: 'polygon',
        at: [round3(Math.min(...xs)), round3(Math.min(...ys))],
        points: points.map((p) => [round3(p.x), round3(p.y)] as [number, number]),
      });
    });
    return () => controller.endPolygonDraw();
  }, [drawingShape, ready]);

  // --- 作图：顶点编辑（拖绿色方块改造型，松手提交） ---
  useEffect(() => {
    const controller = controllerRef.current;
    if (!controller || !ready) return undefined;
    if (editingPartId === null) {
      controller.endVertexEdit();
      return undefined;
    }
    controller.beginVertexEdit(editingPartId, (partId, contours) => {
      void actionsRef.current.updateOutline(partId, contours.map(
        (pts) => pts.map((p) => [round3(p.x), round3(p.y)] as [number, number])));
    });
    return () => controller.endVertexEdit();
  }, [editingPartId, ready]);

  // --- 数据同步 ---
  useEffect(() => {
    controllerRef.current?.setGeometry(polylines, layers);
    const entered = modeRef.current !== viewMode;
    modeRef.current = viewMode;

    // 进入图纸/排料模式时适配一次视野；排料过程中拖拽不重置视野
    if (entered && polylines.length > 0) {
      controllerRef.current?.fitToContent();
    } else if (viewMode === 'doc' && polylines.length > 0 && fittedRef.current !== polylines) {
      fittedRef.current = polylines;
      controllerRef.current?.fitToContent();
    }
  }, [polylines, layers, viewMode]);

  useEffect(() => {
    controllerRef.current?.setSelection(selection);
  }, [selection]);

  // 异常线段（图形校验）单独同步：与图形无关，导入一次变一次
  useEffect(() => {
    controllerRef.current?.setIssues(issueSegments);
  }, [issueSegments]);

  return (
    <div className="canvas-wrap">
      <canvas ref={canvasRef} className="canvas" />
      <div className="hud">
        <span>X {hud.cx.toFixed(2)}</span>
        <span>Y {hud.cy.toFixed(2)}</span>
        <span>{(hud.scale * 100).toFixed(1)}%</span>
        <span className="hud-tip">
          {drawingShape
            ? '点左键加顶点 · 双击或回车闭合 · Esc 取消'
            : editingPartId !== null
              ? '拖动绿色方块改造型 · Esc 退出'
              : pickingStart
                ? '点选起割点 · Esc 退出 · 滚轮缩放'
                : viewMode === 'layout'
                  ? '拖拽零件摆放 · R 旋转 · L 锁定 · 滚轮缩放'
                  : viewMode === 'preview'
                    ? '实线切削 · 虚线抬刀 · 菱形起割点 · F 适配'
                    : '滚轮缩放 · 中键/空格平移 · 左键框选'}
        </span>
      </div>
    </div>
  );
}
