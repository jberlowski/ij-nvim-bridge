"""Inline variable (FEATURES.md's extract/inline/move): `refactor.inline` code actions.

Java only so far. `InlineLocalHandler` is not an intention - it is a refactoring handler, invoked
from its own shortcut, not `ShowIntentionsPass` - but reproducing against the real IDE found it is
already `ModCommand`-based (`InlineLocalHandler.doInline`), so it reuses the exact edit-diffing
machinery an intention's `ModCommand` already does, needing no new edit-computation code. Kotlin's
own inline (`KotlinInlinePropertyProcessor`) is the older kind that mutates PSI directly when run;
tried against a scratch copy (the same pattern `source.generate.*` already uses successfully), it
produced no change at all - undocumented internal behaviour, not yet worth its own spike (D2).
"""
from __future__ import annotations

from harness.wire import SRC, uri
from test_formatting import apply_edits
from test_navigation import at, mirror, ready

MULT = f"{SRC}/probe/Multiplier.java"
CALC = f"{SRC}/probe/Calculator.kt"

JAVA_ONE_USAGE = """package dev.bridge.fixture.probe;

public class Multiplier {
    public int multiply(int a, int b) {
        int product = a * b;
        return product;
    }
}
"""

JAVA_TWO_USAGES = """package dev.bridge.fixture.probe;

public class Multiplier {
    public int multiply(int a, int b) {
        int product = a * b;
        System.out.println(product);
        return product;
    }
}
"""

KOTLIN_LOCAL = """package dev.bridge.fixture.probe

class Calculator {
    fun add(a: Int, b: Int): Int {
        val sum = a + b
        println(sum)
        return sum
    }
}
"""


def actions_at(w, path, pos):
    return w.request("textDocument/codeAction", {
        "textDocument": {"uri": uri(path)},
        "range": {"start": {"line": pos[0], "character": pos[1]}, "end": {"line": pos[0], "character": pos[1]}},
        "context": {"diagnostics": []},
    }, timeout=30)


def inline_offer(actions):
    return next((a for a in actions if a["kind"] == "refactor.inline"), None)


class TestOffered:
    def test_at_the_declaration(self, bridge_container, wire):
        ready(wire)
        mirror(wire, bridge_container, MULT)
        wire.did_change(MULT, 1, {"text": JAVA_ONE_USAGE})
        pos = at(JAVA_ONE_USAGE, "product =", 0, 0)
        offer = inline_offer(actions_at(wire, MULT, pos))
        assert offer and offer["title"] == "Inline variable 'product'"

    def test_at_a_usage_too(self, bridge_container, wire):
        ready(wire)
        mirror(wire, bridge_container, MULT)
        wire.did_change(MULT, 1, {"text": JAVA_ONE_USAGE})
        pos = at(JAVA_ONE_USAGE, "product;", 0, 0)  # `return product;`
        offer = inline_offer(actions_at(wire, MULT, pos))
        assert offer and offer["title"] == "Inline variable 'product'"

    def test_kotlin_is_not_offered_yet(self, bridge_container, wire):
        """A deliberate gap marker (D2, FEATURES.md), not an oversight: Kotlin's own inline processor
        mutates PSI directly and produced no change at all against a scratch copy, undocumented
        internal behaviour not yet worth its own spike. Named to fail loudly the day someone builds
        it, at which point this becomes a TestOffered/TestResolving case like Java's."""
        ready(wire)
        mirror(wire, bridge_container, CALC)
        wire.did_change(CALC, 1, {"text": KOTLIN_LOCAL})
        pos = at(KOTLIN_LOCAL, "sum =", 0, 0)
        assert inline_offer(actions_at(wire, CALC, pos)) is None


class TestResolving:
    def test_a_single_usage_is_replaced_and_the_declaration_removed(self, bridge_container, wire):
        ready(wire)
        mirror(wire, bridge_container, MULT)
        wire.did_change(MULT, 1, {"text": JAVA_ONE_USAGE})
        pos = at(JAVA_ONE_USAGE, "product =", 0, 0)
        offer = inline_offer(actions_at(wire, MULT, pos))
        resolved = wire.request("codeAction/resolve", offer, timeout=30)
        edits = resolved["edit"]["changes"][uri(MULT)]
        result = apply_edits(JAVA_ONE_USAGE, edits)
        assert "int product" not in result
        assert "return a * b;" in result

    def test_every_usage_is_replaced(self, bridge_container, wire):
        """Two usages: both become `a * b`, not just the one the action was asked about."""
        ready(wire)
        mirror(wire, bridge_container, MULT)
        wire.did_change(MULT, 1, {"text": JAVA_TWO_USAGES})
        pos = at(JAVA_TWO_USAGES, "product =", 0, 0)
        offer = inline_offer(actions_at(wire, MULT, pos))
        resolved = wire.request("codeAction/resolve", offer, timeout=30)
        edits = resolved["edit"]["changes"][uri(MULT)]
        result = apply_edits(JAVA_TWO_USAGES, edits)
        assert "product" not in result
        assert result.count("a * b") == 2

    def test_resolving_from_a_usage_gives_the_same_edit_as_from_the_declaration(self, bridge_container, wire):
        ready(wire)
        mirror(wire, bridge_container, MULT)
        wire.did_change(MULT, 1, {"text": JAVA_ONE_USAGE})
        from_decl = inline_offer(actions_at(wire, MULT, at(JAVA_ONE_USAGE, "product =", 0, 0)))
        from_usage = inline_offer(actions_at(wire, MULT, at(JAVA_ONE_USAGE, "product;", 0, 0)))
        resolved_decl = wire.request("codeAction/resolve", from_decl, timeout=30)
        resolved_usage = wire.request("codeAction/resolve", from_usage, timeout=30)
        assert resolved_decl["edit"] == resolved_usage["edit"]


class TestThroughNeovim:
    def test_the_edit_applies_through_code_action_apply(self, nvim, bridge_container):
        from test_navigation import attached
        from harness.util import wait_until

        nvim.command(f"edit {MULT}")
        wait_until(lambda: attached(nvim) == 1, message="never attached")
        nvim.current.buffer[:] = JAVA_ONE_USAGE.rstrip("\n").split("\n")
        line, col = at(JAVA_ONE_USAGE, "product =", 0, 0)
        nvim.current.window.cursor = (line + 1, col)

        def done():
            nvim.exec_lua("""vim.lsp.buf.code_action({ context = { only = { 'refactor.inline' } },
                apply = true })""")
            return "int product" not in "\n".join(nvim.current.buffer[:])
        wait_until(done, timeout=30, interval=2, message="the variable was never inlined:\n" + "\n".join(nvim.current.buffer[:]))
