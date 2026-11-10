"""Conformance: the same scenarios against the Python gateway and the Rust data plane in front of it.

Run from the repository root, after `cargo build --release` in dataplane/:

    python dataplane/conformance/run.py

Each scenario is sent to the Python gateway directly and to the data plane, and the status, the error
code, the body and the headers the gateway promises are compared. A status, error-code or body
difference fails the run. Header-name differences are listed as known gaps, not failures.
"""
import hashlib, json, os, secrets, signal, subprocess, sys, tempfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from perf.suite import _BACKEND, _BACKEND_RUNNER, free_port, wait_healthy  # noqa: E402

BIN = ROOT / "dataplane/target/release/dataplane"
PROMISED = ("x-request-id", "traceparent", "retry-after", "content-type", "x-response-time", "x-api-version")


def main() -> int:
    if not BIN.exists():
        print(f"build first: cargo build --release  (in {BIN.parent.parent.parent})")
        return 2
    bport, gport, dport = free_port(), free_port(), free_port()
    admin = secrets.token_urlsafe(32)
    procs = []
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "SECRET_KEY": secrets.token_urlsafe(48), "ADMIN_TOKEN": admin,
               "DATABASE_URL": f"sqlite+aiosqlite:///{tmp}/g.db", "HTTP_BACKEND_URL": f"http://127.0.0.1:{bport}",
               "REDIS_ENABLED": "false", "LOG_LEVEL": "WARNING", "STARTUP_BANNER": "false", "DEBUG": "false",
               "TRUSTED_PROXIES": "127.0.0.1"}
        try:
            procs.append(subprocess.Popen([sys.executable, "-c", _BACKEND_RUNNER, str(ROOT / _BACKEND), str(bport)],
                                          cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            procs.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                                           "--port", str(gport), "--log-level", "warning"], cwd=ROOT, env=env,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            wait_healthy(f"http://127.0.0.1:{bport}/health", procs[0])
            wait_healthy(f"http://127.0.0.1:{gport}/health", procs[1])

            def make_key(name, per_min):
                r = httpx.post(f"http://127.0.0.1:{gport}/admin/api-keys",
                               params={"name": name, "rate_limit_per_minute": per_min, "rate_limit_per_hour": 1000},
                               headers={"Authorization": f"Bearer {admin}"}, timeout=10)
                r.raise_for_status()
                return r.json()

            # Separate keys per target and per limit, so the two planes never share a counter.
            keys = {"py": make_key("conf-py", 1000), "rs": make_key("conf-rs", 1000),
                    "py_lim": make_key("conf-py-lim", 3), "rs_lim": make_key("conf-rs-lim", 3)}
            snap = {"keys": [{"hash": hashlib.sha256(keys[n]["key"].encode()).hexdigest(), "id": keys[n].get("id", n),
                              "rate_limit_per_minute": keys[n]["rate_limit_per_minute"], "rate_limit_per_hour": 1000}
                             for n in ("rs", "rs_lim")]}
            (Path(tmp) / "snapshot.json").write_text(json.dumps(snap))
            procs.append(subprocess.Popen([str(BIN), "--listen", f"127.0.0.1:{dport}", "--snapshot", f"{tmp}/snapshot.json",
                                           "--backend", f"http://127.0.0.1:{bport}", "--control", f"http://127.0.0.1:{gport}"]))
            wait_healthy(f"http://127.0.0.1:{dport}/health", procs[2])

            py, rs = f"http://127.0.0.1:{gport}", f"http://127.0.0.1:{dport}"
            tp_in = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
            scenarios = [
                ("no key", "GET", "/api/users", {}, None, "py", "rs"),
                ("wrong key", "GET", "/api/users", {"X-API-Key": "sk-nope"}, None, "py", "rs"),
                ("valid key", "GET", "/api/users", {"X-API-Key": "{key}"}, None, "py", "rs"),
                ("valid key, POST body", "POST", "/api/users", {"X-API-Key": "{key}", "Content-Type": "application/json"},
                 {"name": "Ada", "email": "ada@example.com"}, "py", "rs"),
                ("request id echoed", "GET", "/api/users", {"X-API-Key": "{key}", "X-Request-ID": "conf-123"}, None, "py", "rs"),
                ("traceparent kept", "GET", "/api/users", {"X-API-Key": "{key}", "traceparent": tp_in}, None, "py", "rs"),
                ("health (control plane)", "GET", "/health", {}, None, "py", "rs"),
                ("admin passes through", "GET", "/admin/api-keys", {"Authorization": f"Bearer {admin}"}, None, "py", "rs"),
                ("browser without key", "GET", "/api/users", {"Accept": "text/html"}, None, "py", "rs"),
            ]
            failures, gaps = 0, set()

            def call(base, method, path, headers, body, key):
                h = {k: (v.replace("{key}", key) if isinstance(v, str) else v) for k, v in headers.items()}
                return httpx.request(method, base + path, headers=h, json=body, timeout=15, follow_redirects=False)

            VOLATILE = {"id", "created_at", "timestamp", "request_id", "updated_at"}

            def mask(x):
                if isinstance(x, dict):
                    return {k: ("<volatile>" if k in VOLATILE else mask(v)) for k, v in x.items()}
                if isinstance(x, list):
                    return [mask(i) for i in x]
                return x

            def shape(r):
                try:
                    j = mask(r.json())
                except Exception:
                    return r.text[:80]
                if isinstance(j, dict) and "error" in j:
                    return {"error": j["error"], "message": j.get("message"), "keys": sorted(j)}
                return j

            def compare(name, a, b):
                nonlocal failures
                bad = []
                if a.status_code != b.status_code:
                    bad.append(f"status {a.status_code} vs {b.status_code}")
                if shape(a) != shape(b):
                    bad.append(f"body {shape(a)} vs {shape(b)}")
                for h in PROMISED:
                    if (h in a.headers) != (h in b.headers):
                        bad.append(f"header {h}: python={h in a.headers} rust={h in b.headers}")
                gaps.update(set(map(str.lower, a.headers)) ^ set(map(str.lower, b.headers)))
                print(f"{'PASS' if not bad else 'FAIL'}  {name}  [{a.status_code}]" + ("" if not bad else "  " + "; ".join(bad)))
                failures += bool(bad)

            for name, method, path, headers, body, _a, _b in scenarios:
                compare(name, call(py, method, path, headers, body, keys["py"]["key"]),
                        call(rs, method, path, headers, body, keys["rs"]["key"]))

            # Rate limit: 3 per minute, so the 4th request is denied on both.
            a = [call(py, "GET", "/api/users", {"X-API-Key": "{key}"}, None, keys["py_lim"]["key"]) for _ in range(4)]
            b = [call(rs, "GET", "/api/users", {"X-API-Key": "{key}"}, None, keys["rs_lim"]["key"]) for _ in range(4)]
            print("      rate-limit statuses  python", [r.status_code for r in a], " rust", [r.status_code for r in b])
            compare("rate limited (429 shape)", a[3], b[3])
            if [r.status_code for r in a] != [r.status_code for r in b]:
                print("FAIL  rate limit sequence differs"); failures += 1
            if a[3].status_code == 429 and b[3].status_code == 429:
                if json.loads(a[3].text)["details"] != json.loads(b[3].text)["details"]:
                    print("FAIL  429 details differ"); failures += 1

            known = sorted(h for h in gaps if h not in PROMISED)
            print(f"\nheader names present on only one side (known gaps, not failures): {known or 'none'}")
            print(f"\n{'ALL PASS' if not failures else str(failures) + ' FAILED'}")
            return 1 if failures else 0
        finally:
            for p in reversed(procs):
                p.send_signal(signal.SIGTERM)
            for p in procs:
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()


if __name__ == "__main__":
    sys.exit(main())
