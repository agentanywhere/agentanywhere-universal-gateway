import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Badge } from './Badge'

describe('Badge', () => {
  it('renders children with the default variant', () => {
    render(<Badge>hi</Badge>)
    expect(screen.getByText('hi').className).toContain('bg-secondary')
  })

  it.each([
    ['success', 'emerald'],
    ['warning', 'amber'],
    ['danger', 'red'],
    ['info', 'brand-cyan'],
    ['primary', 'bg-primary/10'],
    ['muted', 'bg-muted'],
  ] as const)('applies %s variant styles', (variant, token) => {
    render(<Badge variant={variant}>b</Badge>)
    expect(screen.getByText('b').className).toContain(token)
  })

  it('renders a dot, pulsing for success', () => {
    const { container, rerender } = render(
      <Badge variant="success" dot>
        s
      </Badge>
    )
    expect(container.querySelector('[aria-hidden="true"]')?.className).toContain('animate-pulse')
    rerender(
      <Badge variant="muted" dot>
        s
      </Badge>
    )
    expect(container.querySelector('[aria-hidden="true"]')?.className).toContain('bg-muted-foreground/60')
  })

  it.each(['warning', 'danger', 'info', 'primary', 'default'] as const)('renders a dot for %s', (v) => {
    const { container } = render(
      <Badge variant={v} dot>
        d
      </Badge>
    )
    expect(container.querySelector('[aria-hidden="true"]')).toBeInTheDocument()
  })

  it('renders no dot by default and passes through html attrs', () => {
    const { container } = render(<Badge data-testid="b" className="x">n</Badge>)
    expect(container.querySelector('[aria-hidden="true"]')).toBeNull()
    expect(screen.getByTestId('b')).toHaveClass('x')
  })
})
