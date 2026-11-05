-- IntelliJ supplies build roots; cwd and build-file heuristics never choose a build.
local M = {}

local function contains(root, path)
  return path == root or path:sub(1, #root + 1) == root .. '/'
end

function M.for_buffer(buf)
  buf = (not buf or buf == 0) and vim.api.nvim_get_current_buf() or buf
  local ij = require('ij_bridge')
  local c = ij.client(buf)
  if not c then return vim.b[buf].ij_bridge_context end
  local workspace = (ij.sessions[c.id] or {}).workspace or { root = c.config.root_dir }
  local path = vim.api.nvim_buf_get_name(buf)
  path = vim.uv.fs_realpath(path) or path
  local best
  for _, build in ipairs(workspace.builds or {}) do
    if contains(build.path, path) and (not best or #build.path > #best.path) then best = build end
  end
  local root = workspace.root or c.config.root_dir
  local label = workspace.name or vim.fs.basename(root)
  if best and best.path ~= root then label = label .. ' / ' .. best.name end
  return { client_id = c.id, root = root, build = best and best.path, label = label,
    uri = vim.uri_from_bufnr(buf) }
end

function M.client(context)
  if not context then return end
  local c = vim.lsp.get_client_by_id(context.client_id)
  if c and not c:is_stopped() then return c end
  for _, candidate in ipairs(vim.lsp.get_clients({ name = require('ij_bridge').name })) do
    if candidate.config.root_dir == context.root and not candidate:is_stopped() then return candidate end
  end
end

function M.params(context)
  return context and context.uri and { textDocument = { uri = context.uri } } or {}
end

function M.key(context)
  return context and (context.build or context.root)
end

function M.bind(buf, context)
  vim.b[buf].ij_bridge_context = context
end

return M
