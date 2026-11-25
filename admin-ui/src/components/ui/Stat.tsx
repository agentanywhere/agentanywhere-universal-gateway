import * as React from 'react'
import { ArrowDownRight, ArrowUpRight, Minus } from 'lucide-react'
import { cn } from '../../lib/utils'

type Tone = 'neutral' | 'primary' | 'success' | 'warning' | 'danger' | 'info'

const toneStyles: Record<Tone, string> = {
  neutral: 'text-muted-foreground',
  primary: 'text-primary',
  success: 'text-emerald-600 dark:text-emerald-400',
  warning: 'text-amber-600 dark:text-amber-400',
  danger: 'text-red-600 dark:text-red-400',
  info: 'text-sky-700 dark:text-brand-cyan',
}

export interface StatProps {
  label: string
  value: React.ReactNode
  hint?: string
  icon?: React.ReactNode
  tone?: Tone
  trend?: {
    delta: number
    label?: string
    inverse?: boolean
  }
  isLoading?: boolean
  className?: string
}

export function Stat({
  label,
  value,
  hint,
  icon,
  tone = 'neutral',
  trend,
  isLoading,
  className,
}: StatProps) {
  const trendUp = trend ? trend.delta > 0 : false
  const trendFlat = trend ? trend.delta === 0 : false
  const positive = trend ? (trend.inverse ? !trendUp : trendUp) : false

  return (
    <div
      className={cn(
        'group relative overflow-hidden rounded-xl border border-border/80 bg-card/80 p-4 sm:p-5',
        'transition-[border-color,background-color] duration-200 hover:border-primary/25 hover:bg-card',
        className
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <p className="text-[12px] font-medium uppercase tracking-[0.08em] text-muted-foreground">
          {label}
        </p>
        {icon && (
          <div className={cn('shrink-0 opacity-70', toneStyles[tone])}>{icon}</div>
        )}
      </div>

      <div className="mt-3 flex items-end justify-between gap-3">
        <div className="min-w-0 space-y-1">
          {isLoading ? (
            <div className="h-8 w-24 animate-pulse rounded-md bg-muted" />
          ) : (
            <p className="font-display text-[1.75rem] font-semibold leading-none tracking-tight text-foreground tabular-nums">
              {value}
            </p>
          )}
          {hint && !isLoading && (
            <p className="text-xs text-muted-foreground/80">{hint}</p>
          )}
        </div>

        {trend && !trendFlat && (
          <span
            className={cn(
              'mb-0.5 inline-flex items-center gap-0.5 rounded-md px-1.5 py-0.5 font-mono text-[11px] font-medium leading-none',
              positive
                ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
                : 'bg-red-500/10 text-red-600 dark:text-red-400'
            )}
          >
            {trendUp ? (
              <ArrowUpRight className="h-3 w-3" />
            ) : (
              <ArrowDownRight className="h-3 w-3" />
            )}
            {Math.abs(trend.delta).toFixed(1)}%
          </span>
        )}
        {trend && trendFlat && (
          <span className="mb-0.5 inline-flex items-center gap-0.5 rounded-md bg-muted px-1.5 py-0.5 font-mono text-[11px] font-medium leading-none text-muted-foreground">
            <Minus className="h-3 w-3" />
            0%
          </span>
        )}
      </div>
    </div>
  )
}
