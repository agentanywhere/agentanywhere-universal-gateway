import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes, useLocation } from 'react-router-dom'
import { renderWithProviders } from '../test/utils'

vi.mock('../api/admin', () => ({ adminApi: { getRoutes: vi.fn(), getConfig: vi.fn() } }))
import { adminApi } from '../api/admin'
import Protocols from './Protocols'

const api = vi.mocked(adminApi)

function Where() {
  return <div data-testid="where">{useLocation().pathname}</div>
}

function renderPage() {
  return renderWithProviders(
    <>
      <Routes>
        <Route path="/" element={<Protocols />} />
        <Route path="/test/:p" element={<Where />} />
      </Routes>
    </>
  )
}

describe('Protocols page', () => {
  beforeEach(() => {
    api.getRoutes.mockReset().mockResolvedValue([])
    api.getConfig.mockReset().mockResolvedValue({})
  })

  it('lists all six protocols as not configured by default', async () => {
    renderPage()
    expect(screen.getByRole('heading', { name: 'Protocols' })).toBeInTheDocument()
    for (const n of ['Model Context Protocol', 'Agent-to-Agent', 'HTTP REST API', 'MQTT', 'WebSocket', 'gRPC']) {
      expect(screen.getAllByText(n).length).toBeGreaterThan(0)
    }
    // 6 badges + the "Not configured" stat label
    expect((await screen.findAllByText('Not configured')).length).toBe(7)
  })

  it('marks protocols configured when a route and backend URL exist', async () => {
    api.getRoutes.mockResolvedValue([
      { protocol: 'mcp', path: '/mcp' },
      { protocol: 'a2a', path: '/a2a' },
    ] as any)
    api.getConfig.mockResolvedValue({ mcp_backend_url: 'http://mcp:1' })
    renderPage()
    expect(await screen.findByText('Active')).toBeInTheDocument()
    expect(screen.getByText('Backend not configured')).toBeInTheDocument()
    expect(screen.getAllByText('Not configured').length).toBe(5)
  })

  it('expands a protocol, copies endpoints, and navigates to its test page', async () => {
    const user = userEvent.setup()
    const writeText = vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue()
    api.getRoutes.mockResolvedValue([{ protocol: 'mcp', path: '/mcp' }] as any)
    api.getConfig.mockResolvedValue({ mcp_backend_url: 'http://mcp:1' })
    renderPage()
    await screen.findByText('Active')
    await user.click(screen.getByText('Model Context Protocol'))
    expect(screen.getByText('http://mcp:1')).toBeInTheDocument()
    const copyButtons = screen.getAllByRole('button', { name: /copy/i })
    expect(copyButtons.length).toBeGreaterThan(0)
    await user.click(copyButtons[0])
    await waitFor(() => expect(writeText).toHaveBeenCalled())
    expect(await screen.findByText('Copied to clipboard')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Test MCP' }))
    expect(await screen.findByTestId('where')).toHaveTextContent('/test/mcp')
  })

  it('toasts when copy fails', async () => {
    const user = userEvent.setup()
    vi.spyOn(navigator.clipboard, 'writeText').mockRejectedValue(new Error('no'))
    renderPage()
    await user.click(await screen.findByText('Model Context Protocol'))
    await user.click(screen.getAllByRole('button', { name: /copy/i })[0])
    expect(await screen.findByText('Copy failed')).toBeInTheDocument()
  })

  it('collapses an expanded protocol on second click', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findAllByText('Not configured')
    await user.click(screen.getByText('Model Context Protocol'))
    expect(screen.queryAllByRole('button', { name: /copy/i }).length).toBeGreaterThan(0)
    await user.click(screen.getByText('Model Context Protocol'))
    expect(screen.queryAllByRole('button', { name: /copy/i })).toHaveLength(0)
  })
})
