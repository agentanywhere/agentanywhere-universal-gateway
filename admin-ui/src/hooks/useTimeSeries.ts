import { useEffect, useRef, useState } from 'react'

export interface SeriesPoint {
  t: number
  requests: number
  errors: number
  latency: number
}

interface SnapshotInput {
  total_requests?: number
  total_errors?: number
  avg_response_time?: number
  requests_per_minute?: number
}

const MAX_POINTS = 60

/**
 * Builds a rolling client-side time series from successive Dashboard snapshots.
 * Uses delta-from-previous so we always show real per-tick activity instead of
 * a monotonically increasing counter.
 */
export function useTimeSeries(snapshot: SnapshotInput | undefined): {
  series: SeriesPoint[]
  reset: () => void
} {
  const [series, setSeries] = useState<SeriesPoint[]>([])
  const lastTotals = useRef<{ requests: number; errors: number } | null>(null)

  useEffect(() => {
    if (!snapshot) return
    const requestsTotal = snapshot.total_requests ?? 0
    const errorsTotal = snapshot.total_errors ?? 0
    const latency = snapshot.avg_response_time ?? 0

    const prev = lastTotals.current
    if (!prev) {
      lastTotals.current = { requests: requestsTotal, errors: errorsTotal }
      // Seed with a single point so the chart isn't empty.
      setSeries([
        {
          t: Date.now(),
          requests: snapshot.requests_per_minute ?? 0,
          errors: 0,
          latency,
        },
      ])
      return
    }

    const dRequests = Math.max(0, requestsTotal - prev.requests)
    const dErrors = Math.max(0, errorsTotal - prev.errors)
    lastTotals.current = { requests: requestsTotal, errors: errorsTotal }

    setSeries((s) => {
      const next: SeriesPoint = {
        t: Date.now(),
        requests: dRequests,
        errors: dErrors,
        latency,
      }
      const merged = [...s, next]
      return merged.length > MAX_POINTS ? merged.slice(merged.length - MAX_POINTS) : merged
    })
  }, [snapshot])

  return {
    series,
    reset: () => {
      lastTotals.current = null
      setSeries([])
    },
  }
}
