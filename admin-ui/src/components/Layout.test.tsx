import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router-dom'
import { renderWithProviders } from '../test/utils'
import { fakeJwt } from '../test/jwt'
import Layout from './Layout'

vi.mock('../ida', async (orig) => ({ ...(await orig<typeof import('../ida')>()), idaLogout: vi.fn(), isIdaSession: () => false }))

function setDesktop(desktop: boolean) {
  window.matchMedia = ((q: string) => ({
    matches: q.includes('min-width: 1024px') ? desktop : false,
    media: q,
    addEventListener() {},
    removeEventListener() {},
  })) as unknown as typeof window.matchMedia
}

function renderLayout(route = '/dashboard') {
  return renderWithProviders(
    <Routes>
      <Route
        path="*"
        element={
          <Layout>
            <div>PAGE BODY</div>
          </Layout>
        }
      />
    </Routes>,
    { route }
  )
}

describe('Layout', () => {
  let hrefSet: string | undefined
  beforeEach(() => {
    hrefSet = undefined
    setDesktop(true)
    vi.stubGlobal('location', { ...window.location, set href(v: string) { hrefSet = v }, get href() { return '' } })
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders nav groups, children and the page title', () => {
    renderLayout('/agents')
    for (const g of ['Operate', 'Observe', 'Configure']) expect(screen.getByText(g)).toBeInTheDocument()
    expect(screen.getByText('PAGE BODY')).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: 'Agents' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Skip to content' })).toHaveAttribute('href', '#main')
  })

  it('marks the current route and falls back to "Admin" for unknown paths', () => {
    renderLayout('/routes')
    expect(screen.getByRole('link', { name: 'Routes' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('link', { name: 'Dashboard' })).not.toHaveAttribute('aria-current')
  })

  it('shows "Admin" for routes without a title', () => {
    renderLayout('/test/mcp')
    expect(screen.getByRole('heading', { level: 2, name: 'Admin' })).toBeInTheDocument()
  })

  it('shows the signed-in user (name and email)', () => {
    localStorage.setItem('admin_token', fakeJwt({ name: 'Ada', email: 'ada@x.io' }))
    renderLayout()
    expect(screen.getByText('Ada')).toBeInTheDocument()
    expect(screen.getByText('ada@x.io')).toBeInTheDocument()
  })

  it('toggles the theme from the sidebar', async () => {
    renderLayout()
    const btn = screen.getByRole('button', { name: /(Dark|Light) mode/ })
    const before = btn.textContent
    await userEvent.click(btn)
    expect(screen.getByRole('button', { name: /(Dark|Light) mode/ }).textContent).not.toBe(before)
  })

  it('signs out', async () => {
    localStorage.setItem('admin_token', 't')
    renderLayout()
    await userEvent.click(screen.getByRole('button', { name: /Sign out/ }))
    expect(hrefSet).toBe('/login')
    expect(localStorage.getItem('admin_token')).toBeNull()
  })

  it('opens the command palette from the header and with Ctrl+K, and closes with Escape', async () => {
    renderLayout()
    await userEvent.click(screen.getAllByRole('button', { name: 'Open command palette' })[0])
    expect(screen.getByRole('dialog', { name: 'Command palette' })).toBeInTheDocument()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    fireEvent.keyDown(window, { key: 'k', ctrlKey: true })
    expect(screen.getByRole('dialog', { name: 'Command palette' })).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'k', metaKey: true })
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  describe('mobile', () => {
    beforeEach(() => setDesktop(false))

    it('has a hidden drawer that opens, locks scroll, and closes via backdrop and Escape', async () => {
      renderLayout()
      expect(screen.getAllByRole('complementary', { hidden: true }).length).toBe(2)
      await userEvent.click(screen.getByRole('button', { name: 'Open navigation' }))
      expect(document.body.style.overflow).toBe('hidden')
      const close = screen.getByRole('button', { name: 'Close menu' })
      expect(close).toHaveAttribute('aria-hidden', 'false')
      await userEvent.click(close)
      expect(document.body.style.overflow).toBe('')
      await userEvent.click(screen.getByRole('button', { name: 'Open navigation' }))
      fireEvent.keyDown(window, { key: 'Escape' })
      expect(document.body.style.overflow).toBe('')
    })

    it('closes the drawer after navigating', async () => {
      renderLayout()
      await userEvent.click(screen.getByRole('button', { name: 'Open navigation' }))
      const drawer = screen.getAllByRole('complementary', { hidden: true })[1]
      await userEvent.click(within(drawer).getByRole('link', { name: 'Logs' }))
      expect(await screen.findByRole('heading', { level: 2, name: 'Logs' })).toBeInTheDocument()
      await waitFor(() => expect(document.body.style.overflow).toBe(''))
    })
  })
})
