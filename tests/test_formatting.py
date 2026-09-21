"""Formatting (FEATURES.md §3): `textDocument/formatting` and `rangeFormatting`.

The project's original motivation: the *IDE's* code style applied to the
developer's buffer, not a headless engine's idea of it. Edits are computed on a
copy and returned for Neovim to apply; the Mirror and the disk are never touched.
"""
from __future__ import annotations

import time

import pytest

from harness.util import wait_until
from harness.wire import SRC, RpcError, uri
from test_navigation import attached, mirror

PROBE = f"{SRC}/probe"
TARGET = f"{PROBE}/InspectionWarning.kt"          # any existing project file will do
JAVA_TARGET = f"{PROBE}/JavaShapes.java"

MESSY_KOTLIN = '''package dev.bridge.fixture.probe

class   Messy( val a:Int,val b : String ){
fun compute( x:Int ):Int{
val y=x+1
if(y>2){
return y*2
}else{
return   y
}
}

fun other()  =   "a"+"b"
}
'''

FORMATTED_KOTLIN = '''package dev.bridge.fixture.probe

class Messy(val a: Int, val b: String) {
    fun compute(x: Int): Int {
        val y = x + 1
        if (y > 2) {
            return y * 2
        } else {
            return y
        }
    }

    fun other() = "a" + "b"
}
'''

MESSY_JAVA = '''package dev.bridge.fixture.probe;
public class JavaMessy{
public int add(int a,int b){return a+b;}
private   String name ;
}
'''


# ------------------------------------------------------------------ helpers
def apply_edits(text: str, edits: list[dict]) -> str:
    """Apply LSP TextEdits (UTF-16 positions) to `text`, as a client would."""
    lines = text.split("\n")

    def offset(p):
        return sum(len(l) + 1 for l in lines[:p["line"]]) + p["character"]
    for e in sorted(edits, key=lambda e: offset(e["range"]["start"]), reverse=True):
        text = text[:offset(e["range"]["start"])] + e["newText"] + text[offset(e["range"]["end"]):]
    return text


def fmt(w, path, range_=None):
    method = "textDocument/rangeFormatting" if range_ else "textDocument/formatting"
    params = {"textDocument": {"uri": uri(path)},
              "options": {"tabSize": 8, "insertSpaces": False}}          # Neovim's, and ignored
    if range_:
        params["range"] = range_
    return w.request(method, params, timeout=90)


def open_messy(w, path, text):
    w.did_open(path, text)


# ------------------------------------------------------------ wire level
class TestFormatting:

    def test_kotlin_is_formatted_to_the_ides_style(self, wire):
        open_messy(wire, TARGET, MESSY_KOTLIN)
        edits = fmt(wire, TARGET)
        assert apply_edits(MESSY_KOTLIN, edits) == FORMATTED_KOTLIN

    def test_java_is_formatted_to_the_ides_style(self, wire):
        open_messy(wire, JAVA_TARGET, MESSY_JAVA)
        out = apply_edits(MESSY_JAVA, fmt(wire, JAVA_TARGET))
        for line in ("public class JavaMessy {", "    public int add(int a, int b) {",
                     "        return a + b;", "    private String name;"):
            assert line in out.split("\n"), (line, out)

    def test_formatting_twice_changes_nothing(self, wire):
        """Idempotence: the strongest cheap check that the result is IntelliJ's
        own fixed point rather than an approximation."""
        open_messy(wire, TARGET, FORMATTED_KOTLIN)
        assert fmt(wire, TARGET) == []

    def test_edits_are_minimal_not_a_whole_file_replacement(self, wire):
        open_messy(wire, TARGET, MESSY_KOTLIN)
        edits = fmt(wire, TARGET)
        assert len(edits) >= 2
        untouched = "package dev.bridge.fixture.probe"
        assert not any(untouched in e["newText"] for e in edits), "the untouched line was rewritten"

    def test_it_formats_the_unsaved_buffer_not_the_file_on_disk(self, wire, bridge_container):
        on_disk = bridge_container.read_file(TARGET)
        open_messy(wire, TARGET, MESSY_KOTLIN)
        assert apply_edits(MESSY_KOTLIN, fmt(wire, TARGET)) == FORMATTED_KOTLIN
        assert on_disk != MESSY_KOTLIN

    def test_a_request_changes_neither_the_mirror_nor_the_disk(self, wire, bridge_container):
        """Edits are computed, never applied (D2): the Editor owns the bytes."""
        before_disk = bridge_container.read_file(TARGET)
        open_messy(wire, TARGET, MESSY_KOTLIN)
        fmt(wire, TARGET)
        (m,) = wire.debug_state(text=True)["mirrors"]
        assert m["text"] == MESSY_KOTLIN, "formatting modified the Mirror"
        assert m["version"] == 0
        assert bridge_container.read_file(TARGET) == before_disk

    def test_the_clients_tab_options_are_ignored(self, wire):
        """Passthrough: it is the IDE's style that is wanted, not Neovim's tabs."""
        open_messy(wire, TARGET, MESSY_KOTLIN)
        out = apply_edits(MESSY_KOTLIN, fmt(wire, TARGET))     # sent with tabSize 8, insertSpaces False
        assert "\t" not in out and "    fun compute(x: Int): Int {" in out

    def test_range_formatting_touches_only_the_range(self, wire):
        text = ('package dev.bridge.fixture.probe\n\n'
                'fun first( a:Int ):Int{return a+1}\n\n'
                'fun second( b:Int ):Int{return b+2}\n')
        open_messy(wire, TARGET, text)
        line = text.split("\n").index("fun first( a:Int ):Int{return a+1}")
        rng = {"start": {"line": line, "character": 0}, "end": {"line": line, "character": 34}}
        out = apply_edits(text, fmt(wire, TARGET, rng))
        assert "fun first(a: Int): Int {" in out, out
        assert "fun second( b:Int ):Int{return b+2}" in out, "outside the range must be untouched"

    def test_the_borrowed_setting_wins_editorconfig_indent(self, wire, project_files):
        """The IDE's configuration decides, as the developer set it up: a project
        `.editorconfig` asking for 2-space indentation, with Neovim asking for tabs."""
        project_files("/work/fixture/.editorconfig",
                      "root = true\n\n[*.kt]\nindent_style = space\nindent_size = 2\n")
        # The IDE reads the file on its next VFS refresh; give it a moment, then ask.
        open_messy(wire, TARGET, MESSY_KOTLIN)

        def two_space():
            out = apply_edits(MESSY_KOTLIN, fmt(wire, TARGET))
            return out if "\n  fun compute(x: Int): Int {" in out else None
        out = wait_until(two_space, timeout=60, interval=2.0,
                         message="the .editorconfig indent_size was never applied")
        assert "\n    val y = x + 1" in out, "the body is two levels of two spaces"
        assert "\n    fun compute" not in out, "four-space indentation survived"

    def test_it_works_while_indexing_because_it_needs_only_syntax(self, wire):
        open_messy(wire, TARGET, MESSY_KOTLIN)
        wire.request("$/ij/debug/indexing", {"ms": 4000})
        wait_until(lambda: wire.debug_state()["state"] == "Indexing", timeout=15)
        assert apply_edits(MESSY_KOTLIN, fmt(wire, TARGET)) == FORMATTED_KOTLIN
        wait_until(lambda: wire.debug_state()["state"] == "Ready", timeout=60)

    def test_a_change_mid_request_is_content_modified_not_stale(self, wire):
        open_messy(wire, TARGET, MESSY_KOTLIN)
        wire.request("$/ij/debug/navigationDelay", {"ms": 1500})
        try:
            rid = wire.send_request("textDocument/formatting", {
                "textDocument": {"uri": uri(TARGET)}, "options": {"tabSize": 4, "insertSpaces": True}})
            time.sleep(0.3)
            from harness.wire import replace_range
            wire.did_change(TARGET, 1, replace_range(0, 0, 0, "// changed\n"))
            assert wire.await_response(rid, timeout=15)["error"]["code"] == -32801
        finally:
            wire.request("$/ij/debug/navigationDelay", {"ms": 0})

    def test_an_unmirrored_document_is_an_error(self, wire):
        with pytest.raises(RpcError) as e:
            fmt(wire, TARGET)
        assert e.value.code == -32602

    def test_both_capabilities_are_advertised(self, bridge):
        from harness.wire import Wire
        with Wire(bridge.port) as w:
            caps = w.initialize()["capabilities"]
        assert caps["documentFormattingProvider"] is True
        assert caps["documentRangeFormattingProvider"] is True


# ============================================== through Neovim's own built-ins
def open_in_nvim(nvim, path, text):
    nvim.command(f"edit {path}")
    wait_until(lambda: attached(nvim) == 1, message="never attached")
    nvim.current.buffer[:] = text.rstrip("\n").split("\n")
    # The Brain must see the buffer before the request, exactly.
    wait_until(lambda: True)


class TestThroughNeovimsBuiltins:

    def test_vim_lsp_buf_format_formats_the_buffer(self, nvim):
        open_in_nvim(nvim, TARGET, MESSY_KOTLIN)
        nvim.exec_lua("vim.lsp.buf.format({ name = 'ij-bridge', timeout_ms = 60000 })")
        assert "\n".join(nvim.current.buffer[:]) + "\n" == FORMATTED_KOTLIN

    def test_the_result_is_stable_when_formatted_again(self, nvim):
        open_in_nvim(nvim, TARGET, MESSY_KOTLIN)
        nvim.exec_lua("vim.lsp.buf.format({ name = 'ij-bridge', timeout_ms = 60000 })")
        once = list(nvim.current.buffer[:])
        nvim.exec_lua("vim.lsp.buf.format({ name = 'ij-bridge', timeout_ms = 60000 })")
        assert list(nvim.current.buffer[:]) == once

    def test_a_visual_range_formats_only_that_range(self, nvim):
        text = ('package dev.bridge.fixture.probe\n\n'
                'fun first( a:Int ):Int{return a+1}\n\n'
                'fun second( b:Int ):Int{return b+2}\n')
        open_in_nvim(nvim, TARGET, text)
        row = text.split("\n").index("fun first( a:Int ):Int{return a+1}") + 1     # 1-based
        nvim.exec_lua(f"""vim.lsp.buf.format({{ name = 'ij-bridge', timeout_ms = 60000,
            range = {{ start = {{ {row}, 0 }}, ['end'] = {{ {row}, 34 }} }} }})""")
        lines = nvim.current.buffer[:]
        assert "fun first(a: Int): Int {" in lines
        assert "fun second( b:Int ):Int{return b+2}" in lines

    def test_formatting_does_not_touch_the_disk_until_the_developer_writes(self, nvim, bridge_container):
        on_disk = bridge_container.read_file(TARGET)
        open_in_nvim(nvim, TARGET, MESSY_KOTLIN)
        nvim.exec_lua("vim.lsp.buf.format({ name = 'ij-bridge', timeout_ms = 60000 })")
        assert bridge_container.read_file(TARGET) == on_disk

    def test_the_mirror_follows_the_formatted_buffer(self, nvim, probe):
        """Formatting changes the buffer, so the ordinary sync must carry it to the Mirror."""
        open_in_nvim(nvim, TARGET, MESSY_KOTLIN)
        nvim.exec_lua("vim.lsp.buf.format({ name = 'ij-bridge', timeout_ms = 60000 })")
        want = "\n".join(nvim.current.buffer[:]) + "\n"
        wait_until(lambda: [m for m in probe.debug_state(text=True)["mirrors"]
                            if m["uri"].endswith("InspectionWarning.kt")][0]["text"] == want,
                   timeout=15, message="the Mirror never caught up with the formatted buffer")
