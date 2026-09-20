# IJ-Nvim Bridge — Specification

Status: **pre-implementation.** Nothing here may be built until the Spike (§10) passes.

Vocabulary is defined in [CONTEXT.md](./CONTEXT.md) and used precisely throughout. Capitalised terms are glossary terms.

---

## 1. Thesis

Neovim should be able to borrow the code intelligence of the IntelliJ that is *already open on your desk*, with the settings you have already configured in it.

This is deliberately not "IntelliJ as a language server". A headless engine — including JetBrains' own, which `intellij-server.nvim` and `nvim-intellij-lsp` wrap — resolves its configuration independently. Ask it to format and you get *its* idea of your code style. Ask the Brain and you get the code style you set in the IDE window you are looking at.

Everything in this document follows from that one distinction. Where a design choice trades fidelity-to-your-IDE against implementation convenience, fidelity wins.

## 2. Non-goals

- **Co-editing.** The Editor is the only surface the developer types into. IntelliJ's window is not an input surface, and text never originates there.
- **Headless operation.** No Brain means Dormant, not a fallback engine.
- **Native Windows.** WSL2 is the target. macOS is a bonus and never a constraint on design.
- **Replicating Comrade.** Where this design diverges, the divergence is recorded in an ADR.
- **Feature parity with any LSP server.** See Passthrough (§3).

## 3. Governing constraints

These are invariants. A design that violates one is wrong, not a trade-off.

**Passthrough.** The Bridge exposes what the connected Brain can do and never reimplements what it lacks. A Capability absent from the connected IntelliJ is absent from the Bridge. No polyfills, no shims, no fallback that imitates a missing feature. See [ADR-0004](./docs/adr/0004-passthrough-no-polyfills.md).

**Unprivileged.** Nothing the Bridge does to communicate may require elevated privileges. No privileged ports, no system-wide paths, no daemon, no install step needing root. The developer's account owns every socket, path and process.

**The Editor owns the bytes.** The Editor's buffer is the Source of Truth. The Mirror follows it. Disk follows it. Nothing else may claim authorship of text.

**Dormant is normal.** Most files the developer opens are not in a Project Root. In that state nvim must behave exactly as if the Bridge were not installed — no errors, no latency, no surprises.

## 4. Architecture

### 4.1 Roles

| | Process | Language | Role |
|---|---|---|---|
| **Brain** | IntelliJ IDEA + Bridge plugin | Kotlin | Listens. Answers. |
| **Editor** | Neovim + Bridge plugin | Lua | Connects. Asks. Owns text. |

The arrow points Editor → Brain. Comrade pointed it the other way because it needed IntelliJ to drive nvim's buffer API; here the Editor drives, so the Brain listens.

### 4.2 Discovery

Each open IntelliJ **project** serves its own unix socket. The Brain publishes a Registry:

```
$XDG_RUNTIME_DIR/ij-nvim-bridge/        ← fallback: $HOME/.ij-nvim-bridge/
  registry.json
  <hash>.sock
```

```json
{
  "version": 1,
  "brains": [
    { "root": "/home/j/work/alpha", "sock": "a3f1c2.sock", "pid": 4411, "ide": "IU-2026.2" }
  ]
}
```

The Editor resolves a buffer by **longest-prefix match** of its absolute path against `root`. No match → Dormant. The Brain is authoritative about its own Project Roots; the Editor never infers them from build files.

`$XDG_RUNTIME_DIR` is unset on WSL without systemd. The fallback path is not optional.

Registry entries are validated by liveness (`pid` alive **and** socket connectable) before use, and stale entries are pruned by whichever side notices first.

### 4.3 Wire protocol

**LSP over JSON-RPC 2.0**, `Content-Length` framed, with `$/ij/*` extensions for what LSP cannot express. Hand-rolled on the Brain side with `kotlinx.serialization` — *not* LSP4J. See [ADR-0002](./docs/adr/0002-lsp-wire-format-hand-rolled-not-lsp4j.md).

Standard methods used in v1:

```
initialize · initialized · shutdown · exit
textDocument/didOpen · didChange · didSave · didClose
textDocument/completion
textDocument/publishDiagnostics
```

Extensions (v1):

```
$/ij/completion          request       streaming completion (§6)
$/ij/completionItems     notification  S→C, subsequent batches
$/ij/completionCancel    notification  C→S, abandon a stream
$/ij/status              notification  S→C, Brain state (§8)
$/ij/caret               notification  S→C, Mirror caret — debug only (§9)
$/ij/debug/state         request       harness introspection (§9)
```

### 4.4 Capability negotiation

The Brain reports Capabilities at `initialize`, derived from what the *connected* IDE can actually do — tier, installed plugins, project type — never inferred from a version string.

```json
{ "capabilities": {
    "completion": { "streaming": true },
    "diagnostics": true,
    "formatting": false,
    "ij": { "ide": "IU-2026.2", "tier": "free", "optimizeImports": true }
} }
```

The Editor registers only the features the Brain advertised. A Capability that is absent is simply not offered to the user — per Passthrough, it is never emulated.

## 5. Documents

### 5.1 Mirrors are real editors

A Mirror is a **genuine IntelliJ editor**, opened in preview-tab mode, not a synthetic document. This is forced by IntelliJ, not chosen for convenience:

- `CodeCompletionHandlerBase.invokeCompletion(project, editor, …)` **requires an `Editor`**.
- Diagnostics are harvested from the daemon's markup model, and `DaemonCodeAnalyzer` only highlights editors IntelliJ believes are in use.

Both dealbreaker features therefore require IntelliJ to believe a real editor exists on the file. See [ADR-0001](./docs/adr/0001-mirrors-are-real-intellij-editors.md).

Consequences accepted: the IDE's tab bar changes as the developer navigates in nvim; Mirrors are bound to real `Document`s and therefore to disk.

### 5.2 Mirror Set

```
Mirror Set = { active buffer } ∪ { every buffer with unsaved changes }
```

The bound is semantic, not a count. Unsaved buffers stay Mirrored because the Brain would otherwise answer from a stale disk copy — the common Java failure being *add a method to `Foo`, switch to `Bar`, get "cannot resolve method"*.

LazyVim sets `autowrite = true` but not `autowriteall`, and `autowrite` does not fire on `:b`, window switches, or Telescope jumps. So `{modified}` is routinely non-empty in exactly the navigation pattern that triggers the failure.

The Bridge owns eviction explicitly. IntelliJ's own *Editor Tabs limit* (default **30** on 2026.2.3, measured in Spike Q6) closes least-recently-used tabs; a Mirror must never be evicted by IntelliJ without the Bridge knowing. Every eviction arrives as a `fileClosed` event, and pinned tabs are exempt — the Bridge pins Mirrors and subscribes to `FileEditorManagerListener`.

### 5.3 Attach / detach

```
BufEnter / TextChanged  → ensure Mirrored:  didOpen  (full text, version 0)
buffer edited           → didChange (incremental)
buffer left AND clean   → didClose, Mirror released
buffer :bd              → didClose
```

`didOpen` carries the buffer's text, which may differ from disk. Per LSP, the Brain must not read disk for an open document.

### 5.4 Saving

The Editor writes to disk. The Brain never does. See [ADR-0003](./docs/adr/0003-editor-owns-disk-writes.md).

```
BufWritePre
  → flush pending didChange            (version N)
  → await Brain ack of version N       (timeout ~200ms → proceed anyway)
  → nvim writes the file
  → didSave { version: N }
  → Brain: Mirror is Convergent with disk → mark clean, no reconcile
```

The handshake exists because a conflict is only possible if Mirror and disk *diverge*. Establishing Convergence before the write makes divergence structurally impossible, which is why `:w` needs no cooperation from IntelliJ's save machinery.

If the Brain is absent, Dormant, or the ack times out, `:w` proceeds as a plain local write. `:w` must never fail or block because of the Bridge.

**Not included:** IntelliJ's on-save actions (reformat-on-save, optimize-imports-on-save) do not fire. They arrive in v2 as explicit commands.

## 6. Completion

The primary feature. Its speed is a dealbreaker (§7).

### 6.1 Streaming

IntelliJ produces completion results progressively. LSP's `textDocument/completion` is strictly one response per request and cannot express that. blink.cmp's source API can:

> "The callback _MUST_ be called at least once. The first time it's called, blink.cmp will show the results in the completion menu. Subsequent calls will append the results to the menu to support streaming results."

So streaming rides on `$/ij/completion`:

```
C→S  $/ij/completion       { textDocument, position, context }
S→C  response              { streamId, items[], isIncomplete, timings }
S→C  $/ij/completionItems  { streamId, items[], done, timings }   ×N
C→S  $/ij/completionCancel { streamId }                            (on new keystroke)
```

**The Brain emits the moment IntelliJ has anything**, then coalesces subsequent batches (~30ms) until IntelliJ finishes or a 300ms cap is reached. There is no artificial first-response deadline: a fast IntelliJ is passed straight through rather than held back by our own clock.

At the cap, the stream closes with `isIncomplete: true`; the next keystroke re-requests.

### 6.2 Fallback door

`textDocument/completion` is implemented as a plain single-shot against the same engine, returning whatever IntelliJ has produced when it is called. nvim-cmp and built-in `vim.lsp.completion` therefore work — without progressive refinement. This costs almost nothing since the engine is shared, and is not a polyfill: it is the same Brain answering a less expressive question.

### 6.3 Harvesting

Drive `CodeCompletionHandlerBase` on the Mirror's editor, read the candidates from the `LookupImpl` it creates, then `hideActiveLookup`. This is the primary path, not a fallback: the handler does not return items, only a lookup does. It runs on an editor that is not the focus owner, and the popup appears in the IntelliJ window, which is harmless while the developer looks at nvim and useful in the harness's noVNC view. See [ADR-0008](./docs/adr/0008-completion-is-harvested-from-the-lookup.md).

Completion must not run inside a write action; it is scheduled via `invokeLater`.

### 6.4 Supersession

`invokeCompletion` blocks the EDT for ~90% of the time to first items, so requests serialise and the synchronous part cannot be cancelled. A newer request for a buffer therefore *supersedes* an older one: at most one request in flight and one pending, newest wins. One dropped before it starts never reaches IntelliJ; one already running finishes, has its results discarded, and has its lookup hidden. `$/ij/completionCancel` is the same operation. Queue time behind a superseded request counts as Overhead.

## 7. Speed contract

The Bridge cannot be faster than IntelliJ. What it controls is the delta it adds. **Overhead is gated; absolute latency is reported and never gated.** See [ADR-0005](./docs/adr/0005-gate-on-bridge-overhead-not-latency.md).

```
t0  Editor sends request
t1  Brain receives
t2  IntelliJ yields first results     ← instrumented inside the plugin
t3  Editor renders

IJ_TIME  = t2 − t1                    reported every run, never gated
OVERHEAD = (t3 − t0) − (t2 − t1)      gated
```

**Gate:** `p95 OVERHEAD < 15ms` **and** no regression against a recorded baseline. *(15 ms remains provisional: the Spike measured `IJ_TIME` — 178–349 ms warm median — but cannot measure Bridge overhead before a Bridge exists. 15 ms is 4–8% of that; revisit at the first Bridge measurement.)*

A slow IntelliJ — cold index, large project, loaded machine — must never fail the build. That number belongs to IntelliJ, not to the Bridge.

## 8. States

| State | Meaning | Behaviour |
|---|---|---|
| **Dormant** | Buffer matches no Project Root | Bridge invisible. nvim entirely normal. |
| **Indexing** | Brain present, rebuilding indices **or importing the build model** | Completion: dumb-aware items only, marked Degraded. Diagnostics: **withheld**. State visible in statusline. |
| **Ready** | Brain answering normally | Full advertised Capabilities. |

Indexing happens several times a day on a real Java project — project open, `git pull`, Gradle sync — and can last minutes. The Gradle import counts: while it is in flight `DumbService` reports not-dumb, yet the daemon returns *Not resolved until the project is fully loaded* for every reference and completion resolves nothing (HARNESS §13). Publishing those would be exactly the stale-diagnostics failure the rule below exists to prevent. It is always visible and never silent. On exit from Indexing the Brain re-publishes diagnostics for every Mirror.

## 9. Diagnostics

Standard `textDocument/publishDiagnostics`, harvested from the daemon's markup model.

Inspections alone are insufficient: *cannot resolve symbol*, syntax errors and type errors come from annotators and highlight visitors, not from `InspectionEngine`. For Java and Kotlin that is the majority of the diagnostic value. The daemon is therefore the source, not the inspection engine.

- **Trigger:** daemon highlighting pass completion.
- **Coalescing:** ~150ms, since the daemon delivers progressively.
- **Granularity:** full-file replacement per publish.
- **Severity:** IntelliJ `HighlightSeverity` → LSP `DiagnosticSeverity`, carrying IntelliJ's inspection id in `code`.
- **Indexing:** withheld entirely. Stale diagnostics are worse than none.

## 10. Debug surface

Exists for the harness and for development. Not a user feature in v1.

```
$/ij/caret        S→C  { uri, position }      Mirror caret moved
$/ij/debug/state  req  → { project, capabilities, state,
                           mirrors: [{ uri, version, caret, convergent }] }
```

Caret sync is *exposed* but not *acted on*: the Editor does not move its cursor in response. Bidirectional caret following is deferred — it is nearly free given real editors, but it is a distraction from the dealbreakers.

## 11. The Spike — gate before implementation

*Prototype code: branch `spike/q2-q6-probes` (`canary/.../Spike.kt`). Throwaway; not on `main`.*

**No implementation begins until this passes.** It exists because the whole design rests on assumptions about IntelliJ internals that the platform docs do not confirm.

| # | Question | Pass | Result |
|---|---|---|---|
| 1 | Can the Brain open a file as a preview-tab editor without stealing focus from the developer? | Editor exists; IDE focus unchanged | ✅ Passed. 5.3 ms warm |
| 2 | Can completion be driven on it and `LookupElement`s harvested? | Real items returned for a fixture file | ✅ **Passed**, on an editor that is *not* the focus owner. `CodeCompletionHandlerBase(BASIC).invokeCompletion` returned 402 items for `LargeSurface().compute` (400 of them `computeMetricNumberNNN`) and 842 for `LargeSurface().`. Harvested from `LookupImpl.items`. See note below |
| 3 | If not — does the lookup-model fallback (§6.3) work? | Items read from active lookup; popup dismissed | ✅ Not needed as a fallback: the handler path *is* the lookup-model path (a `LookupImpl` is created, `isShown == true`). Reading items and `hideActiveLookup` both worked, 20/20 iterations, no lingering popup |
| 4 | Does `DaemonCodeAnalyzer` produce harvestable `HighlightInfo` for it? | *cannot resolve symbol* observed on a broken fixture | ✅ **Passed.** `ERROR` "Unresolved reference 'thisFunctionDoesNotExistAnywhere'." and `'NoSuchTypeInAnyClasspath'` from the markup model, plus daemon-only "never used" warnings. Warm: ~370 ms after `restart(psiFile)`; ~5.3 s when forced from cold |
| 5 | What is IntelliJ's own completion time on the fixture? | Baseline recorded for the overhead gate | 📏 See *Baseline* below |
| 6 | Do Mirrors survive IntelliJ's Editor Tabs limit under the Mirror Set policy? | No Mirror evicted without the Bridge knowing | ✅ **Passed.** 8 files opened at limit 3: all 5 evictions arrived as `fileClosed`. Pinned tabs survived; raising the limit evicts nothing. Default limit is 30, not 10 |

**Note on Q2 vs Q3.** These two turned out to be one mechanism. `invokeCompletion` does not hand back items; it creates a `LookupImpl`, and the items are read from it. A path that yields `LookupElement`s *without* any lookup (`CompletionService.performCompletion` with hand-built `CompletionParameters`) was not attempted and is not needed. Consequence for the design: §6.3's "fallback" is the primary path, and the IDE window will show a popup flicker on every completion.

**Two behaviours the Brain must be designed around.**

- `invokeCompletion` **blocks the EDT** for ~90% of the time-to-first-items (`invokeCall` 160 ms of 178 ms warm). Completions serialise on IntelliJ's UI thread, and a cancelled request cannot interrupt the synchronous part.
- Result sets are **not deterministic across runs** (841–843 items for the same position), and basic completion caps a prefix match at ~400 items.

**Baseline (Q5)**, fixture `spring-kotlin-mvc`, in-IDE nanos, 1 ms poll, aarch64 container, `invokeCompletion` on the EDT. First-items is the moment the lookup holds items; it is an upper bound by at most one poll.

| Scenario | Items | Cold (1st) | Warm median | Warm p95 |
|---|---|---|---|---|
| `LargeSurface().compute` | 402 | 283 ms | **178 ms** | 240 ms |
| `LargeSurface().` | 842 | 422 ms (done 1302 ms) | **349 ms** | 406 ms |

The very first completion after IDE start took **2.4 s** to first items and 5.5 s to finish. Streaming is real: the cold run delivered first items ~3 s before IntelliJ finished calculating.

**Failure of 2 *and* 3 is a genuine no-go** and triggers re-evaluation against `intellij-server.nvim` rather than building on fragile internals.

## 12. Scope

**v1 — the two dealbreakers.** Both carry the architectural risk; both need a real Editor and a running daemon.

- Discovery, Registry, Session, Dormant
- Mirror Set, attach/detach, `didChange`, save handshake
- Streaming completion + blink.cmp source + overhead gate
- Diagnostics from daemon markup
- Indexing state, surfaced
- Capability negotiation
- Debug surface

**v2 — cheap once the spine exists.** Each is one request handler against proven machinery.

- `textDocument/formatting` — *the original motivation*
- `$/ij/optimizeImports`
- `textDocument/hover`, `definition`, `codeAction`
- IntelliJ on-save actions as explicit commands

**Deferred.** Bidirectional caret following · run configurations · refactorings beyond rename · licensed-tier harness profile and deep Spring assertions.

**Never.** Co-editing · headless operation · any polyfill for a missing Capability.

## 13. Stack

| | Choice | Notes |
|---|---|---|
| Brain | Kotlin, Gradle, IntelliJ Platform Gradle Plugin 2.x | |
| Brain wire | hand-rolled JSON-RPC + `kotlinx.serialization` | [ADR-0002](./docs/adr/0002-lsp-wire-format-hand-rolled-not-lsp4j.md) |
| IntelliJ | **Current major — 2026.2** | `sinceBuild 262` / `untilBuild 262.*`. Patches need no action; a new major is the maintenance event. [ADR-0007](./docs/adr/0007-support-the-current-intellij-major.md) |
| Editor | Lua, nvim **0.11+** | `vim.lsp.rpc.connect(path)` handles the unix socket natively |
| Completion UI | blink.cmp (LazyVim default since v14) | nvim-cmp and `vim.lsp.completion` work via the fallback door |
| Harness | Python + pytest, OCI container via OrbStack | [HARNESS.md](./HARNESS.md) · [ADR-0006](./docs/adr/0006-harness-is-a-mac-local-container.md) |

**Tier.** Free tier is fully functional for Java, Kotlin and Gradle. Deep Spring support — bean graph, `@Autowired` resolution, config-key completion — is paid. Per Passthrough this needs no code: those Capabilities are simply advertised or not.

## 14. Open

- **Overhead budget of 15ms p95 is a guess.** Replace with a measured figure once the Spike records a baseline.
- **Coalescing intervals** (30ms completion, 150ms diagnostics) are guesses, to be tuned against the harness.
- **How the Brain detects an import in flight.** The harness reads `idea.log`, which the Brain cannot. Candidate: the external-system processing manager's in-progress `RESOLVE_PROJECT` task. Unverified, and it may need a plugin dependency on the Gradle integration.
- **Multiple nvim instances against one project** — several Sessions on one socket. Expected to work; untested.
