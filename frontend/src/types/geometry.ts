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
  /** 非 0 表示当前板材是余料库里的某块余料，轮廓即真实边界 */
  scrapId?: number;
}

/**
 * 余料（模块 10）：排板切割后剩下的料。
 *
 * outline 的坐标相对**自身包围盒左下角**，与板材坐标同构，
 * 所以直接当板材用时不需要任何平移；几何全部由后端算好。
 */
export interface ScrapInfo {
  id: number;
  projectId: number;
  name: string;
  code: string;
  material: string;
  thickness: number;
  /** 包围盒宽高（mm） */
  width: number;
  height: number;
  /** 轮廓净面积（mm²） */
  area: number;
  /** 闭合多边形点集（不重复首点） */
  outline: Array<[number, number]>;
  points: number;
  /** manual = 手工录入；layout = 排板剩余区域保存 */
  source: string;
  sourceOrderId: number;
  note: string;
  createdAt: number;
  updatedAt: number;
}

/** 排板剩余区域：outline 是**板材坐标**，前端只画预览与勾选 */
export interface ScrapRegion {
  index: number;
  area: number;
  width: number;
  height: number;
  outline: Array<[number, number]>;
  points: number;
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
  /** 当前板材绑定的余料 id（0 = 规则整板） */
  scrapId?: number;
  /** 真实可用边界面积：余料轮廓面积或矩形板面积 */
  boundaryArea?: number;
}

/** 排料视图全量状态（来自 LAYOUT_STATE 帧） */
export interface LayoutState {
  sheet: SheetSpec;
  parts: LayoutPartInfo[];
  stats: LayoutStats;
  /** 余料作为板材时下发（含轮廓），否则为 null */
  scrap?: ScrapInfo | null;
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

/** 刀路/排料任务统计结果（后端原样回传，字段随业务增长；排料的 rotation 是布尔） */
export interface TaskStats {
  [key: string]: number | string | boolean | null | undefined;
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
