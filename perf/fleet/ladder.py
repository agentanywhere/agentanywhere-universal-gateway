"""Drive the benchmark fleet from your own machine over SSH.

For every (config, workers) pair it restarts the gateway, then for every target rate it
captures gateway-host CPU and memory while the load generator runs a constant-rate attack
at the gateway; it also runs the same attack straight at the backend, so latency can be
attributed to the gateway and not the upstream. Results are appended to a JSON-lines file.

    FLEET=fleet.json python -m perf.fleet.ladder --label fixed \\
        --configs nodb,sqlite,sqlite-wal,postgres --workers 4,8 --rates 500,1000,2000

``fleet.json`` names the hosts::

    {"user": "azureuser", "key": "~/.ssh/id_ed25519",
     "gateway": {"ssh": "20.0.0.1", "private": "10.0.0.4"},
     "backend": {"ssh": "20.0.0.2", "private": "10.0.0.5"},
     "loadgen": {"ssh": "20.0.0.3", "private": "10.0.0.6"},
     "postgres": {"ssh": "20.0.0.4", "private": "10.0.0.7"}}

Each host needs the repository in ``~/gw`` with a virtualenv (gateway, backend), ``vegeta`` on
the load generator, ``sysstat`` on the gateway host, and ``~/gw/run/secrets.env`` and
``~/gw/run/pg.url`` on the gateway host. See docs/how-to/performance-testing.md.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


class Fleet:
    def __init__(self, path: str) -> None:
        self.cfg = json.loads(Path(path).read_text())
        self.key = os.path.expanduser(self.cfg["key"])
        self.user = self.cfg.get("user", "azureuser")

    def private(self, role: str) -> str:
        return self.cfg[role]["private"]

    def run(self, role: str, command: str, timeout: Optional[int] = None, check: bool = True) -> str:
        cmd = ["ssh", "-i", self.key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
               "-o", "ServerAliveInterval=20", f"{self.user}@{self.cfg[role]['ssh']}", command]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if check and out.returncode != 0:
            raise RuntimeError(f"{role}: {command[:80]} failed: {out.stderr[-400:]}")
        return out.stdout

    def copy_key(self) -> None:
        """Copy the API key the gateway created to the load generator without printing it."""
        key = self.run("gateway", "cat ~/gw/run/key").strip()
        self.run("loadgen", f"umask 077; mkdir -p ~/run; printf %s {shlex.quote(key)} > ~/run/key")


def last_json(text: str) -> Dict[str, Any]:
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    return {}


def reset_postgres(fleet: Fleet) -> None:
    fleet.run("postgres", "sudo -u postgres psql -d gateway -q -c "
              "'DROP SCHEMA public CASCADE; CREATE SCHEMA public AUTHORIZATION gateway;'")


def attack(fleet: Fleet, url: str, rate: int, duration: int, warmup: int, tag: str, method: str, key: bool) -> Dict[str, Any]:
    out = fleet.run(
        "loadgen",
        f"cd ~/gw && bash perf/fleet/attack.sh {url} {rate} {duration} {warmup} ~/results/{tag} {method} "
        f"{'~/run/key' if key else ''}",
        timeout=duration + 120,
    )
    return last_json(out)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--fleet", default=os.environ.get("FLEET", "fleet.json"))
    p.add_argument("--label", required=True, help="a name for this series, for example fixed or as-shipped")
    p.add_argument("--configs", default="nodb,sqlite,sqlite-wal,postgres")
    p.add_argument("--workers", default="4")
    p.add_argument("--rates", default="500,1000,2000")
    p.add_argument("--duration", type=int, default=45, help="seconds per run")
    p.add_argument("--warmup", type=int, default=10, help="seconds discarded from each run")
    p.add_argument("--method", default="GET")
    p.add_argument("--path", default="/api/bench")
    p.add_argument("--out", default="results.jsonl")
    p.add_argument("--gateway-env", default="", help="extra settings for the gateway, e.g. API_KEY_LOCAL_CACHE_TTL_SECONDS=5")
    p.add_argument("--no-direct", action="store_true", help="skip the direct-to-backend baseline")
    args = p.parse_args(argv)

    fleet = Fleet(args.fleet)
    rates = [int(r) for r in args.rates.split(",")]
    gw_url = f"http://{fleet.private('gateway')}:8000{args.path}"
    be_url = f"http://{fleet.private('backend')}:8003{args.path}"
    out = Path(args.out)

    def record(row: Dict[str, Any]) -> None:
        row["label"] = args.label
        row["time"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with out.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        keys = ("target_rate", "throughput_rps", "error_rate", "p50_ms", "p95_ms", "p99_ms", "p999_ms")
        print(f"  {row['config']:<22} w={row.get('workers', '-')!s:<2} " + " ".join(f"{k}={row.get(k)}" for k in keys), flush=True)

    if not args.no_direct:
        print("direct baseline (load generator to backend)")
        for rate in rates:
            res = attack(fleet, be_url, rate, args.duration, args.warmup, f"direct-{rate}", args.method, key=False)
            record({"config": "direct-backend", "workers": None, **res})

    for config in args.configs.split(","):
        for workers in [int(w) for w in args.workers.split(",")]:
            print(f"config={config} workers={workers}")
            if config.startswith("postgres"):
                reset_postgres(fleet)
            fleet.run("gateway", f"cd ~/gw && CONFIG={config} WORKERS={workers} BACKEND_URL=http://{fleet.private('backend')}:8003 "
                                 f"GATEWAY_ENV={shlex.quote(args.gateway_env)} bash perf/fleet/gateway_up.sh", timeout=240)
            fleet.copy_key()
            for rate in rates:
                tag = f"{args.label}-{config}-w{workers}-r{rate}"
                cap = f"~/capture/{tag}"
                fleet.run("gateway", f"bash ~/gw/perf/fleet/capture.sh start {cap}")
                res = attack(fleet, gw_url, rate, args.duration, args.warmup, tag, args.method, key=True)
                fleet.run("gateway", f"bash ~/gw/perf/fleet/capture.sh stop {cap}", check=False)
                host = last_json(fleet.run(
                    "gateway",
                    f"python3 ~/gw/perf/fleet/host_stats.py --mpstat {cap}/mpstat.txt --pidstat {cap}/pidstat.txt "
                    f"--warmup {args.warmup}", check=False))
                record({"config": config, "workers": workers, **res, "host": host})
    return 0


if __name__ == "__main__":
    sys.exit(main())
