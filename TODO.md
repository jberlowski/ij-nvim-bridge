# Bridge workflow fixes

Agreed 2026-11-01. Reproduce reported behavior before fixing; record evidence here.

- [ ] Unused declarations: compare IntelliJ and Neovim for Kotlin and Java classes (including public classes) and variables. Check both diagnostic delivery and grey styling; fix confirmed gaps.
- [x] Gradle picker: put whole-project `build` and `clean build` first with an empty query. While searching, put matching whole-project task options before individual subproject options; hide unrelated entries.
- [x] Offer an all-projects option for shared tasks, including tasks such as `bootJar` and `generateOpenApi`. Run each matching task once; retain individual subproject choices and existing output display.
- [x] Verify the existing Gradle sync command performs IntelliJ's import/reload after build-file and dependency changes, is accessible outside Gradle files, and reports completion/failure.
- [x] Investigate initial project import versus file refresh. Expose a separate refresh-files action if supported and verify external create/change/delete behavior.
- [x] Investigate IntelliJ cache-clearing actions and restart requirements before deciding whether to expose one.
- [x] Add a display-only action to exclude the current Neovim filetype from IntelliJ diagnostics. Persist exclusions across restarts; provide a menu to view/remove them. Markdown (`markdown`, including `.md`) is the motivating case. Hide existing and incoming diagnostics, retain raw diagnostics, and restore them on removal. Other diagnostic sources and bridge features remain available.

## Initial findings

- `:IjBridge sync` already exists; its backend requests Gradle external-system refresh. End-to-end dependency resolution still needs verification.
- The task picker currently lists imported tasks without aggregate choices or priority ordering.
- Diagnostic filtering currently supports severity only; no filetype exclusions exist.
- Existing unused-declaration tests cover Kotlin/Java imports, variables, and some classes; Neovim coverage currently checks unused imports rather than class styling.
- File refresh currently exists only as a debug request. Cache clearing has no editor command.

## Verification and investigation

- Editor regression loop: `XDG_STATE_HOME=/private/tmp/ij-bridge-test-state nvim --headless -u NONE -l tests/workflow.lua`. Reproduced missing build ordering before changes; now passes task ordering, aggregate selection, clean/build request arguments, exclusion persistence, incoming/cached filtering, raw reporting, and restoration.
- Local Brain build against IntelliJ 262.10968.63: `./gradlew buildPlugin -PlocalIdePath="/Users/jarke/Applications/IntelliJ IDEA.app"` passed.
- Cache investigation against the installed IntelliJ 262 jars (`javap`): `InvalidateCachesAction.actionPerformed` calls `InvalidateCacheService.invalidateCachesAndRestart`. That service opens an IntelliJ modal dialog, runs selected invalidators, and restarts the IDE after confirmation. A direct `invalidateCaches(Predicate)` API also exists, but is not equivalent to a completed cache rebuild. No cache command added: retain IntelliJ's own UI pending a separate product decision about restarting and dialog interaction.
- Rebuilt the missing dedicated `ij-nvim-harness:base` image. Integration runs use disposable `ij-nvim-*` containers; `devbox` was not used or changed.

- First IntelliJ run: 14 passed. Kotlin/Java unused imports, variables, private/package-private classes, public classes, unnecessary tags, and Neovim import handling passed. Gradle sync imported a newly registered task into its model.
- Second IntelliJ run: 18 passed. Neovim drew `DiagnosticUnnecessary` marks for Kotlin/Java classes and variables; task picking/running/repeating, external file create/change/delete refresh, unsaved-buffer preservation, and keybinding checks passed.
- Local checks: 2 passed (`tests/test_workflow_local.py`). A real Gradle invocation selected tasks in `:a`, `:b`, and `:a:nested` exactly once, even without that task on the root project. The editor check uses Neovim's actual diagnostic handler and confirms another diagnostic source remains visible.
- Unused-class report remains open: **not reproducible now** with the tested declarations and harness configuration. User will provide evidence and a reproduction when available. No speculative diagnostic change made.
- New commands: `:IjBridge diagnostics exclude`; `:IjBridge diagnostics exclusions` / `<leader>au`; `:IjBridge refresh` / `<leader>aj`. Existing sync remains `:IjBridge sync` / `<leader>cs`.

- Dependency-sync reproduction: `tests/test_gradle_tasks.py::TestSyncModelChanges::test_sync_makes_a_new_dependency_resolve` failed before the fix: sync reported success, but `org.apache.commons.text.WordUtils` stayed unresolved after adding `commons-text:1.12.0` and waiting 120 seconds. IntelliJ's `ImportSpecBuilder.callback(...)` explicitly sets `myImportProjectData=false`; `withCallback(...)` retains model import. Switched to `withCallback`; both model-change tests now pass, including resolving the newly added dependency in a fresh IDE instance.

- Final sync verification: 2 passed (`tests/test_gradle_tasks.py::TestSyncModelChanges`), including the previously failing dependency-resolution test. Brain build and `git diff --check` pass. Across targeted runs, 35 distinct tests passed.
