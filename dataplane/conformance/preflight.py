"""Conformance of preflight answers: what the data plane tells a web page that asks before a
cross-origin request, against Starlette's own `CORSMiddleware`, which answers for the Python gateway.

Run from the repository root, after `cargo build --release` in dataplane/:

    python dataplane/conformance/preflight.py [cases] [seed]

The snapshot describes how the middleware was set up (`cross_origin.preflight`), and the data plane
answers from that description with code of its own. So the property that must hold is:

    for every way of setting the middleware up, and every preflight:
        the data plane's status, headers and text == the middleware's.

This script generates set-ups and preflights, asks `dataplane --judge-preflights`, and fails on any
difference. A preflight the data plane leaves to Python (a header it will not read) is counted.
"""
import json
import os
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SECRET_KEY", "preflight-conformance-" + "x" * 32)
os.environ.setdefault("LOG_LEVEL", "CRITICAL")

from starlette.datastructures import Headers  # noqa: E402
from starlette.middleware.cors import CORSMiddleware  # noqa: E402

from app import dataplane_sync  # noqa: E402

BIN = ROOT / "dataplane/target/release/dataplane"
ORIGINS = ["http://localhost:3001", "https://app.example", "https://app.example:8443", "http://a.example", "null", "https://APP.example"]
METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD", "get", "Post", "PROPFIND", "", " GET"]
NAMES = ["X-API-Key", "Content-Type", "Authorization", "X-Custom", "accept", "Accept-Language", "Content-Language", "x-requested-with",
         "X-Request-ID", "range", "", "x api", "X_Under"]


def setup(rng):
    origins = rng.sample(ORIGINS, rng.randint(0, 3)) + (["*"] if rng.random() < 0.3 else [])
    methods = ["*"] if rng.random() < 0.3 else rng.sample(METHODS[:7], rng.randint(0, 5))
    headers = ["*"] if rng.random() < 0.3 else rng.sample(NAMES[:10], rng.randint(0, 4))
    if rng.random() < 0.1:
        headers = headers + ["*"]
    return {"allow_origins": origins, "allow_credentials": rng.random() < 0.5, "allow_methods": methods, "allow_headers": headers}


def asked(rng, options):
    """A preflight's headers: mostly near what the set-up allows, sometimes not a preflight at all."""
    sent = []
    if rng.random() < 0.95:
        known = [o for o in options["allow_origins"] if o != "*"]
        sent.append(("origin", rng.choice(known) if known and rng.random() < 0.6 else rng.choice(ORIGINS + ["https://evil.example"])))
    if rng.random() < 0.95:
        allowed = [m for m in options["allow_methods"] if m != "*"]
        sent.append(("access-control-request-method", rng.choice(allowed) if allowed and rng.random() < 0.6 else rng.choice(METHODS)))
    if rng.random() < 0.7:
        allowed = [h for h in options["allow_headers"] if h != "*"] + ["accept", "content-type"]
        names = [rng.choice(allowed) if rng.random() < 0.7 else rng.choice(NAMES) for _ in range(rng.randint(0, 4))]
        names = [n.upper() if rng.random() < 0.2 else n.lower() if rng.random() < 0.3 else n for n in names]
        joined = rng.choice([",", ", ", " , ", ",\t", ",,"]).join(names)
        sent.append(("access-control-request-headers", rng.choice(["", " ", "\t"]) + joined + rng.choice(["", " ", ","])))
    if rng.random() < 0.03 and sent:
        sent.append(rng.choice(sent))  # a header sent twice
    rng.shuffle(sent)
    return sent


def python_says(cors, sent):
    """What the middleware answers, or None when it would not treat the request as a preflight."""
    headers = Headers(raw=[(k.encode("latin-1"), v.encode("latin-1")) for k, v in sent])
    if headers.get("origin") is None or "access-control-request-method" not in headers:
        return None
    response = cors.preflight_response(request_headers=headers)
    kept = {k: v for k, v in response.headers.items() if k not in ("content-length", "content-type")}
    return {"status": response.status_code, "headers": kept, "body": response.body.decode()}


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 40000
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    rng = random.Random(seed)
    cases, expected, undescribed = [], [], 0
    while len(cases) < count:
        options = setup(rng)
        rules = dataplane_sync._preflight(options["allow_origins"], options["allow_credentials"], options["allow_methods"], options["allow_headers"])
        if rules is None:
            undescribed += 1
            continue
        cors = CORSMiddleware(None, **options)
        snapshot = {"any_origin": "*" in options["allow_origins"], "origins": sorted(set(options["allow_origins"]) - {"*"}),
                    "credentials": options["allow_credentials"], "preflight": rules}
        for _ in range(8):
            sent = asked(rng, options)
            cases.append(json.dumps({"rules": snapshot, "headers": sent}))
            expected.append((options, sent, python_says(cors, sent)))
    out = subprocess.run([str(BIN), "--judge-preflights"], input="\n".join(cases) + "\n", capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"dataplane --judge-preflights failed: {out.stderr.strip()}")
    answers = [json.loads(line) for line in out.stdout.splitlines()]
    assert len(answers) == len(expected), (len(answers), len(expected))
    wrong, left, allowed, refused = [], 0, 0, 0
    for (options, sent, want), got in zip(expected, answers):
        if got is None:
            # Left to Python, which is always right. It must not be a plain preflight, though.
            left += 1
            names = [k for k, _ in sent]
            if want is not None and len(names) == len(set(names)):
                wrong.append((options, sent, want, got))
            continue
        got = {"status": got["status"], "headers": {k.lower(): v for k, v in got["headers"]}, "body": got["body"]}
        if got != want:
            wrong.append((options, sent, want, got))
        elif got["status"] == 200:
            allowed += 1
        else:
            refused += 1
    for options, sent, want, got in wrong[:10]:
        print(f"DIFFERENT\n  set-up  {options}\n  sent    {sent}\n  python  {want}\n  plane   {got}")
    print(f"{len(expected)} preflights over {len(expected) // 8} set-ups: {allowed} allowed, {refused} refused, {left} left to Python, "
          f"{undescribed} set-ups not described; {len(wrong)} different")
    sys.exit(1 if wrong else 0)


if __name__ == "__main__":
    main()
