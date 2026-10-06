import * as React from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'
import { cn } from '../../lib/utils'

interface DialogProps {
  open: boolean
  onClose: () => void
  title?: React.ReactNode
  description?: React.ReactNode
  children?: React.ReactNode
  footer?: React.ReactNode
  className?: string
  /** Hide the close button in the corner */
  hideClose?: boolean
}

export function Dialog({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  className,
  hideClose,
}: DialogProps) {
  const dialogRef = React.useRef<HTMLDivElement | null>(null)

  React.useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    const previouslyFocused = document.activeElement as HTMLElement | null
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    requestAnimationFrame(() => {
      const focusable = dialogRef.current?.querySelector<HTMLElement>(
        'input, textarea, button, [tabindex]:not([tabindex="-1"])'
      )
      focusable?.focus()
    })

    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = previousOverflow
      previouslyFocused?.focus?.()
    }
  }, [open, onClose])

  if (!open) return null

  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby={title ? 'dialog-title' : undefined}
      className="fixed inset-0 z-[100] flex items-center justify-center p-4 sm:p-6"
    >
      <button
        type="button"
        aria-label="Close dialog"
        onClick={onClose}
        className="absolute inset-0 bg-background/70 backdrop-blur-sm animate-fade-in"
      />
      <div
        ref={dialogRef}
        className={cn(
          'relative z-10 w-full max-w-md rounded-2xl border border-border bg-card text-card-foreground shadow-2xl shadow-black/30',
          'animate-scale-in',
          className
        )}
      >
        {(title || !hideClose) && (
          <div className="flex items-start justify-between gap-4 p-6 pb-2">
            <div className="space-y-1">
              {title && (
                <h2 id="dialog-title" className="text-lg font-semibold tracking-tight text-foreground">
                  {title}
                </h2>
              )}
              {description && (
                <p className="text-sm text-muted-foreground">{description}</p>
              )}
            </div>
            {!hideClose && (
              <button
                type="button"
                onClick={onClose}
                aria-label="Close dialog"
                className="-mr-2 -mt-2 inline-flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <X className="h-4 w-4" />
              </button>
            )}
          </div>
        )}
        <div className="p-6 pt-4">{children}</div>
        {footer && (
          <div className="flex items-center justify-end gap-2 border-t border-border bg-muted/30 px-6 py-4 rounded-b-2xl">
            {footer}
          </div>
        )}
      </div>
    </div>,
    document.body
  )
}
