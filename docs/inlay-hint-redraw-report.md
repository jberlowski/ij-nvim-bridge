# Inlay-hint redraw failure — photo transcription

Source: user-supplied `Zdjęcie 1.jpg`, transcribed on 2026-10-15. This is the readable text in the main terminal pane; the adjacent terminal is cut off. Claims below are transcribed from the photo, not independently verified findings. Line wrapping is normalised.

## Transcription

The lead outcome is possible but not confirmed. There haven't been any explicit tests or validation mentioned for the user's request, so I won't include them. The developer specifically noted that validation checks should only be mentioned if requested. I could discuss test coverage to help with bug investigation, but I'll refrain from mentioning specific tests. It seems the next step is to investigate the Bridge to Neovim handoff. While the user is working on something else, I might propose adding temporary metadata instrumentation to capture important details, but I'll need to ask for their permission first.

I need to express that the root cause isn't determined, and there won't be any more changes. It's crucial to keep this clear and straightforward. So, I should wrap up the explanation in about 130 words, making sure I convey that while the issue is still unresolved, we've concluded any further modifications for now. I want to ensure the user understands the status without confusion. Keeping it concise will help maintain clarity!

• Yes—the IntelliJ Bridge could be responsible. I was too quick to blame Neovim alone. The stack trace identifies where drawing failed, not where the incorrect hint position originated.

Your Bridge logs confirm it was returning inlay hints during the failing editing session. The Bridge rejects some responses when the Mirror's version changes, but that doesn't prove every accepted response matches Neovim's current buffer.

My earlier reproduction only established that stale positions can crash Neovim's renderer—it didn't establish who made them stale. The investigation should now focus on the Bridge–Neovim handoff: matching hint responses to buffer versions during rapid edits, rather than making further changes to inline Git diffs.

> can you try to describe repro case? i will fix it on another computer

• Repro: inlay-hint redraw crashes after editing or navigating changes

Environment: Neovim 0.12.5 (Homebrew package 0.12.5_1), LazyVim, IntelliJ Bridge providing LSP inlay hints. Observed in a TypeScript file.

Steps to try:

1. Open a project in IntelliJ and attach Neovim through the Bridge.
2. Open a file with visible inferred-type or parameter-name hints. Confirm Neovim's inlay hints are enabled.
3. Rapidly edit a hinted line—especially shorten it, delete part of it, or undo/redo while hint requests are outstanding.
4. Trigger redraws during editing or press F7 to navigate to another Git change.
5. Repeat with inline Git diffs disabled—the error still occurred after we added disable-on-edit behavior.

Actual result:

```text
Decoration provider "win" (ns=nvim.lsp.inlayhint):
.../vim/lsp/inlay_hint.lua:362: Invalid 'col': out of range
```

The failing call is Neovim's `nvim_buf_set_extmark()` while drawing cached inlay hints. Snacks notifications appeared further down the stack because they triggered a redraw.

Expected: hints from an outdated document snapshot are discarded or refreshed; editing and navigation never produce a decoration error.

Evidence and investigation targets

The Bridge log around 2026-10-15 11:34 UTC showed overlapping `textDocument/inlayHint` requests during rapid `didChange` notifications. Some replies took roughly 0.5–1.1 seconds. Some were rejected with `-32801: the buffer changed while answering`; others succeeded.

Capture these values together:

- Buffer URI, request ID, and buffer version/changedtick when requesting hints.
- Mirror version used to compute the response.
- Neovim version/changedtick when accepting and rendering it.
- Hint row/byte-column and the current line's byte length.

Important uncertainty: this is the observed workflow, not yet a deterministic Bridge-specific reproduction. Separately, I reproduced the exact native error by caching a valid hint at column 9, shortening its line to `"x"` while leaving the cached version unchanged, then invoking the real renderer. That proves the stale-column failure mechanism—not that the Bridge is necessarily its source.

## Local reproduction attempts

User clarification after the first attempt: the error occurred simply by adding a new line to a file with many inlay hints. Rapid editing or forced redraws are not required. The original photo transcription above is preserved as received.

2026-10-15: ran the real IntelliJ/Neovim Docker harness using the locally built Brain plugin and the current Editor source. The focused run reported `NVIM v0.12.5`.

```sh
.venv/bin/python -m pytest tests/test_inlay_hint_redraw_repro.py::test_rapid_shortening_and_redraw -q -s
```

```text
100 rapid shortening/restore cycles: no inlay-hint decoration error
NVIM v0.12.5
1 passed in 105.54s (0:01:45)
```

The test waits for three genuine Java parameter-name hints, then restores and shortens the hinted line 100 times, with delays of 0–100 ms, forced redraws, and cursor jumps. It checks Neovim's messages for the reported column error and inlay-hint decoration errors, then verifies that hints return after restoring the line. It never modifies Neovim's private hint cache. The earlier run also passed the two existing built-in inlay display/edit-refresh checks, before the exploratory test encountered a duplicate-provider setup error caused by collecting its imported helper class; that collection issue was corrected before the focused run above.

**Result: the reported crash was not reproduced.** This is an exploratory stress check, not a confirmed regression test for the reported bug. It uses Java rather than the reported TypeScript file, and cursor jumps rather than F7/Git-change navigation. It does not replay the original overlapping request trace or reproduce the user's inline-diff setup. No Bridge defect or root cause has been established, and no runtime fix was made.

Next reproduction input: the original TypeScript snippet and relevant Bridge request/response log around the failure, ideally with request IDs and buffer/Mirror versions. Keep the todo open until the reported path is reproduced and diagnosed.

### Many hints and a single newline

Following the clarification, ran:

```sh
.venv/bin/python -m pytest tests/test_inlay_hint_redraw_repro.py::test_single_newline_with_many_hints -q -s
```

The file has 400 Java calls with **1,200 genuine Bridge parameter hints**, verified before each edit. Each scenario restores the file, waits for hints, positions the cursor, and presses `i<CR><Esc>` once. There are no explicit redraw commands or cache modifications. Each edit is followed by two seconds of error monitoring and a check that all 1,200 hints are available. Tested Enter before the first call, before a middle call, inside a middle call before its arguments, inside it after the first argument, and after the final call.

```text
before first call: one Enter, 1200 hints, no decoration error
before middle call: one Enter, 1200 hints, no decoration error
inside middle call before arguments: one Enter, 1200 hints, no decoration error
inside middle call after first argument: one Enter, 1200 hints, no decoration error
after final call: one Enter, 1200 hints, no decoration error
NVIM v0.12.5
1 passed in 112.52s (0:01:52)
```

**Not reproduced with this large Java file either.** This covers the clarified ordinary-newline trigger with many hints; it still does not establish behavior for the original TypeScript file, its mix of hint kinds, or the user's IDE settings. No runtime fix was made.

## Defensive Editor checks

The developer could no longer reproduce the original failure after restarting and requested defensive checks on the Neovim side. A second photo confirms the native stack: `nvim_buf_set_extmark` → `inlay_hint.lua:362` → `nvim__redraw` → Snacks window/notifier redraw. It does not establish the original source of the stale column.

`editor/lua/ij_bridge/inlay_hints.lua` now guards only Bridge requests and cached Bridge hints:

- Capture the buffer's changedtick when requesting hints; discard the response if that snapshot has changed or the buffer is unloaded.
- Validate integer row/column bounds using strict conversion from the negotiated position encoding before handing results to Neovim.
- Empty the Bridge client's cached hints synchronously on buffer edits, before a redraw can reuse their old positions. Leave hints enabled so normal LSP refresh supplies fresh ones.

The deterministic test `tests/inlay_hint_guard.lua` uses Neovim 0.12.5's actual response handler and renderer with controlled responses. Its unguarded baseline triggers the exact `Invalid 'col'` error by shortening a line under a cached hint while the LSP version stays unchanged. With the guards enabled, the same renderer call succeeds. It also verifies delayed-response rejection, invalid-position filtering, UTF-16 positions converted to actual byte-column extmarks, newline insertion, and fresh hints returning. This proves the defensive behavior for constructed stale data; the original session's root cause remains unconfirmed.

```sh
docker run --rm -v "$PWD:/work/repo:ro" -w /work/repo \
  --entrypoint /opt/nvim/bin/nvim ij-nvim-harness:base \
  --headless -u NONE -l tests/inlay_hint_guard.lua
```

Integration validation against the real Bridge, with guards enabled:

```sh
.venv/bin/python -m pytest tests/test_inlay_hints.py::TestThroughNeovimsBuiltins tests/test_inlay_hint_redraw_repro.py -q -s
```

**4 passed in 129.35s** on Neovim 0.12.5: initial hint display, automatic hint refresh after an edit, 100 rapid shortening/restore cycles, and the five single-Enter cases with 1,200 hints. The exploratory cases now use the shared `nvim` fixture so they can run alongside the other integration tests without initializing the same plugin twice.
