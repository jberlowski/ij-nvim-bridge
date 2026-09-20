"""Talking to the Brain's unix socket.

Two transports, deliberately:

*Host mode* bridges the container's unix socket to a TCP port with socat, so
host-side pytest can make functional assertions conveniently.

*In-container mode* runs the request inside the container. This is not a
convenience — it is required for timing. Host mode adds a socat hop and Docker's
userland port forwarding, neither of which is the Bridge, and both of which would
be charged to OVERHEAD (SPEC.md §7). Any measurement that feeds the speed gate
must use in-container mode.
"""
from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass

from .container import Container
from .ide import Ide

REGISTRY_DIR = "/run/user/1000/ij-nvim-bridge"
# The port socat listens on *inside* the container. Container.start publishes
# it on a host port of the caller's choosing (Brain.port), so the two differ
# whenever a second container runs beside the first; using the host number
# inside the container silently bridged nothing.
BRIDGE_TCP_PORT = 7878


class Framing:
    """LSP framing: Content-Length header, blank line, JSON body."""

    @staticmethod
    def encode(payload: dict) -> bytes:
        body = json.dumps(payload).encode("utf-8")
        return b"Content-Length: %d\r\n\r\n%s" % (len(body), body)

    @staticmethod
    def decode(sock: socket.socket, timeout: float = 30.0) -> dict:
        sock.settimeout(timeout)
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = sock.recv(1)
            if not chunk:
                raise ConnectionError("closed while reading header")
            header += chunk
        length = None
        for line in header.decode("ascii").split("\r\n"):
            if line.lower().startswith("content-length:"):
                length = int(line.split(":", 1)[1].strip())
        if length is None:
            raise ValueError(f"no Content-Length in {header!r}")
        body = b""
        while len(body) < length:
            chunk = sock.recv(length - len(body))
            if not chunk:
                raise ConnectionError("closed while reading body")
            body += chunk
        return json.loads(body.decode("utf-8"))


@dataclass
class Registry:
    raw: dict

    @property
    def brains(self) -> list[dict]:
        return self.raw.get("brains", [])

    def socket_for(self, path: str) -> str | None:
        """Longest-prefix match, per SPEC.md §4.2. No match means Dormant."""
        best, best_len = None, -1
        for brain in self.brains:
            root = brain.get("root", "")
            if (path == root or path.startswith(root.rstrip("/") + "/")) and len(root) > best_len:
                best, best_len = brain.get("sock"), len(root)
        return best


class Brain:
    def __init__(self, container: Container, port: int | None = None):
        self.c = container
        self.port = port or container.brain_port
        self._bridged: str | None = None

    # ---------------------------------------------------------------- registry
    def await_registry(self, timeout: float = 300.0) -> Registry:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            out = self.c.exec(f"cat {REGISTRY_DIR}/registry.json 2>/dev/null",
                              check=False).stdout.strip()
            if out:
                try:
                    return Registry(json.loads(out))
                except json.JSONDecodeError:
                    pass          # mid-write; try again
            time.sleep(1.0)
        raise TimeoutError(f"no registry.json after {timeout}s")

    def registry(self) -> Registry:
        return Registry(json.loads(self.c.read_file(f"{REGISTRY_DIR}/registry.json")))

    def sockets(self) -> list[str]:
        return [s for s in self.c.exec(
            f"ls -1 {REGISTRY_DIR}/*.sock 2>/dev/null", check=False
        ).stdout.split() if s]

    # ---------------------------------------------------------------- host mode
    def bridge(self, sock_path: str, timeout: float = 30.0) -> None:
        """Expose a container unix socket on a published TCP port.

        Waits for socat to actually be listening rather than sleeping a guessed
        interval - it needs an unpredictable moment to bind, and a fixed sleep
        produced intermittent ConnectionRefusedError.
        """
        if self._bridged == sock_path:
            return
        self.c.exec("pkill -f 'socat TCP-LISTEN' || true", check=False)
        self.c.exec_detached(
            f"socat TCP-LISTEN:{BRIDGE_TCP_PORT},fork,reuseaddr,bind=0.0.0.0 "
            f"UNIX-CONNECT:{sock_path}",
            log="/home/dev/.harness/log/socat.log",
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # Ask from *inside* the container. A host-side connect proves nothing:
            # Docker's port forwarder accepts TCP before socat has bound, so the
            # first real connection after a "successful" probe was reset.
            listening = self.c.exec(
                f"bash -c 'exec 3<>/dev/tcp/127.0.0.1/{BRIDGE_TCP_PORT}'", check=False
            ).returncode == 0
            if listening:
                self._bridged = sock_path
                return
            time.sleep(0.25)
        raise TimeoutError(f"socat bridge to {sock_path} never listened on :{BRIDGE_TCP_PORT}")

    # ---------------------------------------------------------------- readiness
    def await_ready(self, sock_path: str, timeout: float = 900.0,
                    synced_after: int = 0,
                    state_method: str = "$/canary/state") -> None:
        """Block until the Brain can actually answer.

        A window titled after the project appears long before the project is
        usable: IntelliJ still has to import the Gradle build and then index.
        Not-Indexing alone is not enough either - it is reached while the
        import is still in flight, when diagnostics come back as "Not resolved
        until the project is fully loaded" and completion resolves nothing
        (HARNESS.md §13). The state the Brain reports along the way is recorded in
        `self.trace`, so a test can check it never claimed Ready mid-import.
        Ready therefore means:

          1. a Gradle import has committed its model since `synced_after`
             imports had (idea.log is appended across relaunches), and
          2. not Indexing, twice running (SPEC.md §8), *after* that.
        """
        ide = Ide(self.c)
        deadline = time.monotonic() + timeout
        consecutive = 0
        last = "never answered"
        self.trace = []   # (state the Brain reported, import commits so far)
        while time.monotonic() < deadline:
            try:
                commits = ide.sync_commits()
                state = self.request_local(sock_path, state_method)[0]["result"]
                # The canary reports a JSON boolean `indexing`; the Bridge's
                # debug surface reports SPEC.md §8's state by name.
                idle = (state["indexing"] is False if "indexing" in state
                        else state.get("state") == "Ready")
                self.trace.append(("Ready" if idle else "Indexing", commits))
                last = state.get("indexing", state.get("state", "?"))
                if commits <= synced_after:
                    last, consecutive = "Gradle import not committed", 0
                else:
                    consecutive = consecutive + 1 if idle else 0
                    if consecutive >= 2:
                        return
            except Exception as exc:  # noqa: BLE001 - the IDE may be mid-import
                last = f"{type(exc).__name__}: {exc}"
                consecutive = 0
            time.sleep(3.0)
        raise TimeoutError(f"Brain not ready within {timeout}s (last: {last})")

    def request(self, method: str, params: dict | None = None,
                request_id: str = "1", timeout: float = 30.0) -> dict:
        with socket.create_connection(("127.0.0.1", self.port), timeout=timeout) as s:
            s.sendall(Framing.encode({
                "jsonrpc": "2.0", "id": request_id,
                "method": method, "params": params or {},
            }))
            return Framing.decode(s, timeout)

    # -------------------------------------------------------- in-container mode
    def request_local(self, sock_path: str, method: str,
                      params: dict | None = None, repeat: int = 1) -> list[dict]:
        """Issue requests from inside the container and report wall-clock timings.

        Returns one dict per call with the client-side round trip in nanoseconds
        alongside whatever the Brain reported, so in-IDE time can be separated
        from transport time.
        """
        script = _LOCAL_CLIENT.replace("__SOCK__", sock_path) \
                              .replace("__METHOD__", method) \
                              .replace("__PARAMS__", json.dumps(params or {})) \
                              .replace("__REPEAT__", str(repeat))
        self.c.write_file("/tmp/brain_client.py", script)
        out = self.c.exec("python3 /tmp/brain_client.py", timeout=180).stdout
        return [json.loads(line) for line in out.splitlines() if line.strip()]


_LOCAL_CLIENT = r'''
import json, socket, time

SOCK, METHOD, PARAMS, REPEAT = "__SOCK__", "__METHOD__", __PARAMS__, __REPEAT__

def framed(payload):
    body = json.dumps(payload).encode()
    return b"Content-Length: %d\r\n\r\n%s" % (len(body), body)

def read(sock):
    header = b""
    while b"\r\n\r\n" not in header:
        c = sock.recv(1)
        if not c: raise ConnectionError("closed")
        header += c
    length = int([l for l in header.decode().split("\r\n")
                  if l.lower().startswith("content-length:")][0].split(":")[1])
    body = b""
    while len(body) < length:
        body += sock.recv(length - len(body))
    return json.loads(body.decode())

for i in range(REPEAT):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(SOCK)
    t0 = time.perf_counter_ns()
    s.sendall(framed({"jsonrpc":"2.0","id":str(i),"method":METHOD,"params":PARAMS}))
    resp = read(s)
    t1 = time.perf_counter_ns()
    s.close()
    result = resp.get("result", {})
    print(json.dumps({"roundTripNanos": t1 - t0, "result": result}))
'''
