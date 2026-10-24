/**
 * 版本信息（前端侧唯一事实来源）。
 *
 * 改版本号时要同步这几处，缺一处就会出现"关于页、安装包、健康检查"数字不一致：
 *   frontend/src/version.ts        ← 本文件
 *   frontend/package.json
 *   frontend/src-tauri/tauri.conf.json / Cargo.toml（安装包与 exe 属性）
 *   backend/app/version.py（/api/health 返回的后端版本）
 */

export const APP_NAME = 'BtCAM';

/** 语义化版本，与安装包/exe 版本号一致 */
export const APP_VERSION = '0.4.0';

/** 关于页展示的版本标识 */
export const APP_EDITION = 'V0.4 完整版';

/** 已交付的 12 个业务模块，关于页原样列出（纯展示，不参与任何逻辑） */
export const APP_MODULES = [
  '工程与存储',
  'DXF 导入',
  '图形编辑与校验',
  '自动排料',
  '刀路生成',
  '刀具库与工艺模板',
  '后处理与导出',
  '工单存档',
  '日志与告警',
  '余料库',
  '几何引擎',
  'MES 对接',
] as const;
