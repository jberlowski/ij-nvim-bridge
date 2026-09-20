# The Editor writes to disk, after a version handshake

Neovim writes files; the Brain never does. Because [ADR-0001](./0001-mirrors-are-real-intellij-editors.md) binds Mirrors to real `Document`s, and a Mirror holding unsaved text is a dirty document diverged from disk, an external write would otherwise trigger IntelliJ's *file changed externally* reconciliation at an unpredictable moment. The Bridge avoids this by establishing Convergence before writing — flush pending changes, await the Brain's acknowledgement of version N, then write — so Mirror and disk hold identical bytes and IntelliJ has nothing to reconcile.

Comrade did the opposite: it routed `:w` into `FileDocumentManager.saveDocument()` and let IntelliJ write. That is the cheaper way to discharge a dirty document, and it earns IntelliJ's on-save actions for free.

## Consequences

`:w` keeps its exact normal meaning and never depends on IntelliJ. When the Brain is absent, Dormant, or slow to acknowledge, the write proceeds as a plain local write. `:w` must never fail or block because of the Bridge — this is the property being bought, and it is worth the round-trip.

Divergence is prevented structurally rather than detected and resolved. There is no conflict-handling code because there is no reachable conflict state.

IntelliJ's on-save actions — reformat on save, optimize imports on save — do not fire. They arrive in v2 as explicit commands, which keeps `:w` a local operation rather than a remote one with invisible side effects.

## Amendment: Convergence is not enough on its own

The first vertical slice showed that "IntelliJ has nothing to reconcile" does not hold by itself, and that a second, worse hazard sits beside it. The decision above stands — the Editor writes, the Brain never does — but it now depends on the Brain actively keeping IntelliJ's own disk synchronisation away from Mirrors.

- **A modal dialog holds the EDT.** When a file changes on disk under an unsaved document, IntelliJ's `MemoryDiskConflictResolver` records a conflict and, when it next processes conflicts (application activation, a save), raises *"Changes have been made to … in memory and on disk"*. That happened with the Mirror and disk holding identical text. The dialog is modal, so every Brain request that touches the EDT hangs; it presents as a dead Brain, and nobody is looking at the IDE to click it.
- **Autosave writes the Editor's unsaved buffer.** IntelliJ saves unsaved documents when idle and on frame deactivation. A Mirror's document is exactly an unsaved buffer, so the IDE would write text the developer never saved — or explicitly discarded.

The Brain therefore installs a `FileDocumentSynchronizationVetoer` refusing saves and reloads for Mirrors, replaces the conflict resolver so it answers *keep memory* for Mirrors and defers to IntelliJ for everything else, and reloads a document from disk when its Mirror closes, so nothing unsaved outlives the buffer. Each is covered by a test in `test_bridge_slice.py`.

`FileDocumentManagerImpl` and `MemoryDiskConflictResolver` are implementation classes, not API. Expect to revisit them on each IntelliJ major ([ADR-0007](./0007-support-the-current-intellij-major.md)).

Not yet established: whether Neovim's real write pattern (backup-and-rename, by default) behaves like the harness's in-place write. That needs the Editor-side slice.
