#!/bin/bash
# Start the gateway for one benchmark configuration and create an API key for the load.
#   CONFIG   nodb | sqlite | sqlite-wal | postgres, optionally with -batched
#            (nodb = request-log writes off; -batched = REQUEST_LOG_MODE=batched)
#   WORKERS  uvicorn worker processes
# Expects, in ~/gw/run: secrets.env (ADMIN_TOKEN, SECRET_KEY) and, for postgres, pg.url
# (DATABASE_URL). Run from the repository root on the gateway host.
set -euo pipefail
CONFIG=${CONFIG:?}; WORKERS=${WORKERS:?}
BACKEND_URL=${BACKEND_URL:-http://10.42.1.5:8003}
RUN=$HOME/gw/run; mkdir -p "$RUN"
# Stop every gateway process, workers included: a worker outlives its master, keeps the
# listening socket and would keep answering with a database that has since been deleted.
pkill -f 'uvicorn app.main' 2>/dev/null || true
pkill -f 'multiprocessing' 2>/dev/null || true
for _ in $(seq 1 40); do ss -ltn | grep -q ':8000 ' || break; sleep 0.5; done
if ss -ltn | grep -q ':8000 '; then pkill -9 -f 'multiprocessing' 2>/dev/null || true; pkill -9 -f 'uvicorn' 2>/dev/null || true; sleep 1; fi
ss -ltn | grep -q ':8000 ' && { echo "port 8000 is still in use" >&2; exit 1; }
set -a; . "$RUN/secrets.env"; set +a

base=${CONFIG%-batched}
export REQUEST_LOG_MODE=inline
[[ $CONFIG == *-batched ]] && export REQUEST_LOG_MODE=batched
[[ $base == nodb ]] && export REQUEST_LOG_MODE=off
case $base in
  nodb|sqlite) DB=sqlite; export SQLITE_JOURNAL_MODE=DELETE SQLITE_SYNCHRONOUS=FULL SQLITE_BUSY_TIMEOUT_MS=0 ;;
  sqlite-wal)  DB=sqlite; export SQLITE_JOURNAL_MODE=WAL SQLITE_SYNCHRONOUS=NORMAL SQLITE_BUSY_TIMEOUT_MS=5000 ;;
  postgres)    DB=postgres ;;
  *) echo "unknown CONFIG $CONFIG" >&2; exit 2 ;;
esac
if [[ $DB == postgres ]]; then
  export DATABASE_URL=$(cat "$RUN/pg.url")
else
  rm -f "$RUN"/gateway.db "$RUN"/gateway.db-*
  export DATABASE_URL=sqlite+aiosqlite:///$RUN/gateway.db
fi
# Extra settings for this run, for example GATEWAY_ENV="API_KEY_LOCAL_CACHE_TTL_SECONDS=5".
if [ -n "${GATEWAY_ENV:-}" ]; then export $GATEWAY_ENV; fi
export HTTP_BACKEND_URL=$BACKEND_URL REDIS_ENABLED=false LOG_LEVEL=WARNING STARTUP_BANNER=false DEBUG=false

cd "$HOME/gw"
nohup .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers "$WORKERS" --log-level warning \
  > "$RUN/gateway.log" 2>&1 &
for _ in $(seq 1 90); do
  curl -sf -o /dev/null http://127.0.0.1:8000/health && break
  sleep 1
done
curl -sf -o /dev/null http://127.0.0.1:8000/health || { tail -20 "$RUN/gateway.log" >&2; exit 1; }
# /health answers before every worker has created the tables, so retry until a key is issued.
for _ in $(seq 1 60); do
  if curl -sf -X POST "http://127.0.0.1:8000/admin/api-keys?name=bench&rate_limit_per_minute=1000000000&rate_limit_per_hour=1000000000" \
      -H "Authorization: Bearer $ADMIN_TOKEN" 2>/dev/null \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["key"])' > "$RUN/key" 2>/dev/null && [ -s "$RUN/key" ]; then
    break
  fi
  sleep 1
done
[ -s "$RUN/key" ] || { echo "could not create an API key" >&2; tail -20 "$RUN/gateway.log" >&2; exit 1; }
chmod 600 "$RUN/key"
if [ "$WORKERS" -gt 1 ]; then
  sleep 2
  ALIVE=$(pgrep -fc 'multiprocessing.spawn' || true)
  [ "$ALIVE" -eq "$WORKERS" ] || { echo "only $ALIVE of $WORKERS workers are running" >&2; tail -30 "$RUN/gateway.log" >&2; exit 1; }
fi
echo "gateway up: config=$CONFIG workers=$WORKERS mode=$REQUEST_LOG_MODE db=$DB"
