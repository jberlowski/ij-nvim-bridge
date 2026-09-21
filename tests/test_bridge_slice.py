"""The first Bridge vertical slice, asserted against a real Brain.

Covers SPEC.md §4 (Registry, Session, capabilities), §5 (Mirror Set, attach,
didChange, save) and §6 (streaming completion, supersession), plus the §7 speed
gate. Every test here talks to the Brain plugin over its socket; none of them
involves Neovim, which is a separate slice.
"""
from __future__ import annotations

import hashlib
import json
import statistics

import pytest

from harness.wire import (SRC, RpcError, Wire, all_items, replace_range, uri,
                          run_local)

PRODUCER = f"{SRC}/probe/CrossFileProducer.kt"
CONSUMER = f"{SRC}/probe/CrossFileConsumer.kt"
INSERTION = "// HARNESS-INSERTION-POINT"
CONSUMER_MARKER = "// A test appends a call to the unsaved method here and asserts it resolves."

# SPEC.md §7. Provisional until the first measured baseline is agreed (§14).
OVERHEAD_BUDGET_MS = 15.0


def md5(c, path: str) -> str:
    return hashlib.md5(c.read_file(path).encode()).hexdigest()


def end_of(text: str) -> tuple[int, int]:
    """LSP position of the end of `text`."""
    lines = text.split("\n")
    return len(lines) - 1, len(lines[-1])


# ------------------------------------------------------------------ session
class TestSession:

    def test_registry_names_this_project(self, bridge, bridge_container):
        brains = bridge.registry().brains
        mine = [b for b in brains if b["root"] == "/work/fixture"]
        assert len(mine) == 1, brains
        pid = mine[0]["pid"]
        assert bridge_container.exec(f"kill -0 {pid}", check=False).returncode == 0
        assert mine[0]["ide"].startswith("IU-")

    def test_socket_and_directory_are_owner_only(self, bridge, bridge_container):
        """SPEC.md §3: Unprivileged. Another account must not reach the IDE."""
        sock = bridge.socket
        assert bridge_container.exec(f"stat -c %a {sock}").stdout.strip() == "600"
        assert bridge_container.exec(f"stat -c %a {sock.rsplit('/', 1)[0]}").stdout.strip() == "700"

    def test_initialize_advertises_only_what_works(self, bridge):
        """Passthrough: a Capability not proven is not offered."""
        with Wire(bridge.port) as w:
            caps = w.initialize()["capabilities"]
        assert caps["completion"] == {"streaming": True, "resolve": True}
        assert caps["diagnostics"] is True
        assert caps["formatting"] is False
        assert caps["ij"]["ide"].startswith("IU-262")

    def test_ready_once_the_import_has_finished(self, wire):
        assert wire.debug_state()["state"] == "Ready"

    def test_unknown_request_is_answered_not_ignored(self, wire):
        with pytest.raises(RpcError) as e:
            wire.request("textDocument/rename", {})          # not implemented yet (FEATURES.md §3)
        assert e.value.code == -32601

    def test_a_failing_handler_reports_instead_of_stalling(self, wire):
        """A throwing handler once closed the canary's connection and made the
        client hang until its timeout (HARNESS.md §13)."""
        with pytest.raises(RpcError):
            wire.request("$/ij/completion", {})            # no textDocument
        assert wire.debug_state()["state"] == "Ready"      # and the Session lives


# ------------------------------------------------------------------ mirrors
class TestMirrors:

    def test_didopen_text_wins_over_disk(self, bridge_container, wire):
        disk = bridge_container.read_file(PRODUCER)
        buffer = disk + "\n// only in the buffer\n"
        before = md5(bridge_container, PRODUCER)
        wire.did_open(PRODUCER, buffer)

        (m,) = wire.debug_state(text=True)["mirrors"]
        assert m["uri"] == uri(PRODUCER)
        assert m["text"] == buffer
        assert m["open"] is True and m["convergent"] is True
        assert md5(bridge_container, PRODUCER) == before, "the Brain must never write"

    def test_incremental_changes_apply_in_order(self, bridge_container, wire):
        text = bridge_container.read_file(PRODUCER)
        wire.did_open(PRODUCER, text)
        wire.did_change(PRODUCER, 1, replace_range(0, 0, 0, "// first\n"))
        wire.did_change(PRODUCER, 2, replace_range(0, 3, 8, "FIRST"))
        expected = ("// first\n" + text).replace("// first", "// FIRST", 1)

        (m,) = wire.debug_state(text=True)["mirrors"]
        assert m["text"] == expected
        assert m["version"] == 2

    def test_a_change_without_a_range_replaces_the_whole_buffer(self, bridge_container, wire):
        wire.did_open(PRODUCER, bridge_container.read_file(PRODUCER))
        wire.did_change(PRODUCER, 1, {"text": "package dev.bridge.fixture.probe\n"})
        (m,) = wire.debug_state(text=True)["mirrors"]
        assert m["text"] == "package dev.bridge.fixture.probe\n"

    def test_edits_never_reach_disk(self, bridge_container, wire):
        before = md5(bridge_container, PRODUCER)
        wire.did_open(PRODUCER, bridge_container.read_file(PRODUCER))
        wire.did_change(PRODUCER, 1, replace_range(0, 0, 0, "// edited\n"))
        assert md5(bridge_container, PRODUCER) == before

    def test_didclose_releases_the_mirror(self, bridge_container, wire):
        wire.did_open(PRODUCER, bridge_container.read_file(PRODUCER))
        assert len(wire.debug_state()["mirrors"]) == 1
        wire.did_close(PRODUCER)
        assert wire.debug_state()["mirrors"] == []

    def test_the_editors_write_does_not_disturb_the_ide(self, bridge_container, wire):
        """SPEC.md §5.4. The Editor writes; the Brain writes nothing and asks
        IntelliJ for nothing - see MirrorVetoer."""
        import time
        original = bridge_container.read_file(PRODUCER)
        text = original + "\n// saved by the Editor\n"
        wire.did_open(PRODUCER, text)
        try:
            bridge_container.write_file(PRODUCER, text)     # the Editor's write
            time.sleep(2)                                   # the file watcher reacts
            wire.notify("textDocument/didSave", {"textDocument": {"uri": uri(PRODUCER), "version": 0}})
            wire.timeout = 10
            (m,) = wire.debug_state(text=True)["mirrors"]
            assert m["text"] == text
            assert m["convergent"] is True
        finally:
            bridge_container.write_file(PRODUCER, original)

    def test_will_save_acknowledges_only_after_earlier_changes_are_applied(
            self, bridge_container, wire):
        """SPEC.md §5.4: flush pending didChange, then await the Brain's ack of
        version N, then write. The ack must therefore be ordered behind the
        changes it covers."""
        wire.did_open(PRODUCER, bridge_container.read_file(PRODUCER))
        wire.did_change(PRODUCER, 1, replace_range(0, 0, 0, "// one\n"))
        wire.did_change(PRODUCER, 2, replace_range(0, 0, 0, "// two\n"))
        ack = wire.request("textDocument/willSaveWaitUntil", {
            "textDocument": {"uri": uri(PRODUCER)}, "reason": 1})
        assert ack == []
        # Nothing was in flight when the ack came back: version 2 is already there.
        (m,) = wire.debug_state(text=True)["mirrors"]
        assert m["version"] == 2 and m["text"].startswith("// two\n// one\n")

    def test_closing_a_mirror_discards_its_unsaved_text(self, bridge_container, wire):
        """:bd! throws a buffer away. Its Mirror must not linger as an unsaved
        document for IntelliJ to autosave onto disk once the veto lifts."""
        import time
        before = md5(bridge_container, PRODUCER)
        wire.did_open(PRODUCER, bridge_container.read_file(PRODUCER) + "\n// discarded\n")
        wire.did_close(PRODUCER)
        wire.request("$/ij/debug/saveAll", {})
        time.sleep(1)
        assert md5(bridge_container, PRODUCER) == before

    def test_an_external_write_never_blocks_the_ide(self, bridge_container, wire):
        """ADR-0003 hazard, found by the harness: the Editor writes the file
        while the Mirror holds unsaved text. Unvetoed, IntelliJ raises a modal
        "changes in memory and on disk" dialog that holds the EDT."""
        original = bridge_container.read_file(PRODUCER)
        wire.did_open(PRODUCER, original + "\n// unsaved in the Mirror\n")
        try:
            bridge_container.write_file(PRODUCER, original + "\n// different on disk\n")
            import time
            time.sleep(3)                       # let the file watcher see it
            wire.timeout = 10
            assert wire.debug_state(text=True)["mirrors"], "the EDT is held"
        finally:
            bridge_container.write_file(PRODUCER, original)

    def test_the_ide_never_autosaves_a_mirror(self, bridge_container, wire):
        """IntelliJ saves unsaved documents when idle and on frame deactivation.
        A Mirror's unsaved text is an Editor buffer the developer never saved."""
        import time
        before = md5(bridge_container, PRODUCER)
        wire.did_open(PRODUCER, bridge_container.read_file(PRODUCER) + "\n// never saved\n")
        # Ask for exactly what an idle IDE or a frame deactivation does.
        wire.request("$/ij/debug/saveAll", {})
        time.sleep(1)
        assert md5(bridge_container, PRODUCER) == before

    def test_unsaved_buffer_is_visible_to_another_buffer(self, bridge_container, wire):
        """The scenario the Mirror Set rule exists for (SPEC.md §5.2): add a
        method to Foo without saving, switch to Bar, and Bar must see it. From
        disk the Brain would answer "cannot resolve"."""
        producer = bridge_container.read_file(PRODUCER).replace(
            INSERTION, "fun brandNewUnsavedMethod(): Int = 42")
        consumer = bridge_container.read_file(CONSUMER).replace(
            CONSUMER_MARKER, "fun probe() = producer.")
        wire.did_open(PRODUCER, producer)
        wire.did_open(CONSUMER, consumer)

        line, ch = next((i, len(l)) for i, l in enumerate(consumer.split("\n"))
                        if "fun probe() = producer." in l)
        first, batches = wire.complete(CONSUMER, line, ch)
        labels = [i["label"] for i in all_items(first, batches)]
        assert "brandNewUnsavedMethod" in labels, labels[:20]
        assert "existingMethod" in labels

    def test_mirrors_survive_the_editor_tabs_limit(self, bridge_container, wire):
        """SPEC.md §11 Q6, as a standing regression: with a tiny limit, opening
        more Mirrors than fit must not lose any of them."""
        wire.request("$/ij/debug/setTabLimit", {"limit": 3})
        files = [f"{SRC}/{n}" for n in (
            "Application.kt", "domain/Greeting.kt", "domain/GreetingRepository.kt",
            "service/GreetingService.kt", "web/GreetingController.kt",
            "probe/ResolutionError.kt")]
        for f in files:
            wire.did_open(f, bridge_container.read_file(f))
        state = wire.debug_state()
        assert len(state["mirrors"]) == 6
        assert all(m["open"] for m in state["mirrors"]), \
            [(m["uri"].rsplit("/", 1)[1], m["open"]) for m in state["mirrors"]]


# --------------------------------------------------------------- completion
def large_surface(c) -> tuple[str, int, int]:
    """CrossFileConsumer with a half-typed call into the 800-member class."""
    text = c.read_file(CONSUMER).replace(
        CONSUMER_MARKER, "val probe = LargeSurface().compute")
    line = next(i for i, l in enumerate(text.split("\n")) if "LargeSurface().compute" in l)
    return text, line, len(text.split("\n")[line])


class TestCompletion:

    def test_items_are_intellijs_own_and_in_its_order(self, bridge_container, wire):
        text, line, ch = large_surface(bridge_container)
        wire.did_open(CONSUMER, text)
        first, batches = wire.complete(CONSUMER, line, ch)
        items = all_items(first, batches)

        labels = [i["label"] for i in items]
        assert sum(l.startswith("computeMetricNumber") for l in labels) >= 300
        assert labels[0] == "computeMetricNumber000"
        # IntelliJ's ranking must survive: the Editor may not re-sort.
        sort = [i["sortText"] for i in items if i["label"].startswith("computeMetric")]
        assert sort == sorted(sort)

    def test_stream_is_well_formed(self, bridge_container, wire):
        text, line, ch = large_surface(bridge_container)
        wire.did_open(CONSUMER, text)
        first, batches = wire.complete(CONSUMER, line, ch)
        assert first["streamId"]
        assert first["done"] or batches, "an unfinished Stream must keep delivering"
        assert first["done"] or batches[-1]["done"] is True
        assert all(b["streamId"] == first["streamId"] for b in batches)

    def test_response_carries_the_timings_the_gate_needs(self, bridge_container, wire):
        text, line, ch = large_surface(bridge_container)
        wire.did_open(CONSUMER, text)
        t = wire.complete(CONSUMER, line, ch)[0]["timings"]
        assert set(t) >= {"queuedNanos", "ijFirstItemsNanos", "renderNanos", "sendAfterReceiveNanos"}
        assert t["ijFirstItemsNanos"] > 0

    def test_the_popup_is_hidden_afterwards(self, bridge_container, wire):
        import time
        text, line, ch = large_surface(bridge_container)
        wire.did_open(CONSUMER, text)
        wire.complete(CONSUMER, line, ch)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and wire.debug_state()["lookupActive"]:
            time.sleep(0.1)
        assert wire.debug_state()["lookupActive"] is False

    def test_a_newer_request_supersedes_an_older_one(self, bridge_container, wire):
        """ADR-0008: invokeCompletion cannot be interrupted, so the older
        request is answered RequestCancelled and only the newest is delivered."""
        import time
        text, line, ch = large_surface(bridge_container)
        wire.did_open(CONSUMER, text)
        params = {"textDocument": {"uri": uri(CONSUMER)},
                  "position": {"line": line, "character": ch}}
        # A warm IntelliJ can answer inside the 20 ms below, and then nothing is superseded.
        # Make it finish slowly, after it has been invoked, so the older is still running.
        wire.request("$/ij/debug/completionDelay", {"ms": 600})
        try:
            older = wire.send_request("$/ij/completion", params)
            time.sleep(0.2)                    # older is now running on the EDT
            newer = wire.send_request("$/ij/completion", params)
            a = wire.await_response(older)
        finally:
            wire.request("$/ij/debug/completionDelay", {"ms": 0})
        if "error" in a:
            assert a["error"]["code"] == -32800                   # replaced before it started
        else:
            # It was already being worked on: the answer is delivered, flagged, so the
            # Editor can cache it and show it while the newer request is awaited.
            assert a["result"]["superseded"] is True, a
        b = wire.await_response(newer)
        assert "result" in b and b["result"]["items"], b
        assert wire.debug_state()["state"] == "Ready"

    def test_completion_of_an_unmirrored_buffer_is_an_error(self, wire):
        with pytest.raises(RpcError):
            wire.complete(CONSUMER, 0, 0)


# -------------------------------------------------------------- diagnostics
RESOLUTION = f"{SRC}/probe/ResolutionError.kt"


def published(wire, path, until, timeout=40):
    """Read publishDiagnostics for `path` until `until(diagnostics)` holds."""
    target = uri(path)
    got = wire.notifications(
        "textDocument/publishDiagnostics",
        until=lambda p: p["uri"] == target and until(p["diagnostics"]), timeout=timeout)
    last = got[-1]
    assert last["uri"] == target and until(last["diagnostics"]), (
        f"no matching publishDiagnostics; saw {[(g['uri'].rsplit('/', 1)[1], len(g['diagnostics'])) for g in got]}")
    return last


def unresolved(diagnostics):
    return [d for d in diagnostics if "Unresolved reference" in d["message"]]


class TestDiagnostics:
    """SPEC.md §9: harvested from the daemon, not from inspections alone."""

    def test_cannot_resolve_is_published(self, bridge_container, wire):
        text = bridge_container.read_file(RESOLUTION)
        wire.did_open(RESOLUTION, text)
        pub = published(wire, RESOLUTION, lambda d: len(unresolved(d)) >= 2)

        call = next(d for d in unresolved(pub["diagnostics"]) if "thisFunctionDoesNotExist" in d["message"])
        line = next(i for i, l in enumerate(text.split("\n")) if "thisFunctionDoesNotExistAnywhere()" in l)
        assert call["severity"] == 1
        assert call["source"] == "IntelliJ"
        assert call["range"]["start"]["line"] == line
        assert pub["version"] == 0

    def test_they_follow_an_unsaved_edit(self, bridge_container, wire):
        """A clean file, then an error typed into the buffer and not saved: the
        Brain must flag it, and stop flagging it once fixed."""
        text = bridge_container.read_file(CONSUMER)
        wire.did_open(CONSUMER, text)
        published(wire, CONSUMER, lambda d: not unresolved(d))

        end_line = len(text.rstrip("\n").split("\n")) - 1     # before the closing brace
        wire.did_change(CONSUMER, 1, replace_range(end_line, 0, 0, "    fun broken() = stillNotDefined()\n"))
        published(wire, CONSUMER, lambda d: any("stillNotDefined" in x["message"] for x in unresolved(d)))

        wire.did_change(CONSUMER, 2, {"text": text})            # back to the clean text
        published(wire, CONSUMER, lambda d: not unresolved(d))

    def test_closing_a_mirror_clears_its_diagnostics(self, bridge_container, wire):
        wire.did_open(RESOLUTION, bridge_container.read_file(RESOLUTION))
        published(wire, RESOLUTION, lambda d: len(unresolved(d)) >= 2)
        wire.did_close(RESOLUTION)
        published(wire, RESOLUTION, lambda d: d == [])


# ------------------------------------------------------------------- state
def status_of(wire, until, timeout=30):
    """Read `$/ij/status` notifications until one satisfies `until`."""
    got = wire.notifications("$/ij/status", until=until, timeout=timeout)
    assert got and until(got[-1]), f"never saw the expected status; saw {got}"
    return got[-1]


class TestState:
    """SPEC.md §8: Indexing is always visible and never silent."""

    def test_the_brain_never_claimed_ready_before_the_import_finished(self, bridge):
        """The startup trace: the state the Brain reported, alongside how many
        Gradle imports had committed. Ready before the first commit would mean
        serving 'not resolved until the project is fully loaded'."""
        trace = bridge.trace
        assert any(state == "Indexing" for state, _ in trace), (
            f"the import window was never observed, so this proves nothing: {trace}")
        assert not [t for t in trace if t[0] == "Ready" and t[1] == 0], trace

    def test_state_is_pushed_when_a_session_starts(self, wire):
        assert status_of(wire, lambda p: True)["state"] == "Ready"

    def test_indexing_is_announced_and_diagnostics_are_withheld(self, bridge_container, wire):
        text = bridge_container.read_file(CONSUMER)
        wire.did_open(CONSUMER, text)
        published(wire, CONSUMER, lambda d: not unresolved(d))

        wire.request("$/ij/debug/indexing", {"ms": 6000})
        during = status_of(wire, lambda p: p["state"] == "Indexing")
        assert during["reason"] == "indexing"

        # An error typed while Indexing must not be published: stale is worse than none.
        end_line = len(text.rstrip("\n").split("\n")) - 1
        wire.did_change(CONSUMER, 1, replace_range(end_line, 0, 0, "    fun broken() = stillNotDefined()\n"))
        import time
        wire.timeout = 2.5
        try:
            leaked = [p for p in wire.notifications("textDocument/publishDiagnostics",
                                                    until=lambda p: True, timeout=2.5)
                      if p["uri"] == uri(CONSUMER)]
        except Exception:                                   # nothing arrived: what we want
            leaked = []
        assert leaked == [], f"diagnostics were published while Indexing: {leaked}"
        wire.timeout = 40

        # Leaving Indexing re-publishes what was withheld.
        status_of(wire, lambda p: p["state"] == "Ready")
        published(wire, CONSUMER, lambda d: any("stillNotDefined" in x["message"] for x in unresolved(d)))

    def test_completion_while_indexing_is_degraded_not_an_error(self, bridge_container, wire):
        text, line, ch = large_surface(bridge_container)
        wire.did_open(CONSUMER, text)
        wire.request("$/ij/debug/indexing", {"ms": 4000})
        status_of(wire, lambda p: p["state"] == "Indexing")
        result = wire.request("$/ij/completion", {
            "textDocument": {"uri": uri(CONSUMER)}, "position": {"line": line, "character": ch}})
        assert result["degraded"] is True and result["items"] == []
        assert result["isIncomplete"] is True, "the Editor must ask again on the next keystroke"
        status_of(wire, lambda p: p["state"] == "Ready")


# ------------------------------------------------------------------ overhead
class TestOverhead:
    """SPEC.md §7 / ADR-0005: gate on what the Bridge adds, never on IntelliJ."""

    def test_overhead_stays_inside_the_budget(self, bridge, bridge_container, tmp_path):
        text, line, ch = large_surface(bridge_container)
        samples = run_local(bridge_container, bridge.socket, {
            "open": [{"path": CONSUMER, "text": text}],
            "complete": {"path": CONSUMER, "line": line, "character": ch,
                         "repeat": 30, "warmup": 5},
        })
        assert len(samples) == 30

        # OVERHEAD = (t3 - t0) - (t2 - t1), with t2 - t1 the time IntelliJ took.
        overhead = [(s["roundTripNanos"] - s["timings"]["ijFirstItemsNanos"]) / 1e6
                    for s in samples]
        ij = [s["timings"]["ijFirstItemsNanos"] / 1e6 for s in samples]
        queued = [s["timings"]["queuedNanos"] / 1e6 for s in samples]
        render = [s["timings"]["renderNanos"] / 1e6 for s in samples]
        p95 = lambda v: sorted(v)[int(len(v) * 0.95) - 1]

        report = {
            "items": samples[-1]["items"],
            "ij_time_ms": {"median": statistics.median(ij), "p95": p95(ij)},
            "overhead_ms": {"median": statistics.median(overhead), "p95": p95(overhead)},
            "of_which_queue_ms": statistics.median(queued),
            "of_which_render_ms": statistics.median(render),
            "budget_ms": OVERHEAD_BUDGET_MS,
        }
        print("\n  " + json.dumps(report))
        assert all(o > 0 for o in overhead), "in-IDE time exceeded the round trip"
        assert p95(overhead) < OVERHEAD_BUDGET_MS, report
