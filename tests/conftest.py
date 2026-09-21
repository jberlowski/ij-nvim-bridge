"""Harness fixtures.

The container is session-scoped: building the image is cheap but starting
IntelliJ and letting it index a Gradle project is not, and every test in the
suite wants the same warmed project. Tests that need a cold Brain say so by
asking for `fresh_container`.
"""
from __future__ import annotations

import os
import time
import shutil
from pathlib import Path

import pytest

from harness.brain import Brain
from harness.container import Container
from harness.display import Display
from harness.editor import Editor
from harness.ide import Ide

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = REPO_ROOT / "tests" / "artifacts"
CANARY_ZIP = REPO_ROOT / "canary" / "build" / "distributions" / "canary-0.1.0.zip"
BRAIN_ZIP = REPO_ROOT / "brain" / "build" / "distributions" / "brain-0.1.0.zip"
FIXTURE_PROJECT = "/work/fixture"


def pytest_configure(config):
    if ARTIFACTS.exists():
        shutil.rmtree(ARTIFACTS)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------------ memory
# Each container runs an IntelliJ, its Gradle and Kotlin daemons: 3 to 5 GiB. Docker's VM
# has about 10, so two at once is tight and three is not survivable: the last modules of a
# full run failed for want of memory, passing when run alone. So only one container is ever
# up: modules run grouped by the container they use (below), and a session container is
# stopped as soon as the last test that needs it has run.
LIVE: dict[str, Container] = {}
SESSION_CONTAINERS = ("container", "bridge_container")


def pytest_collection_modifyitems(items):
    """The lifecycle module (its own container), then the sufficiency tests (the canary
    IDE), then everything that shares the Bridge's IDE. Stable: order within a module holds."""
    def group(item):
        name = item.module.__name__.rsplit(".", 1)[-1]
        return {"test_lifecycle": 0, "test_harness_sufficiency": 1}.get(name, 2)
    items.sort(key=group)


@pytest.hookimpl(trylast=True)      # after the test's own fixtures have been torn down
def pytest_runtest_teardown(item, nextitem):
    if os.environ.get("HARNESS_KEEP") == "1":
        return
    remaining = item.session.items[item.session.items.index(item) + 1:]
    for name in SESSION_CONTAINERS:
        if name in LIVE and not any(name in later.fixturenames for later in remaining):
            LIVE.pop(name).stop()


# ------------------------------------------------------------------ container
@pytest.fixture(scope="session")
def container():
    c = Container.start()
    LIVE["container"] = c
    try:
        yield c
    finally:
        if os.environ.get("HARNESS_KEEP") != "1":
            c.stop()


@pytest.fixture
def fresh_container():
    """A container nothing has touched. For reset-semantics assertions."""
    c = Container.start(novnc_port=6081)
    try:
        yield c
    finally:
        c.stop()


@pytest.fixture(scope="session")
def display(container):
    return Display(container)


# ------------------------------------------------------------------ the brain
@pytest.fixture(scope="session")
def canary_zip():
    if not CANARY_ZIP.exists():
        pytest.skip(f"canary plugin not built: {CANARY_ZIP} (run scripts/build-canary.sh)")
    return CANARY_ZIP


@pytest.fixture(scope="session")
def ide(container, canary_zip):
    ide = Ide(container)
    ide.trust()
    ide.install_plugin(canary_zip)
    ide.launch(FIXTURE_PROJECT)
    ide.await_window("fixture", "spring-kotlin-mvc", timeout=300)
    return ide


@pytest.fixture(scope="session")
def brain(container, ide):
    b = Brain(container)
    b.await_registry(timeout=300)
    socks = b.sockets()
    assert socks, "brain published no socket"
    # Not optional: the project window appears while IntelliJ is still importing
    # the Gradle build, and a Brain mid-import answers nothing useful.
    b.await_ready(socks[0], timeout=900)
    b.bridge(socks[0])
    return b


@pytest.fixture(scope="session")
def brain_socket(container, brain):
    socks = brain.sockets()
    assert socks, "brain published no socket"
    return socks[0]


# Probe files added to fixture/ after the image was built. The image bakes the
# fixture in, so copy any that are missing and ask the IDE to pick them up. A
# rebuilt image makes this a no-op.
NEW_PROBES = [
    "src/main/kotlin/dev/bridge/fixture/probe/Shapes.kt",
    "src/main/kotlin/dev/bridge/fixture/probe/ShapeCaller.kt",
    "src/main/kotlin/dev/bridge/fixture/probe/JavaShapes.java",
]


def sync_fixture(container, brain) -> None:
    from harness.util import wait_until
    from harness.wire import Wire
    added = False
    for rel in NEW_PROBES:
        guest = f"{FIXTURE_PROJECT}/{rel}"
        if container.exec(f"test -f {guest}", check=False).returncode != 0:
            container.write_file(guest, (REPO_ROOT / "fixture" / rel).read_text().rstrip("\n"))
            added = True
    if not added:
        return
    with Wire(brain.port) as w:
        w.initialize()
        w.request("$/ij/debug/refresh", {})
        time.sleep(3)                       # let indexing of the new files begin
        # Ready twice running, as await_ready: the state can read Ready between
        # the refresh and the indexing it triggers.
        seen = {"n": 0}

        def ready():
            seen["n"] = seen["n"] + 1 if w.debug_state()["state"] == "Ready" else 0
            return seen["n"] >= 3
        wait_until(ready, timeout=240, interval=1.0, message="the IDE never settled after adding probe files")


# ---------------------------------------------------------------- the Bridge
# The Brain plugin and the canary both publish a Registry and a socket for the
# same Project Root, so they cannot share an IDE. The Bridge gets its own
# container, on its own ports so both can be up in one pytest session.
@pytest.fixture(scope="session")
def bridge_container():
    c = Container.start(novnc_port=6082, nvim_port=7779, brain_port=7880)
    LIVE["bridge_container"] = c
    try:
        yield c
    finally:
        if os.environ.get("HARNESS_KEEP") != "1":
            c.stop()


def start_bridge(container) -> Brain:
    """The Brain plugin, running in an IDE that has finished importing."""
    if not BRAIN_ZIP.exists():
        pytest.skip(f"brain plugin not built: {BRAIN_ZIP} (run make brain)")
    c = container
    ide = Ide(c)
    ide.trust()
    ide.install_plugin(BRAIN_ZIP)
    ide.launch(FIXTURE_PROJECT)
    ide.await_window("fixture", "spring-kotlin-mvc", timeout=300)
    b = Brain(c)
    b.await_registry(timeout=300)
    socks = b.sockets()
    assert socks, "brain published no socket"
    b.await_ready(socks[0], timeout=900, state_method="$/ij/debug/state")
    b.bridge(socks[0])
    b.socket = socks[0]
    b.ide = ide
    sync_fixture(c, b)
    return b


@pytest.fixture(scope="session")
def bridge(bridge_container):
    return start_bridge(bridge_container)


@pytest.fixture
def wire(bridge):
    """A fresh Session, initialised, with every Mirror it opened closed after."""
    from harness.wire import Wire
    w = Wire(bridge.port)
    w.initialize()
    # A modal dialog holds the EDT and every Brain request then times out one by
    # one - a 21-test suite spent ten minutes doing exactly that. Say so once,
    # early, and let the failure hook attach a screenshot (HARNESS.md §13).
    w.timeout = 15
    try:
        w.debug_state()
    except Exception as exc:  # noqa: BLE001
        w.close()
        pytest.fail(f"the Brain is unresponsive - is a dialog holding the EDT? ({exc!r})")
    w.timeout = 30
    try:
        yield w
    finally:
        for m in w.debug_state()["mirrors"]:
            w.notify("textDocument/didClose", {"textDocument": {"uri": m["uri"]}})
        w.request("$/ij/debug/setTabLimit", {"limit": 30})
        w.close()




@pytest.fixture
def project_files(bridge, bridge_container):
    """Create files inside the fixture project for one test, and remove them after.

    The IDE is asked to pick them up and the test waits until it has settled,
    since a new file starts indexing.
    """
    from harness.util import wait_until
    from harness.wire import Wire
    made: list[str] = []

    def settle():
        with Wire(bridge.port) as w:
            w.initialize()
            w.request("$/ij/debug/refresh", {})
            time.sleep(1.5)
            seen = {"n": 0}

            def ready():
                seen["n"] = seen["n"] + 1 if w.debug_state()["state"] == "Ready" else 0
                return seen["n"] >= 3
            wait_until(ready, timeout=120, interval=0.7, message="the IDE never settled")

    def add(path: str, data: bytes | str) -> str:
        bridge_container.write_bytes(path, data.encode() if isinstance(data, str) else data)
        made.append(path)
        settle()
        return path

    def add_many(files: dict) -> None:
        """Several files, and the IDE is left to settle once, not once for each."""
        for path, data in files.items():
            bridge_container.write_bytes(path, data.encode() if isinstance(data, str) else data)
            made.append(path)
        settle()

    add.many = add_many
    yield add
    if made:
        for path in made:
            bridge_container.exec(f"rm -f '{path}'", check=False)
        settle()


@pytest.fixture
def probe(bridge):
    """A passive Session for looking at the Brain while Neovim drives it.

    Unlike `wire` it opens nothing and closes nothing, so it cannot disturb the
    Mirrors Neovim's own Session owns.
    """
    from harness.wire import Wire
    w = Wire(bridge.port)
    w.initialize()
    w.timeout = 15
    try:
        w.debug_state()
    except Exception as exc:  # noqa: BLE001
        w.close()
        pytest.fail(f"the Brain is unresponsive - is a dialog holding the EDT? ({exc!r})")
    try:
        yield w
    finally:
        w.close()


EDITOR_DIR = REPO_ROOT / "editor"
EDITOR_GUEST = "/home/dev/ij-nvim-bridge/editor"


def start_nvim(container, wait_for_blink: bool = True):
    """Neovim (LazyVim, blink.cmp) running the Editor plugin."""
    c = container
    c.copy_in(EDITOR_DIR, EDITOR_GUEST)
    e = Editor(c)
    e.launch(cwd=FIXTURE_PROJECT)
    nv = e.attach(timeout=120)
    # Neovim is listening before lazy.nvim has finished loading its plugins.
    from harness.util import wait_until
    wait_until(lambda: nv.exec_lua("return (pcall(require, 'blink.cmp'))"), timeout=120,
               message="blink.cmp never became loadable")
    nv.exec_lua(f"""
        vim.opt.rtp:prepend('{EDITOR_GUEST}')
        require('ij_bridge').setup()
        local blink = require('blink.cmp')
        blink.add_source_provider('ij_bridge', {{
          module = 'ij_bridge.blink', name = 'IntelliJ', async = true }})
        blink.add_filetype_source('kotlin', 'ij_bridge')
    """)
    if not wait_for_blink:
        # This Neovim never completes anything: it must not fetch blink.cmp's binary either. The
        # download can fail (no network) and its error would land in v:errmsg, which tests assert on.
        nv.exec_lua("""
            local cfg = require('blink.cmp.config')
            cfg.fuzzy.implementation = 'lua'
            cfg.fuzzy.prebuilt_binaries.download = false""")
        return nv
    # blink.cmp's documentation window can raise "Invalid window id" while it is closed by `accept`
    # (`trigger.hide` -> `windows.documentation.close` -> `nvim_win_close` on a float that is already
    # gone), which aborts the accept: nothing is inserted. An upstream race, seen when the menu is
    # closed as a highlighted item's documentation is being shown. Guard the close, so that a test of
    # what the Bridge does is not decided by it.
    nv.exec_lua("""
        local win = require('blink.cmp.lib.window')
        local close = win.close
        win.close = function(self)
          if self.id ~= nil and not vim.api.nvim_win_is_valid(self.id) then
            self.id = nil
          end
          return close(self)
        end""")
    # blink.cmp fetches its fuzzy-matching binary over the network the first time
    # it loads, in every fresh container: it is not baked into the image. A test
    # that types before that finishes sees "the menu never appeared". Load it now
    # (entering insert mode is what triggers it) and wait for the file.
    nv.feedkeys(nv.replace_termcodes("i<Esc>"), "n", False)
    from harness.util import wait_until
    wait_until(
        lambda: c.exec(
            # The library appears before the download is over; the version file is written last.
            "test -f ~/.local/share/nvim/lazy/blink.cmp/target/release/libblink_cmp_fuzzy.so"
            " && test -f ~/.local/share/nvim/lazy/blink.cmp/target/release/version",
            check=False).returncode == 0,
        timeout=240, interval=1.0, message="blink.cmp never finished downloading its binary")
    return nv


@pytest.fixture(scope="session")
def nvim_session(bridge, bridge_container):
    return start_nvim(bridge_container)


# ------------------------------------------------------- the lifecycle container
# IntelliJ is restarted and killed in these tests, which would wreck the shared
# IDE every other test uses (a relaunched IDE loses its imported model). They get
# a container of their own, on their own ports, for the length of one module.
@pytest.fixture(scope="module")
def life_container():
    c = Container.start(novnc_port=6083, nvim_port=7780, brain_port=7881)
    try:
        yield c
    finally:
        if os.environ.get("HARNESS_KEEP") != "1":
            c.stop()


@pytest.fixture(scope="module")
def life(life_container):
    return start_bridge(life_container)


@pytest.fixture(scope="module")
def life_nvim_session(life, life_container):
    return start_nvim(life_container, wait_for_blink=False)


@pytest.fixture(scope="session")
def bridge_display(bridge_container):
    return Display(bridge_container)


@pytest.fixture
def nvim(nvim_session):
    """The same Neovim, with every buffer wiped before and after a test."""
    def wipe():
        nvim_session.command("stopinsert")
        nvim_session.command("silent! %bwipeout!")
        # A test asserts v:errmsg is empty; nothing an earlier test or a fixture
        # provoked may leak into it.
        nvim_session.command("let v:errmsg = ''")
    wipe()
    yield nvim_session
    wipe()


# ----------------------------------------------------------------- the editor
@pytest.fixture(scope="session")
def editor(container):
    e = Editor(container)
    e.launch(cwd=FIXTURE_PROJECT)
    e.attach(timeout=60)
    return e


# ------------------------------------------------------------------ evidence
@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    """Failures ship with a picture (HARNESS.md §4)."""
    outcome = yield
    report = outcome.get_result()
    # Setup and teardown too: a blocked EDT surfaces in the `wire` fixture, which
    # is neither the test body nor something a screenshot of "call" would catch.
    if not report.failed:
        return
    stem = item.name if report.when == "call" else f"{item.name}.{report.when}"
    c = item.funcargs.get("container") or item.funcargs.get("bridge_container")
    if c is None:
        return
    try:
        Display(c).screenshot(ARTIFACTS / f"{stem}.png")
        logs = c.exec("tail -80 /home/dev/.harness/log/*.log 2>/dev/null",
                      check=False).stdout
        (ARTIFACTS / f"{stem}.log").write_text(logs)
    except Exception as exc:  # noqa: BLE001 - evidence is best-effort
        print(f"could not capture evidence: {exc}")
