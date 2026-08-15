/**
 * 自选板块列表自定义列配置。
 *
 * 与自选列表配置完全独立，互不影响。
 */

import { storage } from '@/lib/storage'
import {
  buildExtColumnsParam as buildExtColumnsParamBase,
  createExtColumn as createExtColumnBase,
  mergeColumns as mergeColumnsBase,
  serializeColumns as serializeColumnsBase,
  type ColumnConfig,
  type ColumnGroup,
  type ColumnSource,
  type ExtColumnDisplayConfig,
  type CandleColumnConfig,
} from '@/lib/list-columns'

export type { ColumnConfig, ColumnGroup, ColumnSource, ExtColumnDisplayConfig, CandleColumnConfig }

// 复用自选列表的内置列和分组
import { BUILTIN_COLUMNS, COLUMN_GROUPS, ACTION_COLUMN_ID } from '@/lib/watchlist-columns'
export { BUILTIN_COLUMNS, COLUMN_GROUPS, ACTION_COLUMN_ID }

// ===== localStorage 持久化 =====

/** 序列化列配置（只保存用户可自定义的列，排除 pinned 和 action） */
export function serializeColumns(columns: ColumnConfig[]): ColumnConfig[] {
  return serializeColumnsBase(columns, ACTION_COLUMN_ID)
}

/** 序列化并保存到 localStorage（自选板块专用） */
export async function saveColumnConfig(columns: ColumnConfig[]): Promise<void> {
  const saveable = serializeColumns(columns)
  // 只写 localStorage（自选板块暂时不做后端持久化）
  storage.watchlistGroupsColumns.set(saveable)
}

/** 加载列配置：从 localStorage 加载，最终用默认值（自选板块专用） */
export async function loadColumnConfig(): Promise<ColumnConfig[]> {
  // 1. 尝试从 localStorage 加载
  const saved = storage.watchlistGroupsColumns.get([]) as ColumnConfig[]
  if (saved.length > 0) {
    return mergeColumns(saved, BUILTIN_COLUMNS)
  }

  // 2. 默认值
  return [...BUILTIN_COLUMNS]
}

/** 合并用户保存的列与默认列 */
function mergeColumns(saved: ColumnConfig[], defaults: ColumnConfig[]): ColumnConfig[] {
  return mergeColumnsBase(saved, defaults, { actionColumnId: ACTION_COLUMN_ID })
}

/** 从列配置中提取 ext 列参数，用于后端 enriched 接口 */
export function buildExtColumnsParam(columns: ColumnConfig[]): string {
  return buildExtColumnsParamBase(columns)
}

/** 根据 ext schema 数据创建 ext 列配置 */
export function createExtColumn(
  configId: string,
  configLabel: string,
  fieldName: string,
  fieldLabel?: string,
): ColumnConfig {
  return createExtColumnBase(configId, configLabel, fieldName, fieldLabel)
}
