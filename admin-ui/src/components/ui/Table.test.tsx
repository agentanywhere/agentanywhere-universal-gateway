import { createRef } from 'react'
import { describe, expect, it } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { Table, Tbody, Td, Th, Thead, Tr } from './Table'

describe('Table', () => {
  it('renders semantic table structure', () => {
    render(
      <Table>
        <Thead>
          <Tr>
            <Th>Name</Th>
            <Th>Value</Th>
          </Tr>
        </Thead>
        <Tbody>
          <Tr>
            <Td>a</Td>
            <Td>1</Td>
          </Tr>
          <Tr>
            <Td>b</Td>
            <Td>2</Td>
          </Tr>
        </Tbody>
      </Table>
    )
    expect(screen.getByRole('table')).toBeInTheDocument()
    expect(screen.getAllByRole('columnheader').map((c) => c.textContent)).toEqual(['Name', 'Value'])
    const rows = screen.getAllByRole('row')
    expect(rows).toHaveLength(3)
    expect(within(rows[2]).getAllByRole('cell').map((c) => c.textContent)).toEqual(['b', '2'])
  })

  it('merges custom classes and forwards refs', () => {
    const ref = createRef<HTMLTableElement>()
    const cellRef = createRef<HTMLTableCellElement>()
    render(
      <Table ref={ref} className="my-table">
        <Tbody className="my-body">
          <Tr className="my-row">
            <Td ref={cellRef} className="my-cell">
              x
            </Td>
          </Tr>
        </Tbody>
      </Table>
    )
    expect(ref.current).toHaveClass('my-table')
    expect(cellRef.current).toHaveClass('my-cell')
    expect(screen.getByRole('row')).toHaveClass('my-row')
    expect(document.querySelector('tbody')).toHaveClass('my-body')
  })
})
