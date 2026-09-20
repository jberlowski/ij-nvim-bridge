"""Does the harness support the work ahead?

Each test names the SPEC or HARNESS clause it underwrites. If one fails, the
harness cannot carry the Spike (SPEC.md §11) and the gap is the failure message,
not a guess.

Nothing here tests the Bridge. The Bridge does not exist.
"""
from __future__ import annotations

import json
import re
import statistics
import urllib.request

import pytest

FIXTURE = "/work/fixture"
PROBE = f"{FIXTURE}/src/main/kotlin/dev/bridge/fixture/probe"
CONTROLLER = f"{FIXTURE}/src/main/kotlin/dev/bridge/fixture/web/GreetingController.kt"


# ---------------------------------------------------------------- environment
class TestEnvironment:
    """HARNESS.md §2 — the container is a usable Linux with the right pieces."""

    def test_intellij_is_the_pinned_build(self, container):
        build = container.read_file("/opt/idea/build.txt").strip()
        assert build == "IU-262.10968.63", f"pinned build drifted: {build}"

    def test_neovim_and_blink_present(self, container):
        version = container.exec("nvim --version | head -1").stdout
        assert "NVIM v0.12.5" in version, version
        # blink.cmp is LazyVim's default and the Bridge ships a source for it
        # (SPEC.md §6.1), so the harness must run the real thing.
        container.exec("test -d ~/.local/share/nvim/lazy/blink.cmp")

    def test_runtime_dir_is_the_real_path_not_the_fallback(self, container):
        """SPEC.md §4.2 — exercising the fallback by accident would hide bugs."""
        out = container.exec('echo "$XDG_RUNTIME_DIR"').stdout.strip()
        assert out == "/run/user/1000"
        perms = container.exec("stat -c '%a %U' /run/user/1000").stdout.strip()
        assert perms == "700 dev", perms

    def test_inotify_headroom_for_vfs_watchers(self, container):
        """HARNESS.md §12 — IntelliJ's VFS uses inotify; low limits break it."""
        watches = int(container.read_file("/proc/sys/fs/inotify/max_user_watches"))
        assert watches >= 100_000, f"only {watches} inotify watches"

    def test_nothing_runs_as_root(self, container):
        """SPEC.md §3 — Unprivileged is an invariant, so prove the premise."""
        assert container.exec("id -un").stdout.strip() == "dev"
        assert container.exec("id -u").stdout.strip() == "1000"


# ------------------------------------------------------------------- fixture
class TestFixture:
    """HARNESS.md §7 — the deliberate probes exist and say why."""

    @pytest.mark.parametrize("name", [
        "ResolutionError.kt", "InspectionWarning.kt",
        "CrossFileProducer.kt", "CrossFileConsumer.kt", "LargeSurface.kt",
    ])
    def test_probe_present(self, container, name):
        container.exec(f"test -f {PROBE}/{name}")

    def test_resolution_error_is_genuinely_unresolvable(self, container):
        body = container.read_file(f"{PROBE}/ResolutionError.kt")
        assert "thisFunctionDoesNotExistAnywhere" in body
        assert "Do not \"fix\" this file" in body

    def test_large_surface_is_big_enough_to_cost_something(self, container):
        body = container.read_file(f"{PROBE}/LargeSurface.kt")
        members = len(re.findall(r"\bfun computeMetricNumber|\bval describedProperty", body))
        assert members >= 500, f"only {members} members; completion may be free"

    def test_cross_file_scenario_has_an_insertion_point(self, container):
        body = container.read_file(f"{PROBE}/CrossFileProducer.kt")
        assert "HARNESS-INSERTION-POINT" in body

    def test_dependencies_are_pre_resolved(self, container):
        """Reindexing from cold every run would dominate and prove nothing."""
        out = container.exec("du -sm ~/.gradle | cut -f1").stdout.strip()
        assert int(out) > 100, f"gradle cache only {out}MB; deps not primed"


# ---------------------------------------------------------------- observation
class TestObservability:
    """HARNESS.md §4 — evidence, never verdicts."""

    def test_novnc_serves_the_live_display(self, container):
        with urllib.request.urlopen(container.novnc_url, timeout=10) as r:
            assert r.status == 200

    def test_screenshot_of_running_ide(self, container, ide, tmp_path):
        shot = tmp_path / "ide.png"
        from harness.display import Display
        Display(container).screenshot(shot)
        assert shot.stat().st_size > 20_000, "screenshot suspiciously small"
        assert shot.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    def test_ide_window_is_actually_mapped(self, ide, display):
        """The project window is real and laid out.

        Matches either name: IntelliJ titles the window after the directory
        until Gradle sync completes, then after rootProject.name.
        """
        titles = [t.lower() for t in display.window_titles()]
        assert any("fixture" in t or "spring-kotlin-mvc" in t for t in titles), titles


# -------------------------------------------------------------------- editor
class TestEditorControl:
    """HARNESS.md §5 — drive nvim from outside and read its state back."""

    def test_editor_state_is_readable(self, editor):
        editor.edit(CONTROLLER)
        lines = editor.lines()
        assert any("class GreetingController" in ln for ln in lines)

    def test_cursor_is_observable_and_movable(self, editor):
        """Both halves of the cursor-sync test: move it, then read it back.

        The target line is chosen from the buffer rather than hardcoded — a
        blank line silently clamps the column and makes the assertion a lie.
        """
        editor.edit(CONTROLLER)
        lines = editor.lines()
        target = next(i for i, ln in enumerate(lines, start=1) if len(ln) > 20)

        editor.nvim.funcs.cursor(target, 5)     # 1-based row, 1-based column
        row, col = editor.cursor()
        assert (row, col) == (target, 5), f"cursor reported {(row, col)}"

        editor.nvim.funcs.cursor(target, 1)
        assert editor.cursor() == (target, 1)

    def test_buffer_edits_apply_without_saving(self, editor):
        """The unsaved-buffer case the Mirror Set exists for (SPEC.md §5.2)."""
        editor.edit(f"{PROBE}/CrossFileProducer.kt")
        editor.nvim.command(
            r"silent! %s/\/\/ HARNESS-INSERTION-POINT/fun addedWhileUnsaved(): String = \"x\"/"
        )
        assert editor.nvim.current.buffer.options["modified"] is True
        assert any("addedWhileUnsaved" in ln for ln in editor.lines())
        editor.nvim.command("edit!")          # discard, leave fixture pristine


# --------------------------------------------------------------------- brain
class TestBrainReachable:
    """SPEC.md §4.2 — plugin loads, serves a socket, publishes the Registry."""

    def test_canary_plugin_loaded(self, container, ide):
        assert "canary" in ide.installed_plugins()

    def test_registry_published(self, brain):
        reg = brain.registry()
        assert reg.raw.get("version") == 1
        assert len(reg.brains) == 1
        entry = reg.brains[0]
        assert entry["root"] == FIXTURE
        assert entry["sock"].endswith(".sock")
        assert entry["ide"].startswith("IU-262")

    def test_registry_longest_prefix_match_resolves(self, brain):
        reg = brain.registry()
        assert reg.socket_for(CONTROLLER) is not None

    def test_unmatched_path_is_dormant(self, brain):
        """Dormant is the normal state for most files (SPEC.md §3)."""
        assert reg_socket(brain, "/etc/passwd") is None
        assert reg_socket(brain, "/home/dev/scratch/Other.kt") is None

    def test_socket_is_unprivileged_and_owner_only(self, container, brain_socket):
        """SPEC.md §3 — no privileged path, no shared-access socket."""
        assert brain_socket.startswith("/run/user/1000/ij-nvim-bridge/")
        perms = container.exec(f"stat -c '%a %U' {brain_socket}").stdout.strip()
        mode, owner = perms.split()
        assert owner == "dev"
        assert mode == "600", f"socket mode {mode}, expected 600"

    def test_request_response_over_the_socket(self, container, brain, brain_socket):
        brain.bridge(brain_socket)
        resp = brain.request("$/canary/ping")
        assert resp["result"]["pong"] is True


# --------------------------------------------------------------- ide internals
class TestBrainIntrospection:
    """The debug surface of SPEC.md §10 — what assertions will be built on."""

    def test_reports_project_and_indexing_state(self, brain, brain_socket):
        brain.bridge(brain_socket)
        state = brain.request("$/canary/state")["result"]
        assert state["root"] == FIXTURE
        assert isinstance(state["indexing"], bool)
        assert state["ide"].startswith("IU-262")

    def test_indexing_finishes(self, brain, brain_socket):
        """SPEC.md §8 — Indexing must be observable, and must end."""
        import time
        brain.bridge(brain_socket)
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            if brain.request("$/canary/state")["result"]["indexing"] is False:
                return
            time.sleep(5)
        pytest.fail("IntelliJ never left Indexing within 600s")

    def test_plugin_can_open_a_real_editor(self, brain, brain_socket):
        """SPEC.md §11 question 1 — ADR-0001's premise, cheapest probe."""
        brain.bridge(brain_socket)
        result = brain.request("$/canary/openEditor", timeout=120)["result"]
        assert result["opened"] is True, result
        assert result["documentLength"] > 0

    def test_opened_editor_is_visible_to_the_ide(self, brain, brain_socket):
        brain.bridge(brain_socket)
        brain.request("$/canary/openEditor", timeout=120)
        state = brain.request("$/canary/state")["result"]
        assert any("GreetingController.kt" in f for f in state["openFiles"]), state


# ------------------------------------------------------------------- timing
class TestTiming:
    """SPEC.md §7 — the speed gate needs separable, in-container measurement."""

    def test_in_ide_time_is_separable_from_transport(self, brain, brain_socket):
        samples = brain.request_local(brain_socket, "$/canary/ping", repeat=30)
        assert len(samples) == 30

        round_trips, in_ide = [], []
        for s in samples:
            round_trips.append(s["roundTripNanos"])
            r = s["result"]
            in_ide.append(int(r["tRepliedNanos"]) - int(r["tReceivedNanos"]))

        # This is exactly the OVERHEAD decomposition of SPEC.md §7: what the
        # Brain spent, versus what the round trip cost on top of it.
        overhead = [rt - ide for rt, ide in zip(round_trips, in_ide)]
        assert all(o > 0 for o in overhead), "in-IDE time exceeded round trip"

        p95 = statistics.quantiles(overhead, n=20)[-1] / 1e6
        print(f"\n  transport overhead p95 = {p95:.3f} ms over {len(overhead)} samples")
        assert p95 < 50, f"harness transport alone costs {p95:.1f}ms"

    def test_host_measurement_is_not_used_for_gating(self, brain, brain_socket):
        """Host mode adds socat + docker-proxy; it must be visibly worse."""
        brain.bridge(brain_socket)
        local = brain.request_local(brain_socket, "$/canary/ping", repeat=20)
        local_med = statistics.median(s["roundTripNanos"] for s in local) / 1e6

        import time
        host = []
        for _ in range(20):
            t0 = time.perf_counter_ns()
            brain.request("$/canary/ping")
            host.append(time.perf_counter_ns() - t0)
        host_med = statistics.median(host) / 1e6

        print(f"\n  in-container median = {local_med:.3f} ms"
              f"\n  via host median     = {host_med:.3f} ms")
        assert local_med < host_med, (
            "host path was not slower; the in-container requirement may be moot"
        )


def reg_socket(brain, path: str):
    return brain.registry().socket_for(path)
