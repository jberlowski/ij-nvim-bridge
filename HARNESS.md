# Harness

Vocabulary: [CONTEXT.md](./CONTEXT.md). Design it serves: [SPEC.md](./SPEC.md).

## 1. What this is, and what it is not

The harness is a **Mac-local development loop**. Its only job is to provide an IntelliJ and a Neovim that can talk to each other, resettable to a known state and observable while running, so features can be built and verified in a loop.

**It never runs on the WSL box.** The WSL box is the production target: a licensed IntelliJ displaying through WSLg, and a Neovim, both native, communicating via the Bridge. Portability of the harness to that machine is explicitly not a requirement. See [ADR-0006](./docs/adr/0006-harness-is-a-mac-local-container.md).

The *Unprivileged* constraint in [SPEC.md §3](./SPEC.md) is a property of the Bridge, not of the harness. It exists because Bridge communication must never need elevation on the WSL box — not because the harness must install without root.

## 2. Shape

An OCI container, run via OrbStack.

On macOS a container and a VM both run inside a Linux VM regardless, so the choice is about the *shape of the artifact*, not about virtualisation. The container wins on the two things that matter for a development loop: reset is discarding a container rather than restoring a snapshot, and the image is declarative and layer-cached, so a plugin change rebuilds only what changed.

```
ij-nvim-harness
├── Xvfb :99  +  x11vnc  +  noVNC          → watch at localhost:6080
├── IntelliJ IDEA 2026.2.3 (262.10968.63, free tier) on :99
├── Neovim + LazyVim (pinned) + Bridge plugin
│     └── --listen /run/harness/nvim.sock  ← drivable by RPC
├── JDK + Gradle
└── fixtures/spring-kotlin-mvc
```

Neither Lima nor OrbStack provides a display; both require Xvfb inside or XQuartz on the host. Xvfb inside keeps the harness self-contained and — critically — screenshotable.

## 3. Reset

```
reset:    remove container, run again        (~1s)
rebuild:  cached layers, only what changed
```

Every test starts from an identical state. This is the whole reason for the container: a Brain that has been running for an hour has warmed caches, accumulated undo history, drifted settings and possibly entered a state no fresh instance reaches. Latency figures and diagnostics from such an instance are not comparable between runs.

The Gradle and IntelliJ **index** caches are the exception: baked into the image, not rebuilt per run. Reindexing a Spring project on every test would dominate the runtime and tell us nothing. Tests that need Indexing (SPEC §8) trigger it explicitly rather than relying on a cold start.

## 4. Observability

Three consumers, three needs.

**Live, human.** noVNC at `localhost:6080` shows the actual IntelliJ window. This is what answers "where is my cursor in IntelliJ right now" and "did the completion popup actually appear" — questions no assertion answers well.

**On failure, automated.** Every failing test captures a screenshot of `:99` and both sides' event logs as pytest artifacts. A state mismatch with no picture is expensive to diagnose.

**Never an assertion.** Screenshots are evidence, not verdicts. Golden-image comparison is brittle against fonts, themes, antialiasing and timing, and would produce failures that say nothing useful.

## 5. Driving both sides

Both sides are driven over sockets, from outside, by Python.

```python
# Editor
nvim = pynvim.attach('socket', path='/run/harness/nvim.sock')
nvim.command('edit /fixtures/spring-kotlin-mvc/src/.../GreetingController.kt')
nvim.feedkeys('...')
nvim.funcs.nvim_win_get_cursor(0)

# Brain
brain = JsonRpcClient('$XDG_RUNTIME_DIR/ij-nvim-bridge/<hash>.sock')
brain.request('$/ij/debug/state')
```

Neovim runs **inside a terminal emulator on `:99`**, not headless. It is therefore visible in noVNC and in screenshots, while `--listen` still exposes it for RPC. Headless nvim would run the plugins but render nothing, which forfeits the observability the container exists to provide.

## 6. Assertions

Three mechanisms, each doing what it is actually good at.

### State — deterministic

| Side | Source |
|---|---|
| Editor | `nvim_win_get_cursor`, `nvim_buf_get_lines`, `vim.diagnostic.get` |
| Brain | `$/ij/debug/state` → `{ project, capabilities, state, mirrors[] }` |

Cursor sync is a direct two-way state compare; this is why the debug surface exists (SPEC §10).

### Speed — the gated contract

Both sides emit timestamped events keyed by request id. The harness computes `IJ_TIME` and `OVERHEAD` per SPEC §7.

```
GATE:  p95 OVERHEAD < 15ms  AND  no regression vs baseline
```

Absolute end-to-end latency is recorded and reported every run but **never fails a build** — a cold index or a loaded machine is IntelliJ's business, not the Bridge's.

The baseline is committed and updated deliberately, so a slow drift that stays inside the budget is still visible as a regression.

### Visual — evidence only

See §4.

## 7. Fixture

`fixtures/spring-kotlin-mvc` — a Spring Boot + Kotlin MVC application with an in-memory H2 database.

Chosen because it is a realistic multi-file Gradle project with genuine indexing cost and rich Kotlin resolution, which is what latency benchmarking needs. Its Spring-ness is a bonus rather than the point: on the free tier, Kotlin null-safety inspections and JPA entity resolution are assertable; the `@Autowired` bean graph is not.

The fixture must contain, deliberately:

- a file with a **resolution error** (`cannot resolve symbol`) — proves the daemon harvest, not just inspections
- a file with an **inspection warning** but no error — proves severity mapping
- a **cross-file unsaved-edit scenario** — add a method to a class, call it from another file, without saving (SPEC §5.2)
- a file large enough that completion is not instantaneous — otherwise the overhead gate measures nothing

## 8. Test layout

```
tests/
  conftest.py            container lifecycle, fixture reset, both clients
  test_discovery.py      registry, longest-prefix match, Dormant, stale entries
  test_mirrors.py        Mirror Set, attach/detach, eviction, tab limit
  test_sync.py           didChange fidelity, save handshake, Convergence
  test_completion.py     items, streaming batches, cancel, OVERHEAD gate
  test_diagnostics.py    daemon harvest, severity mapping, coalescing
  test_states.py         Dormant / Indexing / Ready transitions
  test_cursor_sync.py    two-way state compare via debug surface
  baseline/overhead.json committed, deliberately updated
```

Python is deliberately a **third** language. A Kotlin harness could import the Brain's DTOs, and a wrong DTO would then pass its own test — the code and the test sharing one mistake. A neutral driver forces genuine black-box testing of the wire format.

## 9. Version pinning

The image pins an exact IntelliJ build — `262.10968.63` — even though the project targets latest-only ([ADR-0007](./docs/adr/0007-target-latest-intellij-only.md)). Latest-only is a *policy about which version we support*; the harness still needs a *fixed* version, or a rebuilt image could change behaviour with no corresponding change in the repository, and the overhead baseline would drift for reasons nothing recorded.

Bumping it is a deliberate commit. An IntelliJ upgrade breaking the plugin is expected rather than exceptional, and re-running the Spike's assumptions against a new build is precisely what the harness exists to do.

The JDK, Gradle, Neovim and LazyVim versions are pinned for the same reason.

## 10. Tier profiles

```
make harness              free tier — ephemeral, no credentials   ← v1
make harness TIER=licensed                                        ← deferred
```

Only the free profile is built. The licensed profile — mounting a real IDE config to assert deep Spring intelligence — is deferred, and per Passthrough needs no Bridge-side code when it arrives: those Capabilities are either advertised by the connected Brain or they are not.

## 11. Unit tests are elsewhere

The harness is **integration only**. It is slow, it involves two real processes and a real IDE, and it should stay small and high-value.

| Layer | Where |
|---|---|
| Brain logic | JUnit + `CodeInsightTestFixture`, headless, in the Gradle build |
| Editor logic | busted / plenary, inside nvim |
| The Bridge | this harness |

A behaviour testable headlessly in the Gradle build belongs there, not here.

## 12. Open

- **Does IntelliJ behave correctly in a container** — inotify limits for VFS file watchers, memory ceilings, Xvfb quirks. To be answered by the Spike (SPEC §11), which is the harness's first real workload.
- **Image size.** IntelliJ plus a JDK plus a warmed Gradle cache plus a baked index is large. Acceptable if layer caching keeps rebuilds cheap; revisit if it does not.
- **Whether baked indices survive a container reset cleanly**, or whether IntelliJ invalidates them on a new instance id.
- **Terminal emulator choice** for hosting nvim on `:99` — needs to be scriptable, fast, and faithful enough that blink.cmp's popup renders as it would for a real user.
