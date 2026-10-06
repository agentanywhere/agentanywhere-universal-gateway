import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Globe,
  Info,
  TrendingDown,
  TrendingUp,
} from 'lucide-react'
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  PolarAngleAxis,
  PolarGrid,
  PolarRadiusAxis,
  Radar,
  RadarChart,
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
import { Table, Tbody, Td, Th, Thead, Tr } from '../components/ui/Table'
import { formatMs, formatNumber, formatPercent } from '../lib/utils'
import { useTimeSeries } from '../hooks/useTimeSeries'

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

const PROTOCOL_DEMO = [
  { name: 'MCP', value: 35, color: '#0ea5e9' },
  { name: 'A2A', value: 20, color: '#10b981' },
  { name: 'HTTP/REST', value: 25, color: '#8b5cf6' },
  { name: 'MQTT', value: 12, color: '#f59e0b' },
  { name: 'WebSocket', value: 5, color: '#ec4899' },
  { name: 'gRPC', value: 3, color: '#6366f1' },
]

const STATUS_DEMO = [
  { name: '2xx', value: 85, count: 8500, color: '#10b981' },
  { name: '3xx', value: 5, count: 500, color: '#3b82f6' },
  { name: '4xx', value: 7, count: 700, color: '#f59e0b' },
  { name: '5xx', value: 3, count: 300, color: '#ef4444' },
]

const ENDPOINTS_DEMO = [
  { name: '/mcp/query', requests: 4523, errors: 23, avgLatency: 145 },
  { name: '/mcp/context', requests: 3210, errors: 12, avgLatency: 98 },
  { name: '/a2a/message', requests: 2890, errors: 45, avgLatency: 234 },
  { name: '/a2a/discover', requests: 1876, errors: 8, avgLatency: 67 },
  { name: '/mcp/stream', requests: 1654, errors: 34, avgLatency: 312 },
]

const RADAR_DEMO = [
  { subject: 'Availability', score: 99.9, fullMark: 100 },
  { subject: 'Performance', score: 95, fullMark: 100 },
  { subject: 'Reliability', score: 98, fullMark: 100 },
  { subject: 'Security', score: 100, fullMark: 100 },
  { subject: 'Scalability', score: 92, fullMark: 100 },
]

function DemoBanner() {
  return (
    <div className="flex items-start gap-2.5 rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-700 dark:text-amber-300">
      <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <span>
        Sample data — backend doesn&apos;t yet expose this aggregation. Live numbers will replace this once
        the analytics endpoint ships.
      </span>
    </div>
  )
}

export default function Analytics() {
  const { data: stats, isLoading } = useQuery({
    queryKey: ['analytics-stats'],
    queryFn: adminApi.getDashboardStats,
    refetchInterval: 10000,
  })

  const { series } = useTimeSeries(stats)

  const performanceMetrics = useMemo(() => {
    return [
      {
        label: 'Throughput',
        value: stats ? `${formatNumber(stats.requests_per_minute)}/min` : '—',
        icon: <TrendingUp className="h-5 w-5" />,
        tone: 'primary' as const,
      },
      {
        label: 'Avg latency',
        value: stats ? formatMs(stats.avg_response_time, 2) : '—',
        icon: <Activity className="h-5 w-5" />,
        tone: 'success' as const,
      },
      {
        label: 'Error rate',
        value: stats ? formatPercent(stats.error_rate, 2) : '—',
        icon: <AlertTriangle className="h-5 w-5" />,
        tone: stats && stats.error_rate > 0.05 ? ('danger' as const) : ('warning' as const),
      },
      {
        label: 'Total errors',
        value: stats ? formatNumber(stats.total_errors) : '—',
        icon: <TrendingDown className="h-5 w-5" />,
        tone: 'danger' as const,
      },
    ]
  }, [stats])

  return (
    <div className="space-y-6 sm:space-y-8">
      <PageHeader
        title="Analytics"
        description="Performance trends across the gateway. Numbers refresh every 10 seconds."
        actions={<Badge variant="info">Live + sample mix</Badge>}
      />

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {performanceMetrics.map((m) => (
          <Stat
            key={m.label}
            label={m.label}
            value={m.value}
            icon={m.icon}
            tone={m.tone}
            isLoading={isLoading}
          />
        ))}
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2 lg:gap-6">
        <Card>
          <CardHeader>
            <CardTitle>Live request volume</CardTitle>
            <CardDescription>Requests per polling tick (real data).</CardDescription>
          </CardHeader>
          <CardContent>
            {series.length < 2 ? (
              <div className="flex h-[260px] items-center justify-center rounded-lg border border-dashed border-border text-sm text-muted-foreground">
                Sampling…
              </div>
            ) : (
              <ResponsiveContainer width="100%" height={260}>
                <AreaChart data={series} margin={{ top: 10, right: 10, left: -10, bottom: 0 }}>
                  <defs>
                    <linearGradient id="vol" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="hsl(var(--primary))" stopOpacity={0.35} />
                      <stop offset="95%" stopColor="hsl(var(--primary))" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" opacity={0.4} />
                  <XAxis
                    dataKey="t"
                    tickFormatter={(t) =>
                      new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
                    }
                    stroke="hsl(var(--muted-foreground))"
                    tick={{ fontSize: 11 }}
                    tickLine={false}
                  />
                  <YAxis
                    stroke="hsl(var(--muted-foreground))"
                    tick={{ fontSize: 11 }}
                    width={36}
                    tickLine={false}
                    allowDecimals={false}
                  />
                  <Tooltip
                    {...tooltipStyles}
                    labelFormatter={(t) => new Date(t as number).toLocaleTimeString()}
                  />
                  <Area
                    type="monotone"
                    dataKey="requests"
                    stroke="hsl(var(--primary))"
                    fill="url(#vol)"
                    strokeWidth={2}
                    name="Requests"
                    isAnimationActive={false}
                  />
                </AreaChart>
              </ResponsiveContainer>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-start justify-between space-y-0">
            <div className="space-y-1">
              <CardTitle>Protocol distribution</CardTitle>
              <CardDescription>
                <Globe className="mr-1 inline h-3.5 w-3.5" /> Share of traffic by protocol.
              </CardDescription>
            </div>
            <Badge variant="muted">Demo</Badge>
          </CardHeader>
          <CardContent className="space-y-3">
            <DemoBanner />
            <ResponsiveContainer width="100%" height={240}>
              <PieChart>
                <Pie
                  data={PROTOCOL_DEMO}
                  cx="50%"
                  cy="50%"
                  innerRadius={56}
                  outerRadius={92}
                  paddingAngle={2}
                  dataKey="value"
                  stroke="hsl(var(--background))"
                  strokeWidth={2}
                >
                  {PROTOCOL_DEMO.map((entry) => (
                    <Cell key={entry.name} fill={entry.color} />
                  ))}
                </Pie>
                <Tooltip {...tooltipStyles} />
              </PieChart>
            </ResponsiveContainer>
            <div className="grid grid-cols-2 gap-1.5 text-xs sm:grid-cols-3">
              {PROTOCOL_DEMO.map((p) => (
                <div key={p.name} className="flex items-center gap-2">
                  <span
                    aria-hidden="true"
                    className="inline-block h-2.5 w-2.5 rounded-sm"
                    style={{ backgroundColor: p.color }}
                  />
                  <span className="text-muted-foreground">{p.name}</span>
                  <span className="ml-auto font-medium text-foreground">{p.value}%</span>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2 lg:gap-6">
        <Card>
          <CardHeader className="flex flex-row items-start justify-between space-y-0">
            <div className="space-y-1">
              <CardTitle>Top endpoints</CardTitle>
              <CardDescription>Highest-traffic routes.</CardDescription>
            </div>
            <Badge variant="muted">Demo</Badge>
          </CardHeader>
          <CardContent className="space-y-3">
            <DemoBanner />
            <ResponsiveContainer width="100%" height={260}>
              <BarChart data={ENDPOINTS_DEMO} layout="vertical" margin={{ left: 0, right: 16 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" opacity={0.4} />
                <XAxis
                  type="number"
                  stroke="hsl(var(--muted-foreground))"
                  tick={{ fontSize: 11 }}
                  tickLine={false}
                />
                <YAxis
                  dataKey="name"
                  type="category"
                  stroke="hsl(var(--muted-foreground))"
                  tick={{ fontSize: 11 }}
                  width={120}
                  tickLine={false}
                />
                <Tooltip {...tooltipStyles} />
                <Bar
                  dataKey="requests"
                  fill="hsl(var(--primary))"
                  radius={[0, 6, 6, 0]}
                  isAnimationActive={false}
                />
              </BarChart>
            </ResponsiveContainer>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-start justify-between space-y-0">
            <div className="space-y-1">
              <CardTitle>Status code breakdown</CardTitle>
              <CardDescription>HTTP response classes over the period.</CardDescription>
            </div>
            <Badge variant="muted">Demo</Badge>
          </CardHeader>
          <CardContent className="space-y-3">
            <DemoBanner />
            <ResponsiveContainer width="100%" height={220}>
              <PieChart>
                <Pie
                  data={STATUS_DEMO}
                  cx="50%"
                  cy="50%"
                  innerRadius={56}
                  outerRadius={92}
                  paddingAngle={2}
                  dataKey="value"
                  stroke="hsl(var(--background))"
                  strokeWidth={2}
                >
                  {STATUS_DEMO.map((entry) => (
                    <Cell key={entry.name} fill={entry.color} />
                  ))}
                </Pie>
                <Tooltip {...tooltipStyles} />
              </PieChart>
            </ResponsiveContainer>
            <div className="grid grid-cols-2 gap-2">
              {STATUS_DEMO.map((s) => (
                <div
                  key={s.name}
                  className="flex items-center justify-between rounded-md bg-muted/30 px-3 py-2 text-sm"
                >
                  <div className="flex items-center gap-2">
                    <span
                      aria-hidden="true"
                      className="inline-block h-2.5 w-2.5 rounded-sm"
                      style={{ backgroundColor: s.color }}
                    />
                    <span className="text-muted-foreground">{s.name}</span>
                  </div>
                  <span className="font-medium tabular-nums">
                    {formatNumber(s.count)}
                  </span>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader className="flex flex-row items-start justify-between space-y-0">
          <div className="space-y-1">
            <CardTitle>Performance overview</CardTitle>
            <CardDescription>Five-axis health score.</CardDescription>
          </div>
          <Badge variant="muted">Demo</Badge>
        </CardHeader>
        <CardContent>
          <DemoBanner />
          <ResponsiveContainer width="100%" height={260}>
            <RadarChart data={RADAR_DEMO}>
              <PolarGrid stroke="hsl(var(--border))" />
              <PolarAngleAxis dataKey="subject" stroke="hsl(var(--muted-foreground))" tick={{ fontSize: 11 }} />
              <PolarRadiusAxis
                angle={90}
                domain={[0, 100]}
                stroke="hsl(var(--muted-foreground))"
                tick={{ fontSize: 10 }}
              />
              <Radar
                name="Score"
                dataKey="score"
                stroke="hsl(var(--primary))"
                fill="hsl(var(--primary))"
                fillOpacity={0.35}
              />
              <Tooltip {...tooltipStyles} />
            </RadarChart>
          </ResponsiveContainer>
        </CardContent>
      </Card>

      <Card className="overflow-hidden">
        <CardHeader className="flex flex-row items-start justify-between space-y-0">
          <div className="space-y-1">
            <CardTitle>Endpoint performance</CardTitle>
            <CardDescription>Per-route requests, errors, and latency.</CardDescription>
          </div>
          <Badge variant="muted">Demo</Badge>
        </CardHeader>
        <div className="px-6 pb-3">
          <DemoBanner />
        </div>
        <Table>
          <Thead>
            <Tr>
              <Th>Endpoint</Th>
              <Th className="text-right">Requests</Th>
              <Th className="text-right">Errors</Th>
              <Th className="text-right">Error rate</Th>
              <Th className="text-right">Avg latency</Th>
              <Th>Status</Th>
            </Tr>
          </Thead>
          <Tbody>
            {ENDPOINTS_DEMO.map((endpoint) => {
              const errorRate = (endpoint.errors / endpoint.requests) * 100
              const status =
                errorRate < 1 ? 'success' : errorRate < 5 ? 'warning' : 'danger'
              return (
                <Tr key={endpoint.name}>
                  <Td>
                    <code className="rounded bg-muted px-1.5 py-0.5 text-xs font-mono">
                      {endpoint.name}
                    </code>
                  </Td>
                  <Td className="text-right tabular-nums">{formatNumber(endpoint.requests)}</Td>
                  <Td className="text-right tabular-nums text-red-500">
                    {formatNumber(endpoint.errors)}
                  </Td>
                  <Td className="text-right tabular-nums text-muted-foreground">
                    {errorRate.toFixed(2)}%
                  </Td>
                  <Td className="text-right tabular-nums text-muted-foreground">
                    {formatMs(endpoint.avgLatency)}
                  </Td>
                  <Td>
                    <Badge variant={status as any}>
                      {status === 'success' ? 'Healthy' : status === 'warning' ? 'Warning' : 'Critical'}
                    </Badge>
                  </Td>
                </Tr>
              )
            })}
          </Tbody>
        </Table>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <BarChart3 className="h-5 w-5 text-primary" />
            What&apos;s real vs sample?
          </CardTitle>
          <CardDescription>
            The top stats and the live request volume chart use real data from{' '}
            <code className="rounded bg-muted px-1 py-0.5 text-xs">/admin/dashboard/stats</code>. Aggregations
            (per-endpoint, status code, geographic, protocol share) need a backend analytics endpoint;
            those panels are rendered with sample data and clearly labeled.
          </CardDescription>
        </CardHeader>
      </Card>

      <PerformanceLineDemo />
    </div>
  )
}

function PerformanceLineDemo() {
  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between space-y-0">
        <div className="space-y-1">
          <CardTitle>Per-protocol traffic (24h)</CardTitle>
          <CardDescription>Hour-over-hour comparison across all protocols.</CardDescription>
        </div>
        <Badge variant="muted">Demo</Badge>
      </CardHeader>
      <CardContent className="space-y-3">
        <DemoBanner />
        <ResponsiveContainer width="100%" height={260}>
          <LineChart
            data={Array.from({ length: 24 }, (_, i) => ({
              time: `${i.toString().padStart(2, '0')}:00`,
              mcp: 600 + ((i * 41) % 600),
              a2a: 400 + ((i * 27) % 400),
              http: 500 + ((i * 33) % 500),
              mqtt: 300 + ((i * 19) % 300),
            }))}
            margin={{ left: -10, right: 10 }}
          >
            <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" opacity={0.4} />
            <XAxis dataKey="time" stroke="hsl(var(--muted-foreground))" tick={{ fontSize: 11 }} tickLine={false} />
            <YAxis stroke="hsl(var(--muted-foreground))" tick={{ fontSize: 11 }} width={36} tickLine={false} />
            <Tooltip {...tooltipStyles} />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Line type="monotone" dataKey="mcp" stroke="#0ea5e9" strokeWidth={2} name="MCP" dot={false} />
            <Line type="monotone" dataKey="a2a" stroke="#10b981" strokeWidth={2} name="A2A" dot={false} />
            <Line type="monotone" dataKey="http" stroke="#8b5cf6" strokeWidth={2} name="HTTP" dot={false} />
            <Line type="monotone" dataKey="mqtt" stroke="#f59e0b" strokeWidth={2} name="MQTT" dot={false} />
          </LineChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  )
}
