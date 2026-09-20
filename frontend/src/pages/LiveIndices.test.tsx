// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { LiveIndices } from './LiveIndices'

vi.mock('@/lib/api', () => ({
  api: {
    indexQuotes: async () => ({ rows: [], count: 0, source: 'index_daily' }),
    klineMinuteBatch: async () => ({ data: {}, awaiting_open: false }),
    indexDaily: async (symbol: string) => ({
      symbol,
      rows: ['2026-09-17', '2026-09-18', '2026-09-19'].map(date => ({
        date, open: 10, high: 12, low: 9, close: 11, volume: 1,
      })),
    }),
  },
}))
vi.mock('@/lib/useSharedQueries', () => ({
  useCapabilities: () => ({ data: { capabilities: { 'kline.minute.batch': true } } }),
  useQuoteInterval: () => ({ data: { interval: 6 } }),
}))
vi.mock('@/components/EChartsIntraday', () => ({
  EChartsIntraday: () => <div data-intraday />,
}))
vi.mock('@/components/EChartsCandlestick', () => ({
  EChartsCandlestick: () => <div data-candle />,
}))

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  localStorage.clear()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: {
    retry: false, staleTime: Infinity, gcTime: Infinity,
  } } })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

async function settle() {
  for (let i = 0; i < 5; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  }
}

async function renderPage() {
  await act(async () => root.render(
    <QueryClientProvider client={client}><LiveIndices /></QueryClientProvider>,
  ))
  await settle()
}

// 布局按钮以 title 标注当前列数 (点击切换布局 (当前: 1行N个))
function layoutButton(): HTMLButtonElement {
  const btn = host.querySelector<HTMLButtonElement>('button[title*="1行"]')
  expect(btn).not.toBeNull()
  return btn!
}

function periodButton(label: string): HTMLButtonElement {
  const btn = [...host.querySelectorAll('button')].find(b => b.textContent === label)
  expect(btn).not.toBeNull()
  return btn!
}

it('restores persisted layout and period on mount', async () => {
  localStorage.setItem('live-indices-grid-cols', '2')
  localStorage.setItem('live-indices-period', 'D')
  await renderPage()
  expect(layoutButton().title).toContain('1行2个')
  expect(periodButton('日K').className).toContain('bg-accent')
  // 日K 走 EChartsCandlestick, 四张指数卡都渲染蜡烛图
  expect(host.querySelectorAll('[data-candle]').length).toBe(4)
})

it('falls back to defaults for missing or invalid stored values', async () => {
  await renderPage()
  expect(layoutButton().title).toContain('1行3个')
  expect(periodButton('分时').className).toContain('bg-accent')
  expect(host.querySelectorAll('[data-candle]').length).toBe(0)

  // 历史残留/手改的非法值同样回退默认
  await act(async () => root.render(null))
  localStorage.setItem('live-indices-grid-cols', '9')
  localStorage.setItem('live-indices-period', 'bogus')
  await renderPage()
  expect(layoutButton().title).toContain('1行3个')
  expect(periodButton('分时').className).toContain('bg-accent')
})

it('persists period and layout changes to localStorage', async () => {
  await renderPage()
  await act(async () => periodButton('日K').click())
  await settle()
  expect(localStorage.getItem('live-indices-period')).toBe('D')
  expect(periodButton('日K').className).toContain('bg-accent')
  expect(host.querySelectorAll('[data-candle]').length).toBe(4)

  await act(async () => layoutButton().click()) // 3 -> 4
  await settle()
  expect(localStorage.getItem('live-indices-grid-cols')).toBe('4')
  expect(layoutButton().title).toContain('1行4个')
})
