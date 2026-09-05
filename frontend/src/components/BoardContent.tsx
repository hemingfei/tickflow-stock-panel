import { useState, useRef, type ReactNode } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { motion } from 'framer-motion'
import { ArrowDownRight, ArrowUpRight, BarChart3, BellRing, Flame, Info, LineChart, Sparkles, Target, type LucideIcon } from 'lucide-react'
import type { AlertEvent, MarketSnapshotRow, OverviewDimensionRankItem, OverviewMarket } from '@/lib/api'
import { fmtBigNum, fmtPct } from '@/lib/format'
import { dimensionKindForSourceField, type DimensionMembersTarget } from '@/components/DimensionMembersDialog'
import { SealedBadge } from '@/components/SealedBadge'
import { toNavItems, type NavItem } from '@/components/StockPreviewDialog'
import { cn } from '@/lib/cn'
import { cnSignal } from '@/lib/signals'
import { strategyEventMeta, strategyName } from '@/lib/strategyMonitorEvents'
import { boardTag } from '@/components/stock-table/primitives'

/**
 * 市场看板主体渲染 (指数/KPI/涨跌分布/情绪雷达/趋势/概念行业热度/四榜单/涨停梯队/监控中心)。
 *
 * 由「市场看板」(pages/Dashboard.tsx) 与「看板回溯」(pages/BoardReplay.tsx) 共用,
 * 保证回放内容与实时看板同一渲染来源; 快照数据结构见 BoardSnapshot (lib/api.ts)。
 * 交互回调全部可选: 回溯页不传即纯只读展示。
 */

function n(v: number | null | undefined) {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

export function scoreColor(v: number) {
  // A 股惯例: 强势=红, 弱式=绿
  if (v >= 70) return '#F04438'
  if (v >= 55) return '#FB923C'
  if (v >= 45) return '#F59E0B'
  if (v >= 30) return '#84CC16'
  return '#12B76A'
}

function fmtPrice(v: number | null | undefined, digits = 2) {
  const x = n(v)
  return x == null ? '—' : x.toFixed(digits)
}

function fmtIndexPct(v: number | null | undefined) {
  const x = n(v)
  if (x == null) return '—'
  return `${x >= 0 ? '+' : ''}${x.toFixed(2)}%`
}

function fmtStockPct(v: number | null | undefined) {
  const x = n(v)
  if (x == null) return '—'
  return `${x >= 0 ? '+' : ''}${(x * 100).toFixed(2)}%`
}

function pctClass(v: number | null | undefined) {
  const x = n(v)
  if (x == null || x === 0) return 'text-muted'
  return x > 0 ? 'text-bull' : 'text-bear'
}

function compactCount(v: number | null | undefined) {
  const x = n(v)
  if (x == null) return '—'
  if (x >= 1000) return `${(x / 1000).toFixed(1)}k`
  return x.toFixed(0)
}

function SectionTitle({ icon: Icon, title, hint }: { icon: LucideIcon; title: string; hint?: ReactNode }) {
  return (
    <div className="mb-2 flex items-center justify-between gap-2">
      <div className="flex items-center gap-1.5">
        <span className="h-3 w-0.5 rounded-full bg-gradient-to-b from-accent to-accent/30" />
        <Icon className="h-3.5 w-3.5 text-accent" />
        <h2 className="text-xs font-semibold text-foreground">{title}</h2>
      </div>
      {hint && <span className="font-mono text-[10px] text-muted">{hint}</span>}
    </div>
  )
}

// 看板监控中心小组件 — 显示前 10 条触发记录
const _SOURCE_BADGE: Record<string, string> = {
  strategy: 'bg-amber-400/10 text-amber-400',
  signal: 'bg-accent/10 text-accent',
  price: 'bg-emerald-400/10 text-emerald-400',
  market: 'bg-purple-500/10 text-purple-400',
  sector: 'bg-cyan-500/10 text-cyan-700 dark:text-cyan-300',
}
const _SOURCE_LABEL: Record<string, string> = {
  strategy: '策略', signal: '信号', price: '价格', market: '异动', sector: '板块',
}
const _SEVERITY_BAR: Record<string, string> = {
  info: 'bg-accent/40', warn: 'bg-warning', critical: 'bg-danger',
}

// events 由调用方传入: 实时页传轮询查询结果, 回溯页传快照内定格的告警列表
function MonitorWidget({ events, onStockClick, activeSymbol }: {
  events: AlertEvent[]
  onStockClick?: (event: AlertEvent, navList?: NavItem[]) => void
  activeSymbol?: string
}) {
  const navigate = useNavigate()
  // 切股导航列表: 有 symbol 的触发记录
  const alertNav = toNavItems(events.filter((ev): ev is AlertEvent & { symbol: string } => !!ev.symbol))

  if (events.length === 0) {
    return (
      <div className="mt-1 py-6 text-center text-[11px] text-muted">暂无触发记录</div>
    )
  }

  return (
    <>
      <div className="mt-1 space-y-1.5">
        {events.map((ev, i) => {
          const sev = _SEVERITY_BAR[ev.severity ?? 'info'] ?? _SEVERITY_BAR.info
          const pct = ev.change_pct ?? 0
          const isStrategy = ev.source === 'strategy'
          const isSector = ev.source === 'sector'
          const sname = isStrategy ? strategyName(ev.message ?? '') : ''
          const eventMeta = strategyEventMeta(ev.type)
          return (
            <motion.div
              key={`${ev.ts}-${i}`}
              initial={{ opacity: 0, y: -8, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              transition={{ duration: 0.3, delay: Math.min(i * 0.03, 0.3) }}
              className={`relative overflow-hidden rounded-md border pl-2.5 pr-2 py-1.5 transition-colors ${ev.symbol && ev.symbol === activeSymbol ? 'border-accent/40 bg-accent/5' : 'border-border/40 bg-surface/60 hover:border-border hover:bg-surface'}`}
            >
              <div className={cn('absolute left-0 top-0 h-full w-0.5', sev)} />
              {/* 第一行: 代码 + 名称 + 价格 + 涨跌幅 (点击代码/名称弹日K) */}
              <div className="flex items-center gap-1.5">
                <button
                  onClick={() => isSector ? navigate('/monitor') : ev.symbol && onStockClick?.(ev, alertNav)}
                  title={isSector ? '在监控中心查看板块告警' : ev.symbol ? `查看 ${ev.symbol} 日K` : undefined}
                  className={`inline-flex items-center gap-1 min-w-0 shrink-0 rounded hover:bg-elevated/60 transition-colors -mx-0.5 px-0.5 ${isSector || ev.symbol ? 'cursor-pointer' : 'cursor-default'}`}
                >
                  <span className="font-mono text-[10px] font-medium text-foreground/80 hover:text-accent">{ev.symbol?.replace(/\.(SH|SZ|BJ)$/, '')}</span>
                  {ev.symbol && (() => {
                    const board = boardTag(ev.symbol)
                    return board && (
                      <span className={`inline-flex items-center justify-center h-3 w-3 rounded text-[7px] font-bold leading-none border ${board.color}`}>
                        {board.label}
                      </span>
                    )
                  })()}
                  {ev.name && <span className="text-[10px] text-secondary truncate max-w-[5rem] hover:text-foreground">{ev.name}</span>}
                </button>
                <span className="flex-1" />
                {ev.price != null && (
                  <span className="text-[10px] font-mono text-foreground/60 shrink-0">{fmtPrice(ev.price)}</span>
                )}
                {ev.change_pct != null && (
                  <span className={cn('text-[10px] font-mono font-medium shrink-0 w-12 text-right', pct >= 0 ? 'text-danger' : 'text-bear')}>
                    {fmtPct(pct)}
                  </span>
                )}
              </div>
              {/* 第二行: 策略类型走新格式, 其他走旧格式 */}
              {isStrategy ? (
                <>
                  {ev.symbol ? (
                    <div className="mt-0.5 flex min-w-0 items-center gap-1.5">
                      <span className={cn('shrink-0 text-[9px] font-medium', eventMeta.className)}>
                        {eventMeta.action}
                      </span>
                      {sname
                        ? <span className="truncate text-[9px] font-medium text-amber-400">「{sname}」</span>
                        : ev.message && <span className="truncate text-[9px] text-muted">{ev.message}</span>}
                      <span className="flex-1" />
                      <span className="text-[8px] text-muted/50 shrink-0 font-mono">
                        {ev.ts ? new Date(ev.ts).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : ''}
                      </span>
                    </div>
                  ) : (
                    <div className="mt-0.5 flex min-w-0 items-center gap-1.5">
                      <span className="truncate text-[9px] text-muted">{ev.message}</span>
                      <span className="flex-1" />
                      <span className="text-[8px] text-muted/50 shrink-0 font-mono">
                        {ev.ts ? new Date(ev.ts).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : ''}
                      </span>
                    </div>
                  )}
                  {ev.signals && ev.signals.length > 0 && (
                    <div className="mt-1 flex flex-wrap gap-1">
                      {ev.signals.map(signal => (
                        <span key={signal} className="rounded bg-accent/8 px-1 py-px text-[8px] text-accent/80">{cnSignal(signal)}</span>
                      ))}
                    </div>
                  )}
                </>
              ) : (
                <>
                  <div className="mt-0.5 flex items-center gap-1.5">
                    <span className={cn('shrink-0 rounded px-1 py-px text-[8px] font-medium', _SOURCE_BADGE[ev.source] ?? 'bg-elevated text-muted')}>
                      {_SOURCE_LABEL[ev.source] ?? ev.source}
                    </span>
                    {ev.message && (
                      <span className="text-[9px] text-muted truncate flex-1">{ev.message}</span>
                    )}
                    <span className="text-[8px] text-muted/50 shrink-0 font-mono">
                      {ev.ts ? new Date(ev.ts).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : ''}
                    </span>
                  </div>
                  {ev.signals && ev.signals.length > 0 && (
                    <div className="mt-1 flex flex-wrap gap-1">
                      {ev.signals.map((s, j) => (
                        <span key={j} className="rounded bg-accent/8 px-1 py-px text-[8px] text-accent/80">{cnSignal(s)}</span>
                      ))}
                    </div>
                  )}
                </>
              )}
            </motion.div>
          )
        })}
      </div>
    </>
  )
}

function KpiCell({ label, value, sub, tone = 'neutral' }: { label: ReactNode; value: ReactNode; sub?: string; tone?: 'bull' | 'bear' | 'accent' | 'neutral' }) {
  const isPlain = typeof value === 'string' || typeof value === 'number'
  const color = tone === 'bull' ? 'text-bull' : tone === 'bear' ? 'text-bear' : tone === 'accent' ? 'text-accent' : 'text-foreground'
  return (
    <div className="min-w-0 overflow-hidden rounded-lg border border-border bg-surface/80 px-2 py-1 shadow-[0_1px_2px_hsl(var(--border)/0.4)] backdrop-blur-sm transition-all hover:border-accent/30 hover:shadow-[0_2px_8px_hsl(var(--accent)/0.15)]">
      <div className="flex items-center gap-1 text-[11px] text-muted">{label}</div>
      <div className={`mt-1 truncate font-mono text-lg font-semibold leading-none tabular-nums ${isPlain ? color : 'text-foreground'}`}>{value}</div>
      {sub && <div className="mt-1 truncate text-[10px] text-muted">{sub}</div>}
    </div>
  )
}

function IndexTicker({ item }: { item: OverviewMarket['indices'][number] }) {
  const pct = item.change_pct
  const isUp = (n(pct) ?? 0) >= 0
  return (
    <Link
      to={`/indices?symbol=${encodeURIComponent(item.symbol)}`}
      className="grid min-w-0 grid-cols-[1fr_auto] items-center gap-x-2 gap-y-0.5 rounded-lg border border-border bg-elevated/45 px-1.5 py-1 shadow-[0_1px_1px_hsl(var(--border)/0.3)] backdrop-blur-sm transition-all hover:border-accent/40 hover:bg-elevated hover:shadow-[0_2px_6px_hsl(var(--accent)/0.15)]"
    >
      <div className="truncate text-xs font-medium text-foreground">{item.name || item.symbol}</div>
      <div className={`font-mono text-xs font-semibold ${pctClass(pct)}`}>{fmtIndexPct(pct)}</div>
      <div className="font-mono text-[10px] text-muted">{item.symbol}</div>
      <div className={`flex items-center gap-1 font-mono text-[11px] ${pctClass(pct)}`}>
        {isUp ? <ArrowUpRight className="h-3 w-3" /> : <ArrowDownRight className="h-3 w-3" />}
        {fmtPrice(item.last_price)}
      </div>
    </Link>
  )
}

function BreadthBar({ data }: { data: OverviewMarket['breadth'] }) {
  const denom = Math.max(data.total, 1)
  const upW = data.up / denom * 100
  const downW = data.down / denom * 100
  const flatW = Math.max(0, 100 - upW - downW)
  return (
    <div className="space-y-2">
      <div className="flex h-2.5 overflow-hidden rounded-full bg-elevated">
        <div className="bg-bull/85" style={{ width: `${upW}%` }} />
        <div className="bg-muted/45" style={{ width: `${flatW}%` }} />
        <div className="bg-bear/85" style={{ width: `${downW}%` }} />
      </div>
      <div className="grid grid-cols-3 gap-1.5 text-[11px]">
        <div className="rounded bg-bull/8 px-2 py-1 text-bull">涨 <span className="font-mono">{data.up}</span></div>
        <div className="rounded bg-elevated/70 px-2 py-1 text-muted">平 <span className="font-mono">{data.flat}</span></div>
        <div className="rounded bg-bear/8 px-2 py-1 text-bear">跌 <span className="font-mono">{data.down}</span></div>
      </div>
    </div>
  )
}

function DistributionBars({ rows }: { rows: OverviewMarket['distribution'] }) {
  const maxCount = Math.max(...rows.map(r => r.count), 1)
  return (
    <div className="grid h-24 grid-cols-8 items-end gap-1 pt-1">
      {rows.map((r, i) => {
        const positive = i >= 4
        return (
          <div key={r.label} className="flex h-full min-w-0 flex-col items-center justify-end gap-0.5">
            <div className="font-mono text-[9px] text-muted">{r.count || ''}</div>
            <div
              className={`w-2 rounded-full ${positive ? 'bg-gradient-to-t from-bull/45 to-bull/90' : 'bg-gradient-to-t from-bear/45 to-bear/90'}`}
              style={{ height: `${Math.max(4, r.count / maxCount * 86)}%` }}
              title={`${r.label}: ${r.count}只`}
            />
            <div className="truncate text-[9px] text-muted">{r.label}</div>
          </div>
        )
      })}
    </div>
  )
}

function EmotionRadar({ radar, score }: { radar: OverviewMarket['radar']; score: number }) {
  const size = 240
  const cx = size / 2
  const cy = size / 2
  const maxR = 78
  const color = scoreColor(score)
  const [isHovering, setIsHovering] = useState(false)
  const [mousePos, setMousePos] = useState({ x: 0, y: 0 })
  const containerRef = useRef<HTMLDivElement>(null)

  if (!radar.length) return <div className="flex h-52 items-center justify-center text-xs text-muted">暂无雷达数据</div>

  const points = radar.map((r, i) => {
    const angle = -Math.PI / 2 + i * 2 * Math.PI / radar.length
    const radius = maxR * Math.max(0, Math.min(100, r.value)) / 100
    return {
      ...r,
      x: cx + Math.cos(angle) * radius,
      y: cy + Math.sin(angle) * radius,
      lx: cx + Math.cos(angle) * (maxR + 27),
      ly: cy + Math.sin(angle) * (maxR + 27),
      gx: cx + Math.cos(angle) * maxR,
      gy: cy + Math.sin(angle) * maxR,
    }
  })

  const polygon = points.map(p => `${p.x},${p.y}`).join(' ')
  const gridPolygons = [1, 0.66, 0.33].map((level, idx) => ({
    level,
    idx,
    points: radar.map((_, i) => {
      const angle = -Math.PI / 2 + i * 2 * Math.PI / radar.length
      return `${cx + Math.cos(angle) * maxR * level},${cy + Math.sin(angle) * maxR * level}`
    }).join(' '),
  }))

  const handleMouseMove = (e: React.MouseEvent) => {
    if (containerRef.current) {
      const rect = containerRef.current.getBoundingClientRect()
      setMousePos({
        x: e.clientX - rect.left + 15,
        y: e.clientY - rect.top + 15
      })
    }
  }

  return (
    <div ref={containerRef} className="flex justify-center relative">
      <svg
        viewBox={`0 0 ${size} ${size}`}
        className="h-56 w-full"
        style={{ cursor: 'default' }}
        onMouseEnter={() => setIsHovering(true)}
        onMouseLeave={() => setIsHovering(false)}
        onMouseMove={handleMouseMove}
      >
        <defs>
          <radialGradient id="emotionRadarFill" cx="50%" cy="45%" r="70%">
            <stop offset="0%" stopColor={`${color}57`} />
            <stop offset="100%" stopColor={`${color}1f`} />
          </radialGradient>
          {/* 中心/网格用 CSS 变量取色, 亮暗主题自动切换 (SVG 属性支持 hsl(var(--x))) */}
          <radialGradient id="emotionRadarCenter" cx="50%" cy="50%" r="55%">
            <stop offset="0%" stopColor="hsl(var(--surface) / 0.92)" />
            <stop offset="68%" stopColor="hsl(var(--surface) / 0.70)" />
            <stop offset="100%" stopColor="hsl(var(--surface) / 0)" />
          </radialGradient>
        </defs>
        {gridPolygons.map(g => (
          <polygon
            key={g.level}
            points={g.points}
            fill={g.idx % 2 === 0 ? 'hsl(var(--elevated) / 0.55)' : 'hsl(var(--elevated) / 0.3)'}
            stroke={g.level === 1 ? 'hsl(var(--border) / 0.9)' : 'hsl(var(--border) / 0.5)'}
            strokeWidth={g.level === 1 ? 1.2 : 0.8}
          />
        ))}
        {points.map(p => <line key={p.key} x1={cx} y1={cy} x2={p.gx} y2={p.gy} stroke="hsl(var(--border) / 0.4)" />)}
        <polygon points={polygon} fill="url(#emotionRadarFill)" stroke={color} strokeWidth="2" />
        {points.map(p => (
          <circle
            key={p.key}
            cx={p.x}
            cy={p.y}
            r={isHovering ? "4.5" : "2.8"}
            fill={color}
            stroke="hsl(var(--surface) / 0.9)"
            strokeWidth="1.5"
            style={{ transition: 'r 0.15s ease-out' }}
          />
        ))}
        <circle cx={cx} cy={cy} r="29" fill="url(#emotionRadarCenter)" />
        <text x={cx} y={cy + 7} textAnchor="middle" className="fill-foreground font-mono text-[24px] font-bold">{score}</text>
        {points.map(p => (
          <text
            key={`${p.key}-label`}
            x={p.lx}
            y={p.ly + 4}
            textAnchor="middle"
            className="fill-secondary text-[10px] font-medium"
          >
            {p.label}
          </text>
        ))}
      </svg>
      {isHovering && (
        <div
          className="absolute px-3 py-2 rounded-lg bg-surface/98 border border-border shadow-lg backdrop-blur-sm pointer-events-none"
          style={{
            zIndex: 50,
            left: mousePos.x,
            top: mousePos.y
          }}
        >
          <div className="flex flex-col gap-1">
            <div className="text-xs font-semibold text-foreground mb-1">情绪雷达维度得分</div>
            {points.map(p => (
              <div key={p.key} className="flex items-center justify-between gap-4 text-xs">
                <span className="text-secondary">{p.label}</span>
                <span className="font-mono font-semibold text-foreground">{Math.round(p.value)}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function LadderMini({ limit }: { limit: OverviewMarket['limit'] }) {
  const tiers = limit.tiers.filter(t => t.boards >= 2).slice(0, 6)
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between rounded bg-elevated/55 px-2 py-1.5 text-[11px]">
        <span className="text-muted">封板率</span>
        <span className="font-mono text-accent">{(limit.seal_rate ?? 0).toFixed(0)}%</span>
      </div>
      {tiers.length === 0 && <div className="rounded border border-dashed border-border py-5 text-center text-xs text-muted">暂无 2 板以上</div>}
      {tiers.map(t => {
        const stocks = t.stocks ?? []
        const showStocks = stocks.length > 0 && stocks.length <= 3
        return (
          <div key={t.boards} className="rounded bg-elevated/35 px-2 py-1.5">
            <div className="grid grid-cols-[42px_1fr_auto] items-center gap-2">
              <span className={`font-mono text-sm font-bold ${t.boards >= 5 ? 'text-bull' : t.boards >= 3 ? 'text-accent' : 'text-secondary'}`}>{t.boards}板</span>
              <div className="h-1.5 overflow-hidden rounded-full bg-base">
                <div className="h-full rounded-full bg-bull/70" style={{ width: `${Math.min(100, t.count * 12)}%` }} />
              </div>
              <span className="font-mono text-xs text-foreground">{t.count}</span>
            </div>
            {showStocks && (
              <div className="mt-1 flex flex-wrap gap-x-2 gap-y-0.5 pl-[50px]">
                {stocks.map(s => (
                  <span key={s.symbol} className="inline-flex items-center gap-0.5 text-[9px] text-secondary">
                    {s.name || s.symbol}
                  </span>
                ))}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

function MiniMetric({ label, value, cls = 'text-foreground' }: { label: string; value: string; cls?: string }) {
  return (
    <div className="rounded-md bg-elevated/45 px-2 py-1.5 border border-border/40">
      <div className="text-[10px] text-muted">{label}</div>
      <div className={`mt-0.5 font-mono text-xs font-semibold ${cls}`}>{value}</div>
    </div>
  )
}

function StockList({ title, rows, mode, onStockClick, activeSymbol }: {
  title: string; rows: MarketSnapshotRow[]; mode: 'gain' | 'loss' | 'amount' | 'active';
  onStockClick?: (symbol: string, name?: string) => void;
  activeSymbol?: string;
}) {
  return (
    <div className="rounded-card border border-border bg-surface/80 p-1.5 shadow-[0_1px_2px_hsl(var(--border)/0.4)] backdrop-blur-sm transition-shadow hover:shadow-[0_2px_8px_hsl(var(--border)/0.5)]">
      <div className="mb-1 flex items-center justify-between">
        <h3 className="text-xs font-semibold text-foreground">{title}</h3>
        <span className="text-[9px] text-muted">TOP {Math.min(rows.length, 8)}</span>
      </div>
      <div className="space-y-1">
        {rows.slice(0, 8).map((r, idx) => (
          <div
            key={`${r.symbol}-${idx}`}
            className={`grid grid-cols-[18px_1fr_auto] items-center gap-1.5 rounded-md px-1.5 py-1 cursor-pointer transition-colors border ${r.symbol === activeSymbol ? 'bg-accent/10 border-accent/30' : 'bg-elevated/40 border-transparent hover:bg-elevated hover:brightness-110 hover:border-border/60'}`}
            onClick={() => onStockClick?.(r.symbol, r.name ?? undefined)}
          >
            <span className="text-center font-mono text-[10px] text-muted">{idx + 1}</span>
            <div className="min-w-0">
              <div className="flex items-center gap-1">
                <span className="truncate text-[11px] text-foreground">{r.name || r.symbol}</span>
                {(() => {
                  const board = boardTag(r.symbol)
                  return board ? (
                    <span className={`shrink-0 inline-flex items-center justify-center h-3 px-1 rounded text-[8px] font-bold leading-none border ${board.color}`}>
                      {board.label}
                    </span>
                  ) : null
                })()}
              </div>
              <span className="font-mono text-[9px] text-muted">{r.symbol}</span>
            </div>
            <div className="text-right">
              {mode === 'amount' ? (
                <>
                  <div className="font-mono text-[11px] text-foreground">{fmtBigNum(r.amount)}</div>
                  <div className={`font-mono text-[9px] ${pctClass(r.change_pct)}`}>{fmtStockPct(r.change_pct)}</div>
                </>
              ) : mode === 'active' ? (
                <>
                  <div className="font-mono text-[11px] text-accent">{fmtPrice(r.turnover_rate, 1)}%</div>
                  <div className={`font-mono text-[9px] ${pctClass(r.change_pct)}`}>{fmtStockPct(r.change_pct)}</div>
                </>
              ) : (
                <>
                  <div className={`font-mono text-[11px] font-semibold ${pctClass(r.change_pct)}`}>{fmtStockPct(r.change_pct)}</div>
                  <div className="font-mono text-[9px] text-muted">{fmtPrice(r.close)}</div>
                </>
              )}
            </div>
          </div>
        ))}
        {rows.length === 0 && <div className="py-5 text-center text-xs text-muted">暂无数据</div>}
      </div>
    </div>
  )
}

function RankColumn({ title, rows, tone, onStockClick, onDimensionClick, activeSymbol }: {
  title: string; rows: OverviewDimensionRankItem[]; tone: 'bull' | 'bear';
  onStockClick?: (symbol: string, name?: string) => void;
  onDimensionClick?: (target: DimensionMembersTarget) => void;
  activeSymbol?: string;
}) {
  return (
    <div className="min-w-0 space-y-1">
      <div className={`text-[10px] font-medium ${tone === 'bull' ? 'text-bull' : 'text-bear'}`}>{title}</div>
      {rows.slice(0, 5).map((r, idx) => {
        const kind = r.source_field ? dimensionKindForSourceField(r.source_field) : null
        const clickable = !!(r.source_field && kind && onDimensionClick)
        const isActive = r.leader?.symbol != null && r.leader.symbol === activeSymbol
        return (
        <div
          key={`${title}-${r.name}-${idx}`}
          onClick={() => clickable && onDimensionClick!({
            kind: kind!,
            value: r.name,
            sourceField: r.source_field!,
          })}
          title={clickable ? `查看「${r.name}」成分股` : undefined}
          className={`grid grid-cols-[14px_1fr_auto] items-center gap-1 rounded-md px-1.5 py-1 border transition-colors ${
            isActive ? 'border-accent/30 bg-accent/10' : 'border-transparent bg-elevated/40'
          } ${clickable ? 'cursor-pointer hover:border-accent/40 hover:bg-elevated/70' : 'hover:border-border/60'}`}
        >
          <span className="text-center font-mono text-[9px] text-muted">{idx + 1}</span>
          <div className="min-w-0">
            <div className="truncate text-[11px] text-foreground" title={r.name}>
              {r.name}
              {clickable && <span className="ml-1 text-[8px] text-muted/50">↗</span>}
            </div>
            <div className="mt-0.5 flex items-center gap-1">
              <span className="shrink-0 font-mono text-[9px] text-muted">{r.count}只</span>
              <span className="text-muted">·</span>
              {r.leader?.symbol ? (
                <button
                  onClick={(e) => { e.stopPropagation(); onStockClick?.(r.leader!.symbol!, r.leader!.name ?? undefined) }}
                  className="truncate text-[10px] font-medium text-secondary hover:text-accent cursor-pointer transition-colors"
                  title={r.leader?.symbol ?? undefined}
                >{r.leader?.name ?? '—'}</button>
              ) : (
                <span className="truncate text-[10px] text-muted">{r.leader?.name ?? '—'}</span>
              )}
              {r.leader?.change_pct != null && (
                <span className={`shrink-0 font-mono text-[9px] tabular-nums ${pctClass(r.leader.change_pct)}`}>
                  {fmtStockPct(r.leader.change_pct)}
                </span>
              )}
              {r.leader?.symbol && (() => {
                const board = boardTag(r.leader!.symbol!)
                return board ? (
                  <span className={`shrink-0 inline-flex items-center justify-center h-3 px-1 rounded text-[8px] font-bold leading-none border ${board.color}`}>
                    {board.label}
                  </span>
                ) : null
              })()}
            </div>
          </div>
          <div className={`font-mono text-[10px] font-semibold ${pctClass(r.avg_pct)}`}>{fmtStockPct(r.avg_pct)}</div>
        </div>
        )
      })}
      {rows.length === 0 && <div className="rounded border border-dashed border-border py-4 text-center text-xs text-muted">暂无数据</div>}
    </div>
  )
}

function HotRankCard({ title, rank, configUrl, onStockClick, onDimensionClick, activeSymbol }: {
  title: string; rank?: OverviewMarket['concept_rank']; configUrl: string;
  onStockClick?: (symbol: string, name?: string) => void;
  onDimensionClick?: (target: DimensionMembersTarget) => void;
  activeSymbol?: string;
}) {
  const hasData = (rank?.leading?.length ?? 0) > 0 || (rank?.lagging?.length ?? 0) > 0
  return (
    <section className="rounded-card border border-border bg-surface/80 p-1.5 shadow-[0_1px_2px_hsl(var(--border)/0.4)] backdrop-blur-sm transition-shadow hover:shadow-[0_2px_8px_hsl(var(--border)/0.5)]">
      <SectionTitle icon={Flame} title={title} hint="领涨/领跌 · 点击板块看成分股" />
      {hasData ? (
        <div className="grid grid-cols-2 gap-2">
          <RankColumn title="领涨" rows={rank?.leading ?? []} tone="bull" onStockClick={onStockClick} onDimensionClick={onDimensionClick} activeSymbol={activeSymbol} />
          <RankColumn title="领跌" rows={rank?.lagging ?? []} tone="bear" onStockClick={onStockClick} onDimensionClick={onDimensionClick} activeSymbol={activeSymbol} />
        </div>
      ) : (
        <div className="py-4 text-center">
          <p className="text-[11px] text-muted">未配置扩展数据源</p>
          <Link
            to={configUrl}
            className="mt-1.5 inline-block text-[11px] text-accent hover:text-accent/80 transition-colors"
          >
            前往配置 →
          </Link>
        </div>
      )}
    </section>
  )
}

// 切股导航列表构建 (与列表展示行一致: StockList 只显示前 8)
function stockListNav(rows: MarketSnapshotRow[]): NavItem[] {
  return toNavItems(rows.slice(0, 8))
}
function rankNav(rank?: OverviewMarket['concept_rank']): NavItem[] {
  const leaders = [...(rank?.leading ?? []), ...(rank?.lagging ?? [])]
    .map(r => r.leader)
    .filter((l): l is NonNullable<typeof l> & { symbol: string } => !!l?.symbol)
  return toNavItems(leaders)
}

/** 看板上的高亮来源 (榜单/告警), 用于行高亮与切股导航 */
export type BoardSource = 'gain' | 'loss' | 'amount' | 'active' | 'concept' | 'industry' | 'alert'

export function BoardContent({
  data, alerts, hasDepth, sealedReady,
  activeSource, activeSymbol,
  onStockClick, onAlertClick, onDimensionClick,
  showMonitor = true,
}: {
  data: OverviewMarket
  /** 监控中心触发记录: 实时页传轮询结果, 回溯页传快照定格列表 */
  alerts: AlertEvent[]
  hasDepth: boolean
  sealedReady: boolean
  /** 当前高亮的来源榜与个股 (预览弹窗打开时) */
  activeSource?: BoardSource
  activeSymbol?: string
  /** 来源榜/概念行业龙头点击: source 标识榜单, navList 为该榜单的切股导航 */
  onStockClick?: (source: BoardSource, symbol: string, name?: string, navList?: NavItem[]) => void
  onAlertClick?: (event: AlertEvent, navList?: NavItem[]) => void
  onDimensionClick?: (target: DimensionMembersTarget) => void
  /** 公开回放隐藏监控中心板块: 告警已被后端剥离, 空板块会被误读为「当时无告警」 */
  showMonitor?: boolean
}) {
  // 实时模式: none / watchlist / full_market。
  // watchlist 模式仅自选 ≤5 只实时, 看板呈现的大盘数据实为盘后快照, 需提示避免误读。
  const quoteMode = data.quote_status?.mode as ('none' | 'watchlist' | 'full_market') | undefined

  return (
    <>
      {/* 自选实时模式提示: 大盘看板为盘后数据, 仅自选股实时。避免用户误读为全市场实时。 */}
      {quoteMode === 'watchlist' && (
        <div className="mb-1.5 flex items-start gap-2 rounded-card border border-amber-500/30 bg-amber-500/8 px-3 py-1.5 text-[11px] leading-relaxed">
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-500" />
          <div className="min-w-0 flex-1 text-secondary">
            当前为「自选实时」模式,看板展示的大盘数据为<strong className="text-foreground">盘后快照</strong>(最新有数据日),并非盘中实时;
            仅自选股({data.quote_status?.watchlist_symbol_count ?? 0} 只)支持实时监控。
            <span className="ml-1 text-accent">全市场实时依赖数据源支持</span>
          </div>
        </div>
      )}

      <div className="mb-1.5 grid grid-cols-4 gap-1">
        {data.indices.map(item => <IndexTicker key={item.symbol} item={item} />)}
      </div>

      <div className="mb-1.5 grid grid-cols-6 gap-1">
        <KpiCell label="个股涨 / 平 / 跌" value={<><span className="text-bull">{data.breadth.up}</span><span className="text-muted">/</span><span className="text-muted">{data.breadth.flat}</span><span className="text-muted">/</span><span className="text-bear">{data.breadth.down}</span></>} sub={`上涨率 ${data.breadth.up_pct.toFixed(1)}%`} />
        <KpiCell label="强势 / 弱势" value={<><span className="text-bull">{data.breadth.strong_up ?? 0}</span><span className="text-muted">/</span><span className="text-bear">{data.breadth.strong_down ?? 0}</span></>} sub="涨跌 ≥3%" />
        <KpiCell label={<span className="inline-flex items-center gap-1">涨停 / 跌停<SealedBadge degraded={!hasDepth || !sealedReady} hasDepth={hasDepth} isHistorical={false} sealedReady={sealedReady} sealedCountsUp={{ real: data.limit.limit_up, fake: data.limit.fake_up ?? 0, pending: 0 }} sealedCountsDown={{ real: data.limit.limit_down, fake: data.limit.fake_down ?? 0, pending: 0 }} rawUp={data.limit.limit_up + (data.limit.fake_up ?? 0)} rawDown={data.limit.limit_down + (data.limit.fake_down ?? 0)} invalidateKeys={['overview-market', 'limit-ladder']} /></span>} value={<><span className="text-bull">{data.limit.limit_up}</span><span className="text-muted">/</span><span className="text-bear">{data.limit.limit_down}</span></>} sub={`封板率 ${(data.limit.seal_rate ?? 0).toFixed(0)}%`} />
        <KpiCell label="最高连板" value={`${data.limit.max_boards || 0}板`} sub={(() => {
          const top = data.limit.tiers.find(t => t.boards === data.limit.max_boards)
          const stocks = top?.stocks ?? []
          if (stocks.length > 0 && stocks.length <= 3) return stocks.map(s => s.name || s.symbol).join(' · ')
          return `梯队 ${data.limit.tiers.length}`
        })()} tone="accent" />
        <KpiCell label="成交额" value={fmtBigNum(data.amount.total)} sub={`均额 ${fmtBigNum(data.amount.avg)}`} />
        <KpiCell label="换手 / 量比" value={`${fmtPrice(data.activity.avg_turnover, 1)}% / ${fmtPrice(data.activity.vol_ratio, 2)}`} sub={`高换手 ${data.activity.high_turnover} · 放量占比 ${fmtPrice(data.activity.high_vol_ratio, 1)}%`} tone="accent" />
      </div>

      <div className="grid grid-cols-1 gap-1.5 xl:grid-cols-[minmax(0,1fr)_20rem]">
        <main className="min-w-0 space-y-1.5">
          <div className="grid grid-cols-1 gap-1.5 lg:grid-cols-3">
            <section className="rounded-card border border-border bg-surface/80 p-1.5 shadow-[0_1px_2px_hsl(var(--border)/0.4)] backdrop-blur-sm transition-shadow hover:shadow-[0_2px_8px_hsl(var(--border)/0.5)]">
              <SectionTitle icon={BarChart3} title="涨跌分布 / 广度" hint={`${data.breadth.total}只`} />
              <DistributionBars rows={data.distribution} />
              <div className="mt-2">
                <BreadthBar data={data.breadth} />
              </div>
              <div className="mt-2 grid grid-cols-2 gap-1.5">
                <MiniMetric label="平均涨跌" value={fmtStockPct(data.breadth.avg_pct)} cls={pctClass(data.breadth.avg_pct)} />
                <MiniMetric label="中位涨跌" value={fmtStockPct(data.breadth.median_pct)} cls={pctClass(data.breadth.median_pct)} />
              </div>
            </section>

            <section
              className="rounded-card border bg-surface/80 p-1.5 shadow-[0_1px_2px_hsl(var(--border)/0.4)] backdrop-blur-sm transition-shadow hover:shadow-[0_2px_8px_hsl(var(--border)/0.5)]"
              style={{ borderColor: `${scoreColor(data.emotion?.score ?? 50)}40` }}
            >
              <SectionTitle icon={Sparkles} title="情绪雷达" hint={`情绪评分 ${data.emotion?.score ?? 50}`} />
              <EmotionRadar radar={data.radar} score={data.emotion?.score ?? 50} />
            </section>

            <section className="flex flex-col rounded-card border border-border bg-surface/80 p-1.5 shadow-[0_1px_2px_hsl(var(--border)/0.4)] backdrop-blur-sm transition-shadow hover:shadow-[0_2px_8px_hsl(var(--border)/0.5)]">
              <div>
                <SectionTitle icon={LineChart} title="趋势强度" hint="均线/新高低" />
                <div className="grid grid-cols-3 gap-1.5">
                  <MiniMetric label="站上MA5" value={`${data.trend.above_ma5_pct.toFixed(0)}%`} cls="text-accent" />
                  <MiniMetric label="站上MA20" value={`${data.trend.above_ma20_pct.toFixed(0)}%`} cls="text-accent" />
                  <MiniMetric label="站上MA60" value={`${data.trend.above_ma60_pct.toFixed(0)}%`} cls="text-accent" />
                  <MiniMetric label="60日新高" value={compactCount(data.trend.new_high)} cls="text-bull" />
                  <MiniMetric label="60日新低" value={compactCount(data.trend.new_low)} cls="text-bear" />
                  <MiniMetric label="高低比" value={`${data.trend.new_high + data.trend.new_low > 0 ? Math.round(data.trend.new_high / (data.trend.new_high + data.trend.new_low) * 100) : 50}%`} cls={data.trend.new_high >= data.trend.new_low ? 'text-bull' : 'text-bear'} />
                </div>
              </div>
              <div className="mt-1.5 border-t border-border pt-1.5">
                <SectionTitle icon={Target} title="实用监控" hint="盘中观察" />
                <div className="grid grid-cols-3 gap-1.5">
                  <MiniMetric label="炸板" value={`${data.limit.broken ?? 0}`} cls="text-warning" />
                  <MiniMetric label="跌停" value={`${data.limit.limit_down ?? 0}`} cls="text-bear" />
                  <MiniMetric label="站上MA60" value={`${data.trend.above_ma60_pct.toFixed(0)}%`} cls="text-accent" />
                  <MiniMetric label="新高/新低" value={`${compactCount(data.trend.new_high)}/${compactCount(data.trend.new_low)}`} cls={data.trend.new_high >= data.trend.new_low ? 'text-bull' : 'text-bear'} />
                  <MiniMetric label="高换手数" value={`${data.activity.high_turnover}`} cls="text-accent" />
                  <MiniMetric label="放量占比" value={`${fmtPrice(data.activity.high_vol_ratio, 1)}%`} cls="text-accent" />
                </div>
              </div>
            </section>
          </div>

          <div className="grid grid-cols-1 gap-1.5 md:grid-cols-2">
            <HotRankCard title="概念热度" rank={data.concept_rank} configUrl="/concept-analysis" activeSymbol={activeSource === 'concept' ? activeSymbol : undefined}
              onStockClick={(symbol, name) => onStockClick?.('concept', symbol, name, rankNav(data.concept_rank))}
              onDimensionClick={onDimensionClick} />
            <HotRankCard title="行业热度" rank={data.industry_rank} configUrl="/industry-analysis" activeSymbol={activeSource === 'industry' ? activeSymbol : undefined}
              onStockClick={(symbol, name) => onStockClick?.('industry', symbol, name, rankNav(data.industry_rank))}
              onDimensionClick={onDimensionClick} />
          </div>

          <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2 xl:grid-cols-4">
            <StockList title="涨幅榜" rows={data.top_gainers} mode="gain" activeSymbol={activeSource === 'gain' ? activeSymbol : undefined} onStockClick={(symbol, name) => onStockClick?.('gain', symbol, name, stockListNav(data.top_gainers))} />
            <StockList title="跌幅榜" rows={data.top_losers} mode="loss" activeSymbol={activeSource === 'loss' ? activeSymbol : undefined} onStockClick={(symbol, name) => onStockClick?.('loss', symbol, name, stockListNav(data.top_losers))} />
            <StockList title="成交额榜" rows={data.turnover_leaders} mode="amount" activeSymbol={activeSource === 'amount' ? activeSymbol : undefined} onStockClick={(symbol, name) => onStockClick?.('amount', symbol, name, stockListNav(data.turnover_leaders))} />
            <StockList title="活跃换手" rows={data.active_leaders} mode="active" activeSymbol={activeSource === 'active' ? activeSymbol : undefined} onStockClick={(symbol, name) => onStockClick?.('active', symbol, name, stockListNav(data.active_leaders))} />
          </div>
        </main>

        <aside className="min-w-0 space-y-1.5">
          <section className="rounded-card border border-border bg-surface/80 p-1.5 shadow-[0_1px_2px_hsl(var(--border)/0.4)] backdrop-blur-sm transition-shadow hover:shadow-[0_2px_8px_hsl(var(--border)/0.5)]">
            <SectionTitle icon={Flame} title="涨停梯队" hint={<span className="inline-flex items-center gap-1">{`涨停 ${data.limit.limit_up}`}{(!hasDepth || !sealedReady) && <span className="text-[9px] px-1 rounded bg-yellow-500/10 text-yellow-600 dark:text-yellow-500">{hasDepth ? '未修正' : '降级'}</span>}</span>} />
            <LadderMini limit={data.limit} />
          </section>
          {showMonitor && (
            <section className="rounded-card border border-border bg-surface/80 p-1.5 shadow-[0_1px_2px_hsl(var(--border)/0.4)] backdrop-blur-sm transition-shadow hover:shadow-[0_2px_8px_hsl(var(--border)/0.5)]">
              <div className="mb-2 flex items-center justify-between gap-2">
                <div className="flex items-center gap-1.5">
                  <BellRing className="h-3.5 w-3.5 text-accent" />
                  <h2 className="text-xs font-semibold text-foreground">监控中心</h2>
                  <span className="font-mono text-[10px] text-muted">实时信号</span>
                </div>
                <Link to="/monitor" className="inline-flex items-center justify-center h-5 w-5 rounded text-muted hover:text-accent hover:bg-accent/10 transition-colors" title="进入监控中心">
                  <ArrowUpRight className="h-3.5 w-3.5" />
                </Link>
              </div>
              <MonitorWidget
                events={alerts}
                activeSymbol={activeSource === 'alert' ? activeSymbol : undefined}
                onStockClick={onAlertClick}
              />
            </section>
          )}
        </aside>
      </div>
    </>
  )
}
