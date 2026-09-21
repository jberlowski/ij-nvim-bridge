"""Gradle's Kotlin scripts (`*.gradle.kts`, FEATURES.md §6d): Kotlin files that IntelliJ analyses against the
Gradle model, so the DSL resolves: `plugins {}`, `dependencies {}`, `implementation(...)`.

Each feature is asserted on the wire and, for the ones a developer meets first, through Neovim's own calls. A
deliberate error is the control for "no errors": a clean report only means something if a real one is reported.
"""
from __future__ import annotations

import time

import pytest

from harness.util import wait_until
from harness.wire import RpcError, uri
from test_bridge_slice import published
from test_editor_slice import attached
from test_formatting import apply_edits
from test_navigation import at

BUILD = "/work/fixture/build.gradle.kts"
SETTINGS = "/work/fixture/settings.gradle.kts"


def ask(w, method, params, timeout=90):
    """Ask again while IntelliJ says "indexing": a Gradle script is analysed only once the model is loaded."""
    for attempt in range(20):
        try:
            return w.request(method, params, timeout=timeout)
        except RpcError as e:
            if e.code != -32801 or attempt == 19:
                raise
            time.sleep(3)


def pos(text: str, needle: str, into: int = 0) -> dict:
    line, col = at(text, needle, 0, into)
    return {"line": line, "character": col}


@pytest.fixture
def build(wire, bridge_container):
    text = bridge_container.read_file(BUILD)
    wire.did_open(BUILD, text)
    return text


def errors(w, path, settle=True):
    """The errors IntelliJ reports for the file, once its analysis has settled."""
    pub = published(w, path, lambda d: True, timeout=90)
    if settle:
        time.sleep(4)
        try:
            pub = published(w, path, lambda d: True, timeout=8)
        except (AssertionError, TimeoutError):
            pass
    return [d["message"] for d in pub["diagnostics"] if d["severity"] == 1]


class TestBuildScript:

    def test_the_real_script_has_no_errors_and_a_real_error_is_reported(self, wire, build):
        """The DSL resolves (no false 'unresolved reference' on `plugins`, `repositories`, `implementation`),
        and the check is alive: an error typed into the script is reported, and clears when it is removed."""
        assert errors(wire, BUILD) == []
        broken = build.replace("    runtimeOnly(", "    implementation(thisDoesNotExist)\n    runtimeOnly(", 1)
        wire.did_change(BUILD, 1, {"text": broken})
        wait_until(lambda: any("thisDoesNotExist" in m or "Unresolved" in m for m in errors(wire, BUILD, settle=False)),
                   timeout=90, interval=1, message="the deliberate error in the script was never reported")
        wire.did_change(BUILD, 2, {"text": build})
        wait_until(lambda: errors(wire, BUILD, settle=False) == [], timeout=90, interval=1)

    def test_hover_shows_the_gradle_dsl(self, wire, build):
        hover = ask(wire, "textDocument/hover", {
            "textDocument": {"uri": uri(BUILD)},
            "position": pos(build, 'implementation("org.springframework.boot:spring-boot-starter-web")', 3)})
        assert "DependencyHandler.implementation" in hover["contents"]["value"], hover

    def test_definition_goes_into_the_gradle_api(self, wire, build):
        loc = ask(wire, "textDocument/definition", {"textDocument": {"uri": uri(BUILD)}, "position": pos(build, "mavenCentral", 3)})
        assert loc and loc[0]["uri"].endswith("RepositoryHandler.java"), loc
        assert "/.cache/ij-nvim-bridge/library/" in loc[0]["uri"], "library code is extracted read-only (D1)"

    def test_definition_of_the_kotlin_plugin_helper(self, wire, build):
        loc = ask(wire, "textDocument/definition", {"textDocument": {"uri": uri(BUILD)}, "position": pos(build, 'kotlin("jvm")', 2)})
        assert loc and "KotlinDependencyExtensions" in loc[0]["uri"], loc

    def test_completion_inside_dependencies(self, wire, build):
        new = build.replace("    runtimeOnly(", "    impl\n    runtimeOnly(", 1)
        wire.did_change(BUILD, 1, {"text": new})
        line, col = at(new, "    impl\n", 0, 8)
        for attempt in range(6):
            first, batches = wire.complete(BUILD, line, col, timeout=60)
            labels = [i["label"] for i in first["items"] + [i for b in batches for i in b["items"]]]
            if "implementation" in labels:
                break
            time.sleep(3)
        assert "implementation" in labels and "testImplementation" in labels, labels

    def test_symbols_are_the_top_level_blocks(self, wire, build):
        symbols = ask(wire, "textDocument/documentSymbol", {"textDocument": {"uri": uri(BUILD)}})
        names = [s["name"] for s in symbols]
        assert {"plugins", "group", "version", "java", "repositories", "dependencies"} <= set(names), names

    def test_the_blocks_fold(self, wire, build):
        ranges = ask(wire, "textDocument/foldingRange", {"textDocument": {"uri": uri(BUILD)}})
        assert ranges and ranges[0]["startLine"] == 0, ranges[:2]
        assert any(r["startLine"] == pos(build, "dependencies {")["line"] for r in ranges), "the dependencies block folds"

    def test_a_messy_script_is_formatted_in_the_ides_style(self, wire, build):
        messy = "plugins {\nkotlin(\"jvm\")   version \"2.4.20\"\n}\ndependencies {\nimplementation(  \"a:b\"  )\n}\n"
        wire.did_change(BUILD, 1, {"text": messy})
        edits = ask(wire, "textDocument/formatting", {"textDocument": {"uri": uri(BUILD)},
                                                     "options": {"tabSize": 8, "insertSpaces": False}})
        out = apply_edits(messy, edits)
        assert '    kotlin("jvm") version "2.4.20"' in out and '    implementation("a:b")' in out, out

    def test_organize_imports_is_not_offered_for_a_script(self, wire, build):
        """The optimiser cannot run on a copy of a script (it needs the script's own analysis context and
        fails inside K2 on the DSL's calls), so the action is not offered, rather than offered to fail."""
        text = "import java.io.File\nimport java.util.Date\n\n" + build.replace("group = ", "val d = Date()\ngroup = ", 1)
        wire.did_change(BUILD, 1, {"text": text})
        actions = ask(wire, "textDocument/codeAction", {
            "textDocument": {"uri": uri(BUILD)}, "context": {"diagnostics": []},
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}})
        assert not [a for a in actions if a["kind"] == "source.organizeImports"], [a["title"] for a in actions]

    def test_a_request_changes_neither_the_mirror_nor_the_disk(self, wire, build, bridge_container):
        on_disk = bridge_container.read_file(BUILD)
        ask(wire, "textDocument/hover", {"textDocument": {"uri": uri(BUILD)}, "position": pos(build, "repositories", 3)})
        (m,) = wire.debug_state(text=True)["mirrors"]
        assert m["text"] == build and bridge_container.read_file(BUILD) == on_disk


class TestSettingsScript:

    def test_settings_gradle_kts_resolves_too(self, wire, bridge_container):
        text = bridge_container.read_file(SETTINGS)
        wire.did_open(SETTINGS, text)
        hover = ask(wire, "textDocument/hover", {"textDocument": {"uri": uri(SETTINGS)}, "position": pos(text, "rootProject", 3)})
        assert hover and "rootproject" in hover["contents"]["value"].lower(), hover     # `getRootProject()`, its accessor
        assert errors(wire, SETTINGS) == []


class TestThroughNeovim:

    def test_neovim_treats_it_as_a_kotlin_file_served_by_intellij(self, nvim):
        nvim.command(f"edit {BUILD}")
        wait_until(lambda: attached(nvim) == 1, message="never attached")
        assert nvim.eval("&filetype") == "kotlin", "a Gradle Kotlin script is a `kotlin` file, so the Kotlin keys apply"
        wait_until(lambda: nvim.exec_lua("return require('ij_bridge').statusline()") == "IJ", timeout=120, interval=2)

    def test_hover_definition_and_completion_through_the_builtins(self, nvim):
        nvim.command(f"edit {BUILD}")
        wait_until(lambda: attached(nvim) == 1)
        wait_until(lambda: nvim.exec_lua("return require('ij_bridge').statusline()") == "IJ", timeout=120, interval=2)
        lines = nvim.current.buffer[:]
        row = next(i for i, l in enumerate(lines) if "mavenCentral" in l)
        nvim.current.window.cursor = (row + 1, lines[row].index("mavenCentral") + 3)
        hover = nvim.exec_lua("""
            local res = vim.lsp.buf_request_sync(0, 'textDocument/hover', vim.lsp.util.make_position_params(0, 'utf-16'), 60000)
            for _, r in pairs(res or {}) do if r.result then return r.result.contents.value end end""")
        assert hover and "mavenCentral" in hover, hover
        nvim.exec_lua("vim.lsp.buf.definition()")
        wait_until(lambda: nvim.eval("expand('%:t')") == "RepositoryHandler.java", timeout=60,
                   message="go to definition did not reach the Gradle API: " + nvim.eval("expand('%:p')"))
