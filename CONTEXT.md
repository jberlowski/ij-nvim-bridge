# IJ-Nvim Bridge

A bridge that lets Neovim borrow the code intelligence of an already-running, already-configured IntelliJ IDE. The point is not headless analysis — it is that answers come from *your* open IDE, with *your* settings.

## Language

### The two sides

**Editor**:
Neovim. The only surface the developer types into, and the sole source of truth for buffer text.
_Avoid_: client, frontend

**Brain**:
The running IntelliJ IDE instance that answers questions about code. Already open, already configured by the developer.
_Avoid_: server, backend, LSP server, daemon

**Bridge**:
The pair of plugins — one in the Brain, one in the Editor — plus the protocol between them. The whole system, not either half.

### Documents

**Mirror**:
A genuine IntelliJ editor, opened by the Brain, holding the current text of a buffer the developer has open in the Editor. Real rather than synthetic because IntelliJ only analyses and completes against editors it believes a human is using. Always downstream of the Editor; never authoritative.
_Avoid_: shadow, replica, sync'd document, virtual buffer

**Attach**:
Binding an Editor buffer to a Mirror, so that questions about that buffer can be answered. The Brain opens the file; the Editor starts reporting changes.

**Mirror Set**:
The buffers currently Mirrored: the active one, plus every buffer holding unsaved changes. Bounded by what the developer is actually working on rather than by an arbitrary count. Unsaved buffers stay in the set because the Brain would otherwise answer from a stale disk copy.

**Source of Truth**:
The Editor's buffer. Where the bytes are decided. The Mirror follows it; disk follows it.

**Convergent**:
The condition where a Mirror holds exactly the bytes of its buffer at a known version. Required before the Editor writes to disk, because it is what makes a write unable to create a conflict.
_Avoid_: in sync, up to date

### Wiring

**Project Root**:
The directory a Brain instance has open as a project. The Brain is the authority on what its own roots are — the Editor never guesses them.

**Registry**:
The index the Brain publishes mapping each Project Root to the socket serving it. How the Editor discovers which Brain, if any, can answer for a given file.

**Unprivileged**:
The Bridge's standing constraint that nothing it does to communicate may require elevated privileges — no privileged ports, no system-wide locations, no daemon, no installation step needing root. The developer's account owns every socket, path, and process involved.
_Avoid_: rootless, userspace

**Session**:
One Editor connected to one Brain for one project. A single Neovim may hold several, one per project it has files open from.

**Dormant**:
The Bridge's state when a buffer matches no Project Root in the Registry. Neovim behaves exactly as if the Bridge were not installed. The normal, expected state for most files.
_Avoid_: disconnected, offline, failed

**Indexing**:
The Bridge's state while the Brain is rebuilding its indices or importing the project's build model, and cannot answer questions that depend on them. Distinct from Dormant: a Brain is present and will recover. Always visible to the developer, never silent.
_Avoid_: dumb mode, busy, loading

### Answers

**Stream**:
A sequence of progressively better answers to a single question, delivered as the Brain produces them rather than withheld until it finishes. How the Bridge reconciles a slow Brain with a fast Editor.
_Avoid_: partial results, incremental response

**Supersede**:
For a newer question about the same buffer to replace an older one still open. The Brain cannot always interrupt work already begun, so an older question is dropped if it has not started and otherwise finished and discarded, never delivered.
_Avoid_: cancel, abort

**Cap**:
The point at which the Bridge stops waiting for more of a Stream and closes it as Degraded. Not a promise about speed — a bound on how long one question may stay open before the Editor should ask a fresher one.
_Avoid_: deadline, timeout, budget

**Overhead**:
The time the Bridge adds to an answer, over and above what the Brain itself took to produce it. The only part of speed the Bridge controls, and therefore the only part it is held to.
_Avoid_: latency, response time, round-trip

**Degraded**:
An answer known to be incomplete — because a Cap was reached or the Brain was Indexing. Always marked as such, so the Editor knows to ask again.
_Avoid_: partial, stale, best-effort

**Lookup**:
IntelliJ's ranked list of completion candidates for one caret position. The Bridge reads answers out of it whether or not IntelliJ shows it.

**Harvest**:
Reading an answer out of a model IntelliJ maintains for its own purposes — the Lookup for completion, the daemon's markup for diagnostics — rather than out of an interface built to be called. What the Bridge does wherever no such interface exists.
_Avoid_: scrape, poll

### Capabilities

**Borrowed Setting**:
An IDE configuration — code style, inspection profile, scope — that shapes an answer and that the developer has already set in the Brain's UI. The reason for the project's existence: a headless engine would resolve these differently or not at all.

**Extension**:
A request the Bridge carries that standard LSP has no vocabulary for, because it names an IntelliJ concept rather than a general editing one.

**Passthrough**:
The Bridge's governing constraint: it exposes what the connected Brain can do and never reimplements what the Brain lacks. A capability absent from the connected IDE is absent from the Bridge. There are no polyfills, and no fallback that imitates a missing feature.
_Avoid_: fallback, shim, polyfill, degraded implementation

**Capability**:
Something the connected Brain has proven it can answer, established at handshake rather than assumed from a version number. What varies between IntelliJ tiers, installed plugins, and project types.
