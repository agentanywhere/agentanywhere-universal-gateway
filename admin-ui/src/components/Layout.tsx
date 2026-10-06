import { useEffect, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import {
  Activity,
  BarChart3,
  Boxes,
  Brain,
  FileText,
  Key,
  LayoutDashboard,
  LogOut,
  Menu,
  Moon,
  Network,
  Route as RouteIcon,
  Search,
  Settings,
  Sun,
  Zap,
} from 'lucide-react'
import { useAuth } from '../contexts/AuthContext'
import { useTheme } from '../contexts/ThemeContext'
import { useMediaQuery } from '../hooks/useMediaQuery'
import { useHotkey } from '../hooks/useHotkey'
import { CommandPalette } from './CommandPalette'
import { IconButton } from './ui/IconButton'
import { Kbd } from './ui/Kbd'
import { cn } from '../lib/utils'

interface LayoutProps {
  children: React.ReactNode
}

const navGroups = [
  {
    label: 'Operate',
    items: [
      { name: 'Dashboard', href: '/dashboard', icon: LayoutDashboard },
      { name: 'Agents', href: '/agents', icon: Boxes },
      { name: 'Mission Control', href: '/monitoring', icon: Activity },
    ],
  },
  {
    label: 'Observe',
    items: [
      { name: 'AI Insights', href: '/ai-insights', icon: Brain },
      { name: 'Analytics', href: '/analytics', icon: BarChart3 },
      { name: 'Reports', href: '/reports', icon: FileText },
      { name: 'Logs', href: '/logs', icon: FileText },
    ],
  },
  {
    label: 'Configure',
    items: [
      { name: 'API Keys', href: '/api-keys', icon: Key },
      { name: 'Protocols', href: '/protocols', icon: Network },
      { name: 'Routes', href: '/routes', icon: RouteIcon },
      { name: 'Settings', href: '/settings', icon: Settings },
    ],
  },
]

const PAGE_TITLES: Record<string, string> = {
  '/dashboard': 'Dashboard',
  '/agents': 'Agents',
  '/monitoring': 'Mission Control',
  '/ai-insights': 'AI Insights',
  '/analytics': 'Analytics',
  '/reports': 'Reports',
  '/api-keys': 'API Keys',
  '/protocols': 'Protocols',
  '/routes': 'Routes',
  '/logs': 'Logs',
  '/settings': 'Settings',
}

export default function Layout({ children }: LayoutProps) {
  const location = useLocation()
  const { logout, user } = useAuth()
  const { theme, toggleTheme } = useTheme()
  const isDesktop = useMediaQuery('(min-width: 1024px)')

  const [drawerOpen, setDrawerOpen] = useState(false)
  const [paletteOpen, setPaletteOpen] = useState(false)

  useHotkey('k', () => setPaletteOpen((o) => !o), {
    meta: true,
    ignoreEditable: true,
  })

  useEffect(() => {
    setDrawerOpen(false)
  }, [location.pathname])

  useEffect(() => {
    if (drawerOpen) {
      const prev = document.body.style.overflow
      document.body.style.overflow = 'hidden'
      const onKey = (e: KeyboardEvent) => {
        if (e.key === 'Escape') setDrawerOpen(false)
      }
      window.addEventListener('keydown', onKey)
      return () => {
        document.body.style.overflow = prev
        window.removeEventListener('keydown', onKey)
      }
    }
  }, [drawerOpen])

  const currentTitle = PAGE_TITLES[location.pathname] || 'Admin'

  const SidebarContent = (
    <div className="flex h-full flex-col">
      <div className="flex h-[4.25rem] items-center border-b border-border/80 px-5">
        <Link to="/dashboard" className="group flex min-w-0 items-center gap-3">
          <div className="brand-mark shadow-soft transition-transform duration-200 group-hover:scale-[1.03]">
            <Zap className="relative z-10 h-4 w-4 text-white" strokeWidth={2.4} />
          </div>
          <div className="min-w-0 leading-tight">
            <div className="font-display text-[15px] font-semibold tracking-brand text-foreground">
              AgentAnywhere
            </div>
            <div className="mt-0.5 font-mono text-[10px] font-medium uppercase tracking-[0.16em] text-muted-foreground">
              Gateway
            </div>
          </div>
        </Link>
      </div>

      <nav className="flex-1 overflow-y-auto px-3 py-5">
        <div className="space-y-6">
          {navGroups.map((group) => (
            <div key={group.label}>
              <div className="mb-2 px-3 font-mono text-[10px] font-medium uppercase tracking-[0.18em] text-muted-foreground/80">
                {group.label}
              </div>
              <ul className="space-y-0.5">
                {group.items.map((item) => {
                  const isActive =
                    location.pathname === item.href ||
                    location.pathname.startsWith(`${item.href}/`)
                  const Icon = item.icon
                  return (
                    <li key={item.name}>
                      <Link
                        to={item.href}
                        className={cn(
                          'group relative flex items-center gap-3 rounded-md px-3 py-2 text-[13px] font-medium transition-colors duration-150',
                          isActive
                            ? 'bg-primary/10 text-primary'
                            : 'text-muted-foreground hover:bg-accent/70 hover:text-foreground'
                        )}
                        aria-current={isActive ? 'page' : undefined}
                      >
                        {isActive && (
                          <span
                            aria-hidden="true"
                            className="absolute left-0 top-1.5 bottom-1.5 w-[2px] rounded-r-sm bg-primary"
                          />
                        )}
                        <Icon
                          className={cn(
                            'h-4 w-4 shrink-0 transition-colors',
                            isActive
                              ? 'text-primary'
                              : 'text-muted-foreground/75 group-hover:text-foreground'
                          )}
                        />
                        <span className="truncate">{item.name}</span>
                      </Link>
                    </li>
                  )
                })}
              </ul>
            </div>
          ))}
        </div>
      </nav>

      <div className="space-y-1 border-t border-border/80 p-3">
        <button
          type="button"
          onClick={toggleTheme}
          className="flex w-full items-center gap-3 rounded-md px-3 py-2 text-[13px] font-medium text-muted-foreground transition-colors hover:bg-accent/70 hover:text-foreground"
        >
          {theme === 'dark' ? (
            <Sun className="h-4 w-4" />
          ) : (
            <Moon className="h-4 w-4" />
          )}
          <span>{theme === 'dark' ? 'Light mode' : 'Dark mode'}</span>
        </button>
        <button
          type="button"
          onClick={logout}
          className="flex w-full items-center gap-3 rounded-md px-3 py-2 text-[13px] font-medium text-muted-foreground transition-colors hover:bg-red-500/10 hover:text-red-500"
        >
          <LogOut className="h-4 w-4" />
          <span>Sign out</span>
        </button>
      </div>
    </div>
  )

  return (
    <div className="min-h-screen bg-background text-foreground">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-[300] focus:rounded-md focus:bg-primary focus:px-3 focus:py-2 focus:text-sm focus:font-medium focus:text-primary-foreground"
      >
        Skip to content
      </a>

      <aside
        className="fixed inset-y-0 left-0 z-40 hidden w-[15.5rem] border-r border-border/80 bg-sidebar/90 backdrop-blur-xl lg:block"
        aria-label="Primary"
      >
        {SidebarContent}
      </aside>

      {!isDesktop && (
        <>
          <button
            type="button"
            aria-label="Close menu"
            aria-hidden={!drawerOpen}
            tabIndex={drawerOpen ? 0 : -1}
            onClick={() => setDrawerOpen(false)}
            className={cn(
              'fixed inset-0 z-40 bg-background/75 backdrop-blur-sm transition-opacity lg:hidden',
              drawerOpen ? 'opacity-100' : 'pointer-events-none opacity-0'
            )}
          />
          <aside
            aria-label="Primary"
            aria-hidden={!drawerOpen}
            className={cn(
              'fixed inset-y-0 left-0 z-50 w-72 max-w-[85%] border-r border-border bg-card shadow-panel transition-transform duration-200 ease-out lg:hidden',
              drawerOpen ? 'translate-x-0' : '-translate-x-full'
            )}
          >
            {SidebarContent}
          </aside>
        </>
      )}

      <div className="lg:pl-[15.5rem]">
        <header className="sticky top-0 z-30 flex h-14 items-center gap-3 border-b border-border/80 bg-background/75 px-4 backdrop-blur-xl sm:px-6 lg:px-8">
          {!isDesktop && (
            <IconButton
              label="Open navigation"
              onClick={() => setDrawerOpen(true)}
              size="md"
              variant="ghost"
            >
              <Menu className="h-5 w-5" />
            </IconButton>
          )}
          <div className="min-w-0 flex-1">
            <h2 className="truncate font-display text-sm font-semibold tracking-tight text-foreground sm:text-[15px]">
              {currentTitle}
            </h2>
          </div>

          <button
            type="button"
            onClick={() => setPaletteOpen(true)}
            className="group hidden items-center gap-2 rounded-md border border-border/80 bg-card/70 px-3 py-1.5 text-[13px] text-muted-foreground transition-colors hover:border-border hover:bg-accent/60 hover:text-foreground sm:flex"
            aria-label="Open command palette"
          >
            <Search className="h-3.5 w-3.5" />
            <span className="hidden md:inline">Search</span>
            <span className="ml-1.5 hidden items-center gap-0.5 md:inline-flex">
              <Kbd>⌘</Kbd>
              <Kbd>K</Kbd>
            </span>
          </button>
          {user && (
            <div className="hidden max-w-[200px] truncate border-l border-border/80 pl-3 text-right sm:block">
              <div className="truncate text-[13px] font-medium text-foreground">{user.name}</div>
              {user.email && (
                <div className="truncate font-mono text-[11px] text-muted-foreground">{user.email}</div>
              )}
            </div>
          )}
          <IconButton
            label="Open command palette"
            variant="ghost"
            onClick={() => setPaletteOpen(true)}
            className="sm:hidden"
          >
            <Search className="h-5 w-5" />
          </IconButton>
        </header>

        <main id="main" className="px-4 pb-14 pt-6 sm:px-6 sm:pt-8 lg:px-8">
          <div className="mx-auto w-full max-w-7xl animate-fade-in space-y-6 sm:space-y-8">
            {children}
          </div>
        </main>
      </div>

      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} />
    </div>
  )
}
