"""IntelliJ lifecycle inside the harness."""
from __future__ import annotations

import time
from pathlib import Path

from .container import Container
from .display import Display

CONFIG = "/home/dev/.config/JetBrains/IntelliJIdea2026.2"
PLUGINS = "/home/dev/.local/share/JetBrains/IntelliJIdea2026.2"
LOG = "/home/dev/.cache/JetBrains/IntelliJIdea2026.2/log/idea.log"

# The last thing a Gradle import does to the project model. Until this line is
# in the log the project has no resolved source roots or classpath, and the
# daemon reports "Not resolved until the project is fully loaded" instead of
# real errors (HARNESS.md §13). Keyed to the pinned IDE build; a new major may
# reword it, and test_ready_means_gradle_synced will say so.
SYNC_COMMITTED = "Project model for project .*External system: commit model"

# Trusting a project means IntelliJ will execute its build scripts. The fixture
# is authored in this repository, so this is not a judgement about third-party
# code — but it is still a security setting, recorded here rather than buried.
TRUSTED_PATHS = """<application>
  <component name="Trusted.Paths">
    <option name="TRUSTED_PROJECT_PATHS">
      <map>
        <entry key="/work/fixture" value="true" />
      </map>
    </option>
  </component>
  <component name="Trusted.Paths.Settings">
    <option name="TRUSTED_PATHS">
      <list>
        <option value="/work" />
      </list>
    </option>
  </component>
</application>
"""


class Ide:
    def __init__(self, container: Container):
        self.c = container
        self.display = Display(container)

    # ---------------------------------------------------------------- setup
    def trust(self, path: str = "/work") -> None:
        self.c.write_file(f"{CONFIG}/options/trusted-paths.xml", TRUSTED_PATHS)


    def install_plugin(self, host_zip: Path) -> None:
        self.c.exec(f"mkdir -p {PLUGINS}")
        self.c.exec(f"rm -rf {PLUGINS}/canary")
        import subprocess
        subprocess.run(
            ["docker", "cp", str(host_zip), f"{self.c.name}:/tmp/plugin.zip"],
            check=True, capture_output=True,
        )
        self.c.exec(f"cd {PLUGINS} && unzip -oq /tmp/plugin.zip")

    def installed_plugins(self) -> list[str]:
        return self.c.exec(f"ls -1 {PLUGINS} 2>/dev/null", check=False).stdout.split()

    # ---------------------------------------------------------------- run
    def launch(self, project: str = "/work/fixture") -> None:
        self.c.exec_detached(
            f"DISPLAY=:99 /opt/idea/bin/idea {project}",
            log="/home/dev/.harness/log/idea.log",
        )

    def await_window(self, *needles: str, timeout: float = 240.0) -> str:
        deadline = time.monotonic() + timeout
        seen: list[str] = []
        while time.monotonic() < deadline:
            seen = self.display.window_titles()
            for title in seen:
                if any(n.lower() in title.lower() for n in needles):
                    return title
            time.sleep(2.0)
        raise TimeoutError(
            f"no window matching {needles!r} within {timeout}s; saw: {seen}"
        )

    def sync_commits(self) -> int:
        """How many Gradle imports have committed their model. idea.log is
        appended across relaunches, so callers waiting for a *new* import
        compare against a count taken before launching."""
        out = self.c.exec(f"grep -c '{SYNC_COMMITTED}' {LOG} 2>/dev/null",
                          check=False).stdout.strip()
        return int(out or 0)

    def log_tail(self, lines: int = 60) -> str:
        return self.c.exec(f"tail -{lines} {LOG} 2>/dev/null", check=False).stdout

    def is_running(self) -> bool:
        # By process name, not `pgrep -f`: the shell running this command has
        # "/opt/idea" in its own command line, so -f always matched itself and
        # reported a dead IDE as running.
        return bool(self.c.exec("pgrep -x idea | head -1", check=False).stdout.strip())

    def kill(self) -> None:
        """A crash: SIGKILL, so nothing shuts down cleanly and the Brain cannot
        withdraw its Registry entry. A plain `quit` (SIGTERM) can take over half a
        minute to finish."""
        self.c.exec("pkill -9 -x idea; pkill -9 -x fsnotifier; true", check=False)

    def quit(self) -> None:
        self.c.exec("pkill -f '/opt/idea' || true", check=False)
