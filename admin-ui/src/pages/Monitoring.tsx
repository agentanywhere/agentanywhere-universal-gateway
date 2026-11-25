import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Gauge,
  Radio,
  Timer,
  XCircle,
  Zap,
} from 'lucide-react'
import { agentsApi, type AgentBoard, type BoardAgent } from '../api/agents'
import { Badge, Card, CardContent, EmptyState, PageHeader, Stat } from '../components/ui'
import { cn } from '../lib/utils'

type ConnState = 'connecting' | 'live' | 'polling'

function timeAgo(ts: number | null): string {
  if (!ts) return 'never'
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts))
  if (s < 2) return 'just now'
  if (s < 60) return `${s}s ago`
  const m = Math.floor(s / 60)
  return `${m}m ago`
}

function Sparkline({ points, color }: { points: number[]; color: string }) {
  if (!points || points.length < 2) {
    return <div className="h-8 w-full rounded bg-muted/40" />
  }
  const w = 120
  const h = 32
  const max = Math.max(...points)
  const min = Math.min(...points)
  const range = max - min || 1
  const step = w / (points.length - 1)
  const d = points
    .map((p, i) => {
      const x = i * step
      const y = h - ((p - min) / range) * (h - 4) - 2
      return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
  return (
    <svg width="100%" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" className="h-8 w-full">
      <path d={d} fill="none" stroke={color} strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  )
}

function AgentCard({ a }: { a: BoardAgent }) {
  const up = a.status === 'up'
  const down = a.status === 'down'
  const accent = up ? '#10B981' : down ? '#EF4444' : '#8B95A1'
  return (
    <div
      className={cn(
        'relative overflow-hidden rounded-xl border bg-card/60 p-5 backdrop-blur-sm transition-all',
        up && 'border-emerald-500/25',
        down && 'border-red-500/40 shadow-lg shadow-red-500/10',
        !up && !down && 'border-border'
      )}
    >
      <div
        aria-hidden
        className="absolute inset-x-0 top-0 h-0.5"
        style={{ background: accent, opacity: down ? 1 : 0.6 }}
      />
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span
              className={cn(
                'inline-block h-2.5 w-2.5 shrink-0 rounded-full',
                up && 'bg-emerald-500 animate-pulse',
                down && 'bg-red-500',
                !up && !down && 'bg-slate-500'
              )}
            />
            <span className="truncate font-semibold text-foreground">{a.slug}</span>
          </div>
          <a
            href={`/agents/${a.slug}/`}
            target="_blank"
            rel="noreferrer"
            className="mt-0.5 block truncate font-mono text-[11px] text-muted-foreground hover:text-primary"
          >
            {a.upstream_base}
          </a>
        </div>
        <Badge variant={up ? 'success' : down ? 'danger' : 'muted'} dot>
          {up ? 'Up' : down ? 'Down' : 'Unknown'}
        </Badge>
      </div>

      <div className="mt-4">
        <Sparkline points={a.sparkline} color={accent} />
      </div>

      <div className="mt-3 grid grid-cols-3 gap-2 text-center">
        <Metric icon={<Timer className="h-3.5 w-3.5" />} label="Latency" value={a.latency_ms != null ? `${a.latency_ms}ms` : '—'} />
        <Metric icon={<Gauge className="h-3.5 w-3.5" />} label="Uptime" value={a.uptime_pct != null ? `${a.uptime_pct}%` : '—'} />
        <Metric icon={<Activity className="h-3.5 w-3.5" />} label="Req/min" value={String(a.req_per_min)} />
      </div>

      <div className="mt-3 flex items-center justify-between text-[11px] text-muted-foreground">
        <span className={cn(a.error_pct > 0 && 'text-red-500')}>
          {a.error_pct > 0 ? `${a.error_pct}% errors` : 'no errors'}
        </span>
        <span>checked {timeAgo(a.last_checked)}</span>
      </div>

      {down && a.error && (
        <p className="mt-2 truncate rounded-md bg-red-500/10 px-2 py-1 text-[11px] text-red-500" title={a.error}>
          {a.error}
        </p>
      )}
    </div>
  )
}

function Metric({ icon, label, value }: { icon: React.ReactNode; label: string; value: string }) {
  return (
    <div className="rounded-lg bg-muted/40 px-2 py-1.5">
      <div className="flex items-center justify-center gap-1 text-muted-foreground">{icon}</div>
      <div className="mt-0.5 text-sm font-semibold tabular-nums text-foreground">{value}</div>
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</div>
    </div>
  )
}

export default function Monitoring() {
  const [board, setBoard] = useState<AgentBoard | null>(null)
  const [conn, setConn] = useState<ConnState>('connecting')
  const esRef = useRef<EventSource | null>(null)
  const pollRef = useRef<number | null>(null)

  useEffect(() => {
    let closed = false

    const startPolling = () => {
      setConn('polling')
      const tick = async () => {
        try {
          const snap = await agentsApi.healthSnapshot()
          if (!closed) setBoard(snap)
        } catch {
          /* keep last board */
        }
      }
      tick()
      pollRef.current = window.setInterval(tick, 4000)
    }

    try {
      const es = new EventSource(agentsApi.streamUrl())
      esRef.current = es
      es.onopen = () => !closed && setConn('live')
      es.onmessage = (evt) => {
        if (closed) return
        try {
          setBoard(JSON.parse(evt.data))
          setConn('live')
        } catch {
          /* ignore malformed frame */
        }
      }
      es.onerror = () => {
        // SSE failed (or proxy buffering) — degrade gracefully to polling.
        es.close()
        esRef.current = null
        if (!closed && pollRef.current == null) startPolling()
      }
    } catch {
      startPolling()
    }

    return () => {
      closed = true
      esRef.current?.close()
      if (pollRef.current != null) window.clearInterval(pollRef.current)
    }
  }, [])

  const agents = board?.agents ?? []
  const summary = board?.summary
  const sortedAgents = useMemo(
    () =>
      [...agents].sort((a, b) => {
        const rank = (s: string) => (s === 'down' ? 0 : s === 'unknown' ? 1 : 2)
        return rank(a.status) - rank(b.status) || a.slug.localeCompare(b.slug)
      }),
    [agents]
  )

  return (
    <div className="space-y-6 sm:space-y-8">
      <PageHeader
        title="Mission Control"
        description="Live health and traffic for every registered agent — streamed in real time."
        actions={
          <Badge variant={conn === 'live' ? 'success' : conn === 'polling' ? 'warning' : 'muted'} dot>
            <Radio className="h-3 w-3" />
            {conn === 'live' ? 'Live' : conn === 'polling' ? 'Polling' : 'Connecting'}
          </Badge>
        }
      />

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="Agents" value={summary?.total ?? '—'} icon={<Zap className="h-4 w-4" />} />
        <Stat label="Healthy" value={summary?.up ?? '—'} tone="success" icon={<CheckCircle2 className="h-4 w-4" />} />
        <Stat label="Down" value={summary?.down ?? '—'} tone={summary && summary.down > 0 ? 'danger' : 'neutral'} icon={<XCircle className="h-4 w-4" />} />
        <Stat label="Req/min" value={summary?.req_per_min ?? '—'} tone="info" icon={<Activity className="h-4 w-4" />} />
      </div>

      {summary && summary.down > 0 && (
        <div className="flex items-center gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-500">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          {summary.down} agent{summary.down === 1 ? '' : 's'} unreachable — traffic to{' '}
          {summary.down === 1 ? 'it' : 'them'} will fail.
        </div>
      )}

      {agents.length === 0 ? (
        <Card>
          <CardContent className="p-0">
            <EmptyState
              icon={<Activity className="h-5 w-5" />}
              title={board ? 'No agents to monitor' : 'Connecting to live stream…'}
              description={
                board
                  ? 'Register an agent to see it appear on the board in real time.'
                  : 'Establishing the Server-Sent Events connection.'
              }
            />
          </CardContent>
        </Card>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {sortedAgents.map((a) => (
            <AgentCard key={a.slug} a={a} />
          ))}
        </div>
      )}
    </div>
  )
}
