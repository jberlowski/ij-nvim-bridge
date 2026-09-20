# Passthrough: the Bridge never reimplements what the Brain lacks

The Bridge exposes what the connected IntelliJ can do, and nothing else. A Capability absent from the connected IDE — because of its tier, its installed plugins, or the project type — is absent from the Bridge. There are no polyfills, no shims, and no fallback that imitates a missing feature.

This is the point of the project. Its entire justification over a headless language server is that answers come from *your* IDE with *your* settings; a Bridge that filled gaps with its own implementations would be reintroducing exactly the second, independently-configured engine it exists to avoid.

## Consequences

Capabilities are negotiated at `initialize` from what the connected Brain proves it can do, never inferred from a version string or a tier name. The Editor registers only what was advertised.

No code anywhere branches on IntelliJ tier. The free/paid distinction — deep Spring support, for instance — needs no handling: those Capabilities are either advertised or they are not.

When a user asks why a feature works in IntelliJ but not through the Bridge, the answer is always a missing Capability or a missing implementation of a passthrough, never a degraded local substitute quietly producing different answers.

A non-streaming `textDocument/completion` alongside the streaming extension is not a violation: it is the same Brain answering a less expressive question, not a reimplementation of one.
