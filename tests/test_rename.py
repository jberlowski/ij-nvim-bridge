"""Rename (FEATURES.md §3): `textDocument/prepareRename` and `textDocument/rename`.

Data first (D2): the Brain answers with the `WorkspaceEdit` IntelliJ's own usage search
implies; nothing is applied in IntelliJ, and the Mirrors and the disk are untouched.
"""
from __future__ import annotations

import time

import pytest

from harness.util import wait_until
from harness.wire import SRC, RpcError, replace_range, uri
from test_formatting import apply_edits
from test_navigation import CALLER, JAVA, SHAPES, at, ask, mirror, ready
from test_editor_slice import attached


def changes(result) -> dict[str, list[dict]]:
    """`{uri: [TextEdit]}` from either shape of WorkspaceEdit."""
    if "changes" in result:
        return result["changes"]
    return {d["textDocument"]["uri"]: d["edits"] for d in result["documentChanges"] if "edits" in d}


def apply_all(texts: dict[str, str], result) -> dict[str, str]:
    edits = changes(result)
    return {path: apply_edits(text, edits.get(uri(path), [])) for path, text in texts.items()}


def again_when_indexing(w, method, path, pos, **extra):
    """What a client does with "IntelliJ is indexing; ask again": ask again."""
    for attempt in range(20):
        try:
            return ask(w, method, path, pos, **extra)
        except RpcError as e:
            if e.code != -32801 or "indexing" not in str(e) or attempt == 19:
                raise
            time.sleep(2)


def rename(w, path, pos, name):
    return again_when_indexing(w, "textDocument/rename", path, pos, newName=name)


# ------------------------------------------------------------- prepareRename
class TestPrepareRename:

    def test_a_usage_gives_the_name_as_written(self, bridge_container, wire):
        t = mirror(wire, bridge_container, SHAPES)[SHAPES]
        line, col = at(t, "shape.area()", 0, 6)
        r = again_when_indexing(wire, "textDocument/prepareRename", SHAPES, (line, col))
        assert r["placeholder"] == "area"
        assert r["range"] == {"start": {"line": line, "character": col},
                              "end": {"line": line, "character": col + 4}}

    def test_a_declaration_gives_its_name(self, bridge_container, wire):
        t = mirror(wire, bridge_container, SHAPES)[SHAPES]
        r = again_when_indexing(wire, "textDocument/prepareRename", SHAPES, at(t, "fun area(): Double", 0, 5))
        assert r["placeholder"] == "area"

    def test_where_there_is_nothing_to_rename_it_says_so(self, bridge_container, wire):
        t = mirror(wire, bridge_container, SHAPES)[SHAPES]
        assert again_when_indexing(wire, "textDocument/prepareRename", SHAPES, at(t, "package dev", 0, 3)) is None

    def test_library_code_cannot_be_renamed_and_says_why(self, bridge_container, wire):
        text = "package dev.bridge.fixture.probe\n\nfun n(s: String) = s.length\n"
        wire.did_open(SHAPES, text)
        with pytest.raises(RpcError) as e:
            again_when_indexing(wire, "textDocument/prepareRename", SHAPES, at(text, "s.length", 0, 3))
        assert e.value.code == -32602 and "not part of this project" in str(e.value)

    def test_it_is_advertised_with_prepare_support(self, bridge):
        from harness.wire import Wire
        with Wire(bridge.port) as w:
            caps = w.initialize()["capabilities"]
        assert caps["renameProvider"] == {"prepareProvider": True}


# ------------------------------------------------------------- Kotlin, on the wire
class TestKotlin:

    def test_an_interface_method_is_renamed_with_its_overriders_and_every_usage(self, bridge_container, wire):
        texts = mirror(wire, bridge_container, SHAPES, CALLER)
        result = rename(wire, SHAPES, at(texts[SHAPES], "fun area(): Double", 0, 5), "measure")
        out = apply_all(texts, result)

        assert out[SHAPES].count("    fun measure(): Double\n") == 1                # the interface
        assert out[SHAPES].count("override fun measure()") == 2               # Circle and Square
        assert out[SHAPES].count("shape.measure()") == 2                      # describe()
        assert "s.measure()" in out[CALLER]                                   # another file
        for text in out.values():
            assert ".area()" not in text and "fun area(" not in text, text
        assert "The area of this shape" in out[SHAPES], "documentation prose is not code"

    def test_asking_from_a_usage_is_asking_about_the_same_thing(self, bridge_container, wire):
        texts = mirror(wire, bridge_container, SHAPES, CALLER)
        from_decl = rename(wire, SHAPES, at(texts[SHAPES], "fun area(): Double", 0, 5), "measure")
        from_use = rename(wire, CALLER, at(texts[CALLER], "s.area()", 0, 3), "measure")
        assert apply_all(texts, from_decl) == apply_all(texts, from_use)

    def test_an_unsaved_buffer_is_renamed_as_it_is_not_as_disk_has_it(self, bridge_container, wire):
        texts = mirror(wire, bridge_container, SHAPES, CALLER)
        wire.did_change(CALLER, 1, replace_range(2, 0, 0, "fun again(s: Shape): Double = s.area()\n"))
        texts[CALLER] = "\n".join(texts[CALLER].split("\n")[:2] + ["fun again(s: Shape): Double = s.area()"]
                                  + texts[CALLER].split("\n")[2:])
        out = apply_all(texts, rename(wire, SHAPES, at(texts[SHAPES], "fun area(): Double", 0, 5), "measure"))
        assert "s.measure()" in out[CALLER] and ".area()" not in out[CALLER], out[CALLER]

    def test_a_request_changes_neither_the_mirror_nor_the_disk(self, bridge_container, wire):
        on_disk = bridge_container.read_file(SHAPES)
        texts = mirror(wire, bridge_container, SHAPES)
        rename(wire, SHAPES, at(texts[SHAPES], "fun area(): Double", 0, 5), "measure")
        (m,) = wire.debug_state(text=True)["mirrors"]
        assert m["text"] == texts[SHAPES] and m["version"] == 0
        assert bridge_container.read_file(SHAPES) == on_disk

    def test_an_empty_name_is_refused_with_a_reason(self, bridge_container, wire):
        """Kotlin accepts nearly any name in backticks, so only the empty one is certain."""
        texts = mirror(wire, bridge_container, SHAPES)
        with pytest.raises(RpcError) as e:
            rename(wire, SHAPES, at(texts[SHAPES], "fun area(): Double", 0, 5), "")
        assert e.value.code == -32602 and "not a valid name" in str(e.value)

    def test_asking_where_there_is_nothing_is_refused(self, bridge_container, wire):
        texts = mirror(wire, bridge_container, SHAPES)
        with pytest.raises(RpcError) as e:
            rename(wire, SHAPES, at(texts[SHAPES], "package dev", 0, 3), "x")
        assert e.value.code == -32602

    def test_renaming_an_override_renames_that_override_and_its_own_overriders(self, bridge_container, wire):
        """IntelliJ asks, in a dialog, whether to rename the base method too. There is no one
        to ask, so the override alone is renamed. Recorded so it is a decision, not an accident."""
        texts = mirror(wire, bridge_container, SHAPES)
        out = apply_all(texts, rename(wire, SHAPES, at(texts[SHAPES], "override fun area", 0, 14), "measure"))
        assert out[SHAPES].count("fun measure(") == 1 and out[SHAPES].count("fun area(") == 2

    def test_a_change_mid_request_is_content_modified(self, bridge_container, wire):
        texts = mirror(wire, bridge_container, SHAPES)
        wire.request("$/ij/debug/navigationDelay", {"ms": 1500})
        try:
            rid = wire.send_request("textDocument/rename", {
                "textDocument": {"uri": uri(SHAPES)},
                "position": dict(zip(("line", "character"), at(texts[SHAPES], "fun area(): Double", 0, 5))),
                "newName": "measure"})
            import time
            time.sleep(0.3)
            wire.did_change(SHAPES, 1, replace_range(0, 0, 0, "// changed\n"))
            assert wire.await_response(rid, timeout=15)["error"]["code"] == -32801
        finally:
            wire.request("$/ij/debug/navigationDelay", {"ms": 0})


# --------------------------------------------------------------- Java, on the wire
class TestJava:

    def test_an_interface_method_is_renamed_with_its_overriders_and_usages(self, bridge_container, wire):
        texts = mirror(wire, bridge_container, JAVA)
        out = apply_all(texts, rename(wire, JAVA, at(texts[JAVA], "double perimeter();", 0, 8), "circumference"))[JAVA]
        assert out.count("double circumference();") == 1
        assert out.count("public double circumference()") == 2                  # Rect and Tri
        assert out.count("shape.circumference()") == 2
        assert "perimeter()" not in out

    def test_a_top_level_class_takes_its_file_with_it(self, bridge_container, wire):
        """Java requires a public class to live in a file of its name: the rename
        goes in `documentChanges`, edits first, then the file rename."""
        texts = mirror(wire, bridge_container, JAVA)
        result = rename(wire, JAVA, at(texts[JAVA], "public class JavaShapes", 0, 14), "Figures")
        ops = result["documentChanges"]
        assert ops[-1] == {"kind": "rename", "oldUri": uri(JAVA), "newUri": uri(JAVA).replace("JavaShapes.java", "Figures.java")}
        out = apply_edits(texts[JAVA], changes(result)[uri(JAVA)])
        assert "public class Figures" in out and "JavaShapes" not in out

    def test_an_invalid_name_is_refused_with_a_reason(self, bridge_container, wire):
        texts = mirror(wire, bridge_container, JAVA)
        with pytest.raises(RpcError) as e:
            rename(wire, JAVA, at(texts[JAVA], "double perimeter();", 0, 8), "1bad name")
        assert e.value.code == -32602 and "not a valid name" in str(e.value)

    def test_a_nested_class_is_renamed_where_it_is_used(self, bridge_container, wire):
        texts = mirror(wire, bridge_container, JAVA)
        out = apply_all(texts, rename(wire, JAVA, at(texts[JAVA], "class Rect", 0, 7), "Rectangle"))[JAVA]
        assert "public static class Rectangle implements Shape" in out
        assert "public Rectangle(double w, double h)" in out, "the constructor is renamed with the class"


# ===================================================== through Neovim's built-ins
def open_buffers(nvim, *paths):
    for p in paths:
        nvim.command(f"edit {p}")
        wait_until(lambda: attached(nvim) == 1, message="never attached")


def buffer_text(nvim, path) -> str:
    return nvim.exec_lua(
        "return table.concat(vim.api.nvim_buf_get_lines(vim.fn.bufnr(...), 0, -1, false), '\\n')", path)


class TestThroughNeovimsBuiltins:

    def test_rename_from_the_cursor_changes_every_file(self, nvim):
        """`vim.lsp.buf.rename`: prepareRename, then rename, applied across files."""
        open_buffers(nvim, CALLER, SHAPES)          # ends in SHAPES
        text = "\n".join(nvim.current.buffer[:])
        line, col = at(text, "fun area(): Double", 0, 5)
        nvim.current.window.cursor = (line + 1, col)
        nvim.exec_lua("vim.lsp.buf.rename('measure', { name = 'ij-bridge' })")

        wait_until(lambda: "fun measure(): Double" in "\n".join(nvim.current.buffer[:]), timeout=30,
                   message="the declaration was not renamed:\n" + "\n".join(nvim.current.buffer[:]))
        shapes = buffer_text(nvim, SHAPES)
        assert shapes.count("measure") >= 5 and "area()" not in shapes, shapes
        assert "s.measure()" in buffer_text(nvim, CALLER), "the other file was not changed"

    def test_java_rename_from_the_cursor(self, nvim):
        open_buffers(nvim, JAVA)
        text = "\n".join(nvim.current.buffer[:])
        line, col = at(text, "double perimeter();", 0, 8)
        nvim.current.window.cursor = (line + 1, col)
        nvim.exec_lua("vim.lsp.buf.rename('circumference', { name = 'ij-bridge' })")
        try:
            wait_until(lambda: "perimeter()" not in "\n".join(nvim.current.buffer[:]), timeout=30)
        except AssertionError as e:
            raise AssertionError("MESSAGES: " + nvim.command_output("messages") + "\nCLIENTS: " + str(nvim.exec_lua(
                "return #vim.lsp.get_clients({ bufnr = 0, name = 'ij-bridge' })"))) from e
        text = "\n".join(nvim.current.buffer[:])
        assert text.count("circumference") == 5, text
        assert "The perimeter of this shape" in text, "documentation prose is not code"

    def test_nothing_reaches_the_disk_until_the_developer_writes(self, nvim, bridge_container):
        before = bridge_container.read_file(SHAPES)
        open_buffers(nvim, SHAPES)
        text = "\n".join(nvim.current.buffer[:])
        line, col = at(text, "fun area(): Double", 0, 5)
        nvim.current.window.cursor = (line + 1, col)
        nvim.exec_lua("vim.lsp.buf.rename('measure', { name = 'ij-bridge' })")
        wait_until(lambda: "fun measure(): Double" in "\n".join(nvim.current.buffer[:]), timeout=30)
        assert bridge_container.read_file(SHAPES) == before
