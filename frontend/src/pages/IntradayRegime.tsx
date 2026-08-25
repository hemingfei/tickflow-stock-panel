/**
 * 实时环境分时页面 - 分钟级环境综合分走势
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import * as echarts from 'echarts'
import {
  Activity, RefreshCw, Loader2, Gauge, Clock, Zap
} from 'lucide-react'
import {
  api, type IntradayRegimeRow,
  REGIME_STATE_LABELS,
  REGIME_STATE_COLORS,
} from '@/lib/api'
import { useChartTheme } from '@/lib/theme'
import { toast } from '@/components/Toast'
import { cn } from '@/lib/cn'
import { DatePicker } from '@/components/DatePicker'

/** 环境标签颜色映射 */
const REGIME_LABEL_COLORS: Record<string, string> = {
  '强势': REGIME_STATE_COLORS['strong'],
  '偏强': REGIME_STATE_COLORS['lean_strong'],
  '震荡': REGIME_STATE_COLORS['range'],
  '偏弱': REGIME_STATE_COLORS['lean_weak'],
  '弱势': REGIME_STATE_COLORS['weak'],
}

/** ECharts hook */
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
    if (eventsRef.current) {
      Object.keys(eventsRef.current).forEach(evt => {
        inst.off(evt)
      })
    }
    if (events) {
      Object.entries(events).forEach(([evt, handler]) => {
        inst.on(evt, handler)
      })
    }
    eventsRef.current = events
  }, [events])

  useEffect(() => {
    if (instRef.current && option) {
      instRef.current.setOption(option, {
        notMerge: opts?.notMerge ?? true,
        lazyUpdate: false
      })
    }
  }, [option, ...deps])

  ;(ref as any).instRef = instRef
  return ref
}

/** 通用 SectionTitle */
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

const cardCls = 'rounded-card border border-border bg-surface/80 shadow-[0_1px_2px_hsl(var(--border)/0.4)] backdrop-blur-sm transition-shadow hover:shadow-[0_2px_8px_hsl(var(--border)/0.5)]'

export function IntradayRegime() {
  const qc = useQueryClient()
  const [hoverIndex, setHoverIndex] = useState<number | null>(null)
  const [selectedDate, setSelectedDate] = useState<string | null>(null)
  const ct = useChartTheme()

  // 查询可用日期
  const dates = useQuery({
    queryKey: ['intradayRegimeDates'],
    queryFn: () => api.intradayRegimeDates(),
  })

  // 查询状态
  const status = useQuery({
    queryKey: ['intradayRegimeStatus'],
    queryFn: () => api.intradayRegimeStatus(),
    refetchInterval: 30000, // 30 秒刷新一次
  })

  // 查询历史数据
  const history = useQuery({
    queryKey: ['intradayRegimeHistory', selectedDate],
    queryFn: () => api.intradayRegimeHistory(selectedDate),
    refetchInterval: selectedDate ? undefined : 60000, // 只有在查看今天时才自动刷新
  })

  const rows: IntradayRegimeRow[] = history.data?.data ?? []
  const latestIndex = hoverIndex ?? (rows.length - 1)
  const latest = rows.length > 0 ? rows[latestIndex] : null

  // 实时更新查询
  const [isRefreshing, setIsRefreshing] = useState(false)

  const handleRefresh = async () => {
    setIsRefreshing(true)
    try {
      await api.intradayRegimeCompute(true)
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['intradayRegimeHistory'] }),
        qc.invalidateQueries({ queryKey: ['intradayRegimeStatus'] }),
      ])
      toast('已更新实时环境数据', 'success')
    } catch (e) {
      toast(`更新失败: ${String((e as Error)?.message || e)}`, 'error')
    } finally {
      setIsRefreshing(false)
    }
  }

  // 分时趋势图
  const trendOption = useMemo<echarts.EChartsOption | null>(() => {
    if (rows.length === 0) return null
    const times = rows.map(r => r.time)
    const scores = rows.map(r => r.score)
    const limitUps = rows.map(r => r.limit_up)
    const profitScores = rows.map(r => r.profit_score ?? null)
    const speculationScores = rows.map(r => r.speculation_score ?? null)
    const resilienceScores = rows.map(r => r.resilience_score ?? null)
    const trendScores = rows.map(r => r.trend_score ?? null)

    // 环境标签背景色带
    const stateBands: any[] = []
    if (rows.length > 0) {
      let bandStartIdx = 0
      let prevLabel = REGIME_STATE_LABELS[rows[0].state]
      for (let i = 1; i < rows.length; i++) {
        const currentLabel = REGIME_STATE_LABELS[rows[i].state]
        if (currentLabel !== prevLabel || i === rows.length - 1) {
          const bandEndIdx = i === rows.length - 1 ? i : i - 1
          if (prevLabel && REGIME_LABEL_COLORS[prevLabel]) {
            stateBands.push([
              { xAxis: bandStartIdx, itemStyle: { color: REGIME_LABEL_COLORS[prevLabel], opacity: 0.08 } },
              { xAxis: bandEndIdx },
            ])
          }
          bandStartIdx = i
          prevLabel = currentLabel
        }
      }
    }

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
          if (!params || params.length === 0) return ''
          const idx = params[0].dataIndex
          const row = rows[idx]
          const label = row ? REGIME_STATE_LABELS[row.state] : ''
          let result = `<div style="font-weight: 600; margin-bottom: 8px;">${row?.date} ${row?.time}</div>`
          result += `<div style="margin-bottom: 4px;"><b>${label}</b> (${row?.score}分)</div>`
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
        data: ['综合分', '涨停数', '赚钱', '投机', '抗跌', '趋势'],
        textStyle: { color: ct.text, fontSize: 10 },
        top: 0,
        selected: { '综合分': true, '涨停数': true, '赚钱': false, '投机': false, '抗跌': false, '趋势': false },
      },
      grid: { left: 48, right: 64, top: 36, bottom: 56 },
      xAxis: {
        type: 'category',
        data: times,
        boundaryGap: false,
        axisLabel: { color: ct.text, fontSize: 10 },
        axisLine: { lineStyle: { color: ct.grid } },
      },
      yAxis: [
        { type: 'value', name: '涨停', position: 'left', axisLabel: { color: ct.text, fontSize: 10 }, splitLine: { show: false }, nameTextStyle: { color: ct.text } },
        { type: 'value', name: '综合分', min: 0, max: 100, position: 'right', axisLabel: { color: ct.text, fontSize: 10 }, splitLine: { lineStyle: { color: ct.grid } }, nameTextStyle: { color: ct.text } },
      ],
      dataZoom: [
        { type: 'inside', start: 0 },
        { type: 'slider', bottom: 8, height: 16, borderColor: ct.border, fillerColor: ct.zoomFill, textStyle: { color: ct.text } },
      ],
      series: [
        { name: '涨停数', type: 'bar', data: limitUps, yAxisIndex: 0, barMaxWidth: 6, itemStyle: { color: REGIME_STATE_COLORS['strong'], opacity: 0.35 }, z: 1 },
        { name: '赚钱', type: 'line', data: profitScores, smooth: true, symbol: 'none', yAxisIndex: 1, lineStyle: { ...subLineStyle, color: '#f59e0b' }, z: 2 },
        { name: '投机', type: 'line', data: speculationScores, smooth: true, symbol: 'none', yAxisIndex: 1, lineStyle: { ...subLineStyle, color: '#a855f7' }, z: 2 },
        { name: '抗跌', type: 'line', data: resilienceScores, smooth: true, symbol: 'none', yAxisIndex: 1, lineStyle: { ...subLineStyle, color: '#10b981' }, z: 2 },
        { name: '趋势', type: 'line', data: trendScores, smooth: true, symbol: 'none', yAxisIndex: 1, lineStyle: { ...subLineStyle, color: '#3b82f6' }, z: 2 },
        { name: '综合分', type: 'line', data: scores, smooth: true, symbol: 'circle', symbolSize: 4, yAxisIndex: 1,
          lineStyle: { width: 1.5, color: ct.textStrong }, areaStyle: { opacity: 0.06 }, z: 3,
          markArea: { silent: true, data: stateBands },
        },
      ],
    }
  }, [rows, ct])

  const trendEvents = useMemo(() => ({
    'globalout': () => {
      setHoverIndex(null)
    }
  }), [])

  const trendRef = useEChart(trendOption, [trendOption], trendEvents)

  // 雷达图: 最新四维
  const radarOption = useMemo<echarts.EChartsOption | null>(() => {
    if (!latest) return null
    const label = REGIME_STATE_LABELS[latest.state]
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
              <div style="font-weight: 600; margin-bottom: 6px;">${label} (${latest.time})</div>
              <div>赚钱: ${latest.profit_score}</div>
              <div>投机: ${latest.speculation_score}</div>
              <div>抗跌: ${latest.resilience_score}</div>
              <div>趋势: ${latest.trend_score}</div>
            </div>
          `
        },
      },
      radar: {
        startAngle: 90,
        indicator: [
          { name: '赚钱', max: 100 },
          { name: '投机', max: 100 },
          { name: '抗跌', max: 100 },
          { name: '趋势', max: 100 },
        ],
        axisName: { color: ct.text, fontSize: 11 },
        splitArea: { areaStyle: { color: ['rgba(255,255,255,0.02)', 'rgba(255,255,255,0.04)'] } },
        axisLine: { lineStyle: { color: ct.grid } },
        splitLine: { lineStyle: { color: ct.grid } },
      },
      series: [{
        type: 'radar',
        data: [{
          id: 'intraday-regime-radar',
          value: [
            latest.profit_score ?? 0,
            latest.speculation_score ?? 0,
            latest.resilience_score ?? 0,
            latest.trend_score ?? 0,
          ],
          name: label,
          areaStyle: { opacity: 0.3, color: REGIME_LABEL_COLORS[label] },
          lineStyle: { color: REGIME_LABEL_COLORS[label], width: 2 },
          itemStyle: { color: REGIME_LABEL_COLORS[label] },
        }],
      }],
    }
  }, [latest, ct])
  const radarRef = useEChart(radarOption, [radarOption], undefined, { notMerge: false })

  // 简单的帮助函数
  const regimeLabelToColor = (label: string): string => {
    return REGIME_LABEL_COLORS[label] || '#6b7280'
  }

  return (
    <div className="mx-auto max-w-[1440px] px-4 py-5 space-y-4">
      <div className={cn(cardCls, 'relative overflow-hidden rounded-card bg-gradient-to-r from-surface/90 to-surface/70 px-4 py-3')}>
        <div className="absolute left-0 top-0 h-full w-1 bg-gradient-to-b from-accent to-accent/20" />
        <div className="flex items-center gap-3 flex-wrap">
          <Activity className="h-5 w-5 text-accent" />
          <h1 className="text-base font-semibold text-foreground">实时环境</h1>
          <span className="text-xs text-muted">分钟级环境综合分 · 实时更新</span>
          
          {/* 日期选择器 */}
          <div className="flex items-center gap-2 ml-auto">
            <DatePicker
              value={selectedDate || ''}
              onChange={(date) => setSelectedDate(date || null)}
              placeholder="选择日期"
              enabledDates={dates.data?.dates}
            />
            
            {/* 如果选择了日期，添加清除按钮 */}
            {selectedDate && (
              <button
                onClick={() => setSelectedDate(null)}
                className="inline-flex items-center justify-center h-7 w-7 rounded-btn border border-border bg-base text-xs text-secondary hover:text-accent"
              >
                ×
              </button>
            )}
            
            <div className={cn(
              'flex items-center gap-1 px-2 py-1 rounded-full text-xs',
              status.data?.trading_time ? 'bg-green-100/20 text-green-500' : 'bg-gray-100/20 text-gray-500'
            )}>
              <Clock className="h-3 w-3" />
              {status.data?.trading_time ? '交易时段' : '非交易时段'}
            </div>
            <button onClick={handleRefresh} disabled={isRefreshing || !!selectedDate}
              className="inline-flex items-center gap-1.5 h-7 px-3 rounded-btn border border-border bg-base text-xs text-secondary hover:text-accent disabled:opacity-50"
            >
              {isRefreshing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
              {isRefreshing ? '更新中…' : '立即更新'}
            </button>
          </div>
        </div>
      </div>

      {latest ? (
        <div className="grid grid-cols-2 gap-3">
          <div className={cn(cardCls, 'p-3')}>
            <div className="flex items-center gap-1.5 text-[10px] text-muted">
              <Gauge className="h-3 w-3" />
              最新环境 · {latest.time}
            </div>
            <div className="mt-1.5 flex items-baseline gap-2">
              <span className="text-2xl font-bold" style={{ color: regimeLabelToColor(REGIME_STATE_LABELS[latest.state]) }}>
                {REGIME_STATE_LABELS[latest.state]}
              </span>
              <span className="text-sm text-muted">{latest.score} 分</span>
            </div>
            <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-base">
              <div className="h-full rounded-full transition-all"
                style={{ width: `${Math.max(2, Math.min(100, latest.score))}%`, backgroundColor: regimeLabelToColor(REGIME_STATE_LABELS[latest.state]) }} />
            </div>
          </div>

          <div className={cn(cardCls, 'p-3')}>
            <div className="flex items-center gap-1.5 text-[10px] text-muted">
              <Activity className="h-3 w-3" />
              四维拆解
            </div>
            <div className="mt-2 space-y-1">
              {[
                { label: '赚钱', val: latest.profit_score, color: '#f59e0b' },
                { label: '投机', val: latest.speculation_score, color: '#a855f7' },
                { label: '抗跌', val: latest.resilience_score, color: '#10b981' },
                { label: '趋势', val: latest.trend_score, color: '#3b82f6' },
              ].map(d => (
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
        </div>
      ) : (
        <div className="rounded-card border border-dashed border-border p-8 text-center text-sm text-muted">
          {history.isLoading ? '加载中…' : '暂无实时环境数据，请等待交易时段或点击「立即更新」'}
        </div>
      )}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className={cn(cardCls, 'p-3')}>
          <SectionTitle icon={Activity} title={hoverIndex != null ? "选中时刻" : "最新雷达图"} hint={latest ? latest.time : ''} />
          <div ref={radarRef} className="mt-2 h-[320px]" />
        </div>
        <div className={cn(cardCls, 'p-3 lg:col-span-2')}>
          <SectionTitle icon={Activity} title="环境综合分走势" hint="分钟级更新 · 点击图例切换维度" />
          <div ref={trendRef} className="mt-2 h-[320px]" />
        </div>
      </div>
    </div>
  )
}

