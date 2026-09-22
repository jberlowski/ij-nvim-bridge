-- The sign column for what can be run (FEATURES.md §6c): `$/ij/runnables`, IntelliJ's own run-gutter
-- markers (JUnit tests, `main`), harvested from the daemon the same way diagnostics are. A `▶` per
-- runnable line, replaced with a pass or fail sign once `test_run.lua` has a result for it.
local M = {}

local ns = vim.api.nvim_create_namespace('ij_bridge_runnables')

--- buf -> { list = runnables[], marks = { [name] = { id, line } } }
M.state = {}

local SIGNS = { passed = '✔', failed = '✘', error = '✘', running = '●', skipped = '◌' }
local HL = {
  passed = 'DiagnosticOk', failed = 'DiagnosticError', error = 'DiagnosticError',
  running = 'DiagnosticWarn', skipped = 'DiagnosticHint',
}

local function sign(status)
  return SIGNS[status] or '▶', HL[status] or 'DiagnosticHint'
end

--- `$/ij/runnables` notification: replaces every sign in this buffer with the fresh list.
function M.on_runnables(params)
  local buf = vim.uri_to_bufnr(params.uri)
  if not vim.api.nvim_buf_is_loaded(buf) then
    return
  end
  vim.api.nvim_buf_clear_namespace(buf, ns, 0, -1)
  local marks = {}
  for _, r in ipairs(params.runnables or {}) do
    local text, hl = sign(nil)
    local line = r.range.start.line
    local id = vim.api.nvim_buf_set_extmark(buf, ns, line, 0, { sign_text = text, sign_hl_group = hl })
    marks[r.name] = { id = id, line = line }
  end
  M.state[buf] = { list = params.runnables or {}, marks = marks }
end

--- A test-status event for a run in this buffer: move its sign from `▶` to what happened.
function M.mark_status(buf, name, status)
  local state = M.state[buf]
  local m = state and state.marks[name]
  if not (m and vim.api.nvim_buf_is_valid(buf)) then
    return
  end
  local text, hl = sign(status)
  vim.api.nvim_buf_set_extmark(buf, ns, m.line, 0, { id = m.id, sign_text = text, sign_hl_group = hl })
end

--- What is known for this buffer, for tests and for `test_run.lua` to look a name up by line.
function M.at(buf, line)
  local state = M.state[buf]
  if not state then
    return nil
  end
  for _, r in ipairs(state.list) do
    if r.range.start.line == line then
      return r
    end
  end
end

function M.clear(buf)
  if vim.api.nvim_buf_is_valid(buf) then
    vim.api.nvim_buf_clear_namespace(buf, ns, 0, -1)
  end
  M.state[buf] = nil
end

return M
