import { ChevronLeft, ChevronRight } from 'lucide-react'

/** 今日 (本地时区) 的 ISO 串, 空值页面的「实时/今天」语义以此为基准 */
function todayISO(): string {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

/** 距 value 最近的上一个/下一个有数据日期 (ISO 日期可直接字符串比较); 无可步进目标返回 null */
function adjacentDate(value: string, dates: string[], dir: -1 | 1): string | null {
  const sorted = [...new Set(dates)].sort()
  const base = value || todayISO()
  if (dir === -1) {
    for (let i = sorted.length - 1; i >= 0; i--) {
      if (sorted[i] < base) return sorted[i]
    }
    return null
  }
  for (const d of sorted) {
    if (d > base) return d
  }
  return null
}

const btnCls = 'inline-flex h-6 w-6 shrink-0 items-center justify-center rounded border border-border bg-elevated text-muted transition-colors hover:text-foreground disabled:opacity-40 disabled:hover:text-muted'

/**
 * 日期快退/快进按钮对: 在 enabledDates (有数据的日期) 间步进, 供历史回溯时
 * 不翻日历直接切换上/下一个日期。value 传 '' 表示「实时/今天」, 此时只能快退。
 */
export function DateStepper({ value, dates, onChange }: {
  value: string          // 当前选中日期 YYYY-MM-DD, '' = 实时/今天
  dates: string[]        // 可选 (有数据) 日期, 顺序任意
  onChange: (date: string) => void
}) {
  const prev = adjacentDate(value, dates, -1)
  const next = adjacentDate(value, dates, 1)
  return (
    <div className="flex shrink-0 items-center gap-0.5">
      <button
        type="button"
        onClick={() => { if (prev) onChange(prev) }}
        disabled={!prev}
        className={btnCls}
        title="上一个有数据的日期"
      >
        <ChevronLeft className="h-3.5 w-3.5" />
      </button>
      <button
        type="button"
        onClick={() => { if (next) onChange(next) }}
        disabled={!next}
        className={btnCls}
        title="下一个有数据的日期"
      >
        <ChevronRight className="h-3.5 w-3.5" />
      </button>
    </div>
  )
}
