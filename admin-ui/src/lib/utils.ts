import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

// This file was never committed (the root .gitignore ignores every lib/
// directory), so the console did not build from a clean clone.

/** Join class names, letting later Tailwind classes override earlier ones. */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs))
}

function isNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

/** 12345 -> "12,345"; missing values render as an em dash. */
export function formatNumber(value: number | null | undefined, digits = 0): string {
  if (!isNumber(value)) return '—'
  return value.toLocaleString(undefined, {
    minimumFractionDigits: 0,
    maximumFractionDigits: digits,
  })
}

/** Milliseconds, switching to seconds from 1000 ms. */
export function formatMs(value: number | null | undefined, digits = 0): string {
  if (!isNumber(value)) return '—'
  if (Math.abs(value) >= 1000) return `${(value / 1000).toFixed(Math.max(digits, 1))} s`
  return `${value.toFixed(digits)} ms`
}

/** A fraction (0.05) as a percentage ("5.00%"). */
export function formatPercent(value: number | null | undefined, digits = 1): string {
  if (!isNumber(value)) return '—'
  return `${(value * 100).toFixed(digits)}%`
}
