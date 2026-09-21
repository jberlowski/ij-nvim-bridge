"""Key bindings (FEATURES.md §9): everything that is not a standard LSP feature has a default key
under one prefix, `<leader>a`, in LazyVim, and never overwrites anything. LazyVim's AI extras use the
same prefix, so no suffix here may be one of theirs."""
from __future__ import annotations

import time

import pytest

from harness.util import wait_until
from harness.wire import SRC
from test_editor_slice import attached

SHAPES = f"{SRC}/probe/Shapes.kt"

EXPECTED = {
    "i": "State of this buffer",
    "o": "Open the project in IntelliJ (start it if none is)",
    "g": "New file from a template",
    "ll": "Open the Editor log",
    "lb": "Open the Brain log",
    "lr": "Gather a bug report",
    "lv": "Set the log level",
}


def mapping(nvim, lhs: str) -> dict:
    """The mapping's description only: the rest of `maparg` holds a function, which cannot cross RPC."""
    return nvim.exec_lua("local m = vim.fn.maparg(..., 'n', false, true); return { desc = m.desc }", lhs)


def keys_setup(nvim, opts: str):
    bound = nvim.exec_lua(f"require('ij_bridge.keys').setup({opts}); return require('ij_bridge.keys').bound")
    return bound or {}          # an empty Lua table arrives as an empty list


def unbind(nvim, prefix: str):
    nvim.exec_lua(f"""
        for _, m in ipairs(vim.api.nvim_get_keymap('n')) do
          local lhs = m.lhs:gsub('^ ', '<Space>')
          if lhs:sub(1, {len(prefix)}) == '{prefix}' then pcall(vim.keymap.del, 'n', m.lhs) end
        end""")


class TestDefaults:

    def test_everything_the_bridge_adds_is_bound_under_leader_a(self, nvim):
        for suffix, desc in EXPECTED.items():
            m = mapping(nvim, f"<leader>a{suffix}")
            assert m and m["desc"] == f"IntelliJ: {desc}", (suffix, m)

    def test_which_key_names_the_groups(self, nvim):
        """What the developer sees when they press <leader>a and wait."""
        groups = nvim.exec_lua("""
            local ok, config = pcall(require, 'which-key.config')
            local names = {}
            for _, m in ipairs(ok and config.mappings or {}) do
              if m.group and m.desc then names[#names + 1] = tostring(m.lhs or m[1]) .. '=' .. m.desc end
            end
            return names""")
        assert "<leader>al=logs" in groups, groups

    def test_the_keys_command_lists_what_is_bound(self, nvim):
        out = nvim.exec_lua("return vim.api.nvim_exec2('IjBridge keys', { output = true }).output")
        for desc in EXPECTED.values():
            assert desc in out, out

    def test_the_group_holds_only_ours_in_this_lazyvim(self, nvim):
        """Checked against the real LazyVim of the harness (no AI extra enabled): the group holds only ours."""
        on_prefix = nvim.exec_lua("""
            local out = {}
            for _, m in ipairs(vim.api.nvim_get_keymap('n')) do
              if m.lhs:sub(1, 2) == ' a' then table.insert(out, m.desc or m.lhs) end
            end
            return out""")
        assert len(on_prefix) == len(EXPECTED) and all(d.startswith("IntelliJ: ") for d in on_prefix), on_prefix

    def test_no_key_here_is_one_the_ai_extras_use(self, bridge_container):
        """LazyVim's Claude Code, Avante, Copilot Chat and Sidekick extras all live on <leader>a. Read from
        LazyVim's own source in the image, so that a new extra is caught the next time this runs."""
        out = bridge_container.exec(
            "grep -rhoE '\"<leader>a[a-zA-Z]*\"' ~/.local/share/nvim/lazy/LazyVim/lua/lazyvim/plugins/extras").stdout
        theirs = {k.strip('"')[len("<leader>a"):] for k in out.split() if len(k.strip('"')) > len("<leader>a")}
        assert {"a", "c", "s", "n"} <= theirs, f"the grep found too little: {sorted(theirs)}"
        for mine in EXPECTED:
            clash = [t for t in theirs if t == mine or t.startswith(mine) or mine.startswith(t)]
            assert not clash, f"<leader>a{mine} collides with the AI extras' {clash}"


class TestNeverOverwrites:

    def test_an_empty_mapping_on_the_prefix_is_a_group_name_not_a_clash(self, nvim):
        """How LazyVim's AI extras name their group: `{ "<leader>a", "", desc = "+ai" }`."""
        try:
            unbind(nvim, "<Space>a")
            nvim.exec_lua("vim.keymap.set('n', '<leader>a', '', { desc = '+ai' })")
            bound = keys_setup(nvim, "{}")
            assert set(bound) == set(EXPECTED), bound
        finally:
            unbind(nvim, "<Space>a")
            keys_setup(nvim, "{}")             # the defaults again, for the tests that follow

    def test_one_of_the_ai_extras_own_keys_is_left_alone(self, nvim):
        try:
            unbind(nvim, "<Space>a")
            nvim.exec_lua("vim.keymap.set('n', '<leader>as', function() end, { desc = 'Stop Avante' })")
            bound = keys_setup(nvim, "{}")
            assert mapping(nvim, "<leader>as")["desc"] == "Stop Avante"
            assert set(bound) == set(EXPECTED), "none of ours is on their keys, so all are bound"
        finally:
            unbind(nvim, "<Space>a")
            keys_setup(nvim, "{}")

    def test_an_existing_mapping_is_left_alone_and_the_rest_is_bound(self, nvim):
        try:
            nvim.exec_lua("vim.keymap.set('n', '<leader>ji', function() end, { desc = 'the developers own' })")
            bound = keys_setup(nvim, "{ prefix = '<leader>j' }")
            assert mapping(nvim, "<leader>ji")["desc"] == "the developers own"
            assert "i" not in bound and "g" in bound and "ll" in bound, bound
        finally:
            unbind(nvim, "<Space>j")

    def test_a_mapped_prefix_binds_nothing_because_it_would_swallow_the_rest(self, nvim):
        try:
            nvim.exec_lua("vim.keymap.set('n', '<leader>k', function() end, { desc = 'the developers own' })")
            assert keys_setup(nvim, "{ prefix = '<leader>k' }") == {}
        finally:
            unbind(nvim, "<Space>k")

    def test_a_different_prefix_and_keys_false(self, nvim):
        try:
            bound = keys_setup(nvim, "{ prefix = '<leader>y' }")
            assert bound["g"].endswith("yg")
            assert mapping(nvim, "<leader>yg")["desc"] == "IntelliJ: New file from a template"
            assert keys_setup(nvim, "{ keys = false }") == {}
        finally:
            unbind(nvim, "<Space>y")


class TestPressingThem:

    def press(self, nvim, keys: str):
        nvim.feedkeys(nvim.replace_termcodes(keys), "m", False)

    def test_state_says_where_the_buffer_stands(self, nvim):
        nvim.command(f"edit {SHAPES}")
        wait_until(lambda: attached(nvim) == 1)
        self.press(nvim, "<Space>ai")
        wait_until(lambda: "attached to /work/fixture" in nvim.command_output("messages"), timeout=10,
                   message="<leader>ai did not print the state: " + nvim.command_output("messages")[-300:])

    def test_new_file_asks_what_and_what_to_call_it_and_writes_it(self, nvim, bridge_container):
        directory = f"{SRC}/probe"
        path = f"{directory}/Keyed.kt"
        bridge_container.exec(f"rm -f {path}", check=False)
        try:
            nvim.command(f"edit {SHAPES}")
            wait_until(lambda: attached(nvim) == 1)
            nvim.exec_lua("""
                vim.ui.select = function(items, opts, on_choice) on_choice('interface') end
                vim.ui.input = function(opts, on_choice) on_choice('Keyed') end""")
            self.press(nvim, "<Space>ag")
            wait_until(lambda: bridge_container.exec(f"test -f {path} && echo y || echo n").stdout.strip() == "y",
                       timeout=30, message="<leader>ag never wrote the file")
            text = bridge_container.read_file(path)
            assert "package dev.bridge.fixture.probe" in text and "interface Keyed" in text, text
        finally:
            nvim.exec_lua("vim.ui.select = nil; vim.ui.input = nil; package.loaded['vim.ui'] = nil")
            nvim.command("silent! %bwipeout!")
            bridge_container.exec(f"rm -f {path}", check=False)

    def test_the_log_level_is_chosen_from_a_list(self, nvim):
        nvim.command(f"edit {SHAPES}")
        wait_until(lambda: attached(nvim) == 1)
        original_select = nvim.exec_lua("_G.__select = vim.ui.select; vim.ui.select = function(items, opts, on_choice) on_choice('info') end; return true")
        try:
            self.press(nvim, "<Space>alv")
            wait_until(lambda: nvim.exec_lua("return require('ij_bridge.log').level") == "info", timeout=10)
        finally:
            nvim.exec_lua("vim.ui.select = _G.__select")
            nvim.command("IjBridge loglevel debug")
            wait_until(lambda: nvim.exec_lua("return require('ij_bridge.log').level") == "debug", timeout=10)
