import { createRef } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Select } from './Select'

describe('Select', () => {
  it('renders options and fires onChange with the selected value', async () => {
    const onChange = vi.fn()
    render(
      <Select defaultValue="a" onChange={onChange} aria-label="pick">
        <option value="a">A</option>
        <option value="b">B</option>
      </Select>
    )
    const sel = screen.getByLabelText('pick')
    expect(sel).toHaveValue('a')
    await userEvent.selectOptions(sel, 'b')
    expect(onChange).toHaveBeenCalledTimes(1)
    expect(sel).toHaveValue('b')
  })

  it('supports disabled, custom classes and ref', () => {
    const ref = createRef<HTMLSelectElement>()
    render(
      <Select ref={ref} disabled className="custom" aria-label="s">
        <option>x</option>
      </Select>
    )
    expect(ref.current).toBeDisabled()
    expect(ref.current).toHaveClass('custom')
  })
})
