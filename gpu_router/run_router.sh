#!/bin/bash
# Start the load-balancing router (nohup, background) on :8010. It fans requests
# across the 5 vLLM backends (4 tunnelled + the local :8002) and is what the app
# talks to when VLLM_PORT=8010.
#
# Run:  bash gpu_router/run_router.sh
# Python: set GPU_ROUTER_PYTHON to the interpreter that has uvicorn+httpx+fastapi
# (the app's env). Defaults to `python3` on PATH.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${GPU_ROUTER_PYTHON:-python3}"
mkdir -p "${DIR}/logs"

if [[ -f "${DIR}/router.pid" ]] && kill -0 "$(cat "${DIR}/router.pid")" 2>/dev/null; then
  echo "router already running (pid $(cat "${DIR}/router.pid"))"
  exit 0
fi

cd "${DIR}"
nohup "${PY}" load_balancer.py >> "${DIR}/logs/router.nohup.log" 2>&1 &
echo $! > "${DIR}/router.pid"
disown
echo "router started (pid $(cat "${DIR}/router.pid")) on :${ROUTER_PORT:-8010}."
echo "Logs: ${DIR}/logs/router.log"
