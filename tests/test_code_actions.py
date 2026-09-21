"""Organize imports (FEATURES.md §3), as a `source.organizeImports` code action.

Titles now, edits on resolve (D3): listing is cheap and computes nothing;
`codeAction/resolve` computes the edit on a copy of the file (D2) and returns it
for Neovim to apply. The Mirror and the disk are never touched.
"""
from __future__ import annotations

import time

import pytest

from harness.util import wait_until
from harness.wire import SRC, RpcError, replace_range, uri
from test_formatting import apply_edits
from test_navigation import attached

PROBE = f"{SRC}/probe"
KOTLIN = f"{PROBE}/InspectionWarning.kt"
JAVA = f"{PROBE}/JavaShapes.java"

ORGANIZE = "source.organizeImports"

MESSY_KOTLIN = '''package dev.bridge.fixture.probe

import java.util.UUID
import java.util.Date
import java.time.Instant
import java.io.File

class ImportUser {
    fun id(): UUID = UUID.randomUUID()
    fun now(): Instant = Instant.now()
}
'''

ORGANIZED_KOTLIN = '''package dev.bridge.fixture.probe

import java.time.Instant
import java.util.UUID

class ImportUser {
    fun id(): UUID = UUID.randomUUID()
    fun now(): Instant = Instant.now()
}
'''

MESSY_JAVA = '''package dev.bridge.fixture.probe;

import java.util.List;
import java.util.Date;
import java.util.ArrayList;
import java.io.File;

public class JavaImports {
    public ArrayList<String> make() { return new ArrayList<>(); }
    public List<String> other() { return null; }
}
'''

ORGANIZED_JAVA = '''package dev.bridge.fixture.probe;

import java.util.ArrayList;
import java.util.List;

public class JavaImports {
    public ArrayList<String> make() { return new ArrayList<>(); }
    public List<String> other() { return null; }
}
'''


def actions(w, path, only=None, version=None):
    ctx = {"diagnostics": []}
    if only is not None:
        ctx["only"] = only
    return w.request("textDocument/codeAction", {
        "textDocument": {"uri": uri(path)},
        "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}},
        "context": ctx}, timeout=60)


def resolve(w, action):
    return w.request("codeAction/resolve", action, timeout=90)


def organize(w, path, text):
    """Offer, resolve and apply organize-imports, as a client would."""
    w.did_open(path, text)
    (action,) = [a for a in actions(w, path) if a["kind"] == ORGANIZE]
    edit = resolve(w, action)["edit"]["changes"][uri(path)]
    return apply_edits(text, edit)


# ------------------------------------------------------------------ wire
class TestOrganizeImports:

    def test_kotlin_removes_unused_and_sorts(self, wire):
        assert organize(wire, KOTLIN, MESSY_KOTLIN) == ORGANIZED_KOTLIN

    def test_java_removes_unused_and_sorts(self, wire):
        assert organize(wire, JAVA, MESSY_JAVA) == ORGANIZED_JAVA

    def test_listing_is_cheap_titles_now_edits_later(self, wire):
        """D3: the offered action carries no edit; that is what resolve is for."""
        wire.did_open(KOTLIN, MESSY_KOTLIN)
        (action,) = [a for a in actions(wire, KOTLIN) if a["kind"] == ORGANIZE]
        assert action["title"] == "Organize imports"
        assert "edit" not in action, "the edit was computed while merely listing"
        assert action["data"]["uri"] == uri(KOTLIN)

    def test_it_organizes_the_unsaved_buffer_not_the_file_on_disk(self, wire, bridge_container):
        on_disk = bridge_container.read_file(KOTLIN)
        assert organize(wire, KOTLIN, MESSY_KOTLIN) == ORGANIZED_KOTLIN
        assert on_disk != MESSY_KOTLIN

    def test_already_organized_yields_no_edits(self, wire):
        wire.did_open(KOTLIN, ORGANIZED_KOTLIN)
        (action,) = [a for a in actions(wire, KOTLIN) if a["kind"] == ORGANIZE]
        assert resolve(wire, action)["edit"]["changes"][uri(KOTLIN)] == []

    def test_a_request_changes_neither_the_mirror_nor_the_disk(self, wire, bridge_container):
        before = bridge_container.read_file(KOTLIN)
        organize(wire, KOTLIN, MESSY_KOTLIN)
        (m,) = wire.debug_state(text=True)["mirrors"]
        assert m["text"] == MESSY_KOTLIN and m["version"] == 0
        assert bridge_container.read_file(KOTLIN) == before

    def test_the_only_filter_is_honoured(self, wire):
        wire.did_open(KOTLIN, MESSY_KOTLIN)
        assert [a["kind"] for a in actions(wire, KOTLIN, only=[ORGANIZE])] == [ORGANIZE]
        assert [a["kind"] for a in actions(wire, KOTLIN, only=["source"])] == [ORGANIZE], "a parent kind matches"
        assert actions(wire, KOTLIN, only=["quickfix"]) == []
        assert actions(wire, KOTLIN, only=["refactor.extract"]) == []

    def test_resolving_against_a_changed_buffer_is_content_modified(self, wire):
        """The imports were computed for text that no longer exists."""
        wire.did_open(KOTLIN, MESSY_KOTLIN)
        (action,) = [a for a in actions(wire, KOTLIN) if a["kind"] == ORGANIZE]
        wire.did_change(KOTLIN, 1, replace_range(0, 0, 0, "// edited after the action was offered\n"))
        with pytest.raises(RpcError) as e:
            resolve(wire, action)
        assert e.value.code == -32801

    def test_a_change_mid_resolve_is_content_modified_not_stale(self, wire):
        wire.did_open(KOTLIN, MESSY_KOTLIN)
        (action,) = [a for a in actions(wire, KOTLIN) if a["kind"] == ORGANIZE]
        wire.request("$/ij/debug/navigationDelay", {"ms": 1500})
        try:
            rid = wire.send_request("codeAction/resolve", action)
            time.sleep(0.3)
            wire.did_change(KOTLIN, 1, replace_range(0, 0, 0, "// changed\n"))
            assert wire.await_response(rid, timeout=15)["error"]["code"] == -32801
        finally:
            wire.request("$/ij/debug/navigationDelay", {"ms": 0})

    def test_while_indexing_it_says_so_it_needs_the_indices(self, wire):
        wire.did_open(KOTLIN, MESSY_KOTLIN)
        (action,) = [a for a in actions(wire, KOTLIN) if a["kind"] == ORGANIZE]
        wire.request("$/ij/debug/indexing", {"ms": 4000})
        wait_until(lambda: wire.debug_state()["state"] == "Indexing", timeout=15)
        with pytest.raises(RpcError) as e:
            resolve(wire, action)
        assert e.value.code == -32801
        wait_until(lambda: wire.debug_state()["state"] == "Ready", timeout=60)

    def test_it_is_advertised_with_its_kinds_and_lazy_resolve(self, bridge):
        from harness.wire import Wire
        with Wire(bridge.port) as w:
            caps = w.initialize()["capabilities"]
        assert caps["codeActionProvider"]["codeActionKinds"] == [ORGANIZE]
        assert caps["codeActionProvider"]["resolveProvider"] is True


# ============================================== through Neovim's own built-ins
def open_in_nvim(nvim, path, text):
    nvim.command(f"edit {path}")
    wait_until(lambda: attached(nvim) == 1, message="never attached")
    nvim.current.buffer[:] = text.rstrip("\n").split("\n")


class TestThroughNeovimsBuiltins:

    APPLY = """vim.lsp.buf.code_action({
        context = { only = { 'source.organizeImports' } }, apply = true })"""

    def test_code_action_organizes_kotlin_imports(self, nvim):
        open_in_nvim(nvim, KOTLIN, MESSY_KOTLIN)
        nvim.exec_lua(self.APPLY)
        wait_until(lambda: "\n".join(nvim.current.buffer[:]) + "\n" == ORGANIZED_KOTLIN, timeout=30,
                   message="the imports were never organized")

    def test_code_action_organizes_java_imports(self, nvim):
        open_in_nvim(nvim, JAVA, MESSY_JAVA)
        nvim.exec_lua(self.APPLY)
        wait_until(lambda: "\n".join(nvim.current.buffer[:]) + "\n" == ORGANIZED_JAVA, timeout=30,
                   message="the imports were never organized")

    def test_the_action_is_offered_in_the_ordinary_code_action_list(self, nvim):
        """`<leader>ca` with no filter: it appears among the actions, as any server's would."""
        open_in_nvim(nvim, KOTLIN, MESSY_KOTLIN)
        titles = nvim.exec_lua("""
            local res = vim.lsp.buf_request_sync(0, 'textDocument/codeAction', {
              textDocument = vim.lsp.util.make_text_document_params(),
              range = { start = { line = 0, character = 0 }, ['end'] = { line = 0, character = 0 } },
              context = { diagnostics = {} } }, 30000)
            local out = {}
            for _, r in pairs(res or {}) do
              for _, a in ipairs(r.result or {}) do table.insert(out, a.title) end
            end
            return out""")
        assert "Organize imports" in titles, titles

    def test_organizing_leaves_the_disk_alone_until_the_developer_writes(self, nvim, bridge_container):
        on_disk = bridge_container.read_file(KOTLIN)
        open_in_nvim(nvim, KOTLIN, MESSY_KOTLIN)
        nvim.exec_lua(self.APPLY)
        wait_until(lambda: "\n".join(nvim.current.buffer[:]) + "\n" == ORGANIZED_KOTLIN, timeout=30)
        assert bridge_container.read_file(KOTLIN) == on_disk

    def test_the_mirror_follows_the_organized_buffer(self, nvim, probe):
        open_in_nvim(nvim, KOTLIN, MESSY_KOTLIN)
        nvim.exec_lua(self.APPLY)
        wait_until(lambda: "\n".join(nvim.current.buffer[:]) + "\n" == ORGANIZED_KOTLIN, timeout=30)
        wait_until(lambda: [m for m in probe.debug_state(text=True)["mirrors"]
                            if m["uri"].endswith("InspectionWarning.kt")][0]["text"] == ORGANIZED_KOTLIN,
                   timeout=15, message="the Mirror never caught up with the organized buffer")
