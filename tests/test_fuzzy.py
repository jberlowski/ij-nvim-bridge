"""The fuzzy matcher and the picker built on it (FEATURES.md §6d). No IDE is needed: this is the Editor's own.

Fuzzy means a subsequence, not a substring: for `findMysteriousTreasure`, `eas` (an `e` in Mysterious, then an
`a` and an `s` after it), `fMT` (its camel-hump initials) and `trea` (a substring) all find it.
"""
from __future__ import annotations

import time

import pytest

from harness.util import wait_until

TASK = "findMysteriousTreasure"


def match(nvim, query: str, text: str):
    """(score or None, matched columns)."""
    r = nvim.exec_lua("local s, p = require('ij_bridge.fuzzy').match(...); return { score = s or vim.NIL, positions = p or {} }",
                      query, text)
    return r["score"], r["positions"]


def found(nvim, query: str, text: str = TASK) -> bool:
    return match(nvim, query, text)[0] is not None


def rank(nvim, query: str, items: list[str]) -> list[str]:
    return nvim.exec_lua("""
        local items, query = ...
        local out = {}
        for _, e in ipairs(require('ij_bridge.fuzzy').rank(items, query, function(t) return t, t end)) do out[#out + 1] = e.item end
        return out""", items, query)


class TestWhatMatches:

    @pytest.mark.parametrize("query", ["eas", "fMT", "trea", "findmys", "FMT", "fmt", "TREASURE", "f", "e", "ftr", "mT", "find treasure"])
    def test_these_all_find_it(self, nvim, query):
        assert found(nvim, query), query

    @pytest.mark.parametrize("query", ["xyz", "reasure2", "tmf", "aef", "findd", "zzz", "seat"])
    def test_these_do_not(self, nvim, query):
        """Not every letter present, or not in that order."""
        assert not found(nvim, query), query

    def test_it_is_not_a_substring_match(self, nvim):
        assert found(nvim, "fMT") and "fmt" not in TASK.lower(), "the letters are there, in order, but not next to each other"
        assert found(nvim, "fmt") and found(nvim, "mst") and "mst" not in TASK.lower()

    def test_case_is_ignored(self, nvim):
        assert found(nvim, "FINDMYSTERIOUSTREASURE") and found(nvim, "findmysterioustreasure")

    def test_several_terms_must_all_match(self, nvim):
        assert found(nvim, "myst trea") and found(nvim, "trea myst")
        assert not found(nvim, "myst zzz")

    def test_an_empty_query_matches_everything(self, nvim):
        assert match(nvim, "", TASK)[0] == 0 and found(nvim, "   ")

    def test_the_matched_columns_are_reported(self, nvim):
        score, positions = match(nvim, "fMT", TASK)
        assert positions == [1, 5, 15], positions          # f, the M of Mysterious, the T of Treasure
        _, positions = match(nvim, "trea", TASK)
        assert positions == [15, 16, 17, 18], positions    # the contiguous "Trea"

    def test_a_pattern_longer_than_the_text_cannot_match(self, nvim):
        assert not found(nvim, "findMysteriousTreasureAndMore")


class TestWhatComesFirst:

    def test_a_contiguous_run_beats_a_scattered_match(self, nvim):
        """`trea` typed: the words that contain "trea" before the one that only has the letters spread out."""
        ranked = rank(nvim, "trea", ["testReleaseArtifact", TASK, "treasureMap", "setupTreasureHunt"])
        assert ranked.index("testReleaseArtifact") == 3, ranked
        assert ranked[0] == "treasureMap", "and one that starts with it comes first"

    def test_camel_hump_initials_beat_scattered_letters(self, nvim):
        ranked = rank(nvim, "fMT", ["formatItemsMeta", TASK])
        assert ranked == [TASK, "formatItemsMeta"], ranked

    def test_the_start_of_a_word_counts_for_more(self, nvim):
        ranked = rank(nvim, "bj", ["subjectBuilder", "bootJar"])
        assert ranked[0] == "bootJar", ranked

    def test_shorter_wins_a_tie(self, nvim):
        assert rank(nvim, "build", ["buildEnvironment", "build"])[0] == "build"

    def test_nothing_that_does_not_match_is_listed(self, nvim):
        assert rank(nvim, "xyz", ["build", "test", TASK]) == []

    def test_the_group_can_be_typed_too_at_less_weight(self, nvim):
        got = nvim.exec_lua("""
            local items = { { name = 'assemble', group = 'build' }, { name = 'help', group = 'help' }, { name = 'buildAll', group = 'other' } }
            local out = {}
            for _, e in ipairs(require('ij_bridge.fuzzy').rank(items, 'build', function(t) return t.name, t.group .. ' ' .. t.name end)) do
              out[#out + 1] = e.item.name
            end
            return out""")
        assert got == ["buildAll", "assemble"], "the task named for it first, then the one in its group: " + str(got)


TASKS = ["assemble", "bootJar", "bootRun", "build", "check", "clean", "compileKotlin", "findMysteriousTreasure",
         "help", "processResources", "test", "treasureMap"]


class TestThePicker:

    def open(self, nvim, chosen=None):
        nvim.exec_lua("""
            local items = {}
            for _, name in ipairs(...) do items[#items + 1] = { name = name, group = 'group', project = 'project' } end
            _G.__chosen = nil
            require('ij_bridge.picker').open(items, {
              title = 'Test', label = function(i) return i.name end,
              texts = function(i) return i.name, i.group .. ' ' .. i.name end,
              on_choose = function(i) _G.__chosen = i.name end })""", TASKS)
        wait_until(lambda: nvim.eval("mode()") == "i", timeout=5, message="the picker never took the input")

    def state(self, nvim) -> dict:
        return nvim.exec_lua("local c = require('ij_bridge.picker').current; if not c then return vim.NIL end; "
                             "local names = {}; for _, e in ipairs(c.ranked) do names[#names + 1] = e.item.name end; "
                             "return { query = c.query, names = names, index = c.index }")

    def test_it_lists_everything_until_something_is_typed(self, nvim):
        self.open(nvim)
        assert sorted(self.state(nvim)["names"]) == sorted(TASKS)
        nvim.input("<Esc>")

    @pytest.mark.parametrize("typed,first", [("fMT", "findMysteriousTreasure"), ("eas", "treasureMap"), ("trea", "treasureMap"),
                                             ("bR", "bootRun"), ("cK", "compileKotlin")])
    def test_typing_filters_and_ranks_as_you_go(self, nvim, typed, first):
        self.open(nvim)
        nvim.input(typed)
        wait_until(lambda: (self.state(nvim) or {}).get("query") == typed, timeout=5)
        names = self.state(nvim)["names"]
        assert names and names[0] == first, names
        assert "findMysteriousTreasure" in names if typed in ("eas", "fMT", "trea") else True
        nvim.input("<Esc>")

    def test_backspace_widens_it_again(self, nvim):
        self.open(nvim)
        nvim.input("xyz")
        wait_until(lambda: self.state(nvim)["names"] == [], timeout=5)
        nvim.input("<BS><BS><BS>")
        wait_until(lambda: len(self.state(nvim)["names"]) == len(TASKS), timeout=5)
        nvim.input("<Esc>")

    def test_the_arrows_and_control_keys_move_and_wrap(self, nvim):
        self.open(nvim)
        nvim.input("<Down><Down>")
        wait_until(lambda: self.state(nvim)["index"] == 3, timeout=5)
        nvim.input("<C-p><Up>")
        wait_until(lambda: self.state(nvim)["index"] == 1, timeout=5)
        nvim.input("<Up>")
        wait_until(lambda: self.state(nvim)["index"] == len(TASKS), timeout=5, message="the list wraps to the end")
        nvim.input("<Esc>")

    def test_enter_chooses_the_highlighted_one_and_closes(self, nvim):
        self.open(nvim)
        nvim.input("fMT<CR>")
        wait_until(lambda: nvim.exec_lua("return _G.__chosen") == "findMysteriousTreasure", timeout=5)
        assert self.state(nvim) is None, "the picker closes"
        assert nvim.eval("mode()") == "n"

    def test_escape_closes_without_choosing(self, nvim):
        self.open(nvim)
        nvim.input("fMT<Esc>")
        wait_until(lambda: self.state(nvim) is None, timeout=5)
        assert nvim.exec_lua("return _G.__chosen") is None

    def test_enter_with_nothing_matching_chooses_nothing(self, nvim):
        self.open(nvim)
        nvim.input("xyz<CR>")
        wait_until(lambda: self.state(nvim) is None, timeout=5)
        assert nvim.exec_lua("return _G.__chosen") is None

    def test_the_matched_letters_are_underlined(self, nvim):
        self.open(nvim)
        nvim.input("fMT")
        wait_until(lambda: (self.state(nvim) or {}).get("query") == "fMT", timeout=5)
        marks = nvim.exec_lua("""
            local ns = vim.api.nvim_create_namespace('ij_bridge_picker')
            local win = vim.tbl_filter(function(w) return vim.api.nvim_win_get_config(w).focusable == false end, vim.api.nvim_list_wins())[1]
            local buf = vim.api.nvim_win_get_buf(win)
            local n = 0
            for _, m in ipairs(vim.api.nvim_buf_get_extmarks(buf, ns, 0, -1, { details = true })) do
              if m[4].hl_group == 'Special' then n = n + 1 end
            end
            return n""")
        assert marks == 3, marks
        nvim.input("<Esc>")
