# ij_bridge - the Neovim side of the Bridge

Lets Neovim borrow the code intelligence of an already-running IntelliJ. See
[SPEC.md](../SPEC.md) for the design and [CONTEXT.md](../CONTEXT.md) for the
vocabulary. Building the two plugins from source (including the IntelliJ side, below)
is [BUILD.md](../BUILD.md); this document is everything after that: setting the
IntelliJ side up once per machine, and the Neovim side up in your config.

## Setup

Two halves, set up once each. Neither depends on the other's setup order.

### 1. IntelliJ: install the Brain plugin

Build it (see [BUILD.md](../BUILD.md) if you have not), then in IntelliJ IDEA:
**Settings → Plugins → the gear icon → Install Plugin from Disk...** and pick
`brain/build/distributions/brain-0.1.0.zip`. Restart the IDE if it asks. Nothing
else to configure: the Brain starts serving the open project automatically, on a
private per-project socket (`$XDG_RUNTIME_DIR/ij-nvim-bridge/`), the moment the
project has finished loading. Open the project(s) you want Neovim to reach.

### 2. Neovim: install the plugin

Requires Neovim 0.11+ (developed against 0.12) and blink.cmp for completion.

```lua
-- lazy.nvim
{
  dir = '/path/to/ij-nvim-bridge/editor',
  event = 'VeryLazy',
  opts = { prefix = '<leader>a', keys = true },   -- the defaults; see "Keys" below
  config = function(_, opts) require('ij_bridge').setup(opts) end,
}
```

Register the completion source with blink.cmp, in blink's own `opts`:

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

### 3. Check it worked

Open a file that is inside a project IntelliJ has open, and run `:IjBridge`. It
prints which project's Brain serves the buffer, and whether it is ready. A file
that is not inside an open IntelliJ project is **Dormant**: the plugin does
nothing and prints nothing there — `:IjBridge` still says so if asked. If it
never leaves "not ready", the project is likely still importing or indexing;
`<leader>ai` (or `:IjBridge`) again once IntelliJ's own status bar is idle.
Nothing else is required — no port, no host, no manual "start the server":
the Brain and the Registry it publishes to are found automatically per project.

### Optional: where IntelliJ itself lives

Two separate settings, both machine- or user-specific, so both prefer an
environment variable over a hardcoded value in a config you might share:

- **Building** the plugin needs to know where an installed IntelliJ IDEA is, to
  compile against its jars: `IJ_NVIM_BRIDGE_IDEA_HOME` (BUILD.md).
- **Starting** IntelliJ from Neovim (`:IjBridge open`, below) needs to know the
  command that launches it, if it is not on `PATH`: `IJ_NVIM_BRIDGE_IDEA_CMD`, or
  `setup({ idea_cmd = '...' })`.

These are unrelated to attaching the Bridge to an IntelliJ that is already
running, which needs neither: discovery is automatic.

### Keys (LazyVim)

Standard features stay on LazyVim's own keys: `gd`, `grr`, `gI`, `gy`, `K` (goto and hover), `<leader>ca` / `<leader>cA` (code actions, including the quick fixes, generate code and source actions), `<leader>co` (organize imports: LazyVim binds it for any server that offers it), `<leader>cr` (rename), `<leader>cR` (rename file), `<leader>cf` (format), `<leader>uh` (inlay hints). What the Bridge adds that is **not** standard LSP goes where LazyVim keeps that kind of thing:

| Section | Key | Does | Where it exists |
|---|---|---|---|
| code | `<leader>cg` | **Gradle: find a task and run it** (fuzzy: `eas`, `fMT` or `trea` find `findMysteriousTreasure`) | Kotlin, Java and Groovy buffers with an IntelliJ connection |
| | `<leader>cG` | Gradle: the tasks as a hierarchy (project, group, task) | same |
| | `<leader>cb` | Gradle: run the last task again | same |
| | `<leader>cB` | Gradle: stop the running task | same |
| | `<leader>cy` | Copy reference: the fully qualified name of the symbol under the cursor, into the clipboard (`:IjBridge copyref`) | Kotlin and Java buffers with an IntelliJ connection |
| | `<leader>cs` | Gradle: sync (reload the Gradle projects; needed after adding a library) | same |
| | `<leader>ce` | **Run Configuration: find and run it** (Gradle-backed ones only, so far) | same |
| | `<leader>cE` | Run Configuration: run the last one again | same |
| | `<leader>cK` | Run Configuration: stop the running one | same |
| test | `<leader>tg` | Go to the test / the class under test, offering to create one | Kotlin and Java buffers with an IntelliJ connection |
| | `<leader>tn` `tc` `tf` | Run the nearest test / every test in this class / file | same |
| | `<leader>tR` `tx` | Run the last test again / stop the running one | same |
| | `<leader>to` `tl` | Show the results tree and the selected test's log / the raw Gradle log | same |
| file/find | `<leader>fN` | New file from an IntelliJ template (asks kind and name); `fn` is LazyVim's own New File | Kotlin and Java buffers **with an IntelliJ connection** |
| miscellaneous | `<leader>ai` | State of this buffer (attached, indexing, ...) | everywhere |
| | `<leader>ao` | Open this project in IntelliJ, starting it if none serves it | everywhere (it is how to get a connection) |
| | `<leader>ak` | List the keys, and why one is missing here | everywhere |
| | `<leader>all` `alb` `alr` `alv` | Editor log, Brain log, bug report, log level | everywhere |

**A key that needs IntelliJ exists only where IntelliJ is.** It is made, buffer-locally, when the buffer attaches to a Session, only for the filetypes it is for, and removed when the Session goes (and back when it reconnects). In a buffer with no connection, or a file of another kind, it is not there at all: which-key does not offer it and it cannot shadow anything. `:IjBridge keys` (`<leader>ak`) lists what is bound here and says why a key is missing.

`<leader>g` is git and plain `g` is goto in LazyVim; nothing of the Bridge's belongs to either. **LazyVim's AI extras (Claude Code, Avante, Copilot Chat, Sidekick) share `<leader>a`** as "+ai", with `C a b c d e f h m n p q r s t v x` under it. The miscellaneous keys are ones they do not use (`g i j k l o u w y z` are free), and the test suite reads LazyVim's extras and fails if one ever collides. An empty mapping on the prefix, which is how they name their group, does not block them; an existing mapping is never overwritten (skipped, and the log says so); if the prefix itself does something, no miscellaneous key is bound. Change the prefix, or turn keys off, in the `opts` from Setup step 2: `{ prefix = ..., keys = false, sections = false }`.

### Gradle tasks

IntelliJ already knows the project's Gradle tasks; the Bridge lists them without running anything and runs one through IntelliJ's own Gradle integration (the project's settings, JVM and wrapper). `<leader>cg` (or `:IjBridge task`) opens a fuzzy finder: type any letters of the name in order (`eas`, `fMT` for the capitals of `findMysteriousTreasure`, `trea`), move with `<C-n>`/`<C-p>`, `<CR>` runs it. `<leader>cG` (`:IjBridge tasks`) shows the hierarchy, project then group then task, with descriptions; `<Tab>` folds, `<CR>` runs, `a` runs with arguments, `/` opens the finder. The output streams into a buffer at the bottom without taking your window, and ends with `✔ finished`, `✘ failed` or `■ cancelled`. `<leader>cb` runs the last again and `<leader>cB` stops the running one. One task runs at a time. A live status line (what IntelliJ's own Gradle tool window would show, e.g. "Executing task ':compileKotlin'") shows as a `winbar` on the output window while it runs.

### Run Configurations

A developer's own named, saved way to run something (IntelliJ's Run/Debug Configurations dropdown), including ones checked into the project's own `.run` folder and so shared with everyone who clones it - distinct from a raw Gradle task, above. `<leader>ce` opens the same kind of fuzzy finder over every configuration IntelliJ knows about; choosing a Gradle-backed one runs it the same way a Gradle task does (streamed output, a live status, `✔`/`✘`/`■` at the end). **Only Gradle-backed configurations can be run so far** - a plain JVM Application configuration is listed but refuses to run, named as "not runnable yet" in the finder, a real known gap rather than a silent failure. `<leader>cE` runs the last one again, `<leader>cK` stops the running one.

### Format on save

Off by default, since it changes what `:w` means: `setup({ format_on_save = true })` makes every save reformat the buffer in IntelliJ's own code style first (`.editorconfig`, the project's scheme), the same edits `<leader>cf` already computes. Rides the version-ack handshake every save already does (above): no extra round trip, no extra keystroke.

### Several projects, several Neovims, starting IntelliJ

Each buffer is served by the IntelliJ that has *its* project open: with two projects open (a second `idea <root>` opens it in the running IDE), one Neovim holds a Session for each and every buffer goes to its own. Several Neovims may share one IDE; closing one releases only what it had open. `:IjBridge open` (`<leader>ao`) starts the IDE for the current buffer's project when none serves it: it runs `idea <root>` (its command set in Setup, above) detached, so it never locks Neovim's terminal and outlives Neovim, shows `IJ: starting` until the project's Brain appears, and then attaches. It is never started by itself.

### New files and moving files

`:IjBridge new [dir]` asks for a kind (class, interface, enum, ...) and a name and creates the file from IntelliJ's own template, with the right `package` line. Programmatically: `require('ij_bridge').new_file({ dir = ..., name = 'Ticket', template = 'class', language = 'kotlin' })`. Moving or renaming a file with a plugin that sends `workspace/willRenameFiles` (snacks, oil.nvim, neo-tree) updates its `package` line and every import that names it. A file is attached to the IDE by its first write: a buffer for a file that does not exist yet is left alone.

### When something goes wrong

Both halves keep a bounded JSON-lines log, always on, that records what was asked and answered (never your code):

- `:IjBridge log` and `:IjBridge brainlog` open the Editor's and the Brain's.
- `:IjBridge report` gathers versions, state and the tail of both logs into one buffer to paste into a bug report.
- `:IjBridge loglevel <off|info|debug|trace>` changes how much is recorded; `trace` includes payloads, which contain code.

The two logs print the same Session id, so a line in one finds its match in the other. See SPEC.md §15.
