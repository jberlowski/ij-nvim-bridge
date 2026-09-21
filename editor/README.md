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

### Keys (LazyVim)

Standard features stay on LazyVim's own keys: `gd`, `grr`, `gI`, `gy`, `K` (goto and hover), `<leader>ca` / `<leader>cA` (code actions, including the quick fixes, generate code and source actions), `<leader>co` (organize imports: LazyVim binds it for any server that offers it), `<leader>cr` (rename), `<leader>cR` (rename file), `<leader>cf` (format), `<leader>uh` (inlay hints). What the Bridge adds that is **not** standard LSP goes where LazyVim keeps that kind of thing:

| Section | Key | Does | Where it exists |
|---|---|---|---|
| file/find | `<leader>fN` | New file from an IntelliJ template (asks kind and name); `fn` is LazyVim's own New File | Kotlin and Java buffers **with an IntelliJ connection** |
| miscellaneous | `<leader>ai` | State of this buffer (attached, indexing, ...) | everywhere |
| | `<leader>ao` | Open this project in IntelliJ, starting it if none serves it | everywhere (it is how to get a connection) |
| | `<leader>ak` | List the keys, and why one is missing here | everywhere |
| | `<leader>all` `alb` `alr` `alv` | Editor log, Brain log, bug report, log level | everywhere |

**A key that needs IntelliJ exists only where IntelliJ is.** It is made, buffer-locally, when the buffer attaches to a Session, only for the filetypes it is for, and removed when the Session goes (and back when it reconnects). In a buffer with no connection, or a file of another kind, it is not there at all: which-key does not offer it and it cannot shadow anything. `:IjBridge keys` (`<leader>ak`) lists what is bound here and says why a key is missing.

`<leader>g` is git and plain `g` is goto in LazyVim; nothing of the Bridge's belongs to either. **LazyVim's AI extras (Claude Code, Avante, Copilot Chat, Sidekick) share `<leader>a`** as "+ai", with `C a b c d e f h m n p q r s t v x` under it. The miscellaneous keys are ones they do not use (`g i j k l o u w y z` are free), and the test suite reads LazyVim's extras and fails if one ever collides. An empty mapping on the prefix, which is how they name their group, does not block them; an existing mapping is never overwritten (skipped, and the log says so); if the prefix itself does something, no miscellaneous key is bound. `setup{ prefix = ..., keys = false, sections = false }` change it.

```lua
-- lazy.nvim, LazyVim
{
  dir = '/path/to/ij-nvim-bridge/editor',
  event = 'VeryLazy',
  opts = { prefix = '<leader>a', keys = true },   -- the defaults; keys = false binds nothing
  config = function(_, opts) require('ij_bridge').setup(opts) end,
}
```

### Several projects, several Neovims, starting IntelliJ

Each buffer is served by the IntelliJ that has *its* project open: with two projects open (a second `idea <root>` opens it in the running IDE), one Neovim holds a Session for each and every buffer goes to its own. Several Neovims may share one IDE; closing one releases only what it had open. `:IjBridge open` (`<leader>ao`) starts the IDE for the current buffer's project when none serves it: it runs `idea <root>` (on your `PATH`; `setup{ idea_cmd = '/path/to/idea' }` otherwise) detached, so it never locks Neovim's terminal and outlives Neovim, shows `IJ: starting` until the project's Brain appears, and then attaches. It is never started by itself.

### New files and moving files

`:IjBridge new [dir]` asks for a kind (class, interface, enum, ...) and a name and creates the file from IntelliJ's own template, with the right `package` line. Programmatically: `require('ij_bridge').new_file({ dir = ..., name = 'Ticket', template = 'class', language = 'kotlin' })`. Moving or renaming a file with a plugin that sends `workspace/willRenameFiles` (snacks, oil.nvim, neo-tree) updates its `package` line and every import that names it. A file is attached to the IDE by its first write: a buffer for a file that does not exist yet is left alone.

### When something goes wrong

Both halves keep a bounded JSON-lines log, always on, that records what was asked and answered (never your code):

- `:IjBridge log` and `:IjBridge brainlog` open the Editor's and the Brain's.
- `:IjBridge report` gathers versions, state and the tail of both logs into one buffer to paste into a bug report.
- `:IjBridge loglevel <off|info|debug|trace>` changes how much is recorded; `trace` includes payloads, which contain code.

The two logs print the same Session id, so a line in one finds its match in the other. See SPEC.md §15.
