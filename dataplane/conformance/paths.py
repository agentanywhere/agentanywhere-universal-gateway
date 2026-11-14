"""Conformance of escaped paths: how the data plane reads a path a client sends, and what path it
asks a backend for, against the code the Python gateway runs for the same two steps.

Run from the repository root, after `cargo build --release` in dataplane/:

    python dataplane/conformance/paths.py [paths] [seed]

In the Python gateway the server (uvicorn) decodes a path's percent-escapes before anything looks at
it, and the HTTP client (httpx) writes the path again when the request goes to a backend: it drops
`.` and `..` segments and escapes what a path may not hold. The data plane does both with code of its
own. So the property that must hold is:

    for every path a client can send, and every backend address:
        the data plane leaves the path to Python, or
        reads it as uvicorn does and asks the backend for what httpx would ask.

and a path Python itself cannot forward (httpx refuses it, or Starlette reads it differently from
uvicorn) must be left to Python. This script generates paths, asks `dataplane --judge-paths`, and
fails on any difference. It also fails if ordinary escaped paths are being left to Python.
"""
import json
import os
import random
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[2]
BIN = ROOT / "dataplane/target/release/dataplane"
PREFIXES = ["", "", "", "/base", "/v1/api", "/a b", "/café", "/x%20y", "/p%zz", "/a/..", "/d/."]
WORDS = ["api", "users", "files", "a", "b", "v1", "report 2024", "café", "日本", "a+b", "a&b=c", "x;y", "k:v", "[1]", "it's", "(x)", "a,b",
         "me@host", "50%", "100%25", "~t", "a|b", "<s>", "{id}", "\"q\"", "a\\b", "^", "`", "\U0001F600", "", ".", "..", "...", ".hidden", "a.", "%41", "%2F", "%2f"]
CLIENT = httpx.Client()
RAW_ODD = ["%", "%%", "%4", "%zz", "%2", "%C3", "%C3%28", "%FF", "%00", "%0A", "%09", "%7F", "%1f", "%3F", "%23", "%25", "%2525", "%20", "%E2%82%AC", "%c3%a9",
           "%F0%9F%98%80", "%ED%A0%80", "%C0%AF", "%2E", "%2e%2e", "%2E%2E%2F", "%5B", "%5d", "%40", "%3A"]


def escaped(rng, word):
    """A word as a client might send it: every character that must be escaped is, others sometimes are."""
    out = []
    for byte in word.encode():
        ch = chr(byte)
        must = byte >= 0x80 or byte <= 0x20 or ch in '"<>\\^`{|}?#%' or byte == 0x7F
        if must or rng.random() < 0.08:
            out.append(("%%%02X" if rng.random() < 0.7 else "%%%02x") % byte)
        else:
            out.append(ch)
    return "".join(out)


def a_path(rng):
    parts = []
    for _ in range(rng.randint(1, 5)):
        roll = rng.random()
        if roll < 0.75:
            parts.append(escaped(rng, rng.choice(WORDS)))
        elif roll < 0.9:
            parts.append(rng.choice(RAW_ODD))
        else:
            parts.append(escaped(rng, rng.choice(WORDS)) + rng.choice(RAW_ODD))
    return "/" + "/".join(parts) + ("/" if rng.random() < 0.1 else "")


def python_says(prefix, raw):
    """What the Python gateway reads and sends, or None when it does not forward the path as one."""
    path = unquote(raw)  # uvicorn
    # Starlette builds a URL around the path and reads the path back out of it; the proxy uses both.
    if urlsplit(f"http://gateway{path}").path != path:
        return None
    try:
        # As the router asks: the address and the path joined as text, the query handed over apart.
        url = CLIENT.build_request("GET", f"http://backend:9000{prefix}{path}", params={}).url
    except httpx.InvalidURL:
        return None
    if url.query or url.fragment or b"?" in url.raw_path:
        return None
    return {"decoded": path, "sent": url.raw_path.decode("ascii")}


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 60000
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    rng = random.Random(seed)
    cases = [(rng.choice(PREFIXES), a_path(rng)) for _ in range(count)]
    lines = "\n".join(json.dumps({"prefix": prefix, "path": raw}) for prefix, raw in cases) + "\n"
    out = subprocess.run([str(BIN), "--judge-paths"], input=lines, capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"dataplane --judge-paths failed: {out.stderr.strip()}")
    answers = [json.loads(line) for line in out.stdout.splitlines()]
    assert len(answers) == len(cases), (len(answers), len(cases))
    wrong, same, left, both_python, needless = [], 0, 0, 0, []
    for (prefix, raw), got in zip(cases, answers):
        want = python_says(prefix, raw)
        if got is None:
            left += 1
            if want is None:
                both_python += 1
            elif all(ord(c) >= 0x20 and c not in "?#\x7f�" for c in want["decoded"]):
                needless.append((prefix, raw, want))
        elif got == want:
            same += 1
        else:
            wrong.append((prefix, raw, want, got))
    for prefix, raw, want, got in wrong[:10]:
        print(f"DIFFERENT\n  backend prefix {prefix!r}, path {raw!r}\n  python {want}\n  plane  {got}")
    for prefix, raw, want in needless[:5]:
        print(f"LEFT TO PYTHON WITHOUT NEED\n  backend prefix {prefix!r}, path {raw!r}\n  python {want}")
    print(f"{count} paths: {same} read and sent alike, {left} left to Python ({both_python} of them Python does not forward either); "
          f"{len(wrong)} different, {len(needless)} left without need")
    sys.exit(1 if wrong or needless else 0)


if __name__ == "__main__":
    main()
