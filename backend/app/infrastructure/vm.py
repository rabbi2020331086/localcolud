import asyncio
import json
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.core.config import settings


@dataclass
class ProviderInstance:
    name: str
    state: str
    ipv4: str | None
    cpu_count: int = 0
    memory_mb: int = 0
    disk_gb: int = 0


class ProviderUnavailable(RuntimeError):
    pass


class VmProvider(Protocol):
    async def health_check(self) -> bool: ...
    async def list_instances(self) -> list[ProviderInstance]: ...
    async def launch(
        self,
        name: str,
        image: str,
        cpus: int,
        memory_mb: int,
        disk_gb: int,
        cloud_init: str | None = None,
    ) -> ProviderInstance: ...
    async def clone(
        self, source: str, name: str, cpus: int, memory_mb: int, disk_gb: int
    ) -> ProviderInstance: ...
    async def start(self, name: str) -> None: ...
    async def stop(self, name: str) -> None: ...
    async def suspend(self, name: str) -> None: ...
    async def delete(self, name: str) -> None: ...
    async def exec(self, name: str, command: list[str], timeout: float = 30) -> str: ...
    async def mount(self, source: str, name: str, target: str) -> None: ...
    async def unmount(self, name: str, target: str) -> None: ...


class MultipassProvider:
    def __init__(self, executable: str = settings.multipass_executable):
        self.executable = executable

    async def _run(self, *args: str, timeout: float = 300) -> str:
        # Keep blocking VM operations off the FastAPI event loop on every host.
        def execute() -> subprocess.CompletedProcess[bytes]:
            try:
                return subprocess.run(
                    [self.executable, *args],
                    capture_output=True,
                    timeout=timeout,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except FileNotFoundError as exc:
                raise ProviderUnavailable(
                    f"Multipass is not installed or {self.executable!r} is not on PATH"
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise ProviderUnavailable("Multipass command timed out") from exc

        process = await asyncio.to_thread(execute)
        if process.returncode:
            raise ProviderUnavailable(
                process.stderr.decode(errors="replace").strip() or "Multipass command failed"
            )
        return process.stdout.decode(errors="replace")

    async def health_check(self) -> bool:
        try:
            await self._run("version", timeout=10)
            return True
        except (ProviderUnavailable, OSError):
            return False

    async def list_instances(self) -> list[ProviderInstance]:
        data = json.loads(await self._run("list", "--format", "json", timeout=30))
        result = []
        for item in data.get("list", []):
            result.append(
                ProviderInstance(
                    item["name"],
                    item.get("state", "UNKNOWN"),
                    next(iter(item.get("ipv4") or []), None),
                )
            )
        return result

    async def launch(
        self,
        name: str,
        image: str,
        cpus: int,
        memory_mb: int,
        disk_gb: int,
        cloud_init: str | None = None,
    ) -> ProviderInstance:
        args = [
            "launch",
            image,
            "--name",
            name,
            "--cpus",
            str(cpus),
            "--memory",
            f"{memory_mb}M",
            "--disk",
            f"{disk_gb}G",
        ]
        if cloud_init:
            args.extend(["--cloud-init", cloud_init])
        await self._run(*args, timeout=900)
        return next(x for x in await self.list_instances() if x.name == name)

    async def start(self, name: str) -> None:
        await self._run("start", name)

    async def clone(
        self, source: str, name: str, cpus: int, memory_mb: int, disk_gb: int
    ) -> ProviderInstance:
        await self._run("clone", source, "--name", name, timeout=900)
        await self._run("set", f"local.{name}.cpus={cpus}")
        await self._run("set", f"local.{name}.memory={memory_mb}M")
        await self._run("set", f"local.{name}.disk={disk_gb}G")
        await self.start(name)
        return next(x for x in await self.list_instances() if x.name == name)

    async def stop(self, name: str) -> None:
        await self._run("stop", name)

    async def suspend(self, name: str) -> None:
        await self._run("suspend", name, timeout=300)

    async def delete(self, name: str) -> None:
        try:
            await self._run("delete", name, "--purge")
        except ProviderUnavailable as exc:
            message = str(exc).lower()
            if "does not exist" not in message and "not found" not in message:
                raise

    async def exec(self, name: str, command: list[str], timeout: float = 30) -> str:
        return await self._run("exec", name, "--", *command, timeout=timeout)

    async def mount(self, source: str, name: str, target: str) -> None:
        await self._run("mount", source, f"{name}:{target}", timeout=120)

    async def unmount(self, name: str, target: str) -> None:
        await self._run("umount", f"{name}:{target}", timeout=120)


class VagrantProvider:
    """Local Vagrant/libvirt compute provider used by a LocalEC2 node agent."""

    image_boxes = {
        "24.04": "bento/ubuntu-24.04",
        "22.04": "bento/ubuntu-22.04",
    }

    def __init__(
        self,
        executable: str = settings.vagrant_executable,
        provider_name: str = settings.vagrant_provider,
        root: Path = settings.vagrant_instances_root,
    ) -> None:
        self.executable = executable
        self.provider_name = provider_name
        self.root = root.expanduser().resolve()

    def instance_root(self, name: str) -> Path:
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", name):
            raise ProviderUnavailable("Invalid Vagrant instance name")
        return self.root / name

    def render_vagrantfile(
        self, name: str, image: str, cpus: int, memory_mb: int, cloud_init: str | None
    ) -> str:
        box = self.image_boxes.get(image, image)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", box):
            raise ProviderUnavailable("Vagrant images must be a trusted box name such as org/box")
        provision = ""
        if cloud_init:
            public_keys = [
                line.strip().removeprefix("- ").strip()
                for line in Path(cloud_init).read_text(encoding="utf-8").splitlines()
                if line.strip().startswith("- ssh-")
            ]
            if public_keys:
                key = public_keys[0].replace("'", "'\\''")
                provision = (
                    "\n  config.vm.provision \"shell\", inline: <<-'SHELL'\n"
                    "    install -d -m 700 -o vagrant -g vagrant /home/vagrant/.ssh\n"
                    f"    printf '%s\\n' '{key}' >> /home/vagrant/.ssh/authorized_keys\n"
                    "    chown vagrant:vagrant /home/vagrant/.ssh/authorized_keys\n"
                    "    chmod 600 /home/vagrant/.ssh/authorized_keys\n"
                    "  SHELL\n"
                )
        return (
            'Vagrant.configure("2") do |config|\n'
            f'  config.vm.box = "{box}"\n'
            f'  config.vm.hostname = "{name}"\n'
            '  config.vm.network "private_network", type: "dhcp"\n'
            f'  config.vm.provider "{self.provider_name}" do |provider|\n'
            f"    provider.cpus = {cpus}\n"
            f"    provider.memory = {memory_mb}\n"
            "  end\n"
            f"{provision}end\n"
        )

    async def _run(self, root: Path, *args: str, timeout: float = 300) -> str:
        def execute() -> subprocess.CompletedProcess[bytes]:
            try:
                return subprocess.run(
                    [self.executable, *args],
                    cwd=root,
                    capture_output=True,
                    timeout=timeout,
                    check=False,
                )
            except FileNotFoundError as exc:
                raise ProviderUnavailable(
                    f"Vagrant is not installed or {self.executable!r} is not on PATH"
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise ProviderUnavailable("Vagrant command timed out") from exc

        process = await asyncio.to_thread(execute)
        if process.returncode:
            raise ProviderUnavailable(
                process.stderr.decode(errors="replace").strip() or "Vagrant command failed"
            )
        return process.stdout.decode(errors="replace")

    async def health_check(self) -> bool:
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            version = await self._run(self.root, "--version", timeout=10)
            plugins = await self._run(self.root, "plugin", "list", timeout=30)
            return bool(version.strip()) and (
                self.provider_name != "libvirt" or "vagrant-libvirt" in plugins
            )
        except (ProviderUnavailable, OSError):
            return False

    async def _state(self, root: Path) -> str:
        output = await self._run(root, "status", "--machine-readable", timeout=30)
        for line in output.splitlines():
            fields = line.split(",", 3)
            if len(fields) == 4 and fields[2] == "state":
                return fields[3].capitalize()
        return "Unknown"

    async def _ipv4(self, root: Path) -> str | None:
        try:
            output = await self._run(
                root, "ssh", "-c", "hostname -I | awk '{print $1}'", timeout=30
            )
            return output.strip().splitlines()[-1] or None
        except (ProviderUnavailable, IndexError):
            return None

    async def list_instances(self) -> list[ProviderInstance]:
        if not self.root.exists():
            return []
        result: list[ProviderInstance] = []
        for path in self.root.iterdir():
            if path.is_dir() and (path / "Vagrantfile").is_file():
                state = await self._state(path)
                result.append(
                    ProviderInstance(
                        path.name, state, await self._ipv4(path) if state == "Running" else None
                    )
                )
        return result

    async def launch(
        self,
        name: str,
        image: str,
        cpus: int,
        memory_mb: int,
        disk_gb: int,
        cloud_init: str | None = None,
    ) -> ProviderInstance:
        root = self.instance_root(name)
        root.mkdir(parents=True, exist_ok=False)
        try:
            (root / "Vagrantfile").write_text(
                self.render_vagrantfile(name, image, cpus, memory_mb, cloud_init),
                encoding="utf-8",
            )
            await self._run(root, "up", f"--provider={self.provider_name}", timeout=900)
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise
        return ProviderInstance(name, "Running", await self._ipv4(root), cpus, memory_mb, disk_gb)

    async def clone(
        self, source: str, name: str, cpus: int, memory_mb: int, disk_gb: int
    ) -> ProviderInstance:
        raise ProviderUnavailable("Vagrant AMI cloning is not implemented yet")

    async def start(self, name: str) -> None:
        await self._run(self.instance_root(name), "up", timeout=900)

    async def stop(self, name: str) -> None:
        await self._run(self.instance_root(name), "halt", timeout=300)

    async def suspend(self, name: str) -> None:
        await self._run(self.instance_root(name), "suspend", timeout=300)

    async def delete(self, name: str) -> None:
        root = self.instance_root(name)
        if not root.exists():
            return
        try:
            await self._run(root, "destroy", "-f", timeout=600)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    async def exec(self, name: str, command: list[str], timeout: float = 30) -> str:
        shell_command = " ".join(shlex.quote(part) for part in command)
        return await self._run(
            self.instance_root(name), "ssh", "-c", shell_command, timeout=timeout
        )

    async def mount(self, source: str, name: str, target: str) -> None:
        raise ProviderUnavailable("Use Ceph RBD for persistent Vagrant volumes")

    async def unmount(self, name: str, target: str) -> None:
        raise ProviderUnavailable("Use Ceph RBD for persistent Vagrant volumes")


class FakeProvider:
    def __init__(self) -> None:
        self.instances: dict[str, ProviderInstance] = {}

    async def health_check(self) -> bool:
        return True

    async def list_instances(self) -> list[ProviderInstance]:
        return list(self.instances.values())

    async def launch(
        self,
        name: str,
        image: str,
        cpus: int,
        memory_mb: int,
        disk_gb: int,
        cloud_init: str | None = None,
    ) -> ProviderInstance:
        await asyncio.sleep(0.05)
        item = ProviderInstance(
            name, "Running", f"10.0.0.{len(self.instances) + 2}", cpus, memory_mb, disk_gb
        )
        self.instances[name] = item
        return item

    async def start(self, name: str) -> None:
        self.instances[name].state = "Running"

    async def clone(
        self, source: str, name: str, cpus: int, memory_mb: int, disk_gb: int
    ) -> ProviderInstance:
        if source not in self.instances:
            raise ProviderUnavailable("Source instance does not exist")
        item = ProviderInstance(
            name,
            "Running",
            f"10.0.0.{len(self.instances) + 2}",
            cpus,
            memory_mb,
            disk_gb,
        )
        self.instances[name] = item
        return item

    async def stop(self, name: str) -> None:
        self.instances[name].state = "Stopped"

    async def suspend(self, name: str) -> None:
        self.instances[name].state = "Suspended"

    async def delete(self, name: str) -> None:
        self.instances.pop(name, None)

    async def exec(self, name: str, command: list[str], timeout: float = 30) -> str:
        return ""

    async def mount(self, source: str, name: str, target: str) -> None:
        return None

    async def unmount(self, name: str, target: str) -> None:
        return None


def configured_provider() -> VmProvider:
    if settings.vm_provider == "fake":
        return FakeProvider()
    if settings.vm_provider == "vagrant-libvirt":
        return VagrantProvider()
    if settings.vm_provider == "multipass":
        return MultipassProvider()
    raise RuntimeError(f"Unsupported VM_PROVIDER: {settings.vm_provider}")


provider: VmProvider = configured_provider()


def safe_name(user_id: str, display_name: str) -> str:
    clean = (
        re.sub(r"[^a-z0-9-]+", "-", display_name.lower().replace("_", "-")).strip("-")[:28] or "vm"
    )
    import secrets

    return f"localec2-{user_id[:8]}-{clean}-{secrets.token_hex(2)}"
