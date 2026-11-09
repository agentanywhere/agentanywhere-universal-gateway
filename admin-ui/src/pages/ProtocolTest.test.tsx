import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes, useLocation } from 'react-router-dom'
import { renderWithProviders } from '../test/utils'

vi.mock('../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))
import apiClient from '../api/client'
import ProtocolTest from './ProtocolTest'

const c = apiClient as unknown as Record<'get' | 'post' | 'put' | 'delete', ReturnType<typeof vi.fn>>

function Where() {
  return <div data-testid="where">{useLocation().pathname}</div>
}

function renderProto(protocol: string) {
  return renderWithProviders(
    <Routes>
      <Route path="/test/:protocol" element={<ProtocolTest />} />
      <Route path="/protocols" element={<Where />} />
    </Routes>,
    { route: `/test/${protocol}` }
  )
}

const ok = (data: unknown = { hello: 'world' }) => ({ status: 200, statusText: 'OK', data })

class FakeWS {
  static last: FakeWS | null = null
  onopen: (() => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  onclose: (() => void) | null = null
  sent: string[] = []
  closed = false
  constructor(public url: string) {
    FakeWS.last = this
  }
  send(m: string) {
    this.sent.push(m)
  }
  close() {
    this.closed = true
  }
}

describe('ProtocolTest page', () => {
  beforeEach(() => {
    for (const fn of Object.values(c)) fn.mockReset().mockResolvedValue(ok())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('shows protocol info, prefilled endpoint and usage; back navigates', async () => {
    renderProto('http')
    expect(screen.getByRole('heading', { name: 'Test HTTP/REST' })).toBeInTheDocument()
    expect(screen.getByLabelText('Endpoint')).toHaveValue('/test/http')
    expect(screen.getByText(/Use GET for retrieving data/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Back to protocols' }))
    expect(await screen.findByTestId('where')).toHaveTextContent('/protocols')
  })

  it('falls back to HTTP info for an unknown protocol', () => {
    renderProto('bogus')
    expect(screen.getByRole('heading', { name: 'Test HTTP/REST' })).toBeInTheDocument()
  })

  describe('http', () => {
    it('GET shows the response and copies it', async () => {
      const user = userEvent.setup()
      const writeText = vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue()
      renderProto('http')
      await user.click(screen.getByRole('button', { name: 'Test' }))
      expect(await screen.findByText(/"hello": "world"/)).toBeInTheDocument()
      expect(c.get).toHaveBeenCalledWith('/test/http')
      await user.click(screen.getByRole('button', { name: 'Copy response' }))
      expect(writeText).toHaveBeenCalled()
      expect(await screen.findByText('Response copied')).toBeInTheDocument()
    })

    it.each([
      ['POST', 'post'],
      ['PUT', 'put'],
    ] as const)('%s sends a parsed JSON body', async (method, fn) => {
      renderProto('http')
      await userEvent.selectOptions(screen.getByLabelText('HTTP method'), method)
      fireEvent.change(screen.getByPlaceholderText('{"key": "value"}'), { target: { value: '{"a":1}' } })
      await userEvent.click(screen.getByRole('button', { name: 'Test' }))
      await waitFor(() => expect(c[fn]).toHaveBeenCalledWith('/test/http', { a: 1 }))
    })

    it('sends a non-JSON body as raw text and an empty body as {}', async () => {
      renderProto('http')
      await userEvent.selectOptions(screen.getByLabelText('HTTP method'), 'POST')
      await userEvent.click(screen.getByRole('button', { name: 'Test' }))
      await waitFor(() => expect(c.post).toHaveBeenCalledWith('/test/http', {}))
      fireEvent.change(screen.getByPlaceholderText('{"key": "value"}'), { target: { value: 'plain' } })
      await userEvent.click(screen.getByRole('button', { name: 'Test' }))
      await waitFor(() => expect(c.post).toHaveBeenLastCalledWith('/test/http', 'plain'))
    })

    it('DELETE calls delete', async () => {
      renderProto('http')
      await userEvent.selectOptions(screen.getByLabelText('HTTP method'), 'DELETE')
      await userEvent.click(screen.getByRole('button', { name: 'Test' }))
      await waitFor(() => expect(c.delete).toHaveBeenCalledWith('/test/http'))
    })

    it('shows the server error detail, then the message, on failure', async () => {
      c.get.mockRejectedValueOnce({ response: { data: { detail: 'forbidden' } } })
      renderProto('http')
      await userEvent.click(screen.getByRole('button', { name: 'Test' }))
      expect(await screen.findByText('forbidden')).toBeInTheDocument()
      c.get.mockRejectedValueOnce(new Error('Network Error'))
      await userEvent.click(screen.getByRole('button', { name: 'Test' }))
      expect(await screen.findByText('Network Error')).toBeInTheDocument()
    })

    it('reports a JSON parse error for a malformed body', async () => {
      renderProto('http')
      await userEvent.selectOptions(screen.getByLabelText('HTTP method'), 'POST')
      fireEvent.change(screen.getByPlaceholderText('{"key": "value"}'), { target: { value: '{bad' } })
      await userEvent.click(screen.getByRole('button', { name: 'Test' }))
      expect((await screen.findAllByText(/JSON/)).length).toBeGreaterThan(0)
      expect(c.post).not.toHaveBeenCalled()
    })

    it('uses an edited endpoint', async () => {
      renderProto('http')
      fireEvent.change(screen.getByLabelText('Endpoint'), { target: { value: '/custom' } })
      await userEvent.click(screen.getByRole('button', { name: 'Test' }))
      await waitFor(() => expect(c.get).toHaveBeenCalledWith('/custom'))
    })
  })

  describe.each([
    ['mcp', '/test/mcp'],
    ['a2a', '/test/a2a'],
  ])('%s', (proto, endpoint) => {
    it('GET and POST', async () => {
      renderProto(proto)
      await userEvent.click(screen.getByRole('button', { name: 'Test' }))
      await waitFor(() => expect(c.get).toHaveBeenCalledWith(endpoint))
      await userEvent.selectOptions(screen.getByLabelText('HTTP method'), 'POST')
      fireEvent.change(screen.getByPlaceholderText('{"key": "value"}'), { target: { value: '{"q":1}' } })
      await userEvent.click(screen.getByRole('button', { name: 'Test' }))
      await waitFor(() => expect(c.post).toHaveBeenCalledWith(endpoint, { q: 1 }))
    })

    it('shows errors', async () => {
      c.get.mockRejectedValue({ response: { data: { detail: 'bad' } } })
      renderProto(proto)
      await userEvent.click(screen.getByRole('button', { name: 'Test' }))
      expect(await screen.findByText('bad')).toBeInTheDocument()
    })
  })

  describe('mqtt', () => {
    it('publishes the default payload', async () => {
      renderProto('mqtt')
      await userEvent.click(screen.getByRole('button', { name: 'Test' }))
      await waitFor(() =>
        expect(c.post).toHaveBeenCalledWith('/mqtt/publish', {
          topic: 'test/topic',
          payload: { message: 'Hello MQTT' },
          qos: 1,
        })
      )
    })

    it('publishes a custom payload and shows errors', async () => {
      renderProto('mqtt')
      fireEvent.change(screen.getByPlaceholderText('{"message": "Hello MQTT"}'), { target: { value: '{"x":2}' } })
      await userEvent.click(screen.getByRole('button', { name: 'Test' }))
      await waitFor(() => expect(c.post).toHaveBeenCalledWith('/mqtt/publish', expect.objectContaining({ payload: { x: 2 } })))
      c.post.mockRejectedValue(new Error('broker down'))
      await userEvent.click(screen.getByRole('button', { name: 'Test' }))
      expect(await screen.findByText('broker down')).toBeInTheDocument()
    })
  })

  describe('grpc', () => {
    it('posts the default and custom bodies', async () => {
      renderProto('grpc')
      await userEvent.click(screen.getByRole('button', { name: 'Test' }))
      await waitFor(() =>
        expect(c.post).toHaveBeenCalledWith('/test/grpc', { service: 'TestService', method: 'TestMethod', data: {} })
      )
      fireEvent.change(screen.getByPlaceholderText(/TestService/), { target: { value: '{"service":"S"}' } })
      await userEvent.click(screen.getByRole('button', { name: 'Test' }))
      await waitFor(() => expect(c.post).toHaveBeenLastCalledWith('/test/grpc', { service: 'S' }))
    })

    it('shows errors', async () => {
      c.post.mockRejectedValue(new Error('unavailable'))
      renderProto('grpc')
      await userEvent.click(screen.getByRole('button', { name: 'Test' }))
      expect(await screen.findByText('unavailable')).toBeInTheDocument()
    })
  })

  describe('websocket', () => {
    beforeEach(() => {
      FakeWS.last = null
      vi.stubGlobal('WebSocket', FakeWS)
      localStorage.setItem('admin_token', 'tok')
    })

    it('connects, exchanges messages and disconnects', async () => {
      renderProto('websocket')
      await userEvent.click(screen.getByRole('button', { name: 'Test' }))
      const ws = FakeWS.last!
      expect(ws.url).toBe('ws://localhost:8000/test/websocket?api_key=tok')
      act(() => ws.onopen?.())
      expect(await screen.findByText('✅ Connected')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Disconnect' })).toBeDisabled()

      act(() => ws.onmessage?.({ data: 'pong' }))
      expect(screen.getByText('📨 pong')).toBeInTheDocument()

      const input = screen.getByPlaceholderText('Type a message…')
      expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
      fireEvent.change(input, { target: { value: 'hi' } })
      await userEvent.click(screen.getByRole('button', { name: 'Send' }))
      expect(ws.sent).toEqual(['hi'])
      expect(screen.getByText('📤 Sent: hi')).toBeInTheDocument()
      expect(input).toHaveValue('')

      fireEvent.change(input, { target: { value: 'again' } })
      fireEvent.keyDown(input, { key: 'Enter' })
      expect(ws.sent).toEqual(['hi', 'again'])

      act(() => ws.onclose?.())
      expect(await screen.findByText('🔌 Disconnected')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Test' })).toBeEnabled()
    })

    it('uses an absolute ws:// endpoint as-is and surfaces connection errors', async () => {
      renderProto('websocket')
      fireEvent.change(screen.getByLabelText('Endpoint'), { target: { value: 'wss://example.test/ws' } })
      await userEvent.click(screen.getByRole('button', { name: 'Test' }))
      expect(FakeWS.last!.url).toBe('wss://example.test/ws?api_key=tok')
      act(() => FakeWS.last!.onerror?.())
      expect(await screen.findByText('WebSocket connection error')).toBeInTheDocument()
      expect(screen.getByText('❌ Connection error')).toBeInTheDocument()
    })
  })
})
