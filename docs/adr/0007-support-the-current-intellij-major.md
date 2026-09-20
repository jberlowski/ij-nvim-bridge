# Support the current IntelliJ major, and only that

The Brain plugin supports one major IntelliJ branch at a time — currently 2026.2, build `262` — declared as `sinceBuild 262` / `untilBuild 262.*`. Patch releases within the branch are supported without action. There is no obligation to keep older majors working, and no deprecation cycle when the branch moves.

This is a single-developer tool whose only user upgrades their IDE. Supporting a wider range would be maintenance work paid for by nobody.

## Consequences

**Within a major, nothing needs doing.** Bounding to the branch rather than to a specific build means 2026.2.1 through 2026.2.n require no change. The maintenance event is a new major — roughly three times a year — not each patch.

**The newest APIs in the current branch are fair game.** This matters more than it looks: the design already leans on platform surface the docs do not fully specify — editor lifecycle, completion driving, daemon harvesting (see [ADR-0001](./0001-mirrors-are-real-intellij-editors.md)). Building against one branch rather than the intersection of several removes a constraint from the riskiest part of the project.

**A major upgrade may break the plugin, and that is expected rather than a defect.** The Spike's assumptions about internals get re-tested by the harness against the new branch. That re-testing is a large part of why the harness exists.

**Widening support later is real work.** Once the code freely uses APIs from one branch, supporting N−1 means auditing every one of them. If the project ever acquires users on older IDEs, that audit is the price of this decision.

The harness pins an exact build rather than the branch — see [HARNESS.md §9](../../HARNESS.md). That is a different concern: supported *range* is a policy, while a reproducible *test environment* needs a fixed point, or a rebuilt image could change behaviour and shift the overhead baseline with nothing in the repository recording why.
