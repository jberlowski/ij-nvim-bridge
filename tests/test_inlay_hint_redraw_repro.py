"""Exploratory reproduction of the photographed inlay-hint decoration error.

Uses real Bridge responses; never changes Neovim's private hint cache.
Java is available in the free-tier harness; the report used TypeScript.
"""
import time

from harness.util import wait_until
from test_inlay_hints import JAVA, TestThroughNeovimsBuiltins as InlayHelpers


def test_rapid_shortening_and_redraw(nvim):
    helper = InlayHelpers()
    helper.enable(nvim)
    wait_until(lambda: len(helper.shown(nvim)) == 3, timeout=60,
               message="no real Bridge hints available for reproduction")
    nvim.command("messages clear")
    nvim.exec_lua("vim.v.errmsg = ''")
    line = next(i for i, text in enumerate(JAVA.splitlines()) if "double a = area" in text)
    original = JAVA.splitlines()[line]
    for cycle in range(100):
        # Let genuine refreshes race with shortening a formerly hinted line.
        nvim.current.buffer[line] = original
        nvim.command("redraw!")
        time.sleep((cycle % 5) * 0.025)
        nvim.current.buffer[line] = "        ;"
        nvim.command("redraw!")
        nvim.command("normal! G")
        nvim.command("redraw!")
        nvim.command("normal! gg")
        messages = nvim.command_output("messages")
        assert "Invalid 'col': out of range" not in messages, messages
        assert "nvim.lsp.inlayhint" not in messages, messages
    nvim.current.buffer[line] = original
    wait_until(lambda: len(helper.shown(nvim)) == 3, timeout=60)
    nvim.command("redraw!")
    messages = nvim.command_output("messages")
    assert "Invalid 'col': out of range" not in messages, messages
    assert "nvim.lsp.inlayhint" not in messages, messages
    print("100 rapid shortening/restore cycles: no inlay-hint decoration error")
    print(nvim.command_output("version").splitlines()[0])


def test_single_newline_with_many_hints(nvim):
    """Insert one Enter per settled scenario, without explicit redraw commands."""
    helper = InlayHelpers()
    calls = [f"        double result{i} = area({i + 1}, {i + 2}, 1.5);"
             for i in range(400)]
    lines = JAVA.splitlines()[:5] + calls + ["    }", "}"]
    expected = 3 * len(calls)

    def errors():
        return nvim.command_output("messages") + "\n" + nvim.eval("v:errmsg")

    def check_errors():
        messages = errors()
        assert "Invalid 'col': out of range" not in messages, messages
        assert "nvim.lsp.inlayhint" not in messages, messages

    helper.enable(nvim)
    for name, row, col in [
        ("before first call", 5, 0),
        ("before middle call", 205, 0),
        ("inside middle call before arguments", 205, calls[200].index("(") + 1),
        ("inside middle call after first argument", 205, calls[200].index(",")),
        ("after final call", 404, len(calls[-1]) - 1),
    ]:
        nvim.current.buffer[:] = lines
        wait_until(lambda: len(helper.shown(nvim)) == expected, timeout=120,
                   message=f"expected {expected} genuine Bridge hints")
        # Start at a settled, visible hinted line; Enter is the only edit.
        nvim.current.window.cursor = (row + 1, col)
        time.sleep(1)
        nvim.command("messages clear")
        nvim.exec_lua("vim.v.errmsg = ''")
        before = nvim.current.buffer[:]
        nvim.input(nvim.replace_termcodes("i<CR><Esc>"))
        wait_until(lambda: len(nvim.current.buffer) == len(before) + 1,
                   timeout=5, message="Enter did not insert one newline")
        for _ in range(20):
            time.sleep(0.1)
            check_errors()
        wait_until(lambda: len(helper.shown(nvim)) == expected, timeout=120,
                   message=f"hints did not return after Enter: {name}")
        check_errors()
        print(f"{name}: one Enter, {expected} hints, no decoration error", flush=True)
    print(nvim.command_output("version").splitlines()[0], flush=True)
