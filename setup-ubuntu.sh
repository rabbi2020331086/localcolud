#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REQUIRED_NODE_MAJOR=20
REQUIRED_PNPM_VERSION=10.15.1
PROJECT_ONLY=false

export PATH="$HOME/.local/bin:$PATH"
for node_bin in "$HOME"/.local/lib/nodejs/node-v20*-linux-x64/bin; do
  if [[ -d "$node_bin" ]]; then
    export PATH="$node_bin:$PATH"
    break
  fi
done

if [[ "${1:-}" == "--project-only" ]]; then
  PROJECT_ONLY=true
elif [[ $# -gt 0 ]]; then
  echo "Usage: $0 [--project-only]" >&2
  exit 2
fi

if [[ "$(uname -s)" != Linux ]] || ! grep -qi ubuntu /etc/os-release; then
  echo "This installer supports Ubuntu only." >&2
  exit 1
fi

install_apt_packages() {
  sudo apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    ca-certificates curl git python3 python3-pip python3-venv
}

if [[ "$PROJECT_ONLY" == false ]]; then
  install_apt_packages

  node_major=0
  if command -v node >/dev/null 2>&1; then
    node_major="$(node --version | sed -E 's/^v([0-9]+).*/\1/')"
  fi
  if (( node_major < REQUIRED_NODE_MAJOR )); then
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y nodejs
  fi

  if ! command -v pnpm >/dev/null 2>&1 || [[ "$(pnpm --version)" != "$REQUIRED_PNPM_VERSION" ]]; then
    sudo npm install --global "pnpm@$REQUIRED_PNPM_VERSION"
  fi

fi

command -v node >/dev/null 2>&1 || { echo "Node.js is missing." >&2; exit 1; }
command -v pnpm >/dev/null 2>&1 || { echo "pnpm is missing." >&2; exit 1; }

[[ -f "$ROOT/backend/.env" ]] || cp "$ROOT/backend/.env.example" "$ROOT/backend/.env"
[[ -f "$ROOT/frontend/.env" ]] || cp "$ROOT/frontend/.env.example" "$ROOT/frontend/.env"
[[ -f "$ROOT/admin-frontend/.env" ]] || \
  cp "$ROOT/admin-frontend/.env.example" "$ROOT/admin-frontend/.env"

ROOT="$ROOT" python3 - <<'PY'
import base64
import os
from pathlib import Path
import secrets

path = Path(os.environ["ROOT"]) / "backend" / ".env"
text = path.read_text()
replacements = {
    "JWT_SECRET=generate-with-documented-command": f"JWT_SECRET={secrets.token_hex(32)}",
    "PRIVATE_KEY_ENCRYPTION_KEY=generate-with-documented-command": (
        "PRIVATE_KEY_ENCRYPTION_KEY="
        + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    ),
}
for old, new in replacements.items():
    text = text.replace(old, new)
path.write_text(text)
PY

python3 -m venv "$ROOT/backend/.venv"
"$ROOT/backend/.venv/bin/python" -m pip install --upgrade pip
"$ROOT/backend/.venv/bin/python" -m pip install -e "$ROOT/backend[dev]"
pnpm --dir "$ROOT/frontend" install --frozen-lockfile
pnpm --dir "$ROOT/admin-frontend" install --frozen-lockfile

echo
echo "Setup complete. Configure MONGODB_URI in backend/.env, then run:"
echo "  ./start-ubuntu.sh"
echo "Compute hosts are installed separately from Admin > Infrastructure."
