"""Summarise gateway-host CPU and memory from the files ``capture.sh`` writes.

``mpstat -P ALL 1`` gives per-CPU use; ``pidstat -u -r -C python 1`` gives the gateway
workers'. The first ``--warmup`` samples are dropped. Prints one JSON object. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from statistics import mean


def parse_mpstat(path: str, warmup: int):
    per_cpu = defaultdict(list)
    for line in open(path, errors="replace"):
        parts = line.split()
        if len(parts) >= 11 and re.match(r"^\d{2}:\d{2}:\d{2}", parts[0]) and (parts[1].isdigit() or parts[1] == "all"):
            try:
                idle = float(parts[-1])
            except ValueError:
                continue
            per_cpu[parts[1]].append(100.0 - idle)
    out = {k: v[warmup:] for k, v in per_cpu.items() if len(v) > warmup}
    return out


def parse_pidstat(path: str, warmup: int):
    cpu_by_time = defaultdict(float)
    rss_by_time = defaultdict(float)
    for line in open(path, errors="replace"):
        parts = line.split()
        if len(parts) < 9 or not re.match(r"^\d{2}:\d{2}:\d{2}", parts[0]):
            continue
        stamp = parts[0]
        if "%CPU" in line or "RSS" in line:
            continue
        try:
            if len(parts) >= 10 and re.match(r"^\d+(\.\d+)?$", parts[7]) and "python" in parts[-1]:
                cpu_by_time[stamp] += float(parts[7])          # %CPU
            elif "python" in parts[-1]:
                rss_by_time[stamp] += float(parts[6]) / 1024.0  # RSS kB -> MB
        except (ValueError, IndexError):
            continue
    cpu = list(cpu_by_time.values())[warmup:]
    rss = list(rss_by_time.values())[warmup:]
    return cpu, rss


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mpstat", required=True)
    p.add_argument("--pidstat")
    p.add_argument("--warmup", type=int, default=10)
    a = p.parse_args(argv)
    per_cpu = parse_mpstat(a.mpstat, a.warmup)
    result = {}
    if per_cpu:
        overall = per_cpu.get("all", [])
        cores = {k: mean(v) for k, v in per_cpu.items() if k != "all"}
        result.update(
            cpu_avg_pct=round(mean(overall), 1) if overall else None,
            cpu_busiest_core_pct=round(max(cores.values()), 1) if cores else None,
            cpu_cores_over_90=sum(1 for v in cores.values() if v > 90),
            cpu_core_count=len(cores),
        )
    if a.pidstat:
        cpu, rss = parse_pidstat(a.pidstat, a.warmup)
        if cpu:
            result["gateway_procs_cpu_pct_sum_avg"] = round(mean(cpu), 1)
        if rss:
            result["gateway_procs_rss_mb_max"] = round(max(rss), 1)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
