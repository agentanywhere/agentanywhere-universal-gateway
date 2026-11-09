#!/bin/bash
# Record CPU and memory on the gateway host.  capture.sh start <dir> | capture.sh stop
set -euo pipefail
case "${1:?}" in
  start)
    DIR=${2:?}; mkdir -p "$DIR"
    nohup mpstat -P ALL 1 > "$DIR/mpstat.txt" 2>&1 & echo $! > "$DIR/.mpstat.pid"
    nohup pidstat -u -r -C python 1 > "$DIR/pidstat.txt" 2>&1 & echo $! > "$DIR/.pidstat.pid"
    nohup vmstat 1 > "$DIR/vmstat.txt" 2>&1 & echo $! > "$DIR/.vmstat.pid"
    echo "capturing to $DIR" ;;
  stop)
    DIR=${2:?}
    for f in mpstat pidstat vmstat; do [ -f "$DIR/.$f.pid" ] && kill "$(cat "$DIR/.$f.pid")" 2>/dev/null || true; done
    echo stopped ;;
esac
