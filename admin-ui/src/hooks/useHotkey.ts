import { useEffect } from 'react'

interface HotkeyOptions {
  /** Match Ctrl on Win/Linux and Cmd on Mac. */
  meta?: boolean
  shift?: boolean
  alt?: boolean
  /** Trigger even when focus is in an input/textarea. */
  ignoreEditable?: boolean
  preventDefault?: boolean
}

const isEditable = (target: EventTarget | null): boolean => {
  if (!(target instanceof HTMLElement)) return false
  const tag = target.tagName
  return (
    tag === 'INPUT' ||
    tag === 'TEXTAREA' ||
    tag === 'SELECT' ||
    target.isContentEditable
  )
}

export function useHotkey(
  key: string,
  handler: (e: KeyboardEvent) => void,
  options: HotkeyOptions = {}
) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key.toLowerCase() !== key.toLowerCase()) return
      if (options.meta && !(e.metaKey || e.ctrlKey)) return
      if (!options.meta && (e.metaKey || e.ctrlKey)) return
      if (!!options.shift !== e.shiftKey) return
      if (!!options.alt !== e.altKey) return
      if (!options.ignoreEditable && isEditable(e.target)) return
      if (options.preventDefault !== false) e.preventDefault()
      handler(e)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [key, handler, options.meta, options.shift, options.alt, options.ignoreEditable, options.preventDefault])
}
