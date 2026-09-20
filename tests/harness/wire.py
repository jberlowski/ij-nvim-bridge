"""A minimal Editor-side LSP client for exercising the Brain over its socket.

This is not the Neovim plugin. It speaks the Bridge protocol (SPEC.md §4.3)
directly so that Brain behaviour can be asserted without Neovim in the loop, and
so timings can be taken with nothing between the test and the socket.

Host mode (through the harness's socat bridge) is fine for functional
assertions. Anything that feeds the speed gate must use `LOCAL_CLIENT`, which
runs inside the container (harness/brain.py explains why).
"""
from __future__ import annotations

import itertools
import socket
import time
from typing import Callable

from .brain import Framing

FIXTURE = "/work/fixture"
SRC = f"{FIXTURE}/src/main/kotlin/dev/bridge/fixture"


def uri(path: str) -> str:
    return f"file://{path}"


class RpcError(Exception):
    def __init__(self, error: dict):
        super().__init__(f"{error.get('code')}: {error.get('message')}")
        self.code = error.get("code")
        self.error = error


class Wire:
    """One Session: one connection, ordered messages."""

    def __init__(self, port: int, timeout: float = 30.0):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.timeout = timeout
        self._ids = itertools.count(1)
        self.inbox: list[dict] = []          # notifications not yet consumed

    def close(self) -> None:
        self.sock.close()

    def __enter__(self) -> "Wire":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ----------------------------------------------------------------- sending
    def notify(self, method: str, params: dict | None = None) -> None:
        self.sock.sendall(Framing.encode(
            {"jsonrpc": "2.0", "method": method, "params": params or {}}))

    def send_request(self, method: str, params: dict | None = None) -> int:
        rid = next(self._ids)
        self.sock.sendall(Framing.encode(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}))
        return rid

    # --------------------------------------------------------------- receiving
    def read(self, timeout: float | None = None) -> dict:
        return Framing.decode(self.sock, timeout or self.timeout)

    def await_response(self, rid: int, timeout: float | None = None) -> dict:
        """Return the reply to `rid`; anything else that arrives goes to `inbox`."""
        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            msg = self.read(max(0.1, deadline - time.monotonic()))
            if msg.get("id") == rid and ("result" in msg or "error" in msg):
                return msg
            self.inbox.append(msg)

    def request(self, method: str, params: dict | None = None,
                timeout: float | None = None):
        reply = self.await_response(self.send_request(method, params), timeout)
        if "error" in reply:
            raise RpcError(reply["error"])
        return reply["result"]

    def notifications(self, method: str, until: Callable[[dict], bool] | None = None,
                      timeout: float | None = None) -> list[dict]:
        """Collect `method` notifications, stopping after the first for which
        `until(params)` is true. Reads the socket only while `until` is unmet."""
        out = [m["params"] for m in self.inbox if m.get("method") == method]
        self.inbox = [m for m in self.inbox if m.get("method") != method]
        deadline = time.monotonic() + (timeout or self.timeout)
        while not (until and out and until(out[-1])) and time.monotonic() < deadline:
            if until is None:
                break
            msg = self.read(max(0.1, deadline - time.monotonic()))
            if msg.get("method") == method:
                out.append(msg["params"])
            else:
                self.inbox.append(msg)
        return out

    # ------------------------------------------------------------------ helpers
    def initialize(self) -> dict:
        result = self.request("initialize", {"processId": None, "rootUri": uri(FIXTURE),
                                             "capabilities": {}})
        self.notify("initialized")
        return result

    def did_open(self, path: str, text: str, version: int = 0) -> None:
        self.notify("textDocument/didOpen", {"textDocument": {
            "uri": uri(path), "languageId": "kotlin", "version": version, "text": text}})

    def did_change(self, path: str, version: int, *changes: dict) -> None:
        self.notify("textDocument/didChange", {
            "textDocument": {"uri": uri(path), "version": version},
            "contentChanges": list(changes)})

    def did_close(self, path: str) -> None:
        self.notify("textDocument/didClose", {"textDocument": {"uri": uri(path)}})

    def debug_state(self, text: bool = False) -> dict:
        return self.request("$/ij/debug/state", {"text": "true" if text else "false"})

    def complete(self, path: str, line: int, character: int,
                 timeout: float | None = None) -> tuple[dict, list[dict]]:
        """A whole Stream: the response plus every $/ij/completionItems batch."""
        first = self.request("$/ij/completion", {
            "textDocument": {"uri": uri(path)},
            "position": {"line": line, "character": character},
            "context": {"triggerKind": 1}}, timeout)
        batches = []
        if not first["done"]:
            batches = self.notifications("$/ij/completionItems",
                                         until=lambda p: p["done"], timeout=timeout)
        return first, batches


def all_items(first: dict, batches: list[dict]) -> list[dict]:
    return first["items"] + [i for b in batches for i in b["items"]]


def replace_range(line: int, start: int, end: int, text: str) -> dict:
    """One incremental LSP content change on a single line."""
    return {"range": {"start": {"line": line, "character": start},
                      "end": {"line": line, "character": end}}, "text": text}


# ------------------------------------------------------------ in-container mode
LOCAL_CLIENT = r'''
import json, socket, sys, time

sc = json.load(open("/tmp/wire_scenario.json"))
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(sc["sock"])
buf = b""

def send(msg):
    body = json.dumps(msg).encode()
    s.sendall(b"Content-Length: %d\r\n\r\n%s" % (len(body), body))

def read():
    global buf
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(65536)
        if not chunk: raise ConnectionError("closed")
        buf += chunk
    head, rest = buf.split(b"\r\n\r\n", 1)
    n = int([l for l in head.decode().split("\r\n") if l.lower().startswith("content-length:")][0].split(":")[1])
    while len(rest) < n:
        chunk = s.recv(65536)
        if not chunk: raise ConnectionError("closed")
        rest += chunk
    buf = rest[n:]
    return json.loads(rest[:n].decode())

def request(rid, method, params):
    send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    while True:
        m = read()
        if m.get("id") == rid: return m

request(1, "initialize", {})
for doc in sc["open"]:
    send({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {"textDocument": {
        "uri": "file://" + doc["path"], "languageId": "kotlin", "version": 0, "text": doc["text"]}}})
request(2, "$/ij/debug/state", {})      # a round trip that proves didOpen was applied

c = sc["complete"]
for i in range(c["repeat"] + c.get("warmup", 0)):
    t0 = time.perf_counter_ns()
    send({"jsonrpc": "2.0", "id": 100 + i, "method": "$/ij/completion", "params": {
        "textDocument": {"uri": "file://" + c["path"]},
        "position": {"line": c["line"], "character": c["character"]}}})
    while True:
        m = read()
        if m.get("id") == 100 + i: break
    t3 = time.perf_counter_ns()
    r = m["result"]
    # drain the rest of the Stream so the next request starts clean
    while not r["done"]:
        n = read()
        if n.get("method") == "$/ij/completionItems" and n["params"]["done"]: break
    if i >= c.get("warmup", 0):
        print(json.dumps({"roundTripNanos": t3 - t0, "items": len(r["items"]),
                          "timings": r["timings"], "done": r["done"]}))
'''


def run_local(container, sock: str, scenario: dict) -> list[dict]:
    """Run a scripted Session from inside the container and return one record
    per measured completion: client-side round trip plus the Brain's timings."""
    import json
    container.write_file("/tmp/wire_scenario.json", json.dumps({"sock": sock, **scenario}))
    container.write_file("/tmp/wire_client.py", LOCAL_CLIENT)
    out = container.exec("python3 /tmp/wire_client.py", timeout=300).stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]
