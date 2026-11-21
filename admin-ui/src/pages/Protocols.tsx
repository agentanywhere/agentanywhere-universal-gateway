import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import {
  AlertCircle,
  CheckCircle,
  Code,
  Copy,
  Globe,
  MessageSquare,
  Play,
  Radio,
  Wifi,
  XCircle,
  Zap,
} from 'lucide-react'
import { adminApi, configString } from '../api/admin'
import { Card, CardContent } from '../components/ui/Card'
import { PageHeader } from '../components/ui/PageHeader'
import { Badge } from '../components/ui/Badge'
import { Button } from '../components/ui/Button'
import { IconButton } from '../components/ui/IconButton'
import { Stat } from '../components/ui/Stat'
import { useToast } from '../components/ui/Toast'
import { cn } from '../lib/utils'

const PROTOCOLS = [
  {
    id: 'mcp',
    name: 'MCP',
    fullName: 'Model Context Protocol',
    description: 'AI/ML model interactions and context management',
    icon: Zap,
    tone: { bg: 'bg-sky-500/10', ring: 'ring-sky-500/20', text: 'text-sky-500' },
    endpoints: ['/mcp/*', '/api/mcp/*', '/test/mcp'],
    example: 'curl http://localhost:8000/test/mcp',
  },
  {
    id: 'a2a',
    name: 'A2A',
    fullName: 'Agent-to-Agent',
    description: 'Agent communication and coordination',
    icon: MessageSquare,
    tone: { bg: 'bg-violet-500/10', ring: 'ring-violet-500/20', text: 'text-violet-500' },
    endpoints: ['/a2a/*', '/api/a2a/*', '/test/a2a'],
    example: 'curl http://localhost:8000/test/a2a',
  },
  {
    id: 'http',
    name: 'HTTP/REST',
    fullName: 'HTTP REST API',
    description: 'Standard RESTful APIs with full HTTP support',
    icon: Globe,
    tone: { bg: 'bg-emerald-500/10', ring: 'ring-emerald-500/20', text: 'text-emerald-500' },
    endpoints: ['/api/*', '/rest/*', '/http/*', '/test/http'],
    example: 'curl http://localhost:8000/test/http',
  },
  {
    id: 'mqtt',
    name: 'MQTT',
    fullName: 'MQTT',
    description: 'IoT messaging and pub/sub for real-time data',
    icon: Radio,
    tone: { bg: 'bg-amber-500/10', ring: 'ring-amber-500/20', text: 'text-amber-500' },
    endpoints: ['/mqtt/publish', '/mqtt/subscribe'],
    example: 'curl -X POST http://localhost:8000/mqtt/publish -H "X-API-Key: your-key"',
  },
  {
    id: 'websocket',
    name: 'WebSocket',
    fullName: 'WebSocket',
    description: 'Real-time bidirectional communication',
    icon: Wifi,
    tone: { bg: 'bg-cyan-500/10', ring: 'ring-cyan-500/20', text: 'text-cyan-500' },
    endpoints: ['/ws/*', '/websocket/*', '/test/websocket'],
    example: 'ws://localhost:8000/test/websocket',
  },
  {
    id: 'grpc',
    name: 'gRPC',
    fullName: 'gRPC',
    description: 'High-performance RPC with protocol buffers',
    icon: Code,
    tone: { bg: 'bg-pink-500/10', ring: 'ring-pink-500/20', text: 'text-pink-500' },
    endpoints: ['/grpc/*', '/api/grpc/*', '/test/grpc'],
    example: 'curl -X POST http://localhost:8000/test/grpc -d \'{"service":"Test","method":"Test"}\'',
  },
]

export default function Protocols() {
  const navigate = useNavigate()
  const { toast } = useToast()
  const [selectedProtocol, setSelectedProtocol] = useState<string | null>(null)

  const { data: routes } = useQuery({ queryKey: ['routes'], queryFn: adminApi.getRoutes })
  const { data: config } = useQuery({ queryKey: ['config'], queryFn: adminApi.getConfig })

  const getProtocolStatus = (id: string) => {
    const route = routes?.find((r: any) => r.protocol === id)
    if (!route) return { status: 'not-configured' as const, message: 'Not configured' }

    const backendUrls: Record<string, string | undefined> = {
      mcp: configString(config, 'mcp_backend_url'),
      a2a: configString(config, 'a2a_backend_url'),
      http: configString(config, 'http_backend_url'),
      mqtt: configString(config, 'mqtt_broker_url'),
      websocket: configString(config, 'websocket_backend_url'),
      grpc: configString(config, 'grpc_backend_url'),
    }
    const url = backendUrls[id]
    if (!url) return { status: 'not-configured' as const, message: 'Backend not configured' }
    return { status: 'configured' as const, message: 'Active', url }
  }

  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text)
      toast({ title: 'Copied to clipboard', variant: 'success' })
    } catch {
      toast({ title: 'Copy failed', variant: 'error' })
    }
  }

  const configuredCount = PROTOCOLS.filter((p) => getProtocolStatus(p.id).status === 'configured').length

  return (
    <div className="space-y-6 sm:space-y-8">
      <PageHeader
        title="Protocols"
        description="All supported protocols and their configuration status."
      />

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <Stat
          label="Total protocols"
          value={PROTOCOLS.length.toString()}
          icon={<Globe className="h-5 w-5" />}
          tone="primary"
        />
        <Stat
          label="Configured"
          value={configuredCount.toString()}
          icon={<CheckCircle className="h-5 w-5" />}
          tone="success"
        />
        <Stat
          label="Not configured"
          value={(PROTOCOLS.length - configuredCount).toString()}
          icon={<AlertCircle className="h-5 w-5" />}
          tone="warning"
        />
      </div>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
        {PROTOCOLS.map((protocol) => {
          const Icon = protocol.icon
          const status = getProtocolStatus(protocol.id)
          const isSelected = selectedProtocol === protocol.id
          const configured = status.status === 'configured'

          return (
            <Card
              key={protocol.id}
              className={cn(
                'cursor-pointer overflow-hidden transition-all duration-200',
                isSelected
                  ? 'border-primary/40 shadow-md'
                  : 'hover:-translate-y-0.5 hover:border-primary/30 hover:shadow-md'
              )}
              onClick={() => setSelectedProtocol(isSelected ? null : protocol.id)}
            >
              <CardContent className="space-y-4 p-5">
                <div className="flex items-start justify-between">
                  <div
                    className={cn(
                      'flex h-10 w-10 items-center justify-center rounded-lg ring-1',
                      protocol.tone.bg,
                      protocol.tone.ring,
                      protocol.tone.text
                    )}
                  >
                    <Icon className="h-5 w-5" />
                  </div>
                  {configured ? (
                    <CheckCircle className="h-5 w-5 text-emerald-500" />
                  ) : (
                    <XCircle className="h-5 w-5 text-muted-foreground" />
                  )}
                </div>
                <div className="space-y-1">
                  <h3 className="text-base font-semibold text-foreground">{protocol.fullName}</h3>
                  <p className="text-sm text-muted-foreground">{protocol.description}</p>
                </div>
                <Badge variant={configured ? 'success' : 'muted'} dot={configured}>
                  {status.message}
                </Badge>

                {isSelected && (
                  <div
                    className="space-y-3 border-t border-border pt-4 text-sm"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <div>
                      <div className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                        Endpoints
                      </div>
                      <div className="space-y-1">
                        {protocol.endpoints.map((endpoint) => (
                          <div
                            key={endpoint}
                            className="flex items-center justify-between rounded-md bg-muted/40 px-2 py-1.5"
                          >
                            <code className="truncate text-xs font-mono text-muted-foreground">{endpoint}</code>
                            <IconButton
                              label={`Copy ${endpoint}`}
                              size="sm"
                              onClick={() => copy(endpoint)}
                            >
                              <Copy className="h-3 w-3" />
                            </IconButton>
                          </div>
                        ))}
                      </div>
                    </div>

                    {status.url && (
                      <div>
                        <div className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                          Backend URL
                        </div>
                        <div className="flex items-center justify-between gap-2 rounded-md bg-muted/40 px-2 py-1.5">
                          <code className="truncate text-xs font-mono text-muted-foreground">
                            {status.url}
                          </code>
                          <IconButton
                            label="Copy backend URL"
                            size="sm"
                            onClick={() => copy(status.url!)}
                          >
                            <Copy className="h-3 w-3" />
                          </IconButton>
                        </div>
                      </div>
                    )}

                    <div>
                      <div className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                        Example
                      </div>
                      <div className="relative">
                        <pre className="overflow-x-auto rounded-md bg-muted/40 p-2 text-[11px] leading-relaxed text-muted-foreground">
                          <code>{protocol.example}</code>
                        </pre>
                        <IconButton
                          label="Copy example"
                          size="sm"
                          className="absolute right-1 top-1"
                          onClick={() => copy(protocol.example)}
                        >
                          <Copy className="h-3 w-3" />
                        </IconButton>
                      </div>
                    </div>

                    <Button
                      onClick={() => navigate(`/test/${protocol.id}`)}
                      size="sm"
                      className="w-full"
                    >
                      <Play className="h-3.5 w-3.5" />
                      Test {protocol.name}
                    </Button>
                  </div>
                )}
              </CardContent>
            </Card>
          )
        })}
      </div>
    </div>
  )
}
