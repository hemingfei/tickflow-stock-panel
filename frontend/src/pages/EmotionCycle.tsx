/**
 * 情绪周期页 — 每日情绪状态时序趋势 + 雷达图 + 状态分布。
 *
 * 数据来源: 后端 sentiment_builder 批算的时序表(每日情绪分 + 6 维度指标)。
 * 不复刻 Dashboard 的当日总览, 聚焦历史趋势与 6 维度拆解。
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import * as echarts from 'echarts'
import {
  Activity, RefreshCw, Loader2, Gauge, TrendingUp, TrendingDown, Minus,
  Pencil, CalendarDays, Repeat, RotateCcw, Rows3, LayoutGrid, Radar,
} from 'lucide-react'
import {
  api, type SentimentRow,
} from '@/lib/api'
import { useChartTheme } from '@/lib/theme'
import { toast } from '@/components/Toast'
import { Modal } from '@/components/Modal'
import { cn } from '@/lib/cn'

/** 情绪标签颜色映射 */
const EMOTION_COLORS: Record<string, string> = {
  '强势': '#ef4444',
  '偏暖': '#f59e0b',
  '震荡': '#6b7280',
  '偏冷': '#3b82f6',
  '冰点': '#10b981',
}


/** 综合分 → 对应颜色(与情绪标签一致阈值: 70/55/45/30) */
function scoreToColor(score: number): string {
  if (score >= 70) return '#ef4444'
  if (score >= 55) return '#f59e0b'
  if (score >= 45) return '#6b7280'
  if (score >= 30) return '#3b82f6'
  return '#10b981'
}

function emotionLabelToColor(label: string): string {
  return EMOTION_COLORS[label] || '#6b7280'
}

// 格式化日期，显示周几
function formatDateWithWeekday(dateStr: string): string {
  if (!dateStr) return dateStr
  const weekDays = ['日', '一', '二', '三', '四', '五', '六']
  const [y, m, d] = dateStr.split('-').map(Number)
  const date = new Date(y, m - 1, d)
  const weekDay = weekDays[date.getDay()]
  return `${dateStr} 周${weekDay}`
}

// ── 时间范围 ──────────────────────────────────────────────
type RangePreset = '1y' | '2y' | 'all' | { custom: number }

const RANGE_LABEL: Record<'1y' | '2y' | 'all', string> = {
  '1y': '1年', '2y': '2年', all: '全部',
}

/** 把 preset 解析成 (start?, end?, limit?) 三元组供 history 接口使用。 */
function resolveHistoryRange(
  preset: RangePreset,
  coverage: { earliest_date: string | null; latest_date: string | null } | undefined,
): { start?: string; end?: string; limit?: number } {
  if (preset === '1y') return { limit: 250 }
  if (preset === '2y') return { limit: 500 }
  if (preset === 'all') {
    // 全部: 用 coverage 实际日期范围, 不传 limit
    return { start: coverage?.earliest_date ?? undefined, end: coverage?.latest_date ?? undefined }
  }
  // 自定义天数
  return { limit: Math.max(1, Math.min(1000, preset.custom)) }
}

/** history/states 共用的"天数"语义: 用于标题展示。 */
function resolveDays(
  preset: RangePreset,
  coverage: { rows: number } | undefined,
): number {
  if (preset === '1y') return 250
  if (preset === '2y') return 500
  if (preset === 'all') return coverage?.rows && coverage.rows > 0 ? coverage.rows : 1000
  return Math.max(1, Math.min(1000, preset.custom))
}

function isPresetKey(p: RangePreset, k: '1y' | '2y' | 'all'): boolean {
  return p === k
}

// ── EChart hook ───────────────────────────────────────────
function useEChart(
  option: echarts.EChartsOption | null,
  deps: unknown[],
  events?: Record<string, (params: any) => void>,
  opts?: { notMerge?: boolean }
) {
  const ref = useRef<HTMLDivElement>(null)
  const instRef = useRef<echarts.ECharts | null>(null)
  const eventsRef = useRef<Record<string, (params: any) => void> | undefined>()
  
  useEffect(() => {
    if (!ref.current) return
    instRef.current = echarts.init(ref.current, undefined, { renderer: 'canvas' })
    const onResize = () => instRef.current?.resize()
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      instRef.current?.dispose()
      instRef.current = null
    }
  }, [])
  
  useEffect(() => {
    if (!instRef.current) return
    const inst = instRef.current
    // 移除旧事件
    if (eventsRef.current) {
      Object.keys(eventsRef.current).forEach(evt => {
        inst.off(evt)
      })
    }
    // 添加新事件
    if (events) {
      Object.entries(events).forEach(([evt, handler]) => {
        inst.on(evt, handler)
      })
    }
    eventsRef.current = events
  }, [events])
  
  useEffect(() => {
    if (instRef.current && option) {
      // 使用 replaceMerge 来优化更新性能，特别是对雷达图这样的图表
      instRef.current.setOption(option, { 
        notMerge: opts?.notMerge ?? true,
        lazyUpdate: false
      })
    }
  }, [option, ...deps])
  
  // 附加实例引用到 ref 对象上，方便外部访问
  ;(ref as any).instRef = instRef
  return ref
}

// ── 页内通用 SectionTitle (对齐 Dashboard 渐变竖条风格) ───
function SectionTitle({ icon: Icon, title, hint }: { icon: typeof Activity; title: string; hint?: ReactNode }) {
  return (
    <div className="flex items-center gap-2">
      <span className="h-3 w-0.5 rounded-full bg-gradient-to-b from-accent to-accent/30" />
      <Icon className="h-3.5 w-3.5 text-accent" />
      <h2 className="text-xs font-semibold text-foreground">{title}</h2>
      {hint != null && <span className="ml-auto text-[10px] text-muted font-mono">{hint}</span>}
    </div>
  )
}

// ── 卡片容器样式 (Dashboard 同款) ─────────────────────────
const cardCls = 'rounded-card border border-border bg-surface/80 shadow-[0_1px_2px_hsl(var(--border)/0.4)] backdrop-blur-sm transition-shadow hover:shadow-[0_2px_8px_hsl(var(--border)/0.5)]'

// ── 主组件 ────────────────────────────────────────────────
export function EmotionCycle() {
  const qc = useQueryClient()
  const [range, setRange] = useState<RangePreset>('1y')
  const [customOpen, setCustomOpen] = useState(false)
  // 日历热力图显示模式: false=单行(月份网格横向排列+滚动条, 默认), true=展开(换行完整网格)
  const [calendarExpanded, setCalendarExpanded] = useState(false)
  // 悬停日期: 记录用户在趋势图上悬停的日期索引，null 表示显示最新
  const [hoverIndex, setHoverIndex] = useState<number | null>(null)
  const ct = useChartTheme()

  // coverage: "全部"模式 + 标题展示依赖
  const coverage = useQuery({
    queryKey: ['sentiment-coverage'],
    queryFn: () => api.sentimentCoverage(),
    staleTime: 5 * 60 * 1000,
  })

  const days = resolveDays(range, coverage.data)
  const histRange = resolveHistoryRange(range, coverage.data)

  // queryKey 用 range 的完整三元组区分: limit / start+end(全部) / custom天数
  const history = useQuery({
    queryKey: ['sentiment-history', range] as const,
    queryFn: () => api.sentimentHistory(histRange.start, histRange.end, histRange.limit),
    staleTime: 5 * 60 * 1000,
  })

  const [recomputing, setRecomputing] = useState(false)
  const [refreshing, setRefreshing] = useState(false)

  const rows: SentimentRow[] = history.data?.rows ?? []
  const latestIndex = hoverIndex ?? (rows.length - 1)
  const latest = rows.length > 0 ? rows[latestIndex] : null

  // ── 当前势头: 基于当前悬停日期计算势头 ──
  const momentum = useMemo(() => {
    if (rows.length === 0) return null
    const currentLabel = rows[latestIndex].emotion_label
    // 从当前索引向前连续同标签天数
    let streak = 1
    for (let i = latestIndex - 1; i >= 0; i--) {
      if (rows[i].emotion_label === currentLabel) streak++
      else break
    }
    // score 5日斜率(正=改善, 负=恶化) - 取当前索引向前5天
    const recentStart = Math.max(0, latestIndex - 4)
    const recent = rows.slice(recentStart, latestIndex + 1)
    const slope = recent.length >= 2
      ? (recent[recent.length - 1].emotion_score - recent[0].emotion_score) / (recent.length - 1)
      : 0
    // 上次冰点距当前天数
    let lastFreezeGap = 0
    for (let i = latestIndex; i >= 0; i--) {
      if (rows[i].emotion_label === '冰点' || rows[i].emotion_label === '偏冷') {
        lastFreezeGap = latestIndex - i
        break
      }
    }
    return { streak, label: currentLabel, slope, lastFreezeGap }
  }, [rows, latestIndex])

  // �─ 状态转换频率: 近 N 天相邻 label 变化次数 ──
  const transitions = useMemo(() => {
    if (rows.length < 2) return { count: 0, rate: 0, label: '数据不足' }
    let count = 0
    for (let i = 1; i < rows.length; i++) {
      if (rows[i].emotion_label !== rows[i - 1].emotion_label) count++
    }
    // 频率 = 转换次数 / 天数; <0.2 稳定, 0.2-0.4 中等, >0.4 频繁
    const rate = count / (rows.length - 1)
    const label = rate < 0.2 ? '稳定' : rate < 0.4 ? '中等切换' : '频繁切换'
    return { count, rate, label }
  }, [rows])

  // 趋势图: 情绪分主线 + 6 子维度曲线(可切换) + 情绪标签背景色带 + 涨停数柱状
  const trendOption = useMemo<echarts.EChartsOption | null>(() => {
    if (rows.length === 0) return null
    const dates = rows.map(r => r.date)
    const scores = rows.map(r => r.emotion_score)
    const limitUps = rows.map(r => r.limit_up)
    const indexScore = rows.map(r => r.index_score ?? null)
    const profitScore = rows.map(r => r.profit_score ?? null)
    const moneyScore = rows.map(r => r.money_score ?? null)
    const speculationScore = rows.map(r => r.speculation_score ?? null)
    const resilienceScore = rows.map(r => r.resilience_score ?? null)
    const mainlineScore = rows.map(r => r.mainline_score ?? null)

    // 情绪标签背景色带: 合并连续同标签日期段, 每段用情绪标签色低透明度着色
    const stateBands: any[] = []
    let bandStart = rows[0]?.date
    let prevLabel = rows[0]?.emotion_label
    rows.forEach((r, i) => {
      if (r.emotion_label !== prevLabel || i === rows.length - 1) {
        const bandEnd = i === rows.length - 1 ? r.date : rows[i - 1].date
        if (prevLabel && emotionLabelToColor(prevLabel)) {
          stateBands.push([
            { xAxis: bandStart, itemStyle: { color: emotionLabelToColor(prevLabel), opacity: 0.08 } },
            { xAxis: bandEnd },
          ])
        }
        bandStart = r.date
        prevLabel = r.emotion_label
      }
    })

    const subLineStyle = { width: 1.2, type: 'dotted' as const, opacity: 0.8 }

    return {
      backgroundColor: 'transparent',
      tooltip: { 
        trigger: 'axis', 
        triggerOn: 'mousemove|click' as const,
        backgroundColor: ct.tooltipBg, 
        borderColor: ct.tooltipBorder, 
        textStyle: { color: ct.tooltipText },
        enterable: true,
        formatter: (params: any) => {
          if (params && params.length > 0 && params[0].dataIndex != null) {
            setHoverIndex(params[0].dataIndex)
          }
          
          // 构建自定义 tooltip 内容
          if (!params || params.length === 0) return ''
          
          let result = `<div style="font-weight: 600; margin-bottom: 8px;">${params[0].axisValue}</div>`
          
          params.forEach((param: any) => {
            if (param.seriesName && param.value != null) {
              const marker = param.marker || '<span style="display:inline-block;margin-right:4px;border-radius:10px;width:10px;height:10px;background-color:' + param.color + ';"></span>'
              result += `<div style="display:flex;align-items:center;margin:3px 0;">${marker}<span style="margin-left:4px;">${param.seriesName}:</span><span style="font-weight:600;margin-left:8px;">${param.value}</span></div>`
            }
          })
          
          return result
        }
      },
      axisPointer: {
        type: 'cross' as const,
        snap: true
      },
      legend: {
        data: ['情绪分', '涨停数', '指数', '赚钱', '量能', '投机', '抗跌', '主线'],
        textStyle: { color: ct.text, fontSize: 10 }, top: 0,
        selected: { '情绪分': true, '涨停数': true, '指数': false, '赚钱': false, '量能': false, '投机': false, '抗跌': false, '主线': false },
      },
      grid: { left: 48, right: 64, top: 36, bottom: 56 },
      xAxis: {
        type: 'category', data: dates, boundaryGap: false,
        axisLabel: { color: ct.text, fontSize: 10, formatter: (v: string) => v.slice(5) },
        axisLine: { lineStyle: { color: ct.grid } },
      },
      yAxis: [
        { type: 'value', name: '涨停', position: 'left', axisLabel: { color: ct.text, fontSize: 10 }, splitLine: { show: false }, nameTextStyle: { color: ct.text } },
        { type: 'value', name: '情绪分', min: 0, max: 100, position: 'right', axisLabel: { color: ct.text, fontSize: 10 }, splitLine: { lineStyle: { color: ct.grid } }, nameTextStyle: { color: ct.text } },
      ],
      dataZoom: [
        { type: 'inside', start: Math.max(0, 100 - (60 / days) * 100) },
        { type: 'slider', bottom: 8, height: 16, borderColor: ct.border, fillerColor: ct.zoomFill, textStyle: { color: ct.text } },
      ],
      series: [
        // 涨停数柱状(半透明背景, 左轴)
        { name: '涨停数', type: 'bar', data: limitUps, yAxisIndex: 0, barMaxWidth: 6,
          itemStyle: { color: '#ef4444', opacity: 0.35 }, z: 1 },
        // 6 子维度曲线(右轴=情绪分)
        { name: '指数', type: 'line', data: indexScore, smooth: true, symbol: 'none', yAxisIndex: 1,
          lineStyle: { ...subLineStyle, color: '#3b82f6' }, z: 2 },
        { name: '赚钱', type: 'line', data: profitScore, smooth: true, symbol: 'none', yAxisIndex: 1,
          lineStyle: { ...subLineStyle, color: '#f59e0b' }, z: 2 },
        { name: '量能', type: 'line', data: moneyScore, smooth: true, symbol: 'none', yAxisIndex: 1,
          lineStyle: { ...subLineStyle, color: '#8b5cf6' }, z: 2 },
        { name: '投机', type: 'line', data: speculationScore, smooth: true, symbol: 'none', yAxisIndex: 1,
          lineStyle: { ...subLineStyle, color: '#a855f7' }, z: 2 },
        { name: '抗跌', type: 'line', data: resilienceScore, smooth: true, symbol: 'none', yAxisIndex: 1,
          lineStyle: { ...subLineStyle, color: '#10b981' }, z: 2 },
        { name: '主线', type: 'line', data: mainlineScore, smooth: true, symbol: 'none', yAxisIndex: 1,
          lineStyle: { ...subLineStyle, color: '#ec4899' }, z: 2 },
        // 情绪分主线(加粗置顶, 右轴) + 情绪标签背景色带 + 阈值横虚线
        { name: '情绪分', type: 'line', data: scores, smooth: true, symbol: 'none', yAxisIndex: 1,
          lineStyle: { width: 1.5, color: ct.textStrong }, areaStyle: { opacity: 0.06 }, z: 3,
          markArea: { silent: true, data: stateBands },
          markLine: {
            silent: true,
            symbol: 'none',
            lineStyle: { type: 'dashed', width: 1.5 },
            label: { position: 'end', fontSize: 10, fontWeight: 'bold', padding: [2, 4], borderRadius: 3 },
            data: [
              { yAxis: 70, lineStyle: { color: '#ef4444' },
                label: { formatter: '强势 70', color: '#fff', backgroundColor: '#ef4444' } },
              { yAxis: 55, lineStyle: { color: '#f59e0b' },
                label: { formatter: '偏暖 55', color: '#fff', backgroundColor: '#f59e0b' } },
              { yAxis: 45, lineStyle: { color: '#6b7280' },
                label: { formatter: '震荡 45', color: '#fff', backgroundColor: '#6b7280' } },
              { yAxis: 30, lineStyle: { color: '#3b82f6' },
                label: { formatter: '偏冷 30', color: '#fff', backgroundColor: '#3b82f6' } },
            ],
          } },
      ],
    }
  }, [rows, days, ct])
  
  // 简化的趋势图事件处理
  const trendEvents = useMemo(() => ({
    // 鼠标离开整个图表区域时清除
    'globalout': () => {
      setHoverIndex(null)
    }
  }), [])
  
  const trendRef = useEChart(trendOption, [trendOption], trendEvents)

  // 雷达图: 最新日 6 维度
  const radarOption = useMemo<echarts.EChartsOption | null>(() => {
    if (!latest) return null
    return {
      backgroundColor: 'transparent',
      animation: true,
      animationDuration: 200,
      animationDurationUpdate: 200,
      animationEasing: 'cubicOut',
      animationEasingUpdate: 'cubicOut',
      tooltip: {
        backgroundColor: ct.tooltipBg,
        borderColor: ct.tooltipBorder,
        textStyle: { color: ct.tooltipText },
        formatter: () => {
          if (!latest) return ''
          return `
            <div style="padding: 4px 0;">
              <div style="font-weight: 600; margin-bottom: 6px;">${latest.emotion_label}</div>
              <div style="display: grid; gap: 2px;">
                <div>指数: ${latest.index_score}</div>
                <div>赚钱: ${latest.profit_score}</div>
                <div>量能: ${latest.money_score}</div>
                <div>投机: ${latest.speculation_score}</div>
                <div>抗跌: ${latest.resilience_score}</div>
                <div>主线: ${latest.mainline_score}</div>
              </div>
            </div>
          `
        }
      },
      radar: {
        startAngle: 90,
        indicator: [
          { name: '指数', max: 100 },
          { name: '主线', max: 100 },
          { name: '抗跌', max: 100 },
          { name: '投机', max: 100 },
          { name: '量能', max: 100 },
          { name: '赚钱', max: 100 },
        ],
        axisName: { color: ct.text, fontSize: 11 },
        splitArea: { areaStyle: { color: ['rgba(255,255,255,0.02)', 'rgba(255,255,255,0.04)'] } },
        axisLine: { lineStyle: { color: ct.grid } },
        splitLine: { lineStyle: { color: ct.grid } },
      },
      series: [{
        type: 'radar',
        data: [{
          id: 'emotion-radar',
          value: [
            latest.index_score,
            latest.mainline_score,
            latest.resilience_score,
            latest.speculation_score,
            latest.money_score,
            latest.profit_score,
          ],
          name: latest.emotion_label,
          areaStyle: { opacity: 0.3, color: emotionLabelToColor(latest.emotion_label) },
          lineStyle: { color: emotionLabelToColor(latest.emotion_label), width: 2 },
          itemStyle: { color: emotionLabelToColor(latest.emotion_label) },
        }],
      }],
    }
  }, [latest, ct])
  const radarRef = useEChart(radarOption, [radarOption], undefined, { notMerge: false })

  // 日历热力图数据: 按月分组(纯 CSS 渲染)
  const calendarMonths = useMemo(() => {
    if (rows.length === 0) return []
    const byMonth = new Map<string, SentimentRow[]>()
    for (const r of rows) {
      const ym = r.date.slice(0, 7) // YYYY-MM
      if (!byMonth.has(ym)) byMonth.set(ym, [])
      byMonth.get(ym)!.push(r)
    }
    const MONTH_LABELS = ['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月']
    return [...byMonth.entries()].sort().map(([ym, monthRows]) => {
      const [y, m] = ym.split('-')
      const year = Number(y), month = Number(m)
      const dateToRow = new Map<string, SentimentRow>()
      for (const r of monthRows) dateToRow.set(r.date, r)
      const firstDow = new Date(year, month - 1, 1).getDay()
      const leadOffset = firstDow === 0 ? 6 : firstDow - 1
      const cells: (SentimentRow | null)[] = Array(leadOffset).fill(null)
      const dayCount = new Date(year, month, 0).getDate()
      for (let day = 1; day <= dayCount; day++) {
        const ds = `${y}-${m.padStart(2, '0')}-${String(day).padStart(2, '0')}`
        cells.push(dateToRow.has(ds) ? dateToRow.get(ds)! : null)
      }
      while (cells.length % 7 !== 0) cells.push(null)
      const weeks: (SentimentRow | null)[][] = []
      for (let i = 0; i < cells.length; i += 7) weeks.push(cells.slice(i, i + 7))
      return { year, month, label: `${y}年${MONTH_LABELS[month - 1]}`, weeks }
    })
  }, [rows])

  // 日历热力图横向滚动容器
  const calendarScrollRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!calendarExpanded && calendarScrollRef.current) {
      calendarScrollRef.current.scrollLeft = calendarScrollRef.current.scrollWidth
    }
  }, [calendarExpanded, calendarMonths])

  const handleRecompute = async () => {
    setRecomputing(true)
    try {
      const r = await api.sentimentRecompute()
      toast(r.computed > 0 ? `重算完成 · 新增 ${r.computed} 天` : '重算完成 · 数据已是最新', 'success')
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['sentiment-history'] }),
        qc.invalidateQueries({ queryKey: ['sentiment-latest'] }),
        qc.invalidateQueries({ queryKey: ['sentiment-coverage'] }),
      ])
    } catch (e) {
      toast(`重算失败 · ${String((e as Error)?.message || e)}`, 'error')
    } finally {
      setRecomputing(false)
    }
  }

  const handleRefresh = async () => {
    setRefreshing(true)
    try {
      const r = await api.sentimentRefresh()
      toast(r.computed > 0 ? `刷新完成 · 新增 ${r.computed} 天` : '刷新完成 · 数据已是最新', 'success')
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['sentiment-history'] }),
        qc.invalidateQueries({ queryKey: ['sentiment-latest'] }),
        qc.invalidateQueries({ queryKey: ['sentiment-coverage'] }),
      ])
    } catch (e) {
      toast(`刷新失败 · ${String((e as Error)?.message || e)}`, 'error')
    } finally {
      setRefreshing(false)
    }
  }

  const customLabel = typeof range === 'object'
    ? `自定义 ${range.custom}天`
    : '自定义'

  return (
    <div className="mx-auto max-w-[1440px] px-4 py-5 space-y-4">
      {/* ── 头部 ── */}
      <div className={cn(cardCls, 'relative overflow-hidden rounded-card bg-gradient-to-r from-surface/90 to-surface/70 px-4 py-3')}>
        <div className="absolute left-0 top-0 h-full w-1 bg-gradient-to-b from-accent to-accent/20" />
        <div className="flex items-center gap-3">
          <Activity className="h-5 w-5 text-accent" />
          <h1 className="text-base font-semibold text-foreground">情绪周期</h1>
          <span className="text-xs text-muted">6维度情绪分 · 赚钱效应 · 日历热力图</span>
          <div className="ml-auto flex items-center gap-2">
            {/* 时间范围按钮组 */}
            <div className="flex items-center rounded-btn border border-border bg-base/60 p-0.5">
              {(['1y', '2y', 'all'] as const).map(k => (
                <button
                  key={k}
                  onClick={() => setRange(k)}
                  className={cn(
                    'h-6 rounded-[5px] px-2.5 text-xs font-medium transition-colors',
                    isPresetKey(range, k)
                      ? 'bg-accent text-white shadow-sm'
                      : 'text-secondary hover:text-foreground',
                  )}
                >
                  {RANGE_LABEL[k]}
                </button>
              ))}
              <button
                onClick={() => setCustomOpen(true)}
                className={cn(
                  'inline-flex items-center gap-1 h-6 rounded-[5px] px-2.5 text-xs font-medium transition-colors',
                  typeof range === 'object'
                    ? 'bg-accent text-white shadow-sm'
                    : 'text-secondary hover:text-foreground',
                )}
              >
                {typeof range === 'object' && <Pencil className="h-3 w-3" />}
                {customLabel}
              </button>
            </div>
            {/* 刷新 */}
            <button onClick={handleRefresh} disabled={refreshing || recomputing}
              className="inline-flex items-center gap-1.5 h-7 px-3 rounded-btn border border-border bg-base text-xs text-secondary hover:text-accent disabled:opacity-50">
              {refreshing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
              {refreshing ? '刷新中…' : '刷新'}
            </button>
            {/* 重算 */}
            <button onClick={handleRecompute} disabled={recomputing || refreshing}
              className="inline-flex items-center gap-1.5 h-7 px-3 rounded-btn border border-border bg-base text-xs text-secondary hover:text-accent disabled:opacity-50">
              {recomputing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RotateCcw className="h-3.5 w-3.5" />}
              {recomputing ? '重算中…' : '重算'}
            </button>
          </div>
        </div>
      </div>

      {/* ── 最新日概览 ── */}
      {latest ? (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          {/* 情绪状态卡 */}
          <div className={cn(cardCls, 'p-3')}>
            <div className="flex items-center gap-1.5 text-[10px] text-muted">
              <Gauge className="h-3 w-3" /> 最新状态 · {formatDateWithWeekday(latest.date)}
            </div>
            <div className="mt-1.5 flex items-baseline gap-2">
              <span className="text-2xl font-bold" style={{ color: emotionLabelToColor(latest.emotion_label) }}>
                {latest.emotion_label}
              </span>
              <span className="text-sm text-muted">{latest.emotion_score} 分</span>
            </div>
            <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-base">
              <div className="h-full rounded-full transition-all"
                style={{ width: `${Math.max(2, Math.min(100, latest.emotion_score))}%`, backgroundColor: emotionLabelToColor(latest.emotion_label) }} />
            </div>
          </div>

          {/* 当前势头 */}
          <div className={cn(cardCls, 'p-3')}>
            <div className="flex items-center gap-1.5 text-[10px] text-muted">
              {(() => {
                const TrendIcon = (momentum?.slope ?? 0) > 0.5 ? TrendingUp : (momentum?.slope ?? 0) < -0.5 ? TrendingDown : Minus
                return <TrendIcon className={`h-3 w-3 ${(momentum?.slope ?? 0) > 0.5 ? 'text-bull' : (momentum?.slope ?? 0) < -0.5 ? 'text-bear' : 'text-muted'}`} />
              })()} 当前势头
            </div>
            {momentum ? (
              <>
                <div className="mt-1.5 text-sm font-semibold text-foreground">
                  连续 <span style={{ color: emotionLabelToColor(momentum.label) }}>{momentum.streak}</span> 天{momentum.label}
                </div>
                <div className="mt-1 text-[10px] text-muted">
                  5日{(momentum.slope > 0 ? '改善' : momentum.slope < 0 ? '恶化' : '持平')}
                  {momentum.lastFreezeGap > 0 && ` · 上次冰点 ${momentum.lastFreezeGap} 天前`}
                </div>
              </>
            ) : <div className="mt-1.5 text-sm text-muted">—</div>}
          </div>

          {/* 6 子维度迷你条 */}
          <div className={cn(cardCls, 'p-3')}>
            <div className="flex items-center gap-1.5 text-[10px] text-muted">
              <Activity className="h-3 w-3" /> 六维拆解 · {formatDateWithWeekday(latest.date)}
            </div>
            <div className="mt-2 space-y-1">
              {([
                { label: '指数', val: latest.index_score, color: '#3b82f6' },
                { label: '赚钱', val: latest.profit_score, color: '#f59e0b' },
                { label: '量能', val: latest.money_score, color: '#8b5cf6' },
                { label: '投机', val: latest.speculation_score, color: '#a855f7' },
                { label: '抗跌', val: latest.resilience_score, color: '#10b981' },
                { label: '主线', val: latest.mainline_score, color: '#ec4899' },
              ] as const).map(d => (
                <div key={d.label} className="flex items-center gap-1.5">
                  <span className="w-6 shrink-0 text-[9px] text-muted">{d.label}</span>
                  <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-base">
                    <div className="h-full rounded-full transition-all"
                      style={{ width: `${d.val ?? 0}%`, backgroundColor: d.color }} />
                  </div>
                  <span className="w-5 shrink-0 text-right text-[9px] font-mono text-muted">{d.val ?? '—'}</span>
                </div>
              ))}
            </div>
          </div>

          {/* 状态转换频率 */}
          <div className={cn(cardCls, 'p-3')}>
            <div className="flex items-center gap-1.5 text-[10px] text-muted">
              <Repeat className="h-3 w-3" /> 情绪切换 · 近 {days} 天
            </div>
            <div className="mt-1.5 text-lg font-semibold text-foreground">
              {transitions.count} <span className="text-xs font-normal text-muted">次切换</span>
            </div>
            <div className="mt-1 text-[10px] text-muted">
              节奏：<span className="text-accent">{transitions.label}</span>
              <span className="ml-1">({(transitions.rate * 100).toFixed(0)}%/天)</span>
            </div>
          </div>
        </div>
      ) : (
        <div className="rounded-card border border-dashed border-border p-8 text-center text-sm text-muted">
          {history.isLoading ? '加载中…' : '暂无情绪数据，请先运行盘后管道或点击「重算」'}
        </div>
      )}

      {/* ── 情绪色带时间轴 ── */}
      {rows.length > 0 && (
        <div className={cn(cardCls, 'p-3')}>
          <SectionTitle icon={Activity} title="情绪时间轴"
            hint={`${rows[0]?.date} → ${rows[rows.length - 1]?.date} · ${rows.length} 天`} />
          <div className="mt-2.5 flex h-7 w-full overflow-hidden rounded-md">
            {rows.map(r => (
              <div key={r.date} title={`${r.date} ${r.emotion_label}(${r.emotion_score})`}
                className="flex-1 min-w-[2px] transition-opacity hover:opacity-80"
                style={{ backgroundColor: emotionLabelToColor(r.emotion_label) }} />
            ))}
          </div>
          <div className="mt-2 flex flex-wrap items-center justify-center gap-x-4 gap-y-1 text-[10px] text-muted">
            {Object.entries(EMOTION_COLORS).map(([label, color]) => (
              <span key={label} className="flex items-center gap-1">
                <span className="inline-block h-2.5 w-2.5 rounded" style={{ backgroundColor: color }} />
                {label}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* ── 雷达图 + 趋势图 ── */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className={cn(cardCls, 'p-3')}>
          <SectionTitle icon={Radar} title={hoverIndex != null ? "悬停日期雷达图" : "最新雷达图"} hint={latest ? latest.date : ''} />
          <div ref={radarRef} className="mt-2 h-[320px]" />
        </div>
        <div className={cn(cardCls, 'p-3 lg:col-span-2')}>
          <SectionTitle icon={Activity} title="情绪分趋势"
            hint="情绪分(粗) · 6维度(细, 可点图例切换) · 背景色=情绪标签 · 悬停查看历史数据" />
          <div ref={trendRef} className="mt-2 h-[320px]" />
        </div>
      </div>

      {/* ── 日历热力图 ── */}
      {calendarMonths.length > 0 && (
        <div className={cn(cardCls, 'p-3')}>
          <SectionTitle icon={CalendarDays} title="日历热力图"
            hint={
              <button
                onClick={() => setCalendarExpanded(v => !v)}
                className="inline-flex items-center gap-1 rounded-btn border border-border bg-base px-2 py-0.5 text-[10px] text-secondary hover:text-accent hover:border-accent/40 transition-colors"
                title={calendarExpanded ? '切换为单行紧凑' : '切换为月份展开'}
              >
                {calendarExpanded ? <><Rows3 className="h-3 w-3" />单行</> : <><LayoutGrid className="h-3 w-3" />展开</>}
              </button>
            }
          />
          {calendarExpanded ? (
            /* 展开模式 */
            <div className="mt-3 flex flex-wrap gap-x-5 gap-y-3">
              {calendarMonths.map(mo => {
                const monthRows = mo.weeks.flat().filter((c): c is SentimentRow => !!c)
                const avgScore = monthRows.length > 0
                  ? Math.round(monthRows.reduce((s, r) => s + r.emotion_score, 0) / monthRows.length) : 0
                return (
                  <div key={`${mo.year}-${mo.month}`} className="shrink-0">
                    <div className="mb-1 flex items-center gap-1.5">
                      <span className="text-[10px] font-medium text-secondary">{mo.label}</span>
                      {avgScore > 0 && (
                        <span className="rounded px-1 py-px text-[9px] font-semibold"
                          style={{ color: scoreToColor(avgScore), backgroundColor: scoreToColor(avgScore) + '20' }}>
                          {avgScore}
                        </span>
                      )}
                    </div>
                    <div className="mb-0.5 grid grid-cols-7 gap-[2px] text-[8px] text-muted">
                      {['一', '二', '三', '四', '五', '六', '日'].map(d => (
                        <div key={d} className="text-center">{d}</div>
                      ))}
                    </div>
                    <div className="grid grid-cols-7 gap-[2px]">
                      {mo.weeks.flat().map((cell, i) => (
                        cell ? (
                          <div key={i}
                            title={`${cell.date} ${cell.emotion_label}(${cell.emotion_score})`}
                            className="h-[14px] w-[14px] rounded-[2px] transition-transform hover:scale-125 hover:z-10 cursor-default"
                            style={{ backgroundColor: emotionLabelToColor(cell.emotion_label) }}
                          />
                        ) : (
                          <div key={i} className="h-[14px] w-[14px]" />
                        )
                      ))}
                    </div>
                  </div>
                )
              })}
            </div>
          ) : (
            /* 单行模式 */
            <div ref={calendarScrollRef} className="mt-3 flex gap-x-5 overflow-x-auto pb-2">
              {calendarMonths.map(mo => {
                const monthRows = mo.weeks.flat().filter((c): c is SentimentRow => !!c)
                const avgScore = monthRows.length > 0
                  ? Math.round(monthRows.reduce((s, r) => s + r.emotion_score, 0) / monthRows.length) : 0
                return (
                  <div key={`${mo.year}-${mo.month}`} className="shrink-0">
                    <div className="mb-1 flex items-center gap-1.5">
                      <span className="text-[10px] font-medium text-secondary">{mo.label}</span>
                      {avgScore > 0 && (
                        <span className="rounded px-1 py-px text-[9px] font-semibold"
                          style={{ color: scoreToColor(avgScore), backgroundColor: scoreToColor(avgScore) + '20' }}>
                          {avgScore}
                        </span>
                      )}
                    </div>
                    <div className="mb-0.5 grid grid-cols-7 gap-[2px] text-[8px] text-muted">
                      {['一', '二', '三', '四', '五', '六', '日'].map(d => (
                        <div key={d} className="text-center">{d}</div>
                      ))}
                    </div>
                    <div className="grid grid-cols-7 gap-[2px]">
                      {mo.weeks.flat().map((cell, i) => (
                        cell ? (
                          <div key={i}
                            title={`${cell.date} ${cell.emotion_label}(${cell.emotion_score})`}
                            className="h-[14px] w-[14px] rounded-[2px] transition-transform hover:scale-125 hover:z-10 cursor-default"
                            style={{ backgroundColor: emotionLabelToColor(cell.emotion_label) }}
                          />
                        ) : (
                          <div key={i} className="h-[14px] w-[14px]" />
                        )
                      ))}
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </div>
      )}

      {/* ── 自定义天数弹窗 ── */}
      {customOpen && (
        <CustomDaysModal
          current={typeof range === 'object' ? range.custom : 120}
          onClose={() => setCustomOpen(false)}
          onApply={(d) => { setRange({ custom: d }); setCustomOpen(false) }}
        />
      )}
    </div>
  )
}

// ── 自定义天数输入弹窗 ────────────────────────────────────
function CustomDaysModal({ current, onClose, onApply }: {
  current: number
  onClose: () => void
  onApply: (days: number) => void
}) {
  const [val, setVal] = useState(String(current))
  const inputRef = useRef<HTMLInputElement>(null)

  const apply = () => {
    const n = Math.max(1, Math.min(1000, Math.floor(Number(val) || 0)))
    if (Number.isNaN(n) || n < 1) {
      toast('请输入 1 ~ 1000 之间的天数', 'error')
      return
    }
    onApply(n)
  }

  return (
    <Modal onClose={onClose} ariaLabel="自定义天数" initialFocusRef={inputRef}
      panelClassName="w-[88vw] max-w-xs bg-surface border border-border rounded-card shadow-xl p-4">
      <div className="space-y-3">
        <div>
          <div className="text-xs font-medium text-foreground">自定义天数</div>
          <div className="mt-0.5 text-[10px] text-muted">范围 1 ~ 1000 个交易日</div>
        </div>
        <input
          ref={inputRef}
          type="number"
          min={1}
          max={1000}
          value={val}
          onChange={e => setVal(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') apply() }}
          className="h-8 w-full rounded-input border border-border bg-base px-2.5 text-sm text-foreground outline-none focus:border-accent"
        />
        {/* 快捷预设 */}
        <div className="flex flex-wrap gap-1.5">
          {[60, 90, 180, 365].map(d => (
            <button key={d} onClick={() => setVal(String(d))}
              className="h-6 rounded-btn border border-border bg-base px-2 text-[11px] text-secondary hover:text-accent hover:border-accent/40 transition-colors">
              {d}天
            </button>
          ))}
        </div>
        <div className="flex justify-end gap-2 pt-1">
          <button onClick={onClose}
            className="h-7 rounded-btn px-3 text-xs text-secondary hover:text-foreground transition-colors">
            取消
          </button>
          <button onClick={apply}
            className="h-7 rounded-btn bg-accent px-3 text-xs font-medium text-white hover:bg-accent/90 transition-colors">
            应用
          </button>
        </div>
      </div>
    </Modal>
  )
}
