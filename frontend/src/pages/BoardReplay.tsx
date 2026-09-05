import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ChevronLeft, ChevronRight, Gauge, History, Loader2, TimerOff } from 'lucide-react'
import { DatePicker } from '@/components/DatePicker'
import { EmptyState } from '@/components/EmptyState'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { BoardContent, scoreColor } from '@/components/BoardContent'

/**
 * 看板回溯 — 按日期 + 时间滑条回放历史看板快照 (只读, 无下钻交互)。
 *
 * 数据源: 开盘时段每 5 分钟落盘的看板结构化快照 (backend board_snapshot_store),
 * 滑条拖到任意位置即取「不晚于该时刻的最近节点」; URL 带 ?date=&time= 可直达。
 * 渲染复用 components/BoardContent.tsx, 与实时看板同一来源保证内容一致。
 *
 * 两个入口共用本组件:
 *   - variant="internal" (默认): 站内 /board-replay, 需登录, 走 /api/board-snapshots/*;
 *   - variant="public": 免登录独立页 /replay, 走认证白名单 /api/public/replay/*,
 *     公开响应已由后端剥离监控中心告警, 页面标题为「看板回放」。
 */

// 滑条范围: A 股连续竞价 09:30-15:00 (北京时间), 午休段拖动时由 floor 语义落到 11:30 节点
const DAY_START_MIN = 9 * 60 + 30
const DAY_END_MIN = 15 * 60

function hhmmToMinutes(t: string): number | null {
  const m = /^(\d{1,2}):(\d{2})$/.exec(t.trim())
  if (!m) return null
  const min = Number(m[1]) * 60 + Number(m[2])
  return Number.isFinite(min) ? min : null
}

function minutesToHHMM(min: number): string {
  const h = Math.floor(min / 60)
  const m = min % 60
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`
}

function clampMin(v: number) {
  return Math.max(DAY_START_MIN, Math.min(DAY_END_MIN, v))
}

/** 不晚于 slider 时刻的最近快照节点; 早于首节点时取首节点 (拖到开盘前仍有内容) */
function nodeAtOrBefore(times: string[], slider: number): string | null {
  let best: string | null = null
  for (const t of times) {
    const m = hhmmToMinutes(t)
    if (m == null) continue
    if (m <= slider) best = t
    else break
  }
  return best ?? times[0] ?? null
}

export function BoardReplay({ variant = 'internal' }: { variant?: 'internal' | 'public' }) {
  const isPublic = variant === 'public'
  const [searchParams, setSearchParams] = useSearchParams()
  // URL 参数直达: /board-replay?date=2026-09-04&time=10:23 → 最近节点
  const [date, setDate] = useState(() => searchParams.get('date') ?? '')
  const [sliderMin, setSliderMin] = useState<number | null>(null)

  const datesQ = useQuery({
    queryKey: isPublic ? QK.publicReplayDates : QK.boardSnapshotDates,
    queryFn: isPublic ? api.publicReplayDates : api.boardSnapshotDates,
    staleTime: 60_000,
  })
  const dates = useMemo(() => datesQ.data?.dates ?? [], [datesQ.data])

  // 未带 date 参数直达时落到最后一个有快照的日期 (最新)
  useEffect(() => {
    if (!date && dates.length > 0) setDate(dates[dates.length - 1])
  }, [date, dates])

  // 快照不可变: 60s 轮询时刻列表, 但仅限最新快照日期 (当天节点盘中逐步出现);
  // 历史日期节点已定格, 不轮询。
  const isLatestDate = date !== '' && date === dates[dates.length - 1]
  const timesQ = useQuery({
    queryKey: (isPublic ? QK.publicReplayTimes : QK.boardSnapshotTimes)(date),
    queryFn: () => (isPublic ? api.publicReplayTimes : api.boardSnapshotTimes)(date),
    enabled: !!date,
    staleTime: 60_000,
    refetchInterval: isLatestDate ? 60_000 : false,
  })
  const times = useMemo(() => timesQ.data?.times ?? [], [timesQ.data])

  // 滑条位置: 未拖动过时取 URL time (直达), 否则当日最后节点
  const urlTimeMin = hhmmToMinutes(searchParams.get('time') ?? '')
  const sliderValue = clampMin(sliderMin ?? urlTimeMin ?? DAY_END_MIN)
  const nodeTime = times.length > 0 ? nodeAtOrBefore(times, sliderValue) : null

  const snapQ = useQuery({
    queryKey: (isPublic ? QK.publicReplayLoad : QK.boardSnapshotLoad)(date || undefined, nodeTime ?? undefined),
    queryFn: () => (isPublic ? api.publicReplayLoad : api.boardSnapshotLoad)(date, nodeTime!),
    enabled: !!date && !!nodeTime,
    staleTime: 30 * 60_000,
    placeholderData: (prev) => prev,  // 拖动切节点时保留上一份画面, 避免整板闪烁
  })
  const snap = snapQ.data?.snapshot

  // 状态同步回 URL (replace 不产生历史记录), 保证任意时刻可复制链接直达;
  // 在既有参数上改写 date/time, 保留外层页面的参数 (如合并页 /share 的 tab)
  useEffect(() => {
    if (!date || times.length === 0) return
    const next = new URLSearchParams(searchParams)
    next.set('date', date)
    next.set('time', minutesToHHMM(sliderValue))
    if (next.toString() !== searchParams.toString()) {
      setSearchParams(next, { replace: true })
    }
  }, [date, sliderValue, times, searchParams, setSearchParams])

  const hasDepth = !!snap?.capabilities?.capabilities?.['depth5.batch']
  const sealedReady = !!snap?.overview?.limit?.sealed_ready
  const score = snap?.overview?.emotion?.score ?? 50
  const emotionLabel = snap?.overview?.emotion?.label

  const stepNode = (dir: 1 | -1) => {
    if (!nodeTime) return
    const idx = times.indexOf(nodeTime)
    if (idx === -1) return
    const nextIdx = Math.max(0, Math.min(times.length - 1, idx + dir))
    setSliderMin(hhmmToMinutes(times[nextIdx]))
  }

  if (datesQ.isLoading) {
    return (
      <div className="flex h-full items-center justify-center bg-base">
        <div className="flex items-center gap-2 text-sm text-muted">
          <Loader2 className="h-4 w-4 animate-spin" /> 加载快照索引…
        </div>
      </div>
    )
  }

  if (dates.length === 0) {
    return (
      <div className="min-h-full bg-base">
        <EmptyState
          icon={History}
          title="暂无看板快照"
          hint="看板快照在开盘时段 (9:30-11:30 / 13:00-15:00, 北京时间) 每 5 分钟自动保存一次, 服务运行一个交易时段后即可在此回溯。"
        />
      </div>
    )
  }

  return (
    <div className={isPublic ? 'min-h-screen bg-base p-1.5' : 'min-h-full bg-base p-1.5'}>
      {/* 回溯工具条: 日期选择 + 时间滑条 (拖到任意时刻取最近节点) */}
      <div className="relative mb-1.5 flex flex-wrap items-center justify-between gap-x-4 gap-y-2 overflow-hidden rounded-card border border-border bg-gradient-to-r from-surface/90 to-surface/70 px-3 py-1.5 shadow-[0_1px_3px_hsl(var(--border)/0.4)] backdrop-blur-sm">
        <div className="pointer-events-none absolute left-0 top-0 h-full w-1 bg-gradient-to-b from-accent to-accent/20" aria-hidden />
        <div className="flex items-center gap-2">
          <Gauge className="h-4 w-4 text-accent" />
          <h1 className="text-base font-semibold text-foreground">{isPublic ? '看板回放' : '看板回溯'}</h1>
          {snap && emotionLabel && (
            <span
              className="rounded-full border px-2 py-0.5 text-[10px] font-medium"
              style={{
                color: scoreColor(score),
                borderColor: `${scoreColor(score)}40`,
                background: `${scoreColor(score)}14`,
              }}
            >
              {emotionLabel} · {score}
            </span>
          )}
          {snap && (
            <span className="flex items-center gap-1 font-mono text-[10px] text-muted">
              <TimerOff className="h-3 w-3" />
              {snap.data_time?.text ?? '—'}
            </span>
          )}
        </div>

        <div className="flex min-w-0 flex-1 items-center justify-end gap-3">
          <DatePicker value={date} onChange={setDate} enabledDates={dates} className="w-32" />
          <div className="flex min-w-[260px] max-w-xl flex-1 items-center gap-1.5">
            <button
              onClick={() => stepNode(-1)}
              disabled={!nodeTime || times.indexOf(nodeTime) <= 0}
              className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded border border-border bg-elevated text-muted transition-colors hover:text-foreground disabled:opacity-40"
              title="上一个快照节点"
            >
              <ChevronLeft className="h-3.5 w-3.5" />
            </button>
            <div className="min-w-0 flex-1">
              <input
                type="range"
                min={DAY_START_MIN}
                max={DAY_END_MIN}
                step={5}
                value={sliderValue}
                onChange={(e) => setSliderMin(Number(e.target.value))}
                disabled={times.length === 0}
                className="w-full accent-accent"
                aria-label="快照时间滑条"
              />
              {/* 节点刻度: 每个已落盘快照一个刻度线 */}
              <div className="relative mt-0.5 h-1.5">
                {times.map(t => {
                  const m = hhmmToMinutes(t)
                  if (m == null) return null
                  return (
                    <span
                      key={t}
                      className={`absolute top-0 h-full w-px ${t === nodeTime ? 'bg-accent' : 'bg-accent/30'}`}
                      style={{ left: `${(m - DAY_START_MIN) / (DAY_END_MIN - DAY_START_MIN) * 100}%` }}
                    />
                  )
                })}
              </div>
              <div className="flex justify-between font-mono text-[9px] text-muted">
                <span>09:30</span>
                <span>11:30/13:00</span>
                <span>15:00</span>
              </div>
            </div>
            <button
              onClick={() => stepNode(1)}
              disabled={!nodeTime || times.indexOf(nodeTime) >= times.length - 1}
              className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded border border-border bg-elevated text-muted transition-colors hover:text-foreground disabled:opacity-40"
              title="下一个快照节点"
            >
              <ChevronRight className="h-3.5 w-3.5" />
            </button>
            <div className="shrink-0 text-right">
              <div className="font-mono text-[11px] text-foreground">{minutesToHHMM(sliderValue)}</div>
              <div className="font-mono text-[9px] text-accent">节点 {nodeTime ?? '—'}</div>
            </div>
          </div>
        </div>
      </div>

      {date && times.length === 0 && !timesQ.isLoading ? (
        <EmptyState
          icon={History}
          title={`${date} 暂无看板快照`}
          hint="该日期没有已落盘的快照 (可能未在开盘时段运行服务), 请选择其他日期。"
        />
      ) : snapQ.isError && !snap ? (
        <div className="flex h-full items-center justify-center bg-base p-6">
          <div className="rounded-card border border-border bg-surface p-6 text-center">
            <div className="text-sm text-danger">快照加载失败</div>
            <button onClick={() => snapQ.refetch()} className="mt-3 rounded-btn bg-accent px-3 py-1.5 text-xs font-medium text-base">重试</button>
          </div>
        </div>
      ) : !snap ? (
        <div className="flex h-64 items-center justify-center">
          <div className="flex items-center gap-2 text-sm text-muted">
            <Loader2 className="h-4 w-4 animate-spin" /> 加载快照画面…
          </div>
        </div>
      ) : !snap.overview ? (
        <EmptyState icon={History} title="快照数据不完整" hint="该快照缺少看板主体数据, 可能由旧版本生成。" />
      ) : (
        /* 只读回放: 屏蔽一切点击下钻, 内容与当时看板一致;
           公开回放隐藏监控中心 (告警已剥离, 空板块会被误读为「当时无告警」) */
        <div className="pointer-events-none select-none" title="回溯快照为只读展示">
          <BoardContent
            data={snap.overview}
            alerts={snap.alerts?.alerts ?? []}
            hasDepth={hasDepth}
            sealedReady={sealedReady}
            showMonitor={!isPublic}
          />
        </div>
      )}
    </div>
  )
}

/** 免登录独立回放页 (/replay): 无应用外壳, 与站内看板回溯共用同一组件 */
export function PublicBoardReplay() {
  return <BoardReplay variant="public" />
}
