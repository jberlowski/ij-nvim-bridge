"""Symbols, folding and selection (FEATURES.md §3-4): document symbols,
workspace symbols, folding ranges, selection ranges.

Asserted on the wire with exact positions, and again through Neovim's own
built-ins (`document_symbol`, `workspace_symbol`, `foldexpr`, `selection_range`).
"""
from __future__ import annotations

from harness.util import wait_until
from harness.wire import uri
from test_navigation import (CALLER, JAVA, SHAPES, at, attached, ask, builtin, goto,
                             mirror)

CLASS, METHOD, CONSTRUCTOR, FIELD, PROPERTY, INTERFACE, FUNCTION = 5, 6, 9, 8, 7, 11, 12


def by_name(symbols):
    return {s["name"]: s for s in symbols}


def slice_of(text, rng):
    """The text a Range covers."""
    lines = text.split("\n")

    def offset(p):
        return sum(len(l) + 1 for l in lines[:p["line"]]) + p["character"]
    return text[offset(rng["start"]):offset(rng["end"])]


def contains(outer, inner):
    def key(p):
        return (p["line"], p["character"])
    return key(outer["start"]) <= key(inner["start"]) and key(inner["end"]) <= key(outer["end"])


def flat(symbols):
    for s in symbols:
        yield s
        yield from flat(s.get("children", []))


# ------------------------------------------------------------ document symbols
class TestDocumentSymbols:

    def request(self, w, path):
        return w.request("textDocument/documentSymbol", {"textDocument": {"uri": uri(path)}}, timeout=60)

    def test_kotlin_hierarchy_and_kinds(self, bridge_container, wire):
        t = mirror(wire, bridge_container, SHAPES)[SHAPES]
        top = by_name(self.request(wire, SHAPES))

        assert top["Shape"]["kind"] == INTERFACE
        assert top["Circle"]["kind"] == CLASS and top["Square"]["kind"] == CLASS
        assert top["describe"]["kind"] == FUNCTION, "a top-level function is a Function, not a Method"

        (area,) = top["Shape"]["children"]
        assert area["name"] == "area" and area["kind"] == METHOD
        assert slice_of(t, area["selectionRange"]) == "area"
        assert contains(area["range"], area["selectionRange"])
        assert contains(top["Shape"]["range"], area["range"]), "children nest inside their parent"

        circle_kids = by_name(top["Circle"]["children"])
        assert circle_kids["area"]["kind"] == METHOD
        assert circle_kids["radius"]["kind"] in (FIELD, PROPERTY), "a constructor property"

    def test_java_hierarchy_and_kinds(self, bridge_container, wire):
        mirror(wire, bridge_container, JAVA)
        (outer,) = self.request(wire, JAVA)
        assert outer["name"] == "JavaShapes" and outer["kind"] == CLASS
        kids = by_name(outer["children"])
        assert kids["Shape"]["kind"] == INTERFACE
        assert by_name(kids["Shape"]["children"])["perimeter"]["kind"] == METHOD
        assert kids["total"]["kind"] == METHOD

        rect = by_name(kids["Rect"]["children"])
        assert rect["w"]["kind"] == FIELD and rect["h"]["kind"] == FIELD
        assert rect["perimeter"]["kind"] == METHOD
        assert any(s["kind"] == CONSTRUCTOR for s in kids["Rect"]["children"]), "the constructor"

    def test_it_reflects_an_unsaved_buffer(self, bridge_container, wire):
        text = bridge_container.read_file(SHAPES) + "\nclass BrandNewInTheBuffer\n"
        wire.did_open(SHAPES, text)
        assert "BrandNewInTheBuffer" in by_name(self.request(wire, SHAPES))


# ----------------------------------------------------------- workspace symbols
class TestWorkspaceSymbols:

    def search(self, w, query):
        return w.request("workspace/symbol", {"query": query}, timeout=60)

    def test_finds_a_class_with_its_location(self, bridge_container, wire):
        found = [s for s in self.search(wire, "Circle") if s["name"] == "Circle"]
        assert found, "Circle not found"
        t = bridge_container.read_file(SHAPES)
        loc = found[0]["location"]
        assert loc["uri"] == uri(SHAPES)
        assert loc["range"]["start"]["line"] == at(t, "class Circle", 0)[0]
        assert found[0]["kind"] == CLASS

    def test_finds_methods_in_java(self, bridge_container, wire):
        """What IntelliJ's Go to Symbol returns is the answer (Passthrough): it may
        list only some of the overriding methods, so assert the declaration is
        found and that nothing else is."""
        t = bridge_container.read_file(JAVA)
        got = {s["location"]["range"]["start"]["line"] for s in self.search(wire, "perimeter")
               if s["location"]["uri"] == uri(JAVA)}
        allowed = {at(t, needle, i)[0] for needle, i in
                   (("double perimeter();", 0), ("public double perimeter()", 0), ("public double perimeter()", 1))}
        assert at(t, "double perimeter();", 0)[0] in got, got
        assert got <= allowed, got

    def test_camel_hump_and_case_insensitive_matching(self, wire):
        assert any(s["name"] == "JavaShapes" for s in self.search(wire, "javash"))
        assert any(s["name"] == "JavaShapes" for s in self.search(wire, "JS"))

    def test_best_match_first(self, wire):
        names = [s["name"] for s in self.search(wire, "describe")]
        assert names and names[0] == "describe"

    def test_an_empty_query_is_an_empty_answer(self, wire):
        assert self.search(wire, "") == []

    def test_it_needs_no_open_buffer(self, wire):
        assert wire.debug_state()["mirrors"] == []
        assert self.search(wire, "Square")

    def test_a_symbol_that_only_exists_in_an_unsaved_buffer(self, bridge_container, wire):
        """The index follows the Mirror, not only the disk."""
        wire.did_open(SHAPES, bridge_container.read_file(SHAPES) + "\nclass OnlyInTheUnsavedBuffer\n")
        wait_until(lambda: any(s["name"] == "OnlyInTheUnsavedBuffer"
                               for s in self.search(wire, "OnlyInTheUnsavedBuffer")),
                   timeout=40, message="an unsaved symbol never became searchable")


# --------------------------------------------------------------------- folding
class TestFoldingRange:

    def request(self, w, path):
        return w.request("textDocument/foldingRange", {"textDocument": {"uri": uri(path)}}, timeout=60)

    def test_kotlin_folds_bodies_and_comments(self, bridge_container, wire):
        t = mirror(wire, bridge_container, SHAPES)[SHAPES]
        ranges = self.request(wire, SHAPES)
        starts = {r["startLine"]: r for r in ranges}

        iface = starts[at(t, "interface Shape", 0)[0]]
        closing = t.split("\n").index("}", iface["startLine"])
        assert iface["endLine"] == closing - 1, "the closing brace stays visible"

        assert at(t, "fun describe", 0)[0] in starts
        kdoc = starts[at(t, "/**", 0)[0]]
        assert kdoc["kind"] == "comment"
        assert all(r["endLine"] > r["startLine"] for r in ranges), "nothing single-line"

    def test_java_folds_nested_types(self, bridge_container, wire):
        t = mirror(wire, bridge_container, JAVA)[JAVA]
        starts = {r["startLine"] for r in self.request(wire, JAVA)}
        for needle in ("public interface Shape", "public static class Rect", "public static class Tri",
                       "public static double total"):
            assert at(t, needle, 0)[0] in starts, needle


# ------------------------------------------------------------------- selection
class TestSelectionRange:

    def test_chain_grows_from_the_word_to_the_file(self, bridge_container, wire):
        t = mirror(wire, bridge_container, SHAPES)[SHAPES]
        line, col = at(t, "shape.area()", 0, 6)
        (chain,) = wire.request("textDocument/selectionRange", {
            "textDocument": {"uri": uri(SHAPES)}, "positions": [{"line": line, "character": col}]}, timeout=60)

        ranges = []
        node = chain
        while node:
            ranges.append(node["range"])
            node = node.get("parent")
        assert slice_of(t, ranges[0]) == "area", "starts at the word under the cursor"
        assert any(slice_of(t, r) == "shape.area()" for r in ranges), [slice_of(t, r) for r in ranges]
        assert len(ranges) >= 4
        for inner, outer in zip(ranges, ranges[1:]):
            assert contains(outer, inner) and outer != inner, "each range strictly contains the last"
        assert slice_of(t, ranges[-1]) == t.rstrip("\n") or slice_of(t, ranges[-1]) == t, "capped by the file"

    def test_several_positions_answer_in_order(self, bridge_container, wire):
        t = mirror(wire, bridge_container, SHAPES)[SHAPES]
        a, b = at(t, "class Circle", 0, 6), at(t, "class Square", 0, 6)
        result = wire.request("textDocument/selectionRange", {
            "textDocument": {"uri": uri(SHAPES)},
            "positions": [{"line": a[0], "character": a[1]}, {"line": b[0], "character": b[1]}]}, timeout=60)
        assert [slice_of(t, r["range"]) for r in result] == ["Circle", "Square"]


# ============================================== through Neovim's own built-ins
class TestThroughNeovimsBuiltins:

    def test_document_symbol_lists_the_outline(self, nvim):
        goto(nvim, SHAPES, "class Circle", 0, 6)
        got = nvim.exec_lua("""
            local items
            vim.lsp.buf.document_symbol({ on_list = function(o) items = o.items end })
            vim.wait(30000, function() return items ~= nil end, 20)
            local out = {}
            for _, it in ipairs(items or {}) do table.insert(out, it.text) end
            return out""")
        text = "\n".join(got)
        for name in ("Shape", "Circle", "Square", "describe", "area"):
            assert name in text, (name, got)

    def test_workspace_symbol_finds_across_the_project(self, nvim):
        goto(nvim, SHAPES, "class Circle", 0, 6)
        got = nvim.exec_lua("""
            local items
            vim.lsp.buf.workspace_symbol('JavaShapes', { on_list = function(o) items = o.items end })
            vim.wait(30000, function() return items ~= nil end, 20)
            local out = {}
            for _, it in ipairs(items or {}) do table.insert(out, { file = it.filename, text = it.text }) end
            return out""")
        assert any(i["file"] == JAVA and "JavaShapes" in i["text"] for i in got), got

    def test_lsp_folding_folds_the_interface_body(self, nvim):
        t = goto(nvim, SHAPES, "interface Shape", 0, 0)
        nvim.command("setlocal foldmethod=expr foldexpr=v:lua.vim.lsp.foldexpr() foldlevel=99")
        body = at(t, "fun area(): Double", 0)[0] + 1            # 1-based line inside the interface

        def level():
            nvim.command("normal! zx")                         # recompute folds
            return nvim.eval(f"foldlevel({body})")
        assert wait_until(lambda: level() >= 1, timeout=20, message="LSP folding never produced a fold")

    def test_selection_range_expands_in_visual_mode(self, nvim):
        t = goto(nvim, SHAPES, "shape.area()", 0, 6)

        def selected():
            return nvim.exec_lua("return table.concat(vim.fn.getregion(vim.fn.getpos('v'), vim.fn.getpos('.')), '\\n')")
        nvim.exec_lua("vim.lsp.buf.selection_range(1)")
        first = selected()
        nvim.exec_lua("vim.lsp.buf.selection_range(1)")
        second = selected()
        nvim.exec_lua("vim.lsp.buf.selection_range(1)")
        third = selected()
        assert "area" in first
        assert len(second) > len(first) and len(third) > len(second), (first, second, third)
        assert second in third


# ------------------------------------------------------------- signature help
KOTLIN_CALLS = '''
fun two(first: Int, second: String): Int = 0
fun overloaded(a: Int): Int = a
fun overloaded(a: Int, b: Int): Int = a + b
val probeCall = two(1, "x")
val probeOverload = overloaded(1, 2)
'''


def with_calls(c):
    return c.read_file(SHAPES) + KOTLIN_CALLS


def java_with_call(c):
    text = c.read_file(JAVA)
    body = "    public static double probeJava() { return total(new Rect(1.0, 2.0)); }\n"
    return text[:text.rindex("}")] + body + "}\n"


def sig(w, path, pos):
    return w.request("textDocument/signatureHelp", {
        "textDocument": {"uri": uri(path)}, "position": {"line": pos[0], "character": pos[1]}}, timeout=60)


class TestSignatureHelp:

    def test_kotlin_first_parameter(self, bridge_container, wire):
        t = with_calls(bridge_container)
        wire.did_open(SHAPES, t)
        h = sig(wire, SHAPES, at(t, "two(1", 0, 4))
        (s,) = h["signatures"]
        assert "first: Int" in s["label"] and "second: String" in s["label"], s["label"]
        assert h["activeParameter"] == 0
        assert len(s["parameters"]) == 2

    def test_kotlin_second_parameter_after_the_comma(self, bridge_container, wire):
        t = with_calls(bridge_container)
        wire.did_open(SHAPES, t)
        h = sig(wire, SHAPES, at(t, 'two(1, "x")', 0, 7))
        assert h["activeParameter"] == 1
        (s,) = h["signatures"]
        a, b = s["parameters"][1]["label"]
        assert s["label"][a:b] == "second: String", s

    def test_overloads_are_all_offered_and_the_matching_one_active(self, bridge_container, wire):
        t = with_calls(bridge_container)
        wire.did_open(SHAPES, t)
        h = sig(wire, SHAPES, at(t, "overloaded(1, 2)", 0, 14))
        labels = [s["label"] for s in h["signatures"]]
        assert len(labels) >= 2, labels
        assert "b: Int" in labels[h["activeSignature"]], (labels, h["activeSignature"])
        assert h["activeParameter"] == 1

    def test_java_constructor(self, bridge_container, wire):
        t = java_with_call(bridge_container)
        wire.did_open(JAVA, t)
        h = sig(wire, JAVA, at(t, "new Rect(1.0, 2.0)", 0, 14))
        (s,) = h["signatures"]
        assert "double w" in s["label"] and "double h" in s["label"], s["label"]
        assert h["activeParameter"] == 1

    def test_outside_a_call_is_null_not_an_error(self, bridge_container, wire):
        t = with_calls(bridge_container)
        wire.did_open(SHAPES, t)
        assert sig(wire, SHAPES, at(t, "interface Shape", 0, 3)) is None

    def test_it_is_advertised_with_its_trigger_characters(self, bridge):
        from harness.wire import Wire
        with Wire(bridge.port) as w:
            caps = w.initialize()["capabilities"]
        assert caps["signatureHelpProvider"]["triggerCharacters"] == ["(", ","]


class TestSignatureHelpThroughNeovim:

    def test_signature_help_opens_a_float_naming_the_parameter(self, nvim, bridge_container):
        nvim.command(f"edit {SHAPES}")
        wait_until(lambda: attached(nvim) == 1)
        lines = nvim.current.buffer[:] + KOTLIN_CALLS.rstrip("\n").split("\n")
        nvim.current.buffer[:] = lines
        row = next(i for i, l in enumerate(lines) if l.startswith("val probeCall"))
        nvim.current.window.cursor = (row + 1, lines[row].index('"x"'))     # after "two(1, "
        nvim.exec_lua("vim.lsp.buf.signature_help()")

        def float_text():
            texts = nvim.exec_lua("""
                local out = {}
                for _, w in ipairs(vim.api.nvim_list_wins()) do
                  if vim.api.nvim_win_get_config(w).relative ~= '' then
                    table.insert(out, table.concat(
                      vim.api.nvim_buf_get_lines(vim.api.nvim_win_get_buf(w), 0, -1, false), ' '))
                  end
                end
                return out""")
            return next((t for t in texts if "second: String" in t), None)
        text = wait_until(float_text, timeout=25, message="signature help never opened a float")
        assert "first: Int" in text
