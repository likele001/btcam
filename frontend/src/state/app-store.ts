/**
 * 全局 UI 状态（zustand）。
 *
 * 只放"渲染需要"的数据：图层、轮廓、选中集、任务列表。
 * 几何计算的中间结果不放这里 —— 那是后端的事。
 */

import { create } from 'zustand';

import type {
  ExportResult,
  LogEntry,
  PostProcessorRecord,
  TemplateRecord,
  ToolpathParams,
  ToolpathStartPoint,
  ToolRecord,
  WorkOrderPost,
  WorkOrderSummary,
} from '@/net/rest';
import type {
  ConnectionState,
  GeometryIssue,
  HealthInfo,
  LayerInfo,
  LayoutPartInfo,
  LayoutState,
  PartInfo,
  Polyline,
  ScrapInfo,
  SheetSpec,
  TaskInfo,
  TaskStats,
  ToolpathPreviewMeta,
} from '@/types/geometry';

type ViewMode = 'doc' | 'preview' | 'layout';

/** 当前打开的工单：导出时自动把文件登记到它名下 */
export interface ActiveWorkOrder {
  id: number;
  name: string;
  post: WorkOrderPost | null;
}

/** 导出面板的临时选项：保存工单时一并固化 */
export interface ExportPreset {
  flipY: boolean;
  speed: number;
  force: number;
}

interface AppState {
  connection: ConnectionState;
  health: HealthInfo | null;
  projectId: number | null;
  layers: LayerInfo[];
  polylines: Polyline[];
  /** 零件列表：排料/锁定以零件为单位（由 GEOMETRY_DOC 的 meta 下发） */
  parts: PartInfo[];
  /** 图形校验告警明细（导入后由 GEOMETRY_ISSUES 推送） */
  issues: GeometryIssue[];
  /** 告警对应的异常线段，画布标红用 */
  issueSegments: Polyline[];
  /** doc = 原始图纸；preview = 刀路/排料结果；layout = 手动排料板 */
  viewMode: ViewMode;
  /** 排料视图元数据；非排料模式为 null */
  layout: LayoutState | null;
  /** 预览底板：整板刀路的板框（只用于绘制，不参与拖拽） */
  board: SheetSpec | null;
  /** 刀路表单参数（与后端 ToolpathRequest 对齐，不含选中集/起割点） */
  toolpathParams: ToolpathParams;
  /** 最近一次成功提交的刀路请求：导出复用，保证与预览一致 */
  toolpathRequest: ToolpathParams | null;
  /** 待提交的起割点（画布点选坐标，后端会吸附到最近轮廓） */
  startPoints: ToolpathStartPoint[];
  /** 是否处于“点选起割点”模式（画布光标的十字准线） */
  pickingStart: boolean;
  /** 作图环节：画布手绘多边形中（面板按钮据此变成“取消绘制”） */
  drawingShape: boolean;
  /** 顶点编辑中的零件 id；null = 不在编辑 */
  editingPartId: number | null;
  /** 最近一次刀路结果明细（轮廓/桥/吸附后的起割点/统计） */
  toolpathPreview: ToolpathPreviewMeta | null;
  /** 刀具库 / 工艺模板（模块 6）：全局缓存，面板与弹窗共用 */
  tools: ToolRecord[];
  templates: TemplateRecord[];
  toolLibraryOpen: boolean;
  /** 后处理（模块 7）：一份配置一台机床，弹窗与导出共用 */
  postProcessors: PostProcessorRecord[];
  postOpen: boolean;
  /** 后处理可视化编辑器（模块 11）：开关 + 打开时默认选中的机床 id */
  postEditorOpen: boolean;
  postEditorTarget: string;
  /** 最近一次导出的文件列表（多板批量时为多条） */
  lastExport: ExportResult | null;
  /** 工单存档（模块 8）：列表、弹窗开关、当前打开的工单 */
  workOrders: WorkOrderSummary[];
  workOrderOpen: boolean;
  activeWorkOrder: ActiveWorkOrder | null;
  exportPreset: ExportPreset;
  /** 运行日志（模块 9）：日志面板的数据源 */
  logs: LogEntry[];
  logOpen: boolean;
  /** 关于弹窗（版本信息）：生产包已关掉开发者工具，版本/数据目录/引擎状态都在这里看 */
  aboutOpen: boolean;
  /** MES 对接（模块 12）：设置页/工单列表弹窗开关 */
  mesOpen: boolean;
  /** 余料库（模块 10）：余料面板弹窗开关 */
  scrapOpen: boolean;
  /** 日志缓冲容量与磁盘文件路径（面板底部展示） */
  logCapacity: number;
  logFile: string;
  /** 待确认的告警队列：一次弹一条，关闭后自动弹下一条 */
  alerts: LogEntry[];
  selection: number[];
  tasks: TaskInfo[];
  lastStats: TaskStats | null;
  message: string;
  busy: boolean;

  setConnection: (state: ConnectionState) => void;
  setHealth: (health: HealthInfo) => void;
  applyDocument: (meta: Record<string, unknown>, polylines: Polyline[]) => void;
  applyPreview: (meta: Record<string, unknown>, polylines: Polyline[]) => void;
  applyGeometryResult: (meta: Record<string, unknown>, polylines: Polyline[]) => void;
  applyLayout: (meta: Record<string, unknown>, polylines: Polyline[]) => void;
  applyIssues: (meta: Record<string, unknown>, segments: Polyline[]) => void;
  setSelection: (ids: number[]) => void;
  toggleLayer: (id: number) => void;
  showAllLayers: () => void;
  upsertTask: (patch: Partial<TaskInfo> & { taskId: string }) => void;
  setStats: (stats: TaskStats | null) => void;
  setMessage: (text: string) => void;
  setBusy: (busy: boolean) => void;
  setToolpathParams: (patch: Partial<ToolpathParams>) => void;
  setToolpathRequest: (request: ToolpathParams | null) => void;
  addStartPoint: (point: ToolpathStartPoint) => void;
  removeStartPoint: (index: number) => void;
  clearStartPoints: () => void;
  setPickingStart: (picking: boolean) => void;
  setDrawingShape: (drawing: boolean) => void;
  setEditingPartId: (partId: number | null) => void;
  setTools: (tools: ToolRecord[]) => void;
  setTemplates: (templates: TemplateRecord[]) => void;
  setToolLibraryOpen: (open: boolean) => void;
  setPostProcessors: (processors: PostProcessorRecord[]) => void;
  setPostOpen: (open: boolean) => void;
  setPostEditorOpen: (open: boolean, target?: string) => void;
  setLastExport: (result: ExportResult | null) => void;
  setWorkOrders: (orders: WorkOrderSummary[]) => void;
  setWorkOrderOpen: (open: boolean) => void;
  setActiveWorkOrder: (order: ActiveWorkOrder | null) => void;
  setExportPreset: (patch: Partial<ExportPreset>) => void;
  setLogs: (logs: LogEntry[], meta?: { capacity?: number; file?: string }) => void;
  appendLog: (entry: LogEntry) => void;
  setLogOpen: (open: boolean) => void;
  setAboutOpen: (open: boolean) => void;
  setMesOpen: (open: boolean) => void;
  setScrapOpen: (open: boolean) => void;
  pushAlert: (entry: LogEntry) => void;
  dismissAlert: () => void;
}

const pickLayers = (meta: Record<string, unknown>): LayerInfo[] =>
  Array.isArray(meta.layers)
    ? (meta.layers as Array<Record<string, unknown>>).map((raw) => ({
      id: Number(raw.id),
      name: String(raw.name ?? '图层'),
      color: Number(raw.color ?? 0x9fb3c8),
      operation: String(raw.operation ?? 'cut'),
      dashed: raw.dashed === true || raw.dashed === 1,
      visible: raw.visible !== false && raw.visible !== 0,
      locked: raw.locked === true || raw.locked === 1,
    }))
    : [];

const pickParts = (meta: Record<string, unknown>): PartInfo[] =>
  Array.isArray(meta.parts)
    ? (meta.parts as Array<Record<string, unknown>>).map((raw) => ({
      id: Number(raw.id),
      name: String(raw.name ?? '零件'),
      source: String(raw.source ?? ''),
      bbox: (Array.isArray(raw.bbox) ? raw.bbox.map(Number) : [0, 0, 0, 0]) as [number, number, number, number],
      polylineIds: Array.isArray(raw.polylineIds) ? raw.polylineIds.map(Number) : [],
    }))
    : [];

const pickIssues = (meta: Record<string, unknown>): GeometryIssue[] =>
  Array.isArray(meta.items)
    ? (meta.items as Array<Record<string, unknown>>).map((raw) => ({
      kind: String(raw.kind ?? 'unknown'),
      message: String(raw.message ?? ''),
      polylineId: Number(raw.polylineId ?? 0),
      severity: String(raw.severity ?? 'warning'),
      point: (Array.isArray(raw.point) ? raw.point.map(Number) : [0, 0]) as [number, number],
    }))
    : [];

/** 余料（模块 10）：排料帧里带 scrap 时，边界用它而不是矩形板 */
const pickScrap = (meta: Record<string, unknown>): ScrapInfo | null => {
  const raw = meta.scrap;
  if (!raw || typeof raw !== 'object') return null;
  const data = raw as Record<string, unknown>;
  const outline = Array.isArray(data.outline)
    ? (data.outline as Array<Record<string, unknown> | number[]>)
      .map((point) => (Array.isArray(point)
        ? [Number(point[0] ?? 0), Number(point[1] ?? 0)] as [number, number]
        : [Number((point as Record<string, unknown>).x ?? 0),
          Number((point as Record<string, unknown>).y ?? 0)] as [number, number]))
    : [];
  if (outline.length < 3) return null;
  return {
    id: Number(data.id ?? 0),
    projectId: Number(data.projectId ?? 0),
    name: String(data.name ?? '余料'),
    code: String(data.code ?? ''),
    material: String(data.material ?? ''),
    thickness: Number(data.thickness ?? 0),
    width: Number(data.width ?? 0),
    height: Number(data.height ?? 0),
    area: Number(data.area ?? 0),
    outline,
    points: outline.length,
    source: String(data.source ?? ''),
    sourceOrderId: Number(data.sourceOrderId ?? 0),
    note: String(data.note ?? ''),
    createdAt: Number(data.createdAt ?? 0),
    updatedAt: Number(data.updatedAt ?? 0),
  };
};

const pickLayout = (meta: Record<string, unknown>): LayoutState | null => {
  // 板材参数缺失说明这不是排料帧（或旧版本后端），不当作排料状态处理
  if (!meta.sheet || typeof meta.sheet !== 'object') return null;
  const sheet = meta.sheet as Record<string, unknown>;
  const stats = (meta.stats ?? {}) as Record<string, unknown>;
  const parts = Array.isArray(meta.parts) ? (meta.parts as Array<Record<string, unknown>>) : [];
  return {
    sheet: {
      width: Number(sheet.width ?? 0),
      height: Number(sheet.height ?? 0),
      margin: Number(sheet.margin ?? 0),
      gap: Number(sheet.gap ?? 0),
      scrapId: Number(sheet.scrapId ?? 0),
    },
    scrap: pickScrap(meta),
    parts: parts.map((raw) => ({
      id: Number(raw.id),
      name: String(raw.name ?? '零件'),
      width: Number(raw.width ?? 0),
      height: Number(raw.height ?? 0),
      polylineIds: Array.isArray(raw.polylineIds) ? raw.polylineIds.map(Number) : [],
      tx: Number(raw.tx ?? 0),
      ty: Number(raw.ty ?? 0),
      rot: Number(raw.rot ?? 0),
      locked: raw.locked === true,
      fixedRot: raw.fixedRot === true,
    })),
    stats: {
      partCount: Number(stats.partCount ?? 0),
      placementCount: Number(stats.placementCount ?? 0),
      lockedCount: Number(stats.lockedCount ?? 0),
      fixedRotCount: Number(stats.fixedRotCount ?? 0),
      overflowCount: Number(stats.overflowCount ?? 0),
      usedArea: Number(stats.usedArea ?? 0),
      sheetArea: Number(stats.sheetArea ?? 0),
      utilization: Number(stats.utilization ?? 0),
      sheetWidth: Number(stats.sheetWidth ?? 0),
      sheetHeight: Number(stats.sheetHeight ?? 0),
      gap: Number(stats.gap ?? 0),
      margin: Number(stats.margin ?? 0),
      scrapId: Number(stats.scrapId ?? 0),
      boundaryArea: Number(stats.boundaryArea ?? 0),
    },
  };
};

/** 排料板规格（预览帧里用于画板框） */
const pickSheet = (meta: Record<string, unknown>): SheetSpec | null => {
  if (!meta.sheet || typeof meta.sheet !== 'object') return null;
  const sheet = meta.sheet as Record<string, unknown>;
  return {
    width: Number(sheet.width ?? 0),
    height: Number(sheet.height ?? 0),
    margin: Number(sheet.margin ?? 0),
    gap: Number(sheet.gap ?? 0),
  };
};

/**
 * 刀路预览明细。只有带 contours/source 的帧才是刀路结果，
 * 旧的排料预览帧（NEST_RESULT）不包成刀路，避免面板误显示。
 */
const pickToolpath = (meta: Record<string, unknown>): ToolpathPreviewMeta | null => {
  const hasSource = meta.source === 'doc' || meta.source === 'layout';
  if (!hasSource && !Array.isArray(meta.contours)) return null;

  const contours = Array.isArray(meta.contours)
    ? (meta.contours as Array<Record<string, unknown>>).map((raw) => ({
        id: Number(raw.id ?? 0),
        isHole: raw.isHole === true,
        closed: raw.closed !== false,
        side: String(raw.side ?? ''),
        direction: String(raw.direction ?? ''),
        segments: Number(raw.segments ?? 1),
        bridges: Number(raw.bridges ?? 0),
        start: (Array.isArray(raw.start) ? raw.start.map(Number) : [0, 0]) as [number, number],
        cutLengthMm: Number(raw.cutLengthMm ?? 0),
      }))
    : [];
  const bridges = Array.isArray(meta.bridges)
    ? (meta.bridges as Array<Record<string, unknown>>).map((raw) => ({
        contourId: Number(raw.contourId ?? 0),
        x: Number(raw.x ?? 0),
        y: Number(raw.y ?? 0),
        width: Number(raw.width ?? 0),
      }))
    : [];
  const startPoints = Array.isArray(meta.startPoints)
    ? (meta.startPoints as Array<Record<string, unknown>>).map((raw) => ({
        x: Number(raw.x ?? 0),
        y: Number(raw.y ?? 0),
      }))
    : [];

  return {
    source: meta.source === 'layout' ? 'layout' : 'doc',
    sheet: pickSheet(meta),
    contours,
    bridges,
    startPoints,
    stats: (meta.stats && typeof meta.stats === 'object' ? meta.stats : {}) as TaskStats,
  };
};

/** 面板最多保留多少条：环形缓冲在后端，这里只留最近的一屏量 */
export const MAX_LOG_ROWS = 500;
/** 告警弹窗最多排队几条：突发报错只弹最前面几条，其余去日志面板看 */
const MAX_ALERTS = 5;

/** 按 id 合并去重升序（ALERT 帧与轮询结果会重叠） */
const mergeLogs = (current: LogEntry[], incoming: LogEntry[]): LogEntry[] => {
  if (incoming.length === 0) return current;
  const byId = new Map<number, LogEntry>();
  current.forEach((item) => byId.set(item.id, item));
  incoming.forEach((item) => byId.set(item.id, item));
  return [...byId.values()].sort((a, b) => a.id - b.id).slice(-MAX_LOG_ROWS);
};

/** 刀路默认参数：与后端 ToolpathRequest 的默认值保持一致 */
export const DEFAULT_TOOLPATH_PARAMS: ToolpathParams = {
  source: 'doc',
  sheetIndex: 0,
  cutType: 'auto',
  direction: 'ccw',
  optimize: true,
  microJoint: false,
  microJointWidth: 2,
  microJointCount: 2,
  toolDiameter: 6,
  side: 'auto',
  depth: 18,
  passDepth: 3,
  feed: 3000,
  plungeFeed: 800,
  safeZ: 5,
  liftZ: 5,
  spindle: 18000,
  leadIn: true,
  leadRadius: 2,
};

/** 根据当前选中集反查被选中的零件（排料模式下选中以零件为单位） */
export function selectedLayoutParts(state: AppState): LayoutPartInfo[] {
  if (!state.layout || state.selection.length === 0) return [];
  const picked = new Set(state.selection);
  return state.layout.parts.filter((part) => part.polylineIds.some((id) => picked.has(id)));
}

export const useAppStore = create<AppState>((set) => ({
  connection: 'connecting',
  health: null,
  projectId: null,
  layers: [],
  polylines: [],
  parts: [],
  issues: [],
  issueSegments: [],
  viewMode: 'doc',
  layout: null,
  board: null,
  toolpathParams: DEFAULT_TOOLPATH_PARAMS,
  toolpathRequest: null,
  startPoints: [],
  pickingStart: false,
  drawingShape: false,
  editingPartId: null,
  toolpathPreview: null,
  tools: [],
  templates: [],
  toolLibraryOpen: false,
  postProcessors: [],
  postOpen: false,
  postEditorOpen: false,
  postEditorTarget: '',
  lastExport: null,
  workOrders: [],
  workOrderOpen: false,
  activeWorkOrder: null,
  exportPreset: { flipY: false, speed: 30, force: 20 },
  logs: [],
  logOpen: false,
  aboutOpen: false,
  mesOpen: false,
  scrapOpen: false,
  logCapacity: 0,
  logFile: '',
  alerts: [],
  selection: [],
  tasks: [],
  lastStats: null,
  message: '正在连接后端…',
  busy: false,

  setConnection: (connection) => set({ connection }),

  setHealth: (health) => set({ health }),

  applyDocument: (meta, polylines) =>
    set((state) => ({
      polylines,
      layers: pickLayers(meta).length > 0 ? pickLayers(meta) : state.layers,
      parts: pickParts(meta),
      // 后端导入流程是“先推 GEOMETRY_DOC 再推 GEOMETRY_ISSUES”，
      // 所以这里清空旧告警不会丢掉本次导入的标红结果。
      issues: [],
      issueSegments: [],
      projectId: meta.projectId ? Number(meta.projectId) : state.projectId,
      viewMode: 'doc',
      layout: null,
      board: null,
      toolpathRequest: null,
      startPoints: [],
      pickingStart: false,
      toolpathPreview: null,
      selection: [],
      message: `已加载 ${polylines.length} 条轮廓`,
    })),

  applyIssues: (meta, segments) =>
    set({
      issues: pickIssues(meta),
      issueSegments: segments,
    }),

  applyPreview: (meta, polylines) =>
    set((state) => {
      const nextLayers = pickLayers(meta);
      const toolpath = pickToolpath(meta);
      return {
        polylines,
        layers: nextLayers.length > 0 ? nextLayers : state.layers,
        viewMode: 'preview',
        layout: null,
        board: pickSheet(meta),
        toolpathPreview: toolpath,
        // 起割点以本次吸附结果为准；非刀路结果（旧排料预览）保留用户点选
        startPoints: toolpath
          ? toolpath.startPoints.map((point) => ({ x: point.x, y: point.y }))
          : state.startPoints,
        pickingStart: false,
        message: toolpath
          ? `刀路预览：${toolpath.contours.length} 条轮廓`
          : `预览：${polylines.length} 条结果轮廓`,
      };
    }),

  /**
   * 几何运算结果（offset / intersect / merge）：只换一份轮廓，
   * 不是刀路预览 —— 走 applyPreview 会把视图切成 preview、
   * 左面板也跟着变成刀路参数，与用户刚做的操作对不上。
   */
  applyGeometryResult: (meta, polylines) =>
    set((state) => {
      const stats = (meta.stats ?? {}) as Record<string, unknown>;
      const labels: Record<string, string> = {
        offset: '偏置', intersect: '求交', merge: '断线合并',
      };
      const label = labels[String(stats.op ?? '')] ?? '几何运算';
      return {
        polylines,
        layers: pickLayers(meta).length > 0 ? pickLayers(meta) : state.layers,
        viewMode: 'doc',
        layout: null,
        board: null,
        issues: [],
        issueSegments: [],
        toolpathRequest: null,
        startPoints: [],
        pickingStart: false,
        toolpathPreview: null,
        selection: [],
        message: `${label}完成：${polylines.length} 条轮廓`,
      };
    }),

  applyLayout: (meta, polylines) =>
    set((state) => {
      const layout = pickLayout(meta);
      // 排料帧没带图层：沿用现有图层表（板 1 用图层 1 的颜色）
      const nextLayers = pickLayers(meta);
      const utilization = layout ? Math.round(layout.stats.utilization * 1000) / 10 : 0;
      // 图纸轮廓与排料轮廓是两套 id：只保留本次帧里仍存在的选中项。
      // 不能每帧清空 —— 连续旋转/锁定时每写一次都广播，清空会让快捷键失活。
      const alive = new Set(polylines.map((poly) => poly.id));
      return {
        polylines,
        layers: nextLayers.length > 0 ? nextLayers : state.layers,
        layout,
        viewMode: 'layout',
        board: null,
        toolpathRequest: null,
        startPoints: [],
        pickingStart: false,
        toolpathPreview: null,
        selection: state.selection.filter((id) => alive.has(id)),
        issues: [],
        issueSegments: [],
        projectId: meta.projectId ? Number(meta.projectId) : state.projectId,
        // 只在切入排料模式时改消息；之后每帧都改会盖掉用户的失败提示
        message: state.viewMode === 'layout'
          ? state.message
          : `排料：${layout?.stats.partCount ?? 0} 个零件 · 利用率 ${utilization}%`,
      };
    }),

  setSelection: (selection) => set({ selection }),

  toggleLayer: (id) =>
    set((state) => ({
      layers: state.layers.map((layer) =>
        layer.id === id ? { ...layer, visible: !layer.visible } : layer),
    })),

  showAllLayers: () =>
    set((state) => ({ layers: state.layers.map((layer) => ({ ...layer, visible: true })) })),

  upsertTask: (patch) =>
    set((state) => {
      const index = state.tasks.findIndex((task) => task.taskId === patch.taskId);
      if (index === -1) {
        const created: TaskInfo = {
          taskId: patch.taskId,
          kind: patch.kind ?? 'unknown',
          status: patch.status ?? 'queued',
          progress: patch.progress ?? 0,
          message: patch.message ?? '',
          createdAt: patch.createdAt ?? Date.now(),
          finishedAt: patch.finishedAt ?? null,
        };
        return { tasks: [created, ...state.tasks].slice(0, 30) };
      }
      const next = [...state.tasks];
      next[index] = { ...next[index], ...patch };
      return { tasks: next };
    }),

  setStats: (lastStats) => set({ lastStats }),

  setMessage: (message) => set({ message }),

  setBusy: (busy) => set({ busy }),

  setToolpathParams: (patch) =>
    set((state) => ({ toolpathParams: { ...state.toolpathParams, ...patch } })),

  setToolpathRequest: (toolpathRequest) => set({ toolpathRequest }),

  addStartPoint: (point) =>
    set((state) => ({ startPoints: [...state.startPoints, point] })),

  removeStartPoint: (index) =>
    set((state) => ({ startPoints: state.startPoints.filter((_, i) => i !== index) })),

  clearStartPoints: () => set({ startPoints: [] }),

  setPickingStart: (pickingStart) => set({ pickingStart }),

  setDrawingShape: (drawingShape) => set({ drawingShape }),

  setEditingPartId: (editingPartId) => set({ editingPartId }),

  setTools: (tools) => set({ tools }),

  setTemplates: (templates) => set({ templates }),

  setToolLibraryOpen: (toolLibraryOpen) => set({ toolLibraryOpen }),

  setPostProcessors: (postProcessors) => set({ postProcessors }),

  setPostOpen: (postOpen) => set({ postOpen }),

  // 打开编辑器时一并记住要编辑哪台机床（'' = 从第一台开始）
  setPostEditorOpen: (postEditorOpen, target) => set((state) => ({
    postEditorOpen,
    postEditorTarget: target ?? state.postEditorTarget,
  })),

  setLastExport: (lastExport) => set({ lastExport }),

  setWorkOrders: (workOrders) => set({ workOrders }),

  setWorkOrderOpen: (workOrderOpen) => set({ workOrderOpen }),

  setActiveWorkOrder: (activeWorkOrder) => set({ activeWorkOrder }),

  setExportPreset: (patch) =>
    set((state) => ({ exportPreset: { ...state.exportPreset, ...patch } })),

  setLogs: (logs, meta) =>
    set((state) => ({
      logs: logs.slice(-MAX_LOG_ROWS),
      logCapacity: meta?.capacity ?? state.logCapacity,
      logFile: meta?.file ?? state.logFile,
    })),

  appendLog: (entry) =>
    set((state) => ({ logs: mergeLogs(state.logs, [entry]) })),

  setLogOpen: (logOpen) => set({ logOpen }),

  setAboutOpen: (aboutOpen) => set({ aboutOpen }),

  setMesOpen: (mesOpen) => set({ mesOpen }),

  /** 余料库弹窗（模块 10）：与 MES 一样是独立浮层，不切换左侧面板 */
  setScrapOpen: (scrapOpen) => set({ scrapOpen }),

  pushAlert: (entry) =>
    set((state) => {
      // 同一 id 重复到达（重连补发）不重复入队
      if (state.alerts.some((item) => item.id === entry.id)) return {};
      return { alerts: [...state.alerts, entry].slice(-MAX_ALERTS) };
    }),

  dismissAlert: () =>
    set((state) => ({ alerts: state.alerts.slice(1) })),
}));
