import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

vi.mock('../api/client', () => {
  const fn: any = vi.fn()
  fn.post = vi.fn()
  return { default: fn }
})
import apiClient from '../api/client'
import ProtocolTester from './ProtocolTester'

const client = apiClient as unknown as ReturnType<typeof vi.fn> & { post: ReturnType<typeof vi.fn> }

describe('ProtocolTester', () => {
  beforeEach(() => {
    client.mockReset().mockResolvedValue({ status: 200, statusText: 'OK', headers: { a: 'b' }, data: { ok: true } })
    client.post.mockReset().mockResolvedValue({ data: { published: true } })
  })

  it('renders the endpoint read-only and the method selector for http', () => {
    render(<ProtocolTester protocol="http" endpoint="/test/http" />)
    expect(screen.getByDisplayValue('/test/http')).toHaveAttribute('readonly')
    expect(screen.getByRole('combobox')).toBeInTheDocument()
    expect(screen.queryByText('Request Body (JSON)')).toBeNull()
  })

  it('sends an http request and renders the response, with copy feedback', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    render(<ProtocolTester protocol="http" endpoint="/test/http" />)
    fireEvent.click(screen.getByRole('button', { name: /Test/ }))
    expect(await screen.findByText(/"ok": true/)).toBeInTheDocument()
    expect(client).toHaveBeenCalledWith({ method: 'get', url: '/test/http' })
    fireEvent.click(screen.getByRole('heading', { name: 'Response' }).nextElementSibling as HTMLElement)
    expect(writeText).toHaveBeenCalled()
    act(() => {
      vi.advanceTimersByTime(2100)
    })
    vi.useRealTimers()
  })

  it('shows body input for POST and parses JSON or falls back to raw text', async () => {
    render(<ProtocolTester protocol="http" endpoint="/x" method="POST" />)
    const body = screen.getByPlaceholderText('{"key": "value"}')
    fireEvent.change(body, { target: { value: '{"a":1}' } })
    await userEvent.click(screen.getByRole('button', { name: /Test/ }))
    await waitFor(() => expect(client).toHaveBeenLastCalledWith({ method: 'post', url: '/x', data: { a: 1 } }))
    fireEvent.change(body, { target: { value: 'raw' } })
    await userEvent.click(screen.getByRole('button', { name: /Test/ }))
    await waitFor(() => expect(client).toHaveBeenLastCalledWith({ method: 'post', url: '/x', data: 'raw' }))
  })

  it('shows request errors', async () => {
    client.mockRejectedValue({ response: { data: { detail: 'nope' } } })
    render(<ProtocolTester protocol="http" endpoint="/x" />)
    await userEvent.click(screen.getByRole('button', { name: /Test/ }))
    expect(await screen.findByText('nope')).toBeInTheDocument()
    client.mockRejectedValue(new Error('boom'))
    await userEvent.click(screen.getByRole('button', { name: /Test/ }))
    expect(await screen.findByText('boom')).toBeInTheDocument()
    client.mockRejectedValue({})
    await userEvent.click(screen.getByRole('button', { name: /Test/ }))
    expect(await screen.findByText('Request failed')).toBeInTheDocument()
  })

  it('explains websocket testing instead of connecting', async () => {
    render(<ProtocolTester protocol="websocket" endpoint="ws://x/ws" />)
    await userEvent.click(screen.getByRole('button', { name: /Test/ }))
    expect(await screen.findByText(/requires a WebSocket client/)).toBeInTheDocument()
    expect(client).not.toHaveBeenCalled()
  })

  it('publishes via POST for mqtt and ignores other methods', async () => {
    const { unmount } = render(<ProtocolTester protocol="mqtt" endpoint="/mqtt/publish" method="POST" />)
    await userEvent.click(screen.getByRole('button', { name: /Test/ }))
    await waitFor(() => expect(client.post).toHaveBeenCalledWith('/mqtt/publish', expect.objectContaining({ topic: 'test/topic' })))
    expect(await screen.findByText(/"published": true/)).toBeInTheDocument()
    unmount()
    render(<ProtocolTester protocol="mqtt" endpoint="/mqtt/publish" />)
    client.post.mockClear()
    await userEvent.click(screen.getByRole('button', { name: /Test/ }))
    expect(client.post).not.toHaveBeenCalled()
  })
})
