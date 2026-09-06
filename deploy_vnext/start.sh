#!/usr/bin/env bash
set -euo pipefail
APP_DIR="${PANK_VNEXT_APP_DIR:-/var/local/serviceuser/projects/pankagent-vnext}"
ENV_FILE="${PANK_VNEXT_ENV_FILE:-/var/local/serviceuser/.config/pankagent-vnext/runtime.env}"
set -a
source "$ENV_FILE"
set +a
cd "$APP_DIR"
exec .venv/bin/python -m uvicorn pankagent_vnext.app:create_app --factory --host 127.0.0.1 --port "${PANK_VNEXT_PORT:-8794}" --workers 1 --no-access-log
