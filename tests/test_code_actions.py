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
        assert caps["codeActionProvider"]["codeActionKinds"] == \
            [ORGANIZE, "source.generate", "quickfix", "refactor.rewrite", "refactor.inline"]
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


# ============================================================ quick fixes and intentions
KOTLIN_FIXABLE = '''package dev.bridge.fixture.probe

class Q {
    fun a(x: Int): String {
        val unused = 5
        return "value: " + x
    }
    fun b(list: List<Int>): Boolean {
        if (list.size == 0) return true
        return false
    }
    fun c(s: String?) = s!!.length
}
'''

JAVA_FIXABLE = '''package dev.bridge.fixture.probe;

import java.util.List;

public class Q {
    public boolean eq(String a) { return a == "x"; }
    public void unused() { int neverUsed = 3; String s = null; }
}
'''


def at_pos(text: str, needle: str, into: int) -> dict:
    idx = text.index(needle) + into
    line = text.count("\n", 0, idx)
    return {"line": line, "character": idx - (text.rfind("\n", 0, idx) + 1)}


def offered(w, path, text, needle, into, title, only=None, timeout=60):
    """What IntelliJ offers at that place, asked again until it offers `title`: the analysis
    behind a quick fix arrives a moment after the file is opened, and a developer presses the key again."""
    pos = at_pos(text, needle, into)
    deadline = time.monotonic() + timeout
    while True:
        ctx = {"diagnostics": []}
        if only is not None:
            ctx["only"] = only
        try:
            found = w.request("textDocument/codeAction", {
                "textDocument": {"uri": uri(path)}, "range": {"start": pos, "end": pos}, "context": ctx}, timeout=90)
        except RpcError as e:
            if e.code != -32801 or time.monotonic() > deadline:      # "IntelliJ is indexing; ask again"
                raise
            time.sleep(2)
            continue
        match = [a for a in found if title in a["title"]]
        if match or time.monotonic() > deadline:
            assert match, f"{title!r} never offered; got {[a['title'] for a in found]}"
            return match[0], found
        time.sleep(2)


def applied(w, path, text, needle, into, title, **kw):
    action, _ = offered(w, path, text, needle, into, title, **kw)
    for attempt in range(10):
        try:
            resolved = w.request("codeAction/resolve", action, timeout=90)
            break
        except RpcError as e:
            if e.code != -32801 or "indexing" not in str(e) and "not ready" not in str(e) or attempt == 9:
                raise
            time.sleep(2)
    return apply_edits(text, resolved["edit"]["changes"].get(uri(path), [])), resolved


class TestQuickFixesAndIntentions:

    def test_kotlin_a_quick_fix_for_a_warning(self, wire):
        wire.did_open(KOTLIN, KOTLIN_FIXABLE)
        out, _ = applied(wire, KOTLIN, KOTLIN_FIXABLE, "list.size == 0", 10, "isEmpty()")
        assert "if (list.isEmpty()) return true" in out, out

    def test_kotlin_concatenation_becomes_a_template(self, wire):
        wire.did_open(KOTLIN, KOTLIN_FIXABLE)
        out, _ = applied(wire, KOTLIN, KOTLIN_FIXABLE, '"value: " + x', 12, "Convert concatenation to template")
        assert 'return "value: $x"' in out, out

    def test_kotlin_an_unused_variable_is_removed(self, wire):
        wire.did_open(KOTLIN, KOTLIN_FIXABLE)
        out, _ = applied(wire, KOTLIN, KOTLIN_FIXABLE, "val unused", 6, "Remove variable 'unused'")
        assert "val unused" not in out and 'return "value: " + x' in out, out

    def test_kotlin_an_intention_that_is_not_a_fix(self, wire):
        wire.did_open(KOTLIN, KOTLIN_FIXABLE)
        action, _ = offered(wire, KOTLIN, KOTLIN_FIXABLE, "s!!.length", 2, "Convert to block body")
        assert action["kind"] == "refactor.rewrite"
        out, _ = applied(wire, KOTLIN, KOTLIN_FIXABLE, "s!!.length", 2, "Convert to block body")
        assert "return s!!.length" in out, out

    def test_java_equals_instead_of_double_equals(self, wire):
        wire.did_open(JAVA, JAVA_FIXABLE)
        out, _ = applied(wire, JAVA, JAVA_FIXABLE, 'a == "x"', 3, "Replace '==' with 'equals()'")
        assert ".equals(" in out and 'a == "x"' not in out, out

    def test_java_an_unused_local_is_removed(self, wire):
        wire.did_open(JAVA, JAVA_FIXABLE)
        out, _ = applied(wire, JAVA, JAVA_FIXABLE, "int neverUsed", 6, "Remove local variable 'neverUsed'")
        assert "neverUsed" not in out, out

    def test_the_kinds_are_told_apart_and_filtered(self, wire):
        wire.did_open(KOTLIN, KOTLIN_FIXABLE)
        offered(wire, KOTLIN, KOTLIN_FIXABLE, "list.size == 0", 10, "isEmpty()")
        kinds = lambda only: sorted({a["kind"] for a in wire.request("textDocument/codeAction", {
            "textDocument": {"uri": uri(KOTLIN)}, "context": {"diagnostics": [], "only": only},
            "range": {"start": at_pos(KOTLIN_FIXABLE, "list.size == 0", 10), "end": at_pos(KOTLIN_FIXABLE, "list.size == 0", 10)}},
            timeout=60)})
        assert kinds(["quickfix"]) == ["quickfix"]
        assert kinds(["refactor"]) == ["refactor.rewrite"], "a parent kind matches what is under it"
        assert kinds(["source"]) == [ORGANIZE]
        assert kinds(["refactor.extract"]) == []

    def test_a_request_changes_neither_the_mirror_nor_the_disk(self, wire, bridge_container):
        on_disk = bridge_container.read_file(KOTLIN)
        wire.did_open(KOTLIN, KOTLIN_FIXABLE)
        applied(wire, KOTLIN, KOTLIN_FIXABLE, "list.size == 0", 10, "isEmpty()")
        (m,) = wire.debug_state(text=True)["mirrors"]
        assert m["text"] == KOTLIN_FIXABLE and m["version"] == 0
        assert bridge_container.read_file(KOTLIN) == on_disk

    def test_an_action_for_text_that_has_changed_is_content_modified(self, wire):
        wire.did_open(KOTLIN, KOTLIN_FIXABLE)
        action, _ = offered(wire, KOTLIN, KOTLIN_FIXABLE, "list.size == 0", 10, "isEmpty()")
        wire.did_change(KOTLIN, 1, replace_range(0, 0, 0, "// edited\n"))
        with pytest.raises(RpcError) as e:
            wire.request("codeAction/resolve", action, timeout=60)
        assert e.value.code == -32801

    def test_an_action_that_needs_a_choice_says_so_instead_of_doing_half(self, wire):
        """"Specify type explicitly" starts a live template: not a text edit."""
        wire.did_open(KOTLIN, KOTLIN_FIXABLE)
        action, _ = offered(wire, KOTLIN, KOTLIN_FIXABLE, "val unused", 6, "Specify type explicitly")
        with pytest.raises(RpcError) as e:
            wire.request("codeAction/resolve", action, timeout=60)
        assert e.value.code == -32602 and "needs more than a text edit" in str(e.value)

    def test_listing_computes_no_edit(self, wire):
        wire.did_open(KOTLIN, KOTLIN_FIXABLE)
        _, found = offered(wire, KOTLIN, KOTLIN_FIXABLE, "list.size == 0", 10, "isEmpty()")
        assert all("edit" not in a for a in found)


class TestQuickFixesThroughNeovim:

    def test_the_action_menu_applies_a_quick_fix(self, nvim):
        open_in_nvim(nvim, KOTLIN, KOTLIN_FIXABLE)
        pos = at_pos(KOTLIN_FIXABLE, "list.size == 0", 10)
        nvim.current.window.cursor = (pos["line"] + 1, pos["character"])

        def done():
            nvim.exec_lua("""vim.lsp.buf.code_action({
                filter = function(a) return a.title:find('isEmpty', 1, true) ~= nil end, apply = true })""")
            time.sleep(3)
            return "list.isEmpty()" in "\n".join(nvim.current.buffer[:])
        wait_until(done, timeout=90, interval=2, message="the quick fix was never applied:\n" + "\n".join(nvim.current.buffer[:]))



class TestChoicesAndNoOps:
    """An action with choices is one action per choice; an action that only navigates is not offered."""

    def test_an_action_with_choices_is_one_action_per_choice(self, wire):
        wire.did_open(KOTLIN, KOTLIN_FIXABLE)
        _, found = offered(wire, KOTLIN, KOTLIN_FIXABLE, "fun c(", 0, "Change visibility: private")
        titles = [a["title"] for a in found]
        assert {"Change visibility: private", "Change visibility: protected", "Change visibility: internal"} <= set(titles), titles
        assert not any(t.endswith("…") for t in titles), titles    # the parent, which alone asked a question

    def test_a_choice_applies_its_own_edit(self, wire):
        wire.did_open(KOTLIN, KOTLIN_FIXABLE)
        result, _ = applied(wire, KOTLIN, KOTLIN_FIXABLE, "fun c(", 0, "Change visibility: private")
        assert "private fun c(" in result, result
        result, _ = applied(wire, KOTLIN, KOTLIN_FIXABLE, "fun c(", 0, "Change visibility: internal")
        assert "internal fun c(" in result, result

    def test_a_choice_in_another_action_too(self, wire):
        wire.did_open(JAVA, JAVA_FIXABLE)
        result, _ = applied(wire, JAVA, JAVA_FIXABLE, "int neverUsed = 3", 17, "Convert number to: Hex")
        assert "int neverUsed = 0x3;" in result, result

    def test_choices_are_offered_without_computing_the_edit(self, wire):
        wire.did_open(KOTLIN, KOTLIN_FIXABLE)
        _, found = offered(wire, KOTLIN, KOTLIN_FIXABLE, "fun c(", 0, "Change visibility: private")
        assert all("edit" not in a for a in found)

    def test_an_action_that_only_navigates_or_copies_is_not_offered(self, wire):
        """"Navigate to duplicate class" was offered, selectable, and did nothing."""
        wire.did_open(JAVA, JAVA_FIXABLE)
        time.sleep(8)
        titles = set()
        for ln, line in enumerate(JAVA_FIXABLE.split("\n")):
            for ch in range(0, len(line), 3):
                pos = {"line": ln, "character": ch}
                found = wire.request("textDocument/codeAction", {
                    "textDocument": {"uri": uri(JAVA)}, "range": {"start": pos, "end": pos}, "context": {"diagnostics": []}}, timeout=90)
                titles.update(a["title"] for a in found)
        assert titles, "nothing was offered at all"
        assert not [t for t in titles if t.startswith(("Navigate to", "Copy "))], sorted(titles)


class TestSuppress:
    """"Suppress for statement / method / class" sit under the fix's arrow in IntelliJ's popup, not in its list."""

    def test_java_offers_the_suppress_actions_for_a_warning(self, wire):
        wire.did_open(JAVA, JAVA_FIXABLE)
        _, found = offered(wire, JAVA, JAVA_FIXABLE, "eq(String", 2, "Suppress for method")
        titles = {a["title"] for a in found}
        assert {"Suppress for method", "Suppress for class"} <= titles, titles

    def test_suppressing_for_a_method_annotates_it(self, wire):
        wire.did_open(JAVA, JAVA_FIXABLE)
        result, _ = applied(wire, JAVA, JAVA_FIXABLE, "eq(String", 2, "Suppress for method")
        assert "@SuppressWarnings(" in result, result

    def test_suppressing_for_a_statement_comments_it(self, wire):
        wire.did_open(JAVA, JAVA_FIXABLE)
        result, _ = applied(wire, JAVA, JAVA_FIXABLE, "a == ", 4, "Suppress for statement")
        assert "//noinspection" in result or "@SuppressWarnings(" in result, result
