"""When the Brain or the connection goes away, and comes back.

IntelliJ quits, restarts or crashes; the socket drops; Neovim's client is
stopped. Neovim must keep working (`:w` must never fail or block because of the
Bridge), say plainly that it is disconnected, and reconnect by itself, sending
every unsaved buffer again, since the new Brain knows nothing about them.

These run in a container of their own: they kill IntelliJ.
"""
from __future__ import annotations

import time

import pytest

from harness.brain import Brain
from harness.util import wait_until
from harness.wire import SRC, Wire

PROBE = f"{SRC}/probe"
PRODUCER = f"{PROBE}/CrossFileProducer.kt"


# ------------------------------------------------------------------ helpers
def statusline(nv) -> str:
    return nv.exec_lua("return require('ij_bridge').statusline()")


def attached(nv) -> int:
    return nv.exec_lua("return #vim.lsp.get_clients({bufnr = 0, name = 'ij-bridge'})")


def brain_pid(life) -> int | None:
    try:
        brains = Brain(life.c).registry().brains
        return brains[0]["pid"] if brains else None
    except Exception:  # noqa: BLE001 - no registry yet
        return None


def mirrors(life) -> dict:
    """The Brain's Mirrors, over a passive Session. {} while there is no Brain."""
    from urllib.parse import unquote
    try:
        with Wire(life.port, timeout=10) as w:
            w.initialize()
            return {unquote(m["uri"].rsplit("/", 1)[1]): m for m in w.debug_state(text=True)["mirrors"]}
    except Exception:  # noqa: BLE001 - the Brain is away
        return {}


def stop_ide(life):
    life.ide.kill()                       # a crash: the stale Registry entry stays
    wait_until(lambda: not life.ide.is_running(), timeout=30, message="IntelliJ never stopped")


def start_ide(life, timeout=240):
    """Launch IntelliJ again and wait for a *new* Brain to publish itself. Not for
    it to be Ready: a relaunched IDE has lost its imported model, and these tests
    are about the connection, not about analysis."""
    old = brain_pid(life)
    life.ide.launch("/work/fixture")
    wait_until(lambda: (brain_pid(life) not in (None, old)) and mirrors_reachable(life),
               timeout=timeout, interval=2.0, message="the Brain never came back")


def mirrors_reachable(life) -> bool:
    try:
        with Wire(life.port, timeout=5) as w:
            w.initialize()
            w.debug_state()
            return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture
def nv(life, life_nvim_session):
    """Neovim with every buffer wiped, and a running IntelliJ."""
    if not life.ide.is_running():
        start_ide(life)
    life_nvim_session.command("stopinsert")
    life_nvim_session.command("silent! %bwipeout!")
    life_nvim_session.command("let v:errmsg = ''")
    yield life_nvim_session
    life_nvim_session.command("silent! %bwipeout!")


def open_and_edit(nv, life, marker):
    nv.command(f"edit {PRODUCER}")
    try:
        wait_until(lambda: attached(nv) == 1, message="never attached")
    except AssertionError as exc:
        state = nv.exec_lua("""
            local m = require('ij_bridge')
            return vim.inspect({ offline = m.offline, attached = m.attached,
              clients = #vim.lsp.get_clients({ name = 'ij-bridge' }),
              resolve = require('ij_bridge.registry').resolve(vim.api.nvim_buf_get_name(0)),
              events = m.events,
              messages = vim.api.nvim_exec2('messages', { output = true }).output })""")
        raise AssertionError(f"{exc}\nplugin state: {state}") from None
    nv.current.buffer.append(marker, 0)
    wait_until(lambda: marker in mirrors(life).get("CrossFileProducer.kt", {}).get("text", ""),
               timeout=20, message="the Brain never saw the unsaved edit")


# ------------------------------------------------------------- tests
class TestDisconnect:

    def test_a_stopped_client_reconnects_and_resends_its_unsaved_buffers(self, life, nv):
        """The socket drops while IntelliJ stays up. The Brain drops the dead
        Session's claim; Neovim reconnects by itself; the unsaved text is back."""
        open_and_edit(nv, life, "// unsaved before the disconnect")
        nv.exec_lua("for _, c in ipairs(vim.lsp.get_clients({name = 'ij-bridge'})) do c:stop(true) end")

        wait_until(lambda: statusline(nv) in ("IJ: disconnected", "") or attached(nv) == 0,
                   timeout=10, message="the disconnect was never noticed")
        wait_until(lambda: attached(nv) == 1 and statusline(nv) == "IJ", timeout=60,
                   message="Neovim never reconnected by itself")
        wait_until(lambda: "unsaved before the disconnect" in mirrors(life).get("CrossFileProducer.kt", {}).get("text", ""),
                   timeout=20, message="the unsaved buffer was not sent again")
        assert nv.eval("v:errmsg") == ""

    def test_the_ide_going_away_is_shown_and_writing_still_works(self, life, nv):
        open_and_edit(nv, life, "// written while the IDE is gone")
        original = life.c.read_file(PRODUCER)
        try:
            stop_ide(life)
            wait_until(lambda: statusline(nv) == "IJ: disconnected", timeout=20,
                       message="the loss of the Brain was never shown")
            out = nv.exec_lua("return vim.api.nvim_exec2('IjBridge', {output = true}).output")
            assert "disconnected" in out, out

            # Editing and saving go on as if the Bridge were not installed.
            nv.current.buffer.append("// still typing", 0)
            nv.command("write")
            on_disk = life.c.read_file(PRODUCER)
            assert "// still typing" in on_disk and "// written while the IDE is gone" in on_disk
            assert nv.eval("v:errmsg") == ""
        finally:
            nv.command("silent! %bwipeout!")
            life.c.write_bytes(PRODUCER, original.encode())

    def test_a_restarted_ide_gets_the_unsaved_buffers_back(self, life, nv):
        open_and_edit(nv, life, "// unsaved across an IntelliJ restart")
        stop_ide(life)
        wait_until(lambda: statusline(nv) == "IJ: disconnected", timeout=20)

        start_ide(life)
        wait_until(lambda: statusline(nv) != "IJ: disconnected", timeout=120,
                   message="Neovim never reconnected to the restarted IDE")
        text = wait_until(lambda: mirrors(life).get("CrossFileProducer.kt", {}).get("text"), timeout=30,
                          message="the restarted Brain never got the unsaved buffer")
        assert text == "\n".join(nv.current.buffer[:]) + "\n", "the Mirror must equal the buffer exactly"
        assert nv.eval("v:errmsg") == ""

    def test_a_stale_registry_entry_is_not_trusted(self, life, nv):
        stop_ide(life)
        # The dead Brain's entry is still in the registry file, with a dead pid.
        assert Brain(life.c).registry().brains, "the test needs the stale entry"
        assert nv.exec_lua(f"return require('ij_bridge.registry').resolve('{PRODUCER}')") is None

        # A buffer opened now must not error, hang or attach to a ghost.
        nv.command(f"edit {PRODUCER}")
        assert attached(nv) == 0
        assert statusline(nv) == ""                 # never attached: Dormant, not "disconnected"
        assert nv.eval("v:errmsg") == ""

        start_ide(life)
        nv.command("edit! " + PRODUCER)             # the next BufEnter after the IDE returns
        wait_until(lambda: attached(nv) == 1, timeout=30, message="never attached once the IDE returned")
