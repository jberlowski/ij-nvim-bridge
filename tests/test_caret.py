"""Caret following, both ways, opt-in (FEATURES.md §6d; Carets.kt).

Off until the Editor says `$/ij/follow { enabled: true }`. Then the Editor's cursor (`$/ij/caret` to the Brain) moves
the Mirror's caret, and the developer moving the caret in the IDE window sends `$/ij/caret` to the Editor. The Brain's
own moves of that caret (answering requests) are never reported. Here a Python client plays the Editor;
`$/ij/debug/caret` is the developer clicking in the IDE window.
"""
from __future__ import annotations

import time

from harness.util import wait_until
from harness.wire import SRC, uri

PATH = f"{SRC}/probe/InspectionWarning.kt"
TEXT = '''package dev.bridge.fixture.probe

class Followed {
    fun a(x: Int) = x + 1
    fun b() = a(2)
}
'''


def caret_of(w, path=PATH):
    for m in w.debug_state()["mirrors"]:
        if m["uri"] == uri(path):
            return m["caret"]


def click(w, line: int, character: int, path=PATH):
    """The developer clicking in the IntelliJ window."""
    w.request("$/ij/debug/caret", {"uri": uri(path), "line": line, "character": character})


def editor_moves(w, line: int, character: int, version: int = 0, path=PATH):
    w.notify("$/ij/caret", {"textDocument": {"uri": uri(path)}, "version": version,
                            "position": {"line": line, "character": character}})


def follow(w, on: bool = True):
    w.notify("$/ij/follow", {"enabled": on})


def carets_sent(w, wait: float) -> list[dict]:
    """Every `$/ij/caret` the Brain sends within `wait` seconds."""
    out, deadline = [], time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            msg = w.read(max(0.1, min(0.5, deadline - time.monotonic())))
        except Exception:                                    # noqa: BLE001 - a read timeout is the answer
            continue
        if msg.get("method") == "$/ij/caret":
            out.append(msg["params"])
        else:
            w.inbox.append(msg)
    return out


def opened(w):
    w.did_open(PATH, TEXT, version=0)
    wait_until(lambda: caret_of(w) is not None, message="never mirrored")


class TestOptIn:

    def test_nothing_is_sent_until_the_editor_asks(self, wire):
        opened(wire)
        click(wire, 4, 8)
        assert carets_sent(wire, 1.5) == []

    def test_the_editors_cursor_is_ignored_until_it_asks(self, wire):
        opened(wire)
        editor_moves(wire, 4, 8)
        time.sleep(1)
        assert caret_of(wire) == {"line": 0, "character": 0}

    def test_it_can_be_turned_off_again(self, wire):
        opened(wire)
        follow(wire)
        follow(wire, False)
        click(wire, 4, 8)
        assert carets_sent(wire, 1.5) == []


class TestIdeToEditor:

    def test_the_developer_moving_the_caret_in_the_ide_is_sent(self, wire):
        opened(wire)
        follow(wire)
        click(wire, 4, 8)
        (sent,) = carets_sent(wire, 3)
        assert sent["uri"] == uri(PATH) and sent["position"] == {"line": 4, "character": 8} and sent["version"] == 0

    def test_continuous_movement_is_one_event_at_the_end(self, wire):
        opened(wire)
        follow(wire)
        for col in range(0, 12):
            click(wire, 4, col)
            time.sleep(0.02)
        sent = carets_sent(wire, 3)
        assert [s["position"] for s in sent] == [{"line": 4, "character": 11}], sent

    def test_the_same_position_is_not_sent_twice(self, wire):
        opened(wire)
        follow(wire)
        click(wire, 4, 8)
        assert len(carets_sent(wire, 2)) == 1
        click(wire, 4, 8)
        assert carets_sent(wire, 1.5) == []

    def test_the_brains_own_moves_are_not_the_developers(self, wire):
        """Answering a request moves the Mirror's caret to where the request is; nobody should hear of it."""
        opened(wire)
        wait_until(lambda: wire.debug_state()["state"] == "Ready", timeout=120)
        follow(wire)
        pos = {"line": 4, "character": 20}
        try:
            wire.request("textDocument/signatureHelp", {"textDocument": {"uri": uri(PATH)}, "position": pos}, timeout=60)
        except Exception:                                    # noqa: BLE001 - only the caret matters here
            pass
        assert carets_sent(wire, 2) == []

    def test_a_change_the_editor_sent_moves_no_caret_to_it(self, wire):
        opened(wire)
        follow(wire)
        wire.did_change(PATH, 1, {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}},
                                  "text": "// added\n"})
        assert carets_sent(wire, 1.5) == []


class TestEditorToIde:

    def test_the_editors_cursor_moves_the_mirrors_caret(self, wire):
        opened(wire)
        follow(wire)
        editor_moves(wire, 4, 8)
        wait_until(lambda: caret_of(wire) == {"line": 4, "character": 8}, message=f"the caret is {caret_of(wire)}")

    def test_it_is_not_sent_back(self, wire):
        opened(wire)
        follow(wire)
        editor_moves(wire, 4, 8)
        wait_until(lambda: caret_of(wire) == {"line": 4, "character": 8})
        assert carets_sent(wire, 1.5) == []

    def test_a_caret_for_text_the_mirror_has_not_got_yet_waits_for_it(self, wire):
        """The Editor sends changes debounced, so a caret can beat the change it refers to."""
        opened(wire)
        follow(wire)
        editor_moves(wire, 4, 8, version=1)                              # refers to the text after the change below
        time.sleep(1)
        assert caret_of(wire) == {"line": 0, "character": 0}
        wire.did_change(PATH, 1, {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}},
                                  "text": "// added\n"})
        wait_until(lambda: caret_of(wire) == {"line": 4, "character": 8}, message=f"the caret is {caret_of(wire)}")

    def test_only_the_owner_may_move_it(self, wire, bridge):
        from harness.wire import Wire
        opened(wire)
        with Wire(bridge.port) as other:
            other.initialize()
            follow(other)
            editor_moves(other, 4, 8)
            time.sleep(1)
        assert caret_of(wire) == {"line": 0, "character": 0}


class TestThroughNeovim:
    """The cursor and IntelliJ's caret, in a real Neovim."""

    PRODUCER = f"{SRC}/probe/CrossFileProducer.kt"

    def attach(self, nvim, probe):
        from test_navigation import attached
        nvim.command(f"edit {self.PRODUCER}")
        wait_until(lambda: attached(nvim) == 1, message="never attached")
        wait_until(lambda: caret_of(probe, self.PRODUCER) is not None, message="never mirrored")

    def follow_on(self, nvim):
        """Turning it on reports the cursor once, after the debounce: let that settle before moving anything."""
        nvim.command("IjBridge follow on")
        time.sleep(1)

    def test_it_is_off_unless_asked_for(self, nvim, probe):
        self.attach(nvim, probe)
        nvim.exec_lua("require('ij_bridge.caret').enable(false)")
        nvim.current.window.cursor = (6, 3)
        time.sleep(1.5)
        assert caret_of(probe, self.PRODUCER) == {"line": 0, "character": 0}
        click(probe, 8, 2, path=self.PRODUCER)                            # and the IDE does not move the cursor either
        time.sleep(1.5)
        assert nvim.current.window.cursor == (6, 3)

    def test_the_cursor_moves_the_ides_caret(self, nvim, probe):
        self.attach(nvim, probe)
        self.follow_on(nvim)
        try:
            nvim.current.window.cursor = (6, 3)
            wait_until(lambda: caret_of(probe, self.PRODUCER) == {"line": 5, "character": 3}, timeout=20,
                       message=f"the IDE's caret is {caret_of(probe, self.PRODUCER)}")
        finally:
            nvim.command("IjBridge follow off")

    def test_the_ides_caret_moves_the_cursor(self, nvim, probe):
        self.attach(nvim, probe)
        self.follow_on(nvim)
        try:
            click(probe, 8, 2, path=self.PRODUCER)
            wait_until(lambda: nvim.current.window.cursor == (9, 2), timeout=20,
                       message=f"the cursor is {nvim.current.window.cursor}")
        finally:
            nvim.command("IjBridge follow off")

    def test_the_two_do_not_chase_each_other(self, nvim, probe):
        self.attach(nvim, probe)
        self.follow_on(nvim)
        try:
            nvim.current.window.cursor = (6, 3)
            wait_until(lambda: caret_of(probe, self.PRODUCER) == {"line": 5, "character": 3}, timeout=20)
            time.sleep(2)
            assert nvim.current.window.cursor == (6, 3)
            assert caret_of(probe, self.PRODUCER) == {"line": 5, "character": 3}
        finally:
            nvim.command("IjBridge follow off")

    def test_a_buffer_entered_reports_where_its_cursor_is(self, nvim, probe):
        self.attach(nvim, probe)
        nvim.current.window.cursor = (7, 1)
        nvim.command("IjBridge follow on")
        try:
            wait_until(lambda: caret_of(probe, self.PRODUCER) == {"line": 6, "character": 1}, timeout=20,
                       message="turning it on did not report the cursor")
        finally:
            nvim.command("IjBridge follow off")
