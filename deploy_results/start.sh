#!/usr/bin/env bash
set -euo pipefail
# Environment files are parsed as data by manage.py; they are never sourced.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/manage.py" foreground "$@"
