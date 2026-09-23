"""IntelliJ Run Configurations (FEATURES.md §6c): `$/ij/runConfigurations`,
`$/ij/runConfiguration/run`, `$/ij/runConfiguration/cancel`.

A developer's own named, saved way to run something (`RunManager`), possibly checked into the
`.run` folder and so shared through the repo (`fixture/.run/*.run.xml` here) - distinct from a raw
Gradle task (`test_gradle_tasks.py`) and from running the test at the cursor
(`test_test_running.py`). Confirmed by reproducing against the real IDE first: a checked-in `.run`
file is picked up with nothing more than the ordinary VFS refresh already used elsewhere, no reload
lever needed.

v1 runs only Gradle-backed configurations - their settings extracted and driven through
`ExternalSystemUtil.runTask`, exactly `GradleTasks`' and `TestRunner`'s own proven machinery. A
plain JVM Application configuration (`fixture/.run/Application.run.xml`, checked in for the listing
to have a real non-Gradle example) is a real, distinct, unfinished gap: every generic execution
entry point tried against it silently did nothing at all, once a "Make before launch" step - which
hangs on this project's own deliberately-broken diagnostics probe - was ruled out. Refused with a
clear reason rather than attempted silently.
"""
from __future__ import annotations

import time

import pytest

from harness.wire import RpcError, Wire
from test_navigation import ready


def listing(w) -> list[dict]:
    return w.request("$/ij/runConfigurations", {}, timeout=30)["configurations"]


def collect(w, run_id: str, timeout: float = 300):
    """The output, status and end of one run, in the order they arrived."""
    output, statuses, deadline = [], [], time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg = w.read(max(1.0, deadline - time.monotonic()))
        method, params = msg.get("method"), msg.get("params") or {}
        if method == "$/ij/runConfiguration/output" and params.get("runId") == run_id:
            output.append(params)
        elif method == "$/ij/runConfiguration/status" and params.get("runId") == run_id:
            statuses.append(params)
        elif method == "$/ij/runConfiguration/finished" and params.get("runId") == run_id:
            return output, statuses, params
        else:
            w.inbox.append(msg)
    raise AssertionError(f"the run never finished; output so far: {''.join(o['text'] for o in output)[-400:]}")


def run(w, name: str):
    started = w.request("$/ij/runConfiguration/run", {"name": name}, timeout=30)
    return started, *collect(w, started["runId"])


class TestCapability:
    def test_it_is_advertised(self, bridge):
        with Wire(bridge.port) as w:
            assert w.initialize()["capabilities"]["runConfigurations"] is True


class TestListing:
    def test_both_the_gradle_and_the_plain_configuration_are_listed(self, bridge_container, wire):
        ready(wire)
        items = {c["name"]: c for c in listing(wire)}
        assert items.keys() >= {"Gradle Help (Info)", "Gradle Build", "Application"}
        assert items["Gradle Help (Info)"]["gradle"] is True
        assert items["Gradle Build"]["gradle"] is True
        assert items["Application"]["gradle"] is False


class TestRunning:
    def test_a_gradle_backed_configuration_runs_with_its_own_parameters(self, bridge_container, wire):
        """`Gradle Help (Info)`: help with --info, checked into .run - proves a *specific-parameters*
        Gradle run, not just a bare task."""
        started, output, statuses, finished = run(wire, "Gradle Help (Info)")
        assert started["name"] == "Gradle Help (Info)"
        assert finished["success"] is True and finished["cancelled"] is False and finished["ms"] > 0
        text = "".join(o["text"] for o in output)
        assert "Welcome to Gradle" in text, text[-300:]
        assert statuses, "no $/ij/runConfiguration/status notification arrived"
        assert all("description" in s for s in statuses)

    def test_a_plain_application_configuration_is_refused(self, bridge_container, wire):
        with pytest.raises(RpcError) as e:
            wire.request("$/ij/runConfiguration/run", {"name": "Application"}, timeout=30)
        assert e.value.code == -32602 and "Gradle" in str(e.value)

    def test_an_unknown_name_is_refused(self, bridge_container, wire):
        with pytest.raises(RpcError) as e:
            wire.request("$/ij/runConfiguration/run", {"name": "ThisDoesNotExist"}, timeout=30)
        assert e.value.code == -32602


class TestOneAtATimeAndCancel:
    def test_a_second_run_is_refused_while_one_is_running_and_cancel_stops_it(self, bridge_container, wire):
        """`Gradle Build`: long enough (compiles the whole module) to ask for a second run, and to
        stop it."""
        started = wire.request("$/ij/runConfiguration/run", {"name": "Gradle Build"}, timeout=30)
        try:
            with pytest.raises(RpcError) as e:
                wire.request("$/ij/runConfiguration/run", {"name": "Gradle Help (Info)"}, timeout=30)
            assert e.value.code == -32602 and "already running" in str(e.value)
            assert listing(wire).__class__ is list  # still answers while one runs

            deadline = time.monotonic() + 90
            cancelled = False
            while not cancelled and time.monotonic() < deadline:
                cancelled = wire.request("$/ij/runConfiguration/cancel", {"runId": started["runId"]}, timeout=30)["cancelled"]
                if not cancelled:
                    time.sleep(1)
            assert cancelled, "the run could not be stopped"
            _, _, finished = collect(wire, started["runId"])
            assert finished["cancelled"] is True and finished["success"] is False
        finally:
            wire.request("$/ij/runConfiguration/cancel", {}, timeout=30)

    def test_cancelling_when_nothing_runs_is_harmless(self, bridge_container, wire):
        result = wire.request("$/ij/runConfiguration/cancel", {}, timeout=30)
        assert result["cancelled"] is False


class TestThroughNeovim:
    def test_the_key_finds_and_runs_one(self, nvim, bridge_container):
        from harness.util import wait_until
        from harness.wire import SRC
        from test_navigation import attached

        calc = f"{SRC}/probe/Calculator.kt"
        nvim.command(f"edit {calc}")
        wait_until(lambda: attached(nvim) == 1, message="never attached")
        wait_until(lambda: nvim.exec_lua("""
                for _, m in ipairs(vim.api.nvim_buf_get_keymap(0, 'n')) do
                  if m.lhs:sub(-2) == 'ce' then return true end
                end
                return false"""), timeout=30, message="<leader>ce never appeared for this buffer")

        nvim.exec_lua("""_G.__finished = nil
            vim.api.nvim_create_autocmd('User', { pattern = 'IjBridgeRunConfigurationFinished', once = true,
              callback = function(a) _G.__finished = a.data end })
            require('ij_bridge.runconfigs').run({ name = 'Gradle Help (Info)' })""")

        wait_until(lambda: nvim.exec_lua("return _G.__finished") is not None, timeout=90,
                   message="the run configuration never finished through Neovim")
        result = nvim.exec_lua("return _G.__finished")
        assert result["success"] is True
        output = nvim.exec_lua("""
            local b = require('ij_bridge.runconfigs').output_buffer_for_tests()
            if not b then return '' end
            return table.concat(vim.api.nvim_buf_get_lines(b, 0, -1, false), '\\n')""")
        assert "Welcome to Gradle" in output
