"""Conformance of MQTT publishing: what the data plane would publish against what the Python gateway publishes.

Run from the repository root, after `cargo build --release` in dataplane/:

    python dataplane/conformance/publish.py [cases] [seed]

For `POST /mqtt/publish` the data plane publishes a message itself only when it is certain which message
the Python gateway would publish for the same body, and hands every other request to the Python gateway.
So the property that must hold for every body is:

    the data plane says what to publish  =>  the Python gateway answers 200 and publishes exactly that
                                             (the same topic, quality of service and payload bytes).

This script generates bodies (ordinary ones, every spelling of a number and of a string, members named
twice, other encodings, broken JSON, topics and qos values of every kind), runs the Python gateway's own
handler on each with its MQTT client recording what it would send, asks `dataplane --judge-publishes`
about the same bodies, and fails on any body that breaks the property. It also reports how often the
data plane leaves to Python a body Python publishes (correct, only slower), and fails if an ordinary
body is among those.
"""
import asyncio
import json
import os
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SECRET_KEY", "publish-conformance-" + "x" * 32)
os.environ.setdefault("LOG_LEVEL", "CRITICAL")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("REDIS_ENABLED", "false")
os.environ.setdefault("STARTUP_BANNER", "false")
os.environ.pop("MQTT_BROKER_URL", None)

from paho.mqtt import client as paho  # noqa: E402

from app import main as gateway  # noqa: E402
from app.protocols import MQTTHandler  # noqa: E402

BIN = ROOT / "dataplane/target/release/dataplane"


class Recording(paho.Client):
    """The gateway's MQTT client, with the one step that writes to the broker replaced by a note of
    what would be written. Everything before it (the checks on topic, qos and payload) is the client's own."""

    def __init__(self):
        super().__init__(client_id="conformance", protocol=paho.MQTTv5)
        self._max_inflight_messages = 0
        self.sent = None
        self.asked = None

    def _send_subscribe(self, dup, topics, properties=None):
        self.asked = (bool(dup), [(bytes(t), o.pack()) for t, o in topics], properties)
        return (paho.MQTT_ERR_SUCCESS, 1)

    def _send_publish(self, mid, topic, payload=b"", qos=0, retain=False, dup=False, info=None, properties=None):
        self.sent = (bytes(topic), int(qos), bytes(payload), bool(retain), bool(dup), properties)
        self._out_messages.clear()
        return paho.MQTT_ERR_SUCCESS


class Posted:
    """As much of a request as the handler reads: its body, parsed the way Starlette parses one."""

    def __init__(self, body):
        self.body = body

    async def json(self):
        return json.loads(self.body)


def python_publishes(handler, body):
    """What the Python gateway publishes for a body: (topic, qos, payload), or None when it answers anything but 200."""
    handler.client.sent = None
    try:
        answer = asyncio.run(gateway.mqtt_publish(Posted(body)))
    except BaseException:  # noqa: BLE001 - an HTTP error or a crash: not published either way
        return None
    sent = handler.client.sent
    if sent is None or sent[3:] != (False, False, None) or answer != {"status": "published", "topic": sent[0].decode("utf-8")}:
        return None
    return sent[:3]


def python_subscribes(handler, body):
    """What the Python gateway subscribes to for a body: (topic filter, qos), or None when it answers anything but 200."""
    handler.client.asked = None
    handler.client._sock = object()  # the client asks the broker only while it has a connection
    try:
        answer = asyncio.run(gateway.mqtt_subscribe(Posted(body)))
    except BaseException:  # noqa: BLE001 - an HTTP error or a crash: not subscribed either way
        return None
    finally:
        handler.client._sock = None
    asked = handler.client.asked
    # One filter, with no option but the quality of service, and no properties.
    if asked is None or asked[0] or asked[2] is not None or len(asked[1]) != 1 or asked[1][0][1][0] > 2:
        return None
    filter_, options = asked[1][0]
    if answer != {"status": "subscribed", "topic": filter_.decode("utf-8")}:
        return None
    return filter_, options[0]


NUMBERS = ["0", "-0", "1", "-7", "42", "1.5", "-0.0", "0.1", "1e22", "1E16", "1e15", "1e-7", "0.0001", "0.00001", "1.0E+2", "0.30000000000000004",
           "5e-324", "1e400", "-1E400", "2.2250738585072014e-308", "1.7976931348623157e308", "123456789012345678.0", "9007199254740993",
           "123456789012345678901234567890", "1e0", "0e0", "-0e-0", "0.1e1", "100e-2", "4.35", "0.000001", "1e16", "9999999999999998.0",
           "9.999999999999999e22", "NaN", "Infinity", "-Infinity", "01", "1.", ".5", "+1", "0x10", "1e", "--1"]
CHARS = ["a", "Z", " ", "/", "~", "\x7f", "é", "☃", "\u2028", "\ufeff", "😀", "\U0001F9D1", "ü", "中", "'", "<", "&"]
ESCAPES = ['\\"', "\\\\", "\\/", "\\b", "\\f", "\\n", "\\r", "\\t", "\\u0041", "\\u00e9", "\\u00E9", "\\u2603", "\\ud83d\\ude00", "\\uD83D\\uDE00",
           "\\ud800", "\\udc00", "\\ude00\\ud83d", "\\u0000", "\\u001f", "\\u007f", "\\x41", "\\u12", "\\'", "\t", "\x00", "\n"]


def blank(rng):
    return rng.choice(["", "", "", " ", "\n", "\t", "\r\n", "  "]) if rng.random() < 0.3 else ""


def number(rng):
    roll = rng.random()
    if roll < 0.35:
        return rng.choice(NUMBERS)
    if roll < 0.55:
        return repr(rng.uniform(-1e6, 1e6))
    if roll < 0.7:
        return repr(rng.random() * 10.0 ** rng.randrange(-320, 308))
    if roll < 0.76:
        # Numbers exactly halfway between two short spellings, and at the edges between powers of two.
        return rng.choice([f"{rng.randrange(10 ** 14, 10 ** 16)}.{rng.choice(['25', '5', '75', '125', '375'])}",
                           "%.25g" % (rng.randrange(2 ** 52, 2 ** 53) * 2.0 ** rng.randrange(-80, 80)),
                           "%.20e" % (2.0 ** rng.randrange(-1074, 1023)), repr(2.0 ** rng.randrange(-1074, 1023) * (1 - 2 ** -53))])
    if roll < 0.85:
        digits = "".join(rng.choice("0123456789") for _ in range(rng.randrange(1, 25)))
        return f"{rng.choice(['', '-'])}{rng.choice('123456789')}{digits}{rng.choice(['', '', '.' + digits[::-1], 'e' + str(rng.randrange(-30, 30)), 'E+' + str(rng.randrange(0, 400))])}"
    return str(rng.randrange(-10 ** rng.randrange(1, 40), 10 ** rng.randrange(1, 40)))


def string(rng, odd=0.15):
    parts = []
    for _ in range(rng.randrange(0, 8)):
        roll = rng.random()
        parts.append(rng.choice(ESCAPES) if roll < odd else rng.choice(CHARS) if roll < 0.5 else rng.choice("abcxyz0189_- "))
    return '"' + "".join(parts) + '"'


def value(rng, depth=0):
    roll = rng.random()
    if depth > rng.choice([2, 3, 5]) or roll < 0.3:
        return rng.choice([number(rng), number(rng), string(rng), "true", "false", "null"])
    if roll < 0.4:
        return rng.choice(["[" * 70 + "]" * 70, "{}", "[]", "[ ]", "{ }", "tru", "nul", "", "'x'", "[1,]", "{\"a\":1,}", "[1 2]"])
    if roll < 0.7:
        return "[" + blank(rng) + ("," + blank(rng)).join(value(rng, depth + 1) for _ in range(rng.randrange(0, 4))) + blank(rng) + "]"
    names = [rng.choice(['"a"', '"b"', '"a"', string(rng, 0.05)]) if rng.random() < 0.3 else f'"k{i}"' for i in range(rng.randrange(0, 4))]
    return "{" + blank(rng) + ("," + blank(rng)).join(f"{n}{blank(rng)}:{blank(rng)}{value(rng, depth + 1)}" for n in names) + blank(rng) + "}"


def topic(rng):
    roll = rng.random()
    if roll < 0.6:
        return '"' + "/".join(rng.choice(["sensors", "t", "a", "site-7", "temp", "$SYS", "é", "x y"]) for _ in range(rng.randrange(1, 4))) + '"'
    if roll < 0.8:
        return string(rng, 0.3)
    if roll < 0.9:
        # Filters: wildcards where a subscription may have them, and where it may not.
        levels = [rng.choice(["a", "b", "+", "+", "#", "a+", "+a", "a#", "#a", "++", "", "é", "x y"]) for _ in range(rng.randrange(1, 5))]
        if rng.random() < 0.5 and "#" in levels:
            levels = [l for l in levels if l != "#"] + ["#"]
        return json.dumps("/".join(levels), ensure_ascii=rng.random() < 0.5)
    return rng.choice(['""', "null", "5", "0", "true", "false", "[]", "{}", '["t"]', '"a/+/b"', '"a/#"', '"#"', '"+"', '"' + "x" * 65535 + '"',
                       '"' + "x" * 65536 + '"', '"' + "é" * 32768 + '"', '"\\u0000"', '"/"', '" "', '"\\ud800"', '"\\ud83d\\ude00"', '"\\u0023"', '"\\u002b"'])


def body(rng):
    members = []
    if rng.random() < 0.95:
        members.append(f'"topic"{blank(rng)}:{blank(rng)}{topic(rng)}')
    if rng.random() < 0.85:
        members.append(f'"payload"{blank(rng)}:{blank(rng)}{value(rng)}')
    if rng.random() < 0.5:
        qos = rng.choice(["0", "1", "2", "0", "1", "2", "3", "-1", "-0", "1.0", "2e0", '"1"', "true", "false", "null", "10", "01", "[1]", "1 ", " 2"])
        members.append(f'"qos"{blank(rng)}:{blank(rng)}{qos}')
    if rng.random() < 0.2:
        members.append(rng.choice(['"retain":true', '"extra":' + value(rng), '"topic":"again"', '"qos":0', '"payload":1', '"\\u0074opic":"escaped"', '"top\\u0069c":"t2"']))
    rng.shuffle(members)
    text = blank(rng) + "{" + blank(rng) + ("," + blank(rng)).join(members) + blank(rng) + "}" + blank(rng)
    roll = rng.random()
    if roll < 0.03:
        return rng.choice(["\ufeff" + text, text + "x", text[:-1], "[" + text + "]", text + text, "", "null", '"x"', "nope"]).encode("utf-8", "surrogatepass")
    if roll < 0.05:
        return text.encode(rng.choice(["utf-16", "utf-16-le", "utf-32", "latin-1", "utf-8-sig"]), "replace")
    if roll < 0.06:
        raw = bytearray(text.encode("utf-8", "replace"))
        if raw:
            raw[rng.randrange(len(raw))] = rng.choice([0xff, 0xc0, 0x80, 0x00, 0xed])
        return bytes(raw)
    return text.encode("utf-8", "replace")


# What clients ordinarily send. The data plane must publish every one of these itself.
ORDINARY = [json.dumps(b, ensure_ascii=ascii, separators=seps).encode() for ascii in (True, False) for seps in ((", ", ": "), (",", ":")) for b in [
    {"topic": "sensors/temp", "payload": {"celsius": 21.5, "unit": "°C", "ok": True}},
    {"topic": "t"},
    {"topic": "site/7/door", "payload": "open", "qos": 0},
    {"topic": "site/7/count", "payload": 12, "qos": 2},
    {"topic": "a/b/c", "payload": [1, 2.25, -3e-9, None, {"deep": {"er": ["x", {"y": 1e21}]}}], "qos": 1},
    {"topic": "notes", "payload": {"text": "naïve café ☕ 😀 \"quoted\" \\ back\nline\ttab", "empty": {}, "none": []}},
    {"topic": "big", "payload": {"n": 12345678901234567890123, "f": 0.1, "g": 1e-7, "h": 123456.789, "z": 0.0}},
]]


def main() -> int:
    if not BIN.exists():
        print(f"build first: cargo build --release  (in {BIN.parent.parent.parent})")
        return 2
    total = int(sys.argv[1]) if len(sys.argv) > 1 else 40_000
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 20261113
    rng = random.Random(seed)
    handler = MQTTHandler("mqtt://conformance:1883")
    handler.client, handler.connected = Recording(), True
    gateway.mqtt_handler = handler

    bodies = list(ORDINARY)
    while len(bodies) < total:
        bodies.append(body(rng))
    expected = [python_publishes(handler, b) for b in bodies]

    run = subprocess.run([str(BIN), "--judge-publishes"], input=("\n".join(b.hex() for b in bodies) + "\n").encode(), capture_output=True, timeout=600)
    if run.returncode != 0:
        print("dataplane --judge-publishes failed:", run.stderr.decode()[-500:])
        return 2
    lines = run.stdout.decode().split("\n")[:-1]
    if len(lines) != len(bodies):
        print(f"{len(lines)} answers for {len(bodies)} bodies")
        return 2

    def show(raw):
        return f"{raw[:200]!r}{'...' if len(raw) > 200 else ''}"

    wrong, needless, slow_ordinary, same = [], [], [], 0
    for i, (raw, want, line) in enumerate(zip(bodies, expected, lines)):
        if line == "ask":
            if want is not None:
                needless.append(raw)
                if i < len(ORDINARY):
                    slow_ordinary.append(raw)
            continue
        said = json.loads(line)
        got = (bytes.fromhex(said["topic"]), said["qos"], bytes.fromhex(said["payload"]))
        if got == want:
            same += 1
        else:
            wrong.append((raw, want, got))

    published = sum(e is not None for e in expected)
    print(f"{len(bodies)} bodies (seed {seed}): Python publishes {published}, answers something else for {len(bodies) - published}")
    print(f"data plane: publishes {same + len(wrong)} itself, leaves {len(bodies) - same - len(wrong)} to Python")
    print(f"left to Python although Python publishes it (correct, slower): {len(needless)}")
    for raw in needless[:5]:
        print("   ", show(raw))
    print(f"{'FAIL' if same == 0 else 'PASS'}  bodies are published without asking")
    print(f"{'FAIL' if slow_ordinary else 'PASS'}  ordinary bodies are published without asking")
    for raw in slow_ordinary:
        print("   ", show(raw))
    print(f"{'FAIL' if wrong else 'PASS'}  whatever the data plane publishes, Python publishes the same")
    for raw, want, got in wrong[:20]:
        print("   ", show(raw), "\n        python:", want and (want[0][:60], want[1], want[2][:200]), "\n        rust:  ", (got[0][:60], got[1], got[2][:200]))

    # The same bodies posted to /mqtt/subscribe: whatever the data plane subscribes to, Python subscribes to the same.
    plain = [json.dumps(b).encode() for b in ({"topic": "s/#"}, {"topic": "a/+/b", "qos": 0}, {"topic": "#", "qos": 2}, {"topic": "sensors/temp", "payload": {"x": 1}}, {"topic": "+/+"})]
    bodies = plain + bodies[len(ORDINARY):]
    expected = [python_subscribes(handler, b) for b in bodies]
    run = subprocess.run([str(BIN), "--judge-subscribes"], input=("\n".join(b.hex() for b in bodies) + "\n").encode(), capture_output=True, timeout=600)
    lines = run.stdout.decode().split("\n")[:-1]
    if run.returncode != 0 or len(lines) != len(bodies):
        print("dataplane --judge-subscribes failed:", run.stderr.decode()[-500:], f"{len(lines)} answers for {len(bodies)} bodies")
        return 2
    astray, left, slow_plain, alike = [], [], [], 0
    for i, (raw, want, line) in enumerate(zip(bodies, expected, lines)):
        if line == "ask":
            if want is not None:
                left.append(raw)
                if i < len(plain):
                    slow_plain.append(raw)
            continue
        said = json.loads(line)
        got = (bytes.fromhex(said["topic"]), said["qos"])
        if got == want:
            alike += 1
        else:
            astray.append((raw, want, got))
    subscribed = sum(e is not None for e in expected)
    print(f"\n{len(bodies)} bodies posted to subscribe: Python subscribes for {subscribed}, answers something else for {len(bodies) - subscribed}")
    print(f"data plane: subscribes for {alike + len(astray)} itself, leaves {len(bodies) - alike - len(astray)} to Python")
    print(f"left to Python although Python subscribes (correct, slower): {len(left)}")
    for raw in left[:5]:
        print("   ", show(raw))
    print(f"{'FAIL' if alike == 0 or slow_plain else 'PASS'}  ordinary subscriptions are made without asking")
    print(f"{'FAIL' if astray else 'PASS'}  whatever the data plane subscribes to, Python subscribes to the same")
    for raw, want, got in astray[:20]:
        print("   ", show(raw), "\n        python:", want, "\n        rust:  ", got)
    return 1 if wrong or slow_ordinary or same == 0 or astray or slow_plain or alike == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
