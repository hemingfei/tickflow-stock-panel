import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { useQuery, useQueryClient, useMutation } from '@tanstack/react-query'
import { Plus, List, Grid, Trash2, Edit2, X, Search, Check, Settings, LayoutGrid, RefreshCw, Settings2, Minus, ChevronsUp, ArrowUpDown, ArrowUp, ArrowDown, Eye, EyeOff } from 'lucide-react'
import { AnimatePresence, motion } from 'framer-motion'
import { toast } from '@/components/Toast'
import { Modal } from '@/components/Modal'
import { EmptyState } from '@/components/EmptyState'
import { StockDataTable } from '@/components/stock-table/StockDataTable'
import { useTableSort } from '@/components/stock-table/useTableSort'
import { renderBuiltinDataCell, boardTag } from '@/components/stock-table/primitives'
import { MiniCandlestick } from '@/components/stock-table/MiniCandlestick'
import { MiniIntraday } from '@/components/stock-table/MiniIntraday'
import { fmtPrice, fmtPct, fmtBigNum, priceColorClass, formatExtNumber } from '@/lib/format'
import { QK } from '@/lib/queryKeys'
import { BUILTIN_COLUMNS, type ColumnConfig, loadColumnConfig, saveColumnConfig, buildExtColumnsParam } from '@/lib/watchlist-groups-columns'
import { resolveCandleConfig, resolveIntradayConfig } from '@/lib/list-columns'
import {
  DimensionMembersDialog,
  dimensionKindForSourceField,
  type DimensionMembersTarget,
} from '@/components/DimensionMembersDialog'
import { ColumnCustomizer } from '@/components/ColumnCustomizer'
import { storage } from '@/lib/storage'
import { api, type WatchlistGroupBoard, type MinuteKlineRow } from '@/lib/api'
import { useQuoteStatus, useCapabilities, usePreferences } from '@/lib/useSharedQueries'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { getSignals, signalCls } from '@/lib/stock-table'
import { cn } from '@/lib/cn'

function StockSearchBox({
  onPreview,
  existingSymbols,
  onAdd,
}: {
  onPreview: (symbol: string, name: string) => void
  existingSymbols: string[]
  onAdd: (symbol: string) => void
}) {
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const [activeIdx, setActiveIdx] = useState(-1)

  const search = useQuery({
    queryKey: QK.instrumentSearch(query, 'stock,etf,index'),
    queryFn: () => api.instrumentSearch(query, 20, 'stock,etf,index'),
    enabled: query.trim().length > 0,
    staleTime: 30_000,
  })

  const results = search.data?.results ?? []

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [])

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Escape') { setOpen(false); return }
    if (!open || results.length === 0) return
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActiveIdx(i => Math.min(i + 1, results.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActiveIdx(i => Math.max(i - 1, -1))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      if (activeIdx >= 0) handleSelect(results[activeIdx])
      else if (results.length > 0) handleSelect(results[0])
    }
  }

  function handleSelect(r: { symbol: string; name: string }) {
    onPreview(r.symbol, r.name)
    setQuery('')
    setOpen(false)
    setActiveIdx(-1)
  }

  return (
    <div ref={containerRef} className="relative">
      <div className="relative flex items-center">
        <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted pointer-events-none" />
        <input
          ref={inputRef}
          type="text"
          placeholder="搜索…"
          value={query}
          onChange={(e) => { setQuery(e.target.value); setOpen(true); setActiveIdx(-1) }}
          onFocus={() => { if (query.trim()) setOpen(true) }}
          onKeyDown={handleKeyDown}
          className="w-44 h-8 pl-8 pr-2.5 rounded-btn bg-elevated border border-border text-xs text-foreground placeholder:text-muted focus:outline-none focus:border-accent/50 focus:w-56 transition-all duration-200"
        />
      </div>

      <AnimatePresence>
        {open && results.length > 0 && (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: 0.12, ease: [0.16, 1, 0.3, 1] }}
            className="absolute right-0 top-full mt-1 z-50 w-64 max-h-[320px] overflow-y-auto rounded-card border border-border bg-base shadow-xl"
          >
            {results.map((r, i) => {
              const inWatchlist = existingSymbols.includes(r.symbol)
              return (
                <div
                  key={r.symbol}
                  className={`flex items-center gap-2.5 px-3 py-2 text-xs transition-colors duration-100 ${
                    i === activeIdx ? 'bg-accent/10 text-accent' : 'hover:bg-elevated text-foreground'
                  }`}
                >
                  <button
                    type="button"
                    onClick={() => handleSelect(r)}
                    className="flex items-center gap-2.5 flex-1 min-w-0 text-left"
                  >
                    <span className="font-mono shrink-0 w-[80px]">{r.symbol}</span>
                    <span className="truncate text-secondary flex-1">{r.name}</span>
                    {r.asset_type === 'etf' && (
                      <span className="shrink-0 px-1 py-0.5 rounded text-[10px] leading-none bg-accent/10 text-accent">ETF</span>
                    )}
                    {r.asset_type === 'index' && (
                      <span className="shrink-0 px-1 py-0.5 rounded text-[10px] leading-none bg-sky-500/10 text-sky-400">指数</span>
                    )}
                    {(() => {
                      const b = boardTag(r.symbol)
                      return b && (
                        <span className={`shrink-0 px-1 py-0.5 rounded text-[10px] leading-none border ${b.color}`}>{b.label}</span>
                      )
                    })()}
                  </button>
                  <button
                    type="button"
                    onClick={(e) => { e.stopPropagation(); onAdd(r.symbol) }}
                    disabled={inWatchlist}
                    className={`shrink-0 p-1 rounded transition-colors ${
                      inWatchlist
                        ? 'text-accent bg-accent/10 cursor-default'
                        : 'text-muted hover:text-accent hover:bg-accent/10'
                    }`}
                    title={inWatchlist ? '已添加' : '添加到分组'}
                  >
                    {inWatchlist ? <Check className="h-3.5 w-3.5" /> : <Plus className="h-3.5 w-3.5" />}
                  </button>
                </div>
              )
            })}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

// 换手率分档色
function turnoverColor(rate: number | null | undefined): string {
  if (rate == null || Number.isNaN(rate)) return 'text-[#888]'
  if (rate < 5) return 'text-[#888]'
  if (rate < 10) return 'text-[#d4a800]'
  if (rate < 20) return 'text-[#f97316]'
  if (rate < 35) return 'text-[#d94a3d]'
  return 'text-[#b84a8a]'
}

// 卡片列数计算
function cardColumnCount(viewportWidth: number): number {
  if (viewportWidth >= 1536) return 6
  if (viewportWidth >= 1280) return 5
  if (viewportWidth >= 768) return 4
  if (viewportWidth >= 640) return 3
  return 2
}

function useCardColumnCount(): number {
  const [count, setCount] = useState(() => cardColumnCount(window.innerWidth))

  useEffect(() => {
    const update = () => setCount(cardColumnCount(window.innerWidth))
    window.addEventListener('resize', update)
    return () => window.removeEventListener('resize', update)
  }, [])

  return count
}

// 自选分组专用的 StockCard 组件
const StockCard = React.memo(function StockCard({
  r,
  onPreview,
  onConfirmRemove,
  onCancelRemove,
  onRequestRemove,
  isConfirming,
  hideSymbol = false,
  swapPriceAndPct = false,
}: {
  r: any
  onPreview: (symbol: string, name: string) => void
  onConfirmRemove: (symbol: string) => void
  onCancelRemove: () => void
  onRequestRemove: (symbol: string) => void
  isConfirming: boolean
  hideSymbol?: boolean
  swapPriceAndPct?: boolean
}) {
  const board = boardTag(r.symbol)
  const price = r.rt_price ?? r.close
  const pct = r.rt_pct ?? r.change_pct
  const name = r.rt_name ?? r.name
  const signals = getSignals(r)
  const isUp = (pct ?? 0) > 0
  const isDown = (pct ?? 0) < 0

  // 动态背景渐变: 涨=红底, 跌=绿底, 平=无色
  const bgGlow = isUp
    ? 'bg-gradient-to-br from-bull/[0.06] via-transparent to-bull/[0.02]'
    : isDown
      ? 'bg-gradient-to-br from-bear/[0.06] via-transparent to-bear/[0.02]'
      : ''
  // 左侧指示条颜色
  const barColor = isUp ? 'bg-bull/70' : isDown ? 'bg-bear/70' : 'bg-muted/30'
  // 涨跌幅标签背景
  const pctBg = isUp ? 'bg-bull/12 text-bull' : isDown ? 'bg-bear/12 text-bear' : 'bg-elevated text-secondary'

  return (
    <div
      className={`relative rounded-lg border border-border bg-surface hover:border-border/80 transition-all duration-200 group cursor-pointer overflow-hidden ${bgGlow}`}
      onClick={() => onPreview(r.symbol, name ?? '')}
    >
      {/* 左侧彩色指示条 */}
      <div className={`absolute left-0 top-0 bottom-0 w-[3px] rounded-l-lg ${barColor}`} />

      {/* 删除按钮 / 确认区 */}
      <div className="absolute top-1.5 right-1.5 z-10">
        {isConfirming ? (
          <div className="flex items-center gap-1" onClick={e => e.stopPropagation()}>
            <button
              onClick={() => onConfirmRemove(r.symbol)}
              className="px-1.5 py-0.5 rounded text-[10px] text-danger bg-danger/10 hover:bg-danger/20 transition-colors"
            >
              确认
            </button>
            <button onClick={() => onCancelRemove()} className="p-0.5 text-muted hover:text-foreground transition-colors">
              <X className="h-3 w-3" />
            </button>
          </div>
        ) : (
          <button
            onClick={e => { e.stopPropagation(); onRequestRemove(r.symbol) }}
            className="opacity-0 group-hover:opacity-100 text-muted hover:text-danger transition-all duration-150 p-0.5 rounded hover:bg-elevated"
            aria-label="移出分组"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        )}
      </div>

      {/* 卡片内容 */}
      <div className="pl-4 pr-2.5 pt-2.5 pb-0">
        {/* 第一行: 代码 + 名称 + 板块标识 */}
        <div className="flex items-center gap-1.5 min-w-0 mb-2">
          {!hideSymbol && (
            <span className="shrink-0 font-mono text-foreground text-xs tracking-wide">
              {r.symbol}
            </span>
          )}
          {name && (
            <span className="text-xs text-secondary truncate">{name}</span>
          )}
          {board && (
            <span className={`shrink-0 inline-flex items-center justify-center px-1 h-[16px] rounded text-[9px] font-bold leading-none ${board.color}`}>
              {board.label}
            </span>
          )}
          {r.consecutive_limit_ups > 0 && (
            <span className="shrink-0 inline-flex items-center justify-center px-1 h-[16px] rounded bg-danger/15 text-danger text-[9px] font-bold tabular-nums">
              {r.consecutive_limit_ups === 1 ? '首板' : `${r.consecutive_limit_ups}连`}
            </span>
          )}
        </div>

        {/* 第二行: 价格和涨跌幅 */}
        <div className="flex items-end justify-between gap-2 mb-2">
          {swapPriceAndPct ? (
            <>
              {pct != null && (
                <span className={`text-xl tabular-nums tracking-tighter leading-none ${priceColorClass(pct)}`}>
                  {fmtPct(pct).replace('%', '')}
                </span>
              )}
              <span className={`shrink-0 inline-flex items-center px-1.5 py-[2px] rounded text-[11px] tabular-nums ${pctBg}`}>
                {fmtPrice(price)}
              </span>
            </>
          ) : (
            <>
              <span className={`text-xl tabular-nums tracking-tighter leading-none ${priceColorClass(pct)}`}>
                {fmtPrice(price)}
              </span>
              {pct != null && (
                <span className={`shrink-0 inline-flex items-center px-1.5 py-[2px] rounded text-[11px] tabular-nums ${pctBg}`}>
                  {fmtPct(pct)}
                </span>
              )}
            </>
          )}
        </div>

        {/* 第三行: 指标 */}
        <div className="flex flex-wrap items-center gap-x-2.5 gap-y-0.5 text-[10px] text-muted leading-relaxed">
          <span title="换手率">换手<span className={`font-mono ml-0.5 ${turnoverColor(r.turnover_rate)}`}>{r.turnover_rate != null ? `${r.turnover_rate.toFixed(2)}%` : '—'}</span></span>
          <span title="量比">量比<span className="font-mono ml-0.5">{fmtPrice(r.vol_ratio_5d)}</span></span>
          <span title="RSI14">RSI<span className="font-mono ml-0.5">{r.rsi_14 != null ? r.rsi_14.toFixed(1) : '—'}</span></span>
        </div>
      </div>

      {/* 信号标签区 */}
      {signals.length > 0 && (
        <div className="pl-4 pr-2.5 pt-1.5 pb-2 flex flex-wrap gap-1">
          {signals.slice(0, 3).map(s => (
            <span key={s.label} className={`inline-block px-1.5 py-[1px] rounded text-[9px] font-medium leading-tight ${signalCls(s.type)}`}>
              {s.label}
            </span>
          ))}
          {signals.length > 3 && (
            <span className="inline-block px-1 py-[1px] rounded text-[9px] text-muted bg-elevated leading-tight">
              +{signals.length - 3}
            </span>
          )}
        </div>
      )}
    </div>
  )
})

// 单个分组卡片组件 - 接收数据作为 props
function GroupCard({
  group,
  onSelect,
  avgPctMode,
  items,
}: {
  group: WatchlistGroupBoard
  onSelect: (groupId: string) => void
  avgPctMode: 'simple' | 'weighted'
  items?: any[]
}) {
  const allRows = items
  const rows = allRows?.slice(0, 5) ?? []
  
  // 前端计算平均涨跌幅
  let avgSimple: number | null = null
  let avgWeighted: number | null = null
  
  if (allRows && allRows.length > 0) {
    let sumSimple = 0
    let sumWeighted = 0
    let totalAmount = 0
    let count = 0
    
    for (const row of allRows) {
      const changePct = row.change_pct
      const amount = row.amount || 0
      if (typeof changePct === 'number' && !Number.isNaN(changePct)) {
        sumSimple += changePct
        count += 1
        if (amount > 0) {
          sumWeighted += changePct * amount
          totalAmount += amount
        }
      }
    }
    
    avgSimple = count > 0 ? sumSimple / count : null
    avgWeighted = totalAmount > 0 ? sumWeighted / totalAmount : null
  }
  
  const displayChange = avgPctMode === 'simple' ? avgSimple : avgWeighted

  return (
    <div
      className="border border-border rounded-lg bg-surface cursor-pointer hover:border-accent/50 transition-colors"
      onClick={() => onSelect(group.group_id)}
    >
      <div className="p-2 border-b border-border flex items-center justify-between">
        <div className="flex items-center gap-2">
          <h3 className="font-semibold text-sm">{group.name}</h3>
          {displayChange != null && (
            <span className={`text-xs font-medium ${displayChange >= 0 ? 'text-bull' : 'text-bear'}`}>
              {fmtPct(displayChange)}
            </span>
          )}
        </div>
        <span className="text-xs text-muted">{group.item_count} 只</span>
      </div>
      <div className="p-2">
        {rows.length > 0 ? (
          <>
            <div className="space-y-0.5">
              {rows.map((row) => (
                <div key={row.symbol} className="flex items-center justify-between py-0.5 text-sm">
                  <div className="flex items-center gap-2 min-w-0">
                    {row.name && <span className="text-muted truncate text-sm">{row.name}</span>}
                  </div>
                  {row.change_pct != null && (
                    <span className={`font-medium text-sm ${row.change_pct >= 0 ? 'text-bull' : 'text-bear'}`}>
                      {fmtPct(row.change_pct)}
                    </span>
                  )}
                </div>
              ))}
            </div>
            {group.item_count > 5 && (
              <button
                type="button"
                onClick={(e) => { e.stopPropagation(); onSelect(group.group_id) }}
                className="text-xs text-accent hover:underline mt-1"
              >
                查看全部 {group.item_count} 只
              </button>
            )}
          </>
        ) : (
          <div className="text-xs text-muted">暂无股票</div>
        )}
      </div>
    </div>
  )
}

/** 渲染扩展数据列的值（含分隔/标签/展开配置） */
function renderExtValue(
  val: any,
  col: ColumnConfig,
  expanded: boolean,
  onToggle: () => void,
  inline?: boolean,
  onTagClick?: (tag: string) => void,
): React.ReactNode {
  if (val == null || Number.isNaN(val)) return <span className="text-muted">—</span>
  if (typeof val === 'number') {
    // 数字格式化: 千分位 + 单位换算 + 小数位(由列配置控制)
    const cfg = col.extDisplay
    const hasNumFmt = cfg?.thousandSeparator || (cfg?.unitConvert && cfg.unitConvert !== 'none')
    const displayVal = hasNumFmt
      ? formatExtNumber(val, { thousandSeparator: cfg?.thousandSeparator, unitConvert: cfg?.unitConvert, unitDecimals: cfg?.unitDecimals })
      : (Number.isInteger(val) ? fmtPrice(val, 0) : fmtPrice(val))
    return <span className="tabular-nums">{displayVal}</span>
  }
  if (typeof val === 'boolean') {
    return <span className={val ? 'text-bull' : 'text-muted'}>{val ? '是' : '否'}</span>
  }

  // String — 按 extDisplay 配置渲染
  const cfg = col.extDisplay
  const str = String(val)

  // 纯文本模式
  if (cfg?.displayMode === 'text') {
    return <span className="text-foreground">{str}</span>
  }

  // 标签模式（默认）
  const separator = cfg?.separator?.trim() || null
  const tags = separator
    ? str.split(separator).map(s => s.trim()).filter(Boolean)
    : str.split(/[、,，;；\-]/).map(s => s.trim()).filter(Boolean)

  if (tags.length === 0) return <span className="text-muted">—</span>

  const maxTags = cfg?.maxTags ?? 0
  const showAll = maxTags <= 0 || expanded || tags.length <= maxTags
  const sliced = showAll ? tags : tags.slice(0, maxTags)
  const hiddenIndices = maxTags > 0 ? cfg?.hiddenIndices : undefined
  const visibleTags = hiddenIndices?.length
    ? sliced.filter((_, i) => !hiddenIndices.includes(i))
    : sliced
  const hiddenCount = tags.length - visibleTags.length

  // 竖向排列：仅在表格视图、收起态、设定了显示上限时生效
  const isVertical = !inline && cfg?.tagLayout === 'vertical' && !expanded

  const tagEls = (
    <>
      {visibleTags.map((tag, i) => onTagClick ? (
        <button
          key={i}
          type="button"
          onClick={event => { event.stopPropagation(); onTagClick(tag) }}
          className="inline-block px-1.5 py-px rounded text-[10px] font-medium leading-tight text-yellow-500 bg-yellow-500/10 hover:brightness-95"
        >
          {tag}
        </button>
      ) : (
        <span key={i} className="inline-block px-1.5 py-px rounded text-[10px] font-medium leading-tight text-yellow-500 bg-yellow-500/10">
          {tag}
        </span>
      ))}
      {!showAll && hiddenCount > 0 && (
        <button
          onClick={onToggle}
          className="inline-block px-1.5 py-px rounded text-[10px] font-medium leading-tight text-accent bg-accent/10 hover:bg-accent/20 transition-colors"
        >
          +{hiddenCount}
        </button>
      )}
      {showAll && maxTags > 0 && tags.length > maxTags && (
        <button
          onClick={onToggle}
          className="inline-block px-1.5 py-px rounded text-[10px] font-medium leading-tight text-muted hover:text-foreground transition-colors"
        >
          收起
        </button>
      )}
    </>
  )

  if (inline) {
    // 卡片视图：返回 inline 片段
    return tagEls
  }
  // 表格视图：用 <div> 包裹
  return <div className={isVertical ? 'flex flex-col items-start gap-0.5' : 'flex flex-wrap gap-0.5'}>{tagEls}</div>
}

/** 渲染扩展数据列的 <td> */
function renderExtCell(
  r: any,
  col: ColumnConfig,
  expandedCells: Set<string>,
  onToggleExpand: (key: string) => void,
  onDimensionClick: (target: DimensionMembersTarget) => void,
): React.ReactNode {
  if (col.source.type !== 'ext') return null
  const { configId, fieldName } = col.source
  const val = r[`${configId}__${fieldName}`]
  const cellKey = `${r.symbol}::${col.id}`
  const expanded = expandedCells.has(cellKey)
  const sourceField = `${configId}.${fieldName}`
  const dimensionKind = dimensionKindForSourceField(sourceField)

  const style: React.CSSProperties = {}
  if (col.extDisplay?.maxWidth) {
    style.maxWidth = col.extDisplay.maxWidth
  }

  // 根据值类型决定 td class
  const tdClass = val == null || Number.isNaN(val)
    ? 'px-2 py-1.5 text-right num tabular-nums text-muted'
    : typeof val === 'number'
      ? 'px-2 py-1.5 text-right num tabular-nums'
      : typeof val === 'boolean'
        ? 'px-2 py-1.5 text-right'
        : 'px-2 py-1.5'

  return (
    <td className={tdClass} style={style}>
      {renderExtValue(
        val,
        col,
        expanded,
        () => onToggleExpand(cellKey),
        false,
        dimensionKind ? value => onDimensionClick({ kind: dimensionKind, value, sourceField }) : undefined,
      )}
    </td>
  )
}

export function WatchlistGroups() {
  const queryClient = useQueryClient()

  const [selectedGroupId, setSelectedGroupId] = useState<string | null>(null)
  const [createDialogOpen, setCreateDialogOpen] = useState(false)
  const [renameDialogOpen, setRenameDialogOpen] = useState(false)
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false)
  const [sidebarSettingsMenuOpen, setSidebarSettingsMenuOpen] = useState(false)
  const [currentGroupForDialog, setCurrentGroupForDialog] = useState<WatchlistGroupBoard | null>(null)
  const sidebarSettingsMenuRef = useRef<HTMLDivElement>(null)
  const [newGroupName, setNewGroupName] = useState('')
  const [previewSymbol, setPreviewSymbol] = useState<string | null>(null)
  const [previewName, setPreviewName] = useState<string>('')
  const [confirmRemove, setConfirmRemove] = useState<string | null>(null)
  const [sortMode, setSortMode] = useState<'custom' | 'ascending' | 'descending'>(() => {
    return storage.watchlistGroupsSortMode.get('custom') as 'custom' | 'ascending' | 'descending'
  })
  const [stockSortMode, setStockSortMode] = useState<'default' | 'ascending' | 'descending'>(() => {
    return storage.watchlistGroupsStockSortMode.get('default') as 'default' | 'ascending' | 'descending'
  })
  const [expandedCells, setExpandedCells] = useState<Set<string>>(new Set())
  const [dimensionTarget, setDimensionTarget] = useState<DimensionMembersTarget | null>(null)
  const [importDialogOpen, setImportDialogOpen] = useState(false)
  const [importReplaceMode, setImportReplaceMode] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

  // 视图模式（列表/卡片）- 自选分组专用
  const [viewMode, setViewMode] = useState<'table' | 'card'>(() => {
    return (storage.watchlistGroupsView.get('table') as 'table' | 'card')
  })

  // 列配置 - 自选分组专用
  const [columns, setColumns] = useState<ColumnConfig[]>([...BUILTIN_COLUMNS])
  const [customizerOpen, setCustomizerOpen] = useState(false)
  const columnsLoaded = useRef(false)

  // 日k蜡烛图显示状态
  const [dailyKChartVisible, setDailyKChartVisible] = useState(() => {
    return storage.watchlistGroupsCandle.get(true)
  })

  // 分时图显示状态
  const [intradayChartVisible, setIntradayChartVisible] = useState(() => {
    return storage.watchlistGroupsIntraday.get(true)
  })

  // 蜡烛列检测和配置
  const candleColumn = useMemo(() =>
    columns.find(c => c.source.type === 'builtin' && c.source.key === 'candle' && c.visible),
    [columns],
  )
  const candleColumnEnabled = !!candleColumn
  const candleResolved = useMemo(() => resolveCandleConfig(candleColumn?.candleConfig), [candleColumn])
  const candleDays = candleResolved.days
  const candleSize = dailyKChartVisible
    ? { width: candleResolved.enabledWidth, height: candleResolved.enabledHeight }
    : { width: candleResolved.disabledWidth, height: candleResolved.disabledHeight }
  const dailyKVisible = candleColumnEnabled && dailyKChartVisible

  // 分时列检测和配置
  const intradayColumn = useMemo(() =>
    columns.find(c => c.source.type === 'builtin' && c.source.key === 'intraday' && c.visible),
    [columns],
  )
  const intradayResolved = useMemo(() => resolveIntradayConfig(intradayColumn?.intradayConfig), [intradayColumn])
  const caps = useCapabilities()
  const hasMinuteBatch = !!caps.data?.capabilities?.['kline.minute.batch']
  const intradayVisible = !!intradayColumn && hasMinuteBatch && intradayChartVisible

  // 切换函数
  const toggleDailyKChart = useCallback(() => {
    setDailyKChartVisible(v => {
      const next = !v
      storage.watchlistGroupsCandle.set(next)
      return next
    })
  }, [])

  const toggleIntradayChart = useCallback(() => {
    setIntradayChartVisible(v => {
      const next = !v
      storage.watchlistGroupsIntraday.set(next)
      return next
    })
  }, [])

  // 设置查询（仅用于侧边栏/卡片视图切换和平均涨跌幅模式）
  const settingsQuery = useQuery({
    queryKey: ['watchlist-groups-settings'],
    queryFn: () => api.watchlistGroupBoards.getSettings(),
    staleTime: Infinity,
  })

  // 从设置中获取或使用默认值（仅保留侧边栏/卡片视图和平均涨跌幅模式）
  const sidebarOrCardsView = (settingsQuery.data?.view_mode as 'sidebar' | 'cards') || 'sidebar'
  const avgPctMode = (settingsQuery.data?.avg_pct_mode as 'simple' | 'weighted') || 'simple'
  
  // 卡片页内部的视图模式：cards（卡片网格）或 list（列表视图）
  const [cardsViewMode, setCardsViewMode] = useState<string>('cards')

  // 所有的 mutations 定义在前面
  const setSidebarOrCardsViewMutation = useMutation({
    mutationFn: (mode: string) => api.watchlistGroupBoards.setViewMode(mode),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['watchlist-groups-settings'] })
    },
  })

  const setAvgPctModeMutation = useMutation({
    mutationFn: (mode: string) => api.watchlistGroupBoards.setAvgPctMode(mode),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['watchlist-groups-settings'] })
    },
  })

  const createMutation = useMutation({
    mutationFn: (name: string) => api.watchlistGroupBoards.create(name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QK.watchlistGroups })
      toast('分组创建成功')
      setCreateDialogOpen(false)
      setNewGroupName('')
    },
    onError: (e: any) => toast(e.message || '创建失败', 'error'),
  })

  const renameMutation = useMutation({
    mutationFn: ({ groupId, name }: { groupId: string; name: string }) => api.watchlistGroupBoards.rename(groupId, name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QK.watchlistGroups })
      toast('分组重命名成功')
      setRenameDialogOpen(false)
      setCurrentGroupForDialog(null)
    },
    onError: (e: any) => toast(e.message || '重命名失败', 'error'),
  })

  const deleteMutation = useMutation({
    mutationFn: (groupId: string) => api.watchlistGroupBoards.delete(groupId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QK.watchlistGroups })
      toast('分组删除成功')
      setDeleteDialogOpen(false)
      if (selectedGroupId === currentGroupForDialog?.group_id) {
        setSelectedGroupId(null)
      }
      setCurrentGroupForDialog(null)
    },
    onError: (e: any) => toast(e.message || '删除失败', 'error'),
  })

  const addItemMutation = useMutation({
    mutationFn: ({ groupId, symbol }: { groupId: string; symbol: string }) => 
      api.watchlistGroupBoards.addItem(groupId, symbol),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QK.watchlistGroups })
      // 使用前缀匹配失效所有 watchlist-groups-all-items 相关查询
      queryClient.invalidateQueries({ queryKey: ['watchlist-groups-all-items'] })
      if (selectedGroupId) {
        // 使用前缀匹配失效所有该 group 的 enriched 查询（不管 ext 参数是什么）
        queryClient.invalidateQueries({ 
          queryKey: ['watchlist-group-items-enriched', selectedGroupId] 
        })
      }
      toast("股票添加成功")
    },
    onError: (e: any) => toast(e.message || "添加失败", "error"),
  })

  const removeItemMutation = useMutation({
    mutationFn: ({ groupId, symbol }: { groupId: string; symbol: string }) => 
      api.watchlistGroupBoards.removeItem(groupId, symbol),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QK.watchlistGroups })
      // 使用前缀匹配失效所有 watchlist-groups-all-items 相关查询
      queryClient.invalidateQueries({ queryKey: ['watchlist-groups-all-items'] })
      if (selectedGroupId) {
        // 使用前缀匹配失效所有该 group 的 enriched 查询（不管 ext 参数是什么）
        queryClient.invalidateQueries({ 
          queryKey: ['watchlist-group-items-enriched', selectedGroupId] 
        })
      }
      toast("股票移除成功")
    },
    onError: (e: any) => toast(e.message || "移除失败", "error"),
  })

  const moveItemToTopMutation = useMutation({
    mutationFn: ({ groupId, symbols }: { groupId: string; symbols: string[] }) =>
      api.watchlistGroupBoards.reorderItems(groupId, symbols),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QK.watchlistGroups })
      // 使用前缀匹配失效所有 watchlist-groups-all-items 相关查询
      queryClient.invalidateQueries({ queryKey: ['watchlist-groups-all-items'] })
      if (selectedGroupId) {
        // 使用前缀匹配失效所有该 group 的 enriched 查询（不管 ext 参数是什么）
        queryClient.invalidateQueries({ 
          queryKey: ['watchlist-group-items-enriched', selectedGroupId] 
        })
      }
    },
    onError: (e: any) => toast(e.message || "置顶失败", "error"),
  })

  const moveGroupToTopMutation = useMutation({
    mutationFn: (groupIds: string[]) =>
      api.watchlistGroupBoards.reorder(groupIds),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QK.watchlistGroups })
    },
    onError: (e: any) => toast(e.message || '分组置顶失败', 'error'),
  })

  const exportConfigMutation = useMutation({
    mutationFn: () => api.watchlistGroupBoards.exportConfig(),
    onSuccess: (data) => {
      // 触发文件下载
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      const date = new Date().toISOString().slice(0, 10).replace(/-/g, '')
      a.download = `watchlist-groups-${date}.json`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
      toast('配置导出成功')
    },
    onError: (e: any) => toast(e.message || '导出失败', 'error'),
  })

  const importConfigMutation = useMutation({
    mutationFn: ({ data, replace }: { data: any; replace: boolean }) =>
      api.watchlistGroupBoards.importConfig(data, replace),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QK.watchlistGroups })
      queryClient.invalidateQueries({ queryKey: ['watchlist-groups-settings'] })
      queryClient.invalidateQueries({ queryKey: ['watchlist-groups-all-items'] })
      setImportDialogOpen(false)
      setImportReplaceMode(false)
      toast('配置导入成功')
    },
    onError: (e: any) => toast(e.message || '导入失败', 'error'),
  })

  // 加载列配置
  useEffect(() => {
    if (columnsLoaded.current) return
    columnsLoaded.current = true
    loadColumnConfig().then(setColumns)
  }, [])

  // 视图模式切换处理
  const toggleView = useCallback(() => {
    setViewMode(v => {
      const next = v === 'table' ? 'card' : 'table'
      storage.watchlistGroupsView.set(next)
      return next
    })
  }, [])

  // 列配置变更处理
  const handleColumnsChange = useCallback((next: ColumnConfig[]) => {
    setColumns(next)
    saveColumnConfig(next)
  }, [])

  // 计算可见列
  const visibleColumns = useMemo(() => {
    return columns.filter(c => c.visible)
  }, [columns])

  // 计算 ext 列参数
  const extColumnsParam = useMemo(() => buildExtColumnsParam(columns), [columns])

  // 标签展开/收起处理
  const handleToggleExpand = useCallback((cellKey: string) => {
    setExpandedCells(prev => {
      const next = new Set(prev)
      if (next.has(cellKey)) next.delete(cellKey)
      else next.add(cellKey)
      return next
    })
  }, [])

  // 卡片列数
  const cardColumns = useCardColumnCount()

  // 排序（复用共享三态排序 hook）
  const { sort, toggle: handleSortToggle, sortRows } = useTableSort()

  // 稳定的回调函数（需要用到 removeItemMutation，所以放在它后面）
  const handleCardPreview = useCallback((sym: string, name: string) => {
    setPreviewSymbol(sym)
    setPreviewName(name)
  }, [])

  const handleCardConfirmRemove = useCallback((sym: string) => {
    if (!selectedGroupId) return
    removeItemMutation.mutate({ groupId: selectedGroupId, symbol: sym })
    setConfirmRemove(null)
  }, [selectedGroupId, removeItemMutation])

  const handleCardCancelRemove = useCallback(() => setConfirmRemove(null), [])

  const handleCardRequestRemove = useCallback((sym: string) => setConfirmRemove(sym), [])

  // 数据查询
  const groupsQuery = useQuery({ queryKey: QK.watchlistGroups, queryFn: api.watchlistGroupBoards.list })

  const selectedGroupItemsQuery = useQuery({
    queryKey: QK.watchlistGroupItemsEnriched(selectedGroupId || '', extColumnsParam),
    queryFn: () => selectedGroupId ? api.watchlistGroupBoards.listItemsEnriched(selectedGroupId, extColumnsParam) : null,
    enabled: !!selectedGroupId,
  })

  // 获取当前选中分组的 symbol 列表
  const currentSymbols = useMemo(() => {
    return selectedGroupItemsQuery.data?.rows?.map((r: any) => r.symbol) ?? []
  }, [selectedGroupItemsQuery.data])

  const currentSymbolsKey = currentSymbols.join(',')

  // 指数过滤，用于分时数据
  const minuteSymbols = useMemo(
    () => currentSymbols.filter((s: string) => (selectedGroupItemsQuery.data?.rows ?? []).find((r: any) => r.symbol === s)?.asset_type !== 'index'),
    [currentSymbols, selectedGroupItemsQuery.data],
  )
  const minuteSymbolsKey = minuteSymbols.join(',')

  // 实时行情状态
  const quoteStatus = useQuoteStatus()
  const realtimeRunning = quoteStatus.data?.running ?? false

  // 批量日K数据
  const klineBatch = useQuery({
    queryKey: QK.watchlistKlineBatch(`${currentSymbolsKey}|${candleDays}`),
    queryFn: () => api.klineDailyBatch(currentSymbols, candleDays),
    enabled: dailyKVisible && currentSymbols.length > 0 && !!selectedGroupId,
    staleTime: 5 * 60_000,
  })

  const klineData = dailyKVisible ? (klineBatch.data?.data ?? {}) : {}

  // 批量分时数据
  const { data: prefsData } = usePreferences()
  const intradayRefreshEnabled = prefsData?.minute_intraday_refresh ?? false
  const intradayRefreshInterval = prefsData?.minute_intraday_refresh_interval ?? 6

  const minuteBatch = useQuery({
    queryKey: QK.minuteBatch(minuteSymbolsKey),
    queryFn: () => api.klineMinuteBatch(minuteSymbols),
    enabled: intradayVisible && minuteSymbols.length > 0 && !!selectedGroupId,
    staleTime: 10_000,
    refetchInterval: (intradayRefreshEnabled && realtimeRunning) ? intradayRefreshInterval * 1000 : false,
  })
  const minuteData = intradayVisible ? (minuteBatch.data?.data ?? {}) : {}

  useEffect(() => {
    // 只有在侧边栏视图时才自动设置第一个分组为选中
    if (groupsQuery.data?.groups?.length && !selectedGroupId && sidebarOrCardsView === 'sidebar') {
      setSelectedGroupId(groupsQuery.data.groups[0].group_id)
    }
  }, [groupsQuery.data, sidebarOrCardsView])

  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (sidebarSettingsMenuRef.current && !sidebarSettingsMenuRef.current.contains(event.target as Node)) {
        setSidebarSettingsMenuOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])


  // 前端计算分组平均涨跌幅
  const calculateGroupAvgChange = useCallback((group: any, allGroupItems?: Record<string, any[]>) => {
    const items = allGroupItems?.[group.group_id]
    if (!items || items.length === 0) return { simple: null, weighted: null }
    
    let sumSimple = 0
    let sumWeighted = 0
    let totalAmount = 0
    let count = 0
    
    for (const row of items) {
      const changePct = row.change_pct
      const amount = row.amount || 0
      if (typeof changePct === 'number' && !Number.isNaN(changePct)) {
        sumSimple += changePct
        count += 1
        if (amount > 0) {
          sumWeighted += changePct * amount
          totalAmount += amount
        }
      }
    }
    
    return {
      simple: count > 0 ? sumSimple / count : null,
      weighted: totalAmount > 0 ? sumWeighted / totalAmount : null,
    }
  }, [])

  // 获取所有分组的 enriched 数据（用于卡片视图和侧边栏）
  const { data: allGroupItemsResponse, refetch: refetchAllGroupItems, isFetching: isFetchingAllGroupItems } = useQuery({
    queryKey: ['watchlist-groups-all-items', groupsQuery.data?.groups?.map(g => g.group_id).join(','), extColumnsParam],
    queryFn: async () => {
      if (!groupsQuery.data?.groups?.length) return { groups: {} }
      return await api.watchlistGroupBoards.listAllItemsEnriched(extColumnsParam)
    },
    enabled: !!groupsQuery.data?.groups?.length,
    staleTime: 60_000,
  })

  // 从响应中提取数据
  const allGroupItemsRaw = useMemo(() => {
    if (!allGroupItemsResponse?.groups) return undefined
    const result: Record<string, any[]> = {}
    for (const groupId in allGroupItemsResponse.groups) {
      result[groupId] = allGroupItemsResponse.groups[groupId].rows || []
    }
    return result
  }, [allGroupItemsResponse])

  // 对个股进行排序
  const allGroupItems = useMemo(() => {
    if (!allGroupItemsRaw) return undefined
    const result: Record<string, any[]> = {}
    
    for (const groupId in allGroupItemsRaw) {
      const items = [...allGroupItemsRaw[groupId]]
      if (stockSortMode !== 'default') {
        items.sort((a, b) => {
          const aPct = a.change_pct ?? 0
          const bPct = b.change_pct ?? 0
          return stockSortMode === 'descending' ? bPct - aPct : aPct - bPct
        })
      }
      result[groupId] = items
    }
    
    return result
  }, [allGroupItemsRaw, stockSortMode])

  // 排序切换逻辑
  const toggleSortMode = useCallback(() => {
    setSortMode(mode => {
      const newMode = mode === 'custom' ? 'descending' : mode === 'descending' ? 'ascending' : 'custom'
      storage.watchlistGroupsSortMode.set(newMode)
      return newMode
    })
  }, [])

  // 个股排序切换逻辑
  const toggleStockSortMode = useCallback(() => {
    setStockSortMode(mode => {
      const newMode = mode === 'default' ? 'descending' : mode === 'descending' ? 'ascending' : 'default'
      storage.watchlistGroupsStockSortMode.set(newMode)
      return newMode
    })
  }, [])

  // 对分组进行排序
  const sortedGroups = useMemo(() => {
    if (!groupsQuery.data?.groups) return []
    const groups = [...groupsQuery.data.groups]

    if (sortMode === 'custom') {
      return groups
    }

    // 计算每个分组的平均涨跌幅
    const groupsWithAvg = groups.map(group => {
      const items = allGroupItems?.[group.group_id]
      const avgChange = items ? calculateGroupAvgChange(group, { [group.group_id]: items }) : { simple: null, weighted: null }
      const displayChange = avgPctMode === 'simple' ? avgChange.simple : avgChange.weighted
      return { group, avgChange: displayChange ?? 0 }
    })

    // 排序
    if (sortMode === 'ascending') {
      groupsWithAvg.sort((a, b) => a.avgChange - b.avgChange)
    } else {
      groupsWithAvg.sort((a, b) => b.avgChange - a.avgChange)
    }

    return groupsWithAvg.map(g => g.group)
  }, [groupsQuery.data?.groups, allGroupItems, sortMode, avgPctMode, calculateGroupAvgChange])


  const renderCell = useCallback((row: any, col: ColumnConfig): React.ReactNode => {
    // ext 列
    if (col.source.type === 'ext') {
      return renderExtCell(row, col, expandedCells, handleToggleExpand, setDimensionTarget)
    }
    
    if (col.source.type === 'builtin' && col.source.key === 'symbol') {
      const currentGroupItems = selectedGroupItemsQuery.data?.rows || []
      const currentSymbols = currentGroupItems.map(r => r.symbol)
      const isFirst = currentSymbols[0] === row.symbol
      const board = boardTag(row.symbol)

      return (
        <td key={col.id} className="px-1.5 py-1.5">
          <div className="flex items-center gap-1 w-full">
            <button
              type="button"
              onClick={() => {
                setPreviewSymbol(row.symbol)
                setPreviewName(row.name ?? '')
              }}
              className="flex items-center gap-1 text-left min-w-0"
            >
              <span className="font-mono text-foreground text-xs group-hover:text-accent transition-colors duration-150">
                {row.symbol}
              </span>
              {row.name && (
                <span className="text-xs text-secondary truncate group-hover:text-foreground transition-colors duration-150">
                  {row.name}
                </span>
              )}
              {board ? (
                <span className={`shrink-0 inline-flex items-center justify-center w-[18px] h-[18px] rounded text-[9px] font-bold leading-none border ${board.color}`}>
                  {board.label}
                </span>
              ) : null}
            </button>
            {/* 删除入口：默认减号图标，二次确认时替换为确定按钮 */}
            <div className="ml-auto pl-1 shrink-0">
              {confirmRemove === row.symbol ? (
                <div className="flex items-center gap-1">
                  <button
                    onClick={() => {
                      if (!selectedGroupId) return
                      removeItemMutation.mutate({ groupId: selectedGroupId, symbol: row.symbol })
                      setConfirmRemove(null)
                    }}
                    className="px-1.5 py-0.5 rounded text-[10px] text-danger bg-danger/10 hover:bg-danger/20 transition-colors"
                  >
                    确认
                  </button>
                  <button
                    onClick={() => setConfirmRemove(null)}
                    className="p-0.5 text-muted hover:text-foreground transition-colors"
                  >
                    <X className="h-3 w-3" />
                  </button>
                </div>
              ) : (
                <div className="flex items-center gap-1">
                  <button
                    onClick={() => setConfirmRemove(row.symbol)}
                    className="p-0.5 text-muted hover:text-danger transition-colors duration-150 ease-smooth"
                    aria-label="移除"
                    title="移除"
                  >
                    <Minus className="h-3.5 w-3.5" />
                  </button>
                  <button
                    onClick={() => {
                      if (!selectedGroupId) return
                      const newSymbols = [row.symbol, ...currentSymbols.filter(s => s !== row.symbol)]
                      moveItemToTopMutation.mutate({ groupId: selectedGroupId, symbols: newSymbols })
                    }}
                    disabled={moveItemToTopMutation.isPending || isFirst}
                    className="p-0.5 text-muted hover:text-accent transition-colors duration-150 ease-smooth disabled:opacity-30 disabled:hover:text-muted"
                    aria-label="移到顶部"
                    title="移到顶部"
                  >
                    <ChevronsUp className="h-3.5 w-3.5" />
                  </button>
                </div>
              )}
            </div>
          </div>
        </td>
      )
    }
    const key = col.source.key
    const price = row.rt_price ?? row.close
    const pct = row.rt_pct ?? row.change_pct

    // 实时行情列：price/pct/amount 使用 rt_ 回退
    const numCls = 'px-2 py-1.5 text-right num tabular-nums'
    if (key === 'price') {
      return <td className={`${numCls} ${priceColorClass(pct)}`}>{fmtPrice(price)}</td>
    }
    if (key === 'pct') {
      return <td className={`${numCls} ${priceColorClass(pct)}`}>{fmtPct(pct)}</td>
    }
    if (key === 'amount') {
      return <td className={`${numCls} text-secondary`}>{fmtBigNum(row.rt_amount ?? row.amount)}</td>
    }
    if (key === 'turnover') {
      return <td className={`${numCls} ${turnoverColor(row.turnover_rate)}`}>{row.turnover_rate != null ? `${row.turnover_rate.toFixed(2)}%` : '—'}</td>
    }

    // 信号列
    if (key === 'signals') {
      const signals = getSignals(row)
      return (
        <td className="px-2 py-1.5">
          {signals.length > 0 && (
            <div className="flex flex-wrap gap-0.5">
              {signals.slice(0, 3).map((s) => (
                <span key={s.label} className={`inline-block px-1.5 py-px rounded text-[10px] font-medium leading-tight ${signalCls(s.type)}`}>
                  {s.label}
                </span>
              ))}
              {signals.length > 3 && (
                <span className="text-[10px] text-muted">+{signals.length - 3}</span>
              )}
            </div>
          )}
        </td>
      )
    }

    // 日k蜡烛图列
    if (key === 'candle') {
      return (
        <td
          className="pl-2 pr-3 py-1.5"
          style={{ width: candleSize.width + 4, minWidth: candleSize.width + 4, maxWidth: candleSize.width + 4, height: candleSize.height }}
        >
          <MiniCandlestick rows={klineData[row.symbol] ?? []} width={candleSize.width} height={candleSize.height} />
        </td>
      )
    }

    // 分时图列
    if (key === 'intraday') {
      // 指数无本地分钟K数据，分时列降级为占位符
      if (row.asset_type === 'index') {
        const iw = intradayChartVisible ? intradayResolved.width : 40
        const ih = intradayChartVisible ? intradayResolved.height : 40
        return (
          <td className="pl-3 pr-2 py-1.5 border-l border-border/30" style={{ width: iw + 4, minWidth: iw + 4, maxWidth: iw + 4, height: ih }}>
            <div className="flex items-center justify-center">
              <span className="text-[10px] text-muted">—</span>
            </div>
          </td>
        )
      }
      const rows: MinuteKlineRow[] = minuteData[row.symbol] ?? []
      const iw = intradayChartVisible ? intradayResolved.width : 40
      const ih = intradayChartVisible ? intradayResolved.height : 40
      return (
        <td className="pl-3 pr-2 py-1.5 border-l border-border/30" style={{ width: iw + 4, minWidth: iw + 4, maxWidth: iw + 4, height: ih }}>
          <div className="flex items-center justify-center">
            {intradayChartVisible
              ? <MiniIntraday rows={rows} prevClose={row.prev_close} changePct={row.change_pct} width={iw - 4} height={ih} />
              : <span className="text-[10px] text-muted">分时</span>}
          </div>
        </td>
      )
    }

    return renderBuiltinDataCell(row, col)
  }, [selectedGroupId, removeItemMutation, expandedCells, handleToggleExpand, candleSize, klineData, intradayChartVisible, intradayResolved, minuteData])

  const renderSidebarView = () => (
    <div className="flex h-full">
      <div className="w-64 border-r bg-surface p-4 overflow-y-auto">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">分组</h2>
          <div className="flex items-center gap-2">
            {/* 列表/卡片视图切换 */}
            <button
              type="button"
              onClick={() => setSidebarOrCardsViewMutation.mutate(sidebarOrCardsView === 'sidebar' ? 'cards' : 'sidebar')}
              disabled={setSidebarOrCardsViewMutation.isPending}
              className="inline-flex items-center justify-center h-8 w-8 rounded-btn bg-elevated hover:bg-elevated/80 text-secondary hover:text-foreground transition-colors duration-150"
              title={sidebarOrCardsView === 'sidebar' ? '切换到卡片视图' : '切换到列表视图'}
            >
              {sidebarOrCardsView === 'sidebar' ? <Grid className="h-4 w-4" /> : <List className="h-4 w-4" />}
            </button>
            <button
              type="button"
              onClick={toggleSortMode}
              className="inline-flex items-center justify-center h-8 w-8 rounded-btn bg-elevated hover:bg-elevated/80 text-secondary hover:text-foreground transition-colors duration-150"
              title={sortMode === 'custom' ? '切换到按涨跌幅递减排序' : sortMode === 'descending' ? '切换到按涨跌幅递增排序' : '切换到自定义排序'}
            >
              {sortMode === 'custom' && <ArrowUpDown className="h-4 w-4" />}
              {sortMode === 'ascending' && <ArrowUp className="h-4 w-4" />}
              {sortMode === 'descending' && <ArrowDown className="h-4 w-4" />}
            </button>
            <div className="relative" ref={sidebarSettingsMenuRef}>
              <button
                type="button"
                onClick={() => setSidebarSettingsMenuOpen(!sidebarSettingsMenuOpen)}
                className="inline-flex items-center justify-center h-8 w-8 rounded-btn bg-elevated hover:bg-elevated/80 text-secondary hover:text-foreground transition-colors duration-150"
              >
                <Settings className="h-4 w-4" />
              </button>
              <AnimatePresence>
                {sidebarSettingsMenuOpen && (
                  <motion.div
                    initial={{ opacity: 0, y: -4 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: -4 }}
                    className="absolute right-0 top-full mt-1 w-40 bg-surface border border-border rounded-btn shadow-xl z-50"
                  >
                    <button
                      type="button"
                      onClick={() => {
                        setSidebarSettingsMenuOpen(false)
                        setCreateDialogOpen(true)
                      }}
                      className="w-full text-left px-3 py-2 text-xs hover:bg-elevated flex items-center gap-2"
                    >
                      <Plus className="h-3.5 w-3.5" />
                      新建分组
                    </button>
                    {selectedGroupId && (
                      <>
                        <div className="border-t border-border my-1" />
                        <button
                          type="button"
                          onClick={() => {
                            setSidebarSettingsMenuOpen(false)
                            const group = groupsQuery.data?.groups?.find(g => g.group_id === selectedGroupId)
                            if (group) {
                              setCurrentGroupForDialog(group)
                              setNewGroupName(group.name)
                              setRenameDialogOpen(true)
                            }
                          }}
                          className="w-full text-left px-3 py-2 text-xs hover:bg-elevated flex items-center gap-2"
                        >
                          <Edit2 className="h-3.5 w-3.5" />
                          重命名分组
                        </button>
                        <button
                          type="button"
                          onClick={() => {
                            setSidebarSettingsMenuOpen(false)
                            const group = groupsQuery.data?.groups?.find(g => g.group_id === selectedGroupId)
                            if (group) {
                              setCurrentGroupForDialog(group)
                              setDeleteDialogOpen(true)
                            }
                          }}
                          className="w-full text-left px-3 py-2 text-xs hover:bg-elevated flex items-center gap-2 text-danger"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                          删除分组
                        </button>
                      </>
                    )}
                    <div className="border-t border-border my-1" />
                    <button
                      type="button"
                      onClick={() => {
                        setSidebarSettingsMenuOpen(false)
                        setAvgPctModeMutation.mutate('simple')
                      }}
                      disabled={setAvgPctModeMutation.isPending}
                      className={cn(
                        'w-full text-left px-3 py-2 text-xs hover:bg-elevated flex items-center gap-2',
                        avgPctMode === 'simple' ? 'text-accent bg-accent/10' : ''
                      )}
                    >
                      {avgPctMode === 'simple' && <Check className="h-3.5 w-3.5" />}
                      {avgPctMode !== 'simple' && <span className="w-3.5 h-3.5" />}
                      算术平均
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setSidebarSettingsMenuOpen(false)
                        setAvgPctModeMutation.mutate('weighted')
                      }}
                      disabled={setAvgPctModeMutation.isPending}
                      className={cn(
                        'w-full text-left px-3 py-2 text-xs hover:bg-elevated flex items-center gap-2',
                        avgPctMode === 'weighted' ? 'text-accent bg-accent/10' : ''
                      )}
                    >
                      {avgPctMode === 'weighted' && <Check className="h-3.5 w-3.5" />}
                      {avgPctMode !== 'weighted' && <span className="w-3.5 h-3.5" />}
                      加权平均
                    </button>
                    <div className="border-t border-border my-1" />
                    <button
                      type="button"
                      onClick={() => {
                        setSidebarSettingsMenuOpen(false)
                        exportConfigMutation.mutate()
                      }}
                      disabled={exportConfigMutation.isPending}
                      className="w-full text-left px-3 py-2 text-xs hover:bg-elevated flex items-center gap-2"
                    >
                      <RefreshCw className={cn("h-3.5 w-3.5", exportConfigMutation.isPending && "animate-spin")} />
                      导出配置
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setSidebarSettingsMenuOpen(false)
                        fileInputRef.current?.click()
                      }}
                      className="w-full text-left px-3 py-2 text-xs hover:bg-elevated flex items-center gap-2"
                    >
                      <Plus className="h-3.5 w-3.5" />
                      导入配置
                    </button>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
          </div>
        </div>
        <div className="space-y-2">
          {sortedGroups.map((group, index) => {
            const rows = allGroupItems?.[group.group_id]
            const avgChange = rows ? calculateGroupAvgChange(group, { [group.group_id]: rows }) : { simple: null, weighted: null }
            const displayChange = avgPctMode === 'simple' ? avgChange.simple : avgChange.weighted
            const isFirst = index === 0 && sortMode === 'custom'

            const handleMoveToTop = (e: React.MouseEvent) => {
              e.stopPropagation()
              const currentGroups = groupsQuery.data?.groups?.map(g => g.group_id) || []
              const newOrder = [group.group_id, ...currentGroups.filter(id => id !== group.group_id)]
              moveGroupToTopMutation.mutate(newOrder)
            }

            return (
              <div
                key={group.group_id}
                className={cn(
                  'flex items-center gap-2 p-2 rounded-btn transition-colors',
                  selectedGroupId === group.group_id ? 'bg-elevated text-foreground' : 'hover:bg-elevated/30'
                )}
              >
                <button
                  onClick={() => setSelectedGroupId(group.group_id)}
                  className="flex-1 text-left min-w-0"
                >
                  <span className="font-medium truncate">{group.name}</span>
                </button>
                <div className="flex items-center gap-2 shrink-0">
                  {displayChange != null && (
                    <span className={cn('text-xs font-medium', displayChange >= 0 ? 'text-bull' : 'text-bear')}>{fmtPct(displayChange)}</span>
                  )}
                  <span className="text-xs opacity-70">{group.item_count} 只</span>
                  {sortMode === 'custom' && (
                    <button
                      onClick={handleMoveToTop}
                      disabled={moveGroupToTopMutation.isPending || isFirst}
                      className={cn(
                        'p-1.5 rounded transition-colors',
                        isFirst
                          ? 'opacity-30 cursor-default'
                          : 'text-muted hover:text-accent hover:bg-accent/10'
                      )}
                      title={isFirst ? '已在顶部' : '移到顶部'}
                    >
                      <ChevronsUp className="h-4 w-4" />
                    </button>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      </div>
      <div className="flex-1 overflow-y-auto">
        {selectedGroupId ? (
          <div className="p-6">
            <div className="flex items-center justify-between mb-6">
              <h1 className="text-2xl font-bold">{groupsQuery.data?.groups?.find(g => g.group_id === selectedGroupId)?.name}</h1>
              <div className="flex items-center gap-2">
                <StockSearchBox
                  onPreview={(sym, name) => { setPreviewSymbol(sym); setPreviewName(name) }}
                  existingSymbols={selectedGroupItemsQuery.data?.rows?.map(r => r.symbol) ?? []}
                  onAdd={(sym) => addItemMutation.mutate({ groupId: selectedGroupId, symbol: sym })}
                />
                <div className="w-px h-5 bg-border" />
                {/* 视图切换按钮 */}
                <button
                  onClick={toggleView}
                  className="inline-flex items-center justify-center h-8 w-8 rounded-btn bg-elevated hover:bg-elevated/80 text-secondary hover:text-foreground transition-colors duration-150"
                  title={viewMode === 'table' ? '卡片视图' : '列表视图'}
                >
                  {viewMode === 'table' ? <LayoutGrid className="h-4 w-4" /> : <List className="h-4 w-4" />}
                </button>
                {/* 个股涨跌幅排序按钮 */}
                <button
                  type="button"
                  onClick={toggleStockSortMode}
                  className="inline-flex items-center justify-center h-8 w-8 rounded-btn bg-elevated hover:bg-elevated/80 text-secondary hover:text-foreground transition-colors duration-150"
                  title={stockSortMode === 'default' ? '切换到个股按涨跌幅降序排序' : stockSortMode === 'descending' ? '切换到个股按涨跌幅升序排序' : '切换到个股默认排序'}
                >
                  {stockSortMode === 'default' && <ArrowUpDown className="h-4 w-4" />}
                  {stockSortMode === 'descending' && <ArrowDown className="h-4 w-4" />}
                  {stockSortMode === 'ascending' && <ArrowUp className="h-4 w-4" />}
                </button>
                {/* 刷新按钮 */}
                <button
                  onClick={() => selectedGroupItemsQuery.refetch()}
                  disabled={selectedGroupItemsQuery.isFetching}
                  className="inline-flex items-center justify-center h-8 w-8 rounded-btn bg-elevated hover:bg-elevated/80 text-secondary hover:text-foreground transition-colors duration-150 disabled:opacity-50"
                  title="刷新"
                >
                  <RefreshCw className={`h-4 w-4 ${selectedGroupItemsQuery.isFetching ? 'animate-spin' : ''}`} />
                </button>
                <div className="w-px h-5 bg-border" />
                {/* 自定义列按钮 */}
                <button
                  onClick={() => setCustomizerOpen(true)}
                  className="inline-flex items-center justify-center h-8 w-8 rounded-btn bg-elevated hover:bg-elevated/80 text-secondary hover:text-foreground transition-colors duration-150"
                  title="自定义列"
                >
                  <Settings2 className="h-4 w-4" />
                </button>
              </div>
            </div>
            {selectedGroupItemsQuery.isLoading ? (
              <EmptyState title="加载中…" />
            ) : selectedGroupItemsQuery.data?.rows && selectedGroupItemsQuery.data.rows.length > 0 ? (
              (() => {
                // Sort selected group's rows if stockSortMode is not default
                let sortedRows = [...selectedGroupItemsQuery.data.rows];
                if (stockSortMode !== 'default') {
                  sortedRows.sort((a, b) => {
                    const aPct = a.change_pct ?? 0;
                    const bPct = b.change_pct ?? 0;
                    return stockSortMode === 'descending' ? bPct - aPct : aPct - bPct;
                  });
                }
                
                return viewMode === 'table' ? (
                  <StockDataTable
                    columns={visibleColumns}
                    rows={sortRows(sortedRows, visibleColumns)}
                    renderCell={renderCell}
                    headerSticky={true}
                    rowKey={(row) => row.symbol}
                    sort={sort}
                    onSortToggle={handleSortToggle}
                    // 日k列表头：标签 + 显示/隐藏眼睛按钮
                    renderHeaderContent={(col) => {
                      if (col.source.type === 'builtin' && col.source.key === 'candle') {
                        return (
                          <span className="inline-flex items-center justify-center gap-1.5">
                            <span>{col.label}</span>
                            <button
                              type="button"
                              onClick={(event) => { event.stopPropagation(); toggleDailyKChart() }}
                              className={`inline-flex items-center justify-center w-5 h-5 rounded transition-colors ${
                                dailyKChartVisible
                                  ? 'text-accent bg-accent/10 hover:bg-accent/20'
                                  : 'text-muted hover:text-foreground hover:bg-elevated'
                              }`}
                              title={dailyKChartVisible ? '隐藏日k蜡烛' : '显示日k蜡烛'}
                              aria-label={dailyKChartVisible ? '隐藏日k蜡烛' : '显示日k蜡烛'}
                            >
                              {dailyKChartVisible ? <Eye className="h-3.5 w-3.5" /> : <EyeOff className="h-3.5 w-3.5" />}
                            </button>
                          </span>
                        )
                      }
                      if (col.source.type === 'builtin' && col.source.key === 'intraday') {
                        const intradayAutoRefresh = intradayRefreshEnabled && realtimeRunning
                        return (
                          <span className="inline-flex items-center justify-center gap-1.5">
                            <span>{col.label}</span>
                            <button
                              type="button"
                              onClick={(event) => { event.stopPropagation(); toggleIntradayChart() }}
                              className={`inline-flex items-center justify-center w-5 h-5 rounded transition-colors ${
                                intradayChartVisible
                                  ? 'text-accent bg-accent/10 hover:bg-accent/20'
                                  : 'text-muted hover:text-foreground hover:bg-elevated'
                              }`}
                              title={intradayChartVisible ? '隐藏分时图' : '显示分时图'}
                              aria-label={intradayChartVisible ? '隐藏分时图' : '显示分时图'}
                            >
                              {intradayChartVisible ? <Eye className="h-3.5 w-3.5" /> : <EyeOff className="h-3.5 w-3.5" />}
                            </button>
                            {/* 分时图显示 且 未开自动轮询时，提供手动刷新按钮 */}
                            {intradayChartVisible && !intradayAutoRefresh && (
                              <button
                                type="button"
                                onClick={(event) => { event.stopPropagation(); minuteBatch.refetch() }}
                                disabled={minuteBatch.isFetching}
                                className="inline-flex items-center justify-center w-5 h-5 rounded text-muted hover:text-accent hover:bg-accent/10 transition-colors disabled:opacity-40"
                                title="刷新分时数据"
                                aria-label="刷新分时数据"
                              >
                                <RefreshCw className={`h-3.5 w-3.5 ${minuteBatch.isFetching ? 'animate-spin' : ''}`} />
                              </button>
                            )}
                            {/* 自动轮询中：显示旋转图标提示正在实时刷新 */}
                            {intradayChartVisible && intradayAutoRefresh && (
                              <RefreshCw className="h-3 w-3 text-accent/60 animate-spin" aria-label="实时刷新中" />
                            )}
                          </span>
                        )
                      }
                      return undefined
                    }}
                  />
                ) : (
                  <div className={`grid gap-3 ${
                    cardColumns === 6 ? '2xl:grid-cols-6' :
                    cardColumns === 5 ? 'xl:grid-cols-5' :
                    cardColumns === 4 ? 'md:grid-cols-4' :
                    cardColumns === 3 ? 'sm:grid-cols-3' :
                    'grid-cols-2'
                  }`}>
                    {sortedRows.map((row) => (
                      <StockCard
                        key={row.symbol}
                        r={row}
                        onPreview={handleCardPreview}
                        onConfirmRemove={handleCardConfirmRemove}
                        onCancelRemove={handleCardCancelRemove}
                        onRequestRemove={handleCardRequestRemove}
                        isConfirming={confirmRemove === row.symbol}
                      />
                    ))}
                  </div>
                )
              })()
            ) : (
              <EmptyState title="该分组暂无股票" hint="点击右上角新建分组或添加股票" />
            )}
          </div>
        ) : (
          <EmptyState title="请选择一个分组" hint="从左侧选择或创建一个新的分组" />
        )}
      </div>
    </div>
  )

  const renderCardsView = () => (
    <div className="flex h-full">
      {/* 左侧侧边栏 - 显示所有分组 */}
      <div className="w-64 border-r bg-surface p-4 overflow-y-auto">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">分组</h2>
          <div className="flex items-center gap-2">
            {/* 列表/卡片视图切换 */}
            <button
              type="button"
              onClick={() => setSidebarOrCardsViewMutation.mutate(sidebarOrCardsView === 'sidebar' ? 'cards' : 'sidebar')}
              disabled={setSidebarOrCardsViewMutation.isPending}
              className="inline-flex items-center justify-center h-8 w-8 rounded-btn bg-elevated hover:bg-elevated/80 text-secondary hover:text-foreground transition-colors duration-150"
              title={sidebarOrCardsView === 'sidebar' ? '切换到卡片视图' : '切换到列表视图'}
            >
              {sidebarOrCardsView === 'sidebar' ? <Grid className="h-4 w-4" /> : <List className="h-4 w-4" />}
            </button>
            <button
              type="button"
              onClick={toggleSortMode}
              className="inline-flex items-center justify-center h-8 w-8 rounded-btn bg-elevated hover:bg-elevated/80 text-secondary hover:text-foreground transition-colors duration-150"
              title={sortMode === 'custom' ? '切换到按涨跌幅递减排序' : sortMode === 'descending' ? '切换到按涨跌幅递增排序' : '切换到自定义排序'}
            >
              {sortMode === 'custom' && <ArrowUpDown className="h-4 w-4" />}
              {sortMode === 'ascending' && <ArrowUp className="h-4 w-4" />}
              {sortMode === 'descending' && <ArrowDown className="h-4 w-4" />}
            </button>
            <div className="relative" ref={sidebarSettingsMenuRef}>
              <button
                type="button"
                onClick={() => setSidebarSettingsMenuOpen(!sidebarSettingsMenuOpen)}
                className="inline-flex items-center justify-center h-8 w-8 rounded-btn bg-elevated hover:bg-elevated/80 text-secondary hover:text-foreground transition-colors duration-150"
              >
                <Settings className="h-4 w-4" />
              </button>
              <AnimatePresence>
                {sidebarSettingsMenuOpen && (
                  <motion.div
                    initial={{ opacity: 0, y: -4 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: -4 }}
                    className="absolute right-0 top-full mt-1 w-40 bg-surface border border-border rounded-btn shadow-xl z-50"
                  >
                    <button
                      type="button"
                      onClick={() => {
                        setSidebarSettingsMenuOpen(false)
                        setCreateDialogOpen(true)
                      }}
                      className="w-full text-left px-3 py-2 text-xs hover:bg-elevated flex items-center gap-2"
                    >
                      <Plus className="h-3.5 w-3.5" />
                      新建分组
                    </button>
                    {selectedGroupId && (
                      <>
                        <div className="border-t border-border my-1" />
                        <button
                          type="button"
                          onClick={() => {
                            setSidebarSettingsMenuOpen(false)
                            const group = groupsQuery.data?.groups?.find(g => g.group_id === selectedGroupId)
                            if (group) {
                              setCurrentGroupForDialog(group)
                              setNewGroupName(group.name)
                              setRenameDialogOpen(true)
                            }
                          }}
                          className="w-full text-left px-3 py-2 text-xs hover:bg-elevated flex items-center gap-2"
                        >
                          <Edit2 className="h-3.5 w-3.5" />
                          重命名分组
                        </button>
                        <button
                          type="button"
                          onClick={() => {
                            setSidebarSettingsMenuOpen(false)
                            const group = groupsQuery.data?.groups?.find(g => g.group_id === selectedGroupId)
                            if (group) {
                              setCurrentGroupForDialog(group)
                              setDeleteDialogOpen(true)
                            }
                          }}
                          className="w-full text-left px-3 py-2 text-xs hover:bg-elevated flex items-center gap-2 text-danger"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                          删除分组
                        </button>
                      </>
                    )}
                    <div className="border-t border-border my-1" />
                    <button
                      type="button"
                      onClick={() => {
                        setSidebarSettingsMenuOpen(false)
                        setAvgPctModeMutation.mutate('simple')
                      }}
                      disabled={setAvgPctModeMutation.isPending}
                      className={cn(
                        'w-full text-left px-3 py-2 text-xs hover:bg-elevated flex items-center gap-2',
                        avgPctMode === 'simple' ? 'text-accent bg-accent/10' : ''
                      )}
                    >
                      {avgPctMode === 'simple' && <Check className="h-3.5 w-3.5" />}
                      {avgPctMode !== 'simple' && <span className="w-3.5 h-3.5" />}
                      算术平均
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setSidebarSettingsMenuOpen(false)
                        setAvgPctModeMutation.mutate('weighted')
                      }}
                      disabled={setAvgPctModeMutation.isPending}
                      className={cn(
                        'w-full text-left px-3 py-2 text-xs hover:bg-elevated flex items-center gap-2',
                        avgPctMode === 'weighted' ? 'text-accent bg-accent/10' : ''
                      )}
                    >
                      {avgPctMode === 'weighted' && <Check className="h-3.5 w-3.5" />}
                      {avgPctMode !== 'weighted' && <span className="w-3.5 h-3.5" />}
                      加权平均
                    </button>
                    <div className="border-t border-border my-1" />
                    <button
                      type="button"
                      onClick={() => {
                        setSidebarSettingsMenuOpen(false)
                        exportConfigMutation.mutate()
                      }}
                      disabled={exportConfigMutation.isPending}
                      className="w-full text-left px-3 py-2 text-xs hover:bg-elevated flex items-center gap-2"
                    >
                      <RefreshCw className={cn("h-3.5 w-3.5", exportConfigMutation.isPending && "animate-spin")} />
                      导出配置
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setSidebarSettingsMenuOpen(false)
                        fileInputRef.current?.click()
                      }}
                      className="w-full text-left px-3 py-2 text-xs hover:bg-elevated flex items-center gap-2"
                    >
                      <Plus className="h-3.5 w-3.5" />
                      导入配置
                    </button>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
          </div>
        </div>
        <div className="space-y-2">
          {sortedGroups.map((group, index) => {
            const rows = allGroupItems?.[group.group_id]
            const avgChange = rows ? calculateGroupAvgChange(group, { [group.group_id]: rows }) : { simple: null, weighted: null }
            const displayChange = avgPctMode === 'simple' ? avgChange.simple : avgChange.weighted
            const isFirst = index === 0 && sortMode === 'custom'

            const handleMoveToTop = (e: React.MouseEvent) => {
              e.stopPropagation()
              const currentGroups = groupsQuery.data?.groups?.map(g => g.group_id) || []
              const newOrder = [group.group_id, ...currentGroups.filter(id => id !== group.group_id)]
              moveGroupToTopMutation.mutate(newOrder)
            }

            return (
              <div
                key={group.group_id}
                className={cn(
                  'flex items-center gap-2 p-2 rounded-btn transition-colors cursor-pointer',
                  selectedGroupId === group.group_id ? 'bg-elevated text-foreground' : 'hover:bg-elevated/30'
                )}
                onClick={() => {
                  setSelectedGroupId(group.group_id);
                }}
              >
                <span className="font-medium truncate flex-1">{group.name}</span>
                <div className="flex items-center gap-2 shrink-0">
                  {displayChange != null && (
                    <span className={cn('text-xs font-medium', displayChange >= 0 ? 'text-bull' : 'text-bear')}>{fmtPct(displayChange)}</span>
                  )}
                  <span className="text-xs opacity-70">{group.item_count} 只</span>
                  {sortMode === 'custom' && (
                    <button
                      onClick={handleMoveToTop}
                      disabled={moveGroupToTopMutation.isPending || isFirst}
                      className={cn(
                        'p-1.5 rounded transition-colors',
                        isFirst
                          ? 'opacity-30 cursor-default'
                          : 'text-muted hover:text-accent hover:bg-accent/10'
                      )}
                      title={isFirst ? '已在顶部' : '移到顶部'}
                    >
                      <ChevronsUp className="h-4 w-4" />
                    </button>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      </div>

      {/* 右侧内容 */}
      <div className="flex-1 flex flex-col h-full overflow-hidden">
        {/* 上半部分：分组列表 */}
        <div className={`flex-shrink-0 overflow-y-auto ${selectedGroupId ? 'h-1/2' : 'h-full'} border-b border-border`}>
          <div className="p-6">
            <div className="flex items-center justify-between mb-6">
              <h1 className="text-2xl font-bold">自选分组</h1>
                <div className="flex items-center gap-2">
                  {/* 视图切换按钮 */}
                  <button
                    type="button"
                    onClick={() => setCardsViewMode((cardsViewMode as string) === 'cards' ? 'list' : 'cards')}
                    className="inline-flex items-center justify-center h-8 w-8 rounded-btn bg-elevated hover:bg-elevated/80 text-secondary hover:text-foreground transition-colors duration-150"
                    title={(cardsViewMode as string) === 'cards' ? '切换到列表视图' : '切换到卡片视图'}
                  >
                    {(cardsViewMode as string) === 'cards' ? <List className="h-4 w-4" /> : <Grid className="h-4 w-4" />}
                  </button>
                <button
                  type="button"
                  onClick={toggleStockSortMode}
                  className="inline-flex items-center justify-center h-8 w-8 rounded-btn bg-elevated hover:bg-elevated/80 text-secondary hover:text-foreground transition-colors duration-150"
                  title={stockSortMode === 'default' ? '切换到个股按涨跌幅降序排序' : stockSortMode === 'descending' ? '切换到个股按涨跌幅升序排序' : '切换到个股默认排序'}
                >
                  {stockSortMode === 'default' && <ArrowUpDown className="h-4 w-4" />}
                  {stockSortMode === 'descending' && <ArrowDown className="h-4 w-4" />}
                  {stockSortMode === 'ascending' && <ArrowUp className="h-4 w-4" />}
                </button>
                {/* 刷新按钮 */}
                <button
                  type="button"
                  onClick={() => refetchAllGroupItems()}
                  disabled={isFetchingAllGroupItems}
                  className="inline-flex items-center justify-center h-8 w-8 rounded-btn bg-elevated hover:bg-elevated/80 text-secondary hover:text-foreground transition-colors duration-150 disabled:opacity-50"
                  title="刷新分组数据"
                >
                  <RefreshCw className={`h-4 w-4 ${isFetchingAllGroupItems ? 'animate-spin' : ''}`} />
                </button>
              </div>
            </div>
            
            {/* 根据视图模式渲染不同内容 */}
            {(cardsViewMode as string) === 'cards' ? (
              <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6 gap-4">
                {sortedGroups.map((group) => (
                  <div 
                    key={group.group_id}
                    className={cn(
                      'cursor-pointer transition-all duration-200',
                      selectedGroupId === group.group_id ? 'ring-2 ring-accent rounded-lg' : ''
                    )}
                  >
                    <GroupCard
                      group={group}
                      onSelect={(id) => { 
                        setSelectedGroupId(id === selectedGroupId ? null : id); 
                      }}
                      avgPctMode={avgPctMode}
                      items={allGroupItems?.[group.group_id]}
                    />
                  </div>
                ))}
              </div>
            ) : (
              <div className="space-y-8">
                {sortedGroups.map((group) => {
                  const rows = allGroupItems?.[group.group_id]
                  const avgChange = rows ? calculateGroupAvgChange(group, { [group.group_id]: rows }) : { simple: null, weighted: null }
                  const displayChange = avgPctMode === 'simple' ? avgChange.simple : avgChange.weighted
                  
                  return (
                    <div 
                      key={group.group_id} 
                      className={cn(
                        'space-y-4 p-3 rounded-lg transition-all duration-200 cursor-pointer',
                        selectedGroupId === group.group_id ? 'bg-elevated/50' : 'hover:bg-elevated/20'
                      )}
                      onClick={() => setSelectedGroupId(group.group_id === selectedGroupId ? null : group.group_id)}
                    >
                      {/* 分组标题栏 */}
                      <div className="flex items-center justify-between">
                        <div className="flex items-center gap-3">
                          <h2 className="text-lg font-semibold">{group.name}</h2>
                          {displayChange != null && (
                            <span className={cn('text-sm font-medium', displayChange >= 0 ? 'text-bull' : 'text-bear')}>{fmtPct(displayChange)}</span>
                          )}
                          <span className="text-sm text-muted">{group.item_count} 只</span>
                        </div>
                      </div>
                      
                      {/* 股票卡片网格 */}
                      {rows && rows.length > 0 ? (
                        <div className={`grid gap-3 ${
                          cardColumns === 6 ? '2xl:grid-cols-6' :
                          cardColumns === 5 ? 'xl:grid-cols-5' :
                          cardColumns === 4 ? 'md:grid-cols-4' :
                          cardColumns === 3 ? 'sm:grid-cols-3' :
                          'grid-cols-2'
                        }`}>
                          {rows.map((row) => (
                            <StockCard
                              key={row.symbol}
                              r={row}
                              onPreview={handleCardPreview}
                              onConfirmRemove={(sym) => {
                                if (selectedGroupId) {
                                  removeItemMutation.mutate({ groupId: group.group_id, symbol: sym })
                                }
                                setConfirmRemove(null)
                              }}
                              onCancelRemove={() => setConfirmRemove(null)}
                              onRequestRemove={(sym) => {
                                setSelectedGroupId(group.group_id)
                                setConfirmRemove(sym)
                              }}
                              isConfirming={confirmRemove === row.symbol && selectedGroupId === group.group_id}
                              hideSymbol={true}
                              swapPriceAndPct={true}
                            />
                          ))}
                        </div>
                      ) : (
                        <div className="text-sm text-muted py-4">该分组暂无股票</div>
                      )}
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        </div>

        {/* 下半部分：分组详情（当选择分组时显示） */}
        {selectedGroupId && (
          <div className="flex-1 flex flex-col overflow-hidden">
            <div className="flex items-center justify-between px-6 py-3 border-b border-border shrink-0 bg-elevated/30">
              <h2 className="text-lg font-semibold">{groupsQuery.data?.groups?.find(g => g.group_id === selectedGroupId)?.name}</h2>
              <div className="flex items-center gap-2">
                <StockSearchBox
                  onPreview={(sym, name) => { setPreviewSymbol(sym); setPreviewName(name) }}
                  existingSymbols={selectedGroupItemsQuery.data?.rows?.map(r => r.symbol) ?? []}
                  onAdd={(sym) => addItemMutation.mutate({ groupId: selectedGroupId, symbol: sym })}
                />
                <div className="w-px h-5 bg-border" />
                {/* 视图切换按钮 */}
                <button
                  onClick={toggleView}
                  className="inline-flex items-center justify-center h-8 w-8 rounded-btn bg-elevated hover:bg-elevated/80 text-secondary hover:text-foreground transition-colors duration-150"
                  title={viewMode === 'table' ? '卡片视图' : '列表视图'}
                >
                  {viewMode === 'table' ? <LayoutGrid className="h-4 w-4" /> : <List className="h-4 w-4" />}
                </button>
                {/* 个股涨跌幅排序按钮 */}
                <button
                  type="button"
                  onClick={toggleStockSortMode}
                  className="inline-flex items-center justify-center h-8 w-8 rounded-btn bg-elevated hover:bg-elevated/80 text-secondary hover:text-foreground transition-colors duration-150"
                  title={stockSortMode === 'default' ? '切换到个股按涨跌幅降序排序' : stockSortMode === 'descending' ? '切换到个股按涨跌幅升序排序' : '切换到个股默认排序'}
                >
                  {stockSortMode === 'default' && <ArrowUpDown className="h-4 w-4" />}
                  {stockSortMode === 'descending' && <ArrowDown className="h-4 w-4" />}
                  {stockSortMode === 'ascending' && <ArrowUp className="h-4 w-4" />}
                </button>
                {/* 刷新按钮 */}
                <button
                  onClick={() => selectedGroupItemsQuery.refetch()}
                  disabled={selectedGroupItemsQuery.isFetching}
                  className="inline-flex items-center justify-center h-8 w-8 rounded-btn bg-elevated hover:bg-elevated/80 text-secondary hover:text-foreground transition-colors duration-150 disabled:opacity-50"
                  title="刷新"
                >
                  <RefreshCw className={`h-4 w-4 ${selectedGroupItemsQuery.isFetching ? 'animate-spin' : ''}`} />
                </button>
                <div className="w-px h-5 bg-border" />
                {/* 自定义列按钮 */}
                <button
                  onClick={() => setCustomizerOpen(true)}
                  className="inline-flex items-center justify-center h-8 w-8 rounded-btn bg-elevated hover:bg-elevated/80 text-secondary hover:text-foreground transition-colors duration-150"
                  title="自定义列"
                >
                  <Settings2 className="h-4 w-4" />
                </button>
                <button
                  type="button"
                  onClick={() => setSelectedGroupId(null)}
                  className="h-8 w-8 inline-flex items-center justify-center rounded-btn text-secondary hover:bg-elevated"
                  title="关闭详情"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
            </div>
            <div className="p-6 overflow-y-auto flex-1">
              {selectedGroupItemsQuery.isLoading ? (
                <EmptyState title="加载中…" />
              ) : selectedGroupItemsQuery.data?.rows && selectedGroupItemsQuery.data.rows.length > 0 ? (
                (() => {
                  // Sort selected group's rows if stockSortMode is not default
                  let sortedRows = [...selectedGroupItemsQuery.data.rows];
                  if (stockSortMode !== 'default') {
                    sortedRows.sort((a, b) => {
                      const aPct = a.change_pct ?? 0;
                      const bPct = b.change_pct ?? 0;
                      return stockSortMode === 'descending' ? bPct - aPct : aPct - bPct;
                    });
                  }
                  
                  return (
                    <div className="flex flex-col">
                      {viewMode === 'table' ? (
                        <StockDataTable
                          columns={visibleColumns}
                          rows={sortRows(sortedRows, visibleColumns)}
                          renderCell={renderCell}
                          headerSticky={true}
                          rowKey={(row) => row.symbol}
                          sort={sort}
                          onSortToggle={handleSortToggle}
                          renderHeaderContent={(col) => {
                            if (col.source.type === 'builtin' && col.source.key === 'candle') {
                              return (
                                <span className="inline-flex items-center justify-center gap-1.5">
                                  <span>{col.label}</span>
                                  <button
                                    type="button"
                                    onClick={(event) => { event.stopPropagation(); toggleDailyKChart() }}
                                    className={`inline-flex items-center justify-center w-5 h-5 rounded transition-colors ${
                                      dailyKChartVisible
                                        ? 'text-accent bg-accent/10 hover:bg-accent/20'
                                        : 'text-muted hover:text-foreground hover:bg-elevated'
                                    }`}
                                    title={dailyKChartVisible ? '隐藏日k蜡烛' : '显示日k蜡烛'}
                                    aria-label={dailyKChartVisible ? '隐藏日k蜡烛' : '显示日k蜡烛'}
                                  >
                                    {dailyKChartVisible ? <Eye className="h-3.5 w-3.5" /> : <EyeOff className="h-3.5 w-3.5" />}
                                  </button>
                                </span>
                              );
                            }
                            if (col.source.type === 'builtin' && col.source.key === 'intraday') {
                              const intradayAutoRefresh = (prefsData?.minute_intraday_refresh ?? false) && realtimeRunning;
                              return (
                                <span className="inline-flex items-center justify-center gap-1.5">
                                  <span>{col.label}</span>
                                  <button
                                    type="button"
                                    onClick={(event) => { event.stopPropagation(); toggleIntradayChart() }}
                                    className={`inline-flex items-center justify-center w-5 h-5 rounded transition-colors ${
                                      intradayChartVisible
                                        ? 'text-accent bg-accent/10 hover:bg-accent/20'
                                        : 'text-muted hover:text-foreground hover:bg-elevated'
                                    }`}
                                    title={intradayChartVisible ? '隐藏分时图' : '显示分时图'}
                                    aria-label={intradayChartVisible ? '隐藏分时图' : '显示分时图'}
                                  >
                                    {intradayChartVisible ? <Eye className="h-3.5 w-3.5" /> : <EyeOff className="h-3.5 w-3.5" />}
                                  </button>
                                  {/* 分时图显示 且 未开自动轮询时，提供手动刷新按钮 */}
                                  {intradayChartVisible && !intradayAutoRefresh && (
                                    <button
                                      type="button"
                                      onClick={(event) => { event.stopPropagation(); minuteBatch.refetch() }}
                                      disabled={minuteBatch.isFetching}
                                      className="inline-flex items-center justify-center w-5 h-5 rounded text-muted hover:text-accent hover:bg-accent/10 transition-colors disabled:opacity-40"
                                      title="刷新分时数据"
                                      aria-label="刷新分时数据"
                                    >
                                      <RefreshCw className={`h-3.5 w-3.5 ${minuteBatch.isFetching ? 'animate-spin' : ''}`} />
                                    </button>
                                  )}
                                  {/* 自动轮询中：显示旋转图标提示正在实时刷新 */}
                                  {intradayChartVisible && intradayAutoRefresh && (
                                    <RefreshCw className="h-3 w-3 text-accent/60 animate-spin" aria-label="实时刷新中" />
                                  )}
                                </span>
                              );
                            }
                            return undefined;
                          }}
                        />
                      ) : (
                        <div className={`grid gap-3 ${
                          cardColumns === 6 ? '2xl:grid-cols-6' :
                          cardColumns === 5 ? 'xl:grid-cols-5' :
                          cardColumns === 4 ? 'md:grid-cols-4' :
                          cardColumns === 3 ? 'sm:grid-cols-3' :
                          'grid-cols-2'
                        }`}>
                          {sortedRows.map((row) => (
                            <StockCard
                              key={row.symbol}
                              r={row}
                              onPreview={handleCardPreview}
                              onConfirmRemove={handleCardConfirmRemove}
                              onCancelRemove={handleCardCancelRemove}
                              onRequestRemove={handleCardRequestRemove}
                              isConfirming={confirmRemove === row.symbol}
                            />
                          ))}
                        </div>
                      )}
                    </div>
                  )
                })()
              ) : (
                <EmptyState title="该分组暂无股票" />
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  )

  return (
    <div className="h-full">
      {sidebarOrCardsView === 'sidebar' ? renderSidebarView() : renderCardsView()}

      {createDialogOpen && (
        <Modal
          onClose={() => setCreateDialogOpen(false)}
          panelClassName="w-[90vw] max-w-md bg-surface border border-border rounded-lg shadow-xl"
        >
          <div className="flex items-center justify-between px-4 py-3 border-b border-border">
            <h2 className="text-lg font-semibold">新建分组</h2>
            <button
              type="button"
              onClick={() => setCreateDialogOpen(false)}
              className="h-8 w-8 inline-flex items-center justify-center rounded-btn text-secondary hover:bg-elevated"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
          <div className="p-4">
            <input
              type="text"
              placeholder="输入分组名称"
              value={newGroupName}
              onChange={(e) => setNewGroupName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && newGroupName.trim()) {
                  createMutation.mutate(newGroupName.trim())
                }
              }}
              className="w-full px-3 py-2 rounded-btn border border-border bg-base text-sm"
            />
          </div>
          <div className="flex items-center justify-end gap-2 px-4 py-3 border-t border-border">
            <button
              type="button"
              onClick={() => setCreateDialogOpen(false)}
              className="px-3 py-1 rounded-btn text-xs text-secondary hover:bg-elevated"
            >
              取消
            </button>
            <button
              type="button"
              onClick={() => {
                if (newGroupName.trim()) {
                  createMutation.mutate(newGroupName.trim())
                }
              }}
              disabled={!newGroupName.trim() || createMutation.isPending}
              className="px-3 py-1 rounded-btn text-xs bg-accent text-white hover:bg-accent/90 disabled:opacity-50"
            >
              创建
            </button>
          </div>
        </Modal>
      )}

      {renameDialogOpen && (
        <Modal
          onClose={() => {
            setRenameDialogOpen(false)
            setCurrentGroupForDialog(null)
          }}
          panelClassName="w-[90vw] max-w-md bg-surface border border-border rounded-lg shadow-xl"
        >
          <div className="flex items-center justify-between px-4 py-3 border-b border-border">
            <h2 className="text-lg font-semibold">重命名分组</h2>
            <button
              type="button"
              onClick={() => {
                setRenameDialogOpen(false)
                setCurrentGroupForDialog(null)
              }}
              className="h-8 w-8 inline-flex items-center justify-center rounded-btn text-secondary hover:bg-elevated"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
          <div className="p-4">
            <input
              type="text"
              placeholder="输入新分组名称"
              value={newGroupName}
              onChange={(e) => setNewGroupName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && newGroupName.trim() && currentGroupForDialog) {
                  renameMutation.mutate({ groupId: currentGroupForDialog.group_id, name: newGroupName.trim() })
                }
              }}
              className="w-full px-3 py-2 rounded-btn border border-border bg-base text-sm"
            />
          </div>
          <div className="flex items-center justify-end gap-2 px-4 py-3 border-t border-border">
            <button
              type="button"
              onClick={() => {
                setRenameDialogOpen(false)
                setCurrentGroupForDialog(null)
              }}
              className="px-3 py-1 rounded-btn text-xs text-secondary hover:bg-elevated"
            >
              取消
            </button>
            <button
              type="button"
              onClick={() => {
                if (newGroupName.trim() && currentGroupForDialog) {
                  renameMutation.mutate({ groupId: currentGroupForDialog.group_id, name: newGroupName.trim() })
                }
              }}
              disabled={!newGroupName.trim() || renameMutation.isPending}
              className="px-3 py-1 rounded-btn text-xs bg-accent text-white hover:bg-accent/90 disabled:opacity-50"
            >
              保存
            </button>
          </div>
        </Modal>
      )}

      {deleteDialogOpen && (
        <Modal
          onClose={() => {
            setDeleteDialogOpen(false)
            setCurrentGroupForDialog(null)
          }}
          panelClassName="w-[90vw] max-w-md bg-surface border border-border rounded-lg shadow-xl"
        >
          <div className="flex items-center justify-between px-4 py-3 border-b border-border">
            <h2 className="text-lg font-semibold">删除分组</h2>
            <button
              type="button"
              onClick={() => {
                setDeleteDialogOpen(false)
                setCurrentGroupForDialog(null)
              }}
              className="h-8 w-8 inline-flex items-center justify-center rounded-btn text-secondary hover:bg-elevated"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
          <div className="p-4">
            <p className="text-muted">确定要删除分组「{currentGroupForDialog?.name}」吗？此操作不可撤销。</p>
          </div>
          <div className="flex items-center justify-end gap-2 px-4 py-3 border-t border-border">
            <button
              type="button"
              onClick={() => {
                setDeleteDialogOpen(false)
                setCurrentGroupForDialog(null)
              }}
              className="px-3 py-1 rounded-btn text-xs text-secondary hover:bg-elevated"
            >
              取消
            </button>
            <button
              type="button"
              onClick={() => {
                if (currentGroupForDialog) {
                  deleteMutation.mutate(currentGroupForDialog.group_id)
                }
              }}
              disabled={deleteMutation.isPending}
              className="px-3 py-1 rounded-btn text-xs bg-danger/15 text-danger hover:bg-danger/25 disabled:opacity-50"
            >
              删除
            </button>
          </div>
        </Modal>
      )}


      <StockPreviewDialog
        symbol={previewSymbol}
        name={previewName}
        onClose={() => {
          setPreviewSymbol(null)
          setPreviewName('')
        }}
      />

      {/* 列自定义侧栏 - 自选分组专用 */}
      <ColumnCustomizer
        columns={columns}
        onChange={handleColumnsChange}
        open={customizerOpen}
        onClose={() => setCustomizerOpen(false)}
      />

      <DimensionMembersDialog
        target={dimensionTarget}
        onClose={() => setDimensionTarget(null)}
        onStockClick={(symbol, name) => {
          setDimensionTarget(null)
          setPreviewSymbol(symbol)
          setPreviewName(name ?? '')
        }}
      />

      {/* 导入配置确认对话框 */}
      {importDialogOpen && (
        <Modal
          onClose={() => {
            setImportDialogOpen(false)
            delete (window as any).__importData
          }}
          panelClassName="w-[90vw] max-w-md bg-surface border border-border rounded-lg shadow-xl"
        >
          <div className="flex items-center justify-between px-4 py-3 border-b border-border">
            <h2 className="text-lg font-semibold">导入配置</h2>
            <button
              type="button"
              onClick={() => {
                setImportDialogOpen(false)
                delete (window as any).__importData
              }}
              className="h-8 w-8 inline-flex items-center justify-center rounded-btn text-secondary hover:bg-elevated"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
          <div className="p-4 space-y-4">
            <p className="text-sm text-muted">
              请选择导入模式：
            </p>
            <div className="space-y-2">
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="radio"
                  checked={!importReplaceMode}
                  onChange={() => setImportReplaceMode(false)}
                  className="w-4 h-4 text-accent"
                />
                <div>
                  <div className="text-sm font-medium">合并模式</div>
                  <div className="text-xs text-muted">保留现有分组，添加新的分组</div>
                </div>
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="radio"
                  checked={importReplaceMode}
                  onChange={() => setImportReplaceMode(true)}
                  className="w-4 h-4 text-accent"
                />
                <div>
                  <div className="text-sm font-medium text-danger">替换模式</div>
                  <div className="text-xs text-muted">删除所有现有分组，完全替换为导入的配置</div>
                </div>
              </label>
            </div>
          </div>
          <div className="flex items-center justify-end gap-2 px-4 py-3 border-t border-border">
            <button
              type="button"
              onClick={() => {
                setImportDialogOpen(false)
                delete (window as any).__importData
              }}
              className="px-3 py-1 rounded-btn text-xs text-secondary hover:bg-elevated"
            >
              取消
            </button>
            <button
              type="button"
              onClick={() => {
                const data = (window as any).__importData
                if (data) {
                  importConfigMutation.mutate({ data, replace: importReplaceMode })
                  delete (window as any).__importData
                }
              }}
              disabled={importConfigMutation.isPending}
              className={cn(
                "px-3 py-1 rounded-btn text-xs text-white",
                importReplaceMode ? "bg-danger hover:bg-danger/90 disabled:opacity-50" : "bg-accent hover:bg-accent/90 disabled:opacity-50"
              )}
            >
              {importConfigMutation.isPending ? "导入中…" : "确认导入"}
            </button>
          </div>
        </Modal>
      )}

      {/* 隐藏的文件输入 */}
      <input
        ref={fileInputRef}
        type="file"
        accept=".json"
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0]
          if (file) {
            const reader = new FileReader()
            reader.onload = (event) => {
              try {
                const data = JSON.parse(event.target?.result as string)
                // 先显示确认对话框
                setImportDialogOpen(true)
                // 临时保存数据
                ;(window as any).__importData = data
              } catch {
                toast('无效的配置文件', 'error')
              }
            }
            reader.readAsText(file)
          }
          // 重置输入，允许再次选择同一个文件
          e.target.value = ''
        }}
      />
    </div>
  )
}
