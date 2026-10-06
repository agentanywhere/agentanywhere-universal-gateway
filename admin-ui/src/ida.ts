// OIDC SSO (Keycloak-compatible) SSO — OIDC Authorization Code + PKCE for the admin SPA.
// The IDA access token is stored as `admin_token` (same key the axios client reads).

const BASE_PATH = (import.meta.env.BASE_URL || '/').replace(/\/?$/, '/')
const ADMIN_BASE = `${BASE_PATH}admin`.replace(/\/+/g, '/').replace(':/', '://')

const TOKEN_KEY = 'admin_token'
const REFRESH_KEY = 'ida_refresh_token'
const ID_TOKEN_KEY = 'ida_id_token'
const MODE_KEY = 'auth_mode'
const VERIFIER_KEY = 'ida_verifier'
const USER_KEY = 'admin_user'

type IdaConfig = { enabled: boolean; authority: string; clientId: string }

const DISABLED: IdaConfig = { enabled: false, authority: '', clientId: '' }

let _config: IdaConfig | null = null

export async function idaConfig(): Promise<IdaConfig> {
  if (_config) return _config
  try {
    const res = await fetch(`${ADMIN_BASE}/auth/config`, { headers: { Accept: 'application/json' } })
    _config = (res.ok ? await res.json() : DISABLED) as IdaConfig
  } catch {
    _config = DISABLED
  }
  return _config ?? DISABLED
}

export async function idaEnabled() {
  return (await idaConfig()).enabled
}

function redirectUri() {
  return window.location.origin + BASE_PATH.replace(/\/$/, '')
}

function b64url(bytes: Uint8Array) {
  let s = ''
  for (const b of bytes) s += String.fromCharCode(b)
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

function randomString(len: number) {
  const a = new Uint8Array(len)
  crypto.getRandomValues(a)
  return b64url(a)
}

async function challenge(verifier: string) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))
  return b64url(new Uint8Array(digest))
}

export async function idaLogin() {
  const cfg = await idaConfig()
  if (!cfg.enabled) return
  const verifier = randomString(48)
  sessionStorage.setItem(VERIFIER_KEY, verifier)
  const url = new URL(`${cfg.authority}/protocol/openid-connect/auth`)
  url.search = new URLSearchParams({
    client_id: cfg.clientId,
    redirect_uri: redirectUri(),
    response_type: 'code',
    scope: 'openid profile email',
    code_challenge: await challenge(verifier),
    code_challenge_method: 'S256',
    state: randomString(8),
  }).toString()
  window.location.assign(url.toString())
}

async function exchange(cfg: IdaConfig, body: Record<string, string>) {
  const res = await fetch(`${cfg.authority}/protocol/openid-connect/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams(body).toString(),
  })
  if (!res.ok) return false
  const t = await res.json()
  if (!t.access_token) return false
  localStorage.setItem(TOKEN_KEY, t.access_token)
  if (t.refresh_token) localStorage.setItem(REFRESH_KEY, t.refresh_token)
  if (t.id_token) localStorage.setItem(ID_TOKEN_KEY, t.id_token)
  localStorage.setItem(MODE_KEY, 'ida')
  return true
}

export async function idaHandleCallback(): Promise<boolean> {
  const qp = new URLSearchParams(window.location.search)
  const code = qp.get('code')
  if (!code) return false
  const cfg = await idaConfig()
  if (!cfg.enabled) return false
  const verifier = sessionStorage.getItem(VERIFIER_KEY) || ''
  const ok = await exchange(cfg, {
    grant_type: 'authorization_code',
    code,
    redirect_uri: redirectUri(),
    client_id: cfg.clientId,
    code_verifier: verifier,
  })
  sessionStorage.removeItem(VERIFIER_KEY)
  window.history.replaceState({}, document.title, BASE_PATH)
  return ok
}

export function isIdaSession() {
  return localStorage.getItem(MODE_KEY) === 'ida'
}

export async function idaLogout() {
  const cfg = await idaConfig()
  const idToken = localStorage.getItem(ID_TOKEN_KEY)
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(REFRESH_KEY)
  localStorage.removeItem(ID_TOKEN_KEY)
  localStorage.removeItem(MODE_KEY)
  localStorage.removeItem(USER_KEY)
  if (cfg.enabled && idToken) {
    const url = new URL(`${cfg.authority}/protocol/openid-connect/logout`)
    url.searchParams.set('id_token_hint', idToken)
    url.searchParams.set('post_logout_redirect_uri', redirectUri())
    window.location.assign(url.toString())
  }
}

export function parseIdaProfile(token: string | null): { name: string; email: string } | null {
  if (!token) return null
  try {
    const payload = token.split('.')[1]
    if (!payload) return null
    const json = JSON.parse(atob(payload.replace(/-/g, '+').replace(/_/g, '/')))
    return {
      name: json.name || json.preferred_username || json.email || 'Operator',
      email: json.email || json.preferred_username || '',
    }
  } catch {
    return null
  }
}
