/**
 * 涨跌颜色管理 — 支持自定义涨/跌颜色
 *
 * 机制:
 *   - 状态存 localStorage，通过 storage.priceColors 管理
 *   - 生效方式: 直接设置 HTML 元素的 --bull / --bear CSS 变量
 *   - 与主题系统类似，支持实时切换和跨标签同步
 */
import { useEffect, useState } from 'react'
import { storage } from './storage'

const EVENT = 'tf-price-colors-change'

// 默认颜色 (HSL 格式，与 index.css 一致)
export const DEFAULT_BULL = '4 87% 60%'  // #F04438 红涨
export const DEFAULT_BEAR = '152 67% 45%' // #12B76A 绿跌

export interface PriceColors {
  bull: string
  bear: string
}

export function getPriceColors(): PriceColors {
  try {
    const saved = storage.priceColors.get({})
    return {
      bull: saved.bull ?? DEFAULT_BULL,
      bear: saved.bear ?? DEFAULT_BEAR,
    }
  } catch {
    return { bull: DEFAULT_BULL, bear: DEFAULT_BEAR }
  }
}

export function setPriceColors(colors: Partial<PriceColors>) {
  const current = getPriceColors()
  const next = {
    bull: colors.bull ?? current.bull,
    bear: colors.bear ?? current.bear,
  }
  storage.priceColors.set(next)
  applyPriceColors(next)
  window.dispatchEvent(new CustomEvent(EVENT, { detail: next }))
}

export function resetPriceColors() {
  storage.priceColors.set({})
  const defaults = { bull: DEFAULT_BULL, bear: DEFAULT_BEAR }
  applyPriceColors(defaults)
  window.dispatchEvent(new CustomEvent(EVENT, { detail: defaults }))
}

function applyPriceColors(colors: PriceColors) {
  document.documentElement.style.setProperty('--bull', colors.bull)
  document.documentElement.style.setProperty('--bear', colors.bear)
}

// 在页面加载时应用保存的颜色
export function initPriceColors() {
  applyPriceColors(getPriceColors())
}

/** hook: 订阅当前涨跌颜色 (本页修改 + 其他标签页修改均同步)。 */
export function usePriceColors(): PriceColors {
  const [colors, set] = useState<PriceColors>(getPriceColors)
  useEffect(() => {
    const onChange = () => set(getPriceColors())
    window.addEventListener(EVENT, onChange)
    window.addEventListener('storage', onChange) // 跨标签页同步
    return () => {
      window.removeEventListener(EVENT, onChange)
      window.removeEventListener('storage', onChange)
    }
  }, [])
  return colors
}

// === 颜色转换工具 ===

/** HSL 转 RGB */
export function hslToRgb(h: number, s: number, l: number): [number, number, number] {
  s /= 100
  l /= 100
  const k = (n: number) => (n + h / 30) % 12
  const a = s * Math.min(l, 1 - l)
  const f = (n: number) => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)))
  return [Math.round(f(0) * 255), Math.round(f(8) * 255), Math.round(f(4) * 255)]
}

/** RGB 转 HSL */
export function rgbToHsl(r: number, g: number, b: number): [number, number, number] {
  r /= 255
  g /= 255
  b /= 255
  const max = Math.max(r, g, b)
  const min = Math.min(r, g, b)
  let h = 0, s = 0, l = (max + min) / 2

  if (max !== min) {
    const d = max - min
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min)
    switch (max) {
      case r: h = (g - b) / d + (g < b ? 6 : 0); break
      case g: h = (b - r) / d + 2; break
      case b: h = (r - g) / d + 4; break
    }
    h *= 60
  }
  return [Math.round(h), Math.round(s * 100), Math.round(l * 100)]
}

/** 解析 HSL 字符串 (例如 "4 87% 60%") */
export function parseHsl(str: string): { h: number; s: number; l: number } | null {
  const match = str.match(/^(\d+)\s+(\d+)%?\s+(\d+)%?$/)
  if (!match) return null
  return {
    h: parseInt(match[1], 10),
    s: parseInt(match[2], 10),
    l: parseInt(match[3], 10),
  }
}

/** HSL 对象转字符串 */
export function stringifyHsl(h: number, s: number, l: number): string {
  return `${h} ${s}% ${l}%`
}

/** HSL 转 HEX */
export function hslToHex(h: number, s: number, l: number): string {
  const [r, g, b] = hslToRgb(h, s, l)
  return '#' + [r, g, b].map(x => x.toString(16).padStart(2, '0')).join('')
}

/** HEX 转 HSL */
export function hexToHsl(hex: string): { h: number; s: number; l: number } | null {
  const match = hex.replace('#', '').match(/^([0-9a-fA-F]{2})([0-9a-fA-F]{2})([0-9a-fA-F]{2})$/)
  if (!match) return null
  const r = parseInt(match[1], 16)
  const g = parseInt(match[2], 16)
  const b = parseInt(match[3], 16)
  const [h, s, l] = rgbToHsl(r, g, b)
  return { h, s, l }
}
