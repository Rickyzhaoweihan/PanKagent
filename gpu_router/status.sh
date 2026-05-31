#!/bin/bash
# One-shot status: process liveness + per-backend health (via the router) +
# local tunnelled-port reachability.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROUTER_PORT="${ROUTER_PORT:-8010}"

proc_state() {
  local name="$1" pidfile="${DIR}/$1.pid"
  if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    echo "RUNNING (pid $(cat "$pidfile"))"
  else
    echo "STOPPED"
  fi
}

echo "tunnel supervisor : $(proc_state tunnel)"
echo "router            : $(proc_state router)"
echo
echo "Tunnelled local ports:"
for p in 7000 7001 7002 7003; do
  if (exec 3<>"/dev/tcp/127.0.0.1/${p}") 2>/dev/null; then
    echo "  ${p}: open"
  else
    echo "  ${p}: CLOSED"
  fi
done
echo
echo "Router health (:${ROUTER_PORT}/health):"
curl -s --max-time 5 "http://127.0.0.1:${ROUTER_PORT}/health" 2>/dev/null \
  | python3 -m json.tool 2>/dev/null \
  || echo "  (router not reachable)"
