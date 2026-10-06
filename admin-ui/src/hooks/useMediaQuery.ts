import { useEffect, useState } from 'react'

export function useMediaQuery(query: string): boolean {
  const get = () =>
    typeof window === 'undefined' ? false : window.matchMedia(query).matches

  const [matches, setMatches] = useState<boolean>(get)

  useEffect(() => {
    if (typeof window === 'undefined') return
    const mql = window.matchMedia(query)
    const listener = () => setMatches(mql.matches)
    listener()
    mql.addEventListener('change', listener)
    return () => mql.removeEventListener('change', listener)
  }, [query])

  return matches
}
