"""Screenshots and window inspection.

Screenshots are evidence, never verdicts (HARNESS.md §4). Nothing in the test
suite asserts on pixels; these exist so a failure arrives with a picture and so
a human can see what the Brain is actually showing.
"""
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from .container import Container


class Display:
    def __init__(self, container: Container, display: str = ":99"):
        self.c = container
        self.display = display

    def screenshot(self, out: Path, window: str = "root") -> Path:
        guest = "/tmp/shot.png"
        self.c.exec(
            f"import -display {self.display} -window {shlex.quote(window)} "
            f"-silent {guest}"
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["docker", "cp", f"{self.c.name}:{guest}", str(out)],
            check=True, capture_output=True,
        )
        return out

    def windows(self) -> list[tuple[str, str]]:
        """(window id, title) for every mapped top-level window."""
        out = self.c.exec(
            f"DISPLAY={self.display} xdotool search --onlyvisible --name '.*' "
            f"2>/dev/null | while read -r id; do "
            f"printf '%s\\t%s\\n' \"$id\" "
            f"\"$(DISPLAY={self.display} xdotool getwindowname $id 2>/dev/null)\"; "
            f"done",
            check=False,
        ).stdout
        rows = []
        for line in out.splitlines():
            if "\t" in line:
                wid, _, title = line.partition("\t")
                if title.strip():
                    rows.append((wid.strip(), title.strip()))
        return rows

    def window_titles(self) -> list[str]:
        return [t for _, t in self.windows()]

    def key(self, keys: str, window: str | None = None) -> None:
        target = f"--window {shlex.quote(window)} " if window else ""
        self.c.exec(f"DISPLAY={self.display} xdotool key {target}{keys}")

    def type(self, text: str) -> None:
        self.c.exec(
            f"DISPLAY={self.display} xdotool type --delay 12 {shlex.quote(text)}"
        )
