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

```
$/ij/focus              C→S  notification  the Editor's active buffer changed (selects the Mirror; drives the daemon)
$/ij/status             S→C  notification  Brain state: Indexing / Ready, and why
$/ij/fileChanged        S→C  notification  a Mirrored file changed on disk outside the Editor
$/ij/reimport           C→S  request      re-import the Gradle/Maven model
$/ij/runConfigurations  C→S  request      list run configurations (Tier 4)
$/ij/search             C→S  request      IDE-scoped project search (Tier 4)
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
