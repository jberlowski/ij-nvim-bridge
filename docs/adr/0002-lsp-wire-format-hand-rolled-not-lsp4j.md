# LSP as the wire format, hand-rolled rather than LSP4J

The Brain speaks LSP over JSON-RPC, extended with `$/ij/*` methods for IntelliJ concepts that LSP has no vocabulary for. LSP was chosen because its document model *is* this architecture: `textDocument/didOpen` and `didChange` already mean "the client owns this buffer, here is its content, do not read disk for it", which is exactly the Editor-owns-the-bytes relationship. Neovim's `vim.lsp.rpc.connect(path)` speaks it over a unix socket with no custom transport code.

The framing and message types are hand-written in Kotlin with `kotlinx.serialization` rather than using LSP4J.

## Considered options

**LSP4J, bundled by the platform.** It is already on the classpath, but as an undeclared internal dependency that drifts — JetBrains bumped it to 1.0.0 in the 263 EAP and broke a shipping plugin. Coupling to it means coupling to an implementation detail that changes without deprecation.

**LSP4J, bundled by us.** LSP4IJ ships its own copy and documents that a plugin embedding a second one produces `ClassCastException` from classloader conflicts. That makes our failure modes depend on which other plugins the user has installed, which is miserable to support.

**A fully bespoke protocol.** Rejected: it would mean reimplementing incremental document sync, position encoding, cancellation and every piece of Neovim completion and diagnostic plumbing that `vim.lsp` provides for free.

## Consequences

LSP framing is a `Content-Length` header and a JSON body, and only the methods actually implemented need DTOs — a short list. We own that code and its bugs.

The streaming completion extension is first-class rather than bolted onto a generated typed API, which matters because streaming is the feature the whole design is organised around.

Any LSP client gets non-streaming completion, diagnostics and formatting for free. That is a side effect, not a goal, and must never constrain a design decision.

`kotlinx.serialization` is **bundled with the plugin**, not taken from the platform. The IDE carries a copy as a library module, but with internal visibility: a third-party plugin that declares a dependency on it is refused at load. The Kotlin stdlib is excluded from the bundle, since the platform supplies it.
