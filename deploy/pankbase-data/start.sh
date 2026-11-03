#!/usr/bin/env bash
set -euo pipefail
umask 077
: "${PANKBASE_DATA_ROOT:?Set immutable snapshot root}"
: "${PANKBASE_PYTHON:?Set Python with FastAPI and uvicorn}"
: "${PANKBASE_SERVICE_DIR:?Set code directory}"
: "${PANKBASE_LOG_DIR:?Set private log directory}"
port="${PANKBASE_PORT:-8818}"
if ss -ltn | awk '{print $4}' | grep -Eq ":${port}$"; then
  echo "Port ${port} already has a listener; refusing to replace it." >&2
  exit 1
fi
mkdir -p "$PANKBASE_LOG_DIR"
chmod 700 "$PANKBASE_LOG_DIR"
cd "$PANKBASE_SERVICE_DIR"
nohup "$PANKBASE_PYTHON" -m uvicorn pankbase_data:create_app --factory --host 127.0.0.1 --port "$port" --no-access-log > "$PANKBASE_LOG_DIR/service.log" 2>&1 < /dev/null &
echo $! > "$PANKBASE_LOG_DIR/service.pid"
echo "Started isolated data service PID $! on loopback:$port"
