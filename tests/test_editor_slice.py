"""The second Bridge slice: the Neovim plugin, driven for real.

A real Neovim (LazyVim, blink.cmp) runs the Editor plugin against the real Brain
in the same container. Nothing here fakes the Editor: buffers are edited, files
are written with `:w`, completion is requested through blink.cmp's source API.
The Brain's side is observed through a passive second Session (`probe`).
"""
from __future__ import annotations

import json
import time
import statistics

import pytest

from harness.util import wait_until
from harness.wire import SRC, uri

PRODUCER = f"{SRC}/probe/CrossFileProducer.kt"
CONSUMER = f"{SRC}/probe/CrossFileConsumer.kt"
DORMANT = "/tmp/not-in-any-project.txt"
INSERTION = "// HARNESS-INSERTION-POINT"
CONSUMER_MARKER = "// A test appends a call to the unsaved method here and asserts it resolves."

OVERHEAD_BUDGET_MS = 15.0    # SPEC.md §7, now measured with the Editor in the loop


def mirrors(probe) -> dict[str, dict]:
    return {m["uri"].rsplit("/", 1)[1]: m for m in probe.debug_state(text=True)["mirrors"]}


def buffer_text(nvim) -> str:
    return "\n".join(nvim.current.buffer[:]) + "\n"


def attached(nvim) -> int:
    return nvim.exec_lua("return #vim.lsp.get_clients({bufnr = 0, name = 'ij-bridge'})")


# Drive the blink source exactly as blink.cmp would, and wait for the whole Stream.
COMPLETE = """
local want = ...
local buf = vim.api.nvim_get_current_buf()
local source = require('ij_bridge.blink').new()
-- Insert mode is where blink.cmp asks, and there the cursor sits *past* the last
-- character; normal mode clamps it onto it, which would ask about the wrong spot.
local row = vim.api.nvim_win_get_cursor(0)[1]
local line = vim.api.nvim_get_current_line()
local col = #line
local ctx = { bufnr = buf, cursor = { row, col }, line = line }
local seen, calls, first = {}, 0, nil
local count = 0
source:get_completions(ctx, function(r)
  calls = calls + 1
  for _, item in ipairs(r.items) do
    count = count + 1
    seen[item.label] = true
    first = first or item.label
  end
end)
-- A Stream is over when the source holds none open, not when blink is told
-- "incomplete": a Stream cut off at the Cap is incomplete *and* finished.
vim.wait(30000, function() return calls > 0 and require('ij_bridge.blink').active() == 0 end, 5)
local closed = require('ij_bridge.blink').active() == 0
local found = {}
for _, label in ipairs(want) do found[label] = seen[label] == true end
return { calls = calls, count = count, first = first, closed = closed, found = found,
         last = require('ij_bridge.blink').last, at = { row, col }, line = ctx.line }
"""


def complete(nvim, *want: str) -> dict:
    return nvim.exec_lua(COMPLETE, list(want))


def go_to_end(nvim) -> None:
    nvim.command("normal! G$")


# ---------------------------------------------------------------- discovery
class TestDiscovery:

    def test_a_file_outside_every_project_is_dormant(self, nvim, bridge_container):
        """SPEC.md §3: Dormant is normal, and invisible."""
        bridge_container.write_file(DORMANT, "nothing to see\n")
        nvim.command(f"edit {DORMANT}")
        assert attached(nvim) == 0
        assert nvim.exec_lua("return vim.lsp.get_clients({name = 'ij-bridge'})[1]") is None
        assert nvim.eval("v:errmsg") == ""

    def test_a_file_in_a_project_root_attaches(self, nvim, probe):
        nvim.command(f"edit {PRODUCER}")
        wait_until(lambda: attached(nvim) == 1, message="the buffer never attached")
        wait_until(lambda: "CrossFileProducer.kt" in mirrors(probe),
                   message="the Brain never got a Mirror for this buffer")

    def test_the_ij_command_says_which(self, nvim):
        nvim.command(f"edit {PRODUCER}")
        wait_until(lambda: attached(nvim) == 1)
        out = nvim.exec_lua("return vim.api.nvim_exec2('IjBridge', {output = true}).output")
        assert "attached to /work/fixture" in out, out


# ------------------------------------------------------------------ mirrors
class TestMirroring:

    def test_what_is_typed_reaches_the_mirror(self, nvim, probe):
        nvim.command(f"edit {PRODUCER}")
        wait_until(lambda: "CrossFileProducer.kt" in mirrors(probe))
        nvim.current.buffer.append("// typed in nvim", 0)
        wait_until(lambda: mirrors(probe)["CrossFileProducer.kt"]["text"] == buffer_text(nvim),
                   message="the Mirror never caught up with the buffer")

    def test_leaving_a_clean_buffer_releases_its_mirror(self, nvim, probe):
        nvim.command(f"edit {PRODUCER}")
        wait_until(lambda: "CrossFileProducer.kt" in mirrors(probe))
        nvim.command(f"edit {CONSUMER}")
        wait_until(lambda: list(mirrors(probe)) == ["CrossFileConsumer.kt"],
                   message="the clean buffer's Mirror was not released")

    def test_leaving_an_unsaved_buffer_keeps_its_mirror(self, nvim, probe):
        """SPEC.md §5.2: the Mirror Set is the active buffer plus every buffer
        with unsaved changes."""
        nvim.command(f"edit {PRODUCER}")
        nvim.current.buffer.append("// unsaved", 0)
        nvim.command(f"hide edit {CONSUMER}")
        wait_until(lambda: sorted(mirrors(probe)) == ["CrossFileConsumer.kt", "CrossFileProducer.kt"],
                   message="the unsaved buffer lost its Mirror")

    def test_unsaved_method_in_one_buffer_completes_in_another(
            self, nvim, probe, bridge_container):
        """The failure the Mirror Set rule exists for, end to end through real
        Neovim: add a method to Foo, do not save, switch to Bar. From disk the
        Brain would answer "cannot resolve"."""
        nvim.command(f"edit {PRODUCER}")
        text = bridge_container.read_file(PRODUCER)
        nvim.current.buffer[:] = text.replace(
            INSERTION, "fun brandNewUnsavedMethod(): Int = 42").rstrip("\n").split("\n")

        nvim.command(f"hide edit {CONSUMER}")
        consumer = bridge_container.read_file(CONSUMER)
        nvim.current.buffer[:] = consumer.replace(
            CONSUMER_MARKER, "fun probe() = producer.").rstrip("\n").split("\n")
        row = next(i for i, l in enumerate(nvim.current.buffer[:]) if "producer." in l and "fun probe" in l)
        nvim.current.window.cursor = (row + 1, len(nvim.current.buffer[row]))

        wait_until(lambda: "brandNewUnsavedMethod" in mirrors(probe)["CrossFileProducer.kt"]["text"])
        result = complete(nvim, "brandNewUnsavedMethod", "existingMethod")
        brain_view = mirrors(probe)["CrossFileConsumer.kt"]["text"].split("\n")
        assert result["found"]["brandNewUnsavedMethod"], (result, brain_view[-6:])
        assert result["found"]["existingMethod"], result


# -------------------------------------------------------------- diagnostics
RESOLUTION = f"{SRC}/probe/ResolutionError.kt"


def diagnostics(nvim) -> list[dict]:
    return nvim.exec_lua("return vim.diagnostic.get(0)")


class TestDiagnostics:
    """Through Neovim's own diagnostic machinery: `vim.diagnostic`, the float,
    `]d`. Nothing here is Bridge-specific on the Neovim side."""

    def test_cannot_resolve_reaches_vim_diagnostic(self, nvim):
        nvim.command(f"edit {RESOLUTION}")
        found = wait_until(
            lambda: [d for d in diagnostics(nvim) if "Unresolved reference" in d["message"]],
            timeout=40, message="no 'Unresolved reference' in vim.diagnostic")
        assert len(found) >= 2
        assert all(d["source"] == "IntelliJ" for d in found)
        assert all(d["severity"] == 1 for d in found)                 # vim.diagnostic.severity.ERROR

    def test_an_unsaved_error_is_flagged_and_then_cleared(self, nvim, bridge_container):
        nvim.command(f"edit {CONSUMER}")
        wait_until(lambda: attached(nvim) == 1)
        lines = bridge_container.read_file(CONSUMER).rstrip("\n").split("\n")
        broken = lines[:-1] + ["    fun broken() = stillNotDefined()"] + lines[-1:]
        nvim.current.buffer[:] = broken
        wait_until(lambda: any("stillNotDefined" in d["message"] for d in diagnostics(nvim)),
                   timeout=40, message="the error typed into the buffer was never flagged")
        nvim.current.buffer[:] = lines
        wait_until(lambda: not any("stillNotDefined" in d["message"] for d in diagnostics(nvim)),
                   timeout=40, message="the fixed error was never cleared")

    def test_switching_back_selects_the_mirror(self, nvim, probe):
        """$/ij/focus: a buffer that stayed Mirrored while hidden must become the
        IDE's selected tab again when the developer returns to it, or the daemon
        stops analysing it."""
        nvim.command(f"edit {CONSUMER}")
        nvim.current.buffer.append("// unsaved", 0)                  # keeps it Mirrored
        nvim.command(f"hide edit {PRODUCER}")
        # Opening another buffer selects *its* tab: the unsaved one is Mirrored but hidden.
        wait_until(lambda: "CrossFileProducer.kt" in mirrors(probe) and
                   not mirrors(probe)["CrossFileConsumer.kt"]["showing"],
                   message="the unsaved buffer's Mirror never went into the background")
        nvim.command(f"buffer {CONSUMER}")
        wait_until(lambda: mirrors(probe)["CrossFileConsumer.kt"]["showing"],
                   message="returning to the buffer did not select its Mirror")


# ------------------------------------------------------------------- state
class TestStatus:

    def test_the_status_line_is_empty_when_dormant(self, nvim, bridge_container):
        bridge_container.write_file(DORMANT, "nothing to see\n")
        nvim.command(f"edit {DORMANT}")
        assert nvim.exec_lua("return require('ij_bridge').statusline()") == ""

    def test_the_status_line_shows_indexing_and_recovers(self, nvim, probe):
        nvim.command(f"edit {PRODUCER}")
        wait_until(lambda: nvim.exec_lua("return require('ij_bridge').statusline()") == "IJ",
                   message="the status line never showed a ready Brain")

        probe.request("$/ij/debug/indexing", {"ms": 5000})
        wait_until(lambda: nvim.exec_lua("return require('ij_bridge').statusline()") == "IJ: indexing",
                   message="Indexing was never shown")
        out = nvim.exec_lua("return vim.api.nvim_exec2('IjBridge', {output = true}).output")
        assert "indexing" in out, out

        wait_until(lambda: nvim.exec_lua("return require('ij_bridge').statusline()") == "IJ",
                   timeout=30, message="the status line never returned to ready")


# --------------------------------------------------------------- the write
class TestWriting:

    @pytest.mark.parametrize("backupcopy", ["auto", "yes", "no"])
    def test_a_real_write_neither_blocks_nor_diverges(
            self, nvim, probe, bridge_container, backupcopy):
        """ADR-0003's open question, answered with Neovim's own `:w`.

        `backupcopy=no` writes a new file and renames it over the old one, which
        is Neovim's default behaviour for most files; `yes` rewrites in place.
        Either way IntelliJ sees the file change under an unsaved Mirror."""
        original = bridge_container.read_file(PRODUCER)
        acks = probe.debug_state()["saveAcks"]
        try:
            nvim.command(f"set backupcopy={backupcopy}")
            nvim.command(f"edit {PRODUCER}")
            wait_until(lambda: "CrossFileProducer.kt" in mirrors(probe))
            nvim.current.buffer.append("// saved by nvim", 0)
            wait_until(lambda: mirrors(probe)["CrossFileProducer.kt"]["text"] == buffer_text(nvim))
            expected = buffer_text(nvim)

            nvim.command("write")

            assert bridge_container.read_file(PRODUCER) == expected, "the Editor's write is the write"
            # The handshake ran: the Brain acknowledged before the bytes hit disk.
            wait_until(lambda: probe.debug_state()["saveAcks"] > acks,
                       message="Neovim never ran the save handshake")
            # And the IDE is still alive, well after the file watcher has reacted.
            import time
            time.sleep(3)
            probe.timeout = 10
            m = mirrors(probe)["CrossFileProducer.kt"]
            assert m["text"] == expected and m["convergent"] is True
        finally:
            nvim.command("set backupcopy&")
            nvim.command("silent! %bwipeout!")
            bridge_container.write_file(PRODUCER, original.rstrip("\n"))

    def test_write_still_works_when_the_brain_is_gone(self, nvim, bridge_container):
        """SPEC.md §3 / §5.4: `:w` must never fail or block because of the Bridge."""
        target = "/work/fixture/src/main/kotlin/dev/bridge/fixture/probe/InspectionWarning.kt"
        original = bridge_container.read_file(target)
        try:
            nvim.command(f"edit {target}")
            wait_until(lambda: attached(nvim) == 1)
            # Cut the Session without telling nvim's buffer.
            nvim.exec_lua("for _, c in ipairs(vim.lsp.get_clients({name='ij-bridge'})) do c:stop(true) end")
            nvim.current.buffer.append("// written without a Brain", 0)
            nvim.command("write")
            assert "// written without a Brain" in bridge_container.read_file(target)
        finally:
            nvim.command("silent! %bwipeout!")
            bridge_container.write_file(target, original.rstrip("\n"))


# --------------------------------------------------------------- completion
def large_surface(nvim, bridge_container) -> None:
    nvim.command(f"edit {CONSUMER}")
    wait_until(lambda: attached(nvim) == 1)
    text = bridge_container.read_file(CONSUMER).replace(
        CONSUMER_MARKER, "val probe = LargeSurface().compute")
    lines = text.rstrip("\n").split("\n")
    nvim.current.buffer[:] = lines
    row = next(i for i, l in enumerate(lines) if "LargeSurface().compute" in l)
    nvim.current.window.cursor = (row + 1, len(lines[row]))


class TestCompletion:
    """Editor -> Brain -> IntelliJ -> blink.cmp, in the state the Bridge lives in:
    Neovim has the focus and IntelliJ is in the background."""

    def test_the_source_streams_intellijs_items_to_blink(self, nvim, bridge_container):
        large_surface(nvim, bridge_container)
        complete(nvim)                    # cold start: IntelliJ's first completion is slow
        result = complete(nvim, "computeMetricNumber000", "computeMetricNumber399")
        assert result["closed"], result
        assert result["count"] >= 300
        assert result["first"] == "computeMetricNumber000"     # IntelliJ's own order
        assert result["found"]["computeMetricNumber000"]
        assert result["calls"] >= 1

    def test_blink_shows_the_menu_for_typed_text(self, nvim, bridge_container, bridge_display):
        """The visual half of observability: does blink's popup render here?
        (HARNESS.md §14.) The screenshot is kept either way."""
        from pathlib import Path
        nvim.command(f"edit {CONSUMER}")
        wait_until(lambda: attached(nvim) == 1)
        nvim.command("normal! G")
        nvim.feedkeys(nvim.replace_termcodes("Oval probe = LargeSurface().compu"), "n", False)
        shown = wait_until(
            lambda: nvim.exec_lua("return require('blink.cmp').is_menu_visible()"),
            timeout=20, message="blink.cmp never showed its menu")

        def labels_now():
            return nvim.exec_lua("""
                local items = require('blink.cmp.completion.list').items or {}
                local out = {}
                for i = 1, math.min(#items, 8) do out[i] = items[i].label end
                return out""")
        # blink shows the menu as soon as any source has answered; ours streams in.
        try:
            wait_until(lambda: any(str(l).startswith("computeMetricNumber") for l in labels_now()),
                       timeout=20, message="IntelliJ's items never reached blink")
        except AssertionError:
            pass                                   # the screenshot below is the evidence
        labels = labels_now()
        artifacts = Path(__file__).parent / "artifacts"
        artifacts.mkdir(exist_ok=True)
        bridge_display.screenshot(artifacts / "blink_menu.png")
        assert shown
        assert any(str(l).startswith("computeMetricNumber") for l in labels), (
            labels, nvim.exec_lua("return require('ij_bridge.blink').last"))


def probe_line(nvim, suffix: str) -> None:
    """Put `LargeSurface().<suffix>` on the probe line and the cursor after it."""
    lines = nvim.current.buffer[:]
    row = next(i for i, l in enumerate(lines) if "val probe = LargeSurface()." in l)
    lines[row] = "    val probe = LargeSurface()." + suffix
    nvim.current.buffer[:] = lines
    nvim.current.window.cursor = (row + 1, len(lines[row]))


def stats(nvim) -> dict:
    return nvim.exec_lua("return require('ij_bridge.blink').stats")


class TestIncrementalCompletion:
    """SPEC.md §12 Next: another character asks IntelliJ again while the menu
    keeps showing what it has, and backspacing is answered from a small cache."""

    def test_identical_state_is_answered_from_the_cache(self, nvim, probe, bridge_container):
        large_surface(nvim, bridge_container)
        complete(nvim)                                   # cold, and fills the cache
        asked = probe.debug_state()["completionRequests"]
        again = complete(nvim, "computeMetricNumber000")

        assert probe.debug_state()["completionRequests"] == asked, "a cache hit must not ask IntelliJ"
        assert again["found"]["computeMetricNumber000"] and again["count"] >= 300
        assert stats(nvim)["hits"] >= 1

    def test_backspacing_returns_to_an_answered_state_instantly(self, nvim, probe, bridge_container):
        large_surface(nvim, bridge_container)
        probe_line(nvim, "comp")
        first = complete(nvim)
        probe_line(nvim, "compu")
        complete(nvim)
        asked = probe.debug_state()["completionRequests"]

        probe_line(nvim, "comp")                         # backspace
        back = complete(nvim, "computeMetricNumber000")

        assert probe.debug_state()["completionRequests"] == asked
        assert back["count"] == first["count"], "the earlier answer, whole, not a subset"
        assert back["found"]["computeMetricNumber000"]

    def test_a_new_prefix_asks_again(self, nvim, probe, bridge_container):
        large_surface(nvim, bridge_container)
        probe_line(nvim, "comp")
        complete(nvim)
        asked = probe.debug_state()["completionRequests"]
        probe_line(nvim, "compu")
        complete(nvim)
        assert probe.debug_state()["completionRequests"] == asked + 1

    def test_an_edit_in_another_buffer_invalidates_the_cache(self, nvim, probe, bridge_container):
        """IntelliJ's answer depends on every Mirrored buffer, not only this one."""
        nvim.command(f"edit {PRODUCER}")
        wait_until(lambda: attached(nvim) == 1)
        nvim.current.buffer.append("// unsaved", 0)            # stays Mirrored while hidden
        nvim.command(f"hide edit {CONSUMER}")
        large_surface(nvim, bridge_container)
        complete(nvim)
        complete(nvim)                                        # a hit
        asked = probe.debug_state()["completionRequests"]

        nvim.command(f"buffer {PRODUCER}")
        nvim.current.buffer.append("// edited again", 0)
        nvim.command(f"buffer {CONSUMER}")
        probe_line(nvim, "compute")
        complete(nvim)
        assert probe.debug_state()["completionRequests"] == asked + 1, "served a stale answer"

    def test_a_cached_answer_expires_after_five_seconds(self, nvim, probe, bridge_container):
        large_surface(nvim, bridge_container)
        probe_line(nvim, "comp")
        complete(nvim)
        asked = probe.debug_state()["completionRequests"]
        complete(nvim)                                            # inside the window: a hit
        assert probe.debug_state()["completionRequests"] == asked
        time.sleep(5.5)
        complete(nvim)                                            # past it: asks again
        assert probe.debug_state()["completionRequests"] == asked + 1

    def _typed_past_a_slow_request(self, nvim, probe, delay_ms=2000):
        """The developer types `comp`, then `u` while IntelliJ is still working on
        `comp`. Returns what the newer request's callback saw, and when."""
        probe.request("$/ij/debug/completionDelay", {"ms": delay_ms})
        try:
            return nvim.exec_lua("""
                local blink = require('ij_bridge.blink')
                local source = blink.new()
                local buf = vim.api.nvim_get_current_buf()
                local lines = vim.api.nvim_buf_get_lines(buf, 0, -1, false)
                local row
                for i, l in ipairs(lines) do if l:find('val probe = LargeSurface%(%)%.') then row = i end end
                local function ctx(suffix)
                  local line = '    val probe = LargeSurface().' .. suffix
                  vim.api.nvim_buf_set_lines(buf, row - 1, row, false, { line })
                  return { bufnr = buf, cursor = { row, #line }, line = line }
                end
                local a = ctx('comp')
                local cancel_a = source:get_completions(a, function() end)
                vim.wait(80)
                local b = ctx('compu')
                cancel_a()                     -- what blink.cmp does when a new list replaces the old
                local t0 = vim.uv.hrtime()
                local calls, labels, dupes = {}, {}, 0
                source:get_completions(b, function(r)
                  table.insert(calls, { t = (vim.uv.hrtime() - t0) / 1e9, n = #r.items })
                  for _, it in ipairs(r.items) do
                    if labels[it.label] then dupes = dupes + 1 end
                    labels[it.label] = true
                  end
                end)
                vim.wait(20000, function() return #calls >= 2 end, 10)
                return { calls = calls, dupes = dupes, stats = blink.stats }""")
        finally:
            probe.request("$/ij/debug/completionDelay", {"ms": 0})

    def test_an_answer_that_arrives_late_is_shown_to_the_newer_request(
            self, nvim, probe, bridge_container):
        """The case: `abc.xy` is still being worked on when `z` is typed. Its
        answer is right for `abc.xy`; it must not be thrown away. It is shown for
        `abc.xyz` while that is awaited, and nothing is shown twice when the newer
        answer follows."""
        large_surface(nvim, bridge_container)
        complete(nvim)                                            # warm IntelliJ up first
        nvim.exec_lua("require('ij_bridge.blink').clear_cache()")
        r = self._typed_past_a_slow_request(nvim, probe)
        first, own = r["calls"][0], r["calls"][1]
        assert first["n"] > 0, "no interim result was shown"
        assert own["t"] - first["t"] > 1.0, (first, own, "the interim came no earlier than the real answer")
        assert r["stats"]["interim"] >= 1
        assert r["dupes"] == 0, "an item was shown twice"

    def test_that_late_answer_is_cached_for_a_backspace(self, nvim, probe, bridge_container):
        large_surface(nvim, bridge_container)
        complete(nvim)
        nvim.exec_lua("require('ij_bridge.blink').clear_cache()")
        self._typed_past_a_slow_request(nvim, probe)
        assert stats(nvim)["late"] >= 1
        asked = probe.debug_state()["completionRequests"]
        probe_line(nvim, "comp")                                  # backspace to what the late answer covers
        back = complete(nvim, "computeMetricNumber000")
        assert probe.debug_state()["completionRequests"] == asked, "the late answer was not cached"
        assert back["found"]["computeMetricNumber000"]

    def test_one_more_character_asks_again_and_the_menu_keeps_its_items(
            self, nvim, probe, bridge_container):
        """Through real blink.cmp. With IntelliJ artificially slow, type another
        character: the request must go out, and the menu must keep showing the
        previous items (filtered) rather than blank while it waits."""
        nvim.command(f"edit {CONSUMER}")
        wait_until(lambda: attached(nvim) == 1)
        nvim.command("normal! G")
        nvim.feedkeys(nvim.replace_termcodes("Oval probe = LargeSurface().comp"), "n", False)
        wait_until(lambda: nvim.exec_lua("return require('blink.cmp').is_menu_visible()"),
                   timeout=30, message="the first menu never appeared")
        wait_until(lambda: nvim.exec_lua("return #(require('blink.cmp.completion.list').items or {})") > 0)
        asked = probe.debug_state()["completionRequests"]

        probe.request("$/ij/debug/completionDelay", {"ms": 3000})
        try:
            nvim.feedkeys("u", "n", False)                    # "compu": one more character
            wait_until(lambda: probe.debug_state()["completionRequests"] > asked,
                       timeout=10, message="typing another character did not ask IntelliJ again")
            # The new answer is 3 s away. What does the menu show meanwhile?
            visible = nvim.exec_lua("return require('blink.cmp').is_menu_visible()")
            shown = nvim.exec_lua("return #(require('blink.cmp.completion.list').items or {})")
            assert visible and shown > 0, (visible, shown)
        finally:
            probe.request("$/ij/debug/completionDelay", {"ms": 0})


class TestCompletionInTheBackground:
    """The condition the Bridge actually runs in."""

    def test_completion_works_with_the_ide_in_the_background(
            self, nvim, probe, bridge_container, bridge_display):
        """The default CodeCompletionHandlerBase returns nothing while IntelliJ is
        not the active application; hooking its completionFinished event, as
        Comrade does, does not (docs/adr/0008, amendment)."""
        bridge_display.focus("nvim-harness")
        # Guard against passing vacuously: the bug only exists in this state.
        assert probe.debug_state()["appActive"] is False, "IntelliJ was the active application"
        large_surface(nvim, bridge_container)
        result = complete(nvim, "computeMetricNumber000")
        assert result["count"] >= 300, result
        assert probe.debug_state()["appActive"] is False


# ------------------------------------------------------------------ overhead
class TestEditorOverhead:
    """SPEC.md §7 with Neovim in the loop and IntelliJ in the background: the
    request leaves nvim, the Brain answers, and blink.cmp has been handed the items."""

    def test_overhead_with_the_editor_inside_the_budget(self, nvim, bridge_container):
        large_surface(nvim, bridge_container)
        samples = nvim.exec_lua("""
            local source = require('ij_bridge.blink').new()
            local blink = require('ij_bridge.blink')
            local out = {}
            for i = 1, 35 do
              local row = vim.api.nvim_win_get_cursor(0)[1]
              local line = vim.api.nvim_get_current_line()
              local ctx = { bufnr = vim.api.nvim_get_current_buf(), cursor = { row, #line },
                            line = line }
              -- Measure IntelliJ, not the answer cache: a hit never reaches the Brain.
              blink.clear_cache()
              blink.last = nil
              source:get_completions(ctx, function() end)
              vim.wait(30000, function() return blink.active() == 0 and blink.last ~= nil end, 1)
              if i > 5 then table.insert(out, blink.last) end   -- five warm-up runs
              vim.wait(150)
            end
            return out""")
        assert len(samples) == 30

        overhead = [(s["to_delivered_ns"] - s["ij_first_items_ns"]) / 1e6 for s in samples]
        ij = [s["ij_first_items_ns"] / 1e6 for s in samples]
        p95 = lambda v: sorted(v)[int(len(v) * 0.95) - 1]
        report = {
            "items": samples[-1]["items"],
            "ij_time_ms": {"median": statistics.median(ij), "p95": p95(ij)},
            "overhead_ms": {"median": statistics.median(overhead), "p95": p95(overhead)},
            "budget_ms": OVERHEAD_BUDGET_MS,
        }
        print("\n  editor-in-loop " + json.dumps(report))
        assert all(o > 0 for o in overhead)
        assert p95(overhead) < OVERHEAD_BUDGET_MS, report
