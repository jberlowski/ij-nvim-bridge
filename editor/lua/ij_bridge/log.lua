-- The Editor's record of what happened, for finding out why something went wrong.
--
-- One JSON object per line in `stdpath('state')/ij-bridge.log`, rotated so it cannot grow
-- without bound. It records *metadata*: which message, how long it took, what failed and
-- why. Never the buffer's text. The Brain keeps a matching log; each Session has an id that
-- both print (`session`), so a line in one can be found in the other.
--
--   info   connections, Sessions, state changes, failures, anything slow
--   debug  (default) every message to and from the Brain, summarised
--   off
--
-- Level: the environment variable IJ_NVIM_BRIDGE_LOG, or `:IjBridge loglevel <level>`.
local uv = vim.uv

local M = {}

local ORDER = { off = 0, info = 1, debug = 2 }

M.level = os.getenv('IJ_NVIM_BRIDGE_LOG')
if not (M.level and ORDER[M.level:lower()]) then
  M.level = 'debug'
end
M.level = M.level:lower()

M.max_bytes = 5 * 1024 * 1024
local KEEP = 3

--- Fixed when the module loads: `vim.fn` is not available in every context a line is written from.
M.path = vim.fs.joinpath(vim.fn.stdpath('state'), 'ij-bridge.log')

--- A reply slower than this is worth a line even at `info`.
M.slow_ms = 250

local fd, size

local function open()
  if fd then
    return true
  end
  vim.fn.mkdir(vim.fs.dirname(M.path), 'p')
  fd = uv.fs_open(M.path, 'a', tonumber('600', 8))
  if not fd then
    return false
  end
  local stat = uv.fs_stat(M.path)
  size = stat and stat.size or 0
  return true
end

local function rotate()
  uv.fs_close(fd)
  fd = nil
  for n = KEEP - 1, 1, -1 do
    uv.fs_rename(M.path .. '.' .. n, M.path .. '.' .. (n + 1))
  end
  uv.fs_rename(M.path, M.path .. '.1')
end

local function stamp()
  local sec, usec = uv.gettimeofday()
  return os.date('%Y-%m-%dT%H:%M:%S', sec) .. ('.%03d'):format(math.floor(usec / 1000)) .. os.date('%z', sec)
end

function M.enabled(at)
  return (ORDER[M.level] or 0) >= ORDER[at]
end

--- @param at 'info'|'debug' the level this needs to be enabled at
--- @param label string what is written as `lvl`
local function write(at, label, event, fields)
  if not M.enabled(at) then
    return
  end
  pcall(function()
    if not open() then
      return
    end
    local line = vim.json.encode(vim.tbl_extend('force', fields or {}, { t = stamp(), lvl = label, ev = event })) .. '\n'
    uv.fs_write(fd, line)
    size = size + #line
    if size > M.max_bytes then
      rotate()
    end
  end)
end

function M.info(event, fields)
  write('info', 'info', event, fields)
end

function M.debug(event, fields)
  write('debug', 'debug', event, fields)
end

function M.warn(event, fields)
  write('info', 'warn', event, fields)
end

function M.error(event, fields)
  write('info', 'error', event, fields)
end

--- The last `n` lines of a file, or nil.
function M.tail(path, n)
  local ok, lines = pcall(vim.fn.readfile, path)
  if not ok then
    return nil
  end
  return vim.list_slice(lines, math.max(1, #lines - n + 1), #lines)
end

-- ------------------------------------------------------------------ the wire
--- What is worth saying about a message, and nothing that is the developer's code.
local function summary(method, params)
  local out = { method = method }
  if type(params) ~= 'table' then
    return out
  end
  local doc = params.textDocument
  if type(doc) == 'table' then
    out.uri = doc.uri
    out.version = doc.version
    if doc.text then
      out.length = #doc.text
    end
  end
  if type(params.contentChanges) == 'table' then
    out.changes = #params.contentChanges
  end
  if type(params.position) == 'table' then
    out.at = ('%s:%s'):format(params.position.line, params.position.character)
  end
  if type(params.items) == 'table' then
    out.items = #params.items
  end
  if params.state then
    out.state = params.state
  end
  if params.done ~= nil then
    out.done = params.done
  end
  return out
end

--- `vim.lsp.rpc.connect(sock)`, with every request, reply and notification recorded.
--- Neovim's own client runs on top of it unchanged.
function M.traced_connect(sock)
  local connect = vim.lsp.rpc.connect(sock)
  return function(dispatchers)
    local traced = vim.tbl_extend('force', {}, dispatchers)
    local on_notification = dispatchers.notification
    traced.notification = function(method, params)
      M.debug('recv', summary(method, params))
      return on_notification(method, params)
    end

    local client = connect(traced)
    local request, notify = client.request, client.notify

    client.request = function(method, params, callback, notify_reply_callback)
      local started = uv.hrtime()
      local fields = summary(method, params)
      local ok, id = request(method, params, function(err, result, ...)
        local ms = (uv.hrtime() - started) / 1e6
        local reply = vim.tbl_extend('force', fields, { id = id, ms = math.floor(ms * 10) / 10 })
        if err then
          reply.code, reply.message = err.code, err.message
          M.warn('response', reply)
        elseif ms > M.slow_ms then
          M.info('slow_response', reply)
        elseif M.enabled('debug') then
          if type(result) == 'table' then
            reply.n = result.items and #result.items or (vim.islist(result) and #result or nil)
          end
          M.debug('response', reply)
        end
        return callback(err, result, ...)
      end, notify_reply_callback)
      fields.id = id
      if not ok then
        M.warn('request_not_sent', fields)
      elseif M.enabled('debug') then
        M.debug('request', fields)
      end
      return ok, id
    end

    client.notify = function(method, params)
      if M.enabled('debug') then
        M.debug('send', summary(method, params))
      end
      return notify(method, params)
    end
    return client
  end
end

return M
