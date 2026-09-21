# Completion is harvested from IntelliJ's own completion, and requests supersede rather than cancel

The Brain drives `CodeCompletionHandlerBase` on the Mirror's editor and takes the candidates from the lookup IntelliJ builds - not from a popup it shows. It uses a subclass that overrides `completionFinished`, the event IntelliJ raises when completion is done, and reads `indicator.lookup.items` there (the technique of [Comrade](https://github.com/beeender/ComradeNeovim), `completion/CodeCompletionHandler.kt`). While completion is still running, the in-progress lookup gives early batches for streaming. The handler never calls `super`, so no popup is ever shown in the IDE and a lone candidate is never auto-inserted into the Mirror.

Because this is IntelliJ's own path, ranking, sorting and every Borrowed Setting are exactly what the developer would see, which Passthrough ([ADR-0004](./0004-passthrough-no-polyfills.md)) requires.

## Considered options

Calling `CompletionService.performCompletion` with hand-built `CompletionParameters` would produce items with no lookup and no popup. It was not attempted and is rejected: the parameters are normally assembled by the handler (dummy identifier insertion, offset maps, the file copy completion runs against), so building them ourselves means reimplementing that setup and drifting from it on every IntelliJ release. That is a polyfill in all but name, and it is the "fragile internals" the Spike existed to avoid.

## Consequences

**No popup appears in the IDE.** The handler is overridden, not shown. Separately, when the IDE window is hidden or minimized on macOS it activated itself during a manual check (`tests/manual/focus_check.py` on branch `spike/q2-q6-probes`); the developer's own setup, IDE on another desktop, was accepted as sufficient evidence, and this stays an open risk.

**`invokeCompletion` blocks the EDT** for about 90% of the time to first items (160 ms of 178 ms warm; 321 of 349 ms with 842 items). Requests therefore serialise on IntelliJ's UI thread, and the synchronous part cannot be interrupted.

So a new keystroke **supersedes** rather than cancels. The Brain keeps at most one request in flight and one pending, newest wins. A request superseded before it starts is dropped without touching IntelliJ. One superseded in flight is allowed to finish, its results are discarded and never emitted, and its lookup is hidden. `$/ij/completionCancel` marks a stream superseded in the same way.

**Time spent queued behind a superseded request is Overhead**, not IJ_TIME: it is the Bridge's choice to serialise, and charging it there lets the gate catch it ([ADR-0005](./0005-gate-on-bridge-overhead-not-latency.md)). IJ_TIME starts when `invokeCompletion` begins. Provisional: revisit against the first vertical slice's measurements.

**Result sets are not deterministic.** The same position returned 841, 842 and 843 items across runs, and IntelliJ caps a prefix match at about 400 items. Tests assert on what must be present, never on exact counts.

**Streaming is real.** A cold run delivered its first items about 3 s before IntelliJ finished calculating (measured against the shown lookup, before the handler was overridden), so the Stream of SPEC §6.1 is not a fiction over a single answer. Whether the in-progress lookup streams as well while nothing is shown has not been measured separately.

## Amendment: reading the shown lookup fails whenever the IDE is in the background

The first design read the lookup once IntelliJ *showed* it, and that returned nothing while IntelliJ was not the active application: 402 items with the IDE focused, 0 with the terminal running Neovim focused, same buffer, Kotlin and Java alike. The developer is always in Neovim, so this was the Bridge's permanent state. The Spike missed it because its container held no other window, so its Q2 and Q3 passes did not cover it.

IntelliJ gives no results to a lookup it is not showing, and an inactive application shows nothing. It was not blocked from starting, and the editor was showing throughout. Ruled out along the way: editor focus ownership, the write/dumb-mode/viewer guards at the top of `invokeCompletion`, waiting longer for a late lookup, and the non-synchronous and auto-popup handler variants. No application-activity check exists in 797 classes of the completion, lookup and command packages, so the dependence was indirect.

**Resolved by taking the items from `completionFinished` instead**, as Comrade does, which never needs the lookup to be shown. With IntelliJ in the background: 402 items in 0.1-0.3 s warm, and the unsaved-cross-file scenario returns the unsaved method. `test_completion_works_with_the_ide_in_the_background` guards this, and asserts `appActive == false` so it cannot pass in the state that hid the bug.

**A second, smaller hazard, handled by retrying.** IntelliJ sometimes declines to *start* a completion at all - no process, no result, `completionFinished` never raised - most often for the first request after a file is opened; 29 of 30 identical requests succeeded, the failure was the first. The engine gives a running completion all the time it needs, and asks again if nothing is running after 100 ms, up to a stall guard of 10 s. The cause is unknown. It is a workaround; the response's `diag` (`attempts`, `phase`, `hasProcess`) says when it was needed.

## Amendment: a superseded answer is not discarded

The first version threw a superseded in-flight answer away, on the reasoning that it was stale. It is not stale, only for *older text*, and IntelliJ had already done the work: the synchronous part cannot be interrupted, so the newer request waits for it regardless. Discarding it cost the typist twice: `abc.xy` was in flight when `z` was typed, blink.cmp dropped that request and asked for `abc.xyz`, and neither the interim result nor a cache entry for the backspace ever existed. Delivered flagged, it seeds the Editor's cache and is offered to the newer request as an interim result. The rule that a superseded request must not *displace* the newer one stands: only text-extending, same-word answers are ever shown for a newer request.
