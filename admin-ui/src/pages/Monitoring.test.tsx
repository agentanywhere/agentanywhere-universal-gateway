import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'

vi.mock('../api/agents', () => ({
  agentsApi: { healthSnapshot: vi.fn(), streamUrl: vi.fn(() => '/admin/agents/stream?token=t') },
}))
import { agentsApi } from '../api/agents'
import Monitoring from './Monitoring'

class FakeEventSource {
  static instances: FakeEventSource[] = []
  onopen: (() => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  closed = false
  constructor(public url: string) {
    FakeEventSource.instances.push(this)
  }
  close() {
    this.closed = true
  }
}

const agent = (over: Record<string, unknown> = {}) => ({
  slug: 'alpha',
  upstream_base: 'http://alpha:1',
  status: 'up',
  latency_ms: 12,
  status_code: 200,
  error: null,
  last_checked: Date.now() / 1000,
  consecutive_failures: 0,
  uptime_pct: 99.9,
  sparkline: [1, 3, 2, 5],
  req_per_min: 42,
  error_pct: 0,
  total_requests: 100,
  avg_latency_ms: 10,
  ...over,
})

const board = (agents: any[], down = 0) => ({
  generated_at: Date.now() / 1000,
  summary: { total: agents.length, up: agents.length - down, down, unknown: 0, req_per_min: 42 },
  agents,
})

describe('Monitoring page', () => {
  beforeEach(() => {
    FakeEventSource.instances = []
    vi.stubGlobal('EventSource', FakeEventSource)
    vi.mocked(agentsApi.healthSnapshot).mockReset()
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  const es = () => FakeEventSource.instances[0]

  it('starts in the connecting state with a placeholder', () => {
    render(<Monitoring />)
    expect(screen.getByText('Connecting')).toBeInTheDocument()
    expect(screen.getByText('Connecting to live stream…')).toBeInTheDocument()
    expect(es().url).toBe('/admin/agents/stream?token=t')
  })

  it('goes live on open and renders streamed agents sorted down-first', () => {
    render(<Monitoring />)
    act(() => es().onopen?.())
    expect(screen.getByText('Live')).toBeInTheDocument()
    act(() =>
      es().onmessage?.({
        data: JSON.stringify(
          board(
            [
              agent({ slug: 'zeta' }),
              agent({ slug: 'beta', status: 'down', error: 'connection refused', latency_ms: null, uptime_pct: null, error_pct: 12, last_checked: null, sparkline: [] }),
              agent({ slug: 'gamma', status: 'unknown', last_checked: Date.now() / 1000 - 300 }),
            ],
            1
          )
        ),
      })
    )
    const names = screen.getAllByText(/^(zeta|beta|gamma)$/).map((n) => n.textContent)
    expect(names).toEqual(['beta', 'gamma', 'zeta'])
    expect(screen.getByText('connection refused')).toBeInTheDocument()
    expect(screen.getByText(/1 agent unreachable/)).toBeInTheDocument()
    expect(screen.getByText('12% errors')).toBeInTheDocument()
    expect(screen.getAllByText('no errors').length).toBe(2)
    expect(screen.getByText('checked never')).toBeInTheDocument()
    expect(screen.getByText('checked 5m ago')).toBeInTheDocument()
    expect(screen.getByText('Up')).toBeInTheDocument()
    expect(screen.getAllByText('Down').length).toBe(2) // stat label + badge
    expect(screen.getByText('Unknown')).toBeInTheDocument()
  })

  it('pluralises the unreachable banner', () => {
    render(<Monitoring />)
    act(() =>
      es().onmessage?.({
        data: JSON.stringify(board([agent({ status: 'down' }), agent({ slug: 'b', status: 'down' })], 2)),
      })
    )
    expect(screen.getByText(/2 agents unreachable/)).toBeInTheDocument()
  })

  it('shows an empty board message when connected with no agents', () => {
    render(<Monitoring />)
    act(() => es().onmessage?.({ data: JSON.stringify(board([])) }))
    expect(screen.getByText('No agents to monitor')).toBeInTheDocument()
  })

  it('ignores malformed frames', () => {
    render(<Monitoring />)
    act(() => es().onmessage?.({ data: '{not json' }))
    expect(screen.getByText('Connecting to live stream…')).toBeInTheDocument()
  })

  it('falls back to polling the snapshot endpoint when the stream errors', async () => {
    vi.useFakeTimers()
    vi.mocked(agentsApi.healthSnapshot).mockResolvedValue(board([agent({ slug: 'polled' })]) as any)
    render(<Monitoring />)
    await act(async () => {
      es().onerror?.()
    })
    expect(es().closed).toBe(true)
    expect(screen.getByText('Polling')).toBeInTheDocument()
    expect(screen.getByText('polled')).toBeInTheDocument()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000)
    })
    expect(vi.mocked(agentsApi.healthSnapshot).mock.calls.length).toBeGreaterThanOrEqual(2)
  })

  it('keeps the last board when a poll fails', async () => {
    vi.useFakeTimers()
    vi.mocked(agentsApi.healthSnapshot).mockRejectedValue(new Error('x'))
    render(<Monitoring />)
    await act(async () => {
      es().onerror?.()
    })
    expect(screen.getByText('Polling')).toBeInTheDocument()
    expect(screen.getByText('Connecting to live stream…')).toBeInTheDocument()
  })

  it('does not start polling twice and cleans up on unmount', async () => {
    vi.useFakeTimers()
    vi.mocked(agentsApi.healthSnapshot).mockResolvedValue(board([]) as any)
    const { unmount } = render(<Monitoring />)
    await act(async () => {
      es().onerror?.()
    })
    const calls = vi.mocked(agentsApi.healthSnapshot).mock.calls.length
    unmount()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(9000)
    })
    expect(vi.mocked(agentsApi.healthSnapshot).mock.calls.length).toBe(calls)
  })

  it('falls back to polling immediately when EventSource construction throws', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('EventSource', function () {
      throw new Error('unsupported')
    })
    vi.mocked(agentsApi.healthSnapshot).mockResolvedValue(board([agent()]) as any)
    render(<Monitoring />)
    await act(async () => {})
    expect(screen.getByText('Polling')).toBeInTheDocument()
  })

  it('closes the stream on unmount', () => {
    const { unmount } = render(<Monitoring />)
    unmount()
    expect(es().closed).toBe(true)
  })
})
