-- The Brain sends every diagnostic IntelliJ's daemon produced, including the ones IntelliJ itself
-- draws faintly or not at all ("No highlighting, only fix", "Consideration": LSP severity 4). What
-- to show is the Editor's choice: `min_severity` hides everything less severe than it. The Brain's
-- answer is kept as it came, so lowering the level brings the hidden ones back without a new pass.
local M = {}

--- The least severe LSP DiagnosticSeverity each level still shows (1 error .. 4 hint).
local LEVELS = { error = 1, warn = 2, info = 3, hint = 4 }

local level = 'hint' -- everything, as sent
local excluded = {}
local state_path
local raw = {} -- [client_id][uri] = the params of the last publishDiagnostics

function M.level()
  return level
end

local function show(err, params, ctx)
  local limit = LEVELS[level]
  local buf = vim.fn.bufnr(vim.uri_to_fname(params.uri))
  local ft = buf >= 0 and vim.bo[buf].filetype or vim.filetype.match({ filename = vim.uri_to_fname(params.uri) })
  local kept = vim.tbl_filter(function(d)
    return not excluded[ft] and (d.severity or 1) <= limit
  end, params.diagnostics or {})
  vim.lsp.diagnostic.on_publish_diagnostics(err, vim.tbl_extend('force', params, { diagnostics = kept }), ctx)
end

local function reshow()
  for client_id, uris in pairs(raw) do
    if vim.lsp.get_client_by_id(client_id) then
      for _, params in pairs(uris) do
        show(nil, params, { client_id = client_id, method = 'textDocument/publishDiagnostics' })
      end
    end
  end
end

function M.exclusions()
  local names = vim.tbl_keys(excluded)
  table.sort(names)
  return names
end

function M.setup(opts)
  opts = opts or {}
  state_path = opts.state_path or (vim.fn.stdpath('state') .. '/ij_bridge/diagnostic-exclusions.json')
  if vim.fn.filereadable(state_path) == 1 then
    local ok, names = pcall(function() return vim.json.decode(table.concat(vim.fn.readfile(state_path), '\n')) end)
    if ok and type(names) == 'table' then
      for _, name in ipairs(names) do
        if type(name) == 'string' and name ~= '' then excluded[name] = true end
      end
    else
      vim.notify('ij-bridge: could not read diagnostic exclusions', vim.log.levels.WARN)
    end
  end
  local group = vim.api.nvim_create_augroup('IjBridgeDiagnosticExclusions', { clear = true })
  vim.api.nvim_create_autocmd('FileType', { group = group, callback = reshow })
  reshow()
end

local function change(ft, value)
  if not ft or ft == '' then
    vim.notify('ij-bridge: this buffer has no filetype', vim.log.levels.WARN)
    return false
  end
  if not state_path then M.setup() end
  local previous = excluded[ft]
  excluded[ft] = value or nil
  local ok, err = pcall(function()
    vim.fn.mkdir(vim.fn.fnamemodify(state_path, ':h'), 'p')
    vim.fn.writefile({ vim.json.encode(M.exclusions()) }, state_path)
  end)
  if not ok then
    excluded[ft] = previous
    vim.notify('ij-bridge: could not save diagnostic exclusions: ' .. tostring(err), vim.log.levels.ERROR)
    return false
  end
  reshow()
  return true
end

function M.exclude(ft) return change(ft or vim.bo.filetype, true) end
function M.include(ft) return change(ft, false) end

function M.menu()
  local ft = vim.bo.filetype
  local items = {}
  if ft ~= '' and not excluded[ft] then
    items[#items + 1] = { label = 'Disable diagnostics for filetype: ' .. ft, ft = ft, disable = true }
  end
  for _, name in ipairs(M.exclusions()) do
    items[#items + 1] = { label = 'Restore diagnostics for: ' .. name, ft = name }
  end
  if #items == 0 then
    vim.notify('ij-bridge: no diagnostic exclusions; this buffer has no filetype')
    return
  end
  vim.ui.select(items, { prompt = 'IntelliJ diagnostic exclusions', format_item = function(item) return item.label end }, function(item)
    if item then change(item.ft, item.disable) end
  end)
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
