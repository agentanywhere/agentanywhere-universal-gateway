"""The fleet tooling's pure functions: run summary, host statistics and the report."""
import json

import pytest

from perf.fleet import host_stats, report, summarize


def _line(ts, code, latency_ms):
    return json.dumps({"timestamp": ts, "code": code, "latency": int(latency_ms * 1e6)})


def test_summarize_drops_the_warmup_and_counts_failures():
    lines = [_line(f"2026-11-09T10:00:{s:02d}.500000000Z", 200, 5) for s in range(0, 20)]
    lines += [_line("2026-11-09T10:00:15.700000000Z", 0, 30000)]          # a timeout in the window
    lines += [_line("2026-11-09T10:00:16.100000000Z", 503, 12)]
    out = summarize.summarize(lines, warmup=10, rate=1)
    assert out["requests"] == 12 and out["window_s"] == pytest.approx(9.2, abs=0.6)
    assert out["error_rate"] == pytest.approx(2 / 12, abs=1e-3)
    assert out["p50_ms"] == 5.0 and out["max_ms"] == 30000.0 and out["p999_ms"] == 30000.0


def test_summarize_empty_input():
    assert summarize.summarize([], warmup=10) == {"requests": 0}


def test_percentile_nearest_rank():
    data = [float(i) for i in range(1, 1001)]
    assert summarize.percentile(data, 50) == 500.0
    assert summarize.percentile(data, 99.9) == 999.0


def test_host_stats_reads_mpstat_and_pidstat(tmp_path):
    mp = ["Linux 7.0 (h)\t11/09/26\t_x86_64_\t(2 CPU)", "",
          "16:17:03     CPU    %usr   %nice    %sys %iowait    %irq   %soft  %steal  %guest  %gnice   %idle"]
    for sec in range(4, 8):
        mp.append(f"16:17:{sec:02d}     all   50.00    0.00    0.00    0.00    0.00    0.00    0.00    0.00    0.00   50.00")
        mp.append(f"16:17:{sec:02d}       0   95.00    0.00    0.00    0.00    0.00    0.00    0.00    0.00    0.00    5.00")
        mp.append(f"16:17:{sec:02d}       1    5.00    0.00    0.00    0.00    0.00    0.00    0.00    0.00    0.00   95.00")
    (tmp_path / "mp.txt").write_text("\n".join(mp))
    pid = ["Linux", "", "16:17:03      UID       PID    %usr %system  %guest   %wait    %CPU   CPU  Command"]
    for sec in range(4, 8):
        pid.append(f"16:17:{sec:02d}     1000       11   60.00    5.00    0.00    1.00   65.00     2  python3")
        pid.append(f"16:17:{sec:02d}     1000       12   30.00    5.00    0.00    1.00   35.00     3  python3")
    pid += ["", "16:17:03      UID       PID  minflt/s  majflt/s     VSZ     RSS   %MEM  Command"]
    for sec in range(4, 8):
        pid.append(f"16:17:{sec:02d}     1000       11      1.00      0.00  900000  512000   3.00  python3")
    (tmp_path / "pid.txt").write_text("\n".join(pid))
    mp_data = host_stats.parse_mpstat(str(tmp_path / "mp.txt"), warmup=1)
    assert set(mp_data) == {"all", "0", "1"} and len(mp_data["all"]) == 3
    cpu, rss = host_stats.parse_pidstat(str(tmp_path / "pid.txt"), warmup=1)
    assert cpu == [100.0] * 3 and max(rss) == pytest.approx(500.0)


def _row(config, workers, rate, err, p50=5.0, p99=11.0):
    return {"label": "t", "config": config, "workers": workers, "target_rate": float(rate),
            "error_rate": err, "p50_ms": p50, "p99_ms": p99}


def test_report_has_a_table_per_worker_count_and_marks_failures():
    rows = [_row("direct-backend", None, 100, 0.0, 1.3, 4.0), _row("direct-backend", None, 200, 0.0, 1.3, 4.0),
            _row("nodb", 4, 100, 0.0), _row("nodb", 4, 200, 0.5, 900.0, 15000.0),
            _row("nodb", 8, 100, 0.0), _row("nodb", 8, 200, 0.0)]
    md = report.tables(rows, fail=0.01)
    assert md.count("### ") == 2 and "### 4 gateway workers" in md and "### 8 gateway workers" in md
    assert "| config | 100 req/s | 200 req/s |" in md
    assert "| backend direct | 100% 1.3/4.0 | 100% 1.3/4.0 |" in md
    assert "**x** 50% 900/15000" in md
    assert "| nodb | 100% 5.0/11.0 | 100% 5.0/11.0 |" in md.split("### 8 gateway workers")[1]


def test_report_filters_by_label(tmp_path):
    p = tmp_path / "r.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in [_row("nodb", 4, 100, 0.0), {**_row("nodb", 4, 100, 0.9), "label": "old"}]))
    assert len(report.load(str(p), "t")) == 1 and len(report.load(str(p), None)) == 2
