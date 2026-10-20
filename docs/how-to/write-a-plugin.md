# Write a plugin

**Goal:** change the gateway's behaviour with your own Python code (add or
remove headers, accept another kind of credential, answer a custom path, or run
code at start-up) without changing the gateway's source.

**Before you start:** Python, and a gateway you can restart. Plugins run inside
the gateway process with its full permissions, and they run synchronously on
the request path: a slow plugin slows every request.

## Contents

- [The plugin interface](#the-plugin-interface)
- [Where plugins run](#where-plugins-run)
- [Steps: a transform plugin](#steps-a-transform-plugin)
- [Other plugin types](#other-plugin-types)
- [Configuration](#configuration)
- [Built-in plugins](#built-in-plugins)
- [Manage plugins at run time](#manage-plugins-at-run-time)
- [Test a plugin](#test-a-plugin)
- [If it did not work](#if-it-did-not-work)

## The plugin interface

A plugin is a class that subclasses one of the bases in `app.plugins`
(defined in `app/plugins/base.py`) and sets a `metadata` class attribute:

```python
PluginMetadata(name="stamp", version="1.0.0", description="...", plugin_type=PluginType.TRANSFORM)
```

`name` must be unique. `plugin_type` must match the base class, because the
registry selects plugins by `metadata.plugin_type`.

| Base class | `plugin_type` | Override | Called |
|---|---|---|---|
| `TransformPlugin` | `TRANSFORM` | `transform_request(ctx)`, `transform_response(ctx)`; return a `PluginContext` or `None` to leave it unchanged | Before forwarding, and after a buffered response |
| `AuthProviderPlugin` | `AUTH_PROVIDER` | `authenticate(ctx)`; return a truthy dict to accept the request | Before API-key validation |
| `ProtocolPlugin` | `PROTOCOL` | set `path_prefix`; `handle(ctx)` returns a dict (sent as JSON, status 200) or `None` to pass | Before routing, for paths starting with `path_prefix` |
| `HookPlugin` | `HOOK` | `hooks()` returns `{"hook name": callable}` | See below |

Every plugin also has `setup()` (after it is registered), `teardown()` (when
it is removed or the gateway stops), `health()`, a `config` dict and an
`enabled` flag.

`PluginContext` fields: `method`, `path`, `headers` (dict), `query_params`
(dict), `body` (bytes, or `None`), `status_code` (responses), `request_id`,
`api_key` (the caller's key), `tenant_id`, `extras`.

Hooks that the gateway fires: `startup`, `shutdown`, `request_pre` (after
request transforms) and `response_post` (after response transforms).
`request_post` and `response_pre` are mentioned in the base class but never
fired.

Every call is wrapped in `try`/`except`: an exception is logged, counted in the
plugin's stats, and the request continues as if the plugin had returned
`None`.

## Where plugins run

- Only on the catch-all proxy path. They do not run for `/graphql`,
  `/mqtt/*`, `/admin/*`, `/oauth2/*`, health and metrics paths, WebSocket
  routes or the agent edge.
- `transform_response` does not run on streamed responses (AG-UI and ACP
  streams, A2A `message:stream`).
- An `AuthProviderPlugin` that accepts a request **skips everything else** in
  the authentication middleware: API-key validation, rate limits, tenant
  quotas, IP rules and policies.

## Steps: a transform plugin

This plugin adds a header to every request the gateway forwards. It was
loaded in process for this page, and a test backend received the header.

1. Create a directory outside the repository, for example
   `/opt/gateway-plugins`, with a file `stamp.py`:

   ```python
   from app.plugins import TransformPlugin, PluginContext, PluginMetadata, PluginType

   class StampPlugin(TransformPlugin):
       metadata = PluginMetadata(
           name="stamp",
           version="1.0.0",
           description="Add X-Stamp to every proxied request",
           plugin_type=PluginType.TRANSFORM,
       )

       def transform_request(self, ctx: PluginContext):
           ctx.headers = {**ctx.headers, "X-Stamp": self.config.get("value", "yes")}
           return ctx
   ```

2. Tell the gateway where to look, and optionally configure the plugin. In
   the environment of the gateway process:

   ```bash
   export MCP_GATEWAY_PLUGIN_PATH=/opt/gateway-plugins
   export PLUGIN_CONFIGS_JSON='{"stamp": {"value": "from-config"}}'
   ```

   `MCP_GATEWAY_PLUGIN_PATH` is read from the process environment only, so
   export it rather than writing it in `.env` (Docker Compose's `env_file` does
   export it). Alternatively, set `PLUGIN_PATHS=["/opt/gateway-plugins"]`,
   which `.env` does apply.

3. Restart the gateway.

**Check:**

```bash
curl -s "$URL/admin/plugins" -H "Authorization: Bearer $ADMIN"
```

The list includes `{"name": "stamp", ..., "enabled": true}` next to the
built-in plugins, and your backend receives `X-Stamp: from-config` on every
proxied request.

To strip the caller's API key before it reaches the backend, the same pattern
works with:

```python
def transform_request(self, ctx):
    ctx.headers = {k: v for k, v in ctx.headers.items() if k.lower() not in ("x-api-key", "authorization")}
    return ctx
```

## Other plugin types

A protocol plugin that answers `/echo/...` itself:

```python
from app.plugins import ProtocolPlugin, PluginMetadata, PluginType

class EchoPlugin(ProtocolPlugin):
    metadata = PluginMetadata(name="echo", version="1.0.0", plugin_type=PluginType.PROTOCOL)
    path_prefix = "/echo/"

    def handle(self, ctx):
        return {"path": ctx.path, "method": ctx.method}
```

The caller still needs an API key; the plugin runs after authentication.

A hook plugin:

```python
from app.plugins import HookPlugin, PluginMetadata, PluginType

class StartupNotice(HookPlugin):
    metadata = PluginMetadata(name="startup-notice", version="1.0.0", plugin_type=PluginType.HOOK)

    def hooks(self):
        return {"startup": lambda ctx: print("gateway started")}
```

These two follow the interface in `app/plugins/base.py` and the registry in
`app/plugins/registry.py`; they were not run for this page.

## Packaging a plugin

Instead of a directory, install a package that exposes the class through the
`mcp_gateway.plugins` entry-point group:

```toml
[project.entry-points."mcp_gateway.plugins"]
stamp = "mypackage.stamp:StampPlugin"
```

The gateway loads, in order: the built-in plugins in `app/plugins/builtin/`,
entry points, then plugin directories. A plugin loaded later with the same
name replaces the earlier one.

## Configuration

`PLUGIN_CONFIGS_JSON` is a JSON object of plugin name to configuration dict.
Each plugin receives its dict as `self.config`. The built-in plugins other than
`request-id` read `enabled` from it.

`PLUGINS_DISABLED=["name"]` (a JSON array) or
`MCP_GATEWAY_PLUGIN_DISABLED=name,other` (comma-separated, environment only)
skip plugins at load time. `PLUGINS_ENABLED=false` turns the plugin system off.

## Built-in plugins

| Name | Type | On by default | Does |
|---|---|---|---|
| `request-id` | transform | yes | Adds `X-Request-ID` to the forwarded request, and to the backend's response, when absent. |
| `header-injector` | transform | no | Adds configured static headers. |
| `audit-logger` | hook | no | Writes structured audit log lines. |
| `static-token-auth` | auth provider | no | Accepts a fixed shared bearer token, **bypassing API keys, limits and quotas**. |

Turn one on with `PLUGIN_CONFIGS_JSON='{"header-injector": {"enabled": true, ...}}'`
or `POST /admin/plugins/header-injector/enable`.

## Manage plugins at run time

| Call | Does |
|---|---|
| `GET /admin/plugins` | List plugins with metadata, `enabled` and call statistics. |
| `GET /admin/plugins/{name}` | One plugin. |
| `POST /admin/plugins/{name}/enable`, `/disable` | Turn a plugin on or off without unloading it. |
| `DELETE /admin/plugins/{name}` | Unregister it (runs `teardown()`). |
| `POST /admin/plugins/reload` | Load again, optionally with a JSON body `{"plugin_paths": [...], "plugins_disabled": [...], "plugin_configs": {...}}`. |

These act on the process that handles the call only.

## Test a plugin

Plugins are plain classes, so a unit test needs no gateway:

```python
from app.plugins import PluginContext
from stamp import StampPlugin

def test_stamp_adds_header():
    out = StampPlugin(config={"value": "x"}).transform_request(PluginContext(headers={}))
    assert out.headers["X-Stamp"] == "x"
```

For tests that go through the gateway, use the `client` fixture in
`tests/conftest.py`, register the plugin with
`app.plugins.plugin_registry.register(...)`, and call
`plugin_registry.clear()` afterwards. `tests/test_plugins.py` has examples.

## If it did not work

| Symptom | Cause | Fix |
|---|---|---|
| The plugin is not in `GET /admin/plugins` | Path not seen: `MCP_GATEWAY_PLUGIN_PATH` only in `.env`, a typo, or an import error | Export the variable, or use `PLUGIN_PATHS`; look for `plugins_loaded` and errors in the start-up log. |
| Start-up fails with `error parsing value for field "plugins_disabled"` | A comma-separated value | Write a JSON array. |
| The plugin is listed but nothing happens | `plugin_type` does not match the base class, the plugin is disabled, or the request is not on the catch-all path | Check `metadata` and `enabled`; see [Where plugins run](#where-plugins-run). |
| Errors counted in the plugin's stats | An exception in your code | `GET /admin/plugins/{name}` shows `last_error`. |
