"""The debug logs (SPEC.md §15): a record of what happened, for finding out why something
went wrong on somebody else's machine.

The Brain and the Editor each write one JSON object per line, rotated so the file cannot grow
without bound. They record metadata, never the developer's code (except at `trace`, which is
opt-in), and both print the Session's id so a line in one can be found in the other.
"""
from __future__ import annotations

import json
import time

import pytest

from conftest import BRAIN_VERSION
from harness.util import wait_until
from harness.wire import SRC, Wire, replace_range, uri
from test_editor_slice import attached

PROBE = f"{SRC}/probe"
SHAPES = f"{PROBE}/Shapes.kt"
SECRET = "SECRET_TOKEN_THAT_IS_CODE_9137"


def lines_of(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass          # a line cut off by a rotation, or a partial write
    return out


def brain_log(c, path: str) -> list[dict]:
    return lines_of(c.read_file(path))


@pytest.fixture
def path(wire):
    return wire.request("$/ij/log", {})["path"]


def session_events(c, path, session) -> list[dict]:
    return [e for e in brain_log(c, path) if e.get("sess") == session]


# ====================================================================== the Brain
class TestBrainLog:

    def test_a_session_is_recorded_from_open_to_close(self, bridge, bridge_container, path):
        with Wire(bridge.port) as w:
            session = w.initialize()["serverInfo"]["session"]
            w.did_open(SHAPES, bridge_container.read_file(SHAPES))
            w.request("textDocument/definition", {"textDocument": {"uri": uri(SHAPES)},
                                                  "position": {"line": 10, "character": 8}}, timeout=60)
            with pytest.raises(Exception):
                w.request("workspace/nothingLikeThis", {})
        wait_until(lambda: any(e["ev"] == "session_close" for e in session_events(bridge_container, path, session)),
                   timeout=15, message="the session's end was never recorded")
        events = session_events(bridge_container, path, session)
        names = [e["ev"] for e in events]
        assert names[0] == "session_open" and names[-1] == "session_close", names

        did_open = next(e for e in events if e["ev"] == "recv" and e["method"] == "textDocument/didOpen")
        assert did_open["uri"] == uri(SHAPES) and did_open["length"] == len(bridge_container.read_file(SHAPES))
        answered = next(e for e in events if e["ev"] == "response" and e["method"] == "textDocument/definition")
        assert answered["ms"] > 0 and "code" not in answered
        refused = next(e for e in events if e["ev"] == "response" and e["method"] == "workspace/nothingLikeThis")
        assert refused["lvl"] == "warn" and refused["code"] == -32601, refused
        assert events[-1]["messages"] >= 5 and "why" in events[-1]

    def test_no_code_reaches_the_log_by_default(self, bridge, bridge_container, path):
        with Wire(bridge.port) as w:
            session = w.initialize()["serverInfo"]["session"]
            w.did_open(SHAPES, f"// {SECRET}\n" + bridge_container.read_file(SHAPES))
            w.did_change(SHAPES, 1, replace_range(0, 0, 0, SECRET))
            w.request("textDocument/hover", {"textDocument": {"uri": uri(SHAPES)},
                                             "position": {"line": 10, "character": 8}}, timeout=60)
        wait_until(lambda: any(e["ev"] == "session_close" for e in session_events(bridge_container, path, session)),
                   timeout=15)
        assert SECRET not in bridge_container.read_file(path)

    def test_trace_is_opt_in_and_records_payloads(self, bridge, bridge_container, path, wire):
        try:
            assert wire.request("$/ij/log", {"level": "trace"})["level"] == "trace"
            with Wire(bridge.port) as w:
                session = w.initialize()["serverInfo"]["session"]
                w.send_request("workspace/symbol", {"query": SECRET})
                time.sleep(1)
            wait_until(lambda: any(SECRET in json.dumps(e) for e in session_events(bridge_container, path, session)),
                       timeout=15, message="trace recorded no payload")
        finally:
            wire.request("$/ij/log", {"level": "debug"})

    def test_a_failure_carries_its_type_and_a_stack(self, bridge, bridge_container, path):
        with Wire(bridge.port) as w:
            session = w.initialize()["serverInfo"]["session"]
            # A file that does not exist: didOpen fails inside the Brain.
            w.notify("textDocument/didOpen", {"textDocument": {
                "uri": "file:///work/fixture/src/does/not/exist.kt", "version": 0, "text": "x", "languageId": "kotlin"}})
            time.sleep(2)
        wait_until(lambda: any(e["ev"] == "error" for e in session_events(bridge_container, path, session)),
                   timeout=15, message="the failure was not recorded")
        error = next(e for e in session_events(bridge_container, path, session) if e["ev"] == "error")
        assert error["where"] == "textDocument/didOpen"
        assert "IllegalArgumentException" in error["type"] and "no file for" in error["message"]
        assert "  at dev.bridge.brain" in error["stack"]

    def test_slow_requests_are_noticed_even_when_only_the_brains_life_is_recorded(self, bridge, bridge_container, path, wire):
        try:
            wire.request("$/ij/log", {"level": "info"})
            wire.request("$/ij/debug/navigationDelay", {"ms": 600})
            with Wire(bridge.port) as w:
                session = w.initialize()["serverInfo"]["session"]
                w.did_open(SHAPES, bridge_container.read_file(SHAPES))
                w.request("textDocument/hover", {"textDocument": {"uri": uri(SHAPES)},
                                                 "position": {"line": 10, "character": 8}}, timeout=60)
            wait_until(lambda: any(e["ev"] == "session_close" for e in session_events(bridge_container, path, session)), timeout=15)
        finally:
            wire.request("$/ij/debug/navigationDelay", {"ms": 0})
            wire.request("$/ij/log", {"level": "debug"})
        events = session_events(bridge_container, path, session)
        slow = [e for e in events if e["ev"] == "slow_response"]
        assert slow and slow[0]["method"] == "textDocument/hover" and slow[0]["ms"] >= 600, events
        assert not [e for e in events if e["ev"] == "recv"], "info records the Brain's life, not every message"

    def test_indexing_and_readiness_are_recorded(self, wire, bridge_container, path):
        wire.request("$/ij/debug/indexing", {"ms": 2500})
        wait_until(lambda: wire.debug_state()["state"] == "Indexing", timeout=15)
        wait_until(lambda: wire.debug_state()["state"] == "Ready", timeout=60)

        def states():
            return [e["state"] for e in brain_log(bridge_container, path) if e["ev"] == "status"]
        # The Brain notices a change on its next look, a moment after the state itself has changed.
        wait_until(lambda: states()[-1:] == ["Ready"], timeout=15, message=f"the log's last states: {states()[-6:]}")
        assert "Indexing" in states()

    def test_the_start_of_the_brain_is_recorded_with_where_it_runs(self, bridge_container, path):
        starts = [e for e in brain_log(bridge_container, path) if e["ev"] == "brain_start"]
        if not starts:
            pytest.skip("the start of this Brain has been rotated out of the log")
        start = starts[0]
        assert start["root"] == "/work/fixture" and start["socket"].endswith(".sock")
        assert start["java"] and start["os"] and start["plugin"] == BRAIN_VERSION

    def test_the_file_is_private(self, bridge_container, path):
        mode = bridge_container.exec(f"stat -c %a {path}").stdout.strip()
        assert mode == "600", mode

    def test_it_cannot_grow_without_bound(self, bridge, bridge_container, path, wire):
        wire.request("$/ij/log", {"maxBytes": 20000})
        try:
            with Wire(bridge.port) as w:
                w.initialize()
                for _ in range(400):
                    w.send_request("workspace/nothingLikeThis", {})
                time.sleep(2)
            sizes = bridge_container.exec(f"ls -l {path}*").stdout
            assert f"{path}.1" in sizes, sizes
            assert f"{path}.4" not in sizes, "rotation keeps three old files"
            assert int(bridge_container.exec(f"stat -c %s {path}").stdout) < 20000 + 4000
        finally:
            wire.request("$/ij/log", {"maxBytes": 5 * 1024 * 1024})


# ===================================================================== the Editor
def editor_log(nvim, container) -> list[dict]:
    p = nvim.exec_lua("return require('ij_bridge.log').path")
    return lines_of(container.read_file(p))


class TestEditorLog:

    def test_the_two_logs_share_a_session_id(self, nvim, bridge_container, bridge):
        nvim.command(f"edit {SHAPES}")
        wait_until(lambda: attached(nvim) == 1)

        def sessions():
            return [e for e in editor_log(nvim, bridge_container) if e["ev"] == "session" and e.get("session")]
        before = len(sessions())
        # A Session of its own for this test: the Brain's log is rotated by other tests, and an
        # old Session's first line may be long gone.
        nvim.exec_lua("for _, c in ipairs(vim.lsp.get_clients({ name = 'ij-bridge' })) do c:stop(true) end")
        wait_until(lambda: len(sessions()) > before and attached(nvim) == 1, timeout=60,
                   message="the Editor never logged a new Session")
        session = sessions()[-1]
        assert session["brain_log"], "the Brain says where its log is"
        theirs = [e for e in lines_of(bridge_container.read_file(session["brain_log"]))
                  if e.get("sess") == session["session"] and e["ev"] == "session_open"]
        assert theirs, "the Brain has no session with the id the Editor printed"

    def test_requests_and_replies_are_recorded_with_how_long_they_took(self, nvim, bridge_container):
        nvim.command(f"edit {SHAPES}")
        wait_until(lambda: attached(nvim) == 1)
        nvim.exec_lua("""vim.lsp.buf_request_sync(0, 'textDocument/hover', {
            textDocument = vim.lsp.util.make_text_document_params(), position = { line = 10, character = 8 } }, 30000)""")
        events = editor_log(nvim, bridge_container)
        request = [e for e in events if e["ev"] == "request" and e["method"] == "textDocument/hover"]
        reply = [e for e in events if e["ev"] == "response" and e["method"] == "textDocument/hover"]
        assert request and reply and reply[-1]["ms"] > 0 and reply[-1]["id"] == request[-1]["id"]

    def test_the_buffers_text_never_reaches_it(self, nvim, bridge_container):
        nvim.command(f"edit {SHAPES}")
        wait_until(lambda: attached(nvim) == 1)
        nvim.current.buffer.append(f"// {SECRET}", 0)
        time.sleep(1.5)
        p = nvim.exec_lua("return require('ij_bridge.log').path")
        text = bridge_container.read_file(p)
        assert SECRET not in text
        assert any(e["ev"] == "send" and e["method"] == "textDocument/didChange" for e in lines_of(text))

    def test_the_loss_of_the_brain_is_recorded(self, nvim, bridge_container):
        """Neovim's own error and the Bridge's reaction to it, in the order they happened."""
        nvim.command(f"edit {SHAPES}")
        wait_until(lambda: attached(nvim) == 1)
        nvim.exec_lua("""
            local client = vim.lsp.get_clients({ name = 'ij-bridge' })[1]
            client.config.on_error(vim.lsp.rpc.client_errors.READ_ERROR, 'ECONNRESET')""")
        wait_until(lambda: any(e["ev"] == "conn" and "lost" in e["what"] for e in editor_log(nvim, bridge_container)),
                   message="the loss was not recorded")
        events = editor_log(nvim, bridge_container)
        assert any(e["ev"] == "transport_error" for e in events)
        wait_until(lambda: attached(nvim) == 1, timeout=60)

    def test_report_gathers_what_a_bug_report_needs(self, nvim, bridge_container):
        nvim.command(f"edit {SHAPES}")
        wait_until(lambda: attached(nvim) == 1)
        out = nvim.exec_lua("return require('ij_bridge').report()")
        text = "\n".join(out)
        for needle in ("# ij-nvim-bridge report", "nvim ", "statusline:", "sessions:", "editor log:",
                       "brain log:", "## recent connection events", "## editor log", "## brain log"):
            assert needle in text, needle
        assert '"ev":"session"' in text.replace(" ", "") or "session" in text

    def test_the_level_is_set_for_both_sides_and_trace_is_only_the_brains(self, nvim, bridge_container, wire):
        nvim.command(f"edit {SHAPES}")
        wait_until(lambda: attached(nvim) == 1)
        try:
            nvim.command("IjBridge loglevel trace")
            wait_until(lambda: wire.request("$/ij/log", {})["level"] == "trace", message="the Brain's level did not change")
            assert nvim.exec_lua("return require('ij_bridge.log').level") == "debug"
            nvim.command("IjBridge loglevel nonsense")
            assert wire.request("$/ij/log", {})["level"] == "trace", "nonsense must change nothing"
        finally:
            nvim.command("IjBridge loglevel debug")
            wait_until(lambda: wire.request("$/ij/log", {})["level"] == "debug")
