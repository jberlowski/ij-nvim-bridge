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
