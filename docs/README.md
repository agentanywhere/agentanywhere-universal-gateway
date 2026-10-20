# Documentation map

Documentation for AgentAnywhere Universal Gateway, grouped by what you want to
do. If you are new, start with the first tutorial and read the known
limitations before you deploy anything.

If a page and the gateway disagree, the gateway is right and the page has a
bug; please [report it](support.md).

## Tutorials: learn by doing

| Page | You will |
|---|---|
| [Your first authenticated request](tutorials/getting-started.md) | Install the gateway, start a sample backend, create an API key and call the backend through the gateway. |
| [Two backends, limits, quotas and metrics](tutorials/two-backends-end-to-end.md) | Put an HTTP and an MCP backend behind the gateway, give two clients keys, hit a rate limit and a tenant quota, and read the metrics. |

## How-to guides: get a task done

| Page | Task |
|---|---|
| [Create, rotate and revoke API keys](how-to/manage-api-keys.md) | Issue, replace and take away keys. |
| [Configure routes and backends](how-to/configure-routes-and-backends.md) | Send each kind of traffic to the right backend. |
| [Set rate limits and tenant quotas](how-to/rate-limits-and-quotas.md) | Cap requests per key and per customer. |
| [Run behind nginx with TLS](how-to/run-behind-nginx-tls.md) | Serve HTTPS and keep operational endpoints private. |
| [Secure the gateway for production](how-to/secure-for-production.md) | Work through the production checklist. |
| [Back up and restore](how-to/back-up-and-restore.md) | Keep and recover keys, tenants and routes. |
| [Upgrade the gateway](how-to/upgrade.md) | Move to a new release; versioning, Alembic and the behaviour changes in this release. |
| [Monitor the gateway](how-to/monitor.md) | Health endpoints, Prometheus, Grafana, logs and traces. |
| [Write a plugin](how-to/write-a-plugin.md) | Extend the gateway with Python. |
| [Send events to a webhook](how-to/configure-webhooks.md) | Get notified of rate-limit, circuit breaker and key events. |
| [Run the tests](how-to/run-the-tests.md) | Run the suite and the documentation checks. |
| [Deploy with Docker Compose](how-to/deploy-with-docker-compose.md) | Run the gateway, Redis, the console and optional services. |
| [Troubleshoot 401, 403, 404, 502 and 429](how-to/troubleshoot-401-502-429.md) | Fix the most common request errors. |

## Reference: look it up

| Page | Contents |
|---|---|
| [Configuration](reference/configuration.md) | Every environment variable, with defaults. Checked against the code. |
| [HTTP API](reference/http-api.md) | Conventions, headers and the main endpoints, with real responses. |
| [HTTP endpoints (generated)](reference/http-endpoints.md) | Every operation, its credential and parameters. Generated from the code. |
| [CLI](reference/cli.md) | `agentanywhere-gateway` commands, exit codes, and the one list of names and legacy aliases. |
| [Errors and status codes](reference/errors.md) | Every error code the gateway produces. |
| [Metrics](reference/metrics.md) | Every Prometheus metric and its labels. |
| [Database](reference/database.md) | Every table and column. |

## Explanation: understand it

| Page | Explains |
|---|---|
| [What the gateway is, and what it is not](explanation/overview.md) | Purpose, audience, non-goals, licence. |
| [Architecture](explanation/architecture.md) | How a request flows; admin plane and data plane; where state lives. |
| [Security model](explanation/security-model.md) | What authenticates what, key storage, the admin credential, the `SECRET_KEY` overload. |
| [Protocol support](explanation/protocol-support.md) | What works for each protocol, and the evidence. |
| [Design decisions and trade-offs](explanation/design-decisions.md) | Why it works the way it does, and running more than one worker. |

## Everything else

| Page | For |
|---|---|
| [Known limitations](known-limitations.md) | What does not work, and the workarounds. |
| [Troubleshooting](troubleshooting.md) | Problems listed by symptom. |
| [FAQ](faq.md) | Straight answers to common questions. |
| [Glossary](glossary.md) | Terms used in these pages. |
| [Support and community](support.md) | Where to ask and how to report. |
| [Security policy](../SECURITY.md) | How to report a vulnerability; supported versions. |
| [Contributing](../CONTRIBUTING.md) | How to contribute, and a tour of the codebase. |
| [Changelog](../CHANGELOG.md) | What changed in each release. |
| [Plugins (moved)](PLUGINS.md), [Upgrading (moved)](UPGRADING.md), [Deployment (moved)](DEPLOYMENT.md), [Configuration (moved)](CONFIGURATION.md) | Pointers kept for links from code and configuration comments. |

## Elsewhere in the repository

- [Sample backends](../samples/backends/README.md): one small server per protocol, used by the tutorials.
- [Examples](../examples/README.md): Python and JavaScript client snippets.
- [Cookbooks](../cookbooks/README.md): LangGraph and CrewAI integration sketches.

## Checks

`scripts/check_docs.py` fails on broken links, broken anchors and malformed
Mermaid blocks in every Markdown file. `scripts/check_env_vars.py` fails if the
[configuration reference](reference/configuration.md) and the code disagree.
`scripts/generate_http_reference.py --check` fails if the
[generated endpoint list](reference/http-endpoints.md) is out of date. CI runs
all three.
