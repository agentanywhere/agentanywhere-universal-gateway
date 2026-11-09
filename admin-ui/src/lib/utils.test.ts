import { describe, expect, it } from 'vitest'
import { cn, formatMs, formatNumber, formatPercent } from './utils'

describe('cn', () => {
  it('joins truthy class names and drops falsy ones', () => {
    expect(cn('a', false, undefined, null, 'b')).toBe('a b')
  })
  it('lets later tailwind classes override earlier conflicting ones', () => {
    expect(cn('px-2 py-1', 'px-4')).toBe('py-1 px-4')
  })
  it('supports conditional object syntax', () => {
    expect(cn({ on: true, off: false })).toBe('on')
  })
})

describe('formatNumber', () => {
  it('formats with thousands separators', () => {
    expect(formatNumber(12345, 0)).toBe((12345).toLocaleString())
  })
  it('limits fraction digits', () => {
    expect(formatNumber(1.23456, 2)).toBe((1.23).toLocaleString(undefined, { maximumFractionDigits: 2 }))
  })
  it.each([null, undefined, NaN, Infinity])('renders %s as an em dash', (v) => {
    expect(formatNumber(v as number)).toBe('—')
  })
  it('renders zero as 0', () => {
    expect(formatNumber(0)).toBe('0')
  })
})

describe('formatMs', () => {
  it('shows milliseconds below 1s', () => {
    expect(formatMs(250)).toBe('250 ms')
    expect(formatMs(12.345, 2)).toBe('12.35 ms')
  })
  it('switches to seconds from 1000 ms', () => {
    expect(formatMs(1000)).toBe('1.0 s')
    expect(formatMs(2500, 2)).toBe('2.50 s')
  })
  it('handles negative values by magnitude', () => {
    expect(formatMs(-1500)).toBe('-1.5 s')
  })
  it('renders missing values as an em dash', () => {
    expect(formatMs(null)).toBe('—')
    expect(formatMs(undefined)).toBe('—')
  })
})

describe('formatPercent', () => {
  it('converts a fraction to a percentage', () => {
    expect(formatPercent(0.05)).toBe('5.0%')
    expect(formatPercent(0.0512, 2)).toBe('5.12%')
  })
  it('renders missing values as an em dash', () => {
    expect(formatPercent(undefined)).toBe('—')
    expect(formatPercent(NaN)).toBe('—')
  })
})
