#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

export PATH="$HOME/.local/bin:$PATH"

for node_bin in "$HOME"/.local/lib/nodejs/node-v20*-linux-x64/bin; do
  if [[ -d "$node_bin" ]]; then
    export PATH="$node_bin:$PATH"
    break
  fi
done

command -v pnpm >/dev/null || {
  echo "pnpm is missing. Run ./setup-ubuntu.sh first." >&2
  exit 1
}

[[ -f "$ROOT/frontend/package.json" ]] || {
  echo "frontend/package.json was not found." >&2
  exit 1
}

cd "$ROOT/frontend"

echo "Starting LocalEC2 customer frontend..."
echo "Frontend: http://localhost:5173"
echo "Press Ctrl+C to stop."

exec pnpm run dev
