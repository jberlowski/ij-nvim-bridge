"""New file from a template (FEATURES.md §5): `$/ij/newFile`.

An extension, since LSP has no "create from a template" request. The Brain returns the text
IntelliJ's own file template gives and where it belongs; it creates nothing.
"""
from __future__ import annotations

import pytest

from harness.wire import SRC, RpcError, uri

DIR = f"{SRC}/probe/created"            # does not exist: the package is the path's, not the disk's
PKG = "dev.bridge.fixture.probe.created"


def new_file(w, name="Ticket", template="class", language="kotlin", directory=DIR):
    return w.request("$/ij/newFile", {"directory": uri(directory), "name": name,
                                      "template": template, "language": language}, timeout=60)


class TestKotlin:

    def test_a_class_gets_its_package_from_the_directory(self, wire):
        r = new_file(wire)
        assert r["uri"] == uri(f"{DIR}/Ticket.kt") and r["package"] == PKG
        assert f"package {PKG}" in r["text"] and "class Ticket" in r["text"], r["text"]
        assert r["text"].endswith("\n") and "\r" not in r["text"]

    @pytest.mark.parametrize("kind,needle", [
        ("interface", "interface Ticket"),
        ("enum", "enum class Ticket"),
        ("object", "object Ticket"),
        ("dataClass", "data class Ticket(val value: String)"),
    ])
    def test_the_other_kinds(self, wire, kind, needle):
        assert needle in new_file(wire, template=kind)["text"]

    def test_the_source_root_itself_has_no_package_line(self, wire):
        r = new_file(wire, directory="/work/fixture/src/main/kotlin")
        assert r["package"] == "" and "package" not in r["text"], r["text"]


class TestJava:

    def test_a_class(self, wire):
        r = new_file(wire, language="java")
        assert r["uri"] == uri(f"{DIR}/Ticket.java")
        assert f"package {PKG};" in r["text"] and "public class Ticket" in r["text"], r["text"]

    @pytest.mark.parametrize("kind,needle", [
        ("interface", "public interface Ticket"),
        ("enum", "public enum Ticket"),
        ("record", "public record Ticket"),
    ])
    def test_the_other_kinds(self, wire, kind, needle):
        assert needle in new_file(wire, language="java", template=kind)["text"]


class TestRefusals:

    def test_nothing_is_created_on_disk(self, wire, bridge_container):
        new_file(wire)
        assert bridge_container.exec(f"test -e {DIR} && echo yes || echo no").stdout.strip() == "no"

    @pytest.mark.parametrize("name", ["", "1Ticket", "Tick et", "a-b"])
    def test_a_name_that_is_not_a_class_name(self, wire, name):
        with pytest.raises(RpcError) as e:
            new_file(wire, name=name)
        assert e.value.code == -32602 and "not a valid name" in str(e.value)

    def test_a_file_that_exists_is_not_overwritten(self, wire):
        with pytest.raises(RpcError) as e:
            new_file(wire, name="Shapes", directory=f"{SRC}/probe")
        assert e.value.code == -32602 and "already exists" in str(e.value)

    def test_a_directory_outside_every_source_root(self, wire):
        with pytest.raises(RpcError) as e:
            new_file(wire, directory="/work/fixture/build")
        assert e.value.code == -32602 and "source root" in str(e.value)

    def test_an_unknown_kind_says_what_there_is(self, wire):
        with pytest.raises(RpcError) as e:
            new_file(wire, template="widget")
        assert e.value.code == -32602 and "interface" in str(e.value)
