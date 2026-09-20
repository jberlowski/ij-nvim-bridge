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

--- Timings of the most recent first response, read by the harness's speed gate
--- (SPEC.md §7). t0 is when the request was sent; `delivered` is when blink.cmp
--- had been handed the items - the closest the Editor can measure to "rendered"
--- without also timing blink's own drawing.
M.last = nil

--- How many Streams are still delivering. The harness waits on this: a Stream
--- that hit the Cap ends with is_incomplete_forward = true, so that flag cannot
--- be used to tell "finished" from "still going".
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
  -- LSP counts UTF-16 code units; blink and nvim count bytes.
  local character = vim.str_utfindex(ctx.line, 'utf-16', col, false)
  local state = { cancelled = false, callback = callback }
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
      callback({ items = {}, is_incomplete_forward = false, is_incomplete_backward = false })
      return
    end
    state.streamId = result.streamId
    if not result.done then
      streams[result.streamId] = state
    end
    callback({
      items = convert(result.items),
      -- Still streaming, or closed at the Cap: ask again on the next keystroke.
      is_incomplete_forward = (not result.done) or result.isIncomplete == true,
      is_incomplete_backward = false,
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
  -- Always answer the closing message, even with nothing to append: it is the
  -- only way blink.cmp learns the final is_incomplete flag.
  if #params.items > 0 or params.done then
    state.callback({
      items = convert(params.items),
      is_incomplete_forward = (not params.done) or params.isIncomplete == true,
      is_incomplete_backward = false,
    })
  end
end

return M
