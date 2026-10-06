import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Copy, Eye, EyeOff, Key, Plus, Trash2 } from 'lucide-react'
import { format } from 'date-fns'
import { adminApi, type APIKey } from '../api/admin'
import { Button } from '../components/ui/Button'
import { Card } from '../components/ui/Card'
import { PageHeader } from '../components/ui/PageHeader'
import { Input } from '../components/ui/Input'
import { Badge } from '../components/ui/Badge'
import { Dialog } from '../components/ui/Dialog'
import { EmptyState } from '../components/ui/EmptyState'
import { Skeleton } from '../components/ui/Skeleton'
import { IconButton } from '../components/ui/IconButton'
import { Table, Tbody, Td, Th, Thead, Tr } from '../components/ui/Table'
import { useToast } from '../components/ui/Toast'

export default function APIKeys() {
  const queryClient = useQueryClient()
  const { toast } = useToast()
  const [showKeys, setShowKeys] = useState<Record<string, boolean>>({})
  const [newKeyName, setNewKeyName] = useState('')
  const [showCreateModal, setShowCreateModal] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState<APIKey | null>(null)

  const { data: keys, isLoading } = useQuery({
    queryKey: ['api-keys'],
    queryFn: adminApi.getAPIKeys,
  })

  const createMutation = useMutation({
    mutationFn: adminApi.createAPIKey,
    onSuccess: (key) => {
      queryClient.invalidateQueries({ queryKey: ['api-keys'] })
      setShowCreateModal(false)
      setNewKeyName('')
      toast({
        title: 'API key created',
        description: key.name,
        variant: 'success',
      })
    },
    onError: (err: any) => {
      toast({
        title: "Couldn't create key",
        description: err?.response?.data?.detail || err?.message || 'Unknown error',
        variant: 'error',
      })
    },
  })

  const deleteMutation = useMutation({
    mutationFn: adminApi.deleteAPIKey,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['api-keys'] })
      toast({ title: 'API key deleted', variant: 'success' })
      setConfirmDelete(null)
    },
  })

  const toggleMutation = useMutation({
    mutationFn: adminApi.toggleAPIKey,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['api-keys'] })
    },
  })

  const copyToClipboard = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text)
      toast({ title: 'Copied to clipboard', variant: 'success' })
    } catch {
      toast({ title: 'Copy failed', variant: 'error' })
    }
  }

  const handleCreate = () => {
    if (newKeyName.trim()) {
      createMutation.mutate({ name: newKeyName.trim() })
    }
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="API keys"
        description="Manage credentials issued to gateway clients."
        actions={
          <Button onClick={() => setShowCreateModal(true)} size="md">
            <Plus className="h-4 w-4" />
            Create key
          </Button>
        }
      />

      <Card className="overflow-hidden">
        {isLoading ? (
          <div className="space-y-2 p-4">
            {Array.from({ length: 4 }).map((_, i) => (
              <Skeleton key={i} className="h-12 w-full" />
            ))}
          </div>
        ) : !keys || keys.length === 0 ? (
          <EmptyState
            className="m-4"
            icon={<Key className="h-5 w-5" />}
            title="No API keys yet"
            description="Create a key to authenticate clients connecting through the gateway."
            action={
              <Button onClick={() => setShowCreateModal(true)} size="md">
                <Plus className="h-4 w-4" />
                Create your first key
              </Button>
            }
          />
        ) : (
          <Table>
            <Thead>
              <Tr>
                <Th>Name</Th>
                <Th>Key</Th>
                <Th>Rate limits</Th>
                <Th>Status</Th>
                <Th>Created</Th>
                <Th className="text-right">Actions</Th>
              </Tr>
            </Thead>
            <Tbody>
              {keys.map((key) => (
                <Tr key={key.key}>
                  <Td className="font-medium">{key.name}</Td>
                  <Td>
                    <div className="flex items-center gap-2">
                      <code className="rounded bg-muted px-1.5 py-0.5 text-xs font-mono text-muted-foreground">
                        {showKeys[key.key]
                          ? key.key
                          : `${key.key.substring(0, 12)}…`}
                      </code>
                      <IconButton
                        label={showKeys[key.key] ? 'Hide key' : 'Reveal key'}
                        size="sm"
                        onClick={() =>
                          setShowKeys({ ...showKeys, [key.key]: !showKeys[key.key] })
                        }
                      >
                        {showKeys[key.key] ? (
                          <EyeOff className="h-3.5 w-3.5" />
                        ) : (
                          <Eye className="h-3.5 w-3.5" />
                        )}
                      </IconButton>
                      <IconButton
                        label="Copy key"
                        size="sm"
                        onClick={() => copyToClipboard(key.key)}
                      >
                        <Copy className="h-3.5 w-3.5" />
                      </IconButton>
                    </div>
                  </Td>
                  <Td className="whitespace-nowrap text-muted-foreground tabular-nums">
                    {key.rate_limit_per_minute}/min · {key.rate_limit_per_hour}/hr
                  </Td>
                  <Td>
                    {key.is_active ? (
                      <Badge variant="success" dot>
                        Active
                      </Badge>
                    ) : (
                      <Badge variant="muted">Inactive</Badge>
                    )}
                  </Td>
                  <Td className="whitespace-nowrap text-muted-foreground">
                    {format(new Date(key.created_at), 'MMM dd, yyyy')}
                  </Td>
                  <Td>
                    <div className="flex items-center justify-end gap-2">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => toggleMutation.mutate(key.key)}
                        disabled={toggleMutation.isPending}
                      >
                        {key.is_active ? 'Deactivate' : 'Activate'}
                      </Button>
                      <IconButton
                        label="Delete key"
                        variant="danger"
                        size="sm"
                        onClick={() => setConfirmDelete(key)}
                      >
                        <Trash2 className="h-4 w-4" />
                      </IconButton>
                    </div>
                  </Td>
                </Tr>
              ))}
            </Tbody>
          </Table>
        )}
      </Card>

      <Dialog
        open={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        title="Create API key"
        description="Give the key a memorable name so you can identify it later."
        footer={
          <>
            <Button variant="ghost" onClick={() => setShowCreateModal(false)}>
              Cancel
            </Button>
            <Button
              onClick={handleCreate}
              isLoading={createMutation.isPending}
              disabled={!newKeyName.trim()}
            >
              Create key
            </Button>
          </>
        }
      >
        <div className="space-y-2">
          <label htmlFor="key-name" className="text-sm font-medium text-foreground">
            Name
          </label>
          <Input
            id="key-name"
            autoFocus
            value={newKeyName}
            onChange={(e) => setNewKeyName(e.target.value)}
            placeholder="e.g. Mobile clients"
            onKeyDown={(e) => {
              if (e.key === 'Enter' && newKeyName.trim()) handleCreate()
            }}
          />
        </div>
      </Dialog>

      <Dialog
        open={!!confirmDelete}
        onClose={() => setConfirmDelete(null)}
        title="Delete API key?"
        description={
          confirmDelete
            ? `"${confirmDelete.name}" will stop authenticating immediately. This can't be undone.`
            : ''
        }
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirmDelete(null)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              isLoading={deleteMutation.isPending}
              onClick={() => confirmDelete && deleteMutation.mutate(confirmDelete.key)}
            >
              Delete key
            </Button>
          </>
        }
      >
        <></>
      </Dialog>
    </div>
  )
}
