import * as React from 'react'
import { createPortal } from 'react-dom'
import { CheckCircle2, AlertCircle, AlertTriangle, Info, X } from 'lucide-react'
import { cn } from '../../lib/utils'

type ToastVariant = 'success' | 'error' | 'warning' | 'info'

interface ToastItem {
  id: number
  title: string
  description?: string
  variant: ToastVariant
}

interface ToastContextValue {
  toast: (input: { title: string; description?: string; variant?: ToastVariant }) => void
}

const ToastContext = React.createContext<ToastContextValue | null>(null)

const variantStyles: Record<ToastVariant, { icon: React.ReactNode; classes: string }> = {
  success: {
    icon: <CheckCircle2 className="h-5 w-5 text-emerald-500" />,
    classes: 'border-emerald-500/30',
  },
  error: {
    icon: <AlertCircle className="h-5 w-5 text-red-500" />,
    classes: 'border-red-500/30',
  },
  warning: {
    icon: <AlertTriangle className="h-5 w-5 text-amber-500" />,
    classes: 'border-amber-500/30',
  },
  info: {
    icon: <Info className="h-5 w-5 text-sky-500" />,
    classes: 'border-sky-500/30',
  },
}

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = React.useState<ToastItem[]>([])
  const counter = React.useRef(0)

  const remove = React.useCallback((id: number) => {
    setItems((prev) => prev.filter((t) => t.id !== id))
  }, [])

  const toast = React.useCallback<ToastContextValue['toast']>(
    ({ title, description, variant = 'info' }) => {
      const id = ++counter.current
      setItems((prev) => [...prev, { id, title, description, variant }])
      window.setTimeout(() => remove(id), 4000)
    },
    [remove]
  )

  return (
    <ToastContext.Provider value={{ toast }}>
      {children}
      {typeof document !== 'undefined' &&
        createPortal(
          <div
            aria-live="polite"
            aria-atomic="true"
            className="pointer-events-none fixed inset-x-0 bottom-4 z-[200] flex flex-col items-center gap-2 px-4 sm:bottom-6 sm:left-auto sm:right-6 sm:items-end"
          >
            {items.map((item) => {
              const v = variantStyles[item.variant]
              return (
                <div
                  key={item.id}
                  className={cn(
                    'pointer-events-auto flex w-full max-w-sm items-start gap-3 rounded-xl border bg-card/95 px-4 py-3 shadow-lg shadow-black/20 backdrop-blur-md',
                    'animate-slide-up',
                    v.classes
                  )}
                  role="status"
                >
                  <div className="mt-0.5 shrink-0">{v.icon}</div>
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium text-foreground">{item.title}</p>
                    {item.description && (
                      <p className="mt-0.5 text-xs text-muted-foreground break-words">
                        {item.description}
                      </p>
                    )}
                  </div>
                  <button
                    type="button"
                    aria-label="Dismiss"
                    onClick={() => remove(item.id)}
                    className="-mr-1 -mt-1 inline-flex h-6 w-6 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
              )
            })}
          </div>,
          document.body
        )}
    </ToastContext.Provider>
  )
}

export function useToast(): ToastContextValue {
  const ctx = React.useContext(ToastContext)
  if (!ctx) throw new Error('useToast must be used inside <ToastProvider>')
  return ctx
}
