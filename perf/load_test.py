"""A small closed-loop HTTP load generator.

``concurrency`` workers each send one request at a time, as fast as the
server answers, for ``duration`` seconds after an optional warm-up. Latency is
measured per request with a monotonic clock; the warm-up requests are
discarded. Use it against any running gateway::

    python -m perf.load_test --url http://127.0.0.1:8000 --path /api/users \\
        --api-key "$API_KEY" --concurrency 32 --duration 20

Exit status is 1 when ``--max-p95-ms`` or ``--max-error-rate`` is exceeded, so
it can gate a CI job.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Sequence

import httpx


@dataclass
class Result:
    name: str
    requests: int
    errors: int
    duration_s: float
    rps: float
    min_ms: float
    mean_ms: float
    p50_ms: float
    p90_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    status_counts: Dict[str, int] = field(default_factory=dict)

    @property
    def error_rate(self) -> float:
        return self.errors / self.requests if self.requests else 1.0


def percentile(sorted_values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile of an ascending list; 0.0 for an empty list."""
    if not sorted_values:
        return 0.0
    rank = max(1, math.ceil(round(pct / 100.0 * len(sorted_values), 9)))   # round: 99.9% of 1000 is 999, not 999.0000000000001
    return sorted_values[min(rank, len(sorted_values)) - 1]


def summarize(
    name: str,
    latencies_s: List[float],
    statuses: Dict[int, int],
    errors: int,
    duration_s: float,
    expect_status: Optional[int],
) -> Result:
    ms = sorted(v * 1000.0 for v in latencies_s)
    total = len(ms)
    wrong = sum(n for code, n in statuses.items() if _is_error(code, expect_status))
    return Result(
        name=name,
        requests=total,
        errors=errors + wrong,
        duration_s=round(duration_s, 3),
        rps=round(total / duration_s, 1) if duration_s > 0 else 0.0,
        min_ms=round(ms[0], 3) if ms else 0.0,
        mean_ms=round(sum(ms) / total, 3) if ms else 0.0,
        p50_ms=round(percentile(ms, 50), 3),
        p90_ms=round(percentile(ms, 90), 3),
        p95_ms=round(percentile(ms, 95), 3),
        p99_ms=round(percentile(ms, 99), 3),
        max_ms=round(ms[-1], 3) if ms else 0.0,
        status_counts={str(k): v for k, v in sorted(statuses.items())},
    )


def _is_error(status: int, expect_status: Optional[int]) -> bool:
    return status != expect_status if expect_status is not None else status >= 400


async def run_load(
    *,
    name: str,
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[str] = None,
    concurrency: int = 16,
    duration: float = 10.0,
    warmup: float = 2.0,
    timeout: float = 10.0,
    expect_status: Optional[int] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> Result:
    """Drive ``url`` and return the measurements. ``expect_status`` makes any other
    status count as an error (use 401 to measure a deliberate rejection)."""
    latencies: List[float] = []
    statuses: Dict[int, int] = {}
    errors = 0
    send_headers = dict(headers or {})
    if body is not None:
        send_headers.setdefault("Content-Type", "application/json")

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(limits=limits, timeout=timeout, transport=transport) as client:
        start = time.monotonic()
        measure_from = start + warmup
        stop = measure_from + duration

        async def worker() -> None:
            nonlocal errors
            while True:
                began = time.monotonic()
                if began >= stop:
                    return
                try:
                    resp = await client.request(method, url, headers=send_headers, content=body)
                    code = resp.status_code
                except httpx.HTTPError:
                    code = None
                ended = time.monotonic()
                if began < measure_from:
                    continue
                if code is None:
                    errors += 1
                else:
                    statuses[code] = statuses.get(code, 0) + 1
                    latencies.append(ended - began)

        await asyncio.gather(*(worker() for _ in range(concurrency)))
    return summarize(name, latencies, statuses, errors, duration, expect_status)


def format_table(results: Sequence[Result]) -> str:
    head = f"{'scenario':<28}{'req':>9}{'err':>6}{'rps':>10}{'p50 ms':>9}{'p95 ms':>9}{'p99 ms':>9}{'max ms':>9}"
    rows = [head, "-" * len(head)]
    for r in results:
        rows.append(
            f"{r.name:<28}{r.requests:>9}{r.errors:>6}{r.rps:>10.1f}"
            f"{r.p50_ms:>9.2f}{r.p95_ms:>9.2f}{r.p99_ms:>9.2f}{r.max_ms:>9.2f}"
        )
    return "\n".join(rows)


def check_thresholds(
    results: Sequence[Result], max_p95_ms: Optional[float], max_error_rate: Optional[float]
) -> List[str]:
    failures: List[str] = []
    for r in results:
        if max_p95_ms is not None and r.p95_ms > max_p95_ms:
            failures.append(f"{r.name}: p95 {r.p95_ms:.2f} ms is over {max_p95_ms} ms")
        if max_error_rate is not None and r.error_rate > max_error_rate:
            failures.append(f"{r.name}: error rate {r.error_rate:.2%} is over {max_error_rate:.2%}")
    return failures


def _parse_headers(items: Sequence[str]) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for item in items:
        key, sep, value = item.partition(":")
        if not sep or not key.strip():
            raise SystemExit(f"--header expects 'Name: value', got {item!r}")
        headers[key.strip()] = value.strip()
    return headers


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Closed-loop HTTP load test.")
    p.add_argument("--url", default="http://127.0.0.1:8000", help="base URL of the gateway")
    p.add_argument("--path", default="/health")
    p.add_argument("--method", default="GET")
    p.add_argument("--api-key", help="sent as X-API-Key")
    p.add_argument("--header", action="append", default=[], help="extra 'Name: value' header (repeatable)")
    p.add_argument("--body", help="request body, sent as application/json")
    p.add_argument("--concurrency", type=int, default=16)
    p.add_argument("--duration", type=float, default=10.0, help="measured seconds")
    p.add_argument("--warmup", type=float, default=2.0, help="seconds discarded before measuring")
    p.add_argument("--expect-status", type=int, help="count any other status as an error")
    p.add_argument("--max-p95-ms", type=float)
    p.add_argument("--max-error-rate", type=float, help="fraction, e.g. 0.01")
    p.add_argument("--json", dest="json_path", help="also write the result to this file")
    args = p.parse_args(argv)

    headers = _parse_headers(args.header)
    if args.api_key:
        headers["X-API-Key"] = args.api_key
    result = asyncio.run(
        run_load(
            name=f"{args.method} {args.path}",
            url=args.url.rstrip("/") + args.path,
            method=args.method.upper(),
            headers=headers,
            body=args.body,
            concurrency=args.concurrency,
            duration=args.duration,
            warmup=args.warmup,
            expect_status=args.expect_status,
        )
    )
    print(format_table([result]))
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(asdict(result), fh, indent=2)
    failures = check_thresholds([result], args.max_p95_ms, args.max_error_rate)
    for line in failures:
        print(f"FAIL {line}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
