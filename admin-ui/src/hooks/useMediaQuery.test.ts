import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { useMediaQuery } from './useMediaQuery'

function mockMatchMedia(initial: boolean) {
  let listener: (() => void) | null = null
  const mql = {
    matches: initial,
    addEventListener: vi.fn((_: string, l: () => void) => {
      listener = l
    }),
    removeEventListener: vi.fn(),
  }
  const original = window.matchMedia
  window.matchMedia = vi.fn(() => mql) as unknown as typeof window.matchMedia
  return {
    mql,
    fire(next: boolean) {
      mql.matches = next
      listener?.()
    },
    restore: () => {
      window.matchMedia = original
    },
  }
}

describe('useMediaQuery', () => {
  let restore: () => void = () => {}
  afterEach(() => restore())

  it('returns the initial match state', () => {
    const m = mockMatchMedia(true)
    restore = m.restore
    const { result } = renderHook(() => useMediaQuery('(min-width: 1024px)'))
    expect(result.current).toBe(true)
  })

  it('updates when the media query changes', () => {
    const m = mockMatchMedia(false)
    restore = m.restore
    const { result } = renderHook(() => useMediaQuery('(min-width: 1024px)'))
    expect(result.current).toBe(false)
    act(() => m.fire(true))
    expect(result.current).toBe(true)
  })

  it('unsubscribes on unmount', () => {
    const m = mockMatchMedia(false)
    restore = m.restore
    const { unmount } = renderHook(() => useMediaQuery('(min-width: 1024px)'))
    unmount()
    expect(m.mql.removeEventListener).toHaveBeenCalledWith('change', expect.any(Function))
  })
})
