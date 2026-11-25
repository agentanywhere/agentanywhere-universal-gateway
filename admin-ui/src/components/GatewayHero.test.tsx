import { afterEach, describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import GatewayHero from './GatewayHero'

const original = window.matchMedia

function setReducedMotion(reduce: boolean) {
  window.matchMedia = ((q: string) => ({
    matches: reduce && q.includes('reduced-motion'),
    media: q,
    onchange: null,
    addEventListener() {},
    removeEventListener() {},
    addListener() {},
    removeListener() {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

afterEach(() => {
  window.matchMedia = original
})

describe('GatewayHero', () => {
  it('draws every protocol around the gateway, with an accessible label', () => {
    setReducedMotion(false)
    render(<GatewayHero />)
    const svg = screen.getByRole('img', { name: /AgentAnywhere Universal Gateway/ })
    for (const name of ['MCP', 'A2A', 'AG-UI', 'ACP', 'HTTP', 'GraphQL', 'MQTT', 'WebSocket', 'gRPC']) {
      expect(screen.getByText(name)).toBeInTheDocument()
    }
    // Requests in and answers out: two moving packets per protocol.
    expect(svg.querySelectorAll('animateMotion')).toHaveLength(18)
  })

  it('draws a still scene when the viewer prefers reduced motion', () => {
    setReducedMotion(true)
    render(<GatewayHero />)
    const svg = screen.getByTestId('gateway-hero')
    expect(svg.querySelectorAll('animateMotion')).toHaveLength(0)
    expect(svg.querySelectorAll('animate')).toHaveLength(0)
    expect(svg.querySelector('.animate-spin-slow')).toBeNull()
  })
})
