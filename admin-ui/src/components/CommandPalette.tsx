import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import {
  Activity,
  BarChart3,
  Boxes,
  Brain,
  ChevronRight,
  Code,
  FileText,
  Globe,
  Key,
  LayoutDashboard,
  LogOut,
  MessageSquare,
  Moon,
  Network,
  Radio,
  Route as RouteIcon,
  Search,
  Settings as SettingsIcon,
  Sun,
  Wifi,
  Zap,
} from 'lucide-react'
import { useTheme } from '../contexts/ThemeContext'
import { useAuth } from '../contexts/AuthContext'
import { Kbd } from './ui/Kbd'
import { cn } from '../lib/utils'

interface CommandPaletteProps {
  open: boolean
  onClose: () => void
}

interface Command {
  id: string
  label: string
  hint?: string
  group: 'Navigate' | 'Test protocols' | 'Actions'
  icon: React.ComponentType<{ className?: string }>
  keywords?: string
  perform: () => void
}

export function CommandPalette({ open, onClose }: CommandPaletteProps) {
  const navigate = useNavigate()
  const { theme, toggleTheme } = useTheme()
  const { logout } = useAuth()
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)

  const commands = useMemo<Command[]>(() => {
    const go = (path: string) => () => {
      navigate(path)
      onClose()
    }
    return [
      { id: 'dashboard', label: 'Dashboard', group: 'Navigate', icon: LayoutDashboard, perform: go('/dashboard'), hint: 'Overview & live metrics' },
      { id: 'agents', label: 'Agents', group: 'Navigate', icon: Boxes, perform: go('/agents'), hint: 'Register & route agents', keywords: 'agent edge slug upstream route sso' },
      { id: 'monitoring', label: 'Mission Control', group: 'Navigate', icon: Activity, perform: go('/monitoring'), hint: 'Live agent health & traffic', keywords: 'monitoring health live status uptime latency sse' },
      { id: 'ai', label: 'AI Insights', group: 'Navigate', icon: Brain, perform: go('/ai-insights'), hint: 'Anomalies & forecasts' },
      { id: 'analytics', label: 'Analytics', group: 'Navigate', icon: BarChart3, perform: go('/analytics') },
      { id: 'reports', label: 'Reports', group: 'Navigate', icon: FileText, perform: go('/reports') },
      { id: 'keys', label: 'API Keys', group: 'Navigate', icon: Key, perform: go('/api-keys') },
      { id: 'protocols', label: 'Protocols', group: 'Navigate', icon: Network, perform: go('/protocols') },
      { id: 'routes', label: 'Routes', group: 'Navigate', icon: RouteIcon, perform: go('/routes') },
      { id: 'logs', label: 'Logs', group: 'Navigate', icon: FileText, perform: go('/logs') },
      { id: 'settings', label: 'Settings', group: 'Navigate', icon: SettingsIcon, perform: go('/settings') },
      { id: 'test-mcp', label: 'Test MCP', group: 'Test protocols', icon: Zap, keywords: 'mcp', perform: go('/test/mcp') },
      { id: 'test-a2a', label: 'Test A2A', group: 'Test protocols', icon: MessageSquare, keywords: 'a2a agent', perform: go('/test/a2a') },
      { id: 'test-http', label: 'Test HTTP/REST', group: 'Test protocols', icon: Globe, keywords: 'http rest', perform: go('/test/http') },
      { id: 'test-mqtt', label: 'Test MQTT', group: 'Test protocols', icon: Radio, keywords: 'mqtt', perform: go('/test/mqtt') },
      { id: 'test-ws', label: 'Test WebSocket', group: 'Test protocols', icon: Wifi, keywords: 'websocket ws', perform: go('/test/websocket') },
      { id: 'test-grpc', label: 'Test gRPC', group: 'Test protocols', icon: Code, keywords: 'grpc', perform: go('/test/grpc') },
      {
        id: 'theme',
        label: theme === 'dark' ? 'Switch to Light Mode' : 'Switch to Dark Mode',
        group: 'Actions',
        icon: theme === 'dark' ? Sun : Moon,
        keywords: 'theme dark light',
        perform: () => {
          toggleTheme()
          onClose()
        },
      },
      {
        id: 'logout',
        label: 'Sign out',
        group: 'Actions',
        icon: LogOut,
        keywords: 'logout sign out',
        perform: () => {
          onClose()
          logout()
        },
      },
    ]
  }, [navigate, theme, toggleTheme, logout, onClose])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return commands
    return commands.filter((c) => {
      const haystack = `${c.label} ${c.hint ?? ''} ${c.keywords ?? ''} ${c.group}`.toLowerCase()
      return haystack.includes(q)
    })
  }, [commands, query])

  const grouped = useMemo(() => {
    const groups: Record<string, Command[]> = {}
    for (const cmd of filtered) {
      if (!groups[cmd.group]) groups[cmd.group] = []
      groups[cmd.group].push(cmd)
    }
    return groups
  }, [filtered])

  useEffect(() => {
    if (open) {
      setQuery('')
      setActive(0)
      requestAnimationFrame(() => inputRef.current?.focus())
    }
  }, [open])

  useEffect(() => setActive(0), [query])

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        onClose()
      } else if (e.key === 'ArrowDown') {
        e.preventDefault()
        setActive((i) => Math.min(filtered.length - 1, i + 1))
      } else if (e.key === 'ArrowUp') {
        e.preventDefault()
        setActive((i) => Math.max(0, i - 1))
      } else if (e.key === 'Enter') {
        e.preventDefault()
        filtered[active]?.perform()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, onClose, filtered, active])

  useEffect(() => {
    if (!open) return
    const el = listRef.current?.querySelector<HTMLElement>(`[data-index="${active}"]`)
    el?.scrollIntoView({ block: 'nearest' })
  }, [active, open])

  if (!open) return null

  let runningIndex = -1

  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Command palette"
      className="fixed inset-0 z-[150] flex items-start justify-center p-4 pt-[15vh] sm:pt-[18vh]"
    >
      <button
        type="button"
        aria-label="Close command palette"
        onClick={onClose}
        className="absolute inset-0 bg-background/80 backdrop-blur-sm"
      />
      <div className="relative z-10 w-full max-w-xl overflow-hidden rounded-2xl border border-border bg-popover text-popover-foreground shadow-2xl shadow-black/40 animate-scale-in">
        <div className="flex items-center gap-3 border-b border-border px-4">
          <Search className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
          <input
            ref={inputRef}
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search pages, actions, protocols…"
            className="h-12 flex-1 bg-transparent text-sm text-foreground placeholder:text-muted-foreground focus:outline-none"
            aria-label="Command palette search"
          />
          <Kbd>esc</Kbd>
        </div>

        <div ref={listRef} className="max-h-[50vh] overflow-y-auto py-2">
          {filtered.length === 0 ? (
            <div className="px-4 py-10 text-center text-sm text-muted-foreground">
              No results for &ldquo;{query}&rdquo;
            </div>
          ) : (
            Object.entries(grouped).map(([group, items]) => (
              <div key={group} className="px-2 pb-1.5">
                <div className="px-3 pb-1 pt-2 text-xs font-medium uppercase tracking-wider text-muted-foreground/70">
                  {group}
                </div>
                {items.map((cmd) => {
                  runningIndex += 1
                  const idx = runningIndex
                  const isActive = idx === active
                  const Icon = cmd.icon
                  return (
                    <button
                      key={cmd.id}
                      data-index={idx}
                      onMouseEnter={() => setActive(idx)}
                      onClick={cmd.perform}
                      className={cn(
                        'group flex w-full items-center gap-3 rounded-lg px-3 py-2 text-left text-sm transition-colors',
                        isActive
                          ? 'bg-accent text-accent-foreground'
                          : 'text-foreground hover:bg-accent/50'
                      )}
                    >
                      <Icon
                        className={cn(
                          'h-4 w-4 shrink-0 transition-colors',
                          isActive ? 'text-primary' : 'text-muted-foreground'
                        )}
                      />
                      <span className="flex-1 truncate font-medium">{cmd.label}</span>
                      {cmd.hint && (
                        <span className="hidden text-xs text-muted-foreground sm:inline">
                          {cmd.hint}
                        </span>
                      )}
                      <ChevronRight
                        className={cn(
                          'h-3.5 w-3.5 shrink-0 transition-opacity',
                          isActive ? 'opacity-100 text-muted-foreground' : 'opacity-0'
                        )}
                      />
                    </button>
                  )
                })}
              </div>
            ))
          )}
        </div>

        <div className="flex items-center justify-between border-t border-border bg-muted/30 px-4 py-2 text-xs text-muted-foreground">
          <div className="flex items-center gap-3">
            <span className="flex items-center gap-1">
              <Kbd>↑</Kbd>
              <Kbd>↓</Kbd>
              navigate
            </span>
            <span className="flex items-center gap-1">
              <Kbd>↵</Kbd>
              select
            </span>
          </div>
          <span className="flex items-center gap-1">
            <Activity className="h-3.5 w-3.5" />
            AgentAnywhere Universal Gateway
          </span>
        </div>
      </div>
    </div>,
    document.body
  )
}
