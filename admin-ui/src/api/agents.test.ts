import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('./client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))
import apiClient from './client'
import { agentsApi } from './agents'

const c = apiClient as unknown as Record<'get' | 'post' | 'put' | 'delete', ReturnType<typeof vi.fn>>

describe('agentsApi', () => {
  beforeEach(() => {
    for (const fn of Object.values(c)) fn.mockReset().mockResolvedValue({ data: { ok: 1 } })
  })

  it('list / healthSnapshot / export unwrap data', async () => {
    c.get.mockResolvedValueOnce({ data: [{ slug: 'a' }] })
    expect(await agentsApi.list()).toEqual([{ slug: 'a' }])
    expect(c.get).toHaveBeenLastCalledWith('/admin/agents')
    await agentsApi.healthSnapshot()
    expect(c.get).toHaveBeenLastCalledWith('/admin/agents/health')
    await agentsApi.export()
    expect(c.get).toHaveBeenLastCalledWith('/admin/agents/export')
  })

  it('create posts the input', async () => {
    const input = { slug: 'a', upstream_base: 'http://x' }
    await agentsApi.create(input)
    expect(c.post).toHaveBeenCalledWith('/admin/agents', input)
  })

  it('update and remove URL-encode the slug', async () => {
    await agentsApi.update('a/b c', { enabled: false })
    expect(c.put).toHaveBeenCalledWith('/admin/agents/a%2Fb%20c', { enabled: false })
    await agentsApi.remove('a/b c')
    expect(c.delete).toHaveBeenCalledWith('/admin/agents/a%2Fb%20c')
  })

  it('probe posts the path (default /)', async () => {
    await agentsApi.probe('x')
    expect(c.post).toHaveBeenCalledWith('/admin/agents/x/health', { path: '/' })
    await agentsApi.probe('x', '/ping')
    expect(c.post).toHaveBeenLastCalledWith('/admin/agents/x/health', { path: '/ping' })
  })

  it('reload posts', async () => {
    c.post.mockResolvedValue({ data: { reloaded: true, live_routes: 3 } })
    expect(await agentsApi.reload()).toEqual({ reloaded: true, live_routes: 3 })
    expect(c.post).toHaveBeenCalledWith('/admin/agents/reload')
  })

  it('streamUrl embeds the encoded admin token', () => {
    localStorage.setItem('admin_token', 'a b&c')
    expect(agentsApi.streamUrl()).toBe('/admin/agents/stream?token=a%20b%26c')
  })

  it('streamUrl uses an empty token when none is stored', () => {
    expect(agentsApi.streamUrl()).toBe('/admin/agents/stream?token=')
  })

  it('propagates errors', async () => {
    c.get.mockRejectedValue(new Error('nope'))
    await expect(agentsApi.list()).rejects.toThrow('nope')
  })
})
