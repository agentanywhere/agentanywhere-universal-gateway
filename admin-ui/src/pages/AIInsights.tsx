import { useQuery } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Brain,
  DollarSign,
  Lightbulb,
  Zap,
} from 'lucide-react'
import {
  Area,
  AreaChart,
  CartesianGrid,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { adminApi } from '../api/admin'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/Card'
import { PageHeader } from '../components/ui/PageHeader'
import { Badge } from '../components/ui/Badge'
import { Stat } from '../components/ui/Stat'
import { Skeleton } from '../components/ui/Skeleton'
import { EmptyState } from '../components/ui/EmptyState'
import { formatNumber, formatPercent } from '../lib/utils'

const tooltipStyles = {
  contentStyle: {
    backgroundColor: 'hsl(var(--popover))',
    border: '1px solid hsl(var(--border))',
    borderRadius: '8px',
    color: 'hsl(var(--foreground))',
    fontSize: 12,
  },
  labelStyle: { color: 'hsl(var(--foreground))', fontWeight: 500 },
  itemStyle: { color: 'hsl(var(--muted-foreground))' },
}

export default function AIInsights() {
  const { data: insights } = useQuery({
    queryKey: ['ai-insights'],
    queryFn: () => adminApi.getAIInsights(),
    refetchInterval: 10000,
  })

  const { data: anomalies, isLoading: anomaliesLoading } = useQuery({
    queryKey: ['anomalies'],
    queryFn: () => adminApi.getAnomalies(60),
    refetchInterval: 5000,
  })

  const { data: scaling } = useQuery({
    queryKey: ['scaling-recommendations'],
    queryFn: () => adminApi.getScalingRecommendations(),
    refetchInterval: 30000,
  })

  const { data: costAnalysis } = useQuery({
    queryKey: ['cost-analysis'],
    queryFn: () => adminApi.getCostAnalysis(24),
    refetchInterval: 60000,
  })

  const { data: forecast } = useQuery({
    queryKey: ['traffic-forecast'],
    queryFn: () => adminApi.getTrafficForecast(24),
    refetchInterval: 300000,
  })

  const forecastData =
    forecast?.forecast?.map((f: any) => ({
      time: new Date(f.timestamp).toLocaleTimeString('en-US', {
        hour: '2-digit',
        minute: '2-digit',
      }),
      predicted: f.predicted_requests_per_hour,
      lower: f.confidence_lower,
      upper: f.confidence_upper,
    })) || []

  const anomalyRate = anomalies?.anomaly_rate ?? 0
  const anomalyTone =
    anomalyRate > 0.1 ? ('danger' as const) : anomalyRate > 0.05 ? ('warning' as const) : ('success' as const)

  return (
    <div className="space-y-6 sm:space-y-8">
      <PageHeader
        title="AI Insights"
        description="Anomaly detection, scaling, cost, and traffic forecasting powered by built-in ML."
        actions={
          <Badge variant="primary" dot>
            <Brain className="h-3 w-3" />
            ML active
          </Badge>
        }
      />

      <Card>
        <CardHeader className="flex flex-row items-start justify-between space-y-0">
          <div className="space-y-1">
            <CardTitle className="flex items-center gap-2">
              <AlertTriangle className="h-5 w-5 text-red-500" />
              Anomaly detection
            </CardTitle>
            <CardDescription>Last 60 minutes of traffic.</CardDescription>
          </div>
          <Badge variant={anomalyTone} dot>
            {formatPercent(anomalyRate, 1)} anomaly rate
          </Badge>
        </CardHeader>
        <CardContent>
          {anomaliesLoading ? (
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
              <Skeleton className="h-24" />
              <Skeleton className="h-24" />
              <Skeleton className="h-24" />
            </div>
          ) : (
            <>
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
                <Stat
                  label="Total requests"
                  value={formatNumber(anomalies?.total_requests ?? 0)}
                  icon={<Activity className="h-5 w-5" />}
                  tone="info"
                />
                <Stat
                  label="Anomalies detected"
                  value={formatNumber(anomalies?.anomalies ?? 0)}
                  icon={<AlertTriangle className="h-5 w-5" />}
                  tone={anomalyTone}
                />
                <Stat
                  label="Top anomalous path"
                  value={
                    <span className="truncate font-mono text-base">
                      {anomalies?.top_anomalous_paths?.[0]?.path || '—'}
                    </span>
                  }
                  icon={<Zap className="h-5 w-5" />}
                  tone="warning"
                />
              </div>

              {anomalies?.recent_anomalies && anomalies.recent_anomalies.length > 0 ? (
                <div className="mt-5">
                  <div className="mb-2 text-sm font-medium text-foreground">Recent anomalies</div>
                  <div className="space-y-1.5">
                    {anomalies.recent_anomalies.slice(0, 5).map((a: any, idx: number) => (
                      <div
                        key={idx}
                        className="flex items-center justify-between gap-2 rounded-lg border border-red-500/20 bg-red-500/5 px-3 py-2 text-sm"
                      >
                        <code className="truncate font-mono text-xs text-foreground">{a.path}</code>
                        <Badge variant="danger">Score {a.score.toFixed(2)}</Badge>
                      </div>
                    ))}
                  </div>
                </div>
              ) : null}
            </>
          )}
        </CardContent>
      </Card>

      {scaling && (
        <Card>
          <CardHeader className="flex flex-row items-start justify-between space-y-0">
            <div className="space-y-1">
              <CardTitle className="flex items-center gap-2">
                <Zap className="h-5 w-5 text-amber-500" />
                Auto-scaling recommendations
              </CardTitle>
              <CardDescription>{scaling.reason}</CardDescription>
            </div>
            <Badge
              variant={
                scaling.action === 'scale_up'
                  ? 'info'
                  : scaling.action === 'scale_down'
                  ? 'success'
                  : 'muted'
              }
            >
              {String(scaling.action).replace('_', ' ').toUpperCase()}
            </Badge>
          </CardHeader>
          <CardContent className="space-y-5">
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <Stat
                label="Current utilization"
                value={formatPercent(scaling.current_utilization, 1)}
                icon={<Activity className="h-5 w-5" />}
                tone="primary"
              />
              <Stat
                label="Current instances"
                value={formatNumber(scaling.current_instances)}
                tone="info"
                icon={<BarChart3 className="h-5 w-5" />}
              />
              <Stat
                label="Recommended"
                value={formatNumber(scaling.recommended_instances)}
                tone="success"
                icon={<Brain className="h-5 w-5" />}
              />
              <Stat
                label="Confidence"
                value={formatPercent(scaling.confidence, 0)}
                tone="warning"
                icon={<Lightbulb className="h-5 w-5" />}
              />
            </div>

            {scaling.cost_change_per_hour !== undefined && (
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <div className="rounded-lg border border-border bg-card/60 p-4">
                  <div className="text-sm text-muted-foreground">Current cost / hour</div>
                  <div className="mt-1 text-xl font-semibold tabular-nums">
                    ${scaling.current_cost_per_hour?.toFixed(2) ?? '0.00'}
                  </div>
                </div>
                <div
                  className={`rounded-lg border p-4 ${
                    scaling.cost_change_per_hour < 0
                      ? 'border-emerald-500/30 bg-emerald-500/5'
                      : 'border-red-500/30 bg-red-500/5'
                  }`}
                >
                  <div className="text-sm text-muted-foreground">
                    {scaling.cost_change_per_hour < 0 ? 'Potential savings' : 'Additional cost'}
                  </div>
                  <div
                    className={`mt-1 text-xl font-semibold tabular-nums ${
                      scaling.cost_change_per_hour < 0 ? 'text-emerald-500' : 'text-red-500'
                    }`}
                  >
                    ${Math.abs(scaling.cost_change_per_hour).toFixed(2)}/hr
                  </div>
                  {scaling.estimated_savings_per_month != null && scaling.estimated_savings_per_month > 0 && (
                    <div className="mt-1 text-xs text-muted-foreground">
                      ${scaling.estimated_savings_per_month.toFixed(2)}/month
                    </div>
                  )}
                </div>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {costAnalysis && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <DollarSign className="h-5 w-5 text-emerald-500" />
              Cost optimization
            </CardTitle>
            <CardDescription>24-hour window.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-5">
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
              <Stat
                label="Total cost (24h)"
                value={`$${costAnalysis.analysis?.total_cost?.toFixed(2) ?? '0.00'}`}
                tone="info"
                icon={<DollarSign className="h-5 w-5" />}
              />
              <Stat
                label="Estimated monthly"
                value={`$${costAnalysis.analysis?.estimated_monthly_cost?.toFixed(2) ?? '0.00'}`}
                tone="primary"
                icon={<BarChart3 className="h-5 w-5" />}
              />
              <Stat
                label="Potential savings"
                value={`$${costAnalysis.analysis?.potential_monthly_savings?.toFixed(2) ?? '0.00'}`}
                tone="success"
                icon={<Lightbulb className="h-5 w-5" />}
              />
            </div>

            {costAnalysis.recommendations && costAnalysis.recommendations.length > 0 && (
              <div>
                <div className="mb-2 text-sm font-medium text-foreground">Recommendations</div>
                <div className="space-y-2">
                  {costAnalysis.recommendations.slice(0, 5).map((rec: any, idx: number) => (
                    <div key={idx} className="rounded-lg border border-border bg-card/40 p-4">
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-2 text-sm font-medium text-foreground">
                            <Lightbulb className="h-4 w-4 text-amber-500" />
                            <span className="capitalize">
                              {rec.type?.replace('_', ' ') || 'Recommendation'}
                            </span>
                            <Badge
                              variant={rec.priority === 'high' ? 'danger' : 'warning'}
                              className="ml-1"
                            >
                              {rec.priority}
                            </Badge>
                          </div>
                          {rec.actions && rec.actions.length > 0 && (
                            <ul className="ml-6 mt-2 list-disc space-y-1 text-sm text-muted-foreground">
                              {rec.actions.map((action: string, i: number) => (
                                <li key={i}>{action}</li>
                              ))}
                            </ul>
                          )}
                        </div>
                        {rec.potential_savings != null && (
                          <div className="text-right">
                            <div className="text-xs text-muted-foreground">Potential savings</div>
                            <div className="text-base font-semibold text-emerald-500 tabular-nums">
                              ${rec.potential_savings.toFixed(2)}
                            </div>
                          </div>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <BarChart3 className="h-5 w-5 text-primary" />
            Traffic forecast (next 24h)
          </CardTitle>
          <CardDescription>
            Predicted requests per hour with confidence bounds.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {forecastData.length === 0 ? (
            <EmptyState
              icon={<BarChart3 className="h-5 w-5" />}
              title="Forecast not available yet"
              description="The model needs more historical data before it can produce a 24h forecast."
            />
          ) : (
            <ResponsiveContainer width="100%" height={300}>
              <AreaChart data={forecastData} margin={{ left: -10, right: 10 }}>
                <defs>
                  <linearGradient id="forecast" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="hsl(var(--primary))" stopOpacity={0.8} />
                    <stop offset="95%" stopColor="hsl(var(--primary))" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" opacity={0.4} />
                <XAxis dataKey="time" stroke="hsl(var(--muted-foreground))" tick={{ fontSize: 11 }} tickLine={false} />
                <YAxis stroke="hsl(var(--muted-foreground))" tick={{ fontSize: 11 }} width={42} tickLine={false} />
                <Tooltip {...tooltipStyles} />
                <Area
                  type="monotone"
                  dataKey="predicted"
                  stroke="hsl(var(--primary))"
                  strokeWidth={2}
                  fill="url(#forecast)"
                  name="Predicted"
                />
                <Line type="monotone" dataKey="lower" stroke="hsl(var(--primary))" strokeOpacity={0.5} strokeDasharray="4 4" name="Lower" dot={false} />
                <Line type="monotone" dataKey="upper" stroke="hsl(var(--primary))" strokeOpacity={0.5} strokeDasharray="4 4" name="Upper" dot={false} />
              </AreaChart>
            </ResponsiveContainer>
          )}

          {forecast?.capacity_needs && (
            <div className="mt-4 flex items-center justify-between rounded-lg border border-primary/20 bg-primary/5 p-3 text-sm">
              <div>
                <div className="font-medium text-foreground">Capacity recommendation</div>
                <div className="text-muted-foreground">
                  {forecast.capacity_needs.recommended_capacity} instances suggested
                </div>
              </div>
              <Badge variant="primary">{forecast.capacity_needs.recommendation}</Badge>
            </div>
          )}
        </CardContent>
      </Card>

      {insights?.intelligent_rate_limiting && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Activity className="h-5 w-5 text-sky-500" />
              Intelligent rate limiting
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <Stat
                label="Active clients"
                value={formatNumber(insights.intelligent_rate_limiting.active_clients ?? 0)}
                icon={<Activity className="h-5 w-5" />}
                tone="info"
              />
              <Stat
                label="Adaptive limits active"
                value={formatNumber(insights.intelligent_rate_limiting.adaptive_limits_active ?? 0)}
                icon={<Brain className="h-5 w-5" />}
                tone="primary"
              />
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
