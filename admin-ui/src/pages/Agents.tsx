import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  Boxes,
  CheckCircle2,
  Download,
  ExternalLink,
  Globe,
  Pencil,
  Plus,
  RefreshCw,
  ShieldCheck,
  Trash2,
  XCircle,
} from 'lucide-react'
import { agentsApi, type Agent, type AgentInput, type HealthProbe } from '../api/agents'
import {
  Badge,
  Button,
  Card,
  CardContent,
  Dialog,
  EmptyState,
  Input,
  PageHeader,
  Select,
  Skeleton,
  Stat,
  Table,
  Tbody,
  Td,
  Th,
  Thead,
  Tr,
  useToast,
} from '../components/ui'
import { cn } from '../lib/utils'

const EMPTY_FORM: AgentInput = {
  slug: '',
  upstream_base: '',
  audience: '',
  public_paths: [],
  status: 'published',
  display_name: '',
  description: '',
  runtime_mode: 'dedicated',
  data_residency: 'in',
  enabled: true,
}

function download(filename: string, text: string) {
  const blob = new Blob([text], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

export default function Agents() {
  const qc = useQueryClient()
  const { toast } = useToast()

  const { data: agents, isLoading } = useQuery({ queryKey: ['agents'], queryFn: agentsApi.list })

  const [editorOpen, setEditorOpen] = useState(false)
  const [editing, setEditing] = useState<Agent | null>(null)
  const [form, setForm] = useState<AgentInput>(EMPTY_FORM)
  const [pathsText, setPathsText] = useState('')
  const [deleting, setDeleting] = useState<Agent | null>(null)
  const [probe, setProbe] = useState<Record<string, HealthProbe & { loading?: boolean }>>({})

  const invalidate = () => qc.invalidateQueries({ queryKey: ['agents'] })

  const saveMutation = useMutation({
    mutationFn: async (input: AgentInput) =>
      editing ? agentsApi.update(editing.slug, input) : agentsApi.create(input),
    onSuccess: (a) => {
      toast({ title: editing ? 'Agent updated' : 'Agent created', description: `${a.slug} is live now.`, variant: 'success' })
      setEditorOpen(false)
      invalidate()
    },
    onError: (e: any) =>
      toast({ title: 'Save failed', description: e?.response?.data?.detail ?? String(e), variant: 'error' }),
  })

  const deleteMutation = useMutation({
    mutationFn: async (slug: string) => agentsApi.remove(slug),
    onSuccess: () => {
      toast({ title: 'Agent removed', variant: 'success' })
      setDeleting(null)
      invalidate()
    },
    onError: (e: any) =>
      toast({ title: 'Delete failed', description: e?.response?.data?.detail ?? String(e), variant: 'error' }),
  })

  const reloadMutation = useMutation({
    mutationFn: agentsApi.reload,
    onSuccess: (r) => {
      toast({ title: 'Routes reloaded', description: `${r.live_routes} agents live.`, variant: 'success' })
      invalidate()
    },
  })

  const stats = useMemo(() => {
    const list = agents ?? []
    return {
      total: list.length,
      live: list.filter((a) => a.live).length,
      published: list.filter((a) => a.status === 'published').length,
      disabled: list.filter((a) => !a.enabled).length,
    }
  }, [agents])

  function openCreate() {
    setEditing(null)
    setForm(EMPTY_FORM)
    setPathsText('')
    setEditorOpen(true)
  }

  function openEdit(a: Agent) {
    setEditing(a)
    setForm({
      slug: a.slug,
      upstream_base: a.upstream_base,
      audience: a.audience ?? '',
      public_paths: a.public_paths,
      status: a.status,
      display_name: a.display_name ?? '',
      description: a.description ?? '',
      runtime_mode: a.runtime_mode,
      data_residency: a.data_residency,
      enabled: a.enabled,
    })
    setPathsText(a.public_paths.join('\n'))
    setEditorOpen(true)
  }

  function submit() {
    const public_paths = pathsText
      .split(/[\n,]/)
      .map((s) => s.trim())
      .filter(Boolean)
    saveMutation.mutate({ ...form, public_paths })
  }

  async function runProbe(a: Agent) {
    setProbe((p) => ({ ...p, [a.slug]: { ...(p[a.slug] as any), loading: true } as any }))
    try {
      const res = await agentsApi.probe(a.slug)
      setProbe((p) => ({ ...p, [a.slug]: res }))
      toast({
        title: res.reachable ? `Reachable · ${res.status_code}` : 'Unreachable',
        description: `${res.target} · ${res.latency_ms}ms`,
        variant: res.reachable ? 'success' : 'error',
      })
    } catch (e: any) {
      toast({ title: 'Probe failed', description: String(e), variant: 'error' })
      setProbe((p) => {
        const { [a.slug]: _, ...rest } = p
        return rest
      })
    }
  }

  async function exportConfig() {
    try {
      const data = await agentsApi.export()
      download('agent-routes.json', JSON.stringify(data, null, 2))
      toast({ title: 'Exported', description: `${data.count} agents.`, variant: 'success' })
    } catch (e: any) {
      toast({ title: 'Export failed', description: String(e), variant: 'error' })
    }
  }

  const slugError =
    form.slug && !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(form.slug)
      ? 'kebab-case only (lowercase, digits, single dashes)'
      : ''
  const upstreamError =
    form.upstream_base && !/^https?:\/\//.test(form.upstream_base)
      ? 'must start with http:// or https://'
      : ''
  const canSave = !!form.slug && !!form.upstream_base && !slugError && !upstreamError

  return (
    <div className="space-y-6 sm:space-y-8">
      <PageHeader
        title="Agents"
        description="Register agents, wire their upstreams and SSO — changes go live instantly, no redeploy."
        actions={
          <>
            <Button variant="outline" size="sm" onClick={exportConfig}>
              <Download className="h-4 w-4" /> Export
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => reloadMutation.mutate()}
              isLoading={reloadMutation.isPending}
            >
              <RefreshCw className="h-4 w-4" /> Reload
            </Button>
            <Button size="sm" onClick={openCreate}>
              <Plus className="h-4 w-4" /> Add agent
            </Button>
          </>
        }
      />

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="Registered" value={stats.total} icon={<Boxes className="h-4 w-4" />} />
        <Stat label="Live" value={stats.live} icon={<Activity className="h-4 w-4" />} />
        <Stat label="Published" value={stats.published} icon={<Globe className="h-4 w-4" />} />
        <Stat label="Disabled" value={stats.disabled} icon={<XCircle className="h-4 w-4" />} />
      </div>

      <Card>
        <CardContent className="p-0">
          {isLoading ? (
            <div className="space-y-2 p-4">
              {Array.from({ length: 5 }).map((_, i) => (
                <Skeleton key={i} className="h-12" />
              ))}
            </div>
          ) : !agents || agents.length === 0 ? (
            <EmptyState
              icon={<Boxes className="h-5 w-5" />}
              title="No agents registered"
              description="Add your first agent to route gateway.example.com/agents/<slug>/ to its upstream."
            />
          ) : (
            <Table>
              <Thead>
                <Tr>
                  <Th>Agent</Th>
                  <Th>Upstream</Th>
                  <Th>Auth</Th>
                  <Th>Status</Th>
                  <Th className="text-right">Actions</Th>
                </Tr>
              </Thead>
              <Tbody>
                {agents.map((a) => {
                  const pr = probe[a.slug]
                  return (
                    <Tr key={a.slug}>
                      <Td>
                        <div className="flex flex-col">
                          <span className="font-medium text-foreground">
                            {a.display_name || a.slug}
                          </span>
                          <a
                            href={a.public_url}
                            target="_blank"
                            rel="noreferrer"
                            className="inline-flex items-center gap-1 font-mono text-xs text-muted-foreground hover:text-primary"
                          >
                            /agents/{a.slug}/ <ExternalLink className="h-3 w-3" />
                          </a>
                        </div>
                      </Td>
                      <Td>
                        <span className="font-mono text-xs text-muted-foreground">
                          {a.upstream_base}
                        </span>
                        {pr && !pr.loading && (
                          <div
                            className={cn(
                              'mt-1 inline-flex items-center gap-1 text-xs',
                              pr.reachable ? 'text-emerald-500' : 'text-red-500'
                            )}
                          >
                            {pr.reachable ? (
                              <CheckCircle2 className="h-3 w-3" />
                            ) : (
                              <XCircle className="h-3 w-3" />
                            )}
                            {pr.reachable ? `${pr.status_code} · ${pr.latency_ms}ms` : 'unreachable'}
                          </div>
                        )}
                      </Td>
                      <Td>
                        <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                          <ShieldCheck className="h-3.5 w-3.5" />
                          {a.effective_audience}
                        </span>
                        {a.public_paths.length > 0 && (
                          <div className="mt-0.5 text-[11px] text-muted-foreground">
                            {a.public_paths.length} public path{a.public_paths.length === 1 ? '' : 's'}
                          </div>
                        )}
                      </Td>
                      <Td>
                        <div className="flex flex-wrap items-center gap-1.5">
                          {a.live ? (
                            <Badge variant="success" dot>
                              Live
                            </Badge>
                          ) : (
                            <Badge variant="muted">Not routed</Badge>
                          )}
                          {!a.enabled && <Badge variant="warning">Disabled</Badge>}
                          {a.status === 'hidden' && <Badge variant="muted">Hidden</Badge>}
                        </div>
                      </Td>
                      <Td>
                        <div className="flex items-center justify-end gap-1">
                          <Button
                            variant="ghost"
                            size="icon-sm"
                            title="Test upstream"
                            isLoading={pr?.loading}
                            onClick={() => runProbe(a)}
                          >
                            <Activity className="h-4 w-4" />
                          </Button>
                          <Button variant="ghost" size="icon-sm" title="Edit" onClick={() => openEdit(a)}>
                            <Pencil className="h-4 w-4" />
                          </Button>
                          <Button
                            variant="ghost"
                            size="icon-sm"
                            title="Delete"
                            className="text-red-500 hover:bg-red-500/10"
                            onClick={() => setDeleting(a)}
                          >
                            <Trash2 className="h-4 w-4" />
                          </Button>
                        </div>
                      </Td>
                    </Tr>
                  )
                })}
              </Tbody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* Create / edit */}
      <Dialog
        open={editorOpen}
        onClose={() => setEditorOpen(false)}
        title={editing ? `Edit ${editing.slug}` : 'Add agent'}
        description="Routing takes effect immediately across the gateway."
        className="max-w-lg"
        footer={
          <>
            <Button variant="ghost" onClick={() => setEditorOpen(false)}>
              Cancel
            </Button>
            <Button onClick={submit} isLoading={saveMutation.isPending} disabled={!canSave}>
              {editing ? 'Save changes' : 'Create agent'}
            </Button>
          </>
        }
      >
        <div className="max-h-[60vh] space-y-4 overflow-y-auto pr-1">
          <Field label="Slug" hint={slugError} error={!!slugError}>
            <Input
              value={form.slug}
              disabled={!!editing}
              placeholder="orders-agent"
              onChange={(e) => setForm({ ...form, slug: e.target.value })}
            />
          </Field>
          <Field label="Upstream base URL" hint={upstreamError} error={!!upstreamError}>
            <Input
              value={form.upstream_base}
              placeholder="http://10.0.0.10:8090"
              onChange={(e) => setForm({ ...form, upstream_base: e.target.value })}
            />
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Display name">
              <Input
                value={form.display_name ?? ''}
                placeholder="Code Evaluator"
                onChange={(e) => setForm({ ...form, display_name: e.target.value })}
              />
            </Field>
            <Field label="Audience" hint="defaults to slug">
              <Input
                value={form.audience ?? ''}
                placeholder={form.slug || 'slug'}
                onChange={(e) => setForm({ ...form, audience: e.target.value })}
              />
            </Field>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Status">
              <Select
                value={form.status}
                onChange={(e) => setForm({ ...form, status: e.target.value as any })}
              >
                <option value="published">Published</option>
                <option value="hidden">Hidden</option>
              </Select>
            </Field>
            <Field label="Runtime mode">
              <Select
                value={form.runtime_mode}
                onChange={(e) => setForm({ ...form, runtime_mode: e.target.value as any })}
              >
                <option value="dedicated">Dedicated</option>
                <option value="shared">Shared</option>
              </Select>
            </Field>
          </div>
          <Field label="Public paths (bypass SSO)" hint="one per line, e.g. /invite">
            <textarea
              value={pathsText}
              onChange={(e) => setPathsText(e.target.value)}
              rows={3}
              placeholder={'/invite\n/api/v1/invites'}
              className="flex w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </Field>
          <Field label="Description">
            <Input
              value={form.description ?? ''}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
            />
          </Field>
          <label className="flex items-center gap-2 text-sm text-foreground">
            <input
              type="checkbox"
              checked={!!form.enabled}
              onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
              className="h-4 w-4 rounded border-input"
            />
            Enabled (routable)
          </label>
        </div>
      </Dialog>

      {/* Delete confirm */}
      <Dialog
        open={!!deleting}
        onClose={() => setDeleting(null)}
        title="Remove agent?"
        description={
          deleting
            ? `${deleting.slug} will stop routing immediately. This cannot be undone.`
            : undefined
        }
        footer={
          <>
            <Button variant="ghost" onClick={() => setDeleting(null)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              isLoading={deleteMutation.isPending}
              onClick={() => deleting && deleteMutation.mutate(deleting.slug)}
            >
              Remove
            </Button>
          </>
        }
      >
        <p className="text-sm text-muted-foreground">
          Traffic to <span className="font-mono text-foreground">/agents/{deleting?.slug}/</span> will
          return 404 until re-added.
        </p>
      </Dialog>
    </div>
  )
}

function Field({
  label,
  hint,
  error,
  children,
}: {
  label: string
  hint?: string
  error?: boolean
  children: React.ReactNode
}) {
  return (
    <div className="space-y-1.5">
      <label className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
        {label}
      </label>
      {children}
      {hint && (
        <p className={cn('text-xs', error ? 'text-red-500' : 'text-muted-foreground')}>{hint}</p>
      )}
    </div>
  )
}
