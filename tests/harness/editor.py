"""Driving Neovim from outside.

Neovim runs in a terminal on the X display rather than headless, so it appears
in screenshots and in the live noVNC view (HARNESS.md §5). Headless would run
the plugins but render nothing, forfeiting the observability the container
exists to provide.

Its control socket is TCP purely as a harness convenience; it is not the Bridge
socket and says nothing about Bridge transport, which is a unix socket under
XDG_RUNTIME_DIR per SPEC.md §4.2.
"""
from __future__ import annotations

import time

from .container import Container

NVIM_PORT = 7777


class Editor:
    def __init__(self, container: Container, port: int | None = None):
        self.c = container
        self.port = port or container.nvim_port
        self._nvim = None

    def launch(self, cwd: str = "/work", geometry: str = "200x50") -> None:
        self.c.exec_detached(
            f"cd {cwd} && DISPLAY=:99 xterm -fa 'JetBrains Mono' -fs 11 "
            f"-geometry {geometry} -title 'nvim-harness' "
            f"-e nvim --listen 0.0.0.0:{self.port}",
            log="/home/dev/.harness/log/nvim.log",
        )

    def attach(self, timeout: float = 30.0):
        import pynvim
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                self._nvim = pynvim.attach(
                    "tcp", address="127.0.0.1", port=self.port
                )
                self._nvim.command("echo ''")
                return self._nvim
            except Exception as exc:  # noqa: BLE001 - retry until it is up
                last = exc
                time.sleep(0.4)
        raise TimeoutError(f"could not attach to nvim on :{self.port}: {last}")

    # ------------------------------------------------------------------ state
    @property
    def nvim(self):
        if self._nvim is None:
            raise RuntimeError("attach() first")
        return self._nvim

    def edit(self, path: str) -> None:
        self.nvim.command(f"edit {path}")

    def cursor(self) -> tuple[int, int]:
        row, col = self.nvim.funcs.getpos(".")[1:3]
        return row, col

    def lines(self) -> list[str]:
        return self.nvim.current.buffer[:]

    def version(self) -> str:
        return self.nvim.funcs.execute("version").strip().splitlines()[0]

    def lua(self, src: str):
        return self.nvim.exec_lua(src)
