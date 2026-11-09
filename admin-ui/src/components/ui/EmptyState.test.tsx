import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { EmptyState } from './EmptyState'

describe('EmptyState', () => {
  it('renders only a title when nothing else is given', () => {
    const { container } = render(<EmptyState title="Nothing" />)
    expect(screen.getByRole('heading', { name: 'Nothing' })).toBeInTheDocument()
    expect(container.querySelector('p')).toBeNull()
  })

  it('renders icon, description and action', () => {
    render(
      <EmptyState
        className="mine"
        icon={<i data-testid="icon" />}
        title="Empty"
        description="Add something"
        action={<button>Add</button>}
      />
    )
    expect(screen.getByTestId('icon')).toBeInTheDocument()
    expect(screen.getByText('Add something')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add' })).toBeInTheDocument()
    expect(screen.getByRole('heading').parentElement).toHaveClass('mine')
  })
})
