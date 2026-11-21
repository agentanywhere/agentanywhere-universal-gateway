"""Conformance: the same scenarios against the Python gateway and the Rust data plane in front of it.

Run from the repository root, after `cargo build --release` in dataplane/:

    python dataplane/conformance/run.py

Each scenario is sent to the Python gateway directly and to the data plane, and the status, the error
code, the body and the headers the gateway promises are compared. A status, error-code or body
difference fails the run. Header-name differences are listed as known gaps, not failures.
"""
import json, os, secrets, signal, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from perf.suite import _BACKEND, _BACKEND_RUNNER, free_port, wait_healthy  # noqa: E402

BIN = ROOT / "dataplane/target/release/dataplane"
PROMISED = ("x-request-id", "traceparent", "retry-after", "content-type", "x-response-time", "x-api-version",
            "x-tenant-id", "x-content-type-options")


def webhook_receiver():
    """A local endpoint that keeps every webhook the gateway delivers; returns (url, received)."""
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers.get("content-length", 0)))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}/hook", received


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)  # in order with what the processes write to stderr
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
               "TRUSTED_PROXIES": "127.0.0.1", "DATAPLANE_TOKEN": "conformance-token",
               "CIRCUIT_BREAKER_TIMEOUT": "2", "DEFAULT_TIMEOUT": "2",  # short, so the breaker and timeout scenarios can wait them out
               # SQLite as one would run it: in its default journal a reader holds writers off and every
               # commit waits for the disk. On a slow disk a report could then take the control plane
               # longer than the data plane waits for it (3 s): the data plane sends it again and the
               # counts in it are applied twice, and the request-log rows in it are not sent again.
               "SQLITE_JOURNAL_MODE": "WAL", "SQLITE_SYNCHRONOUS": "NORMAL"}

        def start_backend():
            return subprocess.Popen([sys.executable, "-c", _BACKEND_RUNNER, str(ROOT / _BACKEND), str(bport)],
                                    cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        try:
            procs.append(start_backend())
            # What the gateway logs (warnings and errors) is kept, and shown if a check fails.
            gateway_log = open(os.path.join(tmp, "gateway.log"), "wb")
            procs.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                                           "--port", str(gport), "--log-level", "warning"], cwd=ROOT, env=env,
                                          stdout=gateway_log, stderr=subprocess.STDOUT))
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
            # The data plane gets its keys from the Python gateway; a fast interval and a short
            # staleness limit keep the live scenarios quick.
            procs.append(subprocess.Popen([str(BIN), "--listen", f"127.0.0.1:{dport}", "--control-token", "conformance-token",
                                           "--sync-interval-ms", "200", "--max-staleness-ms", "8000", "--report-interval-ms", "300",
                                           "--backend", f"http://127.0.0.1:{bport}", "--control", f"http://127.0.0.1:{gport}"]))
            time.sleep(1.5)

            py, rs = f"http://127.0.0.1:{gport}", f"http://127.0.0.1:{dport}"
            tp_in = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
            adm = {"Authorization": f"Bearer {admin}"}

            def served_natively(key):
                """200 stamped X-Served-By: dataplane: answered by the data plane, not by Python behind it."""
                r = httpx.get(f"{rs}/api/users", headers={"X-API-Key": key}, timeout=10)
                return r.status_code == 200 and r.headers.get("x-served-by") == "dataplane"

            def wait_until(check, seconds=4.0):
                end = time.time() + seconds
                while time.time() < end:
                    if check():
                        return time.time()
                    time.sleep(0.05)
                return None

            assert wait_until(lambda: served_natively(keys["rs"]["key"]), 8), "data plane never started serving natively"
            hook_url, hooks = webhook_receiver()
            httpx.post(f"{py}/admin/webhooks", headers=adm, timeout=10, json={
                "url": hook_url, "events": ["rate_limit_exceeded", "circuit_breaker_open", "circuit_breaker_closed"],
            }).raise_for_status()

            def hooked(event, **data):
                return [h for h in hooks if h.get("event_type") == event
                        and all(h.get("data", {}).get(k) == v for k, v in data.items())]
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

            def check(name, ok, detail=""):
                nonlocal failures
                print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))
                failures += not ok

            print()
            # --- live control-plane link ---------------------------------------------------------
            ip_dp = httpx.get(f"{rs}/internal/dataplane/snapshot", headers={"X-Dataplane-Token": "conformance-token"}, timeout=10)
            check("/internal is not reachable through the data plane", ip_dp.status_code == 404, f"[{ip_dp.status_code}]")
            direct = httpx.get(f"{py}/internal/dataplane/snapshot", timeout=10)
            check("snapshot endpoint refuses a missing token", direct.status_code == 403, f"[{direct.status_code}]")
            snap = httpx.get(f"{py}/internal/dataplane/snapshot", headers={"X-Dataplane-Token": "conformance-token"}, timeout=10).json()
            check("snapshot holds digests, never plaintext", all(k["key"] not in json.dumps(snap) for k in keys.values()))

            new = make_key("conf-new", 1000)
            t0 = time.time()
            t = wait_until(lambda: served_natively(new["key"]))
            check("a key created after start becomes native", t is not None, f"after {t - t0:.2f}s" if t else "never")

            rev = make_key("conf-revoke", 1000)
            assert wait_until(lambda: served_natively(rev["key"])), "key never became native"
            t0 = time.time()
            httpx.delete(f"{py}/admin/api-keys/{rev['id']}", headers=adm, timeout=10).raise_for_status()
            t = wait_until(lambda: httpx.get(f"{rs}/api/users", headers={"X-API-Key": rev["key"]}, timeout=10).status_code == 401)
            check("a revoked key is refused within seconds", t is not None and t - t0 < 2.5, f"after {t - t0:.2f}s" if t else "still served")

            # --- governance and tenants, decided by the data plane itself ---------------------------
            print()

            def same(name, a, b, native=True, details=True):
                """Same status, same body in full (bar the volatile fields), and answered by the data plane."""
                compare(name, a, b)
                if details and a.status_code == b.status_code and a.status_code >= 400:
                    x, y = a.json().get("details"), b.json().get("details")
                    check(f"{name}: details match", x == y, "" if x == y else f"{x} vs {y}")
                    check(f"{name}: keys in the same order", list(a.json()) == list(b.json()), str(list(b.json())))
                if native:
                    check(f"{name}: answered by the data plane", b.headers.get("x-served-by") == "dataplane")

            def both(method, path, pair, extra=None):
                h = {"X-API-Key": "{key}", **(extra or {})}
                return call(py, method, path, h, None, pair["py"]["key"]), call(rs, method, path, h, None, pair["rs"]["key"])

            def post(path, body):
                r = httpx.post(f"{py}{path}", headers=adm, json=body, timeout=10)
                r.raise_for_status()
                return r.json()

            def scoped_key(name):
                r = httpx.post(f"{py}/admin/api-keys", headers=adm, timeout=10, params={
                    "name": name, "rate_limit_per_minute": 1000, "rate_limit_per_hour": 10000, "allowed_paths": "/api/users"})
                r.raise_for_status()
                return r.json()

            scoped = {"py": scoped_key("conf-py-scoped"), "rs": scoped_key("conf-rs-scoped")}
            assert wait_until(lambda: served_natively(scoped["rs"]["key"])), "scoped key never became native"
            same("a scoped key inside its paths", *both("GET", "/api/users/1", scoped))
            same("a scoped key outside its paths (403)", *both("GET", "/api/orders", scoped))
            a, b = both("GET", "/api/users%2F1", scoped)
            same("an escaped path is left to Python", a, b, native=False)
            check("an escaped path is not answered by the data plane", b.headers.get("x-served-by") != "dataplane")

            plain = {"py": keys["py"], "rs": keys["rs"]}
            far = {"X-Forwarded-For": "198.51.100.9"}

            def native_status(want, extra=None):
                def probe():
                    r = call(rs, "GET", "/api/users", {"X-API-Key": "{key}", **(extra or {})}, None, keys["rs"]["key"])
                    return r.status_code == want and r.headers.get("x-served-by") == "dataplane"
                return probe

            rule = post("/admin/governance/ip-rules", {"cidr": "198.51.100.0/24", "action": "deny", "scope": "global"})
            t0 = time.time()
            t = wait_until(native_status(403, far))
            check("an IP rule is enforced by the data plane within seconds", t is not None, f"after {t - t0:.2f}s" if t else "never")
            same("a denied address (403)", *both("GET", "/api/users", plain, far))
            same("another address under the same rule", *both("GET", "/api/users", plain))
            httpx.delete(f"{py}/admin/governance/ip-rules/{rule['id']}", headers=adm, timeout=10).raise_for_status()
            check("removing the rule lets the address back in", wait_until(native_status(200, far)) is not None)

            allow = post("/admin/governance/ip-rules", {"cidr": "198.51.100.0/24", "action": "allow", "scope": "global"})
            assert wait_until(native_status(403)), "allow-list never reached the data plane"
            same("an address outside the allow-list (403)", *both("GET", "/api/users", plain))
            same("an address inside the allow-list", *both("GET", "/api/users", plain, far))
            httpx.delete(f"{py}/admin/governance/ip-rules/{allow['id']}", headers=adm, timeout=10).raise_for_status()
            assert wait_until(native_status(200)), "allow-list never left the data plane"

            policy = post("/admin/governance/policies", {"name": "conf-no-deletes", "priority": 10, "action": {"type": "deny", "reason": "No deletes \u2014 read \"only\""},
                                                         "conditions": {"path": "/api/users/*", "methods": ["DELETE"]}})

            def refused_delete():
                r = call(rs, "DELETE", "/api/users/999", {"X-API-Key": "{key}"}, None, keys["rs"]["key"])
                return r.status_code == 403 and r.headers.get("x-served-by") == "dataplane"

            t0 = time.time()
            t = wait_until(refused_delete)
            check("a policy is enforced by the data plane within seconds", t is not None, f"after {t - t0:.2f}s" if t else "never")
            same("a policy refuses the request (403)", *both("DELETE", "/api/users/999", plain))
            same("the same policy lets other methods through", *both("GET", "/api/users/1", plain))
            httpx.delete(f"{py}/admin/governance/policies/{policy['id']}", headers=adm, timeout=10).raise_for_status()
            check("removing the policy lifts it", wait_until(lambda: not refused_delete()) is not None)

            # Tenant quotas are counted per clock minute: do not start a count that straddles one.
            if time.time() % 60 > 40:
                time.sleep(61 - time.time() % 60)

            def tenant(tid, per_minute, name):
                post("/admin/tenants", {"tenant_id": tid, "name": tid, "quota": {"requests_per_minute": per_minute}})
                key = make_key(name, 1000)
                post(f"/admin/tenants/{tid}/associate-api-key", {"api_key_id": key["id"], "api_key": key["key"]})
                return key

            def tenant_native(key, tid):
                def probe():
                    r = httpx.get(f"{rs}/health", timeout=10)  # not counted against any quota
                    snap = httpx.get(f"{py}/internal/dataplane/snapshot", headers={"X-Dataplane-Token": "conformance-token"}, timeout=10).json()
                    return r.status_code == 200 and any(k.get("id") == key["id"] and k.get("tenant") == tid and k.get("native_ok") for k in snap["keys"])
                return probe

            owned = {"py": tenant("conf-py-tenant", 3, "conf-py-owned"), "rs": tenant("conf-rs-tenant", 3, "conf-rs-owned")}
            assert wait_until(tenant_native(owned["rs"], "conf-rs-tenant")), "tenant key never reached the snapshot"
            time.sleep(0.6)  # one more sync, so the data plane holds that snapshot
            def tenant_used(tid):
                r = httpx.get(f"{py}/admin/tenants/{tid}/usage", headers=adm, timeout=10)
                return r.json()["current"]["minute"]["request_count"] if r.status_code == 200 else -1

            # Python counts a tenant's request once it has answered it, in the background: wait for
            # each count to land before the next request, as requests a moment apart would find it.
            pairs = []
            for i in range(4):
                pairs.append(both("GET", "/api/users", owned))
                if i < 3:
                    wait_until(lambda: tenant_used("conf-py-tenant") >= i + 1, 10)
            print("      tenant-quota statuses  python", [x.status_code for x, _ in pairs], " rust", [y.status_code for _, y in pairs])
            check("tenant quota sequence matches", [x.status_code for x, _ in pairs] == [y.status_code for _, y in pairs] == [200, 200, 200, 429])
            same("a tenant's request", *pairs[0])
            same("a tenant over its quota (429)", *pairs[3], details=False)  # they name different tenants: compared below
            fix = lambda d: json.loads(json.dumps(d).replace("conf-py-tenant", "conf-rs-tenant"))
            for (x, y), what in ((pairs[0], "served"), (pairs[3], "refused")):
                check(f"X-Tenant-ID on a {what} request", x.headers.get("x-tenant-id") == "conf-py-tenant"
                      and y.headers.get("x-tenant-id") == "conf-rs-tenant", f"{x.headers.get('x-tenant-id')} / {y.headers.get('x-tenant-id')}")
            check("the 429 names the tenant and its quota", fix(pairs[3][0].json()["details"]) == pairs[3][1].json()["details"],
                  json.dumps(pairs[3][1].json()["details"]))

            if time.time() % 60 > 45:
                time.sleep(61 - time.time() % 60)
            shared = tenant("conf-shared-tenant", 4, "conf-shared")
            assert wait_until(tenant_native(shared, "conf-shared-tenant")), "shared tenant key never reached the snapshot"
            time.sleep(0.6)
            hit = lambda base: httpx.get(f"{base}/api/users", headers={"X-API-Key": shared["key"]}, timeout=10)
            first = [hit(py).status_code for _ in range(2)]
            wait_until(lambda: tenant_used("conf-shared-tenant") >= 2, 10)  # counted by Python, in the background
            time.sleep(1.0)  # the data plane hears what Python served
            second = [hit(rs) for _ in range(3)]
            check("a quota is shared: what Python served counts in the data plane",
                  first == [200, 200] and [r.status_code for r in second] == [200, 200, 429]
                  and all(r.headers.get("x-served-by") == "dataplane" for r in second),
                  f"python {first}, then data plane {[r.status_code for r in second]}")
            # Python hears what the data plane served when its next report lands.
            wait_until(lambda: tenant_used("conf-shared-tenant") >= 4, 10)
            third = hit(py)
            check("a quota is shared: what the data plane served counts in Python", third.status_code == 429, f"[{third.status_code}]")
            usage = httpx.get(f"{py}/admin/tenants/conf-shared-tenant/usage", headers=adm, timeout=10)
            if usage.status_code == 200:
                print("      tenant usage as the product reports it:", json.dumps(usage.json())[:200])

            # --- what the data plane served is visible to the product --------------------------------
            def settled(read, quiet=1.5, seconds=20.0):
                """What ``read`` gives once it has not changed for ``quiet`` seconds: whatever earlier
                steps had the data plane report has landed by then (it reports every 0.3 s)."""
                value, since, end = read(), time.time(), time.time() + seconds
                while time.time() < end and time.time() - since < quiet:
                    time.sleep(0.1)
                    now = read()
                    if now != value:
                        value, since = now, time.time()
                return value

            def metric(name, **labels):
                want = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
                total = 0.0
                for line in httpx.get(f"{py}/metrics", timeout=10).text.splitlines():
                    if line.startswith(name + "{") or line.startswith(name + " "):
                        have = line[line.find("{") + 1: line.rfind("}")] if "{" in line else ""
                        pairs = sorted(p for p in have.split(",") if p)
                        if all(any(p == f'{k}="{v}"' for p in pairs) for k, v in labels.items()):
                            total += float(line.rsplit(" ", 1)[1])
                return total

            time.sleep(1.5)  # let earlier reports land
            probe = make_key("conf-probe", 1000)
            assert wait_until(lambda: served_natively(probe["key"])), "probe key never became native"
            time.sleep(1.5)
            req_labels = dict(method="GET", path="/api/users", status_code="200", protocol="http")
            # Reports of earlier steps may still be on their way: start from counts that have stopped moving.
            before, before_native, before_count = settled(lambda: (
                metric("gateway_requests_total", **req_labels),
                metric("gateway_dataplane_native_requests_total", status_class="2xx"),
                metric("gateway_request_duration_seconds_count", method="GET", path="/api/users", protocol="http")))
            N = 200
            for i in range(N):
                httpx.get(f"{rs}/api/users", headers={"X-API-Key": probe["key"]}, params={"i": i}, timeout=10)
            # Reports land a few times a second; on a busy machine some are late.
            wait_until(lambda: metric("gateway_requests_total", **req_labels) - before >= N
                       and metric("gateway_dataplane_native_requests_total", status_class="2xx") - before_native >= N
                       and metric("gateway_request_duration_seconds_count", method="GET", path="/api/users", protocol="http") - before_count >= N, 30)
            delta = metric("gateway_requests_total", **req_labels) - before
            check("native requests are counted exactly in gateway_requests_total", delta == N, f"{delta:.0f} of {N}")
            delta_n = metric("gateway_dataplane_native_requests_total", status_class="2xx") - before_native
            check("native share is counted in gateway_dataplane_native_requests_total", delta_n == N, f"{delta_n:.0f} of {N}")
            delta_h = metric("gateway_request_duration_seconds_count", method="GET", path="/api/users", protocol="http") - before_count
            check("latency histogram count matches", delta_h == N, f"{delta_h:.0f} of {N}")

            logs = httpx.get(f"{py}/admin/logs", params={"limit": 1000, "path": "/api/users"}, headers=adm, timeout=10).json()["logs"]
            mine = [l for l in logs if l.get("api_key_id") == probe["id"]]
            check("native requests appear in the request log with their key", len(mine) >= 1, f"{len(mine)} rows (rate-capped, errors always kept)")
            check("native log rows carry the protocol", bool(mine) and all(l.get("protocol") == "http" for l in mine))

            listed = httpx.get(f"{py}/admin/api-keys", headers=adm, timeout=10).json()
            seen = next((k for k in listed if k["id"] == probe["id"]), {})
            check("a natively used key shows a last-used time", bool(seen.get("last_used_at")), str(seen.get("last_used_at")))

            lim = keys["rs_lim"]
            logs429 = httpx.get(f"{py}/admin/logs", params={"limit": 1000, "status_code": 429}, headers=adm, timeout=10).json()["logs"]
            check("a native 429 is logged with its key", any(l.get("api_key_id") == lim["id"] for l in logs429))
            check("a native 429 feeds the rate-limit metric", metric("gateway_rate_limit_hits_total", client_id=lim["id"]) >= 1)

            got = hooked("rate_limit_exceeded", api_key_id=lim["id"])
            check("a native 429 fires the rate-limit webhook", len(got) >= 1,
                  json.dumps(got[0]["data"], sort_keys=True) if got else "none received")
            theirs = hooked("rate_limit_exceeded", api_key_id=keys["py_lim"]["id"])
            if got and theirs:
                extra = set(got[0]["data"]) ^ set(theirs[0]["data"])
                check("the rate-limit webhook carries the same fields (plus a count)", extra == {"count"}, str(sorted(extra)))

            # --- the backend goes away ---------------------------------------------------------------
            print()
            bkey = {"py": make_key("conf-py-breaker", 1000), "rs": make_key("conf-rs-breaker", 1000)}
            assert wait_until(lambda: served_natively(bkey["rs"]["key"])), "breaker key never became native"
            time.sleep(1.5)
            errs0 = (metric("gateway_errors_total", error_type="BACKEND_ERROR", protocol="gateway"),
                     metric("gateway_errors_total", error_type="SERVICE_UNAVAILABLE", protocol="gateway"),
                     metric("gateway_errors_total", error_type="ConnectError", protocol="unknown"))
            before_errs_delta = lambda now: tuple(n - o for n, o in zip(now, errs0))
            procs[0].send_signal(signal.SIGTERM)
            procs[0].wait(timeout=15)
            get = lambda base, who: call(base, "GET", "/api/users", {"X-API-Key": "{key}"}, None, bkey[who]["key"])
            # The data plane first and alone, so the webhook it causes can be told from Python's own.
            b = [get(rs, "rs") for _ in range(6)]
            check("the data plane answered these itself", all(r.headers.get("x-served-by") == "dataplane" for r in b))
            t = wait_until(lambda: hooked("circuit_breaker_open"), 4)
            opened = hooked("circuit_breaker_open")
            check("the breaker opening in the data plane fires the webhook", t is not None and len(opened) == 1,
                  json.dumps(opened[0]["data"], sort_keys=True) if opened else "none received")
            time.sleep(1.5)
            errs = lambda **l: metric("gateway_errors_total", **l)
            seen_rs = (errs(error_type="BACKEND_ERROR", protocol="gateway"), errs(error_type="SERVICE_UNAVAILABLE", protocol="gateway"),
                       errs(error_type="ConnectError", protocol="unknown"))
            a = [get(py, "py") for _ in range(6)]
            time.sleep(0.5)
            seen_py = (errs(error_type="BACKEND_ERROR", protocol="gateway") - seen_rs[0],
                       errs(error_type="SERVICE_UNAVAILABLE", protocol="gateway") - seen_rs[1],
                       errs(error_type="ConnectError", protocol="unknown") - seen_rs[2])
            print("      gateway_errors_total (BACKEND_ERROR, SERVICE_UNAVAILABLE, ConnectError)  from the data plane",
                  before_errs_delta(seen_rs), " from python", seen_py)
            check("upstream failures feed gateway_errors_total the same way", before_errs_delta(seen_rs) == seen_py)
            print("      backend-down statuses  python", [r.status_code for r in a], " rust", [r.status_code for r in b])
            compare("backend unreachable (502 shape)", a[0], b[0])
            compare("circuit open (503 shape)", a[5], b[5])
            if [r.status_code for r in a] != [r.status_code for r in b]:
                print("FAIL  backend-down sequence differs"); failures += 1
            for name, x, y in (("502", a[0], b[0]), ("503", a[5], b[5])):
                if x.status_code == y.status_code and x.status_code >= 500 and x.json().get("details") != y.json().get("details"):
                    print(f"FAIL  {name} details differ: {x.json().get('details')} vs {y.json().get('details')}"); failures += 1
            wait_until(lambda: len(hooked("circuit_breaker_open")) == 2, 3)
            both = hooked("circuit_breaker_open")
            if len(both) == 2:
                when = lambda d: {k: ("<when>" if k == "opened_at" else v) for k, v in d.items()}
                x, y = when(both[0]["data"]), when(both[1]["data"])
                check("the open webhook carries the same payload from both", x == y,
                      "" if x == y else f"{json.dumps(x, sort_keys=True)} vs {json.dumps(y, sort_keys=True)}")
            else:
                check("Python's own breaker also announced opening", False, f"{len(both)} open webhooks")

            procs[0] = start_backend()
            wait_healthy(f"http://127.0.0.1:{bport}/health", procs[0])
            time.sleep(2.2)  # past CIRCUIT_BREAKER_TIMEOUT: half-open
            b = get(rs, "rs")
            check("after the wait the data plane tries the backend again", b.status_code == 200
                  and b.headers.get("x-served-by") == "dataplane", f"[{b.status_code}]")
            t = wait_until(lambda: hooked("circuit_breaker_closed"), 4)
            check("the breaker closing in the data plane fires the webhook", t is not None and len(hooked("circuit_breaker_closed")) == 1)
            compare("backend back (200)", get(py, "py"), get(rs, "rs"))
            wait_until(lambda: len(hooked("circuit_breaker_closed")) == 2, 3)
            both = hooked("circuit_breaker_closed")
            check("the closed webhook carries the same payload from both",
                  len(both) == 2 and mask(both[0]["data"]) == mask(both[1]["data"]), f"{len(both)} closed webhooks")

            # The backend accepts the connection and then says nothing.
            time.sleep(1.5)
            slow = lambda: errs(error_type="ReadTimeout", protocol="unknown")
            s0 = slow()
            procs[0].send_signal(signal.SIGSTOP)
            try:
                b = get(rs, "rs")
                time.sleep(1.0)
                s1 = slow()
                a = get(py, "py")
            finally:
                procs[0].send_signal(signal.SIGCONT)
            time.sleep(0.5)
            same("backend too slow to answer (timeout)", a, b)
            check("a timeout is counted under the same error type by both", (s1 - s0, slow() - s1) == (1.0, 1.0),
                  f"data plane {s1 - s0:.0f}, python {slow() - s1:.0f}")
            compare("backend answering again (200)", get(py, "py"), get(rs, "rs"))

            # Last: the control plane goes away. The data plane must stop serving natively.
            procs[1].send_signal(signal.SIGTERM)
            procs[1].wait(timeout=15)
            t = wait_until(lambda: httpx.get(f"{rs}/api/users", headers={"X-API-Key": keys["rs"]["key"]}, timeout=10).status_code >= 500, 20)
            check("with the control plane down, native serving stops (fails closed)", t is not None)

            known = sorted(h for h in gaps if h not in PROMISED)
            print(f"\nheader names present on only one side (known gaps, not failures): {known or 'none'}")
            if failures:
                gateway_log.flush()
                with open(gateway_log.name, errors="replace") as f:
                    said = [line.rstrip() for line in f if line.strip()]
                print(f"\nwhat the Python gateway logged ({len(said)} lines, the last 60):")
                for line in said[-60:]:
                    print("      " + line[:300])
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
