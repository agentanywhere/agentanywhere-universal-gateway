import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from '../test/utils'

vi.mock('../api/admin', () => ({ adminApi: { getLogs: vi.fn() } }))
import { adminApi } from '../api/admin'
import Logs from './Logs'

const api = vi.mocked(adminApi)

const log = (over: Record<string, unknown> = {}) => ({
  request_id: 'r1',
  method: 'GET',
  path: '/api/things',
  client_ip: '10.0.0.1',
  status_code: 200,
  response_time_ms: 12.5,
  timestamp: '2025-03-04T10:11:12Z',
  ...over,
})

describe('Logs page', () => {
  beforeEach(() => {
    api.getLogs.mockReset().mockResolvedValue({ logs: [], total: 0 })
  })

  it('shows skeleton rows while loading', () => {
    api.getLogs.mockReturnValue(new Promise(() => {}))
    const { container } = renderWithProviders(<Logs />)
    expect(container.querySelectorAll('.animate-pulse')).toHaveLength(8)
  })

  it('shows the empty state when no logs match', async () => {
    renderWithProviders(<Logs />)
    expect(await screen.findByText('No logs match your filters')).toBeInTheDocument()
  })

  it('shows the empty state when the request fails', async () => {
    api.getLogs.mockRejectedValue(new Error('x'))
    renderWithProviders(<Logs />)
    expect(await screen.findByText('No logs match your filters')).toBeInTheDocument()
  })

  it('renders rows with method / status variants and latency', async () => {
    api.getLogs.mockResolvedValue({
      total: 6,
      logs: [
        log({ request_id: '1', method: 'GET', status_code: 200 }),
        log({ request_id: '2', method: 'POST', status_code: 302 }),
        log({ request_id: '3', method: 'PUT', status_code: 404 }),
        log({ request_id: '4', method: 'PATCH', status_code: 500 }),
        log({ request_id: '5', method: 'DELETE', status_code: 200 }),
        log({ request_id: '6', method: 'OPTIONS', status_code: 200, response_time_ms: 2500 }),
      ],
    })
    renderWithProviders(<Logs />)
    expect(await screen.findByText('OPTIONS')).toBeInTheDocument()
    expect(screen.getByText('GET').className).toContain('brand-cyan')
    expect(screen.getByText('POST').className).toContain('emerald')
    expect(screen.getByText('PUT').className).toContain('amber')
    expect(screen.getByText('DELETE').className).toContain('red')
    expect(screen.getByText('OPTIONS').className).toContain('bg-muted')
    expect(screen.getByText('302').className).toContain('brand-cyan')
    expect(screen.getByText('404').className).toContain('amber')
    expect(screen.getByText('500').className).toContain('red')
    expect(screen.getAllByText('12.50 ms')).toHaveLength(5)
    expect(screen.getByText('2.50 s')).toBeInTheDocument()
    expect(screen.getAllByText('10.0.0.1').length).toBe(6)
  })

  it('refetches with filters when level, limit and path change', async () => {
    renderWithProviders(<Logs />)
    await waitFor(() => expect(api.getLogs).toHaveBeenCalled())
    expect(api.getLogs.mock.calls[0][0]).toEqual({ limit: 100, level: '', path: '' })
    await userEvent.selectOptions(screen.getByLabelText('Filter by level'), 'error')
    await waitFor(() => expect(api.getLogs).toHaveBeenLastCalledWith({ limit: 100, level: 'error', path: '' }))
    await userEvent.selectOptions(screen.getByLabelText('Number of entries'), '500')
    await waitFor(() => expect(api.getLogs).toHaveBeenLastCalledWith({ limit: 500, level: 'error', path: '' }))
    fireEvent.change(screen.getByLabelText('Filter by path'), { target: { value: '/x' } })
    await waitFor(() => expect(api.getLogs).toHaveBeenLastCalledWith({ limit: 500, level: 'error', path: '/x' }))
  })

  it('manual refresh refetches', async () => {
    renderWithProviders(<Logs />)
    await screen.findByText('No logs match your filters')
    const before = api.getLogs.mock.calls.length
    await userEvent.click(screen.getByRole('button', { name: 'Refresh logs' }))
    await waitFor(() => expect(api.getLogs.mock.calls.length).toBeGreaterThan(before))
  })
})
