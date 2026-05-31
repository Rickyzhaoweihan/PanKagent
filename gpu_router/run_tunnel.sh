#!/bin/bash
# Start the SSH tunnel supervisor (nohup, background). It authenticates with the
# password from ../.env + a Duo push (approve on your phone), then holds the four
# -L forwards to the cluster GPUs and auto-reconnects on drops.
#
# Run from anywhere:  bash gpu_router/run_tunnel.sh
# Python: set GPU_ROUTER_PYTHON to the interpreter that has pexpect+python-dotenv
# (the app's env). Defaults to `python3` on PATH.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${GPU_ROUTER_PYTHON:-python3}"
mkdir -p "${DIR}/logs"

# Refuse to double-start.
if [[ -f "${DIR}/tunnel.pid" ]] && kill -0 "$(cat "${DIR}/tunnel.pid")" 2>/dev/null; then
  echo "tunnel already running (pid $(cat "${DIR}/tunnel.pid"))"
  exit 0
fi

cd "${DIR}"
nohup "${PY}" tunnel_supervisor.py >> "${DIR}/logs/tunnel.nohup.log" 2>&1 &
echo $! > "${DIR}/tunnel.pid"
disown
echo "tunnel supervisor started (pid $(cat "${DIR}/tunnel.pid")). Approve the Duo push on your phone."
echo "Logs: ${DIR}/logs/tunnel.log"
