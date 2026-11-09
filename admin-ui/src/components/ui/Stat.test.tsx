import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Stat } from './Stat'

describe('Stat', () => {
  it('renders label, value, hint and icon', () => {
    render(<Stat label="Requests" value="1,234" hint="Lifetime" icon={<i data-testid="ic" />} />)
    expect(screen.getByText('Requests')).toBeInTheDocument()
    expect(screen.getByText('1,234')).toBeInTheDocument()
    expect(screen.getByText('Lifetime')).toBeInTheDocument()
    expect(screen.getByTestId('ic')).toBeInTheDocument()
  })

  it('hides the value and hint while loading', () => {
    const { container } = render(<Stat label="L" value="99" hint="h" isLoading />)
    expect(screen.queryByText('99')).toBeNull()
    expect(screen.queryByText('h')).toBeNull()
    expect(container.querySelector('.animate-pulse')).toBeInTheDocument()
  })

  it('shows a positive trend as green with an up arrow', () => {
    render(<Stat label="L" value="1" trend={{ delta: 12.345 }} />)
    const badge = screen.getByText('12.3%')
    expect(badge.className).toContain('emerald')
  })

  it('shows a negative trend as red', () => {
    render(<Stat label="L" value="1" trend={{ delta: -5 }} />)
    expect(screen.getByText('5.0%').className).toContain('red')
  })

  it('inverts colours when trend.inverse is set (up is bad)', () => {
    render(<Stat label="L" value="1" trend={{ delta: 5, inverse: true }} />)
    expect(screen.getByText('5.0%').className).toContain('red')
  })

  it('shows 0% for a flat trend', () => {
    render(<Stat label="L" value="1" trend={{ delta: 0 }} />)
    expect(screen.getByText('0%')).toBeInTheDocument()
  })

  it('shows no trend when none is given', () => {
    render(<Stat label="L" value="1" />)
    expect(screen.queryByText(/%$/)).toBeNull()
  })
})
