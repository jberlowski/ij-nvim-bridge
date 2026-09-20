# Target only the latest IntelliJ, with no compatibility range

The Brain plugin supports exactly one IntelliJ version: the current release. `sinceBuild` tracks the latest branch — `262` at time of writing, IntelliJ IDEA 2026.2 — and moves forward with it. There is no supported range, no compatibility shim, and no deprecation cycle.

This is a single-developer tool whose only user upgrades their IDE. Supporting older builds would be maintenance work paid for by nobody.

## Consequences

The newest platform APIs are fair game from day one, which matters because the design already depends on API surface the platform docs do not fully specify — editor lifecycle, completion driving, daemon harvesting (see [ADR-0001](./0001-mirrors-are-real-intellij-editors.md)). Being able to use whatever the current release offers, rather than the intersection of several releases, removes a constraint from the riskiest part of the project.

An IntelliJ upgrade may break the plugin, and that is accepted as normal rather than treated as a defect. The Spike's assumptions about internals are re-tested by the harness on each upgrade; that is what the harness is for.

This is genuinely hard to unpick later. Once the code freely uses APIs from a single recent build, widening support means auditing every one of them. If the project ever acquires users on older IDEs, that cost is the price of this decision.

The harness pins a specific build (`262.10968.63`) so that test results are reproducible, and bumping it is a deliberate commit rather than a silent drift — otherwise a rebuilt image could change behaviour with no corresponding change in the repository.
