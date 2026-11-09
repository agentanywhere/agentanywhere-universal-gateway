import { describe, expect, it } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { useTimeSeries } from './useTimeSeries'

type Snap = Parameters<typeof useTimeSeries>[0]

function setup(initial: Snap) {
  return renderHook(({ s }: { s: Snap }) => useTimeSeries(s), { initialProps: { s: initial } })
}

describe('useTimeSeries', () => {
  it('is empty without a snapshot', () => {
    const { result } = setup(undefined)
    expect(result.current.series).toEqual([])
  })

  it('seeds one point from requests_per_minute on the first snapshot', () => {
    const { result } = setup({ total_requests: 100, total_errors: 1, avg_response_time: 12, requests_per_minute: 7 })
    expect(result.current.series).toHaveLength(1)
    expect(result.current.series[0]).toMatchObject({ requests: 7, errors: 0, latency: 12 })
  })

  it('appends per-tick deltas on later snapshots', () => {
    const { result, rerender } = setup({ total_requests: 100, total_errors: 1, avg_response_time: 10 })
    rerender({ s: { total_requests: 130, total_errors: 4, avg_response_time: 20 } })
    expect(result.current.series).toHaveLength(2)
    expect(result.current.series[1]).toMatchObject({ requests: 30, errors: 3, latency: 20 })
  })

  it('clamps negative deltas (counter reset) to zero', () => {
    const { result, rerender } = setup({ total_requests: 100, total_errors: 10 })
    rerender({ s: { total_requests: 5, total_errors: 0 } })
    expect(result.current.series[1]).toMatchObject({ requests: 0, errors: 0 })
  })

  it('defaults missing fields to zero', () => {
    const { result, rerender } = setup({})
    rerender({ s: {} })
    expect(result.current.series[0]).toMatchObject({ requests: 0, latency: 0 })
    expect(result.current.series[1]).toMatchObject({ requests: 0, errors: 0, latency: 0 })
  })

  it('keeps at most 60 points', () => {
    const { result, rerender } = setup({ total_requests: 0 })
    for (let i = 1; i <= 80; i++) rerender({ s: { total_requests: i } })
    expect(result.current.series).toHaveLength(60)
  })

  it('reset clears the series and re-seeds on the next snapshot', () => {
    const { result, rerender } = setup({ total_requests: 10, requests_per_minute: 3 })
    rerender({ s: { total_requests: 20 } })
    expect(result.current.series).toHaveLength(2)
    act(() => result.current.reset())
    expect(result.current.series).toEqual([])
    rerender({ s: { total_requests: 50, requests_per_minute: 9 } })
    expect(result.current.series).toHaveLength(1)
    expect(result.current.series[0].requests).toBe(9)
  })
})
