#!/bin/bash
# Start one comparison target on the gateway host, listening on :8000 in front of the
# benchmark backend, pinned to the cores in CORES (default 0-1), with a static API key
# required on every request. Writes the key the load generator must send to ~/run/key.
#
#   TARGET=nginx|python|python-nodb|rust|agentgateway|litellm  BACKEND_URL=http://10.60.1.5:8003  bash target_up.sh
#
# Expects the repository in ~/gw with its virtualenv (and dataplane/target/release/dataplane
# for rust), and ~/gw/run/secrets.env with ADMIN_TOKEN and SECRET_KEY.
set -euo pipefail
TARGET=${TARGET:?}; BACKEND_URL=${BACKEND_URL:?}; CORES=${CORES:-0-1}
NCORES=$(( ${CORES#*-} - ${CORES%-*} + 1 ))
HERE=$(cd "$(dirname "$0")" && pwd)
RUN=$HOME/run; mkdir -p "$RUN"; chmod 700 "$RUN"

stop_all() {
  pkill -f 'uvicorn app.main' 2>/dev/null || true
  pkill -f 'multiprocessing' 2>/dev/null || true
  pkill -f 'release/dataplane' 2>/dev/null || true
  pkill -f 'agentgateway' 2>/dev/null || true
  pkill -f 'litellm' 2>/dev/null || true
  sudo pkill -f 'nginx: master' 2>/dev/null || true
  for _ in $(seq 1 40); do ss -ltn | grep -qE ':(8000|8001) ' || return 0; sleep 0.5; done
  pkill -9 -f 'uvicorn|multiprocessing|dataplane|agentgateway|litellm' 2>/dev/null || true
  sudo pkill -9 nginx 2>/dev/null || true
  sleep 1
}
wait_up() {   # url, then optional header
  for _ in $(seq 1 120); do curl -s -o /dev/null -m 2 ${2:+-H "$2"} "$1" && return 0; sleep 1; done
  echo "target did not come up at $1" >&2; return 1
}
static_key() { [ -s "$RUN/static.key" ] || (umask 077; openssl rand -hex 24 > "$RUN/static.key"); cat "$RUN/static.key"; }

# The Python gateway, pinned, on PORT; issues an API key into $RUN/key.
python_up() {
  local port=$1; shift
  set -a; . "$HOME/gw/run/secrets.env"; set +a
  rm -f "$RUN"/gateway.db "$RUN"/gateway.db-*
  export DATABASE_URL=sqlite+aiosqlite:///$RUN/gateway.db
  export HTTP_BACKEND_URL=$BACKEND_URL REDIS_ENABLED=false LOG_LEVEL=WARNING STARTUP_BANNER=false DEBUG=false
  for kv in "$@"; do export "$kv"; done
  cd "$HOME/gw"
  nohup taskset -c "${PY_CORES:-$CORES}" .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "$port" --workers "$NCORES" \
    --log-level warning > "$RUN/python.log" 2>&1 &
  wait_up "http://127.0.0.1:$port/health"
  for _ in $(seq 1 60); do
    if curl -sf -X POST "http://127.0.0.1:$port/admin/api-keys" -H "Authorization: Bearer $ADMIN_TOKEN" \
        -H 'Content-Type: application/json' \
        -d '{"name":"bench","rate_limit_per_minute":1000000000,"rate_limit_per_hour":1000000000}' \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["key"])' > "$RUN/key.tmp" 2>/dev/null && [ -s "$RUN/key.tmp" ]; then
      mv "$RUN/key.tmp" "$RUN/key"; chmod 600 "$RUN/key"; return 0
    fi
    sleep 1
  done
  echo "could not create an API key" >&2; tail -20 "$RUN/python.log" >&2; return 1
}

stop_all
case $TARGET in
  nginx)
    KEY=$(static_key); cp "$RUN/static.key" "$RUN/key"
    sed -e "s#@KEY@#$KEY#" -e "s#@BACKEND_HOSTPORT@#${BACKEND_URL#http://}#" -e "s#@WORKERS@#$NCORES#" "$HERE/nginx.conf" > "$RUN/nginx.conf"
    sudo taskset -c "$CORES" nginx -c "$RUN/nginx.conf"
    ;;
  python)        # as shipped: request log written to SQLite on every request
    python_up 8000 ;;
  python-nodb)   # request-log writes off
    python_up 8000 REQUEST_LOG_MODE=off ;;
  python-tuned)  # request-log writes and per-request AI scoring off, validated keys cached 5 s
    python_up 8000 REQUEST_LOG_MODE=off AI_ANALYTICS_ENABLED=false API_KEY_LOCAL_CACHE_TTL_SECONDS=5 ;;
  rust|rust-split)   # the Rust data plane in front of the Python gateway; rust-split puts the
                     # Python control plane on the next core, off the request path's core
    [ "$TARGET" = rust-split ] && PY_CORES=$(( ${CORES#*-} + 1 ))
    TOKEN=$(openssl rand -hex 24)
    python_up 8001 DATAPLANE_TOKEN="$TOKEN" TRUSTED_PROXIES=127.0.0.1 REQUEST_LOG_MODE=off AI_ANALYTICS_ENABLED=false
    nohup taskset -c "$CORES" "$HOME/gw/dataplane/target/release/dataplane" --listen 0.0.0.0:8000 \
      --backend "$BACKEND_URL" --control http://127.0.0.1:8001 --control-token "$TOKEN" \
      --workers "$NCORES" --metrics-listen 127.0.0.1:9101 > "$RUN/dataplane.log" 2>&1 &
    wait_up "http://127.0.0.1:8000/api/bench" "X-API-Key: $(cat "$RUN/key")"
    # Wait until the data plane serves requests itself rather than handing them over.
    for _ in $(seq 1 30); do
      curl -s -o /dev/null -H "X-API-Key: $(cat "$RUN/key")" http://127.0.0.1:8000/api/bench
      curl -s http://127.0.0.1:9101/metrics | grep -q '^dataplane_snapshot_fresh 1' && break; sleep 1
    done
    ;;
  agentgateway)
    KEY=$(static_key); cp "$RUN/static.key" "$RUN/key"
    sed -e "s#@KEY@#$KEY#" -e "s#@BACKEND_HOSTPORT@#${BACKEND_URL#http://}#" -e "s#@THREADS@#$NCORES#" "$HERE/agentgateway.yaml" > "$RUN/agentgateway.yaml"
    nohup taskset -c "$CORES" env ${AGW_ENV:-} "$HOME/bin/agentgateway" -f "$RUN/agentgateway.yaml" > "$RUN/agentgateway.log" 2>&1 &
    ;;
  litellm)
    KEY=sk-$(static_key); printf %s "$KEY" > "$RUN/key"
    sed -e "s#@BACKEND@#$BACKEND_URL#" "$HERE/litellm.yaml" > "$RUN/litellm.yaml"
    cd "$HOME"
    LITELLM_MASTER_KEY=$KEY LITELLM_LOG=ERROR nohup taskset -c "$CORES" "$HOME/litellm-venv/bin/litellm" --config "$RUN/litellm.yaml" \
      --host 0.0.0.0 --port 8000 --num_workers "$NCORES" > "$RUN/litellm.log" 2>&1 &
    ;;
  *) echo "unknown TARGET $TARGET" >&2; exit 2 ;;
esac
wait_up "http://127.0.0.1:8000/api/bench" "X-API-Key: $(cat "$RUN/key")"
code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:8000/api/bench")
ok=$(curl -s -o /dev/null -w '%{http_code}' -H "X-API-Key: $(cat "$RUN/key")" -H "Authorization: Bearer $(cat "$RUN/key")" "http://127.0.0.1:8000/api/bench")
echo "target up: $TARGET cores=$CORES without-key=$code with-key=$ok"
[ "$ok" = 200 ] && [ "$code" != 200 ] || { echo "key check failed (without=$code with=$ok)" >&2; exit 1; }
