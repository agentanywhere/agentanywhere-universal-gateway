"""Conformance of applied transformation rules: what the data plane makes of a request and of its
answer when rules apply, against the code the Python gateway runs for the same.

Run from the repository root, after `cargo build --release` in dataplane/:

    python dataplane/conformance/shapes.py [cases] [seed]

The Python gateway tells the data plane what a rule does only when the data plane can do exactly the
same (`app.dataplane_sync._rule_actions`): change headers and query parameters of the request, and
headers and the status of the answer. Any other rule is applied by the Python gateway. So the
property that must hold is:

    for every set of rules the data plane is told the actions of, and every exchange:
        the backend is sent the headers and the query the Python gateway would send it, and
        the caller is given the headers and the status the Python gateway would give.

This script generates rules as the admin API takes them and exchanges to apply them to. The Python
side is the gateway's own: its rule classes, the dictionaries it reads headers and a query into, its
HTTP client writing the request and its web framework writing the answer. The other side is
`dataplane --judge-shapes`, given the actions as the snapshot carries them. The script fails on any
difference, and if ordinary rules are not being described to the data plane at all.

Headers are compared as a receiver reads them: for each name, whatever its case, the values in order.
"""
import json
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from starlette.datastructures import QueryParams  # noqa: E402
from starlette.responses import Response  # noqa: E402

from app.dataplane_sync import _rule_actions  # noqa: E402
from app.transformers import RequestTransformer, ResponseTransformer, TransformationRule  # noqa: E402

BIN = ROOT / "dataplane/target/release/dataplane"
NAMES = ["x-a", "X-A", "X-a", "x-b", "X-B", "x-c", "X-C", "x-trace-me", "X-Trace-Me", "authorization", "Authorization",
         "cookie", "x-api-key", "X-Api-Key", "x_under", "X-Gateway-Internal", "x-gateway-internal", "etag", "ETag",
         "cache-control", "Cache-Control", "x-d", "x-e"]
# Names and values the two sides might treat differently: a rule with one stays with Python.
KEPT = ["Host", "Content-Type", "User-Agent", "x-request-id", "Access-Control-Allow-Origin", "Set-Cookie", "Accept",
        "X-Forwarded-For", "x-ratelimit-limit", "bad name", "café", ""]
VALUES = ["v1", "v2", "yes", "", "a b", "text/plain; q=0.5", "Bearer abc.def", "1", "x=y; z=1", "~!@#$%^&*()"]
ODD_VALUES = [" lead", "trail ", "naïve", "two\nlines", "tab\there", 7, None]
KEYS = ["a", "b", "c", "page", "q", "A", "sort"]
WORDS = ["1", "two", "", "x-y_z.~", "A1", "10"]
ODD_WORDS = ["a b", "a&b", "café", "50%", 3]
STATUSES = [200, 201, 202, 301, 400, 401, 404, 409, 500, 502, 503]
ODD_STATUSES = [204, 304, 99, 700, 0, "502"]


def pick(rng, usual, odd, odds=0.04):
    return rng.choice(odd) if rng.random() < odds else rng.choice(usual)


def some(rng, make, most=3):
    return [make() for _ in range(rng.choice([0, 0, 1, 1, 2, most]))]


def header_actions(rng):
    name = lambda: pick(rng, NAMES, KEPT)  # noqa: E731
    return {
        "headers_rename": dict(some(rng, lambda: (name(), name()))),
        "headers_remove": some(rng, name),
        "headers_set": dict(some(rng, lambda: (name(), pick(rng, VALUES, ODD_VALUES)))),
    }


def a_rule(rng, n):
    rule = {"name": f"rule {n}", "path_pattern": "*"}
    if rng.random() < 0.8:
        rule["request_config"] = {
            **header_actions(rng),
            "query_remove": some(rng, lambda: rng.choice(KEYS)),
            "query_set": dict(some(rng, lambda: (rng.choice(KEYS), pick(rng, WORDS, ODD_WORDS)))),
        }
        if rng.random() < 0.03:
            rule["request_config"].update(rng.choice([{"body_set": {"a": 1}}, {"template": "{{ body }}"},
                                                      {"path_rewrite": {"pattern": "^/x", "replacement": "/y"}}]))
    if rng.random() < 0.8:
        rule["response_config"] = {
            **header_actions(rng),
            "status_map": dict(some(rng, lambda: (str(pick(rng, STATUSES, ["abc", "0200", "204", "20"])), pick(rng, STATUSES, ODD_STATUSES)))),
        }
        if rng.random() < 0.03:
            rule["response_config"]["body_remove"] = ["secret"]
    return rule


def an_exchange(rng):
    """A request and an answer as they reach the rules. A request's names are in lower case and
    given once (the data plane hands over a request that gives a name the rules mention twice);
    an answer may give a name more than once."""
    names = sorted({n.lower() for n in rng.sample(NAMES, rng.randint(0, 8))})
    rng.shuffle(names)
    headers = [[n, rng.choice(VALUES)] for n in names]
    keys = rng.sample(KEYS, rng.randint(0, 4))
    query = "&".join(f"{k}={rng.choice(WORDS)}" for k in keys)
    answer = []
    for n in rng.sample(NAMES, rng.randint(0, 8)):
        answer += [[n, rng.choice(VALUES)] for _ in range(rng.choice([1, 1, 1, 2, 3]))]
    return {"headers": headers, "query": query, "answer": answer, "status": rng.choice(STATUSES + [204, 304])}


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
    query = dict(QueryParams(case["query"]))
    headers, query, _, _ = before.transform("GET", "/x", dict(case["headers"]), query, None)
    sent = httpx.Request("GET", "http://backend:9000/x", headers=headers, params=query)
    sent_headers = [(n, v) for n, v in sent.headers.raw if n.lower() != b"host"]

    after = ResponseTransformer()
    after.rules = rules
    arrived = httpx.Response(case["status"], headers=[tuple(line) for line in case["answer"]])
    status, answer, _ = after.transform(case["status"], dict(arrived.headers), None, {"method": "GET", "path": "x"})
    given = Response(content=None, status_code=status or case["status"], headers=answer)
    return {
        "headers": as_read(sent_headers),
        "query": sent.url.query.decode("ascii") or None,
        "answer": as_read((n, v) for n, v in given.raw_headers if n != b"content-length"),
        "status": given.status_code,
    }


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 40000
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    rng = random.Random(seed)
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
    wrong, changed = [], 0
    for (described, rules, case), got in zip(cases, answers):
        got["headers"], got["answer"] = as_read(got["headers"]), as_read(got["answer"])
        want = python_says(rules, case)
        if got != want:
            wrong.append((described, case, want, got))
        untouched = {"headers": as_read(case["headers"]), "query": case["query"] or None, "answer": as_read(case["answer"]), "status": case["status"]}
        changed += want != untouched
    for described, case, want, got in wrong[:8]:
        differ = [part for part in want if want[part] != got[part]]
        print(f"DIFFERENT in {differ}\n  rules  {json.dumps(described)}\n  case   {json.dumps(case)}")
        for part in differ:
            print(f"  python {part}: {want[part]}\n  plane  {part}: {got[part]}")
    print(f"{count} exchanges under rules the data plane is told the actions of ({changed} of them changed by the rules): "
          f"{len(wrong)} different; {kept} sets of rules kept with Python")
    if kept > 2 * count:
        print("too many ordinary rules are being kept with Python")
    sys.exit(1 if wrong or kept > 2 * count or changed < count // 2 else 0)


if __name__ == "__main__":
    main()
