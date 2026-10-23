/**
 * REST 客户端：只负责"提交任务/查状态/导出"这类一次性的请求。
 * 图形数据绝不走这里 —— 见 net/protocol.ts。
 */

import type { HealthInfo, LayoutPlacement, SheetSpec, TaskStats } from '@/types/geometry';

/** 后端地址：开发环境用默认端口，打包后由 Tauri 注入同端口 */
const BACKEND = (import.meta.env.VITE_BACKEND_URL as string | undefined) ?? 'http://127.0.0.1:8975';

export const WS_URL = `${BACKEND.replace(/^http/, 'ws')}/ws`;
export const API_BASE = BACKEND;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
  if (!response.ok) {
    const text = await response.text().catch(() => '');
    throw new Error(`${response.status} ${response.statusText} ${text}`.trim());
  }
  return (await response.json()) as T;
}

export interface TaskAccepted {
  taskId: string;
  kind: string;
  status: string;
}

/** 手工起割点：画布点一下即可，后端自动吸附到最近轮廓 */
export interface ToolpathStartPoint {
  x: number;
  y: number;
  /** 可选：指定吸附到哪条轮廓（不传则全局最近） */
  contourId?: number;
}

export type CutDirection = 'ccw' | 'cw' | 'climb' | 'conventional';
export type ToolCategory = 'mill' | 'wire';
export type MillToolType = 'flat' | 'ball' | 'v';
export type ToolSide = 'auto' | 'outside' | 'inside' | 'on';

export interface ToolpathParams {
  projectId?: number;
  ids?: number[];
  layerIds?: number[];
  contours?: Array<{ id: number; layerId: number; closed: boolean; pts: number[][] }>;
  /** doc = 图纸轮廓；layout = 当前排料板整板（模块 5） */
  source?: 'doc' | 'layout';
  sheetIndex?: number;
  /** auto = 内外轮廓都做；outer/inner 只做其中一类 */
  cutType?: 'auto' | 'outer' | 'inner';
  /** ccw/cw 强制点序；climb 顺铣、conventional 逆铣由后端按内外轮廓映射 */
  direction?: CutDirection;
  optimize?: boolean;
  startPoints?: ToolpathStartPoint[];
  microJoint?: boolean;
  microJointWidth?: number;
  microJointCount?: number;
  toolDiameter: number;
  /** 刀具库 id：绑定后刀具参数优先于表单值 */
  toolId?: number | null;
  /** 工艺模板 id：补偿/微连接按模板 */
  templateId?: number | null;
  /** auto = 外轮廓向外、内孔向内；outside/inside/on 为强制值 */
  side: ToolSide;
  depth: number;
  passDepth: number;
  feed: number;
  plungeFeed: number;
  safeZ: number;
  /** 抬刀高度：退刀到该 Z，缺省与 safeZ 相同 */
  liftZ?: number;
  spindle: number;
  leadIn: boolean;
  leadRadius: number;
}

// ---------------------------------------------------------------- 刀具库 / 工艺模板（模块 6）
/** 刀具记录：铣刀用 diameter/tipRadius，钼丝用 wireDiameter/dischargeGap */
export interface ToolRecord {
  id: number;
  projectId: number;
  name: string;
  category: ToolCategory;
  toolType: MillToolType;
  diameter: number;
  tipRadius: number;
  wireDiameter: number;
  dischargeGap: number;
  feed: number;
  plungeFeed: number;
  spindle: number;
  passDepth: number;
  safeZ: number;
  liftZ: number;
  direction: 'climb' | 'conventional';
  note: string;
  /** 后端算好的等效补偿直径（钼丝 = 丝径 + 2×间隙） */
  effectiveDiameter: number;
  compensationRadius: number;
  updatedAt: number;
}

export interface ToolInput {
  projectId?: number;
  name: string;
  category: ToolCategory;
  toolType: MillToolType;
  diameter: number;
  tipRadius: number;
  wireDiameter: number;
  dischargeGap: number;
  feed: number;
  plungeFeed: number;
  spindle: number;
  passDepth: number;
  safeZ: number;
  liftZ: number;
  direction: 'climb' | 'conventional';
  note: string;
}

/** 模板只保存补偿/微连接参数，不含刀具与切深 */
export interface TemplateParams {
  cutType: 'auto' | 'outer' | 'inner';
  side: ToolSide;
  optimize: boolean;
  microJoint: boolean;
  microJointWidth: number;
  microJointCount: number;
  leadIn: boolean;
  leadRadius: number;
}

export interface TemplateInput {
  projectId?: number;
  name: string;
  material: string;
  toolId: number | null;
  params: TemplateParams;
  note: string;
}

export interface TemplateRecord extends TemplateInput {
  id: number;
  projectId: number;
  createdAt: number;
  updatedAt: number;
  /** 模板绑定的刀具详情；刀具被删后为 null */
  tool: ToolRecord | null;
}

export interface ToolListResponse {
  projectId: number;
  tools: ToolRecord[];
}

export interface TemplateListResponse {
  projectId: number;
  templates: TemplateRecord[];
}

// ---------------------------------------------------------------- 后处理（模块 7）
export type PostKind = 'gcode' | 'hpgl' | 'dxf';

/** G 代码配置：header/footer/toolChange 是“一行一条”的模板数组 */
export interface GcodePostConfig {
  precision: number;
  lineNumbers: boolean;
  lineNumberStart: number;
  lineNumberStep: number;
  programNo: string;
  header: string[];
  footer: string[];
  toolChange: string[];
  rapidZ: string;
  rapidXY: string;
  plungeZ: string;
  feed: string;
  cutXY: string;
}

/** HPGL/裁割配置：坐标乘以 unitsPerMm 取整 */
export interface HpglPostConfig {
  unitsPerMm: number;
  header: string[];
  footer: string[];
  penUp: string;
  penDown: string;
  penLift: string;
}

export interface DxfPostConfig {
  version: string;
}

/** 一份后处理 = 一台机床；内置配置只读，自定义可增删改 */
export interface PostProcessorRecord {
  id: string;
  name: string;
  kind: PostKind;
  ext: string;
  description: string;
  builtin: boolean;
  version: number;
  gcode?: GcodePostConfig;
  hpgl?: HpglPostConfig;
  dxf?: DxfPostConfig;
}

/** 新建/修改后处理的请求体（builtin/version 由后端维护） */
export interface PostProcessorInput {
  id: string;
  name: string;
  kind: PostKind;
  ext: string;
  description: string;
  gcode?: GcodePostConfig;
  hpgl?: HpglPostConfig;
  dxf?: DxfPostConfig;
}

export interface PostListResponse {
  processors: PostProcessorRecord[];
}

/** 导出单个文件的结果；多板批量时 files 有多条 */
export interface ExportFileInfo {
  path: string;
  format: string;
  processor: string;
  bytes: number;
  sheetIndex?: number;
  contours?: number;
  stats?: TaskStats;
}

export interface ExportResult {
  path: string;
  format: string;
  processor: string;
  files: ExportFileInfo[];
  stats: TaskStats;
  tool: ToolRecord | null;
  template: TemplateRecord | null;
}

/** 导出请求：在刀路参数上追加后处理与批量选项 */
export interface ExportParams extends ToolpathParams {
  format: 'nc' | 'plt' | 'cut' | 'dxf';
  /** 指定后处理；不传时按 format 走内置映射 */
  processorId?: string;
  name?: string;
  flipY?: boolean;
  speed?: number;
  force?: number;
  /** 排料来源时：每块板一个文件 */
  allSheets?: boolean;
  /** 关联工单：导出后由后端自动登记加工文件路径 */
  workOrderId?: number;
}

// ---------------------------------------------------------------- 工单存档（模块 8）
export interface WorkOrderStats {
  partCount: number;
  polylineCount: number;
  pointCount: number;
  layerCount: number;
  placementCount: number;
  utilization: number;
  sheetWidth: number;
  sheetHeight: number;
}

/** 列表行：不含快照，只带冗余统计与文件数 */
export interface WorkOrderSummary {
  id: number;
  name: string;
  code: string;
  note: string;
  createdAt: number;
  updatedAt: number;
  stats: WorkOrderStats;
  fileCount: number;
}

export interface WorkOrderFile {
  id: number;
  orderId: number;
  path: string;
  format: string;
  processor: string;
  bytes: number;
  sheetIndex: number;
  createdAt: number;
}

/** 工单里固化的后处理选择 */
export interface WorkOrderPost {
  processorId: string | null;
  format: 'nc' | 'plt' | 'cut' | 'dxf';
  name: string;
  flipY: boolean;
  speed: number;
  force: number;
}

/** 详情：打开工单后前端按 toolpath/post 回填面板 */
export interface WorkOrderDetail extends WorkOrderSummary {
  version: number;
  toolpath: Partial<ToolpathParams>;
  post: WorkOrderPost;
  tool: ToolRecord | null;
  template: TemplateRecord | null;
  files: WorkOrderFile[];
  /** 刀具/模板已不在库时给出提示，参数里对应 id 已置空 */
  missing: string[];
}

export interface WorkOrderSaveParams {
  projectId?: number;
  name: string;
  code?: string;
  note?: string;
  toolpath?: Partial<ToolpathParams>;
  post?: Partial<WorkOrderPost>;
  /** PUT 时 true 表示把当前工程重新拍成快照写回 */
  recapture?: boolean;
}

export interface WorkOrderListParams {
  keyword?: string;
  dateFrom?: string;
  dateTo?: string;
  limit?: number;
}

/** 运行日志（模块 9）：后端环形缓冲里的一条结构化记录 */
export type LogLevel = 'debug' | 'info' | 'warning' | 'error' | 'critical';

export interface LogEntry {
  /** 自增 id：前端据此去重与增量拉取 */
  id: number;
  /** Unix 秒 */
  time: number;
  level: LogLevel;
  /** 来源：任务/模块名，前端上报为组件名 */
  source: string;
  message: string;
  taskId: string | null;
  /** 业务分类，如 geometry_check / frontend */
  kind: string | null;
  /** 异常堆栈或明细，面板里展开看 */
  detail: string | null;
}

export interface LogListParams {
  /** 最低级别过滤 */
  level?: LogLevel | '';
  keyword?: string;
  /** 来源前缀，如 task / import / frontend */
  source?: string;
  limit?: number;
  after?: number;
}

export interface LogListResponse {
  logs: LogEntry[];
  total: number;
  /** 缓冲里最新的 id，供轮询判断是否有新条目 */
  seq: number;
  capacity: number;
  /** 磁盘日志路径，面板底部展示 */
  file: string;
}

export interface NestParams {
  projectId?: number;
  sheetWidth: number;
  sheetHeight: number;
  gap: number;
  rotation: boolean;
  engine: 'auto' | 'deepnest' | 'shelf';
  layerIds?: number[];
  partIds?: number[];
}

/** DXF 导入的可调参数；全部可选，不传走后端默认值 */
export interface ImportOptions {
  read?: {
    flattenDistance?: number;
    unitScale?: number | null;
    skipOffLayers?: boolean;
    explodeBlocks?: boolean;
    includeHatch?: boolean;
  };
  repair?: {
    mergeTol?: number;
    minLength?: number;
    minArea?: number;
    closeGap?: number;
    removeOverlap?: boolean;
    checkIssues?: boolean;
  };
  /** true = 先清空当前工程图形（默认），false = 追加 */
  replace?: boolean;
  /** true = 按包含关系拆成多个零件（默认） */
  splitParts?: boolean;
}

/** 排料写操作的结果：ok=false 时 reason 给用户提示 */
export interface LayoutActionResult {
  ok: boolean;
  reason: string;
  stats?: Record<string, number>;
  sheet?: SheetSpec;
  /** 失败时回带当前真实摆放，前端据此回弹 */
  placements?: LayoutPlacement[];
}

export interface SheetParams {
  projectId?: number;
  width: number;
  height: number;
  margin: number;
  gap: number;
}

/** 自动排料（模块 4）：在排料板上重排零件；板材参数与零件列表可选 */
export interface LayoutNestParams {
  projectId?: number;
  engine: 'auto' | 'deepnest' | 'scan';
  /** 是否允许 90° 旋转；木纹锁定的零件始终不转 */
  rotation: boolean;
  /** 板材参数：不传则沿用当前排料板 */
  width?: number;
  height?: number;
  margin?: number;
  gap?: number;
  /** 只重排这些零件；不传/为空表示全部未锁定零件 */
  partIds?: number[];
}

/** 任务取消的返回：ok=false 时由 HTTP 状态码带出原因 */
export interface TaskCancelResult {
  ok: boolean;
  taskId: string;
  kind: string;
  status: string;
}

export const api = {
  health: () => request<HealthInfo>('/api/health'),

  project: () => request<Record<string, unknown>>('/api/project'),

  loadDemo: () => request<{ polylines: number; projectId: number }>('/api/demo', { method: 'POST' }),

  tasks: () => request<{ tasks: unknown[] }>('/api/tasks'),

  /** 取消排队/运行中的任务（自动排料等长任务） */
  cancelTask: (taskId: string) =>
    request<TaskCancelResult>(`/api/tasks/${encodeURIComponent(taskId)}/cancel`,
      { method: 'POST' }),

  offset: (payload: { projectId?: number; ids?: number[]; distance: number; join?: string }) =>
    request<TaskAccepted>('/api/offset', { method: 'POST', body: JSON.stringify(payload) }),

  nest: (payload: NestParams) =>
    request<TaskAccepted>('/api/nest', { method: 'POST', body: JSON.stringify(payload) }),

  toolpath: (payload: ToolpathParams) =>
    request<TaskAccepted>('/api/toolpath', { method: 'POST', body: JSON.stringify(payload) }),

  /** 刀具库（模块 6）：增删改查；刀路通过 toolId 引用 */
  tools: {
    list: (projectId?: number) =>
      request<ToolListResponse>(`/api/tools${projectId ? `?projectId=${projectId}` : ''}`),

    create: (payload: ToolInput) =>
      request<ToolRecord>('/api/tools', { method: 'POST', body: JSON.stringify(payload) }),

    update: (id: number, payload: ToolInput) =>
      request<ToolRecord>(`/api/tools/${id}`, { method: 'PUT', body: JSON.stringify(payload) }),

    remove: (id: number, projectId?: number) =>
      request<{ deleted: boolean; id: number }>(
        `/api/tools/${id}${projectId ? `?projectId=${projectId}` : ''}`, { method: 'DELETE' }),
  },

  /** 工艺模板：一把刀 + 一组补偿参数，按物料分类一键加载 */
  templates: {
    list: (projectId?: number) =>
      request<TemplateListResponse>(
        `/api/templates${projectId ? `?projectId=${projectId}` : ''}`),

    create: (payload: TemplateInput) =>
      request<TemplateRecord>('/api/templates',
        { method: 'POST', body: JSON.stringify(payload) }),

    update: (id: number, payload: TemplateInput) =>
      request<TemplateRecord>(`/api/templates/${id}`,
        { method: 'PUT', body: JSON.stringify(payload) }),

    remove: (id: number, projectId?: number) =>
      request<{ deleted: boolean; id: number }>(
        `/api/templates/${id}${projectId ? `?projectId=${projectId}` : ''}`,
        { method: 'DELETE' }),
  },

  /**
   * 导入 DXF（可多文件批量）。
   *
   * 注意：这里必须清掉默认的 JSON Content-Type，交给浏览器自动生成
   * multipart boundary，否则后端解析不出文件。
   */
  importDxf: (files: File[], options: ImportOptions = {}, projectId?: number) => {
    const form = new FormData();
    files.forEach((file) => form.append('files', file, file.name));
    form.append('options', JSON.stringify(options));
    if (projectId) form.append('projectId', String(projectId));
    return request<TaskAccepted>('/api/import/dxf', {
      method: 'POST',
      body: form,
      headers: {},
    });
  },

  importDefaults: () => request<ImportOptions>('/api/import/defaults'),

  exportFile: (payload: ExportParams) =>
    request<ExportResult>('/api/export', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  exportDir: () => request<{ dir: string }>('/api/export/dir'),

  /** 工单存档（模块 8）：本地 SQLite 快照，可检索、重新打开、另存 */
  workorders: {
    list: (params: WorkOrderListParams = {}) => {
      const query = new URLSearchParams();
      if (params.keyword) query.set('keyword', params.keyword);
      if (params.dateFrom) query.set('dateFrom', params.dateFrom);
      if (params.dateTo) query.set('dateTo', params.dateTo);
      if (params.limit) query.set('limit', String(params.limit));
      const suffix = query.toString();
      return request<{ orders: WorkOrderSummary[]; total: number }>(
        `/api/workorders${suffix ? `?${suffix}` : ''}`);
    },

    get: (id: number) => request<WorkOrderDetail>(`/api/workorders/${id}`),

    save: (payload: WorkOrderSaveParams) =>
      request<WorkOrderDetail>('/api/workorders', {
        method: 'POST',
        body: JSON.stringify(payload),
      }),

    update: (id: number, payload: WorkOrderSaveParams) =>
      request<WorkOrderDetail>(`/api/workorders/${id}`, {
        method: 'PUT',
        body: JSON.stringify(payload),
      }),

    remove: (id: number) =>
      request<{ deleted: boolean; id: number }>(`/api/workorders/${id}`,
        { method: 'DELETE' }),

    duplicate: (id: number, name = '') =>
      request<WorkOrderDetail>(`/api/workorders/${id}/duplicate`, {
        method: 'POST',
        body: JSON.stringify({ name }),
      }),

    /** 打开：后端把快照写回当前工程并广播全量图形 */
    open: (id: number) =>
      request<WorkOrderDetail>(`/api/workorders/${id}/open`, { method: 'POST' }),

    files: (id: number) =>
      request<{ files: WorkOrderFile[]; total: number }>(`/api/workorders/${id}/files`),
  },

  /** 后处理（模块 7）：一份 JSON 配置一台机床，内置配置只读 */
  posts: {
    list: () => request<PostListResponse>('/api/post'),

    get: (id: string) =>
      request<PostProcessorRecord>(`/api/post/${encodeURIComponent(id)}`),

    create: (payload: PostProcessorInput) =>
      request<PostProcessorRecord>('/api/post', {
        method: 'POST',
        body: JSON.stringify(payload),
      }),

    update: (id: string, payload: PostProcessorInput) =>
      request<PostProcessorRecord>(`/api/post/${encodeURIComponent(id)}`, {
        method: 'PUT',
        body: JSON.stringify(payload),
      }),

    remove: (id: string) =>
      request<{ deleted: boolean; id: string }>(
        `/api/post/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  },

  /** 运行日志（模块 9）：面板轮询查询、清空缓冲、前端异常上报 */
  logs: {
    list: (params: LogListParams = {}) => {
      const query = new URLSearchParams();
      if (params.level) query.set('level', params.level);
      if (params.keyword) query.set('keyword', params.keyword);
      if (params.source) query.set('source', params.source);
      if (params.limit) query.set('limit', String(params.limit));
      if (params.after) query.set('after', String(params.after));
      const suffix = query.toString();
      return request<LogListResponse>(`/api/logs${suffix ? `?${suffix}` : ''}`);
    },

    clear: () =>
      request<{ cleared: number; capacity: number }>('/api/logs', { method: 'DELETE' }),

    /** 前端异常上报：后端入缓冲 + 写文件，error 级还会广播 ALERT */
    report: (payload: {
      level?: 'info' | 'warning' | 'error';
      message: string;
      detail?: string;
      kind?: string;
      source?: string;
    }) =>
      request<{ ok: boolean; id: number; time: number; level: string; seq: number }>(
        '/api/logs/client', { method: 'POST', body: JSON.stringify(payload) }),
  },

  /** 排料（模块 3）：进模式 / 改板材 / 拖拽 / 旋转 / 锁定 / 复位 */
  layout: {
    open: (projectId?: number) =>
      request<LayoutActionResult>(`/api/layout/open${projectId ? `?projectId=${projectId}` : ''}`,
        { method: 'POST' }),

    sheet: (payload: SheetParams) =>
      request<LayoutActionResult>('/api/layout/sheet', {
        method: 'POST',
        body: JSON.stringify(payload),
      }),

    place: (payload: { projectId?: number; partId: number; tx: number; ty: number }) =>
      request<LayoutActionResult>('/api/layout/place', {
        method: 'POST',
        body: JSON.stringify(payload),
      }),

    rotate: (payload: { projectId?: number; partId: number; rot: number }) =>
      request<LayoutActionResult>('/api/layout/rotate', {
        method: 'POST',
        body: JSON.stringify(payload),
      }),

    lock: (payload: { projectId?: number; partId: number; locked: boolean }) =>
      request<LayoutActionResult>('/api/layout/lock', {
        method: 'POST',
        body: JSON.stringify(payload),
      }),

    fixedRot: (payload: { projectId?: number; partId: number; fixedRot: boolean }) =>
      request<LayoutActionResult>('/api/layout/fixed-rot', {
        method: 'POST',
        body: JSON.stringify(payload),
      }),

    /** 自动排料：异步任务，完成后由后端广播 LAYOUT_STATE */
    nest: (payload: LayoutNestParams) =>
      request<TaskAccepted>('/api/layout/nest', {
        method: 'POST',
        body: JSON.stringify(payload),
      }),

    reset: (projectId?: number) =>
      request<LayoutActionResult>(`/api/layout/reset${projectId ? `?projectId=${projectId}` : ''}`,
        { method: 'POST' }),
  },
};
