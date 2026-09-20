"""Harness fixtures.

The container is session-scoped: building the image is cheap but starting
IntelliJ and letting it index a Gradle project is not, and every test in the
suite wants the same warmed project. Tests that need a cold Brain say so by
asking for `fresh_container`.
"""
from __future__ import annotations

import os
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
FIXTURE_PROJECT = "/work/fixture"


def pytest_configure(config):
    if ARTIFACTS.exists():
        shutil.rmtree(ARTIFACTS)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------------ container
@pytest.fixture(scope="session")
def container():
    c = Container.start()
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
    if report.when != "call" or not report.failed:
        return
    c = item.funcargs.get("container")
    if c is None:
        return
    try:
        Display(c).screenshot(ARTIFACTS / f"{item.name}.png")
        logs = c.exec("tail -80 /home/dev/.harness/log/*.log 2>/dev/null",
                      check=False).stdout
        (ARTIFACTS / f"{item.name}.log").write_text(logs)
    except Exception as exc:  # noqa: BLE001 - evidence is best-effort
        print(f"could not capture evidence: {exc}")
