"""Running a test (FEATURES.md §6c): `$/ij/runnables`, `$/ij/run`, `$/ij/run/cancel`.

The Run action of IntelliJ's own gutter marker, performed for a position exactly the way a real
click resolves it (`ConfigurationContext`), so which test framework and whether Gradle or IntelliJ's
own runner executes it are the project's own settings - confirmed by reproducing against the real
IDE before writing any of this. Reproduction also found the generic `ProgramRunnerUtil` execution
path produces no output at all for this project's Gradle-backed configuration in this headless
harness; driving the same settings through `ExternalSystemUtil.runTask`, exactly as `GradleTasks`
already does (tested), is what actually works and is what is tested here.
"""
from __future__ import annotations

import time

import pytest

from harness.wire import RpcError, SRC, uri
from test_navigation import at, mirror, ready

CALC = f"{SRC}/probe/Calculator.kt"
CALC_TEST = f"{SRC.replace('/main/', '/test/')}/probe/CalculatorTest.kt"
MULT_TEST = f"{SRC.replace('/main/', '/test/')}/probe/MultiplierTest.java"
FAILING_TEST = f"{SRC.replace('/main/', '/test/')}/probe/FailingTest.kt"
SLOW_TEST = f"{SRC.replace('/main/', '/test/')}/probe/SlowTest.kt"


def run_at(w, path, pos, scope="nearest", timeout=30):
    return w.request("$/ij/run", {
        "textDocument": {"uri": uri(path)},
        "position": {"line": pos[0], "character": pos[1]},
        "scope": scope,
    }, timeout=timeout)


def until_finished(w, run_id, timeout=90):
    return w.notifications("$/ij/run/finished", until=lambda p: p["runId"] == run_id, timeout=timeout)[-1]


def until_status(w, run_id, statuses, timeout=90):
    """Every `$/ij/test/status` for this run, up to and including the first terminal one."""
    return w.notifications("$/ij/test/status", until=lambda p: p["runId"] == run_id and p["status"] in statuses, timeout=timeout)


class TestCapability:
    def test_it_is_advertised(self, bridge):
        from harness.wire import Wire
        with Wire(bridge.port) as w:
            assert w.initialize()["capabilities"]["testRunning"] is True


class TestRunnables:
    """What can be run, harvested from IntelliJ's own gutter markers - the sign column's data."""

    def test_kotlin_class_and_method_markers(self, bridge_container, wire):
        ready(wire)
        texts = mirror(wire, bridge_container, CALC_TEST)
        markers = wire.notifications("$/ij/runnables",
            until=lambda p: p["uri"] == uri(CALC_TEST) and p["runnables"], timeout=60)[-1]["runnables"]
        by_name = {m["name"]: m for m in markers}
        assert by_name.keys() == {"CalculatorTest", "addsTwoNumbers", "subtractsTwoNumbers"}
        assert by_name["CalculatorTest"]["kind"] == "class"
        assert by_name["addsTwoNumbers"]["kind"] == "method"
        assert by_name["subtractsTwoNumbers"]["kind"] == "method"
        assert all("Run Test" in m["title"] and "  " not in m["title"] for m in markers)

    def test_java_class_and_method_markers(self, bridge_container, wire):
        ready(wire)
        mirror(wire, bridge_container, MULT_TEST)
        markers = wire.notifications("$/ij/runnables",
            until=lambda p: p["uri"] == uri(MULT_TEST) and p["runnables"], timeout=60)[-1]["runnables"]
        by_name = {m["name"]: m for m in markers}
        assert by_name.keys() == {"MultiplierTest", "multipliesTwoNumbers"}
        assert by_name["MultiplierTest"]["kind"] == "class"
        assert by_name["multipliesTwoNumbers"]["kind"] == "method"

    def test_a_plain_class_with_no_tests_has_none(self, bridge_container, wire):
        ready(wire)
        texts = mirror(wire, bridge_container, CALC)
        wire.did_change(CALC, 1, {"text": texts[CALC]})  # force a fresh daemon pass on this Mirror
        empty_or_none = wire.notifications("$/ij/runnables", until=lambda p: p["uri"] == uri(CALC), timeout=30)
        assert not empty_or_none or empty_or_none[-1]["runnables"] == []


class TestRunningIt:
    def test_the_nearest_test_passes(self, bridge_container, wire):
        ready(wire)
        texts = mirror(wire, bridge_container, CALC_TEST)
        pos = at(texts[CALC_TEST], "fun addsTwoNumbers", 0, 4)
        started = run_at(wire, CALC_TEST, pos)
        run_id = started["runId"]
        statuses = until_status(wire, run_id, {"passed", "failed", "error"})
        assert [s["status"] for s in statuses] == ["started", "passed"]
        assert statuses[-1]["name"] == "addsTwoNumbers"
        finished = until_finished(wire, run_id)
        assert finished == {"runId": run_id, "success": True, "cancelled": False, "ms": finished["ms"]}

    def test_a_failing_test_reports_failed_with_a_stacktrace(self, bridge_container, wire):
        ready(wire)
        texts = mirror(wire, bridge_container, FAILING_TEST)
        pos = at(texts[FAILING_TEST], "fun deliberatelyWrong", 0, 4)
        started = run_at(wire, FAILING_TEST, pos)
        run_id = started["runId"]
        statuses = until_status(wire, run_id, {"passed", "failed", "error"})
        failed = statuses[-1]
        assert failed["status"] == "failed" and failed["name"] == "deliberatelyWrong"
        assert "AssertionFailedError" in failed["stacktrace"] and "FailingTest.kt" in failed["stacktrace"]
        finished = until_finished(wire, run_id)
        assert finished["success"] is False and finished["cancelled"] is False and "error" in finished

    def test_class_scope_runs_the_classs_own_tests(self, bridge_container, wire):
        ready(wire)
        texts = mirror(wire, bridge_container, CALC_TEST)
        pos = at(texts[CALC_TEST], "class CalculatorTest", 0, 6)
        started = run_at(wire, CALC_TEST, pos, scope="class")
        finished = until_finished(wire, started["runId"])
        assert finished["success"] is True

    def test_nowhere_runnable_finishes_unsuccessfully(self, bridge_container, wire):
        """Resolving what to run touches PSI on the EDT, so this cannot be known synchronously: the
        request still acks with a runId, and "nothing here" arrives as an unsuccessful finish."""
        ready(wire)
        texts = mirror(wire, bridge_container, CALC)
        pos = at(texts[CALC], "fun add", 0, 4)
        started = run_at(wire, CALC, pos)
        finished = until_finished(wire, started["runId"])
        assert finished["success"] is False and "error" in finished


class TestOneAtATimeAndCancel:
    def test_a_second_run_is_refused_while_one_is_running_and_cancel_stops_it(self, bridge_container, wire):
        """`SlowTest`: with the Gradle build cache warm, an ordinary test finishes too fast to
        reliably race a cancel against - this one sleeps long enough to actually stop mid-run."""
        ready(wire)
        texts = mirror(wire, bridge_container, SLOW_TEST)
        pos = at(texts[SLOW_TEST], "fun takesAWhile", 0, 4)
        started = run_at(wire, SLOW_TEST, pos)
        try:
            with pytest.raises(RpcError) as e:
                run_at(wire, SLOW_TEST, pos, timeout=30)
            assert e.value.code == -32602 and "already running" in str(e.value)

            deadline = time.monotonic() + 90
            cancelled = False
            while not cancelled and time.monotonic() < deadline:  # it may not have a taskId assigned yet
                cancelled = wire.request("$/ij/run/cancel", {"runId": started["runId"]}, timeout=30)["cancelled"]
                if not cancelled:
                    time.sleep(1)
            assert cancelled
            finished = until_finished(wire, started["runId"])
            assert finished["cancelled"] is True and finished["success"] is False
        finally:
            wire.request("$/ij/run/cancel", {"runId": started["runId"]}, timeout=30)


class TestThroughNeovim:
    def test_the_nearest_key_starts_a_run_and_shows_the_result(self, nvim, bridge):
        from test_navigation import attached
        from harness.util import wait_until

        nvim.command(f"edit {CALC_TEST}")
        wait_until(lambda: attached(nvim) == 1, message="never attached")
        text = "\n".join(nvim.current.buffer[:])
        line, col = at(text, "fun addsTwoNumbers", 0, 4)
        nvim.current.window.cursor = (line + 1, col)
        wait_until(lambda: nvim.exec_lua("""
                for _, m in ipairs(vim.api.nvim_buf_get_keymap(0, 'n')) do
                  if m.lhs:sub(-2) == 'tn' then return true end
                end
                return false"""), timeout=30, message="<leader>tn never appeared for this buffer")
        nvim.exec_lua("""_G.__finished = nil
            vim.api.nvim_create_autocmd('User', { pattern = 'IjBridgeTestRunFinished', once = true,
              callback = function(a) _G.__finished = a.data end })""")
        nvim.feedkeys(nvim.replace_termcodes("<Space>tn"), "m", False)

        wait_until(lambda: nvim.exec_lua("return _G.__finished") is not None, timeout=90,
                   message="the test run never finished through Neovim")
        result = nvim.exec_lua("return _G.__finished")
        assert result["success"] is True

        sign = nvim.exec_lua("""
            local buf = vim.api.nvim_get_current_buf()
            local state = require('ij_bridge.runnables').state[buf]
            local m = state and state.marks['addsTwoNumbers']
            if not m then return nil end
            local marks = vim.api.nvim_buf_get_extmarks(buf, vim.api.nvim_create_namespace('ij_bridge_runnables'),
              m.id, m.id, { details = true })
            return marks[1] and marks[1][4].sign_text""")
        assert sign and sign.strip() == '✔', "the sign never turned into a pass mark"
