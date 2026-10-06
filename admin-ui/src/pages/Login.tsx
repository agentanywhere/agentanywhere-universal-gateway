import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowRight, Lock, Shield, User, Zap } from 'lucide-react'
import { useAuth } from '../contexts/AuthContext'
import { adminApi } from '../api/admin'
import { idaEnabled, idaLogin } from '../ida'
import { Button } from '../components/ui/Button'
import { Input } from '../components/ui/Input'

export default function Login() {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [isLoading, setIsLoading] = useState(false)
  const [ssoLoading, setSsoLoading] = useState(false)
  const [ssoEnabled, setSsoEnabled] = useState(false)
  const [configLoaded, setConfigLoaded] = useState(false)
  const [showNative, setShowNative] = useState(false)
  const { login, isAuthenticated } = useAuth()
  const navigate = useNavigate()

  useEffect(() => {
    idaEnabled()
      .then((on) => {
        setSsoEnabled(on)
        setShowNative(!on)
      })
      .catch(() => setShowNative(true))
      .finally(() => setConfigLoaded(true))
  }, [])

  useEffect(() => {
    if (isAuthenticated) {
      navigate('/dashboard', { replace: true })
    }
  }, [isAuthenticated, navigate])

  const handleSso = async () => {
    setError('')
    setSsoLoading(true)
    try {
      await idaLogin()
    } catch {
      setError('Could not start OIDC SSO sign-in')
      setSsoLoading(false)
    }
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setIsLoading(true)

    if (!username.trim() || !password.trim()) {
      setError('Please enter both username and password')
      setIsLoading(false)
      return
    }

    try {
      const response = await adminApi.login(username, password)
      if (response.access_token) {
        login(response.access_token)
        navigate('/dashboard')
      } else {
        setError('Invalid response from server')
      }
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setError(detail || 'Invalid username or password')
    } finally {
      setIsLoading(false)
    }
  }

  if (!configLoaded) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background">
        <div className="h-9 w-9 animate-spin rounded-full border-2 border-primary border-t-transparent" />
      </div>
    )
  }

  return (
    <div className="relative min-h-screen overflow-hidden bg-background text-foreground">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 -z-10"
        style={{
          backgroundImage:
            'radial-gradient(72rem 40rem at 78% -18%, hsl(173 72% 36% / 0.18), transparent 58%), radial-gradient(48rem 28rem at -8% 100%, hsl(210 40% 30% / 0.12), transparent 55%)',
        }}
      />
      <div
        aria-hidden="true"
        className="mesh-grid absolute inset-0 -z-10 opacity-60 [mask-image:radial-gradient(ellipse_at_center,black_25%,transparent_72%)]"
      />

      <div className="mx-auto grid min-h-screen max-w-6xl grid-cols-1 lg:grid-cols-[1.15fr_0.85fr]">
        <section className="relative flex flex-col justify-between px-6 py-10 sm:px-10 lg:px-14 lg:py-14">
          <div className="flex items-center gap-3 animate-fade-in">
            <div className="brand-mark shadow-soft">
              <Zap className="relative z-10 h-4 w-4 text-white" strokeWidth={2.4} />
            </div>
            <span className="font-mono text-[11px] font-medium uppercase tracking-[0.2em] text-muted-foreground">
              Console
            </span>
          </div>

          <div className="max-w-xl space-y-6 py-16 animate-rise lg:py-0">
            <h1 className="font-display text-5xl font-semibold leading-[0.95] tracking-tight text-foreground sm:text-6xl lg:text-7xl">
              AgentAnywhere
            </h1>
            <p className="max-w-md text-base leading-relaxed text-muted-foreground sm:text-lg">
              Universal Gateway control plane for routing, observability, and agent health across the platform.
            </p>
            <div className="flex flex-wrap gap-x-6 gap-y-2 font-mono text-[11px] uppercase tracking-[0.14em] text-muted-foreground/90">
              <span>MCP · A2A · HTTP</span>
              <span>MQTT · gRPC · WS</span>
            </div>
          </div>

          <p className="hidden text-xs text-muted-foreground lg:block">
            Platform operators · routes, metrics, and agent health
          </p>
        </section>

        <section className="flex items-center justify-center px-6 py-10 sm:px-10 lg:px-12">
          <div className="w-full max-w-md animate-slide-up">
            <div className="surface-panel rounded-xl p-6 shadow-panel sm:p-8">
              <div className="mb-6 space-y-1.5">
                <h2 className="font-display text-xl font-semibold tracking-tight text-foreground">
                  Sign in
                </h2>
                <p className="text-sm text-muted-foreground">
                  Access the gateway admin console
                </p>
              </div>

              {ssoEnabled && (
                <div className="space-y-4">
                  <Button
                    type="button"
                    size="lg"
                    isLoading={ssoLoading}
                    onClick={handleSso}
                    className="w-full gap-2"
                  >
                    {!ssoLoading && <Shield className="h-4 w-4" />}
                    Continue with SSO
                    {!ssoLoading && <ArrowRight className="h-4 w-4" />}
                  </Button>
                  <p className="text-center text-xs text-muted-foreground">
                    Secured by{' '}
                    <span className="font-medium text-foreground">sso.example.com</span>
                  </p>
                  {!showNative && (
                    <button
                      type="button"
                      onClick={() => setShowNative(true)}
                      className="w-full text-center text-xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
                    >
                      Use a local password instead
                    </button>
                  )}
                </div>
              )}

              {showNative && (
                <form
                  className={`space-y-4 ${ssoEnabled ? 'mt-6 border-t border-border pt-6' : ''}`}
                  onSubmit={handleSubmit}
                >
                  {ssoEnabled && (
                    <p className="font-mono text-[10px] font-medium uppercase tracking-[0.16em] text-muted-foreground">
                      Local administrator
                    </p>
                  )}
                  <div className="space-y-1.5">
                    <label htmlFor="username" className="text-sm font-medium text-foreground">
                      Username
                    </label>
                    <Input
                      id="username"
                      type="text"
                      autoComplete="username"
                      leftIcon={<User className="h-4 w-4" />}
                      value={username}
                      onChange={(e) => setUsername(e.target.value)}
                      placeholder="admin"
                    />
                  </div>
                  <div className="space-y-1.5">
                    <label htmlFor="password" className="text-sm font-medium text-foreground">
                      Password
                    </label>
                    <Input
                      id="password"
                      type="password"
                      autoComplete="current-password"
                      leftIcon={<Lock className="h-4 w-4" />}
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      placeholder="ADMIN_TOKEN or SECRET_KEY"
                    />
                    <p className="text-xs text-muted-foreground">
                      There is no default password. Use the gateway&apos;s{' '}
                      <code className="font-mono">ADMIN_TOKEN</code>, or its{' '}
                      <code className="font-mono">SECRET_KEY</code> if no admin token is set.
                    </p>
                  </div>
                  {error && (
                    <div
                      role="alert"
                      className="animate-slide-up rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive"
                    >
                      {error}
                    </div>
                  )}
                  <Button
                    type="submit"
                    size="lg"
                    isLoading={isLoading}
                    variant={ssoEnabled ? 'secondary' : 'primary'}
                    className="w-full"
                  >
                    {isLoading ? 'Signing in…' : 'Sign in'}
                  </Button>
                </form>
              )}
            </div>

            <p className="mt-6 text-center text-xs text-muted-foreground lg:hidden">
              Platform operators · routes, metrics, and agent health
            </p>
          </div>
        </section>
      </div>
    </div>
  )
}
