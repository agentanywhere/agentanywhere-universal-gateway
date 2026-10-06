import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { format } from 'date-fns'
import { FileText, RefreshCw, Search } from 'lucide-react'
import { adminApi, type LogEntry } from '../api/admin'
import { Card, CardContent } from '../components/ui/Card'
import { PageHeader } from '../components/ui/PageHeader'
import { Input } from '../components/ui/Input'
import { Select } from '../components/ui/Select'
import { Badge } from '../components/ui/Badge'
import { Skeleton } from '../components/ui/Skeleton'
import { EmptyState } from '../components/ui/EmptyState'
import { IconButton } from '../components/ui/IconButton'
import { Table, Tbody, Td, Th, Thead, Tr } from '../components/ui/Table'
import { formatMs } from '../lib/utils'

function statusVariant(code: number): 'success' | 'info' | 'warning' | 'danger' {
  if (code < 300) return 'success'
  if (code < 400) return 'info'
  if (code < 500) return 'warning'
  return 'danger'
}

function methodVariant(method: string): 'info' | 'success' | 'warning' | 'danger' | 'muted' {
  switch (method.toUpperCase()) {
    case 'GET':
      return 'info'
    case 'POST':
      return 'success'
    case 'PUT':
    case 'PATCH':
      return 'warning'
    case 'DELETE':
      return 'danger'
    default:
      return 'muted'
  }
}

export default function Logs() {
  const [search, setSearch] = useState('')
  const [level, setLevel] = useState('')
  const [limit, setLimit] = useState(100)

  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: ['logs', { limit, level, path: search }],
    queryFn: () => adminApi.getLogs({ limit, level, path: search }),
    refetchInterval: 5000,
  })

  return (
    <div className="space-y-6">
      <PageHeader
        title="Request logs"
        description="Live tail of gateway request logs. Filters update automatically."
        actions={
          <IconButton
            label="Refresh logs"
            variant="subtle"
            onClick={() => refetch()}
            disabled={isFetching}
          >
            <RefreshCw className={`h-4 w-4 ${isFetching ? 'animate-spin' : ''}`} />
          </IconButton>
        }
      />

      <Card>
        <CardContent className="p-4">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-[1fr_auto_auto]">
            <Input
              leftIcon={<Search className="h-4 w-4" />}
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Filter by path…"
              aria-label="Filter by path"
            />
            <Select
              value={level}
              onChange={(e) => setLevel(e.target.value)}
              aria-label="Filter by level"
              className="sm:w-44"
            >
              <option value="">All levels</option>
              <option value="info">Info</option>
              <option value="warning">Warning</option>
              <option value="error">Error</option>
            </Select>
            <Select
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value))}
              aria-label="Number of entries"
              className="sm:w-44"
            >
              <option value={50}>50 entries</option>
              <option value={100}>100 entries</option>
              <option value={200}>200 entries</option>
              <option value={500}>500 entries</option>
            </Select>
          </div>
        </CardContent>
      </Card>

      <Card className="overflow-hidden">
        {isLoading ? (
          <div className="space-y-2 p-4">
            {Array.from({ length: 8 }).map((_, i) => (
              <Skeleton key={i} className="h-10 w-full" />
            ))}
          </div>
        ) : !data?.logs || data.logs.length === 0 ? (
          <EmptyState
            className="m-4"
            icon={<FileText className="h-5 w-5" />}
            title="No logs match your filters"
            description="Try widening the search or removing the level filter."
          />
        ) : (
          <Table>
            <Thead>
              <Tr>
                <Th>Timestamp</Th>
                <Th>Method</Th>
                <Th>Path</Th>
                <Th>Status</Th>
                <Th className="text-right">Latency</Th>
                <Th>Client IP</Th>
              </Tr>
            </Thead>
            <Tbody>
              {data.logs.map((log: LogEntry) => (
                <Tr key={log.request_id}>
                  <Td className="whitespace-nowrap text-muted-foreground">
                    {format(new Date(log.timestamp), 'MMM dd · HH:mm:ss')}
                  </Td>
                  <Td>
                    <Badge variant={methodVariant(log.method)}>{log.method}</Badge>
                  </Td>
                  <Td className="max-w-[420px] truncate font-mono text-xs">{log.path}</Td>
                  <Td>
                    <Badge variant={statusVariant(log.status_code)}>{log.status_code}</Badge>
                  </Td>
                  <Td className="whitespace-nowrap text-right tabular-nums text-muted-foreground">
                    {formatMs(log.response_time_ms, 2)}
                  </Td>
                  <Td className="whitespace-nowrap font-mono text-xs text-muted-foreground">
                    {log.client_ip}
                  </Td>
                </Tr>
              ))}
            </Tbody>
          </Table>
        )}
      </Card>
    </div>
  )
}
