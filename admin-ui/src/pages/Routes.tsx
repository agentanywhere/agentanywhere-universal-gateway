import { useQuery } from '@tanstack/react-query'
import {
  CheckCircle,
  Clock,
  Code,
  Globe,
  MessageSquare,
  Radio,
  Route as RouteIcon,
  Shield,
  Wifi,
  Zap,
} from 'lucide-react'
import { adminApi, type Route } from '../api/admin'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/Card'
import { PageHeader } from '../components/ui/PageHeader'
import { Badge } from '../components/ui/Badge'
import { Skeleton } from '../components/ui/Skeleton'
import { EmptyState } from '../components/ui/EmptyState'
import { cn } from '../lib/utils'

const PROTOCOL_META: Record<
  string,
  { icon: any; bg: string; ring: string; text: string; label: string }
> = {
  mcp: { icon: Zap, bg: 'bg-sky-500/10', ring: 'ring-sky-500/20', text: 'text-sky-500', label: 'MCP' },
  a2a: {
    icon: MessageSquare,
    bg: 'bg-violet-500/10',
    ring: 'ring-violet-500/20',
    text: 'text-violet-500',
    label: 'A2A',
  },
  http: {
    icon: Globe,
    bg: 'bg-emerald-500/10',
    ring: 'ring-emerald-500/20',
    text: 'text-emerald-500',
    label: 'HTTP',
  },
  mqtt: {
    icon: Radio,
    bg: 'bg-amber-500/10',
    ring: 'ring-amber-500/20',
    text: 'text-amber-500',
    label: 'MQTT',
  },
  websocket: {
    icon: Wifi,
    bg: 'bg-cyan-500/10',
    ring: 'ring-cyan-500/20',
    text: 'text-cyan-500',
    label: 'WS',
  },
  grpc: {
    icon: Code,
    bg: 'bg-pink-500/10',
    ring: 'ring-pink-500/20',
    text: 'text-pink-500',
    label: 'gRPC',
  },
}

export default function Routes() {
  const { data: routes, isLoading } = useQuery({
    queryKey: ['routes'],
    queryFn: adminApi.getRoutes,
  })

  return (
    <div className="space-y-6 sm:space-y-8">
      <PageHeader
        title="Routes"
        description="Configured routing rules and protocol endpoints."
        actions={
          routes && routes.length > 0 ? (
            <Badge variant="muted">
              {routes.length} route{routes.length === 1 ? '' : 's'}
            </Badge>
          ) : null
        }
      />

      {isLoading ? (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-40" />
          ))}
        </div>
      ) : !routes || routes.length === 0 ? (
        <EmptyState
          icon={<RouteIcon className="h-5 w-5" />}
          title="No routes configured"
          description="Routes are derived from the backend URLs you configure in Settings."
        />
      ) : (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          {routes.map((route: Route) => {
            const meta = PROTOCOL_META[route.protocol] ?? {
              icon: Globe,
              bg: 'bg-muted',
              ring: 'ring-border',
              text: 'text-muted-foreground',
              label: route.protocol.toUpperCase(),
            }
            const Icon = meta.icon
            return (
              <Card
                key={route.path}
                className="transition-all duration-200 hover:-translate-y-0.5 hover:border-primary/30 hover:shadow-md"
              >
                <CardHeader className="flex flex-row items-start justify-between gap-3 space-y-0">
                  <div className="flex min-w-0 items-start gap-3">
                    <div
                      className={cn(
                        'flex h-10 w-10 shrink-0 items-center justify-center rounded-lg ring-1',
                        meta.bg,
                        meta.ring,
                        meta.text
                      )}
                    >
                      <Icon className="h-5 w-5" />
                    </div>
                    <div className="min-w-0">
                      <CardTitle className="truncate font-mono">{route.path}</CardTitle>
                      <CardDescription className="truncate font-mono text-xs">
                        {route.backend_url}
                      </CardDescription>
                    </div>
                  </div>
                  <Badge variant="muted" className={cn(meta.text, 'border-current/20')}>
                    {meta.label}
                  </Badge>
                </CardHeader>
                <CardContent className="space-y-2 border-t border-border pt-4 text-sm">
                  <div className="flex items-center gap-2 text-muted-foreground">
                    <Clock className="h-4 w-4" />
                    Timeout
                    <span className="ml-auto font-medium text-foreground tabular-nums">
                      {route.timeout}s
                    </span>
                  </div>
                  <div className="flex items-center gap-2 text-muted-foreground">
                    <Shield className="h-4 w-4" />
                    Authentication
                    <span className="ml-auto">
                      {route.requires_auth ? (
                        <Badge variant="success" dot>
                          Required
                        </Badge>
                      ) : (
                        <Badge variant="warning">Optional</Badge>
                      )}
                    </span>
                  </div>
                  <div className="flex items-center gap-2 text-muted-foreground">
                    <CheckCircle className="h-4 w-4 text-emerald-500" />
                    Status
                    <span className="ml-auto">
                      <Badge variant="success" dot>
                        Active
                      </Badge>
                    </span>
                  </div>
                </CardContent>
              </Card>
            )
          })}
        </div>
      )}

      {routes && routes.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Protocol summary</CardTitle>
            <CardDescription>Routes per protocol family.</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
              {Object.entries(PROTOCOL_META).map(([id, meta]) => {
                const count = routes.filter((r) => r.protocol === id).length
                const Icon = meta.icon
                return (
                  <div
                    key={id}
                    className="flex flex-col items-start gap-2 rounded-lg border border-border bg-background/40 p-3"
                  >
                    <Icon className={cn('h-4 w-4', meta.text)} />
                    <div className="text-2xl font-semibold tabular-nums text-foreground">{count}</div>
                    <div className="text-xs uppercase tracking-wider text-muted-foreground">{meta.label}</div>
                  </div>
                )
              })}
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
