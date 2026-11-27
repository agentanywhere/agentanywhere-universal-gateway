"""Compare gateways on the benchmark fleet, from your own machine over SSH.

Every target runs on the gateway host pinned to the same cores (``--cores``, default 0-1),
listens on :8000 in front of the same backend and requires an API key on every request.
For each target the load generator runs constant-rate attacks (vegeta, open loop) at
rising rates until the target stops keeping up, so the result is the highest rate it
sustains, and the latency it gives at a rate every target can carry (``--fixed-rate``).

A rate is *sustained* when, over the steady-state window, at least 99.9% of requests
succeed, achieved throughput is at least 97% of the target rate, and p99 is at most
``--p99-limit-ms``. A rate that fails is run once more before it counts as failed.

    python perf/compare/compare.py --fleet fleet.json --targets nginx,python,rust --out compare.jsonl

The fleet file is the one perf/fleet/ladder.py uses (gateway, backend, loadgen). See
perf/compare/README.md.
"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from perf.fleet.ladder import Fleet as _Fleet, last_json  # noqa: E402


class Fleet(_Fleet):
    """The ladder's fleet, plus an optional known_hosts file in the fleet file."""

    def run(self, role: str, command: str, timeout: Optional[int] = None, check: bool = True) -> str:
        import subprocess
        extra = ["-o", f"UserKnownHostsFile={self.cfg['known_hosts']}"] if self.cfg.get("known_hosts") else []
        cmd = ["ssh", "-i", self.key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", *extra,
               "-o", "ServerAliveInterval=20", f"{self.user}@{self.cfg[role]['ssh']}", command]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if check and out.returncode != 0:
            raise RuntimeError(f"{role}: {command[:80]} failed: {out.stderr[-400:]}")
        return out.stdout

HEADER = {"litellm": "Authorization: Bearer"}   # every other target takes X-API-Key


def attack(fleet: Fleet, target: str, url: str, rate: int, duration: int, warmup: int, tag: str) -> Dict[str, Any]:
    header = HEADER.get(target, "X-API-Key:")
    out = fleet.run("loadgen", f"bash ~/gw/perf/compare/attack.sh {url} {rate} {duration} {warmup} "
                               f"~/results/{tag} {shlex.quote(header)}", timeout=duration + 180)
    return last_json(out)


def sustained(res: Dict[str, Any], rate: int, p99_limit: float) -> bool:
    return (res.get("error_rate", 1) <= 0.001 and res.get("success_rps", 0) >= 0.97 * rate
            and res.get("p99_ms", 1e9) <= p99_limit)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--fleet", default="fleet.json")
    p.add_argument("--targets", default="nginx,python,python-nodb,rust,agentgateway,litellm")
    p.add_argument("--cores", default="0-1")
    p.add_argument("--start-rate", type=int, default=500)
    p.add_argument("--start-rates", default="python=50,python-nodb=50,python-tuned=50,litellm=50",
                   help="per-target start rates, overriding --start-rate")
    p.add_argument("--step", type=float, default=1.4, help="rate multiplier between attacks")
    p.add_argument("--max-rate", type=int, default=200000)
    p.add_argument("--fixed-rate", type=int, default=1000)
    p.add_argument("--p99-limit-ms", type=float, default=100.0)
    p.add_argument("--duration", type=int, default=30)
    p.add_argument("--warmup", type=int, default=5)
    p.add_argument("--path", default="/api/bench")
    p.add_argument("--out", default="compare.jsonl")
    args = p.parse_args(argv)

    fleet = Fleet(args.fleet)
    url = f"http://{fleet.private('gateway')}:8000{args.path}"
    backend = f"http://{fleet.private('backend')}:8003"
    out = Path(args.out)

    def record(row: Dict[str, Any]) -> None:
        row["time"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with out.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        print(f"  {row['target']:<13} {row['kind']:<6} rate={row['target_rate']:<7} "
              f"ok_rps={row.get('success_rps')} err={row.get('error_rate')} "
              f"p50={row.get('p50_ms')} p99={row.get('p99_ms')} busy={row.get('host', {}).get('pinned_busy_pct')}", flush=True)

    def run(target: str, rate: int, kind: str) -> Dict[str, Any]:
        tag = f"{target}-{kind}-r{rate}"
        cap = f"~/capture/{tag}"
        fleet.run("gateway", f"bash ~/gw/perf/fleet/capture.sh start {cap}")
        res = attack(fleet, target, url, rate, args.duration, args.warmup, tag)
        fleet.run("gateway", f"bash ~/gw/perf/fleet/capture.sh stop {cap}", check=False)
        host = last_json(fleet.run("gateway", f"python3 ~/gw/perf/compare/cores_busy.py {cap}/mpstat.txt "
                                              f"--cores {args.cores} --warmup {args.warmup} "
                                              f"--window {args.duration - args.warmup}", check=False))
        row = {"target": target, "kind": kind, "target_rate": rate, "cores": args.cores, **res, "host": host}
        record(row)
        return row

    for target in args.targets.split(","):
        print(f"target={target}", flush=True)
        start = f"cd ~/gw && TARGET={target} CORES={args.cores} BACKEND_URL={backend} bash perf/compare/target_up.sh"
        try:
            up = fleet.run("gateway", start, timeout=300)
        except RuntimeError:          # an overloaded predecessor can hold the port for a while
            time.sleep(20)
            up = fleet.run("gateway", start, timeout=300)
        print("  " + up.strip().splitlines()[-1], flush=True)
        key = fleet.run("gateway", "cat ~/run/key").strip()
        fleet.run("loadgen", f"umask 077; mkdir -p ~/run; printf %s {shlex.quote(key)} > ~/run/key")
        run(target, args.fixed_rate, "fixed")
        starts = dict(kv.split("=") for kv in args.start_rates.split(",") if kv)
        rate, best = int(starts.get(target, args.start_rate)), None
        def holds(r: int) -> bool:
            # A single transient stall (noisy neighbour, a GC pause in the load generator) can push
            # one run's p99 over the limit, so a failed rate is tried once more before it counts.
            if sustained(run(target, r, "ramp"), r, args.p99_limit_ms):
                return True
            time.sleep(10)
            return sustained(run(target, r, "retry"), r, args.p99_limit_ms)

        while rate <= args.max_rate:
            if not holds(rate):
                break
            best = rate
            rate = int(rate * args.step)
        if best is not None and rate <= args.max_rate:   # one bisection step between the last pass and the fail
            mid = (best + rate) // 2
            if mid > best and holds(mid):
                best = mid
        record({"target": target, "kind": "result", "target_rate": best or 0, "max_sustained_rps": best or 0,
                "cores": args.cores})
    return 0


if __name__ == "__main__":
    sys.exit(main())
