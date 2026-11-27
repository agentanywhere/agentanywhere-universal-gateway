"""Turn compare.py's JSON lines into a Markdown table.

    python perf/compare/report.py compare.jsonl

For each target: the highest rate it sustained (see compare.py), how busy its pinned
cores were at that rate, and the latency it gave at the fixed rate. Standard library only.
"""
import json
import sys
from collections import OrderedDict

NAMES = {
    "nginx": "nginx 1.24 (floor: proxy + key check, no gateway features)",
    "python": "AgentAnywhere, Python gateway as shipped (request log in SQLite)",
    "python-nodb": "AgentAnywhere, Python gateway, request log off",
    "python-tuned": "AgentAnywhere, Python gateway tuned (log off, AI scoring off, key cache)",
    "rust": "AgentAnywhere, Rust data plane in front of the Python gateway",
    "rust-split": "AgentAnywhere, Rust data plane (Python control plane on another core)",
    "agentgateway": "agentgateway 1.7.0",
    "litellm": "LiteLLM 1.111.0 (pass-through endpoint, master key)",
}


def main(path: str) -> int:
    rows = [json.loads(l) for l in open(path) if l.strip()]
    targets = OrderedDict()
    for r in rows:
        t = targets.setdefault(r["target"], {"ramp": {}, "fixed": None, "best": None})
        if r["kind"] == "ramp":
            t["ramp"][r["target_rate"]] = r
        elif r["kind"] == "fixed":
            t["fixed"] = r
        elif r["kind"] == "result":
            t["best"] = r["max_sustained_rps"]
    fixed_rate = next((t["fixed"]["target_rate"] for t in targets.values() if t["fixed"]), "?")
    print(f"| Gateway | Max sustained req/s per core | p50 / p99 at {int(fixed_rate)} req/s (ms) | Core busy at max |")
    print("|---|---:|---:|---:|")
    for name, t in targets.items():
        best = t["best"] or 0
        at = t["ramp"].get(best, {})
        busy = (at.get("host") or {}).get("pinned_busy_pct")
        f = t["fixed"] or {}
        fx = f"{f.get('p50_ms')} / {f.get('p99_ms')}" if f.get("error_rate", 1) <= 0.001 else "did not keep up"
        print(f"| {NAMES.get(name, name)} | {best:,} | {fx} | {busy if busy is not None else '-'}% |")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
