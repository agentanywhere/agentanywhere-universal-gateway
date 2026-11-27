#!/bin/bash
# One constant-rate run with vegeta against a comparison target, summarised over the
# steady-state window.   attack.sh <url> <rate> <duration-s> <warmup-s> <outfile-prefix> <header-prefix>
# <header-prefix> is "X-API-Key:" or "Authorization: Bearer"; the key is read from ~/run/key.
set -euo pipefail
URL=$1; RATE=$2; DUR=$3; WARM=$4; OUT=$5; HDR=$6
mkdir -p "$(dirname "$OUT")"
TARGETS=$(mktemp)
printf 'GET %s\n%s %s\n' "$URL" "$HDR" "$(cat ~/run/key)" > "$TARGETS"
ulimit -n 1048576 2>/dev/null || ulimit -n 65535
vegeta attack -targets="$TARGETS" -rate="$RATE" -duration="${DUR}s" -timeout=10s -max-workers=8000 -workers=200 \
  -keepalive=true > "$OUT.bin"
# Encode after the run, so the load generator spends its CPU on sending during it.
vegeta encode --to json < "$OUT.bin" > "$OUT.ndjson"
rm -f "$OUT.bin"
rm -f "$TARGETS"
python3 ~/gw/perf/fleet/summarize.py --warmup "$WARM" --rate "$RATE" "$OUT.ndjson"
rm -f "$OUT.ndjson"
