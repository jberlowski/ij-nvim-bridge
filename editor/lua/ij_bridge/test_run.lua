-- Run the test (FEATURES.md §6c): `$/ij/run` performs the Run action of the marker at a position -
-- the same `ConfigurationContext` API a real click on the gutter icon resolves through - so which
-- test framework, and Gradle versus IntelliJ's own runner, are the project's own settings.
--
--   run_nearest()   whatever the cursor is in - a method, or its class if not inside one
--   run_class()     every test in the enclosing class     run_file()   every test in the file
--   repeat_last()   the last of those, again              cancel()     stops the one running
--
-- Output streams into a buffer, as Gradle tasks do (`tasks.lua`); per-test results become signs
-- (`runnables.lua`) and lines here.
local log = require('ij_bridge.log')

local M = {}

--- The last run's own params, for `repeat_last()`: { buf, uri, position, scope }.
M.last = nil

--- The run whose output is coming in, and which buffer its signs belong to: { run_id, buf }.
M.current = nil

local function say(msg, level)
  vim.notify('ij-bridge: ' .. msg, level or vim.log.levels.INFO)
end

-- ------------------------------------------------------------------ output buffer
-- Same shape as `tasks.lua`'s: kept by handle, `bufnr()` matches by prefix.
local output_buf

local function output_buffer()
  if output_buf and vim.api.nvim_buf_is_valid(output_buf) then
    return output_buf
  end
  local buf = vim.api.nvim_create_buf(false, true)
  output_buf = buf
  vim.api.nvim_buf_set_name(buf, 'IntelliJ Test output')
  vim.bo[buf].buftype, vim.bo[buf].bufhidden, vim.bo[buf].swapfile, vim.bo[buf].filetype = 'nofile', 'hide', false, 'ij-bridge-output'
  vim.bo[buf].modifiable = false
  vim.keymap.set('n', 'q', '<cmd>close<cr>', { buffer = buf, desc = 'Close the output' })
  return buf
end

local function append(buf, text)
  vim.bo[buf].modifiable = true
  local last = vim.api.nvim_buf_line_count(buf)
  local lines = vim.split(text, '\n', { plain = true })
  local first = vim.api.nvim_buf_get_lines(buf, last - 1, last, false)[1] or ''
  lines[1] = first .. lines[1]
  vim.api.nvim_buf_set_lines(buf, last - 1, last, false, lines)
  vim.bo[buf].modifiable = false
  for _, win in ipairs(vim.fn.win_findbuf(buf)) do
    local count = vim.api.nvim_buf_line_count(buf)
    local cursor = vim.api.nvim_win_get_cursor(win)[1]
    if cursor >= last - 1 then
      vim.api.nvim_win_set_cursor(win, { count, 0 })
    end
  end
end

local function clean(text)
  return (text:gsub('\27%[[%d;]*[A-Za-z]', ''):gsub('\r\n', '\n'):gsub('\r', '\n'))
end

local function show(buf)
  if #vim.fn.win_findbuf(buf) == 0 then
    local from = vim.api.nvim_get_current_win()
    vim.cmd('botright 14split')
    vim.api.nvim_win_set_buf(0, buf)
    vim.wo.winfixheight = true
    if vim.api.nvim_win_is_valid(from) then
      vim.api.nvim_set_current_win(from)
    end
  end
end

--- The output buffer, if there is one: for tests.
function M.output_buffer_for_tests()
  return (output_buf and vim.api.nvim_buf_is_valid(output_buf)) and output_buf or nil
end

-- ------------------------------------------------------------------- running
local function do_run(buf, uri, position, scope)
  local client = require('ij_bridge').client(buf)
  if not client then
    say('no IntelliJ connection', vim.log.levels.WARN)
    return
  end
  client:request('$/ij/run', { textDocument = { uri = uri }, position = position, scope = scope }, function(err, result)
    if err then
      say(err.message, vim.log.levels.WARN)
      return
    end
    M.last = { buf = buf, uri = uri, position = position, scope = scope }
    M.current = { run_id = result.runId, buf = buf }
    local obuf = output_buffer()
    vim.bo[obuf].modifiable = true
    vim.api.nvim_buf_set_lines(obuf, 0, -1, false, { '' })
    vim.bo[obuf].modifiable = false
    append(obuf, ('▶ %s test run\n\n'):format(scope))
    -- The results tree and the log of the selected test are what is shown; the raw Gradle log is still kept
    -- here, for `show_raw_log()`.
    require('ij_bridge.test_results').start(result.runId, client.config.root_dir)
    log.info('test_run_started', { run = result.runId, scope = scope })
  end, buf)
end

local function cursor_position()
  local pos = vim.api.nvim_win_get_cursor(0)
  return { line = pos[1] - 1, character = pos[2] }
end

function M.run_nearest()
  local buf = vim.api.nvim_get_current_buf()
  do_run(buf, vim.uri_from_bufnr(buf), cursor_position(), 'nearest')
end

function M.run_class()
  local buf = vim.api.nvim_get_current_buf()
  do_run(buf, vim.uri_from_bufnr(buf), cursor_position(), 'class')
end

function M.run_file()
  local buf = vim.api.nvim_get_current_buf()
  do_run(buf, vim.uri_from_bufnr(buf), cursor_position(), 'file')
end

--- The raw Gradle log of the last run, at the bottom.
function M.show_raw_log()
  show(output_buffer())
end

function M.repeat_last()
  if not M.last then
    say('no test has been run yet')
    return
  end
  do_run(M.last.buf, M.last.uri, M.last.position, M.last.scope)
end

function M.cancel()
  local client = require('ij_bridge').client(0)
  if not client then
    return
  end
  client:request('$/ij/run/cancel', {}, function(err, result)
    if err then
      say(err.message, vim.log.levels.WARN)
    elseif not (result and result.cancelled) then
      say('no test is running')
    end
  end, 0)
end

-- ------------------------------------------------------------------- notifications
function M.on_output(params)
  if M.current and M.current.run_id == params.runId then
    append(output_buffer(), clean(params.text))
  end
end

function M.on_status(params)
  if not (M.current and M.current.run_id == params.runId) then
    return
  end
  require('ij_bridge.runnables').mark_status(M.current.buf, params.name, params.status)
  require('ij_bridge.test_results').on_status(params)
  if params.status == 'started' then
    append(output_buffer(), '▶ ' .. params.name .. '\n')
  else
    local icon = ({ passed = '✔', failed = '✘', error = '✘', skipped = '◌' })[params.status] or '?'
    append(output_buffer(), icon .. ' ' .. params.name .. ' ' .. params.status .. '\n')
    if params.message then
      append(output_buffer(), '  ' .. params.message .. '\n')
    end
  end
end

function M.on_finished(params)
  local seconds = ('%.1fs'):format((params.ms or 0) / 1000)
  local summary
  if params.cancelled then
    summary = '■ cancelled after ' .. seconds
  elseif params.success then
    summary = '✔ passed in ' .. seconds
  else
    summary = '✘ failed after ' .. seconds .. (params.error and (': ' .. params.error) or '')
  end
  append(output_buffer(), '\n' .. summary .. '\n')
  require('ij_bridge.test_results').finish(params)
  log.info('test_run_finished', { run = params.runId, success = params.success, cancelled = params.cancelled, ms = params.ms })
  vim.notify('ij-bridge: test run ' .. summary:sub(3), params.success and vim.log.levels.INFO or vim.log.levels.WARN)
  if M.current and M.current.run_id == params.runId then
    M.current = nil
  end
  vim.api.nvim_exec_autocmds('User', { pattern = 'IjBridgeTestRunFinished', modeline = false, data = params })
end

return M
