import * as React from 'react'
import { cn } from '../../lib/utils'

type Variant = 'primary' | 'secondary' | 'outline' | 'ghost' | 'danger'
type Size = 'sm' | 'md' | 'lg' | 'icon' | 'icon-sm'

const variantStyles: Record<Variant, string> = {
  primary:
    'bg-primary text-primary-foreground hover:bg-primary/90 shadow-soft',
  secondary:
    'bg-secondary text-secondary-foreground hover:bg-secondary/80 border border-border/80',
  outline:
    'border border-border bg-transparent text-foreground hover:bg-accent hover:text-accent-foreground',
  ghost: 'bg-transparent text-foreground hover:bg-accent hover:text-accent-foreground',
  danger:
    'bg-destructive text-destructive-foreground hover:bg-destructive/90 shadow-soft',
}

const sizeStyles: Record<Size, string> = {
  sm: 'h-8 px-3 text-[13px] rounded-md gap-1.5',
  md: 'h-10 px-4 text-sm rounded-md gap-2',
  lg: 'h-11 px-5 text-sm rounded-md gap-2',
  icon: 'h-10 w-10 rounded-md',
  'icon-sm': 'h-8 w-8 rounded-md',
}

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant
  size?: Size
  isLoading?: boolean
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant = 'primary', size = 'md', isLoading, disabled, children, ...props }, ref) => {
    return (
      <button
        ref={ref}
        disabled={disabled || isLoading}
        className={cn(
          'inline-flex items-center justify-center font-medium whitespace-nowrap',
          'transition-[background-color,color,border-color,box-shadow,transform] duration-150',
          'active:scale-[0.98]',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
          'disabled:opacity-50 disabled:cursor-not-allowed disabled:active:scale-100',
          variantStyles[variant],
          sizeStyles[size],
          className
        )}
        {...props}
      >
        {isLoading && (
          <span
            aria-hidden="true"
            className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent"
          />
        )}
        {children}
      </button>
    )
  }
)
Button.displayName = 'Button'
