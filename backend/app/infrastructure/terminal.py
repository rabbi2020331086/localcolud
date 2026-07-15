import asyncio
import fcntl
import os
import pty
import signal
import struct
import subprocess
import termios
from typing import Protocol


class TerminalProcess(Protocol):
    async def read(self, size: int = 4096) -> str: ...
    async def write(self, data: str) -> None: ...
    async def resize(self, rows: int, cols: int) -> None: ...
    async def close(self) -> None: ...


class WindowsTerminal:
    def __init__(self, process: object) -> None:
        self.process = process

    @classmethod
    async def spawn(cls, executable: str, instance_name: str) -> "WindowsTerminal":
        from winpty import PtyProcess

        process = await asyncio.to_thread(
            PtyProcess.spawn,
            [executable, "shell", instance_name],
            dimensions=(30, 120),
        )
        return cls(process)

    async def read(self, size: int = 4096) -> str:
        return await asyncio.to_thread(self.process.read, size)  # type: ignore[attr-defined]

    async def write(self, data: str) -> None:
        await asyncio.to_thread(self.process.write, data)  # type: ignore[attr-defined]

    async def resize(self, rows: int, cols: int) -> None:
        await asyncio.to_thread(self.process.setwinsize, rows, cols)  # type: ignore[attr-defined]

    async def close(self) -> None:
        await asyncio.to_thread(self.process.terminate, True)  # type: ignore[attr-defined]


class PosixTerminal:
    def __init__(self, process: subprocess.Popen[bytes], master_fd: int) -> None:
        self.process = process
        self.master_fd = master_fd

    @classmethod
    async def spawn(cls, executable: str, instance_name: str) -> "PosixTerminal":
        master_fd, slave_fd = pty.openpty()
        process = subprocess.Popen(
            [executable, "shell", instance_name],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave_fd)
        terminal = cls(process, master_fd)
        await terminal.resize(30, 120)
        return terminal

    async def read(self, size: int = 4096) -> str:
        if self.process.poll() is not None:
            return ""
        try:
            data = await asyncio.to_thread(os.read, self.master_fd, size)
        except OSError:
            return ""
        return data.decode(errors="replace")

    async def write(self, data: str) -> None:
        await asyncio.to_thread(os.write, self.master_fd, data.encode())

    async def resize(self, rows: int, cols: int) -> None:
        dimensions = struct.pack("HHHH", rows, cols, 0, 0)
        await asyncio.to_thread(fcntl.ioctl, self.master_fd, termios.TIOCSWINSZ, dimensions)

    async def close(self) -> None:
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.to_thread(self.process.wait, 2)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
        os.close(self.master_fd)


async def create_terminal(executable: str, instance_name: str) -> TerminalProcess:
    if os.name == "nt":
        return await WindowsTerminal.spawn(executable, instance_name)
    return await PosixTerminal.spawn(executable, instance_name)
