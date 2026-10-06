# Plugin System

> Reference material. It is kept in step with the code by the automated tests and by
> review, not by a line-by-line manual check. If this page and the gateway disagree,
> the gateway is right; please open an issue.

The AgentAnywhere Universal Gateway ships a small extension framework for third parties.
Plugins can rewrite traffic, plug in additional auth providers, register
custom protocol handlers, and react to lifecycle events.

The system is purposely lightweight: a plugin is just a Python class
that subclasses one of the bases in `app.plugins`. The gateway owns
discovery, lifecycle, and dispatch.

---

## Quick start

1. Write a plugin module:

```python
# myplugin.py
from app.plugins import (
    TransformPlugin,
    PluginContext,
    PluginMetadata,
    PluginType,
)

class StampPlugin(TransformPlugin):
    metadata = PluginMetadata(
        name="stamp",
        version="1.0.0",
        description="Stamp X-My-Header on every request",
        plugin_type=PluginType.TRANSFORM,
    )

    def transform_request(self, ctx: PluginContext):
        ctx.headers = {**ctx.headers, "X-My-Header": "yes"}
        return ctx
```

2. Make the gateway find it. Pick one:

   - **Filesystem** — drop `myplugin.py` into a directory and point the
     gateway at it via the `MCP_GATEWAY_PLUGIN_PATH` env var (`:`
     separated) or the `plugin_paths` setting.
   - **Entry point** — install your package and expose:

     ```toml
     [project.entry-points."mcp_gateway.plugins"]
     stamp = "myplugin:StampPlugin"
     ```

3. Restart the gateway, then verify with the CLI:

```bash
agentanywhere-gateway plugins list
agentanywhere-gateway plugins show stamp
agentanywhere-gateway plugins disable stamp   # toggle without restart
```

---

## Plugin types

| Base class           | What it does                                     |
| -------------------- | ------------------------------------------------ |
| `TransformPlugin`    | Rewrite request / response (headers, body, etc.) |
| `AuthProviderPlugin` | Custom authentication backends                   |
| `ProtocolPlugin`     | Handle a custom path-prefixed protocol           |
| `HookPlugin`         | Lifecycle / request hooks (`startup`, etc.)      |

All inherit from `Plugin` and may override:

- `setup()` — called once when registered.
- `teardown()` — called on unregister / shutdown.
- `health()` — return a JSON-serialisable health snapshot.

---

## Configuration

Per-plugin configuration is passed to the constructor as a `dict`. You
can either set it via the gateway settings (`PLUGIN_CONFIGS_JSON` env
var, JSON object map) or supply it on the fly via the CLI:

```bash
echo '{"header-injector": {"enabled": true, "request_headers": {"X-A": "1"}}}' > cfg.json
agentanywhere-gateway plugins reload --config-file cfg.json
```

Disable specific plugins at startup with `PLUGINS_DISABLED=foo,bar` or
`MCP_GATEWAY_PLUGIN_DISABLED=foo,bar`.

---

## Built-in plugins

Ship with the gateway as both reference implementations and useful
defaults. They live in `app/plugins/builtin/`:

| Name               | Type           | Default | What it does                            |
| ------------------ | -------------- | ------- | --------------------------------------- |
| `request-id`       | transform      | enabled | Stamps `X-Request-ID` if absent          |
| `header-injector`  | transform      | off     | Adds configurable static headers         |
| `audit-logger`     | hook           | off     | Emits structured audit log lines         |
| `static-token-auth`| auth provider  | off     | Permits a fixed shared bearer token      |

Enable an off-by-default plugin via config or:

```bash
agentanywhere-gateway plugins enable header-injector
```

---

## Admin API

| Method | Path                              | Description                  |
| ------ | --------------------------------- | ---------------------------- |
| GET    | `/admin/plugins`                  | List plugins + stats         |
| GET    | `/admin/plugins/{name}`           | Inspect a plugin             |
| POST   | `/admin/plugins/{name}/enable`    | Enable                       |
| POST   | `/admin/plugins/{name}/disable`   | Disable (without unloading)  |
| DELETE | `/admin/plugins/{name}`           | Unregister and tear down     |
| POST   | `/admin/plugins/reload`           | Re-discover from all sources |

---

## Errors and isolation

A plugin failure must never crash the gateway. The registry guards
every dispatch with `try/except`, increments an error counter on the
plugin, and continues processing the request. Inspect failures with:

```bash
agentanywhere-gateway plugins show stamp --json | jq .stats
```

---

## Writing tests

```python
from app.plugins import PluginContext
from myplugin import StampPlugin

def test_stamp_adds_header():
    ctx = PluginContext(headers={})
    out = StampPlugin().transform_request(ctx)
    assert out.headers["X-My-Header"] == "yes"
```

For end-to-end tests use the global `plugin_registry` and call
`plugin_registry.clear()` between cases.
