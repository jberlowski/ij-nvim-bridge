"""Go to test / go to the class under test (FEATURES.md §6c): `$/ij/testTargets`.

Language-agnostic in the platform (`TestFinderHelper`), same as Go to Implementation: from a
class, its test(s); from a test, the class it tests; and, when a production class has no test
yet, a skeleton to create one. One request, one direction chosen by what the caret is in.
"""
from __future__ import annotations

import time

from harness.util import wait_until
from harness.wire import SRC, RpcError, uri
from test_editor_slice import attached
from test_navigation import at, mirror

CALC = f"{SRC}/probe/Calculator.kt"
CALC_TEST = f"{SRC.replace('/main/', '/test/')}/probe/CalculatorTest.kt"
MULT = f"{SRC}/probe/Multiplier.java"
MULT_TEST = f"{SRC.replace('/main/', '/test/')}/probe/MultiplierTest.java"
NO_TEST = f"{SRC}/probe/NoTestYet.kt"
NO_TEST_JAVA = f"{SRC}/probe/NoTestYetJava.java"


def ask(w, path, pos, timeout=90):
    """`$/ij/testTargets`, retried while the project is still indexing."""
    for attempt in range(20):
        try:
            return w.request("$/ij/testTargets", {
                "textDocument": {"uri": uri(path)},
                "position": {"line": pos[0], "character": pos[1]}}, timeout=timeout)
        except RpcError as e:
            if e.code != -32801 or attempt == 19:
                raise
            time.sleep(3)


def paths_of(result) -> set[str]:
    return {loc["uri"].rsplit("/", 1)[-1] for loc in result["locations"]}


class TestCapability:
    def test_it_is_advertised(self, bridge):
        from harness.wire import Wire
        with Wire(bridge.port) as w:
            assert w.initialize()["capabilities"]["testNavigation"] is True


class TestGoToTest:
    """From a production class (or a member of it) with an existing test: the test's location(s)."""

    def test_kotlin_class_to_its_test(self, bridge_container, wire):
        t = mirror(wire, bridge_container, CALC)[CALC]
        result = ask(wire, CALC, at(t, "class Calculator", 0, 6))
        assert result["kind"] == "test"
        assert paths_of(result) == {"CalculatorTest.kt"}
        assert result.get("create") is None, "there is already a test: nothing to create"

    def test_java_class_to_its_test(self, bridge_container, wire):
        t = mirror(wire, bridge_container, MULT)[MULT]
        result = ask(wire, MULT, at(t, "class Multiplier", 0, 6))
        assert result["kind"] == "test"
        assert paths_of(result) == {"MultiplierTest.java"}

    def test_from_a_method_inside_the_class_too(self, bridge_container, wire):
        t = mirror(wire, bridge_container, CALC)[CALC]
        result = ask(wire, CALC, at(t, "fun add", 0, 4))
        assert paths_of(result) == {"CalculatorTest.kt"}


class TestGoToClassUnderTest:
    """From a test: the class it tests."""

    def test_kotlin_test_to_its_class(self, bridge_container, wire):
        t = mirror(wire, bridge_container, CALC_TEST)[CALC_TEST]
        result = ask(wire, CALC_TEST, at(t, "class CalculatorTest", 0, 6))
        assert result["kind"] == "class"
        assert paths_of(result) == {"Calculator.kt"}

    def test_java_test_to_its_class(self, bridge_container, wire):
        t = mirror(wire, bridge_container, MULT_TEST)[MULT_TEST]
        result = ask(wire, MULT_TEST, at(t, "class MultiplierTest", 0, 6))
        assert result["kind"] == "class"
        assert paths_of(result) == {"Multiplier.java"}

    def test_from_a_test_method_too(self, bridge_container, wire):
        t = mirror(wire, bridge_container, CALC_TEST)[CALC_TEST]
        result = ask(wire, CALC_TEST, at(t, "fun addsTwoNumbers", 0, 4))
        assert paths_of(result) == {"Calculator.kt"}


class TestOfferingToCreateOne:
    """No test exists yet: nothing to go to, but a skeleton to create one."""

    def test_kotlin_class_with_no_test_offers_to_create_one(self, bridge_container, wire):
        t = mirror(wire, bridge_container, NO_TEST)[NO_TEST]
        result = ask(wire, NO_TEST, at(t, "class NoTestYet", 0, 6))
        assert result["kind"] == "test" and result["locations"] == []
        created = result["create"]
        assert created["uri"] == uri(NO_TEST.replace("/main/", "/test/").replace("NoTestYet.kt", "NoTestYetTest.kt"))
        assert created["className"] == "NoTestYetTest"
        assert created["package"] == "dev.bridge.fixture.probe"
        text = created["text"]
        assert "package dev.bridge.fixture.probe" in text
        assert "import org.junit.jupiter.api.Test" in text
        assert "class NoTestYetTest" in text
        assert "fun testGreet()" in text and "fun testFarewell()" in text, text

    def test_java_class_with_no_test_offers_to_create_one(self, bridge_container, wire):
        t = mirror(wire, bridge_container, NO_TEST_JAVA)[NO_TEST_JAVA]
        result = ask(wire, NO_TEST_JAVA, at(t, "class NoTestYetJava", 0, 6))
        created = result["create"]
        assert created["uri"].endswith("/probe/NoTestYetJavaTest.java")
        assert created["className"] == "NoTestYetJavaTest"
        text = created["text"]
        assert "package dev.bridge.fixture.probe;" in text
        assert "import org.junit.jupiter.api.Test;" in text
        assert "class NoTestYetJavaTest" in text
        assert "void testGreet()" in text, text

    def test_the_created_skeleton_actually_compiles_once_written(self, bridge_container, wire):
        """The point of it: not just plausible text, a real, valid test IntelliJ accepts. `did_open`
        of a file that does not exist on disk yet is silently dropped (the same as a brand-new
        buffer from `new_file`, FEATURES.md §5e) so - as a real Editor would - the file is written
        first; that first write is what attaches it."""
        t = mirror(wire, bridge_container, NO_TEST)[NO_TEST]
        result = ask(wire, NO_TEST, at(t, "class NoTestYet", 0, 6))
        created = result["create"]
        path = created["uri"].replace("file://", "")
        try:
            bridge_container.write_file(path, created["text"])
            wire.request("$/ij/debug/refresh", {})
            wire.did_open(path, created["text"])
            pub = wire.notifications("textDocument/publishDiagnostics",
                                     until=lambda p: p["uri"] == created["uri"], timeout=60)
            errors = [d for d in pub[-1]["diagnostics"] if d["severity"] == 1] if pub else []
            assert errors == [], errors
        finally:
            wire.did_close(path)
            bridge_container.exec(f"rm -f {path}", check=False)
            # Otherwise the VFS keeps believing the file exists for the rest of the session (it was
            # told to look once, when the file was written; deleting it on disk alone does not tell
            # it again), and every later "does a test already exist" check sees a false positive.
            wire.request("$/ij/debug/refresh", {})

    def test_nothing_is_written_to_disk_by_asking(self, bridge_container, wire):
        t = mirror(wire, bridge_container, NO_TEST)[NO_TEST]
        ask(wire, NO_TEST, at(t, "class NoTestYet", 0, 6))
        result = bridge_container.exec(
            "test -f /work/fixture/src/test/kotlin/dev/bridge/fixture/probe/NoTestYetTest.kt && echo y || echo n")
        assert result.stdout.strip() == "n"

    def test_once_a_test_exists_it_is_no_longer_offered(self, bridge_container, wire):
        """CalculatorTest.kt already exists: Calculator must not still offer to create one (a
        control showing the "create" case above is not simply always returned)."""
        t = mirror(wire, bridge_container, CALC)[CALC]
        result = ask(wire, CALC, at(t, "class Calculator", 0, 6))
        assert result.get("create") is None


class TestThroughNeovim:

    def request(self, nvim, needle: str, into: int):
        text = "\n".join(nvim.current.buffer[:])
        line, col = at(text, needle, 0, into)
        return nvim.exec_lua("""
            local line, col = ...
            local res = vim.lsp.buf_request_sync(0, '$/ij/testTargets', {
              textDocument = vim.lsp.util.make_text_document_params(),
              position = { line = line, character = col } }, 60000)
            for _, r in pairs(res or {}) do if r.result then return r.result end end""", line, col)

    def test_class_to_test_through_neovims_own_request(self, nvim):
        nvim.command(f"edit {CALC}")
        wait_until(lambda: attached(nvim) == 1, message="never attached")
        result = self.request(nvim, "class Calculator", 6)
        assert result["kind"] == "test"
        assert any(loc["uri"].endswith("CalculatorTest.kt") for loc in result["locations"]), result

    def test_going_there_lands_in_the_test_file(self, nvim):
        nvim.command(f"edit {CALC}")
        wait_until(lambda: attached(nvim) == 1, message="never attached")
        result = self.request(nvim, "class Calculator", 6)
        (loc,) = [l for l in result["locations"] if l["uri"].endswith("CalculatorTest.kt")]
        nvim.exec_lua("vim.lsp.util.show_document(..., 'utf-16', { focus = true })", loc)
        wait_until(lambda: nvim.eval("expand('%:t')") == "CalculatorTest.kt", timeout=15,
                   message="never navigated to the test file")

    def test_the_command_goes_there_with_one_result(self, nvim):
        nvim.command(f"edit {CALC}")
        wait_until(lambda: attached(nvim) == 1, message="never attached")
        line, col = at("\n".join(nvim.current.buffer[:]), "class Calculator", 0, 6)
        nvim.current.window.cursor = (line + 1, col)
        nvim.command("IjBridge test")
        wait_until(lambda: nvim.eval("expand('%:t')") == "CalculatorTest.kt", timeout=30,
                   message="the command never navigated to the test file")

    def test_the_key_does_the_same(self, nvim):
        nvim.command(f"edit {MULT}")
        wait_until(lambda: attached(nvim) == 1, message="never attached")
        line, col = at("\n".join(nvim.current.buffer[:]), "class Multiplier", 0, 6)
        nvim.current.window.cursor = (line + 1, col)
        wait_until(lambda: nvim.exec_lua("""
                for _, m in ipairs(vim.api.nvim_buf_get_keymap(0, 'n')) do
                  if m.lhs:sub(-2) == 'tg' then return true end
                end
                return false"""), timeout=30, message="<leader>tg never appeared for this buffer")
        nvim.feedkeys(nvim.replace_termcodes("<Space>tg"), "m", False)
        wait_until(lambda: nvim.eval("expand('%:t')") == "MultiplierTest.java", timeout=30,
                   message="the key never navigated to the test file")

    def test_the_key_offers_to_create_one_when_there_is_none(self, nvim, bridge_container):
        new_path = f"{SRC}/probe/NoTestYet.kt".replace("/main/", "/test/").replace("NoTestYet.kt", "NoTestYetTest.kt")
        bridge_container.exec(f"rm -f {new_path}", check=False)
        try:
            nvim.command(f"edit {NO_TEST}")
            wait_until(lambda: attached(nvim) == 1, message="never attached")
            line, col = at("\n".join(nvim.current.buffer[:]), "class NoTestYet", 0, 6)
            nvim.current.window.cursor = (line + 1, col)
            nvim.exec_lua("""
                _G.__prompt = nil
                vim.ui.select = function(items, opts, on_choice) _G.__prompt = opts.prompt; on_choice('Yes') end""")
            wait_until(lambda: nvim.exec_lua("""
                for _, m in ipairs(vim.api.nvim_buf_get_keymap(0, 'n')) do
                  if m.lhs:sub(-2) == 'tg' then return true end
                end
                return false"""), timeout=30)
            nvim.command("IjBridge test")
            try:
                wait_until(lambda: nvim.eval("expand('%:t')") == "NoTestYetTest.kt", timeout=30)
            except AssertionError:
                raise AssertionError("never created and opened the new test file: prompt="
                    + repr(nvim.exec_lua("return _G.__prompt")) + " buffer=" + nvim.eval("expand('%:t')")
                    + " messages=" + nvim.command_output("messages")) from None
            assert "NoTestYetTest" in nvim.exec_lua("return _G.__prompt")
            wait_until(lambda: bridge_container.exec(f"test -f {new_path} && echo y || echo n").stdout.strip() == "y",
                       timeout=15, message="the file was never written to disk")
        finally:
            nvim.exec_lua("vim.ui.select = nil; package.loaded['vim.ui'] = nil")
            nvim.command("silent! %bwipeout!")
            bridge_container.exec(f"rm -f {new_path}", check=False)
