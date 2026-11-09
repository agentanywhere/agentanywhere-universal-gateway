import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Dialog } from './Dialog'

describe('Dialog', () => {
  it('renders nothing when closed', () => {
    render(
      <Dialog open={false} onClose={() => {}} title="T">
        body
      </Dialog>
    )
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('renders title, description, body and footer in a portal when open', () => {
    render(
      <Dialog open onClose={() => {}} title="Title" description="Desc" footer={<button>Foot</button>}>
        <p>Body</p>
      </Dialog>
    )
    const dlg = screen.getByRole('dialog')
    expect(dlg).toHaveAttribute('aria-modal', 'true')
    expect(dlg).toHaveAttribute('aria-labelledby', 'dialog-title')
    expect(screen.getByRole('heading', { name: 'Title' })).toBeInTheDocument()
    expect(screen.getByText('Desc')).toBeInTheDocument()
    expect(screen.getByText('Body')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Foot' })).toBeInTheDocument()
    expect(document.body.contains(dlg)).toBe(true)
  })

  it('omits aria-labelledby without a title', () => {
    render(<Dialog open onClose={() => {}} hideClose>x</Dialog>)
    expect(screen.getByRole('dialog')).not.toHaveAttribute('aria-labelledby')
  })

  it('calls onClose on Escape, backdrop and the close button', async () => {
    const onClose = vi.fn()
    render(
      <Dialog open onClose={onClose} title="T">
        x
      </Dialog>
    )
    await userEvent.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalledTimes(1)
    const closers = screen.getAllByRole('button', { name: 'Close dialog' })
    expect(closers).toHaveLength(2)
    await userEvent.click(closers[0])
    await userEvent.click(closers[1])
    expect(onClose).toHaveBeenCalledTimes(3)
  })

  it('hides the corner close button when hideClose is set', () => {
    render(
      <Dialog open onClose={() => {}} title="T" hideClose>
        x
      </Dialog>
    )
    expect(screen.getAllByRole('button', { name: 'Close dialog' })).toHaveLength(1)
  })

  it('locks body scroll while open and restores on close', () => {
    const { rerender } = render(
      <Dialog open onClose={() => {}} title="T">
        x
      </Dialog>
    )
    expect(document.body.style.overflow).toBe('hidden')
    rerender(
      <Dialog open={false} onClose={() => {}} title="T">
        x
      </Dialog>
    )
    expect(document.body.style.overflow).toBe('')
  })

  it('focuses the first focusable element inside', async () => {
    render(
      <Dialog open onClose={() => {}} title="T" hideClose>
        <input aria-label="first" />
      </Dialog>
    )
    await waitFor(() => expect(screen.getByLabelText('first')).toHaveFocus())
  })

  // A parent re-render with a new onClose identity (e.g. every keystroke in a form
  // inside the dialog) must not move focus back to the corner close button.
  it('keeps focus on the active field when the parent re-renders with a new onClose', async () => {
    const ui = (onClose: () => void) => (
      <Dialog open onClose={onClose} title="T">
        <input aria-label="field" />
      </Dialog>
    )
    const { rerender } = render(ui(() => {}))
    const field = screen.getByLabelText('field')
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Close dialog' })[1]).toHaveFocus())
    field.focus()
    rerender(ui(() => {}))
    await new Promise((r) => setTimeout(r, 50))
    expect(field).toHaveFocus()
  })
})
