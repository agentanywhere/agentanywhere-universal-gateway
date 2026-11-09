#!/bin/bash
# One constant-rate run with vegeta, summarised over the steady-state window.
#   attack.sh <url> <rate> <duration-s> <warmup-s> <outfile-prefix> [GET|POST] [key-file]
# Writes <prefix>.ndjson (per request) and prints the summary JSON.
set -euo pipefail
URL=$1; RATE=$2; DUR=$3; WARM=$4; OUT=$5; METHOD=${6:-GET}; KEYFILE=${7:-}
HERE=$(cd "$(dirname "$0")" && pwd)
TARGETS=$(mktemp)
{
  echo "$METHOD $URL"
  [ -n "$KEYFILE" ] && echo "X-API-Key: $(cat "$KEYFILE")"
  if [ "$METHOD" = POST ]; then
    echo "Content-Type: application/json"
    BODY=$(mktemp); echo '{"name":"bench","email":"bench@example.com"}' > "$BODY"; echo "@$BODY"
  fi
} > "$TARGETS"
# Enough workers that a slow server shows up as latency, not as a client-side limit.
vegeta attack -targets="$TARGETS" -rate="$RATE" -duration="${DUR}s" -timeout=30s -max-workers=4000 -workers=100 \
  | vegeta encode --to json > "$OUT.ndjson"
python3 "$HERE/summarize.py" --warmup "$WARM" --rate "$RATE" "$OUT.ndjson"
rm -f "$TARGETS" ${BODY:-}
