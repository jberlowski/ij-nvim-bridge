"""Moving a file (FEATURES.md §5): `workspace/willRenameFiles`.

The claim is not "these edits look right" but "after them, and the move, the project
compiles": each test applies the answer, moves the file on disk, and asks IntelliJ, through
the diagnostics the Brain publishes, whether anything in the affected files is broken. A
control test moves the file *without* the edits, to show the harness can see the breakage.
"""
from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest

from harness.util import wait_until
from harness.wire import SRC, uri
from test_bridge_slice import published
from test_editor_slice import attached
from test_formatting import apply_edits

MV = f"{SRC}/mv"
MVJ = f"{SRC}/mvj"
PKG = "dev.bridge.fixture.mv"
PKGJ = "dev.bridge.fixture.mvj"

# ------------------------------------------------------------------- Kotlin project
KT = {
    f"{MV}/old/Gadget.kt": f"package {PKG}.old\n\nclass Gadget(val name: String) {{\n    fun describe(): String = Tool.label(name)\n}}\n",
    f"{MV}/old/Tool.kt": f"package {PKG}.old\n\nobject Tool {{\n    fun label(s: String) = \"tool:$s\"\n}}\n",
    f"{MV}/old/SamePackageUser.kt": f"package {PKG}.old\n\nfun make(): Gadget = Gadget(\"a\")\n",
    f"{MV}/other/Importer.kt": f"package {PKG}.other\n\nimport {PKG}.old.Gadget\n\nfun use(g: Gadget) = g.describe()\n",
    f"{MV}/other/Qualified.kt": f"package {PKG}.other\n\nfun q() = {PKG}.old.Gadget(\"q\")\n",
    f"{MV}/other/StarUser.kt": f"package {PKG}.other\n\nimport {PKG}.old.*\n\nfun star(): Gadget = Gadget(\"s\")\n",
}
KT_MOVED = (f"{MV}/old/Gadget.kt", f"{MV}/dest/Gadget.kt")

# --------------------------------------------------------------------- Java project
JV = {
    f"{MVJ}/old/Widget.java": f"package {PKGJ}.old;\n\npublic class Widget {{\n    public String describe() {{ return Tool.label(\"w\"); }}\n}}\n",
    f"{MVJ}/old/Tool.java": f"package {PKGJ}.old;\n\npublic class Tool {{\n    public static String label(String s) {{ return \"tool:\" + s; }}\n}}\n",
    f"{MVJ}/old/SamePackageUser.java": f"package {PKGJ}.old;\n\npublic class SamePackageUser {{\n    public Widget make() {{ return new Widget(); }}\n}}\n",
    f"{MVJ}/other/Importer.java": f"package {PKGJ}.other;\n\nimport {PKGJ}.old.Widget;\n\npublic class Importer {{\n    public String use(Widget w) {{ return w.describe(); }}\n}}\n",
    f"{MVJ}/other/Qualified.java": f"package {PKGJ}.other;\n\npublic class Qualified {{\n    public Object q() {{ return new {PKGJ}.old.Widget(); }}\n}}\n",
}
JV_MOVED = (f"{MVJ}/old/Widget.java", f"{MVJ}/fresh/Widget.java")


@pytest.fixture
def project(request, bridge_container, project_files, wire):
    """A small project of files that use one another, made for one test and removed after."""
    files = request.param
    project_files.many(files)
    yield files
    bridge_container.exec(f"rm -rf {MV} {MVJ}", check=False)


def path_of(u: str) -> str:
    return unquote(urlparse(u).path)


def will_rename(wire, old: str, new: str):
    return wire.request("workspace/willRenameFiles",
                        {"files": [{"oldUri": uri(old), "newUri": uri(new)}]}, timeout=120)


def apply_to_disk(c, edit) -> None:
    for u, edits in edit["changes"].items():
        p = path_of(u)
        c.write_file(p, apply_edits(c.read_file(p), edits))


def move_on_disk(c, old: str, new: str) -> None:
    c.exec(f"mkdir -p $(dirname {new}) && mv {old} {new}")


def errors(diagnostics):
    return [d for d in diagnostics if d["severity"] == 1]


def broken_files(c, wire, paths):
    """Open each file and return {path: [error messages]} for those IntelliJ finds broken."""
    wire.request("$/ij/debug/refresh", {})
    time.sleep(2)
    out = {}
    for p in paths:
        wire.did_open(p, c.read_file(p))
        try:
            pub = published(wire, p, lambda d: True, timeout=60)
        except (AssertionError, TimeoutError):
            out[p] = ["NOTHING WAS PUBLISHED for this file"]
            continue
        time.sleep(3)          # the analysis of a fresh file settles after its first publication
        try:
            pub = published(wire, p, lambda d: True, timeout=5)
        except (AssertionError, TimeoutError):
            pass
        if errors(pub["diagnostics"]):
            out[p] = [d["message"] for d in errors(pub["diagnostics"])]
    return out


def after_move(c, wire, files, moved, use_edits=True):
    old, new = moved
    edit = will_rename(wire, old, new)
    if use_edits:
        apply_to_disk(c, edit)
    else:
        # What a person does by hand: change the moved file's own package line, and stop there.
        own = {u: e for u, e in edit["changes"].items() if path_of(u) == old}
        apply_to_disk(c, {"changes": {u: [e[0]] for u, e in own.items()}})
    move_on_disk(c, old, new)
    paths = [new if p == old else p for p in files]
    return edit, broken_files(c, wire, paths)


# ================================================================ what is answered
class TestEdits:

    @pytest.mark.parametrize("project", [KT], indirect=True)
    def test_kotlin_the_package_imports_and_names_all_follow(self, project, bridge_container, wire):
        edit = will_rename(wire, *KT_MOVED)
        texts = {path_of(u): apply_edits(bridge_container.read_file(path_of(u)), e) for u, e in edit["changes"].items()}

        moved = texts[KT_MOVED[0]]
        assert f"package {PKG}.dest" in moved and f"package {PKG}.old" not in moved
        assert f"import {PKG}.old.Tool" in moved, "what it used from its old package, unsaid"
        assert f"import {PKG}.dest.Gadget" in texts[f"{MV}/old/SamePackageUser.kt"], "the file next door"
        assert f"import {PKG}.dest.Gadget" in texts[f"{MV}/other/Importer.kt"]
        assert f"{PKG}.old.Gadget" not in texts[f"{MV}/other/Importer.kt"]
        assert f"{PKG}.dest.Gadget(\"q\")" in texts[f"{MV}/other/Qualified.kt"], "spelled out in full"
        assert f"import {PKG}.dest.Gadget" in texts[f"{MV}/other/StarUser.kt"], "a star import no longer covers it"
        assert f"{MV}/old/Tool.kt" not in texts, "what did not move, and is not used, is left alone"

    @pytest.mark.parametrize("project", [JV], indirect=True)
    def test_java_the_package_imports_and_names_all_follow(self, project, bridge_container, wire):
        edit = will_rename(wire, *JV_MOVED)
        texts = {path_of(u): apply_edits(bridge_container.read_file(path_of(u)), e) for u, e in edit["changes"].items()}

        moved = texts[JV_MOVED[0]]
        assert f"package {PKGJ}.fresh;" in moved
        assert f"import {PKGJ}.old.Tool;" in moved
        assert f"import {PKGJ}.fresh.Widget;" in texts[f"{MVJ}/old/SamePackageUser.java"]
        assert f"import {PKGJ}.fresh.Widget;" in texts[f"{MVJ}/other/Importer.java"]
        assert f"new {PKGJ}.fresh.Widget()" in texts[f"{MVJ}/other/Qualified.java"]

    @pytest.mark.parametrize("project", [KT], indirect=True)
    def test_a_file_moved_within_its_package_needs_nothing(self, project, bridge_container, wire):
        edit = will_rename(wire, f"{MV}/old/Gadget.kt", f"{MV}/old/Renamed.kt")
        assert edit == {"changes": {}}

    @pytest.mark.parametrize("project", [KT], indirect=True)
    def test_it_answers_with_the_files_as_they_are_and_changes_none(self, project, bridge_container, wire):
        before = {p: bridge_container.read_file(p) for p in KT}
        will_rename(wire, *KT_MOVED)
        assert {p: bridge_container.read_file(p) for p in KT} == before

    def test_it_is_advertised_for_kotlin_and_java_files(self, bridge):
        from harness.wire import Wire
        with Wire(bridge.port) as w:
            ops = w.initialize()["capabilities"]["workspace"]["fileOperations"]
        assert ops["willRename"]["filters"][0]["pattern"]["glob"] == "**/*.{kt,java}"
        assert set(ops) == {"willRename", "didRename", "didCreate", "didDelete"}


# ====================================== the project compiles afterwards (IntelliJ says so)
class TestTheProjectStillCompiles:

    @pytest.mark.parametrize("project", [KT], indirect=True)
    def test_control_changing_only_the_moved_files_package_breaks_it_and_the_harness_sees_that(self, project, bridge_container, wire):
        _, broken = after_move(bridge_container, wire, list(KT), KT_MOVED, use_edits=False)
        assert broken, "changing only the package left nothing broken: the check proves nothing"
        assert f"{MV}/other/Importer.kt" in broken, broken

    @pytest.mark.parametrize("project", [KT], indirect=True)
    def test_kotlin_after_the_edits_and_the_move_nothing_is_broken(self, project, bridge_container, wire):
        _, broken = after_move(bridge_container, wire, list(KT), KT_MOVED)
        assert broken == {}, broken

    @pytest.mark.parametrize("project", [JV], indirect=True)
    def test_java_after_the_edits_and_the_move_nothing_is_broken(self, project, bridge_container, wire):
        _, broken = after_move(bridge_container, wire, list(JV), JV_MOVED)
        assert broken == {}, broken
