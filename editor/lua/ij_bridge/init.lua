-- The Editor half of the Bridge (SPEC.md §4.1): connects, asks, owns the text.
--
-- A Session is a plain LSP client over the Brain's unix socket, so Neovim's own
-- machinery does the heavy lifting: incremental didChange, didSave, and the
-- save handshake (`textDocument/willSaveWaitUntil` is awaited before a write,
-- after pending changes are flushed - SPEC.md §5.4). What this file adds is the
-- Bridge's policy: when to be Dormant, and which buffers are Mirrored.
local registry = require('ij_bridge.registry')

local M = {}

M.name = 'ij-bridge'

--- How many `$/ij/status` notifications have arrived: each clears the completion cache.
M.status_events = 0

--- The Brain's last announced state per Session (SPEC.md §8), by client id.
M.states = {}

--- Buffers that *should* be Mirrored: attached and not released. This is what a
--- reconnect must restore, and how a buffer whose Session died is told apart from
--- one that was never attached.
M.attached = {}

--- Project Roots whose Session died unexpectedly and are being retried:
--- root -> { attempt = n }.
M.offline = {}

local leaving = false

--- A short trail of connection events, for `:IjBridge` and for tests to print
--- when a reconnect misbehaves. Bounded: it must never grow.
M.events = {}
local function note(what)
  table.insert(M.events, ('%.1f %s'):format(vim.uv.hrtime() / 1e9 % 10000, what))
  if #M.events > 60 then
    table.remove(M.events, 1)
  end
end

local reasons = {
  import = 'importing project',
  indexing = 'indexing',
  model = 'loading project model',
}

--- Called for every `$/ij/status` notification.
function M.on_status(client_id, params)
  M.states[client_id] = params
  M.status_events = M.status_events + 1
  -- What IntelliJ would have said may change when it stops or finishes indexing.
  require('ij_bridge.blink').clear_cache()
  vim.api.nvim_exec_autocmds('User', { pattern = 'IjBridgeStatus', modeline = false, data = params })
  vim.cmd.redrawstatus()
end

--- The Brain's state for the buffer's Session, or nil when Dormant / not attached.
function M.status(buf)
  local client = M.client(buf or 0)
  return client and M.states[client.id] or nil
end

--- For a statusline. Empty when Dormant, so it costs nothing outside a project.
--- Indexing is always visible and never silent: the developer must be able to see
--- why completion and diagnostics have gone quiet.
---   lualine: sections = { lualine_x = { require('ij_bridge').statusline } }
function M.statusline()
  local status = M.status(0)
  if not status then
    -- A buffer that should be Mirrored but has no Session: the Brain went away.
    local buf = vim.api.nvim_get_current_buf()
    return (M.attached[buf] and not M.client(buf)) and 'IJ: disconnected' or ''
  end
  if status.state == 'Ready' then
    return 'IJ'
  end
  return 'IJ: ' .. (reasons[status.reason] or 'not ready')
end

local function buftype_ok(buf)
  return vim.api.nvim_buf_is_valid(buf)
    and vim.bo[buf].buftype == ''
    and vim.api.nvim_buf_get_name(buf) ~= ''
end

--- The Session serving `buf`, or nil when it is Dormant or not attached.
function M.client(buf)
  return vim.lsp.get_clients({ bufnr = buf or 0, name = M.name })[1]
end

local function capabilities()
  local caps = vim.lsp.protocol.make_client_capabilities()
  local ok, blink = pcall(require, 'blink.cmp')
  if ok and blink.get_lsp_capabilities then
    caps = blink.get_lsp_capabilities(caps)
  end
  return caps
end

--- A Session for `root` that is up and not shutting down, if there is one.
local function live_client(root)
  for _, client in ipairs(vim.lsp.get_clients({ name = M.name })) do
    if client.config.root_dir == root and not client:is_stopped() then
      return client
    end
  end
end

--- Connect `buf` to the Brain serving it.
local function connect(buf, entry)
  note('connect buf ' .. buf)
  local id = vim.lsp.start({
    name = M.name,
    cmd = vim.lsp.rpc.connect(entry.sock),
    root_dir = entry.root,
    capabilities = capabilities(),
    handlers = {
      ['$/ij/completionItems'] = function(err, params)
        require('ij_bridge.blink').on_items(err, params)
      end,
      ['$/ij/status'] = function(_, params, ctx)
        if params then
          require('ij_bridge').on_status(ctx.client_id, params)
        end
      end,
    },
    on_exit = function(code, signal, client_id)
      note(('exit client %d code=%s signal=%s'):format(client_id, tostring(code), tostring(signal)))
      M.states[client_id] = nil
      if not leaving then
        vim.schedule(function()
          M.lost(entry.root)
        end)
      end
    end,
  }, { bufnr = buf })
  if id then
    M.attached[buf] = true
    -- vim.lsp.start reuses a running Session, and can hand the buffer to one that
    -- is already on its way out. Confirm it took, and repair it if it did not.
    vim.defer_fn(function()
      if M.attached[buf] and vim.api.nvim_buf_is_valid(buf) and not M.client(buf) then
        local client = live_client(entry.root)
        if client then
          vim.lsp.buf_attach_client(buf, client.id)
        end
      end
    end, 300)
  end
  return id
end

--- Ensure `buf` is Mirrored, if it belongs to a Project Root at all.
--- Returns the client id, or nil for Dormant (or while the Brain is away: the
--- reconnect loop owns that case).
function M.attach(buf)
  if not buftype_ok(buf) then
    return nil
  end
  local entry = registry.resolve(vim.api.nvim_buf_get_name(buf))
  if not entry then
    return nil -- Dormant: no message, no latency, no surprises
  end
  if M.offline[entry.root] then
    if live_client(entry.root) then
      M.offline[entry.root] = nil -- a Session is up after all: not offline
    else
      M.attached[buf] = true -- remember it, so the reconnect brings it back
      return nil
    end
  end
  return connect(buf, entry)
end

-- ----------------------------------------------------------------- reconnect
-- The Brain can go away: IntelliJ quits or restarts, the socket drops. Neovim
-- must keep working (`:w` must never fail or block because of the Bridge), say
-- plainly that it is disconnected, and come back by itself - re-sending every
-- unsaved buffer, since the new Brain knows nothing.

local function under_root(buf, root)
  local name = vim.api.nvim_buf_get_name(buf)
  return name == root or name:sub(1, #root + 1) == root .. '/'
end

local function buffers_of(root)
  local out = {}
  for buf in pairs(M.attached) do
    -- Loaded, not merely valid: a hidden or unloaded buffer cannot take a Session,
    -- and a Session with no buffer exits at once - which would read as another
    -- lost connection, forever.
    if vim.api.nvim_buf_is_valid(buf) and vim.api.nvim_buf_is_loaded(buf) and under_root(buf, root) then
      table.insert(out, buf)
    else
      M.attached[buf] = nil
    end
  end
  return out
end

local function retry_in(root, attempt)
  -- 0.5 s, 1 s, 2 s, 4 s, 8 s, then every 10 s.
  local delay = math.min(10000, 500 * 2 ^ attempt)
  vim.defer_fn(function()
    M.reconnect(root)
  end, delay)
end

--- A Session for `root` ended without being asked to. Idempotent: several
--- buffers share one Session, and every exit reports it.
function M.lost(root)
  note('lost ' .. root .. (live_client(root) and ' (live client exists)' or ''))
  if leaving or M.offline[root] or live_client(root) then
    return -- already known, or another Session for the root replaced the one that ended
  end
  if #buffers_of(root) == 0 then
    return -- nothing was Mirrored, nothing to restore
  end
  M.offline[root] = { attempt = 0 }
  vim.cmd.redrawstatus()
  vim.api.nvim_exec_autocmds('User', { pattern = 'IjBridgeStatus', modeline = false,
    data = { state = 'Disconnected' } })
  retry_in(root, 0)
end

function M.reconnect(root)
  note('reconnect ' .. root .. ' attempt ' .. tostring((M.offline[root] or {}).attempt))
  local state = M.offline[root]
  if not state or leaving then
    return
  end
  local bufs = buffers_of(root)
  if #bufs == 0 then
    M.offline[root] = nil
    return
  end
  local entry = registry.resolve(vim.api.nvim_buf_get_name(bufs[1]))
  if entry and entry.root == root then
    -- Stay marked offline while connecting: a refused connection exits at once and
    -- calls lost() again, which must not start a second retry loop.
    for _, buf in ipairs(bufs) do
      connect(buf, entry)
    end
    vim.defer_fn(function()
      for _, client in ipairs(vim.lsp.get_clients({ name = M.name })) do
        if client.config.root_dir == root and not client:is_stopped() then
          M.offline[root] = nil -- it held
          require('ij_bridge.blink').clear_cache()
          vim.cmd.redrawstatus()
          return
        end
      end
      state.attempt = state.attempt + 1
      retry_in(root, state.attempt)
    end, 1500)
  else
    state.attempt = state.attempt + 1
    retry_in(root, state.attempt)
  end
end

--- Tell the Brain which buffer is active (FEATURES.md §9). Its Mirror becomes the
--- selected tab in the IDE, which is what makes the daemon analyse it. A buffer
--- attached just now needs no message: its didOpen selects it.
local function focus(buf)
  local client = M.client(buf)
  if client then
    client:notify('$/ij/focus', { textDocument = { uri = vim.uri_from_bufnr(buf) } })
  end
end

--- Release the Mirror for `buf`: didClose is sent by detaching.
function M.detach(buf)
  M.attached[buf] = nil
  for _, client in ipairs(vim.lsp.get_clients({ bufnr = buf, name = M.name })) do
    vim.lsp.buf_detach_client(buf, client.id)
  end
end

--- SPEC.md §5.2: Mirror Set = { active buffer } U { every buffer with unsaved
--- changes }. Unsaved buffers stay Mirrored because the Brain would otherwise
--- answer from a stale disk copy - "cannot resolve" for a method just added to
--- the other file. A buffer that is left *and clean* is released.
local function release_if_clean(buf)
  if buftype_ok(buf) and not vim.bo[buf].modified then
    M.detach(buf)
  end
end

function M.setup(_)
  local group = vim.api.nvim_create_augroup('IjBridge', { clear = true })

  vim.api.nvim_create_autocmd('BufEnter', {
    group = group,
    callback = function(args)
      local was_attached = M.client(args.buf) ~= nil
      M.attach(args.buf)
      if was_attached then
        focus(args.buf)
      end
    end,
  })
  vim.api.nvim_create_autocmd('BufLeave', {
    group = group,
    callback = function(args)
      release_if_clean(args.buf)
    end,
  })
  -- A buffer saved while it is not the active one has just become clean.
  vim.api.nvim_create_autocmd('BufWritePost', {
    group = group,
    callback = function(args)
      if args.buf ~= vim.api.nvim_get_current_buf() then
        release_if_clean(args.buf)
      end
    end,
  })

  vim.api.nvim_create_autocmd('VimLeavePre', {
    group = group,
    callback = function()
      leaving = true -- quitting is not a lost connection
    end,
  })
  vim.api.nvim_create_autocmd({ 'BufWipeout', 'BufUnload' }, {
    group = group,
    callback = function(args)
      M.attached[args.buf] = nil
    end,
  })

  vim.api.nvim_create_user_command('IjBridge', function()
    local buf = vim.api.nvim_get_current_buf()
    local entry = registry.resolve(vim.api.nvim_buf_get_name(buf))
    if not entry and M.attached[buf] then
      print('ij-bridge: disconnected; the Brain is not running, retrying')
    elseif not entry then
      print('ij-bridge: Dormant (no Project Root matches this buffer)')
    elseif M.client(buf) then
      local status = M.status(buf)
      local state = not status and 'state not reported yet'
        or status.state == 'Ready' and 'ready'
        or (reasons[status.reason] or 'not ready')
      print(('ij-bridge: attached to %s (%s), %s'):format(entry.root, entry.ide or '?', state))
    elseif M.attached[buf] then
      print(('ij-bridge: disconnected from %s; reconnecting'):format(entry.root or '?'))
    else
      print(('ij-bridge: %s is serving this buffer, but it is not attached'):format(entry.root))
    end
  end, { desc = 'Show whether this buffer is served by an IntelliJ Brain' })

  M.attach(vim.api.nvim_get_current_buf())
end

return M
