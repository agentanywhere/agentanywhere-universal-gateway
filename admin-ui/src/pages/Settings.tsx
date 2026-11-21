import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Bell,
  Code,
  Copy,
  Globe,
  Mail,
  MessageSquare,
  Plus,
  Radio,
  RefreshCw,
  Save,
  Settings as SettingsIcon,
  Star,
  StarOff,
  TestTube,
  Trash2,
  Wifi,
  Zap,
} from 'lucide-react'
import { adminApi, configString } from '../api/admin'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/Card'
import { PageHeader } from '../components/ui/PageHeader'
import { Button } from '../components/ui/Button'
import { Input } from '../components/ui/Input'
import { Select } from '../components/ui/Select'
import { Badge } from '../components/ui/Badge'
import { IconButton } from '../components/ui/IconButton'
import { EmptyState } from '../components/ui/EmptyState'
import { Skeleton } from '../components/ui/Skeleton'
import { Dialog } from '../components/ui/Dialog'
import { useToast } from '../components/ui/Toast'
import { cn } from '../lib/utils'

const PROTOCOLS = [
  { id: 'mcp', name: 'MCP', icon: Zap, color: 'text-sky-500' },
  { id: 'a2a', name: 'A2A', icon: MessageSquare, color: 'text-violet-500' },
  { id: 'http', name: 'HTTP/REST', icon: Globe, color: 'text-emerald-500' },
  { id: 'mqtt', name: 'MQTT', icon: Radio, color: 'text-amber-500' },
  { id: 'websocket', name: 'WebSocket', icon: Wifi, color: 'text-cyan-500' },
  { id: 'grpc', name: 'gRPC', icon: Code, color: 'text-pink-500' },
]

type Tab = 'protocols' | 'profiles' | 'notifications'

export default function Settings() {
  const queryClient = useQueryClient()
  const { toast } = useToast()
  const [activeTab, setActiveTab] = useState<Tab>('protocols')
  const [activeProtocol, setActiveProtocol] = useState('mcp')
  const [selectedProfile, setSelectedProfile] = useState<string | null>(null)
  const [profileName, setProfileName] = useState('')
  const [profileDescription, setProfileDescription] = useState('')
  const [confirmDelete, setConfirmDelete] = useState<{ id: string; name: string } | null>(null)

  const [emailConfig, setEmailConfig] = useState({
    smtp_host: '',
    smtp_port: 587,
    smtp_user: '',
    smtp_password: '',
    smtp_use_tls: true,
    from_email: '',
  })
  const [emailRecipients, setEmailRecipients] = useState('')
  const [alertThresholds, setAlertThresholds] = useState({
    rate_limit_enabled: true,
    circuit_breaker_enabled: true,
    anomaly_score_threshold: 0.7,
    error_rate_threshold: 0.1,
    traffic_spike_threshold: 2.0,
    min_interval_seconds: 300,
    email_rate_limit_per_hour: 50,
  })

  const { data: config, isLoading: configLoading } = useQuery({
    queryKey: ['config'],
    queryFn: adminApi.getConfig,
  })

  const { data: protocols, isLoading: protocolsLoading } = useQuery({
    queryKey: ['protocols'],
    queryFn: adminApi.getSupportedProtocols,
  })

  const { data: profiles, isLoading: profilesLoading } = useQuery({
    queryKey: ['settings-profiles'],
    queryFn: adminApi.getSettingsProfiles,
  })

  const { data: alertThresholdsData } = useQuery({
    queryKey: ['alert-thresholds'],
    queryFn: adminApi.getAlertThresholds,
    enabled: activeTab === 'notifications',
  })

  useEffect(() => {
    if (alertThresholdsData?.thresholds) {
      setAlertThresholds((cur) => ({
        ...cur,
        ...alertThresholdsData.thresholds,
        email_rate_limit_per_hour:
          alertThresholdsData.email_rate_limit_per_hour ?? cur.email_rate_limit_per_hour,
      }))
    }
  }, [alertThresholdsData])

  const createProfileMutation = useMutation({
    mutationFn: adminApi.createSettingsProfile,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['settings-profiles'] })
      toast({ title: 'Profile saved', variant: 'success' })
      setProfileName('')
      setProfileDescription('')
    },
    onError: () => toast({ title: "Couldn't save profile", variant: 'error' }),
  })

  const deleteProfileMutation = useMutation({
    mutationFn: adminApi.deleteSettingsProfile,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['settings-profiles'] })
      toast({ title: 'Profile deleted', variant: 'success' })
      setConfirmDelete(null)
      if (selectedProfile === confirmDelete?.id) setSelectedProfile(null)
    },
  })

  const setDefaultMutation = useMutation({
    mutationFn: adminApi.setDefaultProfile,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['settings-profiles'] })
      toast({ title: 'Default profile updated', variant: 'success' })
    },
  })

  const configureEmailMutation = useMutation({
    mutationFn: adminApi.configureEmailNotifications,
    onSuccess: () => toast({ title: 'Email config saved', variant: 'success' }),
    onError: () => toast({ title: 'Failed to save email config', variant: 'error' }),
  })

  const setEmailRecipientsMutation = useMutation({
    mutationFn: adminApi.setEmailRecipients,
    onSuccess: () => toast({ title: 'Recipients updated', variant: 'success' }),
  })

  const testEmailMutation = useMutation({
    mutationFn: adminApi.testEmailNotifications,
    onSuccess: (data) =>
      toast({
        title: data.connected ? 'Email service is working' : 'Email connection failed',
        variant: data.connected ? 'success' : 'error',
      }),
    onError: () => toast({ title: 'Email test failed', variant: 'error' }),
  })

  const updateAlertThresholdsMutation = useMutation({
    mutationFn: adminApi.updateAlertThresholds,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['alert-thresholds'] })
      toast({ title: 'Thresholds updated', variant: 'success' })
    },
  })

  const currentProtocol = protocols?.find((p: any) => p.type === activeProtocol)

  const handleSaveProfile = () => {
    if (!profileName.trim()) return
    const protocolSettings: Record<string, any> = {}
    PROTOCOLS.forEach((proto) => {
      protocolSettings[proto.id] = {
        rate_limit_per_minute: 60,
        rate_limit_per_hour: 1000,
        timeout: 30,
        circuit_breaker_enabled: true,
      }
    })
    createProfileMutation.mutate({
      name: profileName,
      description: profileDescription,
      protocol_settings: protocolSettings,
      global_settings: {},
      is_default: false,
    })
  }

  const handleCopyProfile = (profileId: string) => {
    const profile = profiles?.find((p: any) => p.id === profileId)
    if (profile) {
      setProfileName(`${profile.name} (Copy)`)
      setProfileDescription(profile.description || '')
      setSelectedProfile(null)
    }
  }

  const tabs: { id: Tab; label: string; icon: React.ComponentType<{ className?: string }> }[] = [
    { id: 'protocols', label: 'Protocol settings', icon: SettingsIcon },
    { id: 'profiles', label: 'Profiles', icon: Star },
    { id: 'notifications', label: 'Alerts', icon: Bell },
  ]

  const isInitialLoading = configLoading || protocolsLoading

  return (
    <div className="space-y-6 sm:space-y-8">
      <PageHeader
        title="Settings"
        description="Protocol-specific configuration, reusable profiles, and alerting."
      />

      <div
        role="tablist"
        aria-label="Settings sections"
        className="flex gap-1 overflow-x-auto rounded-lg border border-border bg-card/50 p-1"
      >
        {tabs.map((tab) => {
          const Icon = tab.icon
          const isActive = activeTab === tab.id
          return (
            <button
              key={tab.id}
              role="tab"
              aria-selected={isActive}
              onClick={() => setActiveTab(tab.id)}
              className={cn(
                'inline-flex items-center gap-2 whitespace-nowrap rounded-md px-3 py-1.5 text-sm font-medium transition-colors',
                isActive
                  ? 'bg-background text-foreground shadow-sm'
                  : 'text-muted-foreground hover:text-foreground'
              )}
            >
              <Icon className="h-4 w-4" />
              {tab.label}
            </button>
          )
        })}
      </div>

      {activeTab === 'protocols' && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-4 lg:gap-6">
          <Card className="lg:col-span-1">
            <CardHeader className="pb-3">
              <CardTitle>Protocols</CardTitle>
            </CardHeader>
            <CardContent className="space-y-1 p-3 pt-0">
              {PROTOCOLS.map((proto) => {
                const Icon = proto.icon
                const isActive = activeProtocol === proto.id
                return (
                  <button
                    key={proto.id}
                    onClick={() => setActiveProtocol(proto.id)}
                    className={cn(
                      'flex w-full items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors',
                      isActive
                        ? 'bg-primary/10 text-primary'
                        : 'text-muted-foreground hover:bg-accent hover:text-foreground'
                    )}
                  >
                    <Icon className={cn('h-4 w-4', isActive ? 'text-primary' : proto.color)} />
                    {proto.name}
                  </button>
                )
              })}
            </CardContent>
          </Card>

          <Card className="lg:col-span-3">
            {isInitialLoading ? (
              <CardContent className="space-y-4 p-6">
                <Skeleton className="h-6 w-1/3" />
                <Skeleton className="h-4 w-2/3" />
                <div className="grid grid-cols-2 gap-4 pt-4">
                  {Array.from({ length: 4 }).map((_, i) => (
                    <Skeleton key={i} className="h-20" />
                  ))}
                </div>
              </CardContent>
            ) : (
              <>
                <CardHeader>
                  <CardTitle className="text-lg">{currentProtocol?.name || 'Protocol settings'}</CardTitle>
                  <CardDescription>{currentProtocol?.description}</CardDescription>
                </CardHeader>
                <CardContent className="space-y-6">
                  <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                    <Field label="Rate limit (per minute)">
                      <Input
                        type="number"
                        defaultValue={currentProtocol?.default_settings?.rate_limit_per_minute || 60}
                      />
                    </Field>
                    <Field label="Rate limit (per hour)">
                      <Input
                        type="number"
                        defaultValue={currentProtocol?.default_settings?.rate_limit_per_hour || 1000}
                      />
                    </Field>
                    <Field label="Timeout (seconds)">
                      <Input
                        type="number"
                        defaultValue={currentProtocol?.default_settings?.timeout || 30}
                      />
                    </Field>
                    {(activeProtocol === 'mqtt' || activeProtocol === 'websocket') && (
                      <>
                        <Field label="Max connections">
                          <Input
                            type="number"
                            defaultValue={currentProtocol?.default_settings?.max_connections || 1000}
                          />
                        </Field>
                        <Field label="Keep alive (seconds)">
                          <Input
                            type="number"
                            defaultValue={currentProtocol?.default_settings?.keep_alive || 60}
                          />
                        </Field>
                      </>
                    )}
                    {activeProtocol === 'mqtt' && (
                      <Field label="QoS level">
                        <Select defaultValue={currentProtocol?.default_settings?.qos_level || 1}>
                          <option value={0}>0 — At most once</option>
                          <option value={1}>1 — At least once</option>
                          <option value={2}>2 — Exactly once</option>
                        </Select>
                      </Field>
                    )}
                  </div>

                  <div className="flex flex-wrap items-center gap-4 border-t border-border pt-4">
                    <Toggle
                      defaultChecked={currentProtocol?.default_settings?.circuit_breaker_enabled}
                      label="Circuit breaker"
                    />
                    {(activeProtocol === 'http' || activeProtocol === 'grpc') && (
                      <Toggle
                        defaultChecked={currentProtocol?.default_settings?.compression_enabled}
                        label="Compression"
                      />
                    )}
                  </div>

                  <div className="flex justify-end gap-2 border-t border-border pt-4">
                    <Button variant="ghost">
                      <RefreshCw className="h-4 w-4" />
                      Reset
                    </Button>
                    <Button>
                      <Save className="h-4 w-4" />
                      Save changes
                    </Button>
                  </div>
                </CardContent>
              </>
            )}
          </Card>

          <Card className="lg:col-span-4">
            <CardHeader>
              <CardTitle>Global configuration</CardTitle>
              <CardDescription>Read-only — sourced from environment.</CardDescription>
            </CardHeader>
            <CardContent>
              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                <Field label="MCP backend URL">
                  <Input value={configString(config, 'mcp_backend_url')} readOnly />
                </Field>
                <Field label="A2A backend URL">
                  <Input value={configString(config, 'a2a_backend_url')} readOnly />
                </Field>
                <Field label="Environment">
                  <Input value={configString(config, 'environment')} readOnly />
                </Field>
                <Field label="Log level">
                  <Input value={configString(config, 'log_level')} readOnly />
                </Field>
              </div>
            </CardContent>
          </Card>
        </div>
      )}

      {activeTab === 'profiles' && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-3 lg:gap-6">
          <Card className="lg:col-span-1">
            <CardHeader className="flex-row items-center justify-between space-y-0">
              <div>
                <CardTitle>Profiles</CardTitle>
                <CardDescription>Reusable configurations.</CardDescription>
              </div>
              <IconButton
                label="New profile"
                variant="subtle"
                onClick={() => {
                  setSelectedProfile(null)
                  setProfileName('')
                  setProfileDescription('')
                }}
              >
                <Plus className="h-4 w-4" />
              </IconButton>
            </CardHeader>
            <CardContent className="space-y-2 p-3 pt-0">
              {profilesLoading ? (
                <>
                  <Skeleton className="h-16" />
                  <Skeleton className="h-16" />
                </>
              ) : profiles && profiles.length > 0 ? (
                profiles.map((profile: any) => {
                  const isSelected = selectedProfile === profile.id
                  return (
                    <div
                      key={profile.id}
                      onClick={() => {
                        setSelectedProfile(profile.id)
                        setProfileName(profile.name)
                        setProfileDescription(profile.description || '')
                      }}
                      className={cn(
                        'cursor-pointer rounded-lg border bg-background/40 p-3 transition-all',
                        isSelected
                          ? 'border-primary/40 ring-1 ring-primary/30'
                          : 'border-border hover:border-primary/30'
                      )}
                    >
                      <div className="flex items-start justify-between gap-2">
                        <div className="min-w-0">
                          <div className="flex items-center gap-2 text-sm font-medium text-foreground">
                            <span className="truncate">{profile.name}</span>
                            {profile.is_default && (
                              <Badge variant="warning" className="text-[10px]">
                                Default
                              </Badge>
                            )}
                          </div>
                          {profile.description && (
                            <p className="mt-0.5 truncate text-xs text-muted-foreground">
                              {profile.description}
                            </p>
                          )}
                        </div>
                      </div>
                      <div className="mt-2 flex items-center gap-1">
                        <IconButton
                          label="Duplicate profile"
                          size="sm"
                          onClick={(e) => {
                            e.stopPropagation()
                            handleCopyProfile(profile.id)
                          }}
                        >
                          <Copy className="h-3 w-3" />
                        </IconButton>
                        <IconButton
                          label={profile.is_default ? 'Default profile' : 'Set as default'}
                          size="sm"
                          onClick={(e) => {
                            e.stopPropagation()
                            if (!profile.is_default) setDefaultMutation.mutate(profile.id)
                          }}
                        >
                          {profile.is_default ? (
                            <Star className="h-3 w-3 fill-amber-500 text-amber-500" />
                          ) : (
                            <StarOff className="h-3 w-3" />
                          )}
                        </IconButton>
                        <IconButton
                          label="Delete profile"
                          variant="danger"
                          size="sm"
                          onClick={(e) => {
                            e.stopPropagation()
                            setConfirmDelete({ id: profile.id, name: profile.name })
                          }}
                        >
                          <Trash2 className="h-3 w-3" />
                        </IconButton>
                      </div>
                    </div>
                  )
                })
              ) : (
                <EmptyState
                  className="m-0"
                  icon={<Star className="h-5 w-5" />}
                  title="No profiles yet"
                  description="Create one to save and reuse a configuration."
                />
              )}
            </CardContent>
          </Card>

          <Card className="lg:col-span-2">
            <CardHeader>
              <CardTitle>{selectedProfile ? 'Edit profile' : 'New profile'}</CardTitle>
              <CardDescription>Save and reuse protocol configurations.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-5">
              <Field label="Profile name *">
                <Input
                  value={profileName}
                  onChange={(e) => setProfileName(e.target.value)}
                  placeholder="e.g., Production, Development, High-traffic"
                />
              </Field>
              <Field label="Description">
                <textarea
                  value={profileDescription}
                  onChange={(e) => setProfileDescription(e.target.value)}
                  rows={3}
                  placeholder="Describe this profile…"
                  className="w-full resize-none rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground transition-colors focus-visible:border-primary/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background"
                />
              </Field>

              <div className="flex justify-end border-t border-border pt-4">
                <Button
                  onClick={handleSaveProfile}
                  isLoading={createProfileMutation.isPending}
                  disabled={!profileName.trim()}
                >
                  <Save className="h-4 w-4" />
                  Save profile
                </Button>
              </div>
            </CardContent>
          </Card>
        </div>
      )}

      {activeTab === 'notifications' && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2 lg:gap-6">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Mail className="h-5 w-5 text-primary" />
                Email delivery
              </CardTitle>
              <CardDescription>SMTP credentials and recipients.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <Field label="SMTP host">
                  <Input
                    value={emailConfig.smtp_host}
                    onChange={(e) => setEmailConfig({ ...emailConfig, smtp_host: e.target.value })}
                    placeholder="smtp.example.com"
                  />
                </Field>
                <Field label="SMTP port">
                  <Input
                    type="number"
                    value={emailConfig.smtp_port}
                    onChange={(e) =>
                      setEmailConfig({ ...emailConfig, smtp_port: Number(e.target.value) })
                    }
                  />
                </Field>
                <Field label="SMTP user">
                  <Input
                    value={emailConfig.smtp_user}
                    onChange={(e) => setEmailConfig({ ...emailConfig, smtp_user: e.target.value })}
                    placeholder="user@example.com"
                  />
                </Field>
                <Field label="SMTP password">
                  <Input
                    type="password"
                    value={emailConfig.smtp_password}
                    onChange={(e) =>
                      setEmailConfig({ ...emailConfig, smtp_password: e.target.value })
                    }
                  />
                </Field>
                <Field label="From email">
                  <Input
                    type="email"
                    value={emailConfig.from_email}
                    onChange={(e) =>
                      setEmailConfig({ ...emailConfig, from_email: e.target.value })
                    }
                  />
                </Field>
                <div className="flex items-end">
                  <Toggle
                    checked={emailConfig.smtp_use_tls}
                    onChange={(checked) =>
                      setEmailConfig({ ...emailConfig, smtp_use_tls: checked })
                    }
                    label="Use TLS"
                  />
                </div>
              </div>

              <Field label="Recipients (comma separated)">
                <Input
                  value={emailRecipients}
                  onChange={(e) => setEmailRecipients(e.target.value)}
                  placeholder="ops@example.com, on-call@example.com"
                />
              </Field>

              <div className="flex flex-wrap gap-2 border-t border-border pt-4">
                <Button onClick={() => configureEmailMutation.mutate(emailConfig)} isLoading={configureEmailMutation.isPending}>
                  <Save className="h-4 w-4" />
                  Save SMTP
                </Button>
                <Button
                  variant="secondary"
                  onClick={() =>
                    setEmailRecipientsMutation.mutate(
                      emailRecipients.split(',').map((s) => s.trim()).filter(Boolean)
                    )
                  }
                  isLoading={setEmailRecipientsMutation.isPending}
                >
                  <Save className="h-4 w-4" />
                  Save recipients
                </Button>
                <Button
                  variant="outline"
                  onClick={() => testEmailMutation.mutate()}
                  isLoading={testEmailMutation.isPending}
                >
                  <TestTube className="h-4 w-4" />
                  Send test email
                </Button>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Bell className="h-5 w-5 text-amber-500" />
                Alert thresholds
              </CardTitle>
              <CardDescription>When to fire notifications.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-3">
                <Toggle
                  checked={alertThresholds.rate_limit_enabled}
                  onChange={(checked) =>
                    setAlertThresholds({ ...alertThresholds, rate_limit_enabled: checked })
                  }
                  label="Rate-limit alerts"
                />
                <Toggle
                  checked={alertThresholds.circuit_breaker_enabled}
                  onChange={(checked) =>
                    setAlertThresholds({ ...alertThresholds, circuit_breaker_enabled: checked })
                  }
                  label="Circuit-breaker alerts"
                />
              </div>

              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <Field label="Anomaly score threshold">
                  <Input
                    type="number"
                    step={0.05}
                    min={0}
                    max={1}
                    value={alertThresholds.anomaly_score_threshold}
                    onChange={(e) =>
                      setAlertThresholds({
                        ...alertThresholds,
                        anomaly_score_threshold: Number(e.target.value),
                      })
                    }
                  />
                </Field>
                <Field label="Error-rate threshold">
                  <Input
                    type="number"
                    step={0.01}
                    min={0}
                    max={1}
                    value={alertThresholds.error_rate_threshold}
                    onChange={(e) =>
                      setAlertThresholds({
                        ...alertThresholds,
                        error_rate_threshold: Number(e.target.value),
                      })
                    }
                  />
                </Field>
                <Field label="Traffic-spike threshold (×)">
                  <Input
                    type="number"
                    step={0.1}
                    min={1}
                    value={alertThresholds.traffic_spike_threshold}
                    onChange={(e) =>
                      setAlertThresholds({
                        ...alertThresholds,
                        traffic_spike_threshold: Number(e.target.value),
                      })
                    }
                  />
                </Field>
                <Field label="Cooldown (seconds)">
                  <Input
                    type="number"
                    min={0}
                    value={alertThresholds.min_interval_seconds}
                    onChange={(e) =>
                      setAlertThresholds({
                        ...alertThresholds,
                        min_interval_seconds: Number(e.target.value),
                      })
                    }
                  />
                </Field>
                <Field label="Max emails / hour">
                  <Input
                    type="number"
                    min={0}
                    value={alertThresholds.email_rate_limit_per_hour}
                    onChange={(e) =>
                      setAlertThresholds({
                        ...alertThresholds,
                        email_rate_limit_per_hour: Number(e.target.value),
                      })
                    }
                  />
                </Field>
              </div>

              <div className="flex justify-end border-t border-border pt-4">
                <Button
                  onClick={() => updateAlertThresholdsMutation.mutate(alertThresholds)}
                  isLoading={updateAlertThresholdsMutation.isPending}
                >
                  <Save className="h-4 w-4" />
                  Save thresholds
                </Button>
              </div>
            </CardContent>
          </Card>
        </div>
      )}

      <Dialog
        open={!!confirmDelete}
        onClose={() => setConfirmDelete(null)}
        title="Delete profile?"
        description={
          confirmDelete ? `"${confirmDelete.name}" will be removed permanently.` : ''
        }
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirmDelete(null)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              isLoading={deleteProfileMutation.isPending}
              onClick={() =>
                confirmDelete && deleteProfileMutation.mutate(confirmDelete.id)
              }
            >
              Delete profile
            </Button>
          </>
        }
      >
        <></>
      </Dialog>
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block space-y-1.5">
      <span className="block text-sm font-medium text-foreground">{label}</span>
      {children}
    </label>
  )
}

interface ToggleProps {
  label: string
  checked?: boolean
  defaultChecked?: boolean
  onChange?: (checked: boolean) => void
}

function Toggle({ label, checked, defaultChecked, onChange }: ToggleProps) {
  const isControlled = checked !== undefined
  const [internal, setInternal] = useState<boolean>(!!defaultChecked)
  const value = isControlled ? !!checked : internal

  const toggle = () => {
    const next = !value
    if (!isControlled) setInternal(next)
    onChange?.(next)
  }

  return (
    <button
      type="button"
      role="switch"
      aria-checked={value}
      onClick={toggle}
      className="inline-flex cursor-pointer items-center gap-2.5 text-sm text-foreground focus-visible:outline-none"
    >
      <span
        aria-hidden="true"
        className={cn(
          'relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors',
          value ? 'bg-primary' : 'bg-muted'
        )}
      >
        <span
          className={cn(
            'inline-block h-4 w-4 transform rounded-full bg-card shadow-sm transition-transform',
            value ? 'translate-x-[1.125rem]' : 'translate-x-0.5'
          )}
        />
      </span>
      <span>{label}</span>
    </button>
  )
}
