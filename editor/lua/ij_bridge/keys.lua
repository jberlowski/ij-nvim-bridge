-- Default key bindings for everything the Bridge offers that is not a standard LSP feature.
--
-- Standard features stay on the editor's own keys (`gd`, `grr`, `<leader>ca`, `<leader>cr`, ...):
-- they are ordinary LSP. What is left is the Bridge's own, and it gets one home, under one prefix,
-- so it can be found (which-key shows the group) and never collides with anything else.
--
-- The prefix is `<leader>i`, for IntelliJ. LazyVim's AI extras (Claude Code, Avante, Copilot Chat,
-- Sidekick) all claim `<leader>a` as "+ai" and use `aa`, `ac`, `an`, `as`, so `a` is taken;
-- across LazyVim's core and all its extras the letters `i`, `j`, `k`, `v`, `y` and `z` are never used.
--
--   setup({ prefix = '<leader>i', keys = true })       -- the defaults
--   setup({ keys = false })                            -- bind nothing (the commands remain)
--
-- An existing mapping is never overwritten: it is skipped, and the log says so.
local log = require('ij_bridge.log')

local M = {}

M.defaults = { prefix = '<leader>i', keys = true }

--- The lhs actually bound, by suffix: for `:IjBridge keys` and for tests.
M.bound = {}

local function bridge()
  return require('ij_bridge')
end

--- The Bridge's own actions: { suffix, description, function }. A suffix of more than one key opens a group
--- named in `M.groups`.
local function actions()
  return {
    { 's', 'State of this buffer', function() bridge().show_status() end },
    { 'n', 'New file from a template', function() bridge().new_file_interactive() end },
    { 'll', 'Open the Editor log', function() vim.cmd('IjBridge log') end },
    { 'lb', 'Open the Brain log', function() vim.cmd('IjBridge brainlog') end },
    { 'lr', 'Gather a bug report', function() vim.cmd('IjBridge report') end },
    {
      'lv',
      'Set the log level',
      function()
        vim.ui.select({ 'off', 'info', 'debug', 'trace' }, { prompt = 'Log level (trace records code)' }, function(level)
          if level then
            bridge().set_log_level(level)
          end
        end)
      end,
    },
  }
end

M.groups = { [''] = 'IntelliJ', l = 'logs' }

local function taken(lhs)
  return vim.fn.maparg(lhs, 'n') ~= ''
end

--- @param opts? { prefix?: string, keys?: boolean }
function M.setup(opts)
  opts = vim.tbl_extend('force', M.defaults, opts or {})
  M.bound = {}
  if not opts.keys then
    return
  end
  local prefix = opts.prefix

  -- The prefix itself must be free: a mapping on it would swallow every key below it.
  if taken(prefix) then
    log.warn('keys_skipped', { prefix = prefix, why = 'the prefix is already a mapping' })
    vim.notify(('ij-bridge: %s is already mapped; no keys bound (set prefix, or keys = false)'):format(prefix),
      vim.log.levels.WARN)
    return
  end

  for _, action in ipairs(actions()) do
    local suffix, desc, run = action[1], action[2], action[3]
    local lhs = prefix .. suffix
    if taken(lhs) then
      log.warn('key_skipped', { lhs = lhs, why = 'already mapped', for_ = desc })
    else
      vim.keymap.set('n', lhs, run, { desc = 'IntelliJ: ' .. desc, silent = true })
      M.bound[suffix] = lhs
    end
  end

  -- which-key shows the groups' names; without it the descriptions still show.
  local ok, wk = pcall(require, 'which-key')
  if ok and wk.add then
    local specs = {}
    for suffix, name in pairs(M.groups) do
      table.insert(specs, { prefix .. suffix, group = name })
    end
    pcall(wk.add, specs)
  end
end

--- What is bound, for `:IjBridge keys`.
function M.describe()
  local out = {}
  for _, action in ipairs(actions()) do
    local lhs = M.bound[action[1]]
    table.insert(out, ('%-12s %s'):format(lhs or '(not bound)', action[2]))
  end
  return out
end

return M
