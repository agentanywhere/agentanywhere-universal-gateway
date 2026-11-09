import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'
import { Skeleton, SkeletonText } from './Skeleton'

describe('Skeleton', () => {
  it('is hidden from assistive tech and accepts classes', () => {
    const { container } = render(<Skeleton className="h-4 w-4" data-testid="s" />)
    const el = container.firstElementChild as HTMLElement
    expect(el).toHaveAttribute('aria-hidden', 'true')
    expect(el).toHaveClass('animate-pulse', 'h-4', 'w-4')
  })

  it('SkeletonText renders the requested lines with a short last line', () => {
    const { container } = render(<SkeletonText lines={4} />)
    const lines = container.querySelectorAll('[aria-hidden="true"]')
    expect(lines).toHaveLength(4)
    expect(lines[3]).toHaveClass('w-2/3')
    expect(lines[0]).toHaveClass('w-full')
  })

  it('SkeletonText defaults to 3 lines', () => {
    const { container } = render(<SkeletonText />)
    expect(container.querySelectorAll('[aria-hidden="true"]')).toHaveLength(3)
  })
})
