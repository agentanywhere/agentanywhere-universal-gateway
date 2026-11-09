import { createRef } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Button } from './Button'

describe('Button', () => {
  it('renders children and handles clicks', async () => {
    const onClick = vi.fn()
    render(<Button onClick={onClick}>Save</Button>)
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(onClick).toHaveBeenCalledTimes(1)
  })

  it('applies variant and size classes', () => {
    render(
      <Button variant="danger" size="sm" className="extra">
        Del
      </Button>
    )
    const btn = screen.getByRole('button')
    expect(btn.className).toContain('bg-destructive')
    expect(btn.className).toContain('h-8')
    expect(btn).toHaveClass('extra')
  })

  it('defaults to primary / md', () => {
    render(<Button>Go</Button>)
    expect(screen.getByRole('button').className).toContain('bg-primary')
    expect(screen.getByRole('button').className).toContain('h-10')
  })

  it('is disabled and shows a spinner while loading', async () => {
    const onClick = vi.fn()
    const { container } = render(
      <Button isLoading onClick={onClick}>
        Wait
      </Button>
    )
    const btn = screen.getByRole('button')
    expect(btn).toBeDisabled()
    expect(container.querySelector('.animate-spin')).toBeInTheDocument()
    await userEvent.click(btn)
    expect(onClick).not.toHaveBeenCalled()
  })

  it('respects the disabled prop without a spinner', () => {
    const { container } = render(<Button disabled>No</Button>)
    expect(screen.getByRole('button')).toBeDisabled()
    expect(container.querySelector('.animate-spin')).toBeNull()
  })

  it('forwards refs', () => {
    const ref = createRef<HTMLButtonElement>()
    render(<Button ref={ref}>R</Button>)
    expect(ref.current).toBeInstanceOf(HTMLButtonElement)
  })
})
