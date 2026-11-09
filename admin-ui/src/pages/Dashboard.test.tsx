import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from '../test/utils'

vi.mock('../api/admin', () => ({
  adminApi: { getDashboardStats: vi.fn(), getDetailedHealth: vi.fn(), getRoutes: vi.fn() },
}))
import { adminApi } from '../api/admin'
import Dashboard from './Dashboard'

const api = vi.mocked(adminApi)

const stats = (over: Record<string, unknown> = {}) => ({
  total_requests: 12345,
  total_errors: 12,
  avg_response_time: 45.5,
  active_connections: 3,
  rate_limit_hits: 7,
  circuit_breaker_states: {},
  requests_per_minute: 90,
  error_rate: 0.001,
  ...over,
})

describe('Dashboard page', () => {
  beforeEach(() => {
    api.getDashboardStats.mockReset().mockResolvedValue(stats() as any)
    api.getDetailedHealth.mockReset().mockResolvedValue({ services: {} })
    api.getRoutes.mockReset().mockResolvedValue([])
  })

  it('shows loading placeholders before data arrives', () => {
    api.getDashboardStats.mockReturnValue(new Promise(() => {}))
    api.getDetailedHealth.mockReturnValue(new Promise(() => {}))
    const { container } = renderWithProviders(<Dashboard />)
    expect(screen.getByRole('heading', { name: 'Dashboard' })).toBeInTheDocument()
    expect(container.querySelectorAll('.animate-pulse').length).toBeGreaterThan(6)
  })

  it('renders formatted stats and the live badge', async () => {
    renderWithProviders(<Dashboard />)
    expect(await screen.findByText((12345).toLocaleString())).toBeInTheDocument()
    expect(screen.getByText('45.50 ms')).toBeInTheDocument()
    expect(screen.getByText('90')).toBeInTheDocument()
    expect(screen.getAllByText('0.10%')).toHaveLength(2) // stat tile + errors card badge
    expect(screen.getByText('Live · 5s')).toBeInTheDocument()
    expect(screen.queryByText('Stats unavailable')).toBeNull()
  })

  it('shows the sampling placeholder until two data points exist', async () => {
    renderWithProviders(<Dashboard />)
    await screen.findByText((12345).toLocaleString())
    expect(screen.getByText(/Sampling… points appear/)).toBeInTheDocument()
    expect(screen.getByText('1 pts')).toBeInTheDocument()
  })

  it('accumulates chart points across refetches', async () => {
    const { client } = renderWithProviders(<Dashboard />)
    await screen.findByText('1 pts')
    api.getDashboardStats.mockResolvedValue(stats({ total_requests: 12445, requests_per_minute: 100 }) as any)
    await act(async () => {
      await client.refetchQueries({ queryKey: ['dashboard-stats'] })
    })
    expect(await screen.findByText('2 pts')).toBeInTheDocument()
    expect(screen.queryByText(/Sampling… points appear/)).toBeNull()
  })

  it('colours the error-rate badge by threshold', async () => {
    api.getDashboardStats.mockResolvedValue(stats({ error_rate: 0.2 }) as any)
    renderWithProviders(<Dashboard />)
    const badges = await screen.findAllByText('20.00%')
    expect(badges.some((b) => b.className.includes('red'))).toBe(true)
  })

  it('shows an error banner with retry when stats fail', async () => {
    api.getDashboardStats.mockRejectedValue(new Error('500'))
    renderWithProviders(<Dashboard />)
    expect(await screen.findByText("Couldn't load gateway metrics")).toBeInTheDocument()
    expect(screen.getByText('Stats unavailable')).toBeInTheDocument()
    api.getDashboardStats.mockResolvedValue(stats() as any)
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(screen.queryByText("Couldn't load gateway metrics")).toBeNull())
    expect(await screen.findByText('Live · 5s')).toBeInTheDocument()
  })

  it('marks protocols Active only when a route uses them', async () => {
    api.getRoutes.mockResolvedValue([
      { path: '/a', backend_url: 'x', protocol: 'mcp', timeout: 1, requires_auth: true },
      { path: '/b', backend_url: 'x', protocol: 'grpc', timeout: 1, requires_auth: true },
    ])
    renderWithProviders(<Dashboard />)
    await waitFor(() => expect(screen.getAllByText('Active')).toHaveLength(2))
    expect(screen.getAllByText('Idle')).toHaveLength(4)
  })

  it('shows the empty state when no services are reported', async () => {
    renderWithProviders(<Dashboard />)
    expect(await screen.findByText('No services reported')).toBeInTheDocument()
  })

  it('lists service health with healthy/unhealthy badges and messages', async () => {
    api.getDetailedHealth.mockResolvedValue({
      services: {
        redis_cache: { status: 'healthy' },
        database: { status: 'degraded', message: 'replica lag' },
        unknown_svc: {},
      },
    })
    renderWithProviders(<Dashboard />)
    expect(await screen.findByText('redis cache')).toBeInTheDocument()
    expect(screen.getByText('healthy')).toBeInTheDocument()
    expect(screen.getByText('degraded')).toBeInTheDocument()
    expect(screen.getByText('replica lag')).toBeInTheDocument()
    expect(screen.getByText('unknown')).toBeInTheDocument()
  })
})
