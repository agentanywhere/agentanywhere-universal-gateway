import { useState } from 'react'
import { Play, Copy, CheckCircle, Loader2 } from 'lucide-react'
import apiClient from '../api/client'

interface ProtocolTesterProps {
  protocol: 'http' | 'websocket' | 'mqtt' | 'grpc'
  endpoint: string
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE'
}

export default function ProtocolTester({ protocol, endpoint, method = 'GET' }: ProtocolTesterProps) {
  const [requestBody, setRequestBody] = useState('')
  const [response, setResponse] = useState<any>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)

  const handleTest = async () => {
    setLoading(true)
    setError(null)
    setResponse(null)

    try {
      let result
      if (protocol === 'http') {
        const config: any = {
          method: method.toLowerCase(),
          url: endpoint,
        }
        if (requestBody && (method === 'POST' || method === 'PUT')) {
          try {
            config.data = JSON.parse(requestBody)
          } catch {
            config.data = requestBody
          }
        }
        result = await apiClient(config)
        setResponse({
          status: result.status,
          statusText: result.statusText,
          headers: result.headers,
          data: result.data,
        })
      } else if (protocol === 'websocket') {
        // WebSocket test would need a WebSocket client
        setResponse({
          message: 'WebSocket testing requires a WebSocket client. Use the browser console or a WebSocket testing tool.',
          example: `const ws = new WebSocket('${endpoint}?api_key=YOUR_KEY');
ws.onopen = () => console.log('Connected');
ws.onmessage = (e) => console.log('Message:', e.data);
ws.send('Hello');`,
        })
      } else if (protocol === 'mqtt') {
        if (method === 'POST') {
          result = await apiClient.post('/mqtt/publish', {
            topic: 'test/topic',
            payload: requestBody || { message: 'Hello MQTT' },
            qos: 1,
          })
          setResponse(result.data)
        }
      }
    } catch (err: any) {
      setError(err.response?.data?.detail || err.message || 'Request failed')
    } finally {
      setLoading(false)
    }
  }

  const handleCopy = (text: string) => {
    navigator.clipboard.writeText(text)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <input
          type="text"
          value={endpoint}
          readOnly
          className="flex-1 px-4 py-2 border border-border rounded-lg bg-background text-foreground text-sm"
        />
        {protocol === 'http' && (
          <select
            value={method}
            onChange={(e) => {}}
            className="px-3 py-2 border border-border rounded-lg bg-background text-foreground"
          >
            <option>GET</option>
            <option>POST</option>
            <option>PUT</option>
            <option>DELETE</option>
          </select>
        )}
        <button
          onClick={handleTest}
          disabled={loading}
          className="flex items-center gap-2 px-4 py-2 bg-primary text-primary-foreground rounded-lg hover:bg-primary/90 transition-colors disabled:opacity-50"
        >
          {loading ? (
            <Loader2 className="w-4 h-4 animate-spin" />
          ) : (
            <Play className="w-4 h-4" />
          )}
          Test
        </button>
      </div>

      {(method === 'POST' || method === 'PUT') && protocol === 'http' && (
        <div>
          <label className="block text-sm font-medium text-foreground mb-2">Request Body (JSON)</label>
          <textarea
            value={requestBody}
            onChange={(e) => setRequestBody(e.target.value)}
            placeholder='{"key": "value"}'
            rows={4}
            className="w-full px-4 py-2 border border-border rounded-lg bg-background text-foreground font-mono text-sm"
          />
        </div>
      )}

      {error && (
        <div className="p-4 rounded-lg bg-destructive/10 border border-destructive/20">
          <p className="text-sm text-destructive">{error}</p>
        </div>
      )}

      {response && (
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <h4 className="text-sm font-semibold text-foreground">Response</h4>
            <button
              onClick={() => handleCopy(JSON.stringify(response, null, 2))}
              className="p-1.5 rounded hover:bg-accent transition-colors"
            >
              {copied ? (
                <CheckCircle className="w-4 h-4 text-green-500" />
              ) : (
                <Copy className="w-4 h-4 text-muted-foreground" />
              )}
            </button>
          </div>
          <pre className="p-4 rounded-lg bg-background/50 border border-border text-xs text-muted-foreground overflow-x-auto">
            <code>{JSON.stringify(response, null, 2)}</code>
          </pre>
        </div>
      )}
    </div>
  )
}


