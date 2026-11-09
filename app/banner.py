"""Start-up banner: one readable summary of how this gateway instance is configured.

Printed to stderr so it never mixes with JSON log lines on stdout. It shows
which URLs to try and which credential opens the admin API, and it never
prints a secret, a token or a database password. Turn it off with
``STARTUP_BANNER=false``; colour is used only on a terminal and is switched
off by ``NO_COLOR``.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional, TextIO, Tuple
from urllib.parse import urlsplit

_RULE = "─" * 58


def _local_host(host: str) -> str:
    """A host a browser or curl on this machine can actually use."""
    return "127.0.0.1" if host in ("", "0.0.0.0", "::", "localhost") else host


def _cli_value(argv: List[str], flag: str) -> Optional[str]:
    for i, arg in enumerate(argv):
        if arg == flag and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith(flag + "="):
            return arg.split("=", 1)[1]
    return None


def _bind_address(settings: Any, argv: Optional[List[str]] = None) -> Tuple[str, int]:
    """Where the server is listening. ``uvicorn --host/--port`` wins over HOST/PORT,
    because those flags are what actually bind the socket."""
    argv = list(sys.argv[1:] if argv is None else argv)
    host = _cli_value(argv, "--host") or getattr(settings, "host", "0.0.0.0")
    port_raw = _cli_value(argv, "--port")
    try:
        port = int(port_raw) if port_raw else int(getattr(settings, "port", 8000))
    except ValueError:
        port = int(getattr(settings, "port", 8000))
    return host, port


def _admin_credential(settings: Any) -> str:
    if getattr(settings, "admin_token", ""):
        return "ADMIN_TOKEN"
    key = getattr(settings, "secret_key", "") or ""
    if key and not key.startswith("change-me"):
        return "SECRET_KEY"
    return "none"


def _database(settings: Any) -> str:
    url = getattr(settings, "database_url", None) or "sqlite"
    scheme = urlsplit(url).scheme or url.split(":", 1)[0]
    # Only the driver, never the host, user or password.
    return scheme.split("+", 1)[0]


def banner_facts(settings: Any) -> Dict[str, Any]:
    """The facts shown in the banner, also logged as one structured line."""
    host, port = _bind_address(settings)
    base = f"http://{_local_host(host)}:{port}"
    docs_on = bool(getattr(settings, "debug", False))
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "listen": f"{host}:{port}",
        "health_url": f"{base}/health",
        "docs_url": f"{base}/docs" if docs_on else None,
        "admin_credential": _admin_credential(settings),
        "database": _database(settings),
        "redis": bool(getattr(settings, "redis_enabled", False)),
        "log_level": str(settings.log_level).upper(),
        "log_format": settings.log_format,
        "log_sampling_rate": settings.log_sampling_rate,
        "log_redaction": bool(settings.log_redaction_enabled),
        "agent_edge": bool(getattr(settings, "agent_edge_enabled", False)),
        "ops_endpoints_require_admin": bool(getattr(settings, "ops_endpoints_require_admin", False)),
    }


def _admin_row(credential: str) -> str:
    if credential == "none":
        return "locked: set ADMIN_TOKEN to enable it"
    if credential == "SECRET_KEY":
        return "sign in with SECRET_KEY (set ADMIN_TOKEN for a separate admin credential)"
    return "sign in with ADMIN_TOKEN"


def _rows(facts: Dict[str, Any]) -> List[Tuple[str, str]]:
    return [
        ("Environment", facts["environment"]),
        ("Listening", facts["listen"]),
        ("Health check", facts["health_url"]),
        ("API docs", facts["docs_url"] or "off (set DEBUG=true to serve /docs)"),
        ("Admin API", _admin_row(facts["admin_credential"])),
        ("Storage", f"{facts['database']} database, Redis {'enabled' if facts['redis'] else 'disabled'}"),
        (
            "Logging",
            f"{facts['log_level']} · {facts['log_format']} · sampling {facts['log_sampling_rate']}"
            f" · redaction {'on' if facts['log_redaction'] else 'OFF'}",
        ),
        ("Agent edge", "on" if facts["agent_edge"] else "off"),
    ]


def render_banner(settings: Any, *, color: bool = False) -> str:
    facts = banner_facts(settings)

    def paint(text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if color else text

    width = max(len(label) for label, _ in _rows(facts))
    lines = [
        "",
        f"  {paint(facts['name'], '1')}  {paint('v' + str(facts['version']), '36')}",
        f"  {paint(_RULE, '2')}",
    ]
    for label, value in _rows(facts):
        lines.append(f"  {paint(label.ljust(width), '2')}  {value}")
    lines += [f"  {paint(_RULE, '2')}", ""]
    return "\n".join(lines)


def print_banner(settings: Any, stream: Optional[TextIO] = None) -> bool:
    """Print the banner if ``startup_banner`` is on. Returns whether it printed."""
    if not getattr(settings, "startup_banner", True):
        return False
    out = stream if stream is not None else sys.stderr
    try:
        color = bool(getattr(out, "isatty", lambda: False)()) and "NO_COLOR" not in os.environ
        out.write(render_banner(settings, color=color) + "\n")
        out.flush()
    except (OSError, ValueError):
        return False
    return True
