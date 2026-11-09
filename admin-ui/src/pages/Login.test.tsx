import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router-dom'
import { renderWithProviders } from '../test/utils'
import { fakeJwt } from '../test/jwt'

vi.mock('../api/admin', () => ({ adminApi: { login: vi.fn() } }))
vi.mock('../ida', async (orig) => {
  const actual = await orig<typeof import('../ida')>()
  return { ...actual, idaEnabled: vi.fn(), idaLogin: vi.fn() }
})
import { adminApi } from '../api/admin'
import { idaEnabled, idaLogin } from '../ida'
import Login from './Login'

function renderLogin() {
  return renderWithProviders(
    <Routes>
      <Route path="/" element={<Login />} />
      <Route path="/dashboard" element={<div>DASHBOARD PAGE</div>} />
    </Routes>
  )
}

const fill = async (u: string, p: string) => {
  await userEvent.type(await screen.findByLabelText('Username'), u)
  await userEvent.type(screen.getByLabelText('Password'), p)
}

describe('Login page', () => {
  beforeEach(() => {
    vi.mocked(idaEnabled).mockResolvedValue(false)
    vi.mocked(idaLogin).mockResolvedValue(undefined)
    vi.mocked(adminApi.login).mockReset()
  })

  it('shows a spinner then the native form when SSO is off', async () => {
    renderLogin()
    expect(await screen.findByLabelText('Username')).toBeInTheDocument()
    expect(screen.getByLabelText('Password')).toHaveAttribute('type', 'password')
    expect(screen.queryByText('Continue with SSO')).toBeNull()
  })

  it('validates empty credentials without calling the API', async () => {
    renderLogin()
    await userEvent.click(await screen.findByRole('button', { name: 'Sign in' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Please enter both username and password')
    expect(adminApi.login).not.toHaveBeenCalled()
  })

  it('submits credentials, stores the token and navigates to the dashboard', async () => {
    const token = fakeJwt({ name: 'Ada' })
    vi.mocked(adminApi.login).mockResolvedValue({ access_token: token, token_type: 'bearer', username: 'admin' })
    renderLogin()
    await fill('admin', 'secret-token')
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    await waitFor(() => expect(screen.getByText('DASHBOARD PAGE')).toBeInTheDocument())
    expect(adminApi.login).toHaveBeenCalledWith('admin', 'secret-token')
    expect(localStorage.getItem('admin_token')).toBe(token)
  })

  it('shows the server detail on a 401', async () => {
    vi.mocked(adminApi.login).mockRejectedValue({ response: { status: 401, data: { detail: 'Bad credentials' } } })
    renderLogin()
    await fill('admin', 'wrong')
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Bad credentials')
    expect(localStorage.getItem('admin_token')).toBeNull()
    expect(screen.queryByText('DASHBOARD PAGE')).toBeNull()
  })

  it('falls back to a generic message when the error has no detail', async () => {
    vi.mocked(adminApi.login).mockRejectedValue(new Error('Network Error'))
    renderLogin()
    await fill('admin', 'x')
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid username or password')
  })

  it('rejects a response without an access token', async () => {
    vi.mocked(adminApi.login).mockResolvedValue({ access_token: '', token_type: 'bearer', username: 'a' })
    renderLogin()
    await fill('a', 'b')
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid response from server')
  })

  it('clears a previous error on the next submit', async () => {
    vi.mocked(adminApi.login).mockRejectedValueOnce({ response: { data: { detail: 'nope' } } })
    vi.mocked(adminApi.login).mockResolvedValueOnce({ access_token: 't', token_type: 'b', username: 'u' })
    renderLogin()
    await fill('a', 'b')
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
  })

  it('redirects straight to the dashboard when already authenticated', async () => {
    localStorage.setItem('admin_token', 'existing')
    renderLogin()
    expect(await screen.findByText('DASHBOARD PAGE')).toBeInTheDocument()
  })

  describe('with SSO enabled', () => {
    beforeEach(() => {
    vi.mocked(idaEnabled).mockResolvedValue(true)
  })

    it('shows the SSO button and hides the local form until requested', async () => {
      renderLogin()
      expect(await screen.findByText('Continue with SSO')).toBeInTheDocument()
      expect(screen.queryByLabelText('Username')).toBeNull()
      await userEvent.click(screen.getByText('Use a local password instead'))
      expect(screen.getByLabelText('Username')).toBeInTheDocument()
      expect(screen.getByText('Local administrator')).toBeInTheDocument()
      expect(screen.queryByText('Use a local password instead')).toBeNull()
    })

    it('starts the SSO flow', async () => {
      renderLogin()
      await userEvent.click(await screen.findByRole('button', { name: /Continue with SSO/ }))
      expect(idaLogin).toHaveBeenCalledTimes(1)
    })

    it('shows an error when SSO cannot start, with the local form hidden', async () => {
      vi.mocked(idaLogin).mockRejectedValue(new Error('x'))
      renderLogin()
      await userEvent.click(await screen.findByRole('button', { name: /Continue with SSO/ }))
      expect(await screen.findByRole('alert')).toHaveTextContent('Could not start OIDC SSO sign-in')
      expect(screen.queryByLabelText('Username')).toBeNull()
    })

    it('shows the SSO error once the local form is open too', async () => {
      vi.mocked(idaLogin).mockRejectedValue(new Error('x'))
      renderLogin()
      await userEvent.click(await screen.findByText('Use a local password instead'))
      await userEvent.click(screen.getByRole('button', { name: /Continue with SSO/ }))
      expect(await screen.findAllByRole('alert')).toHaveLength(1)
    })
  })

  it('falls back to the native form if the SSO config check rejects', async () => {
    vi.mocked(idaEnabled).mockRejectedValue(new Error('cfg'))
    renderLogin()
    expect(await screen.findByLabelText('Username')).toBeInTheDocument()
  })
})
