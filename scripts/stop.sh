#!/usr/bin/env bash
set -uo pipefail
source "$(dirname "$0")/env.sh"
if [[ -f "$PROJ/logs/server.pid" ]]; then
  PID=$(cat "$PROJ/logs/server.pid")
  if kill -0 "$PID" 2>/dev/null; then kill "$PID"; echo "stopped $PID"; else echo "not running ($PID)"; fi
  rm -f "$PROJ/logs/server.pid"
else
  pkill -f llama-kvmem-server && echo "stopped via pkill" || echo "no server found"
fi
