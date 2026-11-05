# One IntelliJ workspace, several builds

Agreed behavior: IntelliJ opens a parent directory (`~/projects`) with independent
Gradle builds A and B already linked/imported. Neovim can start in A and open
files in either build. The bridge does not import builds automatically.

One IntelliJ workspace keeps one Brain/socket. New project commands follow the
current buffer's owning build, using roots reported by IntelliJ, independently
of Neovim's starting directory or later `:cd` changes. An unresolved build must
never silently select a sibling. Gradle subprojects remain in their owning build.

The persistent connection indicator identifies both levels: `IJ: projects / A`,
changing immediately to `IJ: projects / B` when the current buffer changes. A
single-build workspace shows `IJ: A`. Indexing/disconnection remain visible.
Outside a served workspace the indicator says `IJ: dormant`. No switch popups.
`:IjBridge` exposes full paths so equally named directories can be distinguished.

Task/configuration pickers capture the originating connection and build before
opening. Task listing and sync target that build. Run configurations carry stable
IDE identity and their external build path; ambiguous names are refused. Repeat
uses the last operation for the selected build; stop uses the recorded run and
connection. Existing output keeps its originating build label across switches.
The existing one-run-at-a-time limit per runner in an IntelliJ workspace remains.

File features continue to use IntelliJ's real module/classpath and settings:
completion/insertion/cache, diagnostics, navigation, hover/signatures, symbols,
folding/selection, hints, formatting/imports/save, actions/generation/inline,
rename/move/templates, test navigation/generation/markers/execution/results,
caret/foreign edits, Mirror ownership and reconnect. Semantic searches and edits
retain IntelliJ's scope and real dependencies; independent builds must not resolve
identically named declarations from a sibling by accident. Workspace symbols
retain the connected IntelliJ workspace's scope.

## Acceptance tests

Use a dedicated parent workspace with two independently linked builds, one socket
and one IntelliJ project. Start Neovim in A; exercise A → B → A without changing
cwd, then two Neovims. Use identical task/test/class names with different output
and different classpaths. A small checked-in second toy build avoids duplicating
the Spring fixture.

| Family | Required evidence in the parent workspace |
| --- | --- |
| Discovery/display | One Brain, correct owning build, immediate visible switch, full paths, dormant/indexing/disconnected |
| Gradle | Same-name tasks write only selected build marker; picker/tree capture; args/repeat/stop/output; sync only selected build |
| Configurations | Build filtering, IDE identity, same display names, run/repeat/stop/output |
| Completion/diagnostics/navigation | Same FQN in A/B resolves in its module; isolated dependency; unsaved text and positions |
| Symbols/reading | Document/workspace symbols, references/implementation, hover/signatures, folds/selection, library source, hints |
| Editing | Formatting/imports/actions/generate/inline, rename/move/new templates and test skeletons stay in correct module |
| Tests | Same class/method names, correct build runner and result signs/navigation |
| Lifecycle | Dirty A/B buffers, save/close, foreign edit/caret, two Sessions, disconnect/reconnect |

Run the new routing tests against the real IDE and Neovim, plus existing feature
suites for regressions. Record actual passes and gaps; a reviewed feature is not
claimed verified merely because its request includes a URI.

## Existing-feature review

File requests are dispatched through their URI to a Mirror, whose real IntelliJ
module owns PSI resolution and classpath. Keeping one workspace connection does
not replace this module context. Completion insertion and code actions retain the
URI in their resolution data. Save/format-on-save, caret and foreign edits also
carry the file URI. Module-based source roots place generated files and tests.
These paths retain their existing implementations.

The project-level paths needed changes: Gradle listing/sync lacked file context;
configuration lookup used a name alone; Editor run history and cancellation used
the current connection. The implementation now carries the originating context,
filters imported models, uses configuration identity/path, partitions history by
build and cancels by recorded run ID. Test-results source lookup now starts in
the selected build, avoiding same-name files in a sibling.

Workspace symbols and filesystem refresh intentionally cover the connected
IntelliJ workspace. References, implementations, rename and move use IntelliJ's
semantic resolution across its real module relationships. They are not restricted
by a directory-only filter, which would hide legitimate dependencies.

The dedicated suite checks one Brain/two builds, identical task names and FQNs,
configuration selection/execution, unowned-file refusal, rendered status switches,
picker origin after switching, per-build repeat, cancellation after switching,
unsaved navigation positions, rename isolation, symbols/hover/folding/selection,
formatting/templates, scoped sync, real JUnit execution/results, completion and
insertion isolation, and two Neovims with dirty files.

## Validation (2026-11-05)

- `make brain`: passed against the pinned IntelliJ SDK.
- `cd tests && ../.venv/bin/python -m pytest test_unnecessary.py::TestNeverUsed test_workspace_projects.py -q`:
  **25 passed**, including all **22** parent-workspace acceptance tests.
- Local workflow checks: **2 passed**.
- Existing feature regressions were covered across runs. The initial `make test`
  was interrupted after **348 passed / 8 skipped** when stale key-buffer entries
  caused cascading reload failures. Cleanup now drops wiped buffers and tolerates
  a late detach. All key tests passed on the subsequent run.
- The affected/remaining suites plus completion-cache retries ran **295 tests**:
  **294 passed**, with one obsolete unused-declaration assertion rejecting native
  suppression quick fixes. That assertion now permits suppression while retaining
  its removal-feature sentinel; its three cases passed in the final run above.
- One completion-cache assertion failed once in the broad run and passed on retry
  with all eight cache tests; no cache implementation change was made. The eight
  skipped tests are opt-in benchmarks, not routing checks.

This records coverage across runs, not a claim that a single uninterrupted full
suite was green. Existing unsupported capabilities (for example executing plain
JVM Application configurations) retain their documented limitations.
