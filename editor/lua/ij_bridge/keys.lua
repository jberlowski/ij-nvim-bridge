-- Default key bindings for everything the Bridge offers that is not a standard LSP feature.
--
-- Standard features stay on the editor's own keys (`gd`, `grr`, `<leader>ca`, `<leader>cr`, ...):
-- they are ordinary LSP. What is left is the Bridge's own, and it gets one home, under one prefix,
-- so it can be found (which-key shows the group) and never collides with anything else.
--
-- The prefix is `<leader>a`, chosen by the developer so that everything is together. LazyVim's AI
-- extras (Claude Code, Avante, Copilot Chat, Sidekick) use the same prefix as "+ai", with the keys
-- `C a b c d e f h m n p q r s t v x` under it, so the suffixes here are ones they do not use
-- (`g i j k l o u w y z` are free), and `tests/test_keys.py` checks that against LazyVim's own source.
-- An empty mapping on the prefix itself, which is how those extras name their group, is not a clash.
--
--   setup({ prefix = '<leader>a', keys = true })       -- the defaults
--   setup({ keys = false })                            -- bind nothing (the commands remain)
--
-- An existing mapping is never overwritten: it is skipped, and the log says so.
local log = require('ij_bridge.log')

local M = {}

M.defaults = { prefix = '<leader>a', keys = true }

--- The lhs actually bound, by suffix: for `:IjBridge keys` and for tests.
M.bound = {}

local function bridge()
  return require('ij_bridge')
end

--- The Bridge's own actions: { suffix, description, function }. A suffix of more than one key opens a group
--- named in `M.groups`.
local function actions()
  return {
    { 'i', 'State of this buffer', function() bridge().show_status() end },
    { 'o', 'Open the project in IntelliJ (start it if none is)', function() bridge().open_ide() end },
    { 'g', 'New file from a template', function() bridge().new_file_interactive() end },
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

--- A real mapping: one that does something. An empty one is how a plugin names a which-key group.
local function taken(lhs)
  local m = vim.fn.maparg(lhs, 'n', false, true)
  return next(m) ~= nil and ((m.rhs or '') ~= '' or m.callback ~= nil)
end

--- Whether which-key already has a name for this group.
local function group_named(wk_config, lhs)
  for _, m in ipairs(wk_config and wk_config.mappings or {}) do
    if m.group and (m.lhs or m[1]) == lhs and m.desc then
      return true
    end
  end
  return false
end

--- @param opts? { prefix?: string, keys?: boolean }
function M.setup(opts)
  opts = vim.tbl_extend('force', M.defaults, opts or {})
  M.bound = {}
  if not opts.keys then
    return
  end
  local prefix = opts.prefix

  -- The prefix itself must not do something: such a mapping would swallow every key below it.
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

  -- which-key shows the groups' names; without it the descriptions still show. A group that already has a
  -- name (the AI extras call `<leader>a` "+ai") keeps it: ours are told apart by "IntelliJ: " in each description.
  local ok, wk = pcall(require, 'which-key')
  if ok and wk.add then
    local _, config = pcall(require, 'which-key.config')
    local specs = {}
    for suffix, name in pairs(M.groups) do
      local lhs = prefix .. suffix
      if not group_named(config, lhs) then
        table.insert(specs, { lhs, group = name })
      end
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
