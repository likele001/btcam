/**
 * 后处理草稿工具（模块 7 配置页 / 模块 11 可视化编辑器共用）。
 *
 * 这里只有"整份配置怎么复制、空模板长什么样、新 id 怎么起名"这类纯数据处理，
 * 不含校验与渲染：校验/预览都由后端 post.editor 完成，前端不复制规则。
 */

import type {
  GcodePostConfig,
  HpglPostConfig,
  PostKind,
  PostProcessorRecord,
} from '@/net/rest';

export const KIND_LABEL: Record<PostKind, string> = {
  gcode: 'G 代码',
  hpgl: 'HPGL 笔式',
  dxf: 'DXF 图形',
};

export const KIND_EXT: Record<PostKind, string> = {
  gcode: 'nc',
  hpgl: 'plt',
  dxf: 'dxf',
};

/** 新建 gcode 配置时的最小可用模板：五个指令槽位必须给值，否则后端校验不过 */
export const BLANK_GCODE: GcodePostConfig = {
  precision: 3,
  lineNumbers: false,
  lineNumberStart: 10,
  lineNumberStep: 10,
  programNo: '0001',
  header: [
    '%',
    '( BTCAM - {name} )',
    '( Generated {date} )',
    'G21',
    'G90',
    'G17',
    'G40',
    'G0 Z{safeZ}',
    'M3 S{spindle}',
  ],
  footer: ['G0 Z{safeZ}', 'M5', 'M30', '%'],
  toolChange: ['T{tool} M6', 'G43 H{tool}'],
  rapidZ: 'G0 Z{z}',
  rapidXY: 'G0 X{x} Y{y}',
  plungeZ: 'G1 Z{z} F{feed}',
  feed: 'F{feed}',
  cutXY: 'G1 X{x} Y{y}',
};

export const BLANK_HPGL: HpglPostConfig = {
  unitsPerMm: 40,
  header: ['IN;', 'SP1;'],
  footer: ['SP0;'],
  penUp: 'PU{x},{y};',
  penDown: 'PD{x},{y};',
  penLift: 'PU;',
};

/** 深拷贝一份配置：面板编辑的是草稿，未保存前不能碰到 store 里的原始对象 */
export function cloneProcessor(record: PostProcessorRecord): PostProcessorRecord {
  return {
    ...record,
    gcode: record.gcode
      ? {
        ...record.gcode,
        header: [...record.gcode.header],
        footer: [...record.gcode.footer],
        toolChange: [...record.gcode.toolChange],
      }
      : undefined,
    hpgl: record.hpgl
      ? { ...record.hpgl, header: [...record.hpgl.header], footer: [...record.hpgl.footer] }
      : undefined,
    dxf: record.dxf ? { ...record.dxf } : undefined,
  };
}

/** 另存为时生成一个不冲突的 id（后端限制 2~40 位小写字母/数字/下划线） */
export function suggestId(base: string, processors: PostProcessorRecord[]): string {
  const taken = new Set(processors.map((item) => item.id));
  const stem = base.replace(/[^a-z0-9_]/g, '_').slice(0, 34) || 'custom_post';
  let candidate = stem;
  let index = 2;
  while (taken.has(candidate)) {
    candidate = `${stem}_${index}`;
    index += 1;
  }
  return candidate;
}

/** 切换类别时补一份该类别的最小配置，并同步默认扩展名 */
export function switchKind(record: PostProcessorRecord, kind: PostKind): PostProcessorRecord {
  return {
    ...record,
    kind,
    ext: record.kind === kind ? record.ext : KIND_EXT[kind],
    gcode: kind === 'gcode'
      ? (record.gcode ?? {
        ...BLANK_GCODE,
        header: [...BLANK_GCODE.header],
        footer: [...BLANK_GCODE.footer],
        toolChange: [...BLANK_GCODE.toolChange],
      })
      : record.gcode,
    hpgl: kind === 'hpgl'
      ? (record.hpgl ?? { ...BLANK_HPGL, header: [...BLANK_HPGL.header], footer: [...BLANK_HPGL.footer] })
      : record.hpgl,
    dxf: kind === 'dxf' ? (record.dxf ?? { version: 'R2010' }) : record.dxf,
  };
}
