#!/bin/bash
# Stop the router and/or the tunnel supervisor.
#   bash gpu_router/stop.sh           # stop both
#   bash gpu_router/stop.sh router    # stop only the router
#   bash gpu_router/stop.sh tunnel    # stop only the tunnel
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

stop_one() {
  local name="$1" pidfile="${DIR}/$1.pid"
  if [[ -f "$pidfile" ]]; then
    local pid; pid="$(cat "$pidfile")"
    if kill -0 "$pid" 2>/dev/null; then
      echo "stopping ${name} (pid ${pid})"
      kill "$pid" 2>/dev/null || true
    else
      echo "${name}: pid ${pid} not running"
    fi
    rm -f "$pidfile"
  else
    echo "${name}: no pid file"
  fi
}

case "${1:-all}" in
  router) stop_one router ;;
  tunnel) stop_one tunnel ;;
  all)    stop_one router; stop_one tunnel ;;
  *)      echo "usage: stop.sh [router|tunnel|all]"; exit 1 ;;
esac
