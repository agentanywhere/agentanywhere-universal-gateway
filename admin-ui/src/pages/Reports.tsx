import { useState } from 'react'
import { format, subDays } from 'date-fns'
import {
  AlertCircle,
  Calendar,
  CheckCircle2,
  Download,
  FileText,
  Info,
  TrendingUp,
} from 'lucide-react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/Card'
import { PageHeader } from '../components/ui/PageHeader'
import { Button } from '../components/ui/Button'
import { Input } from '../components/ui/Input'
import { Badge } from '../components/ui/Badge'
import { useToast } from '../components/ui/Toast'
import { cn } from '../lib/utils'

interface Report {
  id: string
  name: string
  type: 'daily' | 'weekly' | 'monthly' | 'custom'
  generatedAt: Date
  status: 'ready' | 'generating' | 'failed'
}

const reportTemplates = [
  {
    id: 'performance',
    name: 'Performance report',
    description: 'Comprehensive performance metrics and trends.',
    icon: TrendingUp,
    tone: 'info' as const,
  },
  {
    id: 'errors',
    name: 'Error analysis',
    description: 'Error breakdown with root-cause hints.',
    icon: AlertCircle,
    tone: 'danger' as const,
  },
  {
    id: 'usage',
    name: 'Usage statistics',
    description: 'API consumption and quota patterns.',
    icon: FileText,
    tone: 'success' as const,
  },
  {
    id: 'health',
    name: 'Health check',
    description: 'System health and availability snapshot.',
    icon: CheckCircle2,
    tone: 'primary' as const,
  },
]

const toneClass: Record<'info' | 'danger' | 'success' | 'primary', string> = {
  info: 'bg-sky-500/10 text-sky-600 dark:text-sky-400 ring-sky-500/20',
  danger: 'bg-red-500/10 text-red-600 dark:text-red-400 ring-red-500/20',
  success: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 ring-emerald-500/20',
  primary: 'bg-primary/10 text-primary ring-primary/20',
}

export default function Reports() {
  const { toast } = useToast()
  const [selectedReportType, setSelectedReportType] = useState<Report['type']>('daily')
  const [customDateRange, setCustomDateRange] = useState({
    start: subDays(new Date(), 7),
    end: new Date(),
  })

  const reports: Report[] = [
    { id: '1', name: 'Daily performance report', type: 'daily', generatedAt: new Date(), status: 'ready' },
    { id: '2', name: 'Weekly analytics summary', type: 'weekly', generatedAt: subDays(new Date(), 1), status: 'ready' },
    { id: '3', name: 'Monthly executive report', type: 'monthly', generatedAt: subDays(new Date(), 5), status: 'ready' },
  ]

  const generateReport = (id: string) => {
    toast({
      title: 'Report generation queued',
      description: `${id} report — preview only; backend endpoint isn't wired yet.`,
      variant: 'info',
    })
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Reports"
        description="Generate and download gateway summaries."
      />

      <div className="flex items-start gap-2.5 rounded-lg border border-sky-500/30 bg-sky-500/5 px-4 py-3 text-sm text-sky-700 dark:text-sky-300">
        <Info className="mt-0.5 h-4 w-4 shrink-0" />
        <p>
          The reports module is a UI preview. Once the backend exposes a report-generation endpoint,
          the buttons here will produce real PDF/CSV/Excel files.
        </p>
      </div>

      <div>
        <h2 className="mb-3 text-sm font-semibold text-foreground">Templates</h2>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {reportTemplates.map((template) => {
            const Icon = template.icon
            return (
              <button
                key={template.id}
                onClick={() => generateReport(template.name)}
                className="group rounded-xl border border-border bg-card/60 p-5 text-left transition-all hover:-translate-y-0.5 hover:border-primary/30 hover:shadow-md"
              >
                <div
                  className={cn(
                    'mb-4 inline-flex h-10 w-10 items-center justify-center rounded-lg ring-1',
                    toneClass[template.tone]
                  )}
                >
                  <Icon className="h-5 w-5" />
                </div>
                <h3 className="text-sm font-semibold text-foreground">{template.name}</h3>
                <p className="mt-1 text-xs text-muted-foreground">{template.description}</p>
              </button>
            )
          })}
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Generate custom report</CardTitle>
          <CardDescription>Pick a window and format. Output isn&apos;t wired up yet.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-5">
          <div className="space-y-2">
            <div className="text-sm font-medium text-foreground">Range</div>
            <div className="flex flex-wrap gap-2">
              {(['daily', 'weekly', 'monthly', 'custom'] as const).map((type) => (
                <button
                  key={type}
                  onClick={() => setSelectedReportType(type)}
                  className={cn(
                    'rounded-lg border px-3 py-1.5 text-sm font-medium capitalize transition-colors',
                    selectedReportType === type
                      ? 'border-primary/30 bg-primary/10 text-primary'
                      : 'border-border bg-card text-muted-foreground hover:bg-accent hover:text-foreground'
                  )}
                >
                  {type}
                </button>
              ))}
            </div>
          </div>

          {selectedReportType === 'custom' && (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <div>
                <label className="mb-1.5 block text-sm font-medium text-foreground">Start date</label>
                <Input
                  type="date"
                  leftIcon={<Calendar className="h-4 w-4" />}
                  value={format(customDateRange.start, 'yyyy-MM-dd')}
                  onChange={(e) =>
                    setCustomDateRange({ ...customDateRange, start: new Date(e.target.value) })
                  }
                />
              </div>
              <div>
                <label className="mb-1.5 block text-sm font-medium text-foreground">End date</label>
                <Input
                  type="date"
                  leftIcon={<Calendar className="h-4 w-4" />}
                  value={format(customDateRange.end, 'yyyy-MM-dd')}
                  onChange={(e) =>
                    setCustomDateRange({ ...customDateRange, end: new Date(e.target.value) })
                  }
                />
              </div>
            </div>
          )}

          <div className="space-y-2">
            <div className="text-sm font-medium text-foreground">Format</div>
            <div className="flex flex-wrap gap-2">
              {['PDF', 'CSV', 'Excel'].map((f) => (
                <Button key={f} variant="secondary" size="sm">
                  <Download className="h-3.5 w-3.5" />
                  {f}
                </Button>
              ))}
            </div>
          </div>

          <Button onClick={() => generateReport(selectedReportType)}>Generate report</Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Generated reports</CardTitle>
          <CardDescription>Preview list — not yet persisted.</CardDescription>
        </CardHeader>
        <CardContent className="p-0">
          <div className="divide-y divide-border">
            {reports.map((report) => (
              <div
                key={report.id}
                className="flex items-center justify-between gap-4 px-6 py-4 transition-colors hover:bg-accent/30"
              >
                <div className="flex min-w-0 items-center gap-3">
                  <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary ring-1 ring-primary/20">
                    <FileText className="h-4 w-4" />
                  </div>
                  <div className="min-w-0">
                    <h3 className="truncate text-sm font-medium text-foreground">{report.name}</h3>
                    <p className="text-xs text-muted-foreground">
                      Generated {format(report.generatedAt, 'MMM dd, yyyy · HH:mm')}
                    </p>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <Badge
                    variant={
                      report.status === 'ready'
                        ? 'success'
                        : report.status === 'generating'
                        ? 'warning'
                        : 'danger'
                    }
                  >
                    {report.status}
                  </Badge>
                  {report.status === 'ready' && (
                    <Button variant="ghost" size="sm">
                      <Download className="h-3.5 w-3.5" />
                      Download
                    </Button>
                  )}
                </div>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
