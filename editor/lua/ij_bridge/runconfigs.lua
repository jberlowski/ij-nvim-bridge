-- IntelliJ Run Configurations (FEATURES.md §6c): a developer's own named, saved way to run
-- something (`RunManager`), possibly checked into the `.run` folder and so shared through the repo -
-- distinct from Gradle tasks (`tasks.lua`, the project's own task list) and from running the test at
-- the cursor (`test_run.lua`). v1 runs only Gradle-backed configurations (the Brain refuses any
-- other kind with a reason); a Run Configuration is a checked-in, named, repeatable version of
-- "run a Gradle task with specific parameters".
--
--   pick()          a fuzzy finder over every configuration; only a Gradle-backed one can be chosen
--   run(item)       runs one and streams its output into a buffer, with a live status and how it ended
--   stop()          cancels the one running          repeat_last()   runs the last one again
local log = require('ij_bridge.log')

local M = {}

--- The last one run: { name }, for repeating it.
M.last = nil

--- The run whose output is coming in: { id, buf, name }.
M.run_state = nil

local function say(msg, level)
  vim.notify('ij-bridge: ' .. msg, level or vim.log.levels.INFO)
end

local function client()
  local c = require('ij_bridge').client(0)
  if not c then
    say('no IntelliJ is serving this project (:IjBridge open)', vim.log.levels.WARN)
  end
  return c
end

function M.fetch(callback)
  local c = client()
  if not c then
    return
  end
  c:request('$/ij/runConfigurations', {}, function(err, result)
    if err then
      say(err.message, vim.log.levels.WARN)
      return
    end
    callback(result.configurations or {})
  end, 0)
end

-- ------------------------------------------------------------------- finding
function M.pick()
  M.fetch(function(items)
    if #items == 0 then
      say('no run configurations (none defined, or the project is still importing)', vim.log.levels.WARN)
      return
    end
    require('ij_bridge.picker').open(items, {
      title = 'Run configuration',
      label = function(item) return item.name .. (item.gradle and '' or '  (not runnable yet)') end,
      texts = function(item) return item.name, item.type end,
      detail = function(item) return item.type end,
      on_choose = function(item)
        if item.gradle then
          M.run(item)
        else
          say(("'%s' is a %s configuration; only Gradle-backed ones can be run so far"):format(item.name, item.type),
            vim.log.levels.WARN)
        end
      end,
    })
  end)
end

-- ------------------------------------------------------------------- running
-- Same shape as `tasks.lua`'s output buffer, kept separate: these are named configurations, not
-- Gradle tasks, and a developer telling them apart by window matters more than sharing one buffer.
local output_buf

local function output_buffer()
  if output_buf and vim.api.nvim_buf_is_valid(output_buf) then
    return output_buf
  end
  local buf = vim.api.nvim_create_buf(false, true)
  output_buf = buf
  vim.api.nvim_buf_set_name(buf, 'IntelliJ Run Configuration output')
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

local function set_status(buf, text)
  for _, win in ipairs(vim.fn.win_findbuf(buf)) do
    vim.wo[win].winbar = (text and text ~= '') and ('IntelliJ: ' .. text) or ''
  end
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

--- `item` is { name }, from the finder.
function M.run(item)
  local c = client()
  if not c then
    return
  end
  c:request('$/ij/runConfiguration/run', { name = item.name }, function(err, result)
    if err then
      say(err.message, vim.log.levels.WARN)
      return
    end
    M.last = { name = item.name }
    local buf = output_buffer()
    vim.bo[buf].modifiable = true
    vim.api.nvim_buf_set_lines(buf, 0, -1, false, { '' })
    vim.bo[buf].modifiable = false
    M.run_state = { id = result.runId, buf = buf, name = item.name }
    append(buf, ('▶ %s\n\n'):format(item.name))
    show(buf)
    log.info('run_configuration_started', { name = item.name, run = result.runId })
  end, 0)
end

function M.repeat_last()
  if not M.last then
    say('no run configuration has been run yet')
    return
  end
  M.run(M.last)
end

function M.stop()
  local c = client()
  if not c then
    return
  end
  c:request('$/ij/runConfiguration/cancel', {}, function(err, result)
    if err then
      say(err.message, vim.log.levels.WARN)
    elseif not (result and result.cancelled) then
      say('no run configuration is running')
    end
  end, 0)
end

function M.on_status(params)
  local state = M.run_state
  if state and state.id == params.runId and vim.api.nvim_buf_is_valid(state.buf) then
    set_status(state.buf, params.description)
  end
end

function M.on_output(params)
  local state = M.run_state
  if state and state.id == params.runId and vim.api.nvim_buf_is_valid(state.buf) then
    append(state.buf, clean(params.text))
  end
end

function M.on_finished(params)
  local state = M.run_state
  local seconds = ('%.1fs'):format((params.ms or 0) / 1000)
  local summary
  if params.cancelled then
    summary = '■ cancelled after ' .. seconds
  elseif params.success then
    summary = '✔ finished in ' .. seconds
  else
    summary = '✘ failed after ' .. seconds .. (params.error and (': ' .. params.error) or '')
  end
  if state and state.id == params.runId and vim.api.nvim_buf_is_valid(state.buf) then
    append(state.buf, '\n' .. summary .. '\n')
    set_status(state.buf, nil)
    state.finished = params
  end
  log.info('run_configuration_finished', { run = params.runId, success = params.success, cancelled = params.cancelled, ms = params.ms })
  vim.notify('ij-bridge: ' .. (state and state.name or '') .. ' ' .. summary:sub(3),
    params.success and vim.log.levels.INFO or vim.log.levels.WARN)
  vim.api.nvim_exec_autocmds('User', { pattern = 'IjBridgeRunConfigurationFinished', modeline = false, data = params })
end

return M
