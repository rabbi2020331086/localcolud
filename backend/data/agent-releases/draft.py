"""LocalEC2 compute-node inventory and execution agent.

The agent uses outbound HTTPS only. It reports real host capacity, addresses,
virtualization capabilities, and libvirt networks to the control plane.
"""

import argparse
import asyncio
import base64
import fcntl
import hashlib
import json
import os
import platform
import pty
import re
import select
import shlex
import shutil
import socket
import struct
import subprocess
import termios
import time
from pathlib import Path
from typing import Any

import httpx
import psutil
from websockets.asyncio.client import connect
from websockets.typing import Subprotocol

AGENT_VERSION = "0.7.1"
# AUTO-GENERATED COMPATIBILITY MARKER: "agent_version": "0.7.1"


def version_from_source(source: bytes) -> str | None:
    match = re.search(rb'^AGENT_VERSION\s*=\s*"([^"]+)"', source, re.MULTILINE)
    if not match:
        match = re.search(rb'"agent_version"\s*:\s*"([^"]+)"', source)
    return match.group(1).decode() if match else None


def vagrant_capabilities() -> tuple[str, list[str]]:
    capabilities = ["heartbeat", "capacity", "jobs", "volume-create", "orphan-gc"]
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
    provider_available = any(Path("/usr/share/rubygems-integration").glob("**/vagrant-libvirt-*"))
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
        "agent_version": AGENT_VERSION,
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
        self.launch_timeout = max(
            900, int(os.environ.get("LOCAL_EC2_LAUNCH_TIMEOUT_SECONDS", "1800"))
        )
        self.root.mkdir(parents=True, exist_ok=True)
        self.progress: dict[str, tuple[int, str]] = {}
        self.orphan_first_seen: dict[str, float] = {}

    def report_progress(self, job_id: str, progress: int, message: str) -> None:
        if job_id:
            self.progress[job_id] = (max(0, min(100, progress)), message[:200])

    def run_conversion_with_progress(
        self, command: list[str], job_id: str
    ) -> tuple[int, str]:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
        output = bytearray()
        last_percent = -1
        started = time.monotonic()
        assert process.stdout is not None
        while True:
            ready, _, _ = select.select([process.stdout], [], [], 1.0)
            value = os.read(process.stdout.fileno(), 4096) if ready else b""
            if value:
                output.extend(value)
                if len(output) > 8000:
                    del output[:-4000]
                matches = re.findall(rb"\(([0-9]+(?:\.[0-9]+)?)/100%\)", output[-160:])
                if matches:
                    percent = max(0, min(100, int(float(matches[-1]))))
                    if percent != last_percent:
                        last_percent = percent
                        self.report_progress(
                            job_id,
                            10 + int(45 * percent / 100),
                            f"Compressing root disk ({percent}%)",
                        )
            elif process.poll() is not None:
                break
            if time.monotonic() - started > self.launch_timeout:
                process.kill()
                process.wait()
                raise RuntimeError(
                    f"Root-disk conversion timed out after {self.launch_timeout} seconds"
                )
        return process.wait(), output.decode(errors="replace")

    def instance_root(self, name: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise RuntimeError("Invalid instance name in node job")
        return self.root / name

    def prepare_firmware(self, root: Path) -> tuple[str, str] | None:
        configured_code = os.environ.get("LOCAL_EC2_OVMF_CODE", "").strip()
        configured_vars = os.environ.get("LOCAL_EC2_OVMF_VARS", "").strip()
        pairs = [
            (configured_code, configured_vars),
            (
                "/usr/share/OVMF/OVMF_CODE.fd",
                "/usr/share/OVMF/OVMF_VARS.fd",
            ),
            (
                "/usr/share/OVMF/OVMF_CODE_4M.fd",
                "/usr/share/OVMF/OVMF_VARS_4M.fd",
            ),
            (
                "/usr/share/edk2/x64/OVMF_CODE.fd",
                "/usr/share/edk2/x64/OVMF_VARS.fd",
            ),
        ]
        for code, variables in pairs:
            if code and variables and Path(code).is_file() and Path(variables).is_file():
                writable_variables = root / "OVMF_VARS.fd"
                shutil.copy2(variables, writable_variables)
                return code, str(writable_variables)
        return None

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
                output = exc.stderr or exc.stdout or ""
                if isinstance(output, bytes):
                    output = output.decode(errors="replace")
                detail = str(output).strip()[-1000:]
                message = f"Vagrant operation timed out after {timeout:.0f} seconds"
                if detail:
                    message += f": {detail}"
                raise RuntimeError(message) from exc
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
        root = self.instance_root(name)
        config = root / ".localec2-ssh-config"
        for attempt in range(2):
            if not config.is_file():
                output = self.run(root, "ssh-config", timeout=30)
                config.write_text(output, encoding="utf-8")
                config.chmod(0o600)
            try:
                result = subprocess.run(
                    [
                        "ssh",
                        "-F",
                        str(config),
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "ConnectTimeout=10",
                        "default",
                        command,
                    ],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                if attempt == 0:
                    config.unlink(missing_ok=True)
                    continue
                raise RuntimeError(f"Guest SSH failed: {exc}") from exc
            if result.returncode == 0:
                return result.stdout
            if attempt == 0:
                config.unlink(missing_ok=True)
                continue
            raise RuntimeError(
                result.stderr.strip() or result.stdout.strip() or "Guest SSH command failed"
            )
        raise RuntimeError("Guest SSH command failed")

    def guest_exec_input(
        self, name: str, command: str, input_text: str, timeout: float = 60
    ) -> str:
        """Send sensitive guest script content over stdin instead of the process arguments."""
        root = self.instance_root(name)
        config = root / ".localec2-ssh-config"
        for attempt in range(2):
            if not config.is_file():
                output = self.run(root, "ssh-config", timeout=30)
                config.write_text(output, encoding="utf-8")
                config.chmod(0o600)
            try:
                result = subprocess.run(
                    [
                        "ssh",
                        "-F",
                        str(config),
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "ConnectTimeout=10",
                        "default",
                        command,
                    ],
                    cwd=root,
                    input=input_text,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                if attempt == 0:
                    config.unlink(missing_ok=True)
                    continue
                raise RuntimeError(f"Guest SSH failed: {exc}") from exc
            if result.returncode == 0:
                return result.stdout
            if attempt == 0:
                config.unlink(missing_ok=True)
                continue
            raise RuntimeError(
                result.stderr.strip() or result.stdout.strip() or "Guest SSH command failed"
            )
        raise RuntimeError("Guest SSH command failed")

    def volume_url(self, volume_id: str) -> str:
        base_url = os.environ.get("LOCAL_EC2_API_URL", "").rstrip("/")
        node_id = os.environ.get("LOCAL_EC2_NODE_ID", "")
        return f"{base_url}/agent/nodes/{node_id}/volumes/{volume_id}"

    def ami_url(self, ami_id: str) -> str:
        base_url = os.environ.get("LOCAL_EC2_API_URL", "").rstrip("/")
        node_id = os.environ.get("LOCAL_EC2_NODE_ID", "")
        return f"{base_url}/agent/nodes/{node_id}/amis/{ami_id}"

    def recovery_url(self, recovery_point_id: str) -> str:
        base_url = os.environ.get("LOCAL_EC2_API_URL", "").rstrip("/")
        node_id = os.environ.get("LOCAL_EC2_NODE_ID", "")
        return f"{base_url}/agent/nodes/{node_id}/recovery-points/{recovery_point_id}"

    def domain_root_disk(self, name: str) -> Path:
        result = subprocess.run(
            ["virsh", "--connect", "qemu:///system", "domblklist", f"{name}_default", "--details"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(result.stderr or "Could not inspect the VM root disk")
        for line in result.stdout.splitlines():
            fields = line.split()
            if (
                len(fields) >= 4
                and fields[0] == "file"
                and fields[1] == "disk"
                and fields[2] == "vda"
            ):
                path = Path(" ".join(fields[3:])).resolve()
                if not path.is_file():
                    raise RuntimeError("The VM root disk is missing")
                return path
        raise RuntimeError("The VM root disk could not be identified")

    @staticmethod
    def snapshot_convert_command(disk: Path, snapshot: Path) -> list[str]:
        # The VM is suspended before this command. QEMU still owns its write
        # lock, so -U is required for a concurrent read-only source open.
        return [
            "sudo",
            "-n",
            "qemu-img",
            "convert",
            "-U",
            "-c",
            "-m",
            "2",
            "-p",
            "-O",
            "qcow2",
            str(disk),
            str(snapshot),
        ]

    def volume_headers(self) -> dict[str, str]:
        token = os.environ.get("LOCAL_EC2_NODE_TOKEN", "")
        return {"Authorization": f"Bearer {token}"}

    def destroy_orphan_domain(self, name: str) -> None:
        domain = f"{name}_default"
        exists = subprocess.run(
            ["virsh", "--connect", "qemu:///system", "dominfo", domain],
            capture_output=True,
            text=True,
            check=False,
        )
        if exists.returncode:
            return
        subprocess.run(
            ["virsh", "--connect", "qemu:///system", "destroy", domain],
            capture_output=True,
            text=True,
            check=False,
        )
        result = subprocess.run(
            ["virsh", "--connect", "qemu:///system", "undefine", domain],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode and "cannot undefine domain with nvram" in result.stderr.lower():
            result = subprocess.run(
                [
                    "virsh",
                    "--connect",
                    "qemu:///system",
                    "undefine",
                    domain,
                    "--nvram",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        if result.returncode and "failed to get domain" not in result.stderr.lower():
            raise RuntimeError(result.stderr or "Could not remove orphaned libvirt domain")
        subprocess.run(
            [
                "virsh",
                "--connect",
                "qemu:///system",
                "vol-delete",
                f"{domain}.img",
                "--pool",
                "default",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def render_vagrantfile(self, payload: dict[str, Any]) -> str:
        image = str(payload.get("image", "24.04"))
        box = self.image_boxes.get(image, image)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", box):
            raise RuntimeError("Untrusted Vagrant box name")
        name = str(payload["name"])
        cpu = int(payload["cpu_count"])
        memory = int(payload["memory_mb"])
        disk = int(payload["disk_gb"])
        loader = str(payload.get("_firmware_loader") or "")
        nvram = str(payload.get("_firmware_nvram") or "")
        firmware = (
            f'    provider.loader = "{loader}"\n    provider.nvram = "{nvram}"\n'
            if loader and nvram
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

    def capture_recovery_point(
        self, name: str, point_id: str, job_id: str
    ) -> dict[str, object]:
        self.report_progress(job_id, 5, "Inspecting the VM root disk")
        disk = self.domain_root_disk(name)
        domain = f"{name}_default"
        state = subprocess.run(
            ["virsh", "--connect", "qemu:///system", "domstate", domain],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.lower()
        was_running = "running" in state
        was_interrupted_pause = "paused" in state
        directory = self.root.parent / "recovery-points"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        snapshot = directory / f"{point_id}.qcow2"
        snapshot.unlink(missing_ok=True)
        paused = False
        paused_at: float | None = None
        pause_seconds = 0.0
        try:
            if was_interrupted_pause:
                # A previous capture can be interrupted after libvirt suspend
                # but before its finally block resumes the domain. Treat that
                # state as ours and guarantee resume after this retry.
                paused = True
                paused_at = time.monotonic()
                self.report_progress(job_id, 8, "Recovering an interrupted snapshot attempt")
            elif was_running:
                result = subprocess.run(
                    ["virsh", "--connect", "qemu:///system", "suspend", domain],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if result.returncode:
                    raise RuntimeError(result.stderr or "Could not pause VM for backup")
                paused = True
                paused_at = time.monotonic()
            self.report_progress(job_id, 10, "Converting the root disk to compressed qcow2")
            returncode, conversion_output = self.run_conversion_with_progress(
                self.snapshot_convert_command(disk, snapshot), job_id
            )
            if returncode:
                raise RuntimeError(conversion_output or "Could not create root-disk snapshot")
            ownership = subprocess.run(
                ["sudo", "-n", "chown", f"{os.getuid()}:{os.getgid()}", str(snapshot)],
                capture_output=True,
                text=True,
                check=False,
            )
            if ownership.returncode:
                raise RuntimeError(ownership.stderr or "Could not read the completed snapshot")
        finally:
            if paused:
                subprocess.run(
                    ["virsh", "--connect", "qemu:///system", "resume", domain],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if paused_at is not None:
                    pause_seconds = time.monotonic() - paused_at
        try:
            digest = hashlib.sha256()
            size = snapshot.stat().st_size
            hashed = 0
            with snapshot.open("rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
                    hashed += len(block)
                    self.report_progress(
                        job_id,
                        55 + int(15 * hashed / max(1, size)),
                        f"Verifying snapshot integrity ({hashed * 100 // max(1, size)}%)",
                    )
            uploaded = 0

            def upload_blocks() -> Any:
                nonlocal uploaded
                with snapshot.open("rb") as source:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        uploaded += len(block)
                        self.report_progress(
                            job_id,
                            70 + int(19 * uploaded / max(1, size)),
                            f"Uploading snapshot ({uploaded * 100 // max(1, size)}%)",
                        )
                        yield block

            with (
                httpx.Client(headers=self.volume_headers(), timeout=1800) as client,
            ):
                response = client.put(self.recovery_url(point_id), content=upload_blocks())
                response.raise_for_status()
            self.report_progress(job_id, 89, "Canonical snapshot uploaded")
            return {
                "size_bytes": size,
                "sha256": digest.hexdigest(),
                "paused": was_running or was_interrupted_pause,
                "pause_seconds": round(pause_seconds, 3),
            }
        finally:
            snapshot.unlink(missing_ok=True)

    def download_recovery_point(self, point_id: str, job_id: str = "") -> Path:
        directory = self.root.parent / "recovery-points"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = directory / f"{point_id}.qcow2"
        temporary = target.with_suffix(".downloading")
        with httpx.Client(headers=self.volume_headers(), timeout=1800) as client:
            with client.stream("GET", self.recovery_url(point_id)) as response:
                response.raise_for_status()
                total = int(response.headers.get("content-length") or 0)
                downloaded = 0
                with temporary.open("wb") as output:
                    for chunk in response.iter_bytes():
                        output.write(chunk)
                        downloaded += len(chunk)
                        percent = min(100, int(downloaded * 100 / total)) if total else 0
                        self.report_progress(job_id, percent, f"Copying snapshot ({percent}%)")
        self.report_progress(job_id, 100, "Copying snapshot (100%)")
        os.replace(temporary, target)
        return target

    def garbage_collect(
        self, inventory: dict[str, Any], grace_seconds: int
    ) -> dict[str, object]:
        """Remove only LocalEC2-owned resources absent from an authenticated manifest."""
        desired_instances = {
            value for value in inventory.get("instance_names", [])
            if isinstance(value, str) and re.fullmatch(r"localec2-[A-Za-z0-9_.-]+", value)
        }
        desired_volumes = {
            value for value in inventory.get("volume_ids", [])
            if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F-]{36}", value)
        }
        desired_points = {
            value for value in inventory.get("recovery_point_ids", [])
            if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F-]{36}", value)
        }
        desired_amis = {
            value for value in inventory.get("ami_ids", [])
            if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F-]{36}", value)
        }
        now = time.monotonic()
        removed = {"domains": 0, "workspaces": 0, "snapshots": 0, "volumes": 0, "amis": 0}
        reclaimed = 0

        def ready(key: str) -> bool:
            first = self.orphan_first_seen.setdefault(key, now)
            return now - first >= grace_seconds

        def retained(key: str) -> None:
            self.orphan_first_seen.pop(key, None)

        domains = subprocess.run(
            ["virsh", "--connect", "qemu:///system", "list", "--all", "--name"],
            capture_output=True,
            text=True,
            check=False,
        )
        if domains.returncode:
            raise RuntimeError(domains.stderr or "Could not inventory libvirt domains")
        for domain in domains.stdout.splitlines():
            domain = domain.strip()
            if not domain.startswith("localec2-") or not domain.endswith("_default"):
                continue
            name = domain.removesuffix("_default")
            key = f"domain:{domain}"
            if name in desired_instances:
                retained(key)
            elif ready(key):
                self.destroy_orphan_domain(name)
                removed["domains"] += 1
                retained(key)

        if self.root.is_dir():
            for workspace in self.root.iterdir():
                if not workspace.is_dir() or not workspace.name.startswith("localec2-"):
                    continue
                key = f"workspace:{workspace.name}"
                if workspace.name in desired_instances:
                    retained(key)
                elif ready(key):
                    reclaimed += sum(
                        item.stat().st_size
                        for item in workspace.rglob("*")
                        if item.is_file()
                    )
                    shutil.rmtree(workspace)
                    removed["workspaces"] += 1
                    retained(key)

        file_groups = (
            (self.root.parent / "recovery-points", desired_points, "snapshots"),
            (self.root.parent / "volumes", desired_volumes, "volumes"),
        )
        for directory, desired, category in file_groups:
            if not directory.is_dir():
                continue
            for image in directory.glob("*.qcow2"):
                resource_id = image.stem
                if not re.fullmatch(r"[0-9a-fA-F-]{36}", resource_id):
                    continue
                key = f"{category}:{resource_id}"
                if resource_id in desired:
                    retained(key)
                elif ready(key):
                    reclaimed += image.stat().st_size
                    image.unlink(missing_ok=True)
                    removed[category] += 1
                    retained(key)

        for package in self.root.parent.glob("ami-*.box"):
            ami_id = package.stem.removeprefix("ami-")
            if not re.fullmatch(r"[0-9a-fA-F-]{36}", ami_id):
                continue
            key = f"ami-package:{ami_id}"
            if ami_id in desired_amis:
                retained(key)
            elif ready(key):
                reclaimed += package.stat().st_size
                package.unlink(missing_ok=True)
                removed["amis"] += 1
                retained(key)

        boxes = subprocess.run(
            [self.executable, "box", "list"], capture_output=True, text=True, check=False
        )
        if boxes.returncode == 0:
            for match in re.finditer(
                r"^localec2/ami-([0-9a-fA-F-]{36})\s", boxes.stdout, re.MULTILINE
            ):
                ami_id = match.group(1)
                key = f"ami-box:{ami_id}"
                if ami_id in desired_amis:
                    retained(key)
                elif ready(key):
                    result = subprocess.run(
                        [
                            self.executable, "box", "remove", "-f",
                            f"localec2/ami-{ami_id}", "--provider", self.provider,
                        ],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if result.returncode:
                        raise RuntimeError(result.stderr or "Could not remove orphan AMI box")
                    removed["amis"] += 1
                    retained(key)
        return {"removed": removed, "reclaimed_bytes": reclaimed}

    def execute(self, job: dict[str, Any]) -> dict[str, object]:
        kind = str(job["job_type"])
        job_id = str(job.get("id") or "")
        payload = dict(job.get("payload") or {})
        name = str(payload.get("name") or "")
        if kind in {"REPLICA_CAPTURE", "REPLICA_DOWNLOAD", "REPLICA_DELETE", "RECOVER"}:
            point_id = str(payload.get("recovery_point_id") or "")
            if not re.fullmatch(r"[0-9a-fA-F-]{36}", point_id):
                raise RuntimeError("Invalid recovery point identity")
            if kind == "REPLICA_CAPTURE":
                return self.capture_recovery_point(name, point_id, job_id)
            if kind == "REPLICA_DOWNLOAD":
                target = self.download_recovery_point(point_id, job_id)
                expected = str(payload.get("sha256") or "")
                if expected:
                    digest = hashlib.sha256()
                    with target.open("rb") as source:
                        for block in iter(lambda: source.read(1024 * 1024), b""):
                            digest.update(block)
                    if digest.hexdigest() != expected:
                        target.unlink(missing_ok=True)
                        raise RuntimeError("Downloaded recovery point checksum does not match")
                return {"stored": True, "size_bytes": target.stat().st_size}
            if kind == "REPLICA_DELETE":
                (self.root.parent / "recovery-points" / f"{point_id}.qcow2").unlink(missing_ok=True)
                return {"deleted": True}
            replica = self.root.parent / "recovery-points" / f"{point_id}.qcow2"
            if not replica.is_file():
                replica = self.download_recovery_point(point_id)
            launch_payload = {
                key: value for key, value in payload.items() if key != "recovery_point_id"
            }
            self.execute({"job_type": "LAUNCH", "payload": launch_payload})
            root = self.instance_root(name)
            self.run(root, "halt", timeout=300)
            disk = self.domain_root_disk(name)
            replacement = disk.with_suffix(".recovered")
            disk_stat = disk.stat()
            converted = subprocess.run(
                [
                    "sudo",
                    "-n",
                    "qemu-img",
                    "convert",
                    "-p",
                    "-O",
                    "qcow2",
                    str(replica),
                    str(replacement),
                ],
                capture_output=True,
                text=True,
                timeout=self.launch_timeout,
                check=False,
            )
            if converted.returncode:
                subprocess.run(["sudo", "-n", "rm", "-f", str(replacement)], check=False)
                raise RuntimeError(converted.stderr or "Could not restore the root disk")
            installed = subprocess.run(
                [
                    "sudo",
                    "-n",
                    "chown",
                    f"{disk_stat.st_uid}:{disk_stat.st_gid}",
                    str(replacement),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if not installed.returncode:
                installed = subprocess.run(
                    ["sudo", "-n", "chmod", oct(disk_stat.st_mode & 0o777), str(replacement)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            if not installed.returncode:
                installed = subprocess.run(
                    ["sudo", "-n", "mv", "-f", str(replacement), str(disk)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            if installed.returncode:
                raise RuntimeError(installed.stderr or "Could not install the recovered root disk")
            self.run(root, "up", f"--provider={self.provider}", timeout=self.launch_timeout)
            return self.inspect(name)
        if kind == "HOST_EXEC":
            command = str(payload.get("command") or "")
            timeout = min(600, max(1, int(payload.get("timeout") or 60)))
            run_as_root = bool(payload.get("run_as_root", False))
            if not command or "\x00" in command or len(command) > 20000:
                raise RuntimeError("Invalid host command")
            if not run_as_root and re.search(
                r"(^|[\s;&|`()])(?:/[^\s;&|`]*/)?(?:sudo|su|pkexec|doas)(?=$|[\s;&|`(])",
                command,
            ):
                raise RuntimeError("Privilege escalation requires enabling Run as root (sudo)")
            try:
                completed = subprocess.run(
                    (
                        ["sudo", "-n", "/bin/bash", "-lc", command]
                        if run_as_root
                        else ["/bin/bash", "-lc", command]
                    ),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"Host command timed out after {timeout} seconds") from exc
            return {
                "stdout": completed.stdout[-200000:],
                "stderr": completed.stderr[-200000:],
                "exit_code": completed.returncode,
            }
        if kind == "AGENT_UPDATE":
            control_plane = str(payload.get("control_plane_url") or "").rstrip("/")
            if not re.fullmatch(r"https?://[^\s#]+/api/v1", control_plane):
                raise RuntimeError("Invalid control-plane URL for agent update")
            staging = self.root.parent / "agent-update"
            staging.mkdir(mode=0o700, parents=True, exist_ok=True)
            agent_file = staging / "agent.py"
            service_file = staging / "localec2-agent.service"
            with httpx.Client(timeout=120) as client:
                agent_response = client.get(f"{control_plane}/agent/agent.py")
                agent_response.raise_for_status()
                service_response = client.get(f"{control_plane}/agent/localec2-agent.service")
                service_response.raise_for_status()
            agent_file.write_bytes(agent_response.content)
            service_file.write_bytes(service_response.content)
            downloaded_version = version_from_source(agent_response.content)
            if not downloaded_version:
                raise RuntimeError("Downloaded agent does not contain version metadata")
            installed = subprocess.run(
                [
                    "sudo",
                    "-n",
                    "install",
                    "-o",
                    "root",
                    "-g",
                    "root",
                    "-m",
                    "0755",
                    str(agent_file),
                    "/opt/localec2-agent/agent.py",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if installed.returncode:
                raise RuntimeError(installed.stderr or "Could not install updated agent")
            installed_service = subprocess.run(
                [
                    "sudo",
                    "-n",
                    "install",
                    "-o",
                    "root",
                    "-g",
                    "root",
                    "-m",
                    "0644",
                    str(service_file),
                    "/etc/systemd/system/localec2-agent.service",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if installed_service.returncode:
                raise RuntimeError(
                    installed_service.stderr or "Could not install updated service definition"
                )
            # The transient root unit survives this agent process and restarts
            # it only after execute_with_heartbeats has reported job completion.
            update_unit = (
                "localec2-agent-update-"
                + re.sub(r"[^a-zA-Z0-9]", "", str(job.get("id") or "update"))[:24]
            )
            scheduled = subprocess.run(
                [
                    "sudo",
                    "-n",
                    "systemd-run",
                    f"--unit={update_unit}",
                    "--on-active=10s",
                    "/bin/sh",
                    "-c",
                    "systemctl daemon-reload && systemctl restart localec2-agent",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if scheduled.returncode:
                raise RuntimeError(scheduled.stderr or "Could not schedule agent restart")
            return {
                "updated": True,
                "version": downloaded_version,
                "restart_in_seconds": 10,
            }
        root = self.instance_root(name)
        if kind == "AMI_CAPTURE":
            ami_id = str(payload.get("ami_id") or "")
            if not re.fullmatch(r"[0-9a-fA-F-]{36}", ami_id):
                raise RuntimeError("Invalid AMI identity")
            package = self.root.parent / f"ami-{ami_id}.box"
            package.unlink(missing_ok=True)
            self.run(root, "package", "--output", str(package), timeout=self.launch_timeout)
            try:
                with (
                    package.open("rb") as source,
                    httpx.Client(headers=self.volume_headers(), timeout=1800) as client,
                ):
                    response = client.put(self.ami_url(ami_id), content=source)
                    response.raise_for_status()
            finally:
                package.unlink(missing_ok=True)
            return {"replicated": True}
        if kind == "AMI_DELETE_LOCAL":
            ami_id = str(payload.get("ami_id") or "")
            box_name = f"localec2/ami-{ami_id}"
            subprocess.run(
                [self.executable, "box", "remove", "-f", box_name, "--provider", self.provider],
                capture_output=True,
                text=True,
                check=False,
            )
            return {"deleted": True}
        if kind == "LAUNCH":
            ami_id = str(payload.get("ami_id") or "")
            if ami_id:
                if not re.fullmatch(r"[0-9a-fA-F-]{36}", ami_id):
                    raise RuntimeError("Invalid AMI identity")
                box_name = f"localec2/ami-{ami_id}"
                listed = subprocess.run(
                    [self.executable, "box", "list"], capture_output=True, text=True, check=False
                )
                if box_name not in listed.stdout:
                    package = self.root.parent / f"ami-{ami_id}.box"
                    with httpx.Client(headers=self.volume_headers(), timeout=1800) as client:
                        with client.stream("GET", self.ami_url(ami_id)) as response:
                            response.raise_for_status()
                            with package.open("wb") as target:
                                for chunk in response.iter_bytes():
                                    target.write(chunk)
                    try:
                        added = subprocess.run(
                            [
                                self.executable,
                                "box",
                                "add",
                                "--force",
                                "--name",
                                box_name,
                                str(package),
                            ],
                            capture_output=True,
                            text=True,
                            timeout=1800,
                            check=False,
                        )
                        if added.returncode:
                            raise RuntimeError(added.stderr or "Could not install replicated AMI")
                    finally:
                        package.unlink(missing_ok=True)
                payload["image"] = box_name
            if root.exists():
                try:
                    self.run(root, "destroy", "-f", timeout=600)
                except Exception:
                    # Older vagrant-libvirt releases cannot undefine UEFI
                    # domains with NVRAM. Fall back to libvirt directly.
                    self.destroy_orphan_domain(name)
                finally:
                    shutil.rmtree(root, ignore_errors=True)
            else:
                self.destroy_orphan_domain(name)
            root.mkdir(parents=True)
            try:
                firmware = self.prepare_firmware(root)
                if firmware:
                    payload.update(
                        _firmware_loader=firmware[0],
                        _firmware_nvram=firmware[1],
                    )
                (root / "Vagrantfile").write_text(
                    self.render_vagrantfile(payload), encoding="utf-8"
                )
                self.run(
                    root,
                    "up",
                    f"--provider={self.provider}",
                    timeout=self.launch_timeout,
                )
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
            self.run(
                root,
                "up",
                f"--provider={self.provider}",
                timeout=self.launch_timeout,
            )
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
                    try:
                        self.guest_exec(
                            name,
                            "command -v tailscale >/dev/null && "
                            "(sudo tailscale funnel reset || true; sudo tailscale logout || true)",
                            timeout=30,
                        )
                    except Exception:
                        pass
                    self.run(root, "destroy", "-f", timeout=600)
                except Exception:
                    self.destroy_orphan_domain(name)
                finally:
                    shutil.rmtree(root, ignore_errors=True)
            else:
                self.destroy_orphan_domain(name)
            return {"name": name, "state": "Deleted", "ipv4_address": None}
        if kind == "EXEC":
            output = self.guest_exec(
                name,
                str(payload.get("command") or ""),
                float(payload.get("timeout") or 60),
            )
            return {"stdout": output}
        if kind == "TAILSCALE_SETUP":
            auth_key = str(payload.get("auth_key") or "")
            hostname = str(payload.get("hostname") or "")
            tag = str(payload.get("tag") or "")
            target_port = int(payload.get("target_port") or 80)
            install_default_web = bool(payload.get("install_default_web", True))
            if not auth_key.startswith("tskey-") or not re.fullmatch(r"[a-z0-9-]{1,63}", hostname):
                raise RuntimeError("Invalid Tailscale provisioning data")
            advertise_tag = f" --advertise-tags={shlex.quote(tag)}" if tag else ""
            script = (
                "set -euo pipefail\n"
                "export DEBIAN_FRONTEND=noninteractive\n"
                "export APT_OPTS='-o Acquire::Retries=2 -o Acquire::http::Timeout=30 "
                "-o Acquire::https::Timeout=30'\n"
                "wait_for_apt() {\n"
                "  timeout 180 sh -c 'while fuser /var/lib/dpkg/lock-frontend "
                "/var/lib/apt/lists/lock /var/cache/apt/archives/lock "
                ">/dev/null 2>&1; do sleep 3; done'\n"
                "  dpkg --configure -a\n"
                "}\n"
                "wait_for_apt\n"
                "command -v curl >/dev/null || "
                "(apt-get $APT_OPTS update && apt-get $APT_OPTS install -y curl)\n"
                "command -v tailscale >/dev/null || "
                "curl --connect-timeout 15 --max-time 120 -fsSL "
                "https://tailscale.com/install.sh | sh\n"
                "systemctl enable --now tailscaled\n"
                "auth_file=$(mktemp)\n"
                "trap 'rm -f \"$auth_file\"' EXIT\n"
                f'printf %s {shlex.quote(auth_key)} > "$auth_file"\n'
                'chmod 600 "$auth_file"\n'
                "if tailscale status --json 2>/dev/null | "
                'grep -q \'"BackendState": "Running"\'; then\n'
                f"  tailscale set --hostname={shlex.quote(hostname)}\n"
                "else\n"
                f'  timeout 120 tailscale up --auth-key="file:$auth_file" '
                f"--hostname={shlex.quote(hostname)}{advertise_tag}\n"
                "fi\n"
                f"if command -v ufw >/dev/null; then ufw allow in on tailscale0 "
                f"to any port {target_port} proto tcp; fi\n"
                + (
                    f"if ! ss -ltn | awk '{{print $4}}' | grep -Eq ':{target_port}$'; then\n"
                    "  wait_for_apt\n"
                    "  apt-get $APT_OPTS update && apt-get $APT_OPTS install -y nginx\n"
                    f"  printf '%s\\n' '<!doctype html><title>{hostname}</title>"
                    f"<h1>{hostname}</h1><p>Served by LocalEC2</p>' "
                    "> /var/www/html/index.html\n"
                    "  systemctl enable --now nginx\n"
                    "fi\n"
                    if install_default_web
                    else ""
                )
                + f"timeout 60 tailscale funnel --bg --yes {target_port}\n"
                "tailscale status --json\n"
            )
            output = self.guest_exec_input(name, "timeout 340 sudo bash -s", script, timeout=360)
            status = json.loads(output[output.find("{") :])
            dns_name = str(status.get("Self", {}).get("DNSName") or "").rstrip(".")
            addresses = status.get("Self", {}).get("TailscaleIPs") or []
            ipv4 = next((value for value in addresses if "." in str(value)), None)
            if not dns_name:
                raise RuntimeError("Tailscale did not return a MagicDNS name")
            return {
                "tailscale_ipv4_address": ipv4,
                "tailscale_dns_name": dns_name,
                "public_endpoint": f"https://{dns_name}",
            }
        if kind == "INSPECT":
            return self.inspect(name)
        if kind == "VOLUME_CREATE":
            volume_id = str(payload.get("volume_id") or "")
            if not re.fullmatch(r"[0-9a-fA-F-]{36}", volume_id):
                raise RuntimeError("Invalid volume identity")
            size_gb = int(payload.get("size_gb") or 1)
            volumes = self.root.parent / "volumes"
            volumes.mkdir(mode=0o770, parents=True, exist_ok=True)
            image = volumes / f"{volume_id}.qcow2"
            created = subprocess.run(
                ["qemu-img", "create", "-f", "qcow2", str(image), f"{size_gb}G"],
                capture_output=True,
                text=True,
                check=False,
            )
            if created.returncode:
                raise RuntimeError(created.stderr or "Could not create volume image")
            os.chmod(image, 0o660)
            with (
                image.open("rb") as source,
                httpx.Client(headers=self.volume_headers(), timeout=600) as client,
            ):
                response = client.put(self.volume_url(volume_id), content=source)
                response.raise_for_status()
            return {"host_path": str(image)}
        if kind == "VOLUME_ATTACH":
            volume_id = str(payload.get("volume_id") or "")
            if not re.fullmatch(r"[0-9a-fA-F-]{36}", volume_id):
                raise RuntimeError("Invalid volume identity")
            volumes = self.root.parent / "volumes"
            volumes.mkdir(mode=0o770, parents=True, exist_ok=True)
            image = volumes / f"{volume_id}.qcow2"
            size_gb = int(payload.get("size_gb") or 1)
            incoming = image.with_suffix(".downloading")
            incoming.unlink(missing_ok=True)
            with httpx.Client(headers=self.volume_headers(), timeout=300) as client:
                with client.stream("GET", self.volume_url(volume_id)) as response:
                    if response.status_code == 200:
                        with incoming.open("wb") as target:
                            for chunk in response.iter_bytes():
                                target.write(chunk)
                        os.replace(incoming, image)
                    elif response.status_code == 404:
                        created = subprocess.run(
                            ["qemu-img", "create", "-f", "qcow2", str(image), f"{size_gb}G"],
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                        if created.returncode:
                            raise RuntimeError(created.stderr or "Could not create volume image")
                    else:
                        raise RuntimeError(
                            f"Volume download failed with HTTP {response.status_code}"
                        )
            os.chmod(image, 0o660)
            domain = f"{name}_default"
            block_list = subprocess.run(
                ["virsh", "--connect", "qemu:///system", "domblklist", domain, "--details"],
                capture_output=True,
                text=True,
                check=False,
            )
            if block_list.returncode:
                raise RuntimeError(block_list.stderr or "Could not inspect domain disks")
            used_targets = {
                fields[2]
                for line in block_list.stdout.splitlines()
                if len(fields := line.split()) >= 4 and fields[0] == "file"
            }
            requested_device = str(payload.get("device_name") or "")
            candidates = [
                requested_device,
                *(f"vd{letter}" for letter in "bcdefghijklmnopqrstuvwxyz"),
            ]
            target_device = next(
                (
                    candidate
                    for candidate in candidates
                    if re.fullmatch(r"vd[b-z]", candidate) and candidate not in used_targets
                ),
                None,
            )
            if not target_device:
                raise RuntimeError("Instance has no available block-device slots")
            attached = subprocess.run(
                [
                    "virsh",
                    "--connect",
                    "qemu:///system",
                    "attach-disk",
                    domain,
                    str(image),
                    target_device,
                    "--targetbus",
                    "virtio",
                    "--subdriver",
                    "qcow2",
                    "--live",
                    "--config",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if attached.returncode and "already in use" not in attached.stderr.lower():
                raise RuntimeError(attached.stderr or "Could not attach volume")
            mount_path = str(payload.get("mount_path") or "/mnt/data")
            quoted_mount = shlex.quote(mount_path)
            command = (
                f"set -e; dev=/dev/{target_device}; "
                "for i in $(seq 1 20); do [ -b $dev ] && break; sleep 1; done; "
                "[ -b $dev ]; sudo blkid $dev >/dev/null 2>&1 || sudo mkfs.ext4 -F $dev; "
                f"sudo mkdir -p {quoted_mount}; "
                "uuid=$(sudo blkid -s UUID -o value $dev); "
                f"sudo awk -v p={quoted_mount} '$2 != p' /etc/fstab | "
                "sudo tee /etc/fstab.localec2 >/dev/null; "
                "sudo mv /etc/fstab.localec2 /etc/fstab; "
                f'grep -q "UUID=$uuid " /etc/fstab || echo "UUID=$uuid {quoted_mount} '
                'ext4 defaults,nofail 0 2" | sudo tee -a /etc/fstab >/dev/null; '
                f"mountpoint -q {quoted_mount} || sudo mount {quoted_mount}; "
                f"df -B1 --output=used,avail,pcent {quoted_mount} | tail -1"
            )
            output = self.guest_exec(name, command, timeout=120)
            usage = output.strip().splitlines()[-1].split()
            return {
                "host_path": str(image),
                "device_name": target_device,
                "used_bytes": int(usage[0]),
                "available_bytes": int(usage[1]),
                "usage_percent": float(usage[2].rstrip("%")),
            }
        if kind == "VOLUME_DETACH":
            mount_path = str(payload.get("mount_path") or "/mnt/data")
            quoted_mount = shlex.quote(mount_path)
            self.guest_exec(
                name,
                f"mountpoint -q {quoted_mount} && sudo umount {quoted_mount} || true; "
                f"sudo awk -v p={quoted_mount} '$2 != p' /etc/fstab | "
                "sudo tee /etc/fstab.localec2 >/dev/null; "
                "sudo mv /etc/fstab.localec2 /etc/fstab",
                timeout=60,
            )
            domain = f"{name}_default"
            target_device = str(payload.get("device_name") or "vdb")
            if not re.fullmatch(r"vd[b-z]", target_device):
                raise RuntimeError("Invalid volume device")
            detached = subprocess.run(
                [
                    "virsh",
                    "--connect",
                    "qemu:///system",
                    "detach-disk",
                    domain,
                    target_device,
                    "--live",
                    "--config",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if detached.returncode and "no disk found" not in detached.stderr.lower():
                raise RuntimeError(detached.stderr or "Could not detach volume")
            volume_id = str(payload.get("volume_id") or "")
            image = self.root.parent / "volumes" / f"{volume_id}.qcow2"
            if not image.is_file():
                raise RuntimeError("Local volume backing image is missing")
            with (
                image.open("rb") as source,
                httpx.Client(headers=self.volume_headers(), timeout=600) as client,
            ):
                response = client.put(self.volume_url(volume_id), content=source)
                response.raise_for_status()
            image.unlink()
            return {"detached": True}
        if kind == "VOLUME_DELETE":
            volume_id = str(payload.get("volume_id") or "")
            if not re.fullmatch(r"[0-9a-fA-F-]{36}", volume_id):
                raise RuntimeError("Invalid volume identity")
            image = self.root.parent / "volumes" / f"{volume_id}.qcow2"
            image.unlink(missing_ok=True)
            return {"deleted": True}
        raise RuntimeError(f"Unsupported node job type: {kind}")


class TerminalSession:
    def __init__(
        self, session_id: str, name: str, executor: NodeExecutor, *, host: bool = False
    ) -> None:
        self.session_id = session_id
        self.master_fd, slave_fd = pty.openpty()
        root = executor.root
        command = ["sudo", "-n", "/bin/bash", "-l"]
        if not host:
            root = executor.instance_root(name)
            command = [executor.executable, "ssh"]
            if not root.is_dir():
                os.close(self.master_fd)
                os.close(slave_fd)
                raise RuntimeError("Instance workspace is unavailable on this node")
        self.process = subprocess.Popen(
            command,
            cwd=root,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave_fd)

    def write(self, value: bytes) -> None:
        if self.process.poll() is None:
            os.write(self.master_fd, value)

    def resize(self, rows: int, cols: int) -> None:
        size = struct.pack("HHHH", max(1, rows), max(1, cols), 0, 0)
        fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, size)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
        try:
            os.close(self.master_fd)
        except OSError:
            pass


async def terminal_connection(
    base_url: str, node_id: str, token: str, executor: NodeExecutor
) -> None:
    websocket_url = re.sub(r"^http", "ws", base_url.rstrip("/"))
    websocket_url = websocket_url.removesuffix("/api/v1")
    sessions: dict[str, TerminalSession] = {}
    readers: dict[str, asyncio.Task[None]] = {}
    send_lock = asyncio.Lock()

    async def send(message: dict[str, object]) -> None:
        async with send_lock:
            await websocket.send(__import__("json").dumps(message))

    async def read_output(session: TerminalSession) -> None:
        try:
            while session.process.poll() is None:
                data = await asyncio.to_thread(os.read, session.master_fd, 4096)
                if not data:
                    break
                await send(
                    {
                        "type": "output",
                        "session_id": session.session_id,
                        "data": base64.b64encode(data).decode(),
                    }
                )
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            code = session.process.poll()
            await send({"type": "exit", "session_id": session.session_id, "code": code or 0})
            session.close()
            sessions.pop(session.session_id, None)
            readers.pop(session.session_id, None)

    async with connect(
        f"{websocket_url}/ws/agent/nodes/{node_id}/terminal",
        subprotocols=[Subprotocol(f"localec2-node.{token}")],
        ping_interval=20,
        ping_timeout=20,
        max_size=1024 * 1024,
    ) as websocket:
        async for raw in websocket:
            message = __import__("json").loads(raw)
            session_id = str(message.get("session_id") or "")
            kind = message.get("type")
            try:
                if kind == "start":
                    if session_id in sessions:
                        continue
                    session = TerminalSession(
                        session_id,
                        str(message.get("name") or ""),
                        executor,
                        host=message.get("target") == "host",
                    )
                    session.resize(int(message.get("rows", 30)), int(message.get("cols", 120)))
                    sessions[session_id] = session
                    readers[session_id] = asyncio.create_task(read_output(session))
                    await send({"type": "ready", "session_id": session_id})
                elif kind == "input" and session_id in sessions:
                    sessions[session_id].write(base64.b64decode(str(message.get("data") or "")))
                elif kind == "resize" and session_id in sessions:
                    sessions[session_id].resize(
                        int(message.get("rows", 30)), int(message.get("cols", 120))
                    )
                elif kind == "close" and session_id in sessions:
                    session = sessions.pop(session_id)
                    session.close()
                    task = readers.pop(session_id, None)
                    if task:
                        task.cancel()
            except Exception as exc:
                await send({"type": "error", "session_id": session_id, "message": str(exc)[:500]})

    for session in sessions.values():
        session.close()
    for task in readers.values():
        task.cancel()


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


async def renew_job(
    client: httpx.AsyncClient,
    base_url: str,
    node_id: str,
    job_id: str,
    progress: tuple[int, str] | None = None,
) -> None:
    response = await client.post(
        f"{base_url.rstrip('/')}/agent/nodes/{node_id}/jobs/{job_id}/renew",
        json=(
            {"progress": progress[0], "message": progress[1]}
            if progress
            else None
        ),
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
            await renew_job(
                client,
                base_url,
                node_id,
                str(job["id"]),
                executor.progress.get(str(job["id"])),
            )
        except httpx.HTTPError:
            # VM execution must not be cancelled by a transient heartbeat error.
            pass
    try:
        return await execution
    finally:
        executor.progress.pop(str(job["id"]), None)


async def run(interval: float, once: bool) -> None:
    base_url = os.environ.get("LOCAL_EC2_API_URL", "").strip()
    node_id = os.environ.get("LOCAL_EC2_NODE_ID", "").strip()
    token = os.environ.get("LOCAL_EC2_NODE_TOKEN", "").strip()
    if not base_url or not node_id or not token:
        raise SystemExit(
            "LOCAL_EC2_API_URL, LOCAL_EC2_NODE_ID, and LOCAL_EC2_NODE_TOKEN are required"
        )
    headers = {"Authorization": f"Bearer {token}"}
    executor = NodeExecutor()
    concurrency = max(1, min(8, int(os.environ.get("LOCAL_EC2_JOB_CONCURRENCY", "3"))))
    auto_update_interval = max(
        60, int(os.environ.get("LOCAL_EC2_AUTO_UPDATE_INTERVAL_SECONDS", "300"))
    )
    auto_update_enabled = os.environ.get("LOCAL_EC2_AUTO_UPDATE", "true").lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    gc_interval = max(60, int(os.environ.get("LOCAL_EC2_GC_INTERVAL_SECONDS", "300")))
    gc_grace = max(300, int(os.environ.get("LOCAL_EC2_GC_GRACE_SECONDS", "3600")))

    async def terminal_loop() -> None:
        delay_seconds = 2
        while True:
            try:
                await terminal_connection(base_url, node_id, token, executor)
                delay_seconds = 2
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(delay_seconds)
                delay_seconds = min(delay_seconds * 2, 30)

    async with httpx.AsyncClient(headers=headers, timeout=30) as client:
        if once:
            try:
                await send_heartbeat(client, base_url, node_id)
                return
            except (httpx.HTTPError, OSError) as exc:
                raise SystemExit(f"Heartbeat failed: {exc}") from exc

        async def heartbeat_loop() -> None:
            delay_seconds = 2.0
            while True:
                try:
                    await send_heartbeat(client, base_url, node_id)
                    delay_seconds = interval
                except (httpx.HTTPError, OSError):
                    delay_seconds = min(max(2.0, delay_seconds * 2), 60)
                await asyncio.sleep(delay_seconds)

        async def job_worker() -> None:
            delay_seconds = 1.0
            while True:
                try:
                    job = await claim_job(client, base_url, node_id)
                    if not job:
                        await asyncio.sleep(1)
                        continue
                    delay_seconds = 1.0
                    try:
                        result = await execute_with_heartbeats(
                            executor, job, client, base_url, node_id, min(interval, 1.0)
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
                except asyncio.CancelledError:
                    raise
                except (httpx.HTTPError, OSError):
                    await asyncio.sleep(delay_seconds)
                    delay_seconds = min(delay_seconds * 2, 30)

        async def auto_update_loop() -> None:
            await asyncio.sleep(min(30, auto_update_interval))
            retry_seconds = float(auto_update_interval)
            while True:
                try:
                    response = await client.get(f"{base_url.rstrip('/')}/agent/agent.py")
                    response.raise_for_status()
                    published_version = version_from_source(response.content)
                    if not published_version:
                        raise RuntimeError("Published agent has no version metadata")
                    if published_version != AGENT_VERSION:
                        await asyncio.to_thread(
                            executor.execute,
                            {
                                "id": f"automatic-{int(time.time())}",
                                "job_type": "AGENT_UPDATE",
                                "payload": {"control_plane_url": base_url.rstrip("/")},
                            },
                        )
                        await asyncio.sleep(auto_update_interval)
                    retry_seconds = float(auto_update_interval)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    retry_seconds = min(max(60.0, retry_seconds * 2), 3600.0)
                await asyncio.sleep(retry_seconds)

        async def garbage_collection_loop() -> None:
            # Never collect from cached state: every pass requires a fresh,
            # authenticated control-plane inventory.
            await asyncio.sleep(min(30, gc_interval))
            while True:
                try:
                    response = await client.get(
                        f"{base_url.rstrip('/')}/agent/nodes/{node_id}/inventory"
                    )
                    response.raise_for_status()
                    result = await asyncio.to_thread(
                        executor.garbage_collect, dict(response.json()), gc_grace
                    )
                    report = await client.post(
                        f"{base_url.rstrip('/')}/agent/nodes/{node_id}/gc-report",
                        json={"status": "SUCCEEDED", **result},
                    )
                    report.raise_for_status()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    try:
                        await client.post(
                            f"{base_url.rstrip('/')}/agent/nodes/{node_id}/gc-report",
                            json={
                                "status": "FAILED",
                                "removed": {},
                                "reclaimed_bytes": 0,
                                "error": str(exc)[:1000],
                            },
                        )
                    except Exception:
                        pass
                await asyncio.sleep(gc_interval)

        tasks = [
            asyncio.create_task(terminal_loop()),
            asyncio.create_task(heartbeat_loop()),
            asyncio.create_task(garbage_collection_loop()),
            *[asyncio.create_task(job_worker()) for _ in range(concurrency)],
        ]
        if auto_update_enabled:
            tasks.append(asyncio.create_task(auto_update_loop()))
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="LocalEC2 dedicated compute-node agent")
    parser.add_argument("--interval", type=float, default=15, help="Heartbeat interval in seconds")
    parser.add_argument("--once", action="store_true", help="Send one heartbeat and exit")
    arguments = parser.parse_args()
    asyncio.run(run(max(5, arguments.interval), arguments.once))


if __name__ == "__main__":
    main()
