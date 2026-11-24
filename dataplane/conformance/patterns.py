"""Conformance of transformation rule patterns: which paths the data plane says a pattern matches,
against the Python gateway's own `TransformationRule.matches`.

Run from the repository root, after `cargo build --release` in dataplane/:

    python dataplane/conformance/patterns.py [patterns] [seed]

The Python gateway applies a transformation rule; the data plane only decides whether a request is one
the rule applies to. A pattern is a glob (with or without character classes), every path, a regular
expression, or text the path must contain, and the two sides match it with different code (for a regular
expression, with different engines). So the property that must hold is:

    for every pattern the snapshot describes, and every path:  the data plane's answer == Python's.

This script generates patterns of every kind and paths near them, asks `dataplane --judge-patterns`,
and fails on any difference. A pattern the snapshot does not describe is not sent, and is counted.
"""
import json
import os
import random
import subprocess
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SECRET_KEY", "pattern-conformance-" + "x" * 32)
os.environ.setdefault("LOG_LEVEL", "CRITICAL")

from app import dataplane_sync  # noqa: E402
from app.transformers import TransformationRule  # noqa: E402

BIN = ROOT / "dataplane/target/release/dataplane"
# Python remarks on some of the odd patterns generated here; they are meant to be odd.
warnings.simplefilter("ignore", FutureWarning)
WORDS = ["api", "v1", "v2", "orders", "items", "a", "b", "ab", "x", "7", "42", "shelf-a", "user_1", "a.json", "A", "~t", "o'k", "a,b", "k=v", "a b"]
ATOMS = ["a", "b", "x", "/", "api", "v", "orders", ".", "\\.", "\\d", "\\w", "[0-9]", "[a-c]", "[^/]", "[ab.]", "[A-Za-z0-9_]", "[^a-b7]", "\\-", "-", "_", "~",
         "(?:a|b)", "(a|ab)", "(?:v[0-9]+)", "(x|)", "[\\]/]", "[\\d_]", "1", "7", " ", "'", ",", "=", "json"]
ODD = ["(?=a)", "(?i)", "\\s", "\\1", "\\b", "[z-a]", "(", ")", "*", "{2,1}", "\\Z", "[[:alpha:]]", "(?P<n>a)", "{", "}", "^", "$", "\\", "#", "[]", "a{,2}", "++"]
GLOB = list("ab/x7-_.~") + ["*", "?", "[ab]", "[!a]", "[a-c]", "[]]", "[!]]", "[", "]", "[-a]", "[a-]", "[c-a]", "[b-d-f]", "[!!]", "[^a]", "[\\]", "[a&&b]", "api", "/v"]


def regex(rng):
    body = ""
    for _ in range(rng.randrange(1, 7)):
        atom = rng.choice(ODD) if rng.random() < 0.06 else rng.choice(ATOMS)
        body += atom + rng.choice(["", "", "", "*", "+", "?", "{2}", "{1,3}", "{2,}", "*?", "+?", "??", "|"])
    front, end = rng.choice([("^", ""), ("", "$"), ("^", "$"), ("^/", ""), ("^/api/", "$")])
    return front + body + end


def pattern(rng):
    roll = rng.random()
    if roll < 0.55:
        return regex(rng)
    if roll < 0.95:
        return rng.choice(["", "/", "/api/"]) + "".join(rng.choice(GLOB) for _ in range(rng.randrange(1, 7)))
    return rng.choice(["*", "**", "/api/*", "/api/item?", "^/api/(x", "/api/[z-a]$", "/a)$"])


def paths(rng, pat):
    out = ["", "/", "/api/v1/orders/7", "api/v1/orders/7"]
    plain = "".join(c for c in pat if c.isalnum() or c in "/_-.~")
    for _ in range(24):
        roll = rng.random()
        if roll < 0.4:
            text = rng.choice(["", "/"]) + "/".join(rng.choice(WORDS) for _ in range(rng.randrange(0, 4)))
        elif roll < 0.7:
            text = "".join(rng.choice("ab/x7-_.~cvA1 ]!^") for _ in range(rng.randrange(0, 9)))
        else:
            # Near the pattern: its plain characters, with a few changed.
            text = "".join(rng.choice("ab/x7.") if rng.random() < 0.15 else c for c in plain)[:rng.randrange(1, 30)]
        out.append(text)
    return out


def main() -> int:
    if not BIN.exists():
        print(f"build first: cargo build --release  (in {BIN.parent.parent.parent})")
        return 2
    total = int(sys.argv[1]) if len(sys.argv) > 1 else 30_000
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 20261113
    rng = random.Random(seed)
    cases, kinds, kept = [], {}, 0
    for _ in range(total):
        pat = pattern(rng)
        described = dataplane_sync._rule_pattern(pat)
        if described is None:
            kept += 1
            continue
        kind = next(iter(described))
        kinds[kind] = kinds.get(kind, 0) + 1
        tried = paths(rng, pat)
        rule = TransformationRule(name="conformance", path_pattern=pat)
        cases.append((pat, described, tried, "".join("1" if rule.matches({"path": p, "method": "GET"}) else "0" for p in tried)))
    feed = "\n".join(json.dumps({"rule": {**d, "methods": []}, "paths": t}) for _, d, t, _ in cases) + "\n"
    run = subprocess.run([str(BIN), "--judge-patterns"], input=feed.encode(), capture_output=True, timeout=600)
    if run.returncode != 0:
        print("dataplane --judge-patterns failed:", run.stderr.decode()[-500:])
        return 2
    answers = run.stdout.decode().split("\n")[:-1]
    if len(answers) != len(cases):
        print(f"{len(answers)} answers for {len(cases)} cases")
        return 2
    wrong, refused, matched = [], 0, 0
    for (pat, described, tried, want), got in zip(cases, answers):
        if got == "ask":
            refused += 1
            continue
        matched += want.count("1")
        wrong += [(pat, described, p, w, g) for p, w, g in zip(tried, want, got) if w != g]
    print(f"{total} patterns (seed {seed}): described for the data plane {len(cases)} {kinds}, kept by Python {kept}")
    print(f"the data plane would not trust {refused} of those described (correct: every path then stays with Python)")
    print(f"{sum(len(t) for _, _, t, _ in cases)} pattern and path pairs compared, {matched} of them matches")
    print(f"{'FAIL' if not matched or min(kinds.get(k, 0) for k in ('regex', 'parts', 'glob', 'contains', 'all')) == 0 else 'PASS'}  every kind of pattern was tried, and some paths matched")
    print(f"{'FAIL' if wrong else 'PASS'}  the data plane matches every described pattern as Python does")
    for pat, described, path, want, got in wrong[:20]:
        print(f"    pattern {pat!r} as {described}  path {path!r}: python {want}, data plane {got}")
    return 1 if wrong or not matched else 0


if __name__ == "__main__":
    sys.exit(main())
