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

# Inside the container. Container.start publishes it on a host port of the
# caller's choosing (Editor.port); listening on the host number here bridged
# nothing whenever a second container ran on other ports.
NVIM_PORT = 7777
NVIM2_PORT = 7778


class Editor:
    """One Neovim in the container. `slot` 1 is the usual one; slot 2 is a second, for the tests that
    need several Neovims at once (the container maps a second control port for it)."""

    def __init__(self, container: Container, port: int | None = None, slot: int = 1):
        self.c = container
        self.slot = slot
        self.listen_port = NVIM_PORT if slot == 1 else NVIM2_PORT
        self.port = port or (container.nvim_port if slot == 1 else container.nvim2_port)
        self._nvim = None

    def launch(self, cwd: str = "/work", geometry: str = "200x50") -> None:
        self.c.exec_detached(
            f"cd {cwd} && DISPLAY=:99 xterm -fa 'JetBrains Mono' -fs 11 "
            f"-geometry {geometry} -title 'nvim-harness-{self.slot}' "
            f"-e nvim --listen 0.0.0.0:{self.listen_port}",
            log=f"/home/dev/.harness/log/nvim{self.slot}.log",
        )

    def close(self, hard: bool = True) -> None:
        """The developer closes this Neovim: the process ends (`hard`: killed, as a crash or a closed
        terminal would), and whatever it had open with the Brain is left to the Brain to notice."""
        self._nvim = None
        signal = "-9" if hard else "-15"
        self.c.exec(f"pkill {signal} -f 'nvim --listen 0.0.0.0:{self.listen_port}'; true", check=False)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            gone = self.c.exec(f"pgrep -f 'nvim --listen 0.0.0.0:{self.listen_port}' || true", check=False)
            if not gone.stdout.strip():
                return
            time.sleep(0.3)

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
