"""Unit tests for gRPC, WebSocket and MQTT protocol handlers (no real sockets/brokers)."""
import asyncio
import json
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import grpc
import httpx
import pytest
from fastapi import WebSocketDisconnect

from app.exceptions import BackendError
from app.protocols import grpc_handler as gh
from app.protocols import mqtt_handler as mh
from app.protocols import websocket_handler as wh


# =====================================================================
# gRPC
# =====================================================================
@pytest.fixture
def handler(monkeypatch):
    h = gh.GRPCHandler("backend:50051")
    h.channel = MagicMock(name="channel")
    return h


def test_create_channel_options(monkeypatch):
    seen = {}
    monkeypatch.setattr(gh.grpc, "insecure_channel", lambda url, options: seen.update(url=url, opts=options) or "CH")
    h = gh.GRPCHandler("x:1")
    assert h.get_channel() == "CH"
    assert h.get_channel() == "CH"  # cached
    keys = [k for k, _ in seen["opts"]]
    assert "grpc.keepalive_time_ms" in keys
    h.channel = None
    h.create_channel({"max_receive_message_length": 5})
    assert ("grpc.max_receive_message_length", 5) in seen["opts"]


def test_close(handler):
    ch = handler.channel
    handler.close()
    ch.close.assert_called_once()
    assert handler.channel is None
    handler.close()  # idempotent


def _fake_reflection(monkeypatch):
    pkg = types.ModuleType("grpc_reflection")
    v1 = types.ModuleType("grpc_reflection.v1alpha")
    refl = types.ModuleType("grpc_reflection.v1alpha.reflection")
    pb2 = types.ModuleType("grpc_reflection.v1alpha.reflection_pb2")
    pb2.ServerReflectionStub = MagicMock()
    v1.reflection, v1.reflection_pb2 = refl, pb2
    pkg.v1alpha = v1
    for name, mod in [("grpc_reflection", pkg), ("grpc_reflection.v1alpha", v1),
                      ("grpc_reflection.v1alpha.reflection", refl),
                      ("grpc_reflection.v1alpha.reflection_pb2", pb2)]:
        monkeypatch.setitem(sys.modules, name, mod)


def test_call_unary_unsupported_for_unknown_service(handler, monkeypatch):
    _fake_reflection(monkeypatch)
    r = handler.call_unary("Other", "Method", {"a": 1})
    assert r["status"] == "unsupported"
    assert r["service"] == "Other" and r["method"] == "Method" and r["request_data"] == {"a": 1}
    assert "not forwarded" in r["message"]


def test_call_unary_unsupported_without_reflection(handler, monkeypatch):
    for name in ("grpc_reflection", "grpc_reflection.v1alpha", "grpc_reflection.v1alpha.reflection",
                 "grpc_reflection.v1alpha.reflection_pb2"):
        monkeypatch.setitem(sys.modules, name, None)  # forces ImportError
    r = handler.call_unary("Greeter", "SayHello", {})
    assert r["status"] == "unsupported" and "reflection is unavailable" in r["message"]


def _fake_greeter(monkeypatch, say_hello=None, say_many=None):
    pb2 = types.ModuleType("greeter_pb2")
    pb2.HelloRequest = lambda name: types.SimpleNamespace(name=name)
    grpc_mod = types.ModuleType("greeter_pb2_grpc")
    stub = MagicMock()
    if say_hello is not None:
        stub.SayHello.side_effect = say_hello
    if say_many is not None:
        stub.SayManyHellos.side_effect = say_many
    grpc_mod.GreeterStub = lambda ch: stub
    monkeypatch.setitem(sys.modules, "greeter_pb2", pb2)
    monkeypatch.setitem(sys.modules, "greeter_pb2_grpc", grpc_mod)
    monkeypatch.setattr(gh.os.path if hasattr(gh, "os") else __import__("os").path, "exists", lambda p: True)
    return stub


def test_call_unary_greeter_success(handler, monkeypatch):
    _fake_reflection(monkeypatch)
    stub = _fake_greeter(monkeypatch, say_hello=lambda req, timeout: types.SimpleNamespace(message=f"hi {req.name}"))
    r = handler.call_unary("Greeter", "SayHello", {"name": "Sid"}, timeout=5)
    assert r == {"status": "success", "message": "hi Sid", "service": "Greeter", "method": "SayHello"}
    assert stub.SayHello.call_args.kwargs["timeout"] == 5


class _RpcErr(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.UNAVAILABLE


def test_call_unary_rpc_error_becomes_backend_error(handler, monkeypatch):
    _fake_reflection(monkeypatch)
    def boom(req, timeout):
        raise _RpcErr()
    _fake_greeter(monkeypatch, say_hello=boom)
    with pytest.raises(BackendError):
        handler.call_unary("Greeter", "SayHello", {})


def test_call_unary_generic_stub_failure_falls_back_to_unsupported(handler, monkeypatch):
    _fake_reflection(monkeypatch)
    def boom(req, timeout):
        raise ValueError("bad stub")
    _fake_greeter(monkeypatch, say_hello=boom)
    assert handler.call_unary("Greeter", "SayHello", {})["status"] == "unsupported"


def test_call_unary_greeter_without_generated_modules(handler, monkeypatch):
    _fake_reflection(monkeypatch)
    monkeypatch.setattr("os.path.exists", lambda p: False)
    assert handler.call_unary("Greeter", "SayHello", {})["status"] == "unsupported"


def test_call_unary_channel_failure_reraises(monkeypatch):
    h = gh.GRPCHandler("x")
    monkeypatch.setattr(h, "get_channel", MagicMock(side_effect=RuntimeError("no chan")))
    with pytest.raises(RuntimeError):
        h.call_unary("S", "M", {})


def test_streaming(handler, monkeypatch):
    _fake_greeter(monkeypatch, say_many=lambda req, timeout: iter(
        [types.SimpleNamespace(message="a"), types.SimpleNamespace(message="b")]))
    out = handler.call_streaming("Greeter", "SayManyHellos", {"name": "x"})
    assert out == [{"status": "stream_chunk", "message": "a"}, {"status": "stream_chunk", "message": "b"}]
    with pytest.raises(ValueError):
        handler.call_streaming("Other", "Thing", {})


def test_streaming_requires_generated_modules(handler, monkeypatch):
    monkeypatch.setattr("os.path.exists", lambda p: False)
    with pytest.raises(RuntimeError):
        list(handler.iter_say_many_hellos({}))
    # path exists but import fails -> continue then RuntimeError
    monkeypatch.setattr("os.path.exists", lambda p: True)
    monkeypatch.setitem(sys.modules, "greeter_pb2", None)
    with pytest.raises(RuntimeError):
        list(handler.iter_say_many_hellos({}))


# =====================================================================
# WebSocket
# =====================================================================
def _ws(receive):
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.receive_text = AsyncMock(side_effect=receive)
    ws.receive_bytes = AsyncMock(return_value=b"\x01")
    ws.send_text = AsyncMock()
    ws.send_bytes = AsyncMock()
    return ws


async def test_ws_disconnect_cleans_up(monkeypatch):
    h = wh.WebSocketHandler("http://backend/")
    monkeypatch.setattr(h, "_forward_to_backend", AsyncMock(return_value=None))
    ws = _ws([json.dumps({"m": 1}), WebSocketDisconnect()])
    await h.handle_connection(ws, "/chat", client_id="c1")
    ws.accept.assert_awaited_once()
    h._forward_to_backend.assert_awaited_once()
    assert h._forward_to_backend.call_args.args[1] == "http://backend/chat"
    ws.send_text.assert_not_awaited()  # documented: backend response is never produced
    assert h.get_connection_count() == 0


async def test_ws_sends_response_when_present(monkeypatch):
    h = wh.WebSocketHandler("http://b")
    monkeypatch.setattr(h, "_forward_to_backend", AsyncMock())
    monkeypatch.setattr(h, "_get_backend_response", AsyncMock(return_value={"ok": True}))
    ws = _ws(['{"a":1}', WebSocketDisconnect()])
    await h.handle_connection(ws, "/p")
    ws.send_text.assert_awaited_once_with(json.dumps({"ok": True}))


async def test_ws_non_json_text_is_echoed_as_text_then_error_ends(monkeypatch):
    h = wh.WebSocketHandler("http://b")
    monkeypatch.setattr(h, "_forward_to_backend", AsyncMock())
    ws = _ws(["not-json", RuntimeError("socket died")])
    await h.handle_connection(ws, "/p", client_id="c")
    ws.send_text.assert_awaited_once_with("not-json")
    ws.receive_bytes.assert_not_awaited()
    assert "c" not in h.active_connections


async def test_ws_registers_connection_while_active(monkeypatch):
    h = wh.WebSocketHandler("http://b")
    seen = {}

    async def recv():
        seen["count"] = h.get_connection_count()
        raise WebSocketDisconnect()

    ws = _ws(None)
    ws.receive_text = recv
    await h.handle_connection(ws, "/p")
    assert seen["count"] == 1 and h.get_connection_count() == 0


async def test_ws_forward_to_backend():
    h = wh.WebSocketHandler("http://b")
    client = MagicMock()
    client.post = AsyncMock(return_value=MagicMock(status_code=200, json=lambda: {"r": 1}))
    assert await h._forward_to_backend(client, "http://b/x", {"m": 1}) == {"r": 1}
    client.post = AsyncMock(return_value=MagicMock(status_code=502))
    assert await h._forward_to_backend(client, "http://b/x", {}) is None
    client.post = AsyncMock(side_effect=httpx.ConnectTimeout("t"))
    assert await h._forward_to_backend(client, "http://b/x", {}) is None
    assert await h._get_backend_response(client, "http://b/x") is None


async def test_ws_broadcast():
    h = wh.WebSocketHandler("http://b")
    good = MagicMock(send_text=AsyncMock())
    h.active_connections = {"a": good}
    h.broadcast({"x": 1})
    await asyncio.sleep(0.01)
    good.send_text.assert_awaited_once_with(json.dumps({"x": 1}))
    bad = MagicMock()
    bad.send_text.side_effect = RuntimeError("closed")
    h.active_connections = {"bad": bad}
    h.broadcast({"x": 1})
    assert h.get_connection_count() == 0


async def test_websocket_route_closes_connection_documented():
    """Documented limitation: the gateway's WebSocket route closes every connection."""
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect as SDisc
    from app.main import app
    c = TestClient(app, raise_server_exceptions=False)
    with pytest.raises(SDisc):
        with c.websocket_connect("/ws/anything") as sock:
            sock.receive_text()


# =====================================================================
# MQTT
# =====================================================================
@pytest.fixture
def mclient(monkeypatch):
    client = MagicMock(name="paho")
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(mh.mqtt_client, "Client", factory)
    return client, factory


@pytest.mark.parametrize("url,expected", [
    ("mqtt://broker:1884", ("broker", 1884, False)),
    ("mqtts://secure:8883", ("secure", 8883, True)),
])
def test_mqtt_parse_url_explicit_port(url, expected):
    assert mh.MQTTHandler(url)._parse_broker_url() == expected


def test_mqtt_parse_url_default_port(monkeypatch):
    import app.config as cfg
    monkeypatch.setattr(cfg, "get_settings", lambda: types.SimpleNamespace(protocol_mqtt_default_port=1999))
    assert mh.MQTTHandler("mqtt://hostonly")._parse_broker_url() == ("hostonly", 1999, False)


def test_mqtt_client_id_default():
    assert mh.MQTTHandler("mqtt://h:1").client_id.startswith("gateway_")
    assert mh.MQTTHandler("mqtt://h:1", "cid").client_id == "cid"


def test_mqtt_connect_success_and_tls(mclient):
    client, factory = mclient
    h = mh.MQTTHandler("mqtts://h:8883", "cid")
    assert h.connect() is True
    client.tls_set.assert_called_once()
    client.connect.assert_called_once_with("h", 8883, keepalive=60)
    client.loop_start.assert_called_once()
    assert client.on_message == h._on_message
    h2 = mh.MQTTHandler("mqtt://h:1883")
    client.reset_mock()
    assert h2.connect() is True
    client.tls_set.assert_not_called()


def test_mqtt_connect_failure(mclient):
    client, _ = mclient
    client.connect.side_effect = ConnectionRefusedError("no broker")
    assert mh.MQTTHandler("mqtt://h:1").connect() is False
    assert mh.MQTTHandler("mqtt://h:notaport").connect() is False  # malformed port


def test_mqtt_callbacks_state():
    h = mh.MQTTHandler("mqtt://h:1")
    h._on_connect(None, None, {}, 0)
    assert h.connected is True
    h._on_connect(None, None, {}, 5)  # refused: state unchanged
    assert h.connected is True
    h._on_disconnect(None, None, 1)
    assert h.connected is False


def test_mqtt_survives_the_broker_dropping_the_connection():
    """The client speaks MQTT 5, and an MQTT 5 client hands the disconnect callback the properties
    too. Called the way the client's network loop calls it, the callback must not raise: an
    exception there ends that thread, after which the client never reconnects and every publish
    fails until the gateway is restarted."""
    from paho.mqtt import client as paho

    h = mh.MQTTHandler("mqtt://h:1")
    client = paho.Client(client_id="outage", protocol=paho.MQTTv5)
    client.on_connect, client.on_disconnect = h._on_connect, h._on_disconnect
    h.connected = True
    client._do_on_disconnect(7, None)
    assert h.connected is False
    # The reconnect the network loop then makes is announced the usual way.
    h._on_connect(client, None, {}, 0, None)
    assert h.connected is True


def test_mqtt_on_message_paths():
    h = mh.MQTTHandler("mqtt://h:1")
    got = []
    h.message_callbacks["t/1"] = lambda topic, data: got.append((topic, data))
    msg = lambda topic, payload: types.SimpleNamespace(topic=topic, payload=payload)
    h._on_message(None, None, msg("t/1", b'{"a": 1}'))
    h._on_message(None, None, msg("t/1", b"plain text"))
    assert got == [("t/1", {"a": 1}), ("t/1", {"raw": "plain text"})]
    h._on_message(None, None, msg("other", b"{}"))  # default handler
    h._on_message(None, None, msg("t/1", b"\xff\xfe"))  # undecodable -> swallowed
    h.message_callbacks["bad"] = lambda *a: 1 / 0
    h._on_message(None, None, msg("bad", b"{}"))  # callback error swallowed


def test_mqtt_subscribe_publish(mclient):
    client, _ = mclient
    h = mh.MQTTHandler("mqtt://h:1")
    assert h.subscribe("t") is False
    assert h.publish("t", {}) is False
    h.client, h.connected = client, True
    cb = MagicMock()
    assert h.subscribe("t", cb, qos=2) is True
    client.subscribe.assert_called_with("t", qos=2)
    assert h.message_callbacks["t"] is cb
    assert h.subscribe("u") is True and "u" not in h.message_callbacks
    client.subscribe.side_effect = RuntimeError
    assert h.subscribe("v") is False

    client.publish.return_value = types.SimpleNamespace(rc=mh.mqtt_client.MQTT_ERR_SUCCESS)
    assert h.publish("t", {"a": 1}, qos=0, retain=True) is True
    client.publish.assert_called_with("t", '{"a": 1}', qos=0, retain=True)
    client.publish.return_value = types.SimpleNamespace(rc=4)
    assert h.publish("t", {}) is False
    assert h.publish("t", {"x": object()}) is False  # not JSON-serialisable
    client.publish.side_effect = OSError
    assert h.publish("t", {}) is False


def test_mqtt_disconnect(mclient):
    client, _ = mclient
    h = mh.MQTTHandler("mqtt://h:1")
    h.disconnect()  # no client: no-op
    h.client, h.connected = client, True
    h.disconnect()
    client.loop_stop.assert_called_once()
    client.disconnect.assert_called_once()
    assert h.connected is False


def test_mqtt_gateway(mclient):
    client, _ = mclient
    g = mh.MQTTGateway("mqtt://h:1")
    g.add_route("a/#", "http://svc1")
    g.add_route("b/+", "http://svc2")
    assert g.topic_routes == {"a/#": "http://svc1", "b/+": "http://svc2"}
    g.handler.connected = True  # emulate on_connect having fired
    assert g.start() is True
    assert [c.args[0] for c in client.subscribe.call_args_list] == ["a/#", "b/+"]
    g.stop()
    client.loop_stop.assert_called_once()
    client.connect.side_effect = OSError
    assert mh.MQTTGateway("mqtt://h:1").start() is False
