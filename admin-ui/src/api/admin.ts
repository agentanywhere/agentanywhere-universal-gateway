import apiClient from './client'

export interface DashboardStats {
  total_requests: number
  total_errors: number
  avg_response_time: number
  active_connections: number
  rate_limit_hits: number
  circuit_breaker_states: Record<string, string>
  requests_per_minute: number
  error_rate: number
}

export interface APIKey {
  key: string
  name: string
  rate_limit_per_minute: number
  rate_limit_per_hour: number
  is_active: boolean
  created_at: string
  last_used?: string
}

export interface Route {
  path: string
  backend_url: string
  protocol: string
  timeout: number
  requires_auth: boolean
}

export interface LogEntry {
  request_id: string
  method: string
  path: string
  client_ip: string
  status_code: number
  response_time_ms: number
  timestamp: string
  error?: string
}

export const adminApi = {
  getDashboardStats: async (): Promise<DashboardStats> => {
    const response = await apiClient.get('/admin/dashboard/stats')
    return response.data
  },

  getAPIKeys: async (): Promise<APIKey[]> => {
    const response = await apiClient.get('/admin/api-keys')
    return response.data
  },

  createAPIKey: async (data: {
    name: string
    rate_limit_per_minute?: number
    rate_limit_per_hour?: number
  }): Promise<APIKey> => {
    const response = await apiClient.post('/admin/api-keys', null, {
      params: data,
    })
    return response.data
  },

  deleteAPIKey: async (keyId: string): Promise<void> => {
    await apiClient.delete(`/admin/api-keys/${keyId}`)
  },

  toggleAPIKey: async (keyId: string): Promise<void> => {
    await apiClient.put(`/admin/api-keys/${keyId}/toggle`)
  },

  getRoutes: async (): Promise<Route[]> => {
    const response = await apiClient.get('/admin/routes')
    return response.data
  },

  createRoute: async (route: Route): Promise<Route> => {
    const response = await apiClient.post('/admin/routes', route)
    return response.data
  },

  getLogs: async (params: {
    limit?: number
    offset?: number
    level?: string
    path?: string
  }): Promise<{ logs: LogEntry[]; total: number }> => {
    const response = await apiClient.get('/admin/logs', { params })
    return response.data
  },

  getDetailedHealth: async () => {
    const response = await apiClient.get('/admin/health/detailed')
    return response.data
  },

  getConfig: async () => {
    const response = await apiClient.get('/admin/config')
    return response.data
  },

  getAIInsights: async () => {
    const response = await apiClient.get('/admin/ai/insights')
    return response.data
  },

  getAnomalies: async (windowMinutes: number = 60) => {
    const response = await apiClient.get('/admin/ai/anomalies', {
      params: { window_minutes: windowMinutes },
    })
    return response.data
  },

  getScalingRecommendations: async () => {
    const response = await apiClient.get('/admin/ai/scaling-recommendations')
    return response.data
  },

  getCostAnalysis: async (windowHours: number = 24) => {
    const response = await apiClient.get('/admin/ai/cost-analysis', {
      params: { window_hours: windowHours },
    })
    return response.data
  },

  getTrafficForecast: async (hoursAhead: number = 24) => {
    const response = await apiClient.get('/admin/ai/traffic-forecast', {
      params: { hours_ahead: hoursAhead },
    })
    return response.data
  },

  trainAIModels: async () => {
    const response = await apiClient.post('/admin/ai/train')
    return response.data
  },

  // Settings Profiles
  getSettingsProfiles: async (): Promise<any[]> => {
    const response = await apiClient.get('/admin/settings/profiles')
    return response.data
  },

  getSettingsProfile: async (profileId: string): Promise<any> => {
    const response = await apiClient.get(`/admin/settings/profiles/${profileId}`)
    return response.data
  },

  createSettingsProfile: async (profile: any): Promise<any> => {
    const response = await apiClient.post('/admin/settings/profiles', profile)
    return response.data
  },

  updateSettingsProfile: async (profileId: string, profile: any): Promise<any> => {
    const response = await apiClient.put(`/admin/settings/profiles/${profileId}`, profile)
    return response.data
  },

  deleteSettingsProfile: async (profileId: string): Promise<void> => {
    await apiClient.delete(`/admin/settings/profiles/${profileId}`)
  },

  setDefaultProfile: async (profileId: string): Promise<any> => {
    const response = await apiClient.post(`/admin/settings/profiles/${profileId}/set-default`)
    return response.data
  },

  getSupportedProtocols: async (): Promise<any[]> => {
    const response = await apiClient.get('/admin/settings/protocols')
    return response.data
  },

  // Email Notifications
  configureEmailNotifications: async (config: {
    smtp_host: string
    smtp_port?: number
    smtp_user?: string
    smtp_password?: string
    smtp_use_tls?: boolean
    from_email?: string
  }): Promise<any> => {
    const response = await apiClient.post('/admin/notifications/email/configure', config)
    return response.data
  },

  setEmailRecipients: async (emails: string[]): Promise<any> => {
    const response = await apiClient.post('/admin/notifications/email/recipients', emails)
    return response.data
  },

  testEmailNotifications: async (): Promise<{ service: string; connected: boolean }> => {
    const response = await apiClient.post('/admin/notifications/email/test')
    return response.data
  },

  // Alert Thresholds
  getAlertThresholds: async (): Promise<any> => {
    const response = await apiClient.get('/admin/notifications/alert-thresholds')
    return response.data
  },

  updateAlertThresholds: async (thresholds: {
    rate_limit_enabled?: boolean
    circuit_breaker_enabled?: boolean
    anomaly_score_threshold?: number
    error_rate_threshold?: number
    traffic_spike_threshold?: number
    min_interval_seconds?: number
    email_rate_limit_per_hour?: number
  }): Promise<any> => {
    const response = await apiClient.post('/admin/notifications/alert-thresholds', thresholds)
    return response.data
  },

  // Authentication
  login: async (username: string, password: string): Promise<{ access_token: string; token_type: string; username: string }> => {
    const response = await apiClient.post('/admin/auth/login', {
      username,
      password,
    })
    return response.data
  },
}

