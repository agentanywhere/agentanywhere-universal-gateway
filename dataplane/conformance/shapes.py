"""Conformance of applied transformation rules: what the data plane makes of a request and of its
answer when rules apply, against the code the Python gateway runs for the same.

Run from the repository root, after `cargo build --release` in dataplane/:

    python dataplane/conformance/shapes.py [cases] [seed]

The Python gateway tells the data plane what a rule does only when the data plane can do exactly the
same (`app.dataplane_sync._rule_actions`): change headers, query parameters and the fields of a JSON
body of the request, and headers and the status of the answer. Any other rule is applied by the
Python gateway. So the property that must hold is:

    for every set of rules the data plane is told the actions of, and every exchange:
        the backend is asked for the path, and sent the headers, the query and the body, that the
        Python gateway would ask for and send, and
        the caller is given the headers and the status the Python gateway would give.

A body the data plane is not certain how Python would read or write, it hands over with its request
(it says `ask`), and so do no harm; the script fails if it does that for ordinary bodies.

A path rewritten by a regular expression gets a second, larger run of its own: expressions put
together at random from what the two engines share, each rewritten path compared with `re.sub`.
The two engines do not always find the same match for an expression both take, so which expressions
the data plane is told of (`app.dataplane_sync._path_actions`) is what this run checks.

This script generates rules as the admin API takes them and exchanges to apply them to. The Python
side is the gateway's own: its rule classes, the dictionaries it reads headers and a query into, its
HTTP client writing the request and its web framework writing the answer. The other side is
`dataplane --judge-shapes`, given the actions as the snapshot carries them. The script fails on any
difference, and if ordinary rules are not being described to the data plane at all.

Headers are compared as a receiver reads them: for each name, whatever its case, the values in order.
"""
import asyncio
from datetime import datetime
from types import SimpleNamespace
import json
import random
from urllib.parse import unquote_to_bytes
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from starlette.datastructures import Headers, QueryParams  # noqa: E402
from starlette.responses import Response  # noqa: E402

from app import routing  # noqa: E402
from app.routing import repeated_headers, repeated_query  # noqa: E402
from app.dataplane_sync import _path_actions, _rule_actions  # noqa: E402
from app.main import answer_from, router  # noqa: E402
from app.versioning import APIVersion, VersionStatus  # noqa: E402
from app.transformers import RequestTransformer, ResponseTransformer, TransformConfig, TransformationRule, _apply_path_rewrite, transformation_manager  # noqa: E402

BIN = ROOT / "dataplane/target/release/dataplane"
NAMES = ["x-a", "X-A", "X-a", "x-b", "X-B", "x-c", "X-C", "x-trace-me", "X-Trace-Me", "authorization", "Authorization",
         "cookie", "x-api-key", "X-Api-Key", "x_under", "X-Gateway-Internal", "x-gateway-internal", "etag", "ETag",
         "cache-control", "Cache-Control", "x-d", "x-e",
         # Headers the gateway itself writes after the rules, each under one exact spelling.
         "x-forwarded-for", "X-Forwarded-For", "X-FORWARDED-FOR", "x-protocol", "X-Protocol", "X-PROTOCOL", "traceparent", "Traceparent",
         "x-request-id", "X-Request-ID", "X-Request-Id", "x-response-time", "X-Response-Time"]
# Names and values the two sides might treat differently: a rule with one stays with Python.
KEPT = ["Host", "Content-Type", "User-Agent", "Accept-Version", "Access-Control-Allow-Origin", "Set-Cookie", "Accept",
        "X-API-Version", "x-ratelimit-limit", "bad name", "café", ""]
VALUES = ["v1", "v2", "yes", "", "a b", "text/plain; q=0.5", "Bearer abc.def", "1", "x=y; z=1", "~!@#$%^&*()"]
ODD_VALUES = [" lead", "trail ", "naïve", "two\nlines", "tab\there", 7, None]
KEYS = ["a", "b", "c", "page", "q", "A", "sort"]
WORDS = ["1", "two", "", "x-y_z.~", "A1", "10"]
ODD_WORDS = ["a b", "a&b", "café", "50%", 3]
# What the gateway tells a backend with a version on its way out, each under one exact spelling.
ANNOUNCED = ["warning", "Warning", "WARNING", "sunset", "Sunset"]
# What a caller may write in a query that is not a plain word: escapes (whole, cut short, not
# text once read), `+`, and the characters a URL keeps for itself.
ODD_PARTS = ["a+b", "+", "%20", "%7E", "%7e", "%zz", "%", "%4", "%2541", "%25", "%C3%A9", "caf%c3%a9", "a/b", "x;y",
             "a:b@c", "k=v", "=", "~", "a*b", "(1)", "!$'", "a,b", "?", "%00", "%26", "%3D", "a%20b", "%41"]
# Escapes that are not text once read: the Python gateway reads those alone.
NOT_TEXT = ["%ff", "%E2%82", "%80", "%C3", "%ED%A0%80", "%F0%9F%98", "%C0%AF", "%F4%90%80%80", "%E2%82%AC%ff", "%FE%FF"]
STATUSES = [200, 201, 202, 301, 400, 401, 404, 409, 500, 502, 503]
ODD_STATUSES = [204, 304, 99, 700, 0, "502"]


def pick(rng, usual, odd, odds=0.04):
    return rng.choice(odd) if rng.random() < odds else rng.choice(usual)


def some(rng, make, most=3):
    return [make() for _ in range(rng.choice([0, 0, 1, 1, 2, most]))]


PATHS = ["a", "b", "c", "a.b", "a.b.c", "meta", "meta.source", "meta.user", "user", "", "a..b", "é", "x y", "😀.a", "b.a"]
PARTS = ["a", "b", "c", "meta", "source", "user", "", "é", "x y", "😀"]
TEXTS = ["", "x", "é☃", "😀", "a\"b\\c", "line\nend", "\u007f\u0001", "</script>", "1"]
NUMBERS = [0, 1, -7, 10 ** 20, 1.5, -0.0, 0.1, 1e22, 1e-7, 123456789012345678.0, 2.5e-5, 100.0]


def a_value(rng, depth=0):
    kind = rng.random()
    if depth < 3 and kind < 0.25:
        return {rng.choice(PARTS): a_value(rng, depth + 1) for _ in range(rng.randint(0, 3))}
    if depth < 3 and kind < 0.35:
        return [a_value(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    return rng.choice([None, True, False, rng.choice(TEXTS), rng.choice(NUMBERS), rng.randint(-1000, 1000), rng.random() * 10 ** rng.randint(-8, 20)])


ODD_SET = [float("nan"), float("inf"), {1: "a"}, (1, 2), 10 ** 400, "\ud800", {"k": {"\udc00": 1}}, b"bytes"]
# Bodies that are not an object, are not JSON, or are JSON Python reads in a way of its own.
ODD_BODIES = [b"[1, 2]", b'"text"', b"5", b"null", b"true", b"not json", b"\xff\xfe{}", b'{"a":1,"a":{"b":2},"c":3}', b'{"a":NaN}',
              b'{"a":1e400}', b'{"a":-0,"b":1.50,"c":1E2}', b'{"a":"\\ud83d\\ude00\\ud800"}', b"\xef\xbb\xbf{}", b'{"a":' + b"[" * 70 + b"]" * 70 + b"}",
              b'{"a":' + b"9" * 400 + b"}", b" \n{\"a\" :\t1 }\r\n", b'{"a":1,}', b"{'a':1}", b'{"a":"\x01"}', b'{"a":{"b":null},"b":null}',
              b'{"a":600654557994792.25}', b'{"":{"":1}}', b"{}", b'{"a":1}{"b":2}', b'{"a":Infinity}', b'{"a":01}',
              # A JSON string that spells an object, which an answer is read a second time for.
              b'"{\\"a\\": 1, \\"user\\": \\"ana\\", \\"secret\\": 2}"', b' "{\\"meta\\": {\\"source\\": 1}}"', b'"[1, 2]"']


def a_body(rng):
    if rng.random() < 0.2:
        return rng.choice(ODD_BODIES)
    value = {rng.choice(PARTS): a_value(rng, 1) for _ in range(rng.randint(0, 5))}
    style = rng.choice([{}, {"separators": (",", ":")}, {"indent": 2}, {"ensure_ascii": False}, {"ensure_ascii": False, "separators": (",", ":")}])
    return json.dumps(value, **style).encode("utf-8")


def body_actions(rng):
    return {
        "body_rename": dict(some(rng, lambda: (rng.choice(PATHS), rng.choice(PATHS)))),
        "body_remove": some(rng, lambda: rng.choice(PATHS)),
        "body_set": dict(some(rng, lambda: (rng.choice(PATHS), rng.choice(ODD_SET) if rng.random() < 0.03 else a_value(rng)))),
    }


def header_actions(rng, names=NAMES):
    name = lambda: pick(rng, names, KEPT)  # noqa: E731
    return {
        "headers_rename": dict(some(rng, lambda: (name(), name()))),
        "headers_remove": some(rng, name),
        "headers_set": dict(some(rng, lambda: (name(), pick(rng, VALUES, ODD_VALUES)))),
    }


SEGMENTS = ["a", "b", "ab", "v1", "v2", "x.y", "a-b", "", "users", "é", "a b", ".", "..", "aab", "abab", "abcd", "42", "v10", "a_b", "bcd"]
FROM = ["/a", "a", "^/a", "^/a/", "/", "^/", "b/a", "x\\.y", "a\\-b", "^/v1", "v1", "/users/", "^/ab/a", "//", "ab"]
ODD_FROM = ["^/a$", "/(a)", "a+", ".", "a|b", "[ab]", "^", "(?i)a", "a$", "\\d", 5, "é", "a b"]
TO = ["", "/", "/b", "b", "/v2", "v2", "/a/a", "x.y", "a-b", "~", "/users", "ab"]
ODD_TO = ["\\1", "\\g<0>", "a b", "é", "?q=1", "#f", "%20", 7]
# Expressions, and replacements that put groups of the match back.
EXPRESSIONS = ["/(a|b)", "^/(v[0-9]+)/", "a+", "[ab]+", "(a)(b)?", "/([^/]+)/([^/]+)", "\\d+", "\\w+$", "^/users/(.*)$", "(?:a|ab)(b?)", "/(a*)b",
               "$", "a{2,}", "(a|ab)(c|bcd)?", "[^/]$", "/.", "(a+?)(a*)", "^/(?:a|b)/", "x\\.(y)", ".+?/", "(/a)+", "((a)|b)+", "(a*)+b",
               "(a|b)*c", "/(?:(a)|(b))", "a{1,2}?b", "[a-c]{2}", "(b|ab|a)+?d", "^/a|/b$", "(.)(.)$", "/(\\w)(\\w*)", "(a?)+b", "((a*)b)*c", "a.c",
               "(?:(a)|b)+", "(a*)?b", "(?:(a*))+b", "(a|b){2}", "(a?){2}b", "(a)|(b)", "(a*)(a*)", "(a+)(a+)?", "/(a|ab)(b?)/"]
ODD_EXPRESSIONS = ["a*", "/?$", "^/a|", "(a|)", "(?=a)", "\\bab", "(?P<n>a)", "a{3,1}", "(a", "[b-a]", "\\_a", "a++", "(a)\\1", "^$", "a(?#c)"]
WITH_GROUPS = ["\\1", "/\\1", "\\g<0>", "\\g<1>x", "\\2\\1", "/\\g<2>/\\1", "\\g<0>-\\g<0>", "<\\1>", "\\3", "\\10", "\\g<1", "\\n", "\\\\"]
ATOMS = ["a", "b", "c", "/", ".", "\\d", "\\w", "[ab]", "[^/]", "[a-c]", "v", "1"]


def an_expression(rng, depth=0):
    """An expression put together at random from what the two engines share."""
    def piece():
        if depth < 2 and rng.random() < 0.3:
            inner = an_expression(rng, depth + 1)
            one = rng.choice(["(", "(", "(?:"]) + inner + ")"
        else:
            one = rng.choice(ATOMS)
        return one + rng.choice([""] * 6 + ["*", "+", "?", "+?", "*?", "{2}", "{1,2}", "{0,2}?"])
    branches = ["".join(piece() for _ in range(rng.randint(1, 3))) for _ in range(rng.choice([1, 1, 1, 2]))]
    return "|".join(branches)


def a_rewrite(rng):
    if rng.random() < 0.5:
        made = rng.choice(["", "", "^"]) + an_expression(rng) + rng.choice(["", "", "", "$"])
        rewrite = {"pattern": rng.choice([pick(rng, EXPRESSIONS, ODD_EXPRESSIONS, 0.1), made])}
        if rng.random() < 0.9:
            rewrite["replacement"] = rng.choice([pick(rng, TO, ODD_TO, 0.05), rng.choice(WITH_GROUPS), rng.choice(TO) + rng.choice(WITH_GROUPS)])
        return rewrite
    rewrite = {"pattern": pick(rng, FROM, ODD_FROM, 0.08)}
    if rng.random() < 0.9:
        rewrite["replacement"] = pick(rng, TO, ODD_TO, 0.08)
    return rng.choice([rewrite] * 12 + [{}, {"replacement": "/x"}, {"pattern": ""}, "text"])


def a_rule(rng, n):
    rule = {"name": f"rule {n}", "path_pattern": "*"}
    if rng.random() < 0.8:
        rule["request_config"] = {
            **header_actions(rng, NAMES + ANNOUNCED + ["content-type", "Content-Type"]),
            **(body_actions(rng) if rng.random() < 0.5 else {}),
            "query_remove": some(rng, lambda: rng.choice(KEYS)),
            "query_set": dict(some(rng, lambda: (rng.choice(KEYS), pick(rng, WORDS, ODD_WORDS)))),
        }
        if rng.random() < 0.03:
            rule["request_config"]["template"] = "{{ body }}"
        if rng.random() < 0.3:
            rule["request_config"]["path_rewrite"] = a_rewrite(rng)
    if rng.random() < 0.8:
        rule["response_config"] = {
            **header_actions(rng),
            "status_map": dict(some(rng, lambda: (str(pick(rng, STATUSES, ["abc", "0200", "204", "20"])), pick(rng, STATUSES, ODD_STATUSES)))),
        }
        if rng.random() < 0.5:
            rule["response_config"].update(body_actions(rng))
        if rng.random() < 0.02:
            rule["response_config"]["template"] = "{{ body }}"
    return rule


def an_exchange(rng):
    """A request and an answer as they reach the rules. A request's names are in lower case, as
    its server reads them, and some are given more than once; so may an answer's be. Half the
    requests carry a body, with its length and, usually, what it is. A query is plain pairs or
    anything a caller may write."""
    names = sorted({n.lower() for n in rng.sample(NAMES + ANNOUNCED + ["x-api-version"], rng.randint(0, 8))})
    rng.shuffle(names)
    headers = [[n, rng.choice(VALUES)] for n in names]
    if names and rng.random() < 0.3:
        for _ in range(rng.randint(1, 3)):
            headers.insert(rng.randint(0, len(headers)), [rng.choice(names), rng.choice(VALUES)])
    body = a_body(rng) if rng.random() < 0.5 else None
    if body is not None:
        headers.append(["content-length", str(len(body))])
        if rng.random() < 0.7:
            headers.append(["content-type", rng.choice(["application/json", "text/plain", "application/json; charset=utf-8"])])
        rng.shuffle(headers)
    keys = rng.sample(KEYS, rng.randint(0, 4))
    query = "&".join(f"{k}={rng.choice(WORDS)}" for k in keys)
    if rng.random() < 0.4:
        part = lambda plain: rng.choice(plain) if rng.random() < 0.6 else "".join(rng.choice(NOT_TEXT if rng.random() < 0.15 else ODD_PARTS + plain) for _ in range(rng.randint(1, 3)))
        pieces = [rng.choice([f"{part(KEYS)}={part(WORDS)}"] * 6 + [part(KEYS), ""]) for _ in range(rng.randint(0, 6))]
        query = "&".join(pieces)
    answer = []
    for n in rng.sample(NAMES, rng.randint(0, 8)):
        answer += [[n, rng.choice(VALUES)] for _ in range(rng.choice([1, 1, 1, 2, 3]))]
    path = "/" + "/".join(rng.choice(SEGMENTS) for _ in range(rng.randint(0, 5)))
    status = rng.choice(STATUSES + [204, 304])
    # Most answers that may have a body have one, with its length and, usually, what it is.
    given = a_body(rng) if status not in (204, 304) and rng.random() < 0.7 else None
    if given is not None:
        answer.append(["content-length", str(len(given))])
        if rng.random() < 0.7:
            answer.append(["content-type", rng.choice(["application/json", "text/plain", "application/json; charset=utf-8"])])
    return {"headers": headers, "query": query, "body": body.hex() if body is not None else None, "path": path, "answer": answer,
            "answer_body": given.hex() if given is not None else None, "status": status,
            # The protocol name the backend is told: the one the router gives a path with no prefix.
            "protocol": router._get_backend_url(path)[1].value,
            "version": a_version(rng)}


def a_version(rng):
    """The version a backend is told, half the time: `[version, warning, sunset]`, with what the
    gateway's own class says a version of that standing announces."""
    if rng.random() < 0.5:
        return None
    version = APIVersion(rng.choice(["1", "2", "7"]), status=rng.choice(list(VersionStatus)[:3]),
                         sunset_date=rng.choice([None, datetime(2099, 1, 2, 3, 4, 5)]))
    return [version.version, version.get_warning_header(), version.get_sunset_header()]


def as_read(lines):
    """Header lines as a receiver reads them: for each name, the values in order."""
    out = {}
    for name, value in lines:
        if isinstance(name, bytes):
            name, value = name.decode("latin-1"), value.decode("latin-1")
        out.setdefault(name.lower(), []).append(value)
    return {name: ", ".join(values) for name, values in out.items()}


def python_says(rules, case):
    """The exchange as the Python gateway shapes it (`Router._prepare_upstream_parts` and the
    proxy handler's answer), with its own classes at each step."""
    before = RequestTransformer()
    before.rules = rules
    # A request's headers and query as the proxy handler holds them: a dictionary of each, and
    # beside it what was given more than once.
    asked, came_with = QueryParams(case["query"]), Headers(raw=[(name.encode("latin-1"), value.encode("latin-1")) for name, value in case["headers"]])
    query, held = dict(asked), dict(came_with)
    came = bytes.fromhex(case["body"]) if case["body"] is not None else None
    method = "GET" if came is None else "POST"
    # The router's own preparation of what goes upstream: the rules, then the headers the gateway
    # itself writes. (Which version a request resolves to is not the rules' business: the case
    # says which the backend is told, and what that version announces.)
    routing.transformation_manager.transform_request = lambda **asked: before.transform(
        asked["method"], asked["path"], asked["headers"], asked["query_params"], asked["body"])
    told = case["version"] and SimpleNamespace(version=case["version"][0], backend_url=None,
                                               get_warning_header=lambda: case["version"][1], get_sunset_header=lambda: case["version"][2])
    routing.version_manager.resolve = lambda path, headers: (told, path)
    url, headers, query, content, json_payload, backend, protocol, _ = router._prepare_upstream_parts(
        method, case["path"], held, query, came, None, repeated_query(asked), repeated_headers(came_with))
    assert protocol.value == case["protocol"], (protocol, case["path"])
    path = url[len(backend.rstrip("/")):]
    # The trace is the gateway's own, new for each request.
    headers = [(name, "<trace>" if name == "traceparent" else value) for name, value in (headers.items() if isinstance(headers, dict) else headers)]
    sent = httpx.Request(method, "http://backend:9000/x", headers=headers, params=query, content=content, json=json_payload)
    sent_headers = [(n, v) for n, v in sent.headers.raw if n.lower() != b"host"]
    body = came if content is None and json_payload is None else sent.read()

    lines = [tuple(line) for line in case["answer"]]
    if case["answer_body"] is not None:
        # An answer with a body, through the gateway's own code for answers.
        transformation_manager.rules_by_id = {rule.id: rule for rule in rules}
        arrived = httpx.Response(case["status"], headers=lines, content=bytes.fromhex(case["answer_body"]))
        given = asyncio.run(answer_from(arrived, "GET", "x", {}, None))
        said = [(n, v) for n, v in given.raw_headers if n != b"content-type" or any(name.lower() == "content-type" for name, _ in lines)]
        answer, body = as_read(said), given.body.hex()
    else:
        after = ResponseTransformer()
        after.rules = rules
        arrived = httpx.Response(case["status"], headers=lines)
        status, answer, _ = after.transform(case["status"], dict(arrived.headers), None, {"method": "GET", "path": "x"})
        given = Response(content=None, status_code=status or case["status"], headers=answer)
        answer, body = as_read((n, v) for n, v in given.raw_headers if n != b"content-length"), None
    return {
        "headers": as_read(sent_headers),
        "query": sent.url.query.decode("ascii") or None,
        "body": sent.read().hex() if came is not None else None,  # as the client writes it
        "path": path,
        "answer": answer,
        "given": body,
        "status": given.status_code,
    }


FRONTS = ["a", "[ab]", "/", "\\w", "a*", "[ab]+", "(?:a|b)", "a?", "/a", "[^/]", "b{2}", ".", "a{1,2}"]
BACK = ["", "\\1", "\\2", "\\g<0>", "\\g<1>", "\\3", "-\\1-"]
PLAIN = ["a", "b", "ab", "aab", "abab", "abcd", "42", "v1", "v10", "a_b", "bcd", "x.y", "a-b", "", "users", "c", "1", "aa", "bb", "cab", "a1b2"]


def too_slow(*_):
    raise TimeoutError


def rewrites(count, seed):
    """Paths rewritten by expressions put together at random, each compared with `re.sub`. Only
    expressions the data plane is told of take part; half of them or so match the path."""
    rng = random.Random(seed)
    cases = []
    while len(cases) < count:
        pattern = rng.choice(["", "", "^"]) + an_expression(rng) + rng.choice(["", "", "", "$"])
        if rng.random() < 0.2:
            pattern = rng.choice(EXPRESSIONS)
        elif rng.random() < 0.35:
            # Alternatives that begin alike, bare or in a group.
            front = rng.choice(FRONTS)
            several = "|".join(front + an_expression(rng, 1) for _ in range(rng.randint(2, 3)))
            pattern = rng.choice(["{}", "({})", "(?:{})", "/(?:{})", "({})c", "x?(?:{})"]).format(several)
        cfg = TransformConfig(path_rewrite={"pattern": pattern, "replacement": rng.choice(["", "/", "x", "/b"]) + rng.choice(BACK) + rng.choice(["", "", "\\1", "~"])})
        told = _path_actions(cfg)
        if told and "regex" in told.get("path", {}):
            cases.append((cfg, told, "/" + "/".join(rng.choice(PLAIN) for _ in range(rng.randint(0, 6)))))
    lines = [json.dumps({"headers": [], "query": "", "body": None, "path": path, "answer": [], "status": 200, "request": [told], "response": []})
             for _, told, path in cases]
    out = subprocess.run([str(BIN), "--judge-shapes"], input="\n".join(lines) + "\n", capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"dataplane --judge-shapes failed: {out.stderr.strip()}")
    answers = [json.loads(line) for line in out.stdout.splitlines()]
    assert len(answers) == len(cases), (len(answers), len(cases))
    wrong, moved, slow, refused = [], 0, 0, 0
    for (cfg, _, path), got in zip(cases, answers):
        if got == "unready":
            refused += 1
            continue
        signal.setitimer(signal.ITIMER_REAL, 1.0)
        try:
            want = _apply_path_rewrite(path, cfg)
        except TimeoutError:
            slow += 1
            continue
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
        moved += want != path
        if got["path"] != want:
            wrong.append((cfg.path_rewrite, path, want, got["path"]))
    for rewrite, path, want, got in wrong[:8]:
        print(f"DIFFERENT for {json.dumps(rewrite)} on {path}\n  python {want}\n  plane  {got}")
    print(f"{count} paths under a rewrite by expression ({moved} of them rewritten): {len(wrong)} different; "
          f"{refused} expressions the engine here does not take, {slow} not compared, Python's engine taking too long")
    if moved < count // 5:
        print("too few expressions match their path")
    return not wrong and moved >= count // 5 and slow <= count // 100 and refused <= count // 100


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 40000
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    rng = random.Random(seed)
    signal.signal(signal.SIGALRM, too_slow)
    cases, lines, kept = [], [], 0
    while len(cases) < count:
        described = [a_rule(rng, n) for n in range(rng.choice([1, 1, 2, 3]))]
        rules = [TransformationRule.from_dict(r) for r in described]
        actions = [_rule_actions(rule) for rule in rules]
        if any(a is None for a in actions):
            kept += 1
            continue
        case = an_exchange(rng)
        cases.append((described, rules, case))
        lines.append(json.dumps({**case, "request": [a["request"] for a in actions if a["request"]],
                                 "response": [a["response"] for a in actions if a["response"]]}))
    out = subprocess.run([str(BIN), "--judge-shapes"], input="\n".join(lines) + "\n", capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"dataplane --judge-shapes failed: {out.stderr.strip()}")
    answers = [json.loads(line) for line in out.stdout.splitlines()]
    assert len(answers) == len(cases), (len(answers), len(cases))
    wrong, changed, asked, rewritten, moved, expressed, left, slow, unsure, answered = [], 0, 0, 0, 0, 0, 0, 0, 0, 0
    unread, twice, requeried = 0, 0, 0
    for (described, rules, case), got, line in zip(cases, answers, lines):
        # A query whose escapes are not text is read with replacement characters on both sides.
        try:
            unquote_to_bytes(case["query"].replace("+", " ")).decode("utf-8")
        except UnicodeDecodeError:
            unread += 1
        by_expression = any("regex" in (a.get("path") or {}) for a in json.loads(line)["request"])
        if got == "ask" and by_expression and not case["path"].isascii():
            # Beyond ASCII the two engines' classes differ: such a path is rewritten by Python.
            left += 1
            continue
        if got == "ask python":
            # An answer whose body this side is not certain of: the Python gateway is asked.
            unsure += 1
            continue
        if got in ("ask", "unready"):
            # Handed over with its request, or the rule kept with Python once its value is read.
            asked += 1
            continue
        got["headers"], got["answer"] = as_read(got["headers"]), as_read(got["answer"])
        # An expression put together at random can keep Python's engine trying for longer than
        # anyone would wait; the engine here takes time in proportion to the path. Not compared.
        signal.setitimer(signal.ITIMER_REAL, 2.0)
        try:
            want = python_says(rules, case)
        except TimeoutError:
            slow += 1
            continue
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
        if got != want:
            wrong.append((described, case, want, got))
        twice += len({name for name, _ in case["headers"]}) < len(case["headers"])
        requeried += (want["query"] or "") != case["query"]
        untouched = {"headers": as_read(case["headers"]), "query": case["query"] or None, "body": case["body"], "path": case["path"],
                     "answer": as_read(case["answer"]), "given": case["answer_body"], "status": case["status"]}
        answered += want["given"] != case["answer_body"]
        changed += want != untouched
        rewritten += want["body"] != case["body"]
        moved += want["path"] != case["path"]
        expressed += by_expression and want["path"] != case["path"]
    for described, case, want, got in wrong[:8]:
        differ = [part for part in want if want[part] != got.get(part)]
        print(f"DIFFERENT in {differ}\n  rules  {json.dumps(described)}\n  case   {json.dumps(case)}")
        for part in differ:
            print(f"  python {part}: {want[part]}\n  plane  {part}: {got.get(part)}")
    print(f"{count} exchanges under rules the data plane is told the actions of ({changed} of them changed by the rules, "
          f"{rewritten} with a body written anew, {moved} with a path rewritten, {expressed} of those by expression): {len(wrong)} different; "
          f"{answered} with an answer's body written anew; "
          f"{asked} handed over for their body, {left} for a path beyond ASCII, {unsure} answers left for Python to shape; {slow} not compared, Python's engine taking too long; "
          f"{kept} sets of rules kept with Python; "
          f"{twice} requests gave a header more than once, {requeried} queries were written anew, {unread} of them with escapes that are not text")
    if twice < count // 20 or requeried < count // 20 or unread < count // 50:
        print("too few requests with a header given twice or a query written anew are being compared")
    if kept > 2 * count:
        print("too many ordinary rules are being kept with Python")
    if asked > count // 20 or rewritten < count // 20:
        print("too few ordinary bodies are being written here")
    if moved < count // 20 or expressed < count // 100:
        print("too few ordinary rewrites are being done here")
    few = twice < count // 20 or requeried < count // 20 or unread < count // 50 or answered < count // 20 or unsure > count // 10 or slow > count // 100 or asked > count // 20 or rewritten < count // 20 or moved < count // 20 or expressed < count // 100
    alike = rewrites(5 * count, seed)
    sys.exit(1 if wrong or kept > 2 * count or changed < count // 2 or few or not alike else 0)


if __name__ == "__main__":
    main()
