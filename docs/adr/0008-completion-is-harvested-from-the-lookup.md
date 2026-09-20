# Completion is harvested from IntelliJ's lookup, and requests supersede rather than cancel

The Brain drives `CodeCompletionHandlerBase` on the Mirror's editor and reads the candidates out of the `LookupImpl` it creates, then hides the popup. Spike Q2 and Q3 turned out to be one mechanism: the handler does not return items, it builds a lookup, and the lookup is the only place they exist. The path works on an editor that is not the focus owner and yields real, correctly ranked items (402 for `LargeSurface().compute`, 842 for `LargeSurface().`).

Because this is IntelliJ's own path, ranking, sorting and every Borrowed Setting are exactly what the developer would see — which Passthrough ([ADR-0004](./0004-passthrough-no-polyfills.md)) requires.

## Considered options

Calling `CompletionService.performCompletion` with hand-built `CompletionParameters` would produce items with no lookup and no popup. It was not attempted and is rejected: the parameters are normally assembled by the handler (dummy identifier insertion, offset maps, the file copy completion runs against), so building them ourselves means reimplementing that setup and drifting from it on every IntelliJ release. That is a polyfill in all but name, and it is the "fragile internals" the Spike existed to avoid.

## Consequences

**A popup appears in the IDE window on every request.** Harmless while the IDE is behind the terminal, and it is why the harness's noVNC view is useful. When the IDE window is hidden or minimized on macOS it activated itself during a manual check (`tests/manual/focus_check.py` on branch `spike/q2-q6-probes`); the developer's own setup, IDE on another desktop, was accepted as sufficient evidence, and this stays an open risk.

**`invokeCompletion` blocks the EDT** for about 90% of the time to first items (160 ms of 178 ms warm; 321 of 349 ms with 842 items). Requests therefore serialise on IntelliJ's UI thread, and the synchronous part cannot be interrupted.

So a new keystroke **supersedes** rather than cancels. The Brain keeps at most one request in flight and one pending, newest wins. A request superseded before it starts is dropped without touching IntelliJ. One superseded in flight is allowed to finish, its results are discarded and never emitted, and its lookup is hidden. `$/ij/completionCancel` marks a stream superseded in the same way.

**Time spent queued behind a superseded request is Overhead**, not IJ_TIME: it is the Bridge's choice to serialise, and charging it there lets the gate catch it ([ADR-0005](./0005-gate-on-bridge-overhead-not-latency.md)). IJ_TIME starts when `invokeCompletion` begins. Provisional: revisit against the first vertical slice's measurements.

**Result sets are not deterministic.** The same position returned 841, 842 and 843 items across runs, and IntelliJ caps a prefix match at about 400 items. Tests assert on what must be present, never on exact counts.

**Streaming is real.** A cold run delivered its first items about 3 s before IntelliJ finished calculating, so the Stream of SPEC §6.1 is not a fiction over a single answer.
