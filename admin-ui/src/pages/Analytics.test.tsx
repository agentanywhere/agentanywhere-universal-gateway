import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, screen } from '@testing-library/react'
import { renderWithProviders } from '../test/utils'

vi.mock('../api/admin', () => ({ adminApi: { getDashboardStats: vi.fn() } }))
import { adminApi } from '../api/admin'
import Analytics from './Analytics'

const api = vi.mocked(adminApi)

const stats = (over: Record<string, unknown> = {}) => ({
  total_requests: 1000,
  total_errors: 40,
  avg_response_time: 12.345,
  active_connections: 1,
  rate_limit_hits: 0,
  circuit_breaker_states: {},
  requests_per_minute: 321,
  error_rate: 0.04,
  ...over,
})

describe('Analytics page', () => {
  beforeEach(() => {
    api.getDashboardStats.mockReset().mockResolvedValue(stats() as any)
  })

  it('renders dashes for live metrics before data arrives', () => {
    api.getDashboardStats.mockReturnValue(new Promise(() => {}))
    renderWithProviders(<Analytics />)
    expect(screen.getByRole('heading', { name: 'Analytics' })).toBeInTheDocument()
  })

  it('renders live performance metrics from stats', async () => {
    renderWithProviders(<Analytics />)
    expect(await screen.findByText('321/min')).toBeInTheDocument()
    expect(screen.getByText('12.35 ms')).toBeInTheDocument()
    expect(screen.getByText('4.00%')).toBeInTheDocument()
    expect(screen.getByText('40')).toBeInTheDocument()
  })

  it('shows the sample-data banner and demo breakdowns', async () => {
    renderWithProviders(<Analytics />)
    await screen.findByText('321/min')
    expect(screen.getAllByText(/Sample data/).length).toBeGreaterThan(0)
    expect(screen.getAllByText('/mcp/query').length).toBeGreaterThan(0)
    expect(screen.getAllByText('2xx').length).toBeGreaterThan(0)
  })

  it('uses the danger tone when error rate exceeds 5%', async () => {
    api.getDashboardStats.mockResolvedValue(stats({ error_rate: 0.2 }) as any)
    renderWithProviders(<Analytics />)
    expect(await screen.findByText('20.00%')).toBeInTheDocument()
  })

  it('renders time-series charts once two samples exist', async () => {
    const { client } = renderWithProviders(<Analytics />)
    await screen.findByText('321/min')
    api.getDashboardStats.mockResolvedValue(stats({ total_requests: 1100 }) as any)
    await act(async () => {
      await client.refetchQueries({ queryKey: ['analytics-stats'] })
    })
    expect(screen.getByRole('heading', { name: 'Analytics' })).toBeInTheDocument()
  })
})
