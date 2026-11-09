"""The load generator's maths, thresholds and request loop (no real server)."""
import asyncio
import json

import httpx
import pytest

from perf import load_test as lt
from perf import suite


def test_percentile_is_nearest_rank():
    data = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    assert lt.percentile(data, 50) == 5.0
    assert lt.percentile(data, 90) == 9.0
    assert lt.percentile(data, 99) == 10.0
    assert lt.percentile(data, 0) == 1.0
    assert lt.percentile([], 95) == 0.0
    assert lt.percentile([7.0], 99) == 7.0


def test_summarize_counts_wrong_statuses_as_errors():
    r = lt.summarize("x", [0.001, 0.002, 0.003, 0.004], {200: 3, 500: 1}, errors=2, duration_s=2.0, expect_status=None)
    assert r.requests == 4 and r.errors == 3          # 1 server error + 2 transport errors
    assert r.rps == 2.0 and r.min_ms == 1.0 and r.max_ms == 4.0 and r.mean_ms == 2.5
    assert r.status_counts == {"200": 3, "500": 1}
    assert r.error_rate == 0.75


def test_expected_status_is_not_an_error():
    r = lt.summarize("deny", [0.001] * 3, {401: 3}, errors=0, duration_s=1.0, expect_status=401)
    assert r.errors == 0
    r = lt.summarize("deny", [0.001] * 3, {200: 3}, errors=0, duration_s=1.0, expect_status=401)
    assert r.errors == 3


def test_empty_run_is_all_error_rate():
    r = lt.summarize("none", [], {}, errors=0, duration_s=1.0, expect_status=None)
    assert r.requests == 0 and r.rps == 0.0 and r.error_rate == 1.0 and r.p95_ms == 0.0


def _result(name, p95, errors=0, requests=100):
    return lt.Result(name, requests, errors, 1.0, 100.0, 1, 1, 1, 1, p95, p95, p95)


def test_thresholds_report_each_breach():
    results = [_result("fast", 5.0), _result("slow", 500.0), _result("flaky", 5.0, errors=10)]
    failures = lt.check_thresholds(results, max_p95_ms=100.0, max_error_rate=0.05)
    assert len(failures) == 2
    assert "slow" in failures[0] and "p95" in failures[0]
    assert "flaky" in failures[1] and "error rate" in failures[1]
    assert lt.check_thresholds(results, None, None) == []


def test_header_parsing():
    assert lt._parse_headers(["A: 1", "B:two"]) == {"A": "1", "B": "two"}
    with pytest.raises(SystemExit):
        lt._parse_headers(["nonsense"])


def test_run_load_measures_status_and_sends_headers_and_body():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.headers.get("x-api-key"), request.content))
        return httpx.Response(201)

    result = asyncio.run(
        lt.run_load(
            name="t", url="http://gw.test/x", method="POST", headers={"X-API-Key": "k"}, body='{"a":1}',
            concurrency=4, duration=0.2, warmup=0.0, transport=httpx.MockTransport(handler),
        )
    )
    assert result.requests > 0 and result.errors == 0 and result.status_counts == {"201": result.requests}
    assert {s[:2] for s in seen} == {("POST", "k")} and {s[2] for s in seen} == {b'{"a":1}'}


def test_run_load_counts_transport_failures_and_discards_warmup():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    result = asyncio.run(
        lt.run_load(name="down", url="http://gw.test/", concurrency=2, duration=0.1, warmup=0.1,
                    transport=httpx.MockTransport(boom))
    )
    assert result.requests == 0 and result.errors > 0 and result.error_rate == 1.0


def test_cli_exit_status_follows_thresholds(monkeypatch, tmp_path, capsys):
    async def fake(**kw):
        return _result(kw["name"], 250.0)

    monkeypatch.setattr(lt, "run_load", fake)
    out = tmp_path / "r.json"
    assert lt.main(["--path", "/p", "--api-key", "k", "--json", str(out)]) == 0
    assert json.loads(out.read_text())["p95_ms"] == 250.0
    assert lt.main(["--max-p95-ms", "100"]) == 1
    assert "FAIL" in capsys.readouterr().err


def test_overhead_and_markdown_report():
    direct = _result("backend direct", 10.0)
    direct.p50_ms = 4.0
    proxied = _result("proxy GET /api/users", 25.0)
    proxied.p50_ms = 9.0
    assert suite.overhead_ms([direct, proxied]) == {"p50": 5.0, "p95": 15.0}
    assert suite.overhead_ms([direct]) is None
    md = suite.render_markdown([direct, proxied], {"python": "3.12", "cpu_count": 8})
    assert "| python | 3.12 |" in md and "| proxy GET /api/users |" in md
    assert "p50 +5.0 ms" in md and "indicative" in md


def test_free_port_is_usable():
    import socket
    port = suite.free_port()
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))
