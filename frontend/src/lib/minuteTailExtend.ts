// 分钟序列尾部实时续画 (纯函数, 自 Watchlist 抽出以便单测)。
//
// 用 enriched 实时行情的 close 续写每只标的分钟序列的最后一根 / 分钟滚动追加
// 新K — 轮询间隔内分时图随实时价跳动, 零额外请求。纯视图叠加不写回缓存:
// 轮询拉回的服务端定版K始终是权威值, 覆盖续画值。
//
// 新鲜度守卫 (fail-closed): 仅当标的所属资产类型的行情日期 (enriched 响应的
// dates[asset_type]) 是今天时才续画。ETF 等未开实时拉取的资产类型, enriched
// close 是旧日K回退值 (与当日价可偏离数个百分点), 拼到当日分钟K尾部会把整条
// 分时压扁并在尾部画出错价长刺; dates 缺失 (旧后端) 同样一律不续画。
import type { MinuteKlineRow } from '@/lib/api'

export interface TailExtendLiveRow {
  symbol: string
  rt_price?: number | null
  close?: number | null
  asset_type?: string | null
}

/** 按资产类型的行情日期 (enriched 响应 dates 字段, ISO 日期); 旧后端不返回 */
export type TailExtendQuoteDates = {
  stock?: string | null
  etf?: string | null
  index?: string | null
}

/**
 * 连续竞价时段 (9:31-11:30, 13:01-15:00); 15:00 是收盘集合竞价定版K, 也要续写。
 * 收盘后 rt 价即收盘价无需续写。now 必须是「本地钟毫秒 +8h」的 instant ——
 * 其 UTC 分量即北京墙钟, 跨时区机器上读本地分量会错位 (见 extendMinuteTail)。
 */
function inContinuousSession(now: Date): boolean {
  const hh = now.getUTCHours(), mm = now.getUTCMinutes()
  return (hh === 9 && mm >= 31) || hh === 10 ||
    (hh === 11 && mm <= 30) || (hh === 13 && mm >= 1) || hh === 14 ||
    (hh === 15 && mm === 0)
}

const pad = (n: number) => String(n).padStart(2, '0')

/** now 约定传 new Date(Date.now() + 8 * 3_600_000) — UTC 分量即北京墙钟, 见 inContinuousSession */
export function extendMinuteTail(
  base: Record<string, MinuteKlineRow[]>,
  liveRows: TailExtendLiveRow[] | undefined,
  quoteDates: TailExtendQuoteDates | null | undefined,
  now: Date,
): Record<string, MinuteKlineRow[]> {
  if (!liveRows?.length || !inContinuousSession(now)) return base
  // fail-closed: 旧后端无 dates 字段时整个续画停用 (退化为纯轮询节奏, 不会画错)
  if (!quoteDates) return base

  // 与服务端同构的 naive 北京时间戳: 分钟K的 datetime 是北京墙钟, 用「本地钟 +8h
  // 的 instant」读 UTC 分量手工拼接 (不能用 toISOString — 那会再偏 8h; 也不能用
  // 本地分量 — 跨时区机器会拼出未来K或静默失效)。
  const todayStr = `${now.getUTCFullYear()}-${pad(now.getUTCMonth() + 1)}-${pad(now.getUTCDate())}`
  const barTs = `${todayStr}T${pad(now.getUTCHours())}:${pad(now.getUTCMinutes())}:00`

  const liveBySymbol = new Map(liveRows.map((r) => [r.symbol, r]))
  const patched: Record<string, MinuteKlineRow[]> = {}
  for (const sym of Object.keys(base)) {
    const arr = base[sym]
    if (!Array.isArray(arr) || arr.length === 0) { patched[sym] = arr; continue }
    const live = liveBySymbol.get(sym)
    const price = live?.rt_price ?? live?.close
    // 未知资产类型取到 undefined → 不等于当日 → 跳过 (fail-closed)
    const quoteDate = (quoteDates as Record<string, string | null | undefined>)[
      live?.asset_type ?? 'stock'
    ]
    // 行情日期非当日 (旧日K回退) 或价格非法 (含 0) 的标的不续画
    if (quoteDate !== todayStr) { patched[sym] = arr; continue }
    if (typeof price !== 'number' || !Number.isFinite(price) || price <= 0) {
      patched[sym] = arr; continue
    }
    const last = arr[arr.length - 1]
    if (last.datetime === barTs) {
      patched[sym] = [...arr.slice(0, -1), {
        ...last,
        close: price,
        high: Math.max(last.high, price),
        low: Math.min(last.low, price),
      }]
    } else if (barTs > last.datetime) {
      patched[sym] = [...arr, {
        datetime: barTs, open: price, high: price, low: price, close: price,
        volume: 0, amount: 0,   // 量/额由下一轮轮询定版覆盖
      }]
    } else {
      patched[sym] = arr   // 轮询数据已新于本地时钟 (钟差兜底), 不动
    }
  }
  return patched
}
