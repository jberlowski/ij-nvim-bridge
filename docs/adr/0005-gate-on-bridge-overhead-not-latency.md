# The speed gate measures Bridge overhead, not end-to-end latency

Completion speed is a dealbreaking requirement, so it is enforced by a test rather than hoped for. But the gate measures `OVERHEAD = (t3 − t0) − (t2 − t1)` — round-trip minus IntelliJ's own time, captured by instrumentation inside the plugin — and not absolute latency. The Bridge cannot be faster than IntelliJ; what it controls is the delta it adds.

**Gate:** `p95 OVERHEAD < 15ms` and no regression against a committed baseline. Absolute end-to-end time is reported every run and never fails anything.

## Considered options

An absolute p95 ceiling was the first proposal and is wrong. It would fail the build because the project grew, the index was cold, or the container was loaded — none of which the Bridge causes or can fix. A gate that fires on things outside the system's control gets muted, and then enforces nothing.

## Consequences

The plugin must be instrumented to record when IntelliJ itself yielded results, which is a real requirement on the Brain's internals rather than a harness-only concern.

Overhead is stable across machines, architectures and project sizes in a way absolute latency is not, so the gate means the same thing on an arm64 Mac container and an x86_64 WSL box.

Because the gate is on the delta, the Brain should emit results the moment IntelliJ has any, then coalesce — rather than holding them for a fixed deadline. An artificial first-response deadline would add overhead precisely where it is being measured, and would slow down a fast IntelliJ for no benefit.

The baseline is committed and updated deliberately, so drift that stays inside the budget is still caught.
