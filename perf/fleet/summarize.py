"""Summarise one constant-rate run.

Reads the per-request JSON lines that ``vegeta encode --to json`` writes, drops the first
``--warmup`` seconds, and prints one JSON object: achieved throughput, success rate and
latency percentiles (p50, p90, p95, p99, p99.9, max, in milliseconds) over the steady-state
window. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from typing import Dict, List, Optional


def percentile(sorted_values: List[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    rank = max(1, math.ceil(round(pct / 100.0 * len(sorted_values), 9)))   # round: 99.9% of 1000 is 999, not 999.0000000000001
    return sorted_values[min(rank, len(sorted_values)) - 1]


def _ts(value: str) -> float:
    # vegeta writes RFC 3339 with nanoseconds; datetime keeps microseconds.
    head, _, frac = value.rstrip("Z").partition(".")
    frac = (frac.split("+")[0].split("-")[0] + "000000")[:6]
    return datetime.fromisoformat(f"{head}.{frac}").timestamp()


def summarize(lines, warmup: float, rate: Optional[float] = None) -> Dict:
    rows = []
    for line in lines:
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    if not rows:
        return {"requests": 0}
    t0 = min(_ts(r["timestamp"]) for r in rows)
    window = [r for r in rows if _ts(r["timestamp"]) - t0 >= warmup]
    if not window:
        return {"requests": 0}
    span = max(_ts(r["timestamp"]) for r in window) - min(_ts(r["timestamp"]) for r in window)
    ok = [r for r in window if 200 <= r["code"] < 400]
    lat = sorted(r["latency"] / 1e6 for r in window)           # ns -> ms, all requests
    codes: Dict[str, int] = {}
    for r in window:
        key = str(r["code"]) if r["code"] else (r.get("error") or "error")[:40]
        codes[key] = codes.get(key, 0) + 1
    return {
        "target_rate": rate,
        "requests": len(window),
        "window_s": round(span, 1),
        "throughput_rps": round(len(window) / span, 1) if span > 0 else 0.0,
        "success_rps": round(len(ok) / span, 1) if span > 0 else 0.0,
        "error_rate": round(1 - len(ok) / len(window), 5),
        "p50_ms": round(percentile(lat, 50), 2),
        "p90_ms": round(percentile(lat, 90), 2),
        "p95_ms": round(percentile(lat, 95), 2),
        "p99_ms": round(percentile(lat, 99), 2),
        "p999_ms": round(percentile(lat, 99.9), 2),
        "max_ms": round(lat[-1], 2),
        "status": codes,
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("file", nargs="?", help="vegeta JSON lines (default: stdin)")
    p.add_argument("--warmup", type=float, default=10.0, help="seconds to discard from the start")
    p.add_argument("--rate", type=float, help="the target rate, recorded in the output")
    args = p.parse_args(argv)
    stream = open(args.file) if args.file else sys.stdin
    print(json.dumps(summarize(stream, args.warmup, args.rate)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
