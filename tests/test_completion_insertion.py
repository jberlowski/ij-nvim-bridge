"""Accepting a completion item (FEATURES.md §3): `completionItem/resolve`.

The Brain runs IntelliJ's own insert handler on a copy and returns what changed:
`textEdit` (the word and what it grew into), `additionalTextEdits` (imports) and,
when IntelliJ leaves the caret inside the insertion, a snippet `$0`.
"""
from __future__ import annotations

import time

import pytest

from harness.util import wait_until
from harness.wire import SRC, RpcError, all_items, replace_range, uri
from test_formatting import apply_edits
from test_navigation import attached

PROBE = f"{SRC}/probe"
KOTLIN = f"{PROBE}/InspectionWarning.kt"
JAVA = f"{PROBE}/JavaShapes.java"


def position_of(text: str) -> tuple[str, int, int]:
    """`text` with a `|` marking the caret: the text without it, and where it was."""
    before, after = text.split("|")
    line = before.count("\n")
    return before + after, line, len(before.split("\n")[-1])


def offer(w, path, marked, label, package=None):
    text, line, col = position_of(marked)
    w.did_open(path, text)
    # A cold IntelliJ can answer before every class has been indexed: ask again, as a
    # developer typing on would.
    for _ in range(6):
        first, batches = w.complete(path, line, col, timeout=60)
        items = all_items(first, batches)
        found = [i for i in items if i["label"] == label
                 and (package is None or package in i.get("labelDetails", {}).get("detail", ""))]
        if found:
            return text, found[0]
        time.sleep(3)
    raise AssertionError(f"{label!r} not offered: {[(i['label'], i.get('labelDetails')) for i in items][:20]}")


def accept(w, path, marked, label, package=None):
    """Resolve an item and apply everything it says, as a client would."""
    text, item = offer(w, path, marked, label, package)
    resolved = w.request("completionItem/resolve", item, timeout=90)
    edits = [resolved["textEdit"]] + resolved.get("additionalTextEdits", [])
    return apply_edits(text, edits), resolved


KOTLIN_CLASS = '''package dev.bridge.fixture.probe

class Inserts {
    fun id(): Any = UUI|
}
'''

KOTLIN_CALL = '''package dev.bridge.fixture.probe

class Inserts {
    fun size(s: String) = s.subs|
}
'''

JAVA_CLASS = '''package dev.bridge.fixture.probe;

public class JavaInserts {
    public Object id() { return UUI|; }
}
'''

JAVA_CALL = '''package dev.bridge.fixture.probe;

public class JavaInserts {
    public String cut(String s) { return s.subs|; }
}
'''


class TestResolve:

    def test_kotlin_class_gets_its_import(self, wire):
        text, resolved = accept(wire, KOTLIN, KOTLIN_CLASS, "UUID", "java.util")
        assert "import java.util.UUID" in text
        assert "= UUID\n" in text

    def test_java_class_gets_its_import(self, wire):
        text, resolved = accept(wire, JAVA, JAVA_CLASS, "UUID", "java.util")
        assert "import java.util.UUID;" in text
        assert "return UUID;" in text

    def test_kotlin_call_gets_parentheses_and_the_caret_inside(self, wire):
        text, resolved = accept(wire, KOTLIN, KOTLIN_CALL, "substring")
        assert "s.substring($0)" in text
        assert resolved["insertTextFormat"] == 2

    def test_java_call_gets_parentheses_and_the_caret_inside(self, wire):
        text, resolved = accept(wire, JAVA, JAVA_CALL, "substring")
        assert "s.substring($0)" in text
        assert resolved["insertTextFormat"] == 2


KOTLIN_LOCAL = '''package dev.bridge.fixture.probe

class Inserts {
    fun f(): Int {
        val counter = 1
        return coun|
    }
}
'''

KOTLIN_LAMBDA = '''package dev.bridge.fixture.probe

class Inserts {
    fun f(list: List<Int>) = list.forEa|
}
'''


class TestResolveContract:

    def test_a_plain_identifier_is_a_plain_edit(self, wire):
        text, resolved = accept(wire, KOTLIN, KOTLIN_LOCAL, "counter")
        assert resolved["additionalTextEdits"] == []
        assert resolved["insertTextFormat"] == 1
        assert resolved["textEdit"]["newText"] == "counter"
        assert "return counter\n" in text

    def test_a_lambda_call_gets_its_braces(self, wire):
        text, resolved = accept(wire, KOTLIN, KOTLIN_LAMBDA, "forEach")
        assert resolved["insertTextFormat"] == 2, resolved["textEdit"]
        assert "list.forEach {" in text and "$0" in text, text

    def test_the_item_is_the_answer_to_the_offer_it_came_from(self, wire):
        """The Editor caches answers: a backspace returns to a text an item was
        offered for, and the item must still resolve there."""
        text, item = offer(wire, KOTLIN, KOTLIN_CLASS, "UUID", "java.util")
        line = text.split("\n")[3]
        wire.did_change(KOTLIN, 1, replace_range(3, len(line), len(line), "x"))
        wire.did_change(KOTLIN, 2, replace_range(3, len(line), len(line) + 1, ""))
        resolved = wire.request("completionItem/resolve", item, timeout=90)
        assert resolved["additionalTextEdits"], resolved

    def test_more_of_the_word_typed_since_does_not_stale_the_item(self, wire):
        """An answer for `UUI` is right for `UUID`: the developer typed on while it was on screen."""
        text, item = offer(wire, KOTLIN, KOTLIN_CLASS, "UUID", "java.util")
        line = text.split("\n")[3]
        wire.did_change(KOTLIN, 1, replace_range(3, len(line), len(line), "D"))
        resolved = wire.request("completionItem/resolve", item, timeout=90)
        assert resolved["textEdit"]["newText"] == "UUID"
        assert resolved["textEdit"]["range"]["end"]["character"] == len(line) + 1
        assert resolved["additionalTextEdits"]

    def test_an_item_for_text_that_has_changed_is_content_modified(self, wire):
        text, item = offer(wire, KOTLIN, KOTLIN_CLASS, "UUID", "java.util")
        wire.did_change(KOTLIN, 1, replace_range(0, 0, 0, "// edited\n"))
        with pytest.raises(RpcError) as e:
            wire.request("completionItem/resolve", item, timeout=30)
        assert e.value.code == -32801

    def test_an_item_the_brain_never_offered_is_content_modified(self, wire):
        wire.did_open(KOTLIN, KOTLIN_CLASS.replace("|", ""))
        with pytest.raises(RpcError) as e:
            wire.request("completionItem/resolve", {
                "label": "UUID", "data": {"uri": uri(KOTLIN), "id": "nope.0"}}, timeout=30)
        assert e.value.code == -32801

    def test_a_resolve_changes_neither_the_mirror_nor_the_disk(self, wire, bridge_container):
        on_disk = bridge_container.read_file(KOTLIN)
        text, _ = accept(wire, KOTLIN, KOTLIN_CLASS, "UUID", "java.util")
        (m,) = wire.debug_state(text=True)["mirrors"]
        assert m["text"] == KOTLIN_CLASS.replace("|", "") and m["version"] == 0
        assert bridge_container.read_file(KOTLIN) == on_disk


# ============================================ through Neovim, with blink.cmp itself
def blink_accept(nvim, path, before_caret, after_caret, label, wait_for_prefetch=True, package="", brain=None):
    """Type `before_caret` in insert mode, let blink.cmp show IntelliJ's items,
    accept `label` as a developer would, and return the buffer."""
    nvim.command(f"edit {path}")
    wait_until(lambda: attached(nvim) == 1, message="never attached")
    lines = KOTLIN_TEMPLATE.replace("<<line>>", before_caret + after_caret).split("\n")
    nvim.current.buffer[:] = lines
    row = next(i for i, l in enumerate(lines) if before_caret + after_caret in l)
    stem = before_caret[:-3] if before_caret.endswith(("UUI", "sub")) else before_caret
    nvim.current.buffer[row] = lines[row].replace(before_caret + after_caret, stem + after_caret)
    nvim.current.window.cursor = (row + 1, len(lines[row]) - len(before_caret + after_caret) + len(stem))
    nvim.feedkeys(nvim.replace_termcodes("a" + before_caret[len(stem):]), "n", False)

    def index_of_label():
        return nvim.exec_lua(f"""
            local items = require('blink.cmp.completion.list').items or {{}}
            for i, item in ipairs(items) do
              local detail = item.labelDetails and item.labelDetails.detail or ''
              if item.label == '{label}' and detail:find('{package}', 1, true) then return i end
            end
            return nil""")
    wait_until(lambda: nvim.exec_lua("return require('blink.cmp').is_menu_visible()") and index_of_label(),
               timeout=60, message=f"{label!r} never reached blink's list")
    # A developer reads the menu before pressing a key: by then IntelliJ's answer for the
    # text as it now is has arrived, and the list is that answer, not one for a word typed a
    # moment ago. Wait for the list to hold still.
    settled = {"seen": None, "since": time.monotonic()}

    def list_is_still():
        now = nvim.exec_lua("""
            local items = require('blink.cmp.completion.list').items or {}
            local first = items[1] and items[1].data and items[1].data.id or ''
            return #items .. ':' .. first""")
        if now != settled["seen"]:
            settled["seen"], settled["since"] = now, time.monotonic()
        return time.monotonic() - settled["since"] > 1.5
    wait_until(list_is_still, timeout=60, interval=0.3, message="blink's list never settled")
    wait_until(index_of_label, timeout=30, message=f"{label!r} vanished from blink's list")
    # As arrowing to the item would.
    nvim.exec_lua(f"require('blink.cmp.completion.list').select({index_of_label()})")
    if wait_for_prefetch:
        # blink.cmp resolves the highlighted item ahead of time (its `prefetch`), so by the
        # time a developer presses Enter it has been answered. A person takes far longer
        # than that; a script must be told to.
        wait_until(lambda: nvim.exec_lua(f"""
            for _, r in ipairs(require('ij_bridge.blink').resolves) do
              if r.label == '{label}' and r.ms then return true end
            end
            return false"""), timeout=30, message="blink never resolved the highlighted item")
    nvim.exec_lua(f"""
        _G.accept_result = {{}}
        local ok, err = pcall(function()
          return require('blink.cmp').accept({{ index = {index_of_label()},
            callback = function() _G.accept_result.done = true end }})
        end)
        _G.accept_result.ok = ok; _G.accept_result.err = tostring(err)""")


KOTLIN_TEMPLATE = '''package dev.bridge.fixture.probe

class Inserts {
<<line>>
}
'''


class TestThroughBlink:

    def test_accepting_before_the_answer_arrives_inserts_the_plain_word(self, nvim, probe):
        """blink.cmp gives resolve 100 ms by default. Past that it carries on with the
        item as it is: the word, no import. Degraded, never broken."""
        probe.request("$/ij/debug/navigationDelay", {"ms": 3000})
        try:
            blink_accept(nvim, KOTLIN, "    fun id(): Any = UUI", "", "UUID", wait_for_prefetch=False,
                         package="java.util")
            wait_until(lambda: "= UUID" in "\n".join(nvim.current.buffer[:]), timeout=30)
            assert "import" not in "\n".join(nvim.current.buffer[:])
        finally:
            probe.request("$/ij/debug/navigationDelay", {"ms": 0})

    def test_accepting_a_class_adds_the_import(self, nvim):
        blink_accept(nvim, KOTLIN, "    fun id(): Any = UUI", "", "UUID", package="java.util")
        try:
            wait_until(lambda: "import java.util.UUID" in "\n".join(nvim.current.buffer[:]), timeout=30)
        except AssertionError as e:
            raise AssertionError(
                "\n".join(nvim.current.buffer[:]) + "\nRESOLVES " + str(nvim.exec_lua("return require('ij_bridge.blink').resolves"))
                + "\nMESSAGES " + nvim.command_output("messages")
                + "\nACCEPT " + str(nvim.exec_lua("return _G.accept_result"))) from e
        assert "= UUID" in "\n".join(nvim.current.buffer[:])

    def test_accepting_a_call_adds_parentheses_with_the_caret_inside(self, nvim, probe):
        blink_accept(nvim, KOTLIN, "    fun size(s: String) = s.sub", "", "substring", brain=probe)
        try:
            wait_until(lambda: "s.substring(" in "\n".join(nvim.current.buffer[:]), timeout=30)
        except AssertionError as e:
            raise AssertionError("\n".join(nvim.current.buffer[:]) + "\nRESOLVES " + str(
                nvim.exec_lua("return require('ij_bridge.blink').resolves"))) from e
        row, col = nvim.current.window.cursor
        line = nvim.current.buffer[row - 1]
        assert line.endswith("s.substring()") and col == len(line) - 1, (line, col)
