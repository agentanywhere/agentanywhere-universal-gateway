"""Tests for the plugin system."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.plugins import (
    AuthProviderPlugin,
    HookPlugin,
    Plugin,
    PluginContext,
    PluginMetadata,
    PluginRegistry,
    PluginType,
    ProtocolPlugin,
    TransformPlugin,
    load_plugins,
    plugin_registry,
)
from app.plugins.builtin import (
    AuditLoggerPlugin,
    HeaderInjectorPlugin,
    RequestIDPlugin,
    StaticTokenAuthPlugin,
)
from app.plugins.loader import PluginLoader
from app.plugins.registry import PluginError


@pytest.fixture(autouse=True)
def _clean_registry():
    plugin_registry.clear()
    yield
    plugin_registry.clear()


# ---------------------------------------------------------------------
# Base classes
# ---------------------------------------------------------------------


def test_plugin_metadata_to_dict():
    md = PluginMetadata(name="x", version="2.0", description="d", tags=["t"])
    data = md.to_dict()
    assert data["name"] == "x"
    assert data["version"] == "2.0"
    assert data["tags"] == ["t"]


def test_plugin_context_to_dict():
    ctx = PluginContext(method="GET", path="/foo", headers={"H": "v"}, request_id="r1")
    out = ctx.to_dict()
    assert out["method"] == "GET"
    assert out["path"] == "/foo"
    assert out["headers"] == {"H": "v"}


def test_plugin_default_health():
    class P(Plugin):
        metadata = PluginMetadata(name="p", version="0.1")

    p = P()
    h = p.health()
    assert h["name"] == "p"
    assert h["status"] == "ok"


# ---------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------


class _Counter:
    setup_calls = 0
    teardown_calls = 0


class CountingPlugin(Plugin):
    metadata = PluginMetadata(name="counter", version="1.0", plugin_type=PluginType.GENERIC)

    def setup(self):
        _Counter.setup_calls += 1

    def teardown(self):
        _Counter.teardown_calls += 1


def test_registry_register_and_unregister():
    _Counter.setup_calls = 0
    _Counter.teardown_calls = 0
    reg = PluginRegistry()
    reg.register(CountingPlugin())
    assert len(reg.list()) == 1
    assert _Counter.setup_calls == 1
    assert reg.unregister("counter") is True
    assert _Counter.teardown_calls == 1
    assert reg.unregister("counter") is False  # already gone


def test_registry_duplicate_without_replace_raises():
    reg = PluginRegistry()
    reg.register(CountingPlugin())
    with pytest.raises(PluginError):
        reg.register(CountingPlugin())


def test_registry_replace_calls_teardown():
    _Counter.setup_calls = 0
    _Counter.teardown_calls = 0
    reg = PluginRegistry()
    reg.register(CountingPlugin())
    reg.register(CountingPlugin(), replace=True)
    assert _Counter.setup_calls == 2
    assert _Counter.teardown_calls == 1


def test_registry_set_enabled():
    reg = PluginRegistry()
    reg.register(CountingPlugin())
    assert reg.set_enabled("counter", False) is True
    assert reg.get("counter").enabled is False
    assert reg.set_enabled("missing", True) is False


# ---------------------------------------------------------------------
# Transform plugins
# ---------------------------------------------------------------------


class StampHeader(TransformPlugin):
    metadata = PluginMetadata(name="stamp", plugin_type=PluginType.TRANSFORM)

    def transform_request(self, ctx: PluginContext):
        ctx.headers = {**ctx.headers, "X-Stamped": "yes"}
        return ctx

    def transform_response(self, ctx: PluginContext):
        ctx.headers = {**ctx.headers, "X-Resp": "1"}
        return ctx


def test_transform_request_chain():
    reg = PluginRegistry()
    reg.register(StampHeader())
    out = reg.transform_request(PluginContext(method="GET", path="/x"))
    assert out.headers["X-Stamped"] == "yes"


def test_transform_response_chain():
    reg = PluginRegistry()
    reg.register(StampHeader())
    out = reg.transform_response(PluginContext(method="GET", path="/x"))
    assert out.headers["X-Resp"] == "1"


def test_transform_request_skips_disabled():
    reg = PluginRegistry()
    reg.register(StampHeader())
    reg.set_enabled("stamp", False)
    out = reg.transform_request(PluginContext(method="GET", path="/x"))
    assert "X-Stamped" not in out.headers


def test_transform_request_swallows_exception():
    class Boom(TransformPlugin):
        metadata = PluginMetadata(name="boom", plugin_type=PluginType.TRANSFORM)

        def transform_request(self, ctx):
            raise RuntimeError("nope")

    reg = PluginRegistry()
    reg.register(Boom())
    reg.transform_request(PluginContext())  # must not raise
    assert reg.stats("boom")["errors"] == 1


# ---------------------------------------------------------------------
# Auth provider plugins
# ---------------------------------------------------------------------


class AlwaysAllow(AuthProviderPlugin):
    metadata = PluginMetadata(name="allow-all", plugin_type=PluginType.AUTH_PROVIDER)

    def authenticate(self, ctx):
        return {"authenticated": True, "principal": "anon"}


def test_authenticate_short_circuits():
    reg = PluginRegistry()
    reg.register(AlwaysAllow())
    out = reg.authenticate(PluginContext())
    assert out is not None
    assert out["principal"] == "anon"


def test_authenticate_returns_none_when_no_provider_matches():
    class Reject(AuthProviderPlugin):
        metadata = PluginMetadata(name="reject", plugin_type=PluginType.AUTH_PROVIDER)

        def authenticate(self, ctx):
            return None

    reg = PluginRegistry()
    reg.register(Reject())
    assert reg.authenticate(PluginContext()) is None


# ---------------------------------------------------------------------
# Protocol plugins
# ---------------------------------------------------------------------


class FakeAMQP(ProtocolPlugin):
    metadata = PluginMetadata(name="amqp", plugin_type=PluginType.PROTOCOL)
    path_prefix = "/amqp/"

    def handle(self, ctx):
        return {"protocol": "amqp", "path": ctx.path}


def test_dispatch_protocol_matches_prefix():
    reg = PluginRegistry()
    reg.register(FakeAMQP())
    out = reg.dispatch_protocol(PluginContext(path="/amqp/queue"))
    assert out == {"protocol": "amqp", "path": "/amqp/queue"}


def test_dispatch_protocol_returns_none_when_no_match():
    reg = PluginRegistry()
    reg.register(FakeAMQP())
    assert reg.dispatch_protocol(PluginContext(path="/other")) is None


# ---------------------------------------------------------------------
# Hook plugins
# ---------------------------------------------------------------------


class TallyHook(HookPlugin):
    metadata = PluginMetadata(name="tally", plugin_type=PluginType.HOOK)

    def __init__(self, config=None):
        super().__init__(config)
        self.calls = []

    def hooks(self):
        return {
            "request_pre": self._pre,
            "request_post": self._post,
        }

    def _pre(self, ctx):
        self.calls.append(("pre", ctx.path))

    def _post(self, ctx):
        self.calls.append(("post", ctx.path))


def test_run_hook_invokes_matching_hooks():
    reg = PluginRegistry()
    plugin = TallyHook()
    reg.register(plugin)
    reg.run_hook("request_pre", PluginContext(path="/a"))
    reg.run_hook("request_post", PluginContext(path="/a"))
    assert plugin.calls == [("pre", "/a"), ("post", "/a")]


def test_run_hook_unknown_hook_no_op():
    reg = PluginRegistry()
    reg.register(TallyHook())
    assert reg.run_hook("does-not-exist") == []


# ---------------------------------------------------------------------
# Built-in plugins
# ---------------------------------------------------------------------


def test_builtin_request_id_adds_header():
    p = RequestIDPlugin()
    out = p.transform_request(PluginContext(headers={}, request_id="abc"))
    assert out.headers["X-Request-ID"] == "abc"


def test_builtin_request_id_does_not_override_existing():
    p = RequestIDPlugin()
    out = p.transform_request(PluginContext(headers={"X-Request-ID": "keep"}))
    assert out.headers["X-Request-ID"] == "keep"


def test_builtin_header_injector_disabled_by_default():
    p = HeaderInjectorPlugin()
    assert p.enabled is False


def test_builtin_header_injector_injects_when_configured():
    p = HeaderInjectorPlugin(config={"enabled": True, "request_headers": {"X-A": "1"}})
    out = p.transform_request(PluginContext(headers={}))
    assert out.headers["X-A"] == "1"


def test_builtin_audit_logger_disabled_by_default():
    p = AuditLoggerPlugin()
    assert p.enabled is False
    # Hooks still defined
    assert "request_pre" in p.hooks()


def test_builtin_static_token_auth_matches():
    p = StaticTokenAuthPlugin(config={"enabled": True, "token": "tok", "principal": "svc"})
    ctx = PluginContext(headers={"Authorization": "Bearer tok"})
    out = p.authenticate(ctx)
    assert out is not None
    assert out["principal"] == "svc"


def test_builtin_static_token_auth_rejects_wrong_token():
    p = StaticTokenAuthPlugin(config={"enabled": True, "token": "tok"})
    ctx = PluginContext(headers={"Authorization": "Bearer other"})
    assert p.authenticate(ctx) is None


def test_builtin_static_token_auth_no_token_configured():
    p = StaticTokenAuthPlugin(config={})
    assert p.authenticate(PluginContext(headers={"Authorization": "Bearer x"})) is None


# ---------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------


def test_loader_loads_builtins_into_registry():
    reg = PluginRegistry()
    loader = PluginLoader(registry=reg)
    result = loader.load()
    names = {p.name for p in reg.list()}
    assert "request-id" in names
    assert "header-injector" in names
    assert "audit-logger" in names
    assert "static-token-auth" in names
    assert "request-id" in result.loaded


def test_loader_respects_disabled_list():
    reg = PluginRegistry()
    loader = PluginLoader(registry=reg, disabled=["request-id"])
    loader.load()
    names = {p.name for p in reg.list()}
    assert "request-id" not in names


def test_loader_loads_directory_plugin(tmp_path: Path, monkeypatch):
    plugin_file = tmp_path / "myplug.py"
    plugin_file.write_text(
        "from app.plugins import TransformPlugin, PluginMetadata, PluginType, PluginContext\n"
        "class ExternalPlugin(TransformPlugin):\n"
        "    metadata = PluginMetadata(name='external', plugin_type=PluginType.TRANSFORM)\n"
        "    def transform_request(self, ctx):\n"
        "        ctx.headers = {**ctx.headers, 'X-External': '1'}\n"
        "        return ctx\n"
    )
    reg = PluginRegistry()
    loader = PluginLoader(registry=reg, plugin_paths=[str(tmp_path)])
    loader.load()
    names = {p.name for p in reg.list()}
    assert "external" in names

    out = reg.transform_request(PluginContext(headers={}))
    assert out.headers["X-External"] == "1"


def test_loader_passes_plugin_config():
    reg = PluginRegistry()
    cfg = {"header-injector": {"enabled": True, "request_headers": {"X-Cfg": "y"}}}
    loader = PluginLoader(registry=reg, plugin_configs=cfg)
    loader.load()
    inj = reg.get("header-injector")
    assert inj is not None
    assert inj.enabled is True
    out = inj.transform_request(PluginContext(headers={}))
    assert out.headers["X-Cfg"] == "y"


def test_load_plugins_helper_uses_default_registry():
    plugin_registry.clear()
    result = load_plugins(disabled=["audit-logger"])
    assert "audit-logger" not in result.loaded
    assert any(p.name == "request-id" for p in plugin_registry.list())


def test_registry_to_dict_shape():
    reg = PluginRegistry()
    reg.register(StampHeader())
    out = reg.to_dict()
    assert isinstance(out, list) and out
    item = out[0]
    assert item["name"] == "stamp"
    assert item["enabled"] is True
    assert "stats" in item
    assert "health" in item
