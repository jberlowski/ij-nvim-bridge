-- Default key bindings for what the Bridge offers that is not a standard LSP feature.
--
-- Standard features stay on the editor's own keys (`gd`, `grr`, `gI`, `<leader>ca`, `<leader>cr`,
-- `<leader>cf`, ...): they are ordinary LSP. What is left goes where LazyVim would look for it, by section:
--
--   <leader>c   code           Gradle tasks: find and run (`cg`), the hierarchy (`cG`), run the last again (`cb`), stop (`cB`), sync (`cs`)
--                              Run Configurations: find and run (`ce`), run the last again (`cE`), stop (`cK`)
--                              copy reference (`cy`): the fully qualified name of the symbol under the cursor
--   <leader>f   file/find      new file from an IntelliJ template (`fN`; `fn` is LazyVim's own New File)
--   <leader>t   test           go to the test (`tg`); run nearest/class/file, repeat, stop (`tn`, `tc`, `tf`, `tR`, `tx`), the results tree and the raw log (`to`, `tl`)
--   <leader>a   miscellaneous  state, opening the project, the keys, the logs
--
-- Organize imports is not here: `source.organizeImports` is a standard code action, and LazyVim already binds
-- it (`<leader>co`) in every buffer whose server offers it, ours included. The same goes for rename (`cr`),
-- format (`cf`), code actions (`ca`, `cA`) and file rename (`cR`): they need nothing of ours.
--
-- (`<leader>g` is git, and `g` alone is goto: nothing of ours belongs to either.)
--
-- **A binding that needs IntelliJ exists only where IntelliJ is**: it is made, buffer-locally, when a buffer
-- attaches to a Session (`LspAttach`), only for the filetypes it is for, and removed when the Session goes
-- (`LspDetach`). In a buffer with no IntelliJ, or in a file of another kind, the key is not there at all, so
-- it is neither offered by which-key nor able to shadow something. The miscellaneous keys under `<leader>a`
-- are global: they must work with no connection (opening the project is how to get one).
--
-- LazyVim's AI extras use `<leader>a` too, as "+ai", with `C a b c d e f h m n p q r s t v x` under it, so the
-- keys here are ones they do not use (`tests/test_keys.py` reads their source and checks). An empty mapping on
-- the prefix, which is how they name their group, is not a clash. An existing mapping is never overwritten:
-- it is skipped, and the log says so.
--
--   setup({ prefix = '<leader>a', keys = true, sections = true })   -- the defaults
--   setup({ keys = false })                                          -- bind nothing (the commands remain)
--   setup({ sections = false })                                      -- only the miscellaneous keys
local log = require('ij_bridge.log')

local M = {}

M.defaults = { prefix = '<leader>a', keys = true, sections = true }

--- The filetypes IntelliJ serves. Java and Kotlin, including Gradle's Kotlin scripts (filetype `kotlin`).
M.jvm = { 'kotlin', 'java' }

--- Where Gradle tasks make sense: the languages of a Gradle project, and Groovy build scripts.
M.gradle = { 'kotlin', 'java', 'groovy' }

--- The miscellaneous keys' lhs actually bound, by suffix: for tests and `:IjBridge keys`.
M.bound = {}

--- The buffer-local keys bound in each buffer now: buf -> { lhs, ... }.
M.in_buffer = {}

local function bridge()
  return require('ij_bridge')
end

--- Everything bound. `misc`: under the prefix, global. Otherwise `lhs` is absolute, `when` says where it is
--- (`connected`: only in a buffer with a live Session) and `ft` for which filetypes.
local function actions()
  return {
    -- miscellaneous
    { misc = 'i', desc = 'State of this buffer', run = function() bridge().show_status() end },
    { misc = 'o', desc = 'Open the project in IntelliJ (start it if none is)', run = function() bridge().open_ide() end },
    { misc = 'F', desc = 'Toggle caret following (the cursor and IntelliJ\'s caret move together)',
      run = function() local c = require('ij_bridge.caret'); vim.cmd('IjBridge follow ' .. (c.enabled and 'off' or 'on')) end },
    { misc = 'k', desc = 'List the keys the Bridge binds', run = function() vim.cmd('IjBridge keys') end },
    { misc = 'll', desc = 'Open the Editor log', run = function() vim.cmd('IjBridge log') end },
    { misc = 'lb', desc = 'Open the Brain log', run = function() vim.cmd('IjBridge brainlog') end },
    { misc = 'lr', desc = 'Gather a bug report', run = function() vim.cmd('IjBridge report') end },
    {
      misc = 'lv',
      desc = 'Set the log level',
      run = function()
        vim.ui.select({ 'off', 'info', 'debug', 'trace' }, { prompt = 'Log level (trace records code)' }, function(level)
          if level then
            bridge().set_log_level(level)
          end
        end)
      end,
    },
    { lhs = '<leader>cy', desc = 'Copy reference: the fully qualified name of the symbol', run = function() bridge().copy_reference() end,
      when = 'connected', ft = M.jvm },
    -- code: Gradle tasks
    { lhs = '<leader>cg', desc = 'Gradle: find a task and run it', run = function() require('ij_bridge.tasks').pick() end,
      when = 'connected', ft = M.gradle },
    { lhs = '<leader>cG', desc = 'Gradle: the tasks as a hierarchy', run = function() require('ij_bridge.tasks').tree() end,
      when = 'connected', ft = M.gradle },
    { lhs = '<leader>cb', desc = 'Gradle: run the last task again', run = function() require('ij_bridge.tasks').repeat_last() end,
      when = 'connected', ft = M.gradle },
    { lhs = '<leader>cB', desc = 'Gradle: stop the running task', run = function() require('ij_bridge.tasks').stop() end,
      when = 'connected', ft = M.gradle },
    { lhs = '<leader>cs', desc = 'Gradle: sync (reload the Gradle projects)', run = function() require('ij_bridge.tasks').sync() end,
      when = 'connected', ft = M.gradle },
    -- code: Run Configurations
    { lhs = '<leader>ce', desc = 'Run Configuration: find and run it', run = function() require('ij_bridge.runconfigs').pick() end,
      when = 'connected', ft = M.gradle },
    { lhs = '<leader>cE', desc = 'Run Configuration: run the last one again', run = function() require('ij_bridge.runconfigs').repeat_last() end,
      when = 'connected', ft = M.gradle },
    { lhs = '<leader>cK', desc = 'Run Configuration: stop the running one', run = function() require('ij_bridge.runconfigs').stop() end,
      when = 'connected', ft = M.gradle },
    -- file
    { lhs = '<leader>fN', desc = 'New file from an IntelliJ template', run = function() bridge().new_file_interactive() end,
      when = 'connected', ft = M.jvm },
    -- test
    { lhs = '<leader>tg', desc = 'Go to the test / the class under test', run = function() require('ij_bridge.test_nav').go() end,
      when = 'connected', ft = M.jvm },
    { lhs = '<leader>tn', desc = 'Run the nearest test', run = function() require('ij_bridge.test_run').run_nearest() end,
      when = 'connected', ft = M.jvm },
    { lhs = '<leader>tc', desc = 'Run every test in this class', run = function() require('ij_bridge.test_run').run_class() end,
      when = 'connected', ft = M.jvm },
    { lhs = '<leader>tf', desc = 'Run every test in this file', run = function() require('ij_bridge.test_run').run_file() end,
      when = 'connected', ft = M.jvm },
    { lhs = '<leader>tR', desc = 'Run the last test again', run = function() require('ij_bridge.test_run').repeat_last() end,
      when = 'connected', ft = M.jvm },
    { lhs = '<leader>to', desc = 'Show the test results (the tree and the selected test\'s log)',
      run = function() require('ij_bridge.test_results').show() end, when = 'connected', ft = M.jvm },
    { lhs = '<leader>tl', desc = 'Show the raw Gradle log of the last test run',
      run = function() require('ij_bridge.test_run').show_raw_log() end, when = 'connected', ft = M.jvm },
    { lhs = '<leader>tx', desc = 'Stop the running test', run = function() require('ij_bridge.test_run').cancel() end,
      when = 'connected', ft = M.jvm },
  }
end

M.groups = { [''] = 'IntelliJ', l = 'logs' }

--- A real mapping: one that does something. An empty one is how a plugin names a which-key group. Asked in
--- `buf` when given, where its buffer-local mappings count as well as the global ones.
local function taken(lhs, buf)
  local function ask()
    return vim.fn.maparg(lhs, 'n', false, true)
  end
  local m = (buf and vim.api.nvim_buf_is_valid(buf)) and vim.api.nvim_buf_call(buf, ask) or ask()
  if next(m) == nil then
    return false
  end
  return (m.rhs or '') ~= '' or m.callback ~= nil
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

local function matches(action, buf)
  if not action.ft then
    return true
  end
  return vim.tbl_contains(action.ft, vim.bo[buf].filetype)
end

--- The keys that need IntelliJ, for one buffer: made when it attaches, again if its filetype changes.
local function bind_buffer(buf)
  if not vim.api.nvim_buf_is_valid(buf) then
    return
  end
  M.unbind_buffer(buf)
  local bound = {}
  for _, action in ipairs(actions()) do
    if action.when == 'connected' and matches(action, buf) then
      if taken(action.lhs, buf) then
        log.warn('key_skipped', { lhs = action.lhs, why = 'already mapped', for_ = action.desc, buf = buf })
      else
        vim.keymap.set('n', action.lhs, action.run, { buffer = buf, desc = 'IntelliJ: ' .. action.desc, silent = true })
        table.insert(bound, action.lhs)
      end
    end
  end
  M.in_buffer[buf] = bound
  if #bound > 0 then
    log.debug('keys_bound', { buf = buf, keys = bound })
  end
end

function M.unbind_buffer(buf)
  for _, lhs in ipairs(M.in_buffer[buf] or {}) do
    -- Only ours: a developer's own mapping put there since is not ours to remove.
    local m = vim.api.nvim_buf_call(buf, function() return vim.fn.maparg(lhs, 'n', false, true) end)
    if m.desc and vim.startswith(m.desc, 'IntelliJ: ') then
      pcall(vim.keymap.del, 'n', lhs, { buffer = buf })
    end
  end
  M.in_buffer[buf] = nil
end

local function ours(client_id)
  local client = client_id and vim.lsp.get_client_by_id(client_id)
  return client ~= nil and client.name == 'ij-bridge'
end

--- @param opts? { prefix?: string, keys?: boolean, sections?: boolean }
function M.setup(opts)
  opts = vim.tbl_extend('force', M.defaults, opts or {})
  local previous = M.bound
  M.bound = {}
  local group = vim.api.nvim_create_augroup('IjBridgeKeys', { clear = true })
  for buf in pairs(M.in_buffer) do
    M.unbind_buffer(buf)
  end
  -- What an earlier call bound goes first (only ours), so that a reload, or `keys = false`, leaves no stale key.
  for _, lhs in pairs(previous) do
    local m = vim.fn.maparg(lhs, 'n', false, true)
    if m.desc and vim.startswith(m.desc, 'IntelliJ: ') then
      pcall(vim.keymap.del, 'n', lhs)
    end
  end
  if not opts.keys then
    return
  end
  local prefix = opts.prefix

  -- The prefix itself must not do something: such a mapping would swallow every key below it.
  if taken(prefix) then
    log.warn('keys_skipped', { prefix = prefix, why = 'the prefix is already a mapping' })
    vim.notify(('ij-bridge: %s is already mapped; no miscellaneous keys bound (set prefix, or keys = false)'):format(prefix),
      vim.log.levels.WARN)
  else
    for _, action in ipairs(actions()) do
      if action.misc then
        local lhs = prefix .. action.misc
        if taken(lhs) then
          log.warn('key_skipped', { lhs = lhs, why = 'already mapped', for_ = action.desc })
        else
          vim.keymap.set('n', lhs, action.run, { desc = 'IntelliJ: ' .. action.desc, silent = true })
          M.bound[action.misc] = lhs
        end
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

  if opts.sections then
    vim.api.nvim_create_autocmd('LspAttach', {
      group = group,
      callback = function(args)
        if ours(args.data and args.data.client_id) then
          bind_buffer(args.buf)
        end
      end,
    })
    vim.api.nvim_create_autocmd('LspDetach', {
      group = group,
      callback = function(args)
        if ours(args.data and args.data.client_id) then
          M.unbind_buffer(args.buf)
        end
      end,
    })
    -- A buffer whose filetype is settled after it attached.
    vim.api.nvim_create_autocmd('FileType', {
      group = group,
      callback = function(args)
        if bridge().client(args.buf) then
          bind_buffer(args.buf)
        end
      end,
    })
    -- Buffers already attached when this ran.
    for _, buf in ipairs(vim.api.nvim_list_bufs()) do
      if vim.api.nvim_buf_is_loaded(buf) and bridge().client(buf) then
        bind_buffer(buf)
      end
    end
  end
end

--- What is bound, for `:IjBridge keys`: the global keys, then what this buffer has and what it lacks, and why.
function M.describe()
  local out = { 'Miscellaneous (global):' }
  for _, action in ipairs(actions()) do
    if action.misc then
      table.insert(out, ('  %-14s %s'):format(M.bound[action.misc] or '(not bound)', action.desc))
    end
  end
  local buf = vim.api.nvim_get_current_buf()
  local connected = bridge().client(buf) ~= nil
  table.insert(out, ('In this buffer (%s, %s):'):format(vim.bo[buf].filetype ~= '' and vim.bo[buf].filetype or 'no filetype',
    connected and 'connected to IntelliJ' or 'no IntelliJ connection'))
  for _, action in ipairs(actions()) do
    if action.lhs then
      local here = vim.tbl_contains(M.in_buffer[buf] or {}, action.lhs)
      local why = here and '' or (not connected and '  (needs an IntelliJ connection)') or '  (for ' .. table.concat(action.ft or {}, ', ') .. ' files)'
      table.insert(out, ('  %-14s %s%s'):format(here and action.lhs or '-', action.desc, why))
    end
  end
  return out
end

return M
