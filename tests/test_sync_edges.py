"""Editor -> Brain sync at the edges.

Formatting and every other edit will send text the other way, so the Mirror must
equal the buffer *exactly*, under everything Neovim can do to a buffer: text
that is not ASCII, line endings, undo, large and rapid edits, files that change
under it, unusual paths, and more than one Session.

Real Neovim throughout, with IntelliJ in the background.
"""
from __future__ import annotations

import time

import pytest

from harness.util import wait_until
from harness.wire import SRC, Wire, uri
from test_navigation import SHAPES, at, attached, builtin, goto

PROBE = f"{SRC}/probe"
PRODUCER = f"{PROBE}/CrossFileProducer.kt"
CONSUMER = f"{PROBE}/CrossFileConsumer.kt"


def mirrors(probe) -> dict[str, dict]:
    """Mirrors by file name, decoded: a URI is percent-encoded, a path is not."""
    from urllib.parse import unquote
    return {unquote(m["uri"].rsplit("/", 1)[1]): m for m in probe.debug_state(text=True)["mirrors"]}


def buffer_text(nvim) -> str:
    """What the buffer holds, as one string, the way the Mirror should."""
    text = "\n".join(nvim.current.buffer[:])
    return text if nvim.eval("&endofline") == 0 and nvim.eval("&fixendofline") == 0 else text + "\n"


def follows(nvim, probe, name, timeout=15):
    """Wait for the Brain's Mirror of `name` to equal the buffer, exactly."""
    wait_until(lambda: mirrors(probe).get(name, {}).get("text") == buffer_text(nvim), timeout=timeout,
               message=f"the Mirror of {name} never equalled the buffer")


def open_buffer(nvim, path, probe=None):
    nvim.command(f"edit {path}")
    wait_until(lambda: attached(nvim) == 1, message="the buffer never attached")
    if probe:
        name = path.rsplit("/", 1)[1]
        wait_until(lambda: name in mirrors(probe), message="the Brain never mirrored it")


# ---------------------------------------------------------------- non-ASCII
class TestNonAscii:
    """An emoji is two UTF-16 code units; LSP counts in code units, Neovim in bytes."""

    def test_an_edit_after_multibyte_text_lands_in_the_right_place(self, nvim, probe):
        open_buffer(nvim, PRODUCER, probe)
        buf = nvim.current.buffer
        buf.append("// héllo \U0001F600 世界 end", 0)
        follows(nvim, probe, "CrossFileProducer.kt")
        # Insert between the CJK text and " end": the range starts after a surrogate pair.
        line = buf[0]
        byte_col = len(line[: line.index(" end")].encode())
        nvim.api.buf_set_text(buf.handle, 0, byte_col, 0, byte_col, ["XYZ"])
        follows(nvim, probe, "CrossFileProducer.kt")
        assert "世界XYZ end" in mirrors(probe)["CrossFileProducer.kt"]["text"]

    def test_deleting_and_replacing_across_a_surrogate_pair(self, nvim, probe):
        open_buffer(nvim, PRODUCER, probe)
        buf = nvim.current.buffer
        buf.append("// a\U0001F600b", 0)
        follows(nvim, probe, "CrossFileProducer.kt")
        nvim.command("1")
        nvim.command("normal! 0fa")
        nvim.command("normal! lx")                     # delete the emoji, one whole character
        follows(nvim, probe, "CrossFileProducer.kt")
        assert mirrors(probe)["CrossFileProducer.kt"]["text"].startswith("// ab\n")

    def test_navigation_after_an_emoji_on_the_same_line(self, nvim):
        open_buffer(nvim, SHAPES)
        buf = nvim.current.buffer
        row = next(i for i, l in enumerate(buf[:]) if "val measured = shape.area()" in l)
        buf[row] = '    val measured = "\U0001F600é世".length + shape.area()'
        text = "\n".join(buf[:]) + "\n"
        col = len(buf[row][: buf[row].index("area()")].encode())
        nvim.current.window.cursor = (row + 1, col)
        items = builtin(nvim, "definition")
        assert [(i["file"], i["lnum"]) for i in items] == [(SHAPES, at(text, "fun area(): Double", 0)[0] + 1)]

    def test_a_diagnostic_after_an_emoji_points_at_the_right_bytes(self, nvim, probe):
        open_buffer(nvim, CONSUMER, probe)
        buf = nvim.current.buffer
        lines = buf[:]
        row = len(lines) - 1                                  # before the closing brace
        bad = '    val bad = "\U0001F600é" + stillUndefinedName'
        buf[row:row] = [bad]
        wait_until(lambda: any("stillUndefinedName" in d["message"]
                               for d in nvim.exec_lua("return vim.diagnostic.get(0)")),
                   timeout=40, message="no diagnostic for the error after the emoji")
        d = next(d for d in nvim.exec_lua("return vim.diagnostic.get(0)") if "stillUndefinedName" in d["message"])
        start = len(bad[: bad.index("stillUndefinedName")].encode())
        assert (d["lnum"], d["col"]) == (row, start), d
        assert d["end_col"] == start + len("stillUndefinedName"), d


# ------------------------------------------------------------ line endings
class TestLineEndings:

    def test_a_crlf_file_mirrors_without_carriage_returns_and_keeps_them_on_disk(
            self, nvim, probe, bridge_container, project_files):
        path = f"{PROBE}/Crlf.kt"
        project_files(path, b"package dev.bridge.fixture.probe\r\n\r\nfun crlfOne(): Int = 1\r\nfun crlfTwo(): Int = 2\r\n")
        open_buffer(nvim, path, probe)
        assert nvim.eval("&fileformat") == "dos"
        follows(nvim, probe, "Crlf.kt")
        assert "\r" not in mirrors(probe)["Crlf.kt"]["text"]

        nvim.current.buffer.append("fun crlfThree(): Int = 3")
        follows(nvim, probe, "Crlf.kt")
        nvim.command("write")
        on_disk = bridge_container.read_bytes(path)
        assert on_disk.count(b"\r\n") == on_disk.count(b"\n") == 5, on_disk
        probe.timeout = 10
        assert mirrors(probe)["Crlf.kt"]["convergent"] is True         # the IDE is alive

    def test_a_file_with_no_final_newline(self, nvim, probe, project_files):
        path = f"{PROBE}/NoEol.kt"
        project_files(path, b"package dev.bridge.fixture.probe\n\nfun noEol(): Int = 1")
        open_buffer(nvim, path, probe)
        nvim.command("set nofixendofline")
        follows(nvim, probe, "NoEol.kt")
        assert not mirrors(probe)["NoEol.kt"]["text"].endswith("\n")
        nvim.current.buffer[-1] = "fun noEol(): Int = 2"
        follows(nvim, probe, "NoEol.kt")


# ------------------------------------------------- undo, bulk and rapid edits
class TestBufferOperations:

    def test_undo_and_redo_are_followed(self, nvim, probe):
        open_buffer(nvim, PRODUCER, probe)
        base = mirrors(probe)["CrossFileProducer.kt"]["text"]
        nvim.command("normal! Ofirst")
        nvim.command("normal! Osecond")
        follows(nvim, probe, "CrossFileProducer.kt")
        two = mirrors(probe)["CrossFileProducer.kt"]["text"]
        nvim.command("undo")
        follows(nvim, probe, "CrossFileProducer.kt")
        assert mirrors(probe)["CrossFileProducer.kt"]["text"] != two
        nvim.command("undo")
        follows(nvim, probe, "CrossFileProducer.kt")
        assert mirrors(probe)["CrossFileProducer.kt"]["text"] == base
        nvim.command("redo")
        nvim.command("redo")
        follows(nvim, probe, "CrossFileProducer.kt")
        assert mirrors(probe)["CrossFileProducer.kt"]["text"] == two

    def test_a_large_paste(self, nvim, probe):
        open_buffer(nvim, PRODUCER, probe)
        block = [f"// pasted line {i} é\U0001F600" for i in range(3000)]
        started = time.monotonic()
        nvim.current.buffer[0:0] = block
        follows(nvim, probe, "CrossFileProducer.kt", timeout=30)
        assert time.monotonic() - started < 20

    def test_a_burst_of_rapid_edits_converges(self, nvim, probe):
        """Typing faster than the debounce: only the final state matters, and it
        must be exact."""
        open_buffer(nvim, PRODUCER, probe)
        buf = nvim.current.buffer
        buf.append("// x", 0)
        for i in range(60):
            nvim.api.buf_set_text(buf.handle, 0, 4 + i, 0, 4 + i, [str(i % 10)])
        follows(nvim, probe, "CrossFileProducer.kt", timeout=20)

    def test_discarding_changes_with_edit_bang_returns_to_disk(self, nvim, probe, bridge_container):
        open_buffer(nvim, PRODUCER, probe)
        on_disk = bridge_container.read_file(PRODUCER)
        nvim.current.buffer.append("// unsaved", 0)
        follows(nvim, probe, "CrossFileProducer.kt")
        nvim.command("edit!")
        wait_until(lambda: mirrors(probe)["CrossFileProducer.kt"]["text"] == on_disk, timeout=15,
                   message="the Mirror kept text the developer discarded")

    def test_saveas_moves_the_mirror_to_the_new_file(self, nvim, probe, project_files, bridge_container):
        new = f"{PROBE}/SavedAs.kt"
        try:
            open_buffer(nvim, PRODUCER, probe)
            nvim.command(f"saveas {new}")
            wait_until(lambda: "SavedAs.kt" in mirrors(probe) and "CrossFileProducer.kt" not in mirrors(probe),
                       timeout=20, message="the Mirror did not follow the rename")
            follows(nvim, probe, "SavedAs.kt")
        finally:
            nvim.command("silent! %bwipeout!")
            bridge_container.exec(f"rm -f {new}", check=False)


# ------------------------------------------------- changes outside Neovim
class TestDiskChangesOutsideNeovim:

    def test_an_external_change_to_a_clean_buffer_reaches_the_mirror(
            self, nvim, probe, bridge_container):
        original = bridge_container.read_file(PRODUCER)
        try:
            open_buffer(nvim, PRODUCER, probe)
            changed = original.replace("already on disk", "changed behind nvim's back")
            bridge_container.write_bytes(PRODUCER, changed.encode())
            wait_until(lambda: (nvim.command("checktime") or True) and
                       "changed behind" in "\n".join(nvim.current.buffer[:]),
                       timeout=15, message="Neovim never reloaded the changed file")
            follows(nvim, probe, "CrossFileProducer.kt")
            assert "changed behind" in mirrors(probe)["CrossFileProducer.kt"]["text"]
        finally:
            nvim.command("silent! %bwipeout!")
            bridge_container.write_bytes(PRODUCER, original.encode())


# ------------------------------------------------------------ unusual paths
class TestPaths:

    def test_a_path_with_a_space_and_non_ascii_characters(self, nvim, probe, project_files):
        path = f"{PROBE}/sp ace/Ünï.kt"
        project_files(path, "package dev.bridge.fixture.probe\n\nfun spaced(s: Shape): Double = s.area()\n")
        open_buffer(nvim, path, probe)
        follows(nvim, probe, "%C3%9Cn%C3%AF.kt" if "%C3%9Cn%C3%AF.kt" in mirrors(probe) else "Ünï.kt")
        text = "\n".join(nvim.current.buffer[:])
        nvim.current.window.cursor = (3, text.split("\n")[2].index("area()"))
        items = builtin(nvim, "definition")
        assert items and items[0]["file"] == SHAPES, items


# --------------------------------------------------------------- Sessions
class TestSessions:
    """Mirrors belong to the project, but a Session's claim on one must not
    outlive it, and must not be cancelled by another Session's `didClose`."""

    def test_another_sessions_didclose_does_not_release_my_mirror(self, nvim, probe):
        open_buffer(nvim, PRODUCER, probe)
        nvim.current.buffer.append("// unsaved in nvim", 0)
        follows(nvim, probe, "CrossFileProducer.kt")

        other = Wire(probe.sock.getpeername()[1])
        other.initialize()
        other.did_open(PRODUCER, "package other\n")
        other.did_close(PRODUCER)
        time.sleep(1)
        other.close()

        assert "CrossFileProducer.kt" in mirrors(probe), "another Session's close released nvim's Mirror"
        follows(nvim, probe, "CrossFileProducer.kt")

    def test_a_session_that_goes_away_takes_its_mirrors_with_it(self, probe, bridge_container):
        other = Wire(probe.sock.getpeername()[1])
        other.initialize()
        other.did_open(SHAPES, bridge_container.read_file(SHAPES) + "\n// unsaved, then the editor dies\n")
        wait_until(lambda: "Shapes.kt" in mirrors(probe))
        other.close()                                         # Neovim quit, or crashed
        wait_until(lambda: "Shapes.kt" not in mirrors(probe), timeout=15,
                   message="a dead Session's Mirror is still held, unsaved text and all")

    def test_stopping_the_neovim_client_releases_its_mirrors(self, nvim, probe):
        open_buffer(nvim, PRODUCER, probe)
        nvim.current.buffer.append("// unsaved", 0)
        follows(nvim, probe, "CrossFileProducer.kt")
        nvim.exec_lua("for _, c in ipairs(vim.lsp.get_clients({name = 'ij-bridge'})) do c:stop(true) end")
        wait_until(lambda: "CrossFileProducer.kt" not in mirrors(probe), timeout=15,
                   message="a stopped client's Mirror is still held")
