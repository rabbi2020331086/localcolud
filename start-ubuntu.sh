#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$ROOT/backend/.venv/bin/python"
BIND_HOST="${LOCAL_EC2_BIND_HOST:-0.0.0.0}"

# Support the user-local Node installation used when system-wide sudo access
# is unavailable. New terminal sessions do not always include ~/.local/bin.
export PATH="$HOME/.local/bin:$PATH"
for node_bin in "$HOME"/.local/lib/nodejs/node-v20*-linux-x64/bin; do
  if [[ -d "$node_bin" ]]; then
    export PATH="$node_bin:$PATH"
    break
  fi
done

[[ -x "$PYTHON" ]] || { echo "Run ./setup-ubuntu.sh first." >&2; exit 1; }
[[ -f "$ROOT/backend/.env" ]] || { echo "backend/.env is missing." >&2; exit 1; }
command -v pnpm >/dev/null || { echo "pnpm is missing; run ./setup-ubuntu.sh." >&2; exit 1; }

cd "$ROOT/backend"
"$PYTHON" -m app.check_database
# Reload backend code during local development just as Vite reloads the frontend.
setsid "$PYTHON" -m uvicorn app.main:app --host "$BIND_HOST" --port 8000 --reload \
  >backend.out.log 2>backend.error.log &
backend_pid=$!
cleanup() {
  if [[ -n "${frontend_pid:-}" ]]; then
    kill -TERM -- "-$frontend_pid" 2>/dev/null || true
  fi
  kill -TERM -- "-$backend_pid" 2>/dev/null || true
  if [[ -n "${frontend_pid:-}" ]]; then
    wait "$frontend_pid" 2>/dev/null || true
  fi
  wait "$backend_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

sleep 2
if ! kill -0 "$backend_pid" 2>/dev/null; then
  echo "Backend failed to start:" >&2
  tail -n 50 backend.error.log >&2
  exit 1
fi

echo "Backend: http://localhost:8000"
echo "Node API listener: $BIND_HOST:8000 (restrict port 8000 to your LAN/VPN)"
echo "Frontend: http://localhost:5173"
echo "Press Ctrl+C to stop both applications."
cd "$ROOT/frontend"
setsid pnpm run dev &
frontend_pid=$!
wait "$frontend_pid"
