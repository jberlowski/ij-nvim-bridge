"""A file that disappears while it is open (a `git reset`, a checkout of another branch, a plain `rm`).

Reported: "it throws a lot of errors and also I think IntelliJ hangs". Reproduced first: IntelliJ closes the Mirror's
editor, the Brain then tried to reopen it on an invalid file (a SEVERE in the IDE's log), kept a Mirror with a
disposed editor that failed every request, and failed again when the buffer was closed. Now the Mirror is released
as the file goes, without touching the dead file, and the Editor is told (`$/ij/fileGone`) so that it stops asking
and picks the file up again when it is saved or comes back. The Editor's own text is the truth and is never touched.
"""
from __future__ import annotations

import time

import pytest

from harness.util import wait_until
from harness.wire import SRC, RpcError, Wire, uri

PROBE = f"{SRC}/probe"
SCRATCH = f"{PROBE}/Vanishing.kt"
TEXT = '''package dev.bridge.fixture.probe

class Vanishing {
    fun a(x: Int) = x + 1
}
'''
BRAIN_LOG = "f=$(ls -t /home/dev/.local/state/ij-nvim-bridge/brain-*.log | head -1); "
IDEA_LOG = "/home/dev/.cache/JetBrains/IntelliJIdea2026.2/log/idea.log"


class Logs:
    """What the two logs gained while a test ran: errors are what was reported."""

    def __init__(self, c):
        self.c = c
        self.brain = self.count(BRAIN_LOG + "wc -l < $f")
        self.idea = self.count(f"wc -l < {IDEA_LOG}")

    def count(self, cmd: str) -> int:
        return int(self.c.exec(cmd, check=False).stdout.strip() or 0)

    def errors(self) -> str:
        brain = self.c.exec(BRAIN_LOG + f"tail -n +{self.brain + 1} $f | grep -E '\"lvl\":\"error\"' | cut -c1-260",
                            check=False).stdout
        idea = self.c.exec(f"tail -n +{self.idea + 1} {IDEA_LOG} | grep -E ' SEVERE |is not valid|already disposed' | cut -c1-260",
                           check=False).stdout
        return (brain + idea).strip()


def mirrors(w) -> dict[str, dict]:
    return {m["uri"].rsplit("/", 1)[1]: m for m in w.debug_state(text=True)["mirrors"]}


def vanish(c, w, command: str):
    """What deletes or moves a file behind the IDE's back, then what the IDE's file watcher would tell it."""
    c.exec(command)
    w.request("$/ij/debug/refresh", {})


def gone_notices(w, want: int, timeout: float = 30) -> list[dict]:
    """The `$/ij/fileGone` notifications the Brain sends, until `want` have come or the time is up."""
    out = [m["params"] for m in w.inbox if m.get("method") == "$/ij/fileGone"]
    w.inbox = [m for m in w.inbox if m.get("method") != "$/ij/fileGone"]
    deadline = time.monotonic() + timeout
    while len(out) < want and time.monotonic() < deadline:
        try:
            msg = w.read(max(0.2, min(2.0, deadline - time.monotonic())))
        except TimeoutError:
            continue
        if msg.get("method") == "$/ij/fileGone":
            out.append(msg["params"])
        else:
            w.inbox.append(msg)
    return out


class TestOnTheWire:

    def test_a_deleted_file_is_released_and_the_editor_is_told(self, wire, project_files, bridge_container):
        project_files(SCRATCH, TEXT)
        wire.did_open(SCRATCH, TEXT)
        wait_until(lambda: "Vanishing.kt" in mirrors(wire), message="never mirrored")
        logs = Logs(bridge_container)
        vanish(bridge_container, wire, f"rm -f '{SCRATCH}'")
        (notice,) = gone_notices(wire, 1)
        assert notice["uri"] == uri(SCRATCH) and "deleted" in notice["why"], notice
        assert "Vanishing.kt" not in mirrors(wire)
        time.sleep(2)
        assert logs.errors() == ""

    def test_an_unsaved_buffer_of_a_deleted_file_is_released_the_same(self, wire, project_files, bridge_container):
        project_files(SCRATCH, TEXT)
        wire.did_open(SCRATCH, TEXT)
        wire.did_change(SCRATCH, 1, {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}},
                                     "text": "// unsaved\n"})
        wait_until(lambda: mirrors(wire).get("Vanishing.kt", {}).get("version") == 1, message="never mirrored")
        logs = Logs(bridge_container)
        vanish(bridge_container, wire, f"rm -f '{SCRATCH}'")
        assert len(gone_notices(wire, 1)) == 1
        time.sleep(2)
        assert logs.errors() == ""

    def test_requests_and_closing_the_buffer_afterwards_are_refusals_not_crashes(self, wire, project_files, bridge_container):
        project_files(SCRATCH, TEXT)
        wire.did_open(SCRATCH, TEXT)
        wait_until(lambda: "Vanishing.kt" in mirrors(wire), message="never mirrored")
        logs = Logs(bridge_container)
        vanish(bridge_container, wire, f"rm -f '{SCRATCH}'")
        gone_notices(wire, 1)
        for method, params in (
            ("textDocument/hover", {"textDocument": {"uri": uri(SCRATCH)}, "position": {"line": 3, "character": 8}}),
            ("textDocument/codeAction", {"textDocument": {"uri": uri(SCRATCH)}, "range": {
                "start": {"line": 3, "character": 8}, "end": {"line": 3, "character": 8}}, "context": {"diagnostics": []}}),
        ):
            with pytest.raises(RpcError) as e:
                wire.request(method, params, timeout=20)
            assert e.value.code == -32602 and "not mirrored" in str(e.value), (method, str(e.value))   # a refusal, not a crash
        wire.did_close(SCRATCH)
        wire.debug_state()                                             # still answering
        time.sleep(1)
        assert logs.errors() == ""

    def test_a_whole_directory_going_takes_every_mirror_in_it(self, wire, project_files, bridge_container):
        folder = f"{PROBE}/vanishing_dir"
        a, b = f"{folder}/A.kt", f"{folder}/B.kt"
        try:
            project_files.many({a: "package dev.bridge.fixture.probe.vanishing_dir\n\nclass A\n",
                                b: "package dev.bridge.fixture.probe.vanishing_dir\n\nclass B\n"})
            wire.did_open(a, "package dev.bridge.fixture.probe.vanishing_dir\n\nclass A\n")
            wire.did_open(b, "package dev.bridge.fixture.probe.vanishing_dir\n\nclass B\n")
            wait_until(lambda: {"A.kt", "B.kt"} <= set(mirrors(wire)), message="never mirrored")
            logs = Logs(bridge_container)
            vanish(bridge_container, wire, f"rm -rf '{folder}'")
            notices = gone_notices(wire, 2)
            assert {n["uri"] for n in notices} == {uri(a), uri(b)}, notices
            assert not {"A.kt", "B.kt"} & set(mirrors(wire))
            time.sleep(2)
            assert logs.errors() == ""
        finally:
            bridge_container.exec(f"rm -rf '{folder}'", check=False)

    def test_a_renamed_file_is_gone_from_where_the_editor_has_it(self, wire, project_files, bridge_container):
        moved = f"{PROBE}/VanishingMoved.kt"
        project_files(SCRATCH, TEXT)
        try:
            wire.did_open(SCRATCH, TEXT)
            wait_until(lambda: "Vanishing.kt" in mirrors(wire), message="never mirrored")
            vanish(bridge_container, wire, f"mv '{SCRATCH}' '{moved}'")
            (notice,) = gone_notices(wire, 1)
            assert notice["uri"] == uri(SCRATCH) and ("renamed" in notice["why"] or "moved" in notice["why"] or "deleted" in notice["why"])
        finally:
            bridge_container.exec(f"rm -f '{moved}'", check=False)

    def test_the_file_coming_back_can_be_opened_again(self, wire, project_files, bridge_container):
        project_files(SCRATCH, TEXT)
        wire.did_open(SCRATCH, TEXT)
        wait_until(lambda: "Vanishing.kt" in mirrors(wire), message="never mirrored")
        vanish(bridge_container, wire, f"rm -f '{SCRATCH}'")
        gone_notices(wire, 1)
        bridge_container.write_bytes(SCRATCH, TEXT.encode())          # the checkout that brings it back
        wire.request("$/ij/debug/refresh", {})
        wire.did_open(SCRATCH, TEXT, version=5)
        wait_until(lambda: mirrors(wire).get("Vanishing.kt", {}).get("text") == TEXT, timeout=30, message="never mirrored again")


class TestThroughNeovim:

    def open_scratch(self, nvim, wire_probe, project_files):
        from test_navigation import attached
        project_files(SCRATCH, TEXT)
        nvim.command(f"edit {SCRATCH}")
        wait_until(lambda: attached(nvim) == 1, message="never attached")
        wait_until(lambda: "Vanishing.kt" in mirrors(wire_probe), message="never mirrored")

    def test_the_buffer_keeps_its_text_and_is_attached_again_when_saved(self, nvim, probe, project_files, bridge_container):
        from test_navigation import attached
        self.open_scratch(nvim, probe, project_files)
        nvim.current.buffer.append("// unsaved", 0)
        time.sleep(1)
        logs = Logs(bridge_container)
        vanish(bridge_container, probe, f"rm -f '{SCRATCH}'")
        wait_until(lambda: attached(nvim) == 0, timeout=30, message="the buffer was still attached to a file that is gone")
        assert "Vanishing.kt" not in mirrors(probe)
        assert nvim.current.buffer[0] == "// unsaved" and nvim.eval("&modified")     # the Editor's text is the truth
        time.sleep(2)
        assert logs.errors() == ""

        nvim.command("write")                                          # recreates the file
        wait_until(lambda: attached(nvim) == 1, timeout=30, message="saving did not attach the buffer again")
        wanted = "\n".join(nvim.current.buffer[:]) + "\n"
        wait_until(lambda: mirrors(probe).get("Vanishing.kt", {}).get("text") == wanted, timeout=30,
                   message="the Mirror is not the saved text")
        assert logs.errors() == ""

    def test_the_file_coming_back_from_a_checkout_attaches_the_buffer_again(self, nvim, probe, project_files, bridge_container):
        from test_navigation import attached
        self.open_scratch(nvim, probe, project_files)
        vanish(bridge_container, probe, f"rm -f '{SCRATCH}'")
        wait_until(lambda: attached(nvim) == 0, timeout=30, message="the buffer was still attached to a file that is gone")
        bridge_container.write_bytes(SCRATCH, TEXT.encode())
        nvim.command("checktime")
        nvim.command("doautocmd FocusGained")
        wait_until(lambda: attached(nvim) == 1, timeout=30, message="the file coming back did not attach the buffer")
