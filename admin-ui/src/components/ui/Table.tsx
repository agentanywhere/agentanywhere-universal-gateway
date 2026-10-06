import * as React from 'react'
import { cn } from '../../lib/utils'

export const Table = React.forwardRef<
  HTMLTableElement,
  React.HTMLAttributes<HTMLTableElement>
>(({ className, ...props }, ref) => (
  <div className="relative w-full overflow-x-auto">
    <table
      ref={ref}
      className={cn('w-full caption-bottom text-sm text-foreground', className)}
      {...props}
    />
  </div>
))
Table.displayName = 'Table'

export const Thead = React.forwardRef<
  HTMLTableSectionElement,
  React.HTMLAttributes<HTMLTableSectionElement>
>(({ className, ...props }, ref) => (
  <thead
    ref={ref}
    className={cn(
      'border-b border-border bg-muted/30 text-xs uppercase tracking-wider text-muted-foreground',
      className
    )}
    {...props}
  />
))
Thead.displayName = 'Thead'

export const Tbody = React.forwardRef<
  HTMLTableSectionElement,
  React.HTMLAttributes<HTMLTableSectionElement>
>(({ className, ...props }, ref) => (
  <tbody
    ref={ref}
    className={cn('[&>tr:last-child]:border-0 divide-y divide-border', className)}
    {...props}
  />
))
Tbody.displayName = 'Tbody'

export const Tr = React.forwardRef<
  HTMLTableRowElement,
  React.HTMLAttributes<HTMLTableRowElement>
>(({ className, ...props }, ref) => (
  <tr
    ref={ref}
    className={cn(
      'transition-colors hover:bg-accent/40 data-[state=selected]:bg-accent/60',
      className
    )}
    {...props}
  />
))
Tr.displayName = 'Tr'

export const Th = React.forwardRef<
  HTMLTableCellElement,
  React.ThHTMLAttributes<HTMLTableCellElement>
>(({ className, ...props }, ref) => (
  <th
    ref={ref}
    className={cn(
      'h-10 px-4 text-left align-middle font-medium text-muted-foreground',
      className
    )}
    {...props}
  />
))
Th.displayName = 'Th'

export const Td = React.forwardRef<
  HTMLTableCellElement,
  React.TdHTMLAttributes<HTMLTableCellElement>
>(({ className, ...props }, ref) => (
  <td
    ref={ref}
    className={cn('px-4 py-3 align-middle text-foreground', className)}
    {...props}
  />
))
Td.displayName = 'Td'
