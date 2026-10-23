/**
 * 前端领域类型定义。
 * 坐标单位统一 mm，世界坐标系 Y 轴向上（与后端一致）。
 */

/** 一条 2D 折线；pts 为 [x0,y0,x1,y1,...] 的紧凑浮点数组，直接来自二进制帧 */
export interface Polyline {
  id: number;
  layerId: number;
  closed: boolean;
  /** 内孔轮廓（后端导入修复阶段判定）；渲染层用虚线区分，刀路阶段决定补偿方向 */
  isHole?: boolean;
  pts: Float32Array;
}

/** 零件：排料/锁定的最小单位，一个 DXF 文件可能拆出多个零件 */
export interface PartInfo {
  id: number;
  name: string;
  /** 来源文件名 */
  source: string;
  bbox: [number, number, number, number];
  /** 属于该零件的轮廓 id（二进制块里不存 part_id，靠这份映射关联） */
  polylineIds: number[];
}

/** 图形校验告警（自相交 / 开放轮廓），point 为异常位置的世界坐标 */
export interface GeometryIssue {
  kind: 'self_intersection' | 'open_contour' | string;
  message: string;
  polylineId: number;
  severity: 'warning' | 'error' | string;
  point: [number, number];
}

export interface LayerInfo {
  id: number;
  name: string;
  /** 0xRRGGBB */
  color: number;
  /** cut/engrave/drill 为图形层；rapid 抬刀、marker 起割点为刀路预览层 */
  operation: 'cut' | 'engrave' | 'drill' | 'rapid' | 'marker' | string;
  /** 后端指定该图层用虚线绘制（刀路预览的抬刀空走） */
  dashed?: boolean;
  visible: boolean;
  locked: boolean;
}

export interface BBox {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
}

/** 板材规格（排料板）：宽高 + 四边预留余量 + 零件最小间距 */
export interface SheetSpec {
  width: number;
  height: number;
  margin: number;
  gap: number;
}

/** 一个零件在板材上的摆放状态（后端持久化，前端只读） */
export interface LayoutPlacement {
  partId: number;
  sheet: number;
  /** 旋转后包围盒左下角在板材上的位置 */
  tx: number;
  ty: number;
  /** 逆时针角度（度） */
  rot: number;
  locked: boolean;
  /** 木纹方向锁定：自动排料与手动旋转都不改变角度 */
  fixedRot: boolean;
}

/** 排料面板用的零件信息（尺寸 + 当前摆放 + 所属轮廓） */
export interface LayoutPartInfo {
  id: number;
  name: string;
  width: number;
  height: number;
  polylineIds: number[];
  tx: number;
  ty: number;
  rot: number;
  locked: boolean;
  fixedRot: boolean;
}

/** 排料统计（后端计算，前端只展示） */
export interface LayoutStats {
  partCount: number;
  placementCount: number;
  lockedCount: number;
  fixedRotCount: number;
  overflowCount: number;
  usedArea: number;
  sheetArea: number;
  utilization: number;
  sheetWidth: number;
  sheetHeight: number;
  gap: number;
  margin: number;
}

/** 排料视图全量状态（来自 LAYOUT_STATE 帧） */
export interface LayoutState {
  sheet: SheetSpec;
  parts: LayoutPartInfo[];
  stats: LayoutStats;
}

export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export type TaskStatus = 'queued' | 'running' | 'done' | 'failed' | 'cancelled';

export interface TaskInfo {
  taskId: string;
  kind: string;
  status: TaskStatus;
  progress: number;
  message: string;
  createdAt?: number;
  finishedAt?: number | null;
}

export interface HealthInfo {
  status: string;
  version: string;
  geometryEngine: string;
  nestingEngine: string;
  clients: number;
  workers: number;
  dataDir: string;
}

/** 刀路/排料任务统计结果（后端原样回传，字段随业务增长） */
export interface TaskStats {
  [key: string]: number | string | null | undefined;
}

/** 单条轮廓的刀路明细（TOOLPATH_PREVIEW 的 meta.contours） */
export interface ToolpathContourInfo {
  id: number;
  isHole: boolean;
  closed: boolean;
  side: string;
  direction: string;
  segments: number;
  bridges: number;
  start: [number, number];
  cutLengthMm: number;
}

/** 微连接（桥）位置：切削到这里抬刀跨过，宽度即桥长 */
export interface ToolpathBridgeInfo {
  contourId: number;
  x: number;
  y: number;
  width: number;
}

/** 起割点（后端吸附到轮廓后的真实坐标） */
export interface ToolpathStartInfo {
  x: number;
  y: number;
}

/** 刀路预览明细：面板展示数据源/统计/图例，也用于“重新生成”判定来源 */
export interface ToolpathPreviewMeta {
  source: 'doc' | 'layout';
  sheet: SheetSpec | null;
  contours: ToolpathContourInfo[];
  bridges: ToolpathBridgeInfo[];
  startPoints: ToolpathStartInfo[];
  stats: TaskStats;
}

export type ConnectionState = 'connecting' | 'online' | 'offline';
