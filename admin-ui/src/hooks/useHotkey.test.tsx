import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, renderHook } from '@testing-library/react'
import { useHotkey } from './useHotkey'

describe('useHotkey', () => {
  it('fires the handler on a matching key (case-insensitive)', () => {
    const handler = vi.fn()
    renderHook(() => useHotkey('k', handler))
    fireEvent.keyDown(window, { key: 'K' })
    expect(handler).toHaveBeenCalledTimes(1)
  })

  it('ignores other keys', () => {
    const handler = vi.fn()
    renderHook(() => useHotkey('k', handler))
    fireEvent.keyDown(window, { key: 'j' })
    expect(handler).not.toHaveBeenCalled()
  })

  it('requires ctrl or meta when meta option is set, and rejects them otherwise', () => {
    const withMeta = vi.fn()
    const without = vi.fn()
    renderHook(() => useHotkey('k', withMeta, { meta: true }))
    renderHook(() => useHotkey('k', without))
    fireEvent.keyDown(window, { key: 'k' })
    expect(withMeta).not.toHaveBeenCalled()
    expect(without).toHaveBeenCalledTimes(1)
    fireEvent.keyDown(window, { key: 'k', ctrlKey: true })
    fireEvent.keyDown(window, { key: 'k', metaKey: true })
    expect(withMeta).toHaveBeenCalledTimes(2)
    expect(without).toHaveBeenCalledTimes(1)
  })

  it('respects shift and alt modifiers exactly', () => {
    const handler = vi.fn()
    renderHook(() => useHotkey('k', handler, { shift: true, alt: true }))
    fireEvent.keyDown(window, { key: 'k', shiftKey: true })
    expect(handler).not.toHaveBeenCalled()
    fireEvent.keyDown(window, { key: 'k', shiftKey: true, altKey: true })
    expect(handler).toHaveBeenCalledTimes(1)
  })

  it('skips editable targets unless ignoreEditable is set', () => {
    const blocked = vi.fn()
    const allowed = vi.fn()
    renderHook(() => useHotkey('/', blocked))
    renderHook(() => useHotkey('/', allowed, { ignoreEditable: true }))
    const { container } = render(<><input data-testid="i" /><textarea /><select /></>)
    for (const el of Array.from(container.children)) fireEvent.keyDown(el, { key: '/' })
    expect(blocked).not.toHaveBeenCalled()
    expect(allowed).toHaveBeenCalledTimes(3)
  })

  it('prevents default unless disabled', () => {
    renderHook(() => useHotkey('a', () => {}))
    const e1 = new KeyboardEvent('keydown', { key: 'a', cancelable: true })
    window.dispatchEvent(e1)
    expect(e1.defaultPrevented).toBe(true)

    renderHook(() => useHotkey('b', () => {}, { preventDefault: false }))
    const e2 = new KeyboardEvent('keydown', { key: 'b', cancelable: true })
    window.dispatchEvent(e2)
    expect(e2.defaultPrevented).toBe(false)
  })

  it('removes the listener on unmount', () => {
    const handler = vi.fn()
    const { unmount } = renderHook(() => useHotkey('k', handler))
    unmount()
    fireEvent.keyDown(window, { key: 'k' })
    expect(handler).not.toHaveBeenCalled()
  })
})
