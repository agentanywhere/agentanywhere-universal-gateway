"""Turn a ladder results file (JSON lines from ``perf.fleet.ladder``) into Markdown tables.

    python -m perf.fleet.report results.jsonl --label tuned > report.md

One table per worker count. Rows are configurations, columns are target rates; each cell shows
success rate and p50 / p99 latency in milliseconds, so the rate at which a configuration stops
coping is easy to see. A cell is marked ``x`` when more than ``--fail`` of requests failed.
Standard library only.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import OrderedDict
from typing import Dict, List, Optional


def load(path: str, label: Optional[str]) -> List[dict]:
    rows = []
    for line in open(path):
        line = line.strip()
        if line:
            row = json.loads(line)
            if label is None or row.get("label") == label:
                rows.append(row)
    return rows


def _fmt_ms(value: float) -> str:
    return f"{value:.1f}" if value < 100 else f"{value:.0f}"


def cell(row: Optional[dict], fail: float) -> str:
    if row is None:
        return "-"
    ok = 100 * (1 - row["error_rate"])
    text = f"{ok:.0f}% {_fmt_ms(row['p50_ms'])}/{_fmt_ms(row['p99_ms'])}"
    return f"**x** {text}" if row["error_rate"] > fail else text


def tables(rows: List[dict], fail: float = 0.01) -> str:
    direct = {int(r["target_rate"]): r for r in rows if r["config"] == "direct-backend"}
    gateway = [r for r in rows if r["config"] != "direct-backend"]
    rates = sorted({int(r["target_rate"]) for r in rows})
    out: List[str] = []
    for workers in sorted({r["workers"] for r in gateway if r.get("workers") is not None}):
        out += [f"### {workers} gateway workers", "",
                "Each cell: requests succeeded, then p50 / p99 latency in ms. **x** = more than "
                f"{fail:.0%} failed.", "",
                "| config | " + " | ".join(f"{r} req/s" for r in rates) + " |",
                "|---|" + "---|" * len(rates)]
        out.append("| backend direct | " + " | ".join(cell(direct.get(r), fail) for r in rates) + " |")
        by_config: Dict[str, Dict[int, dict]] = OrderedDict()
        for r in gateway:
            if r["workers"] == workers:
                by_config.setdefault(r["config"], {})[int(r["target_rate"])] = r
        for config, per_rate in by_config.items():
            out.append(f"| {config} | " + " | ".join(cell(per_rate.get(r), fail) for r in rates) + " |")
        out.append("")
    return "\n".join(out)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("file")
    p.add_argument("--label")
    p.add_argument("--fail", type=float, default=0.01, help="error fraction above which a cell is marked failed")
    args = p.parse_args(argv)
    print(tables(load(args.file, args.label), args.fail))
    return 0


if __name__ == "__main__":
    sys.exit(main())
