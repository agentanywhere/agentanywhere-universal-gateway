# Database reference

The tables that AgentAnywhere Universal Gateway creates, from the models in
`app/db_models.py`. The gateway creates any missing table when it starts
(`init_db()` in `app/database.py` runs SQLAlchemy's `create_all`, which never
alters or drops an existing table), and the Alembic baseline creates the same
schema (on an existing database it only adds what is missing).

The database is chosen by `DATABASE_URL`. The default is
`sqlite+aiosqlite:///./gateway.db`, a file in the working directory.
PostgreSQL works through `postgresql+asyncpg://...`.

## Contents

- [Tables at a glance](#tables-at-a-glance)
- [api_keys](#api_keys)
- [routes](#routes)
- [tenants, tenant_api_keys and tenant_usage](#tenants-tenant_api_keys-and-tenant_usage)
- [request_logs](#request_logs)
- [settings_profiles](#settings_profiles)
- [Governance tables](#governance-tables)
- [openapi_registrations](#openapi_registrations)
- [agent_registry](#agent_registry)
- [Alembic](#alembic)

## Tables at a glance

| Table | Holds | Written by |
|---|---|---|
| `api_keys` | API keys as SHA-256 hashes, with limits and scope (rows from earlier builds may still hold the key until it is next used) | `/admin/api-keys` |
| `routes` | Dynamic routes | `POST /admin/routes` |
| `tenants` | Tenants with quota and config | `/admin/tenants` |
| `tenant_api_keys` | Which key hash belongs to which tenant | `associate-api-key` |
| `tenant_usage` | Request counters per tenant, per minute, hour and day window | Every request from a tenant's key |
| `request_logs` | One row per request, including rejected ones | Every request (not pruned) |
| `settings_profiles` | Saved protocol and global settings | `/admin/settings/profiles` |
| `governance_audit_log` | Hash-chained audit entries | Governance and admin actions |
| `admin_roles` | Token hash to role (not enforced) | `/admin/governance/roles` |
| `governance_policies` | Declarative policies | `/admin/governance/policies` |
| `governance_ip_rules` | IP allow and deny rules | `/admin/governance/ip-rules` |
| `openapi_registrations` | OpenAPI documents exposed as MCP tools and A2A skills | `/admin/registrations/openapi` |
| `agent_registry` | Agent-edge slugs and upstreams | `/admin/agents`, `AGENT_ROUTES_JSON` |

## api_keys

| Column | Type | Notes |
|---|---|---|
| `id` | string, primary key | UUID. Needed to toggle or delete a key. |
| `key` | string, unique, nullable | Empty for keys created by this release. Rows written by earlier builds hold the key in plaintext until it next authenticates, when it is cleared. |
| `key_hash` | string, unique, not null | SHA-256 of the key, hex. Used for lookups. |
| `name` | string | |
| `rate_limit_per_minute` | integer | Default 60. |
| `rate_limit_per_hour` | integer | Default 1000. |
| `allowed_paths` | JSON list | Path scope; requests outside it answer 403. Empty means every path. |
| `key_metadata` | JSON object | Holds `key_prefix`, the first characters of the key, shown by the admin API. |
| `created_at`, `expires_at`, `last_used_at` | datetime | Naive UTC. `last_used_at` is updated on every database lookup. |
| `is_active` | boolean | |

## routes

`id`, `path`, `backend_url`, `protocol`, `timeout` (default 30), `rate_limit`,
`requires_auth` (default true), `headers` (JSON), `rewrite_path`, `is_active`,
`created_at`, `updated_at`. Only active rows are loaded into the route table.
`rate_limit`, `requires_auth` and `headers` are not applied. There is no admin
endpoint to delete or deactivate a route; change `is_active` or delete the row
directly.

## tenants, tenant_api_keys and tenant_usage

`tenants`: `tenant_id` (primary key), `name`, `description`, `config` (JSON),
`quota` (JSON: `requests_per_minute`, `requests_per_hour`, `requests_per_day`,
`storage_mb`), `is_active`, `created_at`, `updated_at`.

`tenant_api_keys`: `id`, `tenant_id`, `api_key_id`, `api_key_hash`,
`created_at`. Foreign keys to `tenants` and `api_keys` with `ON DELETE
CASCADE`; SQLite does not enforce those unless foreign keys are turned on, so
the repository code deletes associations itself when a tenant is deleted.

`tenant_usage`: `id`, `tenant_id`, `window` (`minute`, `hour` or `day`),
`window_start`, `request_count`, `error_count`, `bytes_in`, `bytes_out`. One
row per tenant, window and window start. `bytes_in` is always 0 in this
release.

## request_logs

`id` (integer), `request_id`, `method`, `path`, `client_ip`, `api_key_id`,
`tenant_id`, `status_code`, `response_time_ms` (integer), `timestamp`,
`user_agent`, `error`, `protocol`.

Requests rejected by the authentication layer (401, 403, 429) are logged too,
with the error code in `error` (for example `RATE_LIMIT_EXCEEDED`) and the
key's id in `api_key_id` when the key was recognised. `tenant_id` and
`protocol` are written as null in this release. The table is never pruned.

## settings_profiles

`id`, `name` (unique), `description`, `protocol_settings` (JSON),
`global_settings` (JSON), `is_default`, `created_at`, `updated_at`,
`created_by`.

## Governance tables

- `governance_audit_log`: `id`, `timestamp`, `actor`, `actor_role`, `action`,
  `resource_type`, `resource_id`, `request_id`, `client_ip`, `user_agent`,
  `outcome`, `before`, `after`, `metadata`, `prev_hash`, `entry_hash`. Each
  entry hashes the previous one; `GET /admin/governance/audit/verify` walks the
  chain.
- `admin_roles`: `id`, `token_hash` (unique), `name`, `role`, `created_at`,
  `last_used_at`, `is_active`.
- `governance_policies`: `id`, `name` (unique), `description`, `priority`,
  `enabled`, `conditions` (JSON), `action` (JSON), `created_at`, `updated_at`.
- `governance_ip_rules`: `id`, `cidr`, `action` (`allow` or `deny`), `scope`
  (`global`, `tenant` or `api_key`), `scope_id`, `description`, `enabled`,
  `created_at`.

## openapi_registrations

`id`, `name` (unique), `description`, `openapi_document` (JSON),
`upstream_base_url`, `path_prefix`, `expose_mcp_tools`, `expose_a2a_skills`,
`is_active`, `created_at`, `updated_at`.

## agent_registry

`slug` (primary key), `upstream_base`, `audience`, `public_paths` (JSON),
`status`, `display_name`, `description`, `runtime_mode`, `data_residency`,
`enabled`, `created_at`, `updated_at`, `updated_by`.

## Alembic

`alembic/versions/` has one migration, the baseline `0001`
(`0001_baseline_schema.py`). It creates every table above with the same
columns, indexes and foreign keys as `create_all`, and on a database the
gateway already created it adds only what is missing.
`tests/test_migrations.py` fails if the models and the migration disagree.
See [Upgrade the gateway](../how-to/upgrade.md).
