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

    def test_a_failing_test_turns_its_sign_into_a_cross_and_shows_the_assertion(self, nvim, bridge):
        from test_navigation import attached
        from harness.util import wait_until

        nvim.command(f"edit {FAILING_TEST}")
        wait_until(lambda: attached(nvim) == 1, message="never attached")
        text = "\n".join(nvim.current.buffer[:])
        line, col = at(text, "fun deliberatelyWrong", 0, 4)
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
        assert nvim.exec_lua("return _G.__finished")["success"] is False

        sign = nvim.exec_lua("""
            local buf = vim.api.nvim_get_current_buf()
            local state = require('ij_bridge.runnables').state[buf]
            local m = state and state.marks['deliberatelyWrong']
            if not m then return nil end
            local marks = vim.api.nvim_buf_get_extmarks(buf, vim.api.nvim_create_namespace('ij_bridge_runnables'),
              m.id, m.id, { details = true })
            return marks[1] and marks[1][4].sign_text""")
        shown = nvim.exec_lua("""
            local b = require('ij_bridge.test_run').output_buffer_for_tests()
            return b and table.concat(vim.api.nvim_buf_get_lines(b, 0, -1, false), '\\n')""")
        print("SIGN:", repr(sign))
        print("OUTPUT:\n" + (shown or ""))
        assert sign and sign.strip() == '✘', f"the sign was {sign!r}"


class TestResultsTree:
    """What the Brain tells the Editor about a run, enough for a tree of results and a log per test."""

    @staticmethod
    def run_class_in_neovim(nvim, path, marker):
        from test_navigation import attached
        from harness.util import wait_until

        nvim.command(f"edit {path}")
        wait_until(lambda: attached(nvim) == 1, message="never attached")
        text = "\n".join(nvim.current.buffer[:])
        line, col = at(text, marker, 0, 6)
        nvim.current.window.cursor = (line + 1, col)
        wait_until(lambda: nvim.exec_lua("""
                for _, m in ipairs(vim.api.nvim_buf_get_keymap(0, 'n')) do
                  if m.lhs:sub(-2) == 'tc' then return true end
                end
                return false"""), timeout=30, message="<leader>tc never appeared for this buffer")
        nvim.exec_lua("""_G.__finished = nil
            vim.api.nvim_create_autocmd('User', { pattern = 'IjBridgeTestRunFinished', once = true,
              callback = function(a) _G.__finished = a.data end })""")
        nvim.feedkeys(nvim.replace_termcodes("<Space>tc"), "m", False)
        wait_until(lambda: nvim.exec_lua("return _G.__finished") is not None, timeout=90,
                   message="the test run never finished through Neovim")

    @staticmethod
    def tree(nvim) -> list[str]:
        return nvim.exec_lua("return require('ij_bridge.test_results').tree_lines_for_tests()")

    @staticmethod
    def log(nvim) -> str:
        return "\n".join(nvim.exec_lua("return require('ij_bridge.test_results').log_lines_for_tests()"))

    @staticmethod
    def select(nvim, text):
        """Put the cursor on the tree line containing `text`, as a developer moving down to it would."""
        nvim.exec_lua("""
            local text = ...
            local b = require('ij_bridge.test_results').run.tree_buf
            local win = vim.fn.win_findbuf(b)[1]
            for i, l in ipairs(vim.api.nvim_buf_get_lines(b, 0, -1, false)) do
              if l:find(text, 1, true) then
                vim.api.nvim_set_current_win(win)
                vim.api.nvim_win_set_cursor(win, { i, 0 })
                vim.cmd('doautocmd CursorMoved')
                return
              end
            end
            error('no tree line with ' .. text)""", text)

    def test_a_failed_run_is_a_tree_with_the_assertion_and_what_the_test_printed(self, nvim, bridge):
        self.run_class_in_neovim(nvim, FAILING_TEST, "class FailingTest")
        tree = self.tree(nvim)
        assert tree[0].startswith("1 failed"), tree
        assert any(l.startswith("✘ FailingTest") for l in tree) and any(l.startswith("  ✘ deliberatelyWrong") for l in tree), tree

        self.select(nvim, "deliberatelyWrong")
        log = self.log(nvim)
        assert "expected: <1> but was: <2>" in log, log            # the failed assertion
        assert "FailingTest.kt:" in log, log                        # the developer's own frame is there...
        assert "org.junit.jupiter.api.AssertEquals" not in log, log   # ...the framework's are folded away
        assert "about to fail" in log and "[stderr] a complaint on stderr" in log, log   # what the test printed

    def test_a_failed_assertion_is_marked_on_its_line(self, nvim, bridge):
        self.run_class_in_neovim(nvim, FAILING_TEST, "class FailingTest")
        marks = nvim.exec_lua("""
            return vim.tbl_map(function(d) return { line = d.lnum, message = d.message } end,
              vim.diagnostic.get(vim.api.nvim_get_current_buf(), { namespace = vim.api.nvim_create_namespace('ij_bridge_test_failures') }))""")
        text = nvim.current.buffer[:]
        (mark,) = marks
        assert "assertEquals(1, 2)" in text[mark["line"]] and "expected: <1> but was: <2>" in mark["message"], (mark, text)

    def test_a_passing_class_is_a_tree_of_ticks(self, nvim, bridge):
        self.run_class_in_neovim(nvim, CALC_TEST, "class CalculatorTest")
        tree = self.tree(nvim)
        assert "failed" not in tree[0] and "passed" in tree[0], tree
        assert any(l.startswith("✔ CalculatorTest") for l in tree) and any("addsTwoNumbers" in l for l in tree), tree
        self.select(nvim, "addsTwoNumbers")
        assert "addsTwoNumbers" in self.log(nvim)

    def test_the_failures_only_filter(self, nvim, bridge):
        self.run_class_in_neovim(nvim, CALC_TEST, "class CalculatorTest")
        nvim.exec_lua("require('ij_bridge.test_results').toggle_failures()")
        assert self.tree(nvim)[1:] == [], self.tree(nvim)            # nothing failed: nothing listed
        nvim.exec_lua("require('ij_bridge.test_results').toggle_failures()")
        assert len(self.tree(nvim)) > 1

    def test_running_the_same_tests_again_runs_them_again(self, nvim, bridge):
        """Gradle would call the second run UP-TO-DATE and report nothing: a run is always a run."""
        self.run_class_in_neovim(nvim, CALC_TEST, "class CalculatorTest")
        self.run_class_in_neovim(nvim, CALC_TEST, "class CalculatorTest")
        tree = self.tree(nvim)
        assert "passed" in tree[0] and any(l.startswith("✔ CalculatorTest") for l in tree), tree
