"""Key bindings (FEATURES.md §9): what is not a standard LSP feature is bound where LazyVim would look for it.

  <leader>c  code            Gradle tasks: find and run (cg), the hierarchy (cG), repeat (cb), stop (cB)
  <leader>f  file/find       new file from an IntelliJ template (fN)
  <leader>t  test            go to the test / the class under test (tg)
  <leader>a  miscellaneous   state, open the project, keys, logs

Organize imports, rename, format and code actions are standard LSP, and LazyVim binds them itself (`co`, `cr`,
`cf`, `ca`) for any server that offers them: nothing of ours is needed, and a test says so.

A binding that needs IntelliJ exists only in a buffer with a live Session and a filetype it is for: it is made
when the buffer attaches and removed when the Session goes. Nothing is ever overwritten.
"""
from __future__ import annotations

import time

import pytest

from harness.util import wait_until
from harness.wire import SRC
from test_code_actions import MESSY_KOTLIN, ORGANIZED_KOTLIN
from test_editor_slice import attached

SHAPES = f"{SRC}/probe/Shapes.kt"
JAVASHAPES = f"{SRC}/probe/JavaShapes.java"
PROPERTIES = "/work/fixture/gradle.properties"

MISC = {
    "i": "State of this buffer",
    "o": "Open the project in IntelliJ (start it if none is)",
    "k": "List the keys the Bridge binds",
    "ll": "Open the Editor log",
    "lb": "Open the Brain log",
    "lr": "Gather a bug report",
    "lv": "Set the log level",
}
SECTIONS = {
    "<leader>cg": "Gradle: find a task and run it",
    "<leader>cG": "Gradle: the tasks as a hierarchy",
    "<leader>cb": "Gradle: run the last task again",
    "<leader>cB": "Gradle: stop the running task",
    "<leader>fN": "New file from an IntelliJ template",
    "<leader>tg": "Go to the test / the class under test",
    "<leader>tn": "Run the nearest test",
    "<leader>tc": "Run every test in this class",
    "<leader>tf": "Run every test in this file",
    "<leader>tR": "Run the last test again",
    "<leader>tx": "Stop the running test",
}


def mapping(nvim, lhs: str) -> dict:
    """The mapping's description and whether it is buffer-local. (The rest of `maparg` holds a function.)"""
    found = nvim.exec_lua("local m = vim.fn.maparg(..., 'n', false, true); return { desc = m.desc, buffer = m.buffer }", lhs)
    return found if isinstance(found, dict) else {}          # an empty Lua table arrives as an empty list


def here(nvim) -> dict:
    """The section keys this buffer has, by lhs."""
    return {lhs: mapping(nvim, lhs) for lhs in SECTIONS if mapping(nvim, lhs).get("desc")}


def keys_setup(nvim, opts: str):
    bound = nvim.exec_lua(f"require('ij_bridge.keys').setup({opts}); return require('ij_bridge.keys').bound")
    return bound or {}          # an empty Lua table arrives as an empty list


def unbind(nvim, prefix: str):
    nvim.exec_lua(f"""
        for _, m in ipairs(vim.api.nvim_get_keymap('n')) do
          local lhs = m.lhs:gsub('^ ', '<Space>')
          if lhs:sub(1, {len(prefix)}) == '{prefix}' then pcall(vim.keymap.del, 'n', m.lhs) end
        end""")


def open_kotlin(nvim, path=SHAPES):
    nvim.command(f"edit {path}")
    wait_until(lambda: attached(nvim) == 1, message="never attached")
    wait_until(lambda: len(here(nvim)) == len(SECTIONS), timeout=30, message=f"the section keys never appeared: {here(nvim)}")


class TestMiscellaneous:

    def test_they_are_bound_globally_under_leader_a(self, nvim):
        for suffix, desc in MISC.items():
            m = mapping(nvim, f"<leader>a{suffix}")
            assert m.get("desc") == f"IntelliJ: {desc}" and not m.get("buffer"), (suffix, m)

    def test_they_exist_with_no_connection_at_all(self, nvim):
        """Opening the project is how to get a connection: it cannot need one."""
        nvim.command("edit /tmp/not-a-project-at-all.txt")
        assert mapping(nvim, "<leader>ao").get("desc")

    def test_which_key_names_the_logs_group(self, nvim):
        groups = nvim.exec_lua("""
            local ok, config = pcall(require, 'which-key.config')
            local names = {}
            for _, m in ipairs(ok and config.mappings or {}) do
              if m.group and m.desc then names[#names + 1] = tostring(m.lhs or m[1]) .. '=' .. m.desc end
            end
            return names""")
        assert "<leader>al=logs" in groups, groups

    def test_the_keys_command_says_what_is_bound_here_and_why_not(self, nvim):
        nvim.command("edit /tmp/not-a-project-at-all.txt")
        out = nvim.exec_lua("return vim.api.nvim_exec2('IjBridge keys', { output = true }).output")
        for desc in MISC.values():
            assert desc in out, out
        assert "no IntelliJ connection" in out and "needs an IntelliJ connection" in out, out

    def test_no_key_here_is_one_the_ai_extras_use(self, bridge_container):
        """LazyVim's Claude Code, Avante, Copilot Chat and Sidekick extras all live on <leader>a. Read from
        LazyVim's own source in the image, so that a new extra is caught the next time this runs."""
        out = bridge_container.exec(
            "grep -rhoE '\"<leader>a[a-zA-Z]*\"' ~/.local/share/nvim/lazy/LazyVim/lua/lazyvim/plugins/extras").stdout
        theirs = {k.strip('"')[len("<leader>a"):] for k in out.split() if len(k.strip('"')) > len("<leader>a")}
        assert {"a", "c", "s", "n"} <= theirs, f"the grep found too little: {sorted(theirs)}"
        for mine in MISC:
            clash = [t for t in theirs if t == mine or t.startswith(mine) or mine.startswith(t)]
            assert not clash, f"<leader>a{mine} collides with the AI extras' {clash}"

    def test_the_group_holds_only_ours_in_this_lazyvim(self, nvim):
        on_prefix = nvim.exec_lua("""
            local out = {}
            for _, m in ipairs(vim.api.nvim_get_keymap('n')) do
              if m.lhs:sub(1, 2) == ' a' then table.insert(out, m.desc or m.lhs) end
            end
            return out""")
        assert len(on_prefix) == len(MISC) and all(d.startswith("IntelliJ: ") for d in on_prefix), on_prefix

    def test_no_section_key_is_taken_by_lazyvim_itself(self, nvim):
        """`<leader>fN` is free in LazyVim as the harness has it (`<leader>fn` is its New File)."""
        nvim.command("edit /tmp/not-a-project-at-all.txt")
        assert here(nvim) == {}
        assert mapping(nvim, "<leader>fn").get("desc") == "New File"

    def test_the_gradle_keys_are_free_in_lazyvim_and_its_extras(self, nvim, bridge_container):
        """`cg cG cb cB`: read from LazyVim's core and extras, none of them is used there."""
        out = bridge_container.exec(
            "grep -rhoE '\"<leader>c[a-zA-Z]\"' ~/.local/share/nvim/lazy/LazyVim/lua/lazyvim | sort -u").stdout
        used = {k.strip('"')[len("<leader>"):] for k in out.split()}
        assert used, "the grep found nothing"
        for lhs in ("cg", "cG", "cb", "cB"):
            assert lhs not in used, f"<leader>{lhs} is used by LazyVim itself: {sorted(used)}"

    def test_the_test_keys_are_free_in_lazyvim_and_its_extras(self, nvim, bridge_container):
        """`tg tn tc tf tR tx`: LazyVim's core has nothing at all on <leader>t, but its "test" extra
        (neotest) does use several of that section's letters (tt, ts, ...) - read its actual set,
        not assumed."""
        out = bridge_container.exec(
            "grep -rhoE '\"<leader>t[a-zA-Z]\"' ~/.local/share/nvim/lazy/LazyVim/lua/lazyvim | sort -u").stdout
        used = {k.strip('"')[len("<leader>"):] for k in out.split()}
        assert used, "the grep found nothing"
        for lhs in ("tg", "tn", "tc", "tf", "tR", "tx"):
            assert lhs not in used, f"<leader>{lhs} is used by LazyVim itself: {sorted(used)}"


class TestOnlyWhereIntelliJIs:

    def test_a_connected_kotlin_buffer_has_them_buffer_locally(self, nvim):
        open_kotlin(nvim)
        for lhs, desc in SECTIONS.items():
            m = mapping(nvim, lhs)
            assert m["desc"] == f"IntelliJ: {desc}" and m["buffer"] == 1, (lhs, m)

    def test_a_connected_java_buffer_too(self, nvim):
        open_kotlin(nvim, JAVASHAPES)

    def test_a_file_outside_every_project_has_none(self, nvim):
        nvim.command("edit /tmp/not-a-project-at-all.txt")
        time.sleep(1)
        assert here(nvim) == {}

    def test_a_file_of_another_kind_in_the_project_has_none_though_connected(self, nvim):
        nvim.command(f"edit {PROPERTIES}")
        wait_until(lambda: attached(nvim) == 1, message="never attached")
        time.sleep(1.5)
        assert here(nvim) == {}, "connected, but not a Kotlin or Java file"

    def test_they_go_when_the_connection_goes_and_return_with_it(self, nvim):
        open_kotlin(nvim)
        nvim.exec_lua("for _, c in ipairs(vim.lsp.get_clients({ name = 'ij-bridge' })) do c:stop(true) end")
        wait_until(lambda: here(nvim) == {}, timeout=15, message=f"the keys stayed after the connection went: {here(nvim)}")
        wait_until(lambda: len(here(nvim)) == len(SECTIONS), timeout=60, interval=1,
                   message="the keys never came back after the Bridge reconnected")

    def test_a_buffer_left_for_a_non_jvm_file_does_not_carry_them(self, nvim):
        open_kotlin(nvim)
        kotlin_buffer = nvim.current.buffer.number
        nvim.command(f"edit {PROPERTIES}")
        wait_until(lambda: attached(nvim) == 1)
        assert here(nvim) == {}
        nvim.command(f"buffer {kotlin_buffer}")
        assert len(here(nvim)) == len(SECTIONS), "and the Kotlin buffer still has its own"

    def test_the_developers_own_buffer_mapping_is_never_overwritten_or_removed(self, nvim):
        nvim.command(f"edit {SHAPES}")
        wait_until(lambda: attached(nvim) == 1)
        wait_until(lambda: len(here(nvim)) == len(SECTIONS), timeout=30)
        nvim.exec_lua("vim.keymap.set('n', '<leader>fN', function() end, { buffer = 0, desc = 'the developers own' })")
        keys_setup(nvim, "{}")                                            # binds again, as a reload would
        assert mapping(nvim, "<leader>fN")["desc"] == "the developers own"

    def test_keys_false_binds_nothing_and_sections_false_only_the_misc_keys(self, nvim):
        try:
            open_kotlin(nvim)
            keys_setup(nvim, "{ keys = false }")
            assert here(nvim) == {} and not mapping(nvim, "<leader>ai").get("desc")
            bound = keys_setup(nvim, "{ sections = false }")
            assert set(bound) == set(MISC) and here(nvim) == {}
        finally:
            keys_setup(nvim, "{}")


class TestNeverOverwrites:

    def test_an_existing_mapping_is_left_alone_and_the_rest_is_bound(self, nvim):
        try:
            nvim.exec_lua("vim.keymap.set('n', '<leader>ji', function() end, { desc = 'the developers own' })")
            bound = keys_setup(nvim, "{ prefix = '<leader>j' }")
            assert mapping(nvim, "<leader>ji")["desc"] == "the developers own"
            assert "i" not in bound and "o" in bound and "ll" in bound, bound
        finally:
            unbind(nvim, "<Space>j")
            keys_setup(nvim, "{}")

    def test_an_empty_mapping_on_the_prefix_is_a_group_name_not_a_clash(self, nvim):
        """How LazyVim's AI extras name their group: `{ "<leader>a", "", desc = "+ai" }`."""
        try:
            unbind(nvim, "<Space>a")
            nvim.exec_lua("vim.keymap.set('n', '<leader>a', '', { desc = '+ai' })")
            assert set(keys_setup(nvim, "{}")) == set(MISC)
        finally:
            unbind(nvim, "<Space>a")
            keys_setup(nvim, "{}")

    def test_one_of_the_ai_extras_own_keys_is_left_alone(self, nvim):
        try:
            unbind(nvim, "<Space>a")
            nvim.exec_lua("vim.keymap.set('n', '<leader>as', function() end, { desc = 'Stop Avante' })")
            bound = keys_setup(nvim, "{}")
            assert mapping(nvim, "<leader>as")["desc"] == "Stop Avante"
            assert set(bound) == set(MISC), "none of ours is on their keys, so all are bound"
        finally:
            unbind(nvim, "<Space>a")
            keys_setup(nvim, "{}")

    def test_a_mapped_prefix_binds_no_misc_keys_because_it_would_swallow_the_rest(self, nvim):
        try:
            nvim.exec_lua("vim.keymap.set('n', '<leader>k', function() end, { desc = 'the developers own' })")
            assert keys_setup(nvim, "{ prefix = '<leader>k' }") == {}
        finally:
            unbind(nvim, "<Space>k")
            keys_setup(nvim, "{}")

    def test_a_different_prefix(self, nvim):
        try:
            bound = keys_setup(nvim, "{ prefix = '<leader>y' }")
            assert bound["o"].endswith("yo") and mapping(nvim, "<leader>yo")["desc"].startswith("IntelliJ: ")
        finally:
            unbind(nvim, "<Space>y")
            keys_setup(nvim, "{}")


class TestPressingThem:

    def press(self, nvim, keys: str):
        nvim.feedkeys(nvim.replace_termcodes(keys), "m", False)

    def test_state_says_where_the_buffer_stands(self, nvim):
        nvim.command(f"edit {SHAPES}")
        wait_until(lambda: attached(nvim) == 1)
        nvim.exec_lua("_G.__out = {}; _G.__print_orig = print; print = function(...) table.insert(_G.__out, table.concat(vim.tbl_map(tostring, { ... }), ' ')) end")
        try:
            self.press(nvim, "<Space>ai")
            wait_until(lambda: any("attached to /work/fixture" in l for l in nvim.exec_lua("return _G.__out")), timeout=10,
                       message="<leader>ai did not print the state")
        finally:
            nvim.exec_lua("print = _G.__print_orig")

    def test_lazyvims_own_organize_imports_key_works_through_the_bridge(self, nvim):
        """`<leader>co` is LazyVim's, for any server that offers `source.organizeImports`: nothing of ours is bound,
        and it works because the Bridge is an ordinary language server."""
        open_kotlin(nvim)
        wait_until(lambda: mapping(nvim, "<leader>co").get("desc") == "Organize Imports", timeout=30,
                   message=f"LazyVim never bound <leader>co here: {mapping(nvim, '<leader>co')}")
        assert mapping(nvim, "<leader>co")["buffer"] == 1
        nvim.current.buffer[:] = MESSY_KOTLIN.rstrip("\n").split("\n")
        self.press(nvim, "<Space>co")
        wait_until(lambda: "\n".join(nvim.current.buffer[:]) + "\n" == ORGANIZED_KOTLIN, timeout=60, interval=1,
                   message="<leader>co did not organize the imports:\n" + "\n".join(nvim.current.buffer[:]))

    def test_new_file_through_the_key_asks_what_and_what_to_call_it_and_writes_it(self, nvim, bridge_container):
        directory = f"{SRC}/probe"
        path = f"{directory}/Keyed.kt"
        bridge_container.exec(f"rm -f {path}", check=False)
        try:
            open_kotlin(nvim)
            nvim.exec_lua("""
                vim.ui.select = function(items, opts, on_choice) on_choice('interface') end
                vim.ui.input = function(opts, on_choice) on_choice('Keyed') end""")
            self.press(nvim, "<Space>fN")
            wait_until(lambda: bridge_container.exec(f"test -f {path} && echo y || echo n").stdout.strip() == "y",
                       timeout=30, message="<leader>fN never wrote the file")
            text = bridge_container.read_file(path)
            assert "package dev.bridge.fixture.probe" in text and "interface Keyed" in text, text
        finally:
            nvim.exec_lua("vim.ui.select = nil; vim.ui.input = nil; package.loaded['vim.ui'] = nil")
            nvim.command("silent! %bwipeout!")
            bridge_container.exec(f"rm -f {path}", check=False)

    def test_the_log_level_is_chosen_from_a_list(self, nvim):
        nvim.command(f"edit {SHAPES}")
        wait_until(lambda: attached(nvim) == 1)
        nvim.exec_lua("_G.__select = vim.ui.select; vim.ui.select = function(items, opts, on_choice) on_choice('info') end")
        try:
            self.press(nvim, "<Space>alv")
            wait_until(lambda: nvim.exec_lua("return require('ij_bridge.log').level") == "info", timeout=10)
        finally:
            nvim.exec_lua("vim.ui.select = _G.__select")
            nvim.command("IjBridge loglevel debug")
            wait_until(lambda: nvim.exec_lua("return require('ij_bridge.log').level") == "debug", timeout=10)
