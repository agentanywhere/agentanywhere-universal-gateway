import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from '../test/utils'

vi.mock('../api/admin', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/admin')>()),
  adminApi: {
    getConfig: vi.fn(),
    getSupportedProtocols: vi.fn(),
    getSettingsProfiles: vi.fn(),
    getAlertThresholds: vi.fn(),
    createSettingsProfile: vi.fn(),
    deleteSettingsProfile: vi.fn(),
    setDefaultProfile: vi.fn(),
    configureEmailNotifications: vi.fn(),
    setEmailRecipients: vi.fn(),
    testEmailNotifications: vi.fn(),
    updateAlertThresholds: vi.fn(),
  },
}))
import { adminApi, flattenConfig } from '../api/admin'
import Settings from './Settings'

const api = vi.mocked(adminApi)

const profiles = [
  { id: 'p1', name: 'Production', description: 'prod cfg', is_default: true },
  { id: 'p2', name: 'Dev', description: '', is_default: false },
]

describe('Settings page', () => {
  beforeEach(() => {
    api.getConfig.mockReset().mockResolvedValue({
      mcp_backend_url: 'http://mcp:1',
      a2a_backend_url: 'http://a2a:1',
      environment: 'staging',
      log_level: 'INFO',
    })
    api.getSupportedProtocols.mockReset().mockResolvedValue([
      { type: 'mcp', name: 'MCP Protocol', description: 'Model context', default_settings: { timeout: 45 } },
      { type: 'mqtt', name: 'MQTT Protocol', description: 'IoT', default_settings: { qos_level: 2 } },
    ])
    api.getSettingsProfiles.mockReset().mockResolvedValue(profiles)
    api.getAlertThresholds.mockReset().mockResolvedValue({
      thresholds: { error_rate_threshold: 0.25 },
      email_rate_limit_per_hour: 99,
    })
    api.createSettingsProfile.mockReset().mockResolvedValue({})
    api.deleteSettingsProfile.mockReset().mockResolvedValue(undefined)
    api.setDefaultProfile.mockReset().mockResolvedValue({})
    api.configureEmailNotifications.mockReset().mockResolvedValue({})
    api.setEmailRecipients.mockReset().mockResolvedValue({})
    api.testEmailNotifications.mockReset().mockResolvedValue({ service: 'email', connected: true })
    api.updateAlertThresholds.mockReset().mockResolvedValue({})
  })

  describe('protocols tab', () => {
    it('shows a skeleton while config/protocols load', () => {
      api.getConfig.mockReturnValue(new Promise(() => {}))
      const { container } = renderWithProviders(<Settings />)
      expect(container.querySelectorAll('.animate-pulse').length).toBeGreaterThan(3)
    })

    it('shows the selected protocol, its defaults and global config', async () => {
      renderWithProviders(<Settings />)
      expect(await screen.findByText('MCP Protocol')).toBeInTheDocument()
      expect(screen.getByText('Model context')).toBeInTheDocument()
      expect(screen.getByLabelText('Timeout (seconds)')).toHaveValue(45)
      expect(screen.getByLabelText('MCP backend URL')).toHaveValue('http://mcp:1')
      expect(screen.getByLabelText('Environment')).toHaveValue('staging')
      expect(screen.queryByLabelText('QoS level')).toBeNull()
    })

    it('fills global config from the sectioned GET /admin/config answer', async () => {
      api.getConfig.mockResolvedValue(
        flattenConfig({
          sections: {
            application: { environment: 'development' },
            backends: { mcp_backend_url: 'http://mcp-backend:8001', a2a_backend_url: 'http://a2a-backend:8002' },
            logging: { log_level: 'DEBUG' },
          },
          safe_reload_fields: ['log_level'],
        })
      )
      renderWithProviders(<Settings />)
      expect(await screen.findByText('MCP Protocol')).toBeInTheDocument()
      expect(screen.getByLabelText('MCP backend URL')).toHaveValue('http://mcp-backend:8001')
      expect(screen.getByLabelText('A2A backend URL')).toHaveValue('http://a2a-backend:8002')
      expect(screen.getByLabelText('Environment')).toHaveValue('development')
      expect(screen.getByLabelText('Log level')).toHaveValue('DEBUG')
    })

    it('switches protocol and reveals protocol-specific fields', async () => {
      renderWithProviders(<Settings />)
      await screen.findByText('MCP Protocol')
      await userEvent.click(screen.getByRole('button', { name: 'MQTT' }))
      expect(await screen.findByText('MQTT Protocol')).toBeInTheDocument()
      expect(screen.getByLabelText('QoS level')).toHaveValue('2')
      expect(screen.getByLabelText('Max connections')).toBeInTheDocument()
      await userEvent.click(screen.getByRole('button', { name: 'HTTP/REST' }))
      expect(screen.getByText('Compression')).toBeInTheDocument()
      expect(screen.getAllByText('Protocol settings').length).toBeGreaterThan(0)
    })

    it('toggles an uncontrolled switch', async () => {
      renderWithProviders(<Settings />)
      await screen.findByText('MCP Protocol')
      const sw = screen.getByRole('switch', { name: 'Circuit breaker' })
      expect(sw).toHaveAttribute('aria-checked', 'false')
      await userEvent.click(sw)
      expect(sw).toHaveAttribute('aria-checked', 'true')
    })
  })

  describe('profiles tab', () => {
    async function openProfiles() {
      renderWithProviders(<Settings />)
      await userEvent.click(await screen.findByRole('tab', { name: /Profiles/ }))
    }

    it('lists profiles with default badge, or an empty state', async () => {
      await openProfiles()
      expect(await screen.findByText('Production')).toBeInTheDocument()
      expect(screen.getByText('Default')).toBeInTheDocument()
      expect(screen.getByText('prod cfg')).toBeInTheDocument()
    })

    it('shows the empty state when there are no profiles', async () => {
      api.getSettingsProfiles.mockResolvedValue([])
      await openProfiles()
      expect(await screen.findByText('No profiles yet')).toBeInTheDocument()
    })

    it('selecting a profile loads it into the form; new profile clears it', async () => {
      await openProfiles()
      await userEvent.click(await screen.findByText('Dev'))
      expect(screen.getByText('Edit profile')).toBeInTheDocument()
      expect(screen.getByLabelText('Profile name *')).toHaveValue('Dev')
      await userEvent.click(screen.getByRole('button', { name: 'New profile' }))
      expect(screen.getByText('New profile', { selector: 'h3' })).toBeInTheDocument()
      expect(screen.getByLabelText('Profile name *')).toHaveValue('')
    })

    it('duplicates a profile into the form', async () => {
      await openProfiles()
      const duplicate = await screen.findAllByRole('button', { name: 'Duplicate profile' })
      await userEvent.click(duplicate[0])
      expect(screen.getByLabelText('Profile name *')).toHaveValue('Production (Copy)')
      expect(screen.getByLabelText('Description')).toHaveValue('prod cfg')
    })

    it('saves a new profile with default protocol settings for all six protocols', async () => {
      await openProfiles()
      const save = await screen.findByRole('button', { name: /Save profile/ })
      expect(save).toBeDisabled()
      await userEvent.type(screen.getByLabelText('Profile name *'), 'Staging')
      await userEvent.type(screen.getByLabelText('Description'), 'stg')
      await userEvent.click(save)
      await waitFor(() => expect(api.createSettingsProfile).toHaveBeenCalled())
      const payload = api.createSettingsProfile.mock.calls[0][0]
      expect(payload).toMatchObject({ name: 'Staging', description: 'stg', is_default: false, global_settings: {} })
      expect(Object.keys(payload.protocol_settings)).toEqual(['mcp', 'a2a', 'http', 'mqtt', 'websocket', 'grpc'])
      expect(await screen.findByText('Profile saved')).toBeInTheDocument()
      expect(screen.getByLabelText('Profile name *')).toHaveValue('')
    })

    it('toasts when saving fails', async () => {
      api.createSettingsProfile.mockRejectedValue(new Error('no'))
      await openProfiles()
      await userEvent.type(await screen.findByLabelText('Profile name *'), 'X')
      await userEvent.click(screen.getByRole('button', { name: /Save profile/ }))
      expect(await screen.findByText("Couldn't save profile")).toBeInTheDocument()
    })

    it('sets a non-default profile as default, and ignores the current default', async () => {
      await openProfiles()
      await userEvent.click(await screen.findByRole('button', { name: 'Set as default' }))
      await waitFor(() => expect(api.setDefaultProfile).toHaveBeenCalled())
      expect(api.setDefaultProfile.mock.calls[0][0]).toBe('p2')
      expect(await screen.findByText('Default profile updated')).toBeInTheDocument()
      await userEvent.click(screen.getByRole('button', { name: 'Default profile' }))
      expect(api.setDefaultProfile).toHaveBeenCalledTimes(1)
    })

    it('confirms before deleting, then deletes', async () => {
      await openProfiles()
      await userEvent.click((await screen.findAllByRole('button', { name: 'Delete profile' }))[0])
      const dialog = screen.getByRole('dialog')
      expect(within(dialog).getByText('Delete profile?')).toBeInTheDocument()
      expect(within(dialog).getByText(/"Production" will be removed permanently/)).toBeInTheDocument()
      expect(api.deleteSettingsProfile).not.toHaveBeenCalled()
      await userEvent.click(within(dialog).getByRole('button', { name: 'Delete profile' }))
      await waitFor(() => expect(api.deleteSettingsProfile).toHaveBeenCalled())
      expect(api.deleteSettingsProfile.mock.calls[0][0]).toBe('p1')
      expect(await screen.findByText('Profile deleted')).toBeInTheDocument()
    })

    it('cancel leaves the profile', async () => {
      await openProfiles()
      await userEvent.click((await screen.findAllByRole('button', { name: 'Delete profile' }))[0])
      await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }))
      expect(screen.queryByRole('dialog')).toBeNull()
      expect(api.deleteSettingsProfile).not.toHaveBeenCalled()
    })
  })

  describe('alerts tab', () => {
    async function openAlerts() {
      renderWithProviders(<Settings />)
      await userEvent.click(await screen.findByRole('tab', { name: /Alerts/ }))
    }

    it('loads thresholds from the API into the form', async () => {
      await openAlerts()
      await waitFor(() => expect(screen.getByLabelText('Error-rate threshold')).toHaveValue(0.25))
      expect(screen.getByLabelText('Max emails / hour')).toHaveValue(99)
    })

    it('saves SMTP config', async () => {
      await openAlerts()
      await userEvent.type(await screen.findByLabelText('SMTP host'), 'smtp.test')
      await userEvent.clear(screen.getByLabelText('SMTP port'))
      await userEvent.type(screen.getByLabelText('SMTP port'), '2525')
      await userEvent.type(screen.getByLabelText('SMTP user'), 'u')
      await userEvent.type(screen.getByLabelText('SMTP password'), 'pw')
      await userEvent.type(screen.getByLabelText('From email'), 'a@b.co')
      await userEvent.click(screen.getByRole('switch', { name: 'Use TLS' }))
      await userEvent.click(screen.getByRole('button', { name: /Save SMTP/ }))
      await waitFor(() => expect(api.configureEmailNotifications).toHaveBeenCalled())
      expect(api.configureEmailNotifications.mock.calls[0][0]).toEqual({
        smtp_host: 'smtp.test',
        smtp_port: 2525,
        smtp_user: 'u',
        smtp_password: 'pw',
        smtp_use_tls: false,
        from_email: 'a@b.co',
      })
      expect(await screen.findByText('Email config saved')).toBeInTheDocument()
    })

    it('toasts when SMTP save fails', async () => {
      api.configureEmailNotifications.mockRejectedValue(new Error('x'))
      await openAlerts()
      await userEvent.click(await screen.findByRole('button', { name: /Save SMTP/ }))
      expect(await screen.findByText('Failed to save email config')).toBeInTheDocument()
    })

    it('parses recipients from a comma-separated list', async () => {
      await openAlerts()
      await userEvent.type(await screen.findByLabelText('Recipients (comma separated)'), 'a@x.io, ,b@x.io')
      await userEvent.click(screen.getByRole('button', { name: /Save recipients/ }))
      await waitFor(() => expect(api.setEmailRecipients).toHaveBeenCalled())
      expect(api.setEmailRecipients.mock.calls[0][0]).toEqual(['a@x.io', 'b@x.io'])
      expect(await screen.findByText('Recipients updated')).toBeInTheDocument()
    })

    it.each([
      [true, 'Email service is working'],
      [false, 'Email connection failed'],
    ])('email test (connected=%s)', async (connected, title) => {
      api.testEmailNotifications.mockResolvedValue({ service: 'email', connected })
      await openAlerts()
      await userEvent.click(await screen.findByRole('button', { name: /Send test email/ }))
      expect(await screen.findByText(title)).toBeInTheDocument()
    })

    it('toasts when the email test errors', async () => {
      api.testEmailNotifications.mockRejectedValue(new Error('x'))
      await openAlerts()
      await userEvent.click(await screen.findByRole('button', { name: /Send test email/ }))
      expect(await screen.findByText('Email test failed')).toBeInTheDocument()
    })

    it('edits and saves thresholds', async () => {
      await openAlerts()
      await waitFor(() => expect(screen.getByLabelText('Max emails / hour')).toHaveValue(99))
      await userEvent.click(screen.getByRole('switch', { name: 'Rate-limit alerts' }))
      await userEvent.click(screen.getByRole('switch', { name: 'Circuit-breaker alerts' }))
      for (const [label, value] of [
        ['Anomaly score threshold', '0.5'],
        ['Error-rate threshold', '0.3'],
        ['Traffic-spike threshold (×)', '3'],
        ['Cooldown (seconds)', '60'],
        ['Max emails / hour', '10'],
      ] as const) {
        const el = screen.getByLabelText(label)
        await userEvent.clear(el)
        await userEvent.type(el, value)
      }
      await userEvent.click(screen.getByRole('button', { name: /Save thresholds/ }))
      await waitFor(() => expect(api.updateAlertThresholds).toHaveBeenCalled())
      expect(api.updateAlertThresholds.mock.calls[0][0]).toEqual({
        rate_limit_enabled: false,
        circuit_breaker_enabled: false,
        anomaly_score_threshold: 0.5,
        error_rate_threshold: 0.3,
        traffic_spike_threshold: 3,
        min_interval_seconds: 60,
        email_rate_limit_per_hour: 10,
      })
      expect(await screen.findByText('Thresholds updated')).toBeInTheDocument()
    })
  })
})
