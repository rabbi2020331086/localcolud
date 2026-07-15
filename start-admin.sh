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

command -v pnpm >/dev/null || { echo "pnpm is missing; run ./setup-ubuntu.sh." >&2; exit 1; }
[[ -d "$ROOT/admin-frontend/node_modules" ]] || {
  echo "Admin dependencies are missing; run ./setup-ubuntu.sh --project-only." >&2
  exit 1
}

echo "Admin console: http://localhost:5174"
pnpm --dir "$ROOT/admin-frontend" run dev
