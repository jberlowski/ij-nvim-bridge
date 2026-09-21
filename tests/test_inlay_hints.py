"""Inlay hints (FEATURES.md §4): `textDocument/inlayHint`.

Harvested from the inlays IntelliJ itself puts on the Mirror's editor, so they are the developer's
IDE's own hints, by the developer's own settings, for the unsaved text.
"""
from __future__ import annotations

import time

import pytest

from harness.util import wait_until
from harness.wire import SRC, replace_range, uri
from test_editor_slice import attached

KT = f"{SRC}/probe/InspectionWarning.kt"
JV = f"{SRC}/probe/JavaShapes.java"

KOTLIN = '''package dev.bridge.fixture.probe

class Q {
    fun area(width: Int, height: Int, scale: Double = 1.0): Double = width * height * scale
    fun run() {
        val a = area(3, 4)
        val b = area(width = 1, height = 2, scale = 2.0)
        println(a + b)
    }
}
'''

JAVA = '''package dev.bridge.fixture.probe;

public class Q {
    static double area(int width, int height, double scale) { return width * height * scale; }
    public void run() {
        double a = area(3, 4, 1.5);
        System.out.println(a);
    }
}
'''


def pos(text: str, needle: str, into: int = 0) -> dict:
    idx = text.index(needle) + into
    return {"line": text.count("\n", 0, idx), "character": idx - (text.rfind("\n", 0, idx) + 1)}


def label(hint) -> str:
    return hint["label"] if isinstance(hint["label"], str) else "".join(p["value"] for p in hint["label"])


def hints(w, path, rng=None) -> list[dict]:
    params = {"textDocument": {"uri": uri(path)}}
    params["range"] = rng or {"start": {"line": 0, "character": 0}, "end": {"line": 10_000, "character": 0}}
    return w.request("textDocument/inlayHint", params, timeout=60)


def hints_until(w, path, want, rng=None, timeout=60) -> list[dict]:
    """IntelliJ's passes finish a moment after a file is opened: ask again, as the client is told to."""
    deadline = time.monotonic() + timeout
    while True:
        got = hints(w, path, rng)
        if want(got) or time.monotonic() > deadline:
            assert want(got), f"the hints never arrived; last: {[(h['position'], label(h)) for h in got]}"
            return got
        time.sleep(1.5)


class TestOnTheWire:

    def test_it_is_advertised(self, bridge):
        from harness.wire import Wire
        with Wire(bridge.port) as w:
            assert w.initialize()["capabilities"]["inlayHintProvider"] == {"resolveProvider": False}

    def test_kotlin_parameter_names_at_the_arguments(self, wire):
        wire.did_open(KT, KOTLIN)
        got = hints_until(wire, KT, lambda h: len(h) >= 2)
        by_position = {(h["position"]["line"], h["position"]["character"]): h for h in got}
        three, four = pos(KOTLIN, "area(3, 4)", 5), pos(KOTLIN, "area(3, 4)", 8)
        first, second = by_position[(three["line"], three["character"])], by_position[(four["line"], four["character"])]
        assert "width" in label(first) and "height" in label(second), (label(first), label(second))
        assert first["kind"] == 2 and first.get("paddingRight") is True

    def test_named_arguments_need_no_hint(self, wire):
        wire.did_open(KT, KOTLIN)
        got = hints_until(wire, KT, lambda h: len(h) >= 2)
        named = pos(KOTLIN, "area(width = 1")["line"]
        assert not [h for h in got if h["position"]["line"] == named], "the names are already written there"

    def test_java_parameter_names_at_the_arguments(self, wire):
        wire.did_open(JV, JAVA)
        got = hints_until(wire, JV, lambda h: len(h) >= 3)
        assert [label(h) for h in got] == ["width:", "height:", "scale:"], [label(h) for h in got]
        assert [h["position"] for h in got] == [pos(JAVA, "area(3, 4, 1.5)", n) for n in (5, 8, 11)]
        assert all(h["kind"] == 2 for h in got)

    def test_a_range_gets_only_the_hints_inside_it(self, wire):
        wire.did_open(JV, JAVA)
        hints_until(wire, JV, lambda h: len(h) >= 3)
        line = pos(JAVA, "area(3, 4, 1.5)")["line"]
        inside = hints(wire, JV, {"start": {"line": line, "character": 0}, "end": {"line": line + 1, "character": 0}})
        assert len(inside) == 3
        outside = hints(wire, JV, {"start": {"line": 0, "character": 0}, "end": {"line": line, "character": 0}})
        assert outside == []

    def test_an_unsaved_edit_gets_its_own_hints(self, wire):
        wire.did_open(JV, JAVA)
        hints_until(wire, JV, lambda h: len(h) >= 3)
        line = pos(JAVA, "System.out")["line"]
        wire.did_change(JV, 1, replace_range(line, 0, 0, "        area(7, 8, 9.0);\n"))
        got = hints_until(wire, JV, lambda h: len(h) >= 6)
        assert [label(h) for h in got if h["position"]["line"] == line] == ["width:", "height:", "scale:"]

    def test_the_client_is_told_to_ask_again_when_the_hints_change(self, wire):
        """The passes finish after the file is opened, and again after an edit: without this the
        client would ask once, too early, and show nothing until the next keystroke."""
        wire.did_open(JV, JAVA)
        deadline = time.monotonic() + 60
        seen = None
        while seen is None and time.monotonic() < deadline:
            seen = next((m for m in wire.inbox if m.get("method") == "workspace/inlayHint/refresh"), None)
            if seen is None:
                msg = wire.read(5)
                if msg.get("method") == "workspace/inlayHint/refresh":
                    seen = msg
                else:
                    wire.inbox.append(msg)
        assert seen is not None, "the Brain never asked the client to ask again"
        assert "id" in seen and "params" not in seen, "a request with no parameters, as LSP has it"

    def test_a_request_changes_neither_the_mirror_nor_the_disk(self, wire, bridge_container):
        on_disk = bridge_container.read_file(JV)
        wire.did_open(JV, JAVA)
        hints_until(wire, JV, lambda h: len(h) >= 3)
        (m,) = wire.debug_state(text=True)["mirrors"]
        assert m["text"] == JAVA and m["version"] == 0
        assert bridge_container.read_file(JV) == on_disk


class TestThroughNeovimsBuiltins:

    def enable(self, nvim):
        nvim.command(f"edit {JV}")
        wait_until(lambda: attached(nvim) == 1, message="never attached")
        nvim.current.buffer[:] = JAVA.rstrip("\n").split("\n")
        nvim.exec_lua("vim.lsp.inlay_hint.enable(true, { bufnr = 0 })")

    def shown(self, nvim) -> list[str]:
        return nvim.exec_lua("""
            local out = {}
            for _, h in ipairs(vim.lsp.inlay_hint.get({ bufnr = 0 })) do
              local label = h.inlay_hint.label
              if type(label) == 'table' then label = table.concat(vim.tbl_map(function(p) return p.value end, label)) end
              table.insert(out, h.inlay_hint.position.line .. ':' .. label)
            end
            return out""")

    def test_neovim_shows_intellijs_hints(self, nvim):
        self.enable(nvim)
        line = pos(JAVA, "area(3, 4, 1.5)")["line"]
        wait_until(lambda: self.shown(nvim) == [f"{line}:width:", f"{line}:height:", f"{line}:scale:"],
                   timeout=60, message="Neovim never showed the hints: " + str(self.shown(nvim)))

    def test_they_follow_an_edit_without_being_asked_for_again(self, nvim):
        """The Brain's `workspace/inlayHint/refresh` makes Neovim ask; nothing here does."""
        self.enable(nvim)
        wait_until(lambda: len(self.shown(nvim)) == 3, timeout=60)
        line = pos(JAVA, "System.out")["line"]
        nvim.current.buffer.append("        area(7, 8, 9.0);", line)
        wait_until(lambda: len(self.shown(nvim)) == 6, timeout=60,
                   message="the new call never got its hints: " + str(self.shown(nvim)))
