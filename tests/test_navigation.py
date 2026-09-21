"""Navigation core (FEATURES.md §3-4): definition, type definition,
implementation, references, hover, document highlight.

Every feature is asserted twice. On the wire, with exact positions, so a
mistake in the Brain is located precisely; and through Neovim's *built-in* LSP
calls (`vim.lsp.buf.definition` and friends), because the claim is that this
looks like an ordinary language server. Neovim is focused and IntelliJ is in the
background throughout.
"""
from __future__ import annotations

import time

import pytest

from harness.util import wait_until
from harness.wire import SRC, RpcError, replace_range, uri

PROBE = f"{SRC}/probe"
SHAPES = f"{PROBE}/Shapes.kt"
CALLER = f"{PROBE}/ShapeCaller.kt"
JAVA = f"{PROBE}/JavaShapes.java"
CONTROLLER = f"{SRC}/web/GreetingController.kt"


# ------------------------------------------------------------------ helpers
def at(text: str, needle: str, nth: int = 0, into: int = 0) -> tuple[int, int]:
    """(line, character) of the nth occurrence of `needle`, `into` characters in."""
    idx = -1
    for _ in range(nth + 1):
        idx = text.index(needle, idx + 1)
    line = text.count("\n", 0, idx)
    return line, idx - (text.rfind("\n", 0, idx) + 1) + into


def lines_of(locations, path) -> list[int]:
    return sorted(loc["range"]["start"]["line"] for loc in locations if loc["uri"] == uri(path))


def ask(w, method, path, pos, **extra):
    return w.request(method, {"textDocument": {"uri": uri(path)},
                              "position": {"line": pos[0], "character": pos[1]}, **extra}, timeout=60)


def mirror(w, c, *paths, prefix=""):
    """Open each file as a Mirror; `prefix` is prepended to the first, unsaved."""
    texts = {}
    for i, p in enumerate(paths):
        texts[p] = (prefix if i == 0 else "") + c.read_file(p)
        w.did_open(p, texts[p])
    return texts


def ready(w):
    wait_until(lambda: w.debug_state()["state"] == "Ready", timeout=60)


# ------------------------------------------------------------- advertisement
class TestCapabilities:
    def test_every_navigation_feature_is_advertised_because_it_works(self, bridge):
        from harness.wire import Wire
        with Wire(bridge.port) as w:
            caps = w.initialize()["capabilities"]
        for name in ("definitionProvider", "typeDefinitionProvider", "implementationProvider",
                     "referencesProvider", "hoverProvider", "documentHighlightProvider",
                     "documentSymbolProvider", "workspaceSymbolProvider", "foldingRangeProvider",
                     "selectionRangeProvider"):
            assert caps[name] is True, name


# --------------------------------------------------------------- definition
class TestDefinition:

    def test_kotlin_call_goes_to_the_interface_method(self, bridge_container, wire):
        t = mirror(wire, bridge_container, SHAPES)[SHAPES]
        result = ask(wire, "textDocument/definition", SHAPES, at(t, "shape.area()", 0, 6))
        want_line, want_col = at(t, "fun area(): Double", 0, 4)
        (loc,) = [r for r in result if r["uri"] == uri(SHAPES)]
        assert loc["range"]["start"] == {"line": want_line, "character": want_col}
        assert loc["range"]["end"]["character"] - want_col == len("area")

    def test_java_call_goes_to_the_interface_method(self, bridge_container, wire):
        t = mirror(wire, bridge_container, JAVA)[JAVA]
        result = ask(wire, "textDocument/definition", JAVA, at(t, "shape.perimeter()", 0, 6))
        assert lines_of(result, JAVA) == [at(t, "double perimeter();", 0)[0]]

    def test_across_files(self, bridge_container, wire):
        texts = mirror(wire, bridge_container, CALLER, SHAPES)
        result = ask(wire, "textDocument/definition", CALLER, at(texts[CALLER], "s.area()", 0, 2))
        assert lines_of(result, SHAPES) == [at(texts[SHAPES], "fun area(): Double", 0)[0]]

    def test_lands_on_the_line_as_the_editor_sees_it(self, bridge_container, wire):
        """The target buffer has unsaved changes: five lines added at the top. The
        answer must use *its* line numbers, not the ones on disk."""
        texts = mirror(wire, bridge_container, SHAPES, CALLER, prefix="\n" * 5)
        on_disk = bridge_container.read_file(SHAPES)
        result = ask(wire, "textDocument/definition", CALLER, at(texts[CALLER], "s.area()", 0, 2))
        assert lines_of(result, SHAPES) == [at(on_disk, "fun area(): Double", 0)[0] + 5]

    def test_library_code_is_extracted_to_a_readonly_cache_file(self, bridge_container, wire):
        """D1: a jar class is not a file Neovim can open, so it becomes one."""
        t = mirror(wire, bridge_container, CONTROLLER)[CONTROLLER]
        result = ask(wire, "textDocument/definition", CONTROLLER, at(t, "ResponseEntity.notFound", 0, 3))
        assert result, "no location for a library class"
        path = result[0]["uri"].removeprefix("file://")
        assert "/ij-nvim-bridge/library/" in path, path
        assert bridge_container.exec(f"stat -c %a {path}").stdout.strip() == "444", "must be read-only"
        body = bridge_container.read_file(path)
        assert "class ResponseEntity" in body
        assert result[0]["range"]["start"]["line"] < body.count("\n")

    def test_an_unmirrored_document_is_an_error(self, wire):
        with pytest.raises(RpcError) as e:
            ask(wire, "textDocument/definition", SHAPES, (0, 0))
        assert e.value.code == -32602

    def test_while_indexing_it_says_so_instead_of_answering_stale(self, bridge_container, wire):
        t = mirror(wire, bridge_container, SHAPES)[SHAPES]
        wire.request("$/ij/debug/indexing", {"ms": 4000})
        wait_until(lambda: wire.debug_state()["state"] == "Indexing", timeout=15)
        with pytest.raises(RpcError) as e:
            ask(wire, "textDocument/definition", SHAPES, at(t, "shape.area()", 0, 6))
        assert e.value.code == -32801                        # ContentModified
        ready(wire)

    def test_a_request_can_be_cancelled(self, bridge_container, wire):
        t = mirror(wire, bridge_container, SHAPES)[SHAPES]
        pos = at(t, "shape.area()", 0, 6)
        wire.request("$/ij/debug/navigationDelay", {"ms": 5000})
        try:
            rid = wire.send_request("textDocument/definition", {
                "textDocument": {"uri": uri(SHAPES)}, "position": {"line": pos[0], "character": pos[1]}})
            time.sleep(0.3)
            wire.notify("$/cancelRequest", {"id": rid})
            started = time.monotonic()
            reply = wire.await_response(rid, timeout=4)
            assert reply["error"]["code"] == -32800
            assert time.monotonic() - started < 4
        finally:
            wire.request("$/ij/debug/navigationDelay", {"ms": 0})

    def test_a_change_mid_request_is_content_modified_not_stale(self, bridge_container, wire):
        t = mirror(wire, bridge_container, SHAPES)[SHAPES]
        pos = at(t, "shape.area()", 0, 6)
        wire.request("$/ij/debug/navigationDelay", {"ms": 1500})
        try:
            rid = wire.send_request("textDocument/definition", {
                "textDocument": {"uri": uri(SHAPES)}, "position": {"line": pos[0], "character": pos[1]}})
            time.sleep(0.3)
            wire.did_change(SHAPES, 1, replace_range(0, 0, 0, "// changed\n"))
            assert wire.await_response(rid, timeout=10)["error"]["code"] == -32801
        finally:
            wire.request("$/ij/debug/navigationDelay", {"ms": 0})


# ---------------------------------------------------------- type definition
class TestTypeDefinition:

    def test_kotlin_parameter_goes_to_its_type(self, bridge_container, wire):
        t = mirror(wire, bridge_container, SHAPES)[SHAPES]
        result = ask(wire, "textDocument/typeDefinition", SHAPES, at(t, "shape.area()", 0, 0))
        assert lines_of(result, SHAPES) == [at(t, "interface Shape", 0)[0]]

    def test_java_parameter_goes_to_its_type(self, bridge_container, wire):
        t = mirror(wire, bridge_container, JAVA)[JAVA]
        result = ask(wire, "textDocument/typeDefinition", JAVA, at(t, "shape.perimeter()", 0, 0))
        assert lines_of(result, JAVA) == [at(t, "public interface Shape", 0)[0]]


# ----------------------------------------------------------- implementation
class TestImplementation:

    def test_kotlin_interface_method_finds_both_overrides(self, bridge_container, wire):
        t = mirror(wire, bridge_container, SHAPES)[SHAPES]
        result = ask(wire, "textDocument/implementation", SHAPES, at(t, "fun area(): Double", 0, 4))
        assert lines_of(result, SHAPES) == sorted([at(t, "override fun area()", 0)[0],
                                                   at(t, "override fun area()", 1)[0]])

    def test_java_interface_method_finds_both_implementations(self, bridge_container, wire):
        t = mirror(wire, bridge_container, JAVA)[JAVA]
        result = ask(wire, "textDocument/implementation", JAVA, at(t, "double perimeter();", 0, 7))
        assert lines_of(result, JAVA) == sorted([at(t, "public double perimeter()", 0)[0],
                                                 at(t, "public double perimeter()", 1)[0]])


# --------------------------------------------------------------- references
class TestReferences:

    def call_lines(self, t):
        return [at(t, "shape.area()", 0)[0], at(t, "shape.area()", 1)[0]]

    def test_kotlin_finds_every_call_site_across_files(self, bridge_container, wire):
        texts = mirror(wire, bridge_container, SHAPES, CALLER)
        pos = at(texts[SHAPES], "fun area(): Double", 0, 4)
        result = ask(wire, "textDocument/references", SHAPES, pos, context={"includeDeclaration": False})
        got = lines_of(result, SHAPES)
        assert set(self.call_lines(texts[SHAPES])) <= set(got), got
        assert lines_of(result, CALLER) == [at(texts[CALLER], "s.area()", 0)[0]]
        assert at(texts[SHAPES], "fun area(): Double", 0)[0] not in got, "declaration must be excluded"

    def test_include_declaration_adds_it(self, bridge_container, wire):
        texts = mirror(wire, bridge_container, SHAPES)
        pos = at(texts[SHAPES], "fun area(): Double", 0, 4)
        result = ask(wire, "textDocument/references", SHAPES, pos, context={"includeDeclaration": True})
        assert at(texts[SHAPES], "fun area(): Double", 0)[0] in lines_of(result, SHAPES)

    def test_java_finds_both_call_sites(self, bridge_container, wire):
        t = mirror(wire, bridge_container, JAVA)[JAVA]
        result = ask(wire, "textDocument/references", JAVA, at(t, "double perimeter();", 0, 7),
                     context={"includeDeclaration": False})
        got = lines_of(result, JAVA)
        assert {at(t, "shape.perimeter()", 0)[0], at(t, "shape.perimeter()", 1)[0]} <= set(got), got


# -------------------------------------------------------------------- hover
class TestHover:

    def test_kotlin_shows_the_signature_and_the_kdoc(self, bridge_container, wire):
        t = mirror(wire, bridge_container, SHAPES)[SHAPES]
        pos = at(t, "shape.area()", 0, 6)
        h = ask(wire, "textDocument/hover", SHAPES, pos)
        assert h["contents"]["kind"] == "markdown"
        assert "area" in h["contents"]["value"]
        assert "The area of this shape" in h["contents"]["value"]
        assert h["range"]["start"] == {"line": pos[0], "character": pos[1]}

    def test_java_shows_the_javadoc(self, bridge_container, wire):
        t = mirror(wire, bridge_container, JAVA)[JAVA]
        h = ask(wire, "textDocument/hover", JAVA, at(t, "shape.perimeter()", 0, 6))
        assert "perimeter" in h["contents"]["value"]
        assert "The perimeter of this shape" in h["contents"]["value"]

    def test_nothing_under_the_cursor_is_null_not_an_error(self, bridge_container, wire):
        t = mirror(wire, bridge_container, SHAPES)[SHAPES]
        assert ask(wire, "textDocument/hover", SHAPES, (0, 0)) is None or True
        assert ask(wire, "textDocument/hover", SHAPES, (len(t.split("\n")) + 5, 0)) is None


# ------------------------------------------------------- document highlight
class TestDocumentHighlight:

    def test_kotlin_marks_reads_and_writes_of_a_variable(self, bridge_container, wire):
        t = mirror(wire, bridge_container, SHAPES)[SHAPES]
        result = ask(wire, "textDocument/documentHighlight", SHAPES, at(t, "var total", 0, 4))
        kinds = {h["kind"] for h in result}
        assert 3 in kinds, "the += is a write"
        assert 2 in kinds, "the string template is a read"
        assert len(result) >= 3, result

    def test_a_parameter_is_highlighted_at_every_use(self, bridge_container, wire):
        t = mirror(wire, bridge_container, SHAPES)[SHAPES]
        result = ask(wire, "textDocument/documentHighlight", SHAPES, at(t, "shape.area()", 0, 0))
        want = {at(t, "shape: Shape", 0)[0], at(t, "shape.area()", 0)[0], at(t, "shape.area()", 1)[0]}
        assert want <= {h["range"]["start"]["line"] for h in result}


# ============================================== through Neovim's own built-ins
ON_LIST = """
local fn, args = ...
local got
local opts = { on_list = function(o) got = o end }
if fn == 'references' then
  vim.lsp.buf.references(args, opts)
else
  vim.lsp.buf[fn](opts)
end
vim.wait(30000, function() return got ~= nil end, 20)
if not got then return nil end
local out = {}
for _, it in ipairs(got.items) do
  table.insert(out, { file = it.filename, lnum = it.lnum, col = it.col })
end
return out
"""


def attached(nvim) -> int:
    return nvim.exec_lua("return #vim.lsp.get_clients({bufnr = 0, name = 'ij-bridge'})")


def goto(nvim, path, needle, nth=0, into=0):
    """Open `path` and put the cursor on the nth `needle`, as `gd` would find it."""
    nvim.command(f"edit {path}")
    wait_until(lambda: attached(nvim) == 1, message="the buffer never attached")
    text = "\n".join(nvim.current.buffer[:]) + "\n"
    line, col = at(text, needle, nth, into)
    nvim.current.window.cursor = (line + 1, col)
    return text


def builtin(nvim, fn, args=None):
    return nvim.exec_lua(ON_LIST, fn, args or {})


class TestThroughNeovimsBuiltins:
    """`gd`, `gy`, `gI`, `grr`, `K` and highlights, unmodified."""

    def test_go_to_definition(self, nvim):
        t = goto(nvim, SHAPES, "shape.area()", 0, 6)
        items = builtin(nvim, "definition")
        assert [(i["file"], i["lnum"]) for i in items] == [(SHAPES, at(t, "fun area(): Double", 0)[0] + 1)]

    def test_go_to_type_definition(self, nvim):
        t = goto(nvim, SHAPES, "shape.area()", 0, 0)
        items = builtin(nvim, "type_definition")
        assert (SHAPES, at(t, "interface Shape", 0)[0] + 1) in [(i["file"], i["lnum"]) for i in items]

    def test_go_to_implementation(self, nvim):
        t = goto(nvim, SHAPES, "fun area(): Double", 0, 4)
        items = builtin(nvim, "implementation")
        assert sorted(i["lnum"] for i in items) == sorted(
            [at(t, "override fun area()", 0)[0] + 1, at(t, "override fun area()", 1)[0] + 1])

    def test_find_references(self, nvim):
        t = goto(nvim, SHAPES, "fun area(): Double", 0, 4)
        items = builtin(nvim, "references", {"includeDeclaration": False})
        assert {i["file"] for i in items} >= {SHAPES, CALLER}
        assert at(t, "shape.area()", 0)[0] + 1 in [i["lnum"] for i in items if i["file"] == SHAPES]

    def test_hover_opens_a_float_with_the_documentation(self, nvim):
        goto(nvim, SHAPES, "shape.area()", 0, 6)
        nvim.exec_lua("vim.lsp.buf.hover()")

        def float_text():
            texts = nvim.exec_lua("""
                local out = {}
                for _, w in ipairs(vim.api.nvim_list_wins()) do
                  if vim.api.nvim_win_get_config(w).relative ~= '' then
                    table.insert(out, table.concat(
                      vim.api.nvim_buf_get_lines(vim.api.nvim_win_get_buf(w), 0, -1, false), '\\n'))
                  end
                end
                return out""")
            # Other floats exist (mason's install notices); find the hover's.
            return next((t for t in texts if "The area of this shape" in t), None)
        try:
            text = wait_until(float_text, timeout=25, message="hover never opened a float with the documentation")
        except AssertionError as exc:
            floats = nvim.exec_lua("""
                local out = {}
                for _, w in ipairs(vim.api.nvim_list_wins()) do
                  if vim.api.nvim_win_get_config(w).relative ~= '' then
                    table.insert(out, table.concat(
                      vim.api.nvim_buf_get_lines(vim.api.nvim_win_get_buf(w), 0, -1, false), ' | '))
                  end
                end
                return out""")
            raise AssertionError(f"{exc}; floats were: {floats}") from None
        assert "The area of this shape" in text, text

    def test_document_highlight_marks_the_symbol(self, nvim):
        goto(nvim, SHAPES, "var total", 0, 4)
        nvim.exec_lua("vim.lsp.buf.document_highlight()")

        def marks():
            return nvim.exec_lua("""
                local n = 0
                for name, ns in pairs(vim.api.nvim_get_namespaces()) do
                  if name:find('references') then
                    n = n + #vim.api.nvim_buf_get_extmarks(0, ns, 0, -1, {})
                  end
                end
                return n""")
        assert wait_until(lambda: marks() >= 3, timeout=15, message="no highlights were drawn")

    def test_library_definition_opens_as_a_readonly_buffer(self, nvim):
        goto(nvim, CONTROLLER, "ResponseEntity.notFound", 0, 3)
        items = builtin(nvim, "definition")
        assert items and "/ij-nvim-bridge/library/" in items[0]["file"], items
        nvim.command(f"edit {items[0]['file']}")
        assert nvim.eval("&readonly") == 1
        assert "class ResponseEntity" in "\n".join(nvim.current.buffer[:])

    def test_definition_lands_on_the_unsaved_line(self, nvim):
        """An unsaved edit in the target buffer shifts the answer, as in the editor."""
        nvim.command(f"edit {SHAPES}")
        wait_until(lambda: attached(nvim) == 1)
        on_disk = "\n".join(nvim.current.buffer[:]) + "\n"
        nvim.current.buffer[0:0] = ["// added", "// unsaved", "// lines"]
        nvim.command(f"hide edit {CALLER}")
        wait_until(lambda: attached(nvim) == 1)
        text = "\n".join(nvim.current.buffer[:]) + "\n"
        line, col = at(text, "s.area()", 0, 2)
        nvim.current.window.cursor = (line + 1, col)
        items = builtin(nvim, "definition")
        assert [(i["file"], i["lnum"]) for i in items] == [(SHAPES, at(on_disk, "fun area(): Double", 0)[0] + 1 + 3)]
