import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ToastProvider, useToast } from './Toast'

function Trigger({ variant }: { variant?: 'success' | 'error' | 'warning' | 'info' }) {
  const { toast } = useToast()
  return (
    <button onClick={() => toast({ title: 'Hello', description: 'World', variant })}>fire</button>
  )
}

describe('Toast', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('throws when useToast is used outside the provider', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    expect(() => render(<Trigger />)).toThrow(/ToastProvider/)
    spy.mockRestore()
  })

  it('shows a toast with title and description', async () => {
    render(
      <ToastProvider>
        <Trigger variant="success" />
      </ToastProvider>
    )
    await userEvent.click(screen.getByText('fire'))
    expect(screen.getByRole('status')).toBeInTheDocument()
    expect(screen.getByText('Hello')).toBeInTheDocument()
    expect(screen.getByText('World')).toBeInTheDocument()
  })

  it.each(['success', 'error', 'warning', 'info', undefined] as const)('renders variant %s', async (v) => {
    render(
      <ToastProvider>
        <Trigger variant={v} />
      </ToastProvider>
    )
    await userEvent.click(screen.getByText('fire'))
    expect(screen.getByRole('status').className).toContain(
      { success: 'emerald', error: 'red', warning: 'amber', info: 'sky' }[v ?? 'info']
    )
  })

  it('dismisses via the dismiss button', async () => {
    render(
      <ToastProvider>
        <Trigger />
      </ToastProvider>
    )
    await userEvent.click(screen.getByText('fire'))
    await userEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('auto-dismisses after 4 seconds', () => {
    vi.useFakeTimers()
    render(
      <ToastProvider>
        <Trigger />
      </ToastProvider>
    )
    act(() => {
      screen.getByText('fire').click()
    })
    expect(screen.getByRole('status')).toBeInTheDocument()
    act(() => {
      vi.advanceTimersByTime(3999)
    })
    expect(screen.getByRole('status')).toBeInTheDocument()
    act(() => {
      vi.advanceTimersByTime(2)
    })
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('stacks multiple toasts', async () => {
    render(
      <ToastProvider>
        <Trigger />
      </ToastProvider>
    )
    await userEvent.click(screen.getByText('fire'))
    await userEvent.click(screen.getByText('fire'))
    expect(screen.getAllByRole('status')).toHaveLength(2)
  })
})
