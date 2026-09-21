# ij_bridge - the Neovim side of the Bridge

Lets Neovim borrow the code intelligence of an already-running IntelliJ. See
[SPEC.md](../SPEC.md) for the design and [CONTEXT.md](../CONTEXT.md) for the
vocabulary.

Requires Neovim 0.11+ (developed against 0.12) and blink.cmp.

```lua
-- lazy.nvim
{ dir = '/path/to/ij-nvim-bridge/editor', config = function() require('ij_bridge').setup() end }
```

Register the completion source with blink.cmp:

```lua
sources = {
  default = { 'ij_bridge', --[[ ... ]] },
  providers = {
    ij_bridge = { module = 'ij_bridge.blink', name = 'IntelliJ', async = true },
  },
},
-- To keep IntelliJ's ranking rather than blink's fuzzy score:
fuzzy = { sorts = { 'sort_text' } },
```

A file that is not inside an open IntelliJ project is **Dormant**: the plugin does
nothing and prints nothing. `:IjBridge` says which state a buffer is in.

### New files and moving files

`:IjBridge new [dir]` asks for a kind (class, interface, enum, ...) and a name and creates the file from IntelliJ's own template, with the right `package` line. Programmatically: `require('ij_bridge').new_file({ dir = ..., name = 'Ticket', template = 'class', language = 'kotlin' })`. Moving or renaming a file with a plugin that sends `workspace/willRenameFiles` (snacks, oil.nvim, neo-tree) updates its `package` line and every import that names it. A file is attached to the IDE by its first write: a buffer for a file that does not exist yet is left alone.

### When something goes wrong

Both halves keep a bounded JSON-lines log, always on, that records what was asked and answered (never your code):

- `:IjBridge log` and `:IjBridge brainlog` open the Editor's and the Brain's.
- `:IjBridge report` gathers versions, state and the tail of both logs into one buffer to paste into a bug report.
- `:IjBridge loglevel <off|info|debug|trace>` changes how much is recorded; `trace` includes payloads, which contain code.

The two logs print the same Session id, so a line in one finds its match in the other. See SPEC.md §15.
