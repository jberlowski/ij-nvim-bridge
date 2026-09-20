# Mirrors are real IntelliJ editors

A Mirror — the Brain's copy of a buffer the developer has open in Neovim — is a genuine IntelliJ editor opened in preview-tab mode, not a synthetic in-memory document. IntelliJ forces this: `CodeCompletionHandlerBase.invokeCompletion(project, editor, …)` requires an `Editor`, and `DaemonCodeAnalyzer` only produces highlighting for editors it believes are in use. Both of the project's dealbreaker features therefore depend on IntelliJ believing a real editor exists on the file.

## Considered options

A `LightVirtualFile` or other non-physical document was the obvious first choice, and was tentatively adopted early in design: it has no disk association, so it can never conflict with an external write and never pollutes IntelliJ's undo stack. It was abandoned on evidence — neither completion nor the daemon can be driven against it without relying on unproven off-screen behaviour.

## Consequences

The IDE's tab bar changes as the developer navigates in Neovim. This is visible and slightly odd, and is accepted.

Mirrors are bound to real `Document`s and therefore to disk, which resurrects the write-ownership question that a non-physical mirror would have avoided. That is settled separately in [ADR-0003](./0003-editor-owns-disk-writes.md).

IntelliJ's *Editor Tabs limit* (default 30 on 2026.2.3) closes least-recently-used tabs. The Bridge must own Mirror eviction explicitly, or IntelliJ will close Mirrors out from under it.

Comrade almost certainly reached this same constraint, and this is a better explanation of its architecture than the disk-ownership reasoning usually offered for it.
