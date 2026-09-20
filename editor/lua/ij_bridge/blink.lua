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
local CACHE_TTL_NS = 30 * 1e9
local CACHE_MAX_BYTES = 1024 * 1024
local cache = { order = {}, map = {} }

--- Counters for the harness and for :IjBridge diagnostics.
M.stats = { requests = 0, hits = 0 }

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
  if hit then
    M.stats.hits = M.stats.hits + 1
    callback({ items = vim.deepcopy(hit), is_incomplete_forward = true, is_incomplete_backward = true })
    return function() end
  end
  M.stats.requests = M.stats.requests + 1

  -- LSP counts UTF-16 code units; blink and nvim count bytes.
  local character = vim.str_utfindex(ctx.line, 'utf-16', col, false)
  local state = { cancelled = false, callback = callback, key = key, all = {} }
  local t0 = uv.hrtime()

  client:request('$/ij/completion', {
    textDocument = { uri = vim.uri_from_bufnr(ctx.bufnr) },
    position = { line = row - 1, character = character },
    context = { triggerKind = 1 },
  }, function(err, result)
    local t1 = uv.hrtime()
    if state.cancelled then
      return
    end
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
      items = items,
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
  if params.done and not params.isIncomplete then
    cache_put(state.key, vim.deepcopy(state.all)) -- a Stream that finished before the Cap
  end
  -- Always answer the closing message, even with nothing to append: it is the
  -- only way blink.cmp learns the Stream is over.
  if #items > 0 or params.done then
    state.callback({ items = items, is_incomplete_forward = true, is_incomplete_backward = true })
  end
end

return M
