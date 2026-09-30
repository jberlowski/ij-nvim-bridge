"""What the Bridge sends, how much, and how fast: a benchmark, not a test.

Opt-in (`BENCH=1 pytest test_bench.py -s`): it drives a real Neovim through scripted scenarios (idle, typing, reading
code, moving the cursor, saving, opening files, running a test) and reads what the Brain logged for each. The Brain's
log records every message in and out (method, bytes, and how long a reply took), so the counts here are the wire's
own, not estimates. The result is printed and written to `tests/artifacts/bench.md`.

Latency has two sides and both are taken: the Brain's own time to answer (`ms` in its log) and what Neovim saw
(round trip through the socket and the Lua client); the difference is what the Bridge itself adds.
"""
from __future__ import annotations

import json
import os
import statistics
import time
from collections import defaultdict
from pathlib import Path

import pytest

from harness.util import wait_until
from harness.wire import SRC, uri

pytestmark = pytest.mark.skipif(os.environ.get("BENCH") != "1", reason="a benchmark: set BENCH=1")

LARGE = f"{SRC}/probe/LargeSurface.kt"
SCRATCH = f"{SRC}/probe/BenchScratch.kt"
SCRATCH_TEXT = "package dev.bridge.fixture.probe\n\nclass BenchScratch {\n    fun a(x: Int) = x + 1\n}\n"
BRAIN_LOG = "f=$(ls -t /home/dev/.local/state/ij-nvim-bridge/brain-*.log | head -1); "
REPORT = Path(__file__).parent / "artifacts" / "bench.md"

SECTIONS: list[str] = []


# ------------------------------------------------------------------ reading the Brain's log
class Window:
    """The Brain's log lines written while a `with` block ran."""

    def __init__(self, c, name: str):
        self.c, self.name = c, name
        self.lines: list[dict] = []
        self.seconds = 0.0

    def count(self) -> int:
        return int(self.c.exec(BRAIN_LOG + "wc -l < $f", check=False).stdout.strip() or 0)

    def __enter__(self):
        self.start = self.count()
        self.t0 = time.monotonic()
        return self

    def __exit__(self, *exc):
        time.sleep(1.0)                                                 # let the last lines land
        self.seconds = time.monotonic() - self.t0 - 1.0
        out = self.c.exec(BRAIN_LOG + f"tail -n +{self.start + 1} $f", check=False).stdout
        for line in out.splitlines():
            try:
                self.lines.append(json.loads(line))
            except ValueError:
                pass


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p * (len(values) - 1))))]


def summarise(window: Window) -> dict:
    """(direction, method) -> { n, bytes, ms[] }. `recv`: Neovim to Brain. `send`: Brain notifications. `reply`: answers."""
    stats: dict = defaultdict(lambda: {"n": 0, "bytes": 0, "ms": []})
    for line in window.lines:
        ev = line.get("ev")
        if ev not in ("recv", "send", "response", "slow_response"):
            continue
        kind = {"recv": "recv", "send": "send"}.get(ev, "reply")
        s = stats[(kind, line.get("method", "?"))]
        s["n"] += 1
        s["bytes"] += line.get("bytes", 0)
        if "ms" in line:
            s["ms"].append(line["ms"])
    return stats


def report(window: Window, note: str = "") -> dict:
    stats = summarise(window)
    seconds = max(window.seconds, 0.001)
    totals = {k: (sum(s["n"] for (kk, _), s in stats.items() if kk == k),
                  sum(s["bytes"] for (kk, _), s in stats.items() if kk == k)) for k in ("recv", "send", "reply")}
    rows = sorted(stats.items(), key=lambda kv: -kv[1]["bytes"])
    out = [f"### {window.name}", "", note, "",
           f"{seconds:.1f} s. Neovim to Brain: {totals['recv'][0]} messages, {totals['recv'][1]:,} B. "
           f"Brain notifications: {totals['send'][0]}, {totals['send'][1]:,} B. "
           f"Brain replies: {totals['reply'][0]}, {totals['reply'][1]:,} B.", "",
           "| direction | method | n | /s | bytes | B avg | ms p50 | ms p95 | ms max |", "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for (kind, method), s in rows:
        ms = s["ms"]
        out.append(f"| {kind} | `{method}` | {s['n']} | {s['n'] / seconds:.2f} | {s['bytes']:,} | {s['bytes'] // max(1, s['n']):,} | "
                   f"{percentile(ms, .5):.1f} | {percentile(ms, .95):.1f} | {max(ms, default=0):.1f} |" if ms else
                   f"| {kind} | `{method}` | {s['n']} | {s['n'] / seconds:.2f} | {s['bytes']:,} | {s['bytes'] // max(1, s['n']):,} | | | |")
    text = "\n".join(out)
    print("\n" + text)
    SECTIONS.append(text)
    return {"stats": stats, "totals": totals, "seconds": seconds}


def count(result: dict, kind: str, method: str) -> int:
    return result["stats"].get((kind, method), {"n": 0})["n"]


# ------------------------------------------------------------------ driving Neovim
def attach(nvim, path):
    from test_navigation import attached
    nvim.command(f"edit {path}")
    wait_until(lambda: attached(nvim) == 1, message=f"{path} never attached")


def settle(probe, path, seconds=8):
    wait_until(lambda: any(m["uri"] == uri(path) for m in probe.debug_state()["mirrors"]), message="never mirrored")
    time.sleep(seconds)                                                 # the first analysis of a file is not the steady state


def function_lines(nvim, limit=12):
    picks = [(i, l) for i, l in enumerate(nvim.current.buffer[:]) if l.lstrip().startswith("fun ")]
    step = max(1, len(picks) // limit)
    out = []
    for i, line in picks[::step][:limit]:
        out.append({"line": i, "character": line.index("fun ") + 5})
    return out


def type_slowly(nvim, text: str, per_char: float):
    for ch in text:
        nvim.input(ch)
        time.sleep(per_char)


@pytest.fixture(scope="module", autouse=True)
def write_report():
    yield
    if SECTIONS:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text("# Bridge benchmark\n\n" + "\n\n".join(SECTIONS) + "\n")
        print(f"\nwritten to {REPORT}")


# ------------------------------------------------------------------ the scenarios
class TestScenarios:

    def test_1_idle(self, nvim, probe, bridge_container):
        """A file open and nothing happening: what the Bridge says when nobody is doing anything."""
        attach(nvim, LARGE)
        settle(probe, LARGE)
        with Window(bridge_container, "1. Idle: a file open, nobody doing anything, 45 s") as w:
            time.sleep(45)
        r = report(w, "Anything here is the Bridge talking to itself: analysis finishing, refreshes, status.")
        assert r["totals"]["recv"][0] < 50

    def test_2_typing(self, nvim, probe, bridge_container):
        """A line of Kotlin typed at 15 characters a second, in insert mode, with completion on."""
        attach(nvim, LARGE)
        settle(probe, LARGE)
        line = "fun typedByBench(a: Int, b: String): Int { val c = a + b.length; return c * 2 }"
        nvim.command("normal! G")
        nvim.input("o")
        time.sleep(0.5)
        with Window(bridge_container, f"2. Typing {len(line)} characters at 15 per second") as w:
            type_slowly(nvim, line, 0.066)
            time.sleep(3)                                              # the analysis after the last key
        nvim.input("<Esc>")
        r = report(w, f"{len(line)} keystrokes: divide by that for the cost of one.")
        keys = len(line)
        SECTIONS.append(
            f"Per keystroke: {count(r, 'recv', 'textDocument/didChange') / keys:.2f} didChange, "
            f"{count(r, 'recv', '$/ij/completion') / keys:.2f} completion requests, "
            f"{count(r, 'send', 'textDocument/publishDiagnostics') / keys:.2f} publishDiagnostics, "
            f"{r['totals']['recv'][1] / keys:,.0f} B to the Brain, {(r['totals']['send'][1] + r['totals']['reply'][1]) / keys:,.0f} B back.")
        print(SECTIONS[-1])
        nvim.command("edit!")

    def test_3_reading_code(self, nvim, probe, bridge_container):
        """Hover, definition, references, symbols, code actions at a dozen places: Brain time against what Neovim saw."""
        attach(nvim, LARGE)
        settle(probe, LARGE)
        places = function_lines(nvim)
        methods = ["textDocument/hover", "textDocument/definition", "textDocument/references",
                   "textDocument/documentSymbol", "textDocument/codeAction", "textDocument/signatureHelp"]
        seen: dict[str, list[float]] = defaultdict(list)
        with Window(bridge_container, f"3. Reading code: {len(methods)} kinds of request at {len(places)} places") as w:
            for place in places:
                for method in methods:
                    ms = nvim.exec_lua("""
                        local method, line, col = ...
                        local buf = vim.api.nvim_get_current_buf()
                        local uri = vim.uri_from_bufnr(buf)
                        local pos = { line = line, character = col }
                        local params = { textDocument = { uri = uri }, position = pos }
                        if method == 'textDocument/references' then params.context = { includeDeclaration = true } end
                        if method == 'textDocument/documentSymbol' then params = { textDocument = { uri = uri } } end
                        if method == 'textDocument/codeAction' then
                          params = { textDocument = { uri = uri }, range = { start = pos, ['end'] = pos }, context = { diagnostics = {} } }
                        end
                        local t = vim.uv.hrtime()
                        vim.lsp.buf_request_sync(buf, method, params, 20000)
                        return (vim.uv.hrtime() - t) / 1e6""", method, place["line"], place["character"])
                    seen[method].append(ms)
        r = report(w)
        lines = ["", "Neovim's round trip against the Brain's own time, per request kind (ms):", "",
                 "| method | Neovim p50 | Neovim p95 | Brain p50 | Brain p95 | Bridge adds (p50) |", "|---|---:|---:|---:|---:|---:|"]
        for method in methods:
            brain = r["stats"].get(("reply", method), {"ms": []})["ms"]
            n50, b50 = percentile(seen[method], .5), percentile(brain, .5)
            lines.append(f"| `{method}` | {n50:.1f} | {percentile(seen[method], .95):.1f} | {b50:.1f} | {percentile(brain, .95):.1f} | {n50 - b50:.1f} |")
        SECTIONS.append("\n".join(lines))
        print("\n".join(lines))

    @pytest.mark.parametrize("follow", [False, True], ids=["follow off", "follow on"])
    def test_4_moving_the_cursor(self, nvim, probe, bridge_container, follow):
        """Holding `j` down the file, and jumping about: what moving costs, with caret following off and on."""
        attach(nvim, LARGE)
        settle(probe, LARGE)
        nvim.command(f"IjBridge follow {'on' if follow else 'off'}")
        nvim.command("normal! gg")
        time.sleep(1)
        with Window(bridge_container, f"4. Moving the cursor 300 lines in 3 s, then 10 jumps (caret following {'on' if follow else 'off'})") as w:
            for _ in range(60):
                nvim.input("5j")
                time.sleep(0.05)
            for line in (40, 500, 120, 700, 10, 333, 90, 611, 250, 5):
                nvim.command(f"normal! {line}G")
                time.sleep(0.4)
            time.sleep(1)
        nvim.command("IjBridge follow off")
        report(w)

    def test_5_saving(self, nvim, probe, project_files, bridge_container):
        """Ten edits, each written: the save handshake."""
        project_files(SCRATCH, SCRATCH_TEXT)
        attach(nvim, SCRATCH)
        settle(probe, SCRATCH, 4)
        with Window(bridge_container, "5. Ten edit-and-save rounds on a small file") as w:
            for i in range(10):
                nvim.current.buffer.append(f"// edit {i}", 4)
                time.sleep(0.3)
                nvim.command("write")
                time.sleep(0.7)
        report(w)

    def test_6_opening_files(self, nvim, probe, bridge_container):
        """Twenty files opened one after another: the cost of attaching, and how fast each is analysed."""
        files = bridge_container.exec(f"ls {SRC}/probe/*.kt {SRC}/*/*.kt", check=False).stdout.split()[:20]
        with Window(bridge_container, f"6. Opening {len(files)} files, one every 1.5 s") as w:
            for f in files:
                nvim.command(f"edit {f}")
                time.sleep(1.5)
        r = report(w)
        opens = count(r, "recv", "textDocument/didOpen")
        if opens:
            SECTIONS.append(f"Per file opened: {r['totals']['recv'][1] / opens:,.0f} B to the Brain (the whole text, once), "
                            f"{count(r, 'send', 'textDocument/publishDiagnostics') / opens:.1f} publishDiagnostics back.")
            print(SECTIONS[-1])

    def test_7_a_test_run(self, nvim, probe, bridge_container):
        """One Gradle test run: how much the output amounts to."""
        from test_navigation import at
        calc = f"{SRC}/probe/CalculatorTest.kt".replace("/main/", "/test/")
        attach(nvim, calc)
        settle(probe, calc, 4)
        text = "\n".join(nvim.current.buffer[:])
        line, col = at(text, "class CalculatorTest", 0, 6)
        nvim.current.window.cursor = (line + 1, col)
        with Window(bridge_container, "7. One Gradle test run of a small class") as w:
            nvim.exec_lua("_G.__done = false; vim.api.nvim_create_autocmd('User', { pattern = 'IjBridgeTestRunFinished', once = true, callback = function() _G.__done = true end })")
            nvim.feedkeys(nvim.replace_termcodes("<Space>tc"), "m", False)
            wait_until(lambda: nvim.exec_lua("return _G.__done"), timeout=120, message="the run never finished")
        report(w)
