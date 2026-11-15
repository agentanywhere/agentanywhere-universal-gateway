"""Conformance of the JSON-to-gRPC bridge under `/grpc`: what the data plane would do with a body
against what the Python gateway does.

Run from the repository root, after `cargo build --release` in dataplane/:

    python dataplane/conformance/bridge.py [cases] [seed]

The data plane serves a body posted to the bridge itself only when it is certain what the Python gateway
would do with it, and hands every other request to the Python gateway. What the Python gateway does
depends on its installation (whether the gRPC reflection package is there, and whether the sample
service's generated code is), so every body is tried in three installations. The property that must
hold for every body in each of them is:

    the data plane answers with some JSON   =>  the Python gateway answers 200 with exactly those bytes
                                                 and calls nothing
    the data plane calls SayHello(name)     =>  the Python gateway calls SayHello with that name
    the data plane streams SayManyHellos    =>  the Python gateway streams SayManyHellos with that name

This script generates bodies (ordinary ones, every spelling of a number and of a string, members named
twice, other encodings, broken JSON, services, methods and `stream` values of every kind), runs the
Python gateway's own handler on each with a gRPC channel that records what is called, asks
`dataplane --judge-bridges` about the same bodies, and fails on any body that breaks the property.
"""
import asyncio
import json
import os
import random
import subprocess
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import publish as shared  # noqa: E402 - the generators of JSON values, and the gateway set up to be called

from fastapi.responses import JSONResponse, StreamingResponse  # noqa: E402

from app.protocols import GRPCHandler  # noqa: E402

gateway, ROOT, BIN = shared.gateway, shared.ROOT, shared.BIN
STUBS = str(ROOT / "samples/backends/grpc")
sys.path.insert(0, STUBS)
import greeter_pb2  # noqa: E402

REFLECTION = ["grpc_reflection", "grpc_reflection.v1alpha", "grpc_reflection.v1alpha.reflection", "grpc_reflection.v1alpha.reflection_pb2"]
INSTALLATIONS = [(False, True), (True, True), (True, False)]  # (reflection package, generated code), in the judge's order


class Channel:
    """A gRPC channel that calls nothing and notes what would be called."""

    def __init__(self):
        self.called = None

    def unary_unary(self, path, **_):
        def call(request, timeout=None):
            self.called = (path, request.name, timeout)
            return greeter_pb2.HelloReply(message="hi")
        return call

    def unary_stream(self, path, **_):
        def call(request, timeout=None):
            self.called = (path, request.name, timeout)
            return iter([greeter_pb2.HelloReply(message="a"), greeter_pb2.HelloReply(message="b")])
        return call


class Posted:
    """As much of a request as the handler reads before it reaches the bridge."""
    method = "POST"

    def __init__(self, body):
        self.body, self.state = body, types.SimpleNamespace(request_id="conformance")

    async def json(self):
        return json.loads(self.body)


def install(reflection, stubs):
    for name in REFLECTION:
        sys.modules.pop(name, None)
        # `None` makes the import fail, as it does where the package is not installed.
        sys.modules[name] = types.ModuleType(name) if reflection else None
    if reflection:
        sys.modules["grpc_reflection.v1alpha"].reflection_pb2 = sys.modules["grpc_reflection.v1alpha.reflection_pb2"]
    real = install.exists
    os.path.exists = real if stubs else (lambda p: False if str(p).rstrip("/").endswith("samples/backends/grpc") else real(p))


install.exists = os.path.exists


async def answered(channel, body):
    channel.called = None
    try:
        answer = await gateway.gateway_proxy(Posted(body), "grpc/call")
        if isinstance(answer, StreamingResponse):
            lines = b"".join([chunk async for chunk in answer.body_iterator])
            if answer.status_code == 200 and answer.media_type == "application/x-ndjson" and channel.called \
                    and channel.called[::2] == ("/greeter.Greeter/SayManyHellos", 60) \
                    and lines == b'{"status": "stream_chunk", "message": "a"}\n{"status": "stream_chunk", "message": "b"}\n':
                return f"hellos:{channel.called[1].encode().hex()}"
            return None
    except BaseException:  # noqa: BLE001 - an error answer or a crash: not a 200 either way
        return None
    if not isinstance(answer, JSONResponse) or answer.status_code != 200:
        return None
    if channel.called is None:
        return f"answer:{answer.body.hex()}"
    if channel.called[::2] == ("/greeter.Greeter/SayHello", 30) and answer.body == b'{"status":"success","message":"hi","service":"Greeter","method":"SayHello"}':
        return f"hello:{channel.called[1].encode().hex()}"
    return None


def body(rng):
    blank, value, string = shared.blank, shared.value, shared.string
    members = []
    if rng.random() < 0.6:
        members.append(f'"service"{blank(rng)}:{blank(rng)}' + rng.choice(['"Greeter"', '"Greeter"', '"Greeter"', '"greeter"', '"S"', '"pkg.Svc"', '"caf\\u00e9"', string(rng), "null", "5", '""', "[]", '"Gr\\u0065eter"']))
    if rng.random() < 0.7:
        members.append(f'"method"{blank(rng)}:{blank(rng)}' + rng.choice(['"SayHello"', '"SayHello"', '"SayManyHellos"', '"SayManyHellos"', '"M"', string(rng), "null", "1", '""', '"Say\\u0048ello"']))
    if rng.random() < 0.5:
        members.append(f'"stream"{blank(rng)}:{blank(rng)}' + rng.choice(["true", "true", "true", "false", "null", "1", "0", '"yes"', '""', "[]", "[0]", "{}", "1.0", "tru"]))
    if rng.random() < 0.85:
        roll = rng.random()
        if roll < 0.5:
            name = rng.choice(['"Ada"', '"World"', '""', '"caf\\u00e9 \\ud83d\\ude00"', '"é☃"', string(rng, 0.3), "null", "5", "true", '["x"]', '{"a":1}', '"\\u0000"', '"\\ud800"'])
            extra = rng.choice(["", "", f',"other":{value(rng)}', ',"name":"again"'])
            data = "{" + blank(rng) + rng.choice(['"name"', '"name"', '"n\\u0061me"', '"Name"']) + f"{blank(rng)}:{blank(rng)}{name}{extra}" + blank(rng) + "}"
        else:
            data = value(rng)
        members.append(f'"data"{blank(rng)}:{blank(rng)}{data}')
    if rng.random() < 0.15:
        members.append(rng.choice(['"extra":' + value(rng), '"service":"Again"', '"stream":true', '"data":{}', '"\\u0064ata":{"name":"escaped"}', '"metho\\u0064":"SayHello"']))
    rng.shuffle(members)
    text = blank(rng) + "{" + blank(rng) + ("," + blank(rng)).join(members) + blank(rng) + "}" + blank(rng)
    roll = rng.random()
    if roll < 0.03:
        return rng.choice(["﻿" + text, text + "x", text[:-1], "[" + text + "]", text + text, "", "null", '"x"', "nope", "[]", "5"]).encode("utf-8", "surrogatepass")
    if roll < 0.05:
        return text.encode(rng.choice(["utf-16", "utf-16-le", "utf-32", "latin-1", "utf-8-sig"]), "replace")
    if roll < 0.06:
        raw = bytearray(text.encode("utf-8", "replace"))
        if raw:
            raw[rng.randrange(len(raw))] = rng.choice([0xff, 0xc0, 0x80, 0x00, 0xed])
        return bytes(raw)
    return text.encode("utf-8", "replace")


# What clients ordinarily send. The data plane must serve every one of these itself, in every installation.
ORDINARY = [json.dumps(b, ensure_ascii=ascii, separators=seps).encode() for ascii in (True, False) for seps in ((", ", ": "), (",", ":")) for b in [
    {},
    {"service": "Greeter", "method": "SayHello", "data": {"name": "Ada"}},
    {"data": {"name": "naïve café ☕ 😀"}},
    {"service": "Greeter", "method": "SayManyHellos", "stream": True, "data": {"name": "Ada"}},
    {"service": "Greeter", "method": "SayManyHellos", "stream": True},
    {"service": "orders.Orders", "method": "Get", "data": {"id": 7, "tags": ["a", "é"], "price": 12.5, "deep": {"x": [None, True, 1e21]}}},
    {"service": "S", "method": "M", "data": "text \"quoted\" \\ back\nline", "stream": False},
    {"service": "S", "method": "M", "data": [1, 2.25, -3e-9, 12345678901234567890123]},
]]
# With no generated code the Python gateway's stream breaks off at once, which is its own to do.
STREAMS = {b for b in ORDINARY if json.loads(b).get("stream")}


def main() -> int:
    if not BIN.exists():
        print(f"build first: cargo build --release  (in {BIN.parent.parent.parent})")
        return 2
    total = int(sys.argv[1]) if len(sys.argv) > 1 else 20_000
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 20261115
    rng = random.Random(seed)
    handler = GRPCHandler("127.0.0.1:1")
    handler.channel = channel = Channel()
    gateway.grpc_handler = handler

    bodies = list(ORDINARY)
    while len(bodies) < total:
        bodies.append(body(rng))
    expected = []
    for reflection, stubs in INSTALLATIONS:
        install(reflection, stubs)
        expected.append([asyncio.run(answered(channel, b)) for b in bodies])
    install(False, True)

    run = subprocess.run([str(BIN), "--judge-bridges"], input=("\n".join(b.hex() for b in bodies) + "\n").encode(), capture_output=True, timeout=600)
    lines = run.stdout.decode().split("\n")[:-1]
    if run.returncode != 0 or len(lines) != len(bodies):
        print("dataplane --judge-bridges failed:", run.stderr.decode()[-500:], f"{len(lines)} answers for {len(bodies)} bodies")
        return 2

    def show(raw):
        return f"{raw[:200]!r}{'...' if len(raw) > 200 else ''}"

    failed = False
    for which, (reflection, stubs) in enumerate(INSTALLATIONS):
        wrong, needless, slow_ordinary, kinds = [], [], [], {"answer": 0, "hello": 0, "hellos": 0}
        for i, (raw, line) in enumerate(zip(bodies, lines)):
            got, want = line.split(" ")[which], expected[which][i]
            if got == "ask":
                if want is not None:
                    needless.append(raw)
                    if i < len(ORDINARY):
                        slow_ordinary.append(raw)
            elif got == want:
                kinds[got.split(":")[0]] += 1
            else:
                wrong.append((raw, want, got))
        served = sum(e is not None for e in expected[which])
        print(f"\nreflection package {'installed' if reflection else 'missing'}, generated code {'found' if stubs else 'missing'} "
              f"({len(bodies)} bodies, seed {seed}): Python serves {served}")
        print(f"data plane: answers {kinds['answer']} itself, calls SayHello for {kinds['hello']}, streams {kinds['hellos']}, leaves {len(needless)} to Python that Python serves (correct, slower)")
        for raw in needless[:3]:
            print("   ", show(raw))
        checks = [(sum(kinds.values()) == 0, "bodies are served without asking"),
                  (bool(slow_ordinary), "ordinary bodies are served without asking"),
                  (bool(wrong), "whatever the data plane does, Python does the same")]
        if reflection and stubs:
            checks.append((kinds["hello"] == 0 or kinds["hellos"] == 0, "calls and streams are among them"))
        for bad, name in checks:
            print(f"{'FAIL' if bad else 'PASS'}  {name}")
            failed |= bad
        for raw in slow_ordinary:
            print("   ", show(raw))
        for raw, want, got in wrong[:20]:
            print("   ", show(raw), "\n        python:", want and want[:300], "\n        rust:  ", got[:300])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
