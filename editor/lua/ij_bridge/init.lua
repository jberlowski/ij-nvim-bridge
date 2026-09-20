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

--- Ensure `buf` is Mirrored, if it belongs to a Project Root at all.
--- Returns the client id, or nil for Dormant.
function M.attach(buf)
  if not buftype_ok(buf) then
    return nil
  end
  local entry = registry.resolve(vim.api.nvim_buf_get_name(buf))
  if not entry then
    return nil -- Dormant: no message, no latency, no surprises
  end
  return vim.lsp.start({
    name = M.name,
    cmd = vim.lsp.rpc.connect(entry.sock),
    root_dir = entry.root,
    capabilities = capabilities(),
    handlers = {
      ['$/ij/completionItems'] = function(err, params)
        require('ij_bridge.blink').on_items(err, params)
      end,
    },
  }, { bufnr = buf })
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

  vim.api.nvim_create_user_command('IjBridge', function()
    local buf = vim.api.nvim_get_current_buf()
    local entry = registry.resolve(vim.api.nvim_buf_get_name(buf))
    if not entry then
      print('ij-bridge: Dormant (no Project Root matches this buffer)')
    elseif M.client(buf) then
      print(('ij-bridge: attached to %s (%s)'):format(entry.root, entry.ide or '?'))
    else
      print(('ij-bridge: %s is serving this buffer, but it is not attached'):format(entry.root))
    end
  end, { desc = 'Show whether this buffer is served by an IntelliJ Brain' })

  M.attach(vim.api.nvim_get_current_buf())
end

return M
