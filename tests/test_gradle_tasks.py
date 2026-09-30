"""Gradle tasks (FEATURES.md §6d): listed from the model IntelliJ has imported, run through its own Gradle
integration, output streamed back, one at a time, cancellable.
"""
from __future__ import annotations

import time

import pytest

from harness.util import wait_until
from harness.wire import RpcError, Wire

ROOT = "/work/fixture"


def listing(w) -> dict:
    """The task hierarchy, asked again while the model is still being imported."""
    deadline = time.monotonic() + 120
    while True:
        result = w.request("$/ij/tasks", {}, timeout=60)
        if result["projects"] or time.monotonic() > deadline:
            return result
        time.sleep(3)


def collect(w, run_id: str, timeout: float = 300):
    """The output and the end of one run, in the order they arrived."""
    output, deadline = [], time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg = w.read(max(1.0, deadline - time.monotonic()))
        if msg.get("method") == "$/ij/task/output" and msg["params"]["runId"] == run_id:
            output.append(msg["params"])
        elif msg.get("method") == "$/ij/task/finished" and msg["params"]["runId"] == run_id:
            return output, msg["params"]
        else:
            w.inbox.append(msg)
    raise AssertionError(f"the task never finished; output so far: {''.join(o['text'] for o in output)[-400:]}")


def run_task(w, tasks, args=None, path=ROOT):
    params = {"path": path, "tasks": tasks}
    if args:
        params["args"] = args
    started = w.request("$/ij/task/run", params, timeout=60)
    return started, *collect(w, started["runId"])


def text_of(output) -> str:
    return "".join(o["text"] for o in output)


class TestListing:

    def test_it_is_advertised(self, bridge):
        with Wire(bridge.port) as w:
            assert w.initialize()["capabilities"]["tasks"] == {"gradle": True}

    def test_the_project_its_groups_and_its_tasks(self, wire):
        (project,) = listing(wire)["projects"]
        assert project["path"] == ROOT and project["name"] == "spring-kotlin-mvc"
        groups = {g["name"]: g for g in project["groups"]}
        assert {"build", "help", "verification"} <= set(groups), sorted(groups)
        help_group = {t["name"]: t for t in groups["help"]["tasks"]}
        assert {"help", "projects", "tasks"} <= set(help_group)
        assert help_group["projects"]["description"].startswith("Displays the sub-projects"), help_group["projects"]

    def test_tasks_with_no_group_are_under_other_which_comes_last(self, wire):
        (project,) = listing(wire)["projects"]
        names = [g["name"] for g in project["groups"]]
        assert names[-1] == "other" and names[:-1] == sorted(names[:-1]), names

    def test_a_project_with_no_subprojects_has_an_empty_list_of_them(self, wire):
        (project,) = listing(wire)["projects"]
        assert project["projects"] == []


class TestSync:

    def test_a_sync_reloads_the_project_and_says_when_it_ended(self, wire):
        started = wire.request("$/ij/sync", {}, timeout=60)
        assert started["paths"] == [ROOT], started
        done = wire.notifications("$/ij/sync/finished", until=lambda p: True, timeout=300)[0]
        assert done["success"] is True, done
        assert listing(wire)["projects"], "the model was empty after a sync"


class TestRunning:

    def test_a_task_runs_and_its_output_streams_back(self, wire):
        started, output, finished = run_task(wire, ["help"])
        assert started["tasks"] == ["help"] and started["path"] == ROOT
        assert "Welcome to Gradle" in text_of(output), text_of(output)[-300:]
        assert finished["success"] is True and finished["cancelled"] is False and finished["ms"] > 0
        assert all(o["stdout"] in (True, False) for o in output)

    def test_the_end_comes_after_all_the_output(self, wire):
        started = wire.request("$/ij/task/run", {"path": ROOT, "tasks": ["help"]}, timeout=60)
        order = []
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            msg = wire.read(5)
            if msg.get("method") in ("$/ij/task/output", "$/ij/task/finished") and msg["params"]["runId"] == started["runId"]:
                order.append(msg["method"])
                if msg["method"] == "$/ij/task/finished":
                    break
        assert order[-1] == "$/ij/task/finished" and order.count("$/ij/task/finished") == 1 and len(order) > 2, order[-5:]

    def test_a_task_that_does_not_exist_fails_and_says_so(self, wire):
        _, output, finished = run_task(wire, ["thisTaskDoesNotExist"])
        assert finished["success"] is False and finished["cancelled"] is False
        assert "thisTaskDoesNotExist" in text_of(output) or "thisTaskDoesNotExist" in finished.get("error", ""), (text_of(output), finished)

    def test_arguments_reach_gradle(self, wire):
        _, output, finished = run_task(wire, ["help"], args=["--task", "projects"])
        assert finished["success"] is True
        assert "Detailed task information for projects" in text_of(output), text_of(output)[-400:]

    def test_a_live_status_streams_too(self, wire):
        """`$/ij/task/status`: what IntelliJ's own Gradle tool window shows as it runs - free on the
        same listener that already carries the output, just not forwarded before now."""
        started = wire.request("$/ij/task/run", {"path": ROOT, "tasks": ["help"]}, timeout=60)
        collect(wire, started["runId"])  # drains output/finished; status went to the inbox instead
        statuses = [m["params"] for m in wire.inbox if m.get("method") == "$/ij/task/status"
                    and m["params"]["runId"] == started["runId"]]
        assert statuses, "no $/ij/task/status notification arrived"
        assert all("runId" in s and "description" in s for s in statuses)

    def test_the_run_is_recorded_in_the_brains_log(self, wire, bridge_container):
        path = wire.request("$/ij/log", {})["path"]
        run_task(wire, ["help"])
        log = bridge_container.read_file(path)
        assert '"ev":"gradle_run"' in log and '"ev":"gradle_finished"' in log

    def test_no_task_and_no_path_are_refused(self, wire):
        for params in ({"path": ROOT}, {"tasks": ["help"]}):
            with pytest.raises(RpcError) as e:
                wire.request("$/ij/task/run", params, timeout=30)
            assert e.value.code == -32602


class TestOneAtATimeAndCancel:

    def test_a_second_run_is_refused_and_the_first_can_be_cancelled(self, wire):
        """`build` compiles the project: long enough to ask for a second run, and to stop it."""
        started = wire.request("$/ij/task/run", {"path": ROOT, "tasks": ["build"]}, timeout=60)
        try:
            with pytest.raises(RpcError) as e:
                wire.request("$/ij/task/run", {"path": ROOT, "tasks": ["help"]}, timeout=30)
            assert e.value.code == -32602 and "already running" in str(e.value)
            assert wire.request("$/ij/tasks", {}, timeout=30)["running"] == started["runId"], "the listing says one is running"

            cancelled = {"cancelled": False}
            deadline = time.monotonic() + 90
            while not cancelled["cancelled"] and time.monotonic() < deadline:     # it may not have started yet
                cancelled = wire.request("$/ij/task/cancel", {"runId": started["runId"]}, timeout=30)
                if not cancelled["cancelled"]:
                    time.sleep(1)
            assert cancelled["cancelled"] is True, "the task could not be stopped"
            _, finished = collect(wire, started["runId"], timeout=180)
            assert finished["cancelled"] is True and finished["success"] is False, finished
        finally:
            wire.request("$/ij/task/cancel", {}, timeout=30)          # never leave a build running for the next test
            wait_until(lambda: "running" not in wire.request("$/ij/tasks", {}, timeout=30), timeout=120, interval=2)

    def test_after_the_end_another_can_run(self, wire):
        _, _, finished = run_task(wire, ["help"])
        assert finished["success"] is True
        assert "running" not in wire.request("$/ij/tasks", {}, timeout=30)

    def test_cancelling_when_nothing_runs_is_harmless(self, wire):
        assert wire.request("$/ij/task/cancel", {}, timeout=30)["cancelled"] is False
