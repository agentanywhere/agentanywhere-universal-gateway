import apiClient from './client'

export interface Agent {
  slug: string
  upstream_base: string
  audience: string | null
  effective_audience: string
  public_paths: string[]
  status: 'published' | 'hidden'
  display_name: string | null
  description: string | null
  runtime_mode: 'dedicated' | 'shared'
  data_residency: string
  enabled: boolean
  created_at: string | null
  updated_at: string | null
  updated_by: string | null
  live: boolean
  public_url: string
}

export interface AgentInput {
  slug: string
  upstream_base: string
  audience?: string | null
  public_paths?: string[]
  status?: 'published' | 'hidden'
  display_name?: string | null
  description?: string | null
  runtime_mode?: 'dedicated' | 'shared'
  data_residency?: string
  enabled?: boolean
}

export interface HealthProbe {
  reachable: boolean
  status_code?: number
  latency_ms: number
  target: string
  error?: string
}

export interface BoardAgent {
  slug: string
  upstream_base: string | null
  status: 'up' | 'down' | 'unknown'
  latency_ms: number | null
  status_code: number | null
  error: string | null
  last_checked: number | null
  consecutive_failures: number
  uptime_pct: number | null
  sparkline: number[]
  req_per_min: number
  error_pct: number
  total_requests: number
  avg_latency_ms: number | null
}

export interface AgentBoard {
  generated_at: number
  summary: {
    total: number
    up: number
    down: number
    unknown: number
    req_per_min: number
  }
  agents: BoardAgent[]
}

const apiBase = () => (import.meta.env.BASE_URL || '/').replace(/\/$/, '')

export const agentsApi = {
  list: async (): Promise<Agent[]> => {
    const { data } = await apiClient.get('/admin/agents')
    return data
  },

  healthSnapshot: async (): Promise<AgentBoard> => {
    const { data } = await apiClient.get('/admin/agents/health')
    return data
  },

  /** SSE endpoint URL with the admin token embedded (EventSource can't set headers). */
  streamUrl: (): string => {
    const token = localStorage.getItem('admin_token') || ''
    return `${apiBase()}/admin/agents/stream?token=${encodeURIComponent(token)}`
  },

  create: async (input: AgentInput): Promise<Agent> => {
    const { data } = await apiClient.post('/admin/agents', input)
    return data
  },

  update: async (slug: string, input: Partial<AgentInput>): Promise<Agent> => {
    const { data } = await apiClient.put(`/admin/agents/${encodeURIComponent(slug)}`, input)
    return data
  },

  remove: async (slug: string): Promise<void> => {
    await apiClient.delete(`/admin/agents/${encodeURIComponent(slug)}`)
  },

  probe: async (slug: string, path = '/'): Promise<HealthProbe> => {
    const { data } = await apiClient.post(
      `/admin/agents/${encodeURIComponent(slug)}/health`,
      { path }
    )
    return data
  },

  reload: async (): Promise<{ reloaded: boolean; live_routes: number }> => {
    const { data } = await apiClient.post('/admin/agents/reload')
    return data
  },

  export: async (): Promise<{
    AGENT_ROUTES_JSON: Record<string, string>
    AGENT_PUBLIC_PATHS_JSON: Record<string, string[]>
    count: number
  }> => {
    const { data } = await apiClient.get('/admin/agents/export')
    return data
  },
}
