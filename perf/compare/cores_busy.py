"""How busy the pinned cores and the whole gateway host were, from ``mpstat -P ALL 1``.

    cores_busy.py <mpstat.txt> --cores 0-1 --warmup 5 --window 25

Prints one JSON object: mean busy % of the pinned cores (``pinned_busy_pct``, 100 means
they were saturated) and of the others (``other_busy_pct``), over the ``--window`` busiest
seconds of each core. The capture runs a little longer than the attack (while results are
encoded), so a plain mean would be diluted by idle seconds. Standard library only.
"""
import argparse
import json
import sys
from statistics import mean

sys.path.insert(0, __import__("os").path.join(__import__("os").path.dirname(__file__), "..", "fleet"))
from host_stats import parse_mpstat  # noqa: E402

p = argparse.ArgumentParser()
p.add_argument("mpstat")
p.add_argument("--cores", default="0-1")
p.add_argument("--warmup", type=int, default=5)
p.add_argument("--window", type=int, default=0, help="seconds of load; 0 uses every sample")
a = p.parse_args()


def busiest(values):
    values = sorted(values, reverse=True)
    return values[:a.window] if a.window > 0 else values


lo, hi = (int(x) for x in a.cores.split("-"))
per = parse_mpstat(a.mpstat, a.warmup)
pinned = [mean(busiest(v)) for k, v in per.items() if k.isdigit() and lo <= int(k) <= hi and v]
other = [mean(busiest(v)) for k, v in per.items() if k.isdigit() and not lo <= int(k) <= hi and v]
print(json.dumps({"pinned_busy_pct": round(mean(pinned), 1) if pinned else None,
                  "other_busy_pct": round(mean(other), 1) if other else None}))
