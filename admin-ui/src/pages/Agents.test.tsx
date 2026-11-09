import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from '../test/utils'

vi.mock('../api/agents', () => ({
  agentsApi: {
    list: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    remove: vi.fn(),
    probe: vi.fn(),
    reload: vi.fn(),
    export: vi.fn(),
  },
}))
import { agentsApi } from '../api/agents'
import Agents from './Agents'

const api = vi.mocked(agentsApi)

// NOTE: typing keystroke-by-keystroke triggers a real Dialog bug (focus is stolen
// by the close button on every parent re-render; see ui/Dialog.test.tsx), so
// fields are set with change events.
async function fill(el: HTMLElement, text: string) {
  fireEvent.change(el, { target: { value: text } })
}

const agent = (over: Record<string, unknown> = {}) => ({
  slug: 'orders',
  upstream_base: 'http://10.0.0.1:8090',
  audience: null,
  effective_audience: 'orders',
  public_paths: ['/invite'],
  status: 'published',
  display_name: 'Orders Agent',
  description: null,
  runtime_mode: 'dedicated',
  data_residency: 'in',
  enabled: true,
  created_at: null,
  updated_at: null,
  updated_by: null,
  live: true,
  public_url: 'https://gw.test/agents/orders/',
  ...over,
})

describe('Agents page', () => {
  beforeEach(() => {
    for (const fn of Object.values(api)) fn.mockReset()
  })

  it('shows skeletons while loading and zeroed stats', () => {
    api.list.mockReturnValue(new Promise(() => {}))
    const { container } = renderWithProviders(<Agents />)
    expect(screen.getByRole('heading', { name: 'Agents' })).toBeInTheDocument()
    expect(container.querySelectorAll('.animate-pulse').length).toBeGreaterThanOrEqual(5)
    expect(screen.getByText('Registered')).toBeInTheDocument()
  })

  it('shows the empty state when no agents exist', async () => {
    api.list.mockResolvedValue([])
    renderWithProviders(<Agents />)
    expect(await screen.findByText('No agents registered')).toBeInTheDocument()
  })

  it('shows the empty state when the list request errors', async () => {
    api.list.mockRejectedValue(new Error('boom'))
    renderWithProviders(<Agents />)
    expect(await screen.findByText('No agents registered')).toBeInTheDocument()
  })

  it('renders agents with status badges and computes stats', async () => {
    api.list.mockResolvedValue([
      agent(),
      agent({ slug: 'hidden-one', display_name: null, live: false, enabled: false, status: 'hidden', public_paths: [] }),
    ] as any)
    renderWithProviders(<Agents />)
    expect(await screen.findByText('Orders Agent')).toBeInTheDocument()
    expect(screen.getByText('hidden-one')).toBeInTheDocument()
    expect(screen.getAllByText('Live')).toHaveLength(2) // stat label + badge
    expect(screen.getByText('Not routed')).toBeInTheDocument()
    expect(screen.getByText('Disabled', { selector: 'span' })).toBeInTheDocument()
    expect(screen.getByText('Hidden')).toBeInTheDocument()
    expect(screen.getByText('1 public path')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /\/agents\/orders\// })).toHaveAttribute('href', 'https://gw.test/agents/orders/')
    // stats: registered 2, live 1, published 1, disabled 1
    const registered = screen.getByText('Registered').closest('div[class*="rounded-xl"]') as HTMLElement
    expect(within(registered).getByText('2')).toBeInTheDocument()
  })

  describe('create flow', () => {
    async function openCreate() {
      api.list.mockResolvedValue([])
      renderWithProviders(<Agents />)
      await screen.findByText('No agents registered')
      await userEvent.click(screen.getByRole('button', { name: /Add agent/ }))
      return screen.getByRole('dialog')
    }

    it('disables Create until slug and upstream are valid', async () => {
      const dialog = await openCreate()
      const create = within(dialog).getByRole('button', { name: 'Create agent' })
      expect(create).toBeDisabled()
      const [slug, upstream] = within(dialog).getAllByRole('textbox')
      await fill(slug, 'Bad Slug')
      expect(within(dialog).getByText(/kebab-case only/)).toBeInTheDocument()
      expect(create).toBeDisabled()
      await userEvent.clear(slug)
      await fill(slug, 'good-slug')
      await fill(upstream, 'ftp://nope')
      expect(within(dialog).getByText('must start with http:// or https://')).toBeInTheDocument()
      expect(create).toBeDisabled()
      await userEvent.clear(upstream)
      await fill(upstream, 'http://up:1')
      expect(create).toBeEnabled()
    })

    it('creates an agent, parsing public paths from lines/commas', async () => {
      api.create.mockResolvedValue(agent({ slug: 'new-agent' }) as any)
      const dialog = await openCreate()
      const [slug, upstream] = within(dialog).getAllByRole('textbox')
      await fill(slug, 'new-agent')
      await fill(upstream, 'https://up.test')
      await fill(within(dialog).getByPlaceholderText(/\/invite/), '/a, /b\n/c')
      await userEvent.selectOptions(within(dialog).getAllByRole('combobox')[0], 'hidden')
      await userEvent.click(within(dialog).getByRole('button', { name: 'Create agent' }))
      await waitFor(() => expect(api.create).toHaveBeenCalled())
      expect(api.create.mock.calls[0][0]).toMatchObject({
        slug: 'new-agent',
        upstream_base: 'https://up.test',
        public_paths: ['/a', '/b', '/c'],
        status: 'hidden',
        runtime_mode: 'dedicated',
        enabled: true,
      })
      expect(await screen.findByText('Agent created')).toBeInTheDocument()
      expect(screen.getByText('new-agent is live now.')).toBeInTheDocument()
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
      await waitFor(() => expect(api.list).toHaveBeenCalledTimes(2))
    })

    it('shows the server error and keeps the dialog open when create fails', async () => {
      api.create.mockRejectedValue({ response: { data: { detail: 'slug taken' } } })
      const dialog = await openCreate()
      const [slug, upstream] = within(dialog).getAllByRole('textbox')
      await fill(slug, 'dup')
      await fill(upstream, 'http://x')
      await userEvent.click(within(dialog).getByRole('button', { name: 'Create agent' }))
      expect(await screen.findByText('Save failed')).toBeInTheDocument()
      expect(screen.getByText('slug taken')).toBeInTheDocument()
      expect(screen.getByRole('dialog')).toBeInTheDocument()
    })
  })

  describe('edit flow', () => {
    it('prefills the form, locks the slug, and updates', async () => {
      api.list.mockResolvedValue([agent()] as any)
      api.update.mockResolvedValue(agent() as any)
      renderWithProviders(<Agents />)
      await userEvent.click(await screen.findByTitle('Edit'))
      const dialog = screen.getByRole('dialog')
      expect(within(dialog).getByText('Edit orders')).toBeInTheDocument()
      const [slug, upstream] = within(dialog).getAllByRole('textbox')
      expect(slug).toHaveValue('orders')
      expect(slug).toBeDisabled()
      expect(upstream).toHaveValue('http://10.0.0.1:8090')
      expect(within(dialog).getByPlaceholderText(/\/invite/)).toHaveValue('/invite')
      await userEvent.clear(upstream)
      await fill(upstream, 'http://new:1')
      await userEvent.click(within(dialog).getByRole('checkbox'))
      await userEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }))
      await waitFor(() => expect(api.update).toHaveBeenCalled())
      expect(api.update.mock.calls[0][0]).toBe('orders')
      expect(api.update.mock.calls[0][1]).toMatchObject({ upstream_base: 'http://new:1', enabled: false })
      expect(await screen.findByText('Agent updated')).toBeInTheDocument()
      expect(api.create).not.toHaveBeenCalled()
    })
  })

  describe('delete flow', () => {
    it('confirms then removes the agent', async () => {
      api.list.mockResolvedValue([agent()] as any)
      api.remove.mockResolvedValue(undefined)
      renderWithProviders(<Agents />)
      await userEvent.click(await screen.findByTitle('Delete'))
      const dialog = screen.getByRole('dialog')
      expect(within(dialog).getByText('Remove agent?')).toBeInTheDocument()
      expect(within(dialog).getByText(/orders will stop routing immediately/)).toBeInTheDocument()
      expect(api.remove).not.toHaveBeenCalled()
      await userEvent.click(within(dialog).getByRole('button', { name: 'Remove' }))
      await waitFor(() => expect(api.remove).toHaveBeenCalled())
      expect(api.remove.mock.calls[0][0]).toBe('orders')
      expect(await screen.findByText('Agent removed')).toBeInTheDocument()
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    })

    it('cancel does not delete', async () => {
      api.list.mockResolvedValue([agent()] as any)
      renderWithProviders(<Agents />)
      await userEvent.click(await screen.findByTitle('Delete'))
      await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }))
      expect(api.remove).not.toHaveBeenCalled()
      expect(screen.queryByRole('dialog')).toBeNull()
    })

    it('toasts the server error when removal fails', async () => {
      api.list.mockResolvedValue([agent()] as any)
      api.remove.mockRejectedValue({ response: { data: { detail: 'in use' } } })
      renderWithProviders(<Agents />)
      await userEvent.click(await screen.findByTitle('Delete'))
      await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Remove' }))
      expect(await screen.findByText('Delete failed')).toBeInTheDocument()
      expect(screen.getByText('in use')).toBeInTheDocument()
    })
  })

  describe('probe / reload / export', () => {
    it('probes an upstream and shows reachability inline and in a toast', async () => {
      api.list.mockResolvedValue([agent()] as any)
      api.probe.mockResolvedValue({ reachable: true, status_code: 200, latency_ms: 12, target: 'http://t/' })
      renderWithProviders(<Agents />)
      await userEvent.click(await screen.findByTitle('Test upstream'))
      expect(await screen.findByText('Reachable · 200')).toBeInTheDocument()
      expect(screen.getByText('200 · 12ms')).toBeInTheDocument()
      expect(api.probe).toHaveBeenCalledWith('orders')
    })

    it('shows unreachable upstreams', async () => {
      api.list.mockResolvedValue([agent()] as any)
      api.probe.mockResolvedValue({ reachable: false, latency_ms: 5, target: 'http://t/' })
      renderWithProviders(<Agents />)
      await userEvent.click(await screen.findByTitle('Test upstream'))
      expect(await screen.findAllByText('Unreachable')).not.toHaveLength(0)
      expect(screen.getByText('unreachable')).toBeInTheDocument()
    })

    it('toasts when the probe itself fails', async () => {
      api.list.mockResolvedValue([agent()] as any)
      api.probe.mockRejectedValue(new Error('timeout'))
      renderWithProviders(<Agents />)
      await userEvent.click(await screen.findByTitle('Test upstream'))
      expect(await screen.findByText('Probe failed')).toBeInTheDocument()
    })

    it('reloads routes and reports live count', async () => {
      api.list.mockResolvedValue([])
      api.reload.mockResolvedValue({ reloaded: true, live_routes: 7 })
      renderWithProviders(<Agents />)
      await userEvent.click(await screen.findByRole('button', { name: /Reload/ }))
      expect(await screen.findByText('Routes reloaded')).toBeInTheDocument()
      expect(screen.getByText('7 agents live.')).toBeInTheDocument()
    })

    it('exports the config as a JSON download', async () => {
      api.list.mockResolvedValue([])
      api.export.mockResolvedValue({ AGENT_ROUTES_JSON: {}, AGENT_PUBLIC_PATHS_JSON: {}, count: 2 })
      const createObjectURL = vi.fn(() => 'blob:x')
      const revokeObjectURL = vi.fn()
      Object.assign(URL, { createObjectURL, revokeObjectURL })
      const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
      renderWithProviders(<Agents />)
      await userEvent.click(await screen.findByRole('button', { name: /Export/ }))
      expect(await screen.findByText('Exported')).toBeInTheDocument()
      expect(createObjectURL).toHaveBeenCalled()
      expect(click).toHaveBeenCalled()
      expect(revokeObjectURL).toHaveBeenCalledWith('blob:x')
    })

    it('toasts when export fails', async () => {
      api.list.mockResolvedValue([])
      api.export.mockRejectedValue(new Error('no'))
      renderWithProviders(<Agents />)
      await userEvent.click(await screen.findByRole('button', { name: /Export/ }))
      expect(await screen.findByText('Export failed')).toBeInTheDocument()
    })
  })
})
