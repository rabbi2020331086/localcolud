"""LocalEC2 compute-node inventory and execution agent.

The agent uses outbound HTTPS only. It reports real host capacity, addresses,
virtualization capabilities, and libvirt networks to the control plane.
"""

import argparse
import asyncio
import base64
import os
import platform
import re
import shlex
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
import psutil


def vagrant_capabilities() -> tuple[str, list[str]]:
    capabilities = ["heartbeat", "capacity", "jobs"]
    executable = os.environ.get("VAGRANT_EXECUTABLE", "vagrant")

    # Keep the probes independent. A slow `vagrant plugin list` must not hide
    # working libvirt/KVM access from the control plane.
    try:
        version = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        version = None
    if version and version.returncode == 0:
        capabilities.append("vagrant")

    # Ubuntu's packaged provider is a system Ruby gem and does not need to be
    # installed in the service account's VAGRANT_HOME. Prefer that fast check;
    # retain plugin-list support for manually installed providers.
    provider_available = any(
        Path("/usr/share/rubygems-integration").glob("**/vagrant-libvirt-*")
    )
    if not provider_available and version and version.returncode == 0:
        try:
            plugins = subprocess.run(
                [executable, "plugin", "list"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            provider_available = (
                plugins.returncode == 0
                and "vagrant-libvirt" in f"{plugins.stdout}\n{plugins.stderr}"
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    try:
        libvirt = subprocess.run(
            ["virsh", "--connect", "qemu:///system", "list", "--all", "--name"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        libvirt = None
    if provider_available and libvirt and libvirt.returncode == 0:
        capabilities.append("libvirt")
    if os.access("/dev/kvm", os.R_OK | os.W_OK):
        capabilities.append("kvm")
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
            ["virsh", "--connect", "qemu:///system", "net-list", "--all", "--name"],
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
        "agent_version": "0.2.4",
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


class NodeExecutor:
    image_boxes = {
        "24.04": "bento/ubuntu-24.04",
        "22.04": "bento/ubuntu-22.04",
        "debian-12": "bento/debian-12",
    }

    def __init__(self) -> None:
        storage = Path(os.environ.get("LOCAL_EC2_STORAGE_ROOT", "/var/lib/localec2"))
        self.root = storage / "instances"
        self.executable = os.environ.get("VAGRANT_EXECUTABLE", "vagrant")
        self.provider = os.environ.get("VAGRANT_PROVIDER", "libvirt")
        self.root.mkdir(parents=True, exist_ok=True)

    def instance_root(self, name: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise RuntimeError("Invalid instance name in node job")
        return self.root / name

    def run(self, root: Path, *arguments: str, timeout: float = 300) -> str:
        if not root.is_dir():
            raise RuntimeError(
                f"Instance workspace {root.name} does not exist on this compute node"
            )
        for attempt in range(3):
            try:
                result = subprocess.run(
                    [self.executable, *arguments],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
            except FileNotFoundError as exc:
                raise RuntimeError("Vagrant is not installed on this compute node") from exc
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("Vagrant operation timed out") from exc
            if result.returncode == 0:
                return result.stdout
            error = result.stderr.strip() or result.stdout.strip() or "Vagrant operation failed"
            transient_network_error = any(
                marker in error.lower()
                for marker in (
                    "could not resolve host",
                    "failed to connect to",
                    "connection timed out",
                    "temporary failure in name resolution",
                )
            )
            if not transient_network_error or attempt == 2:
                raise RuntimeError(error)
            time.sleep(5 * (attempt + 1))
        raise RuntimeError("Vagrant operation failed")

    def guest_exec(self, name: str, command: str, timeout: float = 60) -> str:
        if len(command) > 20000:
            raise RuntimeError("Guest command is too long")
        return self.run(self.instance_root(name), "ssh", "-c", command, timeout=timeout)

    def render_vagrantfile(self, payload: dict[str, Any]) -> str:
        image = str(payload.get("image", "24.04"))
        box = self.image_boxes.get(image, image)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", box):
            raise RuntimeError("Untrusted Vagrant box name")
        name = str(payload["name"])
        cpu = int(payload["cpu_count"])
        memory = int(payload["memory_mb"])
        disk = int(payload["disk_gb"])
        firmware = (
            '    provider.loader = "/usr/share/OVMF/OVMF_CODE.fd"\n'
            if box.startswith("bento/")
            else ""
        )
        scripts: list[str] = []
        public_key = str(payload.get("public_key") or "").strip()
        if public_key:
            quoted = shlex.quote(public_key)
            scripts.append(
                "install -d -m 700 -o vagrant -g vagrant /home/vagrant/.ssh\n"
                "touch /home/vagrant/.ssh/authorized_keys\n"
                f"grep -qxF {quoted} /home/vagrant/.ssh/authorized_keys || "
                f"printf '%s\\n' {quoted} >> /home/vagrant/.ssh/authorized_keys\n"
                "chown vagrant:vagrant /home/vagrant/.ssh/authorized_keys\n"
                "chmod 600 /home/vagrant/.ssh/authorized_keys"
            )
        for field in ("user_data", "security_script"):
            value = str(payload.get(field) or "").strip()
            if value:
                encoded = base64.b64encode(value.encode()).decode()
                scripts.append(f"printf '%s' '{encoded}' | base64 -d | bash")
        provision = ""
        if scripts:
            encoded = base64.b64encode("\n".join(scripts).encode()).decode()
            provision = (
                f'  config.vm.provision "shell", inline: "printf %s {encoded} | base64 -d | bash"\n'
            )
        return (
            'Vagrant.configure("2") do |config|\n'
            f'  config.vm.box = "{box}"\n'
            f'  config.vm.hostname = "{name}"\n'
            '  config.vm.synced_folder ".", "/vagrant", disabled: true\n'
            '  config.vm.network "private_network", type: "dhcp"\n'
            f'  config.vm.provider "{self.provider}" do |provider|\n'
            '    provider.driver = "kvm"\n'
            '    provider.cpu_mode = "host-passthrough"\n'
            f"{firmware}"
            f"    provider.cpus = {cpu}\n"
            f"    provider.memory = {memory}\n"
            f"    provider.machine_virtual_size = {disk}\n"
            "  end\n"
            f"{provision}end\n"
        )

    def inspect(self, name: str) -> dict[str, object]:
        root = self.instance_root(name)
        state = "Unknown"
        output = self.run(root, "status", "--machine-readable", timeout=30)
        for line in output.splitlines():
            fields = line.split(",", 3)
            if len(fields) == 4 and fields[2] == "state":
                state = fields[3].capitalize()
        ipv4 = None
        if state.lower() == "running":
            try:
                addresses = self.guest_exec(name, "hostname -I | awk '{print $1}'", 30)
                ipv4 = addresses.strip().splitlines()[-1] or None
            except (RuntimeError, IndexError):
                pass
        return {"name": name, "state": state, "ipv4_address": ipv4}

    def execute(self, job: dict[str, Any]) -> dict[str, object]:
        kind = str(job["job_type"])
        payload = dict(job.get("payload") or {})
        name = str(payload.get("name") or "")
        root = self.instance_root(name)
        if kind == "LAUNCH":
            if root.exists():
                try:
                    self.run(root, "destroy", "-f", timeout=600)
                finally:
                    shutil.rmtree(root, ignore_errors=True)
            root.mkdir(parents=True)
            try:
                (root / "Vagrantfile").write_text(
                    self.render_vagrantfile(payload), encoding="utf-8"
                )
                self.run(root, "up", f"--provider={self.provider}", timeout=900)
                return self.inspect(name)
            except Exception:
                if root.exists():
                    try:
                        self.run(root, "destroy", "-f", timeout=600)
                    except Exception:
                        pass
                shutil.rmtree(root, ignore_errors=True)
                raise
        if kind == "START":
            self.run(root, "up", f"--provider={self.provider}", timeout=900)
            security_script = str(payload.get("security_script") or "").strip()
            if security_script:
                encoded = base64.b64encode(security_script.encode()).decode()
                self.guest_exec(
                    name,
                    f"printf '%s' '{encoded}' | base64 -d | sudo bash",
                    timeout=120,
                )
            return self.inspect(name)
        if kind == "STOP":
            self.run(root, "halt", timeout=300)
            return self.inspect(name)
        if kind == "SUSPEND":
            self.run(root, "suspend", timeout=300)
            return self.inspect(name)
        if kind == "DELETE":
            if root.exists():
                try:
                    self.run(root, "destroy", "-f", timeout=600)
                finally:
                    shutil.rmtree(root, ignore_errors=True)
            return {"name": name, "state": "Deleted", "ipv4_address": None}
        if kind == "EXEC":
            output = self.guest_exec(
                name,
                str(payload.get("command") or ""),
                float(payload.get("timeout") or 60),
            )
            return {"stdout": output}
        if kind == "INSPECT":
            return self.inspect(name)
        raise RuntimeError(f"Unsupported node job type: {kind}")


async def claim_job(
    client: httpx.AsyncClient, base_url: str, node_id: str
) -> dict[str, Any] | None:
    response = await client.post(f"{base_url.rstrip('/')}/agent/nodes/{node_id}/jobs/claim")
    if response.status_code == 204:
        return None
    response.raise_for_status()
    return dict(response.json())


async def complete_job(
    client: httpx.AsyncClient,
    base_url: str,
    node_id: str,
    job_id: str,
    succeeded: bool,
    result: dict[str, object] | None = None,
    error: str | None = None,
) -> None:
    for attempt in range(5):
        try:
            response = await client.post(
                f"{base_url.rstrip('/')}/agent/nodes/{node_id}/jobs/{job_id}/complete",
                json={
                    "succeeded": succeeded,
                    "result_payload": result or {},
                    "error_message": error,
                },
            )
            response.raise_for_status()
            return
        except httpx.HTTPError:
            if attempt == 4:
                raise
            await asyncio.sleep(2**attempt)


async def renew_job(client: httpx.AsyncClient, base_url: str, node_id: str, job_id: str) -> None:
    response = await client.post(
        f"{base_url.rstrip('/')}/agent/nodes/{node_id}/jobs/{job_id}/renew"
    )
    response.raise_for_status()


async def execute_with_heartbeats(
    executor: NodeExecutor,
    job: dict[str, Any],
    client: httpx.AsyncClient,
    base_url: str,
    node_id: str,
    interval: float,
) -> dict[str, object]:
    execution = asyncio.create_task(asyncio.to_thread(executor.execute, job))
    while not execution.done():
        done, _ = await asyncio.wait({execution}, timeout=interval)
        if execution in done:
            break
        try:
            await send_heartbeat(client, base_url, node_id)
            await renew_job(client, base_url, node_id, str(job["id"]))
        except httpx.HTTPError:
            # VM execution must not be cancelled by a transient heartbeat error.
            pass
    return await execution


async def run(interval: float, once: bool) -> None:
    base_url = os.environ.get("LOCAL_EC2_API_URL", "").strip()
    node_id = os.environ.get("LOCAL_EC2_NODE_ID", "").strip()
    token = os.environ.get("LOCAL_EC2_NODE_TOKEN", "").strip()
    if not base_url or not node_id or not token:
        raise SystemExit(
            "LOCAL_EC2_API_URL, LOCAL_EC2_NODE_ID, and LOCAL_EC2_NODE_TOKEN are required"
        )
    headers = {"Authorization": f"Bearer {token}"}
    delay = 2.0
    next_heartbeat = 0.0
    executor = NodeExecutor()
    async with httpx.AsyncClient(headers=headers, timeout=30) as client:
        while True:
            try:
                now = asyncio.get_running_loop().time()
                if now >= next_heartbeat:
                    await send_heartbeat(client, base_url, node_id)
                    next_heartbeat = now + interval
                if once:
                    return
                job = await claim_job(client, base_url, node_id)
                if job:
                    try:
                        result = await execute_with_heartbeats(
                            executor, job, client, base_url, node_id, interval
                        )
                        await complete_job(client, base_url, node_id, str(job["id"]), True, result)
                    except Exception as exc:
                        await complete_job(
                            client,
                            base_url,
                            node_id,
                            str(job["id"]),
                            False,
                            error=str(exc)[:4000],
                        )
                delay = 2.0
            except (httpx.HTTPError, OSError) as exc:
                if once:
                    raise SystemExit(f"Heartbeat failed: {exc}") from exc
                delay = min(max(2.0, delay * 2), 60)
            await asyncio.sleep(delay)


def main() -> None:
    parser = argparse.ArgumentParser(description="LocalEC2 dedicated compute-node agent")
    parser.add_argument("--interval", type=float, default=15, help="Heartbeat interval in seconds")
    parser.add_argument("--once", action="store_true", help="Send one heartbeat and exit")
    arguments = parser.parse_args()
    asyncio.run(run(max(5, arguments.interval), arguments.once))


if __name__ == "__main__":
    main()
