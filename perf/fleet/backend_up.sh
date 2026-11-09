#!/bin/bash
# Start the benchmark backend (perf/fleet/bench_backend.py) with WORKERS processes on port 8003.
set -euo pipefail
WORKERS=${WORKERS:-3}
pkill -f 'bench_backend' 2>/dev/null || true
pkill -f 'multiprocessing' 2>/dev/null || true
for _ in $(seq 1 40); do ss -ltn | grep -q ':8003 ' || break; sleep 0.5; done
cd "$HOME/gw"
nohup .venv/bin/uvicorn --app-dir perf/fleet bench_backend:app --host 0.0.0.0 --port 8003 --workers "$WORKERS" \
  --log-level warning > "$HOME/backend.log" 2>&1 &
for _ in $(seq 1 30); do curl -sf -o /dev/null http://127.0.0.1:8003/ && break; sleep 1; done
curl -sf -o /dev/null http://127.0.0.1:8003/ && echo "backend up: workers=$WORKERS"
