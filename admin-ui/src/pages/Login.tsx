import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowRight, Lock, Shield, User, Zap } from 'lucide-react'
import GatewayHero from '../components/GatewayHero'
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
      <div className="grid min-h-screen grid-cols-1 lg:grid-cols-[1.25fr_1fr]">
        {/* The hero is always Sovereign Dark, whatever the console theme. */}
        <section className="dark relative isolate flex flex-col overflow-hidden border-b border-[#26344A] bg-[#0A0E14] px-6 pb-8 pt-8 text-foreground sm:px-10 lg:min-h-screen lg:border-b-0 lg:border-r lg:px-14 lg:py-10">
          <div
            aria-hidden="true"
            className="pointer-events-none absolute inset-0 -z-10"
            style={{
              backgroundImage:
                'radial-gradient(60rem 36rem at 70% 40%, rgba(45,99,246,0.16), transparent 60%), radial-gradient(40rem 30rem at 0% 100%, rgba(103,41,244,0.14), transparent 60%), radial-gradient(36rem 24rem at 10% 0%, rgba(44,199,254,0.10), transparent 60%)',
            }}
          />

          <div className="flex items-center gap-3 animate-fade-in">
            <div className="brand-mark shadow-glow">
              <Zap className="relative z-10 h-4 w-4 text-white" strokeWidth={2.4} />
            </div>
            <div className="leading-tight">
              <div className="font-display text-[17px] font-semibold text-foreground">AgentAnywhere</div>
              <div className="font-mono text-[10px] uppercase tracking-[0.22em] text-muted-foreground">
                Universal Gateway
              </div>
            </div>
          </div>

          <div className="relative mx-auto my-2 flex w-full max-w-[44rem] flex-1 items-center justify-center animate-scale-in">
            <GatewayHero className="h-auto max-h-[30vh] w-full sm:max-h-[44vh] lg:max-h-[58vh]" />
          </div>

          <div className="max-w-xl space-y-4 animate-rise">
            <p className="eyebrow hidden sm:block">Admin console</p>
            <h1 className="font-display text-2xl font-semibold leading-[1.1] text-foreground sm:text-5xl sm:leading-[1.05]">
              Every agent protocol.{' '}
              <span className="text-signature">One gateway.</span>
            </h1>
            <p className="hidden max-w-lg text-base leading-relaxed text-sovereign-body sm:block sm:text-lg">
              Authenticate, rate-limit and route MCP, A2A, AG-UI, ACP, HTTP, GraphQL and MQTT
              traffic, and watch it live from here.
            </p>
          </div>
        </section>

        <section className="relative flex items-center justify-center px-5 py-8 sm:px-10 sm:py-12 lg:px-12">
          <div className="w-full max-w-md animate-slide-up">
            <div className="relative rounded-2xl bg-gradient-to-br from-brand-cyan/50 via-brand-blue/30 to-brand-violet/50 p-px shadow-panel">
            <div className="rounded-[15px] bg-card p-6 sm:p-9">
              <div className="mb-7 space-y-2">
                <p className="eyebrow">Secure access</p>
                <h2 className="font-display text-3xl font-semibold text-foreground">
                  Sign in
                </h2>
                <p className="text-sm text-muted-foreground">
                  Operators only. Every sign-in is checked by the gateway.
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
                  {!showNative && error && (
                    <div
                      role="alert"
                      className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive"
                    >
                      {error}
                    </div>
                  )}
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
                      placeholder="ADMIN_TOKEN"
                    />
                    <p className="text-xs leading-relaxed text-muted-foreground">
                      There is no default password. Use the gateway&apos;s{' '}
                      <code className="font-mono text-foreground/80">ADMIN_TOKEN</code>.{' '}
                      <code className="font-mono text-foreground/80">SECRET_KEY</code> works only
                      while no admin token is set, and never as the example placeholder.
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
            </div>

            <p className="mt-6 text-center font-mono text-[11px] uppercase tracking-[0.18em] text-muted-foreground">
              Apache-2.0 · open source
            </p>
          </div>
        </section>
      </div>
    </div>
  )
}
