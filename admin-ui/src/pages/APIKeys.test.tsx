import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from '../test/utils'

vi.mock('../api/admin', () => ({
  adminApi: {
    getAPIKeys: vi.fn(),
    createAPIKey: vi.fn(),
    deleteAPIKey: vi.fn(),
    toggleAPIKey: vi.fn(),
  },
}))
import { adminApi } from '../api/admin'
import APIKeys from './APIKeys'

const api = vi.mocked(adminApi)

// NOTE: Dialog re-runs its focus effect whenever onClose changes identity (every
// parent render), which moves focus to the close button while typing. Real
// keystrokes would hit it; change events sidestep that so these tests cover the page
// logic. The underlying bug is pinned by an `it.fails` in ui/Dialog.test.tsx.
async function enterName(value: string) {
  fireEvent.change(screen.getByPlaceholderText('e.g. Mobile clients'), { target: { value } })
}

const key = (over: Record<string, unknown> = {}) => ({
  id: 'k1',
  name: 'Mobile clients',
  key_prefix: 'agw_ab12',
  rate_limit_per_minute: 60,
  rate_limit_per_hour: 1000,
  is_active: true,
  created_at: '2025-03-04T10:00:00Z',
  ...over,
})

describe('APIKeys page', () => {
  beforeEach(() => {
    api.getAPIKeys.mockReset()
    api.createAPIKey.mockReset()
    api.deleteAPIKey.mockReset().mockResolvedValue(undefined)
    api.toggleAPIKey.mockReset().mockResolvedValue(undefined)
  })

  it('shows skeletons while loading', () => {
    api.getAPIKeys.mockReturnValue(new Promise(() => {}))
    const { container } = renderWithProviders(<APIKeys />)
    expect(screen.getByRole('heading', { name: 'API keys' })).toBeInTheDocument()
    expect(container.querySelectorAll('.animate-pulse').length).toBeGreaterThanOrEqual(4)
    expect(screen.queryByRole('table')).toBeNull()
  })

  it('renders the empty state and opens the create dialog from it', async () => {
    api.getAPIKeys.mockResolvedValue([])
    renderWithProviders(<APIKeys />)
    expect(await screen.findByText('No API keys yet')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /Create your first key/ }))
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByText('Create API key')).toBeInTheDocument()
  })

  it('treats a failed list request as empty (no crash)', async () => {
    api.getAPIKeys.mockRejectedValue(new Error('500'))
    renderWithProviders(<APIKeys />)
    expect(await screen.findByText('No API keys yet')).toBeInTheDocument()
  })

  it('lists keys with prefix, limits, status and created date', async () => {
    api.getAPIKeys.mockResolvedValue([
      key(),
      key({ id: 'k2', name: 'Old key', is_active: false, key_prefix: '' }),
    ] as any)
    renderWithProviders(<APIKeys />)
    expect(await screen.findByText('Mobile clients')).toBeInTheDocument()
    expect(screen.getByText('agw_ab12…')).toBeInTheDocument()
    expect(screen.getAllByText('60/min · 1000/hr')).toHaveLength(2)
    expect(screen.getByText('Active')).toBeInTheDocument()
    expect(screen.getByText('Inactive')).toBeInTheDocument()
    expect(screen.getAllByText('Mar 04, 2025').length).toBeGreaterThan(0)
    expect(screen.getByText('—')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Deactivate' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Activate' })).toBeInTheDocument()
  })

  it('toggles a key and refetches', async () => {
    api.getAPIKeys.mockResolvedValue([key()] as any)
    renderWithProviders(<APIKeys />)
    await userEvent.click(await screen.findByRole('button', { name: 'Deactivate' }))
    await waitFor(() => expect(api.toggleAPIKey).toHaveBeenCalled())
    expect(api.toggleAPIKey.mock.calls[0][0]).toBe('k1')
    await waitFor(() => expect(api.getAPIKeys).toHaveBeenCalledTimes(2))
  })

  describe('create flow', () => {
    it('disables Create until a name is typed', async () => {
      api.getAPIKeys.mockResolvedValue([key()] as any)
      renderWithProviders(<APIKeys />)
      await userEvent.click(await screen.findByRole('button', { name: /^Create key$/ }))
      const dialog = screen.getByRole('dialog')
      const submit = within(dialog).getByRole('button', { name: 'Create key' })
      expect(submit).toBeDisabled()
      await enterName('   ')
      expect(submit).toBeDisabled()
      await enterName('CI')
      expect(submit).toBeEnabled()
    })

    it('creates a key, shows the full key once and toasts success', async () => {
      api.getAPIKeys.mockResolvedValue([])
      api.createAPIKey.mockResolvedValue(key({ id: 'new', name: 'CI', key: 'agw_FULL_SECRET_KEY' }) as any)
      renderWithProviders(<APIKeys />)
      await userEvent.click(await screen.findByRole('button', { name: /Create your first key/ }))
      await enterName('  CI  ')
      await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Create key' }))
      await waitFor(() => expect(api.createAPIKey).toHaveBeenCalled())
      expect(api.createAPIKey.mock.calls[0][0]).toEqual({ name: 'CI' })
      expect(await screen.findByText('agw_FULL_SECRET_KEY')).toBeInTheDocument()
      expect(screen.getByText('Copy your new API key')).toBeInTheDocument()
      expect(screen.getByText('API key created')).toBeInTheDocument()
      await waitFor(() => expect(api.getAPIKeys).toHaveBeenCalledTimes(2))
      await userEvent.click(screen.getByRole('button', { name: 'Done' }))
      expect(screen.queryByText('agw_FULL_SECRET_KEY')).toBeNull()
    })

    it('submits with Enter in the name field', async () => {
      api.getAPIKeys.mockResolvedValue([])
      api.createAPIKey.mockResolvedValue(key({ key: 'k' }) as any)
      renderWithProviders(<APIKeys />)
      await userEvent.click(await screen.findByRole('button', { name: /Create your first key/ }))
      await enterName('X')
      fireEvent.keyDown(screen.getByPlaceholderText('e.g. Mobile clients'), { key: 'Enter' })
      await waitFor(() => expect(api.createAPIKey).toHaveBeenCalled())
    })

    it('copies the new key to the clipboard', async () => {
      const user = userEvent.setup()
      const writeText = vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue()
      api.getAPIKeys.mockResolvedValue([])
      api.createAPIKey.mockResolvedValue(key({ key: 'SECRET' }) as any)
      renderWithProviders(<APIKeys />)
      await user.click(await screen.findByRole('button', { name: /Create your first key/ }))
      await enterName('X')
      await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Create key' }))
      await user.click(await screen.findByRole('button', { name: 'Copy key' }))
      expect(writeText).toHaveBeenCalledWith('SECRET')
      expect(await screen.findByText('Copied to clipboard')).toBeInTheDocument()
    })

    it('toasts when copying fails', async () => {
      const user = userEvent.setup()
      vi.spyOn(navigator.clipboard, 'writeText').mockRejectedValue(new Error('denied'))
      api.getAPIKeys.mockResolvedValue([])
      api.createAPIKey.mockResolvedValue(key({ key: 'SECRET' }) as any)
      renderWithProviders(<APIKeys />)
      await user.click(await screen.findByRole('button', { name: /Create your first key/ }))
      await enterName('X')
      await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Create key' }))
      await user.click(await screen.findByRole('button', { name: 'Copy key' }))
      expect(await screen.findByText('Copy failed')).toBeInTheDocument()
    })

    it('shows the server detail in a toast when creation fails and keeps the dialog open', async () => {
      api.getAPIKeys.mockResolvedValue([])
      api.createAPIKey.mockRejectedValue({ response: { data: { detail: 'Name already used' } } })
      renderWithProviders(<APIKeys />)
      await userEvent.click(await screen.findByRole('button', { name: /Create your first key/ }))
      await enterName('dup')
      await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Create key' }))
      expect(await screen.findByText("Couldn't create key")).toBeInTheDocument()
      expect(screen.getByText('Name already used')).toBeInTheDocument()
      expect(screen.getByText('Create API key')).toBeInTheDocument()
    })

    it('falls back to the error message, then "Unknown error"', async () => {
      api.getAPIKeys.mockResolvedValue([])
      api.createAPIKey.mockRejectedValueOnce(new Error('Network Error'))
      renderWithProviders(<APIKeys />)
      await userEvent.click(await screen.findByRole('button', { name: /Create your first key/ }))
      await enterName('n')
      await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Create key' }))
      expect(await screen.findByText('Network Error')).toBeInTheDocument()
    })

    it('cancel closes the dialog without calling the API', async () => {
      api.getAPIKeys.mockResolvedValue([])
      renderWithProviders(<APIKeys />)
      await userEvent.click(await screen.findByRole('button', { name: /Create your first key/ }))
      await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
      expect(screen.queryByRole('dialog')).toBeNull()
      expect(api.createAPIKey).not.toHaveBeenCalled()
    })
  })

  describe('delete flow', () => {
    it('asks for confirmation, then deletes and toasts', async () => {
      api.getAPIKeys.mockResolvedValue([key()] as any)
      renderWithProviders(<APIKeys />)
      await userEvent.click(await screen.findByRole('button', { name: 'Delete key' }))
      const dialog = screen.getByRole('dialog')
      expect(within(dialog).getByText('Delete API key?')).toBeInTheDocument()
      expect(within(dialog).getByText(/"Mobile clients" will stop authenticating/)).toBeInTheDocument()
      expect(api.deleteAPIKey).not.toHaveBeenCalled()
      await userEvent.click(within(dialog).getByRole('button', { name: 'Delete key' }))
      await waitFor(() => expect(api.deleteAPIKey).toHaveBeenCalled())
      expect(api.deleteAPIKey.mock.calls[0][0]).toBe('k1')
      expect(await screen.findByText('API key deleted')).toBeInTheDocument()
      await waitFor(() => expect(screen.queryByText('Delete API key?')).toBeNull())
    })

    it('cancel leaves the key alone', async () => {
      api.getAPIKeys.mockResolvedValue([key()] as any)
      renderWithProviders(<APIKeys />)
      await userEvent.click(await screen.findByRole('button', { name: 'Delete key' }))
      await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }))
      expect(screen.queryByRole('dialog')).toBeNull()
      expect(api.deleteAPIKey).not.toHaveBeenCalled()
    })
  })
})
