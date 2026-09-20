# ij-nvim-bridge

Neovim borrowing the code intelligence of the IntelliJ that is already open on your desk — with the settings you have already configured in it.

**Status: pre-implementation.** Specification only. Nothing is built, and nothing may be built until the Spike passes ([SPEC.md §11](./SPEC.md)).

## Why not an existing option

| | Why not |
|---|---|
| [Comrade](https://github.com/beeender/Comrade) | Unmaintained. Python remote plugin and deoplete on the Neovim side are both dead ends. |
| `intellij-server.nvim`, `nvim-intellij-lsp` | Wrap JetBrains' **headless** language server. It resolves configuration independently, so formatting uses *its* code style, not the one set in your IDE. |
| Kotlin LSP | Pre-alpha. |

The distinction that justifies this project: the Brain is a **running, configured IDE**, not a headless engine. Ask it to format and you get the code style from the window you are looking at.

## Documents

| | |
|---|---|
| [CONTEXT.md](./CONTEXT.md) | Glossary. Terms are used precisely; read this first. |
| [SPEC.md](./SPEC.md) | Feature set, protocol, contracts, v1/v2 split, the Spike. |
| [HARNESS.md](./HARNESS.md) | Development harness: container, observability, assertions. |
| [docs/adr/](./docs/adr/) | Why the design is the way it is — including where it diverges from Comrade. |

## Shape

```
  Neovim                              IntelliJ IDEA
  ┌────────────────┐                  ┌────────────────┐
  │ you type here  │                  │ already open   │
  │ owns the bytes │──── didChange ──▶│ Mirror (a real │
  │                │                  │ editor)        │
  │                │◀── completion ───│ daemon, PSI,   │
  │                │◀── diagnostics ──│ your settings  │
  └────────────────┘                  └────────────────┘
         │              unix socket           │
         └──── LSP + $/ij/* extensions ───────┘

  no match in the registry → Dormant, as if not installed
```

## Decisions of record

- Mirrors are **real IntelliJ editors** — completion needs an `Editor`, and the daemon only highlights editors IntelliJ believes are in use ([ADR-0001](./docs/adr/0001-mirrors-are-real-intellij-editors.md))
- **LSP** as the wire format, hand-rolled rather than LSP4J ([ADR-0002](./docs/adr/0002-lsp-wire-format-hand-rolled-not-lsp4j.md))
- **Neovim writes to disk**, after a version handshake — diverging from Comrade ([ADR-0003](./docs/adr/0003-editor-owns-disk-writes.md))
- **Passthrough**: never reimplement what the connected IDE lacks ([ADR-0004](./docs/adr/0004-passthrough-no-polyfills.md))
- Speed is gated on **Bridge overhead**, never on absolute latency ([ADR-0005](./docs/adr/0005-gate-on-bridge-overhead-not-latency.md))
- The harness is **Mac-local** and never runs on the target box ([ADR-0006](./docs/adr/0006-harness-is-a-mac-local-container.md))
- Support the **current IntelliJ major** only, bounded to the branch ([ADR-0007](./docs/adr/0007-support-the-current-intellij-major.md))

## Target

WSL2 Ubuntu: IntelliJ via WSLg and Neovim, both native, communicating without elevated privileges. macOS is a bonus. Native Windows is not a goal.
