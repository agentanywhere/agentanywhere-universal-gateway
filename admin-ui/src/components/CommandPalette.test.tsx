import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes, useLocation } from 'react-router-dom'
import { renderWithProviders } from '../test/utils'
import { CommandPalette } from './CommandPalette'

const logoutSpy = vi.fn()
vi.mock('../ida', async (orig) => ({ ...(await orig<typeof import('../ida')>()), idaLogout: vi.fn(), isIdaSession: () => false }))

function Where() {
  return <div data-testid="where">{useLocation().pathname}</div>
}

function setup(open = true) {
  const onClose = vi.fn()
  renderWithProviders(
    <>
      <CommandPalette open={open} onClose={onClose} />
      <Routes>
        <Route path="*" element={<Where />} />
      </Routes>
    </>
  )
  return { onClose }
}

describe('CommandPalette', () => {
  beforeEach(() => {
    logoutSpy.mockReset()
    vi.stubGlobal('location', { ...window.location, set href(v: string) { logoutSpy(v) }, get href() { return '' } })
    return () => vi.unstubAllGlobals()
  })

  it('renders nothing when closed', () => {
    setup(false)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('lists grouped commands when open', () => {
    setup()
    expect(screen.getByRole('dialog', { name: 'Command palette' })).toBeInTheDocument()
    for (const g of ['Navigate', 'Test protocols', 'Actions']) expect(screen.getByText(g)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Dashboard/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Test gRPC/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Switch to (Dark|Light) Mode/ })).toBeInTheDocument()
  })

  it('filters by label, hint and keyword and shows an empty message', async () => {
    setup()
    const input = screen.getByLabelText('Command palette search')
    await userEvent.type(input, 'websocket')
    expect(screen.getByRole('button', { name: /Test WebSocket/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Dashboard/ })).toBeNull()
    fireEvent.change(input, { target: { value: 'zzzz' } })
    expect(screen.getByText(/No results for/)).toBeInTheDocument()
  })

  it('navigates and closes when a command is clicked', async () => {
    const { onClose } = setup()
    await userEvent.click(screen.getByRole('button', { name: /API Keys/ }))
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent('/api-keys'))
    expect(onClose).toHaveBeenCalled()
  })

  it('supports arrow-key navigation and Enter to run', () => {
    const { onClose } = setup()
    fireEvent.keyDown(document, { key: 'ArrowDown' })
    fireEvent.keyDown(document, { key: 'ArrowDown' })
    fireEvent.keyDown(document, { key: 'ArrowUp' })
    fireEvent.keyDown(document, { key: 'Enter' })
    expect(screen.getByTestId('where')).toHaveTextContent('/agents')
    expect(onClose).toHaveBeenCalled()
  })

  it('does not move past the first or last item', () => {
    setup()
    fireEvent.keyDown(document, { key: 'ArrowUp' })
    for (let i = 0; i < 40; i++) fireEvent.keyDown(document, { key: 'ArrowDown' })
    fireEvent.keyDown(document, { key: 'Enter' })
    expect(logoutSpy).toHaveBeenCalledWith('/login')
  })

  it('closes on Escape and on backdrop click', async () => {
    const { onClose } = setup()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
    await userEvent.click(screen.getByRole('button', { name: 'Close command palette' }))
    expect(onClose).toHaveBeenCalledTimes(2)
  })

  it('hover moves the active item', async () => {
    setup()
    await userEvent.hover(screen.getByRole('button', { name: /Logs/ }))
    fireEvent.keyDown(document, { key: 'Enter' })
    expect(screen.getByTestId('where')).toHaveTextContent('/logs')
  })

  it('toggles the theme', async () => {
    const { onClose } = setup()
    const before = document.documentElement.className
    await userEvent.click(screen.getByRole('button', { name: /Switch to (Dark|Light) Mode/ }))
    expect(document.documentElement.className).not.toBe(before)
    expect(onClose).toHaveBeenCalled()
  })

  it('signs out', async () => {
    const { onClose } = setup()
    await userEvent.click(screen.getByRole('button', { name: /Sign out/ }))
    expect(onClose).toHaveBeenCalled()
    expect(logoutSpy).toHaveBeenCalledWith('/login')
  })
})
