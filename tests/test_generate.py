"""Generate code (FEATURES.md §5): what IntelliJ's Generate menu does, as `source.generate.*` code actions.

The menu's own actions are dialogs, so each is done programmatically on a copy of the file, by
IntelliJ's own helpers, and diffed into edits. Every result is checked the way that matters: opened
as a Mirror, IntelliJ finds nothing wrong with it.
"""
from __future__ import annotations

import time

import pytest

from harness.util import wait_until
from harness.wire import SRC, RpcError, replace_range, uri
from test_bridge_slice import published
from test_editor_slice import attached
from test_formatting import apply_edits

JV = f"{SRC}/probe/JavaShapes.java"
KT = f"{SRC}/probe/InspectionWarning.kt"

PERSON = '''package dev.bridge.fixture.probe;

class Person {
    private String name;
    private int age;
    private double weight;
}
'''

PARTIAL = '''package dev.bridge.fixture.probe;

class Person {
    private String name;
    private int age;

    public String getName() {
        return name;
    }
}
'''

IMPL = '''package dev.bridge.fixture.probe;

class Impl implements Shape2 {
}

interface Shape2 {
    double area();
    String name();
}
'''


def pos(text, needle, into=0):
    idx = text.index(needle) + into
    return {"line": text.count("\n", 0, idx), "character": idx - (text.rfind("\n", 0, idx) + 1)}


def offers(w, path, text, needle, into, only=None):
    p = pos(text, needle, into)
    ctx = {"diagnostics": []}
    if only is not None:
        ctx["only"] = only
    for attempt in range(20):
        try:
            return w.request("textDocument/codeAction", {"textDocument": {"uri": uri(path)},
                             "range": {"start": p, "end": p}, "context": ctx}, timeout=90)
        except RpcError as e:
            if e.code != -32801 or attempt == 19:
                raise
            time.sleep(2)


def generate(w, path, text, needle, into, title):
    found = [a for a in offers(w, path, text, needle, into) if title in a["title"]]
    assert found, f"{title!r} not offered: {[a['title'] for a in offers(w, path, text, needle, into)]}"
    resolved = w.request("codeAction/resolve", found[0], timeout=90)
    return apply_edits(text, resolved["edit"]["changes"].get(uri(path), [])), resolved


def errors_in(w, path, text):
    """What IntelliJ says about `text`: the file's errors, once its analysis has settled."""
    w.did_change(path, 99, {"text": text})
    pub = published(w, path, lambda d: True, timeout=60)
    time.sleep(4)
    try:
        pub = published(w, path, lambda d: True, timeout=8)
    except (AssertionError, TimeoutError):
        pass
    return [d["message"] for d in pub["diagnostics"] if d["severity"] == 1]


class TestJava:

    def test_a_constructor_for_all_the_fields(self, wire):
        wire.did_open(JV, PERSON)
        out, _ = generate(wire, JV, PERSON, "class Person", 8, "Generate constructor")
        assert "public Person(String name, int age, double weight) {" in out, out
        assert all(f"this.{f} = {f};" in out for f in ("name", "age", "weight")), out
        assert errors_in(wire, JV, out) == []

    def test_getters_setters_and_both(self, wire):
        wire.did_open(JV, PERSON)
        getters, _ = generate(wire, JV, PERSON, "class Person", 8, "Generate getters")
        assert "public String getName()" in getters and "public int getAge()" in getters and "public double getWeight()" in getters
        assert "setName" not in getters
        setters, _ = generate(wire, JV, PERSON, "class Person", 8, "Generate setters")
        assert "public void setAge(int age)" in setters and "getAge" not in setters
        both, _ = generate(wire, JV, PERSON, "class Person", 8, "Generate getters and setters")
        assert "getWeight()" in both and "setWeight(double weight)" in both
        assert errors_in(wire, JV, both) == []

    def test_only_what_is_missing_is_generated(self, wire):
        wire.did_open(JV, PARTIAL)
        out, _ = generate(wire, JV, PARTIAL, "class Person", 8, "Generate getters")
        assert out.count("getName()") == 1 and "getAge()" in out, out

    def test_to_string(self, wire):
        wire.did_open(JV, PERSON)
        out, _ = generate(wire, JV, PERSON, "class Person", 8, "Generate toString()")
        assert "public String toString()" in out and '"Person{"' in out and "name='" in out and "age=" in out, out
        assert errors_in(wire, JV, out) == []

    def test_equals_and_hash_code_with_their_import(self, wire):
        wire.did_open(JV, PERSON)
        out, _ = generate(wire, JV, PERSON, "class Person", 8, "Generate equals() and hashCode()")
        assert "public boolean equals(Object o)" in out and "public int hashCode()" in out, out
        assert "Objects.equals(name, that.name)" in out and "age == that.age" in out, out
        assert "Double.compare(weight, that.weight) == 0" in out, "a double is compared as IntelliJ does"
        assert "Objects.hash(name, age, weight)" in out
        assert "import java.util.Objects;" in out, out
        assert errors_in(wire, JV, out) == []

    def test_missing_interface_methods_are_implemented(self, wire):
        wire.did_open(JV, IMPL)
        out, _ = generate(wire, JV, IMPL, "class Impl", 8, "Implement 2 missing methods")
        assert "public double area()" in out and "public String name()" in out and out.count("@Override") == 2, out
        assert errors_in(wire, JV, out) == []


class TestOffers:

    def test_what_exists_is_not_offered_again(self, wire):
        wire.did_open(JV, PERSON)
        out, _ = generate(wire, JV, PERSON, "class Person", 8, "Generate constructor")
        wire.did_change(JV, 1, {"text": out})
        titles = [a["title"] for a in offers(wire, JV, out, "class Person", 8)]
        assert "Generate constructor" not in titles and "Generate toString()" in titles, titles

    def test_a_place_that_is_not_in_a_class_offers_nothing_to_generate(self, wire):
        wire.did_open(JV, PERSON)
        assert not [a for a in offers(wire, JV, PERSON, "package dev", 3) if a["kind"].startswith("source.generate")]

    def test_the_kinds_are_told_apart_and_filtered(self, wire):
        wire.did_open(JV, PERSON)
        kinds = lambda only: sorted({a["kind"] for a in offers(wire, JV, PERSON, "class Person", 8, only=only)})
        assert kinds(["source.generate.toString"]) == ["source.generate.toString"]
        assert set(kinds(["source.generate"])) >= {"source.generate.constructor", "source.generate.getters",
                                                   "source.generate.setters", "source.generate.accessors",
                                                   "source.generate.toString", "source.generate.equalsHashCode"}
        assert "source.generate.constructor" not in kinds(["quickfix"])
        assert all(k.startswith("source.") for k in kinds(["source"])), "a parent kind matches what is under it"

    def test_a_request_changes_neither_the_mirror_nor_the_disk(self, wire, bridge_container):
        on_disk = bridge_container.read_file(JV)
        wire.did_open(JV, PERSON)
        generate(wire, JV, PERSON, "class Person", 8, "Generate constructor")
        (m,) = wire.debug_state(text=True)["mirrors"]
        assert m["text"] == PERSON and m["version"] == 0
        assert bridge_container.read_file(JV) == on_disk

    def test_an_action_for_text_that_has_changed_is_content_modified(self, wire):
        wire.did_open(JV, PERSON)
        (action,) = [a for a in offers(wire, JV, PERSON, "class Person", 8) if a["title"] == "Generate constructor"]
        wire.did_change(JV, 1, replace_range(0, 0, 0, "// edited\n"))
        with pytest.raises(RpcError) as e:
            wire.request("codeAction/resolve", action, timeout=60)
        assert e.value.code == -32801

    def test_it_is_advertised(self, bridge):
        from harness.wire import Wire
        with Wire(bridge.port) as w:
            kinds = w.initialize()["capabilities"]["codeActionProvider"]["codeActionKinds"]
        assert "source.generate" in kinds


class TestThroughNeovim:

    def test_the_source_action_menu_generates_a_constructor(self, nvim):
        nvim.command(f"edit {JV}")
        wait_until(lambda: attached(nvim) == 1, message="never attached")
        nvim.current.buffer[:] = PERSON.rstrip("\n").split("\n")
        p = pos(PERSON, "class Person", 8)
        nvim.current.window.cursor = (p["line"] + 1, p["character"])

        def done():
            nvim.exec_lua("""vim.lsp.buf.code_action({ context = { only = { 'source.generate' } },
                filter = function(a) return a.title == 'Generate constructor' end, apply = true })""")
            time.sleep(3)
            return "public Person(String name, int age, double weight)" in "\n".join(nvim.current.buffer[:])
        wait_until(done, timeout=90, interval=2, message="the constructor was never generated:\n" + "\n".join(nvim.current.buffer[:]))


KOTLIN_PERSON = '''package dev.bridge.fixture.probe

class Person(val name: String, var age: Int) {
    val nick: String? = null
    val tags: Array<String> = arrayOf()
}
'''


class TestKotlin:

    def test_to_string(self, wire):
        wire.did_open(KT, KOTLIN_PERSON)
        out, _ = generate(wire, KT, KOTLIN_PERSON, "class Person", 8, "Generate toString()")
        assert 'return "Person(name=$name, age=$age, nick=$nick, tags=$tags)"' in out, out
        assert "override fun toString(): String" in out
        assert errors_in(wire, KT, out) == []

    def test_equals_and_hash_code(self, wire):
        wire.did_open(KT, KOTLIN_PERSON)
        out, _ = generate(wire, KT, KOTLIN_PERSON, "class Person", 8, "Generate equals() and hashCode()")
        assert "override fun equals(other: Any?): Boolean" in out and "other as Person" in out, out
        assert "if (name != other.name) return false" in out and "if (age != other.age) return false" in out
        assert "if (!tags.contentEquals(other.tags)) return false" in out, "arrays are compared by content"
        assert "var result = name.hashCode()" in out and "result = 31 * result + age" in out
        assert "(nick?.hashCode() ?: 0)" in out and "tags.contentHashCode()" in out, out
        assert errors_in(wire, KT, out) == []

    def test_a_data_class_has_them_already(self, wire):
        text = "package dev.bridge.fixture.probe\n\ndata class Point(val x: Int, val y: Int)\n"
        wire.did_open(KT, text)
        assert not [a for a in offers(wire, KT, text, "data class Point", 12) if a["kind"].startswith("source.generate")]

    def test_what_exists_is_not_offered_again(self, wire):
        wire.did_open(KT, KOTLIN_PERSON)
        out, _ = generate(wire, KT, KOTLIN_PERSON, "class Person", 8, "Generate toString()")
        wire.did_change(KT, 1, {"text": out})
        titles = [a["title"] for a in offers(wire, KT, out, "class Person", 8)]
        assert "Generate toString()" not in titles and "Generate equals() and hashCode()" in titles, titles

    def test_an_interface_and_a_class_without_properties_offer_nothing(self, wire):
        text = "package dev.bridge.fixture.probe\n\ninterface Shape3 { fun area(): Double }\n\nclass Empty\n"
        wire.did_open(KT, text)
        for needle in ("interface Shape3", "class Empty"):
            assert not [a for a in offers(wire, KT, text, needle, 12) if a["kind"].startswith("source.generate")], needle

    def test_a_request_changes_neither_the_mirror_nor_the_disk(self, wire, bridge_container):
        on_disk = bridge_container.read_file(KT)
        wire.did_open(KT, KOTLIN_PERSON)
        generate(wire, KT, KOTLIN_PERSON, "class Person", 8, "Generate toString()")
        (m,) = wire.debug_state(text=True)["mirrors"]
        assert m["text"] == KOTLIN_PERSON and m["version"] == 0
        assert bridge_container.read_file(KT) == on_disk

    def test_missing_members_are_implemented(self, wire):
        text = ("package dev.bridge.fixture.probe\n\ninterface Shape3 {\n    fun area(): Double\n    fun name(): String\n}\n\n"
                "class Square3(val side: Double) : Shape3 {\n}\n")
        wire.did_open(KT, text)
        out, _ = generate(wire, KT, text, "class Square3", 8, "Implement 2 missing members")
        assert "override fun area(): Double" in out and "override fun name(): String" in out, out
        assert errors_in(wire, KT, out) == []
