"""Real-time agent health & traffic monitor (in-memory).

Powers the console's live "Mission Control" board:

* a background loop periodically probes each registered agent's upstream and
  records status + latency + a rolling uptime percentage and latency sparkline;
* the agent-edge proxy path calls :meth:`AgentMonitor.record_request` on every
  proxied request so we can show live req/min and error rate per agent.

Everything is bounded in memory (deques with maxlen / time windows) so it is
safe to run continuously without growth. State is intentionally per-process —
it is telemetry, not the source of truth — which is exactly what a live board
wants.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

import structlog

logger = structlog.get_logger()

# Rolling windows.
_TRAFFIC_WINDOW_S = 300          # keep request events for 5 minutes
_RATE_WINDOW_S = 60              # req/min + error% computed over the last minute
_SPARK_POINTS = 40              # latency history points per agent
_MAX_EVENTS = 5000              # hard cap on stored events per agent


class AgentMonitor:
    """Collects live health + traffic telemetry per agent slug."""

    def __init__(self) -> None:
        # slug -> health dict
        self._health: Dict[str, Dict[str, Any]] = {}
        # slug -> deque[latency_ms] (probe history for sparkline)
        self._spark: Dict[str, Deque[float]] = {}
        # slug -> deque[(ts, ok, latency_ms)] request events
        self._events: Dict[str, Deque[Tuple[float, bool, float]]] = {}
        self._probe_health_path = "/"
        self._started_at = time.time()

    # ------------------------------------------------------------------ traffic
    def record_request(self, slug: str, status_code: int, latency_ms: float) -> None:
        """Record one proxied request (called from the agent-edge proxy)."""
        if not slug:
            return
        ok = status_code < 500
        dq = self._events.setdefault(slug, deque(maxlen=_MAX_EVENTS))
        dq.append((time.time(), ok, float(latency_ms)))

    def _traffic_stats(self, slug: str) -> Dict[str, Any]:
        dq = self._events.get(slug)
        if not dq:
            return {"req_per_min": 0, "error_pct": 0.0, "total_requests": 0, "avg_latency_ms": None}
        now = time.time()
        # Drop events older than the traffic window (cheap amortised cleanup).
        while dq and now - dq[0][0] > _TRAFFIC_WINDOW_S:
            dq.popleft()
        recent = [e for e in dq if now - e[0] <= _RATE_WINDOW_S]
        total_recent = len(recent)
        errors = sum(1 for _, ok, _ in recent if not ok)
        lat = [lm for _, _, lm in recent if lm is not None]
        return {
            "req_per_min": total_recent,
            "error_pct": round((errors / total_recent) * 100, 1) if total_recent else 0.0,
            "total_requests": len(dq),
            "avg_latency_ms": round(sum(lat) / len(lat), 1) if lat else None,
        }

    # ------------------------------------------------------------------- health
    async def probe_once(self, config) -> None:
        """Probe every live agent upstream once and update health state."""
        try:
            import httpx
        except Exception:  # noqa: BLE001
            return

        routes: Dict[str, str] = config.snapshot().get("routes", {})  # type: ignore[assignment]
        if not routes:
            return

        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
            async def _probe(slug: str, base: str) -> None:
                target = base.rstrip("/") + self._probe_health_path
                started = time.perf_counter()
                prev = self._health.get(slug, {})
                checks = int(prev.get("checks", 0)) + 1
                checks_ok = int(prev.get("checks_ok", 0))
                try:
                    resp = await client.get(target)
                    latency = round((time.perf_counter() - started) * 1000, 1)
                    up = resp.status_code < 500
                    status_code: Optional[int] = resp.status_code
                    error = None
                except Exception as exc:  # noqa: BLE001
                    latency = round((time.perf_counter() - started) * 1000, 1)
                    up = False
                    status_code = None
                    error = str(exc)[:200]

                if up:
                    checks_ok += 1
                    self._spark.setdefault(slug, deque(maxlen=_SPARK_POINTS)).append(latency)

                consecutive = 0 if up else int(prev.get("consecutive_failures", 0)) + 1
                prev_status = prev.get("status")
                new_status = "up" if up else "down"
                if prev_status and prev_status != new_status:
                    logger.info(
                        "agent_health_changed",
                        slug=slug, from_status=prev_status, to_status=new_status,
                    )
                self._health[slug] = {
                    "status": new_status,
                    "latency_ms": latency if up else None,
                    "status_code": status_code,
                    "error": error,
                    "last_checked": time.time(),
                    "checks": checks,
                    "checks_ok": checks_ok,
                    "consecutive_failures": consecutive,
                }

            await asyncio.gather(*(_probe(s, b) for s, b in routes.items()))

    async def run(self, config, interval_seconds: int = 10) -> None:
        """Continuously probe agents. Cancels cleanly on shutdown."""
        # Prime immediately so the board is populated on first load.
        try:
            await self.probe_once(config)
        except Exception as exc:  # noqa: BLE001
            logger.debug("agent_monitor_prime_failed", error=str(exc))
        while True:
            try:
                await asyncio.sleep(interval_seconds)
                await self.probe_once(config)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.debug("agent_monitor_probe_failed", error=str(exc))

    # ----------------------------------------------------------------- snapshot
    def snapshot(self, config) -> Dict[str, Any]:
        """Merge health + traffic for every live agent into a board payload."""
        routes: Dict[str, str] = config.snapshot().get("routes", {})  # type: ignore[assignment]
        slugs = sorted(set(routes) | set(self._events))
        agents: List[Dict[str, Any]] = []
        up = down = 0
        for slug in slugs:
            h = self._health.get(slug, {})
            t = self._traffic_stats(slug)
            status = h.get("status", "unknown")
            if status == "up":
                up += 1
            elif status == "down":
                down += 1
            checks = int(h.get("checks", 0))
            checks_ok = int(h.get("checks_ok", 0))
            agents.append(
                {
                    "slug": slug,
                    "upstream_base": routes.get(slug),
                    "status": status,
                    "latency_ms": h.get("latency_ms"),
                    "status_code": h.get("status_code"),
                    "error": h.get("error"),
                    "last_checked": h.get("last_checked"),
                    "consecutive_failures": h.get("consecutive_failures", 0),
                    "uptime_pct": round((checks_ok / checks) * 100, 1) if checks else None,
                    "sparkline": list(self._spark.get(slug, [])),
                    **t,
                }
            )
        total_rpm = sum(a["req_per_min"] for a in agents)
        return {
            "generated_at": time.time(),
            "summary": {
                "total": len(agents),
                "up": up,
                "down": down,
                "unknown": len(agents) - up - down,
                "req_per_min": total_rpm,
            },
            "agents": agents,
        }


# Process-global singleton.
agent_monitor = AgentMonitor()
