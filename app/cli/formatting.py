"""Lightweight terminal formatting helpers for the CLI.

We deliberately avoid third-party dependencies (rich, tabulate, click)
so the CLI works on a fresh ``pip install -r requirements.txt``.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Dict, Iterable, List, Optional


def supports_color() -> bool:
    return sys.stdout.isatty() and not _no_color()


def _no_color() -> bool:
    import os
    return bool(os.environ.get("NO_COLOR"))


def _color(text: str, code: str) -> str:
    if not supports_color():
        return text
    return f"\033[{code}m{text}\033[0m"


def format_error(text: str) -> str:
    return _color(f"error: {text}", "31")


def format_ok(text: str) -> str:
    return _color(text, "32")


def format_json(value: Any) -> str:
    return json.dumps(value, indent=2, default=str)


def _coerce_value(val: Any) -> str:
    if val is None:
        return "-"
    if isinstance(val, (dict, list)):
        return json.dumps(val, default=str)
    return str(val)


def format_table(rows: List[Dict[str, Any]], columns: Optional[Iterable[str]] = None) -> str:
    if not rows:
        return "(no rows)"
    cols = list(columns) if columns else _infer_columns(rows)
    header = cols
    body = [[_coerce_value(r.get(c)) for c in cols] for r in rows]

    widths = [len(c) for c in header]
    for row in body:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    sep = "  "
    lines = [sep.join(c.ljust(widths[i]) for i, c in enumerate(header))]
    lines.append(sep.join("-" * widths[i] for i in range(len(header))))
    for row in body:
        lines.append(sep.join(c.ljust(widths[i]) for i, c in enumerate(row)))
    return "\n".join(lines)


def _infer_columns(rows: List[Dict[str, Any]]) -> List[str]:
    seen: List[str] = []
    for row in rows:
        for k in row.keys():
            if k not in seen:
                seen.append(k)
    return seen


def print_kv(data: Dict[str, Any]) -> None:
    if not data:
        print("(empty)")
        return
    width = max(len(str(k)) for k in data.keys())
    for k, v in data.items():
        print(f"{str(k).ljust(width)}  {_coerce_value(v)}")
