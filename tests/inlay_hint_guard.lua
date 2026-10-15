-- Run with Neovim 0.12.5: nvim --headless -u NONE -l tests/inlay_hint_guard.lua
vim.opt.rtp:prepend(vim.fn.getcwd() .. '/editor')
local api = vim.api
local provider
local original_provider = api.nvim_set_decoration_provider
api.nvim_set_decoration_provider = function(ns, opts)
  if ns == api.nvim_create_namespace('nvim.lsp.inlayhint') then provider = opts.on_win end
  return original_provider(ns, opts)
end
local native = vim.lsp.inlay_hint
api.nvim_set_decoration_provider = original_provider
local buf = api.nvim_get_current_buf()
local pending
local client = {
  id = 42, handlers = {}, offset_encoding = 'utf-16', attached_buffers = { [buf] = true },
  is_stopped = function() return false end,
  request = function(_, _, _, callback)
    pending = callback or vim.lsp.handlers['textDocument/inlayHint']
    return true, 1
  end,
}
local get_client, get_clients = vim.lsp.get_client_by_id, vim.lsp.get_clients
vim.lsp.get_client_by_id = function(id) if id == 42 then return client end end
vim.lsp.get_clients = function() return { client } end
vim.lsp.util.buf_versions[buf] = 1
local ctx = { bufnr = buf, client_id = 42, version = 1 }
local function hint(row, col)
  return { position = { line = row, character = col }, label = 'width:' }
end
local function render()
  provider(nil, api.nvim_get_current_win(), buf, 0, 0)
end
api.nvim_buf_set_lines(buf, 0, -1, false, { 'abcdefghi' })
native.enable(true, { bufnr = buf })
pending(nil, { hint(0, 9) }, ctx)
api.nvim_buf_set_lines(buf, 0, -1, false, { 'x' })
local ok, err = pcall(render)
assert(not ok and tostring(err):find("Invalid 'col'"), tostring(err))
print('Baseline: real native renderer rejects cached column 9 after shortening to x')

local guard = require('ij_bridge.inlay_hints')
guard.on_client(client)
guard.on_attach(client, buf)
api.nvim_buf_set_lines(buf, 0, -1, false, { 'abcdefghi' })
native.enable(true, { bufnr = buf })
pending(nil, { hint(0, 9) }, ctx)
api.nvim_buf_set_lines(buf, 0, -1, false, { 'x' })
render()
assert(#native.get({ bufnr = buf }) == 0, 'cached hints survived edit')
pending(nil, { hint(0, 9) }, ctx)
assert(#native.get({ bufnr = buf }) == 0, 'stale response survived changedtick check')
print('PASS: edit clears cached hints before rendering; delayed response is discarded')

api.nvim_buf_set_lines(buf, 0, -1, false, { 'a😀b' })
vim.lsp.util.buf_versions[buf], ctx.version = 2, 2
client:request('textDocument/inlayHint', {}, nil, buf)
pending(nil, { hint(0, 4), hint(0, 5), hint(1, 0), hint(-1, 0), hint(0, -1), hint(0, 1.5) }, ctx)
local got = native.get({ bufnr = buf })
assert(#got == 1, vim.inspect(got))
assert(got[1].inlay_hint.position.character == 6, 'UTF-16 EOL was not converted to byte EOL')
render()
local ns = api.nvim_create_namespace('nvim.lsp.inlayhint')
local marks = api.nvim_buf_get_extmarks(buf, ns, 0, -1, {})
assert(#marks == 1 and marks[1][3] == 6, vim.inspect(marks))
print('PASS: invalid rows/columns discarded; valid UTF-16 hint retained at byte column 6')
api.nvim_buf_set_lines(buf, 0, -1, false, { '', 'a😀b' })
render()
assert(#native.get({ bufnr = buf }) == 0, 'cached hints survived newline insertion')
vim.lsp.util.buf_versions[buf], ctx.version = 3, 3
client:request('textDocument/inlayHint', {}, nil, buf)
pending(nil, { hint(1, 4) }, ctx)
assert(#native.get({ bufnr = buf }) == 1, 'fresh hints did not return')
assert(native.is_enabled({ bufnr = buf }), 'guard disabled hints')
provider(nil, api.nvim_get_current_win(), buf, 0, 1)
marks = api.nvim_buf_get_extmarks(buf, ns, { 1, 0 }, { 1, -1 }, {})
assert(#marks == 1 and marks[1][2] == 1 and marks[1][3] == 6, vim.inspect(marks))
print('PASS: single newline invalidates cache; fresh hints return and hints stay enabled')
vim.lsp.get_client_by_id, vim.lsp.get_clients = get_client, get_clients
