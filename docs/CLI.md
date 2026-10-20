# CLI: `agentanywhere-gateway`

> Reference material. It is kept in step with the code by the automated tests and by
> review, not by a line-by-line manual check. If this page and the gateway disagree,
> the gateway is right; please open an issue.

A small, dependency-free command-line tool to manage a running
AgentAnywhere Universal Gateway from the terminal. Talks to the admin
API over HTTPS/HTTP.

The canonical command is `agentanywhere-gateway`. The
`agent-universal-gateway` and `mcp-gateway` commands are kept as
backward-compatible aliases.

---

## Install

The CLI is shipped with the project. The package is not on PyPI yet, so
install it from a checkout with `pip install -e .`:

```bash
pip install -e .
agentanywhere-gateway --help
```

Without installing, run the launcher script directly:

```bash
./scripts/mcp-gateway --help
# or
python -m app.cli --help
```

---

## Configure

By default the CLI talks to `http://localhost:8000`. Override per-call
or persistently:

```bash
agentanywhere-gateway --url https://gateway.example.com health
export AGENT_GATEWAY_URL=https://gateway.example.com   # MCP_GATEWAY_URL still works as a fallback
```

Authenticate once and cache the token (stored at
`~/.config/agent-universal-gateway/config.json`):

```bash
agentanywhere-gateway login --username admin --password "$ADMIN_TOKEN"
agentanywhere-gateway logout    # forget the token
```

The password is the gateway's `ADMIN_TOKEN`, or its `SECRET_KEY` when
`ADMIN_TOKEN` is empty. There is no default password.

You can also pass `--token` per-call or set `MCP_GATEWAY_TOKEN`.

Add `--json` to any command for machine-readable output suitable for
piping into `jq`.

---

## Commands

### Diagnostics

```bash
agentanywhere-gateway health
agentanywhere-gateway dashboard
agentanywhere-gateway config show
agentanywhere-gateway logs --limit 50 --status 500
```

### API keys

```bash
agentanywhere-gateway keys list
agentanywhere-gateway keys create --name "ci-bot" --rate-limit-minute 120 --rate-limit-hour 5000 \
    --allowed-paths /mcp /a2a   # optional; prints the key once, keep it
agentanywhere-gateway keys toggle <key-id>
agentanywhere-gateway keys delete <key-id>
```

### Routes

```bash
agentanywhere-gateway routes list
agentanywhere-gateway routes create \
    --path "/svc/*" \
    --backend http://api.local \
    --protocol http \
    --methods GET POST
```

### Plugins

```bash
agentanywhere-gateway plugins list
agentanywhere-gateway plugins show request-id
agentanywhere-gateway plugins enable header-injector
agentanywhere-gateway plugins disable audit-logger
agentanywhere-gateway plugins reload --path /opt/myplugins --config-file plugins.json
agentanywhere-gateway plugins remove header-injector
```

### Webhooks

```bash
agentanywhere-gateway webhooks list
agentanywhere-gateway webhooks create --target-url https://hooks.example.com/x \
    --events rate_limit_exceeded api_key_revoked --secret s3cret
agentanywhere-gateway webhooks delete <id>
```

### Tenants

```bash
agentanywhere-gateway tenants list
agentanywhere-gateway tenants show acme
```

---

## Exit codes

| Code | Meaning                             |
| ---- | ----------------------------------- |
| 0    | Success                             |
| 1    | API call failed (`CLIError`)        |
| 2    | Bad arguments / config file errors  |
| 130  | Interrupted (Ctrl-C)                |

---

## Tips

- Use `--json | jq ...` for scripts and CI.
- The CLI honours `NO_COLOR=1` for plain output.
- Use `agentanywhere-gateway plugins reload` to reconfigure plugins without
  restarting the gateway.
