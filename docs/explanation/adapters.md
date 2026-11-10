# Adapters: swapping parts of the request path

The request path depends on four small interfaces, called **ports**, and not on
concrete libraries. Which implementation each port uses is a setting, so you
can change one without touching the rest of the gateway.

| Port | Setting | Built-in choices | What it does |
|---|---|---|---|
| JSON codec | `JSON_CODEC` | `stdlib` (default), `orjson` | Serialises response bodies |
| Upstream client | `UPSTREAM_CLIENT` | `httpx` (default) | Forwards requests to backends |
| Analytics | `ANALYTICS_ADAPTER` | `full` (default), `off` | Per-request anomaly detection and usage analytics |
| Request-log sink | `REQUEST_LOG_SINK` | `database` (default), `null` | Receives one entry per finished request |

Every default is the behaviour the gateway had before adapters existed.

## Switching

Set the setting and restart:

```bash
JSON_CODEC=orjson          # needs: pip install orjson
ANALYTICS_ADAPTER=off
REQUEST_LOG_SINK=null
```

An unknown name stops the gateway at start-up and lists the valid choices, so a
typo cannot silently fall back to a default.

## Bringing your own

Any setting also accepts `module:attribute`: a callable that takes the settings
and returns an object with the port's methods. The module only has to be
importable by the gateway process.

```python
# my_pkg/fast.py
class MyUpstream:
    async def request(self, method, url, **kwargs): ...
    def build_request(self, method, url, **kwargs): ...
    async def send(self, request, **kwargs): ...
    async def aclose(self): ...

def make_client(settings):
    return MyUpstream()
```

```bash
UPSTREAM_CLIENT=my_pkg.fast:make_client
```

The gateway checks that what comes back has the port's methods and refuses to
start if it does not. The methods each port needs are in
[`app/adapters/ports.py`](https://github.com/agentanywhere/agentanywhere-universal-gateway/blob/main/app/adapters/ports.py).
An upstream client must return objects that behave like `httpx.Response`
(`status_code`, `headers`, `content`, `json()`, `aclose()`), because the proxy
handler reads them that way. That is the seam a faster data plane would use.

## Notes on the built-ins

- **`orjson`** is much faster than `stdlib` but is not byte-identical in edge
  cases: `NaN` and infinity become `null` instead of raising an error, and
  integers beyond 64 bits raise. It applies to responses FastAPI builds from a
  handler's return value; responses a handler constructs itself with
  `JSONResponse(...)` use the standard library.
- **`ANALYTICS_ADAPTER=off`** runs no analytics. `AI_ANALYTICS_ENABLED=false`
  turns the per-request call off for any adapter. The admin API's insights
  endpoints read a separate analytics object and are not affected by either.
- **`REQUEST_LOG_SINK=null`** discards entries like `REQUEST_LOG_MODE=off`. The
  `database` sink is the one `REQUEST_LOG_MODE` configures.

## Adding a built-in adapter

1. Write the class and register a factory in `app/adapters/builtin.py`.
2. Add it to the parametrised contract test for its port in
   `tests/test_adapters.py`. Every adapter must pass the same test.
3. Document the new value in `docs/reference/configuration.md`.
