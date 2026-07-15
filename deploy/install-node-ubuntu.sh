#!/usr/bin/env bash
set -Eeuo pipefail

API_URL=""
NODE_ID=""
NODE_TOKEN=""

usage() {
  echo "Usage: sudo bash install-node-ubuntu.sh --api-url URL --node-id UUID --token TOKEN"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --api-url) API_URL="${2:-}"; shift 2 ;;
    --node-id) NODE_ID="${2:-}"; shift 2 ;;
    --token) NODE_TOKEN="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this installer as root (use sudo)." >&2
  exit 1
fi
if [[ ! -r /etc/os-release ]]; then
  echo "This installer requires Ubuntu." >&2
  exit 1
fi
# shellcheck disable=SC1091
. /etc/os-release
if [[ ${ID:-} != "ubuntu" ]]; then
  echo "Unsupported operating system: ${PRETTY_NAME:-unknown}. This installer currently supports Ubuntu." >&2
  exit 1
fi

API_URL="${API_URL%/}"
if [[ ! "$API_URL" =~ ^https?://[^[:space:]#]+/api/v1$ ]]; then
  echo "--api-url must look like https://control-plane.example.com/api/v1" >&2
  exit 2
fi
if [[ ! "$NODE_ID" =~ ^[0-9a-fA-F-]{36}$ ]]; then
  echo "--node-id must be the UUID shown by LocalEC2." >&2
  exit 2
fi
if [[ ! "$NODE_TOKEN" =~ ^[A-Za-z0-9_-]{20,}$ ]]; then
  echo "--token is not a valid LocalEC2 enrollment token." >&2
  exit 2
fi

echo "[1/7] Installing KVM, libvirt, Vagrant, and agent dependencies"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y \
  ca-certificates curl python3 python3-venv python3-pip \
  qemu-kvm ovmf libvirt-daemon-system libvirt-clients bridge-utils \
  vagrant build-essential ruby-dev libvirt-dev pkg-config

echo "[2/7] Creating the restricted LocalEC2 service account"
if ! id localec2 >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /var/lib/localec2 \
    --shell /usr/sbin/nologin localec2
fi
for group in libvirt kvm; do
  if getent group "$group" >/dev/null; then
    usermod -aG "$group" localec2
  fi
done
install -d -o localec2 -g localec2 -m 0750 /var/lib/localec2
install -d -o root -g root -m 0755 /opt/localec2-agent
install -d -o root -g localec2 -m 0750 /etc/localec2

echo "[3/7] Enabling the system libvirt daemon and default NAT network"
systemctl enable --now libvirtd
VIRSH=(virsh --connect qemu:///system)
if ! "${VIRSH[@]}" net-info default >/dev/null 2>&1; then
  if [[ -f /usr/share/libvirt/networks/default.xml ]]; then
    "${VIRSH[@]}" net-define /usr/share/libvirt/networks/default.xml
  else
    echo "Warning: libvirt did not provide a default network definition." >&2
  fi
fi
if "${VIRSH[@]}" net-info default >/dev/null 2>&1; then
  "${VIRSH[@]}" net-autostart default >/dev/null
  if ! "${VIRSH[@]}" net-list --name | grep -Fxq 'default'; then
    # libvirt may activate the network between the check and start command.
    # Treat that race as success after confirming the network is now active.
    if ! network_start_error=$("${VIRSH[@]}" net-start default 2>&1); then
      if ! "${VIRSH[@]}" net-list --name | grep -Fxq 'default'; then
        echo "$network_start_error" >&2
        exit 1
      fi
    fi
  fi
fi

echo "[4/7] Installing the Vagrant libvirt provider"
# Ubuntu ships a legacy, root-owned system plugin state file. Vagrant upgrades
# it on first use. Perform that migration before the sandboxed agent starts;
# inside ProtectSystem=strict the same write correctly fails with EROFS.
if [[ -f /usr/share/vagrant-plugins/plugins.json ]]; then
  if ! env HOME=/root VAGRANT_HOME=/root/.vagrant.d \
    vagrant plugin list >/dev/null; then
    echo "Warning: Vagrant's system plugin state could not be initialized." >&2
  fi
fi
if ! runuser -u localec2 -- env \
  HOME=/var/lib/localec2 \
  VAGRANT_HOME=/var/lib/localec2/.vagrant.d \
  vagrant plugin list 2>/dev/null | grep -q '^vagrant-libvirt '; then
  if ! runuser -u localec2 -- env \
    HOME=/var/lib/localec2 \
    VAGRANT_HOME=/var/lib/localec2/.vagrant.d \
    vagrant plugin install vagrant-libvirt; then
    echo "Warning: vagrant-libvirt plugin installation failed; the node will report this capability as unavailable." >&2
  fi
fi

echo "[5/7] Installing the LocalEC2 agent"
python3 -m venv /opt/localec2-agent/venv
/opt/localec2-agent/venv/bin/pip install --disable-pip-version-check --no-cache-dir httpx psutil
curl --fail --show-error --silent "$API_URL/agent/agent.py" \
  --output /opt/localec2-agent/agent.py
chmod 0755 /opt/localec2-agent/agent.py

echo "[6/7] Writing the protected node configuration"
{
  printf 'LOCAL_EC2_API_URL=%s\n' "$API_URL"
  printf 'LOCAL_EC2_NODE_ID=%s\n' "$NODE_ID"
  printf 'LOCAL_EC2_NODE_TOKEN=%s\n' "$NODE_TOKEN"
  printf 'LOCAL_EC2_STORAGE_ROOT=/var/lib/localec2\n'
  printf 'VAGRANT_EXECUTABLE=/usr/bin/vagrant\n'
  printf 'VAGRANT_HOME=/var/lib/localec2/.vagrant.d\n'
  printf 'LIBVIRT_DEFAULT_URI=qemu:///system\n'
  printf 'HOME=/var/lib/localec2\n'
} > /etc/localec2/agent.env
chown root:localec2 /etc/localec2/agent.env
chmod 0640 /etc/localec2/agent.env

curl --fail --show-error --silent "$API_URL/agent/localec2-agent.service" \
  --output /etc/systemd/system/localec2-agent.service

echo "[7/7] Starting the node agent"
systemctl daemon-reload
systemctl enable --now localec2-agent
sleep 2
if ! systemctl is-active --quiet localec2-agent; then
  echo "The agent did not start. Recent logs:" >&2
  journalctl -u localec2-agent --no-pager -n 30 >&2
  exit 1
fi

echo
echo "LocalEC2 node installation is complete."
echo "The agent uses outbound HTTP(S); no inbound management port was opened."
echo "Libvirt network: $("${VIRSH[@]}" net-list --all --name | paste -sd, -)"
echo "Agent status: systemctl status localec2-agent"
echo "Agent logs:   journalctl -u localec2-agent -f"
