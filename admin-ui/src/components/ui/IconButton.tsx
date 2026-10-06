import * as React from 'react'
import { cn } from '../../lib/utils'

export interface IconButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  /** Required for accessibility — describes the action. */
  label: string
  size?: 'sm' | 'md'
  variant?: 'ghost' | 'subtle' | 'danger'
}

const sizes = {
  sm: 'h-7 w-7',
  md: 'h-9 w-9',
}

const variants = {
  ghost:
    'bg-transparent text-muted-foreground hover:bg-accent hover:text-foreground',
  subtle:
    'bg-muted text-muted-foreground hover:bg-accent hover:text-foreground',
  danger:
    'bg-transparent text-muted-foreground hover:bg-red-500/10 hover:text-red-500',
}

export const IconButton = React.forwardRef<HTMLButtonElement, IconButtonProps>(
  ({ className, label, size = 'md', variant = 'ghost', ...props }, ref) => {
    return (
      <button
        ref={ref}
        type="button"
        aria-label={label}
        title={label}
        className={cn(
          'inline-flex items-center justify-center rounded-md transition-colors duration-150',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background',
          'disabled:opacity-50 disabled:cursor-not-allowed',
          sizes[size],
          variants[variant],
          className
        )}
        {...props}
      />
    )
  }
)
IconButton.displayName = 'IconButton'
