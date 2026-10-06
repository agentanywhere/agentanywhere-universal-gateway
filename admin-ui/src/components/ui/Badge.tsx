import * as React from 'react'
import { cn } from '../../lib/utils'

type Variant = 'default' | 'success' | 'warning' | 'danger' | 'info' | 'muted' | 'primary'

const variantStyles: Record<Variant, string> = {
  default: 'bg-secondary text-secondary-foreground border-border/80',
  primary: 'bg-primary/10 text-primary border-primary/20',
  success: 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-400 border-emerald-500/20',
  warning: 'bg-amber-500/10 text-amber-700 dark:text-amber-400 border-amber-500/20',
  danger: 'bg-red-500/10 text-red-700 dark:text-red-400 border-red-500/20',
  info: 'bg-teal-500/10 text-teal-700 dark:text-teal-400 border-teal-500/20',
  muted: 'bg-muted text-muted-foreground border-border/80',
}

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  variant?: Variant
  dot?: boolean
}

export function Badge({ className, variant = 'default', dot, children, ...props }: BadgeProps) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-[11px] font-medium leading-none tracking-wide',
        variantStyles[variant],
        className
      )}
      {...props}
    >
      {dot && (
        <span
          aria-hidden="true"
          className={cn(
            'inline-block h-1.5 w-1.5 rounded-full',
            variant === 'success' && 'animate-pulse bg-emerald-500',
            variant === 'warning' && 'bg-amber-500',
            variant === 'danger' && 'bg-red-500',
            variant === 'info' && 'bg-teal-500',
            variant === 'primary' && 'bg-primary',
            (variant === 'default' || variant === 'muted') && 'bg-muted-foreground/60'
          )}
        />
      )}
      {children}
    </span>
  )
}
