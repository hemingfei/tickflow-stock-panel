import { useEffect, useRef } from 'react'
import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'

export interface UseEChartOptions {
  /** setOption 是否整表替换 (默认 true) */
  notMerge?: boolean
  /** 多图联动分组 (tooltip/axisPointer 联动), 实例创建时 echarts.connect */
  group?: string
  /** 实例首次创建后回调 (调用方可缓存实例做派生操作) */
  onReady?: (inst: ECharts) => void
}

/**
 * ECharts 实例管理 hook (页面图表统一入口) — 自动 init / setOption / 事件 / resize / 销毁。
 *
 * - 容器 0×0 (首帧未排版 / tab display:none) 时不 init: 挂 ResizeObserver,
 *   等首次非零尺寸再建实例并应用当前 option, 消除 "Can't get DOM width or height"
 *   警告与空画布。
 * - deps 里放影响显隐/布局的值 (如 view/tab): 容器重新可见时按当前实际尺寸
 *   setOption + resize, 画布不会停留在隐藏期间的 0 尺寸。
 * - events 引用变化时先解绑旧事件再绑新事件; 实例延迟创建时绑定创建时刻的
 *   最新 events (经 ref 读取, 不受回调闭包过期影响)。
 */
export function useEChart(
  option: EChartsOption | null,
  deps: unknown[] = [],
  events?: Record<string, (params: any) => void>,
  opts?: UseEChartOptions,
) {
  const ref = useRef<HTMLDivElement>(null)
  const instRef = useRef<ECharts | null>(null)
  const roRef = useRef<ResizeObserver | null>(null)
  const boundEventsRef = useRef<Record<string, (params: any) => void> | undefined>(undefined)
  const desiredEventsRef = useRef(events)
  desiredEventsRef.current = events
  const optionRef = useRef(option)
  optionRef.current = option
  // opts 每次渲染都是新字面量, 用 ref 供 ensureInstance/RO 回调读取最新值
  const optsRef = useRef(opts)
  optsRef.current = opts

  const bindEvents = (inst: ECharts) => {
    if (boundEventsRef.current) {
      Object.keys(boundEventsRef.current).forEach(evt => inst.off(evt))
    }
    const desired = desiredEventsRef.current
    if (desired) {
      Object.entries(desired).forEach(([evt, handler]) => inst.on(evt, handler))
    }
    boundEventsRef.current = desired
  }

  const ensureInstance = (el: HTMLDivElement) => {
    if (!instRef.current) {
      const inst = echarts.init(el, undefined, { renderer: 'canvas' })
      instRef.current = inst
      const curOpts = optsRef.current
      if (curOpts?.group) {
        inst.group = curOpts.group
        echarts.connect(curOpts.group)
      }
      curOpts?.onReady?.(inst)
      bindEvents(inst)
    }
    return instRef.current
  }

  // 卸载: 清理监听与实例
  useEffect(() => {
    const onResize = () => instRef.current?.resize()
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      roRef.current?.disconnect()
      roRef.current = null
      instRef.current?.dispose()
      instRef.current = null
    }
  }, [])

  // option/deps 变化: 确保实例存在 (0×0 时延迟), 应用 option 并按容器实际尺寸 resize
  useEffect(() => {
    const el = ref.current
    if (!el) return
    if (el.clientWidth === 0 || el.clientHeight === 0) {
      if (!roRef.current) {
        roRef.current = new ResizeObserver(() => {
          if (el.clientWidth === 0 || el.clientHeight === 0) return
          roRef.current?.disconnect()
          roRef.current = null
          const inst = ensureInstance(el)
          if (optionRef.current) {
            inst.setOption(optionRef.current, {
              notMerge: optsRef.current?.notMerge ?? true,
              lazyUpdate: false,
            })
            inst.resize()
          }
        })
        roRef.current.observe(el)
      }
      return
    }
    const inst = ensureInstance(el)
    if (optionRef.current) {
      inst.setOption(optionRef.current, {
        notMerge: optsRef.current?.notMerge ?? true,
        lazyUpdate: false,
      })
      // 容器可能经历 display:none(tab 隐藏) → 可见的切换, 画布尺寸需要按当前
      // 容器实际尺寸重算; 调用方把显隐依赖传入 deps 以触发本 effect。
      inst.resize()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- deps 由调用方显式给出
  }, [option, ...deps])

  // 事件绑定: events 引用变化时重绑 (实例已存在时)
  useEffect(() => {
    const inst = instRef.current
    if (!inst || boundEventsRef.current === events) return
    bindEvents(inst)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- bindEvents 经 ref 读最新 events
  }, [events])

  return ref
}
