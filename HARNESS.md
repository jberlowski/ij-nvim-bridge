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
├── i3                                     → geometry + tiling (§13)
├── Neovim 0.12.5 + LazyVim + blink.cmp    in an xterm on :99
│     └── --listen 0.0.0.0:7777            ← drivable by RPC
├── JDK 21 + Gradle 9.7.1
└── /work/fixture (spring-kotlin-mvc)
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
# Editor — TCP purely as a harness convenience; not Bridge transport
nvim = pynvim.attach('tcp', address='127.0.0.1', port=7777)
nvim.command('edit /work/fixture/src/.../GreetingController.kt')
nvim.funcs.cursor(13, 5)

# Brain — the real unix socket, reached two ways (see harness/brain.py)
brain.request('$/canary/state')                  # host, via socat
brain.request_local(sock, '$/canary/ping', 30)   # in-container, for timing
```

Timing must use the in-container path. Measured on this machine: **0.177 ms** median in-container versus **0.957 ms** through socat plus Docker's port forwarding — a 5.4x difference that would be charged to `OVERHEAD` and is not the Bridge.

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
  conftest.py                  container lifecycle, both clients, evidence
  harness/                     container, display, editor, brain, ide
  test_harness_sufficiency.py  ← exists: proves the harness itself (32 tests)

  # for the Bridge, once the Spike passes:
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

The plugin supports the whole current major branch — `262.*` ([ADR-0007](./docs/adr/0007-support-the-current-intellij-major.md)). The image pins one exact build within it: `262.10968.63`.

These are different concerns. The supported *range* is a policy about which IDEs the Bridge works with. A reproducible *test environment* needs a fixed point, or a rebuilt image could change behaviour with nothing in the repository recording why, and the overhead baseline would shift for unattributable reasons.

Bumping the pin is a deliberate commit. Within the major it should be uneventful; across a major it is the moment the Spike's assumptions about IntelliJ internals get re-tested, which is a large part of why the harness exists.

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

## 12. Running it

```
make image      build the image (IntelliJ, Neovim, LazyVim, fixture)
make canary     build the canary plugin against the pinned IDE
make test       the full sufficiency suite
make test-fast  only the tests that do not start IntelliJ
make harness    leave a container up with IntelliJ on the fixture
make watch      open the live noVNC view
```

### The canary

`canary/` is **not the Bridge**. It is a probe that proves the harness can do what the Spike will need — load a plugin into a running IDE, serve an unprivileged unix socket, publish the Registry, report IDE state, and be timed — without implementing any Bridge behaviour. It is built inside the container against the pinned IDE, so it compiles against the same build the tests run.

It answers three methods: `$/canary/ping`, `$/canary/state`, `$/canary/openEditor`.

### The JetBrains User Agreement

IntelliJ will not start until the agreement is accepted, and **there is no supported non-interactive path**. The only `*NON_INTERACTIVE` flag in the product is `REMOTE_DEV_NON_INTERACTIVE`, which covers shell prompts on the headless remote-dev backend; JetBrains support state plainly that no command-line option exists.

The repository owner accepted it interactively, through noVNC, on 2026-09-20. The image records that acceptance (`eua_accepted_version=2.0`) with provenance in the Dockerfile. It is a record of their acceptance, not a substitute for it — writing the preference directly is the same legal act as ticking the box, so it was not done unilaterally.

`device_id` and `user_id_on_machine` are deliberately **not** baked: they are per-machine identifiers and regenerate per container. Usage statistics are declined.

## 13. Two things that are not optional

Both were discovered the hard way, both looked like IntelliJ API limitations, and both were diagnosed from a screenshot.

### A window manager

Without one, X11 clients get no geometry management at all. IntelliJ's frame rendered as a **~40 pixel sliver**, nothing could be raised or resized, and `FileEditorManager.openTextEditor()` called from a plugin **hung indefinitely**. It looked exactly like a platform restriction on opening editors from a background thread.

i3 runs in the image and also tiles, so the IDE and the terminal sit side by side in the live view instead of one covering the other.

### Suppressing first-run onboarding

The *"Meet the Islands Theme"* tour popup holds the EDT, which produces the same symptom: `openTextEditor()` never returns, with no exception and nothing in the log.

The switch that works is the **Registry** key `ide.experimental.ui.onboarding`, read by `NewUiOnboardingUtil.isOnboardingEnabled`. Note that this is *not* the same as `experimental.ui.onboarding.proposed.version`, which is a PropertiesComponent value that dismissing the dialog happens to write — setting that one does nothing on a first run. The distinction cost an hour.

A corollary worth keeping: **any modal or EDT-holding dialog will look like a Brain that hangs.** The canary answers every request inside a try/catch that returns a JSON-RPC error, so a throwing handler reports rather than stalling — but a blocked EDT still presents as a timeout, and the first diagnostic should be a screenshot.

### "Not Indexing" is not "ready" — and a killed IDE forgets its project

Both found during the Spike; both produced plausible wrong answers.

**Readiness.** `indexing == False` twice running is reached while the Gradle sync is still in flight. Diagnostics run in that window return *"Not resolved until the project is fully loaded"* (INFORMATION) instead of *Unresolved reference* (ERROR). The gate that worked: wait for `Project model for project spring-kotlin-mvc … External system: commit model` in `idea.log`, then a further ~30 s. `Brain.await_ready` now requires it (`Ide.sync_commits`), and `test_ready_means_gradle_synced` fails without it. The marker is keyed to the pinned build; a new IDE major may reword it.

**Relaunching.** `Ide.quit()` followed by `pkill` and a relaunch brings IntelliJ up with **no module model and no source roots** — the project tree shows `fixture` rather than `fixture [spring-kotlin-mvc]`, the editor gutter says `OFF`, the daemon returns zero highlights, and completion resolves nothing. It does not re-sync. Every symptom looks like an IntelliJ or Kotlin limitation. Screenshot first: the project tree gave it away. Use a fresh container per plugin change; do not restart in place.

### `idea.log` is appended, not truncated

Across relaunches in one container, `grep` for a marker line matches the *previous* run. Count before launching and wait for the count to rise.

### The canary's JSON reader dropped escape sequences

`Rpc.stringField` turned `\n` into a literal `n`, so a probe inserted `nval x = …` and completion ran in a spelling/comment context, returning *"Save 'nval' to dictionary"*. Plausible-looking items from the wrong position. Fixed on the `spike/q2-q6-probes` branch; the Bridge uses `kotlinx.serialization` and does not carry this risk.

### A modal dialog is a dead Brain, and the guard is a fixture

Concretely: IntelliJ's *"Changes have been made to … in memory and on disk"* dialog held the EDT and turned a 24-test suite into ten minutes of 30-second timeouts, each looking like a different bug. The `wire` fixture now probes the Brain with a 15-second timeout and fails once, early, with the screenshot; the failure hook captures setup and teardown failures as well as the test body, since that is where a blocked EDT surfaces. Why the dialog appears, and what the Brain does about it: ADR-0003, amendment.

### `Brain.bridge()` lied twice

A host-side `connect()` proves nothing about socat: Docker's port forwarder accepts TCP before socat has bound, so the old readiness probe passed and the first real connection was reset. It now asks from *inside* the container. Separately, socat listened on the **host** port number inside the container, which was invisible while both were 7878 and made a second container, on other ports, bridge nothing. The container-side port is now the constant `BRIDGE_TCP_PORT`.

### The Brain and the canary cannot share an IDE

Both publish a Registry and a socket for the same Project Root. The Bridge tests get their own container (`bridge_container`, other host ports) and the canary suite keeps its own. Build both: `make canary brain`.

## 14. Open

- **Terminal emulator fidelity.** `xterm` is scriptable and works, but it has not been checked whether blink.cmp's completion popup renders there as it would for a real user. If it does not, the visual half of observability is misleading for precisely the feature that matters most.
- **Whether baked Gradle caches survive a reset cleanly**, or whether IntelliJ re-resolves against a new instance id.
- **Mason's `tree-sitter-cli` install fails during the image build.** Harmless so far — treesitter parsers are irrelevant to the Bridge — but the LazyVim install is not pristine.
- **The project name changes after Gradle sync**, from the directory name (`fixture`) to `rootProject.name` (`spring-kotlin-mvc`). Anything keying off the window title must accept both.
