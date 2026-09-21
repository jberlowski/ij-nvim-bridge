# IJ-Nvim Bridge - Feature specification

Status: **decisions D1-D4 taken; the navigation core (2), symbols (3) and formatting are built.** [SPEC.md](./SPEC.md) defines the architecture and the v1 spine (Sessions, Mirrors, completion, diagnostics). This document lists everything else that makes the Bridge a *full* language-server experience, in the order it should be built. Vocabulary is [CONTEXT.md](./CONTEXT.md); capitalised terms are glossary terms.

## 1. The principle: it looks like ordinary LSP

From Neovim this is a normal language server. The Session is a standard LSP client over the Brain's socket, so every feature below is a **standard LSP method** answered by the Brain, and Neovim's built-in machinery does the rest: `vim.lsp.buf.definition` (`gd`), `references` (`grr`), `hover` (`K`), `rename`, `code_action`, `format`, `document_symbol`, LazyVim's and Telescope's LSP pickers, the diagnostics float, inlay hints, folding, breadcrumbs. **No custom Neovim UI, keymap or command is needed for any Tier 1-2 feature.**

Two rules make that true:

1. **Advertise only what is proven** (Passthrough, [ADR-0004](./docs/adr/0004-passthrough-no-polyfills.md)). A method is added to `initialize`'s capabilities when the Brain can answer it for the connected IDE, and never before. Neovim then wires up the built-in feature automatically; a capability that is absent is simply not offered, and nothing is emulated.
2. **`$/ij/*` extensions only where LSP has no vocabulary** (§9): IntelliJ concepts like *optimize imports*, *run configuration*, *generate constructor*. These are surfaced through `workspace/executeCommand` and code actions first, so even they appear in the normal code-action menu.

## 2. Cross-cutting requirements

These apply to every feature and are where the real work is.

**Answer from the Mirror, not from disk.** Every request that names a buffer is answered against the Mirror's text at the version the Editor last sent ([SPEC.md §5](./SPEC.md)). A result that *points into another file* must also respect that file's Mirror when it has one: go-to-definition into an unsaved buffer lands on the line as the Editor sees it, not as disk has it. Positions are converted from IntelliJ offsets through the Mirror's document, never through disk.

**Locations that are not files.** Definitions, references and hierarchy results frequently land in library code: a class inside a jar, a decompiled `.class`, a JDK source. LSP locations are URIs, and Neovim can open only what it can read. **Decided (§10, D1):** the Brain extracts the source or decompiled text to a read-only file in a cache directory and returns a `file://` URI, behind one function so a virtual document can replace it later.

**Edits are computed, never applied to the Mirror.** Refactorings, quick fixes, formatting and import optimisation all *modify documents* when run natively in IntelliJ. The Bridge must instead return a `WorkspaceEdit` for Neovim to apply to its buffers, because the Editor owns the bytes ([ADR-0003](./docs/adr/0003-editor-owns-disk-writes.md)). IntelliJ's `ModCommand` API (`ModUpdateFileText`, `ModNavigate`, `ModCompositeCommand`) already expresses actions as data and is the preferred source; refactorings and older intentions that mutate in place need one of the approaches in §10, D2.

**Threading and cancellation.** Read-only features (definition, references, hover, symbols, hierarchies, highlights) can run in a **background read action**, off the EDT - unlike completion, which must block it ([ADR-0008](./docs/adr/0008-completion-is-harvested-from-the-lookup.md)). They must honour `$/cancelRequest` and IntelliJ's own cancellation, and return `ContentModified` (-32801) when the Mirror changed underneath them.

**Indexing.** Anything that depends on indices answers `ContentModified` with a human-readable message, or an empty result marked degraded, while the Brain is Indexing ([SPEC.md §8](./SPEC.md)). Never a stale answer, never silence.

**Position encoding.** UTF-16, as already negotiated. Every new feature converts through one shared helper.

**Latency.** Read-only navigation is interactive: p95 `OVERHEAD` (§7 of SPEC.md) applies to it too, measured the same way. IntelliJ's own time is reported, never gated.

**Multiple Sessions.** Several Neovims may connect to one project. Results are per request; nothing may assume a single Session. (Open in SPEC.md §14; several features make it matter.)

## 3. Tier 1 - the everyday loop

Without these the Bridge is not usable as a daily driver. Diagnostics and completion are v1 and are listed for completeness.

| Feature | LSP method | What the developer sees | IntelliJ source | Notes |
|---|---|---|---|---|
| **Completion** | `$/ij/completion` (streaming), `textDocument/completion` (§6.2 fallback door) | blink.cmp / nvim-cmp menu in IntelliJ's ranking | `CodeCompletionHandlerBase` + `completionFinished` | Built (slice 2). Fallback door not yet. |
| **Completion insertion** | `completionItem/resolve`, `additionalTextEdits`, `textEdit` | Accepting an item adds the import, the parentheses, the lambda braces, exactly as IntelliJ would | `LookupElement.handleInsert` on a throwaway copy of the document, diffed into edits | The largest unbuilt correctness item: plain `insertText` loses IntelliJ's smart insertion. Resolve lazily on selection. |
| **Diagnostics** | `textDocument/publishDiagnostics` | Squiggles, the diagnostics float, `]d`, `<leader>xx` | Daemon markup ([SPEC.md §9](./SPEC.md)) | Slice 3. Severity mapped, inspection id in `code`, `related` for quick-fix hints. |
| **Go to definition** | `textDocument/definition` | `gd` | `GotoDeclarationHandler` / `TargetElementUtil.findTargetElement` | Multiple targets (overloads, expect/actual) return a list. Library targets: §2, D1. |
| **Go to type definition** | `textDocument/typeDefinition` | `gy` | `TypeDeclarationProvider` | |
| **Go to implementation** | `textDocument/implementation` | `gI` | `DefinitionsScopedSearch` / `GotoImplementationHandler` | Interface -> implementers, method -> overrides. Slow on large projects: stream or cap. |
| **Go to declaration** | `textDocument/declaration` | `gD` | as definition, from a usage | |
| **Find references** | `textDocument/references` | `grr`, Telescope/Snacks picker | `ReferencesSearch` over the project scope | Honour `context.includeDeclaration`. Respect IntelliJ's "usage groups"? No: flat list; grouping is the picker's job. |
| **Hover** | `textDocument/hover` | `K` | `DocumentationTarget` (quick documentation) | Render IntelliJ's documentation HTML to Markdown; keep the signature block first. |
| **Signature help** | `textDocument/signatureHelp` | Parameter hints while typing arguments | `ParameterInfoHandler` (`CreateParameterInfoContext`) | Trigger characters `(` and `,`; active parameter from IntelliJ. |
| **Document symbols** | `textDocument/documentSymbol` | Outline, breadcrumbs, `<leader>ss` | `StructureViewBuilder` / file structure | Hierarchical `DocumentSymbol[]`, not the flat form. |
| **Workspace symbols** | `workspace/symbol` (+ `workspaceSymbol/resolve`) | `<leader>sS` symbol picker | Go to Symbol / `ChooseByName` contributors | Must be fast and capped; results ranked by IntelliJ. |
| **Rename** | `textDocument/prepareRename`, `textDocument/rename` | `<leader>cr` with correct scope: overloads, getters/setters, tests, properties files, Spring bean names | `RenameProcessor` usages -> `WorkspaceEdit` | Edits span files, some unsaved. Returns edits, never applies. §10, D2. |
| **Code actions** | `textDocument/codeAction` (+ `codeAction/resolve`) | `<leader>ca`: IntelliJ's quick fixes and intentions | `IntentionManager`, inspection quick fixes, `ModCommandAction` | The single highest-value Tier 1 feature after completion. Kinds: `quickfix`, `refactor.*`, `source.*`. Resolve lazily: computing every edit up front is too slow. |
| **Formatting** | `textDocument/formatting`, `rangeFormatting` | `<leader>cf` using *the IDE's own* code style | `CodeStyleManager` on a copy, diffed into `TextEdit[]` | **The project's original motivation** ([SPEC.md §1](./SPEC.md)). v2 in SPEC.md; promote it. |
| **Organize imports** | code action `source.organizeImports` | `<leader>co` | `OptimizeImportsProcessor` / import optimiser on a copy | Also the on-save hook target (§7). |

## 4. Tier 2 - navigation and reading

Standard LSP, cheap once the Tier 1 read-action plumbing exists. Semantic tokens are deliberately absent: see D4.

| Feature | LSP method | Notes |
|---|---|---|
| **Document highlight** | `textDocument/documentHighlight` | Occurrences of the symbol under the cursor; `HighlightUsagesHandler`. Read/write kinds from IntelliJ. |
| **Call hierarchy** | `textDocument/prepareCallHierarchy`, `callHierarchy/incomingCalls`, `outgoingCalls` | `CallHierarchyProvider`. |
| **Type hierarchy** | `textDocument/prepareTypeHierarchy`, `typeHierarchy/supertypes`, `subtypes` | `TypeHierarchyProvider`. |
| **Folding ranges** | `textDocument/foldingRange` | `FoldingBuilder`. IntelliJ knows Java/Kotlin folds (imports, comments, lambdas) that treesitter does not. |
| **Selection range** | `textDocument/selectionRange` | Expand/shrink selection from the PSI tree (`ExtendWordSelectionHandler`). |
| **Inlay hints** | `textDocument/inlayHint` (+ `inlayHint/resolve`) | Parameter names, inferred types, chained-call types. `InlayHintsProvider`; respects the IDE's own inlay settings (a **Borrowed Setting**). |
| **Code lens** | `textDocument/codeLens` (+ `codeLens/resolve`) | "N usages", "Run test", "implemented by". Only lenses the IDE itself shows. |
| **Document links** | `textDocument/documentLink` | URLs and file references in strings and comments. |
| **Linked editing** | `textDocument/linkedEditingRange` | Deferred: no Java/Kotlin need. |
| **Workspace diagnostics** | `workspace/diagnostic` | The IDE's *Problems* view for the whole project. Expensive; opt-in, and bounded to already-analysed files. |

## 5. Tier 3 - editing assistance

| Feature | LSP method | Notes |
|---|---|---|
| **On-type formatting** | `textDocument/onTypeFormatting` | Trigger characters `}`, `;`, newline. Smart indent, auto-closing of blocks as IntelliJ does it. Must not fight Neovim's own `indentexpr`. |
| **Range formatting** | `textDocument/rangeFormatting` | With Tier 1 formatting. |
| **Format on save** | `willSaveWaitUntil` returning edits | IntelliJ's reformat-on-save and optimise-imports-on-save. Rides the handshake that already exists ([SPEC.md §5.4](./SPEC.md)); the edits are applied by Neovim before it writes. **Opt-in per developer**: it changes what `:w` means. |
| **Snippets / live templates** | completion items with `insertTextFormat: 2` | IntelliJ live templates (`sout`, `psvm`, `fori`) as snippet completions. |
| **Postfix completion** | completion items with `textEdit` replacing the receiver | `.var`, `.if`, `.not`, `.for`. |
| **Generate code** | code actions `source.generate.*` | Constructor, getters/setters, `equals`/`hashCode`, `toString`, override/implement methods. `Generate` action group -> edits. |
| **Extract / inline / move** | code actions `refactor.extract`, `refactor.inline`, `refactor.move` | Extract method/variable/constant/parameter/interface, inline, introduce parameter object. Interactive in the IDE; the Bridge takes IntelliJ's default choices and returns edits, or surfaces the choices through `window/showMessageRequest`. |
| **New file from template** | `workspace/executeCommand` `ij.newFile` (an extension), or `workspace/willCreateFiles` | New class, interface, enum, data class, record from IntelliJ's file templates, **with the right `package` line**: the package comes from the directory, as IntelliJ derives it. Asked for by the developer as wanted soon. Plain LSP has no "create with template" request, so the command is the superset piece; `willCreateFiles` can additionally fill in a file the developer creates by hand (an empty new `.kt` or `.java` gets its package line and class skeleton). |
| **Move file / update package** | `workspace/willRenameFiles` (moves and renames) | Moving or renaming a file in the tree (oil.nvim, neo-tree and snacks all send it) returns a `WorkspaceEdit`: the `package` line rewritten, and every import and reference to the moved class updated, as IntelliJ's Move refactoring does it. This one is standard LSP. Also wanted: "package does not match the directory" as a code action that fixes the package line, and the reverse (move the file to match its package). Needs D2's data-first refactoring; must be tested for correctness on a real move, see the gold standard in §12. |
| **Change signature** | `$/ij/changeSignature` | Needs a structured UI; likely extension-only. |
| **Safe delete** | code action `refactor.delete` | Usages check first; returns edits, or a message listing blocking usages. |
| **Surround with / unwrap** | code actions `refactor.rewrite` | try/catch, if, for, lambda. |

## 6. Tier 4 - project and IDE features (extensions)

LSP has no vocabulary for these; they are `workspace/executeCommand` commands and `$/ij/*` requests, and where possible they also appear as code actions or code lenses so they need no bespoke UI.

| Feature | Surface | Notes |
|---|---|---|
| **Run / debug configurations** | `$/ij/runConfigurations`, `workspace/executeCommand` | Run the test at the cursor, the class, the application. Output streams as `window/logMessage` or a terminal buffer. Deferred in SPEC.md. |
| **Build** | `$/ij/build`, `$/ij/gradleTask` | Compile the project or a module and report problems as diagnostics. |
| **Gradle / Maven sync** | `$/ij/reimport` | Also the trigger for Indexing state ([SPEC.md §8](./SPEC.md)). |
| **Test navigation** | `$/ij/gotoTest`, code lens | Toggle between a class and its test. |
| **Find in project** | `$/ij/search` | IDE-scoped text search honouring the project's excluded roots and scopes. Scopes are a **Borrowed Setting**. |
| **VCS-aware features** | out of scope | Neovim has better tooling. Not a goal. |
| **Debugger** | out of scope | DAP is a different protocol and a different project. |
| **Database, HTTP client, UML** | out of scope | Not language intelligence. |

## 6c. To do (asked for, not started)

Requested and written down with how it would be built; the order is the developer's to set.

**Tests: go to the test, go to the code under test, run the test, and see what can be run in the sign column.** Asked for together, and they share one thing to build (finding what is a test):

- **Go to test / go to the class under test** (`$/ij/testTargets`; a key under `<leader>t`). IntelliJ's own Go to Test (Ctrl+Shift+T): from a class, its test(s); from a test, the class it tests; and, when there is none, offer to create one from the test template. Language-agnostic in the platform (`TestFinderHelper.findTestsForClass` / `findClassesForTest`), answered as `Location[]` like Go to Implementation, so it is cheap and should come first. From a test method, ideally the method under test where the naming convention gives it.
- **What can be run, shown in the sign column.** The IDE already knows which lines have a run icon in its gutter: the daemon's line markers on the Mirror's editor carry `RunLineMarkerContributor` infos (JUnit 4 and 5, Kotlin tests, `main`), each with its Run and Debug actions. The Brain harvests them, as it does diagnostics and inlay hints, so they are exactly the IDE's own and follow its rules and its settings. Published as a `$/ij/runnables` notification per file (line, kind, title) or as a code lens; the Editor places `▶` in the sign column with extmark `sign_text`, and changes it to a pass or fail sign once a result is known. A code lens is the standard surface, but Neovim draws lenses as virtual text, not in the sign column, so the signs are Editor-side (an extension).
- **Run the test** (nearest, this class, this file, and repeat the last; keys under `<leader>t`). The Run action of the marker is performed for the position, which creates IntelliJ's own (temporary) run configuration, so Gradle versus IntelliJ runner, JVM arguments and the working directory are the project's own settings (Borrowed Settings). Output streams back as `$/ij/run/output` notifications to a terminal-like buffer or the quickfix list; the test tree (`SMTRunnerEventsListener`) gives per-test pass and fail, which become the signs, and failures with their stack traces become diagnostics or quickfix entries with locations. A stop request cancels the process.
- **Depends on, and replaces the first half of, "Running IntelliJ tasks and Gradle tasks" in §6b**: running a test is running a run configuration. Debugging a test from the same sign stays a DAP project (§6b).

**Several Neovims and several IntelliJs: built and tested** (`tests/test_multi.py`, and one case in `test_lifecycle.py`):

1. **One Neovim, two IntelliJ projects: the right one.** A buffer under each project's root attaches to that project's Brain, never the other's. The Registry lists one Brain per open project; a second `idea <root>` opens the project in the already-running IDE process, so two windows are two Brains in one JVM. The Editor keeps one Session per root, a buffer is bound to the one whose root contains it, and the status line follows the buffer. Tested: two Sessions with the two roots, a class that exists in only one project resolving only there, unsaved text held only by its own project's Brain, and the status line empty outside every project.
2. **Two Neovims, one project, one IDE.** Both attach to the one Brain, each its own Session; each edits its own file without touching the other's; closing one (killed, as a closed terminal does) releases only its Mirrors and the other still works. Two people editing *one file* at once stays unsupported.
3. **Two Neovims, two projects.** Each connects to the right IDE and is answered by it.
4. **Neovim closed and reopened**, alone and beside another: the Brain releases the closed one's claims (its unsaved text was Neovim's and goes with it), the other is unaffected, and the reopened one attaches to the same Brain, over several rounds with two projects.
5. **Starting an IntelliJ for a root nothing serves: a command and a key, never by itself** (`:IjBridge open [dir]`, `<leader>ao`). It runs `idea <root>` (assumed on the `PATH`; `setup{ idea_cmd = ... }` names another) as `sh -c 'nohup idea "$root" >/dev/null 2>&1 &'` with `detach`: the first IntelliJ process holds its terminal, and a later `idea <root>` only asks the running process to open the project, so the Editor never waits on it and it has no Neovim for a parent (tested by its parent process). It then polls the Registry, shows `IJ: starting` on buffers under that root, and attaches every loaded buffer under the root when the Brain appears; it gives up after `open_timeout` seconds (default 300), says so, and never raises. The project root is the nearest directory above the buffer with `.idea`, Gradle settings, `pom.xml`, `build.gradle(.kts)` or `.git`. Refused with a reason: an IDE already serving it ("already serves"), a start already under way, `idea` not on the `PATH`, a file in no project. Tested with the IDE running (a second project opened in a new window) and with **no IDE running at all** (killed; the command starts one, and the buffer's unsaved edit reaches the new Brain).

Two facts about the second project: IntelliJ asks "this window or a new one?" on `idea <another project>` unless its setting says (a developer answers it once; the harness sets it through a debug lever, `$/ij/debug/openInNewWindow`, since writing the setting's file made the IDE treat the config as an existing user's and show a modal promo that froze the Brain), and Gradle renames a freshly imported project after its root project, so projects are told apart by their **root path** (`root` is now in the debug state), not their name.

## 6d. The to-do queue, in the developer's order

Asked for, in the order to be done. Not started unless marked.

1. **Keys by LazyVim section, filtered by connection and filetype** (built, tested): see §9. `fN` is the one section key so far; Gradle tasks and the test keys will join it.
2. **`*.gradle.kts` support, tested** (built, tested: 5j below).
3. **Running Gradle tasks** (asked; not started), replacing the first half of §6b: the tasks **shown as a hierarchy** (project, then group, then task, subprojects nested) and a **quick-select box to find a task by fuzzy matching**: not a substring match, but any subsequence of the name, case-insensitive, so for `findMysteriousTreasure` the input `eas`, `fMT` (its camel-hump initials) and `trea` all find it, ranked with runs and word starts first. The Brain lists the tasks from the Gradle model IntelliJ has already imported (nothing is run to list them), runs a chosen one through IntelliJ's own Gradle integration (the project's Gradle settings, JVM and wrapper), streams its output to a buffer, reports success or failure, can cancel it, and remembers the last for a repeat. Keys under `<leader>c` (code), only where IntelliJ is.
4. **Extract / inline / move refactorings** (from the earlier queue; not started): extract method, variable, constant and parameter, inline, move member.
5. **Tests: go to test, run test, run signs in the sign column** (§6c; not started).
6. **Other things in the plans that would be worth doing**, with no order: call and type hierarchy (`callHierarchy`, `typeHierarchy`); code lens from IntelliJ's code vision (usage counts, "implements"); workspace diagnostics (errors across the project, not only the open files); format on save (opt-in) and on-type formatting; postfix completion and live templates; the legacy quick fixes that have no `ModCommand` form ("Create function from usage" is the one most missed), through a scratch copy with the pointers rebound; expanding a multi-choice action (`ModChooseAction`) into one code action per choice; renaming from an override up to the base method; moving a directory (a package rename); `declaration` (`gD`); the debounce of caret events when caret sync is built; baking blink.cmp's binary into the harness image; Gradle/Maven reimport as a command (`$/ij/reimport`); Spring features (a paid tier); debugging through DAP (§6b).

## 6b. Someday / wish list

Not planned, not designed, no priority. Written down so the idea is not lost.

- **Running IntelliJ tasks and Gradle tasks.** List and run the project's Gradle tasks and IntelliJ run configurations from Neovim (build, test, run the application), streaming the output back through standard progress and log notifications and surfacing failures as diagnostics. Sits in Tier 4 (§6, `$/ij/gradleTask`, `$/ij/build`, run configurations) as designed-but-unscheduled; wished for ahead of debugging, since it is the more everyday need.
- **Debugging through DAP.** Drive IntelliJ's debugger from Neovim via the Debug Adapter Protocol (`nvim-dap`): breakpoints, stepping, variables, stack, evaluate. A different protocol and a different adapter, so it is a project of its own rather than a feature of this one; noted as out of scope in §6 and kept here as a wish.

## 7. Editing-lifecycle features

Already in SPEC.md, listed so the feature set reads as one:
Mirror Set attach/detach, incremental `didChange`, the save handshake, `didClose`, Dormant behaviour, Indexing state surfaced to the developer, capability negotiation, debug surface.

Add:

- **`workspace/didChangeWatchedFiles` reversed.** When the developer changes files outside Neovim (a `git checkout`, a generator), the Brain's VFS notices; the Editor is told to reload (`workspace/applyEdit`-free: a `$/ij/fileChanged` notification prompting `:checktime`). Prevents the Mirror and the buffer silently disagreeing.
- **`workspace/willRenameFiles` / `didRenameFiles`.** Renaming a file in Neovim (neo-tree, oil) asks IntelliJ what else changes (package declaration, imports, references) and returns the edits.
- **`window/showMessage`, `window/logMessage`, `$/progress`.** Indexing, long searches and refactorings report progress through standard notifications so Neovim's own progress UI (noice, fidget, lualine) shows them.

## 8. Spring and framework features (paid tier)

Per Passthrough these need **no Bridge code**: each is a `LookupElement`, a reference, an inspection or a code action that the connected IDE already produces. They appear if and only if the IDE has the plugin and licence. The harness's licensed-tier profile ([SPEC.md §12, deferred](./SPEC.md)) is where they get asserted.

Examples, all reached through the standard methods above: bean navigation and `@Autowired` resolution as definition/references; configuration-key completion and navigation in `application.yml`; endpoint search as workspace symbols; JPA entity/repository navigation; Thymeleaf and template references; Spring Security and Data inspections as diagnostics and quick fixes.

## 9. Extensions summary

The `$/ij/*` methods this document introduces, gathered. Each must also be reachable through a standard surface (code action, command, code lens) where one exists.

**Keys.** What is not a standard LSP feature is bound where LazyVim keeps that kind of thing, by its which-key sections (`<leader>c` code, `<leader>f` file/find, `<leader>g` git, `<leader>s` search, `<leader>t` test, `<leader>u` ui, `<leader>x` diagnostics; plain `g` is goto), read from LazyVim's own source and from the running LazyVim in the harness: **`<leader>fN`** new file from an IntelliJ template (LazyVim's `fn` is its New File), and, to come, Gradle tasks under `<leader>c` and the test keys under `<leader>t`; **miscellaneous under `<leader>a`**: `ai` state, `ao` open the project, `ak` list the keys, `al*` the logs. Standard features stay on LazyVim's keys, and it already binds several for any server that offers them: `gd`, `grr`, `gI`, `gy`, `K`, `<leader>ca`, `cA`, `co` (organize imports: found by a test to be LazyVim's own, so not rebound), `cr`, `cR`, `cf`. **A key that needs IntelliJ exists only where IntelliJ is**: buffer-local, made on `LspAttach` for a Session, only for the filetypes it is for (`kotlin`, `java`; Gradle's `*.kts` are `kotlin`), removed on `LspDetach`, back on reconnect; the miscellaneous ones are global, since opening the project is how to get a connection. LazyVim's AI extras share `<leader>a` as "+ai" with `C a b c d e f h m n p q r s t v x` under it, so the miscellaneous suffixes are ones they do not use, and `tests/test_keys.py` reads their source and fails on a collision; an empty mapping on the prefix (how they name their group) is no clash, a which-key group that already has a name keeps it, and each entry is described "IntelliJ: ...". An existing mapping is never overwritten, and one the developer puts on a key of ours is never removed.

```
$/ij/focus              C→S  notification  the Editor's active buffer changed (selects the Mirror; drives the daemon)
$/ij/status             S→C  notification  Brain state: Indexing / Ready, and why
$/ij/fileChanged        S→C  notification  a Mirrored file changed on disk outside the Editor
$/ij/reimport           C→S  request      re-import the Gradle/Maven model
$/ij/runConfigurations  C→S  request      list run configurations (Tier 4)
$/ij/search             C→S  request      IDE-scoped project search (Tier 4)
$/ij/newFile            C→S  request      text and place for a new class from a template (built; <leader>fN)
$/ij/log                C→S  request      the Brain log's level and path (built; <leader>al...)
$/ij/testTargets        C→S  request      the test for a class, or the class for a test (to do; a key under <leader>t)
$/ij/runnables          S→C  notification what can be run in a file, for the sign column (to do)
$/ij/run, run/output    C↔S  request, notification  run a test or configuration, stream its output and results (to do)
```

## 10. Decisions

All four were taken by the maintainer; each records what was chosen and why it holds.

**D1 - Locations that are not files: extract to a cache directory.** A definition inside a jar or a decompiled class is delivered by having the Brain write the source (or IntelliJ's decompiled text) to a cache directory and return a `file://` URI. It works with every client and every Neovim picker and is the simplest to build. **Design constraint:** keep the extraction behind one function so it can be replaced by a virtual document (`workspace/textDocumentContent`, LSP 3.18, with a `BufReadCmd` provider in the Neovim plugin) later without touching any feature. Cached files must be read-only, must live in the developer's own cache directory (Unprivileged), must be named so the buffer identifies the library and version, and must be invalidated when the library changes.

**D2 - Refactorings that mutate documents: data first, scratch copy last.** The Brain returns `WorkspaceEdit`s and never applies them to a Mirror. Order of preference: (a) `ModCommand`-based actions, which are already edits-as-data; (b) for processors that expose a usage list (rename, safe delete), build the edits from `UsageInfo` directly; (c) only for extract-method-class refactorings, run against a **scratch copy** of the affected documents, diff and discard, behind a feature flag and after its own spike. Nothing in (c) ships without that spike.

**D3 - Code-action latency: titles now, edits on resolve.** `textDocument/codeAction` returns titles and kinds immediately; the edit is computed in `codeAction/resolve` when the developer selects one. **Verify first** that Neovim's built-in code-action menu resolves lazily on selection; if it does not, the fallback is computing edits up front for the current line only.

**D4 - Semantic tokens: never.** Treesitter is the highlighting layer. Removed from this document. Reopen only with evidence that treesitter's Java or Kotlin highlighting is inadequate.

## 11. Build order

1. **v1 spine (in progress):** Registry, Sessions, Mirrors, save handshake, streaming completion. **Next in line:** diagnostics, Indexing surfaced, incremental completion and cache.
2. **Navigation core: built.** Definition, type definition, implementation, references, hover and document highlight, each asserted on the wire and through Neovim's own `vim.lsp.buf.*` calls, in Kotlin and Java, with Neovim focused and IntelliJ in the background. How, for whoever extends it:
   - They run in cancellable **background read actions**, not on the EDT, in `NavigationEngine`. A reply is `ContentModified` (-32801) if the Mirror changed while answering or the Brain is Indexing, `RequestCancelled` (-32800) at once on `$/cancelRequest`, and exactly one reply is ever sent.
   - Locations come from the target file's *document*, so a target in a buffer with unsaved changes is reported on the line the Editor sees. Library code follows D1: written read-only to `~/.cache/ij-nvim-bridge/library/<key>/`, keyed by the jar entry's URL, decompiled text for classes.
   - Definition uses `GotoDeclarationAction.findAllTargetElements`, type definition `GotoTypeDeclarationAction.findSymbolTypes`, implementation `DefinitionsScopedSearch`, references and highlights `ReferencesSearch` (project or file scope, read/write from `ReadWriteAccessDetector`).
   - **Hover needs two APIs.** Kotlin (K2) documents symbols only through the newer documentation-target API, whose `computeDocumentation()` throws for Java targets outside a coroutine context; Java answers to the older `DocumentationProvider`. The signature and the documentation fall back independently. IntelliJ's HTML is converted to Markdown by a small converter that is deliberately not a general one.
   - Not built: declaration (`gD`, same as definition from a usage), and results are capped at 5000 locations.
3. **Symbols: built.** Document symbols, workspace symbols, folding ranges, selection ranges and signature help, each asserted on the wire and through Neovim's built-ins (`document_symbol`, `workspace_symbol`, LSP `foldexpr`, `selection_range`, `signature_help`). For whoever extends it:
   - **Document symbols** come from the file's structure view, hierarchical, with the name's range as `selectionRange`. The kind is inferred from the PSI class *names* (`KtClass`, `PsiMethodImpl`, ...) because Java's and Kotlin's PSI classes are not both on the plugin's classpath; a top-level function is a Function and a member a Method.
   - **Workspace symbols** are IntelliJ's Go to Class and Go to Symbol contributors, matched camel-hump style and ranked exact, then prefix, then the rest, capped at 100. What IntelliJ lists is the answer (it does not necessarily offer every overriding method), and it needs no open buffer.
   - **Folding** uses IntelliJ's folding builders; a lone closing bracket stays visible, as other servers do. Kinds are `comment`, `imports`, `region`.
   - **Selection ranges** merge IntelliJ's word-selection handlers with the PSI ancestor chain: the handlers alone gave only "the word" then "nearly the whole file" and skipped every expression in between.
   - **Signature help** plays the part of IntelliJ's parameter-info popup: the handlers draw through a UI context, and the Brain records the label, each parameter and the current one. Newer handlers hand over a parameter list, Kotlin's draws a bare one that is normalised to `name(...)`; overloads are all returned and the one with enough parameters for the one being typed is active. The Mirror's caret is moved first, since some handlers read the caret rather than the offset.
4. **Edits: formatting built.** `textDocument/formatting` and `rangeFormatting`, each asserted on the wire and through `vim.lsp.buf.format`. The Brain copies the Mirror's PSI file, reformats the copy in a write action on the EDT, and diffs it against the original into minimal line-based edits (`ComparisonManager`); the Mirror and disk are never touched. It cannot share the navigation engine's read-action path, since waiting for the EDT from inside a read action can deadlock; it works on syntax, so it is allowed while Indexing. Two things worth knowing: (1) **the style must be resolved from the original file**, not the copy: a copy is not on disk and silently lost the project's `.editorconfig`, formatting with 4 spaces where the project asked for 2, so the original's settings are passed in explicitly; (2) the client's `FormattingOptions` (`tabSize`, `insertSpaces`) are deliberately ignored, since Neovim's settings are not the IDE's and the IDE's are the point. 
5. **Edits: organize imports built.** `textDocument/codeAction` lists `source.organizeImports` (title only, honouring `context.only`, kind or parent kind) and `codeAction/resolve` computes the edit (D3), on a copy of the Mirror's file diffed into edits (D2), tested on the wire and through `vim.lsp.buf.code_action{apply=true}` for Kotlin and Java. The action carries the Mirror's version; resolving against a changed buffer is `ContentModified`, including a change during the computation. Three things worth knowing: (1) **the optimisers must be chosen from the original file**: a copy has no virtual file, and Java's optimiser reported `supports` false for it, silently doing nothing (the same optimiser processes the copy perfectly well); (2) it needs the indices (Indexing answers `ContentModified`), unlike formatting; (3) IntelliJ's own Kotlin behaviour shows through: `java.util.ArrayList` is rewritten to the `kotlin.collections` typealias, as it would be in the IDE. Completion insertion is next.

5b. **Edits: completion insertion built.** `completionItem/resolve`: accepting an item does what IntelliJ does, the import, the parentheses, the lambda braces. The Brain keeps the `LookupElement`s it answered with (`ElementStore`, 8000, each item carrying `data: {uri, id}`); on resolve it runs IntelliJ's own `handleInsert` on a copy of the file with a throwaway editor (D2) and diffs the copy into a `textEdit` (the word and what it grew into), `additionalTextEdits` (the import) and, when IntelliJ leaves the caret inside the insertion (`foo(|)`), a snippet `$0` with `insertTextFormat: 2`. Tested on the wire (Kotlin and Java, class import, call, lambda, plain identifier, contract cases) and through real blink.cmp accepting a menu item. Things worth knowing:
   - **blink.cmp gives resolve 100 ms** (`completion.accept.resolve_timeout_ms`) and past that inserts the plain word, as before this feature: degraded, never broken. Resolve takes about 50 to 150 ms, so it relies on blink's own *prefetch*, which resolves the highlighted item as soon as it is selected (debounced 50 ms, cancelled when the selection moves, and `$/cancelRequest` is honoured); a developer takes far longer than that to press Enter. Only an instant accept degrades. Raising the timeout to 500 to 1000 ms in the developer's blink config removes even that.
   - **An item stays valid while more of the word is typed**: the Brain stores the text each answer was for, and accepts the item if the buffer is that text plus identifier characters at the caret (an answer for `UUI` is right for `UUID`). Any other change is `ContentModified` and the plain word is inserted. This mattered because the Editor shows an older answer as an interim and keeps its items for the new one.
   - The same insertion runs off a copy, so the Mirror and the disk are never touched; it needs the indices (Indexing answers `ContentModified`).
   - Not yet: items other than the completed word's own (auto-import of extension functions works through the same path, untested), `detail`/documentation in the resolved item.
   - **Known IntelliJ behaviour, seen in the harness:** for a file the IDE has been asked about many times (its code style settings cached), a `.editorconfig` created later on disk was not picked up by formatting within a minute, though a refresh had been requested; a file never asked about picked it up at once. The `.editorconfig` test therefore uses a file no other test touches. Worth revisiting if a developer reports formatting ignoring a new `.editorconfig` until the IDE is focused.
   - Harness: `nvim_session` waited only for blink's library file, not for the plugin to load or for the version file, which raced; both are waited for now.

5c. **Edits: rename built.** `textDocument/prepareRename` (the name as written at the caret, and `null` where there is nothing to rename) and `textDocument/rename`, tested on the wire (Kotlin and Java) and through `vim.lsp.buf.rename` across files. Data first (D2): IntelliJ is asked what it would rename (`RenamePsiElementProcessor.prepareRenaming`, which adds overriders) and every usage (`RenameUtil.findUsages`), and the answer is a `WorkspaceEdit`; nothing is applied in IntelliJ, and unsaved buffers are what is searched. Things worth knowing:
   - **Three phases, not one read action**: Kotlin's `prepareRenaming` uses a modal progress to find overriders, which cannot be waited for from inside a read action (`invokeAndWait` deadlock check). So the request validates in a read action, has IntelliJ work out what else must change on the EDT, as its own Rename does, and collects usages in a second read action. It therefore does not use the navigation engine's cancellable read action; a change during the request is `ContentModified` before and after.
   - A top-level class whose file is named after it (Java always; Kotlin when it is the file's only declaration) also renames the file: the answer is then `documentChanges` with the edits first and a `rename` resource operation last. Tested on the wire only: Neovim applying it renames the file on disk.
   - **Decisions taken for the developer, because there is no dialog to ask**: renaming an *override* renames that override and its own overriders, not the base method (IntelliJ asks whether to rename the base too); name conflicts are not checked; library code is refused with a reason ("not part of this project"); Kotlin accepts almost any name in backticks, so only an empty name is refused there, while Java refuses `1bad name`.
   - Not yet: renaming from an override up to the base, name-conflict warnings, properties files and Spring bean names, renaming in comments and strings, Kotlin synthetic accessors of Java getters (a usage whose text differs from the element's name).

5d. **Edits: move file / update package built** (`workspace/willRenameFiles`). Asked before Neovim moves a `.kt` or `.java` file (the client sends it; `vim.lsp.util.rename`-based plugins such as snacks and oil do), it answers with a `WorkspaceEdit` against the files as they are now: the moved file's `package` line from its new directory (relative to the source root), every explicit import and every fully qualified name of what the file declares (found with `ReferencesSearch`, spelled-out detection is textual because the two languages' syntax trees disagree about `a.b.C(...)`), and an import wherever a name stops resolving: in files that used it by sharing the old package or by a star import, and in the moved file for what it used from the old package without saying so. Tested for what matters, that **the project compiles afterwards**: each test applies the edits, moves the file on disk, and asks IntelliJ (through the diagnostics the Brain publishes) whether anything in the affected files is broken, Kotlin and Java, against a control that changes only the moved file's package line and is seen to break. Also built: `workspace/didRenameFiles`, `didCreateFiles` and `didDeleteFiles` make the Brain refresh its view of the disk, since an IDE that is not the active application does not look by itself. Not yet: moving a directory (a package rename), keeping imports sorted (new ones are appended; Organize Imports sorts them), several files moved together that refer to one another, and renaming a file to rename its class.

5e. **New file from a template, and the gold standard, built.** `$/ij/newFile` (an extension; LSP has no such request) returns the text IntelliJ's own file template gives for a class, interface, enum, object, data class or Kotlin file (Java: class, interface, enum, record, annotation) with the `package` line derived from the directory (relative to the source root; the directory need not exist), and the URI it belongs at; it creates nothing. The Editor's `require('ij_bridge').new_file{dir, name, template, language}` (and `:IjBridge new [dir]`, which asks) writes the file, and that first write is what attaches it: **a file that does not exist on disk yet is not attached** (the Brain could not open it), it is attached by its first write. Refused with a reason: an invalid name, a file that already exists, a directory outside every source root, an unknown kind (naming the kinds there are). The **gold standard (§11b)** is `tests/test_gold_standard.py` and passes: create a class from a template, write a body, create a second file that imports it, move the class to another package (the file, its package line and the other file's import follow), in a third new file type a variable of that type (the menu offers it from the package nobody imported; accepting adds the import), and IntelliJ finds nothing wrong, against a deliberate error as the control. Two real bugs it found: (1) **a released Mirror was reloaded from the IDE's stale cached view of the file** when Neovim had written the file behind an IDE that was not the active application (a body written after the file was first seen was lost to the search, so a move did not find the file that used the class). `didSave` now refreshes that one file, keeping the veto so the Mirror's text is untouched; (2) the harness's IDE has no working file watcher (`fsnotifier` exits in the container), which is why it needed a refresh lever at all; on a machine whose watcher works the IDE hears of writes by itself, and the refresh on `didSave`, `didRenameFiles`, `didCreateFiles` and `didDeleteFiles` covers the case where it does not.

5f. **Code actions: quick fixes and intentions built.** `textDocument/codeAction` now lists what IntelliJ would show under Alt+Enter at the range: the fixes for the errors and warnings there (kind `quickfix`) and the intentions (kind `refactor.rewrite`), beside Organize imports, honouring `context.only` (a parent kind matches what is under it). Titles now, edits on resolve (D3): the action is run for its **`ModCommand`, IntelliJ's action-as-data, and that command is read, never executed** (D2), so nothing in IntelliJ changes: a `ModUpdateFileText` (old text, new text) becomes edits by diffing, per file. Tested on the wire (Kotlin K2 and Java; "Replace size check with 'isEmpty()'", "Convert concatenation to template", "Remove variable", "Replace '==' with 'equals()'", "Convert to block body", ...) and through `vim.lsp.buf.code_action`. Things worth knowing:
   - **Only actions with a `ModCommand` form are offered.** An older action mutates the file when invoked, and cannot be run against a copy: its pointers are into the original file. So the legacy ones are absent, notably **Create function/class from usage** (an unresolved name offers only "Remove invocation"). The K2 Kotlin and current Java fixes are largely ModCommand, which is why so much is there. Reaching the rest is a spike of its own (a scratch copy of the file, with the pointers rebound), not attempted.
   - **Steps that need a person are refused with a reason, not half done**: "Specify type explicitly" starts a live template (`ModStartTemplate`), choosing members, renaming in place, editing options, creating or moving a file (`this action needs more than a text edit (ModStartTemplate)`), as `-32602`. A few offered actions are no-ops as edits ("Copy concatenation text to clipboard" resolves to no edits).
   - The Mirror's caret and selection are set to the request's range first (what is available depends on both), so this is not the cancellable read-action path alone; the fixes for an error appear once the daemon has analysed the file, so a request made the moment a file is opened may offer only intentions (the tests ask again, as a developer would).
   - An action is found again at resolve by its family and title at the same range; the buffer having changed is `ContentModified`.

5g. **Inlay hints built.** `textDocument/inlayHint`, tested on the wire (Kotlin and Java, ranges, unsaved edits) and through `vim.lsp.inlay_hint`. Harvested, as diagnostics are, from the inlays IntelliJ's own passes put on the Mirror's editor (which analyses a showing editor), so they are the developer's IDE's own hints, **by the developer's own inlay settings (a Borrowed Setting), for the unsaved text**; no provider is reimplemented. Two kinds of inlay carry the text: the declarative ones (Kotlin's parameter names, and newer hints), read from `DeclarativeInlayRenderer`'s presentation entries, and the older parameter-name hints (Java's), from `ParameterHintsPresentationManager.getHintText`. What is not a text hint is left out: the code-vision inlays at line ends (usage counts, "implements") are what a **code lens** would carry, and are for that feature. Things worth knowing:
   - **The passes finish after the file is opened and again after every edit**, so a request can be answered before they have. The Brain watches each Mirror's inlay model (`InlayModel.Listener`, attached on the EDT) and, once the inlays have settled for 300 ms and really changed, sends the client a `workspace/inlayHint/refresh` request; Neovim's handler asks again. Without it the client asks once, too early, and shows nothing until the next keystroke. Tested through Neovim, including hints for a call typed later, without the test asking again.
   - The kind is `Parameter` for parameter names (with right padding) and `Type` for types; a hint of unknown origin is sent without a kind.
   - Declarative hints are reached through a getter that Kotlin sees as private, called reflectively, so a change in IntelliJ skips those hints and breaks nothing else. Which hints there are (types of locals, chained calls, lambda returns) is the IDE's setting: a hint that is off in the developer's IDE is off here.
   - Found by the Brain's own log, the first time it was needed: attaching the inlay listener off the EDT threw inside `didOpen`. A watcher failing can no longer fail an open.

5h. **Generate code built.** `source.generate.*` code actions (a parent kind matches what is under it, so `only = { 'source.generate' }` lists them all): **Java**: constructor, getters, setters, getters and setters, `toString()`, `equals()` and `hashCode()`, implement missing methods. **Kotlin**: `toString()`, `equals()` and `hashCode()`, implement missing members (offered only for what a data class does not already have). Only what is missing is offered (a getter that exists is not generated again), and a place outside a class offers nothing. Tested on the wire, through `vim.lsp.buf.code_action`, and for what matters: **each result is opened as a Mirror and IntelliJ finds nothing wrong with it** (which caught a real precedence bug in a generated Kotlin `hashCode`). Things worth knowing:
   - **IntelliJ's Generate menu cannot be used as it is**: its actions are dialogs (choose the members) that change the file when they end, and Kotlin's run their analysis in a modal window. A dialog cannot be answered headless, and one shown on the developer's desktop would flash and take the focus. So a `Generator` (an extension point, `dev.bridge.brain.generator`) does the same work programmatically, on a copy of the file, and the result is diffed into edits (D2). The dialogs' choice is made the way they start: **all** the fields or members.
   - **New build dependency, optional at run time**: the Brain now compiles against the bundled Java and Kotlin plugins for typed PSI, declared `optional="true"` in `plugin.xml` with a config file each (`brain-java.xml`, `brain-kotlin.xml`) that registers that language's generator. An IDE without either still loads the Brain, without the feature. This opens the door for the refactorings that need typed PSI.
   - Java uses IntelliJ's own helpers (`GenerateMembersUtil` accessors, `GenerateConstructorHandler`, `OverrideImplementExploreUtil` and `OverrideImplementUtil`) and its default templates' text for `toString`, `equals` and `hashCode` (with `java.util.Objects`, its import added). Kotlin builds `toString` and `equals`/`hashCode` from the syntax tree (constructor properties and body properties with a backing field; arrays by content; nullable by `?.hashCode() ?: 0`), and calls IntelliJ's `KtImplementMembersHandler` for implementing members.
   - **Kotlin's handler is called outside a write action** (`Generator.needsWriteAction`): the analysis API forbids analysis inside one, and the handler writes by itself; it needs `allowAnalysisOnEdt`, and it fails at the very end trying to open the file to place the caret, which a copy does not have (the members are in by then; that one failure is tolerated).
   - Not yet: choosing which fields (there is no way to ask; all are used), Java `Override methods...` (only implementing what is missing), Kotlin secondary constructor, delegate methods, `Generate test`, and templates for `toString` other than the default.

5j. **Gradle Kotlin scripts (`*.gradle.kts`) tested.** They are Kotlin files IntelliJ analyses against the Gradle model, and they work as Mirrors with no change: `build.gradle.kts` and `settings.gradle.kts` attach (Neovim's filetype is `kotlin`, so the Kotlin keys apply), report **no false errors** on the real script (`plugins {}`, `repositories {}`, `dependencies {}` all resolve) and **do report a real one** (a deliberate `implementation(thisDoesNotExist)` is flagged and clears when removed: the control for the claim), hover shows the Gradle DSL (`DependencyHandler.implementation`, `Settings.getRootProject()`), go to definition reaches the Gradle API (`RepositoryHandler.java`, extracted read-only per D1), completion inside `dependencies {}` offers `implementation` and `testImplementation`, symbols are the top-level blocks, blocks fold, a messy script is formatted in the IDE's style, and through Neovim's own calls the buffer is attached, Ready, hovers, and goes to the Gradle API. One real limit found: **organize imports is not offered for a Kotlin script**. The optimiser runs on a copy of the file (D2), and a copy of a script has no script analysis context; K2's optimiser then fails on the DSL's calls (`KaBaseInvokeFunctionReference ... is missing in the map`), although the same call on a plain `.kt` file works. An action that can only fail is not offered (a test asserts it). Not covered: convention plugins in `buildSrc` and `build-logic`, version catalogs (`libs.versions.toml` and the typesafe `libs.` accessors, which exist only after a sync generates them), and behaviour while the Gradle model is still loading (the script is then analysed without the DSL, and the Brain answers Indexing).

   Original plan for this step, kept for the remaining items: formatting and organize imports first (the project's motivation, and smallest: one copy, one diff), then completion insertion, then code actions (D3), then rename (D2).
5. **On-save and generation:** format-on-save, generate code, extract/inline/move.
6. **Hierarchies, inlay hints, code lens, workspace diagnostics.**
7. **Project extensions:** run configurations, build, search.

## 11b. Gold standard scenario

One test that walks the developer's loop end to end in the toy project, in real Neovim with real blink.cmp, and fails if any link breaks. It is the acceptance test for the edits work as a whole, and is written when new-file and move exist (it cannot pass before):

1. Create a new class (from a template, so the package line is right).
2. Move it to another package: the file moves, its `package` line and every reference follow.
3. Write a body in it.
4. In another file, declare a variable of the new type: the completion menu offers it (a class not yet imported, in a different package).
5. Accept it: the import is added, and the file compiles (no unresolved-reference diagnostic).

Steps 4 and 5 already work on the wire and through blink (completion insertion, tested with `UUID`). What remains is 1 and 2.

## 12. Acceptance for every feature

A feature is done when, against the real Brain in the harness, with **Neovim focused and IntelliJ in the background**:

- it works through Neovim's *built-in* LSP call for it, with no custom UI (Tier 1-3);
- it answers from the Mirror when the buffer has unsaved changes (a test that would fail if it read disk);
- it returns `ContentModified`, not a stale answer, when the Mirror changes mid-request or the Brain is Indexing;
- its capability is advertised only after it works, with a test asserting the advertisement;
- its `OVERHEAD` is measured and reported, and gated where it is interactive.
