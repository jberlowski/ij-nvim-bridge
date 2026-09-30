"""An edit made in the IntelliJ window is forwarded to the Editor (ADR-0009).

A Mirror is a real, editable IntelliJ editor, so a person can type into it, or run one of the IDE's own quick
fixes on it. The Brain notices any change it did not make itself and sends it to the Editor as a versioned
`workspace/applyEdit`; the Mirror keeps the IDE's text until the Editor's own `didChange` comes back, then is put
back and takes the Editor's changes, so nothing is applied twice. A refusal puts it back and says so.

Here a Python client plays the Editor (the Neovim slice is at the bottom). `$/ij/debug/edit` changes a Mirror as
typing in the IDE window would.
"""
from __future__ import annotations

import time

import pytest

from harness.brain import Framing
from harness.util import wait_until
from harness.wire import SRC, Wire, uri
from test_formatting import apply_edits

PATH = f"{SRC}/probe/InspectionWarning.kt"
TEXT = '''package dev.bridge.fixture.probe

class Foreign {
    fun a() = 1
}
'''


def mirror_text(w, path=PATH) -> str | None:
    for m in w.debug_state(text=True)["mirrors"]:
        if m["uri"] == uri(path):
            return m["text"]


def ide_types(w, line: int, text: str, path=PATH, character=0, remove=0):
    """What typing in the IntelliJ window does to the Mirror."""
    w.request("$/ij/debug/edit", {"uri": uri(path), "line": line, "character": character, "text": text, "remove": remove})


def next_request(w, method: str, timeout: float = 20) -> dict:
    """The next request of the Brain's to the Editor, whatever else arrives first."""
    for i, msg in enumerate(w.inbox):
        if msg.get("method") == method and "id" in msg:
            return w.inbox.pop(i)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg = w.read(max(0.1, deadline - time.monotonic()))
        if msg.get("method") == method and "id" in msg:
            return msg
        w.inbox.append(msg)
    raise AssertionError(f"the Brain never sent {method}")


def answer(w, request: dict, applied: bool, why: str | None = None):
    result = {"applied": applied}
    if why:
        result["failureReason"] = why
    w.sock.sendall(Framing.encode({"jsonrpc": "2.0", "id": request["id"], "result": result}))


def edit_of(request: dict) -> tuple[int, list[dict]]:
    (change,) = request["params"]["edit"]["documentChanges"]
    return change["textDocument"]["version"], change["edits"]


def no_message(w, method: str, wait: float = 1.5) -> bool:
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            msg = w.read(0.3)
        except Exception:                                    # noqa: BLE001 - a read timeout is the answer
            continue
        if msg.get("method") == method:
            return False
        w.inbox.append(msg)
    return True


class TestForwarding:

    def test_an_edit_made_in_the_ide_is_sent_to_the_editor(self, wire):
        wire.did_open(PATH, TEXT, version=3)
        ide_types(wire, 3, "    // typed in IntelliJ\n")
        request = next_request(wire, "workspace/applyEdit")
        version, edits = edit_of(request)
        assert version == 3                                              # versioned: a moved-on buffer refuses it
        assert apply_edits(TEXT, edits) == TEXT.replace("    fun a()", "    // typed in IntelliJ\n    fun a()")
        answer(wire, request, applied=True)

    def test_it_is_applied_once_when_the_editors_own_change_comes_back(self, wire):
        wire.did_open(PATH, TEXT, version=0)
        ide_types(wire, 3, "    // typed in IntelliJ\n")
        request = next_request(wire, "workspace/applyEdit")
        answer(wire, request, applied=True)
        # The Editor applies the edit as it would any other, and reports it the usual way.
        _, edits = edit_of(request)
        (edit,) = edits
        wire.did_change(PATH, 1, {"range": edit["range"], "text": edit["newText"]})
        final = TEXT.replace("    fun a()", "    // typed in IntelliJ\n    fun a()")
        wait_until(lambda: mirror_text(wire) == final, timeout=20,
                   message=f"the Mirror is not the Editor's text: {mirror_text(wire)!r}")
        time.sleep(1)
        assert mirror_text(wire) == final                                # and it stays so: not applied twice

    def test_a_refusal_puts_the_mirror_back_and_says_so(self, wire):
        wire.did_open(PATH, TEXT, version=0)
        ide_types(wire, 3, "    // typed in IntelliJ\n")
        request = next_request(wire, "workspace/applyEdit")
        answer(wire, request, applied=False, why="the buffer changed")
        message = next_request_or_notification(wire, "window/showMessage")
        assert "was not applied" in message["params"]["message"] and "the buffer changed" in message["params"]["message"]
        wait_until(lambda: mirror_text(wire) == TEXT, timeout=20, message="the Mirror kept an edit the Editor refused")

    def test_a_keystroke_that_beat_it_wins_and_is_kept(self, wire):
        """The Editor typed something the Brain had not seen: it refuses the edit, and its own change comes first."""
        wire.did_open(PATH, TEXT, version=0)
        ide_types(wire, 3, "    // typed in IntelliJ\n")
        request = next_request(wire, "workspace/applyEdit")
        wire.did_change(PATH, 1, {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}},
                                  "text": "// mine\n"})
        answer(wire, request, applied=False, why="buffer is newer")
        wait_until(lambda: mirror_text(wire) == "// mine\n" + TEXT, timeout=20,
                   message=f"the Mirror is not the Editor's text: {mirror_text(wire)!r}")

    def test_an_edit_made_while_another_is_in_flight_follows_it(self, wire):
        wire.did_open(PATH, TEXT, version=0)
        ide_types(wire, 3, "    // first\n")
        first = next_request(wire, "workspace/applyEdit")
        ide_types(wire, 3, "    // second\n")                             # made before the first is answered
        answer(wire, first, applied=True)
        _, edits = edit_of(first)
        (edit,) = edits
        wire.did_change(PATH, 1, {"range": edit["range"], "text": edit["newText"]})
        second = next_request(wire, "workspace/applyEdit")
        version, edits2 = edit_of(second)
        assert version == 1                                              # from where the Editor now stands
        with_first = TEXT.replace("    fun a()", "    // first\n    fun a()")
        both = with_first.replace("    // first\n", "    // second\n    // first\n")
        assert apply_edits(with_first, edits2) == both
        answer(wire, second, applied=True)
        (edit2,) = edits2
        wire.did_change(PATH, 2, {"range": edit2["range"], "text": edit2["newText"]})
        wait_until(lambda: mirror_text(wire) == both, timeout=20,
                   message=f"the Mirror is not the Editor's text: {mirror_text(wire)!r}")

    def test_a_burst_of_changes_is_one_edit(self, wire):
        """A refactor is several document changes in one command; the Editor should get one edit."""
        wire.did_open(PATH, TEXT, version=0)
        ide_types(wire, 3, "    // one\n")
        ide_types(wire, 3, "    // two\n")
        ide_types(wire, 3, "    // three\n")
        request = next_request(wire, "workspace/applyEdit")
        _, edits = edit_of(request)
        assert apply_edits(TEXT, edits) == TEXT.replace("    fun a()", "    // three\n    // two\n    // one\n    fun a()")
        answer(wire, request, applied=True)

    def test_a_change_the_brain_made_itself_is_not_forwarded(self, wire):
        wire.did_open(PATH, TEXT, version=0)
        wire.did_change(PATH, 1, {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}},
                                  "text": "// from the Editor\n"})
        assert no_message(wire, "workspace/applyEdit", wait=2)

    def test_an_edit_the_brain_cannot_forward_is_undone_and_says_so(self, wire, bridge):
        """Two Editors on one file are unsupported (ADR-0003): there is nowhere to send it."""
        with Wire(bridge.port) as other:
            other.initialize()
            other.did_open(PATH, TEXT, version=0)
            wire.did_open(PATH, TEXT, version=0)                     # at once: they must still share one Mirror
            wait_until(lambda: mirror_text(wire) == TEXT, message="never mirrored")
            ide_types(wire, 3, "    // typed in IntelliJ\n")
            wait_until(lambda: mirror_text(wire) == TEXT, timeout=20, message="the Mirror kept an edit nobody could take")
            assert no_message(wire, "workspace/applyEdit", wait=1)
            other.did_close(PATH)


def next_request_or_notification(w, method: str, timeout: float = 20) -> dict:
    for i, msg in enumerate(w.inbox):
        if msg.get("method") == method:
            return w.inbox.pop(i)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg = w.read(max(0.1, deadline - time.monotonic()))
        if msg.get("method") == method:
            return msg
        w.inbox.append(msg)
    raise AssertionError(f"the Brain never sent {method}")


class TestThroughNeovim:
    """Typing in the IntelliJ window, seen from a real Neovim."""

    def test_an_ide_edit_lands_in_the_buffer_as_one_undoable_change(self, nvim, probe):
        from test_navigation import attached

        producer = f"{SRC}/probe/CrossFileProducer.kt"
        nvim.command(f"edit {producer}")
        wait_until(lambda: attached(nvim) == 1, message="never attached")
        original = nvim.current.buffer[:]
        assert not nvim.eval("&modified")
        wait_until(lambda: mirror_text(probe, producer) is not None, message="the buffer was never mirrored")

        ide_types(probe, 0, "// typed in IntelliJ\n", path=producer)
        wait_until(lambda: nvim.current.buffer[0] == "// typed in IntelliJ", timeout=30,
                   message="the edit made in IntelliJ never reached the buffer")
        assert nvim.current.buffer[1:] == original                      # exactly the edit, nothing else changed
        assert nvim.eval("&modified")                                    # an ordinary edit: it is Neovim's to save

        # The Mirror is the buffer's text, once: not the edit applied twice.
        wanted = "\n".join(nvim.current.buffer[:]) + "\n"
        wait_until(lambda: mirror_text(probe, producer) == wanted, timeout=20,
                   message=f"the Mirror is not the buffer's text: {mirror_text(probe, producer)!r}")

        nvim.command("undo")                                             # one step
        assert nvim.current.buffer[:] == original
        wait_until(lambda: mirror_text(probe, producer) == "\n".join(original) + "\n", timeout=20,
                   message="the Mirror did not follow the undo")
