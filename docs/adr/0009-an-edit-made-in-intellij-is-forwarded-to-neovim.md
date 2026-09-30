# An edit made in the IntelliJ window is forwarded to Neovim, not kept

A Mirror is a real, visible, editable IntelliJ editor ([ADR-0001](./0001-mirrors-are-real-intellij-editors.md)), so nothing stops a person typing into it, or running one of IntelliJ's own refactors or quick fixes on it. Such an edit changes the Mirror's Document and reaches neither Neovim nor the disk. The Brain answers every request from the Mirror's text, so completion, diagnostics and navigation are then computed for text the Editor does not have, and the difference is found only when the Mirror is released and reloaded from disk ([ADR-0003](./0003-editor-owns-disk-writes.md)), when the edit disappears without a word.

The Editor's buffer is the Source of Truth. **A Mirror must equal it.** So an edit that arrives from anywhere but the Editor is not kept: it is undone in the Mirror and sent to Neovim as an ordinary edit for Neovim to apply. IntelliJ's own typing, quick fixes and refactors then work as a fallback for anything the Bridge cannot do, and Neovim stays the authority.

## How

**Telling a foreign edit from the Brain's own.** Every change the Brain makes to a Mirror's Document goes through three paths: `didOpen` (the first text), `didChange` (the Editor's edits), and the reload when a Mirror is released. Each sets a flag while it runs. A `DocumentListener` on each Mirror (added when it opens, removed when it is released) treats any other change as foreign. IntelliJ's undo is already off for Mirrors, so IDE-side undo cannot bring anything back either.

**Settling.** A refactor is several document changes in one command, and typing is one per key. On the first foreign change the listener records the Mirror's text as of *just before* it (`base`: equal to Neovim's, since a Mirror is convergent), and waits until changes have stopped for about 200 ms. Then `edits = diff(base, current text)`, computed with the same `TextEdits.diff` code actions use.

**Forwarding.** The Brain sends the Session that owns the Mirror a standard `workspace/applyEdit` request, `documentChanges` with `textDocument.version` set to the Mirror's version. The version is the point: Neovim refuses the edit if its buffer has moved on (it has typed something the Brain has not seen), and a refusal must never mean a silent overwrite. One request carries every Mirrored file the command touched, so each buffer gets one undo step. Neovim's own handler applies it; nothing new is needed in the Editor. Neovim then sends the usual `didChange` for it.

**Not swallowing the echo twice.** Until that `didChange` arrives the Mirror keeps the IDE's text (it is what the buffer is about to hold, so requests are answered for the right text, and the IDE window does not flicker). When the `didChange` arrives, the Brain first restores the Mirror to `base`, silently, and then applies the Editor's changes as it always does. The result is the same text, arrived at from the Editor's side, so the edit cannot be applied twice, and any keystroke Neovim made in the meantime is in that batch too.

**When Neovim refuses** (`applied: false`: its buffer changed, or the buffer is gone), the Brain restores `base` at once and tells the developer with `window/showMessage`: "an edit made in IntelliJ to X was not applied (its buffer changed in Neovim); Neovim's text was kept". The edit is lost, and said to be. Losing it loudly is the price of never guessing which text wins.

**One in flight per Mirror.** A foreign edit made while another is being forwarded is remembered and, once the first has been echoed, replayed as a fresh foreign edit against the new `base`, so it goes through the same steps rather than being folded in.

## Considered options

- **Read-only Mirrors.** Simplest: nothing can diverge because nothing can be typed. Rejected: it also refuses IntelliJ's own quick fixes and refactors on a Mirrored file, which is where a developer goes for what the Bridge lacks (Extract Method), and it makes a Mirror less of a real editor than ADR-0001 says it is.
- **Undo the edit and say so.** Small, and safe. Rejected as the end state, since the edit is then always thrown away; kept as what happens when forwarding is refused.
- **Keep the edit and treat the IDE as a second source of truth.** Rejected: two truths need a merge, and the Bridge has none. It would also turn every keystroke race into a data-loss question.

## Consequences

- Typing in the IntelliJ window works, one forwarded edit per pause. It is not a way to edit at speed, only a fallback that does not lie.
- Two Neovims on one file remain unsupported ([ADR-0003](./0003-editor-owns-disk-writes.md)): with more than one owner the edit is not forwarded, and is undone with a message.
- Files a refactor changes that are **not** Mirrored are written to disk by IntelliJ as usual. A clean buffer for one of them is not in the Mirror Set (only the active buffer and unsaved ones are), so Neovim finds out through its own file-change handling (`autoread`, `:checktime`), as for any external write. Mirrored and unsaved buffers are the ones this ADR covers.
- IntelliJ's save on a Mirror stays vetoed: `Ctrl+S` in the IDE window writes nothing.
- Every foreign edit is written to the Brain's log (`foreign_edit`, with what happened to it), which is also how an edit IntelliJ makes by itself, and nobody meant, would be noticed.

## Status

Built (`ForeignEdits.kt`; `Session.request` for the Brain's own requests to the Editor, whose replies were until now ignored). Changes from the design as first written: the replay of an edit made while another is in flight is decided by comparing the Mirror's text with what was sent, not by a flag, because the second edit can arrive before its own settling has run. Found on the way: `MirrorSet.open` could make two Mirrors on one Document when two Editors opened a file at the same moment; it is now serialized.

## Tests

On the wire, with a client that plays the Editor: an edit made through a debug lever (a Document change the Brain did not make) is answered by a `workspace/applyEdit` with the right version and the right edits; `applied: true` then the echo `didChange` leaves the Mirror equal to the client's text, once; `applied: false` restores the Mirror and shows a message; a second edit during the first is replayed; a change to two Mirrors is one request. Through a real Neovim: the edit appears in the buffer, is one undo step, and completion for a symbol it added resolves.
