#!/usr/bin/env bash
# Package the submission; requires only Bash and Python 3.10+.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -n "${PYTHON:-}" ]]; then
  exec "$PYTHON" "$SCRIPT_DIR/package.py" "$@"
elif command -v python3 >/dev/null 2>&1; then
  exec python3 "$SCRIPT_DIR/package.py" "$@"
elif command -v python >/dev/null 2>&1; then
  exec python "$SCRIPT_DIR/package.py" "$@"
else
  echo "Python 3.10+ is required. Set PYTHON to your Python executable." >&2
  exit 1
fi
