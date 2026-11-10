"""Adapters: each port has a contract every adapter must pass, and the gateway uses them."""
import importlib.util
from types import SimpleNamespace

import httpx
import pytest
from starlette.responses import JSONResponse

from app.adapters import (
    AdapterError,
    AdapterRegistry,
    Analytics,
    JsonCodec,
    RequestLogSink,
    UpstreamClient,
    build_adapters,
    json_response_class,
)
from app.adapters import builtin
from app.config import Settings

HAS_ORJSON = importlib.util.find_spec("orjson") is not None


def settings(**overrides):
    return Settings(**overrides)


# --- registry ------------------------------------------------------------------------------

class _Port:
    pass


def test_unknown_name_lists_the_choices():
    reg = AdapterRegistry("thing", JsonCodec)
    with pytest.raises(AdapterError, match="Choose one of: .*module:attribute"):
        reg.create("nope", settings())


def test_names_are_case_insensitive_and_sorted():
    assert builtin.json_codecs.names() == ["orjson", "stdlib"]
    assert builtin.json_codecs.create("STDLIB", settings()).name == "stdlib"


def make_fake_analytics(_settings):
    class Fake:
        def process_request(self, data):
            return {"is_anomaly": True, "anomaly_score": 0.9}

    return Fake()


def make_misfit(_settings):
    return object()


def make_broken(_settings):
    raise ValueError("boom")


def test_a_module_attribute_path_loads_your_own_adapter():
    adapter = builtin.analytics_engines.create(f"{__name__}:make_fake_analytics", settings())
    assert adapter.process_request({})["is_anomaly"] is True


def test_a_path_that_cannot_be_imported_is_a_clear_error():
    with pytest.raises(AdapterError, match="Cannot load"):
        builtin.analytics_engines.create("no_such_module_xyz:factory", settings())
    with pytest.raises(AdapterError, match="Cannot load"):
        builtin.analytics_engines.create(f"{__name__}:missing", settings())


def test_an_adapter_that_does_not_fit_the_port_is_rejected():
    with pytest.raises(AdapterError, match="does not fit the Analytics port"):
        builtin.analytics_engines.create(f"{__name__}:make_misfit", settings())


def test_a_factory_that_fails_is_reported_with_its_reason():
    with pytest.raises(AdapterError, match="failed to start: boom"):
        builtin.analytics_engines.create(f"{__name__}:make_broken", settings())


# --- JsonCodec contract --------------------------------------------------------------------

CODECS = ["stdlib"] + (["orjson"] if HAS_ORJSON else [])
SAMPLE = {"name": "Ada", "city": "Zürich", "tags": ["a", "b"], "n": 3, "ok": True, "none": None, "nested": {"x": 1.5}}


@pytest.mark.parametrize("name", CODECS)
def test_codec_contract(name):
    codec = builtin.json_codecs.create(name, settings())
    assert isinstance(codec, JsonCodec)
    raw = codec.dumps(SAMPLE)
    assert isinstance(raw, bytes)
    assert b" " not in raw.replace("Zürich".encode(), b"")  # compact
    assert "Zürich".encode("utf-8") in raw  # UTF-8, not \u escapes
    assert codec.loads(raw) == SAMPLE
    assert codec.loads(raw.decode()) == SAMPLE


def test_stdlib_codec_matches_starlette_byte_for_byte():
    codec = builtin.json_codecs.create("stdlib", settings())
    assert codec.dumps(SAMPLE) == JSONResponse(SAMPLE).body


def test_stdlib_keeps_starlettes_own_response_class():
    assert json_response_class(builtin.json_codecs.create("stdlib", settings())) is JSONResponse


@pytest.mark.skipif(not HAS_ORJSON, reason="orjson not installed")
def test_a_non_stdlib_codec_renders_responses_through_itself():
    cls = json_response_class(builtin.json_codecs.create("orjson", settings()))
    assert cls is not JSONResponse and issubclass(cls, JSONResponse)
    assert cls(SAMPLE).body == builtin.json_codecs.create("orjson", settings()).dumps(SAMPLE)


@pytest.mark.skipif(HAS_ORJSON, reason="only when orjson is missing")
def test_orjson_without_the_package_says_how_to_install_it():
    with pytest.raises(AdapterError, match="pip install orjson"):
        builtin.json_codecs.create("orjson", settings())


# --- UpstreamClient contract ---------------------------------------------------------------

async def test_httpx_upstream_contract():
    client = builtin.upstream_clients.create("httpx", settings())
    assert isinstance(client, UpstreamClient)
    assert isinstance(client, httpx.AsyncClient)
    await client.aclose()


async def test_router_uses_an_injected_client_and_builds_one_by_default():
    from app.routing import Router

    mine = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    assert Router(client=mine).client is mine
    default = Router()
    assert isinstance(default.client, UpstreamClient)
    await mine.aclose()
    await default.close()


# --- Analytics contract --------------------------------------------------------------------

REQUEST = {"request_id": "r1", "method": "GET", "path": "/api/x", "status_code": 200,
           "response_time_ms": 4.2, "client_ip": "10.0.0.1", "api_key": None, "timestamp": "2026-11-10T00:00:00"}


@pytest.mark.parametrize("name", ["full", "off"])
def test_analytics_contract(name):
    engine = builtin.analytics_engines.create(name, settings())
    assert isinstance(engine, Analytics)
    assert isinstance(engine.process_request(dict(REQUEST)), dict)


def test_off_reports_nothing():
    assert builtin.analytics_engines.create("off", settings()).process_request(dict(REQUEST)) == {}


# --- RequestLogSink contract ---------------------------------------------------------------

@pytest.mark.parametrize("name", ["database", "null"])
async def test_request_log_sink_contract(name):
    sink = builtin.request_log_sinks.create(name, settings(request_log_mode="off"))
    assert isinstance(sink, RequestLogSink)
    await sink.start()
    sink.submit(object(), None)  # never raises, never blocks
    await sink.stop()
    assert sink.written >= 0 and sink.dropped >= 0 and sink.failed >= 0


async def test_null_sink_discards_everything():
    sink = builtin.request_log_sinks.create("null", settings())
    for _ in range(5):
        sink.submit(object(), None)
    assert (sink.written, sink.dropped, sink.failed) == (0, 0, 0)


# --- composition ---------------------------------------------------------------------------

def test_defaults_are_the_current_behaviour():
    a = build_adapters(settings())
    assert a.json_codec.name == "stdlib"
    assert isinstance(a.upstream, httpx.AsyncClient)
    assert type(a.analytics).__name__ == "AIService"
    assert type(a.request_log).__name__ == "RequestLogWriter"


def test_settings_pick_the_adapters():
    a = build_adapters(settings(analytics_adapter="off", request_log_sink="null"))
    assert type(a.analytics).__name__ == "NoAnalytics"
    assert type(a.request_log).__name__ == "NullRequestLogSink"


def test_a_bad_setting_fails_at_start_up_with_the_choices():
    with pytest.raises(AdapterError, match="request-log sink"):
        build_adapters(settings(request_log_sink="kafka"))


def test_the_gateway_uses_the_adapters_it_built():
    from app import main

    assert main.router.client is main.adapters.upstream
    assert main.request_log_writer is main.adapters.request_log
    assert main.ai_service is main.adapters.analytics
