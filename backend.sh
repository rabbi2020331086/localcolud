#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$ROOT/backend/.venv/bin/python"
BIND_HOST="${LOCAL_EC2_BIND_HOST:-127.0.0.1}"
PORT="${LOCAL_EC2_PORT:-8000}"

[[ -x "$PYTHON" ]] || {
  echo "Backend virtual environment is missing." >&2
  echo "Run ./setup-ubuntu.sh first." >&2
  exit 1
}

[[ -f "$ROOT/backend/.env" ]] || {
  echo "backend/.env is missing." >&2
  exit 1
}

cd "$ROOT/backend"

echo "Checking MongoDB connection..."
"$PYTHON" -m app.check_database

echo "Starting LocalEC2 backend..."
echo "Backend: http://$BIND_HOST:$PORT"
echo "Public Funnel: https://rashpc.tailf6602c.ts.net"
echo "Press Ctrl+C to stop."

exec "$PYTHON" -m uvicorn app.main:app \
  --host "$BIND_HOST" \
  --port "$PORT" \
  --workers 1
