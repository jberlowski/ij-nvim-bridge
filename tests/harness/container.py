"""Container lifecycle for the harness.

Reset is discarding the container and starting another (HARNESS.md §3): a Brain
that has run for an hour has warmed caches, drifted settings and accumulated
state no fresh instance reaches, and its numbers are not comparable between runs.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

IMAGE = "ij-nvim-harness:base"
REPO_ROOT = Path(__file__).resolve().parents[2]


class CommandFailed(RuntimeError):
    def __init__(self, cmd: list[str], result: subprocess.CompletedProcess):
        self.result = result
        super().__init__(
            f"{shlex.join(cmd)} exited {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )


@dataclass
class Container:
    name: str
    novnc_port: int
    nvim_port: int = 7777
    brain_port: int = 7878

    # ---------------------------------------------------------------- lifecycle
    @classmethod
    def start(
        cls,
        image: str = IMAGE,
        novnc_port: int = 6080,
        nvim_port: int = 7777,
        brain_port: int = 7878,
        mounts: dict[str, str] | None = None,
        memory: str = "6g",
    ) -> "Container":
        name = f"ij-nvim-{uuid.uuid4().hex[:8]}"
        cmd = [
            "docker", "run", "-d", "--name", name,
            "-p", f"{novnc_port}:6080",
            # nvim's control socket and the socat bridge to the Brain's unix
            # socket. Host-side pytest reaches both through these; neither is
            # Bridge transport (see harness/brain.py).
            "-p", f"{nvim_port}:7777",
            "-p", f"{brain_port}:7878",
            "--memory", memory,
            # IntelliJ's VFS uses inotify watchers; the default container limit
            # is low enough to matter on a Gradle project (HARNESS.md §12).
            "--ulimit", "nofile=65536:65536",
            "--shm-size", "512m",
        ]
        for host, guest in (mounts or {}).items():
            cmd += ["-v", f"{host}:{guest}"]
        cmd += [image, "sleep", "infinity"]
        _run(cmd)
        c = cls(name=name, novnc_port=novnc_port)
        c.nvim_port = nvim_port
        c.brain_port = brain_port
        c.await_display()
        return c

    def stop(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.name],
                       capture_output=True, text=True)

    # ---------------------------------------------------------------- exec
    def exec(
        self,
        command: str,
        user: str = "dev",
        check: bool = True,
        timeout: int = 120,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        cmd = ["docker", "exec", "-u", user]
        for k, v in (env or {}).items():
            cmd += ["-e", f"{k}={v}"]
        cmd += [self.name, "bash", "-lc", command]
        return _run(cmd, check=check, timeout=timeout)

    def exec_detached(self, command: str, user: str = "dev",
                      log: str | None = None) -> None:
        redirect = f" >{log} 2>&1" if log else " >/dev/null 2>&1"
        cmd = ["docker", "exec", "-d", "-u", user, self.name,
               "bash", "-lc", f"({command}){redirect}"]
        _run(cmd)

    def read_file(self, path: str) -> str:
        return self.exec(f"cat {shlex.quote(path)}").stdout

    def write_file(self, path: str, content: str) -> None:
        self.exec(
            f"mkdir -p {shlex.quote(str(Path(path).parent))} && "
            f"cat > {shlex.quote(path)} <<'HARNESS_EOF'\n{content}\nHARNESS_EOF"
        )

    def write_bytes(self, path: str, data: bytes) -> None:
        """Write exactly these bytes. write_file cannot: its heredoc adds a final
        newline and is no place for CRLF."""
        import base64
        self.exec(
            f"mkdir -p {shlex.quote(str(Path(path).parent))} && "
            f"echo {base64.b64encode(data).decode()} | base64 -d > {shlex.quote(path)}"
        )

    def read_bytes(self, path: str) -> bytes:
        import base64
        out = self.exec(f"base64 < {shlex.quote(path)}").stdout
        return base64.b64decode(out)

    def copy_in(self, host_path: Path, guest_path: str) -> None:
        self.exec(f"mkdir -p {shlex.quote(guest_path)}", check=True)
        _run(["docker", "cp", f"{host_path}/.", f"{self.name}:{guest_path}"])
        self.exec(f"chown -R dev:dev {shlex.quote(guest_path)}", user="root")

    # ---------------------------------------------------------------- waiting
    def await_display(self, timeout: float = 30.0) -> None:
        self._await(
            lambda: self.exec("xdpyinfo -display :99 >/dev/null 2>&1 && echo up",
                              check=False).stdout.strip() == "up",
            timeout, "Xvfb did not come up",
        )

    def _await(self, predicate, timeout: float, message: str,
               interval: float = 0.5) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(interval)
        raise TimeoutError(f"{message} (waited {timeout}s)")

    @property
    def novnc_url(self) -> str:
        return f"http://localhost:{self.novnc_port}/vnc.html"


def _run(cmd: list[str], check: bool = True,
         timeout: int = 300) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise CommandFailed(cmd, result)
    return result
