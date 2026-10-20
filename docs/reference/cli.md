# CLI reference: `agentanywhere-gateway`

`agentanywhere-gateway` is a small command-line client for the admin API of a
running AgentAnywhere Universal Gateway. It uses only the Python standard
library (`urllib`) and talks HTTP to the gateway; it does not start one.

The help output on this page was produced by running the CLI from a checkout
(`python3 -m app.cli`).

## Contents

- [Names and aliases](#names-and-aliases)
- [Install](#install)
- [Global options](#global-options)
- [Authentication](#authentication)
- [Commands](#commands)
- [Commands that do not work as written](#commands-that-do-not-work-as-written)
- [Exit codes](#exit-codes)
- [Environment variables](#environment-variables)

## Names and aliases

This is the one place where the project's names and their legacy aliases are
listed. Other pages use the canonical names only.

| Thing | Canonical name | Also accepted (legacy) |
|---|---|---|
| Product | AgentAnywhere Universal Gateway | MCP Gateway (the original name) |
| Python distribution | `agentanywhere-universal-gateway` | none |
| Command | `agentanywhere-gateway` | `agent-universal-gateway`, `mcp-gateway`; `scripts/mcp-gateway` without installing |
| CLI gateway URL variable | `AGENT_GATEWAY_URL` | `MCP_GATEWAY_URL` |
| CLI config file variable | `AGENT_GATEWAY_CONFIG` | `MCP_GATEWAY_CONFIG` |
| CLI token variable | none | `MCP_GATEWAY_TOKEN` (the only name) |
| Plugin directory variable | `PLUGIN_PATHS` (setting) | `MCP_GATEWAY_PLUGIN_PATH` |
| Plugin disable variable | `PLUGINS_DISABLED` (setting) | `MCP_GATEWAY_PLUGIN_DISABLED` |
| Plugin entry-point group | `mcp_gateway.plugins` (the only name) | none |
| Compose container name | `mcp_a2a_gateway` (the only name) | none |

All three commands run the same program; its help text calls itself
`agent-universal-gateway`.

## Install

The package is not published on PyPI. Install it from a checkout:

```bash
pip install -e .
agentanywhere-gateway --help
```

Without installing:

```bash
python3 -m app.cli --help
./scripts/mcp-gateway --help
```

## Global options

Global options go **before** the command.

```text
usage: agent-universal-gateway [-h] [--url URL] [--token TOKEN]
                               [--timeout TIMEOUT] [--json]
                               {login,logout,health,dashboard,config,keys,routes,plugins,webhooks,tenants,logs}
                               ...
```

| Option | Default | Meaning |
|---|---|---|
| `--url URL` | the URL cached by `login`, then `AGENT_GATEWAY_URL` (or `MCP_GATEWAY_URL`), then `http://localhost:8000` | Gateway base URL. A cached URL wins over the environment variable. |
| `--token TOKEN` | `MCP_GATEWAY_TOKEN`, then the cached token | Admin credential. |
| `--timeout SECONDS` | `15.0` | HTTP timeout. |
| `--json` | off | Print raw JSON, for `jq`. |

## Authentication

```bash
agentanywhere-gateway login --username admin --password "$ADMIN_TOKEN"
agentanywhere-gateway logout
```

`login` calls `POST /admin/auth/login`. The password is the admin credential
(`ADMIN_TOKEN`, or `SECRET_KEY` when `ADMIN_TOKEN` is empty). On success the
CLI writes the returned token, which is the admin credential itself, and the
URL to `~/.config/agent-universal-gateway/config.json` (or
`AGENT_GATEWAY_CONFIG`). Protect that file. `logout` deletes it.

## Commands

| Command | Calls | Notes |
|---|---|---|
| `health` | `GET /health` | No credential needed. |
| `dashboard` | `GET /admin/dashboard/stats` | |
| `config show` | `GET /admin/config` | Secrets are redacted. |
| `keys list` | `GET /admin/api-keys` | Ids, names and limits; never the keys themselves. |
| `keys create --name NAME [--rate-limit-minute N] [--rate-limit-hour N] [--allowed-paths P ...]` | `POST /admin/api-keys` | Prints the new key once; keep it. |
| `keys toggle KEY_ID` | `PUT /admin/api-keys/{id}/toggle` | Needs the key's `id`. |
| `keys delete KEY_ID` | `DELETE /admin/api-keys/{id}` | Needs the key's `id`. |
| `routes list` | `GET /admin/routes` | |
| `routes create --path P --backend URL [--protocol http] [--methods M ...] [--timeout-route 30]` | `POST /admin/routes` | `--methods` is ignored by the gateway. |
| `plugins list` | `GET /admin/plugins` | |
| `plugins show NAME` | `GET /admin/plugins/{name}` | |
| `plugins enable NAME` / `plugins disable NAME` | `POST /admin/plugins/{name}/enable` or `/disable` | |
| `plugins reload [--path DIR ...] [--disable NAME ...] [--config-file FILE]` | `POST /admin/plugins/reload` | `--config-file` is a JSON object of plugin name to configuration. |
| `plugins remove NAME` | `DELETE /admin/plugins/{name}` | |
| `webhooks list` | `GET /admin/webhooks` | |
| `webhooks create --target-url URL --events E [E ...] [--secret S]` | `POST /admin/webhooks` | `--events` is required; one webhook per event. |
| `webhooks delete ID` | `DELETE /admin/webhooks/{id}` | |
| `tenants list` | `GET /admin/tenants` | |
| `tenants show TENANT_ID` | `GET /admin/tenants/{id}` | |
| `logs [--limit 20] [--offset 0] [--status CODE] [--path-filter TEXT]` | `GET /admin/logs` | `--status` filters by status code. |

Plain output is a table or key-value list. `NO_COLOR` turns colour off.

## Commands that do not work as written

| Command | Problem | Do this instead |
|---|---|---|
| `routes create --methods ...` | The CLI sends a `methods` field; routes have none, so every method is routed. | Leave it out. |

`keys create`, `webhooks create`, `webhooks delete` and `logs --status`, which
failed or were ignored in earlier builds, work; `tests/test_cli_against_gateway.py`
runs them against the app.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success. |
| 1 | The request failed: the gateway could not be reached, or answered with an error. The message is printed to stderr, for example `error: could not reach http://127.0.0.1:59999/health: [Errno 61] Connection refused`. |
| 2 | Bad arguments (from `argparse`), or `plugins reload --config-file` could not be read. |
| 130 | Interrupted with Ctrl-C. |

## Environment variables

| Variable | Meaning |
|---|---|
| `AGENT_GATEWAY_URL` | Default gateway URL. `MCP_GATEWAY_URL` is used when it is unset. |
| `AGENT_GATEWAY_CONFIG` | Path of the cached-token file. `MCP_GATEWAY_CONFIG` is used when it is unset. |
| `MCP_GATEWAY_TOKEN` | Admin credential when `--token` is not given. |
| `NO_COLOR` | Any value disables colour. |
