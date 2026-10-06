"""Main CLI entry point for AgentAnywhere Universal Gateway.

The CLI talks to a running gateway via the admin API. It is
intentionally argparse-only so it works without extra dependencies.

Examples
--------

    # Authenticate and cache a token
    agent-universal-gateway login --username admin --password "$ADMIN_TOKEN"

    # API keys
    agent-universal-gateway keys list
    agent-universal-gateway keys create --name "ci-bot" --rate-limit-minute 120

    # Routes
    agent-universal-gateway routes list
    agent-universal-gateway routes create --path "/svc/*" --backend http://api.local

    # Plugins
    agent-universal-gateway plugins list
    agent-universal-gateway plugins enable header-injector
    agent-universal-gateway plugins reload

    # Health & dashboard
    agent-universal-gateway health
    agent-universal-gateway dashboard

The ``agentanywhere-gateway`` and ``mcp-gateway`` commands are kept as legacy
aliases for the canonical ``agent-universal-gateway`` command.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.cli.client import AdminClient, CLIError
from app.cli.formatting import (
    format_error,
    format_json,
    format_table,
    print_kv,
)


def _first_env(*names: str, default: str) -> str:
    """Return the first set environment variable among ``names``.

    Lets the canonical ``AGENT_GATEWAY_*`` variables take precedence while the
    legacy ``MCP_GATEWAY_*`` names keep working for existing setups.
    """
    for name in names:
        val = os.environ.get(name)
        if val:
            return val
    return default


DEFAULT_BASE_URL = _first_env(
    "AGENT_GATEWAY_URL", "MCP_GATEWAY_URL", default="http://localhost:8000"
)
DEFAULT_CONFIG_PATH = Path(
    _first_env(
        "AGENT_GATEWAY_CONFIG",
        "MCP_GATEWAY_CONFIG",
        default=str(Path.home() / ".config" / "agent-universal-gateway" / "config.json"),
    )
)


# ----------------------------------------------------------------------
# Local config (cached token, base URL)
# ----------------------------------------------------------------------


def load_local_config() -> Dict[str, Any]:
    if not DEFAULT_CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(DEFAULT_CONFIG_PATH.read_text())
    except Exception:
        return {}


def save_local_config(data: Dict[str, Any]) -> None:
    DEFAULT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_CONFIG_PATH.write_text(json.dumps(data, indent=2))


def make_client(args: argparse.Namespace) -> AdminClient:
    cfg = load_local_config()
    base_url = args.url or cfg.get("base_url") or DEFAULT_BASE_URL
    token = args.token or os.environ.get("MCP_GATEWAY_TOKEN") or cfg.get("token")
    return AdminClient(base_url=base_url, token=token, timeout=args.timeout)


# ----------------------------------------------------------------------
# Output helpers
# ----------------------------------------------------------------------


def render(args: argparse.Namespace, data: Any, *, columns: Optional[List[str]] = None) -> None:
    if data is None:
        return
    if args.json:
        print(format_json(data))
        return
    if isinstance(data, list) and data and isinstance(data[0], dict):
        print(format_table(data, columns=columns))
        return
    if isinstance(data, dict):
        print_kv(data)
        return
    print(data)


# ----------------------------------------------------------------------
# Command handlers
# ----------------------------------------------------------------------


def cmd_login(args: argparse.Namespace) -> int:
    client = make_client(args)
    token = client.login(args.username, args.password)
    cfg = load_local_config()
    cfg["token"] = token
    cfg["base_url"] = client.base_url
    save_local_config(cfg)
    print(f"Logged in to {client.base_url}; token cached at {DEFAULT_CONFIG_PATH}")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    if DEFAULT_CONFIG_PATH.exists():
        DEFAULT_CONFIG_PATH.unlink()
    print("Logged out.")
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    client = make_client(args)
    data = client.get("/health", admin=False)
    render(args, data)
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    client = make_client(args)
    data = client.get("/admin/dashboard/stats")
    render(args, data)
    return 0


def cmd_config_show(args: argparse.Namespace) -> int:
    client = make_client(args)
    data = client.get("/admin/config")
    render(args, data)
    return 0


# ---- API keys ---------------------------------------------------------


def cmd_keys_list(args: argparse.Namespace) -> int:
    client = make_client(args)
    data = client.get("/admin/api-keys")
    render(args, data, columns=["id", "name", "is_active", "rate_limit_per_minute", "created_at"])
    return 0


def cmd_keys_create(args: argparse.Namespace) -> int:
    client = make_client(args)
    body = {
        "name": args.name,
        "rate_limit_per_minute": args.rate_limit_minute,
        "rate_limit_per_hour": args.rate_limit_hour,
        "allowed_paths": args.allowed_paths or [],
    }
    data = client.post("/admin/api-keys", json_body=body)
    render(args, data)
    return 0


def cmd_keys_delete(args: argparse.Namespace) -> int:
    client = make_client(args)
    data = client.delete(f"/admin/api-keys/{args.key_id}")
    render(args, data or {"key_id": args.key_id, "deleted": True})
    return 0


def cmd_keys_toggle(args: argparse.Namespace) -> int:
    client = make_client(args)
    data = client.put(f"/admin/api-keys/{args.key_id}/toggle")
    render(args, data)
    return 0


# ---- Routes -----------------------------------------------------------


def cmd_routes_list(args: argparse.Namespace) -> int:
    client = make_client(args)
    data = client.get("/admin/routes")
    render(args, data, columns=["path", "backend_url", "protocol", "methods", "timeout"])
    return 0


def cmd_routes_create(args: argparse.Namespace) -> int:
    client = make_client(args)
    body = {
        "path": args.path,
        "backend_url": args.backend,
        "protocol": args.protocol,
        "methods": args.methods or ["GET", "POST", "PUT", "DELETE", "PATCH"],
        "timeout": args.timeout_route,
    }
    data = client.post("/admin/routes", json_body=body)
    render(args, data)
    return 0


# ---- Plugins ---------------------------------------------------------


def cmd_plugins_list(args: argparse.Namespace) -> int:
    client = make_client(args)
    data = client.get("/admin/plugins")
    render(args, data, columns=["name", "version", "plugin_type", "enabled"])
    return 0


def cmd_plugins_show(args: argparse.Namespace) -> int:
    client = make_client(args)
    data = client.get(f"/admin/plugins/{args.name}")
    render(args, data)
    return 0


def cmd_plugins_enable(args: argparse.Namespace) -> int:
    client = make_client(args)
    data = client.post(f"/admin/plugins/{args.name}/enable")
    render(args, data)
    return 0


def cmd_plugins_disable(args: argparse.Namespace) -> int:
    client = make_client(args)
    data = client.post(f"/admin/plugins/{args.name}/disable")
    render(args, data)
    return 0


def cmd_plugins_reload(args: argparse.Namespace) -> int:
    client = make_client(args)
    body: Dict[str, Any] = {}
    if args.path:
        body["plugin_paths"] = list(args.path)
    if args.disable:
        body["plugins_disabled"] = list(args.disable)
    if args.config_file:
        try:
            body["plugin_configs"] = json.loads(Path(args.config_file).read_text())
        except Exception as exc:
            print(format_error(f"failed to read --config-file: {exc}"), file=sys.stderr)
            return 2
    data = client.post("/admin/plugins/reload", json_body=body or None)
    render(args, data)
    return 0


def cmd_plugins_remove(args: argparse.Namespace) -> int:
    client = make_client(args)
    data = client.delete(f"/admin/plugins/{args.name}")
    render(args, data)
    return 0


# ---- Webhooks --------------------------------------------------------


def cmd_webhooks_list(args: argparse.Namespace) -> int:
    client = make_client(args)
    data = client.get("/admin/webhooks")
    render(args, data, columns=["id", "url", "events", "enabled"])
    return 0


def cmd_webhooks_create(args: argparse.Namespace) -> int:
    client = make_client(args)
    body = {
        "url": args.target_url,
        "events": args.events or ["request.completed"],
        "secret": args.secret,
    }
    data = client.post("/admin/webhooks", json_body=body)
    render(args, data)
    return 0


def cmd_webhooks_delete(args: argparse.Namespace) -> int:
    client = make_client(args)
    data = client.delete(f"/admin/webhooks/{args.webhook_id}")
    render(args, data or {"webhook_id": args.webhook_id, "deleted": True})
    return 0


# ---- Tenants --------------------------------------------------------


def cmd_tenants_list(args: argparse.Namespace) -> int:
    client = make_client(args)
    data = client.get("/admin/tenants")
    render(args, data, columns=["tenant_id", "name", "is_active"])
    return 0


def cmd_tenants_show(args: argparse.Namespace) -> int:
    client = make_client(args)
    data = client.get(f"/admin/tenants/{args.tenant_id}")
    render(args, data)
    return 0


# ---- Logs ------------------------------------------------------------


def cmd_logs(args: argparse.Namespace) -> int:
    client = make_client(args)
    params = {"limit": args.limit, "offset": args.offset}
    if args.status:
        params["status"] = args.status
    if args.path_filter:
        params["path"] = args.path_filter
    data = client.get("/admin/logs", params=params)
    render(args, data, columns=["timestamp", "method", "path", "status_code", "duration_ms"])
    return 0


# ----------------------------------------------------------------------
# Parser
# ----------------------------------------------------------------------


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", help=f"Gateway base URL (default: {DEFAULT_BASE_URL})")
    parser.add_argument("--token", help="Admin token (overrides cached token)")
    parser.add_argument("--timeout", type=float, default=15.0, help="HTTP timeout in seconds")
    parser.add_argument("--json", action="store_true", help="Print raw JSON output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-universal-gateway",
        description="Manage AgentAnywhere Universal Gateway from the terminal.",
    )
    _add_common_args(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    # login / logout
    p_login = sub.add_parser("login", help="Authenticate against the gateway and cache a token.")
    p_login.add_argument("--username", required=True)
    p_login.add_argument("--password", required=True)
    p_login.set_defaults(func=cmd_login)

    p_logout = sub.add_parser("logout", help="Forget the cached admin token.")
    p_logout.set_defaults(func=cmd_logout)

    # health / dashboard / config
    p_health = sub.add_parser("health", help="Show gateway health.")
    p_health.set_defaults(func=cmd_health)

    p_dash = sub.add_parser("dashboard", help="Show dashboard statistics.")
    p_dash.set_defaults(func=cmd_dashboard)

    p_config = sub.add_parser("config", help="Inspect gateway configuration.")
    config_sub = p_config.add_subparsers(dest="config_cmd", required=True)
    p_cfg_show = config_sub.add_parser("show", help="Print the gateway config snapshot.")
    p_cfg_show.set_defaults(func=cmd_config_show)

    # keys
    p_keys = sub.add_parser("keys", help="Manage API keys.")
    keys_sub = p_keys.add_subparsers(dest="keys_cmd", required=True)
    p_keys_list = keys_sub.add_parser("list", help="List API keys.")
    p_keys_list.set_defaults(func=cmd_keys_list)
    p_keys_create = keys_sub.add_parser("create", help="Create a new API key.")
    p_keys_create.add_argument("--name", required=True)
    p_keys_create.add_argument("--rate-limit-minute", type=int, default=60)
    p_keys_create.add_argument("--rate-limit-hour", type=int, default=1000)
    p_keys_create.add_argument("--allowed-paths", nargs="*")
    p_keys_create.set_defaults(func=cmd_keys_create)
    p_keys_del = keys_sub.add_parser("delete", help="Delete an API key by id.")
    p_keys_del.add_argument("key_id")
    p_keys_del.set_defaults(func=cmd_keys_delete)
    p_keys_tog = keys_sub.add_parser("toggle", help="Toggle an API key active state.")
    p_keys_tog.add_argument("key_id")
    p_keys_tog.set_defaults(func=cmd_keys_toggle)

    # routes
    p_routes = sub.add_parser("routes", help="Manage routes.")
    routes_sub = p_routes.add_subparsers(dest="routes_cmd", required=True)
    p_routes_list = routes_sub.add_parser("list", help="List routes.")
    p_routes_list.set_defaults(func=cmd_routes_list)
    p_routes_create = routes_sub.add_parser("create", help="Create a route.")
    p_routes_create.add_argument("--path", required=True)
    p_routes_create.add_argument("--backend", required=True)
    p_routes_create.add_argument("--protocol", default="http")
    p_routes_create.add_argument("--methods", nargs="*")
    p_routes_create.add_argument("--timeout-route", type=int, default=30)
    p_routes_create.set_defaults(func=cmd_routes_create)

    # plugins
    p_plugins = sub.add_parser("plugins", help="Manage plugins.")
    plugins_sub = p_plugins.add_subparsers(dest="plugins_cmd", required=True)
    p_pl_list = plugins_sub.add_parser("list", help="List loaded plugins.")
    p_pl_list.set_defaults(func=cmd_plugins_list)
    p_pl_show = plugins_sub.add_parser("show", help="Show a plugin in detail.")
    p_pl_show.add_argument("name")
    p_pl_show.set_defaults(func=cmd_plugins_show)
    p_pl_en = plugins_sub.add_parser("enable", help="Enable a plugin.")
    p_pl_en.add_argument("name")
    p_pl_en.set_defaults(func=cmd_plugins_enable)
    p_pl_dis = plugins_sub.add_parser("disable", help="Disable a plugin.")
    p_pl_dis.add_argument("name")
    p_pl_dis.set_defaults(func=cmd_plugins_disable)
    p_pl_reload = plugins_sub.add_parser("reload", help="Discover and reload plugins.")
    p_pl_reload.add_argument("--path", nargs="*", help="Override plugin search paths")
    p_pl_reload.add_argument("--disable", nargs="*", help="Plugin names to skip")
    p_pl_reload.add_argument("--config-file", help="JSON file with per-plugin config")
    p_pl_reload.set_defaults(func=cmd_plugins_reload)
    p_pl_rm = plugins_sub.add_parser("remove", help="Unregister a plugin.")
    p_pl_rm.add_argument("name")
    p_pl_rm.set_defaults(func=cmd_plugins_remove)

    # webhooks
    p_wh = sub.add_parser("webhooks", help="Manage webhooks.")
    wh_sub = p_wh.add_subparsers(dest="webhooks_cmd", required=True)
    p_wh_list = wh_sub.add_parser("list", help="List webhooks.")
    p_wh_list.set_defaults(func=cmd_webhooks_list)
    p_wh_create = wh_sub.add_parser("create", help="Register a webhook target.")
    p_wh_create.add_argument("--target-url", required=True)
    p_wh_create.add_argument("--events", nargs="*")
    p_wh_create.add_argument("--secret")
    p_wh_create.set_defaults(func=cmd_webhooks_create)
    p_wh_del = wh_sub.add_parser("delete", help="Delete a webhook by id.")
    p_wh_del.add_argument("webhook_id")
    p_wh_del.set_defaults(func=cmd_webhooks_delete)

    # tenants
    p_tn = sub.add_parser("tenants", help="Inspect tenants.")
    tn_sub = p_tn.add_subparsers(dest="tenants_cmd", required=True)
    p_tn_list = tn_sub.add_parser("list", help="List tenants.")
    p_tn_list.set_defaults(func=cmd_tenants_list)
    p_tn_show = tn_sub.add_parser("show", help="Show a tenant.")
    p_tn_show.add_argument("tenant_id")
    p_tn_show.set_defaults(func=cmd_tenants_show)

    # logs
    p_logs = sub.add_parser("logs", help="Tail recent request logs.")
    p_logs.add_argument("--limit", type=int, default=20)
    p_logs.add_argument("--offset", type=int, default=0)
    p_logs.add_argument("--status", type=int)
    p_logs.add_argument("--path-filter", dest="path_filter")
    p_logs.set_defaults(func=cmd_logs)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    func: Callable[[argparse.Namespace], int] = args.func
    try:
        return func(args)
    except CLIError as exc:
        print(format_error(str(exc)), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
