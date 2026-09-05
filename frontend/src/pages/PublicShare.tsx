import { Suspense, lazy } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Activity, Gauge, Loader2 } from 'lucide-react'
import { cn } from '@/lib/cn'

// 两个公开内容页按需懒加载, 保持各自 chunk 独立 (与 /replay、/sentiment 单页共用组件)
const BoardReplay = lazy(() => import('./BoardReplay').then(m => ({ default: m.BoardReplay })))
const IntradayRegimeSentiment = lazy(() => import('./IntradayRegimeSentiment').then(m => ({ default: m.IntradayRegimeSentiment })))

const TABS = [
  { id: 'replay', label: '看板回放', icon: Gauge },
  { id: 'sentiment', label: '实时环境情绪', icon: Activity },
] as const

type TabId = (typeof TABS)[number]['id']

function isTabId(v: string | null): v is TabId {
  return TABS.some(t => t.id === v)
}

/**
 * 免登录公开分享页 (/share): 看板回放 + 实时环境情绪 二合一, 顶部标签切换。
 * tab 同步到 URL (?tab=replay|sentiment) 便于按页直达; 两个子页保留各自工具栏、
 * 轮询与 URL 状态 (回放的 date/time 与 tab 参数互不覆写), 切换时非活动页卸载停轮询。
 */
export function PublicShare() {
  const [searchParams, setSearchParams] = useSearchParams()
  const rawTab = searchParams.get('tab')
  const tab: TabId = isTabId(rawTab) ? rawTab : 'replay'

  const switchTab = (next: TabId) => {
    if (next === tab) return
    const sp = new URLSearchParams(searchParams)
    sp.set('tab', next)
    setSearchParams(sp, { replace: true })
  }

  return (
    <div className="min-h-screen bg-base">
      {/* 顶部标签栏: sticky 置顶, 长页滚动时也可随时切换 */}
      <div className="sticky top-0 z-20 flex items-center justify-center gap-1.5 border-b border-border bg-surface/80 px-3 py-1.5 shadow-[0_1px_3px_hsl(var(--border)/0.4)] backdrop-blur-md">
        {TABS.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            onClick={() => switchTab(id)}
            className={cn(
              'inline-flex items-center gap-1.5 rounded-full border px-3.5 py-1 text-xs font-medium transition-colors',
              tab === id
                ? 'border-accent/40 bg-accent/15 text-accent'
                : 'border-transparent text-muted hover:bg-elevated hover:text-foreground',
            )}
          >
            <Icon className="h-3.5 w-3.5" />
            {label}
          </button>
        ))}
      </div>

      <Suspense
        fallback={
          <div className="flex h-40 items-center justify-center gap-2 text-sm text-muted">
            <Loader2 className="h-4 w-4 animate-spin" /> 加载中…
          </div>
        }
      >
        {tab === 'replay' ? (
          <BoardReplay variant="public" />
        ) : (
          <IntradayRegimeSentiment variant="public" />
        )}
      </Suspense>
    </div>
  )
}
