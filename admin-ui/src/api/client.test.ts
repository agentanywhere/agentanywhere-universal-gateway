import { AxiosError, type AxiosAdapter, type InternalAxiosRequestConfig } from 'axios'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import apiClient from './client'

let seen: InternalAxiosRequestConfig | undefined
const original = apiClient.defaults.adapter

function useAdapter(status: number, data: unknown = { ok: true }) {
  const adapter: AxiosAdapter = async (config) => {
    seen = config
    const response = { data, status, statusText: String(status), headers: {}, config }
    if (status >= 400) {
      throw new AxiosError('fail', 'ERR_BAD_REQUEST', config, null, response as any)
    }
    return response as any
  }
  apiClient.defaults.adapter = adapter
}

describe('apiClient', () => {
  let hrefSet: string | undefined
  beforeEach(() => {
    seen = undefined
    hrefSet = undefined
    vi.stubGlobal('location', {
      pathname: '/dashboard',
      set href(v: string) {
        hrefSet = v
      },
      get href() {
        return 'http://localhost/dashboard'
      },
    })
  })
  afterEach(() => {
    apiClient.defaults.adapter = original
    vi.unstubAllGlobals()
  })

  it('is configured for JSON', () => {
    expect(apiClient.defaults.headers['Content-Type']).toBe('application/json')
  })

  it('adds a Bearer Authorization header when a token is stored', async () => {
    localStorage.setItem('admin_token', 'secret')
    useAdapter(200)
    await apiClient.get('/admin/routes')
    expect(seen?.headers.Authorization).toBe('Bearer secret')
  })

  it('sends no Authorization header without a token', async () => {
    useAdapter(200)
    await apiClient.get('/admin/routes')
    expect(seen?.headers.Authorization).toBeUndefined()
  })

  it('passes successful responses through', async () => {
    useAdapter(200, { hello: 'world' })
    const res = await apiClient.get('/x')
    expect(res.data).toEqual({ hello: 'world' })
  })

  it.each([401, 403])('on %i clears session storage and redirects to login', async (status) => {
    for (const k of ['admin_token', 'auth_mode', 'ida_refresh_token', 'ida_id_token', 'admin_user'])
      localStorage.setItem(k, 'v')
    useAdapter(status)
    await expect(apiClient.get('/admin/routes')).rejects.toMatchObject({ response: { status } })
    for (const k of ['admin_token', 'auth_mode', 'ida_refresh_token', 'ida_id_token', 'admin_user'])
      expect(localStorage.getItem(k)).toBeNull()
    expect(hrefSet).toBe('/login')
  })

  it('does not redirect when already on the login page', async () => {
    vi.stubGlobal('location', {
      pathname: '/console/login',
      set href(v: string) {
        hrefSet = v
      },
      get href() {
        return ''
      },
    })
    localStorage.setItem('admin_token', 'x')
    useAdapter(401)
    await expect(apiClient.get('/x')).rejects.toBeTruthy()
    expect(hrefSet).toBeUndefined()
    expect(localStorage.getItem('admin_token')).toBeNull()
  })

  it.each([400, 404, 500])('on %i rejects but keeps the session', async (status) => {
    localStorage.setItem('admin_token', 'keep')
    useAdapter(status)
    await expect(apiClient.get('/x')).rejects.toMatchObject({ response: { status } })
    expect(localStorage.getItem('admin_token')).toBe('keep')
    expect(hrefSet).toBeUndefined()
  })

  it('rejects network errors (no response) without redirecting', async () => {
    apiClient.defaults.adapter = async (config) => {
      throw new AxiosError('Network Error', 'ERR_NETWORK', config)
    }
    await expect(apiClient.get('/x')).rejects.toMatchObject({ code: 'ERR_NETWORK' })
    expect(hrefSet).toBeUndefined()
  })
})
