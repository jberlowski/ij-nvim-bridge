"""Format on save (FEATURES.md §6c): the `willSaveWaitUntil` handshake, opted in.

Off by default, since it changes what `:w` means (SPEC.md §5.4: "the Brain never edits on save" was
the v2 default; this is the opt-in that changes it). The opt-in is communicated once, at
`initialize`, via `initializationOptions.formatOnSave` - a standard LSP mechanism, no new wire
method needed - and the edits reuse `textDocument/formatting`'s own, already-tested computation
(`Formatting.edits`, shared with `NavigationEngine` rather than a second instance).

Tested on the wire only. Neovim's own core applies `willSaveWaitUntil`'s returned edits before the
write - long-standing, well-documented core behaviour, not something this project implements - but
proving it through this harness would mean stopping and restarting the shared session's already-
connected LSP client (`vim.lsp.start` reuses a client for the same root, so a later `setup()` call
alone does not give a fresh one its new `init_options`), a real risk to other tests sharing that
session under time pressure. Left as a known, named gap rather than attempted riskily.
"""
from __future__ import annotations

from harness.wire import SRC, Wire, uri
from test_navigation import ready

MULT = f"{SRC}/probe/Multiplier.java"

MESSY_TEXT = ("package dev.bridge.fixture.probe;\n\npublic class   Multiplier   {\n"
              "    public int multiply(int a,int b){return a*b;}\n}\n")


def start(bridge, format_on_save: bool) -> Wire:
    w = Wire(bridge.port)
    params = {"processId": None, "rootUri": uri("/work/fixture"), "capabilities": {}}
    if format_on_save:
        params["initializationOptions"] = {"formatOnSave": True}
    w.request("initialize", params)
    w.notify("initialized")
    return w


class TestOnTheWire:
    def test_opted_in_reformats_on_save(self, bridge_container, bridge):
        with start(bridge, format_on_save=True) as w:
            ready(w)
            w.did_open(MULT, MESSY_TEXT)
            edits = w.request("textDocument/willSaveWaitUntil",
                {"textDocument": {"uri": uri(MULT)}, "reason": 1}, timeout=30)
            assert edits, "no edits at all - format on save did nothing"
            assert "public class Multiplier {" in edits[0]["newText"]
            w.notify("textDocument/didClose", {"textDocument": {"uri": uri(MULT)}})

    def test_default_is_off(self, bridge_container, bridge):
        with start(bridge, format_on_save=False) as w:
            ready(w)
            w.did_open(MULT, MESSY_TEXT)
            edits = w.request("textDocument/willSaveWaitUntil",
                {"textDocument": {"uri": uri(MULT)}, "reason": 1}, timeout=30)
            assert edits == []
            w.notify("textDocument/didClose", {"textDocument": {"uri": uri(MULT)}})
