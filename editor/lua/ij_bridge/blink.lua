-- A blink.cmp source backed by the Brain's streaming completion (SPEC.md §6).
--
-- LSP's `textDocument/completion` is one response per request and cannot express
-- IntelliJ's progressive results. blink.cmp's source API can: the first callback
-- shows the menu, later callbacks append. So the Brain answers `$/ij/completion`
-- with whatever it has the moment IntelliJ has anything, and streams the rest as
-- `$/ij/completionItems` notifications.
--
-- Register it with blink.cmp:
--
--   sources = {
--     default = { 'ij_bridge', ... },
--     providers = { ij_bridge = { module = 'ij_bridge.blink', name = 'IntelliJ', async = true } },
--   }
--
-- To keep IntelliJ's ranking (Passthrough) rather than blink's fuzzy score, also
-- set `fuzzy.sorts = { 'sort_text' }`: the Brain's `sortText` encodes IntelliJ's
-- order.
local uv = vim.uv
local log = require('ij_bridge.log')

local M = {}

--- Open Streams by id, so `$/ij/completionItems` can find who is waiting.
local streams = {}

--- A small cache of finished answers, so backspacing is immediate (FEATURES.md,
--- SPEC.md §12 Next).
---
--- IntelliJ's answer depends on the *whole* buffer, on every other Mirrored
--- buffer, and on the position, so the key is all of those: a hash of this
--- buffer's text plus the change ticks of the others. Typing then backspacing
--- returns to an earlier text state, which is exactly a hit; any edit anywhere
--- else changes the key, so a stale answer is never served. Short-lived and
--- small on purpose: a wrong answer is worse than waiting.
local CACHE_MAX = 8
local CACHE_TTL_NS = 5 * 1e9
local CACHE_MAX_BYTES = 1024 * 1024
local cache = { order = {}, map = {} }

--- Counters for the harness and for :IjBridge diagnostics.
M.stats = { requests = 0, hits = 0, interim = 0, late = 0 }

--- The last few `completionItem/resolve` round trips: label, milliseconds, error.
M.resolves = {}

function M.clear_cache()
  cache = { order = {}, map = {} }
end

local function cache_key(ctx, client)
  local lines = vim.api.nvim_buf_get_lines(ctx.bufnr, 0, -1, false)
  local text = table.concat(lines, '\n')
  if #text > CACHE_MAX_BYTES then
    return nil -- hashing a huge buffer on every keystroke costs more than it saves
  end
  local others = 0
  for _, b in ipairs(vim.lsp.get_buffers_by_client_id(client.id)) do
    if b ~= ctx.bufnr and vim.api.nvim_buf_is_valid(b) then
      others = others + vim.api.nvim_buf_get_changedtick(b) + b * 1e6
    end
  end
  return table.concat({ ctx.bufnr, ctx.cursor[1], ctx.cursor[2], vim.fn.sha256(text), others }, ':')
end

local function cache_get(key)
  local entry = key and cache.map[key]
  if not entry then
    return nil
  end
  if vim.uv.hrtime() - entry.at > CACHE_TTL_NS then
    cache.map[key] = nil
    return nil
  end
  return entry.items
end

local function cache_put(key, items)
  if not key then
    return
  end
  if not cache.map[key] then
    table.insert(cache.order, key)
    if #cache.order > CACHE_MAX then
      cache.map[table.remove(cache.order, 1)] = nil
    end
  end
  cache.map[key] = { items = items, at = vim.uv.hrtime() }
end

--- Timings of the most recent first response, read by the harness's speed gate
--- (SPEC.md §7). t0 is when the request was sent; `delivered` is when blink.cmp
--- had been handed the items - the closest the Editor can measure to "rendered"
--- without also timing blink's own drawing.
M.last = nil

--- How many Streams are still delivering. The harness waits on this: results are
--- always marked incomplete now, so that flag cannot tell "finished" from "still going".
function M.active()
  local n = 0
  for _ in pairs(streams) do
    n = n + 1
  end
  return n
end

--- The latest request per buffer, so a late answer for an older one can be handed
--- to it. State: { bufnr, row, col, line, callback, cancelled, answered, emitted }.
local newest = {}

--- Does `new` type on from `old`, within one word? Only then is the older answer
--- a fair stand-in for the newer one while it is awaited: same buffer, same line,
--- the same text up to the old cursor, and only keyword characters added since.
local function extends(old, new)
  if old.bufnr ~= new.bufnr or old.row ~= new.row or new.col < old.col then
    return false
  end
  if new.line:sub(1, old.col) ~= old.line:sub(1, old.col) then
    return false
  end
  return new.line:sub(old.col + 1, new.col):match('^[%w_]*$') ~= nil
end

local function item_key(item)
  return (item.label or '') .. '\0' .. tostring(item.insertText)
end

--- An answer arrived for a request the developer has since typed past (`abc.xy`
--- when they are now at `abc.xyz`). It is still right for the text it was asked
--- about, so: it has already been cached by the caller, and here it is shown for
--- the newer request, if that is still waiting, as an interim. blink.cmp filters
--- it against what is now typed; the newer request's own answer follows and
--- replaces what matters (only the items not already shown are appended).
local function interim(old, items)
  local n = newest[old.bufnr]
  if not n or n == old or n.cancelled or n.answered or not extends(old, n) then
    return
  end
  for _, item in ipairs(items) do
    n.emitted[item_key(item)] = true
  end
  M.stats.interim = M.stats.interim + 1
  log.debug('completion_interim', { row = n.row, col = n.col, items = #items })
  n.callback({ items = vim.deepcopy(items), is_incomplete_forward = true, is_incomplete_backward = true })
end

--- `items` without those an interim already put on screen.
local function not_yet_shown(state, items)
  if next(state.emitted) == nil then
    return items
  end
  local out = {}
  for _, item in ipairs(items) do
    if not state.emitted[item_key(item)] then
      out[#out + 1] = item
    end
  end
  return out
end

local function convert(items)
  local out = {}
  for i, item in ipairs(items) do
    out[i] = {
      label = item.label,
      -- IntelliJ's lookup elements carry no LSP kind; Text keeps blink content.
      kind = 1,
      insertText = item.insertText,
      insertTextFormat = 1,
      -- IntelliJ's ranking, preserved.
      sortText = item.sortText,
      detail = item.detail,
      labelDetails = item.labelDetails,
      -- Names the file and the element: what resolving needs, and nothing else.
      data = item.data,
    }
  end
  return out
end

local Source = {}
Source.__index = Source

function M.new(opts)
  return setmetatable({ opts = opts or {} }, Source)
end

function Source:enabled()
  return require('ij_bridge').client(0) ~= nil
end

function Source:get_trigger_characters()
  return { '.' }
end

function Source:get_completions(ctx, callback)
  local client = require('ij_bridge').client(ctx.bufnr)
  if not client then
    -- Dormant, or not attached: the callback must still be called once.
    callback({ items = {}, is_incomplete_forward = false, is_incomplete_backward = false })
    return function() end
  end

  local row, col = ctx.cursor[1], ctx.cursor[2]

  -- Immediate answer for a text state IntelliJ has already been asked about.
  -- Marked incomplete both ways, so the next keystroke, forward or back, comes
  -- back here rather than being filtered locally by blink's own fuzzy matching:
  -- IntelliJ's matching is the authority (Passthrough), the cache only skips the wait.
  local key = cache_key(ctx, client)
  local hit = cache_get(key)
  log.debug('completion_request', { row = row, col = col, key = key, hit = hit ~= nil, cached = vim.tbl_count(cache.map) })
  if hit then
    M.stats.hits = M.stats.hits + 1
    log.debug('completion_cache_hit', { row = row, col = col, items = #hit })
    callback({ items = vim.deepcopy(hit), is_incomplete_forward = true, is_incomplete_backward = true })
    return function() end
  end
  M.stats.requests = M.stats.requests + 1

  -- LSP counts UTF-16 code units; blink and nvim count bytes.
  local character = vim.str_utfindex(ctx.line, 'utf-16', col, false)
  local state = {
    cancelled = false, callback = callback, key = key, all = {}, answered = false, emitted = {},
    bufnr = ctx.bufnr, row = row, col = col, line = ctx.line,
  }
  newest[ctx.bufnr] = state
  local t0 = uv.hrtime()

  client:request('$/ij/completion', {
    textDocument = { uri = vim.uri_from_bufnr(ctx.bufnr) },
    position = { line = row - 1, character = character },
    context = { triggerKind = 1 },
  }, function(err, result)
    local t1 = uv.hrtime()
    if state.cancelled then
      -- The developer typed past this request, and blink.cmp dropped it. The
      -- answer is still right for the text it was asked about: keep it for a
      -- backspace, and show it to the request that replaced it.
      if result and not err and result.done and not result.isIncomplete and not result.degraded then
        M.stats.late = M.stats.late + 1
        log.debug('completion_late_answer', { row = state.row, col = state.col, items = #result.items })
        local items = convert(result.items)
        cache_put(state.key, vim.deepcopy(items))
        interim(state, items)
      end
      return
    end
    state.answered = true
    if err or not result then
      -- Includes RequestCancelled: a newer request superseded this one.
      callback({ items = {}, is_incomplete_forward = true, is_incomplete_backward = true })
      return
    end
    state.streamId = result.streamId
    local items = convert(result.items)
    vim.list_extend(state.all, items)
    if not result.done then
      streams[result.streamId] = state
    elseif not result.isIncomplete and not result.degraded then
      cache_put(state.key, vim.deepcopy(state.all)) -- a finished, complete answer
    end
    callback({
      items = not_yet_shown(state, items),
      -- Always incomplete: every keystroke asks IntelliJ again while blink keeps
      -- showing the previous list, filtered, so the menu never blanks.
      is_incomplete_forward = true,
      is_incomplete_backward = true,
    })
    local timings = result.timings or {}
    M.last = {
      items = #result.items,
      done = result.done,
      ij_first_items_ns = timings.ijFirstItemsNanos,
      brain_send_ns = timings.sendAfterReceiveNanos,
      to_response_ns = t1 - t0,
      to_delivered_ns = uv.hrtime() - t0,
    }
  end, ctx.bufnr)

  return function()
    state.cancelled = true
    if state.streamId then
      streams[state.streamId] = nil
      client:notify('$/ij/completionCancel', { streamId = state.streamId })
    end
  end
end

--- What accepting an item does, as IntelliJ does it: the import, the parentheses,
--- the lambda braces (`completionItem/resolve`). blink.cmp asks when an item is
--- accepted and applies the answer's `textEdit` and `additionalTextEdits`.
--- IntelliJ's answer is only right for the text the item was offered against, so
--- when the buffer has moved on (or anything else goes wrong) the item is handed
--- back unchanged and the plain word is inserted, as before.
function Source:resolve(item, callback)
  local t0 = uv.hrtime()
  M.resolves[#M.resolves + 1] = { label = item.label, asked = true, has_data = item.data ~= nil }
  local bufnr = vim.api.nvim_get_current_buf()
  local client = require('ij_bridge').client(bufnr)
  if not client or not item.data then
    callback(item)
    return function() end
  end
  local request_id
  local _, id = client:request('completionItem/resolve', {
    label = item.label,
    insertText = item.insertText,
    data = item.data,
  }, function(err, result)
    M.resolves[#M.resolves + 1] = { label = item.label, ms = (uv.hrtime() - t0) / 1e6, err = err and err.message }
    if #M.resolves > 20 then
      table.remove(M.resolves, 1)
    end
    if err or not result then
      log.warn('resolve_fell_back', { label = item.label, err = err and err.message })
      callback(item)
      return
    end
    callback(vim.tbl_extend('force', item, {
      textEdit = result.textEdit,
      additionalTextEdits = result.additionalTextEdits,
      insertTextFormat = result.insertTextFormat,
    }))
  end, bufnr)
  request_id = id
  return function()
    if request_id then
      client:cancel_request(request_id)
    end
  end
end

--- `$/ij/completionItems`: the rest of a Stream.
function M.on_items(err, params)
  if err or not params then
    return
  end
  local state = streams[params.streamId]
  if not state or state.cancelled then
    return
  end
  if params.superseded then
    streams[params.streamId] = nil
    return
  end
  if params.done then
    streams[params.streamId] = nil
  end
  local items = convert(params.items)
  vim.list_extend(state.all, items)
  state.answered = true
  if params.done and not params.isIncomplete then
    cache_put(state.key, vim.deepcopy(state.all)) -- a Stream that finished before the Cap
  end
  -- Always answer the closing message, even with nothing to append: it is the
  -- only way blink.cmp learns the Stream is over.
  if #items > 0 or params.done then
    state.callback({ items = not_yet_shown(state, items), is_incomplete_forward = true, is_incomplete_backward = true })
  end
end

return M
