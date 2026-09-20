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
