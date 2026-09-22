"""What IntelliJ greys out (FEATURES.md §4): unused imports, unused locals, unused declarations.

IntelliJ does not mark these with a distinct severity - an unused-import warning and an ordinary
"weak warning" squiggle are both plain WEAK_WARNING - it marks them with a specific editor text
colour (`NOT_USED_ELEMENT_ATTRIBUTES`). The Brain reads that and sends LSP `DiagnosticTag.Unnecessary`
(tag 1) on exactly those diagnostics, which is what makes a client grey the text out too, the same
way IntelliJ does. Reproduced first against the real IDE (see brain/src/main/kotlin/dev/bridge/brain/
Diagnostics.kt for how) before writing this.
"""
from __future__ import annotations

import time

from harness.util import wait_until
from harness.wire import SRC, uri
from test_bridge_slice import published
from test_editor_slice import attached
from test_formatting import apply_edits
from test_navigation import at

KOTLIN = f"{SRC}/probe/InspectionWarning.kt"
JAVA = f"{SRC}/probe/JavaShapes.java"

UNNECESSARY = 1  # LSP DiagnosticTag.Unnecessary. DiagnosticTag.Deprecated (2) is a separate, distinct
# IntelliJ colour (strikethrough, not grey) and is not sent - see test_deprecated_usage_is_not_tagged_unnecessary.

KOTLIN_UNUSED = '''package dev.bridge.fixture.probe

import java.io.File
import java.util.Date

class Greeter {
    fun greet(name: String): String {
        val unused = 5
        return "hi " + name
    }
    @Deprecated("old")
    fun oldOne() = 1
    fun useOld() = oldOne()
}
'''

JAVA_UNUSED = '''package dev.bridge.fixture.probe;

import java.util.Date;

public class Greeted {
    @Deprecated
    void oldOne() {}
    void run() {
        int unused = 5;
        oldOne();
    }
}
'''

# A private declaration with zero usages: "is never used", greyed the same way as an unused
# import or local, but - unlike them - with no code action to remove it (see TestNeverUsed).
KOTLIN_NEVER_USED = '''package dev.bridge.fixture.probe

private class Lockbox {
    fun open() = 1
}
'''

JAVA_NEVER_USED = '''package dev.bridge.fixture.probe;

class Lockbox {
    private int open() { return 1; }
}
'''

# Explicit type arguments IntelliJ can infer: also greyed out (the same NOT_USED_ELEMENT_ATTRIBUTES
# colour), and - unlike "is never used" - each has a genuine quick fix (not a refactoring).
KOTLIN_TYPE_ARGS = '''package dev.bridge.fixture.probe

class Box<T>(val value: T)

fun make(): Box<String> {
    return Box<String>("hi")
}
'''

JAVA_TYPE_ARGS = '''package dev.bridge.fixture.probe;

import java.util.ArrayList;
import java.util.List;

public class Boxes {
    public List<String> make() {
        List<String> list = new ArrayList<String>();
        list.add("hi");
        return list;
    }
}
'''


def by_message(diagnostics, needle: str) -> dict:
    """The one diagnostic whose message contains `needle`, or the first if several share the text
    (Kotlin's "Unused import directive" names no import, so both unused imports read the same)."""
    found = [d for d in diagnostics if needle in d["message"]]
    assert found, f"no diagnostic matched {needle!r}: {[d['message'] for d in diagnostics]}"
    return found[0]


def all_tags(diagnostics, needle: str) -> list:
    """The `tags` of every diagnostic matching `needle` (order-independent, for when several share
    the message text)."""
    return [d.get("tags", []) for d in diagnostics if needle in d["message"]]


def tagged(diagnostics, needle: str) -> list:
    return by_message(diagnostics, needle).get("tags", [])


class TestOnTheWire:

    def test_kotlin_unused_import_and_variable_are_marked_unnecessary(self, wire):
        wire.did_open(KOTLIN, KOTLIN_UNUSED)
        pub = published(wire, KOTLIN, lambda d: any("Unused import" in x["message"] for x in d))
        diagnostics = pub["diagnostics"]
        assert all_tags(diagnostics, "Unused import directive") == [[UNNECESSARY], [UNNECESSARY]]
        assert tagged(diagnostics, "Unused variable") == [UNNECESSARY]

    def test_java_unused_import_and_variable_are_marked_unnecessary(self, wire):
        wire.did_open(JAVA, JAVA_UNUSED)
        pub = published(wire, JAVA, lambda d: any("Unused import" in x["message"] for x in d))
        diagnostics = pub["diagnostics"]
        assert tagged(diagnostics, "Unused import statement") == [UNNECESSARY]
        assert tagged(diagnostics, "Variable 'unused' is never used") == [UNNECESSARY]

    def test_an_unused_private_declaration_is_marked_unnecessary_too(self, wire):
        wire.did_open(KOTLIN, KOTLIN_UNUSED)
        pub = published(wire, KOTLIN, lambda d: any("never used" in x["message"] for x in d))
        never_used = [d for d in pub["diagnostics"] if "never used" in d["message"]]
        assert never_used and all(d.get("tags") == [UNNECESSARY] for d in never_used), never_used

    def test_deprecated_usage_is_not_tagged_unnecessary(self, wire):
        """Deprecated (strikethrough) is a different IntelliJ colour from unused (grey): the two must
        not be confused. Not asserting DiagnosticTag.Deprecated is sent - only that Unnecessary is not."""
        wire.did_open(KOTLIN, KOTLIN_UNUSED)
        pub = published(wire, KOTLIN, lambda d: any("is deprecated" in x["message"] for x in d))
        deprecated = by_message(pub["diagnostics"], "is deprecated")
        assert UNNECESSARY not in deprecated.get("tags", [])

    def test_an_ordinary_error_carries_no_tags(self, wire, bridge_container):
        from test_bridge_slice import RESOLUTION, unresolved
        text = bridge_container.read_file(RESOLUTION)
        wire.did_open(RESOLUTION, text)
        pub = published(wire, RESOLUTION, lambda d: len(unresolved(d)) >= 1)
        assert all("tags" not in d for d in unresolved(pub["diagnostics"]))

    def test_it_stops_being_marked_once_the_import_is_used(self, wire):
        wire.did_open(KOTLIN, KOTLIN_UNUSED)
        published(wire, KOTLIN, lambda d: any("Unused import" in x["message"] for x in d))
        used = KOTLIN_UNUSED.replace(
            'return "hi " + name', 'return File(name).name + Date().toString()')
        wire.did_change(KOTLIN, 1, {"text": used})
        published(wire, KOTLIN, lambda d: not any("Unused import" in x["message"] for x in d))

    def test_kotlin_a_never_used_class_and_method_are_marked_unnecessary(self, wire):
        wire.did_open(KOTLIN, KOTLIN_NEVER_USED)
        pub = published(wire, KOTLIN, lambda d: any("never used" in x["message"] for x in d))
        assert tagged(pub["diagnostics"], 'Class "Lockbox" is never used') == [UNNECESSARY]
        assert tagged(pub["diagnostics"], 'Function "open" is never used') == [UNNECESSARY]

    def test_java_a_never_used_class_and_method_are_marked_unnecessary(self, wire):
        wire.did_open(JAVA, JAVA_NEVER_USED)
        pub = published(wire, JAVA, lambda d: any("never used" in x["message"] for x in d))
        assert tagged(pub["diagnostics"], "Class 'Lockbox' is never used") == [UNNECESSARY]
        assert tagged(pub["diagnostics"], "Private method 'open()' is never used") == [UNNECESSARY]

    def test_kotlin_a_redundant_type_argument_is_marked_unnecessary(self, wire):
        wire.did_open(KOTLIN, KOTLIN_TYPE_ARGS)
        pub = published(wire, KOTLIN, lambda d: any("can be inferred" in x["message"] for x in d))
        assert tagged(pub["diagnostics"], "Explicit type arguments can be inferred") == [UNNECESSARY]

    def test_java_a_redundant_type_argument_is_marked_unnecessary(self, wire):
        wire.did_open(JAVA, JAVA_TYPE_ARGS)
        pub = published(wire, JAVA, lambda d: any("can be replaced with <>" in x["message"] for x in d))
        assert tagged(pub["diagnostics"], "can be replaced with <>") == [UNNECESSARY]


class TestFixingIt:
    """The point of greying it out: there is something to do about it. IntelliJ's own quick fix
    removes it, offered as an ordinary code action (FEATURES.md §5f), resolved without touching
    the Mirror or the disk (D2), same as any other code action."""

    def pos(self, text, needle, into=0):
        line, col = at(text, needle, 0, into)
        return {"line": line, "character": col}

    def offer(self, w, path, text, needle, into, title, timeout=60):
        p = self.pos(text, needle, into)
        deadline = time.monotonic() + timeout
        while True:
            found = w.request("textDocument/codeAction", {
                "textDocument": {"uri": uri(path)}, "range": {"start": p, "end": p},
                "context": {"diagnostics": []}}, timeout=90)
            match = [a for a in found if title in a["title"]]
            if match or time.monotonic() > deadline:
                assert match, f"{title!r} never offered at {needle!r}; got {[a['title'] for a in found]}"
                return match[0]
            time.sleep(2)

    def test_the_unused_variable_has_a_remove_quick_fix(self, wire):
        wire.did_open(KOTLIN, KOTLIN_UNUSED)
        published(wire, KOTLIN, lambda d: tagged(d, "Unused variable") == [UNNECESSARY])
        action = self.offer(wire, KOTLIN, KOTLIN_UNUSED, "val unused", 6, "Remove variable 'unused'")
        assert action["kind"] == "quickfix"
        resolved = wire.request("codeAction/resolve", action, timeout=90)
        out = apply_edits(KOTLIN_UNUSED, resolved["edit"]["changes"][uri(KOTLIN)])
        assert "val unused" not in out and 'return "hi " + name' in out, out

    def test_the_unused_import_is_removed_by_organize_imports(self, wire):
        """No per-import quick fix exists; Organize Imports is IntelliJ's own answer, already built."""
        wire.did_open(KOTLIN, KOTLIN_UNUSED)
        published(wire, KOTLIN, lambda d: all_tags(d, "Unused import directive") == [[UNNECESSARY], [UNNECESSARY]])
        action = self.offer(wire, KOTLIN, KOTLIN_UNUSED, "import java.io.File", 3, "Organize imports")
        resolved = wire.request("codeAction/resolve", action, timeout=90)
        out = apply_edits(KOTLIN_UNUSED, resolved["edit"]["changes"][uri(KOTLIN)])
        assert "import java.io.File" not in out and "import java.util.Date" not in out, out

    def test_java_unused_variable_has_a_remove_quick_fix(self, wire):
        wire.did_open(JAVA, JAVA_UNUSED)
        published(wire, JAVA, lambda d: tagged(d, "Variable 'unused' is never used") == [UNNECESSARY])
        action = self.offer(wire, JAVA, JAVA_UNUSED, "int unused", 6, "Remove local variable 'unused'")
        resolved = wire.request("codeAction/resolve", action, timeout=90)
        out = apply_edits(JAVA_UNUSED, resolved["edit"]["changes"][uri(JAVA)])
        assert "unused" not in out, out

    def test_kotlin_redundant_type_argument_has_a_remove_quick_fix(self, wire):
        wire.did_open(KOTLIN, KOTLIN_TYPE_ARGS)
        published(wire, KOTLIN, lambda d: tagged(d, "Explicit type arguments can be inferred") == [UNNECESSARY])
        action = self.offer(wire, KOTLIN, KOTLIN_TYPE_ARGS, "Box<String>(\"hi\")", 4, "Remove explicit type arguments")
        assert action["kind"] == "quickfix"
        resolved = wire.request("codeAction/resolve", action, timeout=90)
        out = apply_edits(KOTLIN_TYPE_ARGS, resolved["edit"]["changes"][uri(KOTLIN)])
        assert 'return Box("hi")' in out, out
        assert out.count("Box<String>") == 1, "only the declared return type keeps it; the call site's is gone: " + out

    def test_java_diamond_quick_fix_removes_the_redundant_type_argument(self, wire):
        wire.did_open(JAVA, JAVA_TYPE_ARGS)
        published(wire, JAVA, lambda d: tagged(d, "can be replaced with <>") == [UNNECESSARY])
        action = self.offer(wire, JAVA, JAVA_TYPE_ARGS, "new ArrayList<String>()", 14, "Replace with <>")
        assert action["kind"] == "quickfix"
        resolved = wire.request("codeAction/resolve", action, timeout=90)
        out = apply_edits(JAVA_TYPE_ARGS, resolved["edit"]["changes"][uri(JAVA)])
        assert "new ArrayList<>()" in out and "new ArrayList<String>()" not in out, out


class TestNeverUsed:
    """"is never used" (an unused private class, method or field) is greyed out the same way as an
    unused import or local (TestOnTheWire, above) - but, unlike them, **has no code action to remove
    it today**. What actually removes it in IntelliJ is Safe Delete, and Safe Delete is a refactoring
    (`SafeDeleteHandler`), not an intention: it never appears in `ShowIntentionsPass.getActionsToShow`,
    which is what every code action in this project is built on (CodeActions.kt). Confirmed by asking
    for code actions at the diagnostic's own range and finding no quick fix among them - only Organize
    Imports and unrelated intentions. Safe Delete is on the roadmap (FEATURES.md, decision D2: build its
    edits from the refactoring's own UsageInfo, "usages check first") but is not built; this class is
    the marker for that gap, so it fails loudly, on purpose, the day someone starts implementing it -
    at which point it should be rewritten as a TestFixingIt case, not deleted."""

    def test_it_is_still_marked_unnecessary_though_nothing_can_fix_it_yet(self, wire):
        wire.did_open(KOTLIN, KOTLIN_NEVER_USED)
        pub = published(wire, KOTLIN, lambda d: any("never used" in x["message"] for x in d))
        assert tagged(pub["diagnostics"], 'Function "open" is never used') == [UNNECESSARY]

    def test_no_quick_fix_removes_a_never_used_kotlin_declaration_yet(self, wire):
        wire.did_open(KOTLIN, KOTLIN_NEVER_USED)
        d = published(wire, KOTLIN, lambda d: any("never used" in x["message"] for x in d))
        never_used = by_message(d["diagnostics"], 'Function "open" is never used')
        actions = wire.request("textDocument/codeAction", {
            "textDocument": {"uri": uri(KOTLIN)}, "range": never_used["range"],
            "context": {"diagnostics": []}}, timeout=90)
        assert not [a for a in actions if a["kind"] == "quickfix"], (
            "a quick fix now removes a never-used declaration: promote this gap to a real feature "
            f"(FEATURES.md, D2) and rewrite this as a TestFixingIt case - found {actions}")

    def test_no_quick_fix_removes_a_never_used_java_declaration_yet(self, wire):
        wire.did_open(JAVA, JAVA_NEVER_USED)
        d = published(wire, JAVA, lambda d: any("never used" in x["message"] for x in d))
        never_used = by_message(d["diagnostics"], "Private method 'open()' is never used")
        actions = wire.request("textDocument/codeAction", {
            "textDocument": {"uri": uri(JAVA)}, "range": never_used["range"],
            "context": {"diagnostics": []}}, timeout=90)
        assert not [a for a in actions if a["kind"] == "quickfix"], (
            "a quick fix now removes a never-used declaration: promote this gap to a real feature "
            f"(FEATURES.md, D2) and rewrite this as a TestFixingIt case - found {actions}")


class TestThroughNeovim:
    """Neovim's own vim.diagnostic renders DiagnosticTag.Unnecessary as the `DiagnosticUnnecessary`
    highlight (dim/greyed by default), and offers the same code action through the normal menu."""

    def test_neovim_marks_the_diagnostic_unnecessary(self, nvim):
        nvim.command(f"edit {KOTLIN}")
        wait_until(lambda: attached(nvim) == 1, message="never attached")
        nvim.current.buffer[:] = KOTLIN_UNUSED.rstrip("\n").split("\n")

        def unused_import_diag():
            for d in nvim.exec_lua("return vim.diagnostic.get(0)"):
                if "Unused import" in d.get("message", ""):
                    return d
            return None
        wait_until(lambda: unused_import_diag() is not None, timeout=60, interval=2,
                   message="Neovim never received the unused-import diagnostic")
        diag = unused_import_diag()
        # Neovim keeps the server's raw tags on the LSP-sourced diagnostic's user_data.
        tags = nvim.exec_lua("return (...).user_data and (...).user_data.lsp and (...).user_data.lsp.tags or {}", diag)
        assert UNNECESSARY in tags, diag

    def test_the_remove_quick_fix_works_through_code_action(self, nvim):
        nvim.command(f"edit {KOTLIN}")
        wait_until(lambda: attached(nvim) == 1, message="never attached")
        nvim.current.buffer[:] = KOTLIN_UNUSED.rstrip("\n").split("\n")
        line, col = at(KOTLIN_UNUSED, "val unused", 0, 6)
        nvim.current.window.cursor = (line + 1, col)

        def done():
            nvim.exec_lua("""vim.lsp.buf.code_action({
                filter = function(a) return a.title:find(\"Remove variable\", 1, true) ~= nil end, apply = true })""")
            time.sleep(3)
            return "val unused" not in "\n".join(nvim.current.buffer[:])
        wait_until(done, timeout=90, interval=2, message="the quick fix was never applied:\n" + "\n".join(nvim.current.buffer[:]))
