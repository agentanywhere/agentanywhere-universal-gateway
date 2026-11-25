import { useEffect, useMemo, useState } from 'react'
import { useMediaQuery } from '../hooks/useMediaQuery'

/**
 * The sign-in page's hero: agent protocols around the gateway, with requests
 * flowing in (cyan) and answers flowing back out (violet). Pure SVG + SMIL, so
 * it needs no canvas or animation library. With prefers-reduced-motion the
 * scene is drawn still.
 */

const PROTOCOLS = ['MCP', 'A2A', 'AG-UI', 'ACP', 'HTTP', 'GraphQL', 'MQTT', 'WebSocket', 'gRPC']

const SIZE = 800
const C = SIZE / 2
const ORBIT = 296
// Below lg the hero is drawn small, so labels are scaled up and the orbit
// pulled in to keep them readable and inside the frame.
const COMPACT_LABEL_SCALE = 1.7
const COMPACT_ORBIT = 268
const CORE = 62

function usePrefersReducedMotion(): boolean {
  const query = '(prefers-reduced-motion: reduce)'
  const [reduced, setReduced] = useState(
    () => typeof window !== 'undefined' && !!window.matchMedia?.(query).matches
  )
  useEffect(() => {
    const mql = window.matchMedia?.(query)
    if (!mql?.addEventListener) return
    const onChange = (e: MediaQueryListEvent) => setReduced(e.matches)
    mql.addEventListener('change', onChange)
    return () => mql.removeEventListener('change', onChange)
  }, [])
  return reduced
}

interface Node {
  name: string
  x: number
  y: number
  inbound: string
  outbound: string
  delay: number
  dur: number
}

function buildNodes(orbit: number, inset: number): Node[] {
  return PROTOCOLS.map((name, i) => {
    const a = (i / PROTOCOLS.length) * Math.PI * 2 - Math.PI / 2
    const x = C + orbit * Math.cos(a)
    const y = C + orbit * Math.sin(a)
    // Edge points: just inside the pill, and on the core's rim.
    const ex = C + (orbit - inset) * Math.cos(a)
    const ey = C + (orbit - inset) * Math.sin(a)
    const cx = C + (CORE + 6) * Math.cos(a)
    const cy = C + (CORE + 6) * Math.sin(a)
    // Bend each lane a little, alternately, so the flows read as streams.
    const bend = (i % 2 ? 1 : -1) * 46
    const mx = (ex + cx) / 2 + bend * Math.cos(a + Math.PI / 2)
    const my = (ey + cy) / 2 + bend * Math.sin(a + Math.PI / 2)
    const f = (n: number) => n.toFixed(1)
    return {
      name,
      x,
      y,
      inbound: `M${f(ex)} ${f(ey)} Q${f(mx)} ${f(my)} ${f(cx)} ${f(cy)}`,
      outbound: `M${f(cx)} ${f(cy)} Q${f(mx)} ${f(my)} ${f(ex)} ${f(ey)}`,
      delay: (i * 0.53) % 2.6,
      dur: 2.6 + (i % 3) * 0.55,
    }
  })
}

function hexagon(r: number): string {
  return Array.from({ length: 6 }, (_, k) => {
    const a = (Math.PI / 3) * k - Math.PI / 2
    return `${(C + r * Math.cos(a)).toFixed(1)},${(C + r * Math.sin(a)).toFixed(1)}`
  }).join(' ')
}

export default function GatewayHero({ className }: { className?: string }) {
  const reduced = usePrefersReducedMotion()
  const compact = useMediaQuery('(max-width: 1023px)')
  const k = compact ? COMPACT_LABEL_SCALE : 1
  const orbit = compact ? COMPACT_ORBIT : ORBIT
  const NODES = useMemo(() => buildNodes(orbit, 40 * k), [orbit, k])

  return (
    <svg
      viewBox={`0 0 ${SIZE} ${SIZE}`}
      className={className}
      role="img"
      aria-label="Agent protocols flowing through the AgentAnywhere Universal Gateway"
      data-testid="gateway-hero"
    >
      <defs>
        <linearGradient id="gh-sig" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#2CC7FE" />
          <stop offset="0.55" stopColor="#2D63F6" />
          <stop offset="1" stopColor="#6729F4" />
        </linearGradient>
        <radialGradient id="gh-core" cx="50%" cy="45%" r="60%">
          <stop offset="0" stopColor="#1C3350" />
          <stop offset="1" stopColor="#0A0E14" />
        </radialGradient>
        <radialGradient id="gh-halo" cx="50%" cy="50%" r="50%">
          <stop offset="0" stopColor="#2D63F6" stopOpacity="0.45" />
          <stop offset="0.45" stopColor="#6729F4" stopOpacity="0.16" />
          <stop offset="1" stopColor="#6729F4" stopOpacity="0" />
        </radialGradient>
        <radialGradient id="gh-fade" cx="50%" cy="50%" r="50%">
          <stop offset="0.55" stopColor="#fff" stopOpacity="1" />
          <stop offset="1" stopColor="#fff" stopOpacity="0" />
        </radialGradient>
        <mask id="gh-vignette">
          <rect width={SIZE} height={SIZE} fill="url(#gh-fade)" />
        </mask>
        <pattern id="gh-grid" width="40" height="40" patternUnits="userSpaceOnUse">
          <path d="M40 0H0V40" fill="none" stroke="#26344A" strokeWidth="1" />
        </pattern>
        <filter id="gh-glow" x="-50%" y="-50%" width="200%" height="200%">
          <feGaussianBlur stdDeviation="4" result="b" />
          <feMerge>
            <feMergeNode in="b" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
        {NODES.map((n, i) => (
          <path key={i} id={`gh-in-${i}`} d={n.inbound} />
        ))}
        {NODES.map((n, i) => (
          <path key={i} id={`gh-out-${i}`} d={n.outbound} />
        ))}
      </defs>

      {/* Ground: a fading grid and the gateway's halo. */}
      <rect width={SIZE} height={SIZE} fill="url(#gh-grid)" opacity="0.45" mask="url(#gh-vignette)" />
      <circle cx={C} cy={C} r={260} fill="url(#gh-halo)" />

      {/* Orbit and slow rings. */}
      <circle cx={C} cy={C} r={orbit} fill="none" stroke="#26344A" strokeDasharray="2 10" strokeLinecap="round" />
      <g className={reduced ? undefined : 'animate-spin-slow'} style={{ transformOrigin: `${C}px ${C}px` }}>
        <circle cx={C} cy={C} r={168} fill="none" stroke="url(#gh-sig)" strokeOpacity="0.55" strokeDasharray="1 14" strokeWidth="2" strokeLinecap="round" />
        <circle cx={C} cy={C - 168} r="4" fill="#2CC7FE" filter="url(#gh-glow)" />
      </g>
      <g className={reduced ? undefined : 'animate-spin-slower'} style={{ transformOrigin: `${C}px ${C}px` }}>
        <circle cx={C} cy={C} r={112} fill="none" stroke="#26344A" strokeWidth="1" strokeDasharray="60 18 4 18" />
        <circle cx={C + 112} cy={C} r="3" fill="#6729F4" filter="url(#gh-glow)" />
      </g>

      {/* Lanes. */}
      {NODES.map((n, i) => (
        <use key={i} href={`#gh-in-${i}`} fill="none" stroke="url(#gh-sig)" strokeOpacity="0.32" strokeWidth="1.6" />
      ))}

      {/* Traffic: requests in, answers out. */}
      {!reduced &&
        NODES.map((n, i) => (
          <g key={i} filter="url(#gh-glow)">
            <circle r="4.6" fill="#2CC7FE" opacity="0">
              <animateMotion dur={`${n.dur}s`} begin={`${n.delay}s`} repeatCount="indefinite" calcMode="spline" keyPoints="0;1" keyTimes="0;1" keySplines="0.45 0 0.55 1">
                <mpath href={`#gh-in-${i}`} />
              </animateMotion>
              <animate attributeName="opacity" values="0;1;1;0" keyTimes="0;0.12;0.85;1" dur={`${n.dur}s`} begin={`${n.delay}s`} repeatCount="indefinite" />
            </circle>
            <circle r="3.6" fill="#8B6BFF" opacity="0">
              <animateMotion dur={`${n.dur + 0.8}s`} begin={`${n.delay + n.dur / 2}s`} repeatCount="indefinite" calcMode="spline" keyPoints="0;1" keyTimes="0;1" keySplines="0.45 0 0.55 1">
                <mpath href={`#gh-out-${i}`} />
              </animateMotion>
              <animate attributeName="opacity" values="0;1;1;0" keyTimes="0;0.12;0.85;1" dur={`${n.dur + 0.8}s`} begin={`${n.delay + n.dur / 2}s`} repeatCount="indefinite" />
            </circle>
          </g>
        ))}

      {/* The gateway core. */}
      <g>
        <circle
          cx={C}
          cy={C}
          r={CORE + 26}
          fill="none"
          stroke="url(#gh-sig)"
          strokeOpacity="0.35"
          strokeWidth="1"
          className={reduced ? undefined : 'animate-core-pulse'}
          style={{ transformOrigin: `${C}px ${C}px` }}
        />
        <polygon points={hexagon(CORE)} fill="url(#gh-core)" stroke="url(#gh-sig)" strokeWidth="2" strokeLinejoin="round" />
        <polygon points={hexagon(CORE - 14)} fill="none" stroke="#26344A" strokeWidth="1" strokeLinejoin="round" />
        {/* Gateway glyph: two streams converging through one gate. */}
        <g fill="none" stroke="url(#gh-sig)" strokeWidth="5" strokeLinecap="round" strokeLinejoin="round" filter="url(#gh-glow)">
          <path d={`M${C - 22} ${C - 18} L${C - 4} ${C} L${C - 22} ${C + 18}`} />
          <path d={`M${C + 4} ${C - 18} L${C + 22} ${C} L${C + 4} ${C + 18}`} />
        </g>
      </g>

      {/* Protocol pills. */}
      {NODES.map((n) => {
        const w = Math.max(70, n.name.length * 11.5 + 38) * k
        return (
          <g key={n.name} transform={`translate(${n.x.toFixed(1)} ${n.y.toFixed(1)})`}>
            <rect x={-w / 2} y={-21 * k} width={w} height={42 * k} rx={21 * k} fill="#14202E" stroke="#26344A" strokeWidth={k} />
            <circle cx={-w / 2 + 17 * k} cy={0} r={3.5 * k} fill="#10B981">
              {!reduced && (
                <animate attributeName="opacity" values="1;0.35;1" dur="2.4s" begin={`${n.delay}s`} repeatCount="indefinite" />
              )}
            </circle>
            <text
              x={8 * k}
              y={1}
              textAnchor="middle"
              dominantBaseline="middle"
              fill="#F4F1EA"
              fontSize={19 * k}
              fontFamily="Consolas, ui-monospace, SFMono-Regular, Menlo, monospace"
              letterSpacing="0.04em"
            >
              {n.name}
            </text>
          </g>
        )
      })}
    </svg>
  )
}
