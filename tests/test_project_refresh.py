"""Explicit file refresh updates IntelliJ's VFS and indices without changing open buffers."""
from harness.util import wait_until
from harness.wire import SRC, uri


def test_refresh_detects_external_create_change_delete(wire, bridge_container):
    source = f"{SRC}/probe/InspectionWarning.kt"
    target = f"{SRC}/probe/BridgeRefreshProbe.kt"
    text = "package dev.bridge.fixture.probe\nfun useRefresh() = BridgeRefreshProbe()\n"
    wire.did_open(source, text)

    def refresh():
        wire.request("$/ij/refresh", {}, timeout=60)
        wait_until(lambda: wire.debug_state()["state"] == "Ready", timeout=120, interval=1)

    def definition():
        return wire.request("textDocument/definition", {
            "textDocument": {"uri": uri(source)},
            "position": {"line": 1, "character": 23},
        }, timeout=60)

    try:
        bridge_container.write_file(target, "package dev.bridge.fixture.probe\nclass BridgeRefreshProbe\n")
        refresh()
        wait_until(lambda: bool(definition()), timeout=60, interval=1, message="created class never resolved")
        assert definition()[0]["uri"] == uri(target)
        bridge_container.write_file(target, "package dev.bridge.fixture.probe\n\n\n\nclass BridgeRefreshProbe\n")
        refresh()
        wait_until(lambda: bool(definition()) and definition()[0]["range"]["start"]["line"] == 4,
                   timeout=60, interval=1, message="changed file was not refreshed")
        bridge_container.exec(f"rm -f {target}")
        refresh()
        wait_until(lambda: not definition(), timeout=60, interval=1, message="deleted class still resolved")
        mirrors = wire.debug_state(text=True)["mirrors"]
        assert next(m for m in mirrors if m["uri"] == uri(source))["text"] == text
    finally:
        bridge_container.exec(f"rm -f {target}")
