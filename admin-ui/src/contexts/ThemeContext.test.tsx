import { describe, expect, it, vi } from 'vitest'
import { act, render, renderHook, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ThemeProvider, useTheme } from './ThemeContext'

function Probe() {
  const { theme, toggleTheme, setTheme } = useTheme()
  return (
    <div>
      <span data-testid="theme">{theme}</span>
      <button onClick={toggleTheme}>toggle</button>
      <button onClick={() => setTheme('dark')}>dark</button>
    </div>
  )
}

function setSystemDark(dark: boolean) {
  window.matchMedia = ((q: string) => ({
    matches: dark && q.includes('dark'),
    media: q,
    addEventListener() {},
    removeEventListener() {},
  })) as unknown as typeof window.matchMedia
}

describe('ThemeContext', () => {
  it('throws outside a provider', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    expect(() => renderHook(() => useTheme())).toThrow(/ThemeProvider/)
    spy.mockRestore()
  })

  it('uses the saved theme from localStorage', () => {
    localStorage.setItem('theme', 'dark')
    render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>
    )
    expect(screen.getByTestId('theme')).toHaveTextContent('dark')
    expect(document.documentElement).toHaveClass('dark')
  })

  it('defaults to Sovereign Dark, even when the system prefers light', () => {
    setSystemDark(false)
    render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>
    )
    expect(screen.getByTestId('theme')).toHaveTextContent('dark')
    expect(document.documentElement).toHaveClass('dark')
  })

  it('keeps a saved light choice', () => {
    localStorage.setItem('theme', 'light')
    render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>
    )
    expect(screen.getByTestId('theme')).toHaveTextContent('light')
    expect(document.documentElement).toHaveClass('light')
  })

  it('toggles, applies the root class and persists', async () => {
    setSystemDark(false)
    render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>
    )
    await userEvent.click(screen.getByText('toggle'))
    expect(screen.getByTestId('theme')).toHaveTextContent('light')
    expect(document.documentElement).toHaveClass('light')
    expect(document.documentElement).not.toHaveClass('dark')
    expect(localStorage.getItem('theme')).toBe('light')
    await userEvent.click(screen.getByText('toggle'))
    expect(localStorage.getItem('theme')).toBe('dark')
  })

  it('setTheme sets an explicit theme', async () => {
    setSystemDark(false)
    const { result } = renderHook(() => useTheme(), { wrapper: ThemeProvider })
    act(() => result.current.setTheme('light'))
    expect(result.current.theme).toBe('light')
  })
})
