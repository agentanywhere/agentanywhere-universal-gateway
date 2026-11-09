import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from '../test/utils'

vi.mock('../api/admin', () => ({
  adminApi: {
    getAIInsights: vi.fn(),
    getAnomalies: vi.fn(),
    getScalingRecommendations: vi.fn(),
    getCostAnalysis: vi.fn(),
    getTrafficForecast: vi.fn(),
  },
}))
import { adminApi } from '../api/admin'
import AIInsights from './AIInsights'

const api = vi.mocked(adminApi)

describe('AIInsights page', () => {
  beforeEach(() => {
    api.getAIInsights.mockReset().mockResolvedValue({})
    api.getAnomalies.mockReset().mockResolvedValue({})
    api.getScalingRecommendations.mockReset().mockResolvedValue(null)
    api.getCostAnalysis.mockReset().mockResolvedValue(null)
    api.getTrafficForecast.mockReset().mockResolvedValue({})
  })

  it('shows loading skeletons for anomalies initially', () => {
    api.getAnomalies.mockReturnValue(new Promise(() => {}))
    const { container } = renderWithProviders(<AIInsights />)
    expect(screen.getByRole('heading', { name: 'AI Insights' })).toBeInTheDocument()
    expect(container.querySelectorAll('.animate-pulse').length).toBeGreaterThanOrEqual(3)
  })

  it('renders defaults and the empty forecast when the backend returns nothing', async () => {
    renderWithProviders(<AIInsights />)
    expect(await screen.findByText('Forecast not available yet')).toBeInTheDocument()
    expect(await screen.findByText('0.0% anomaly rate')).toBeInTheDocument()
    expect(screen.queryByText('Auto-scaling recommendations')).toBeNull()
    expect(screen.queryByText('Cost optimization')).toBeNull()
  })

  it('renders anomaly stats and recent anomalies', async () => {
    api.getAnomalies.mockResolvedValue({
      total_requests: 1000,
      anomalies: 80,
      anomaly_rate: 0.08,
      top_anomalous_paths: [{ path: '/api/weird' }],
      recent_anomalies: [{ path: '/api/weird', score: 0.912 }],
    })
    renderWithProviders(<AIInsights />)
    expect(await screen.findByText('8.0% anomaly rate')).toBeInTheDocument()
    expect(screen.getAllByText('/api/weird').length).toBeGreaterThan(0)
    expect(screen.getByText('Score 0.91')).toBeInTheDocument()
    expect(screen.getByText('Recent anomalies')).toBeInTheDocument()
  })

  it.each([
    ['scale_up', 'SCALE UP', -1.5],
    ['scale_down', 'SCALE DOWN', -2],
    ['hold', 'HOLD', 3],
  ])('renders the %s scaling recommendation', async (action, label, delta) => {
    api.getScalingRecommendations.mockResolvedValue({
      action,
      reason: 'because load',
      current_utilization: 0.7,
      current_instances: 3,
      recommended_instances: 4,
      confidence: 0.9,
      current_cost_per_hour: 1.5,
      cost_change_per_hour: delta,
      estimated_savings_per_month: delta < 0 ? 100 : 0,
    })
    renderWithProviders(<AIInsights />)
    expect(await screen.findByText(label)).toBeInTheDocument()
    expect(screen.getByText('because load')).toBeInTheDocument()
    expect(screen.getByText('70.0%')).toBeInTheDocument()
    expect(screen.getByText(delta < 0 ? 'Potential savings' : 'Additional cost')).toBeInTheDocument()
  })

  it('renders cost analysis with recommendations', async () => {
    api.getCostAnalysis.mockResolvedValue({
      analysis: { total_cost: 12.3456, estimated_monthly_cost: 400, potential_monthly_savings: 50 },
      recommendations: [
        { type: 'right_size', priority: 'high', actions: ['Shrink pool'], potential_savings: 25 },
        { priority: 'low' },
      ],
    })
    renderWithProviders(<AIInsights />)
    expect(await screen.findByText('$12.35')).toBeInTheDocument()
    expect(screen.getByText('right size')).toBeInTheDocument()
    expect(screen.getByText('Shrink pool')).toBeInTheDocument()
    expect(screen.getByText('Recommendation')).toBeInTheDocument()
    expect(screen.getByText('$25.00')).toBeInTheDocument()
  })

  it('renders cost analysis defaults when fields are missing', async () => {
    api.getCostAnalysis.mockResolvedValue({})
    renderWithProviders(<AIInsights />)
    expect((await screen.findAllByText('$0.00')).length).toBe(3)
  })

  it('renders the traffic forecast chart and capacity needs', async () => {
    api.getTrafficForecast.mockResolvedValue({
      forecast: [
        { timestamp: '2025-01-01T00:00:00Z', predicted_requests_per_hour: 10, confidence_lower: 5, confidence_upper: 15 },
        { timestamp: '2025-01-01T01:00:00Z', predicted_requests_per_hour: 12, confidence_lower: 6, confidence_upper: 18 },
      ],
      capacity_needs: { recommended_capacity: 5, recommendation: 'add capacity' },
    })
    renderWithProviders(<AIInsights />)
    expect(await screen.findByText('5 instances suggested')).toBeInTheDocument()
    expect(screen.getByText('add capacity')).toBeInTheDocument()
    expect(screen.queryByText('Forecast not available yet')).toBeNull()
  })

  it('renders intelligent rate limiting stats', async () => {
    api.getAIInsights.mockResolvedValue({ intelligent_rate_limiting: { active_clients: 12, adaptive_limits_active: 3 } })
    renderWithProviders(<AIInsights />)
    expect(await screen.findByText('Intelligent rate limiting')).toBeInTheDocument()
    expect(screen.getByText('12')).toBeInTheDocument()
  })

  it('colours the anomaly badge danger above 10%', async () => {
    api.getAnomalies.mockResolvedValue({ anomaly_rate: 0.5 })
    renderWithProviders(<AIInsights />)
    expect((await screen.findByText('50.0% anomaly rate')).className).toContain('red')
  })
})
