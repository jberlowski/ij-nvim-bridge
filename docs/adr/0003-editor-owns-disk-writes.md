# The Editor writes to disk, after a version handshake

Neovim writes files; the Brain never does. Because [ADR-0001](./0001-mirrors-are-real-intellij-editors.md) binds Mirrors to real `Document`s, and a Mirror holding unsaved text is a dirty document diverged from disk, an external write would otherwise trigger IntelliJ's *file changed externally* reconciliation at an unpredictable moment. The Bridge avoids this by establishing Convergence before writing — flush pending changes, await the Brain's acknowledgement of version N, then write — so Mirror and disk hold identical bytes and IntelliJ has nothing to reconcile.

Comrade did the opposite: it routed `:w` into `FileDocumentManager.saveDocument()` and let IntelliJ write. That is the cheaper way to discharge a dirty document, and it earns IntelliJ's on-save actions for free.

## Consequences

`:w` keeps its exact normal meaning and never depends on IntelliJ. When the Brain is absent, Dormant, or slow to acknowledge, the write proceeds as a plain local write. `:w` must never fail or block because of the Bridge — this is the property being bought, and it is worth the round-trip.

Divergence is prevented structurally rather than detected and resolved. There is no conflict-handling code because there is no reachable conflict state.

IntelliJ's on-save actions — reformat on save, optimize imports on save — do not fire. They arrive in v2 as explicit commands, which keeps `:w` a local operation rather than a remote one with invisible side effects.
