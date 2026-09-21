"""The gold standard (FEATURES.md §11b): the developer's loop, end to end, in real Neovim with
real blink.cmp, against a real IntelliJ in the background.

  1. create a class from a template (the package line comes from the directory);
  2. write a body in it;
  3. create a second file that uses it, then move the class to another package: the file, its
     package line and the other file's import all follow;
  4. in a third, new file, type a variable of that type: the menu offers the class (it is in a
     package nobody imported), and accepting it adds the import;
  5. IntelliJ finds nothing wrong with any of it - shown against a deliberate error, so that
     "nothing wrong" means the checking was alive.

If any link breaks, this fails.
"""
from __future__ import annotations

import json
import time

import pytest

from harness.util import wait_until
from harness.wire import SRC
from test_editor_slice import attached

BASE = f"{SRC}/gold"
PKG = "dev.bridge.fixture.gold"
TICKET_OLD = f"{BASE}/inbox/Ticket.kt"
TICKET_NEW = f"{BASE}/board/Ticket.kt"
CONSUMER = f"{BASE}/app/Consumer.kt"
REPORTER = f"{BASE}/app/Reporter.kt"


def text_of(nvim) -> str:
    return "\n".join(nvim.current.buffer[:])


def errors(nvim, buf=0) -> list:
    return nvim.exec_lua(
        "return vim.tbl_map(function(d) return d.message end, "
        "vim.diagnostic.get(..., { severity = vim.diagnostic.severity.ERROR }))", buf)


def new_file(nvim, directory, name, template="class"):
    path = f"{directory}/{name}.kt"
    nvim.exec_lua(f"require('ij_bridge').new_file({{ dir = '{directory}', name = '{name}', template = '{template}' }})")
    wait_until(lambda: nvim.eval("expand('%:p')") == path and attached(nvim) == 1, timeout=60,
               message=f"{name} was never created and attached")
    return path


def write_buffer(nvim, text: str):
    nvim.current.buffer[:] = text.rstrip("\n").split("\n")
    nvim.command("write")


def settle(probe):
    """A developer does not move a file the instant after making another: the IDE indexes what
    it has just been shown, and answers about it once it has."""
    time.sleep(2)
    seen = {"n": 0}

    def ready():
        seen["n"] = seen["n"] + 1 if probe.debug_state()["state"] == "Ready" else 0
        return seen["n"] >= 4
    wait_until(ready, timeout=120, interval=0.7, message="the IDE never settled")


@pytest.fixture
def workspace(bridge_container):
    bridge_container.exec(f"rm -rf {BASE}", check=False)
    yield
    bridge_container.exec(f"rm -rf {BASE}", check=False)


def test_create_move_use_and_import(nvim, probe, bridge_container, workspace):
    nvim.command(f"edit {SRC}/probe/Shapes.kt")          # somewhere in the project to start from
    wait_until(lambda: attached(nvim) == 1)

    # 1. a new class, with the package its directory implies
    new_file(nvim, f"{BASE}/inbox", "Ticket")
    assert f"package {PKG}.inbox" in text_of(nvim) and "class Ticket" in text_of(nvim), text_of(nvim)

    # 2. a body
    write_buffer(nvim, f"package {PKG}.inbox\n\nclass Ticket(val title: String) {{\n    fun label() = \"T-$title\"\n}}\n")

    # 3. another file that uses it, by an explicit import
    new_file(nvim, f"{BASE}/app", "Consumer")
    write_buffer(nvim, f"package {PKG}.app\n\nimport {PKG}.inbox.Ticket\n\nfun show(t: Ticket): String = t.label()\n")

    settle(probe)

    # ... and the move, as a file-tree plugin does it: ask, apply, move, tell
    nvim.command(f"edit {TICKET_OLD}")
    wait_until(lambda: attached(nvim) == 1)
    answered = nvim.exec_lua(f"""
        local old, new = '{TICKET_OLD}', '{TICKET_NEW}'
        local client = vim.lsp.get_clients({{ name = 'ij-bridge' }})[1]
        local files = {{ {{ oldUri = vim.uri_from_fname(old), newUri = vim.uri_from_fname(new) }} }}
        local answer = client:request_sync('workspace/willRenameFiles', {{ files = files }}, 60000)
        assert(answer and answer.result, vim.inspect(answer))
        vim.lsp.util.apply_workspace_edit(answer.result, client.offset_encoding)
        vim.fn.mkdir(vim.fs.dirname(new), 'p')
        vim.lsp.util.rename(old, new)
        client:notify('workspace/didRenameFiles', {{ files = files }})
        vim.cmd('silent! wall')
        return vim.json.encode(answer.result)
    """)
    assert bridge_container.exec(f"test -f {TICKET_NEW} && echo y || echo n").stdout.strip() == "y"
    assert bridge_container.exec(f"test -f {TICKET_OLD} && echo y || echo n").stdout.strip() == "n"
    assert f"package {PKG}.board" in bridge_container.read_file(TICKET_NEW), "the package line follows the file"
    consumer = bridge_container.read_file(CONSUMER)
    assert f"import {PKG}.board.Ticket" in consumer and f"{PKG}.inbox" not in consumer, (consumer[:200], {u.rsplit('/', 1)[1]: [(e['range']['start']['line'], e['newText']) for e in v] for u, v in json.loads(answered).get('changes', {}).items()})

    settle(probe)

    # 4. a third file, and a variable of a type nobody has imported
    new_file(nvim, f"{BASE}/app", "Reporter")
    write_buffer(nvim, f"package {PKG}.app\n\nclass Reporter {{\n    fun run() {{\n        \n    }}\n}}\n")
    nvim.current.window.cursor = (5, 8)
    nvim.feedkeys(nvim.replace_termcodes("A" + "val t: Tick"), "n", False)

    def ticket_index():
        return nvim.exec_lua("""
            local items = require('blink.cmp.completion.list').items or {}
            for i, item in ipairs(items) do
              local detail = item.labelDetails and item.labelDetails.detail or ''
              if item.label == 'Ticket' and detail:find('gold.board', 1, true) then return i end
            end""")
    wait_until(lambda: nvim.exec_lua("return require('blink.cmp').is_menu_visible()") and ticket_index(),
               timeout=90, message="the menu never offered Ticket from the new package:\n" + "\n".join(nvim.current.buffer[:]))
    # As a person would: look at the menu, then choose.
    settled = {"seen": None, "since": time.monotonic()}

    def still():
        now = nvim.exec_lua("local l = require('blink.cmp.completion.list').items or {}; return #l .. ':' .. (l[1] and l[1].data and l[1].data.id or '')")
        if now != settled["seen"]:
            settled["seen"], settled["since"] = now, time.monotonic()
        return time.monotonic() - settled["since"] > 1.5
    wait_until(still, timeout=60, interval=0.3)
    wait_until(ticket_index, timeout=30)
    nvim.exec_lua(f"require('blink.cmp.completion.list').select({ticket_index()})")
    wait_until(lambda: nvim.exec_lua("""
        for _, r in ipairs(require('ij_bridge.blink').resolves) do
          if r.label == 'Ticket' and r.ms then return true end
        end
        return false"""), timeout=30, message="blink never resolved the highlighted Ticket")
    nvim.exec_lua(f"require('blink.cmp').accept({{ index = {ticket_index()} }})")
    wait_until(lambda: f"import {PKG}.board.Ticket" in text_of(nvim), timeout=30,
               message="accepting Ticket did not add its import:\n" + text_of(nvim))
    nvim.command("stopinsert")
    assert "val t: Ticket" in text_of(nvim)

    # 5. IntelliJ is happy with all of it - and shown to be watching
    nvim.command("silent! wall")
    reporter = nvim.current.buffer.number
    last = max(i for i, line in enumerate(nvim.current.buffer[:]) if line.strip() == "}")
    nvim.current.buffer.append("    val broken: NoSuchType = 1", last)   # a deliberate error, the control
    nvim.command("write")
    try:
        wait_until(lambda: any("NoSuchType" in m for m in errors(nvim, reporter)), timeout=90)
    except AssertionError:
        (m,) = [m for m in probe.debug_state(text=True)["mirrors"] if m["uri"].endswith("Reporter.kt")]
        raise AssertionError(
            "the control error was never reported: the checking may not be alive\n"
            f"diagnostics: {errors(nvim, reporter)}\nbuffer:\n{text_of(nvim)}\n"
            f"mirror (v{m['version']}, convergent={m['convergent']}, showing={m['showing']}):\n{m['text']}") from None
    del nvim.current.buffer[next(i for i, line in enumerate(nvim.current.buffer[:]) if "val broken" in line)]
    nvim.command("write")
    wait_until(lambda: errors(nvim, reporter) == [], timeout=90,
               message=f"Reporter still has errors: {errors(nvim, reporter)}\n{text_of(nvim)}")
    for path in (TICKET_NEW, CONSUMER):
        nvim.command(f"edit {path}")
        wait_until(lambda: attached(nvim) == 1)
        buf = nvim.current.buffer.number
        wait_until(lambda: errors(nvim, buf) == [], timeout=90, message=f"{path}: {errors(nvim, buf)}")
