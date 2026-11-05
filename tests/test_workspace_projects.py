"""One IntelliJ Project at a parent, two independently imported Gradle builds.

These are real IDE/Neovim tests. Identical declarations/tasks/tests deliberately
make choosing the wrong build observable rather than accidentally successful.
"""
from __future__ import annotations

import time
import pytest

from conftest import BRAIN_ZIP, REPO_ROOT, start_nvim
from harness.container import Container
from harness.ide import Ide
from harness.brain import Brain
from harness.wire import Wire, RpcError, uri
from harness.util import wait_until
from test_gradle_tasks import collect, text_of
from test_run_configurations import collect as collect_config

ROOT = "/work/workspace"
A, B = ROOT + "/A", ROOT + "/B"
SOURCE = "/src/main/java/dev/bridge/workspace/Identity.java"
TEST = "/src/test/java/dev/bridge/workspace/IdentityTest.java"


def context(path):
    return {"textDocument": {"uri": uri(path)}}


@pytest.fixture(scope="module")
def workspace_container():
    c = Container.start(novnc_port=6084, nvim_port=7783, brain_port=7883, nvim2_port=7784)
    try:
        for build in (A, B):
            c.copy_in(REPO_ROOT / "fixture-secondary", build)
            c.exec(f"cp -r /work/fixture/gradle {build}/gradle && cp /work/fixture/gradlew {build}/gradlew")
        for rel in ("settings.gradle.kts", SOURCE.lstrip("/"), TEST.lstrip("/")):
            path = f"{A}/{rel}"
            c.write_file(path, c.read_file(path).replace('"B"', '"A"').replace('onlyB', 'onlyA').replace('TEST_IN_B', 'TEST_IN_A'))
        # IntelliJ owns the links/import. Opening a directory alone is insufficient.
        c.write_file(ROOT + "/.idea/gradle.xml", '''<project version="4">
  <component name="GradleSettings"><option name="linkedExternalProjectsSettings"><GradleProjectSettings>
    <option name="externalProjectPath" value="$PROJECT_DIR$/A" />
    <option name="gradleJvm" value="#JAVA_HOME" />
  </GradleProjectSettings><GradleProjectSettings>
    <option name="externalProjectPath" value="$PROJECT_DIR$/B" />
    <option name="gradleJvm" value="#JAVA_HOME" />
  </GradleProjectSettings></option></component>
</project>''')
        # Configurations belong to the IntelliJ workspace; each names its own build.
        for label in ("A", "B"):
            c.write_file(ROOT + f"/.run/{label}.run.xml", f'''<component name="ProjectRunConfigurationManager">
  <configuration default="false" name="Probe {label}" type="GradleRunConfiguration" factoryName="Gradle">
    <ExternalSystemSettings>
      <option name="externalProjectPath" value="$PROJECT_DIR$/{label}" />
      <option name="externalSystemIdString" value="GRADLE" />
      <option name="taskNames"><list><option value="routingProbe" /></list></option>
    </ExternalSystemSettings><method v="2" />
  </configuration></component>''')
        yield c
    finally:
        c.stop()


@pytest.fixture(scope="module")
def workspace_brain(workspace_container):
    c = workspace_container
    ide = Ide(c)
    ide.trust()
    ide.install_plugin(BRAIN_ZIP)
    ide.launch(ROOT)
    ide.await_window("workspace", timeout=300)
    brain = Brain(c)
    brain.await_registry()
    assert len(brain.registry().brains) == 1
    sock, = brain.sockets()
    brain.bridge(sock)
    # Wait for both imported models, not just an aggregate log/readiness count.
    with Wire(brain.port, timeout=60) as w:
        w.initialize()
        wait_until(lambda: {p["path"] for p in w.request("$/ij/tasks")["projects"]} == {A, B},
                   timeout=600, interval=3, message="both Gradle builds never imported")
        wait_until(lambda: w.debug_state()["state"] == "Ready", timeout=300, interval=2)
    return brain


@pytest.fixture
def workspace_wire(workspace_brain):
    with Wire(workspace_brain.port) as w:
        w.initialize()
        yield w
        for path in (A + SOURCE, B + SOURCE, A + TEST, B + TEST):
            w.did_close(path)


@pytest.fixture(scope="module")
def workspace_nvim_session(workspace_brain, workspace_container):
    nv = start_nvim(workspace_container, wait_for_blink=False, cwd=A)
    yield nv
    nv.harness_editor.close()


@pytest.fixture
def workspace_nvim(workspace_nvim_session):
    nv = workspace_nvim_session
    nv.command("stopinsert")
    nv.command("silent! %bwipeout!")
    try:
        yield nv
    finally:
        nv.command("stopinsert")
        nv.command("silent! %bwipeout!")


def open_file(nv, path):
    nv.command("edit " + path)
    wait_until(lambda: nv.exec_lua("return require('ij_bridge').client(0) ~= nil"), timeout=60)
    wait_until(lambda: nv.exec_lua("local s = require('ij_bridge').status(); return s and s.state") == "Ready", timeout=180)


def test_one_workspace_reports_both_builds(workspace_wire):
    info = workspace_wire.request("$/ij/projectContext")
    assert info["root"] == ROOT
    assert {b["path"] for b in info["builds"]} == {A, B}


@pytest.mark.parametrize("build", [A, B])
def test_task_listing_and_execution_follow_file(workspace_wire, workspace_container, build):
    w = workspace_wire
    items = w.request("$/ij/tasks", context(build + SOURCE))
    assert [p["path"] for p in items["projects"]] == [build]
    other = B if build == A else A
    workspace_container.exec(f"rm -f {A}/build/routing-marker {B}/build/routing-marker")
    started = w.request("$/ij/task/run", {"path": build, "tasks": ["routingProbe"]})
    output, finished = collect(w, started["runId"])
    assert finished["success"], (finished, text_of(output))
    assert "ROUTED_TO_" + build[-1] in text_of(output)
    assert workspace_container.read_file(build + "/build/routing-marker") == build[-1]
    assert workspace_container.exec(f"test -f {other}/build/routing-marker", check=False).returncode != 0


def test_unowned_file_never_defaults_to_a_sibling(workspace_wire):
    with pytest.raises(RpcError, match="no imported Gradle build"):
        workspace_wire.request("$/ij/tasks", context(ROOT + "/README.md"))


@pytest.mark.parametrize("build", [A, B])
def test_configuration_listing_and_execution_follow_file(workspace_wire, build):
    w = workspace_wire
    configurations = w.request("$/ij/runConfigurations", context(build + SOURCE))["configurations"]
    item, = [c for c in configurations if c["name"].startswith("Probe ")]
    assert item["path"] == build and item["id"]
    started = w.request("$/ij/runConfiguration/run", item)
    output, _, finished = collect_config(w, started["runId"])
    assert finished["success"], finished
    assert "ROUTED_TO_" + build[-1] in text_of(output)


def test_status_switches_builds_without_changing_cwd(workspace_nvim):
    nv = workspace_nvim
    for build in (A, B, A):
        open_file(nv, build + SOURCE)
        assert nv.exec_lua("return vim.fn.getcwd()") == A
        assert nv.exec_lua("return require('ij_bridge').statusline()") == "IJ: workspace / " + build[-1]
        assert nv.exec_lua("return #vim.lsp.get_clients({name='ij-bridge'})") == 1
        assert build in nv.command_output("IjBridge")
        rendered = nv.exec_lua("return vim.api.nvim_eval_statusline(vim.o.statusline, {winid=vim.api.nvim_get_current_win()}).str")
        assert "IJ: workspace / " + build[-1] in rendered, rendered


def test_editor_task_picker_keeps_its_origin_after_switch(workspace_nvim):
    nv = workspace_nvim
    open_file(nv, A + SOURCE)
    nv.exec_lua("""
      _G.workspace_items = nil
      require('ij_bridge.tasks').fetch(function(tree)
        _G.workspace_items = require('ij_bridge.tasks').picker_items(tree)
      end)
    """)
    wait_until(lambda: nv.exec_lua("return _G.workspace_items ~= nil"), timeout=60)
    open_file(nv, B + SOURCE)
    nv.exec_lua("""
      for _, item in ipairs(_G.workspace_items) do
        if item.name == 'routingProbe' then require('ij_bridge.tasks').run(item); return end
      end
      error('routingProbe missing')
    """)
    wait_until(lambda: nv.exec_lua("local s=require('ij_bridge.tasks').run_state; return s and s.finished ~= nil"), timeout=180)
    state = nv.exec_lua("local s=require('ij_bridge.tasks').run_state; return {label=s.context.label,ok=s.finished.success}")
    assert state == {"label": "workspace / A", "ok": True}
    assert nv.exec_lua("return require('ij_bridge').statusline()") == "IJ: workspace / B"


@pytest.mark.parametrize("build", [A, B])
def test_same_fqn_navigation_and_rename_use_own_module(workspace_wire, workspace_container, build):
    from test_navigation import ask, at
    from test_rename import changes
    w = workspace_wire
    source, test = build + SOURCE, build + TEST
    original = workspace_container.read_file(source)
    # The declaration has an unsaved line shift; navigation must use Mirror positions.
    w.did_open(source, "\n\n" + original)
    text = workspace_container.read_file(test)
    w.did_open(test, text)
    pos = at(text, "new Identity()", into=5)
    result = ask(w, "textDocument/definition", test, pos)
    assert {loc["uri"] for loc in result} == {uri(source)}
    assert result[0]["range"]["start"]["line"] == at(original, "class Identity")[0] + 2
    renamed = ask(w, "textDocument/rename", source, at("\n\n" + original, "identify()", into=2), newName="describeBuild")
    edited = changes(renamed)
    assert uri(source) in edited and uri(test) in edited
    assert all(path.startswith(uri(build) + "/") for path in edited), edited


@pytest.mark.parametrize("build", [A, B])
def test_reading_formatting_and_templates_in_each_build(workspace_wire, workspace_container, build):
    from test_navigation import ask, at
    from test_symbols import flat
    w = workspace_wire
    source = build + SOURCE
    text = workspace_container.read_file(source)
    w.did_open(source, text)
    symbols = w.request("textDocument/documentSymbol", context(source))
    assert {s["name"] for s in flat(symbols)} >= {"Identity", "identify", "only" + build[-1]}
    assert ask(w, "textDocument/hover", source, at(text, "identify()", into=2))
    assert w.request("textDocument/foldingRange", context(source))
    assert w.request("textDocument/selectionRange", {**context(source), "positions": [{"line": 3, "character": 20}]})
    messy = text.replace('public String identify() {', 'public   String identify(){')
    w.did_change(source, 1, {"text": messy})
    edits = w.request("textDocument/formatting", {**context(source), "options": {"tabSize": 4, "insertSpaces": True}})
    assert edits, "formatting failed in child build"
    created = w.request("$/ij/newFile", {"directory": uri(build + "/src/main/java/dev/bridge/workspace/newpackage"),
        "name": "Created", "language": "java", "template": "class"})
    assert created["uri"].startswith(uri(build) + "/")
    assert "package dev.bridge.workspace.newpackage;" in created["text"]


@pytest.mark.parametrize("build", [A, B])
def test_sync_targets_only_owning_build(workspace_wire, build):
    w = workspace_wire
    started = w.request("$/ij/sync", context(build + SOURCE))
    assert started["paths"] == [build]
    done = w.notifications("$/ij/sync/finished", until=lambda _: True, timeout=300)[-1]
    assert done["success"], done
    wait_until(lambda: w.debug_state()["state"] == "Ready", timeout=180, interval=2)
    assert {p["path"] for p in w.request("$/ij/tasks")["projects"]} == {A, B}


@pytest.mark.parametrize("build", [A, B])
def test_test_navigation_and_running_stay_in_build(workspace_wire, workspace_container, build):
    from test_navigation import ask, at
    from test_test_running import run_at, until_finished
    w = workspace_wire
    source, test = build + SOURCE, build + TEST
    production = workspace_container.read_file(source)
    text = workspace_container.read_file(test)
    w.did_open(source, production)
    w.did_open(test, text)
    targets = ask(w, "$/ij/testTargets", source, at(production, "class Identity", into=7))
    assert {loc["uri"] for loc in targets["locations"]} == {uri(test)}
    started = run_at(w, test, at(text, "identifiesBuild()", into=3))
    done = until_finished(w, started["runId"], timeout=240)
    assert done["success"], done
    # Test stdout travels through the test-results channel, not the raw Gradle log.
    report = workspace_container.read_file(build + "/build/test-results/test/TEST-dev.bridge.workspace.IdentityTest.xml")
    assert "TEST_IN_" + build[-1] in report
    assert "TEST_IN_" + ("B" if build == A else "A") not in report
    statuses = [m["params"] for m in w.inbox if m.get("method") == "$/ij/test/status"
                and m["params"]["runId"] == started["runId"]]
    assert any(s["status"] == "passed" for s in statuses), statuses


def test_editor_configurations_keep_origin_and_repeat_per_build(workspace_nvim):
    nv = workspace_nvim
    open_file(nv, A + SOURCE)
    nv.exec_lua("""
      _G.workspace_configs = nil
      require('ij_bridge.runconfigs').fetch(function(items) _G.workspace_configs = items end)
    """)
    wait_until(lambda: nv.exec_lua("return _G.workspace_configs ~= nil"), timeout=60)
    assert nv.exec_lua("return _G.workspace_configs[1].path") == A
    open_file(nv, B + SOURCE)
    nv.exec_lua("require('ij_bridge.runconfigs').run(_G.workspace_configs[1])")
    wait_until(lambda: nv.exec_lua("local s=require('ij_bridge.runconfigs').run_state; return s and s.finished ~= nil"), timeout=180)
    assert nv.exec_lua("return require('ij_bridge.runconfigs').run_state.context.build") == A
    nv.exec_lua("""
      _G.repeat_notes = {}
      local old = vim.notify
      vim.notify = function(msg) table.insert(_G.repeat_notes, msg) end
      require('ij_bridge.runconfigs').repeat_last()
      vim.notify = old
    """)
    assert any("no run configuration has been run in this build" in s for s in nv.exec_lua("return _G.repeat_notes"))
    open_file(nv, A + SOURCE)
    previous = nv.exec_lua("return require('ij_bridge.runconfigs').run_state.id")
    nv.exec_lua("require('ij_bridge.runconfigs').repeat_last()")
    wait_until(lambda: nv.exec_lua("return require('ij_bridge.runconfigs').run_state.id") != previous, timeout=30)
    wait_until(lambda: nv.exec_lua("return require('ij_bridge.runconfigs').run_state.finished ~= nil"), timeout=180)
    assert nv.exec_lua("return require('ij_bridge.runconfigs').run_state.finished.success") is True


def test_stop_uses_recorded_run_after_switch(workspace_nvim):
    nv = workspace_nvim
    open_file(nv, A + SOURCE)
    previous = nv.exec_lua("local s=require('ij_bridge.tasks').run_state; return s and s.id")
    nv.exec_lua("require('ij_bridge.tasks').run({path='/work/workspace/A',name='routingSlow'})")
    wait_until(lambda: nv.exec_lua("local s=require('ij_bridge.tasks').run_state; return s and s.id") not in (None, previous), timeout=30)
    open_file(nv, B + SOURCE)
    try:
        wait_until(lambda: "SLOW_A" in nv.exec_lua("local b=require('ij_bridge.tasks').output_buffer_for_tests(); return table.concat(vim.api.nvim_buf_get_lines(b,0,-1,false),'\\n')"), timeout=60)
        nv.exec_lua("require('ij_bridge.tasks').stop()")
        wait_until(lambda: nv.exec_lua("return require('ij_bridge.tasks').run_state.finished ~= nil"), timeout=60)
        assert nv.exec_lua("return require('ij_bridge.tasks').run_state.finished.cancelled") is True
    finally:
        nv.exec_lua("require('ij_bridge.tasks').stop()")


@pytest.mark.parametrize("build", [A, B])
def test_completion_and_insertion_do_not_mix_same_fqn(workspace_wire, workspace_container, build):
    from harness.wire import all_items
    from test_completion_insertion import position_of
    from test_formatting import apply_edits
    w = workspace_wire
    for root in (A, B):
        w.did_open(root + SOURCE, workspace_container.read_file(root + SOURCE))
    marked = '''package dev.bridge.workspace;
public class IdentityTest {
    public int example() { return new Identity().on|; }
}
'''
    text, line, col = position_of(marked)
    path = build + TEST
    w.did_open(path, text)
    first, batches = w.complete(path, line, col, timeout=60)
    items = all_items(first, batches)
    own = "only" + build[-1]
    other = "only" + ("B" if build == A else "A")
    names = {i["label"] for i in items}
    assert own in names and other not in names, names
    item = next(i for i in items if i["label"] == own)
    resolved = w.request("completionItem/resolve", item)
    result = apply_edits(text, [resolved["textEdit"]] + resolved.get("additionalTextEdits", []))
    assert own + "()" in result


def test_two_neovims_share_brain_but_keep_build_context(workspace_nvim, workspace_container):
    nv = workspace_nvim
    open_file(nv, A + SOURCE)
    # Dirty buffers remain Mirrored while a second Editor works in the other build.
    nv.current.buffer.append("// unsaved A")
    other = start_nvim(workspace_container, wait_for_blink=False, slot=2, cwd=B)
    try:
        open_file(other, B + SOURCE)
        other.current.buffer.append("// unsaved B")
        assert nv.exec_lua("return require('ij_bridge').statusline()") == "IJ: workspace / A"
        assert other.exec_lua("return require('ij_bridge').statusline()") == "IJ: workspace / B"
        with Wire(workspace_container.brain_port) as w:
            w.initialize()
            wait_until(lambda: {uri(A + SOURCE), uri(B + SOURCE)} <= {m["uri"] for m in w.debug_state()["mirrors"]}, timeout=30)
    finally:
        other.harness_editor.close()
        nv.command("silent! %bwipeout!")


def test_repeat_test_from_another_file_uses_build_history(workspace_nvim):
    from test_navigation import at
    nv = workspace_nvim
    open_file(nv, A + TEST)
    line, col = at("\n".join(nv.current.buffer[:]), "identifiesBuild()", into=3)
    nv.current.window.cursor = (line + 1, col)
    nv.exec_lua("""
      _G.workspace_test_finished = nil
      vim.api.nvim_create_autocmd('User', {pattern='IjBridgeTestRunFinished',
        callback=function(a) _G.workspace_test_finished=a.data end})
      require('ij_bridge.test_run').run_nearest()
    """)
    wait_until(lambda: nv.exec_lua("return _G.workspace_test_finished ~= nil"), timeout=180)
    assert nv.exec_lua("return _G.workspace_test_finished.success") is True
    open_file(nv, A + SOURCE)
    wait_until(lambda: not nv.exec_lua("return require('ij_bridge').client(vim.fn.bufnr('/work/workspace/A" + TEST + "')) ~= nil"), timeout=30)
    nv.exec_lua("_G.workspace_test_finished=nil; require('ij_bridge.test_run').repeat_last()")
    wait_until(lambda: nv.exec_lua("return _G.workspace_test_finished ~= nil"), timeout=180)
    assert nv.exec_lua("return _G.workspace_test_finished.success") is True
    assert nv.exec_lua("return require('ij_bridge.test_results').run.project_root") == A
    assert not nv.exec_lua("return require('ij_bridge').client(vim.fn.bufnr('/work/workspace/A" + TEST + "')) ~= nil")
