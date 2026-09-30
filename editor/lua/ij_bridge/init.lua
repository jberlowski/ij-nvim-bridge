-- The Editor half of the Bridge (SPEC.md §4.1): connects, asks, owns the text.
--
-- A Session is a plain LSP client over the Brain's unix socket, so Neovim's own
-- machinery does the heavy lifting: incremental didChange, didSave, and the
-- save handshake (`textDocument/willSaveWaitUntil` is awaited before a write,
-- after pending changes are flushed - SPEC.md §5.4). What this file adds is the
-- Bridge's policy: when to be Dormant, and which buffers are Mirrored.
local registry = require('ij_bridge.registry')
local log = require('ij_bridge.log')

local M = {}

M.name = 'ij-bridge'

--- How many `$/ij/status` notifications have arrived: each clears the completion cache.
M.status_events = 0

--- The Brain's last announced state per Session (SPEC.md §8), by client id.
M.states = {}

--- What each Session's Brain said about itself at `initialize`: its session id (the same one its
--- log prints) and where its log is. By client id.
M.sessions = {}

--- Buffers that *should* be Mirrored: attached and not released. This is what a
--- reconnect must restore, and how a buffer whose Session died is told apart from
--- one that was never attached.
M.attached = {}

--- Buffers in a Project Root whose file does not exist on disk yet: attached by their first write.
M.unwritten = {}

--- Project Roots for which an IntelliJ has been started from here and whose Brain has not appeared yet:
--- root -> { timer }.
M.starting = {}

--- The options given to `setup`.
M.opts = {}

--- Project Roots whose Session died unexpectedly and are being retried:
--- root -> { attempt = n }.
M.offline = {}

local leaving = false

--- A short trail of connection events, for `:IjBridge` and for tests to print
--- when a reconnect misbehaves. Bounded: it must never grow.
M.events = {}
local function note(what)
  log.info('conn', { what = what })
  table.insert(M.events, ('%.1f %s'):format(vim.uv.hrtime() / 1e9 % 10000, what))
  if #M.events > 60 then
    table.remove(M.events, 1)
  end
end

local reasons = {
  import = 'importing project',
  indexing = 'indexing',
  model = 'loading project model',
}

--- Called for every `$/ij/status` notification.
function M.on_status(client_id, params)
  M.states[client_id] = params
  log.info('status', { client = client_id, state = params.state, reason = params.reason })
  M.status_events = M.status_events + 1
  -- What IntelliJ would have said may change when it stops or finishes indexing.
  require('ij_bridge.blink').clear_cache()
  vim.api.nvim_exec_autocmds('User', { pattern = 'IjBridgeStatus', modeline = false, data = params })
  vim.cmd.redrawstatus()
end

--- The Brain's state for the buffer's Session, or nil when Dormant / not attached.
function M.status(buf)
  local client = M.client(buf or 0)
  return client and M.states[client.id] or nil
end

--- For a statusline. Empty when Dormant, so it costs nothing outside a project.
--- Indexing is always visible and never silent: the developer must be able to see
--- why completion and diagnostics have gone quiet.
---   lualine: sections = { lualine_x = { require('ij_bridge').statusline } }
function M.statusline()
  local status = M.status(0)
  if not status then
    local name = vim.api.nvim_buf_get_name(0)
    for root in pairs(M.starting) do
      if name == root or name:sub(1, #root + 1) == root .. '/' then
        return 'IJ: starting'
      end
    end
    -- A buffer that should be Mirrored but has no Session: the Brain went away.
    local buf = vim.api.nvim_get_current_buf()
    return (M.attached[buf] and not M.client(buf)) and 'IJ: disconnected' or ''
  end
  if status.state == 'Ready' then
    return 'IJ'
  end
  return 'IJ: ' .. (reasons[status.reason] or 'not ready')
end

local function buftype_ok(buf)
  return vim.api.nvim_buf_is_valid(buf)
    and vim.bo[buf].buftype == ''
    and vim.api.nvim_buf_get_name(buf) ~= ''
end

--- The Session serving `buf`, or nil when it is Dormant or not attached.
function M.client(buf)
  return vim.lsp.get_clients({ bufnr = buf or 0, name = M.name })[1]
end

local function capabilities()
  local caps = vim.lsp.protocol.make_client_capabilities()
  local ok, blink = pcall(require, 'blink.cmp')
  if ok and blink.get_lsp_capabilities then
    caps = blink.get_lsp_capabilities(caps)
  end
  return caps
end

--- A Session for `root` that is up and not shutting down, if there is one.
local function live_client(root)
  for _, client in ipairs(vim.lsp.get_clients({ name = M.name })) do
    if client.config.root_dir == root and not client:is_stopped() then
      return client
    end
  end
end

--- Connect `buf` to the Brain serving it.
local function connect(buf, entry)
  note('connect buf ' .. buf)
  local id = vim.lsp.start({
    name = M.name,
    cmd = log.traced_connect(entry.sock),
    root_dir = entry.root,
    capabilities = capabilities(),
    -- Opt-in (FEATURES.md §6c): off unless asked for, since it changes what `:w` means. The Brain
    -- reads this once, at `initialize`, and answers every `willSaveWaitUntil` accordingly for the
    -- Session's own life; Neovim's own core applies whatever edits come back before the write, the
    -- same mechanism the version-ack handshake (SPEC.md §5.4) already rides.
    init_options = { formatOnSave = M.opts.format_on_save == true },
    handlers = {
      ['$/ij/completionItems'] = function(err, params)
        require('ij_bridge.blink').on_items(err, params)
      end,
      ['$/ij/task/output'] = function(_, params)
        require('ij_bridge.tasks').on_output(params)
      end,
      ['$/ij/task/status'] = function(_, params)
        require('ij_bridge.tasks').on_status(params)
      end,
      ['$/ij/task/finished'] = function(_, params)
        require('ij_bridge.tasks').on_finished(params)
      end,
      ['$/ij/runConfiguration/output'] = function(_, params)
        require('ij_bridge.runconfigs').on_output(params)
      end,
      ['$/ij/runConfiguration/status'] = function(_, params)
        require('ij_bridge.runconfigs').on_status(params)
      end,
      ['$/ij/runConfiguration/finished'] = function(_, params)
        require('ij_bridge.runconfigs').on_finished(params)
      end,
      ['$/ij/runnables'] = function(_, params)
        require('ij_bridge.runnables').on_runnables(params)
      end,
      ['$/ij/run/output'] = function(_, params)
        require('ij_bridge.test_run').on_output(params)
      end,
      ['$/ij/test/status'] = function(_, params)
        require('ij_bridge.test_run').on_status(params)
      end,
      ['$/ij/run/finished'] = function(_, params)
        require('ij_bridge.test_run').on_finished(params)
      end,
      ['$/ij/status'] = function(_, params, ctx)
        if params then
          require('ij_bridge').on_status(ctx.client_id, params)
        end
      end,
    },
    -- A peer that dies with data unread is reset, not closed: Neovim reports a READ_ERROR
    -- and leaves the client standing, so on_exit never comes and the Bridge would go on
    -- believing in a Brain that is gone. A broken transport is a lost connection.
    on_init = function(client, result)
      local info = result and result.serverInfo or {}
      M.sessions[client.id] = info
      log.info('session', { client = client.id, session = info.session, brain_log = info.log, brain = info.version })
    end,
    on_error = function(code, err)
      log.warn('transport_error', { code = code, err = tostring(err) })
      if code ~= vim.lsp.rpc.client_errors.READ_ERROR then
        return
      end
      note('read error ' .. tostring(err))
      vim.schedule(function()
        for _, client in ipairs(vim.lsp.get_clients({ name = M.name })) do
          if client.config.root_dir == entry.root and not client:is_stopped() then
            client:stop(true)
          end
        end
        M.lost(entry.root)
      end)
    end,
    on_exit = function(code, signal, client_id)
      note(('exit client %d code=%s signal=%s'):format(client_id, tostring(code), tostring(signal)))
      M.states[client_id] = nil
      M.sessions[client_id] = nil
      if not leaving then
        vim.schedule(function()
          M.lost(entry.root)
        end)
      end
    end,
  }, { bufnr = buf })
  if id then
    M.attached[buf] = true
    -- vim.lsp.start reuses a running Session, and can hand the buffer to one that
    -- is already on its way out. Confirm it took, and repair it if it did not.
    vim.defer_fn(function()
      if M.attached[buf] and vim.api.nvim_buf_is_valid(buf) and not M.client(buf) then
        local client = live_client(entry.root)
        if client then
          vim.lsp.buf_attach_client(buf, client.id)
        end
      end
    end, 300)
  end
  return id
end

--- Ensure `buf` is Mirrored, if it belongs to a Project Root at all.
--- Returns the client id, or nil for Dormant (or while the Brain is away: the
--- reconnect loop owns that case).
function M.attach(buf)
  if not buftype_ok(buf) then
    return nil
  end
  local entry = registry.resolve(vim.api.nvim_buf_get_name(buf))
  if not entry then
    log.debug('dormant', { buf = buf, file = vim.api.nvim_buf_get_name(buf) })
    return nil -- Dormant: no message, no latency, no surprises
  end
  if not vim.uv.fs_stat(vim.api.nvim_buf_get_name(buf)) then
    -- A file that does not exist yet has no meaning to the IDE, and cannot be opened there:
    -- it is attached by its first write.
    M.unwritten[buf] = true
    log.debug('unwritten', { buf = buf, file = vim.api.nvim_buf_get_name(buf) })
    return nil
  end
  log.debug('attach', { buf = buf, file = vim.api.nvim_buf_get_name(buf), root = entry.root })
  if M.offline[entry.root] then
    if live_client(entry.root) then
      M.offline[entry.root] = nil -- a Session is up after all: not offline
    else
      M.attached[buf] = true -- remember it, so the reconnect brings it back
      return nil
    end
  end
  return connect(buf, entry)
end

-- ----------------------------------------------------------------- reconnect
-- The Brain can go away: IntelliJ quits or restarts, the socket drops. Neovim
-- must keep working (`:w` must never fail or block because of the Bridge), say
-- plainly that it is disconnected, and come back by itself - re-sending every
-- unsaved buffer, since the new Brain knows nothing.

local function under_root(buf, root)
  local name = vim.api.nvim_buf_get_name(buf)
  return name == root or name:sub(1, #root + 1) == root .. '/'
end

local function buffers_of(root)
  local out = {}
  for buf in pairs(M.attached) do
    -- Loaded, not merely valid: a hidden or unloaded buffer cannot take a Session,
    -- and a Session with no buffer exits at once - which would read as another
    -- lost connection, forever.
    if vim.api.nvim_buf_is_valid(buf) and vim.api.nvim_buf_is_loaded(buf) and under_root(buf, root) then
      table.insert(out, buf)
    else
      M.attached[buf] = nil
    end
  end
  return out
end

local function retry_in(root, attempt)
  -- 0.5 s, 1 s, 2 s, 4 s, 8 s, then every 10 s.
  local delay = math.min(10000, 500 * 2 ^ attempt)
  vim.defer_fn(function()
    M.reconnect(root)
  end, delay)
end

--- A Session for `root` ended without being asked to. Idempotent: several
--- buffers share one Session, and every exit reports it.
function M.lost(root)
  note('lost ' .. root .. (live_client(root) and ' (live client exists)' or ''))
  if leaving or M.offline[root] or live_client(root) then
    return -- already known, or another Session for the root replaced the one that ended
  end
  if #buffers_of(root) == 0 then
    return -- nothing was Mirrored, nothing to restore
  end
  M.offline[root] = { attempt = 0 }
  vim.cmd.redrawstatus()
  vim.api.nvim_exec_autocmds('User', { pattern = 'IjBridgeStatus', modeline = false,
    data = { state = 'Disconnected' } })
  retry_in(root, 0)
end

function M.reconnect(root)
  note('reconnect ' .. root .. ' attempt ' .. tostring((M.offline[root] or {}).attempt))
  local state = M.offline[root]
  if not state or leaving then
    return
  end
  local bufs = buffers_of(root)
  if #bufs == 0 then
    M.offline[root] = nil
    return
  end
  local entry = registry.resolve(vim.api.nvim_buf_get_name(bufs[1]))
  if entry and entry.root == root then
    -- Stay marked offline while connecting: a refused connection exits at once and
    -- calls lost() again, which must not start a second retry loop.
    for _, buf in ipairs(bufs) do
      connect(buf, entry)
    end
    vim.defer_fn(function()
      for _, client in ipairs(vim.lsp.get_clients({ name = M.name })) do
        if client.config.root_dir == root and not client:is_stopped() then
          M.offline[root] = nil -- it held
          require('ij_bridge.blink').clear_cache()
          vim.cmd.redrawstatus()
          return
        end
      end
      state.attempt = state.attempt + 1
      retry_in(root, state.attempt)
    end, 1500)
  else
    state.attempt = state.attempt + 1
    retry_in(root, state.attempt)
  end
end

--- Tell the Brain which buffer is active (FEATURES.md §9). Its Mirror becomes the
--- selected tab in the IDE, which is what makes the daemon analyse it. A buffer
--- attached just now needs no message: its didOpen selects it.
local function focus(buf)
  local client = M.client(buf)
  if client then
    client:notify('$/ij/focus', { textDocument = { uri = vim.uri_from_bufnr(buf) } })
  end
end

--- Release the Mirror for `buf`: didClose is sent by detaching.
function M.detach(buf)
  log.debug('detach', { buf = buf })
  M.attached[buf] = nil
  for _, client in ipairs(vim.lsp.get_clients({ bufnr = buf, name = M.name })) do
    vim.lsp.buf_detach_client(buf, client.id)
  end
end

--- SPEC.md §5.2: Mirror Set = { active buffer } U { every buffer with unsaved
--- changes }. Unsaved buffers stay Mirrored because the Brain would otherwise
--- answer from a stale disk copy - "cannot resolve" for a method just added to
--- the other file. A buffer that is left *and clean* is released.
local function release_if_clean(buf)
  if buftype_ok(buf) and not vim.bo[buf].modified then
    M.detach(buf)
  end
end

--- The Session that serves `dir`, for a request about a place rather than a buffer.
local function client_for(dir)
  local here = M.client(0)
  if here then
    return here
  end
  for _, client in ipairs(vim.lsp.get_clients({ name = M.name })) do
    local root = client.config.root_dir
    if root and (dir == root or dir:sub(1, #root + 1) == root .. '/') and not client:is_stopped() then
      return client
    end
  end
end

local KINDS = {
  kotlin = { 'class', 'interface', 'enum', 'object', 'dataClass', 'file' },
  java = { 'class', 'interface', 'enum', 'record', 'annotation' },
}

--- A new class, interface, enum or record, from IntelliJ's own file templates, with the right
--- `package` line for where it goes. The Brain says what the file should hold; this writes it,
--- and the first write is what makes the IDE see it.
---
--- @param opts { dir?: string, name: string, template?: string, language?: 'kotlin'|'java' }
--- @param callback? fun(path: string, result: table)
function M.new_file(opts, callback)
  local dir = opts.dir or vim.fs.dirname(vim.api.nvim_buf_get_name(0))
  local client = client_for(dir)
  if not client then
    vim.notify('ij-bridge: no IntelliJ is serving ' .. dir, vim.log.levels.WARN)
    return
  end
  local language = opts.language or (vim.bo.filetype == 'java' and 'java' or 'kotlin')
  client:request('$/ij/newFile', {
    directory = vim.uri_from_fname(dir),
    name = opts.name,
    template = opts.template or 'class',
    language = language,
  }, function(err, result)
    if err or not result then
      vim.notify('ij-bridge: ' .. (err and err.message or 'no answer'), vim.log.levels.WARN)
      return
    end
    local path = vim.uri_to_fname(result.uri)
    vim.fn.mkdir(vim.fs.dirname(path), 'p')
    vim.cmd.edit(vim.fn.fnameescape(path))
    vim.api.nvim_buf_set_lines(0, 0, -1, false, vim.split((result.text:gsub('\n$', '')), '\n', { plain = true }))
    vim.cmd.write()
    log.info('new_file', { path = path, package = result.package, template = result.template })
    if callback then
      callback(path, result)
    end
  end, 0)
end

--- `:IjBridge new [dir]`: asks what and what to call it.
function M.new_file_interactive(dir)
  local language = vim.bo.filetype == 'java' and 'java' or 'kotlin'
  vim.ui.select(KINDS[language], { prompt = 'New ' .. language .. ' file' }, function(kind)
    if not kind then
      return
    end
    vim.ui.input({ prompt = kind .. ' name: ' }, function(name)
      if name and name ~= '' then
        M.new_file({ dir = dir ~= '' and dir or nil, name = name, template = kind, language = language })
      end
    end)
  end)
end

-- ------------------------------------------------------------ starting an IntelliJ
-- Only when asked (`:IjBridge open`, `<leader>ao`), never by itself. The first `idea <root>` process
-- keeps its terminal for as long as it lives, and a later `idea <root>` only asks that process to
-- open the project (or a file), so the command is started detached and never waited for: it must not
-- lock Neovim's terminal, and it must not die with Neovim.

local ROOT_MARKERS = { '.idea', 'settings.gradle.kts', 'settings.gradle', 'pom.xml', 'build.gradle.kts', 'build.gradle', '.git' }

--- The project a path belongs to: the nearest directory above it that looks like one.
function M.project_root(path)
  path = (path and path ~= '') and path or vim.uv.cwd()
  local start = vim.uv.fs_stat(path) and path or vim.fs.dirname(path)
  return vim.fs.root(start, ROOT_MARKERS)
end

local function under(name, root)
  return name == root or name:sub(1, #root + 1) == root .. '/'
end

--- Attach every loaded buffer under `root`: an IntelliJ has just started serving it.
local function attach_under(root)
  for _, buf in ipairs(vim.api.nvim_list_bufs()) do
    if vim.api.nvim_buf_is_loaded(buf) and under(vim.api.nvim_buf_get_name(buf), root) then
      M.attach(buf)
    end
  end
end

--- Start the IntelliJ that serves the project of `path` (default: this buffer), unless one does.
--- @param path? string
--- @param callback? fun(ok: boolean, root: string) called when it is serving, or when waiting was given up
function M.open_ide(path, callback)
  local root = M.project_root(path or vim.api.nvim_buf_get_name(0))
  if not root then
    vim.notify('ij-bridge: this does not look like a project (no .idea, Gradle, Maven or Git root above it)', vim.log.levels.WARN)
    return
  end
  local serving = registry.resolve(root)
  if serving then
    vim.notify('ij-bridge: IntelliJ already serves ' .. serving.root)
    attach_under(serving.root)
    if callback then callback(true, serving.root) end
    return
  end
  if M.starting[root] then
    vim.notify('ij-bridge: IntelliJ is already starting for ' .. root)
    return
  end
  -- Rarely just `idea`: a Toolbox install's launcher script lives under the user's home directory
  -- and is not on PATH, and on macOS the launcher is inside the .app bundle. That path is specific
  -- to the machine (and often the user), so it does not belong hardcoded in a shared init.lua: the
  -- environment variable lets it be set once per machine instead. `setup{ idea_cmd = ... }` wins if given.
  local command = M.opts.idea_cmd or vim.env.IJ_NVIM_BRIDGE_IDEA_CMD or 'idea'
  if vim.fn.executable(command) ~= 1 then
    vim.notify(('ij-bridge: %s is not on the PATH (set idea_cmd in setup, or the IJ_NVIM_BRIDGE_IDEA_CMD environment variable)')
      :format(command), vim.log.levels.ERROR)
    return
  end

  -- `nohup ... &` in a shell that ends at once: the process is left to itself.
  vim.system({ 'sh', '-c', 'nohup "$0" "$1" >/dev/null 2>&1 &', command, root }, { detach = true })
  log.info('open_ide', { root = root, command = command })
  vim.notify('ij-bridge: starting IntelliJ for ' .. root)

  local timer = vim.uv.new_timer()
  local waited = 0
  M.starting[root] = { timer = timer }
  vim.cmd.redrawstatus()
  timer:start(1000, 1000, vim.schedule_wrap(function()
    waited = waited + 1
    local entry = registry.resolve(root)
    if entry or waited >= (M.opts.open_timeout or 300) then
      timer:stop()
      timer:close()
      M.starting[root] = nil
      if entry then
        log.info('open_ide_ready', { root = entry.root, seconds = waited })
        vim.notify('ij-bridge: IntelliJ is serving ' .. entry.root)
        attach_under(entry.root)
      else
        log.warn('open_ide_gave_up', { root = root, seconds = waited })
        vim.notify('ij-bridge: gave up waiting for IntelliJ to serve ' .. root, vim.log.levels.WARN)
      end
      vim.cmd.redrawstatus()
      if callback then callback(entry ~= nil, root) end
    end
  end))
end

--- What `:IjBridge` says about the current buffer.
function M.show_status()
  local buf = vim.api.nvim_get_current_buf()
  local entry = registry.resolve(vim.api.nvim_buf_get_name(buf))
  if not entry and M.attached[buf] then
    print('ij-bridge: disconnected; the Brain is not running, retrying')
  elseif not entry then
    print('ij-bridge: Dormant (no Project Root matches this buffer)')
  elseif M.client(buf) then
    local status = M.status(buf)
    local state = not status and 'state not reported yet'
      or status.state == 'Ready' and 'ready'
      or (reasons[status.reason] or 'not ready')
    print(('ij-bridge: attached to %s (%s), %s'):format(entry.root, entry.ide or '?', state))
  elseif M.attached[buf] then
    print(('ij-bridge: disconnected from %s; reconnecting'):format(entry.root or '?'))
  else
    print(('ij-bridge: %s is serving this buffer, but it is not attached'):format(entry.root))
  end
end

--- Where the Brain that serves the current buffer keeps its log: as it said at connect,
--- or by the convention it names its file with, for one not connected yet.
function M.brain_log_path()
  for _, info in pairs(M.sessions) do
    if info.log then
      return info.log
    end
  end
  local entry = registry.resolve(vim.api.nvim_buf_get_name(0))
  local hash = entry and entry.sock and entry.sock:match('([^/]+)%.sock$')
  if hash then
    local state = os.getenv('XDG_STATE_HOME')
    state = (state and state ~= '') and state or vim.fs.joinpath(vim.uv.os_homedir(), '.local', 'state')
    return vim.fs.joinpath(state, 'ij-nvim-bridge', 'brain-' .. hash .. '.log')
  end
end

--- Both logs' level. The Brain's is asked, so that what is recorded on the two sides agrees.
function M.set_log_level(level)
  if level ~= 'off' and level ~= 'info' and level ~= 'debug' and level ~= 'trace' then
    print('ij-bridge: log level is one of off, info, debug, trace (trace records payloads, which contain code)')
    return
  end
  log.level = (level == 'trace') and 'debug' or level
  local client = M.client(0)
  if client then
    client:request('$/ij/log', { level = level }, function(err, result)
      print(err and ('ij-bridge: the Brain refused: ' .. err.message)
        or ('ij-bridge: log level ' .. level .. '; Brain log ' .. (result and result.path or '?')))
    end, 0)
  else
    print('ij-bridge: Editor log level ' .. log.level .. ' (no Brain connected)')
  end
end

--- Everything worth pasting into a bug report, in one scratch buffer: versions, the state of the
--- Bridge, and the tail of both logs.
function M.report()
  local out = { '# ij-nvim-bridge report', '' }
  local v = vim.version()
  local uname = vim.uv.os_uname()
  vim.list_extend(out, {
    ('nvim %d.%d.%d, %s %s %s'):format(v.major, v.minor, v.patch, uname.sysname, uname.release, uname.machine),
    'buffer: ' .. vim.api.nvim_buf_get_name(0),
    'statusline: ' .. M.statusline(),
    'offline: ' .. vim.inspect(M.offline),
    'attached buffers: ' .. vim.inspect(vim.tbl_keys(M.attached)),
    'sessions: ' .. vim.inspect(M.sessions),
    'states: ' .. vim.inspect(M.states),
    'editor log: ' .. log.path .. ' (level ' .. log.level .. ')',
    'brain log: ' .. tostring(M.brain_log_path()),
    '',
    '## recent connection events',
  })
  vim.list_extend(out, M.events)
  vim.list_extend(out, { '', '## editor log (last 60 lines)' })
  vim.list_extend(out, log.tail(log.path, 60) or { '(none)' })
  local brain = M.brain_log_path()
  vim.list_extend(out, { '', '## brain log (last 60 lines)' })
  vim.list_extend(out, brain and log.tail(brain, 60) or { '(not readable from here)' })
  out = vim.split(table.concat(out, '\n'), '\n', { plain = true }) -- inspected tables span lines
  vim.cmd.new()
  vim.bo.buftype, vim.bo.bufhidden, vim.bo.swapfile, vim.bo.filetype = 'nofile', 'wipe', false, 'markdown'
  vim.api.nvim_buf_set_lines(0, 0, -1, false, out)
  return out
end

--- @param opts? { prefix?: string, keys?: boolean, sections?: boolean, idea_cmd?: string, open_timeout?: integer } miscellaneous keys under `prefix` (default `<leader>a`), the others by section (`sections = false` to skip them); `keys = false` binds nothing. `idea_cmd` (default `idea`, or the `IJ_NVIM_BRIDGE_IDEA_CMD` environment variable if that is set): the command `:IjBridge open` runs to start IntelliJ. The environment variable is the better place for it when `init.lua` is shared across machines, since the right value (a Toolbox script, a `.app` bundle's launcher) is often machine- or user-specific.
function M.setup(opts)
  M.opts = opts or {}
  local group = vim.api.nvim_create_augroup('IjBridge', { clear = true })

  -- A clean buffer that was left is released once a *file* buffer takes over. Left for an explorer or
  -- a terminal (nothing the IDE could show), it stays Mirrored and selected: releasing it closes its
  -- tab, and IntelliJ then jumps to whichever tab is next.
  local left
  vim.api.nvim_create_autocmd('BufEnter', {
    group = group,
    callback = function(args)
      if buftype_ok(args.buf) then
        if left and left ~= args.buf then
          release_if_clean(left)
        end
        left = nil
      end
      local was_attached = M.client(args.buf) ~= nil
      M.attach(args.buf)
      if was_attached then
        focus(args.buf)
      end
    end,
  })
  vim.api.nvim_create_autocmd('BufLeave', {
    group = group,
    callback = function(args)
      if buftype_ok(args.buf) then
        left = args.buf
      end
    end,
  })
  -- A buffer saved while it is not the active one has just become clean.
  vim.api.nvim_create_autocmd('BufWritePost', {
    group = group,
    callback = function(args)
      if M.unwritten[args.buf] then
        M.unwritten[args.buf] = nil
        M.attach(args.buf)
        return
      end
      if args.buf ~= vim.api.nvim_get_current_buf() then
        release_if_clean(args.buf)
      end
    end,
  })

  vim.api.nvim_create_autocmd('VimLeavePre', {
    group = group,
    callback = function()
      leaving = true -- quitting is not a lost connection
    end,
  })
  vim.api.nvim_create_autocmd({ 'BufWipeout', 'BufUnload' }, {
    group = group,
    callback = function(args)
      M.unwritten[args.buf] = nil
      M.attached[args.buf] = nil
    end,
  })

  vim.api.nvim_create_user_command('IjBridge', function(opts)
    local sub, rest = opts.args:match('^(%S*)%s*(.*)$')
    if sub == '' or sub == 'status' then
      M.show_status()
    elseif sub == 'log' then
      vim.cmd.split(log.path)
    elseif sub == 'brainlog' then
      local path = M.brain_log_path()
      if path and vim.uv.fs_stat(path) then
        vim.cmd.split(path)
      else
        print('ij-bridge: no Brain log found' .. (path and (' at ' .. path) or ''))
      end
    elseif sub == 'loglevel' then
      M.set_log_level(rest)
    elseif sub == 'report' then
      M.report()
    elseif sub == 'tasks' then
      require('ij_bridge.tasks').tree()
    elseif sub == 'task' then
      require('ij_bridge.tasks').pick()
    elseif sub == 'taskstop' then
      require('ij_bridge.tasks').stop()
    elseif sub == 'taskrepeat' then
      require('ij_bridge.tasks').repeat_last()
    elseif sub == 'open' then
      M.open_ide(rest ~= '' and rest or nil)
    elseif sub == 'new' then
      M.new_file_interactive(rest)
    elseif sub == 'test' then
      require('ij_bridge.test_nav').go()
    elseif sub == 'keys' then
      print('ij-bridge keys:\n' .. table.concat(require('ij_bridge.keys').describe(), '\n'))
    else
      print('ij-bridge: unknown subcommand ' .. sub .. ' (status, log, brainlog, loglevel <off|info|debug|trace>, report, new [dir], open [dir], test, keys, tasks, task, taskstop, taskrepeat)')
    end
  end, {
    nargs = '?',
    complete = function()
      return { 'status', 'open', 'log', 'brainlog', 'loglevel', 'report', 'new', 'test', 'keys', 'tasks', 'task', 'taskstop', 'taskrepeat' }
    end,
    desc = 'Show whether this buffer is served by an IntelliJ Brain; see and change the logs',
  })

  require('ij_bridge.keys').setup(opts)

  M.attach(vim.api.nvim_get_current_buf())
end

return M
