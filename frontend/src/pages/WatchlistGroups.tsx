import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { useQuery, useQueryClient, useMutation } from '@tanstack/react-query'
import { Plus, List, Grid, Trash2, Edit2, X, Search, Check } from 'lucide-react'
import { AnimatePresence, motion } from 'framer-motion'
import { toast } from '@/components/Toast'
import { Modal } from '@/components/Modal'
import { EmptyState } from '@/components/EmptyState'
import { StockDataTable } from '@/components/stock-table/StockDataTable'
import { renderBuiltinDataCell, boardTag } from '@/components/stock-table/primitives'
import { fmtPct } from '@/lib/format'
import { QK } from '@/lib/queryKeys'
import { BUILTIN_COLUMNS, type ColumnConfig } from '@/lib/watchlist-columns'
import { api, type WatchlistGroup } from '@/lib/api'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
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
                    title={inWatchlist ? '已添加' : '添加到板块'}
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

// 单个板块卡片组件 - 接收数据作为 props
function GroupCard({
  group,
  onSelect,
  avgPctMode,
  items,
}: {
  group: WatchlistGroup
  onSelect: (groupId: string) => void
  avgPctMode: 'simple' | 'weighted'
  items?: any[]
}) {
  const rows = items?.slice(0, 5) ?? []
  const allRows = items
  
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
      <div className="p-4 border-b border-border flex items-center justify-between">
        <div className="flex items-center gap-2">
          <h3 className="font-semibold">{group.name}</h3>
          {displayChange != null && (
            <span className={`text-sm font-medium ${displayChange >= 0 ? 'text-bull' : 'text-bear'}`}>
              {fmtPct(displayChange)}
            </span>
          )}
        </div>
        <span className="text-sm text-muted">{group.item_count} 只</span>
      </div>
      <div className="p-4">
        {rows.length > 0 ? (
          <>
            <div className="space-y-1">
              {rows.map((row) => (
                <div key={row.symbol} className="flex items-center justify-between py-1 text-sm">
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="font-mono text-secondary">{row.symbol}</span>
                    {row.name && <span className="text-muted truncate">{row.name}</span>}
                  </div>
                  {row.change_pct != null && (
                    <span className={`font-medium ${row.change_pct >= 0 ? 'text-bull' : 'text-bear'}`}>
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
                className="text-sm text-accent hover:underline mt-2"
              >
                查看全部 {group.item_count} 只
              </button>
            )}
          </>
        ) : (
          <div className="text-sm text-muted">暂无股票</div>
        )}
      </div>
    </div>
  )
}

export function WatchlistGroups() {
  const queryClient = useQueryClient()

  const [selectedGroupId, setSelectedGroupId] = useState<string | null>(null)
  const [createDialogOpen, setCreateDialogOpen] = useState(false)
  const [renameDialogOpen, setRenameDialogOpen] = useState(false)
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false)
  const [currentGroupForDialog, setCurrentGroupForDialog] = useState<WatchlistGroup | null>(null)
  const [newGroupName, setNewGroupName] = useState('')
  const [previewSymbol, setPreviewSymbol] = useState<string | null>(null)
  const [previewName, setPreviewName] = useState<string>('')

  // 设置查询
  const settingsQuery = useQuery({
    queryKey: ['watchlist-groups-settings'],
    queryFn: () => api.watchlistGroups.getSettings(),
    staleTime: Infinity,
  })

  // 从设置中获取或使用默认值
  const viewMode = (settingsQuery.data?.view_mode as 'sidebar' | 'cards') || 'sidebar'
  const displayStyle = (settingsQuery.data?.display_style as 'compact' | 'standard' | 'detailed') || 'standard'
  const avgPctMode = (settingsQuery.data?.avg_pct_mode as 'simple' | 'weighted') || 'simple'

  // 设置变更 mutations
  const setViewModeMutation = useMutation({
    mutationFn: (mode: string) => api.watchlistGroups.setViewMode(mode),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['watchlist-groups-settings'] })
    },
  })

  const setDisplayStyleMutation = useMutation({
    mutationFn: (style: string) => api.watchlistGroups.setDisplayStyle(style),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['watchlist-groups-settings'] })
    },
  })

  const setAvgPctModeMutation = useMutation({
    mutationFn: (mode: string) => api.watchlistGroups.setAvgPctMode(mode),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['watchlist-groups-settings'] })
    },
  })

  const groupsQuery = useQuery({ queryKey: QK.watchlistGroups, queryFn: api.watchlistGroups.list })

  const selectedGroupItemsQuery = useQuery({
    queryKey: QK.watchlistGroupItemsEnriched(selectedGroupId || ''),
    queryFn: () => selectedGroupId ? api.watchlistGroups.listItemsEnriched(selectedGroupId) : null,
    enabled: !!selectedGroupId,
  })

  useEffect(() => {
    if (groupsQuery.data?.groups?.length && !selectedGroupId) {
      setSelectedGroupId(groupsQuery.data.groups[0].group_id)
    }
  }, [groupsQuery.data])

  const createMutation = useMutation({
    mutationFn: (name: string) => api.watchlistGroups.create(name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QK.watchlistGroups })
      toast('板块创建成功')
      setCreateDialogOpen(false)
      setNewGroupName('')
    },
    onError: (e: any) => toast(e.message || '创建失败', 'error'),
  })

  const renameMutation = useMutation({
    mutationFn: ({ groupId, name }: { groupId: string; name: string }) => api.watchlistGroups.rename(groupId, name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QK.watchlistGroups })
      toast('板块重命名成功')
      setRenameDialogOpen(false)
      setCurrentGroupForDialog(null)
    },
    onError: (e: any) => toast(e.message || '重命名失败', 'error'),
  })

  const deleteMutation = useMutation({
    mutationFn: (groupId: string) => api.watchlistGroups.delete(groupId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QK.watchlistGroups })
      toast('板块删除成功')
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
      api.watchlistGroups.addItem(groupId, symbol),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QK.watchlistGroups })
      if (selectedGroupId) {
        queryClient.invalidateQueries({ queryKey: QK.watchlistGroupItemsEnriched(selectedGroupId) })
      }
      toast('股票添加成功')
    },
    onError: (e: any) => toast(e.message || '添加失败', 'error'),
  })

  const removeItemMutation = useMutation({
    mutationFn: ({ groupId, symbol }: { groupId: string; symbol: string }) => 
      api.watchlistGroups.removeItem(groupId, symbol),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QK.watchlistGroups })
      if (selectedGroupId) {
        queryClient.invalidateQueries({ queryKey: QK.watchlistGroupItemsEnriched(selectedGroupId) })
      }
      toast('股票移除成功')
    },
    onError: (e: any) => toast(e.message || '移除失败', 'error'),
  })

  // 前端计算板块平均涨跌幅
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

  // 获取所有板块的 enriched 数据（用于卡片视图和侧边栏）
  const { data: allGroupItems } = useQuery({
    queryKey: ['watchlist-groups-all-items', groupsQuery.data?.groups?.map(g => g.group_id).join(',')],
    queryFn: async () => {
      if (!groupsQuery.data?.groups?.length) return {}
      const result: Record<string, any[]> = {}
      for (const group of groupsQuery.data.groups) {
        try {
          const data = await api.watchlistGroups.listItemsEnriched(group.group_id)
          result[group.group_id] = data.rows || []
        } catch {
          result[group.group_id] = []
        }
      }
      return result
    },
    enabled: !!groupsQuery.data?.groups?.length,
    staleTime: 60_000,
  })

  // 根据展示样式选择要显示的列
  const columnsForStyle = useMemo(() => {
    // 精简模式：只显示代码/名称、价格、涨跌幅
    const compactKeys = ['symbol', 'price', 'pct']
    // 标准模式：显示常用列
    const standardKeys = ['symbol', 'price', 'pct', 'turnover', 'vol_ratio', 'rsi14', 'momentum', 'limit_ups', 'signals']
    
    if (displayStyle === 'compact') {
      return BUILTIN_COLUMNS.filter(c => 
        c.source.type === 'builtin' && compactKeys.includes(c.source.key)
      )
    } else if (displayStyle === 'standard') {
      return BUILTIN_COLUMNS.filter(c => 
        c.source.type === 'builtin' && standardKeys.includes(c.source.key)
      )
    } else { // detailed
      // 详细模式：显示所有可见列
      return BUILTIN_COLUMNS.filter(c => c.visible || c.pinned)
    }
  }, [displayStyle])

  const renderCell = useCallback((row: any, col: ColumnConfig): React.ReactNode => {
    if (col.source.type === 'builtin' && col.source.key === 'symbol') {
      return (
        <td key={col.id} className="px-4 py-2">
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => {
                setPreviewSymbol(row.symbol)
                setPreviewName(row.name ?? '')
              }}
              className="flex items-center gap-2 text-left"
            >
              <span className="font-mono text-secondary group-hover:text-accent transition-colors duration-150 leading-snug">{row.symbol}</span>
              {row.name && (
                <span className="text-[11px] text-muted truncate group-hover:text-secondary transition-colors duration-150 leading-snug">{row.name}</span>
              )}
            </button>
            <button
              type="button"
              onClick={() => {
                if (!selectedGroupId) return
                removeItemMutation.mutate({ groupId: selectedGroupId, symbol: row.symbol })
              }}
              disabled={removeItemMutation.isPending}
              className="shrink-0 inline-flex items-center justify-center w-5 h-5 rounded-full border border-border text-muted hover:text-red-400 hover:border-red-400 transition-colors cursor-pointer disabled:opacity-50"
              title="移出板块"
            >
              <Trash2 className="h-3 w-3" />
            </button>
          </div>
        </td>
      )
    }
    return renderBuiltinDataCell(row, col)
  }, [selectedGroupId, removeItemMutation])

  const renderSidebarView = () => (
    <div className="flex h-full">
      <div className="w-64 border-r bg-surface p-4 overflow-y-auto">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">板块</h2>
          <button
            type="button"
            onClick={() => setCreateDialogOpen(true)}
            className="inline-flex items-center gap-1 px-2 py-1 rounded-btn text-xs bg-accent text-white hover:bg-accent/90 transition-colors"
          >
            <Plus className="h-4 w-4" />
            新建
          </button>
        </div>
        <div className="space-y-2">
          {groupsQuery.data?.groups?.map((group) => {
            const rows = allGroupItems?.[group.group_id]
            const avgChange = rows ? calculateGroupAvgChange(group, { [group.group_id]: rows }) : { simple: null, weighted: null }
            const displayChange = avgPctMode === 'simple' ? avgChange.simple : avgChange.weighted

            return (
              <button
                key={group.group_id}
                onClick={() => setSelectedGroupId(group.group_id)}
                className={cn(
                  'w-full text-left p-3 rounded-btn transition-colors',
                  selectedGroupId === group.group_id ? 'bg-elevated text-foreground' : 'hover:bg-elevated/50'
                )}
              >
                <div className="flex items-center justify-between">
                  <span className="font-medium truncate">{group.name}</span>
                  <div className="flex items-center gap-2">
                    {displayChange != null && (
                      <span className={cn('text-xs font-medium', displayChange >= 0 ? 'text-bull' : 'text-bear')}>{fmtPct(displayChange)}</span>
                    )}
                    <span className="text-xs opacity-70">{group.item_count} 只</span>
                  </div>
                </div>
              </button>
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
                <button
                  type="button"
                  onClick={() => {
                    const group = groupsQuery.data?.groups?.find(g => g.group_id === selectedGroupId)
                    if (group) {
                      setCurrentGroupForDialog(group)
                      setNewGroupName(group.name)
                      setRenameDialogOpen(true)
                    }
                  }}
                  className="inline-flex items-center gap-1 px-2 py-1 rounded-btn text-xs bg-elevated hover:bg-elevated/80 transition-colors"
                >
                  <Edit2 className="h-4 w-4" />
                  重命名
                </button>
                <button
                  type="button"
                  onClick={() => {
                    const group = groupsQuery.data?.groups?.find(g => g.group_id === selectedGroupId)
                    if (group) {
                      setCurrentGroupForDialog(group)
                      setDeleteDialogOpen(true)
                    }
                  }}
                  className="inline-flex items-center gap-1 px-2 py-1 rounded-btn text-xs bg-danger/10 text-danger hover:bg-danger/20 transition-colors"
                >
                  <Trash2 className="h-4 w-4" />
                  删除
                </button>
              </div>
            </div>
            {selectedGroupItemsQuery.isLoading ? (
              <EmptyState title="加载中…" />
            ) : selectedGroupItemsQuery.data?.rows && selectedGroupItemsQuery.data.rows.length > 0 ? (
              <StockDataTable
                columns={columnsForStyle}
                rows={selectedGroupItemsQuery.data.rows}
                renderCell={renderCell}
                headerSticky={true}
                rowKey={(row) => row.symbol}
              />
            ) : (
              <EmptyState title="该板块暂无股票" hint="点击右上角新建板块或添加股票" />
            )}
          </div>
        ) : (
          <EmptyState title="请选择一个板块" hint="从左侧选择或创建一个新的板块" />
        )}
      </div>
    </div>
  )

  const renderCardsView = () => (
    <div className="p-6">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold">自选板块</h1>
        <button
          type="button"
          onClick={() => setCreateDialogOpen(true)}
          className="inline-flex items-center gap-1 px-2 py-1 rounded-btn text-xs bg-accent text-white hover:bg-accent/90 transition-colors"
        >
          <Plus className="h-4 w-4" />
          新建板块
        </button>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {groupsQuery.data?.groups?.map((group) => (
            <GroupCard
              key={group.group_id}
              group={group}
              onSelect={setSelectedGroupId}
              avgPctMode={avgPctMode}
              items={allGroupItems?.[group.group_id]}
            />
          ))}
      </div>
      {selectedGroupId && (
        <Modal
          onClose={() => setSelectedGroupId(null)}
          panelClassName="w-[90vw] max-w-5xl max-h-[80vh] flex flex-col bg-surface border border-border rounded-lg shadow-xl"
        >
          <div className="flex items-center justify-between px-4 py-3 border-b border-border shrink-0">
            <h2 className="text-lg font-semibold">{groupsQuery.data?.groups?.find(g => g.group_id === selectedGroupId)?.name}</h2>
            <button
              type="button"
              onClick={() => setSelectedGroupId(null)}
              className="h-8 w-8 inline-flex items-center justify-center rounded-btn text-secondary hover:bg-elevated"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
          <div className="p-4 overflow-y-auto flex-1">
            {selectedGroupItemsQuery.isLoading ? (
              <EmptyState title="加载中…" />
            ) : selectedGroupItemsQuery.data?.rows && selectedGroupItemsQuery.data.rows.length > 0 ? (
              <StockDataTable
                columns={BUILTIN_COLUMNS}
                rows={selectedGroupItemsQuery.data.rows}
                renderCell={renderCell}
                rowKey={(row) => row.symbol}
              />
            ) : (
              <EmptyState title="该板块暂无股票" />
            )}
          </div>
        </Modal>
      )}
    </div>
  )

  return (
    <div className="h-full">
      <div className="border-b border-border px-6 py-3 flex items-center justify-between">
        <div className="flex items-center gap-4">
          {/* 视图模式切换 */}
          <div className="inline-flex rounded-btn border border-border overflow-hidden">
            <button
              type="button"
              onClick={() => setViewModeMutation.mutate('sidebar')}
              disabled={setViewModeMutation.isPending}
              className={cn(
                'px-3 py-1 text-xs transition-colors flex items-center gap-1',
                viewMode === 'sidebar' ? 'bg-elevated text-foreground' : 'text-muted hover:bg-elevated/50'
              )}
            >
              <List className="h-3.5 w-3.5" />
              侧边栏
            </button>
            <button
              type="button"
              onClick={() => setViewModeMutation.mutate('cards')}
              disabled={setViewModeMutation.isPending}
              className={cn(
                'px-3 py-1 text-xs transition-colors flex items-center gap-1',
                viewMode === 'cards' ? 'bg-elevated text-foreground' : 'text-muted hover:bg-elevated/50'
              )}
            >
              <Grid className="h-3.5 w-3.5" />
              卡片
            </button>
          </div>

          {/* 展示样式切换 */}
          {viewMode === 'sidebar' && (
            <div className="inline-flex rounded-btn border border-border overflow-hidden">
              <button
                type="button"
                onClick={() => setDisplayStyleMutation.mutate('compact')}
                disabled={setDisplayStyleMutation.isPending}
                className={cn(
                  'px-3 py-1 text-xs transition-colors',
                  displayStyle === 'compact' ? 'bg-elevated text-foreground' : 'text-muted hover:bg-elevated/50'
                )}
              >
                精简
              </button>
              <button
                type="button"
                onClick={() => setDisplayStyleMutation.mutate('standard')}
                disabled={setDisplayStyleMutation.isPending}
                className={cn(
                  'px-3 py-1 text-xs transition-colors',
                  displayStyle === 'standard' ? 'bg-elevated text-foreground' : 'text-muted hover:bg-elevated/50'
                )}
              >
                标准
              </button>
              <button
                type="button"
                onClick={() => setDisplayStyleMutation.mutate('detailed')}
                disabled={setDisplayStyleMutation.isPending}
                className={cn(
                  'px-3 py-1 text-xs transition-colors',
                  displayStyle === 'detailed' ? 'bg-elevated text-foreground' : 'text-muted hover:bg-elevated/50'
                )}
              >
                详细
              </button>
            </div>
          )}

          {/* 平均涨跌幅模式切换 */}
          <div className="inline-flex rounded-btn border border-border overflow-hidden">
            <button
              type="button"
              onClick={() => setAvgPctModeMutation.mutate('simple')}
              disabled={setAvgPctModeMutation.isPending}
              className={cn(
                'px-3 py-1 text-xs transition-colors',
                avgPctMode === 'simple' ? 'bg-elevated text-foreground' : 'text-muted hover:bg-elevated/50'
              )}
            >
              算术平均
            </button>
            <button
              type="button"
              onClick={() => setAvgPctModeMutation.mutate('weighted')}
              disabled={setAvgPctModeMutation.isPending}
              className={cn(
                'px-3 py-1 text-xs transition-colors',
                avgPctMode === 'weighted' ? 'bg-elevated text-foreground' : 'text-muted hover:bg-elevated/50'
              )}
            >
              加权平均
            </button>
          </div>
        </div>
      </div>
      {viewMode === 'sidebar' ? renderSidebarView() : renderCardsView()}

      {createDialogOpen && (
        <Modal
          onClose={() => setCreateDialogOpen(false)}
          panelClassName="w-[90vw] max-w-md bg-surface border border-border rounded-lg shadow-xl"
        >
          <div className="flex items-center justify-between px-4 py-3 border-b border-border">
            <h2 className="text-lg font-semibold">新建板块</h2>
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
              placeholder="输入板块名称"
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
            <h2 className="text-lg font-semibold">重命名板块</h2>
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
              placeholder="输入新板块名称"
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
            <h2 className="text-lg font-semibold">删除板块</h2>
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
            <p className="text-muted">确定要删除板块「{currentGroupForDialog?.name}」吗？此操作不可撤销。</p>
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
    </div>
  )
}
