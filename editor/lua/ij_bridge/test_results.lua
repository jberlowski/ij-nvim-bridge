-- Test results as a tree, and the log of the test under the cursor (FEATURES.md §6d), as IntelliJ's own
-- test runner window has them: two panes at the bottom, the classes and their tests on the left, what
-- the selected one said on the right (its failed assertion, its stack, what it printed).
--
-- Fed by `test_run.lua` from the Brain's `$/ij/test/suite`, `$/ij/test/status` and `$/ij/test/output`;
-- a test is a node once it has finished (its path is whole then), a class as soon as it starts.
-- A failed assertion is also shown where it happened, as a diagnostic on the line of the test's own
-- frame in the stack trace.
local M = {}

local ns = vim.api.nvim_create_namespace('ij_bridge_test_results')
local diag_ns = vim.api.nvim_create_namespace('ij_bridge_test_failures')

local ICONS = { passed = '✔', failed = '✘', error = '✘', skipped = '◌', started = '●', running = '●' }
local HL = {
  passed = 'DiagnosticOk', failed = 'DiagnosticError', error = 'DiagnosticError',
  skipped = 'DiagnosticHint', started = 'DiagnosticWarn', running = 'DiagnosticWarn',
}

--- The run on show: { id, root, tree_buf, log_buf, lines = { [line] = node }, selected, failures_only, project_root }.
M.run = nil

-- Frames from these are the framework's, not the developer's: hidden in the log, never a place to jump to.
local FRAMEWORK = { 'org.junit.', 'org.opentest4j.', 'java.', 'jdk.', 'sun.', 'kotlin.', 'org.gradle.', 'worker.org.gradle.' }

local function is_framework(class)
  for _, prefix in ipairs(FRAMEWORK) do
    if class:sub(1, #prefix) == prefix then
      return true
    end
  end
  return false
end

--- The first frame of the developer's own code in a stack trace: { file, line }, or nil.
function M.own_frame(stacktrace)
  for line in (stacktrace or ''):gmatch('[^\n]+') do
    local class, file, lnum = line:match('^%s*at%s+([%w_.$<>]+)%.[^%s(]+%(([^:()]+):(%d+)%)')
    if class and not is_framework(class) then
      return { file = file, line = tonumber(lnum) }
    end
  end
end

-- ------------------------------------------------------------------- the tree
local function new_node(name, parent)
  return { name = name, kind = 'test', status = 'running', children = {}, order = {}, parent = parent, output = {} }
end

--- The node at `path`, made (with its classes) if it is not there yet.
local function node_at(root, path)
  local node = root
  for i, name in ipairs(path) do
    local child = node.children[name]
    if not child then
      child = new_node(name, node)
      node.children[name] = child
      table.insert(node.order, child)
    end
    if i < #path then
      child.kind = 'suite'
    end
    node = child
  end
  return node
end

local function counts(node, into)
  into = into or { passed = 0, failed = 0, skipped = 0, running = 0 }
  for _, child in ipairs(node.order) do
    if child.kind == 'suite' then
      counts(child, into)
    elseif child.status == 'passed' then
      into.passed = into.passed + 1
    elseif child.status == 'failed' or child.status == 'error' then
      into.failed = into.failed + 1
    elseif child.status == 'skipped' then
      into.skipped = into.skipped + 1
    else
      into.running = into.running + 1
    end
  end
  return into
end

local function failed(node)
  return node.status == 'failed' or node.status == 'error'
end

--- A class is failed when any test under it is, whatever the framework said of the class itself.
local function has_failure(node)
  if node.kind == 'test' then
    return failed(node)
  end
  for _, child in ipairs(node.order) do
    if has_failure(child) then
      return true
    end
  end
  return false
end

local function shown_status(node)
  if node.kind == 'suite' and node.status ~= 'started' and has_failure(node) then
    return 'failed'
  end
  return node.status
end

local function duration(ms)
  if not ms then
    return ''
  end
  return ms >= 1000 and ('%.1fs'):format(ms / 1000) or (ms .. 'ms')
end

-- ------------------------------------------------------------------- drawing
local function header(run)
  local c = counts(run.root)
  if run.ended and #run.root.order == 0 then
    return 'no test results: nothing ran'
  end
  local parts = {}
  if c.failed > 0 then table.insert(parts, c.failed .. ' failed') end
  if c.passed > 0 then table.insert(parts, c.passed .. ' passed') end
  if c.skipped > 0 then table.insert(parts, c.skipped .. ' skipped') end
  if c.running > 0 then table.insert(parts, c.running .. ' running') end
  local text = #parts > 0 and table.concat(parts, ', ') or 'waiting for results'
  return text .. (run.failures_only and '   [failures only]' or '')
end

local function draw_tree(run)
  if not (run.tree_buf and vim.api.nvim_buf_is_valid(run.tree_buf)) then
    return
  end
  local lines, marks = { header(run) }, {}
  run.lines = {}
  local function walk(node, depth)
    for _, child in ipairs(node.order) do
      if not run.failures_only or has_failure(child) then
        local status = shown_status(child)
        local text = ('%s%s %s'):format(('  '):rep(depth), ICONS[status] or '?', child.name)
        local ms = duration(child.ms)
        if ms ~= '' then
          text = text .. '  ' .. ms
        end
        table.insert(lines, text)
        run.lines[#lines] = child
        table.insert(marks, { line = #lines - 1, col = #('  '):rep(depth), hl = HL[status] })
        if child.kind == 'suite' then
          walk(child, depth + 1)
        end
      end
    end
  end
  walk(run.root, 0)
  local buf = run.tree_buf
  vim.bo[buf].modifiable = true
  vim.api.nvim_buf_set_lines(buf, 0, -1, false, lines)
  vim.bo[buf].modifiable = false
  vim.api.nvim_buf_clear_namespace(buf, ns, 0, -1)
  for _, m in ipairs(marks) do
    if m.hl then
      vim.api.nvim_buf_set_extmark(buf, ns, m.line, m.col, { end_col = m.col + 3, hl_group = m.hl })
    end
  end
end

local function trace_lines(stacktrace)
  local out, hidden = {}, 0
  for line in (stacktrace or ''):gmatch('[^\n]+') do
    local class = line:match('^%s*at%s+([%w_.$<>]+)%.[^%s(]+%(')
    if class and is_framework(class) then
      hidden = hidden + 1
    else
      if hidden > 0 then
        table.insert(out, ('  … %d framework frames'):format(hidden))
        hidden = 0
      end
      table.insert(out, line)
    end
  end
  if hidden > 0 then
    table.insert(out, ('  … %d framework frames'):format(hidden))
  end
  return out
end

--- The log pane's lines for a node: what happened, then what it printed.
local function log_for(node)
  local path = {}
  local n = node
  while n and n.parent do
    table.insert(path, 1, n.name)
    n = n.parent
  end
  local status = shown_status(node)
  local lines = { ('%s %s  %s'):format(ICONS[status] or '?', table.concat(path, ' > '), duration(node.ms)), '' }
  if node.kind == 'suite' then
    for _, child in ipairs(node.order) do
      local s = shown_status(child)
      table.insert(lines, ('%s %s  %s'):format(ICONS[s] or '?', child.name, duration(child.ms)))
      if failed(child) and child.message then
        table.insert(lines, '    ' .. child.message)
      end
    end
    return lines
  end
  if node.message then
    table.insert(lines, node.message)
    table.insert(lines, '')
  end
  if node.expected or node.actual then
    table.insert(lines, 'expected: ' .. tostring(node.expected))
    table.insert(lines, '  actual: ' .. tostring(node.actual))
    table.insert(lines, '')
  end
  if node.stacktrace then
    for _, l in ipairs(trace_lines(node.stacktrace)) do
      table.insert(lines, l)
    end
    table.insert(lines, '')
  end
  if #node.output > 0 then
    table.insert(lines, '── output ' .. ('─'):rep(30))
    for _, o in ipairs(node.output) do
      for _, l in ipairs(vim.split((o.text:gsub('\n$', '')), '\n', { plain = true })) do
        table.insert(lines, (o.stdout and '' or '[stderr] ') .. l)
      end
    end
  elseif node.status ~= 'running' then
    table.insert(lines, '(it printed nothing)')
  end
  return lines
end

local function draw_log(run)
  if not (run.log_buf and vim.api.nvim_buf_is_valid(run.log_buf)) then
    return
  end
  local lines = run.selected and log_for(run.selected) or { header(run) }
  vim.bo[run.log_buf].modifiable = true
  vim.api.nvim_buf_set_lines(run.log_buf, 0, -1, false, lines)
  vim.bo[run.log_buf].modifiable = false
end

local function redraw(run)
  draw_tree(run)
  draw_log(run)
end

--- The node under the cursor in the tree, shown in the log pane.
local function follow_cursor(run)
  if not (run.tree_buf and vim.api.nvim_get_current_buf() == run.tree_buf) then
    return
  end
  local node = run.lines and run.lines[vim.api.nvim_win_get_cursor(0)[1]]
  run.selected = node
  draw_log(run)
end

-- ------------------------------------------------------------------- where a failure happened
local function find_file(run, file)
  for _, buf in ipairs(vim.api.nvim_list_bufs()) do
    local name = vim.api.nvim_buf_get_name(buf)
    if name:sub(-#file - 1) == '/' .. file then
      return name
    end
  end
  if run.project_root then
    return vim.fs.find(file, { path = run.project_root, type = 'file', limit = 1 })[1]
  end
end

local function jump(run, node)
  local frame = node and failed(node) and M.own_frame(node.stacktrace)
  local path = frame and find_file(run, frame.file)
  if not path then
    vim.notify('ij-bridge: no source location for this result', vim.log.levels.INFO)
    return
  end
  vim.cmd('wincmd p')
  vim.cmd.edit(vim.fn.fnameescape(path))
  vim.api.nvim_win_set_cursor(0, { frame.line, 0 })
end

--- The failed assertion, on its line in the test's own file.
local function mark_failure(run, node)
  local frame = M.own_frame(node.stacktrace)
  local path = frame and find_file(run, frame.file)
  if not path then
    return
  end
  local buf = vim.fn.bufadd(path)
  local existing = vim.diagnostic.get(buf, { namespace = diag_ns })
  table.insert(existing, {
    lnum = frame.line - 1, col = 0, severity = vim.diagnostic.severity.ERROR, source = 'IntelliJ test',
    message = node.message or 'test failed',
  })
  vim.diagnostic.set(diag_ns, buf, existing)
end

-- ------------------------------------------------------------------- panes
local function make_buf(name, filetype)
  local buf = vim.api.nvim_create_buf(false, true)
  vim.api.nvim_buf_set_name(buf, name)
  vim.bo[buf].buftype, vim.bo[buf].bufhidden, vim.bo[buf].swapfile, vim.bo[buf].filetype = 'nofile', 'hide', false, filetype
  vim.bo[buf].modifiable = false
  return buf
end

local function shown(buf)
  return buf and vim.api.nvim_buf_is_valid(buf) and #vim.fn.win_findbuf(buf) > 0
end

--- The two panes, at the bottom, without taking the focus from where the developer is.
function M.show()
  local run = M.run
  if not run or shown(run.tree_buf) then
    return
  end
  local from = vim.api.nvim_get_current_win()
  vim.cmd('botright 14split')
  vim.api.nvim_win_set_buf(0, run.tree_buf)
  vim.wo.winfixheight, vim.wo.number, vim.wo.signcolumn, vim.wo.wrap = true, false, 'no', false
  vim.cmd('rightbelow vsplit')
  vim.api.nvim_win_set_buf(0, run.log_buf)
  vim.wo.number, vim.wo.signcolumn, vim.wo.wrap = false, 'no', false
  vim.cmd('wincmd h')
  vim.cmd('vertical resize 48')
  if vim.api.nvim_win_is_valid(from) then
    vim.api.nvim_set_current_win(from)
  end
  redraw(run)
end

function M.close()
  local run = M.run
  if not run then
    return
  end
  for _, buf in ipairs({ run.tree_buf, run.log_buf }) do
    for _, win in ipairs(buf and vim.api.nvim_buf_is_valid(buf) and vim.fn.win_findbuf(buf) or {}) do
      pcall(vim.api.nvim_win_close, win, true)
    end
  end
end

function M.toggle_failures()
  if M.run then
    M.run.failures_only = not M.run.failures_only
    redraw(M.run)
  end
end

local function keymaps(run)
  local function map(lhs, fn, desc)
    vim.keymap.set('n', lhs, fn, { buffer = run.tree_buf, desc = desc, nowait = true })
  end
  map('q', M.close, 'Close the results')
  map('f', M.toggle_failures, 'Only the failures, or everything')
  map('<CR>', function() jump(run, run.lines and run.lines[vim.api.nvim_win_get_cursor(0)[1]]) end, 'Go to where it failed')
  map('r', function() require('ij_bridge.test_run').repeat_last() end, 'Run the last test again')
  vim.api.nvim_create_autocmd('CursorMoved', {
    buffer = run.tree_buf,
    callback = function() follow_cursor(run) end,
  })
end

-- ------------------------------------------------------------------- a run, as it comes in
--- A new run: a fresh tree and panes (the same two buffers are reused by name).
function M.start(run_id, project_root)
  vim.diagnostic.reset(diag_ns)
  local old = M.run
  local tree_buf = old and old.tree_buf and vim.api.nvim_buf_is_valid(old.tree_buf) and old.tree_buf or nil
  local log_buf = old and old.log_buf and vim.api.nvim_buf_is_valid(old.log_buf) and old.log_buf or nil
  local first = not tree_buf
  M.run = {
    id = run_id, root = new_node('', nil), project_root = project_root, lines = {},
    tree_buf = tree_buf or make_buf('IntelliJ Test results', 'ij-bridge-test-results'),
    log_buf = log_buf or make_buf('IntelliJ Test log', 'ij-bridge-test-log'),
  }
  if first then
    keymaps(M.run)
  else
    -- The keymaps and the cursor autocmd closed over the old run: point them at this one.
    vim.api.nvim_buf_clear_namespace(M.run.tree_buf, ns, 0, -1)
    vim.api.nvim_clear_autocmds({ buffer = M.run.tree_buf })
    keymaps(M.run)
  end
  M.show()
  redraw(M.run)
end

local function current(params)
  return M.run and M.run.id == params.runId and M.run or nil
end

--- The run is over: a tree still empty means nothing ran.
function M.finish(params)
  local run = current(params)
  if run then
    run.ended = true
    redraw(run)
  end
end

function M.on_suite(params)
  local run = current(params)
  if not run then
    return
  end
  local node = node_at(run.root, params.path)
  node.kind = 'suite'
  node.status = params.status
  node.ms = params.ms
  redraw(run)
end

function M.on_status(params)
  local run = current(params)
  if not run or params.status == 'started' then
    return
  end
  local node = node_at(run.root, params.path)
  node.kind = 'test'
  node.status, node.ms = params.status, params.ms
  node.message, node.stacktrace = params.message, params.stacktrace
  node.expected, node.actual = params.expected, params.actual
  if failed(node) then
    mark_failure(run, node)
  end
  redraw(run)
end

function M.on_output(params)
  local run = current(params)
  if not run or #params.path == 0 then
    return
  end
  local node = node_at(run.root, params.path)
  table.insert(node.output, { text = params.text, stdout = params.stdout })
  if run.selected == node then
    draw_log(run)
  end
end

--- The tree as plain lines, for tests.
function M.tree_lines_for_tests()
  local run = M.run
  return run and run.tree_buf and vim.api.nvim_buf_is_valid(run.tree_buf)
    and vim.api.nvim_buf_get_lines(run.tree_buf, 0, -1, false) or {}
end

--- The log pane's lines, for tests.
function M.log_lines_for_tests()
  local run = M.run
  return run and run.log_buf and vim.api.nvim_buf_is_valid(run.log_buf)
    and vim.api.nvim_buf_get_lines(run.log_buf, 0, -1, false) or {}
end

return M
