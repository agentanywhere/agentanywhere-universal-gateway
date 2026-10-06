import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Activity,
  AlertCircle,
  Clock,
  Code,
  Globe,
  MessageSquare,
  Radio,
  Shield,
  TrendingUp,
  Wifi,
  Zap,
} from 'lucide-react'
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { adminApi } from '../api/admin'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/Card'
import { PageHeader } from '../components/ui/PageHeader'
import { Stat } from '../components/ui/Stat'
import { Badge } from '../components/ui/Badge'
import { Skeleton } from '../components/ui/Skeleton'
import { EmptyState } from '../components/ui/EmptyState'
import { useTimeSeries } from '../hooks/useTimeSeries'
import { formatMs, formatNumber, formatPercent } from '../lib/utils'

const PROTOCOL_META = [
  { id: 'mcp', name: 'MCP', icon: Zap },
  { id: 'a2a', name: 'A2A', icon: MessageSquare },
  { id: 'http', name: 'HTTP', icon: Globe },
  { id: 'mqtt', name: 'MQTT', icon: Radio },
  { id: 'websocket', name: 'WS', icon: Wifi },
  { id: 'grpc', name: 'gRPC', icon: Code },
]

function chartTooltipStyles() {
  return {
    contentStyle: {
      backgroundColor: 'hsl(var(--popover))',
      border: '1px solid hsl(var(--border))',
      borderRadius: '8px',
      color: 'hsl(var(--foreground))',
      fontSize: 12,
    },
    labelStyle: { color: 'hsl(var(--foreground))', fontWeight: 600 },
    itemStyle: { color: 'hsl(var(--muted-foreground))' },
  }
}

export default function Dashboard() {
  const {
    data: stats,
    isLoading: statsLoading,
    isError: statsError,
    refetch: refetchStats,
  } = useQuery({
    queryKey: ['dashboard-stats'],
    queryFn: adminApi.getDashboardStats,
    refetchInterval: 5000,
  })

  const { data: health, isLoading: healthLoading } = useQuery({
    queryKey: ['health'],
    queryFn: adminApi.getDetailedHealth,
    refetchInterval: 10000,
  })

  const { data: routes } = useQuery({
    queryKey: ['routes'],
    queryFn: adminApi.getRoutes,
  })

  const { series } = useTimeSeries(stats)

  const protocolStatus = useMemo(() => {
    const set = new Set<string>(routes?.map((r) => r.protocol) ?? [])
    return PROTOCOL_META.map((p) => ({ ...p, configured: set.has(p.id) }))
  }, [routes])

  const errorRate = stats?.error_rate ?? 0
  const avgLatency = stats?.avg_response_time ?? 0
  const requestsPerMin = stats?.requests_per_minute ?? 0

  const liveDelta = useMemo(() => {
    if (series.length < 2) return null
    const half = Math.ceil(series.length / 2)
    const recent = series.slice(half).reduce((s, p) => s + p.requests, 0)
    const earlier = series.slice(0, half).reduce((s, p) => s + p.requests, 0)
    if (earlier === 0) return null
    return ((recent - earlier) / earlier) * 100
  }, [series])

  const tooltip = chartTooltipStyles()

  return (
    <div className="space-y-6 sm:space-y-8">
      <PageHeader
        title="Dashboard"
        description="Live gateway throughput, latency, and dependent service health."
        actions={
          statsError ? (
            <Badge variant="danger" dot>
              Stats unavailable
            </Badge>
          ) : (
            <Badge variant="success" dot>
              Live · 5s
            </Badge>
          )
        }
      />

      {statsError && (
        <div className="flex items-start gap-3 rounded-xl border border-red-500/30 bg-red-500/5 p-4 text-sm">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-red-500" />
          <div className="min-w-0 flex-1">
            <div className="font-medium text-foreground">Couldn&apos;t load gateway metrics</div>
            <div className="text-muted-foreground">
              The <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">/admin/dashboard/stats</code>{' '}
              endpoint returned an error.
            </div>
          </div>
          <button
            onClick={() => refetchStats()}
            className="shrink-0 rounded-md border border-border bg-card px-2.5 py-1 text-xs font-medium text-foreground transition-colors hover:bg-accent"
          >
            Retry
          </button>
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-3 xl:grid-cols-6">
        <Stat
          icon={<Activity className="h-4 w-4" />}
          tone="info"
          label="Requests"
          value={formatNumber(stats?.total_requests)}
          isLoading={statsLoading}
          hint="Lifetime"
        />
        <Stat
          icon={<AlertCircle className="h-4 w-4" />}
          tone="danger"
          label="Errors"
          value={formatNumber(stats?.total_errors)}
          isLoading={statsLoading}
        />
        <Stat
          icon={<Clock className="h-4 w-4" />}
          tone="success"
          label="Avg latency"
          value={formatMs(avgLatency, 2)}
          isLoading={statsLoading}
        />
        <Stat
          icon={<TrendingUp className="h-4 w-4" />}
          tone="primary"
          label="Req / min"
          value={formatNumber(requestsPerMin)}
          isLoading={statsLoading}
          trend={liveDelta != null ? { delta: liveDelta, label: 'vs first half of window' } : undefined}
        />
        <Stat
          icon={<Zap className="h-4 w-4" />}
          tone="warning"
          label="Rate limits"
          value={formatNumber(stats?.rate_limit_hits)}
          isLoading={statsLoading}
        />
        <Stat
          icon={<Shield className="h-4 w-4" />}
          tone={errorRate > 0.05 ? 'danger' : errorRate > 0.01 ? 'warning' : 'success'}
          label="Error rate"
          value={formatPercent(errorRate, 2)}
          isLoading={statsLoading}
        />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-5 lg:gap-5">
        <Card className="lg:col-span-3">
          <CardHeader className="flex-row items-start justify-between space-y-0">
            <div className="space-y-1">
              <CardTitle>Request flow</CardTitle>
              <CardDescription>Requests per polling interval</CardDescription>
            </div>
            <Badge variant="muted" className="font-mono">
              {series.length} pts
            </Badge>
          </CardHeader>
          <CardContent className="pb-5 sm:pb-6">
            {statsLoading && series.length === 0 ? (
              <Skeleton className="h-[280px] w-full" />
            ) : series.length < 2 ? (
              <div className="flex h-[280px] items-center justify-center rounded-lg border border-dashed border-border text-sm text-muted-foreground">
                Sampling… points appear after a couple of refreshes.
              </div>
            ) : (
              <ResponsiveContainer width="100%" height={280}>
                <AreaChart data={series} margin={{ top: 10, right: 10, left: -10, bottom: 0 }}>
                  <defs>
                    <linearGradient id="reqArea" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="hsl(var(--primary))" stopOpacity={0.28} />
                      <stop offset="95%" stopColor="hsl(var(--primary))" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" opacity={0.45} vertical={false} />
                  <XAxis
                    dataKey="t"
                    tickFormatter={(t) =>
                      new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
                    }
                    stroke="hsl(var(--muted-foreground))"
                    tick={{ fontSize: 11 }}
                    tickLine={false}
                    axisLine={false}
                  />
                  <YAxis
                    stroke="hsl(var(--muted-foreground))"
                    tick={{ fontSize: 11 }}
                    tickLine={false}
                    axisLine={false}
                    width={36}
                    allowDecimals={false}
                  />
                  <Tooltip
                    {...tooltip}
                    labelFormatter={(t) => new Date(t as number).toLocaleTimeString()}
                  />
                  <Area
                    type="monotone"
                    dataKey="requests"
                    stroke="hsl(var(--primary))"
                    strokeWidth={2}
                    fill="url(#reqArea)"
                    name="Requests"
                    isAnimationActive={false}
                  />
                </AreaChart>
              </ResponsiveContainer>
            )}
          </CardContent>
        </Card>

        <Card className="lg:col-span-2">
          <CardHeader className="flex-row items-start justify-between space-y-0">
            <div className="space-y-1">
              <CardTitle>Errors</CardTitle>
              <CardDescription>Per refresh tick</CardDescription>
            </div>
            <Badge variant={errorRate > 0.05 ? 'danger' : errorRate > 0.01 ? 'warning' : 'success'}>
              {formatPercent(errorRate, 2)}
            </Badge>
          </CardHeader>
          <CardContent className="pb-5 sm:pb-6">
            {statsLoading && series.length === 0 ? (
              <Skeleton className="h-[280px] w-full" />
            ) : series.length < 2 ? (
              <div className="flex h-[280px] items-center justify-center rounded-lg border border-dashed border-border text-sm text-muted-foreground">
                Sampling…
              </div>
            ) : (
              <ResponsiveContainer width="100%" height={280}>
                <BarChart data={series} margin={{ top: 10, right: 10, left: -10, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" opacity={0.45} vertical={false} />
                  <XAxis
                    dataKey="t"
                    tickFormatter={(t) =>
                      new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
                    }
                    stroke="hsl(var(--muted-foreground))"
                    tick={{ fontSize: 11 }}
                    tickLine={false}
                    axisLine={false}
                  />
                  <YAxis
                    stroke="hsl(var(--muted-foreground))"
                    tick={{ fontSize: 11 }}
                    tickLine={false}
                    axisLine={false}
                    width={36}
                    allowDecimals={false}
                  />
                  <Tooltip
                    {...tooltip}
                    labelFormatter={(t) => new Date(t as number).toLocaleTimeString()}
                  />
                  <Bar
                    dataKey="errors"
                    fill="hsl(var(--destructive))"
                    radius={[4, 4, 0, 0]}
                    name="Errors"
                    isAnimationActive={false}
                  />
                </BarChart>
              </ResponsiveContainer>
            )}
          </CardContent>
        </Card>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2 lg:gap-5">
        <Card>
          <CardHeader>
            <CardTitle>Protocol coverage</CardTitle>
            <CardDescription>Backends inferred from active routing rules</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="divide-y divide-border/70 rounded-lg border border-border/80">
              {protocolStatus.map((p) => {
                const Icon = p.icon
                return (
                  <div
                    key={p.id}
                    className="flex items-center justify-between gap-3 px-3.5 py-3 first:rounded-t-lg last:rounded-b-lg"
                  >
                    <div className="flex min-w-0 items-center gap-2.5">
                      <Icon className="h-4 w-4 shrink-0 text-muted-foreground" />
                      <span className="font-mono text-sm font-medium text-foreground">{p.name}</span>
                    </div>
                    {p.configured ? (
                      <Badge variant="success" dot>
                        Active
                      </Badge>
                    ) : (
                      <Badge variant="muted">Idle</Badge>
                    )}
                  </div>
                )
              })}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Service health</CardTitle>
            <CardDescription>
              From <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">/admin/health/detailed</code>
            </CardDescription>
          </CardHeader>
          <CardContent>
            {healthLoading ? (
              <div className="space-y-2">
                {Array.from({ length: 4 }).map((_, i) => (
                  <Skeleton key={i} className="h-12 w-full" />
                ))}
              </div>
            ) : health?.services && Object.keys(health.services).length > 0 ? (
              <div className="divide-y divide-border/70 rounded-lg border border-border/80">
                {Object.entries(health.services).map(([service, status]) => {
                  const s = status as { status?: string; message?: string }
                  const healthy = s.status === 'healthy'
                  return (
                    <div
                      key={service}
                      className="flex items-center justify-between gap-3 px-3.5 py-3 first:rounded-t-lg last:rounded-b-lg"
                    >
                      <div className="min-w-0">
                        <div className="text-sm font-medium capitalize text-foreground">
                          {service.replace(/_/g, ' ')}
                        </div>
                        {s.message && (
                          <div className="truncate text-xs text-muted-foreground">{s.message}</div>
                        )}
                      </div>
                      <Badge variant={healthy ? 'success' : 'danger'} dot>
                        {s.status ?? 'unknown'}
                      </Badge>
                    </div>
                  )
                })}
              </div>
            ) : (
              <EmptyState
                icon={<Shield className="h-5 w-5" />}
                title="No services reported"
                description="The detailed health endpoint didn't return any dependent services."
              />
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
