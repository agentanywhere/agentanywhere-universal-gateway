import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

const stub = vi.hoisted(() => (name: string) => ({ default: () => <div>{name} PAGE</div> }))
vi.mock('./pages/Dashboard', () => stub('Dashboard'))
vi.mock('./pages/Analytics', () => stub('Analytics'))
vi.mock('./pages/AIInsights', () => stub('AIInsights'))
vi.mock('./pages/Reports', () => stub('Reports'))
vi.mock('./pages/APIKeys', () => stub('APIKeys'))
vi.mock('./pages/Agents', () => stub('Agents'))
vi.mock('./pages/Monitoring', () => stub('Monitoring'))
vi.mock('./pages/Routes', () => stub('Routes'))
vi.mock('./pages/Protocols', () => stub('Protocols'))
vi.mock('./pages/ProtocolTest', () => stub('ProtocolTest'))
vi.mock('./pages/Logs', () => stub('Logs'))
vi.mock('./pages/Settings', () => stub('Settings'))
vi.mock('./ida', async (orig) => ({
  ...(await orig<typeof import('./ida')>()),
  idaHandleCallback: vi.fn(),
  idaEnabled: vi.fn(async () => false),
  idaLogout: vi.fn(),
  isIdaSession: () => false,
}))
import { idaHandleCallback } from './ida'
import App from './App'

describe('App', () => {
  beforeEach(() => {
    vi.mocked(idaHandleCallback).mockReset().mockResolvedValue(false)
    window.history.pushState({}, '', '/')
  })

  it('shows a boot spinner until the SSO callback check finishes', async () => {
    let resolve!: (v: boolean) => void
    vi.mocked(idaHandleCallback).mockReturnValue(new Promise((r) => (resolve = r)))
    const { container } = render(<App />)
    expect(container.querySelector('.animate-spin')).toBeInTheDocument()
    expect(screen.queryByText('Sign in')).toBeNull()
    resolve(false)
    await waitFor(() => expect(container.querySelector('.animate-spin')).toBeNull())
  })

  it('redirects unauthenticated users to the login page', async () => {
    window.history.pushState({}, '', '/dashboard')
    render(<App />)
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(window.location.pathname).toBe('/login')
  })

  it('renders the dashboard inside the layout when authenticated, and / redirects to it', async () => {
    localStorage.setItem('admin_token', 'tok')
    render(<App />)
    expect(await screen.findByText('Dashboard PAGE')).toBeInTheDocument()
    expect(window.location.pathname).toBe('/dashboard')
    expect(screen.getByRole('link', { name: 'Skip to content' })).toBeInTheDocument()
  })

  it.each([
    ['/agents', 'Agents'],
    ['/monitoring', 'Monitoring'],
    ['/analytics', 'Analytics'],
    ['/ai-insights', 'AIInsights'],
    ['/reports', 'Reports'],
    ['/api-keys', 'APIKeys'],
    ['/routes', 'Routes'],
    ['/protocols', 'Protocols'],
    ['/test/mcp', 'ProtocolTest'],
    ['/logs', 'Logs'],
    ['/settings', 'Settings'],
  ])('routes %s to its page', async (path, name) => {
    localStorage.setItem('admin_token', 'tok')
    window.history.pushState({}, '', path)
    render(<App />)
    expect(await screen.findByText(`${name} PAGE`)).toBeInTheDocument()
  })

  it('redirects an authenticated user away from /login', async () => {
    localStorage.setItem('admin_token', 'tok')
    window.history.pushState({}, '', '/login')
    render(<App />)
    expect(await screen.findByText('Dashboard PAGE')).toBeInTheDocument()
  })
})
