"""Small polling helpers shared by the suites."""
from __future__ import annotations

import time
from typing import Callable


def wait_until(predicate: Callable[[], object], timeout: float = 10.0,
               interval: float = 0.1, message: str = "condition not met"):
    """Poll `predicate` until it returns something truthy, and return that.

    Neovim and the Brain both work asynchronously (debounced didChange, EDT
    hops), so tests wait for an observable state rather than sleeping a guess.
    """
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except Exception as exc:  # noqa: BLE001 - keep polling, report at the end
            last = exc
        time.sleep(interval)
    raise AssertionError(f"{message} (waited {timeout}s; last: {last!r})")
