import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, Copy, Play, Send, X } from 'lucide-react'
import apiClient from '../api/client'
import { Card, CardContent } from '../components/ui/Card'
import { Button } from '../components/ui/Button'
import { Input } from '../components/ui/Input'
import { Select } from '../components/ui/Select'
import { IconButton } from '../components/ui/IconButton'
import { useToast } from '../components/ui/Toast'

const PROTOCOL_INFO: Record<string, any> = {
  http: {
    name: 'HTTP/REST',
    description: 'Test HTTP REST API endpoints',
    endpoint: '/test/http',
    methods: ['GET', 'POST', 'PUT', 'DELETE'],
  },
  websocket: {
    name: 'WebSocket',
    description: 'Test WebSocket connections',
    endpoint: '/test/websocket',
    methods: [],
  },
  mcp: {
    name: 'MCP',
    description: 'Test MCP protocol endpoints',
    endpoint: '/test/mcp',
    methods: ['GET', 'POST'],
  },
  a2a: {
    name: 'A2A',
    description: 'Test A2A protocol endpoints',
    endpoint: '/test/a2a',
    methods: ['GET', 'POST'],
  },
  mqtt: {
    name: 'MQTT',
    description: 'Test MQTT publish/subscribe',
    endpoint: '/mqtt/publish',
    methods: ['PUBLISH', 'SUBSCRIBE'],
  },
  grpc: {
    name: 'gRPC',
    description: 'Test gRPC calls',
    endpoint: '/test/grpc',
    methods: ['POST'],
  },
}

export default function ProtocolTest() {
  const { protocol } = useParams<{ protocol: string }>()
  const navigate = useNavigate()
  const { toast } = useToast()
  const [method, setMethod] = useState('GET')
  const [endpoint, setEndpoint] = useState('')
  const [requestBody, setRequestBody] = useState('')
  const [response, setResponse] = useState<any>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [wsConnected, setWsConnected] = useState(false)
  const [wsMessages, setWsMessages] = useState<string[]>([])
  const [wsMessage, setWsMessage] = useState('')
  const [ws, setWs] = useState<WebSocket | null>(null)

  const info = PROTOCOL_INFO[protocol || ''] || PROTOCOL_INFO.http

  useEffect(() => {
    if (!endpoint && info.endpoint) {
      setEndpoint(info.endpoint)
    }
  }, [protocol, info.endpoint])

  useEffect(() => {
    return () => {
      if (ws) {
        ws.close()
      }
    }
  }, [ws])

  const handleHttpTest = async () => {
    setLoading(true)
    setError(null)
    setResponse(null)

    try {
      let result
      if (method === 'GET') {
        result = await apiClient.get(endpoint)
      } else if (method === 'POST') {
        const data = requestBody ? (requestBody.trim().startsWith('{') ? JSON.parse(requestBody) : requestBody) : {}
        result = await apiClient.post(endpoint, data)
      } else if (method === 'PUT') {
        const data = requestBody ? (requestBody.trim().startsWith('{') ? JSON.parse(requestBody) : requestBody) : {}
        result = await apiClient.put(endpoint, data)
      } else if (method === 'DELETE') {
        result = await apiClient.delete(endpoint)
      }
      
      if (result) {
        setResponse({
          status: result.status,
          statusText: result.statusText,
          data: result.data,
        })
      }
    } catch (err: any) {
      setError(err.response?.data?.detail || err.message || 'Request failed')
    } finally {
      setLoading(false)
    }
  }

  const handleWebSocketConnect = () => {
    if (ws) {
      ws.close()
      setWs(null)
      setWsConnected(false)
      return
    }

    const token = localStorage.getItem('admin_token')
    const wsUrl = endpoint.startsWith('ws') 
      ? `${endpoint}?api_key=${token}`
      : `ws://localhost:8000${endpoint}?api_key=${token}`

    const websocket = new WebSocket(wsUrl)
    
    websocket.onopen = () => {
      setWsConnected(true)
      setWsMessages((prev) => [...prev, '✅ Connected'])
    }
    
    websocket.onmessage = (event) => {
      setWsMessages((prev) => [...prev, `📨 ${event.data}`])
    }
    
    websocket.onerror = (error) => {
      setError('WebSocket connection error')
      setWsMessages((prev) => [...prev, '❌ Connection error'])
    }
    
    websocket.onclose = () => {
      setWsConnected(false)
      setWsMessages((prev) => [...prev, '🔌 Disconnected'])
      setWs(null)
    }
    
    setWs(websocket)
  }

  const handleWebSocketSend = () => {
    if (ws && wsConnected && wsMessage) {
      ws.send(wsMessage)
      setWsMessages((prev) => [...prev, `📤 Sent: ${wsMessage}`])
      setWsMessage('')
    }
  }

  const handleMqttPublish = async () => {
    setLoading(true)
    setError(null)
    setResponse(null)

    try {
      const result = await apiClient.post('/mqtt/publish', {
        topic: 'test/topic',
        payload: requestBody ? JSON.parse(requestBody) : { message: 'Hello MQTT' },
        qos: 1,
      })
      setResponse(result.data)
    } catch (err: any) {
      setError(err.response?.data?.detail || err.message || 'Request failed')
    } finally {
      setLoading(false)
    }
  }

  const handleMcpTest = async () => {
    setLoading(true)
    setError(null)
    setResponse(null)

    try {
      let result
      if (method === 'GET') {
        result = await apiClient.get(endpoint)
      } else {
        const data = requestBody ? (requestBody.trim().startsWith('{') ? JSON.parse(requestBody) : requestBody) : {}
        result = await apiClient.post(endpoint, data)
      }
      setResponse(result.data)
    } catch (err: any) {
      setError(err.response?.data?.detail || err.message || 'Request failed')
    } finally {
      setLoading(false)
    }
  }

  const handleA2aTest = async () => {
    setLoading(true)
    setError(null)
    setResponse(null)

    try {
      let result
      if (method === 'GET') {
        result = await apiClient.get(endpoint)
      } else {
        const data = requestBody ? (requestBody.trim().startsWith('{') ? JSON.parse(requestBody) : requestBody) : {}
        result = await apiClient.post(endpoint, data)
      }
      setResponse(result.data)
    } catch (err: any) {
      setError(err.response?.data?.detail || err.message || 'Request failed')
    } finally {
      setLoading(false)
    }
  }

  const handleGrpcTest = async () => {
    setLoading(true)
    setError(null)
    setResponse(null)

    try {
      const data = requestBody ? JSON.parse(requestBody) : { service: 'TestService', method: 'TestMethod', data: {} }
      const result = await apiClient.post(endpoint, data)
      setResponse(result.data)
    } catch (err: any) {
      setError(err.response?.data?.detail || err.message || 'Request failed')
    } finally {
      setLoading(false)
    }
  }

  const handleTest = () => {
    if (protocol === 'http') {
      handleHttpTest()
    } else if (protocol === 'websocket') {
      handleWebSocketConnect()
    } else if (protocol === 'mqtt') {
      handleMqttPublish()
    } else if (protocol === 'mcp') {
      handleMcpTest()
    } else if (protocol === 'a2a') {
      handleA2aTest()
    } else if (protocol === 'grpc') {
      handleGrpcTest()
    }
  }

  const showMethod = protocol === 'http' || protocol === 'mcp' || protocol === 'a2a'
  const showBody =
    (showMethod && (method === 'POST' || method === 'PUT')) ||
    protocol === 'mqtt' ||
    protocol === 'grpc'

  return (
    <div className="space-y-6 sm:space-y-8">
      <div className="flex items-center gap-3">
        <IconButton label="Back to protocols" variant="subtle" onClick={() => navigate('/protocols')}>
          <ArrowLeft className="h-4 w-4" />
        </IconButton>
        <div className="min-w-0">
          <h1 className="truncate text-2xl font-semibold tracking-tight text-foreground sm:text-3xl">
            Test {info.name}
          </h1>
          <p className="text-sm text-muted-foreground">{info.description}</p>
        </div>
      </div>

      <Card>
        <CardContent className="space-y-5 p-6">
          <div>
            <label className="mb-1.5 block text-sm font-medium text-foreground">Endpoint</label>
            <div className="flex flex-col gap-2 sm:flex-row sm:items-stretch">
              {showMethod && (
                <Select
                  value={method}
                  onChange={(e) => setMethod(e.target.value)}
                  className="sm:w-32"
                  aria-label="HTTP method"
                >
                  {info.methods.map((m: string) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </Select>
              )}
              <Input
                value={endpoint}
                onChange={(e) => setEndpoint(e.target.value)}
                placeholder={info.endpoint}
                className="sm:flex-1"
                aria-label="Endpoint"
              />
              <Button
                onClick={handleTest}
                isLoading={loading}
                disabled={loading || (protocol === 'websocket' && wsConnected)}
                className="sm:w-auto"
              >
                {!loading &&
                  (protocol === 'websocket' && wsConnected ? (
                    <X className="h-4 w-4" />
                  ) : (
                    <Play className="h-4 w-4" />
                  ))}
                {protocol === 'websocket' && wsConnected ? 'Disconnect' : 'Test'}
              </Button>
            </div>
          </div>

          {showBody && (
            <div>
              <label className="mb-1.5 block text-sm font-medium text-foreground">
                Request body (JSON)
              </label>
              <textarea
                value={requestBody}
                onChange={(e) => setRequestBody(e.target.value)}
                placeholder={
                  protocol === 'mqtt'
                    ? '{"message": "Hello MQTT"}'
                    : protocol === 'grpc'
                    ? '{"service": "TestService", "method": "TestMethod", "data": {}}'
                    : '{"key": "value"}'
                }
                rows={6}
                className="w-full resize-y rounded-lg border border-border bg-background px-3 py-2 font-mono text-xs text-foreground placeholder:text-muted-foreground transition-colors focus-visible:border-primary/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background"
              />
            </div>
          )}

          {protocol === 'websocket' && wsConnected && (
            <div>
              <label className="mb-1.5 block text-sm font-medium text-foreground">Send message</label>
              <div className="flex items-stretch gap-2">
                <Input
                  value={wsMessage}
                  onChange={(e) => setWsMessage(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && handleWebSocketSend()}
                  placeholder="Type a message…"
                />
                <Button onClick={handleWebSocketSend} disabled={!wsMessage}>
                  <Send className="h-4 w-4" />
                  Send
                </Button>
              </div>
            </div>
          )}

          {protocol === 'websocket' && wsMessages.length > 0 && (
            <div>
              <label className="mb-1.5 block text-sm font-medium text-foreground">Messages</label>
              <div className="max-h-64 space-y-1 overflow-y-auto rounded-lg border border-border bg-muted/30 p-3 font-mono text-xs text-muted-foreground">
                {wsMessages.map((msg, idx) => (
                  <div key={idx}>{msg}</div>
                ))}
              </div>
            </div>
          )}

          {error && (
            <div className="rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">
              {error}
            </div>
          )}

          {response && (
            <div>
              <div className="mb-2 flex items-center justify-between">
                <div className="text-sm font-medium text-foreground">Response</div>
                <IconButton
                  label="Copy response"
                  size="sm"
                  onClick={async () => {
                    await navigator.clipboard.writeText(JSON.stringify(response, null, 2))
                    toast({ title: 'Response copied', variant: 'success' })
                  }}
                >
                  <Copy className="h-3.5 w-3.5" />
                </IconButton>
              </div>
              <pre className="overflow-x-auto rounded-lg border border-border bg-muted/30 p-3 font-mono text-xs leading-relaxed text-muted-foreground">
                <code>{JSON.stringify(response, null, 2)}</code>
              </pre>
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardContent className="space-y-3 p-6 text-sm text-muted-foreground">
          <h3 className="mb-1 text-base font-semibold text-foreground">Usage</h3>
          {protocol === 'http' && (
            <>
              <p>• Use GET for retrieving data</p>
              <p>• Use POST/PUT for sending data with a JSON body</p>
              <p>• Test endpoint: <code className="rounded bg-muted px-1.5 py-0.5 text-xs">/test/http</code></p>
              <p>• Example: <code className="rounded bg-muted px-1.5 py-0.5 text-xs">curl http://localhost:8000/test/http</code></p>
            </>
          )}
          {protocol === 'mcp' && (
            <>
              <p>• Test endpoint: <code className="rounded bg-muted px-1.5 py-0.5 text-xs">/test/mcp</code></p>
              <p>• Use GET or POST methods</p>
              <p>• Example: <code className="rounded bg-muted px-1.5 py-0.5 text-xs">curl http://localhost:8000/test/mcp</code></p>
            </>
          )}
          {protocol === 'a2a' && (
            <>
              <p>• Test endpoint: <code className="rounded bg-muted px-1.5 py-0.5 text-xs">/test/a2a</code></p>
              <p>• Use GET or POST methods</p>
              <p>• Example: <code className="rounded bg-muted px-1.5 py-0.5 text-xs">curl http://localhost:8000/test/a2a</code></p>
            </>
          )}
          {protocol === 'websocket' && (
            <>
              <p>• Test endpoint: <code className="rounded bg-muted px-1.5 py-0.5 text-xs">/test/websocket</code></p>
              <p>• No API key required for test endpoint</p>
              <p>• Messages are sent and received in real-time</p>
              <p>• Connection status is shown in the messages panel</p>
              <p>• Example: <code className="rounded bg-muted px-1.5 py-0.5 text-xs">ws://localhost:8000/test/websocket</code></p>
            </>
          )}
          {protocol === 'mqtt' && (
            <>
              <p>• Use POST to /mqtt/publish to send messages</p>
              <p>• Specify topic and payload in the request body</p>
              <p>• QoS levels: 0 (at most once), 1 (at least once), 2 (exactly once)</p>
            </>
          )}
          {protocol === 'grpc' && (
            <>
              <p>• Test endpoint: <code className="rounded bg-muted px-1.5 py-0.5 text-xs">/test/grpc</code></p>
              <p>• Use POST with service, method, and data in the JSON body</p>
              <p>• Example: <code className="rounded bg-muted px-1.5 py-0.5 text-xs">curl -X POST http://localhost:8000/test/grpc -d &apos;{`{"service":"Test","method":"Test"}`}&apos;</code></p>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

