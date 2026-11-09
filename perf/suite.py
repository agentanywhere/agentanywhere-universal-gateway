"""Self-contained performance suite.

Starts the sample HTTP backend and a gateway on free local ports (own temporary
database, no Redis, quiet logs), creates an API key with limits high enough not
to interfere, runs the scenarios below, and stops everything::

    python -m perf.suite --duration 20 --concurrency 32 --report perf-report.md

Scenarios:

* ``backend direct``   the sample backend with no gateway: the baseline
* ``gateway /health``  the gateway with no auth and no backend call
* ``auth rejected``    a request without a key, answered 401
* ``proxy GET``        a keyed GET through the gateway to the backend
* ``proxy POST``       a keyed POST through the gateway to the backend

The report records the machine and settings, because a figure without them
cannot be compared or reproduced. Numbers from a laptop running the load
generator, the gateway and the backend together are indicative, not capacity.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

import httpx

from perf.load_test import Result, check_thresholds, format_table, run_load

REPO = Path(__file__).resolve().parent.parent
_BACKEND = "samples/backends/http/http_server.py"
_BACKEND_RUNNER = (
    "import importlib.util, sys, uvicorn;"
    "spec = importlib.util.spec_from_file_location('sample_http_backend', sys.argv[1]);"
    "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod);"
    "uvicorn.run(mod.app, host='127.0.0.1', port=int(sys.argv[2]), log_level='warning')"
)
USER_BODY = json.dumps({"name": "Ada", "email": "ada@example.com"})


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_healthy(url: str, proc: subprocess.Popen, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"process for {url} exited with status {proc.returncode}")
        try:
            if httpx.get(url, timeout=2.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise TimeoutError(f"{url} did not become healthy within {timeout:.0f}s")


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@contextmanager
def running_stack(workers: int = 1) -> Iterator[Dict[str, Any]]:
    """Yield ``{gateway_url, backend_url, api_key, workers}`` for a live stack."""
    admin_token = secrets.token_urlsafe(32)
    backend_port, gateway_port = free_port(), free_port()
    backend_url = f"http://127.0.0.1:{backend_port}"
    gateway_url = f"http://127.0.0.1:{gateway_port}"
    procs: List[subprocess.Popen] = []
    with tempfile.TemporaryDirectory(prefix="gateway-perf-") as tmp:
        env = {
            **os.environ,
            "SECRET_KEY": secrets.token_urlsafe(48),
            "ADMIN_TOKEN": admin_token,
            "DATABASE_URL": f"sqlite+aiosqlite:///{Path(tmp) / 'gateway.db'}",
            "HTTP_BACKEND_URL": backend_url,
            "REDIS_ENABLED": "false",
            "LOG_LEVEL": "WARNING",
            "STARTUP_BANNER": "false",
            "DEBUG": "false",
        }
        try:
            backend = subprocess.Popen(
                [sys.executable, "-c", _BACKEND_RUNNER, str(REPO / _BACKEND), str(backend_port)],
                cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            procs.append(backend)
            gateway = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                 "--port", str(gateway_port), "--workers", str(workers), "--log-level", "warning"],
                cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            procs.append(gateway)
            wait_healthy(f"{backend_url}/health", backend)
            wait_healthy(f"{gateway_url}/health", gateway)
            resp = httpx.post(
                f"{gateway_url}/admin/api-keys",
                params={"name": "perf", "rate_limit_per_minute": 100_000_000, "rate_limit_per_hour": 1_000_000_000},
                headers={"Authorization": f"Bearer {admin_token}"},
                timeout=10.0,
            )
            resp.raise_for_status()
            yield {
                "gateway_url": gateway_url,
                "backend_url": backend_url,
                "api_key": resp.json()["key"],
                "workers": workers,
            }
        finally:
            for proc in reversed(procs):
                _stop(proc)


def environment(stack: Dict[str, Any], concurrency: int, duration: float, warmup: float) -> Dict[str, Any]:
    try:
        version = httpx.get(f"{stack['gateway_url']}/health", timeout=5.0).json().get("version")
    except (httpx.HTTPError, ValueError):
        version = None
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True, timeout=5
        ).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        commit = None
    return {
        "gateway_version": version,
        "git_commit": commit,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "gateway_workers": stack["workers"],
        "concurrency": concurrency,
        "duration_s": duration,
        "warmup_s": warmup,
        "load_generator": "perf.load_test (httpx, closed loop) on the same machine",
    }


async def run_scenarios(
    stack: Dict[str, Any], *, concurrency: int, duration: float, warmup: float
) -> List[Result]:
    common = dict(concurrency=concurrency, duration=duration, warmup=warmup)
    key = {"X-API-Key": stack["api_key"]}
    gw, be = stack["gateway_url"], stack["backend_url"]
    return [
        await run_load(name="backend direct", url=f"{be}/api/users", **common),
        await run_load(name="gateway /health", url=f"{gw}/health", **common),
        await run_load(name="auth rejected", url=f"{gw}/api/users", expect_status=401, **common),
        await run_load(name="proxy GET /api/users", url=f"{gw}/api/users", headers=key, **common),
        await run_load(
            name="proxy POST /api/users", url=f"{gw}/api/users", method="POST",
            headers=key, body=USER_BODY, **common,
        ),
    ]


def overhead_ms(results: Sequence[Result]) -> Optional[Dict[str, float]]:
    """Added latency of the gateway: proxied GET minus the same GET straight to the backend."""
    by_name = {r.name: r for r in results}
    direct, proxied = by_name.get("backend direct"), by_name.get("proxy GET /api/users")
    if not direct or not proxied or not direct.requests or not proxied.requests:
        return None
    return {
        "p50": round(proxied.p50_ms - direct.p50_ms, 3),
        "p95": round(proxied.p95_ms - direct.p95_ms, 3),
    }


def render_markdown(results: Sequence[Result], env: Dict[str, Any]) -> str:
    lines = [
        "# Gateway performance run",
        "",
        "| Setting | Value |",
        "|---|---|",
        *(f"| {k.replace('_', ' ')} | {v} |" for k, v in env.items()),
        "",
        "| Scenario | Requests | Errors | Req/s | p50 ms | p95 ms | p99 ms | Max ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
        *(
            f"| {r.name} | {r.requests} | {r.errors} | {r.rps} | {r.p50_ms} | {r.p95_ms} | {r.p99_ms} | {r.max_ms} |"
            for r in results
        ),
    ]
    extra = overhead_ms(results)
    if extra:
        lines += ["", f"Gateway overhead on a proxied GET: p50 +{extra['p50']} ms, p95 +{extra['p95']} ms."]
    lines += [
        "",
        "The load generator, gateway and backend share this machine, so the figures are "
        "indicative and not a capacity limit.",
        "",
    ]
    return "\n".join(lines)


def run_suite(
    *, concurrency: int = 16, duration: float = 10.0, warmup: float = 2.0, workers: int = 1
) -> Dict[str, Any]:
    with running_stack(workers=workers) as stack:
        results = asyncio.run(run_scenarios(stack, concurrency=concurrency, duration=duration, warmup=warmup))
        env = environment(stack, concurrency, duration, warmup)
    return {"environment": env, "results": results, "overhead_ms": overhead_ms(results)}


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Run the gateway performance suite against a local stack.")
    p.add_argument("--concurrency", type=int, default=16)
    p.add_argument("--duration", type=float, default=10.0, help="measured seconds per scenario")
    p.add_argument("--warmup", type=float, default=2.0)
    p.add_argument("--workers", type=int, default=1, help="gateway worker processes")
    p.add_argument("--max-p95-ms", type=float)
    p.add_argument("--max-error-rate", type=float)
    p.add_argument("--json", dest="json_path", help="write the full result as JSON")
    p.add_argument("--report", help="write a markdown report")
    args = p.parse_args(argv)

    out = run_suite(
        concurrency=args.concurrency, duration=args.duration, warmup=args.warmup, workers=args.workers
    )
    results: List[Result] = out["results"]
    print(format_table(results))
    if out["overhead_ms"]:
        print(f"\ngateway overhead (proxied GET minus direct): {out['overhead_ms']}")
    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(
                {"environment": out["environment"], "overhead_ms": out["overhead_ms"],
                 "results": [asdict(r) for r in results]},
                indent=2,
            ),
            encoding="utf-8",
        )
    if args.report:
        Path(args.report).write_text(render_markdown(results, out["environment"]), encoding="utf-8")
    failures = check_thresholds(results, args.max_p95_ms, args.max_error_rate)
    for line in failures:
        print(f"FAIL {line}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
