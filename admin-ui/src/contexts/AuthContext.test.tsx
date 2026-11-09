import axios from 'axios'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { AuthProvider, useAuth } from './AuthContext'
import { fakeJwt } from '../test/jwt'

vi.mock('../ida', async (orig) => {
  const actual = await orig<typeof import('../ida')>()
  return { ...actual, idaLogout: vi.fn(), isIdaSession: vi.fn(() => false) }
})
import * as ida from '../ida'

const wrapper = AuthProvider

describe('AuthContext', () => {
  let hrefSet: string | undefined
  beforeEach(() => {
    hrefSet = undefined
    delete axios.defaults.headers.common['Authorization']
    vi.stubGlobal('location', {
      ...window.location,
      set href(v: string) {
        hrefSet = v
      },
      get href() {
        return 'http://localhost/'
      },
    })
    vi.mocked(ida.isIdaSession).mockReturnValue(false)
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('throws when used outside a provider', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    expect(() => renderHook(() => useAuth())).toThrow(/AuthProvider/)
    spy.mockRestore()
  })

  it('starts unauthenticated with no stored token', () => {
    const { result } = renderHook(() => useAuth(), { wrapper })
    expect(result.current.isAuthenticated).toBe(false)
    expect(result.current.token).toBeNull()
    expect(result.current.user).toBeNull()
    expect(axios.defaults.headers.common['Authorization']).toBeUndefined()
  })

  it('restores a persisted token, user profile and axios header', () => {
    const t = fakeJwt({ name: 'Ada', email: 'ada@x.io' })
    localStorage.setItem('admin_token', t)
    const { result } = renderHook(() => useAuth(), { wrapper })
    expect(result.current.isAuthenticated).toBe(true)
    expect(result.current.token).toBe(t)
    expect(result.current.user).toEqual({ name: 'Ada', email: 'ada@x.io' })
    expect(axios.defaults.headers.common['Authorization']).toBe(`Bearer ${t}`)
  })

  it('login stores the token, sets the header and user', () => {
    const { result } = renderHook(() => useAuth(), { wrapper })
    const t = fakeJwt({ preferred_username: 'bob' })
    act(() => result.current.login(t))
    expect(result.current.isAuthenticated).toBe(true)
    expect(localStorage.getItem('admin_token')).toBe(t)
    expect(axios.defaults.headers.common['Authorization']).toBe(`Bearer ${t}`)
    expect(result.current.user?.name).toBe('bob')
  })

  it('login with an opaque (non-JWT) token authenticates without a user profile', () => {
    const { result } = renderHook(() => useAuth(), { wrapper })
    act(() => result.current.login('opaque-admin-token'))
    expect(result.current.isAuthenticated).toBe(true)
    expect(result.current.user).toBeNull()
  })

  it('logout clears state, storage, header and redirects to login', () => {
    localStorage.setItem('admin_token', 'tok')
    localStorage.setItem('auth_mode', 'local')
    const { result } = renderHook(() => useAuth(), { wrapper })
    act(() => result.current.logout())
    expect(result.current.isAuthenticated).toBe(false)
    expect(localStorage.getItem('admin_token')).toBeNull()
    expect(localStorage.getItem('auth_mode')).toBeNull()
    expect(axios.defaults.headers.common['Authorization']).toBeUndefined()
    expect(hrefSet).toBe('/login')
  })

  it('logout delegates to idaLogout for SSO sessions and does not redirect itself', () => {
    vi.mocked(ida.isIdaSession).mockReturnValue(true)
    localStorage.setItem('admin_token', 'tok')
    const { result } = renderHook(() => useAuth(), { wrapper })
    act(() => result.current.logout())
    expect(ida.idaLogout).toHaveBeenCalledTimes(1)
    expect(result.current.isAuthenticated).toBe(false)
    expect(hrefSet).toBeUndefined()
  })
})
