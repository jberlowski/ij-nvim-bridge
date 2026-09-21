"""Gradle tasks in Neovim (FEATURES.md §6d): the fuzzy finder, the hierarchy, running with streamed output,
stopping and repeating, against the real IntelliJ."""
from __future__ import annotations

import time

import pytest

from harness.util import wait_until
from harness.wire import SRC
from test_editor_slice import attached

SHAPES = f"{SRC}/probe/Shapes.kt"


def open_project_buffer(nvim):
    nvim.command(f"edit {SHAPES}")
    wait_until(lambda: attached(nvim) == 1, message="never attached")
    wait_until(lambda: nvim.exec_lua("return require('ij_bridge').statusline()") == "IJ", timeout=120, interval=2,
               message="never Ready")
    # The task model is loaded by the import: ask until the IDE has it.
    wait_until(lambda: nvim.exec_lua("""
        local done, ok = false, false
        require('ij_bridge.tasks').fetch(function(tree) ok = #(tree.projects or {}) > 0; done = true end)
        vim.wait(20000, function() return done end, 50)
        return ok"""), timeout=180, interval=3, message="the IDE never had the Gradle tasks")


def watch_for_the_end(nvim):
    nvim.exec_lua("""_G.__finished = nil
        vim.api.nvim_create_autocmd('User', { pattern = 'IjBridgeTaskFinished', once = true,
          callback = function(a) _G.__finished = a.data end })""")


def finished(nvim, timeout=240) -> dict:
    wait_until(lambda: nvim.exec_lua("return _G.__finished") is not None, timeout=timeout, interval=1,
               message="the task never finished; output:\n" + output(nvim)[-500:])
    return nvim.exec_lua("return _G.__finished")


def output(nvim) -> str:
    return nvim.exec_lua("""
        local b = require('ij_bridge.tasks').output_buffer_for_tests()
        if not b then return '' end
        return table.concat(vim.api.nvim_buf_get_lines(b, 0, -1, false), '\\n')""")


def record_notes(nvim):
    nvim.exec_lua("_G.__notes = {}; vim.notify = function(msg, level) table.insert(_G.__notes, msg) end")


def notes(nvim) -> list[str]:
    return nvim.exec_lua("return _G.__notes or {}")


def type_in_picker(nvim, text: str):
    wait_until(lambda: nvim.eval("mode()") == "i", timeout=10, message="the picker never took the input")
    nvim.input(text)


def picker_names(nvim) -> list[str]:
    return nvim.exec_lua("""
        local c = require('ij_bridge.picker').current
        local out = {}
        for _, e in ipairs(c and c.ranked or {}) do out[#out + 1] = e.item.name end
        return out""")


class TestFindingATask:

    def test_the_finder_lists_the_real_tasks_and_finds_by_fuzzy_match(self, nvim):
        open_project_buffer(nvim)
        nvim.command("IjBridge task")
        wait_until(lambda: len(picker_names(nvim)) > 20, timeout=30, message="the finder never listed the tasks")
        assert {"help", "bootRun", "build", "test"} <= set(picker_names(nvim))
        type_in_picker(nvim, "bR")
        wait_until(lambda: picker_names(nvim)[:1] == ["bootRun"], timeout=10, message=str(picker_names(nvim)[:5]))
        nvim.input("<Esc>")

    def test_choosing_runs_it_and_streams_the_output_without_taking_the_focus(self, nvim):
        open_project_buffer(nvim)
        editing = nvim.current.window.handle
        watch_for_the_end(nvim)
        nvim.command("IjBridge task")
        wait_until(lambda: len(picker_names(nvim)) > 20, timeout=30)
        type_in_picker(nvim, "help<CR>")
        end = finished(nvim)
        assert end["success"] is True and end["cancelled"] is False, end
        text = output(nvim)
        assert "▶ gradle help" in text and "Welcome to Gradle" in text and "✔ finished in" in text, text[-400:]
        assert nvim.exec_lua("return #vim.fn.win_findbuf(require('ij_bridge.tasks').output_buffer_for_tests())") == 1, "the output is on screen"
        assert nvim.current.window.handle == editing, "and the developer is still where they were"

    def test_a_task_that_fails_says_so_in_the_output(self, nvim):
        open_project_buffer(nvim)
        watch_for_the_end(nvim)
        nvim.exec_lua("require('ij_bridge.tasks').run({ name = 'thisTaskDoesNotExist', path = '/work/fixture' })")
        end = finished(nvim)
        assert end["success"] is False and "✘ failed" in output(nvim), output(nvim)[-300:]

    def test_repeating_runs_the_last_again(self, nvim):
        open_project_buffer(nvim)
        watch_for_the_end(nvim)
        nvim.exec_lua("require('ij_bridge.tasks').run({ name = 'help', path = '/work/fixture' })")
        finished(nvim)
        watch_for_the_end(nvim)
        nvim.command("IjBridge taskrepeat")
        assert finished(nvim)["success"] is True
        assert output(nvim).count("▶ gradle help") == 1, "the output is the last run's, not a pile"


class TestTheHierarchy:

    def tree_lines(self, nvim) -> list[str]:
        return nvim.exec_lua("local b = vim.fn.bufnr('IntelliJ Gradle hierarchy$'); return vim.api.nvim_buf_get_lines(b, 0, -1, false)")

    def open_tree(self, nvim):
        open_project_buffer(nvim)
        nvim.command("IjBridge tasks")
        wait_until(lambda: nvim.eval("expand('%')") == "IntelliJ Gradle hierarchy", timeout=30, message="the tree never opened")
        wait_until(lambda: len(self.tree_lines(nvim)) > 3, timeout=30)

    def test_projects_then_groups_then_tasks_folded_by_default(self, nvim):
        self.open_tree(nvim)
        lines = self.tree_lines(nvim)
        assert lines[0] == "▾ spring-kotlin-mvc", lines[:3]
        assert any(l.strip() == "▸ build" for l in lines) and any(l.strip() == "▸ help" for l in lines), lines
        assert not any(l.strip() == "assemble" for l in lines), "tasks are inside folded groups"
        assert lines[-1].strip() == "▸ other", "the ungrouped tasks come last"

    def test_tab_unfolds_a_group_and_shows_its_tasks(self, nvim):
        self.open_tree(nvim)
        row = next(i for i, l in enumerate(self.tree_lines(nvim)) if l.strip() == "▸ help")
        nvim.current.window.cursor = (row + 1, 0)
        nvim.input("<Tab>")
        wait_until(lambda: any(l.strip() == "help" for l in self.tree_lines(nvim)), timeout=10)
        lines = self.tree_lines(nvim)
        assert lines[row].strip() == "▾ help" and lines[row + 1].startswith("      ") and not lines[row + 1].strip().startswith("▾"), lines[row:row + 3]
        nvim.input("<Tab>")
        wait_until(lambda: not any(l.strip() == "projects" for l in self.tree_lines(nvim)), timeout=10)

    def test_zR_and_zM_expand_and_collapse_everything(self, nvim):
        self.open_tree(nvim)
        before = len(self.tree_lines(nvim))
        nvim.input("zR")
        wait_until(lambda: len(self.tree_lines(nvim)) > before + 20, timeout=10)
        nvim.input("zM")
        wait_until(lambda: len(self.tree_lines(nvim)) == 1, timeout=10)        # only the collapsed project
        nvim.input("<Tab>")
        wait_until(lambda: len(self.tree_lines(nvim)) == before, timeout=10)

    def test_enter_on_a_task_runs_it(self, nvim):
        self.open_tree(nvim)
        nvim.input("zR")
        wait_until(lambda: any(l.strip() == "help" for l in self.tree_lines(nvim)), timeout=10)
        row = next(i for i, l in enumerate(self.tree_lines(nvim)) if l.strip() == "help")
        nvim.current.window.cursor = (row + 1, 0)
        watch_for_the_end(nvim)
        record_notes(nvim)
        nvim.input("<CR>")
        try:
            end = finished(nvim, timeout=90)
        except AssertionError as e:
            raise AssertionError(f"{e}\nnotes: {notes(nvim)}\nline {row + 1}: {self.tree_lines(nvim)[row]!r}, "
                                 f"buffer {nvim.eval("expand('%')")}, mode {nvim.eval('mode()')}") from None
        assert end["success"] is True
        assert "Welcome to Gradle" in output(nvim)

    def test_the_slash_key_opens_the_fuzzy_finder(self, nvim):
        self.open_tree(nvim)
        nvim.input("/")
        wait_until(lambda: len(picker_names(nvim)) > 20, timeout=30)
        nvim.input("<Esc>")

    def test_descriptions_are_shown_beside_the_tasks(self, nvim):
        self.open_tree(nvim)
        nvim.input("zR")
        wait_until(lambda: any(l.strip() == "projects" for l in self.tree_lines(nvim)), timeout=10)
        described = nvim.exec_lua("""
            local b = vim.fn.bufnr('IntelliJ Gradle hierarchy$')
            local ns = vim.api.nvim_create_namespace('ij_bridge_tasks')
            local out = {}
            for _, m in ipairs(vim.api.nvim_buf_get_extmarks(b, ns, 0, -1, { details = true })) do
              for _, chunk in ipairs(m[4].virt_text or {}) do out[#out + 1] = chunk[1] end
            end
            return out""")
        assert any("Displays the sub-projects" in d for d in described), described[:5]


class TestStoppingAndOneAtATime:

    def test_a_second_task_is_refused_and_stop_cancels_the_first(self, nvim):
        open_project_buffer(nvim)
        record_notes(nvim)
        watch_for_the_end(nvim)
        try:
            nvim.exec_lua("require('ij_bridge.tasks').run({ name = 'build', path = '/work/fixture' })")
            wait_until(lambda: "▶ gradle build" in output(nvim), timeout=30)
            nvim.exec_lua("require('ij_bridge.tasks').run({ name = 'help', path = '/work/fixture' })")
            wait_until(lambda: any("already running" in n for n in notes(nvim)), timeout=15, message=f"said: {notes(nvim)}")
            time.sleep(1)
            nvim.command("IjBridge taskstop")
            end = finished(nvim, timeout=180)
            assert end["cancelled"] is True and "■ cancelled" in output(nvim), (end, output(nvim)[-300:])
        finally:
            nvim.command("IjBridge taskstop")
            time.sleep(2)

    def test_stopping_when_nothing_runs_says_so(self, nvim):
        open_project_buffer(nvim)
        record_notes(nvim)
        nvim.command("IjBridge taskstop")
        wait_until(lambda: any("no Gradle task is running" in n for n in notes(nvim)), timeout=10, message=str(notes(nvim)))


class TestWithoutIntelliJ:

    def test_it_says_there_is_none_instead_of_failing(self, nvim):
        nvim.command("edit /tmp/not-a-project-at-all.txt")
        record_notes(nvim)
        nvim.command("IjBridge tasks")
        nvim.command("IjBridge task")
        wait_until(lambda: len([n for n in notes(nvim) if "no IntelliJ is serving" in n]) == 2, timeout=10, message=str(notes(nvim)))
        assert nvim.eval("v:errmsg") == ""
