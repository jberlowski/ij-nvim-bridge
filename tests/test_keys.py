"""Key bindings (FEATURES.md §9): everything that is not a standard LSP feature has a default key
under one prefix, `<leader>i`, in LazyVim, and never overwrites anything."""
from __future__ import annotations

import time

import pytest

from harness.util import wait_until
from harness.wire import SRC
from test_editor_slice import attached

SHAPES = f"{SRC}/probe/Shapes.kt"

EXPECTED = {
    "s": "State of this buffer",
    "n": "New file from a template",
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

    def test_everything_the_bridge_adds_is_bound_under_leader_i(self, nvim):
        for suffix, desc in EXPECTED.items():
            m = mapping(nvim, f"<leader>i{suffix}")
            assert m and m["desc"] == f"IntelliJ: {desc}", (suffix, m)

    def test_which_key_names_the_groups(self, nvim):
        """What the developer sees when they press <leader>i and wait."""
        groups = nvim.exec_lua("""
            local ok, config = pcall(require, 'which-key.config')
            local names = {}
            for _, m in ipairs(ok and config.mappings or {}) do
              if m.group and m.desc then names[#names + 1] = tostring(m.lhs or m[1]) .. '=' .. m.desc end
            end
            return names""")
        assert "<leader>i=IntelliJ" in groups and "<leader>il=logs" in groups, groups

    def test_the_keys_command_lists_what_is_bound(self, nvim):
        out = nvim.exec_lua("return vim.api.nvim_exec2('IjBridge keys', { output = true }).output")
        for desc in EXPECTED.values():
            assert desc in out, out

    def test_nothing_of_lazyvims_own_is_on_the_prefix(self, nvim):
        """Checked against the real LazyVim of the harness: the group holds only ours."""
        on_prefix = nvim.exec_lua("""
            local out = {}
            for _, m in ipairs(vim.api.nvim_get_keymap('n')) do
              if m.lhs:sub(1, 2) == ' i' then table.insert(out, m.desc or m.lhs) end
            end
            return out""")
        assert len(on_prefix) == len(EXPECTED) and all(d.startswith("IntelliJ: ") for d in on_prefix), on_prefix


class TestNeverOverwrites:

    def test_an_existing_mapping_is_left_alone_and_the_rest_is_bound(self, nvim):
        try:
            nvim.exec_lua("vim.keymap.set('n', '<leader>js', function() end, { desc = 'the developers own' })")
            bound = keys_setup(nvim, "{ prefix = '<leader>j' }")
            assert mapping(nvim, "<leader>js")["desc"] == "the developers own"
            assert "s" not in bound and "n" in bound and "ll" in bound, bound
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
            assert bound["n"] in ("<leader>yn", " yn") or bound["n"].endswith("yn")
            assert mapping(nvim, "<leader>yn")["desc"] == "IntelliJ: New file from a template"
            assert keys_setup(nvim, "{ keys = false }") == {}
        finally:
            unbind(nvim, "<Space>y")


class TestPressingThem:

    def press(self, nvim, keys: str):
        nvim.feedkeys(nvim.replace_termcodes(keys), "m", False)

    def test_state_says_where_the_buffer_stands(self, nvim):
        nvim.command(f"edit {SHAPES}")
        wait_until(lambda: attached(nvim) == 1)
        self.press(nvim, "<Space>is")
        wait_until(lambda: "attached to /work/fixture" in nvim.command_output("messages"), timeout=10,
                   message="<leader>is did not print the state: " + nvim.command_output("messages")[-300:])

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
            self.press(nvim, "<Space>in")
            wait_until(lambda: bridge_container.exec(f"test -f {path} && echo y || echo n").stdout.strip() == "y",
                       timeout=30, message="<leader>in never wrote the file")
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
            self.press(nvim, "<Space>ilv")
            wait_until(lambda: nvim.exec_lua("return require('ij_bridge.log').level") == "info", timeout=10)
        finally:
            nvim.exec_lua("vim.ui.select = _G.__select")
            nvim.command("IjBridge loglevel debug")
            wait_until(lambda: nvim.exec_lua("return require('ij_bridge.log').level") == "debug", timeout=10)
