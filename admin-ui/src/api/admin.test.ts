import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('./client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))
import apiClient from './client'
import { adminApi, configString, flattenConfig } from './admin'

const c = apiClient as unknown as Record<'get' | 'post' | 'put' | 'delete', ReturnType<typeof vi.fn>>

describe('adminApi', () => {
  beforeEach(() => {
    for (const fn of Object.values(c)) fn.mockReset().mockResolvedValue({ data: { ok: 1 } })
  })

  it('getDashboardStats returns response data', async () => {
    c.get.mockResolvedValue({ data: { total_requests: 5 } })
    expect(await adminApi.getDashboardStats()).toEqual({ total_requests: 5 })
    expect(c.get).toHaveBeenCalledWith('/admin/dashboard/stats')
  })

  it('API key CRUD hits the right endpoints', async () => {
    await adminApi.getAPIKeys()
    expect(c.get).toHaveBeenCalledWith('/admin/api-keys')
    await adminApi.createAPIKey({ name: 'k', rate_limit_per_minute: 5 })
    expect(c.post).toHaveBeenCalledWith('/admin/api-keys', null, { params: { name: 'k', rate_limit_per_minute: 5 } })
    await adminApi.deleteAPIKey('id1')
    expect(c.delete).toHaveBeenCalledWith('/admin/api-keys/id1')
    await adminApi.toggleAPIKey('id1')
    expect(c.put).toHaveBeenCalledWith('/admin/api-keys/id1/toggle')
  })

  it('routes and logs', async () => {
    await adminApi.getRoutes()
    expect(c.get).toHaveBeenCalledWith('/admin/routes')
    const route = { path: '/a', backend_url: 'http://b', protocol: 'http', timeout: 5, requires_auth: true }
    await adminApi.createRoute(route)
    expect(c.post).toHaveBeenCalledWith('/admin/routes', route)
    await adminApi.getLogs({ limit: 10, level: 'error' })
    expect(c.get).toHaveBeenCalledWith('/admin/logs', { params: { limit: 10, level: 'error' } })
  })

  it('health and config', async () => {
    await adminApi.getDetailedHealth()
    expect(c.get).toHaveBeenCalledWith('/admin/health/detailed')
    await adminApi.getConfig()
    expect(c.get).toHaveBeenCalledWith('/admin/config')
  })

  it('getConfig flattens the sections GET /admin/config answers with', async () => {
    c.get.mockResolvedValue({
      data: {
        sections: {
          application: { environment: 'production' },
          backends: { mcp_backend_url: 'http://mcp:8001', http_backend_url: null },
          logging: { log_level: 'INFO' },
        },
        safe_reload_fields: ['log_level'],
      },
    })
    expect(await adminApi.getConfig()).toEqual({
      environment: 'production',
      mcp_backend_url: 'http://mcp:8001',
      http_backend_url: null,
      log_level: 'INFO',
    })
  })

  it('flattenConfig handles a single section, a flat object and junk', () => {
    // GET /admin/config?section=backends answers with that section only.
    expect(flattenConfig({ backends: { mcp_backend_url: 'http://m' } })).toEqual({
      backends: { mcp_backend_url: 'http://m' },
    })
    expect(flattenConfig({ mcp_backend_url: 'http://m' })).toEqual({ mcp_backend_url: 'http://m' })
    expect(flattenConfig(null)).toEqual({})
    expect(flattenConfig('x')).toEqual({})
    expect(flattenConfig({ sections: { a: null, b: { x: 1 } } })).toEqual({ x: 1 })
  })

  it('configString returns text or an empty string', () => {
    expect(configString({ a: 'v', b: null, c: 3 }, 'a')).toBe('v')
    expect(configString({ a: 'v', b: null, c: 3 }, 'b')).toBe('')
    expect(configString({ a: 'v', b: null, c: 3 }, 'c')).toBe('')
    expect(configString(undefined, 'a')).toBe('')
  })

  it('AI endpoints pass windowed params with defaults', async () => {
    await adminApi.getAIInsights()
    expect(c.get).toHaveBeenCalledWith('/admin/ai/insights')
    await adminApi.getAnomalies()
    expect(c.get).toHaveBeenCalledWith('/admin/ai/anomalies', { params: { window_minutes: 60 } })
    await adminApi.getAnomalies(15)
    expect(c.get).toHaveBeenLastCalledWith('/admin/ai/anomalies', { params: { window_minutes: 15 } })
    await adminApi.getScalingRecommendations()
    expect(c.get).toHaveBeenCalledWith('/admin/ai/scaling-recommendations')
    await adminApi.getCostAnalysis()
    expect(c.get).toHaveBeenCalledWith('/admin/ai/cost-analysis', { params: { window_hours: 24 } })
    await adminApi.getTrafficForecast(6)
    expect(c.get).toHaveBeenCalledWith('/admin/ai/traffic-forecast', { params: { hours_ahead: 6 } })
    await adminApi.trainAIModels()
    expect(c.post).toHaveBeenCalledWith('/admin/ai/train')
  })

  it('settings profiles', async () => {
    await adminApi.getSettingsProfiles()
    expect(c.get).toHaveBeenCalledWith('/admin/settings/profiles')
    await adminApi.getSettingsProfile('p')
    expect(c.get).toHaveBeenCalledWith('/admin/settings/profiles/p')
    await adminApi.createSettingsProfile({ name: 'n' })
    expect(c.post).toHaveBeenCalledWith('/admin/settings/profiles', { name: 'n' })
    await adminApi.updateSettingsProfile('p', { name: 'm' })
    expect(c.put).toHaveBeenCalledWith('/admin/settings/profiles/p', { name: 'm' })
    await adminApi.deleteSettingsProfile('p')
    expect(c.delete).toHaveBeenCalledWith('/admin/settings/profiles/p')
    await adminApi.setDefaultProfile('p')
    expect(c.post).toHaveBeenCalledWith('/admin/settings/profiles/p/set-default')
    await adminApi.getSupportedProtocols()
    expect(c.get).toHaveBeenCalledWith('/admin/settings/protocols')
  })

  it('notifications', async () => {
    await adminApi.configureEmailNotifications({ smtp_host: 'h' })
    expect(c.post).toHaveBeenCalledWith('/admin/notifications/email/configure', { smtp_host: 'h' })
    await adminApi.setEmailRecipients(['a@b.c'])
    expect(c.post).toHaveBeenCalledWith('/admin/notifications/email/recipients', ['a@b.c'])
    await adminApi.testEmailNotifications()
    expect(c.post).toHaveBeenCalledWith('/admin/notifications/email/test')
    await adminApi.getAlertThresholds()
    expect(c.get).toHaveBeenCalledWith('/admin/notifications/alert-thresholds')
    await adminApi.updateAlertThresholds({ error_rate_threshold: 0.1 })
    expect(c.post).toHaveBeenCalledWith('/admin/notifications/alert-thresholds', { error_rate_threshold: 0.1 })
  })

  it('login posts credentials and returns the token payload', async () => {
    c.post.mockResolvedValue({ data: { access_token: 't', token_type: 'bearer', username: 'u' } })
    expect(await adminApi.login('u', 'p')).toEqual({ access_token: 't', token_type: 'bearer', username: 'u' })
    expect(c.post).toHaveBeenCalledWith('/admin/auth/login', { username: 'u', password: 'p' })
  })

  it('propagates errors from the client', async () => {
    c.get.mockRejectedValue(new Error('boom'))
    await expect(adminApi.getRoutes()).rejects.toThrow('boom')
  })
})
