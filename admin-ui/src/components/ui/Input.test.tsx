import { createRef } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Input } from './Input'

describe('Input', () => {
  it('defaults to type text and reports typing', async () => {
    const onChange = vi.fn()
    render(<Input placeholder="name" onChange={onChange} />)
    const input = screen.getByPlaceholderText('name')
    expect(input).toHaveAttribute('type', 'text')
    await userEvent.type(input, 'abc')
    expect(onChange).toHaveBeenCalledTimes(3)
    expect(input).toHaveValue('abc')
  })

  it('passes through the type prop', () => {
    render(<Input type="password" placeholder="pw" />)
    expect(screen.getByPlaceholderText('pw')).toHaveAttribute('type', 'password')
  })

  it('renders left and right icons and pads the input accordingly', () => {
    render(<Input placeholder="x" leftIcon={<i data-testid="l" />} rightIcon={<i data-testid="r" />} />)
    expect(screen.getByTestId('l')).toBeInTheDocument()
    expect(screen.getByTestId('r')).toBeInTheDocument()
    const cls = screen.getByPlaceholderText('x').className
    expect(cls).toContain('pl-10')
    expect(cls).toContain('pr-10')
  })

  it('renders no icon wrappers when none are given', () => {
    const { container } = render(<Input />)
    expect(container.querySelectorAll('.relative > div')).toHaveLength(0)
  })

  it('can be disabled and forwards refs', async () => {
    const ref = createRef<HTMLInputElement>()
    render(<Input ref={ref} disabled placeholder="d" />)
    expect(ref.current).toBe(screen.getByPlaceholderText('d'))
    expect(ref.current).toBeDisabled()
  })
})
