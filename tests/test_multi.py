"""Several Neovims and several IntelliJs (FEATURES.md §6c).

  1. one Neovim, two projects (a second `idea <root>` opens it in the running IDE): each buffer
     goes to *its* project's Brain, never the other's;
  2. two Neovims, one project, one IDE;
  3. two Neovims, two projects;
  4. a Neovim closed and reopened, in each of these;
  5. starting the IDE for a root nothing serves: a command and a key, detached, never automatic.

The second project is a copy of the fixture, opened by the Editor's own `:IjBridge open`, which is what
runs `idea <root>` (assumed on the PATH) - so the launch is tested by everything that follows it.
"""
from __future__ import annotations

import time

import pytest

from conftest import start_nvim
from harness.util import wait_until
from harness.wire import SRC
from test_editor_slice import attached

P1 = "/work/fixture"
P2 = "/work/fixture2"
KT = "src/main/kotlin/dev/bridge/fixture/multi"
ONE_DEF, ONE_USE = f"{P1}/{KT}/OnlyInOne.kt", f"{P1}/{KT}/UseOne.kt"
TWO_DEF, TWO_USE = f"{P2}/{KT}/OnlyInTwo.kt", f"{P2}/{KT}/UseTwo.kt"
ONE_ANOTHER = f"{P1}/{KT}/AlsoInOne.kt"


def kotlin(package_tail: str, body: str) -> str:
    return f"package dev.bridge.fixture.multi\n\n{body}\n"


FILES = {
    ONE_DEF: kotlin("", 'class OnlyInOne {\n    fun where() = "one"\n}'),
    ONE_USE: kotlin("", "fun useOne() = OnlyInOne().where()"),
    ONE_ANOTHER: kotlin("", "fun alsoOne() = 1"),
    TWO_DEF: kotlin("", 'class OnlyInTwo {\n    fun where() = "two"\n}'),
    TWO_USE: kotlin("", "fun useTwo() = OnlyInTwo().where()"),
}


# ---------------------------------------------------------------------------- helpers
def root_of(nvim) -> str | None:
    return nvim.exec_lua("local c = vim.lsp.get_clients({ bufnr = 0, name = 'ij-bridge' })[1]; return c and c.config.root_dir")


def brain_state(nvim) -> dict:
    """The debug state of the Brain that serves the current buffer, asked through Neovim's own Session."""
    return nvim.exec_lua("""
        local c = vim.lsp.get_clients({ bufnr = 0, name = 'ij-bridge' })[1]
        if not c then return vim.NIL end
        local r = c:request_sync('$/ij/debug/state', {}, 20000)
        local out = { project = r.result.project, root = r.result.root, state = r.result.state, mirrors = {} }
        for _, m in ipairs(r.result.mirrors) do table.insert(out.mirrors, m.uri) end
        return out""")


def notes(nvim) -> list[str]:
    """What the Editor has said with `vim.notify`. LazyVim replaces it (a popup), so `:messages` has none."""
    return nvim.exec_lua("return _G.__notes or {}")


def record_notes(nvim) -> None:
    nvim.exec_lua("_G.__notes = {}; vim.notify = function(msg, level) table.insert(_G.__notes, msg) end")


def statusline(nvim) -> str:
    return nvim.exec_lua("return require('ij_bridge').statusline()")


def open_file(nvim, path: str, ready: bool = True):
    nvim.command(f"edit {path}")
    wait_until(lambda: attached(nvim) == 1, timeout=60, message=f"{path} never attached")
    if ready:
        wait_until(lambda: statusline(nvim) == "IJ", timeout=300, interval=2,
                   message=f"{path}: the Brain never became Ready (status {statusline(nvim)!r})")


def mirrors(nvim) -> list[str]:
    return sorted(m.rsplit("/", 1)[-1] for m in (brain_state(nvim) or {}).get("mirrors", []))


def definition_uri(nvim, needle: str) -> str | None:
    """Where the Brain says the symbol at `needle` is defined, through Neovim's own request."""
    lines = nvim.current.buffer[:]
    row = next(i for i, l in enumerate(lines) if needle in l)
    col = lines[row].index(needle)
    found = nvim.exec_lua("""
        local row, col = ...
        local res = vim.lsp.buf_request_sync(0, 'textDocument/definition', {
          textDocument = vim.lsp.util.make_text_document_params(),
          position = { line = row, character = col } }, 60000)
        for _, r in pairs(res or {}) do
          local loc = r.result and (r.result[1] or r.result)
          if loc and loc.uri then return loc.uri end
        end
        return vim.inspect(res)""", row, col + 1)
    assert found.startswith("file://"), f"no definition for {needle!r} at {row}:{col + 1} of {nvim.current.buffer.name}: {found}"
    return found


@pytest.fixture
def second_neovim(bridge_container):
    """A second Neovim in the same container, closed at the end."""
    nv = start_nvim(bridge_container, wait_for_blink=False, slot=2)
    yield nv
    nv.harness_editor.close()


@pytest.fixture(scope="module")
def projects(bridge, bridge_container):
    """Two projects: the fixture, and a copy of it that no IDE has opened yet."""
    c = bridge_container
    c.exec("ln -sf /opt/idea/bin/idea /usr/local/bin/idea", user="root")     # `idea` on the PATH
    from harness.wire import Wire
    with Wire(bridge.port) as w:                                              # a second project opens in a new window
        w.initialize()
        w.request("$/ij/debug/openInNewWindow", {})
    c.exec(f"rm -rf {P2} && mkdir -p {P2} && cd {P1} && "
           f"tar cf - --exclude=./build --exclude=./.gradle --exclude=./.idea --exclude=./{KT} . | (cd {P2} && tar xf -)")
    for path, text in FILES.items():
        c.write_file(path, text)
    # The first project's IDE is already running, and there is no file watcher in the container to tell it
    # about the new files (HARNESS.md): ask it to look, and wait until it has settled.
    with Wire(bridge.port) as w:
        w.initialize()
        w.request("$/ij/debug/refresh", {})
        time.sleep(1.5)
        seen = {"n": 0}

        def ready():
            seen["n"] = seen["n"] + 1 if w.debug_state()["state"] == "Ready" else 0
            return seen["n"] >= 3
        wait_until(ready, timeout=180, interval=0.7, message="the first project never settled")
    yield
    c.exec(f"rm -rf {P1}/{KT}", check=False)


# ======================================================== 5. starting the IDE, on request
class TestOpen:

    def test_nothing_starts_by_itself(self, nvim, projects):
        """A file in a project no IDE serves is Dormant, as ever: starting an IDE is asked for."""
        nvim.command(f"edit {TWO_USE}")
        time.sleep(3)
        assert attached(nvim) == 0 and root_of(nvim) is None
        assert not nvim.exec_lua("return next(require('ij_bridge').starting) ~= nil")

    def test_the_command_starts_the_ide_for_a_root_nothing_serves_and_the_buffer_attaches(self, nvim, projects, bridge_container):
        nvim.command(f"edit {TWO_USE}")
        nvim.command("IjBridge open")
        wait_until(lambda: attached(nvim) == 1, timeout=240, interval=2,
                   message="the buffer never attached to the IDE that was started: " + nvim.command_output("messages")[-500:])
        wait_until(lambda: statusline(nvim) == "IJ", timeout=300, interval=2,
                   message=f"never Ready: {statusline(nvim)!r}")
        assert root_of(nvim) == P2
        assert brain_state(nvim)["root"] == P2
        assert not nvim.exec_lua("return next(require('ij_bridge').starting) ~= nil"), "no longer starting"

    def test_the_process_is_not_tied_to_neovim(self, bridge_container):
        """`idea` keeps its terminal for as long as it lives; the Editor must not have waited on it,
        and the IDE must have no Neovim for a parent."""
        parents = bridge_container.exec("ps -o ppid= -p $(pgrep -x idea | head -1)", check=False).stdout.strip()
        nvim_pids = bridge_container.exec("pgrep -x nvim", check=False).stdout.split()
        assert parents and parents not in nvim_pids, (parents, nvim_pids)

    def test_asking_again_says_it_is_already_serving(self, nvim, projects):
        nvim.command(f"edit {TWO_USE}")
        wait_until(lambda: attached(nvim) == 1)
        record_notes(nvim)
        nvim.command("IjBridge open")
        wait_until(lambda: any("already serves /work/fixture2" in n for n in notes(nvim)), timeout=10,
                   message=f"said: {notes(nvim)}")

    def test_a_missing_idea_is_reported_not_raised(self, nvim, projects):
        nvim.exec_lua("require('ij_bridge').opts.idea_cmd = 'no-such-idea-command'")
        record_notes(nvim)
        try:
            nvim.exec_lua("vim.fn.mkdir('/tmp/fixture3', 'p'); vim.fn.writefile({ '' }, '/tmp/fixture3/.git-marker')")
            nvim.exec_lua("vim.fn.mkdir('/tmp/fixture3/.idea', 'p')")
            nvim.command("edit /tmp/fixture3/A.kt")
            nvim.command("IjBridge open")
            wait_until(lambda: any("no-such-idea-command is not on the PATH" in n for n in notes(nvim)), timeout=10,
                       message=f"said: {notes(nvim)}")
            assert not nvim.exec_lua("return next(require('ij_bridge').starting) ~= nil")
        finally:
            nvim.exec_lua("require('ij_bridge').opts.idea_cmd = nil")

    def test_the_launch_command_can_be_set_by_environment_variable(self, nvim, projects):
        """The right `idea_cmd` is often machine- or user-specific (a Toolbox script under the user's
        home, a macOS .app's launcher), so it must be settable without editing a shared init.lua."""
        record_notes(nvim)
        try:
            nvim.exec_lua("vim.env.IJ_NVIM_BRIDGE_IDEA_CMD = 'no-such-idea-command-from-env'")
            nvim.exec_lua("vim.fn.mkdir('/tmp/fixture4/.idea', 'p')")
            nvim.command("edit /tmp/fixture4/A.kt")
            nvim.command("IjBridge open")
            wait_until(lambda: any("no-such-idea-command-from-env is not on the PATH" in n for n in notes(nvim)),
                       timeout=10, message=f"said: {notes(nvim)}")
        finally:
            nvim.exec_lua("vim.env.IJ_NVIM_BRIDGE_IDEA_CMD = nil")

    def test_setup_idea_cmd_wins_over_the_environment_variable(self, nvim, projects):
        record_notes(nvim)
        try:
            nvim.exec_lua("vim.env.IJ_NVIM_BRIDGE_IDEA_CMD = 'no-such-idea-command-from-env'")
            nvim.exec_lua("require('ij_bridge').opts.idea_cmd = 'no-such-idea-command-from-opts'")
            nvim.exec_lua("vim.fn.mkdir('/tmp/fixture5/.idea', 'p')")
            nvim.command("edit /tmp/fixture5/A.kt")
            nvim.command("IjBridge open")
            wait_until(lambda: any("no-such-idea-command-from-opts is not on the PATH" in n for n in notes(nvim)),
                       timeout=10, message=f"said: {notes(nvim)}")
            assert not any("from-env" in n for n in notes(nvim))
        finally:
            nvim.exec_lua("vim.env.IJ_NVIM_BRIDGE_IDEA_CMD = nil")
            nvim.exec_lua("require('ij_bridge').opts.idea_cmd = nil")

    def test_a_file_that_belongs_to_no_project_is_said_so(self, nvim, projects):
        nvim.command("edit /tmp/not-a-project-at-all.txt")
        record_notes(nvim)
        nvim.command("IjBridge open")
        wait_until(lambda: any("does not look like a project" in n for n in notes(nvim)), timeout=10,
                   message=f"said: {notes(nvim)}")


# ================================================== 1. one Neovim, two projects
class TestOneNeovimTwoProjects:

    def test_each_buffer_goes_to_its_own_brain(self, nvim, projects):
        open_file(nvim, ONE_USE)
        assert root_of(nvim) == P1 and brain_state(nvim)["root"] == P1
        buf_one = nvim.current.buffer.number
        open_file(nvim, TWO_USE)
        assert root_of(nvim) == P2 and brain_state(nvim)["root"] == P2
        # and the first is still the first's
        nvim.command(f"buffer {buf_one}")
        assert root_of(nvim) == P1

    def test_two_sessions_not_one(self, nvim, projects):
        open_file(nvim, ONE_USE)
        open_file(nvim, TWO_USE)
        roots = nvim.exec_lua("""
            local out = {}
            for _, c in ipairs(vim.lsp.get_clients({ name = 'ij-bridge' })) do table.insert(out, c.config.root_dir) end
            table.sort(out); return out""")
        assert roots == [P1, P2], roots

    def test_a_request_is_answered_by_the_project_the_buffer_is_in(self, nvim, projects):
        """Each project has a class the other does not: it resolves in its own, and only there."""
        open_file(nvim, ONE_USE)
        assert definition_uri(nvim, "OnlyInOne").endswith(f"{KT}/OnlyInOne.kt")
        assert P2 not in definition_uri(nvim, "OnlyInOne")
        open_file(nvim, TWO_USE)
        assert definition_uri(nvim, "OnlyInTwo").endswith(f"{KT}/OnlyInTwo.kt")
        assert definition_uri(nvim, "OnlyInTwo").startswith(f"file://{P2}/")

    def test_unsaved_text_stays_with_its_own_project(self, nvim, projects):
        open_file(nvim, ONE_USE)
        nvim.current.buffer.append("// only in the first project, unsaved", 0)
        first = nvim.current.buffer.number
        open_file(nvim, TWO_USE)
        wait_until(lambda: "UseTwo.kt" in mirrors(nvim), timeout=30)
        assert "UseOne.kt" not in mirrors(nvim), "the other project's Brain must not hold this buffer"
        nvim.command(f"buffer {first}")
        assert "UseOne.kt" in mirrors(nvim) and "UseTwo.kt" not in mirrors(nvim)

    def test_the_statusline_follows_the_buffer(self, nvim, projects):
        open_file(nvim, ONE_USE)
        open_file(nvim, TWO_USE)
        assert statusline(nvim) == "IJ"
        nvim.command("edit /tmp/not-a-project-at-all.txt")
        assert statusline(nvim) == "", "outside every project: nothing"


# ====================================================== 2. two Neovims, one project
class TestTwoNeovimsOneProject:

    def test_both_attach_to_the_one_brain_and_neither_disturbs_the_other(self, nvim, second_neovim, projects):
        open_file(nvim, ONE_USE)
        open_file(second_neovim, ONE_ANOTHER)
        assert root_of(nvim) == root_of(second_neovim) == P1
        wait_until(lambda: {"UseOne.kt", "AlsoInOne.kt"} <= set(mirrors(nvim)), timeout=30,
                   message=f"the Brain holds {mirrors(nvim)}")

        # each edits its own file: neither's unsaved text is touched by the other
        nvim.current.buffer.append("// from the first", 0)
        second_neovim.current.buffer.append("// from the second", 0)
        time.sleep(1.5)
        assert "from the first" in "\n".join(nvim.current.buffer[:]) and "from the second" not in "\n".join(nvim.current.buffer[:])
        assert "from the second" in "\n".join(second_neovim.current.buffer[:])

    def test_both_get_answers(self, nvim, second_neovim, projects):
        open_file(nvim, ONE_USE)
        open_file(second_neovim, ONE_USE)
        assert definition_uri(nvim, "OnlyInOne").endswith("OnlyInOne.kt")
        assert definition_uri(second_neovim, "OnlyInOne").endswith("OnlyInOne.kt")

    def test_closing_one_releases_only_its_claims(self, nvim, second_neovim, projects):
        open_file(nvim, ONE_USE)
        open_file(second_neovim, ONE_ANOTHER)
        wait_until(lambda: "AlsoInOne.kt" in mirrors(nvim), timeout=30)
        second_neovim.harness_editor.close()                    # killed, as a closed terminal would
        wait_until(lambda: "AlsoInOne.kt" not in mirrors(nvim), timeout=30,
                   message="the Brain kept the closed Neovim's Mirror")
        assert "UseOne.kt" in mirrors(nvim), "the other Neovim's Mirror must survive"
        assert definition_uri(nvim, "OnlyInOne"), "and the first Neovim still works"


# ==================================================== 3. two Neovims, two projects
class TestTwoNeovimsTwoProjects:

    def test_each_connects_to_the_right_ide(self, nvim, second_neovim, projects):
        open_file(nvim, ONE_USE)
        open_file(second_neovim, TWO_USE)
        assert root_of(nvim) == P1 and root_of(second_neovim) == P2
        assert brain_state(nvim)["root"] == P1 and brain_state(second_neovim)["root"] == P2
        assert mirrors(nvim) == ["UseOne.kt"] and mirrors(second_neovim) == ["UseTwo.kt"]

    def test_each_is_answered_by_its_own(self, nvim, second_neovim, projects):
        open_file(nvim, ONE_USE)
        open_file(second_neovim, TWO_USE)
        assert definition_uri(nvim, "OnlyInOne").startswith(f"file://{P1}/")
        assert definition_uri(second_neovim, "OnlyInTwo").startswith(f"file://{P2}/")


# ======================================================== 4. closed and reopened
class TestClosedAndReopened:

    def test_in_one_project_a_reopened_neovim_finds_the_brain_as_it_was(self, nvim, projects, bridge_container):
        first = start_nvim(bridge_container, wait_for_blink=False, slot=2)
        try:
            open_file(first, ONE_ANOTHER)
            wait_until(lambda: "AlsoInOne.kt" in (mirrors(first) or []), timeout=30)
            first.current.buffer.append("// unsaved and then closed", 0)
            first.harness_editor.close()
        finally:
            pass
        open_file(nvim, ONE_USE)
        wait_until(lambda: "AlsoInOne.kt" not in mirrors(nvim), timeout=30, message="the closed Neovim's Mirror stayed")

        again = start_nvim(bridge_container, wait_for_blink=False, slot=2)
        try:
            open_file(again, ONE_ANOTHER)
            wait_until(lambda: "AlsoInOne.kt" in mirrors(again), timeout=30)
            assert "unsaved and then closed" not in "\n".join(again.current.buffer[:]), "unsaved text is Neovim's, and went with it"
        finally:
            again.harness_editor.close()

    def test_with_two_projects_reopening_reconnects_to_the_right_one_each(self, nvim, projects, bridge_container):
        for round_ in range(2):
            other = start_nvim(bridge_container, wait_for_blink=False, slot=2)
            try:
                open_file(other, TWO_USE)
                assert root_of(other) == P2 and brain_state(other)["root"] == P2
                open_file(nvim, ONE_USE)
                assert root_of(nvim) == P1
                other.current.buffer.append(f"// round {round_}", 0)
            finally:
                other.harness_editor.close()
            wait_until(lambda: "UseTwo.kt" not in mirrors_via(nvim, TWO_USE), timeout=30,
                       message=f"round {round_}: the second project kept the closed Neovim's Mirror")


def mirrors_via(nvim, path: str) -> list[str]:
    """The mirrors of the Brain serving `path`, asked without leaving the current buffer's Session."""
    return nvim.exec_lua("""
        local path = ...
        local root = require('ij_bridge.registry').resolve(path)
        for _, c in ipairs(vim.lsp.get_clients({ name = 'ij-bridge' })) do
          if root and c.config.root_dir == root.root then
            local r = c:request_sync('$/ij/debug/state', {}, 20000)
            local out = {}
            for _, m in ipairs(r.result.mirrors) do table.insert(out, m.uri:match('[^/]+$')) end
            return out
          end
        end
        return {}""", path)
