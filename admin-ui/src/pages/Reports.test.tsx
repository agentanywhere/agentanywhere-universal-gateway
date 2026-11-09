import { describe, expect, it } from 'vitest'
import { fireEvent, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from '../test/utils'
import Reports from './Reports'

describe('Reports page', () => {
  it('renders templates, formats and the generated list', () => {
    renderWithProviders(<Reports />)
    expect(screen.getByRole('heading', { name: 'Reports' })).toBeInTheDocument()
    expect(screen.getByText(/UI preview/)).toBeInTheDocument()
    for (const t of ['Performance report', 'Error analysis', 'Usage statistics', 'Health check']) {
      expect(screen.getByText(t)).toBeInTheDocument()
    }
    for (const f of ['PDF', 'CSV', 'Excel']) expect(screen.getByRole('button', { name: f })).toBeInTheDocument()
    expect(screen.getByText('Daily performance report')).toBeInTheDocument()
    expect(screen.getAllByText('ready')).toHaveLength(3)
    expect(screen.getAllByRole('button', { name: /Download/ })).toHaveLength(3)
  })

  it('clicking a template queues a (preview-only) report toast', async () => {
    renderWithProviders(<Reports />)
    await userEvent.click(screen.getByText('Error analysis'))
    expect(await screen.findByText('Report generation queued')).toBeInTheDocument()
    expect(screen.getByText(/Error analysis report/)).toBeInTheDocument()
  })

  it('generate report uses the selected range', async () => {
    renderWithProviders(<Reports />)
    await userEvent.click(screen.getByRole('button', { name: 'weekly' }))
    await userEvent.click(screen.getByRole('button', { name: 'Generate report' }))
    expect(await screen.findByText(/weekly report/)).toBeInTheDocument()
  })

  it('shows date inputs only for the custom range and accepts edits', async () => {
    renderWithProviders(<Reports />)
    expect(screen.queryByText('Start date')).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'custom' }))
    const [start, end] = Array.from(document.querySelectorAll('input[type="date"]')) as HTMLInputElement[]
    expect(start).toBeInTheDocument()
    fireEvent.change(start, { target: { value: '2025-01-01' } })
    fireEvent.change(end, { target: { value: '2025-01-31' } })
    expect(start.value).toBe('2025-01-01')
    expect(end.value).toBe('2025-01-31')
    await userEvent.click(screen.getByRole('button', { name: 'daily' }))
    expect(screen.queryByText('Start date')).toBeNull()
  })
})
