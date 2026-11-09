import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fakeJwt } from './test/jwt'

// ida.ts caches its config at module level, so re-import fresh each test.
async function load() {
  vi.resetModules()
  return import('./ida')
}

function mockFetch(impl: (url: string, init?: RequestInit) => Promise<Partial<Response>>) {
  const f = vi.fn(impl as any)
  vi.stubGlobal('fetch', f)
  return f
}

describe('parseIdaProfile', () => {
  it('returns null for missing or malformed tokens', async () => {
    const { parseIdaProfile } = await load()
    expect(parseIdaProfile(null)).toBeNull()
    expect(parseIdaProfile('nodots')).toBeNull()
    expect(parseIdaProfile('a.@@@.c')).toBeNull()
  })

  it('extracts name and email, with fallbacks', async () => {
    const { parseIdaProfile } = await load()
    expect(parseIdaProfile(fakeJwt({ name: 'Ada', email: 'ada@x.io' }))).toEqual({ name: 'Ada', email: 'ada@x.io' })
    expect(parseIdaProfile(fakeJwt({ preferred_username: 'ada' }))).toEqual({ name: 'ada', email: 'ada' })
    expect(parseIdaProfile(fakeJwt({ email: 'e@x.io' }))).toEqual({ name: 'e@x.io', email: 'e@x.io' })
    expect(parseIdaProfile(fakeJwt({}))).toEqual({ name: 'Operator', email: '' })
  })
})

describe('ida config + session', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('idaConfig falls back to disabled on network failure and caches', async () => {
    const f = mockFetch(async () => {
      throw new Error('down')
    })
    const { idaConfig, idaEnabled } = await load()
    expect((await idaConfig()).enabled).toBe(false)
    expect(await idaEnabled()).toBe(false)
    expect(f).toHaveBeenCalledTimes(1)
  })

  it('idaConfig falls back to disabled on non-ok response', async () => {
    mockFetch(async () => ({ ok: false }))
    const { idaEnabled } = await load()
    expect(await idaEnabled()).toBe(false)
  })

  it('idaConfig returns the server config when ok', async () => {
    mockFetch(async () => ({ ok: true, json: async () => ({ enabled: true, authority: 'https://sso.test/realm', clientId: 'cid' }) }))
    const { idaConfig } = await load()
    expect(await idaConfig()).toEqual({ enabled: true, authority: 'https://sso.test/realm', clientId: 'cid' })
  })

  it('isIdaSession reflects auth_mode', async () => {
    const { isIdaSession } = await load()
    expect(isIdaSession()).toBe(false)
    localStorage.setItem('auth_mode', 'ida')
    expect(isIdaSession()).toBe(true)
  })

  it('idaLogin does nothing when SSO is disabled', async () => {
    mockFetch(async () => ({ ok: true, json: async () => ({ enabled: false, authority: '', clientId: '' }) }))
    const assign = vi.fn()
    vi.stubGlobal('location', { ...window.location, assign, origin: 'http://localhost' })
    const { idaLogin } = await load()
    await idaLogin()
    expect(assign).not.toHaveBeenCalled()
  })

  it('idaLogin redirects to the authority with a PKCE challenge', async () => {
    mockFetch(async () => ({ ok: true, json: async () => ({ enabled: true, authority: 'https://sso.test/realm', clientId: 'cid' }) }))
    const assign = vi.fn()
    vi.stubGlobal('location', { ...window.location, assign, origin: 'http://localhost' })
    const { idaLogin } = await load()
    await idaLogin()
    expect(assign).toHaveBeenCalledTimes(1)
    const url = new URL(assign.mock.calls[0][0])
    expect(url.origin + url.pathname).toBe('https://sso.test/realm/protocol/openid-connect/auth')
    expect(url.searchParams.get('client_id')).toBe('cid')
    expect(url.searchParams.get('code_challenge_method')).toBe('S256')
    expect(url.searchParams.get('code_challenge')).toBeTruthy()
    expect(sessionStorage.getItem('ida_verifier')).toBeTruthy()
  })

  it('idaHandleCallback returns false without a code', async () => {
    const { idaHandleCallback } = await load()
    expect(await idaHandleCallback()).toBe(false)
  })

  it('idaHandleCallback exchanges the code and stores tokens', async () => {
    window.history.replaceState({}, '', '/?code=abc')
    const f = mockFetch(async (url) => {
      if (url.endsWith('/auth/config'))
        return { ok: true, json: async () => ({ enabled: true, authority: 'https://sso.test/realm', clientId: 'cid' }) }
      return { ok: true, json: async () => ({ access_token: 'AT', refresh_token: 'RT', id_token: 'IT' }) }
    })
    sessionStorage.setItem('ida_verifier', 'ver')
    const { idaHandleCallback } = await load()
    expect(await idaHandleCallback()).toBe(true)
    expect(localStorage.getItem('admin_token')).toBe('AT')
    expect(localStorage.getItem('ida_refresh_token')).toBe('RT')
    expect(localStorage.getItem('ida_id_token')).toBe('IT')
    expect(localStorage.getItem('auth_mode')).toBe('ida')
    expect(sessionStorage.getItem('ida_verifier')).toBeNull()
    const tokenCall = f.mock.calls.find((c) => String(c[0]).endsWith('/token'))!
    expect(String((tokenCall[1] as RequestInit).body)).toContain('code_verifier=ver')
    window.history.replaceState({}, '', '/')
  })

  it('idaHandleCallback returns false when the exchange fails or lacks a token', async () => {
    window.history.replaceState({}, '', '/?code=abc')
    let ok = false
    mockFetch(async (url) => {
      if (url.endsWith('/auth/config'))
        return { ok: true, json: async () => ({ enabled: true, authority: 'https://sso.test/realm', clientId: 'cid' }) }
      return ok ? { ok: true, json: async () => ({}) } : { ok: false }
    })
    const { idaHandleCallback } = await load()
    expect(await idaHandleCallback()).toBe(false)
    window.history.replaceState({}, '', '/?code=abc')
    ok = true
    expect(await idaHandleCallback()).toBe(false)
    expect(localStorage.getItem('admin_token')).toBeNull()
    window.history.replaceState({}, '', '/')
  })

  it('idaHandleCallback is a no-op when SSO is disabled', async () => {
    window.history.replaceState({}, '', '/?code=abc')
    mockFetch(async () => ({ ok: true, json: async () => ({ enabled: false, authority: '', clientId: '' }) }))
    const { idaHandleCallback } = await load()
    expect(await idaHandleCallback()).toBe(false)
    window.history.replaceState({}, '', '/')
  })

  it('idaLogout clears stored tokens and redirects to the end-session endpoint', async () => {
    mockFetch(async () => ({ ok: true, json: async () => ({ enabled: true, authority: 'https://sso.test/realm', clientId: 'cid' }) }))
    const assign = vi.fn()
    vi.stubGlobal('location', { ...window.location, assign, origin: 'http://localhost' })
    localStorage.setItem('admin_token', 'AT')
    localStorage.setItem('ida_id_token', 'IT')
    localStorage.setItem('auth_mode', 'ida')
    const { idaLogout } = await load()
    await idaLogout()
    expect(localStorage.getItem('admin_token')).toBeNull()
    expect(localStorage.getItem('auth_mode')).toBeNull()
    const url = new URL(assign.mock.calls[0][0])
    expect(url.pathname).toBe('/realm/protocol/openid-connect/logout')
    expect(url.searchParams.get('id_token_hint')).toBe('IT')
  })

  it('idaLogout skips the redirect when there is no id token', async () => {
    mockFetch(async () => ({ ok: true, json: async () => ({ enabled: true, authority: 'https://sso.test/realm', clientId: 'cid' }) }))
    const assign = vi.fn()
    vi.stubGlobal('location', { ...window.location, assign })
    const { idaLogout } = await load()
    await idaLogout()
    expect(assign).not.toHaveBeenCalled()
  })
})
