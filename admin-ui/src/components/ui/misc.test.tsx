import { createRef } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Card, CardContent, CardDescription, CardHeader, CardTitle, IconButton, Kbd, PageHeader } from './index'

describe('Card family', () => {
  it('renders composed card parts', () => {
    render(
      <Card data-testid="card" className="c">
        <CardHeader>
          <CardTitle>Title</CardTitle>
          <CardDescription>Desc</CardDescription>
        </CardHeader>
        <CardContent>Content</CardContent>
      </Card>
    )
    expect(screen.getByTestId('card')).toHaveClass('c')
    expect(screen.getByText('Title')).toBeInTheDocument()
    expect(screen.getByText('Desc')).toBeInTheDocument()
    expect(screen.getByText('Content')).toBeInTheDocument()
  })
})

describe('PageHeader', () => {
  it('renders title, description and actions', () => {
    render(<PageHeader title="Page" description="About" actions={<button>Act</button>} />)
    expect(screen.getByRole('heading', { level: 1, name: 'Page' })).toBeInTheDocument()
    expect(screen.getByText('About')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Act' })).toBeInTheDocument()
  })

  it('omits optional parts', () => {
    const { container } = render(<PageHeader title="Only" />)
    expect(container.querySelector('p')).toBeNull()
    expect(screen.queryByRole('button')).toBeNull()
  })
})

describe('IconButton', () => {
  it('exposes its label and handles clicks', async () => {
    const onClick = vi.fn()
    const ref = createRef<HTMLButtonElement>()
    render(
      <IconButton ref={ref} label="Delete" variant="danger" size="sm" onClick={onClick}>
        x
      </IconButton>
    )
    const btn = screen.getByRole('button', { name: 'Delete' })
    expect(btn).toHaveAttribute('type', 'button')
    expect(btn.className).toContain('h-7')
    await userEvent.click(btn)
    expect(onClick).toHaveBeenCalledTimes(1)
    expect(ref.current).toBe(btn)
  })

  it('defaults to md ghost', () => {
    render(<IconButton label="Go">x</IconButton>)
    expect(screen.getByRole('button').className).toContain('h-9')
  })
})

describe('Kbd', () => {
  it('renders a kbd element', () => {
    render(<Kbd>K</Kbd>)
    expect(screen.getByText('K').tagName).toBe('KBD')
  })
})
