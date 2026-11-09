import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, within } from '@testing-library/react'
import { renderWithProviders } from '../test/utils'

vi.mock('../api/admin', () => ({ adminApi: { getRoutes: vi.fn() } }))
import { adminApi } from '../api/admin'
import Routes from './Routes'

const api = vi.mocked(adminApi)

const route = (over: Record<string, unknown> = {}) => ({
  path: '/mcp',
  backend_url: 'http://mcp:9000',
  protocol: 'mcp',
  timeout: 30,
  requires_auth: true,
  ...over,
})

describe('Routes page', () => {
  beforeEach(() => {
    api.getRoutes.mockReset()
  })

  it('shows skeleton cards while loading', () => {
    api.getRoutes.mockReturnValue(new Promise(() => {}))
    const { container } = renderWithProviders(<Routes />)
    expect(screen.getByRole('heading', { name: 'Routes' })).toBeInTheDocument()
    expect(container.querySelectorAll('.animate-pulse')).toHaveLength(4)
  })

  it('shows the empty state when there are no routes', async () => {
    api.getRoutes.mockResolvedValue([])
    renderWithProviders(<Routes />)
    expect(await screen.findByText('No routes configured')).toBeInTheDocument()
    expect(screen.queryByText('Protocol summary')).toBeNull()
  })

  it('shows the empty state when the request fails', async () => {
    api.getRoutes.mockRejectedValue(new Error('boom'))
    renderWithProviders(<Routes />)
    expect(await screen.findByText('No routes configured')).toBeInTheDocument()
  })

  it('renders a card per route with details and a pluralised count', async () => {
    api.getRoutes.mockResolvedValue([
      route(),
      route({ path: '/a2a', backend_url: 'http://a2a', protocol: 'a2a', timeout: 10, requires_auth: false }),
    ])
    renderWithProviders(<Routes />)
    expect(await screen.findByText('/mcp')).toBeInTheDocument()
    expect(screen.getByText('http://mcp:9000')).toBeInTheDocument()
    expect(screen.getByText('2 routes')).toBeInTheDocument()
    expect(screen.getByText('30s')).toBeInTheDocument()
    expect(screen.getByText('10s')).toBeInTheDocument()
    expect(screen.getByText('Required')).toBeInTheDocument()
    expect(screen.getByText('Optional')).toBeInTheDocument()
    expect(screen.getAllByText('Active')).toHaveLength(2)
  })

  it('uses the singular for one route', async () => {
    api.getRoutes.mockResolvedValue([route()])
    renderWithProviders(<Routes />)
    expect(await screen.findByText('1 route')).toBeInTheDocument()
  })

  it('falls back to an upper-cased label for unknown protocols', async () => {
    api.getRoutes.mockResolvedValue([route({ path: '/x', protocol: 'zmq' })])
    renderWithProviders(<Routes />)
    expect(await screen.findByText('ZMQ')).toBeInTheDocument()
  })

  it('summarises routes per protocol family', async () => {
    api.getRoutes.mockResolvedValue([
      route(),
      route({ path: '/m2' }),
      route({ path: '/h', protocol: 'http' }),
    ])
    renderWithProviders(<Routes />)
    const heading = await screen.findByText('Protocol summary')
    const card = heading.closest('div[class*="rounded-xl"]') as HTMLElement
    const mcp = within(card).getByText('MCP').previousElementSibling
    expect(mcp).toHaveTextContent('2')
    expect(within(card).getByText('HTTP').previousElementSibling).toHaveTextContent('1')
    expect(within(card).getByText('gRPC').previousElementSibling).toHaveTextContent('0')
  })
})
