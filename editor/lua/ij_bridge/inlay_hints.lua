-- Keep Bridge hints tied to the Editor snapshot that requested them. Neovim's
-- document version can lag behind a buffer edit until didChange is flushed.
local M = {}
local method = 'textDocument/inlayHint'

function M.on_client(client)
  local request = client.request
  client.request = function(self, name, params, handler, bufnr)
    if name ~= method then
      return request(self, name, params, handler, bufnr)
    end
    bufnr = bufnr or vim.api.nvim_get_current_buf()
    if bufnr == 0 then bufnr = vim.api.nvim_get_current_buf() end
    local tick = vim.api.nvim_buf_get_changedtick(bufnr)
    local receive = handler or self.handlers[method] or vim.lsp.handlers[method]
    return request(self, name, params, function(err, hints, ctx, config)
      if not vim.api.nvim_buf_is_loaded(bufnr)
        or vim.api.nvim_buf_get_changedtick(bufnr) ~= tick then
        return
      end
      if err or not hints then return receive(err, hints, ctx, config) end
      local lines = vim.api.nvim_buf_get_lines(bufnr, 0, -1, false)
      local valid = {}
      for _, hint in ipairs(hints) do
        local p = hint.position
        if p and type(p.line) == 'number' and p.line >= 0 and p.line % 1 == 0
          and type(p.character) == 'number' and p.character >= 0 and p.character % 1 == 0
          and lines[p.line + 1] then
          -- Strict conversion rejects columns beyond EOL instead of clamping.
          local ok = pcall(vim.str_byteindex, lines[p.line + 1], self.offset_encoding, p.character, true)
          if ok then valid[#valid + 1] = hint end
        end
      end
      if #valid ~= #hints then
        require('ij_bridge.log').warn('inlay_invalid_positions', { buf = bufnr, dropped = #hints - #valid })
      end
      return receive(nil, valid, ctx, config)
    end, bufnr)
  end
end

function M.on_attach(client, bufnr)
  vim.api.nvim_buf_attach(bufnr, false, {
    on_lines = function()
      if client:is_stopped() or not client.attached_buffers[bufnr] then return true end
      if vim.lsp.inlay_hint.is_enabled({ bufnr = bufnr }) then
        -- Empty only this client's cached results synchronously, before the
        -- next redraw. Leave enabled state and other clients' hints alone.
        vim.lsp.handlers[method](nil, {}, {
          bufnr = bufnr, client_id = client.id,
          version = vim.lsp.util.buf_versions[bufnr],
        })
      end
    end,
  })
end

return M
