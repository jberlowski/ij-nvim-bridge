-- Gradle tasks (FEATURES.md §6d). The Brain lists the tasks IntelliJ has imported and runs them through its own
-- Gradle integration; this is what Neovim shows of that:
--
--   pick()      a fuzzy finder over every task: type `fMT` or `eas` or `trea` to find `findMysteriousTreasure`
--   tree()      the hierarchy: projects (subprojects nested), their groups, their tasks; <CR> runs, <Tab> folds
--   run(item)   runs a task and streams its output into a buffer, with how it ended
--   stop()      cancels the one that is running        repeat_last()   runs the last one again
local log = require('ij_bridge.log')

local M = {}

--- The last task run: { path, tasks, args }, for repeating it.
M.last = nil

--- The run whose output is coming in: { id, buf, pending, task }.
M.run_state = nil

--- The project (its root) the tasks were asked for: the tree buffer is not itself in a project.
M.root = nil

local function say(msg, level)
  vim.notify('ij-bridge: ' .. msg, level or vim.log.levels.INFO)
end

--- The Session serving the buffer. Inside the tasks' own buffers (the tree, the output), which are in no project, it
--- is the one for the root the tasks were last asked about; anywhere else, no connection is none.
local function client()
  local ij = require('ij_bridge')
  local c = ij.client(0)
  if c then
    M.root = c.config.root_dir
    return c
  end
  local ft = vim.bo.filetype
  if M.root and (ft == 'ij-bridge-tasks' or ft == 'ij-bridge-output') then
    for _, other in ipairs(vim.lsp.get_clients({ name = ij.name })) do
      if other.config.root_dir == M.root then
        return other
      end
    end
  end
end

function M.fetch(callback)
  local c = client()
  if not c then
    say('no IntelliJ is serving this project (:IjBridge open)', vim.log.levels.WARN)
    return
  end
  c:request('$/ij/tasks', {}, function(err, result)
    if err then
      say(err.message, vim.log.levels.WARN)
      return
    end
    callback(result)
  end, 0)
end

--- Every task in the tree, flat: { name, group, project, path, description }.
function M.flatten(tree)
  local out = {}
  local function walk(project)
    for _, group in ipairs(project.groups or {}) do
      for _, task in ipairs(group.tasks or {}) do
        table.insert(out, {
          name = task.name, group = group.name, project = project.name, path = project.path,
          description = task.description,
        })
      end
    end
    for _, child in ipairs(project.projects or {}) do
      walk(child)
    end
  end
  for _, project in ipairs(tree.projects or {}) do
    walk(project)
  end
  return out
end

-- ------------------------------------------------------------------- finding
--- The fuzzy finder over every task, and running the one chosen.
function M.pick(on_choose)
  M.fetch(function(tree)
    local items = M.flatten(tree)
    if #items == 0 then
      say('no Gradle tasks (the project may still be importing)', vim.log.levels.WARN)
      return
    end
    require('ij_bridge.picker').open(items, {
      title = 'Gradle task',
      label = function(item) return item.name end,
      -- The name is what is matched first; the group and project too, at less weight.
      texts = function(item) return item.name, item.group .. ' ' .. item.project .. ' ' .. item.name end,
      detail = function(item)
        return ('%s › %s%s'):format(item.project, item.group, item.description and (' — ' .. item.description) or '')
      end,
      on_choose = on_choose or function(item) M.run(item) end,
    })
  end)
end

-- ------------------------------------------------------------------- running
--- The output's buffer. Kept by handle: `bufnr(name)` matches names by prefix, and "IntelliJ Gradle" is the start
--- of the tree's ("IntelliJ Gradle tasks"), which output then streamed into.
local output_buf

local function output_buffer()
  if output_buf and vim.api.nvim_buf_is_valid(output_buf) then
    return output_buf
  end
  local buf = vim.api.nvim_create_buf(false, true)
  output_buf = buf
  vim.api.nvim_buf_set_name(buf, 'IntelliJ Gradle output')
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
  -- Follow the end, as a terminal does, in every window that shows it and is already at the end.
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

--- Show the output window at the bottom, without taking the focus from where the developer is.
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

--- Run a task. `item` is { path, name } (from the finder or the tree); `args` are extra Gradle arguments.
function M.run(item, args)
  local c = client()
  if not c then
    say('no IntelliJ is serving this project (:IjBridge open)', vim.log.levels.WARN)
    return
  end
  local params = { path = item.path, tasks = { item.name }, args = args }
  c:request('$/ij/task/run', params, function(err, result)
    if err then
      say(err.message, vim.log.levels.WARN)
      return
    end
    M.last = { path = item.path, name = item.name, args = args }
    local buf = output_buffer()
    vim.bo[buf].modifiable = true
    vim.api.nvim_buf_set_lines(buf, 0, -1, false, { '' })
    vim.bo[buf].modifiable = false
    M.run_state = { id = result.runId, buf = buf, task = item.name }
    append(buf, ('▶ gradle %s%s   (%s)\n\n'):format(item.name, args and (' ' .. table.concat(args, ' ')) or '', item.path))
    show(buf)
    log.info('task_started', { task = item.name, run = result.runId })
  end, 0)
end

--- The output buffer, if there is one: for tests.
function M.output_buffer_for_tests()
  return (output_buf and vim.api.nvim_buf_is_valid(output_buf)) and output_buf or nil
end

--- What is happening right now, as IntelliJ's own Gradle tool window would show it - a winbar on
--- the output window, not a scrolling line, so "Building..." repeated a hundred times does not
--- flood the buffer a developer may want to scroll back through.
local function set_status(buf, text)
  for _, win in ipairs(vim.fn.win_findbuf(buf)) do
    vim.wo[win].winbar = (text and text ~= '') and ('IntelliJ: ' .. text) or ''
  end
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
  log.info('task_finished', { run = params.runId, success = params.success, cancelled = params.cancelled, ms = params.ms })
  vim.notify('ij-bridge: gradle ' .. (state and state.task or '') .. ' ' .. summary:sub(3),
    params.success and vim.log.levels.INFO or vim.log.levels.WARN)
  vim.api.nvim_exec_autocmds('User', { pattern = 'IjBridgeTaskFinished', modeline = false, data = params })
end

--- Reload the Gradle project(s), as IntelliJ's circle arrows do: needed after a library is added to a build script.
function M.sync()
  local c = client()
  if not c then
    return say('no IntelliJ is serving this buffer', vim.log.levels.WARN)
  end
  c:request('$/ij/sync', {}, function(err)
    if err then
      say('sync: ' .. (err.message or 'refused'), vim.log.levels.WARN)
    else
      say('Gradle sync started')
    end
  end)
end

function M.on_sync_finished(params)
  local seconds = ('%.1fs'):format((params.ms or 0) / 1000)
  if params.success then
    say('Gradle sync finished in ' .. seconds)
  else
    say('Gradle sync failed after ' .. seconds .. (params.error and (': ' .. params.error) or ''), vim.log.levels.ERROR)
  end
end

function M.stop()
  local c = client()
  if not c then
    return
  end
  c:request('$/ij/task/cancel', {}, function(err, result)
    if err then
      say(err.message, vim.log.levels.WARN)
    elseif not (result and result.cancelled) then
      say('no Gradle task is running')
    end
  end, 0)
end

function M.repeat_last()
  if not M.last then
    say('no task has been run yet')
    return
  end
  M.run({ path = M.last.path, name = M.last.name }, M.last.args)
end

-- ---------------------------------------------------------------------- tree
--- The hierarchy, in a buffer of its own. <CR> on a task runs it, on a project or a group folds it.
M.tree_state = nil

local function render_tree(state)
  local lines, nodes, notes = {}, {}, {}
  local function add(text, node, note)
    table.insert(lines, text)
    nodes[#lines] = node
    if note then
      notes[#lines] = note
    end
  end
  local function project(p, depth)
    local pad = ('  '):rep(depth)
    local id = 'p:' .. p.path
    local open = state.expanded[id] ~= false
    add(('%s%s %s'):format(pad, open and '▾' or '▸', p.name), { kind = 'project', id = id }, p.path)
    if open then
      for _, group in ipairs(p.groups or {}) do
        local gid = 'g:' .. p.path .. '#' .. group.name
        local gopen = state.expanded[gid] == true
        add(('%s  %s %s'):format(pad, gopen and '▾' or '▸', group.name), { kind = 'group', id = gid }, ('%d tasks'):format(#group.tasks))
        if gopen then
          for _, task in ipairs(group.tasks) do
            add(('%s      %s'):format(pad, task.name), { kind = 'task', item = { name = task.name, path = p.path, group = group.name, project = p.name } }, task.description)
          end
        end
      end
      for _, child in ipairs(p.projects or {}) do
        project(child, depth + 1)
      end
    end
  end
  for _, p in ipairs(state.tree.projects or {}) do
    project(p, 0)
  end
  return lines, nodes, notes
end

local function redraw()
  local state = M.tree_state
  if not (state and vim.api.nvim_buf_is_valid(state.buf)) then
    return
  end
  local lines, nodes, notes = render_tree(state)
  state.nodes = nodes
  vim.bo[state.buf].modifiable = true
  vim.api.nvim_buf_set_lines(state.buf, 0, -1, false, lines)
  vim.bo[state.buf].modifiable = false
  local ns = vim.api.nvim_create_namespace('ij_bridge_tasks')
  vim.api.nvim_buf_clear_namespace(state.buf, ns, 0, -1)
  for row, note in pairs(notes) do
    vim.api.nvim_buf_set_extmark(state.buf, ns, row - 1, 0, { virt_text = { { '  ' .. note, 'Comment' } }, virt_text_pos = 'eol' })
  end
end

local function expand_all(state, value)
  local function walk(p)
    state.expanded['p:' .. p.path] = value or false
    for _, group in ipairs(p.groups or {}) do
      state.expanded['g:' .. p.path .. '#' .. group.name] = value
    end
    for _, child in ipairs(p.projects or {}) do
      walk(child)
    end
  end
  for _, p in ipairs(state.tree.projects or {}) do
    walk(p)
  end
end

function M.tree()
  M.fetch(function(tree)
    local state = M.tree_state
    if not (state and vim.api.nvim_buf_is_valid(state.buf)) then
      local buf = vim.api.nvim_create_buf(false, true)
      vim.api.nvim_buf_set_name(buf, 'IntelliJ Gradle hierarchy')
      vim.bo[buf].buftype, vim.bo[buf].bufhidden, vim.bo[buf].swapfile, vim.bo[buf].filetype = 'nofile', 'hide', false, 'ij-bridge-tasks'
      state = { buf = buf, expanded = {}, nodes = {} }
      M.tree_state = state
      local function node()
        return state.nodes[vim.api.nvim_win_get_cursor(0)[1]]
      end
      local function toggle()
        local n = node()
        if n and n.id then
          state.expanded[n.id] = not (state.expanded[n.id] ~= false and (n.kind == 'project' or state.expanded[n.id] == true))
          redraw()
        end
      end
      local function map(lhs, fn, desc)
        vim.keymap.set('n', lhs, fn, { buffer = buf, nowait = true, desc = 'IntelliJ tasks: ' .. desc })
      end
      map('<CR>', function()
        local n = node()
        if n and n.kind == 'task' then
          M.run(n.item)
        else
          toggle()
        end
      end, 'run the task, or fold the project or group')
      map('<Tab>', toggle, 'fold or unfold')
      map('o', toggle, 'fold or unfold')
      map('zR', function() expand_all(state, true); redraw() end, 'expand everything')
      map('zM', function() expand_all(state, false); redraw() end, 'collapse everything')
      map('R', function() M.tree() end, 'reload')
      map('/', function() M.pick() end, 'find a task (fuzzy)')
      map('a', function()
        local n = node()
        if n and n.kind == 'task' then
          vim.ui.input({ prompt = 'Arguments for ' .. n.item.name .. ': ' }, function(text)
            if text then
              M.run(n.item, vim.split(text, '%s+', { trimempty = true }))
            end
          end)
        end
      end, 'run with arguments')
      map('q', '<cmd>close<cr>', 'close')
    end
    state.tree = tree
    redraw()
    if #vim.fn.win_findbuf(state.buf) == 0 then
      vim.cmd('botright 20split')
      vim.api.nvim_win_set_buf(0, state.buf)
      vim.wo.wrap, vim.wo.number, vim.wo.cursorline = false, false, true
    end
  end)
end

return M
