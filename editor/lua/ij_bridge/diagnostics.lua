-- The Brain sends every diagnostic IntelliJ's daemon produced, including the ones IntelliJ itself
-- draws faintly or not at all ("No highlighting, only fix", "Consideration": LSP severity 4). What
-- to show is the Editor's choice: `min_severity` hides everything less severe than it. The Brain's
-- answer is kept as it came, so lowering the level brings the hidden ones back without a new pass.
local M = {}

--- The least severe LSP DiagnosticSeverity each level still shows (1 error .. 4 hint).
local LEVELS = { error = 1, warn = 2, info = 3, hint = 4 }

local level = 'hint' -- everything, as sent
local raw = {} -- [client_id][uri] = the params of the last publishDiagnostics

function M.level()
  return level
end

local function show(err, params, ctx)
  local limit = LEVELS[level]
  local kept = vim.tbl_filter(function(d)
    return (d.severity or 1) <= limit
  end, params.diagnostics or {})
  vim.lsp.diagnostic.on_publish_diagnostics(err, vim.tbl_extend('force', params, { diagnostics = kept }), ctx)
end

--- The `textDocument/publishDiagnostics` handler for a Session.
function M.on_publish(err, params, ctx)
  if not params then
    return
  end
  if params.uri then
    raw[ctx.client_id] = raw[ctx.client_id] or {}
    raw[ctx.client_id][params.uri] = #(params.diagnostics or {}) > 0 and params or nil
  end
  show(err, params, ctx)
end

--- What the Brain sent for the current buffer, by IntelliJ's own severity: for finding out what a diagnostic
--- Neovim shows but IntelliJ does not really is. Lines, most severe first.
function M.report(buf)
  buf = buf or vim.api.nvim_get_current_buf()
  local uri = vim.uri_from_bufnr(buf)
  local rows = {}
  for _, uris in pairs(raw) do
    for u, params in pairs(uris) do
      if u == uri then
        for _, d in ipairs(params.diagnostics or {}) do
          local ij = d.data or {}
          table.insert(rows, {
            value = ij.ijSeverityValue or -1,
            text = ('%-18s LSP %d  line %-4d %s%s'):format(
              ij.ijSeverity or '?', d.severity or 1, d.range.start.line + 1, d.code and (d.code .. ': ') or '', d.message),
          })
        end
      end
    end
  end
  table.sort(rows, function(a, b) return a.value > b.value end)
  return vim.tbl_map(function(r) return r.text end, rows)
end

--- Change the level and re-show what every file already has. False for an unknown name.
function M.set_level(name)
  if not LEVELS[name] then
    return false
  end
  level = name
  for client_id, uris in pairs(raw) do
    for _, params in pairs(uris) do
      show(nil, params, { client_id = client_id, method = 'textDocument/publishDiagnostics' })
    end
  end
  return true
end

return M
