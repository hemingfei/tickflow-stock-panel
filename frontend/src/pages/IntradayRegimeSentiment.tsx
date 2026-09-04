/**
 * 实时环境情绪页 - 实时环境与实时情绪的合并页
 * 顶部状态栏的日期选择与刷新同时作用于两个板块;
 * 两个走势图通过 echarts.connect 十字光标联动, 悬停任一走势图时两侧数据卡片与雷达图同步更新
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import * as echarts from 'echarts'
import {
  Activity, RefreshCw, Loader2, Gauge, Clock
} from 'lucide-react'
import {
  api, type IntradayRegimeRow, type IntradaySentimentRow,
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

/** 情绪标签颜色映射 */
const EMOTION_COLORS: Record<string, string> = {
  '强势': '#ef4444',
  '偏暖': '#f59e0b',
  '震荡': '#6b7280',
  '偏冷': '#3b82f6',
  '冰点': '#10b981',
}

const regimeLabelToColor = (label: string): string => REGIME_LABEL_COLORS[label] || '#6b7280'
const emotionLabelToColor = (label: string): string => EMOTION_COLORS[label] || '#6b7280'

/** 两个走势图的联动分组 (echarts.connect) */
const TREND_CONNECT_GROUP = 'intraday-env-sent-trend'

/** ECharts hook (group 用于多图联动) */
function useEChart(
  option: echarts.EChartsOption | null,
  deps: unknown[],
  events?: Record<string, (params: any) => void>,
  opts?: { notMerge?: boolean; group?: string }
) {
  const ref = useRef<HTMLDivElement>(null)
  const instRef = useRef<echarts.ECharts | null>(null)
  const eventsRef = useRef<Record<string, (params: any) => void> | undefined>()

  useEffect(() => {
    if (!ref.current) return
    instRef.current = echarts.init(ref.current, undefined, { renderer: 'canvas' })
    if (opts?.group) {
      instRef.current.group = opts.group
      echarts.connect(opts.group)
    }
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

/** 取悬停时间标签对应的行, 未悬停或该侧缺失此标签时取最后一次采集的行 */
function pickAlignedRow<T>(map: Map<string, T>, hoverLabel: string | null): T | null {
  const hit = hoverLabel != null ? map.get(hoverLabel) : undefined
  if (hit) return hit
  const values = [...map.values()]
  return values.length > 0 ? values[values.length - 1] : null
}

export function IntradayRegimeSentiment() {
  const qc = useQueryClient()
  const [hoverLabel, setHoverLabel] = useState<string | null>(null)
  const [selectedDate, setSelectedDate] = useState<string | null>(null)
  const ct = useChartTheme()

  // 查询可用日期 (两个板块各自维护, 取交集保证所选日期两侧都有数据)
  const envDates = useQuery({
    queryKey: ['intradayRegimeDates'],
    queryFn: () => api.intradayRegimeDates(),
  })
  const sentDates = useQuery({
    queryKey: ['intradaySentimentDates'],
    queryFn: () => api.intradaySentimentDates(),
  })
  const enabledDates = useMemo(() => {
    const env = envDates.data?.dates
    const sent = sentDates.data?.dates
    if (env && sent) return env.filter(d => sent.includes(d))
    return env ?? sent
  }, [envDates.data, sentDates.data])

  // 查询状态
  const envStatus = useQuery({
    queryKey: ['intradayRegimeStatus'],
    queryFn: () => api.intradayRegimeStatus(),
    refetchInterval: 30000,
  })

  // 查询历史数据 (与环境/情绪单页共享缓存)
  const envHistory = useQuery({
    queryKey: ['intradayRegimeHistory', selectedDate],
    queryFn: () => api.intradayRegimeHistory(selectedDate ?? undefined),
    refetchInterval: selectedDate ? undefined : 60000,
  })
  const sentHistory = useQuery({
    queryKey: ['intradaySentimentHistory', selectedDate],
    queryFn: () => api.intradaySentimentHistory(selectedDate ?? undefined),
    refetchInterval: selectedDate ? undefined : 60000,
  })

  const envRows: IntradayRegimeRow[] = envHistory.data?.data ?? []
  const sentRows: IntradaySentimentRow[] = sentHistory.data?.data ?? []

  // 两个走势图的涨停纵轴共用同一最大值 (取两侧数据的最大涨停数, 向上取整到 10 的倍数),
  // 避免各自自适应导致两图刻度不一致; 无数据时不指定, 交给 ECharts 自适应
  const unifiedLimitUpMax = useMemo(() => {
    let max = 0
    for (const r of envRows) max = Math.max(max, r.limit_up)
    for (const r of sentRows) max = Math.max(max, r.limit_up)
    return max > 0 ? Math.ceil(max / 10) * 10 : undefined
  }, [envRows, sentRows])

  // 两条采集链路独立触发, 同一分钟可能各记多条或单侧缺失; 每侧按时间标签去重(保留最后
  // 一次采集), x 轴取并集, 两图共享同一套类目 —— 按索引联动时两侧时间即对齐
  const alignedTrend = useMemo(() => {
    const envMap = new Map<string, IntradayRegimeRow>()
    const sentMap = new Map<string, IntradaySentimentRow>()
    const labelTs = new Map<string, number>()
    const collect = <T extends { time: string; timestamp: number }>(rows: T[], map: Map<string, T>) => {
      for (const r of rows) {
        map.set(r.time, r)
        labelTs.set(r.time, Math.max(labelTs.get(r.time) ?? 0, r.timestamp))
      }
    }
    collect(envRows, envMap)
    collect(sentRows, sentMap)
    const labels = [...labelTs.entries()].sort((a, b) => a[1] - b[1]).map(([t]) => t)
    return { labels, envMap, sentMap }
  }, [envRows, sentRows])

  const envLatest = pickAlignedRow(alignedTrend.envMap, hoverLabel)
  const sentLatest = pickAlignedRow(alignedTrend.sentMap, hoverLabel)

  // 实时更新查询
  const [isRefreshing, setIsRefreshing] = useState(false)

  const handleRefresh = async () => {
    setIsRefreshing(true)
    try {
      await Promise.all([
        api.intradayRegimeCompute(true),
        api.intradaySentimentCompute(true),
      ])
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['intradayRegimeHistory'] }),
        qc.invalidateQueries({ queryKey: ['intradayRegimeStatus'] }),
        qc.invalidateQueries({ queryKey: ['intradaySentimentHistory'] }),
        qc.invalidateQueries({ queryKey: ['intradaySentimentStatus'] }),
      ])
      toast('已更新实时环境与情绪数据', 'success')
    } catch (e) {
      toast(`更新失败: ${String((e as Error)?.message || e)}`, 'error')
    } finally {
      setIsRefreshing(false)
    }
  }

  // ── 环境综合分走势 ──
  const envTrendOption = useMemo<echarts.EChartsOption | null>(() => {
    if (envRows.length === 0) return null
    const { labels: times, envMap } = alignedTrend
    const envAt = (i: number) => envMap.get(times[i])
    const scores = times.map((_, i) => envAt(i)?.score ?? null)
    const limitUps = times.map((_, i) => envAt(i)?.limit_up ?? null)
    const profitScores = times.map((_, i) => envAt(i)?.profit_score ?? null)
    const speculationScores = times.map((_, i) => envAt(i)?.speculation_score ?? null)
    const resilienceScores = times.map((_, i) => envAt(i)?.resilience_score ?? null)
    const trendScores = times.map((_, i) => envAt(i)?.trend_score ?? null)

    // 环境标签背景色带 (按对齐后的类目轴, 该侧缺失的标签处断开)
    const stateBands: any[] = []
    let bandStartIdx = -1
    let prevBandLabel: string | null = null
    const pushBand = (endIdx: number) => {
      if (prevBandLabel && REGIME_LABEL_COLORS[prevBandLabel] && bandStartIdx >= 0) {
        stateBands.push([
          { xAxis: bandStartIdx, itemStyle: { color: REGIME_LABEL_COLORS[prevBandLabel], opacity: 0.08 } },
          { xAxis: endIdx },
        ])
      }
    }
    times.forEach((t, i) => {
      const row = envMap.get(t)
      const bandLabel = row ? REGIME_STATE_LABELS[row.state] : null
      if (bandLabel && bandLabel === prevBandLabel) return
      pushBand(i - 1)
      bandStartIdx = row ? i : -1
      prevBandLabel = bandLabel
    })
    pushBand(times.length - 1)

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
            setHoverLabel(times[params[0].dataIndex] ?? null)
          }
          if (!params || params.length === 0) return ''
          const idx = params[0].dataIndex
          const row = envMap.get(times[idx])
          if (!row) return ''
          const label = REGIME_STATE_LABELS[row.state]
          let result = `<div style="font-weight: 600; margin-bottom: 8px;">${row.date} ${row.time}</div>`
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
        { type: 'value', name: '涨停', min: 0, max: unifiedLimitUpMax, position: 'left', axisLabel: { color: ct.text, fontSize: 10 }, splitLine: { show: false }, nameTextStyle: { color: ct.text } },
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
  }, [envRows, alignedTrend, ct, unifiedLimitUpMax])

  // ── 情绪分时走势 ──
  const sentTrendOption = useMemo<echarts.EChartsOption | null>(() => {
    if (sentRows.length === 0) return null
    const { labels: times, sentMap } = alignedTrend
    const sentAt = (i: number) => sentMap.get(times[i])
    const scores = times.map((_, i) => sentAt(i)?.emotion_score ?? null)
    const limitUps = times.map((_, i) => sentAt(i)?.limit_up ?? null)
    const indexScore = times.map((_, i) => sentAt(i)?.index_score ?? null)
    const profitScore = times.map((_, i) => sentAt(i)?.profit_score ?? null)
    const moneyScore = times.map((_, i) => sentAt(i)?.money_score ?? null)
    const speculationScore = times.map((_, i) => sentAt(i)?.speculation_score ?? null)
    const resilienceScore = times.map((_, i) => sentAt(i)?.resilience_score ?? null)
    const mainlineScore = times.map((_, i) => sentAt(i)?.mainline_score ?? null)

    // 情绪标签背景色带 (按对齐后的类目轴, 该侧缺失的标签处断开)
    const stateBands: any[] = []
    let bandStartIdx = -1
    let prevBandLabel: string | null = null
    const pushBand = (endIdx: number) => {
      if (prevBandLabel && emotionLabelToColor(prevBandLabel) && bandStartIdx >= 0) {
        stateBands.push([
          { xAxis: bandStartIdx, itemStyle: { color: emotionLabelToColor(prevBandLabel), opacity: 0.08 } },
          { xAxis: endIdx },
        ])
      }
    }
    times.forEach((t, i) => {
      const row = sentMap.get(t)
      const bandLabel = row?.emotion_label ?? null
      if (bandLabel && bandLabel === prevBandLabel) return
      pushBand(i - 1)
      bandStartIdx = row ? i : -1
      prevBandLabel = bandLabel
    })
    pushBand(times.length - 1)

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
            setHoverLabel(times[params[0].dataIndex] ?? null)
          }
          if (!params || params.length === 0) return ''
          const idx = params[0].dataIndex
          const row = sentMap.get(times[idx])
          if (!row) return ''
          let result = `<div style="font-weight: 600; margin-bottom: 8px;">${row.date} ${row.time}</div>`
          result += `<div style="margin-bottom: 4px;"><b>${row.emotion_label}</b> (${row.emotion_score}分)</div>`
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
        type: 'category', data: times, boundaryGap: false,
        axisLabel: { color: ct.text, fontSize: 10 },
        axisLine: { lineStyle: { color: ct.grid } },
      },
      yAxis: [
        { type: 'value', name: '涨停', min: 0, max: unifiedLimitUpMax, position: 'left', axisLabel: { color: ct.text, fontSize: 10 }, splitLine: { show: false }, nameTextStyle: { color: ct.text } },
        { type: 'value', name: '情绪分', min: 0, max: 100, position: 'right', axisLabel: { color: ct.text, fontSize: 10 }, splitLine: { lineStyle: { color: ct.grid } }, nameTextStyle: { color: ct.text } },
      ],
      dataZoom: [
        { type: 'inside', start: 0 },
        { type: 'slider', bottom: 8, height: 16, borderColor: ct.border, fillerColor: ct.zoomFill, textStyle: { color: ct.text } },
      ],
      series: [
        { name: '涨停数', type: 'bar', data: limitUps, yAxisIndex: 0, barMaxWidth: 6, itemStyle: { color: '#ef4444', opacity: 0.35 }, z: 1 },
        { name: '指数', type: 'line', data: indexScore, smooth: true, symbol: 'none', yAxisIndex: 1, lineStyle: { ...subLineStyle, color: '#3b82f6' }, z: 2 },
        { name: '赚钱', type: 'line', data: profitScore, smooth: true, symbol: 'none', yAxisIndex: 1, lineStyle: { ...subLineStyle, color: '#f59e0b' }, z: 2 },
        { name: '量能', type: 'line', data: moneyScore, smooth: true, symbol: 'none', yAxisIndex: 1, lineStyle: { ...subLineStyle, color: '#8b5cf6' }, z: 2 },
        { name: '投机', type: 'line', data: speculationScore, smooth: true, symbol: 'none', yAxisIndex: 1, lineStyle: { ...subLineStyle, color: '#a855f7' }, z: 2 },
        { name: '抗跌', type: 'line', data: resilienceScore, smooth: true, symbol: 'none', yAxisIndex: 1, lineStyle: { ...subLineStyle, color: '#10b981' }, z: 2 },
        { name: '主线', type: 'line', data: mainlineScore, smooth: true, symbol: 'none', yAxisIndex: 1, lineStyle: { ...subLineStyle, color: '#ec4899' }, z: 2 },
        { name: '情绪分', type: 'line', data: scores, smooth: true, symbol: 'circle', symbolSize: 4, yAxisIndex: 1,
          lineStyle: { width: 1.5, color: ct.textStrong }, areaStyle: { opacity: 0.06 }, z: 3,
          markArea: { silent: true, data: stateBands },
        },
      ]
    }
  }, [sentRows, alignedTrend, ct, unifiedLimitUpMax])

  const trendEvents = useMemo(() => ({
    'globalout': () => {
      setHoverLabel(null)
    }
  }), [])

  const envTrendRef = useEChart(envTrendOption, [envTrendOption], trendEvents, { group: TREND_CONNECT_GROUP })
  const sentTrendRef = useEChart(sentTrendOption, [sentTrendOption], trendEvents, { group: TREND_CONNECT_GROUP })

  // ── 雷达图: 环境最新四维 ──
  const envRadarOption = useMemo<echarts.EChartsOption | null>(() => {
    if (!envLatest) return null
    const label = REGIME_STATE_LABELS[envLatest.state]
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
          if (!envLatest) return ''
          return `
            <div style="padding: 4px 0;">
              <div style="font-weight: 600; margin-bottom: 6px;">${label} (${envLatest.time})</div>
              <div>赚钱: ${envLatest.profit_score}</div>
              <div>投机: ${envLatest.speculation_score}</div>
              <div>抗跌: ${envLatest.resilience_score}</div>
              <div>趋势: ${envLatest.trend_score}</div>
            </div>
          `
        },
      },
      radar: {
        startAngle: 90,
        radius: '65%',
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
            envLatest.profit_score ?? 0,
            envLatest.speculation_score ?? 0,
            envLatest.resilience_score ?? 0,
            envLatest.trend_score ?? 0,
          ],
          name: label,
          areaStyle: { opacity: 0.3, color: REGIME_LABEL_COLORS[label] },
          lineStyle: { color: REGIME_LABEL_COLORS[label], width: 2 },
          itemStyle: { color: REGIME_LABEL_COLORS[label] },
        }],
      }],
    }
  }, [envLatest, ct])
  const envRadarRef = useEChart(envRadarOption, [envRadarOption], undefined, { notMerge: false })

  // ── 雷达图: 情绪最新六维 ──
  const sentRadarOption = useMemo<echarts.EChartsOption | null>(() => {
    if (!sentLatest) return null
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
          if (!sentLatest) return ''
          return `
            <div style="padding: 4px 0;">
              <div style="font-weight: 600; margin-bottom: 6px;">${sentLatest.emotion_label} (${sentLatest.time})</div>
              <div style="display: grid; gap: 2px;">
                <div>指数: ${sentLatest.index_score}</div>
                <div>赚钱: ${sentLatest.profit_score}</div>
                <div>量能: ${sentLatest.money_score}</div>
                <div>投机: ${sentLatest.speculation_score}</div>
                <div>抗跌: ${sentLatest.resilience_score}</div>
                <div>主线: ${sentLatest.mainline_score}</div>
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
          id: 'intraday-emotion-radar',
          value: [
            sentLatest.index_score,
            sentLatest.mainline_score,
            sentLatest.resilience_score,
            sentLatest.speculation_score,
            sentLatest.money_score,
            sentLatest.profit_score,
          ],
          name: sentLatest.emotion_label,
          areaStyle: { opacity: 0.3, color: emotionLabelToColor(sentLatest.emotion_label) },
          lineStyle: { color: emotionLabelToColor(sentLatest.emotion_label), width: 2 },
          itemStyle: { color: emotionLabelToColor(sentLatest.emotion_label) },
        }],
      }],
    }
  }, [sentLatest, ct])
  const sentRadarRef = useEChart(sentRadarOption, [sentRadarOption], undefined, { notMerge: false })

  return (
    <div className="mx-auto max-w-[1440px] px-4 py-5 space-y-4">
      <div className={cn(cardCls, 'relative overflow-hidden rounded-card bg-gradient-to-r from-surface/90 to-surface/70 px-4 py-3')}>
        <div className="absolute left-0 top-0 h-full w-1 bg-gradient-to-b from-accent to-accent/20" />
        <div className="flex items-center gap-3 flex-wrap">
          <Activity className="h-5 w-5 text-accent" />
          <h1 className="text-base font-semibold text-foreground">实时环境情绪</h1>
          <span className="text-xs text-muted">分钟级环境 & 情绪分时 · 实时更新</span>

          {/* 日期选择器 */}
          <div className="flex items-center gap-2 ml-auto">
            <DatePicker
              value={selectedDate || ''}
              onChange={(date) => setSelectedDate(date || null)}
              placeholder="选择日期"
              enabledDates={enabledDates}
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
              envStatus.data?.trading_time ? 'bg-green-100/20 text-green-500' : 'bg-gray-100/20 text-gray-500'
            )}>
              <Clock className="h-3 w-3" />
              {envStatus.data?.trading_time ? '交易时段' : '非交易时段'}
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

      {/* 实时环境板块 */}
      <section className="space-y-3">
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-[2fr_3fr]">
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <div className={cn(cardCls, 'p-3')}>
              <SectionTitle icon={Activity} title={hoverLabel != null ? "选中时刻" : "最新环境雷达图"} hint={envLatest ? envLatest.time : ''} />
              <div ref={envRadarRef} className="mt-2 h-[300px]" />
            </div>

            <div className="flex flex-col gap-4">
              {envLatest ? (
                <>
                  <div className={cn(cardCls, 'flex-1 p-3')}>
                    <div className="flex items-center gap-1.5 text-[10px] text-muted">
                      <Gauge className="h-3 w-3" />
                      最新环境 · {envLatest.time}
                    </div>
                    <div className="mt-1.5 flex items-baseline gap-2">
                      <span className="text-2xl font-bold" style={{ color: regimeLabelToColor(REGIME_STATE_LABELS[envLatest.state]) }}>
                        {REGIME_STATE_LABELS[envLatest.state]}
                      </span>
                      <span className="text-sm text-muted">{envLatest.score} 分</span>
                    </div>
                    <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-base">
                      <div className="h-full rounded-full transition-all"
                        style={{ width: `${Math.max(2, Math.min(100, envLatest.score))}%`, backgroundColor: regimeLabelToColor(REGIME_STATE_LABELS[envLatest.state]) }} />
                    </div>
                  </div>

                  <div className={cn(cardCls, 'flex flex-[2] flex-col p-3')}>
                    <div className="flex items-center gap-1.5 text-[10px] text-muted">
                      <Activity className="h-3 w-3" />
                      四维拆解
                    </div>
                    <div className="mt-2 flex flex-1 flex-col justify-evenly gap-1">
                      {[
                        { label: '赚钱', val: envLatest.profit_score, color: '#f59e0b' },
                        { label: '投机', val: envLatest.speculation_score, color: '#a855f7' },
                        { label: '抗跌', val: envLatest.resilience_score, color: '#10b981' },
                        { label: '趋势', val: envLatest.trend_score, color: '#3b82f6' },
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
                </>
              ) : (
                <div className="flex flex-1 items-center justify-center rounded-card border border-dashed border-border p-8 text-center text-sm text-muted">
                  {envHistory.isLoading ? '加载中…' : '暂无实时环境数据，请等待交易时段或点击「立即更新」'}
                </div>
              )}
            </div>
          </div>

          <div className={cn(cardCls, 'p-3')}>
            <SectionTitle icon={Activity} title="环境综合分走势" hint="分钟级更新 · 点击图例切换维度" />
            <div ref={envTrendRef} className="mt-2 h-[300px]" />
          </div>
        </div>
      </section>

      {/* 实时情绪板块 */}
      <section className="space-y-3">
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-[2fr_3fr]">
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <div className={cn(cardCls, 'p-3')}>
              <SectionTitle icon={Activity} title={hoverLabel != null ? "选中时刻" : "最新情绪雷达图"} hint={sentLatest ? sentLatest.time : ''} />
              <div ref={sentRadarRef} className="mt-2 h-[300px]" />
            </div>

            <div className="flex flex-col gap-4">
              {sentLatest ? (
                <>
                  <div className={cn(cardCls, 'flex-1 p-3')}>
                    <div className="flex items-center gap-1.5 text-[10px] text-muted">
                      <Gauge className="h-3 w-3" />
                      最新情绪 · {sentLatest.time}
                    </div>
                    <div className="mt-1.5 flex items-baseline gap-2">
                      <span className="text-2xl font-bold" style={{ color: emotionLabelToColor(sentLatest.emotion_label) }}>
                        {sentLatest.emotion_label}
                      </span>
                      <span className="text-sm text-muted">{sentLatest.emotion_score} 分</span>
                    </div>
                    <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-base">
                      <div className="h-full rounded-full transition-all"
                        style={{ width: `${Math.max(2, Math.min(100, sentLatest.emotion_score))}%`, backgroundColor: emotionLabelToColor(sentLatest.emotion_label) }} />
                    </div>
                  </div>

                  <div className={cn(cardCls, 'flex flex-[2] flex-col p-3')}>
                    <div className="flex items-center gap-1.5 text-[10px] text-muted">
                      <Activity className="h-3 w-3" />
                      六维拆解
                    </div>
                    <div className="mt-2 flex flex-1 flex-col justify-evenly gap-1">
                      {[
                        { label: '指数', val: sentLatest.index_score, color: '#3b82f6' },
                        { label: '赚钱', val: sentLatest.profit_score, color: '#f59e0b' },
                        { label: '量能', val: sentLatest.money_score, color: '#8b5cf6' },
                        { label: '投机', val: sentLatest.speculation_score, color: '#a855f7' },
                        { label: '抗跌', val: sentLatest.resilience_score, color: '#10b981' },
                        { label: '主线', val: sentLatest.mainline_score, color: '#ec4899' },
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
                </>
              ) : (
                <div className="flex flex-1 items-center justify-center rounded-card border border-dashed border-border p-8 text-center text-sm text-muted">
                  {sentHistory.isLoading ? '加载中…' : '暂无实时情绪数据，请等待交易时段或点击「立即更新」'}
                </div>
              )}
            </div>
          </div>

          <div className={cn(cardCls, 'p-3')}>
            <SectionTitle icon={Activity} title="情绪分时走势" hint="分钟级更新 · 点击图例切换维度" />
            <div ref={sentTrendRef} className="mt-2 h-[300px]" />
          </div>
        </div>
      </section>
    </div>
  )
}
