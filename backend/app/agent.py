"""LocalEC2 compute-node inventory agent.

The agent uses outbound HTTPS only. It reports real host capacity, addresses,
virtualization capabilities, and libvirt networks to the control plane.
"""

import argparse
import asyncio
import os
import platform
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

import httpx
import psutil


def vagrant_capabilities() -> tuple[str, list[str]]:
    capabilities = ["heartbeat", "capacity"]
    try:
        result = subprocess.run(
            [os.environ.get("VAGRANT_EXECUTABLE", "vagrant"), "plugin", "list"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "vagrant-libvirt", capabilities
    if result.returncode == 0:
        capabilities.append("vagrant")
        if "vagrant-libvirt" in result.stdout:
            capabilities.extend(["libvirt", "kvm"])
    return "vagrant-libvirt", capabilities


def network_inventory() -> tuple[list[dict[str, str]], list[str]]:
    interfaces: list[dict[str, str]] = []
    for name, addresses in psutil.net_if_addrs().items():
        for address in addresses:
            if address.family == socket.AF_INET:
                interfaces.append({"name": name, "family": "IPv4", "address": address.address})
            elif address.family == socket.AF_INET6:
                interfaces.append(
                    {
                        "name": name,
                        "family": "IPv6",
                        "address": address.address.split("%", 1)[0],
                    }
                )

    networks: list[str] = []
    try:
        result = subprocess.run(
            ["virsh", "net-list", "--all", "--name"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            networks = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return interfaces[:100], networks[:50]


def management_address() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            connection.connect(("192.0.2.1", 80))
            return str(connection.getsockname()[0])
    except OSError:
        return None


def heartbeat_payload() -> dict[str, Any]:
    provider, capabilities = vagrant_capabilities()
    interfaces, virtual_networks = network_inventory()
    if virtual_networks:
        capabilities.append("libvirt-network")
    root = Path(os.environ.get("LOCAL_EC2_STORAGE_ROOT", "/var/lib/localec2"))
    probe = root if root.exists() else Path.cwd()
    usage = shutil.disk_usage(probe)
    return {
        "agent_version": "0.1.0",
        "hostname": platform.node() or socket.gethostname(),
        "management_address": management_address(),
        "provider_name": provider,
        "cpu_total": os.cpu_count() or 1,
        "memory_total_mb": max(256, int(psutil.virtual_memory().total / 1024**2)),
        "storage_total_gb": max(1, int(usage.total / 1024**3)),
        "capabilities": capabilities,
        "network_interfaces": interfaces,
        "virtual_networks": virtual_networks,
    }


async def send_heartbeat(client: httpx.AsyncClient, base_url: str, node_id: str) -> None:
    response = await client.post(
        f"{base_url.rstrip('/')}/agent/nodes/{node_id}/heartbeat",
        json=heartbeat_payload(),
    )
    response.raise_for_status()


async def run(interval: float, once: bool) -> None:
    base_url = os.environ.get("LOCAL_EC2_API_URL", "").strip()
    node_id = os.environ.get("LOCAL_EC2_NODE_ID", "").strip()
    token = os.environ.get("LOCAL_EC2_NODE_TOKEN", "").strip()
    if not base_url or not node_id or not token:
        raise SystemExit(
            "LOCAL_EC2_API_URL, LOCAL_EC2_NODE_ID, and LOCAL_EC2_NODE_TOKEN are required"
        )
    headers = {"Authorization": f"Bearer {token}"}
    delay = interval
    async with httpx.AsyncClient(headers=headers, timeout=20) as client:
        while True:
            try:
                await send_heartbeat(client, base_url, node_id)
                delay = interval
            except (httpx.HTTPError, OSError) as exc:
                if once:
                    raise SystemExit(f"Heartbeat failed: {exc}") from exc
                delay = min(max(interval, delay * 2), 60)
            if once:
                return
            await asyncio.sleep(delay)


def main() -> None:
    parser = argparse.ArgumentParser(description="LocalEC2 dedicated compute-node agent")
    parser.add_argument("--interval", type=float, default=15, help="Heartbeat interval in seconds")
    parser.add_argument("--once", action="store_true", help="Send one heartbeat and exit")
    arguments = parser.parse_args()
    asyncio.run(run(max(5, arguments.interval), arguments.once))


if __name__ == "__main__":
    main()
